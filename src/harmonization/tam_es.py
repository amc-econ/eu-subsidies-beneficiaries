"""
harmonization/tam_es.py
=======================
Standardize Spanish state aid data from the BDNS API (supplement to TAM).

Source:  BDNS (Base de Datos Nacional de Subvenciones)
API:     https://www.pap.hacienda.gob.es/bdnstrans/api/ayudasestado/busqueda
Rows:    ~6.3M state aid awards (all Spanish state aid)
Amounts: Already in EUR (Spain is Eurozone).
Country: ES (hardcoded)

The scraper paginates through the ayudasestado endpoint using the
pap.hacienda.gob.es mirror which supports pageSize=1000 (vs 50 on
infosubvenciones.es), reducing total pages from ~126K to ~6.3K.

Pages are cached as JSON files for resume capability.

Usage (scrape only):
    python -m src.harmonization.tam_es --scrape

Usage (standardize from cached data):
    Called from tam_supplements.py via standardize(data_dir, log)
"""

import json
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import requests

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    classify_instrument,
    pack_originals,
    safe_to_numeric,
)

# ---------------------------------------------------------------------------
# API config
# ---------------------------------------------------------------------------
# pap.hacienda.gob.es supports pageSize up to 1000 (infosubvenciones.es caps at 50)
API_BASE = 'https://www.pap.hacienda.gob.es/bdnstrans/api'
PAGE_SIZE = 1000
REQUEST_DELAY = 1.5   # seconds between launching threads
MAX_RETRIES = 5
TIMEOUT = 120
WORKERS = 3           # concurrent API requests (6 causes ~50% throttling)

HEADERS = {
    'Accept': 'application/json',
    'User-Agent': 'Bruegel-Research/1.0 (academic research)',
}

# These are defaults — overridden at runtime when data_dir is passed to standardize()
_DEFAULT_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
CACHE_DIR = _DEFAULT_PROJECT_ROOT / 'external_enrichment' / 'cache' / 'bdns_v2'
RAW_CSV = _DEFAULT_PROJECT_ROOT / 'data' / 'raw' / 'bdns_state_aid_raw.csv'


def _resolve_paths(data_dir: Path | None = None):
    """Compute project paths from data_dir (which points to data/raw/)."""
    if data_dir is not None:
        project_root = data_dir.parent.parent
    else:
        project_root = _DEFAULT_PROJECT_ROOT
    cache_dir = project_root / 'external_enrichment' / 'cache' / 'bdns_v2'
    raw_csv = data_dir / 'bdns_state_aid_raw.csv' if data_dir else RAW_CSV
    return project_root, cache_dir, raw_csv


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------
def _api_get(endpoint: str, params: dict = None) -> dict | None:
    """GET request with retry logic and exponential backoff."""
    url = f"{API_BASE}{endpoint}"
    for attempt in range(MAX_RETRIES):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=TIMEOUT)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                wait = 10 * (attempt + 1)
                time.sleep(wait)
            elif resp.status_code >= 500:
                time.sleep(5 * (attempt + 1))
            else:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(2)
        except requests.exceptions.Timeout:
            if attempt < MAX_RETRIES - 1:
                time.sleep(5 * (attempt + 1))
        except requests.exceptions.RequestException:
            if attempt < MAX_RETRIES - 1:
                time.sleep(3 * (attempt + 1))
    return None


def _parse_beneficiary(raw: str) -> tuple[str, str]:
    """Parse 'NIF - Name' format into (nif, name)."""
    if not raw or pd.isna(raw):
        return ('', '')
    raw = str(raw).strip()
    if ' - ' in raw:
        parts = raw.split(' - ', 1)
        return (parts[0].strip(), parts[1].strip())
    return ('', raw)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
def scrape(log: logging.Logger = None):
    """Scrape all Spanish state aid records from BDNS and save to CSV.

    Uses pap.hacienda.gob.es with pageSize=1000 for ~6x faster scraping
    compared to infosubvenciones.es (which caps at 50/page).
    """
    if log is None:
        log = logging.getLogger(__name__)

    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Get total count
    data = _api_get('/ayudasestado/busqueda',
                    {'vpd': 'GE', 'page': 0, 'pageSize': 1})
    if not data:
        log.error("Cannot reach BDNS API")
        return
    total = data['totalElements']
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE
    log.info(f"BDNS state aid: {total:,} records, {total_pages:,} pages "
             f"(pageSize={PAGE_SIZE})")

    # Find already-cached pages
    cached = set()
    for f in CACHE_DIR.glob('p1k_*.json'):
        try:
            cached.add(int(f.stem.split('_')[1]))
        except ValueError:
            pass
    log.info(f"  Already cached: {len(cached):,} pages")

    # Build list of pages still needed
    remaining = [p for p in range(total_pages) if p not in cached]
    log.info(f"  Pages to fetch: {len(remaining):,}")

    errors = 0
    records_written = 0
    skipped_pages = []
    t0 = time.time()

    def _fetch_page(page: int) -> tuple[int, list | None]:
        """Fetch a single page, return (page_num, records_or_None)."""
        data = _api_get('/ayudasestado/busqueda',
                        {'vpd': 'GE', 'page': page, 'pageSize': PAGE_SIZE,
                         'order': 'codConcesion', 'address': 'asc'})
        if data and 'content' in data and data['content']:
            return (page, data['content'])
        return (page, None)

    # Process in batches of WORKERS concurrent requests
    batch_size = WORKERS
    for batch_start in range(0, len(remaining), batch_size):
        batch = remaining[batch_start:batch_start + batch_size]

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {}
            for page in batch:
                futures[pool.submit(_fetch_page, page)] = page
                time.sleep(REQUEST_DELAY)  # Stagger submissions slightly

            for fut in as_completed(futures):
                page, records = fut.result()
                cache_file = CACHE_DIR / f'p1k_{page:06d}.json'
                if records:
                    with open(cache_file, 'w', encoding='utf-8') as f:
                        json.dump(records, f, ensure_ascii=False)
                    records_written += len(records)
                else:
                    errors += 1
                    skipped_pages.append(page)

        done = batch_start + len(batch)
        if done % 50 < batch_size:
            elapsed = time.time() - t0
            rate = (done - errors) / elapsed * 60 if elapsed > 0 else 0
            eta = (len(remaining) - done) / rate if rate > 0 else 0
            cached_total = len(cached) + done - errors
            log.info(f"  {cached_total:,}/{total_pages:,} pages "
                     f"({cached_total/total_pages*100:.1f}%), "
                     f"{records_written:,} new records, "
                     f"{errors} errors, "
                     f"{rate:.0f} pages/min, "
                     f"ETA: {eta:.0f}min")

    # Retry skipped pages sequentially with longer delay
    if skipped_pages:
        log.info(f"\n  Retrying {len(skipped_pages)} failed pages...")
        retry_ok = 0
        for page in skipped_pages:
            cache_file = CACHE_DIR / f'p1k_{page:06d}.json'
            if cache_file.exists():
                continue
            time.sleep(3)
            _, records = _fetch_page(page)
            if records:
                with open(cache_file, 'w', encoding='utf-8') as f:
                    json.dump(records, f, ensure_ascii=False)
                records_written += len(records)
                retry_ok += 1
        log.info(f"  Retry: {retry_ok}/{len(skipped_pages)} recovered")

    log.info(f"BDNS scrape phase complete: {records_written:,} new records, "
             f"{errors} errors")

    # Build final CSV from all cached pages
    _build_csv_from_cache(log)


def _build_csv_from_cache(log: logging.Logger):
    """Combine all cached pages into a single CSV."""
    pages = sorted(CACHE_DIR.glob('p1k_*.json'))
    if not pages:
        # Also check old-format cache files
        pages = sorted(CACHE_DIR.parent.glob('bdns_full/page_*.json'))
    if not pages:
        log.warning("  No cached pages found")
        return

    log.info(f"  Building CSV from {len(pages):,} cached pages...")
    records = []
    for pf in pages:
        with open(pf, 'r', encoding='utf-8') as f:
            records.extend(json.load(f))

    df = pd.DataFrame(records)
    df.to_csv(RAW_CSV, index=False, encoding='utf-8')
    log.info(f"  Final CSV: {len(df):,} rows → {RAW_CSV.name}")


# ---------------------------------------------------------------------------
# Standardizer
# ---------------------------------------------------------------------------
def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load cached BDNS data and standardize to common schema."""
    log.info("Loading TAM_ES (Spain BDNS state aid) ...")

    project_root, cache_dir, raw_csv = _resolve_paths(data_dir)

    if not raw_csv.exists():
        # Try to build from cached pages
        pages = sorted(cache_dir.glob('p1k_*.json')) if cache_dir.exists() else []
        if not pages:
            # Also check old-format cache
            old_dir = project_root / 'external_enrichment' / 'cache' / 'bdns_full'
            pages = sorted(old_dir.glob('page_*.json')) if old_dir.exists() else []
        if pages:
            log.info(f"  Building from {len(pages):,} cached pages...")
            records = []
            for pf in pages:
                with open(pf, 'r', encoding='utf-8') as f:
                    records.extend(json.load(f))
            df = pd.DataFrame(records)
            df.to_csv(raw_csv, index=False, encoding='utf-8')
            log.info(f"  Built CSV: {len(df):,} rows")
        else:
            log.warning("  No BDNS data available. Run: python -m src.harmonization.tam_es --scrape")
            return pd.DataFrame(columns=COMMON_COLUMNS)

    df = pd.read_csv(raw_csv, low_memory=False)
    log.info(f"  TAM_ES raw: {len(df):,} rows")
    return _standardize(df, log)


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map BDNS state aid data to common schema."""
    log.info("Standardising TAM_ES ...")

    # Parse beneficiary
    parsed = df['beneficiario'].apply(_parse_beneficiary)
    nifs = parsed.apply(lambda x: x[0])
    names = parsed.apply(lambda x: x[1])

    # Parse year from fechaConcesion (format: YYYY-MM-DD)
    years = pd.to_datetime(df['fechaConcesion'], errors='coerce').dt.year

    out = pd.DataFrame(index=df.index)
    out['source'] = 'TAM'
    out['source_record_id'] = df['ayudaEstado'].fillna(
        df['codConcesion'].astype(str)).astype(str)
    out['granularity'] = 'award'
    out['beneficiary_name'] = names
    out['country'] = 'ES'
    out['amount_eur'] = safe_to_numeric(df['importe'], log, 'importe')
    out['amount_type'] = 'grant'
    out['year'] = years
    out['sector_description'] = df.get('sectores', pd.Series(dtype=str))
    out['nace_2digit'] = None  # BDNS has text sectors, not NACE codes
    out['description'] = df.get('objetivo', pd.Series(dtype=str))
    out['overlap_flags'] = ''

    # Pack original columns — minimal set to keep CSV manageable at 20M+ rows
    # Full originals would create a 26GB+ CSV that can't fit in 32GB RAM
    orig_cols = ['codConcesion', 'instrumento', 'ayudaEstado']
    avail = [c for c in orig_cols if c in df.columns]
    packed = df[avail].copy()
    packed['_src'] = 'tam_es'
    packed['_nif'] = nifs
    out['original_columns'] = packed.apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure
    out['programme'] = df.get('convocatoria', pd.Series(dtype=str))
    out['fund'] = None
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('instrumento', pd.Series(dtype=str))
    out['policy_domain'] = df.get('objetivo', pd.Series(dtype=str))

    # Audit validation
    out['year_paid'] = None
    out['flow_stage'] = 'granted'
    out['financial_instrument_class'] = df['instrumento'].apply(classify_instrument)
    out['management_type'] = None
    out['legal_basis'] = df.get('reglamento', pd.Series(dtype=str))
    out['budget_line_code'] = None
    out['budget_execution_type'] = None

    # Schema v2
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='national_budget',
                     resolution_level='beneficiary')

    log.info(f"  TAM_ES standardised: {len(out):,} rows, "
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
        log.info("  python -m src.harmonization.tam_es --scrape      Full scrape (~1h)")
        log.info("  python -m src.harmonization.tam_es --build-csv   Build CSV from cache")
        log.info("")
        log.info("  Progress is cached to external_enrichment/cache/bdns_v2/")
        log.info("  Uses pap.hacienda.gob.es (pageSize=1000, ~6.3K pages)")
