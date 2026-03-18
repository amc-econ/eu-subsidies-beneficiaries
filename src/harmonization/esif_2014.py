"""
harmonization/esif_2014.py
==========================
Standardize ESIF 2014-2020 programme-level categorisation data.

Source type:    Programme-level aggregate (NOT project/beneficiary-level)
Granularity:    Programme × country × fund × year aggregate
Amount:         EU_spend_share_(Elig_Expenditure_Declared_notional) (actuals) or
                EU_amount_planned (fallback)
Coverage:       EU structural & investment funds 2014-2020 (~EUR 454B total envelope)

CRITICAL INCLUSION RULE — DO NOT MODIFY:
    ESIF 2014-2020 programme-level data OVERLAPS with Kohesio project-level data.
    Kohesio is the preferred source (project-level, with beneficiary names).
    ESIF 2014-2020 is CONTEXTUAL ONLY — excluded from master dataset by default
    (MasterConfig.include_esif_programme_level=False).
    All rows are tagged overlap_flags='contextual_only:overlaps_with_kohesio'.
    Use ESIF programme files only for programme-level cross-validation.

Dimension filter:
    Only 'InterventionField' dimension rows are loaded (one non-overlapping
    breakdown per programme × category × year).  This prevents double-counting
    across dimension types (Location, Territory, Economic Activity, etc.).

Moved verbatim from deep_validation_analysis_v2.py standardize_esif_2014() (line 215).
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
    Load and standardize ESIF 2014-2020 categorisation CSV.

    Aggregates at programme × country × fund × year level using
    'InterventionField' dimension rows.

    Parameters
    ----------
    data_dir : Path
        Directory containing ESIF_2014-2020_categorisation_*.csv.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame]
        (standardized_df, raw_cci_df)
        - standardized_df: common schema rows, tagged contextual_only
        - raw_cci_df: raw aggregated rows with CCI column (for overlap detection
          in check_esif_kohesio_overlap())
    """
    # Find file (date suffix varies)
    candidates = list(data_dir.glob('ESIF_2014-2020_categorisation_*.csv'))
    if not candidates:
        log.warning("ESIF 2014-2020 file not found. Skipping.")
        return None, None
    path = candidates[0]
    log.info(f"Loading ESIF 2014-2020: {path.name} ...")

    # Read only needed columns (file is ~186MB)
    usecols = [
        'Dimension Type',
        'Member State (2 digit ISO)',
        'Programme Title',
        'CCI',
        'Fund',
        'Year',
        'EU_spend_share_(Elig_Expenditure_Declared_notional)',
        'EU_amount_planned',
        'EU_Eligible_Costs_Decided_(selected_notional)',
    ]
    try:
        raw = pd.read_csv(path, usecols=usecols, low_memory=False)
    except ValueError as e:
        # Some columns might be absent - try without optional ones
        log.warning(f"  Column mismatch: {e}. Reading all columns.")
        raw = pd.read_csv(path, low_memory=False)

    log.info(f"  Raw ESIF 2014-2020: {len(raw):,} rows")

    # Filter to one non-overlapping dimension type for correct totals
    dim_col = 'Dimension Type'
    if dim_col in raw.columns:
        dim_vals = raw[dim_col].unique().tolist()
        log.info(f"  Dimension types found: {dim_vals[:10]}")
        # Actual values: InterventionField, Economic Activity, Location, Territory, etc.
        # 'InterventionField' is the exhaustive breakdown (one row per programme x category x year)
        # — equivalent to "Category of intervention" and safe for non-overlapping aggregation.
        # Try in priority order: InterventionField → any intervention-like string → all rows.
        cat_rows = raw[raw[dim_col] == 'InterventionField']
        if len(cat_rows) == 0:
            mask = raw[dim_col].str.lower().str.contains('intervention', na=False)
            cat_rows = raw[mask]
        if len(cat_rows) == 0:
            log.warning("  No intervention-type dimension found. Using all rows (may overcount).")
            cat_rows = raw
        log.info(f"  After dimension filter ({cat_rows[dim_col].iloc[0] if len(cat_rows) else 'all'}): "
                 f"{len(cat_rows):,} rows")
    else:
        cat_rows = raw

    # Identify amount column
    amt_col_spent = 'EU_spend_share_(Elig_Expenditure_Declared_notional)'
    amt_col_planned = 'EU_amount_planned'
    if amt_col_spent not in cat_rows.columns:
        amt_col_spent = amt_col_planned  # fallback

    cat_rows = cat_rows.copy()
    # Strip comma thousands-separators then coerce to numeric
    # (EC open data exports use '59,187,213' format)
    for _col in [amt_col_spent, amt_col_planned, 'EU_Eligible_Costs_Decided_(selected_notional)']:
        if _col in cat_rows.columns:
            cat_rows[_col] = (
                cat_rows[_col].astype(str)
                .str.replace(',', '', regex=False)
                .str.strip()
                .pipe(pd.to_numeric, errors='coerce')
            )
    non_null_spent = cat_rows[amt_col_spent].notna().sum() if amt_col_spent in cat_rows.columns else 0
    log.info(f"  ESIF 2014-2020: {non_null_spent:,} rows with non-null spent amount")

    # Aggregate by programme × country × fund × year
    group_cols = ['CCI', 'Programme Title', 'Member State (2 digit ISO)', 'Fund', 'Year']
    group_cols = [c for c in group_cols if c in cat_rows.columns]

    agg_dict = {}
    if amt_col_spent in cat_rows.columns:
        agg_dict['amount_spent'] = (amt_col_spent, 'sum')
    if amt_col_planned in cat_rows.columns:
        agg_dict['amount_planned'] = (amt_col_planned, 'sum')
    if not agg_dict:
        agg_dict['amount_spent'] = ('CCI', 'count')  # fallback sentinel

    agg = cat_rows.groupby(group_cols, dropna=False).agg(**agg_dict).reset_index()

    # Build standardized schema
    std = pd.DataFrame()
    std['source'] = 'ESIF_2014'
    std['beneficiary_name'] = agg.get('Programme Title', pd.Series(['Unknown'] * len(agg)))
    std['country'] = agg.get('Member State (2 digit ISO)', pd.Series([None] * len(agg)))
    std['amount_eur'] = agg.get('amount_spent', pd.Series([0.0] * len(agg))).fillna(0)
    std['amount_eur'] = pd.to_numeric(std['amount_eur'], errors='coerce').fillna(0)
    std['amount_type'] = 'eu_grant_spent (programme level)'
    std['year'] = pd.to_numeric(agg.get('Year', pd.Series([None] * len(agg))), errors='coerce')
    std['sector_description'] = agg.get('Fund', pd.Series([None] * len(agg)))
    std['description'] = agg.get('CCI', pd.Series([None] * len(agg)))
    std['source_record_id'] = agg.get('CCI', pd.Series([None] * len(agg)))
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

    log.info(f"  ESIF 2014-2020 standardized: {len(std):,} rows, "
             f"EUR {std['amount_eur'].sum():,.0f} (programme-level totals)")

    # Return both standardized DF and raw with CCI for overlap checks
    raw_with_cci = cat_rows[group_cols + [c for c in [amt_col_spent, amt_col_planned]
                                          if c in cat_rows.columns]].copy()
    return std, raw_with_cci
