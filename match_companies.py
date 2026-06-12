#!/usr/bin/env python3
"""Match a company list against the EU subsidies dataset.

Usage:
    python match_companies.py --company-list my_companies.csv

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

REPO_ROOT = Path(__file__).resolve().parent
RELEASE = 'https://github.com/amc-econ/eu-subsidies-beneficiaries/releases/download/v2.0'

# downloaded on first use: (local path, release asset, min size in MB)
ASSETS = {
    'master': (REPO_ROOT / 'data' / 'processed' / 'master_dataset.parquet',
               'master_dataset.parquet', 1000),
    'cordis': (REPO_ROOT / 'data' / 'reference' / 'cordis_participants.csv',
               'cordis_participants.csv', 10),
    'eutl': (REPO_ROOT / 'data' / 'reference' / 'eutl_2024_202410.zip',
             'eutl_2024_202410.zip', 30),
    'sa_cases': (REPO_ROOT / 'case-data-SA.json', 'case-data-SA.json', 300),
}


def _ensure(key: str) -> bool:
    path, asset, min_mb = ASSETS[key]
    if path.exists() and path.stat().st_size > min_mb * 1_000_000:
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


def run(company_list: str, aliases: str | None, output_dir: str | None,
        config_json: str | None, pdf_enrichment: bool, use_llm: bool) -> None:
    from src.matching.generic_matcher import MatchConfig, run_matching
    from src.paths import master_dataset_path, ENRICHMENT_DIR

    if not _ensure('master'):
        sys.exit(1)

    company_list_path = Path(company_list).resolve()
    aliases_path = Path(aliases).resolve() if aliases else None
    out = (Path(output_dir).resolve() if output_dir
           else REPO_ROOT / 'data' / 'processed' / 'match_output')
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

    log.info('--- Step 4: Summary charts ---')
    try:
        from src.visualisations.summary_charts import generate_summary_charts
        generate_summary_charts(out / 'consolidated_matches.csv', out / 'charts',
                                prefix=config.output_prefix)
    except Exception as e:
        log.warning(f'Chart generation skipped: {e}')

    log.info('Done — results in %s', out)


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
