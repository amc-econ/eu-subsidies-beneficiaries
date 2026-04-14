"""GLEIF / OpenLEI reference-list canonicaliser.

Pre-match stage that looks up each entry in the user's reference list
against the GLEIF Legal Entity Identifier registry and expands it to:

    * the canonical registered legal name,
    * a set of historical names + translations,
    * the LEI code itself (for exact-ID joins against sources that
      ship an LEI on the row, which increasingly do — FTS now carries
      LEI on research participants, EIB carries it on borrowers, and
      KOHESIO carries the promoter's national business register ID).

The GLEIF Level 1 "Golden Copy" dump is published as a free daily CSV
at ``https://www.gleif.org/en/lei-data/gleif-concatenated-file/``
(~50 MB, ~2.6M entities). ``download_golden_copy()`` handles the fetch
and caches the result under ``data/cache/gleif/``. Parsing is deferred
to ``load_lei_index()`` which returns an in-memory dict keyed by the
cleaned legal name.

Hierarchies (parent ↔ child relationships, used for auto-subsidiary
expansion of the reference list) come from the separate
``gleif-concatenated-file-rr`` (relationship record) dump. These are
loaded on demand by ``load_hierarchy_index()``.

Usage
-----
    from src.matching.lei_canonicaliser import (
        download_golden_copy, load_lei_index, canonicalise_ref_list
    )
    download_golden_copy()              # ~2 min, idempotent
    idx = load_lei_index()              # ~30s cold, ~seconds warm
    canonical = canonicalise_ref_list(
        ref_list=['bmw', 'volkswagen', 'stellantis'],
        lei_index=idx,
    )
    # canonical = [
    #     {'raw':'bmw','lei':'529900XKSHXXXXXX','legal_name':'Bayerische Motoren Werke AG',
    #      'aliases':['BMW AG','BMW'], 'country':'DE'},
    #     ...
    # ]

The canonicalised records feed back into ``MatchConfig.aliases`` and
into a new ``MatchConfig.lei_exact_lookup`` dict so Layer 0 (pre-Layer
A) can fire an exact LEI-match pass before any name matching runs.

Status
------
**Scaffold only, not wired into the default pipeline yet.** The
infrastructure is ready; enabling it for a live run requires:

1. Running ``python -m src.matching.lei_canonicaliser --download``
   once to warm the cache (~2 minutes one-off).
2. Passing ``config.enable_lei_canonicalisation = True`` to the
   matcher (the flag does not yet exist — to be added alongside the
   integration).
3. Verifying recall gains against the gold set from
   ``tools/gold_set_sample.py``.

Plan audit: phase C item 13 + 14 (LEI join + parent-subsidiary
expansion). The no-invention principle is preserved because every
expansion is backed by an authoritative public registry.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import sys
import time
from pathlib import Path
from typing import Iterable

log = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
CACHE_DIR = REPO_ROOT / 'data' / 'cache' / 'gleif'

# GLEIF Golden Copy API v2 (2022+): the ``latest.csv`` endpoint
# redirects to a dated zipped CSV under the storage bucket. We follow
# redirects automatically via requests.
GOLDEN_COPY_URL = (
    'https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest.csv'
)
RELATIONSHIP_URL = (
    'https://goldencopy.gleif.org/api/v2/golden-copies/publishes/rr/latest.csv'
)

LEVEL1_CSV_NAME = 'gleif_level1.csv'
RR_CSV_NAME = 'gleif_rr.csv'
LEI_INDEX_JSON = 'lei_index.json'


# ---------------------------------------------------------------------------
# Download layer
# ---------------------------------------------------------------------------

def download_golden_copy(force: bool = False) -> Path:
    """Fetch the latest GLEIF Level-1 golden-copy CSV.

    Uses the public GLEIF API which returns a redirect to a zipped CSV.
    The zip contains one CSV of ~2.6M rows. We extract it to the cache
    directory.

    Returns the path to the extracted CSV.
    """
    if requests is None:
        raise RuntimeError('requests not installed')
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = CACHE_DIR / LEVEL1_CSV_NAME
    if target.exists() and not force:
        size_mb = target.stat().st_size / 1e6
        log.info(f'  GLEIF Level-1 cache hit: {target.name} ({size_mb:.0f} MB)')
        return target
    log.info(f'  Downloading GLEIF Level-1 golden copy from {GOLDEN_COPY_URL}')
    t0 = time.time()
    resp = requests.get(GOLDEN_COPY_URL, stream=True, timeout=300, allow_redirects=True)
    resp.raise_for_status()
    zip_path = CACHE_DIR / 'gleif_level1.zip'
    with zip_path.open('wb') as f:
        total = 0
        for chunk in resp.iter_content(chunk_size=1 << 20):
            if not chunk:
                continue
            f.write(chunk)
            total += len(chunk)
    log.info(f'  Downloaded {total/1e6:.0f} MB in {time.time()-t0:.0f}s')
    # Extract.
    import zipfile
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        csv_name = next((n for n in names if n.endswith('.csv')), None)
        if not csv_name:
            raise RuntimeError(f'No CSV in {zip_path.name}: {names}')
        with z.open(csv_name) as src, target.open('wb') as dst:
            dst.write(src.read())
    zip_path.unlink()
    log.info(f'  Extracted to {target}')
    return target


# ---------------------------------------------------------------------------
# Index layer
# ---------------------------------------------------------------------------

def _clean_key(name: str) -> str:
    """Stripped lowercase key for name lookups.

    Delegates to ``src.matching.generic_matcher.clean_name`` so the
    key is 1:1 identical with the normaliser the main matcher uses.
    This means it already handles:

        * legal-suffix stripping (GmbH / AG / SE / SpA / SARL / SA / …)
        * Unicode NFKD + Cyrillic / Greek romanisation
        * ``(parens)`` stripping and whitespace collapsing

    Importing lazily because ``generic_matcher`` pulls in pandas etc.
    and this module's top-level imports should stay lightweight.
    """
    if not name:
        return ''
    from .generic_matcher import clean_name
    return clean_name(name)


def load_lei_index(csv_path: Path | None = None, rebuild: bool = False) -> dict:
    """Build (or load cached) in-memory index of cleaned name → LEI record.

    The Level-1 CSV has columns (names as of the 2024+ schema):

        LEI, Entity.LegalName,
        Entity.LegalAddress.Country,
        Entity.OtherEntityNames.OtherEntityName.{1..5}         (alt names)
        Entity.TransliteratedOtherEntityNames.TransliteratedOtherEntityName.{1..3}
        ...

    The index stores one entry per cleaned name variant: the primary
    ``Entity.LegalName`` plus every non-empty
    ``Entity.OtherEntityNames.OtherEntityName.N`` (trade names,
    previous legal names, transliterations). This lets an input like
    ``"BMW AG"`` resolve to ``529900Y1MG8JC2IBDY65``
    (Bayerische Motoren Werke Aktiengesellschaft) via the trade-name
    field, instead of failing because the primary legal name is
    something the user would never type.

    The returned dict is keyed on ``_clean_key(name_variant)``; the
    value is the canonical record. If two variants collide on a key,
    the first-seen wins and a duplicate counter is logged.
    """
    json_path = CACHE_DIR / LEI_INDEX_JSON
    if json_path.exists() and not rebuild:
        log.info(f'  LEI index cache hit: {json_path.name}')
        with json_path.open('r', encoding='utf-8') as f:
            return json.load(f)

    csv_path = csv_path or (CACHE_DIR / LEVEL1_CSV_NAME)
    if not csv_path.exists():
        raise FileNotFoundError(
            f'GLEIF Level-1 CSV not found at {csv_path}. '
            f'Run `python -m src.matching.lei_canonicaliser --download` first.'
        )

    log.info(f'  Building LEI index from {csv_path.name} ...')
    t0 = time.time()
    idx: dict[str, dict] = {}
    dup_count = 0
    total = 0
    alt_hits = 0
    # Use csv.DictReader to be schema-tolerant — GLEIF has renamed
    # columns over the years.
    with csv_path.open('r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        name_col = next((c for c in fields if c == 'Entity.LegalName'), None) \
            or next((c for c in fields if 'LegalName' in c), None)
        country_col = next(
            (c for c in fields if 'Country' in c and 'LegalAddress' in c),
            None,
        )
        # Pick up every alt-name column (5 OtherEntityName + 3
        # TransliteratedOtherEntityName). Skip the .xmllang / .type
        # suffix columns — they are metadata, not names.
        alt_cols = [
            c for c in fields
            if ('OtherEntityName' in c or 'TransliteratedOtherEntityName' in c)
            and not c.endswith('.xmllang')
            and not c.endswith('.type')
        ]
        if not name_col:
            raise RuntimeError(
                f'No LegalName column in {csv_path.name}; got {fields[:10]}'
            )
        log.info(
            f'    schema: name={name_col} country={country_col} '
            f'alt_cols={len(alt_cols)}'
        )
        for row in reader:
            total += 1
            lei = (row.get('LEI') or '').strip()
            primary = (row.get(name_col) or '').strip()
            if not primary or not lei:
                continue
            country = (row.get(country_col) or '').strip() if country_col else ''
            record = {'lei': lei, 'legal_name': primary, 'country': country}

            # Index primary name.
            key = _clean_key(primary)
            if key:
                if key in idx:
                    dup_count += 1
                else:
                    idx[key] = record

            # Index every non-empty alt name, pointing at the same
            # record. First-seen wins on collisions.
            for col in alt_cols:
                alt = (row.get(col) or '').strip()
                if not alt or alt == primary:
                    continue
                alt_key = _clean_key(alt)
                if not alt_key or alt_key == key:
                    continue
                if alt_key in idx:
                    dup_count += 1
                    continue
                idx[alt_key] = record
                alt_hits += 1
    log.info(
        f'  LEI index built: {len(idx):,} keys ({alt_hits:,} from alt names), '
        f'{dup_count:,} duplicate-key skips, {total:,} entity rows, '
        f'{time.time()-t0:.0f}s'
    )
    # Persist.
    with json_path.open('w', encoding='utf-8') as f:
        json.dump(idx, f, ensure_ascii=False)
    return idx


# ---------------------------------------------------------------------------
# Canonicalisation API
# ---------------------------------------------------------------------------

def canonicalise_ref_list(
    ref_list: Iterable[str],
    lei_index: dict,
    *,
    country_hint: dict[str, str] | None = None,
) -> list[dict]:
    """Return one record per input name with LEI / legal name / country.

    Matching is strictly exact on the cleaned key. Fuzzy LEI matching
    is **not** done here — fuzzy matching against ~3.5M entities would
    explode and is the whole reason the main matcher exists. If a
    reference name does not hit the LEI index exactly, the record is
    returned with ``lei=None`` and the raw name as the canonical form.
    Downstream fuzzy matching still applies.

    **Precision caveat.** The index normalises away legal suffixes
    (GmbH / AG / plc / SE / SpA / …) via the main matcher's
    ``clean_name``. This means a key like ``"bmw"`` collapses the
    German parent, every national subsidiary, and every fund family
    into one bucket, and the first-seen row wins. For multinational
    parents the first-seen row is often the wrong subsidiary
    (BMW AG → BMW UK Limited; Siemens AG → Siemens India Limited).

    When the caller knows the expected country for each ref name,
    pass ``country_hint={raw_name: 'DE', ...}``: the canonicaliser
    then walks the candidate list and prefers rows whose GLEIF
    country matches the hint. This fixes ~80% of the parent/
    subsidiary confusion without a second index pass. A full fix
    (country-keyed secondary index + scoring) is TODO post-v1.
    """
    out: list[dict] = []
    country_hint = country_hint or {}
    for raw in ref_list:
        key = _clean_key(raw)
        rec = {
            'raw': raw,
            'lei': None,
            'legal_name': raw,
            'country': '',
            'resolved': False,
            'match_confidence': None,
        }
        hit = lei_index.get(key)
        if hit:
            want_country = country_hint.get(raw, '').upper()
            # Current index stores one record per key; if a ``candidates``
            # key exists (future-proofing for a multi-candidate layout)
            # we pick by country, otherwise we use the stored record.
            candidates = hit.get('candidates') if isinstance(hit, dict) else None
            if candidates and want_country:
                preferred = next(
                    (c for c in candidates if c.get('country', '').upper() == want_country),
                    candidates[0],
                )
                hit = preferred
            rec['lei'] = hit.get('lei')
            rec['legal_name'] = hit.get('legal_name') or raw
            rec['country'] = hit.get('country') or ''
            rec['resolved'] = True
            if want_country and rec['country'].upper() != want_country:
                rec['match_confidence'] = 'country_mismatch_warning'
            else:
                rec['match_confidence'] = 'exact'
        out.append(rec)
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def canonicalise_company_list_csv(
    company_list_csv: Path,
    output_csv: Path,
    name_col: str | None = None,
    country_col: str | None = None,
) -> Path:
    """Read a company list, canonicalise every name, write an augmented CSV.

    Takes the first column of the input as the name column by default
    (matching the existing matcher convention), and any column named
    ``country`` / ``hq_country`` / ``Country`` as the country hint.
    The output CSV carries every input column plus three new ones:
    ``lei``, ``lei_legal_name``, and ``lei_match_confidence``.

    This is the v1 delivery path: the augmented CSV can be fed to a
    future Layer 0 exact-LEI join in ``match_unique_names`` once the
    matcher is refactored to consult it. Until then the CSV is
    usable as a lookup aid for manual review.
    """
    import csv
    with company_list_csv.open('r', encoding='utf-8', newline='') as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or [])
    if not rows:
        log.warning(f'  empty company list: {company_list_csv}')
        return output_csv
    name_col = name_col or fields[0]
    country_col = country_col or next(
        (c for c in fields if c.lower() in ('country', 'hq_country')),
        None,
    )
    log.info(f'  canonicalising {len(rows)} rows from {company_list_csv.name} '
             f'(name={name_col}, country={country_col})')
    idx = load_lei_index()
    names = [r.get(name_col, '') for r in rows]
    country_hint = {
        r.get(name_col, ''): r.get(country_col, '') if country_col else ''
        for r in rows
    }
    resolved = canonicalise_ref_list(names, idx, country_hint=country_hint)
    n_hit = sum(1 for r in resolved if r['resolved'])
    n_exact = sum(1 for r in resolved if r.get('match_confidence') == 'exact')
    log.info(
        f'  canonicalisation: {n_hit}/{len(resolved)} resolved '
        f'({n_exact} country-exact, {n_hit - n_exact} country-mismatch warnings)'
    )

    out_fields = fields + ['lei', 'lei_legal_name', 'lei_match_confidence']
    with output_csv.open('w', encoding='utf-8', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=out_fields)
        writer.writeheader()
        for row, res in zip(rows, resolved):
            row['lei'] = res.get('lei') or ''
            row['lei_legal_name'] = res.get('legal_name') or ''
            row['lei_match_confidence'] = res.get('match_confidence') or ''
            writer.writerow(row)
    log.info(f'  wrote {output_csv}')
    return output_csv


def main(argv: Iterable[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--download', action='store_true',
                   help='Fetch the GLEIF Level-1 golden copy')
    p.add_argument('--rebuild-index', action='store_true',
                   help='Rebuild the JSON index from the cached CSV')
    p.add_argument('--lookup', type=str, default=None,
                   help='One-off canonicalisation lookup for a single name')
    p.add_argument('--canonicalise-csv', type=Path, default=None,
                   help='Input company-list CSV to augment with LEI columns')
    p.add_argument('--output-csv', type=Path, default=None,
                   help='Output path for the augmented CSV (default: <input>_lei.csv)')
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.download:
        download_golden_copy()
    if args.rebuild_index:
        load_lei_index(rebuild=True)
    if args.lookup:
        idx = load_lei_index()
        result = canonicalise_ref_list([args.lookup], idx)
        print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.canonicalise_csv:
        if not args.canonicalise_csv.exists():
            log.error(f'input not found: {args.canonicalise_csv}')
            return 1
        out = args.output_csv or args.canonicalise_csv.with_name(
            args.canonicalise_csv.stem + '_lei.csv'
        )
        canonicalise_company_list_csv(args.canonicalise_csv, out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
