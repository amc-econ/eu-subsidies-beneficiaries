"""
harmonization/rrf.py
====================
Standardize RRF (Recovery & Resilience Facility) sectoral data.

Source type:    Measure-level planned allocations (NOT beneficiary-level disbursements)
Granularity:    Measure-level (one row per national plan measure)
Amount:         'Costs' column (EUR, planned allocation)
Amount type:    Derived from 'Loans/Grants' column per row

CRITICAL ECONOMIC INTERPRETATION — DO NOT MODIFY:
    RRF data is measure-level PLANNED allocation from national recovery plans.
    It is NOT beneficiary-level data and CANNOT be directly compared with TAM awards.
    RRF must NOT be deduplicated against TAM:
      - TAM = actual individual state aid awards (implemented)
      - RRF = national plan allocations (planned/committed at measure level)
    Some RRF grant measures may eventually appear in TAM once implemented,
    but no precise deduplication is possible at this stage.
    RRF grant measures are flagged 'potential_tam_overlap' as a WARNING ONLY —
    they must still be counted in their own right as planned allocations.

Moved verbatim from analysis.py load_rrf() + standardize_rrf() (lines 358-376, 781-841).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize RRF sectoral data.

    Parameters
    ----------
    data_dir : Path
        Directory containing 'RRF Sectoral Data_23.01.2026.xlsx'.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    pd.DataFrame
        Standardized DataFrame with columns matching COMMON_COLUMNS.
        Grant measures are flagged 'potential_tam_overlap' (informational only).
    """
    raw = _load(data_dir, log)
    return _standardize(raw, log)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load RRF sectoral data."""
    rrf_file = data_dir / 'RRF Sectoral Data_23.01.2026.xlsx'
    log.info("Loading RRF ...")
    df = pd.read_excel(rrf_file, sheet_name='RRF Sectoral Data')
    log.info(f"  RRF raw: {len(df):,} rows, {len(df.columns)} columns")

    # Coerce Costs
    if 'Costs' in df.columns:
        df['Costs'] = safe_to_numeric(df['Costs'], log, 'Costs')

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map RRF to common schema."""
    log.info("Standardising RRF ...")

    # Determine amount_type from Loans/Grants column
    def _rrf_amount_type(val):
        if pd.isna(val) or val == 0:
            return 'budget_allocation'
        s = str(val).lower()
        if 'grant' in s and 'loan' in s:
            return 'mixed'
        if 'grant' in s:
            return 'grant'
        if 'loan' in s:
            return 'loan'
        return 'budget_allocation'

    # NACE 2-digit column
    nace_col = None
    for c in df.columns:
        if 'INVESTMENTS' in str(c).upper() and '2-digit' in str(c) and 'NACE code' in str(c):
            nace_col = c
            break

    # Sector description column
    nace_desc_col = None
    for c in df.columns:
        if 'INVESTMENTS' in str(c).upper() and '2-digit' in str(c) and 'description' in str(c).lower():
            nace_desc_col = c
            break

    out = pd.DataFrame()
    out['source'] = 'RRF'
    out['source_record_id'] = df.get('Measure Reference', df.index).astype(str)
    out['granularity'] = 'measure'
    # RRF is measure-level — no individual beneficiaries in the source. We
    # emit ``pd.NA`` rather than ``None`` so the column round-trips through
    # parquet as a real null rather than being coerced to an empty string.
    # Downstream code that filters via ``.isna()`` / ``.notna()`` then sees
    # RRF correctly as "no beneficiary" instead of "empty beneficiary".
    # Plan audit finding L8 / D9: H7's blind-spot silent-null bug.
    out['beneficiary_name'] = pd.NA
    out['country'] = df['Country'].apply(standardize_country) if 'Country' in df.columns else ''
    out['amount_eur'] = df.get('Costs', np.nan)
    out['amount_type'] = df['Loans/Grants'].apply(_rrf_amount_type) if 'Loans/Grants' in df.columns else 'budget_allocation'
    out['year'] = None  # RRF doesn't have per-row years (plan-period: 2020-2026)
    out['sector_description'] = df[nace_desc_col] if nace_desc_col else None
    out['nace_2digit'] = df[nace_col].astype(str) if nace_col else None
    out['description'] = df.get('Measure Name', pd.Series(dtype=str))
    out['overlap_flags'] = ''

    # Flag grant measures as potential TAM overlap (informational only — see module docstring)
    if 'Loans/Grants' in df.columns:
        grant_mask = df['Loans/Grants'].astype(str).str.lower().str.contains('grant', na=False)
        out.loc[grant_mask, 'overlap_flags'] = 'potential_tam_overlap'

    # Pack useful original columns
    orig_cols_candidates = ['Component Name', 'Measure Level', 'Measure Type',
                            'Loans/Grants', 'Climate Tag', 'Digital Tag', 'REPowerEU',
                            'Measure Description', 'Measure Reference']
    avail_orig = [c for c in orig_cols_candidates if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer
    out['programme']          = df.get('Component Name', pd.Series(dtype=str))
    out['fund']               = None
    out['programming_period'] = '2020-2026'   # structural constant — all RRF measures
    out['instrument_subtype'] = df.get('Measure Type', pd.Series(dtype=str))

    # policy_domain: derive from thematic tag columns (all 100% non-null in raw data)
    climate   = df.get('Climate Tag', pd.Series('0', index=df.index)).astype(str).str.strip().eq('1')
    digital   = df.get('Digital Tag',  pd.Series('0', index=df.index)).astype(str).str.strip().eq('1')
    repowereu = df.get('REPowerEU',    pd.Series('No', index=df.index)).astype(str).str.strip().str.upper().eq('YES')
    tags = [
        ' | '.join(t for t, flag in [('Climate', c), ('Digital', d), ('REPowerEU', r)] if flag)
        for c, d, r in zip(climate, digital, repowereu)
    ]
    out['policy_domain'] = pd.array([t if t else None for t in tags], dtype=object)

    # Audit validation layer
    out['year_paid']                  = None   # RRF is planned allocation — no payment year
    out['flow_stage']                 = 'planned'  # col = "Costs" = planned allocation in national recovery plan
    out['financial_instrument_class'] = df.get('Loans/Grants', pd.Series(dtype=str)).str.lower().map(
        {'grants': 'grant', 'loans': 'loan', 'mixed': 'mixed'})
    out['management_type']            = 'indirect'  # national implementation via recovery plans (Art. 154 FR)
    out['legal_basis']                = 'Regulation (EU) 2021/241'  # RRF establishing regulation
    out['budget_line_code']           = None   # no EU budget line codes in RRF data
    out['budget_execution_type']      = 'operational'  # all RRF = operational expenditure

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='eu_borrowing', resolution_level='measure')

    out.index = df.index
    log.info(f"  RRF standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
