"""
master_builder.py
=================
Master dataset builder for the EU Subsidies pipeline.

Loads standardized CSVs produced by harmonize_all.py and assembles a single
master DataFrame according to a configurable inclusion policy.

CRITICAL: This module loads from STANDARDIZED FILES ONLY.
          It never re-reads raw data and never modifies the standardized CSVs.
          All inclusion/exclusion decisions are logged explicitly.

Usage:
    from master_builder import MasterConfig, build_master_dataset
    from pathlib import Path

    config = MasterConfig()  # default: safe, conservative headline
    df = build_master_dataset(output_dir=Path('analysis_output'), config=config)

Interpretation rules enforced here (DO NOT MODIFY — see README_ARCHITECTURE.md):
    1. RRF: treated as planned allocation, separate conceptual instrument.
       NOT deduplicated against TAM. Included alongside grants for planning context.
    2. SCOREBOARD: excluded from headline totals (same underlying data as TAM).
    3. FTS research_programme_overlap: excluded when RESEARCH is active.
    4. ESIF programme-level: excluded by default (overlaps with Kohesio).
    5. EIB/EBRD: optionally filtered to EU-27 only.
    6. COVID: optionally excluded (controlled by exclude_covid flag).
"""

import json
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# CONFIGURATION DATACLASS
# ---------------------------------------------------------------------------

@dataclass
class MasterConfig:
    """
    Controls which sources and rows are included in the master dataset.

    All defaults are conservative (safe for headline totals).
    """

    # Include SCOREBOARD in master?
    # DEFAULT False: Scoreboard aggregates the same underlying awards as TAM.
    # Adding both would massively double-count grants.
    include_scoreboard: bool = False

    # Include ESIF programme-level aggregates (ESIF_2014 / ESIF_2027)?
    # DEFAULT False: These overlap with Kohesio project-level data.
    # Use Kohesio for project-level analysis; ESIF files are contextual only.
    include_esif_programme_level: bool = False

    # Include CINEA non-HORIZON non-INNOVFUND rows (CEF, LIFE, EMFAF, etc.)?
    # DEFAULT True: Some overlap with FTS possible but minimal.
    include_cinea_other: bool = True

    # Include INNOVFUND rows?
    # DEFAULT True: INNOVFUND is EU ETS revenue — genuinely additive, not in FTS.
    include_innovfund: bool = True

    # Exclude FTS rows flagged 'research_programme_overlap'?
    # DEFAULT True: When RESEARCH is included, these FTS rows would double-count.
    exclude_fts_research_overlap: bool = True

    # Filter EIB and EBRD to EU-27 countries only?
    # DEFAULT True: Non-EU rows are not relevant for EU subsidy analysis.
    eu27_only_loans: bool = True

    # Exclude COVID-flagged rows?
    # DEFAULT False: COVID rows are legitimate state aid; exclusion is optional.
    exclude_covid: bool = False


# ---------------------------------------------------------------------------
# EU-27 (for loan filtering)
# ---------------------------------------------------------------------------

EU27 = {
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR',
    'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL',
    'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE',
}


# ---------------------------------------------------------------------------
# BUILDER
# ---------------------------------------------------------------------------

def build_master_dataset(
    output_dir: Path,
    config: MasterConfig | None = None,
    log: logging.Logger | None = None,
) -> pd.DataFrame:
    """
    Assemble master DataFrame from ALL standardized CSVs.

    Schema v2: No rows are deleted. All sources are loaded unconditionally.
    The is_primary_record flag (set by harmonizers and refined here by config)
    controls which rows are included in headline totals.

    Parameters
    ----------
    output_dir : Path
        Directory containing standardized_*.csv files (analysis_output/).
    config : MasterConfig, optional
        Inclusion policy. Defaults to MasterConfig() (conservative).
    log : logging.Logger, optional
        Logger. If None, logs to stdout at INFO level.

    Returns
    -------
    pd.DataFrame
        Combined master dataset with ALL rows. Filter on
        is_primary_record==True for headline totals.
    """
    if config is None:
        config = MasterConfig()
    if log is None:
        log = _default_logger()

    log.info("=" * 60)
    log.info("Building master dataset (schema v2: flag-based, no row deletion)")
    log.info(f"  Config: {config}")
    log.info("=" * 60)

    # ---- Load ALL sources unconditionally ----------------------------------
    all_sources = [
        'tam', 'fts', 'kohesio', 'research', 'eib', 'ebrd', 'rrf',
        'scoreboard', 'cinea', 'innovfund',
        'esif_2014_contextual_only', 'esif_2027_contextual_only',
        'tam_supplements',
    ]
    frames: list[pd.DataFrame] = []
    for src in all_sources:
        df = _load_csv(output_dir, src, log)
        if df is not None:
            frames.append(df)
            log.info(f"  [LOADED] {src.upper()}: {len(df):,} rows")

    if not frames:
        log.warning("No sources loaded — returning empty DataFrame")
        return pd.DataFrame()

    master = pd.concat(frames, ignore_index=True)
    log.info(f"\n  All sources concatenated: {len(master):,} rows")

    # ---- Ensure is_primary_record column exists ----------------------------
    if 'is_primary_record' not in master.columns:
        master['is_primary_record'] = True
    if 'exclude_reason' not in master.columns:
        master['exclude_reason'] = None
    # ---- Schema v3: extra_fields_json default ------------------------------
    # Harmonizers that have been upgraded to the aggressive-extraction
    # layer (EIB deep scraper, SA deep parser, CORDIS topic enricher,
    # KOHESIO intervention enricher, …) populate ``extra_fields_json``
    # with a per-row JSON object string carrying source-specific
    # metadata. Harmonizers that have NOT been upgraded simply omit the
    # column, and we backfill ``"{}"`` here so downstream code can
    # parse every row uniformly.
    if 'extra_fields_json' not in master.columns:
        master['extra_fields_json'] = '{}'
    else:
        mask_empty = master['extra_fields_json'].isna() | (master['extra_fields_json'] == '')
        if mask_empty.any():
            master.loc[mask_empty, 'extra_fields_json'] = '{}'

    # ---- Schema v3.1: is_anonymised structural filter ----------------------
    # Mark rows whose beneficiary_name is a bucket / anonymisation
    # sentinel rather than a real entity. The consolidation headline
    # filter in Phase 5c excludes these rows from published totals.
    from ..harmonization.utils import apply_anonymised_column
    apply_anonymised_column(master)
    n_anon = int(master['is_anonymised'].sum())
    if n_anon:
        n_eur = float(master.loc[master['is_anonymised'], 'amount_eur'].fillna(0).sum())
        log.info(
            f"  Anonymised sentinel scrub: {n_anon:,} rows flagged "
            f"({n_eur/1e9:.2f}B EUR face) across "
            f"{master.loc[master['is_anonymised'], 'source'].nunique()} sources"
        )

    # ---- Apply config-driven exclusion flags -------------------------------
    # These refine is_primary_record based on MasterConfig settings.
    # Rows already marked is_primary_record=False by harmonizers
    # (mega-schemes, scoreboard, ESIF) are untouched.

    # FTS research overlap
    if config.exclude_fts_research_overlap:
        mask = (
            (master['source'] == 'FTS') &
            master['overlap_flags'].str.contains('research_programme_overlap', na=False)
        )
        n = mask.sum()
        master.loc[mask, 'is_primary_record'] = False
        master.loc[mask & master['exclude_reason'].isna(), 'exclude_reason'] = 'research_programme_overlap'
        log.info(f"  FTS research overlap: {n:,} rows flagged non-primary")

    # EIB/EBRD non-EU filtering
    if config.eu27_only_loans:
        loan_mask = master['source'].isin(['EIB', 'EBRD'])
        non_eu = loan_mask & ~master['country'].isin(EU27)
        n = non_eu.sum()
        master.loc[non_eu, 'is_primary_record'] = False
        master.loc[non_eu & master['exclude_reason'].isna(), 'exclude_reason'] = 'non_eu'
        log.info(f"  EIB/EBRD non-EU: {n:,} rows flagged non-primary")

    # COVID exclusion
    if config.exclude_covid:
        covid_mask = master['overlap_flags'].str.contains('covid', na=False, case=False)
        n = covid_mask.sum()
        master.loc[covid_mask, 'is_primary_record'] = False
        master.loc[covid_mask & master['exclude_reason'].isna(), 'exclude_reason'] = 'covid'
        log.info(f"  COVID: {n:,} rows flagged non-primary")

    # CINEA/INNOVFUND config-driven inclusion
    if not config.include_cinea_other:
        mask = master['source'] == 'CINEA'
        master.loc[mask, 'is_primary_record'] = False
        master.loc[mask & master['exclude_reason'].isna(), 'exclude_reason'] = 'config_excluded'
    if not config.include_innovfund:
        mask = master['source'] == 'INNOVFUND'
        master.loc[mask, 'is_primary_record'] = False
        master.loc[mask & master['exclude_reason'].isna(), 'exclude_reason'] = 'config_excluded'

    # ---- Summary -----------------------------------------------------------
    primary = master[master['is_primary_record'] == True]
    excluded = master[master['is_primary_record'] == False]

    log.info(f"\n{'=' * 60}")
    log.info(f"Master dataset assembled: {len(master):,} total rows")
    log.info(f"  Primary records:  {len(primary):,} rows, EUR {primary['amount_eur'].sum():,.0f}")
    log.info(f"  Excluded records: {len(excluded):,} rows, EUR {excluded['amount_eur'].sum():,.0f}")
    log.info(f"  Sources: {sorted(master['source'].unique().tolist())}")
    if len(excluded) > 0:
        reason_counts = excluded['exclude_reason'].value_counts()
        for reason, count in reason_counts.items():
            eur = excluded.loc[excluded['exclude_reason'] == reason, 'amount_eur'].sum()
            log.info(f"    {reason}: {count:,} rows, EUR {eur:,.0f}")
    log.info(f"{'=' * 60}")

    return master


# ---------------------------------------------------------------------------
# HELPERS
# ---------------------------------------------------------------------------

def _load_csv(output_dir: Path, source_lower: str, log: logging.Logger) -> pd.DataFrame | None:
    path = output_dir / f'standardized_{source_lower}.csv'
    if not path.exists():
        log.debug(f"  {path.name}: not found (skipping)")
        return None
    try:
        df = pd.read_csv(path, low_memory=False)

        # 🔥 Force a valid source column
        source_name = source_lower.upper()
        # TAM supplements merge into TAM (same data type, different country portals)
        if source_name == 'TAM_SUPPLEMENTS':
            source_name = 'TAM'
        df['source'] = source_name  # overwrite whatever is there

        return df

    except Exception as e:
        log.warning(f"  Failed to load {path.name}: {e}")
        return None


def _default_logger() -> logging.Logger:
    """Create a simple stdout logger."""
    log = logging.getLogger('master_builder')
    if not log.handlers:
        log.setLevel(logging.INFO)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s',
                                          datefmt='%H:%M:%S'))
        log.addHandler(ch)
    return log

# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------

def main(output_dir: Path | None = None) -> None:
    """Build master dataset with default config and print summary."""
    log = _default_logger()
    if output_dir is None:
        import os
        project_root = Path(os.environ.get(
            'SUBSIDIES_PROJECT_ROOT',
            Path(__file__).resolve().parent.parent.parent.parent
        ))
        output_dir = project_root / 'data' / 'processed'
    config = MasterConfig()
    master = build_master_dataset(output_dir=output_dir, config=config, log=log)

    # Save to master_dataset.csv
    out_path = output_dir / 'master_dataset.csv'
    master.to_csv(out_path, index=False, encoding='utf-8-sig')
    log.info(f"Saved: {out_path}")


if __name__ == '__main__':
    main()
