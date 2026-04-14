"""
harmonization/rrf_national_top100.py
====================================
Generic Member-State RRF Article-9a "top 100 final recipients" adapter.

**Why this module exists.** Article 9a of the RRF regulation obliges
every Member State to publish a list of the 100 final recipients
receiving the highest amount of funding under their national Recovery
and Resilience Plan. Updates are required twice a year. Together
these 27 national lists are the cheapest path to closing plan audit
finding H7 for the 26 Member States that OpenCoesione doesn't cover
(Italy has a much richer project-level source in
``rrf_italia_domani.py``).

The lists are published in a variety of formats — some as HTML
tables, some as PDFs, some as Excel downloads, some embedded in a
Qlik Sense dashboard on the EU Scoreboard. This module handles the
HTML-table cases (the easiest) via a per-country parser function
registered in ``NATIONAL_PORTALS``. Each parser fetches the
portal page, extracts a list of ``RrfRecipient`` records, and
returns them for a common-schema mapping step.

**Current coverage.**

    DE   — Bundesfinanzministerium (BMF) plain HTML table.
           Verified working: 100 rows × 4 columns (name,
           register identifier, EUR amount, affected measures).

**Stubs documented** (not yet implemented):

    ES   — planderecuperacion.gob.es PDF top-100. Implemented
           via pdfplumber text extraction + stateful regex.
           Verified against the 2025-01-24 publication.
    FR   — France 2030 / France Relance: data.gouv.fr carries
           per-department aggregates, not beneficiary-level. Not
           a 9a top-100 source.
    PT   — recuperarportugal.gov.pt: needs site navigation, no
           clean endpoint found in the initial survey.
    EL   — greece20.gov.gr carries an HTML list at
           ``/katalogos-me-toys-100-telikoys-apodektes-...``;
           pattern matches DE but Greek text requires
           transliteration.
    PL   — kpo.gov.pl: no top-100 endpoint located yet.
    RO   — mfe.gov.ro: not surveyed.
    EU   — ec.europa.eu/economy_finance/recovery-and-resilience-scoreboard
           aggregates all 27 MS lists but serves data via a Qlik
           Sense dashboard, which would require WebSocket engine
           reverse-engineering. Deferred.

New per-country parsers should follow the DE pattern: return
``list[RrfRecipient]`` and register in ``NATIONAL_PORTALS``.
Running without arguments processes every registered country and
writes ``standardized_RRF_NATIONAL_TOP100.csv``.

Plan audit: phase C item 20, "RRF national adapters".
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from .utils import COMMON_COLUMNS, apply_v2_columns, pack_originals

log = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None

try:
    from lxml import html as lxml_html
    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False


SOURCE_TAG = 'RRF_NAT_TOP100'
USER_AGENT = 'Mozilla/5.0 (research-project; EU subsidy analysis) Python/3.11'


@dataclass
class RrfRecipient:
    country: str
    name: str
    identifier: str | None
    amount_eur: float | None
    measures: str | None
    source_url: str
    last_updated: str | None = None  # raw string from portal


# ---------------------------------------------------------------------------
# Amount parsing (shared helper)
# ---------------------------------------------------------------------------

def _parse_eur_amount(raw: str) -> float | None:
    """Parse a European-formatted currency string into a float.

    Handles ``"509.959.880,15"`` (DE), ``"509,959,880.15"`` (EN),
    ``"509 959 880,15"`` (FR/nordics), and a bare ``"500000"``.
    Returns ``None`` on any parse failure.
    """
    if not raw:
        return None
    s = str(raw).strip()
    # Strip anything that isn't a digit, separator, minus, or whitespace.
    s = re.sub(r'[^\d\-.,\s]', '', s)
    s = re.sub(r'\s+', '', s)
    if not s:
        return None
    # Decide format by the trailing separator pattern.
    if re.match(r'^-?\d{1,3}(\.\d{3})+(,\d+)?$', s):
        s = s.replace('.', '').replace(',', '.')  # DE / IT / ES
    elif re.match(r'^-?\d{1,3}(,\d{3})+(\.\d+)?$', s):
        s = s.replace(',', '')                    # EN / US
    elif ',' in s and '.' not in s:
        s = s.replace(',', '.')                   # bare decimal comma
    try:
        return float(s)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# DE — Bundesfinanzministerium HTML table parser
# ---------------------------------------------------------------------------

DE_URL = (
    'https://www.bundesfinanzministerium.de/Content/DE/'
    'Standardartikel/Themen/Europa/DARP/top100-empfaenger.html'
)


def parse_de_top100(session) -> list[RrfRecipient]:
    """Fetch the BMF DARP top-100 page and parse its single table.

    The table has four columns:
        Name des Empfängers
        Kennung des Empfängers (register identifier)
        Erhaltener Gesamtbetrag (in Euro)
        Betroffene Maßnahmen (affected measures)

    Validated 2026-04-14: 100 data rows (first row is DB InfraGO AG
    at €510M).
    """
    if not _HAS_LXML:
        raise RuntimeError('lxml required for DE parser')
    r = session.get(DE_URL, timeout=30)
    r.raise_for_status()
    doc = lxml_html.fromstring(r.text)
    tables = doc.xpath('//table')
    if not tables:
        log.warning('  DE: no table found on BMF page')
        return []
    table = tables[0]
    rows = table.xpath('.//tr')
    if len(rows) < 2:
        log.warning(f'  DE: table has only {len(rows)} rows')
        return []

    # Try to extract a "last updated" date from the page.
    updated = None
    for el in doc.xpath("//*[contains(text(),'Stand:') or contains(text(),'Aktualisiert')]"):
        txt = el.text_content().strip()
        m = re.search(r'(\d{1,2}\.\d{1,2}\.\d{2,4})', txt)
        if m:
            updated = m.group(1)
            break

    recipients: list[RrfRecipient] = []
    for tr in rows[1:]:  # skip header
        cells = tr.xpath('.//td')
        if len(cells) < 4:
            continue
        name = ' '.join(cells[0].xpath('.//text()')).strip()
        identifier = ' '.join(cells[1].xpath('.//text()')).strip()
        amount_raw = ' '.join(cells[2].xpath('.//text()')).strip()
        measures = ' '.join(cells[3].xpath('.//text()')).strip()
        name = re.sub(r'\s+', ' ', name)
        identifier = re.sub(r'\s+', ' ', identifier)
        measures = re.sub(r'\s+', ' ', measures)
        amount_eur = _parse_eur_amount(amount_raw)
        if not name:
            continue
        recipients.append(RrfRecipient(
            country='DE',
            name=name,
            identifier=identifier or None,
            amount_eur=amount_eur,
            measures=measures or None,
            source_url=DE_URL,
            last_updated=updated,
        ))
    log.info(f'  DE: parsed {len(recipients)} recipients from BMF top-100')
    return recipients


# ---------------------------------------------------------------------------
# ES — planderecuperacion.gob.es PDF top-100
# ---------------------------------------------------------------------------

ES_URL = (
    'https://planderecuperacion.gob.es/sites/default/files/2025-02/'
    'Listado_100_mayores_perceptores_finales_Next_Generation_20250124.pdf'
)

# Spanish NIF: letter + 7 digits + letter, OR 8 digits + letter (DNI).
# Legal-entity NIFs for this list are overwhelmingly the letter+7+letter
# form (A/B/C/D/E/F/G/H/J/K/L/M/N/P/Q/R/S/U/V/W).
_ES_NIF_RE = re.compile(
    r'^([A-HJ-NP-SUVW]\d{7}[A-Z0-9])\s*(.+?)'
    r'(?:\s+(C\d{1,2})\s+(.*?))?'
    r'\s+([\d.,]+)\s*[€\uFFFD]?\s*$'
)


def parse_es_top100(session) -> list[RrfRecipient]:
    """Fetch the Spanish top-100 PDF and extract one row per entity.

    The PDF uses a stateful single-column layout where each entity has
    one header line (``NIF DENOMINATION Cxx component_name amount €``)
    followed by zero or more continuation lines (``Cxx component_name
    amount €``) for additional components. We aggregate all components
    for a given NIF into a single ``RrfRecipient`` with the summed
    amount. The first component's name becomes the representative
    ``measures`` field; downstream users who want the full component
    list should re-read the PDF via a dedicated component-level parser.
    """
    try:
        import pdfplumber
    except ImportError:
        log.warning('  ES parser needs pdfplumber')
        return []

    # Cache locally under data/cache/rrf_eu_scoreboard/ to avoid
    # re-downloading on every run.
    import sys as _sys
    repo_root = Path(__file__).resolve().parent.parent.parent
    cache_dir = repo_root / 'data' / 'cache' / 'rrf_eu_scoreboard'
    cache_dir.mkdir(parents=True, exist_ok=True)
    pdf_path = cache_dir / 'es_top100.pdf'
    if not pdf_path.exists():
        try:
            r = session.get(ES_URL, timeout=60)
            if r.status_code != 200 or r.content[:4] != b'%PDF':
                log.warning(f'  ES PDF download failed: {r.status_code}')
                return []
            pdf_path.write_bytes(r.content)
        except Exception as exc:
            log.warning(f'  ES PDF download error: {exc}')
            return []

    lines: list[str] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                t = page.extract_text() or ''
                for line in t.split('\n'):
                    line = line.strip()
                    if line:
                        lines.append(line)
    except Exception as exc:
        log.warning(f'  ES PDF parse failed: {exc}')
        return []

    # Walk the lines with state. A new entity starts whenever a line
    # begins with a NIF-looking token. Component continuations start
    # with a Cnn token. The amount format uses European locale
    # ``1.234.567,89``.
    _NIF_PREFIX = re.compile(r'^([A-HJ-NP-SUVW]\d{7}[A-Z0-9])(.*)$')
    _COMPONENT_PREFIX = re.compile(r'^(C\d{1,2})\s+(.*)$')
    _AMOUNT_TAIL = re.compile(r'(.+?)\s+([\d][\d.,]*)\s*[€\uFFFD]?\s*$')

    recipients: list[RrfRecipient] = []
    cur_nif: str | None = None
    cur_name: str | None = None
    cur_total: float = 0.0
    cur_measures: list[str] = []

    def _flush():
        nonlocal cur_nif, cur_name, cur_total, cur_measures
        if cur_nif and cur_name:
            recipients.append(RrfRecipient(
                country='ES',
                name=cur_name.strip(),
                identifier=f'NIF {cur_nif}',
                amount_eur=cur_total if cur_total else None,
                measures=' | '.join(cur_measures[:5]) if cur_measures else None,
                source_url=ES_URL,
                last_updated='2025-01-24',
            ))
        cur_nif = None
        cur_name = None
        cur_total = 0.0
        cur_measures = []

    for line in lines:
        nif_m = _NIF_PREFIX.match(line)
        if nif_m:
            # Flush the previous entity block before starting a new one.
            _flush()
            cur_nif = nif_m.group(1)
            rest = nif_m.group(2).strip()
            # The rest should be "DENOMINATION Cxx component amount €"
            # — extract the amount from the tail first.
            amt_m = _AMOUNT_TAIL.match(rest)
            if amt_m:
                head = amt_m.group(1).strip()
                amt = _parse_eur_amount(amt_m.group(2))
                if amt:
                    cur_total += amt
                comp_m = re.search(r'(C\d{1,2})\s+(.*)$', head)
                if comp_m:
                    cur_name = head[:comp_m.start()].strip()
                    cur_measures.append(f'{comp_m.group(1)} {comp_m.group(2)}'.strip())
                else:
                    cur_name = head
            else:
                cur_name = rest
            continue

        comp_m = _COMPONENT_PREFIX.match(line)
        if comp_m and cur_nif:
            amt_m = _AMOUNT_TAIL.match(line)
            if amt_m:
                body = amt_m.group(1).strip()
                amt = _parse_eur_amount(amt_m.group(2))
                if amt:
                    cur_total += amt
                cur_measures.append(body)
            continue

    _flush()
    log.info(f'  ES: parsed {len(recipients)} recipients from planderecuperacion top-100 PDF')
    return recipients


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

ParserFn = Callable[[object], list[RrfRecipient]]

NATIONAL_PORTALS: dict[str, dict] = {
    'DE': {
        'parser': parse_de_top100,
        'status': 'implemented',
        'url': DE_URL,
        'notes': 'BMF HTML table, 4 cols, updated twice yearly',
    },
    'ES': {
        'parser': parse_es_top100,
        'status': 'implemented',
        'url': ES_URL,
        'notes': 'planderecuperacion.gob.es PDF, aggregated per NIF across components',
    },
    'FR': {
        'parser': None,
        'status': 'aggregates_only',
        'url': 'https://www.data.gouv.fr/organizations/france-relance',
        'notes': 'Per-department aggregates; not Article 9a top-100 beneficiary data',
    },
    'PT': {
        'parser': None,
        'status': 'not_surveyed',
        'url': 'https://www.recuperarportugal.gov.pt/',
        'notes': 'Site navigation unclear; no top-100 endpoint located in initial survey',
    },
    'EL': {
        'parser': None,
        'status': 'html_stub',
        'url': 'https://greece20.gov.gr/katalogos-me-toys-100-telikoys-apodektes-me-tin-ypsiloteri-chrimatodotisi-apo-to-tameio-anakampsis-kai-anthektikotitas-top-100-final-recipients/',
        'notes': 'HTML list, Greek text; needs transliteration, pattern matches DE',
    },
    'PL': {
        'parser': None,
        'status': 'not_surveyed',
        'url': 'https://www.kpo.gov.pl/',
        'notes': 'No top-100 endpoint located',
    },
    'RO': {
        'parser': None,
        'status': 'not_surveyed',
        'url': None,
        'notes': 'mfe.gov.ro not surveyed',
    },
}


# ---------------------------------------------------------------------------
# Standardization — map RrfRecipient rows to COMMON_COLUMNS
# ---------------------------------------------------------------------------

def standardize(repo_root: Path, log: logging.Logger, countries: list[str] | None = None) -> pd.DataFrame:
    """Run every implemented parser and emit a COMMON_COLUMNS DataFrame."""
    if requests is None:
        log.error('requests not installed')
        return pd.DataFrame(columns=COMMON_COLUMNS)
    session = requests.Session()
    session.headers.update({'User-Agent': USER_AGENT})

    wanted = countries or list(NATIONAL_PORTALS.keys())
    all_recipients: list[RrfRecipient] = []
    for country_code in wanted:
        cfg = NATIONAL_PORTALS.get(country_code)
        if not cfg:
            log.warning(f'  unknown country: {country_code}')
            continue
        if cfg['parser'] is None:
            log.info(f'  {country_code}: parser not implemented ({cfg["status"]})')
            continue
        try:
            rows = cfg['parser'](session)
            all_recipients.extend(rows)
        except Exception as exc:
            log.error(f'  {country_code}: parser failed: {exc}')

    if not all_recipients:
        return pd.DataFrame(columns=COMMON_COLUMNS)

    # Map to common schema. IMPORTANT: assign a list-typed column first so
    # pandas grows the frame to the right length, THEN assign scalars.
    # Assigning a scalar to an empty ``pd.DataFrame()`` creates an empty
    # column that stays empty when subsequent list-assignments grow the
    # index — a subtle pandas footgun that silently drops `source`.
    import json
    out = pd.DataFrame({
        'source_record_id': [
            f'{r.country}_{i+1:03d}' for i, r in enumerate(all_recipients)
        ],
    })
    out['source'] = SOURCE_TAG
    out['granularity'] = 'entity'
    out['beneficiary_name'] = [r.name for r in all_recipients]
    out['country'] = [r.country for r in all_recipients]
    out['amount_eur'] = [r.amount_eur for r in all_recipients]
    out['amount_type'] = 'cumulative_disbursement'
    out['year'] = None  # Article-9a lists are cumulative as-of-publication
    out['sector_description'] = None
    out['nace_2digit'] = None
    out['description'] = [r.measures for r in all_recipients]
    out['overlap_flags'] = ''
    out['original_columns'] = [
        pack_originals({
            'identifier': r.identifier,
            'measures': r.measures,
            'source_url': r.source_url,
        }) for r in all_recipients
    ]

    out['programme'] = 'RRF / National Recovery and Resilience Plan'
    out['fund'] = 'RRF'
    out['programming_period'] = '2021-2027'
    out['instrument_subtype'] = None
    out['policy_domain'] = None

    out['year_paid'] = None
    out['flow_stage'] = 'expenditure'
    out['financial_instrument_class'] = 'grant'  # default; top-100 lists don't differentiate
    out['management_type'] = 'shared'
    out['legal_basis'] = 'Regulation (EU) 2021/241 Article 9a'
    out['budget_line_code'] = None
    out['budget_execution_type'] = None

    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    out['is_anonymised'] = False  # top-100 lists are by construction non-anonymised
    apply_v2_columns(out, fiscal_source_type='eu_borrowing', resolution_level='beneficiary')

    out['extra_fields_json'] = [
        json.dumps({
            'identifier': r.identifier or '',
            'measures': r.measures or '',
            'source_url': r.source_url,
            'last_updated': r.last_updated or '',
            'article_9a_rank': i + 1,
        }, ensure_ascii=False)
        for i, r in enumerate(all_recipients)
    ]

    log.info(f'  RRF_NAT_TOP100 standardised: {len(out)} rows across '
             f'{len({r.country for r in all_recipients})} countries')
    return out[COMMON_COLUMNS]


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--countries', nargs='*', default=None,
                   help='ISO-2 country codes to scrape (default: every implemented portal)')
    p.add_argument('--repo-root', type=Path, default=None)
    p.add_argument('--status', action='store_true',
                   help='Print per-country implementation status and exit')
    args = p.parse_args(argv)

    if args.status:
        print('\nRRF national top-100 portal status:\n')
        print(f'{"CC":4s} {"status":20s} {"notes"}')
        print('-' * 78)
        for cc, cfg in NATIONAL_PORTALS.items():
            print(f'{cc:4s} {cfg["status"]:20s} {cfg["notes"][:50]}')
        return

    repo_root = args.repo_root or Path(__file__).resolve().parent.parent.parent
    df = standardize(repo_root, log, countries=args.countries)
    if len(df) == 0:
        log.warning('No rows standardised — check the status table with --status')
        return
    out_dir = repo_root / 'data' / 'processed'
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / 'standardized_RRF_NAT_TOP100.csv'
    df.to_csv(out_path, index=False)
    log.info(f'Wrote {len(df)} rows to {out_path}')


if __name__ == '__main__':
    main()
