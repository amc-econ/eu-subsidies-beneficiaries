#!/usr/bin/env python3
"""
EIB Deep Scraper — structured multi-field extraction from project pages.
========================================================================

Extends the v2 promoter scraper (``eib_promoter_scraper.py``) with
aggressive extraction of every structured field present on an EIB
project page. Where v2 pulled only ``title`` and the first promoter,
this module extracts the full field set the page exposes:

    project_id              8-digit EIB reference
    title                   Project name
    status                  Signed / Under appraisal / Approved / ...
    release_date            Date the decision appeared on the EIB register
    country                 "Countries" field (comma-separated)
    location                Free-text location — often NUTS-3 / city
    sectors                 List (the label uses "Sector(s)")
    description             Full description prose
    objectives              Full objectives prose
    environmental_aspects   Full env aspects prose
    procurement             Free-text procurement statement
    comments                Free-text comments
    promoters               **list** of every promoter on the page
    proposed_finance_eur    The "Proposed EIB finance (Approximate
                            amount)" number, parsed to EUR
    total_cost_eur          The "Total cost" number, parsed to EUR
    signed_total_eur        The "Amount" number (cumulative signed)
    signatures              list of {date, amount_eur} tuples — one
                            per disbursement tranche
    related_doc_urls        URLs to attached PDFs / summary sheets
    link_to_source          External "Link to source" URL if any
    url                     Canonical EIB page URL
    scraped_at              UTC timestamp of the fetch
    schema_version          "eib_deep_v1"

Every field is optional — missing cells are ``None``. The page layout
is stable enough in 2026 to rely on the ``eib-typography__*`` CSS
hooks plus label-to-value sibling traversal, but the extractor also
falls back to regex if lxml is unavailable or the structure changes.

The scraper is resumable: per-page raw HTML is gzipped to
``data/cache/eib_pages/html/{id}.html.gz`` and the structured
extraction is written to ``data/cache/eib_pages/extracted/{id}.json``.
A :class:`src.utils.progress.Checkpoint` tracks completion so an
interrupted run continues from where it stopped. Re-extraction
(after an extractor upgrade) is a separate step that reads cached
HTML and writes fresh JSON without touching the network.

CLI
---
    python -m src.enrichment.eib_deep_scraper --rate-limit 0.5
    python -m src.enrichment.eib_deep_scraper --limit 50 --reparse
    python -m src.enrichment.eib_deep_scraper --reparse-only

The module is a drop-in richer replacement for ``eib_promoter_scraper``.
Once the new JSON has been written for every project, a follow-up
commit will cut ``consolidation.integrate_enrichment`` over to read
the richer enrichment file, preserving backward compatibility via
the ``source_record_id`` + ``amount_eur`` columns.

Sector-agnostic: the base repo ships no sector-specific defaults.
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import signal
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests required. Install with: pip install requests")

try:
    from lxml import html as lxml_html
    _HAS_LXML = True
except ImportError:
    _HAS_LXML = False

from src.paths import REPO_ROOT, ENRICHMENT_DIR
from src.utils.progress import ProgressTicker, Checkpoint


SCHEMA_VERSION = 'eib_deep_v1'

SITEMAP_URL = 'https://www.eib.org/en/sitemaps/dynamic/plr/project.xml'
PROJECT_URL = 'https://www.eib.org/en/projects/all/{project_id}'

CACHE_DIR = REPO_ROOT / 'data' / 'cache' / 'eib_pages'
HTML_CACHE = CACHE_DIR / 'html'
EXTRACT_CACHE = CACHE_DIR / 'extracted'
CHECKPOINT_PATH = CACHE_DIR / 'scrape.ckpt.jsonl'

DEEP_LOOKUP_FILE = ENRICHMENT_DIR / 'eib_deep_lookup.json'

# Graceful shutdown.
_shutdown = False


def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log.info("Shutdown requested — finishing current request...")


signal.signal(signal.SIGINT, _handle_signal)
try:
    signal.signal(signal.SIGTERM, _handle_signal)
except (AttributeError, ValueError):
    pass  # Windows may not support SIGTERM


# ---------------------------------------------------------------------------
# Extraction primitives
# ---------------------------------------------------------------------------

# Every structured field on an EIB project page sits inside an
# ``eib-list__row--body`` block that follows a sibling
# ``eib-list__row--header`` block containing one or more
# ``eib-typography__secondary-label`` cells. The value rows use the
# ``eib-typography__data-sheet--*`` classes.
#
# Rather than trying to parse the whole table-of-tables structure we
# walk the DOM by label, pulling the next data-sheet node at the same
# nesting depth. This is robust to the two layouts the site uses
# (single-column detail and two-column side-by-side).

_LABEL_CSS = 'eib-typography__secondary-label'
_VALUE_CSS_PREFIX = 'eib-typography__data-sheet'


# Fields we actively care about and the canonical key name each maps to.
# The label comparison is case-insensitive + ignores punctuation/whitespace
# differences.
_FIELD_KEYS: dict[str, str] = {
    'amount': 'signed_total_raw',
    'countries': 'country_raw',
    'sectors': 'sectors_raw',
    'sector s': 'sectors_raw',
    'signature dates': 'signatures_raw',
    'signature date s': 'signatures_raw',
    'release date': 'release_date',
    'status': 'status',
    'reference': 'reference',
    'project name': 'title',
    'promoter financial intermediary': 'promoters_raw',
    'proposed eib finance approximate amount': 'proposed_finance_raw',
    'total cost approximate amount': 'total_cost_raw',
    'location': 'location',
    'description': 'description',
    'objectives': 'objectives',
    'environmental aspects': 'environmental_aspects',
    'procurement': 'procurement',
    'comments': 'comments',
    'link to source': 'link_to_source',
    'other links': 'other_links',
}


def _norm_label(s: str) -> str:
    s = re.sub(r'[\u2010-\u2015\-]', ' ', s)  # hyphens / dashes → space
    s = re.sub(r'[^a-z0-9\s]', ' ', s.lower())
    return re.sub(r'\s+', ' ', s).strip()


# Currency prefix followed by a GREEDY run of digits / separators, then an
# optional magnitude unit. The leading ``(?:eur|€|euros?)`` guard is what
# keeps the parser from matching page numbers or years.
_NUM_AMOUNT_RE = re.compile(
    r'(?:eur|€|euros?)\s*'
    r'([\d][\d\s.,]*)'
    r'(?:\s*(million|billion|bn|m|k|thousand))?',
    re.IGNORECASE,
)
_DATE_SPLIT_RE = re.compile(r'(\d{1,2}[/\-\.]\d{1,2}[/\-\.]\d{2,4})')


def _parse_euro_amount(s: str | None) -> float | None:
    """Parse an EIB amount string (``"EUR 200 million"``, ``"€130,000,000"``).

    Returns the value in EUR (not millions). Returns ``None`` on any
    parse failure; never raises.
    """
    if not s:
        return None
    s = s.replace('\u20ac', 'EUR').replace('\u00a0', ' ')
    m = _NUM_AMOUNT_RE.search(s)
    if not m:
        return None
    raw = m.group(1)
    unit = (m.group(2) or '').lower()
    # Remove thousand separators, keep the decimal point / comma.
    # If both ',' and '.' are present, assume ',' = thousand. Otherwise
    # assume ',' is a decimal when it has 1-2 digits after it.
    raw = raw.replace(' ', '')
    if '.' in raw and ',' in raw:
        raw = raw.replace(',', '')
    elif ',' in raw and re.search(r',\d{1,2}$', raw):
        raw = raw.replace('.', '').replace(',', '.')
    else:
        raw = raw.replace(',', '')
    try:
        val = float(raw)
    except ValueError:
        return None
    if unit in ('million', 'm'):
        val *= 1e6
    elif unit in ('billion', 'bn'):
        val *= 1e9
    elif unit in ('thousand', 'k'):
        val *= 1e3
    if val < 1 or val > 1e13:
        return None
    return val


def _split_promoters(raw: str | None) -> list[str]:
    """Multi-promoter cells are comma-separated, but so are legal names
    (``"INTESA SANPAOLO SPA, BRANCH 1"``). Heuristic: split on ``,``
    only when the next token looks like a new legal entity head
    (starts with a capital word ≥ 3 chars not prefixed by a legal
    connector like "DI" or "E"). Fall back to returning the whole
    string as a single promoter.
    """
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    # Most pages join multiple promoters with a literal comma between
    # clearly-separate legal entities. A crude but effective split:
    # break on ",  " (two spaces) or on comma followed by an uppercase
    # word of ≥3 letters.
    parts = re.split(r',(?=\s*[A-Z][A-Z0-9]{2,})', raw)
    out = [p.strip().strip(',.') for p in parts if p.strip()]
    # Post-filter: drop anything that's only digits or <3 chars.
    return [p for p in out if len(p) >= 3 and not p.isdigit()]


def _parse_signatures(raw: str | None) -> list[dict]:
    """Parse the "Signature date(s)" cell into one tranche per signing.

    The cell renders as ``"8/04/2026 : € 30,000,000"`` per tranche but
    multiple tranches flow together once we concatenate the column's
    inner text. Splitting on the date pattern restores the boundaries
    without needing to guess where one number ends and the next date
    begins.
    """
    if not raw:
        return []
    parts = _DATE_SPLIT_RE.split(raw)
    # parts = [pre-first-date, date1, tail1, date2, tail2, ...]
    out: list[dict] = []
    i = 1
    while i < len(parts) - 1:
        date_str = parts[i]
        tail = parts[i + 1]
        amt_eur = _parse_euro_amount(tail)
        out.append({
            'date': date_str,
            'amount_eur': amt_eur,
            'raw': f'{date_str} {tail}'.strip()[:120],
        })
        i += 2
    return out


def _column_text(col) -> str:
    """Collapse a column's inner text to a single normalized line."""
    parts = col.xpath('.//text()')
    return re.sub(r'\s+', ' ', ' '.join(parts)).strip()


def _extract_fields_lxml(html: str) -> dict[str, Any]:
    """Lxml-based extraction.

    EIB project pages are built from repeated ``eib-list__row--header`` /
    ``eib-list__row--body`` pairs. Each header contains N
    ``eib-typography__secondary-label`` cells and each body contains
    exactly N ``eib-list__column`` direct children, one per label. A
    column holds the value for the label in the matching position —
    that's the only mapping rule we need.

    Prose-heavy fields (Description, Objectives, Environmental aspects,
    Procurement, Comments) live in the same structure; the value inside
    each column is wrapped in ``<p>`` rather than a
    ``eib-typography__data-sheet`` span, so we read the column's
    concatenated text regardless of inner tag.
    """
    doc = lxml_html.fromstring(html)
    fields: dict[str, Any] = {}

    headers = doc.xpath("//*[contains(@class, 'eib-list__row--header')]")
    for hdr in headers:
        label_nodes = hdr.xpath(
            ".//*[contains(@class, 'eib-typography__secondary-label')]"
        )
        labels: list[str] = []
        for ln in label_nodes:
            txt = ' '.join(ln.xpath('.//text()')).strip()
            if txt:
                labels.append(_norm_label(txt))
        labels = [l for l in labels if l]
        if not labels:
            continue

        # Find the row-body that directly follows this header.
        body = None
        sib = hdr.getnext()
        while sib is not None:
            cls = sib.get('class') or ''
            if 'eib-list__row--body' in cls:
                body = sib
                break
            if 'eib-list__row--header' in cls:
                break
            sib = sib.getnext()
        if body is None:
            continue

        columns = body.xpath("./*[contains(@class, 'eib-list__column')]")
        for i, label in enumerate(labels):
            if i >= len(columns):
                break
            text = _column_text(columns[i])
            if not text:
                continue
            key = _FIELD_KEYS.get(label)
            if key:
                # ``setdefault`` keeps the first non-empty value for a
                # label — the side panels on each page repeat some
                # fields (Amount, Sector) in multiple blocks and the
                # earliest one (usually in the summary table) is the
                # clean version.
                fields.setdefault(key, text)

    docs = doc.xpath("//a[contains(@href, '.pdf')]/@href")
    if docs:
        fields['related_doc_urls'] = list(dict.fromkeys(docs))
    src = doc.xpath("//a[contains(., 'Link to source')]/@href")
    if src:
        fields['link_to_source'] = src[0]
    return fields


_LABEL_VALUE_RE = re.compile(
    r'eib-typography__secondary-label[^>]*>\s*([^<]+?)\s*<'
    r'.*?eib-typography__data-sheet[^>]*>\s*([^<]+?)\s*<',
    re.DOTALL,
)


def _extract_fields_regex(html: str) -> dict[str, Any]:
    """Regex fallback when lxml is unavailable or raises on malformed HTML."""
    fields: dict[str, Any] = {}
    for m in _LABEL_VALUE_RE.finditer(html):
        label = _norm_label(m.group(1))
        val = re.sub(r'\s+', ' ', m.group(2)).strip()
        if not val:
            continue
        key = _FIELD_KEYS.get(label)
        if key:
            fields.setdefault(key, val)
    # Related docs.
    docs = re.findall(r'href="([^"]+\.pdf[^"]*)"', html)
    if docs:
        fields['related_doc_urls'] = list(dict.fromkeys(docs))
    return fields


# ---------------------------------------------------------------------------
# Record dataclass
# ---------------------------------------------------------------------------

@dataclass
class EibProjectRecord:
    project_id: str
    url: str
    status_code: int | str | None = None
    scraped_at: str | None = None
    schema_version: str = SCHEMA_VERSION
    # Raw label → value map (unparsed), retained for audit.
    raw_fields: dict[str, Any] = field(default_factory=dict)
    # Parsed outputs.
    title: str | None = None
    status: str | None = None
    release_date: str | None = None
    country: str | None = None
    location: str | None = None
    sectors: list[str] = field(default_factory=list)
    description: str | None = None
    objectives: str | None = None
    environmental_aspects: str | None = None
    procurement: str | None = None
    comments: str | None = None
    promoters: list[str] = field(default_factory=list)
    proposed_finance_eur: float | None = None
    total_cost_eur: float | None = None
    signed_total_eur: float | None = None
    signatures: list[dict] = field(default_factory=list)
    related_doc_urls: list[str] = field(default_factory=list)
    link_to_source: str | None = None

    def is_usable(self) -> bool:
        return bool(self.title or self.promoters or self.description)


def _strip_trailing_amount(text: str | None) -> str | None:
    """``"Italy : € 130,000,000"`` → ``"Italy"``. No-op if no trailing cost."""
    if not text:
        return text
    # Match either a " : ", " - ", or " — " followed by anything starting
    # with a currency symbol or an EUR prefix.
    return re.sub(
        r'\s*[:\-\u2013\u2014]\s*(?:eur|€|euros?)\s.*$',
        '',
        text,
        flags=re.IGNORECASE,
    ).strip()


def _strip_trailing_date(text: str | None) -> str | None:
    """``"Signed | 22/12/2020"`` → ``"Signed"``."""
    if not text:
        return text
    return re.sub(r'\s*[|\u2022]\s*\d{1,2}[/\-\.]\d.*$', '', text).strip()


def _split_sectors(raw: str | None) -> list[str]:
    """EIB sector values come in two flavours: one is the single canonical
    sector (``"Urban development - Construction"``, already clean), the
    other is the sector paired with the amount
    (``"Urban development : € 130,000,000"``). We accept either form —
    strip the amount, then split only on multi-sector separators
    (``,`` / ``;`` / ``/``) while preserving the canonical ``-``
    connector that EIB uses between sector and sub-sector.
    """
    if not raw:
        return []
    cleaned = _strip_trailing_amount(raw) or raw
    parts = re.split(r'\s*[,;/]\s*', cleaned)
    return [p.strip() for p in parts if p.strip()]


def record_from_fields(project_id: str, url: str, raw: dict[str, Any]) -> EibProjectRecord:
    """Materialise a record from the raw label map."""
    rec = EibProjectRecord(
        project_id=project_id,
        url=url,
        scraped_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
        raw_fields={k: v for k, v in raw.items() if isinstance(v, (str, list))},
        title=raw.get('title'),
        status=_strip_trailing_date(raw.get('status')),
        release_date=raw.get('release_date'),
        # Location comes from the bottom-of-page "Location" cell which is
        # already clean. The top-of-page "Countries" cell doubles as an
        # amount-by-country table, so we strip the trailing amount and
        # fall back to Location when nothing else is available.
        country=_strip_trailing_amount(raw.get('country_raw'))
                or raw.get('location'),
        location=raw.get('location'),
        description=raw.get('description'),
        objectives=raw.get('objectives'),
        environmental_aspects=raw.get('environmental_aspects'),
        procurement=raw.get('procurement'),
        comments=raw.get('comments'),
        link_to_source=raw.get('link_to_source'),
    )
    rec.sectors = _split_sectors(raw.get('sectors_raw'))
    rec.promoters = _split_promoters(raw.get('promoters_raw'))
    rec.proposed_finance_eur = _parse_euro_amount(raw.get('proposed_finance_raw'))
    rec.total_cost_eur = _parse_euro_amount(raw.get('total_cost_raw'))
    rec.signed_total_eur = _parse_euro_amount(raw.get('signed_total_raw'))
    rec.signatures = _parse_signatures(raw.get('signatures_raw'))
    urls = raw.get('related_doc_urls')
    if isinstance(urls, list):
        rec.related_doc_urls = urls
    return rec


def extract_from_html(html: str, project_id: str, url: str) -> EibProjectRecord:
    """Full extraction pipeline: lxml → regex fallback → record."""
    raw: dict[str, Any] = {}
    if _HAS_LXML:
        try:
            raw = _extract_fields_lxml(html)
        except Exception as exc:
            log.warning(f"  lxml parse failed for {project_id}: {exc}; falling back to regex")
            raw = {}
    if not raw:
        raw = _extract_fields_regex(html)
    return record_from_fields(project_id, url, raw)


# ---------------------------------------------------------------------------
# Cache + IO helpers
# ---------------------------------------------------------------------------

def _ensure_dirs() -> None:
    for d in (CACHE_DIR, HTML_CACHE, EXTRACT_CACHE):
        d.mkdir(parents=True, exist_ok=True)


def _html_path(project_id: str) -> Path:
    return HTML_CACHE / f'{project_id}.html.gz'


def _extract_path(project_id: str) -> Path:
    return EXTRACT_CACHE / f'{project_id}.json'


def _load_html_cache(project_id: str) -> str | None:
    p = _html_path(project_id)
    if not p.exists():
        return None
    try:
        with gzip.open(p, 'rt', encoding='utf-8') as f:
            return f.read()
    except Exception as exc:
        log.warning(f"  html cache read failed for {project_id}: {exc}")
        return None


def _save_html_cache(project_id: str, html: str) -> None:
    p = _html_path(project_id)
    try:
        with gzip.open(p, 'wt', encoding='utf-8') as f:
            f.write(html)
    except Exception as exc:
        log.warning(f"  html cache write failed for {project_id}: {exc}")


def _save_record(rec: EibProjectRecord) -> None:
    p = _extract_path(rec.project_id)
    try:
        with p.open('w', encoding='utf-8') as f:
            json.dump(asdict(rec), f, ensure_ascii=False)
    except Exception as exc:
        log.warning(f"  extract write failed for {rec.project_id}: {exc}")


def _load_record(project_id: str) -> EibProjectRecord | None:
    p = _extract_path(project_id)
    if not p.exists():
        return None
    try:
        with p.open('r', encoding='utf-8') as f:
            data = json.load(f)
        if data.get('schema_version') != SCHEMA_VERSION:
            return None
        # Rehydrate — tolerate missing keys by using dataclass defaults.
        valid_keys = set(EibProjectRecord.__dataclass_fields__.keys())
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return EibProjectRecord(**filtered)
    except Exception as exc:
        log.warning(f"  extract read failed for {project_id}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Scrape loop
# ---------------------------------------------------------------------------

def fetch_sitemap_ids(session: requests.Session) -> list[str]:
    log.info(f"Fetching sitemap: {SITEMAP_URL}")
    resp = session.get(SITEMAP_URL, timeout=60)
    resp.raise_for_status()
    ids = re.findall(r'/en/projects/all/(\d+)', resp.text)
    # Dedup, preserve order.
    seen: set[str] = set()
    out: list[str] = []
    for pid in ids:
        if pid not in seen:
            seen.add(pid)
            out.append(pid)
    log.info(f"  Found {len(out):,} unique project IDs in sitemap")
    return out


def _fetch_page(
    session: requests.Session,
    project_id: str,
    rate_limit: float,
    timeout: int = 30,
    max_retries: int = 3,
) -> tuple[int | str, str | None]:
    url = PROJECT_URL.format(project_id=project_id)
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=timeout)
            time.sleep(rate_limit)
            if resp.status_code == 200:
                return 200, resp.text
            if resp.status_code == 404:
                return 404, None
            if resp.status_code in (429, 503):
                wait = rate_limit * (2 ** attempt)
                time.sleep(wait)
                continue
            return resp.status_code, None
        except requests.Timeout:
            time.sleep(rate_limit * (2 ** attempt))
            continue
        except requests.RequestException as exc:
            log.warning(f"  {project_id}: request error {exc}")
            return 'error', None
    return 'timeout', None


def run_scrape(
    rate_limit: float = 0.5,
    limit: int = 0,
    reparse: bool = False,
    reparse_only: bool = False,
) -> dict[str, EibProjectRecord]:
    """Main entry point. Returns the in-memory project_id → record map."""
    _ensure_dirs()

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (research-project; EU subsidy analysis) Python/3.11',
        'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
        'Accept-Language': 'en-GB,en;q=0.9',
    })

    if reparse_only:
        return _reparse_from_cache(limit)

    # Get the full project ID list.
    project_ids = fetch_sitemap_ids(session)
    if limit > 0:
        project_ids = project_ids[:limit]
    total = len(project_ids)
    log.info(
        f"Deep EIB scrape: {total:,} pages, rate={rate_limit}s/req, "
        f"lxml={_HAS_LXML}"
    )

    results: dict[str, EibProjectRecord] = {}
    ticker = ProgressTicker(total=total, name='EIB scrape', every=50, logger=log)

    with Checkpoint(CHECKPOINT_PATH) as ckpt:
        for pid in project_ids:
            if _shutdown:
                log.info("  Graceful stop: checkpointing and exiting loop.")
                break

            url = PROJECT_URL.format(project_id=pid)

            # Resume path: checkpoint + cached extract.
            if not reparse and ckpt.done(pid):
                rec = _load_record(pid)
                if rec is not None:
                    results[pid] = rec
                    ticker.tick(success=True, latency=0.0)
                    continue

            # Cache-HTML-only path: re-extract without network.
            cached_html = _load_html_cache(pid)
            if cached_html is not None:
                t0 = time.time()
                rec = extract_from_html(cached_html, pid, url)
                rec.status_code = 'cache'
                _save_record(rec)
                ckpt.mark(pid, {'status': 'cache', 'title': rec.title})
                results[pid] = rec
                ticker.tick(success=rec.is_usable(), latency=time.time() - t0)
                continue

            # Network path.
            t0 = time.time()
            status, html = _fetch_page(session, pid, rate_limit=rate_limit)
            latency = time.time() - t0
            if status != 200 or not html:
                rec = EibProjectRecord(
                    project_id=pid,
                    url=url,
                    status_code=status,
                    scraped_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                )
                _save_record(rec)
                ckpt.mark(pid, {'status': status, 'title': None})
                ticker.tick(success=False, latency=latency)
                continue
            _save_html_cache(pid, html)
            rec = extract_from_html(html, pid, url)
            rec.status_code = 200
            _save_record(rec)
            ckpt.mark(pid, {'status': 200, 'title': rec.title})
            results[pid] = rec
            ticker.tick(success=rec.is_usable(), latency=latency)

    ticker.finalise()
    _write_lookup_json(results)
    return results


def _reparse_from_cache(limit: int = 0) -> dict[str, EibProjectRecord]:
    """Re-run extraction over every cached HTML file. No network."""
    _ensure_dirs()
    html_files = sorted(HTML_CACHE.glob('*.html.gz'))
    if limit > 0:
        html_files = html_files[:limit]
    total = len(html_files)
    log.info(f"Re-parsing {total:,} cached HTML files (no network)...")
    results: dict[str, EibProjectRecord] = {}
    ticker = ProgressTicker(total=total, name='EIB reparse', every=500, logger=log)
    for p in html_files:
        if _shutdown:
            log.info("  Graceful stop.")
            break
        pid = p.stem.replace('.html', '')
        try:
            with gzip.open(p, 'rt', encoding='utf-8') as f:
                html = f.read()
            rec = extract_from_html(html, pid, PROJECT_URL.format(project_id=pid))
            rec.status_code = 'cache'
            _save_record(rec)
            results[pid] = rec
            ticker.tick(success=rec.is_usable())
        except Exception as exc:
            log.warning(f"  reparse failed {pid}: {exc}")
            ticker.tick(success=False)
    ticker.finalise()
    _write_lookup_json(results)
    return results


def _write_lookup_json(results: dict[str, EibProjectRecord]) -> None:
    ENRICHMENT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        pid: {
            'title': r.title,
            'promoters': r.promoters,
            'country': r.country,
            'signed_total_eur': r.signed_total_eur,
            'proposed_finance_eur': r.proposed_finance_eur,
            'total_cost_eur': r.total_cost_eur,
            'sectors': r.sectors,
            'status': r.status,
            'n_signatures': len(r.signatures),
            'has_description': bool(r.description),
        }
        for pid, r in results.items()
    }
    with DEEP_LOOKUP_FILE.open('w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    log.info(f"  Saved compact lookup: {DEEP_LOOKUP_FILE}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__ or '')
    parser.add_argument('--rate-limit', type=float, default=0.5,
                        help='Seconds to sleep after each request (default 0.5)')
    parser.add_argument('--limit', type=int, default=0,
                        help='Max number of pages to fetch (0 = all)')
    parser.add_argument('--reparse', action='store_true',
                        help='Ignore existing extract JSON and re-run extractor '
                             '(uses cached HTML if present)')
    parser.add_argument('--reparse-only', action='store_true',
                        help='Skip network entirely; re-run extractor over every '
                             'cached HTML file')
    args = parser.parse_args(list(argv) if argv is not None else None)

    t0 = time.time()
    log.info('=' * 70)
    log.info(f'EIB Deep Scraper — schema {SCHEMA_VERSION}')
    log.info('=' * 70)

    results = run_scrape(
        rate_limit=args.rate_limit,
        limit=args.limit,
        reparse=args.reparse,
        reparse_only=args.reparse_only,
    )

    n_usable = sum(1 for r in results.values() if r.is_usable())
    n_promoter = sum(1 for r in results.values() if r.promoters)
    n_desc = sum(1 for r in results.values() if r.description)
    n_signed = sum(1 for r in results.values() if r.signed_total_eur)

    log.info('')
    log.info('=' * 70)
    log.info('EIB DEEP SCRAPER SUMMARY')
    log.info('=' * 70)
    log.info(f'  Records:            {len(results):,}')
    log.info(f'  Usable:             {n_usable:,}')
    log.info(f'  With promoter:      {n_promoter:,}')
    log.info(f'  With description:   {n_desc:,}')
    log.info(f'  With signed amount: {n_signed:,}')
    log.info(f'  Runtime:            {(time.time()-t0)/60:.1f} min')
    return 0


if __name__ == '__main__':
    sys.exit(main())
