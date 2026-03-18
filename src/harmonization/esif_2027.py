"""
harmonization/esif_2027.py
==========================
Standardize ESIF 2021-2027 programme-level timeseries data.

Source type:    Programme-level aggregate timeseries (NOT project/beneficiary-level)
Granularity:    Programme × country × fund × year aggregate
Amount:         EU share of spending (actuals) or EU planned (fallback)
Coverage:       EU cohesion policy 2021-2027 (~EUR 392B total envelope)

CRITICAL INCLUSION RULE — DO NOT MODIFY:
    ESIF 2021-2027 programme-level data OVERLAPS with Kohesio project-level data.
    Kohesio is the preferred source (project-level, with beneficiary names).
    ESIF 2021-2027 is CONTEXTUAL ONLY — excluded from master dataset by default
    (MasterConfig.include_esif_programme_level=False).
    All rows are tagged overlap_flags='contextual_only:overlaps_with_kohesio'.
    Use ESIF programme files only for programme-level cross-validation.

CRITICAL DEDUPLICATION FILTER — DO NOT MODIFY:
    The source CSV is a timeseries: each programme × year combination has
    multiple snapshot rows (one per TOD — Transfer of Data — cycle).
    Only rows where is_latest_tod_cycle == 'Y' (string, NOT boolean True)
    must be loaded to avoid double-counting cumulative snapshots.
    This filter is non-negotiable and must never be removed.

Moved verbatim from deep_validation_analysis_v2.py standardize_esif_2027() (line 336).
"""

import logging
from pathlib import Path

import pandas as pd

from .utils import COMMON_COLUMNS, apply_v2_columns


def standardize(
    data_dir: Path,
    log: logging.Logger,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Load and standardize ESIF 2021-2027 timeseries CSV.

    Filters to is_latest_tod_cycle == "Y" to avoid double-counting timeseries entries.
    Aggregates at programme × country × fund × year level.

    Parameters
    ----------
    data_dir : Path
        Directory containing 2021-2027_Finances_Detailed_*.csv.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame]
        (standardized_df, raw_cci_df)
        - standardized_df: common schema rows, tagged contextual_only
        - raw_cci_df: raw aggregated rows with cci column (for overlap detection
          in check_esif_kohesio_overlap())
    """
    candidates = list(data_dir.glob('2021-2027_Finances_Detailed_*.csv'))
    if not candidates:
        log.warning("ESIF 2021-2027 file not found. Skipping.")
        return None, None
    path = candidates[0]
    log.info(f"Loading ESIF 2021-2027: {path.name} ...")

    # Read only needed columns (file is ~144MB)
    usecols_try = [
        'is_latest_tod_cycle',
        'ms',
        'fund',
        'cci',
        'programme_short_title',
        'tod_year',
        'EU planned',
        'EU share of decided',
        'EU share of spending',
    ]
    try:
        raw = pd.read_csv(path, usecols=usecols_try, low_memory=False)
    except ValueError as e:
        log.warning(f"  Column mismatch: {e}. Reading all columns.")
        raw = pd.read_csv(path, low_memory=False)

    log.info(f"  Raw ESIF 2021-2027: {len(raw):,} rows")

    # CRITICAL: filter to latest TOD cycle only (string "Y", NOT boolean True)
    if 'is_latest_tod_cycle' in raw.columns:
        raw_latest = raw[raw['is_latest_tod_cycle'] == 'Y'].copy()
        log.info(f"  After is_latest_tod_cycle=='Y' filter: {len(raw_latest):,} rows")
    else:
        log.warning("  'is_latest_tod_cycle' column not found - using all rows (may double-count)")
        raw_latest = raw.copy()

    # Identify amount column (EU share of spending = actual disbursed)
    amt_col = 'EU share of spending'
    amt_planned = 'EU planned'
    if amt_col not in raw_latest.columns:
        amt_col = amt_planned

    # Strip comma thousands-separators then coerce to numeric
    for _col in [amt_col, amt_planned, 'EU share of decided']:
        if _col in raw_latest.columns:
            raw_latest[_col] = (
                raw_latest[_col].astype(str)
                .str.replace(',', '', regex=False)
                .str.strip()
                .pipe(pd.to_numeric, errors='coerce')
            )
    non_null_spent = raw_latest[amt_col].notna().sum() if amt_col in raw_latest.columns else 0
    log.info(f"  ESIF 2021-2027: {non_null_spent:,} rows with non-null spent amount")

    # Aggregate by programme × country × fund × year
    group_cols = ['cci', 'programme_short_title', 'ms', 'fund', 'tod_year']
    group_cols = [c for c in group_cols if c in raw_latest.columns]

    agg_dict = {}
    if amt_col in raw_latest.columns:
        agg_dict['amount_spent'] = (amt_col, 'sum')
    if amt_planned in raw_latest.columns and amt_planned != amt_col:
        agg_dict['amount_planned'] = (amt_planned, 'sum')

    if agg_dict:
        agg = raw_latest.groupby(group_cols, dropna=False).agg(**agg_dict).reset_index()
    else:
        agg = raw_latest[group_cols].drop_duplicates().copy()
        agg['amount_spent'] = 0.0

    # Build standardized schema
    std = pd.DataFrame()
    std['source'] = 'ESIF_2027'
    std['beneficiary_name'] = agg.get('programme_short_title', pd.Series(['Unknown'] * len(agg)))
    std['country'] = agg.get('ms', pd.Series([None] * len(agg)))
    std['amount_eur'] = agg.get('amount_spent', pd.Series([0.0] * len(agg))).fillna(0)
    std['amount_eur'] = pd.to_numeric(std['amount_eur'], errors='coerce').fillna(0)
    std['amount_type'] = 'eu_grant_spent (programme level)'
    std['year'] = pd.to_numeric(agg.get('tod_year', pd.Series([None] * len(agg))), errors='coerce')
    std['sector_description'] = agg.get('fund', pd.Series([None] * len(agg)))
    std['description'] = agg.get('cci', pd.Series([None] * len(agg)))
    std['source_record_id'] = agg.get('cci', pd.Series([None] * len(agg)))
    std['granularity'] = 'programme'
    std['nace_2digit'] = None
    # CONTEXTUAL ONLY: overlaps with Kohesio — excluded from headline totals by default
    std['overlap_flags'] = 'contextual_only:overlaps_with_kohesio'
    std['original_columns'] = None

    # Programme/fund structure layer (contextual-only source — null columns)
    std['programme']          = None
    std['fund']               = None
    std['programming_period'] = None
    std['instrument_subtype'] = None
    std['policy_domain']      = None

    # Audit validation layer
    std['year_paid']                  = None
    std['flow_stage']                 = 'allocated'
    std['financial_instrument_class'] = None
    std['management_type']            = None
    std['legal_basis']                = None
    std['budget_line_code']           = None
    std['budget_execution_type']      = None

    # --- Schema v2 columns ---
    std['flow_stage_confidence'] = 'inferred'
    std['flow_stage_assumption'] = 'ESIF planned/implemented amounts treated as allocated'
    std['exclude_reason'] = 'esif_kohesio_overlap'
    std['is_primary_record'] = False  # ESIF overlaps Kohesio — never primary
    apply_v2_columns(std, fiscal_source_type='eu_budget_shared', resolution_level='aggregate')

    log.info(f"  ESIF 2021-2027 standardized: {len(std):,} rows, "
             f"EUR {std['amount_eur'].sum():,.0f} (programme-level totals)")

    raw_with_cci = raw_latest[group_cols + [c for c in [amt_col, amt_planned]
                                            if c in raw_latest.columns]].copy()
    return std, raw_with_cci
