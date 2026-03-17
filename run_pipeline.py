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


def stage_harmonize() -> None:
    """Stage 1: Harmonize raw data sources into standardized CSVs."""
    log.info("=" * 60)
    log.info("STAGE 1: HARMONIZATION")
    log.info("=" * 60)

    from src.data_cleaning.harmonization.run_all import main as harmonize_main
    data_dir = REPO_ROOT / 'data' / 'raw'
    output_dir = REPO_ROOT / 'data' / 'processed'
    output_dir.mkdir(parents=True, exist_ok=True)
    harmonize_main(data_dir=data_dir, output_dir=output_dir)


def stage_enrich() -> None:
    """Stage 2: Run pre-matching enrichments (CORDIS bulk join, EIB promoter)."""
    log.info("=" * 60)
    log.info("STAGE 2: PRE-MATCHING ENRICHMENT")
    log.info("=" * 60)

    os.environ['SUBSIDIES_PROJECT_ROOT'] = str(REPO_ROOT)

    log.info("Running CORDIS enrichment...")
    try:
        from src.data_extraction.enrichment.cordis_enrichment import main as cordis_main
        cordis_main()
    except Exception as e:
        log.warning(f"CORDIS enrichment skipped: {e}")

    log.info("Running EIB promoter scraper...")
    try:
        from src.data_extraction.enrichment.eib_promoter_scraper import main as eib_main
        eib_main()
    except Exception as e:
        log.warning(f"EIB promoter scraper skipped: {e}")


def stage_master() -> None:
    """Stage 3: Build master dataset from standardized CSVs."""
    log.info("=" * 60)
    log.info("STAGE 3: MASTER DATASET")
    log.info("=" * 60)

    from src.data_cleaning.master.builder import main as master_main
    output_dir = REPO_ROOT / 'data' / 'processed'
    master_main(output_dir=output_dir)


def stage_match(
    company_list: str | None = None,
    aliases: str | None = None,
    output_dir: str | None = None,
    config_json: str | None = None,
    match_config=None,
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
    log.info("=" * 60)
    log.info("STAGE 4: ENTITY MATCHING + ENRICHMENT + CONSOLIDATION")
    log.info("=" * 60)

    from src.data_extraction.matching.generic_matcher import MatchConfig, run_matching
    from src.paths import master_dataset_path, ENRICHMENT_DIR

    master_csv = master_dataset_path()
    if not master_csv.exists():
        log.error(f"Master dataset not found: {master_csv}")
        log.error("Run --stage master first.")
        return

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
        from src.data_extraction.enrichment.fts_cordis_bridge import run_fts_cordis_bridge
        run_fts_cordis_bridge(
            company_list_csv=str(company_list_path),
            aliases_json=str(aliases_path) if aliases_path else None,
            output_dir=str(enrichment_out),
            sector_keywords=sector_keywords,
        )
    except Exception as e:
        log.warning(f"FTS-CORDIS bridge skipped: {e}")

    # ETS free allocation
    log.info("\nRunning ETS enrichment...")
    try:
        from src.data_extraction.enrichment.ets_free_allocation import run_ets_enrichment
        nace_filter = config_data.get('nace_filter', None)
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
        from src.data_extraction.enrichment.ipcei_reference import run_ipcei_enrichment
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
        from src.data_extraction.enrichment.fts_deep_mining import run_fts_deep_mining
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
        from src.data_extraction.enrichment.highvalue_forensics import run_highvalue_forensics
        run_highvalue_forensics(
            company_list_csv=str(company_list_path),
            matched_csv=str(match_log),
            output_dir=str(enrichment_out),
        )
    except Exception as e:
        log.warning(f"High-value forensics skipped: {e}")

    # ---- Step 3: Consolidation ----
    log.info("\n--- Step 3: Consolidation ---")
    from src.data_extraction.matching.consolidation import consolidate

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

    log.info("\n" + "=" * 60)
    log.info("MATCHING PIPELINE COMPLETE")
    log.info("=" * 60)
    log.info(f"  Match results:      {match_log}")
    log.info(f"  Consolidated CSV:   {out / 'consolidated_matches.csv'}")
    log.info(f"  Group summary:      {out / 'group_summary.csv'}")
    log.info(f"  Charts:             {out / 'charts'}")


def stage_automotive() -> None:
    """Stage 5: Run automotive worked example using the pre-built company list."""
    log.info("=" * 60)
    log.info("STAGE 5: AUTOMOTIVE EXAMPLE")
    log.info("=" * 60)

    from examples.automotive.build_company_list import build_company_list

    # Build automotive company list from ORBIS + EV volumes
    lists_dir = REPO_ROOT / 'examples' / 'automotive' / 'company_lists'
    build_company_list(output_dir=lists_dir)

    company_csv = lists_dir / 'automotive_companies.csv'
    aliases_json = lists_dir / 'automotive_aliases.json'
    out_dir = REPO_ROOT / 'data' / 'processed' / 'match_output' / 'automotive'
    config_json = REPO_ROOT / 'examples' / 'automotive' / 'config' / 'pipeline_config.json'

    # Build MatchConfig with Python-based FP patterns (can't be serialized to JSON)
    from src.data_extraction.matching.generic_matcher import MatchConfig
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
        charts_main()
    except Exception as e:
        log.warning(f"Automotive chart generation skipped: {e}")


def main():
    parser = argparse.ArgumentParser(
        description='EU Subsidies Pipeline — harmonize, enrich, build master, match, consolidate',
    )
    parser.add_argument(
        '--stage', '-s',
        choices=['harmonize', 'enrich', 'master', 'match', 'automotive', 'all'],
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

    args = parser.parse_args()

    if args.stage is None:
        parser.print_help()
        print("\nQuick start:")
        print("  python run_pipeline.py --stage match --company-list my_companies.csv")
        sys.exit(0)

    stages = {
        'harmonize': stage_harmonize,
        'enrich': stage_enrich,
        'master': stage_master,
        'match': lambda: stage_match(args.company_list, args.aliases, args.output_dir, args.config),
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
