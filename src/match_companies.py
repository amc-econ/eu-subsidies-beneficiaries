#!/usr/bin/env python3
"""Match a company list against the EU subsidies dataset.

Usage:
    python src/match_companies.py --company-list my_companies.csv

The company list is a CSV with a 'company_name' column and an optional
'country' column (ISO 2-letter). Results land in data/processed/match_output/.

The dataset and reference files are downloaded from this repository's
GitHub release on first run.
"""

import argparse
import json
import logging
import sys
import urllib.request
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger('match_companies')

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))
RELEASE = 'https://github.com/amc-econ/eu-subsidies-beneficiaries/releases/download/v2.0'
MATCH_OUTPUT_DIR = REPO_ROOT / 'data' / 'processed' / 'match_output'

# downloaded on first use: (local path, release asset, min size in bytes)
ASSETS = {
    'master': (REPO_ROOT / 'data' / 'processed' / 'master_dataset.parquet',
               'master_dataset.parquet', 1_000_000_000),
    'cordis': (REPO_ROOT / 'data' / 'reference' / 'cordis_participants.csv',
               'cordis_participants.csv', 10_000_000),
    'eutl': (REPO_ROOT / 'data' / 'reference' / 'eutl_2024_202410.zip',
             'eutl_2024_202410.zip', 30_000_000),
    'sa_cases': (REPO_ROOT / 'case-data-SA.json', 'case-data-SA.json',
                 300_000_000),
    'ipcei_overview': (REPO_ROOT / 'data' / 'reference' / 'ipcei_overview.csv',
                       'ipcei_overview.csv', 300),
    'ipcei_participants': (REPO_ROOT / 'data' / 'reference' /
                           'ipcei_participants.csv',
                           'ipcei_participants.csv', 1_000),
}


def _ensure(key: str) -> bool:
    path, asset, min_bytes = ASSETS[key]
    if path.exists() and path.stat().st_size > min_bytes:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f'{RELEASE}/{asset}'
    log.info(f'Downloading {asset} ...')

    def _progress(count, block, total):
        if total > 0 and count % 400 == 0:
            log.info(f'  {min(count * block * 100 / total, 100):.0f}%')

    try:
        urllib.request.urlretrieve(url, path, _progress)
        return True
    except Exception as e:
        log.error(f'Download failed: {e}')
        log.error(f'Fetch it manually from {url} and place it at {path}')
        return False


def _eur(x) -> str:
    """Compact euro label for the summary line."""
    if x is None:
        return 'n/a'
    a = abs(x)
    if a >= 1e9:
        return f'EUR {x / 1e9:.1f}B'
    if a >= 1e6:
        return f'EUR {x / 1e6:.0f}M'
    if a >= 1e3:
        return f'EUR {x / 1e3:.0f}K'
    return f'EUR {x:.0f}'


def _validate_company_list(path: Path) -> None:
    """Fail fast (before the ~1.7 GB download) if the list is unusable."""
    if not path.exists():
        log.error(f'Company list not found: {path}')
        sys.exit(1)
    import csv
    try:
        with open(path, newline='', encoding='utf-8-sig') as f:
            header = next(csv.reader(f), [])
    except Exception as e:
        log.error(f'Could not read company list {path}: {e}')
        sys.exit(1)
    cols = [h.strip() for h in header]
    if 'company_name' not in cols:
        log.error("Company list needs a 'company_name' column "
                  f"(optional 'country'). Found: {cols or 'no columns'}")
        sys.exit(1)


def _pdf_libs_available() -> bool:
    """True if the PDF text backend needed for co-financing de-dup is installed."""
    import importlib.util
    return importlib.util.find_spec('pdfplumber') is not None


def _print_summary(out: Path) -> None:
    """Print a tidy, plain-words summary of what the run produced."""
    chart_cmd = ('python make_charts.py' if out == MATCH_OUTPUT_DIR
                 else f'python make_charts.py "{out}"')
    headline = None
    cm = out / 'concentration_metrics.json'
    if cm.exists():
        try:
            headline = json.loads(cm.read_text(encoding='utf-8')).get('headline')
        except Exception:
            headline = None

    print()
    print('=' * 64)
    print(f'Done. Results in {out}')
    if headline:
        span = ''
        if headline.get('year_min') and headline.get('year_max'):
            span = f", {headline['year_min']}-{headline['year_max']}"
        print()
        print(f'  Total support{span}')
        print(f"    {_eur(headline.get('total_face_eur'))} face value"
              f"   |   {_eur(headline.get('total_gge_eur'))} grant-equivalent")
        print(f"    {headline.get('n_relations', 0):,} relations"
              f"   |   {headline.get('n_beneficiaries', 0):,} beneficiaries")
        if headline.get('top_beneficiary'):
            print(f"    largest: {headline['top_beneficiary']} "
                  f"({_eur(headline.get('top_beneficiary_eur'))})")
    print()
    print('Output files:')
    print('  consolidated_matches.csv   the full matched dataset')
    print('  T1..T8_*.csv               breakdowns by source, country,')
    print('                             instrument, year, top beneficiaries')
    print('  concentration_metrics.json HHI / Gini / top-5% share')
    print()
    print('Charts are optional and not generated automatically:')
    print('  pip install matplotlib')
    print(f'  {chart_cmd}')
    print('=' * 64)


def run(company_list: str, aliases: str | None, output_dir: str | None,
        config_json: str | None, pdf_enrichment: bool, use_llm: bool) -> None:
    from src.matching.generic_matcher import MatchConfig, run_matching
    from src.paths import master_dataset_path, ENRICHMENT_DIR

    company_list_path = Path(company_list).resolve()
    _validate_company_list(company_list_path)   # fail fast before any download

    if pdf_enrichment and not _pdf_libs_available():
        log.warning('PDF co-financing enrichment requires pdfplumber '
                    '(pip install -r requirements.txt); skipping.')
        pdf_enrichment = False

    if not _ensure('master'):
        sys.exit(1)

    aliases_path = Path(aliases).resolve() if aliases else None
    out = Path(output_dir).resolve() if output_dir else MATCH_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    config_data = {}
    if config_json and Path(config_json).exists():
        config_data = json.loads(Path(config_json).read_text(encoding='utf-8'))

    overrides = config_data.get('match_config', {})
    config = MatchConfig(
        output_prefix=overrides.get('output_prefix', 'match'),
        exact_only_names=frozenset(overrides.get('exact_only_names', [])),
    )

    log.info('--- Step 1: Entity matching ---')
    run_matching(
        master_csv=master_dataset_path(),
        company_list_csv=company_list_path,
        aliases_json=aliases_path,
        output_dir=out,
        config=config,
    )
    match_log = out / 'match_log.csv'
    ENRICHMENT_DIR.mkdir(parents=True, exist_ok=True)

    log.info('--- Step 2: Enrichment ---')
    sector_keywords = config_data.get('sector_keywords', None)
    nace_filter = config_data.get('nace_filter', None)
    if isinstance(nace_filter, list):
        nace_filter = nace_filter[0] if nace_filter else None

    if _ensure('cordis'):
        try:
            from src.enrichment.fts_cordis_bridge import run_fts_cordis_bridge
            run_fts_cordis_bridge(
                company_list_csv=str(company_list_path),
                aliases_json=str(aliases_path) if aliases_path else None,
                output_dir=str(ENRICHMENT_DIR),
                sector_keywords=sector_keywords,
            )
        except Exception as e:
            log.warning(f'FTS-CORDIS bridge skipped: {e}')

    if _ensure('eutl'):
        try:
            from src.enrichment.ets_free_allocation import run_ets_enrichment
            run_ets_enrichment(
                company_list_csv=str(company_list_path),
                aliases_json=str(aliases_path) if aliases_path else None,
                output_dir=str(ENRICHMENT_DIR),
                nace_filter=nace_filter,
            )
        except Exception as e:
            log.warning(f'ETS enrichment skipped: {e}')

    _ensure('ipcei_overview')
    _ensure('ipcei_participants')
    try:
        from src.enrichment.ipcei_reference import run_ipcei_enrichment
        run_ipcei_enrichment(
            company_list_csv=str(company_list_path),
            aliases_json=str(aliases_path) if aliases_path else None,
            output_dir=str(ENRICHMENT_DIR),
        )
    except Exception as e:
        log.warning(f'IPCEI enrichment skipped: {e}')

    try:
        from src.enrichment.fts_deep_mining import run_fts_deep_mining
        run_fts_deep_mining(
            company_list_csv=str(company_list_path),
            matched_csv=str(match_log),
            output_dir=str(ENRICHMENT_DIR),
            sector_keywords=sector_keywords,
        )
    except Exception as e:
        log.warning(f'FTS deep mining skipped: {e}')

    try:
        from src.enrichment.highvalue_forensics import run_highvalue_forensics
        run_highvalue_forensics(
            company_list_csv=str(company_list_path),
            matched_csv=str(match_log),
            output_dir=str(ENRICHMENT_DIR),
            nace_filter=nace_filter,
        )
    except Exception as e:
        log.warning(f'High-value forensics skipped: {e}')

    log.info('--- Step 3: Consolidation ---')
    from src.matching.consolidation import consolidate

    if pdf_enrichment and not _ensure('sa_cases'):
        pdf_enrichment = False

    parent_groups = config_data.get('parent_groups', None)
    if parent_groups and not Path(parent_groups).is_absolute():
        parent_groups = str(REPO_ROOT / parent_groups)

    consolidate(
        match_log_csv=match_log,
        output_dir=out,
        parent_groups=parent_groups,
        enrichment_dir=ENRICHMENT_DIR,
        prefix=config.output_prefix,
        company_list_csv=str(company_list_path),
        aliases_json=str(aliases_path) if aliases_path else None,
        run_pdf_enrichment=pdf_enrichment,
        use_llm=use_llm,
    )

    _print_summary(out)


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('--company-list', '-c', required=True,
                   help="CSV with a 'company_name' column (optional 'country')")
    p.add_argument('--aliases', '-a', help='aliases JSON (optional)')
    p.add_argument('--output-dir', '-o', help='output directory')
    p.add_argument('--config', help='JSON config (parent_groups, sector_keywords, ...)')
    p.add_argument('--skip-pdf-enrichment', action='store_true',
                   help='skip state-aid decision PDF parsing (faster; less '
                        'co-financing detail). Needs: pip install pdfplumber pymupdf4llm')
    p.add_argument('--use-llm', action='store_true',
                   help='LLM fallback in PDF parsing (needs anthropic + API key)')
    args = p.parse_args()
    run(args.company_list, args.aliases, args.output_dir, args.config,
        pdf_enrichment=not args.skip_pdf_enrichment, use_llm=args.use_llm)


if __name__ == '__main__':
    main()
