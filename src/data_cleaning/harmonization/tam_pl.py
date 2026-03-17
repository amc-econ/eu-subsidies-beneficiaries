"""
harmonization/tam_pl.py
=======================
Standardize Polish state aid data from the SUDOP API (supplement to TAM).

Source:  UOKiK SUDOP (System Udostępniania Danych o Pomocy Publicznej)
API:     https://api-sudop.uokik.gov.pl/sudop-api/api/przypadki-pomocy
Portal:  https://sudop.uokik.gov.pl/
Docs:    https://dane.gov.pl/pl/dataset/6068,api-do-sudop

API ARCHITECTURE:
  The SUDOP API uses an async 3-step pattern behind a WSO2 API Manager:
    1. Submit query → HTTP 303 → Location: /sudop-api/api/kolejka/{uuid}
    2. Poll queue (after ~60s) → HTTP 303 → Location: /sudop-api/api/wynik/{uuid}
       (if still preparing: HTTP 200, body = "Przygotowywanie odpowiedzi...")
    3. Fetch result → HTTP 200 → JSON with {liczba-wynikow: N, wyniki: [...]}

  Rate limit: 15 queries/minute. The 60-second async wait is the real bottleneck.
  Results are ephemeral and expire shortly after retrieval.

SCRAPING STRATEGY:
  Phase 1: Discovery — query each of ~52 forma-pomocy-kod codes (page 1 only)
           to get record counts and first 10K records per code.
  Phase 2: Pagination — for codes with >10K records, generate remaining page
           queries and process them in batches of BATCH_SIZE, sharing the 65s
           async wait across all queries in each batch.

  Parallelism: Submit N queries → wait 65s → poll+fetch all N.
  This overlaps the async waits, giving ~(65 + N*5)s per batch instead of N*70s.

RESPONSE SCHEMA (per record):
  - nazwa-beneficjenta: beneficiary name
  - nip-beneficjenta: beneficiary NIP (tax ID)
  - dzien-udzielenia-pomocy: date of aid (YYYY-MM-DD)
  - wartosc-nominalna-pln: nominal value in PLN
  - wartosc-brutto-pln: gross subsidy equivalent in PLN
  - wartosc-brutto-eur: gross subsidy equivalent in EUR (ECB rate)
  - forma-pomocy-kod / forma-pomocy-nazwa: aid form code/name
  - przeznaczenie-pomocy-kod / przeznaczenie-pomocy-nazwa: aid purpose
  - sektor-dzialalnosci-kod / sektor-dzialalnosci-nazwa: PKD sector code
  - srodek-pomocowy-numer / srodek-pomocowy-nazwa: aid programme (SA number)
  - nazwa-udzielajacego-pomocy: granting authority
  - gmina-siedziby-kod / gmina-siedziby-nazwa: municipality
  - wielkosc-beneficjenta-nazwa: enterprise size

Country: PL (hardcoded)

Usage (scrape):
    python -m src.data_cleaning.harmonization.tam_pl --scrape

Usage (standardize from cached data):
    Called from tam_supplements.py via standardize(data_dir, log)
"""

import json
import logging
import math
import sys
import time
from pathlib import Path

import pandas as pd
import requests
import urllib3

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    classify_instrument,
    pack_originals,
    safe_to_numeric,
)

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CACHE_DIR = _DEFAULT_PROJECT_ROOT / 'external_enrichment' / 'cache' / 'sudop'
RAW_CSV = _DEFAULT_PROJECT_ROOT / 'data' / 'raw' / 'poland_state_aid.csv'


def _resolve_paths(data_dir: Path | None = None):
    """Compute project paths from data_dir (which points to data/raw/)."""
    if data_dir is not None:
        project_root = data_dir.parent.parent
    else:
        project_root = _DEFAULT_PROJECT_ROOT
    cache_dir = project_root / 'external_enrichment' / 'cache' / 'sudop'
    raw_csv = data_dir / 'poland_state_aid.csv' if data_dir else RAW_CSV
    return project_root, cache_dir, raw_csv

API_BASE = 'https://api-sudop.uokik.gov.pl/sudop-api'
HEADERS = {'Accept': 'application/json', 'User-Agent': 'Bruegel-Research/1.0'}

# Timing parameters
POLL_WAIT = 65          # seconds before first poll
POLL_INTERVAL = 15      # seconds between re-polls
POLL_MAX = 12           # max poll attempts
INTER_SUBMIT_DELAY = 4  # seconds between submitting queries in a batch
BATCH_SIZE = 8          # queries per async batch (8 pages share 65s wait)
PAGE_SIZE = 10000       # API max per page


# ---------------------------------------------------------------------------
# Polish instrument translations
# ---------------------------------------------------------------------------
PL_INSTRUMENT_MAP = {
    'dotacja': 'grant',
    'refundacja': 'grant',
    'rekompensata': 'grant',
    'bezzwrotne': 'grant',
    'dopłat': 'subsidy',
    'pożyczka': 'loan',
    'kredyt': 'loan',
    'zaliczka zwrotna': 'loan',
    'gwarancja': 'guarantee',
    'poręczenie': 'guarantee',
    'zwolnienie z podatku': 'tax_advantage',
    'odliczenie od podatku': 'tax_advantage',
    'obniżk': 'tax_advantage',
    'zaniechanie poboru': 'tax_advantage',
    'umorzenie': 'debt_relief',
    'odroczenie': 'tax_advantage',
    'rozłożenie na raty': 'tax_advantage',
    'wniesienie kapitału': 'equity',
    'konwersja wierzytelności': 'equity',
}


def _classify_pl_instrument(raw: str) -> str:
    """Classify Polish instrument type from forma-pomocy-nazwa."""
    if pd.isna(raw) or not raw:
        return None
    raw_lower = str(raw).strip().lower()
    for key, val in PL_INSTRUMENT_MAP.items():
        if key in raw_lower:
            return val
    return classify_instrument(raw)


# ---------------------------------------------------------------------------
# SUDOP API — low-level async helpers
# ---------------------------------------------------------------------------
def _abs_url(path: str) -> str:
    if path.startswith('/'):
        return f"https://api-sudop.uokik.gov.pl{path}"
    return path


def _submit(params: dict, log: logging.Logger) -> str | None:
    """Submit query, return queue path or None."""
    url = f"{API_BASE}/api/przypadki-pomocy"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=HEADERS,
                                timeout=30, verify=False, allow_redirects=False)
            if resp.status_code == 303:
                return resp.headers.get('Location', '')
            elif resp.status_code == 429:
                log.warning(f"  Rate limited, waiting {30*(attempt+1)}s")
                time.sleep(30 * (attempt + 1))
            elif resp.status_code == 400:
                body = resp.json() if resp.text else {}
                reason = body.get('error', {}).get('error-reason', '?')
                log.warning(f"  API 400: {reason}")
                return None
            else:
                log.warning(f"  Submit: HTTP {resp.status_code}")
                time.sleep(5)
        except requests.exceptions.RequestException as e:
            log.warning(f"  Submit error: {e}")
            time.sleep(5)
    return None


def _poll(queue_path: str, log: logging.Logger) -> str | None:
    """Poll queue until result ready. Returns result path or None."""
    url = _abs_url(queue_path)
    for attempt in range(POLL_MAX):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=30,
                                verify=False, allow_redirects=False)
            if resp.status_code == 303:
                return resp.headers.get('Location', '')
            elif resp.status_code == 200:
                if attempt < POLL_MAX - 1:
                    time.sleep(POLL_INTERVAL)
            else:
                log.warning(f"  Poll: HTTP {resp.status_code}")
                return None
        except requests.exceptions.RequestException as e:
            log.warning(f"  Poll error: {e}")
            if attempt < POLL_MAX - 1:
                time.sleep(POLL_INTERVAL)
    return None


def _fetch(result_path: str, log: logging.Logger) -> dict | None:
    """Fetch result JSON."""
    url = _abs_url(result_path)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=300,
                            verify=False, allow_redirects=False)
        if resp.status_code == 200:
            return resp.json()
        log.warning(f"  Fetch: HTTP {resp.status_code}")
    except Exception as e:
        log.warning(f"  Fetch error: {e}")
    return None


# ---------------------------------------------------------------------------
# Batch async query engine
# ---------------------------------------------------------------------------
def _batch_query(jobs: list[dict], log: logging.Logger
                  ) -> list[tuple[dict, int, list]]:
    """Execute a batch of async queries with shared wait time.

    Args:
        jobs: list of dicts, each with 'params' and 'label' keys
    Returns:
        list of (job, total_count, records) tuples
    """
    results = []

    # Phase 1: Submit all queries
    queue_paths = []
    for job in jobs:
        path = _submit(job['params'], log)
        queue_paths.append(path)
        if path:
            time.sleep(INTER_SUBMIT_DELAY)

    # Phase 2: Wait for processing (shared wait for all queries)
    time.sleep(POLL_WAIT)

    # Phase 3: Poll all and fetch results
    for job, queue_path in zip(jobs, queue_paths):
        if queue_path is None:
            results.append((job, 0, []))
            continue

        result_path = _poll(queue_path, log)
        if result_path is None:
            results.append((job, 0, []))
            continue

        data = _fetch(result_path, log)
        if data is None:
            results.append((job, 0, []))
            continue

        count = data.get('liczba-wynikow', 0)
        records = data.get('wyniki', [])
        log.info(f"    {job['label']}: {count:,} total, {len(records):,} returned")
        results.append((job, count, records))

    return results


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
def scrape(log: logging.Logger = None):
    """Scrape all Polish state aid from SUDOP.

    Two-phase approach:
      Phase 1: Query each forma-pomocy code (page 1) → get counts + first 10K
      Phase 2: Batch-paginate codes with >10K records

    Parallel batching: submit BATCH_SIZE queries, share 65s wait, fetch all.
    ~(65 + N*5)s per batch instead of N*70s sequential.
    """
    if log is None:
        log = logging.getLogger(__name__)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Get dictionary of aid form codes
    log.info("SUDOP: Loading forma-pomocy dictionary...")
    try:
        resp = requests.get(f"{API_BASE}/slownik/forma-pomocy",
                            headers=HEADERS, timeout=30, verify=False)
        forms = resp.json()
    except Exception as e:
        log.error(f"Cannot load forma-pomocy dictionary: {e}")
        return

    unique_codes = {}
    for f in forms:
        code = f['number']
        if code not in unique_codes:
            unique_codes[code] = f['name']
    log.info(f"  {len(unique_codes)} unique forma-pomocy codes")

    # -----------------------------------------------------------------------
    # Phase 1: Discovery — get page 1 for each code
    # -----------------------------------------------------------------------
    log.info("\n=== PHASE 1: Discovery (page 1 for each code) ===")
    total_records = 0
    pagination_needed = []  # (code, total_count, pages_needed)
    t0 = time.time()

    # Build jobs for codes that aren't cached yet
    discovery_jobs = []
    for code, name in sorted(unique_codes.items()):
        safe = code.replace('.', '_')
        cache_p1 = CACHE_DIR / f'{safe}_p1.json'
        if cache_p1.exists():
            with open(cache_p1, 'r', encoding='utf-8') as f:
                cached = json.load(f)
            count = cached.get('count', 0)
            records = cached.get('records', [])
            total_records += len(records)
            if count > PAGE_SIZE:
                pages = math.ceil(count / PAGE_SIZE)
                pagination_needed.append((code, count, pages))
            continue
        discovery_jobs.append({
            'params': {'forma-pomocy-kod': code, 'strona': 1},
            'label': f'{code} ({name[:40]})',
            'code': code,
        })

    cached_count = len(unique_codes) - len(discovery_jobs)
    log.info(f"  Already cached: {cached_count} codes, "
             f"remaining: {len(discovery_jobs)}")

    # Process discovery in batches
    for batch_start in range(0, len(discovery_jobs), BATCH_SIZE):
        batch = discovery_jobs[batch_start:batch_start + BATCH_SIZE]
        batch_num = batch_start // BATCH_SIZE + 1
        total_batches = math.ceil(len(discovery_jobs) / BATCH_SIZE)
        log.info(f"  Batch {batch_num}/{total_batches}")

        results = _batch_query(batch, log)

        for job, api_count, records in results:
            code = job['code']
            safe = code.replace('.', '_')
            cache_p1 = CACHE_DIR / f'{safe}_p1.json'

            cache_data = {'count': api_count, 'records': records}
            with open(cache_p1, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False)

            total_records += len(records)
            if api_count > PAGE_SIZE:
                pages_needed = math.ceil(api_count / PAGE_SIZE)
                pagination_needed.append((code, api_count, pages_needed))
                log.info(f"      → {code}: {api_count:,} total, "
                         f"needs {pages_needed} pages")

    elapsed1 = time.time() - t0
    log.info(f"\n  Phase 1 complete: {total_records:,} records from page 1s, "
             f"{elapsed1:.0f}s elapsed")
    log.info(f"  Codes needing pagination: {len(pagination_needed)}")

    # -----------------------------------------------------------------------
    # Phase 2: Paginate large codes
    # -----------------------------------------------------------------------
    if pagination_needed:
        log.info("\n=== PHASE 2: Pagination for large codes ===")
        _paginate_large_codes(pagination_needed, log)

    # Build final CSV
    _build_csv_from_cache(log)

    total_elapsed = time.time() - t0
    log.info(f"\nSUDOP scrape complete: {total_elapsed:.0f}s elapsed")


def _paginate_large_codes(codes: list, log: logging.Logger):
    """Paginate codes that have >10K records, using batch async queries.

    Uses the known total record count from Phase 1 discovery to determine
    the expected number of pages, rather than relying on empty responses
    (which may be transient API failures, not genuine end-of-data).
    """
    for code, est_count, est_pages in codes:
        safe = code.replace('.', '_')
        log.info(f"  Paginating {code} (est. {est_count:,} records, "
                 f"{est_pages} pages)")

        page = 2
        consecutive_empty = 0
        code_total = 0

        while page <= est_pages:
            # Check cache — but skip "poisoned" empty files where we
            # know more data should exist (page < est_pages)
            cache_file = CACHE_DIR / f'{safe}_p{page}.json'
            if cache_file.exists():
                with open(cache_file, 'r', encoding='utf-8') as f:
                    cached = json.load(f)
                n = len(cached)
                if n == 0 and page < est_pages:
                    # Poisoned cache file from a transient API failure —
                    # delete it and re-fetch
                    log.info(f"    {code} p{page}: deleting poisoned empty "
                             f"cache (page {page}/{est_pages})")
                    cache_file.unlink()
                else:
                    code_total += n
                    if n > 0 and n < PAGE_SIZE and page >= est_pages - 1:
                        log.info(f"    {code} p{page}: cached, {n:,} records "
                                 f"(final page)")
                        page += 1
                        break
                    page += 1
                    continue

            # Build batch of page queries
            batch_jobs = []
            for p in range(page, min(page + BATCH_SIZE, est_pages + 1)):
                pf = CACHE_DIR / f'{safe}_p{p}.json'
                if not pf.exists():
                    batch_jobs.append({
                        'params': {'forma-pomocy-kod': code, 'strona': p},
                        'label': f'{code} p{p}',
                        'code': code,
                        'page': p,
                    })

            if not batch_jobs:
                page += BATCH_SIZE
                continue

            results = _batch_query(batch_jobs, log)

            for job, _, records in results:
                p = job['page']
                pf = CACHE_DIR / f'{safe}_p{p}.json'

                if len(records) == 0:
                    # Transient failure — do NOT cache empty results
                    # when we know more pages should exist
                    consecutive_empty += 1
                    log.warning(f"    {code} p{p}: empty response "
                                f"({consecutive_empty} consecutive), "
                                f"NOT caching")
                    if consecutive_empty >= 5:
                        log.warning(f"    {code}: 5 consecutive empties, "
                                    f"pausing 120s before retry")
                        time.sleep(120)
                        consecutive_empty = 0  # Reset and try again
                else:
                    with open(pf, 'w', encoding='utf-8') as f:
                        json.dump(records, f, ensure_ascii=False)
                    code_total += len(records)
                    consecutive_empty = 0

            page += len(batch_jobs)

        log.info(f"    {code} pagination done: {code_total:,} additional "
                 f"records (pages 2-{page-1})")


def _build_csv_from_cache(log: logging.Logger):
    """Combine all cached JSON files into a single CSV."""
    files = sorted(CACHE_DIR.glob('*_p*.json'))
    if not files:
        log.warning("  No cached SUDOP data found")
        return

    log.info(f"  Building CSV from {len(files)} cached files...")
    all_records = []
    for f in files:
        with open(f, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
            # Discovery files have {count, records}, page files have [records]
            if isinstance(data, dict) and 'records' in data:
                all_records.extend(data['records'])
            elif isinstance(data, list):
                all_records.extend(data)

    if not all_records:
        log.warning("  All cached files are empty")
        return

    df = pd.DataFrame(all_records)
    # Deduplicate on core fields
    before = len(df)
    id_cols = ['nip-beneficjenta', 'dzien-udzielenia-pomocy',
               'wartosc-nominalna-pln', 'forma-pomocy-kod',
               'przeznaczenie-pomocy-kod']
    avail_id = [c for c in id_cols if c in df.columns]
    if avail_id:
        df = df.drop_duplicates(subset=avail_id, keep='first')
    after = len(df)
    if before != after:
        log.info(f"  Deduplicated: {before:,} → {after:,} ({before-after:,} dupes)")

    df.to_csv(RAW_CSV, index=False, encoding='utf-8')
    log.info(f"  Final CSV: {len(df):,} rows → {RAW_CSV.name}")


# ---------------------------------------------------------------------------
# Standardizer
# ---------------------------------------------------------------------------
def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load cached SUDOP data and standardize to common schema."""
    log.info("Loading TAM_PL (Poland SUDOP state aid) ...")

    project_root, cache_dir, raw_csv = _resolve_paths(data_dir)

    if not raw_csv.exists():
        files = sorted(cache_dir.glob('*_p*.json')) if cache_dir.exists() else []
        if files:
            _build_csv_from_cache(log)
        else:
            log.warning("  No Polish state aid data found.")
            log.warning("  Run: python -m src.data_cleaning.harmonization.tam_pl --scrape")
            return pd.DataFrame(columns=COMMON_COLUMNS)

    if not raw_csv.exists():
        return pd.DataFrame(columns=COMMON_COLUMNS)

    df = pd.read_csv(raw_csv, low_memory=False)
    log.info(f"  TAM_PL raw: {len(df):,} rows")
    return _standardize(df, log)


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map SUDOP data to common schema.

    SUDOP columns → schema mapping:
      nazwa-beneficjenta → beneficiary_name
      dzien-udzielenia-pomocy → year
      wartosc-brutto-eur → amount_eur (already EUR, ECB-converted by SUDOP)
      sektor-dzialalnosci-kod → nace_2digit (first 2 digits of PKD code)
      forma-pomocy-nazwa → instrument_subtype / financial_instrument_class
      srodek-pomocowy-numer → source_record_id (SA number)
      przeznaczenie-pomocy-nazwa → description / policy_domain
      nazwa-udzielajacego-pomocy → granting_authority (packed in original_columns)
    """
    log.info("Standardising TAM_PL ...")

    def _col(name: str) -> str | None:
        if name in df.columns:
            return name
        alt = name.replace('-', '_')
        return alt if alt in df.columns else None

    ben_col = _col('nazwa-beneficjenta')
    date_col = _col('dzien-udzielenia-pomocy')
    amt_eur_col = _col('wartosc-brutto-eur')
    amt_pln_col = _col('wartosc-brutto-pln')
    forma_kod_col = _col('forma-pomocy-kod')
    forma_nazwa_col = _col('forma-pomocy-nazwa')
    purpose_col = _col('przeznaczenie-pomocy-nazwa')
    purpose_kod_col = _col('przeznaczenie-pomocy-kod')
    sector_col = _col('sektor-dzialalnosci-kod')
    sector_name_col = _col('sektor-dzialalnosci-nazwa')
    measure_col = _col('srodek-pomocowy-numer')
    measure_name_col = _col('srodek-pomocowy-nazwa')
    authority_col = _col('nazwa-udzielajacego-pomocy')
    nip_col = _col('nip-beneficjenta')
    size_col = _col('wielkosc-beneficjenta-nazwa')
    gmina_col = _col('gmina-siedziby-nazwa')

    if ben_col is None:
        log.error(f"  Cannot find beneficiary column. Available: {list(df.columns)}")
        return pd.DataFrame(columns=COMMON_COLUMNS)

    out = pd.DataFrame(index=df.index)
    out['source'] = 'TAM'

    if measure_col and measure_col in df.columns:
        out['source_record_id'] = df[measure_col].fillna('').astype(str)
    else:
        out['source_record_id'] = df.index.astype(str)

    out['granularity'] = 'award'
    out['beneficiary_name'] = df[ben_col]
    out['country'] = 'PL'

    # Amount in EUR — SUDOP provides wartosc-brutto-eur (ECB-converted)
    if amt_eur_col and amt_eur_col in df.columns:
        out['amount_eur'] = safe_to_numeric(df[amt_eur_col], log, 'wartosc-brutto-eur')
        log.info("  Using wartosc-brutto-eur (SUDOP ECB-converted)")
    elif amt_pln_col and amt_pln_col in df.columns:
        PLN_EUR = {2007: 3.78, 2008: 3.51, 2009: 4.33, 2010: 3.99,
                   2011: 4.12, 2012: 4.19, 2013: 4.20, 2014: 4.18,
                   2015: 4.18, 2016: 4.36, 2017: 4.26, 2018: 4.26,
                   2019: 4.30, 2020: 4.44, 2021: 4.57, 2022: 4.69,
                   2023: 4.54, 2024: 4.31, 2025: 4.20}
        amounts_pln = safe_to_numeric(df[amt_pln_col], log, 'wartosc-brutto-pln')
        if date_col:
            years = pd.to_datetime(df[date_col], errors='coerce').dt.year
            rates = years.map(PLN_EUR).fillna(4.40)
            out['amount_eur'] = amounts_pln / rates
        else:
            out['amount_eur'] = amounts_pln / 4.40
        log.info("  Converted PLN → EUR using annual average rates")
    else:
        out['amount_eur'] = 0.0

    out['amount_type'] = 'grant'

    if date_col:
        out['year'] = pd.to_datetime(df[date_col], errors='coerce').dt.year
    else:
        out['year'] = None

    if sector_col and sector_col in df.columns:
        out['nace_2digit'] = (df[sector_col].astype(str)
                              .str.replace('.', '', regex=False)
                              .str[:2]
                              .replace({'na': None, 'nan': None, '': None}))
    else:
        out['nace_2digit'] = None

    out['sector_description'] = (df[sector_name_col]
                                 if sector_name_col and sector_name_col in df.columns
                                 else None)
    out['description'] = (df[purpose_col]
                          if purpose_col and purpose_col in df.columns
                          else None)
    out['overlap_flags'] = ''

    # Pack original columns — minimal set to keep CSV manageable at 15M+ rows
    # Full originals would create a 20GB+ CSV that can't fit in 32GB RAM
    pack_cols = [c for c in [measure_col, forma_kod_col, nip_col]
                 if c is not None and c in df.columns]
    packed = df[pack_cols].copy()
    packed['_src'] = 'tam_pl'
    out['original_columns'] = packed.apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    out['programme'] = (df[measure_name_col]
                        if measure_name_col and measure_name_col in df.columns
                        else None)
    out['fund'] = None
    out['programming_period'] = None
    out['instrument_subtype'] = (df[forma_nazwa_col]
                                 if forma_nazwa_col and forma_nazwa_col in df.columns
                                 else None)
    out['policy_domain'] = (df[purpose_col]
                            if purpose_col and purpose_col in df.columns
                            else None)

    out['year_paid'] = None
    out['flow_stage'] = 'granted'
    out['financial_instrument_class'] = (
        df[forma_nazwa_col].apply(_classify_pl_instrument)
        if forma_nazwa_col and forma_nazwa_col in df.columns
        else None)
    out['management_type'] = None
    out['legal_basis'] = None
    out['budget_line_code'] = None
    out['budget_execution_type'] = None

    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='national_budget',
                     resolution_level='beneficiary')

    log.info(f"  TAM_PL standardised: {len(out):,} rows, "
             f"EUR {out['amount_eur'].sum():,.0f}")
    return out[COMMON_COLUMNS]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')
    log = logging.getLogger(__name__)

    if '--scrape' in sys.argv:
        scrape(log)
    elif '--build-csv' in sys.argv:
        _build_csv_from_cache(log)
    else:
        log.info("Usage:")
        log.info("  python -m src.data_cleaning.harmonization.tam_pl --scrape     Full scrape")
        log.info("  python -m src.data_cleaning.harmonization.tam_pl --build-csv  Build CSV from cache")
        log.info("")
        log.info("  SUDOP API: async 3-step pattern (submit → poll → fetch)")
        log.info("  Phase 1: ~52 codes × ~80s batch = ~25 min")
        log.info("  Phase 2: Pagination for large codes, batched")
        log.info("  Cache: external_enrichment/cache/sudop/")
