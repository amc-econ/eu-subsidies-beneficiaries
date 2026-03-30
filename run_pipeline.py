#!/usr/bin/env python3
"""
EU Subsidies Pipeline — Top-Level Orchestrator
================================================

Runs the full pipeline in sequence:
  1. Harmonize raw data -> standardized CSVs
  2. Run pre-matching enrichments (CORDIS bulk join, EIB promoter scraper)
  3. Build master dataset (MasterConfig flag-based exclusions)
  4. Match entities against a user-supplied company list
     → Automatic post-match enrichment (FTS-CORDIS, ETS, IPCEI, deep mining)
     → Automatic consolidation (GGE, dedup, group rollup, summaries)
     → Summary charts
  5. (Optional) Run automotive example with sector-specific analysis

Usage:
    # Run the core pipeline (harmonize + enrich + master)
    python run_pipeline.py

    # Match ANY company list — full pipeline with enrichment + consolidation
    python run_pipeline.py --stage match --company-list my_companies.csv

    # With optional config (parent groups, sector keywords, etc.)
    python run_pipeline.py --stage match --company-list my_companies.csv --config my_config.json

    # Run automotive example (builds company list, runs everything)
    python run_pipeline.py --stage automotive

Data layout:
    data/raw/          <- raw source files (TAM.dsv, FTS Excel, EIB.xlsx, etc.)
    data/processed/    <- harmonized CSVs + master_dataset.parquet
    data/reference/    <- IPCEI reference data (shipped with repo)
    data/processed/match_output/ <- per-run results (charts, CSVs)
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
)
log = logging.getLogger('run_pipeline')

# Repo root = directory containing this script
REPO_ROOT = Path(__file__).resolve().parent

# Pre-built master dataset — downloaded automatically on first run.
# After creating a GitHub Release and uploading the file, update this URL.
MASTER_PARQUET = REPO_ROOT / 'data' / 'processed' / 'master_dataset.parquet'
MASTER_DATASET_URL = (
    "https://github.com/amc-econ/eu-subsidies-beneficiaries"
    "/releases/download/v1.0/master_dataset.parquet"
)

# EC DG Competition state aid case registry — downloaded automatically on first run.
SA_CASE_JSON = REPO_ROOT / 'case-data-SA.json'
SA_CASE_JSON_URL = (
    "https://github.com/amc-econ/eu-subsidies-beneficiaries"
    "/releases/download/v1.0/case-data-SA.json"
)


def _ensure_master_dataset() -> bool:
    """Download master_dataset.parquet if not present. Returns True if ready."""
    if MASTER_PARQUET.exists() and MASTER_PARQUET.stat().st_size > 1_000_000:
        return True
    log.info("master_dataset.parquet not found locally.")
    log.info(f"Downloading from GitHub Releases (~1.7 GB)...")
    MASTER_PARQUET.parent.mkdir(parents=True, exist_ok=True)
    try:
        import urllib.request

        def _progress(count, block_size, total_size):
            if total_size > 0:
                pct = min(count * block_size * 100 / total_size, 100)
                mb = count * block_size / 1_000_000
                total_mb = total_size / 1_000_000
                print(f"\r  {mb:.0f} / {total_mb:.0f} MB ({pct:.1f}%)", end='', flush=True)

        urllib.request.urlretrieve(MASTER_DATASET_URL, MASTER_PARQUET, reporthook=_progress)
        print()
        log.info("Download complete.")
        return True
    except Exception as e:
        log.error(f"Download failed: {e}")
        log.error(f"Download manually from: {MASTER_DATASET_URL}")
        log.error(f"and place at: {MASTER_PARQUET}")
        return False


def _ensure_sa_case_json() -> bool:
    """Download case-data-SA.json if not present. Returns True if ready."""
    if SA_CASE_JSON.exists() and SA_CASE_JSON.stat().st_size > 10_000_000:
        return True
    log.info("case-data-SA.json not found locally.")
    log.info("Downloading from GitHub Releases (~638 MB)...")
    try:
        import urllib.request

        def _progress(count, block_size, total_size):
            if total_size > 0:
                pct = min(count * block_size * 100 / total_size, 100)
                mb = count * block_size / 1_000_000
                total_mb = total_size / 1_000_000
                print(f"\r  {mb:.0f} / {total_mb:.0f} MB ({pct:.1f}%)", end='', flush=True)

        urllib.request.urlretrieve(SA_CASE_JSON_URL, SA_CASE_JSON, reporthook=_progress)
        print()
        log.info("Download complete.")
        return True
    except Exception as e:
        log.error(f"Download failed: {e}")
        log.error(f"Download manually from: {SA_CASE_JSON_URL}")
        log.error(f"and place at: {SA_CASE_JSON}")
        return False


def stage_harmonize() -> None:
    """Stage 1: Harmonize raw data sources into standardized CSVs."""
    log.info("Stage 1: Harmonization")

    from src.harmonization.run_all import main as harmonize_main
    data_dir = REPO_ROOT / 'data' / 'raw'
    output_dir = REPO_ROOT / 'data' / 'processed'
    output_dir.mkdir(parents=True, exist_ok=True)
    harmonize_main(data_dir=data_dir, output_dir=output_dir)


def stage_enrich() -> None:
    """Stage 2: Run pre-matching enrichments (CORDIS bulk join, EIB promoter)."""
    log.info("Stage 2: Pre-matching enrichment")

    os.environ['SUBSIDIES_PROJECT_ROOT'] = str(REPO_ROOT)

    log.info("Running CORDIS enrichment...")
    try:
        from src.enrichment.cordis_enrichment import main as cordis_main
        cordis_main()
    except Exception as e:
        log.warning(f"CORDIS enrichment skipped: {e}")

    log.info("Running EIB promoter scraper...")
    try:
        from src.enrichment.eib_promoter_scraper import main as eib_main
        eib_main()
    except Exception as e:
        log.warning(f"EIB promoter scraper skipped: {e}")


def stage_master() -> None:
    """Stage 3: Build master dataset from standardized CSVs."""
    log.info("Stage 3: Master dataset build")

    from src.master.builder import main as master_main
    output_dir = REPO_ROOT / 'data' / 'processed'
    master_main(output_dir=output_dir)


def stage_match(
    company_list: str | None = None,
    aliases: str | None = None,
    output_dir: str | None = None,
    config_json: str | None = None,
    match_config=None,
    pdf_enrichment: bool = False,
    use_llm: bool = False,
) -> None:
    """Stage 4: Full matching pipeline — match + enrich + consolidate + charts.

    This is the main entry point for ANY company list. Runs:
      1. Fuzzy entity matching against master dataset
      2. Post-match enrichment (FTS-CORDIS, ETS, IPCEI, deep mining, forensics)
      3. Consolidation (GGE, dedup, group rollup, summary tables)
      4. Summary charts

    Parameters
    ----------
    match_config : MatchConfig, optional
        Pre-built MatchConfig (e.g., from automotive with Python FP patterns).
        If None, builds from config_json.
    """
    log.info("Stage 4: Entity matching + enrichment + consolidation")

    from src.matching.generic_matcher import MatchConfig, run_matching
    from src.paths import master_dataset_path, ENRICHMENT_DIR

    if not _ensure_master_dataset():
        return
    master_csv = master_dataset_path()

    if not company_list:
        log.error("--company-list is required for matching stage")
        return

    company_list_path = Path(company_list).resolve()
    aliases_path = Path(aliases).resolve() if aliases else None
    out = Path(output_dir).resolve() if output_dir else REPO_ROOT / 'data' / 'processed' / 'match_output'
    out.mkdir(parents=True, exist_ok=True)

    # Load config if provided
    config_data = {}
    if config_json:
        cfg_path = Path(config_json)
        if not cfg_path.is_absolute():
            cfg_path = REPO_ROOT / cfg_path
        if cfg_path.exists():
            with open(cfg_path) as f:
                config_data = json.load(f)
            log.info(f"Loaded config: {cfg_path.name}")

    # Build MatchConfig (unless pre-built one was passed)
    if match_config is None:
        match_config_overrides = config_data.get('match_config', {})
        config = MatchConfig(
            output_prefix=match_config_overrides.get('output_prefix', 'match'),
            exact_only_names=frozenset(match_config_overrides.get('exact_only_names', [])),
        )
    else:
        config = match_config
    prefix = config.output_prefix

    # ---- Step 1: Fuzzy matching ----
    log.info("\n--- Step 1: Entity Matching ---")
    run_matching(
        master_csv=master_csv,
        company_list_csv=company_list_path,
        aliases_json=aliases_path,
        output_dir=out,
        config=config,
    )

    match_log = out / 'match_log.csv'
    enrichment_out = ENRICHMENT_DIR
    enrichment_out.mkdir(parents=True, exist_ok=True)

    # ---- Step 2: Post-match enrichment ----
    log.info("\n--- Step 2: Post-Match Enrichment ---")
    sector_keywords = config_data.get('sector_keywords', None)

    # FTS-CORDIS bridge
    log.info("\nRunning FTS-CORDIS bridge...")
    try:
        from src.enrichment.fts_cordis_bridge import run_fts_cordis_bridge
        run_fts_cordis_bridge(
            company_list_csv=str(company_list_path),
            aliases_json=str(aliases_path) if aliases_path else None,
            output_dir=str(enrichment_out),
            sector_keywords=sector_keywords,
        )
    except Exception as e:
        log.warning(f"FTS-CORDIS bridge skipped: {e}")

    # ETS free allocation
    nace_filter = config_data.get('nace_filter', None)
    if isinstance(nace_filter, list):
        nace_filter = nace_filter[0] if nace_filter else None
    log.info("\nRunning ETS enrichment...")
    try:
        from src.enrichment.ets_free_allocation import run_ets_enrichment
        run_ets_enrichment(
            company_list_csv=str(company_list_path),
            aliases_json=str(aliases_path) if aliases_path else None,
            output_dir=str(enrichment_out),
            nace_filter=nace_filter,
        )
    except Exception as e:
        log.warning(f"ETS enrichment skipped: {e}")

    # IPCEI reference
    log.info("\nRunning IPCEI enrichment...")
    try:
        from src.enrichment.ipcei_reference import run_ipcei_enrichment
        run_ipcei_enrichment(
            company_list_csv=str(company_list_path),
            aliases_json=str(aliases_path) if aliases_path else None,
            output_dir=str(enrichment_out),
        )
    except Exception as e:
        log.warning(f"IPCEI enrichment skipped: {e}")

    # FTS deep mining
    log.info("\nRunning FTS deep mining...")
    try:
        from src.enrichment.fts_deep_mining import run_fts_deep_mining
        run_fts_deep_mining(
            company_list_csv=str(company_list_path),
            matched_csv=str(match_log),
            output_dir=str(enrichment_out),
            sector_keywords=sector_keywords,
        )
    except Exception as e:
        log.warning(f"FTS deep mining skipped: {e}")

    # High-value forensics
    log.info("\nRunning high-value forensics...")
    try:
        from src.enrichment.highvalue_forensics import run_highvalue_forensics
        run_highvalue_forensics(
            company_list_csv=str(company_list_path),
            matched_csv=str(match_log),
            output_dir=str(enrichment_out),
            nace_filter=nace_filter,
        )
    except Exception as e:
        log.warning(f"High-value forensics skipped: {e}")

    # ---- Step 3: Consolidation ----
    log.info("\n--- Step 3: Consolidation ---")
    from src.matching.consolidation import consolidate

    # Resolve parent_groups path from config
    parent_groups = config_data.get('parent_groups', None)
    if parent_groups and not Path(parent_groups).is_absolute():
        parent_groups = str(REPO_ROOT / parent_groups)

    consolidate(
        match_log_csv=match_log,
        output_dir=out,
        parent_groups=parent_groups,
        enrichment_dir=enrichment_out,
        prefix=prefix,
        company_list_csv=str(company_list_path),
        aliases_json=str(aliases_path) if aliases_path else None,
        run_pdf_enrichment=pdf_enrichment,
        use_llm=use_llm,
    )

    # ---- Step 4: Summary charts ----
    log.info("\n--- Step 4: Summary Charts ---")
    try:
        from src.visualisations.summary_charts import generate_summary_charts
        charts_dir = out / 'charts'
        consolidated_csv = out / 'consolidated_matches.csv'
        generate_summary_charts(consolidated_csv, charts_dir, prefix=prefix)
    except Exception as e:
        log.warning(f"Chart generation skipped: {e}")

    log.info("Matching complete — results in %s", out)


def stage_enrich_pdf(
    consolidated_csv: str | None = None,
    use_llm: bool = False,
) -> None:
    """Enrich an existing consolidated_matches.csv with SA PDF co-financing data.

    Runs AFTER the match pipeline — no need to re-run matching. Loads the
    existing consolidated output, adds sa_cofin_* columns by parsing EC state aid
    decision PDFs for all TAM rows, then re-runs _flag_pdf_cofin_overlaps to
    update dc_preferred / dc_flag, and saves the file in place.

    Usage:
        python run_pipeline.py --stage enrich-pdf \\
            --consolidated data/processed/match_output/automotive/consolidated_matches.csv
        python run_pipeline.py --stage enrich-pdf \\
            --consolidated path/to/consolidated_matches.csv --use-llm
    """
    import pandas as pd
    from src.enrichment.sa_pdf_parser import SACofinParser
    from src.enrichment.sa_case_lookup import SACaseLookup
    from src.matching.consolidation import _flag_pdf_cofin_overlaps

    if not consolidated_csv:
        log.error("--consolidated is required for enrich-pdf stage")
        return

    csv_path = Path(consolidated_csv).resolve()
    if not csv_path.exists():
        log.error(f"File not found: {csv_path}")
        return

    log.info(f"Loading {csv_path.name} ...")
    df = pd.read_csv(csv_path, low_memory=False)
    log.info(f"  {len(df):,} rows, {(df['source'] == 'TAM').sum():,} TAM rows")

    # sa_case column: normalised SA code derived from source_record_id on TAM rows.
    # This column is expected by SACofinParser.enrich_dataframe(). It is populated
    # automatically when running through the full consolidation pipeline (Phase 2b),
    # but may be absent on CSV files produced by an earlier pipeline version.
    from src.enrichment.sa_case_lookup import normalise_sa
    if 'sa_case' not in df.columns:
        df['sa_case'] = ''
    tam_mask = df['source'] == 'TAM'
    df.loc[tam_mask, 'sa_case'] = df.loc[tam_mask, 'source_record_id'].astype(str).map(normalise_sa)

    # Load SA case lookup — auto-download if missing
    sa_json = REPO_ROOT / 'case-data-SA.json'
    if not sa_json.exists():
        if not _ensure_sa_case_json():
            log.error("case-data-SA.json unavailable — skipping PDF enrichment")
            return
    sa_lookup = SACaseLookup(sa_json).load()

    # PDF enrichment — adds sa_cofin_* columns to TAM rows
    pdf_cache = csv_path.parent / 'sa_decisions'
    parser = SACofinParser(cache_dir=pdf_cache, use_llm=use_llm)
    log.info("Running PDF co-financing enrichment...")
    df = parser.enrich_dataframe(df, sa_lookup)

    # Re-run PDF-backed dedup now that sa_cofin_* columns are populated
    # Ensure dc_flag + dc_preferred exist (they should from the original run)
    if 'dc_flag' not in df.columns:
        df['dc_flag'] = ''
    if 'dc_preferred' not in df.columns:
        df['dc_preferred'] = True
    if 'cofinancing_partner_id' not in df.columns:
        df['cofinancing_partner_id'] = ''

    log.info("Re-running PDF co-financing overlap detection...")
    df = _flag_pdf_cofin_overlaps(df)

    # Save in place (backup original)
    backup = csv_path.with_name(csv_path.stem + '_pre_pdf.csv')
    import shutil
    shutil.copy2(csv_path, backup)
    log.info(f"  Backup saved → {backup.name}")

    df.to_csv(csv_path, index=False)
    log.info(f"  Updated file saved → {csv_path}")

    # Summary
    n_cofin = (df.get('sa_cofin_level', '') == 'confirmed').sum() if 'sa_cofin_level' in df.columns else 0
    n_flagged = df['dc_flag'].str.contains('_pdf', na=False).sum()
    log.info(f"  TAM rows with confirmed co-financing: {n_cofin}")
    log.info(f"  Rows flagged by PDF dedup: {n_flagged}")


def stage_automotive() -> None:
    """Stage 5: Run the automotive worked example using the pre-built company list."""
    log.info("Stage 5: Automotive example")

    from examples.automotive.build_company_list import build_company_list

    # Build automotive company list from ORBIS + EV volumes
    lists_dir = REPO_ROOT / 'examples' / 'automotive' / 'company_lists'
    build_company_list(output_dir=lists_dir)

    company_csv = lists_dir / 'automotive_companies.csv'
    aliases_json = lists_dir / 'automotive_aliases.json'
    out_dir = REPO_ROOT / 'data' / 'processed' / 'match_output' / 'automotive'
    config_json = REPO_ROOT / 'examples' / 'automotive' / 'config' / 'pipeline_config.json'

    # Build MatchConfig with Python-based FP patterns (can't be serialized to JSON)
    from src.matching.generic_matcher import MatchConfig
    from examples.automotive.config import (
        AUTOMOTIVE_CONTEXTUAL_BLOCKLIST,
        AUTOMOTIVE_EXACT_ONLY,
        AUTOMOTIVE_FP_PAIRS,
        AUTOMOTIVE_FP_PATTERNS,
    )

    auto_match_config = MatchConfig(
        output_prefix='automotive',
        exact_only_names=AUTOMOTIVE_EXACT_ONLY,
        contextual_blocklist=AUTOMOTIVE_CONTEXTUAL_BLOCKLIST,
        false_positive_pairs=AUTOMOTIVE_FP_PAIRS,
        beneficiary_fp_patterns=AUTOMOTIVE_FP_PATTERNS,
    )

    # Run the full generic pipeline with automotive config + Python FP patterns
    stage_match(
        company_list=str(company_csv),
        aliases=str(aliases_json) if aliases_json.exists() else None,
        output_dir=str(out_dir),
        config_json=str(config_json),
        match_config=auto_match_config,
    )

    # Run automotive-specific extras (nationality, sector analysis, narrative)
    log.info("\nRunning automotive-specific analysis...")
    try:
        from examples.automotive.consolidation import run_automotive_extras
        run_automotive_extras(out_dir)
    except Exception as e:
        log.warning(f"Automotive extras skipped: {e}")

    # Generate automotive-specific presentation charts
    log.info("\nGenerating automotive presentation charts...")
    try:
        from examples.automotive.presentation_charts import main as charts_main
        charts_main(label='Automotive')
    except Exception as e:
        log.warning(f"Automotive chart generation skipped: {e}")


def main():
    parser = argparse.ArgumentParser(
        description='EU Subsidies Pipeline — harmonize, enrich, build master, match, consolidate',
    )
    parser.add_argument(
        '--stage', '-s',
        choices=['harmonize', 'enrich', 'master', 'match', 'enrich-pdf', 'automotive', 'all'],
        default=None,
        help='Pipeline stage to run',
    )
    parser.add_argument(
        '--company-list', '-c',
        help='Path to company list CSV (required for --stage match)',
    )
    parser.add_argument(
        '--aliases', '-a',
        help='Path to aliases JSON (optional)',
    )
    parser.add_argument(
        '--output-dir', '-o',
        help='Output directory for matching results',
    )
    parser.add_argument(
        '--config', '-cfg',
        help='Path to JSON config (parent_groups, sector_keywords, match overrides)',
    )
    parser.add_argument(
        '--consolidated',
        help='Path to consolidated_matches.csv — used by --stage enrich-pdf',
    )
    parser.add_argument(
        '--pdf-enrichment',
        action='store_true',
        default=True,
        help=(
            'Download and parse EC state aid decision PDFs to detect EU fund co-financing '
            'for TAM rows. Adds sa_cofin_* columns and enables PDF-backed duplicate detection '
            'across all EU fund sources. PDFs are cached in output_dir/sa_decisions/. '
            'Enabled by default — use --no-pdf-enrichment to skip. '
            'Requires: pip install pdfplumber pymupdf4llm'
        ),
    )
    parser.add_argument(
        '--no-pdf-enrichment',
        dest='pdf_enrichment',
        action='store_false',
        help='Disable PDF co-financing enrichment (not recommended — reduces dedup accuracy).',
    )
    parser.add_argument(
        '--use-llm',
        action='store_true',
        default=False,
        help=(
            'Use Claude Haiku as a fallback in PDF co-financing detection when regex finds '
            'no signal (non-English PDFs, footnote-fragmented sentences). Only active with '
            '--pdf-enrichment. Requires: pip install anthropic  and  ANTHROPIC_API_KEY env var. '
            '~$0.0014/PDF.'
        ),
    )
    parser.add_argument(
        '--download-data',
        action='store_true',
        help='Download master_dataset.parquet (~1.7 GB) without running any stage',
    )

    args = parser.parse_args()

    if args.download_data:
        _ensure_master_dataset()
        sys.exit(0)

    if args.stage is None:
        parser.print_help()
        print("\nQuick start:")
        print("  python run_pipeline.py --stage match --company-list my_companies.csv")
        sys.exit(0)

    stages = {
        'harmonize': stage_harmonize,
        'enrich': stage_enrich,
        'master': stage_master,
        'match': lambda: stage_match(
            args.company_list, args.aliases, args.output_dir, args.config,
            pdf_enrichment=args.pdf_enrichment,
            use_llm=args.use_llm,
        ),
        'enrich-pdf': lambda: stage_enrich_pdf(
            consolidated_csv=args.consolidated,
            use_llm=args.use_llm,
        ),
        'automotive': stage_automotive,
    }

    if args.stage == 'all':
        for name in ['harmonize', 'enrich', 'master']:
            stages[name]()
        log.info("\nCore pipeline complete. Run --stage match or --stage automotive for entity matching.")
    else:
        stages[args.stage]()

    log.info("Done.")


if __name__ == '__main__':
    main()
