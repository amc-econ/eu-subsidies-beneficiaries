"""
harmonization/ebrd.py
=====================
Standardize EBRD (European Bank for Reconstruction and Development) investments.

Source type:    EBRD project-level investment financing
Granularity:    Project-level (one row per signed operation, 1991-2024)
Amount:         EBRD Finance (total); sub-columns: Debt, Equity, Guarantee
Amount type:    Derived per-row from sub-column values (loan/equity/guarantee/mixed)
Overlap:        6,150 of 8,818 rows are in non-EU countries — flagged 'non_eu'.
                EBRD is a separate institution from EIB — no overlap.
Inclusion:      Optionally filtered to EU-27 (master_builder.py: eu27_only_loans flag).
                NOT merged into grant logic — loans/equity are separate instruments.

Moved verbatim from analysis.py load_ebrd() + standardize_ebrd() (lines 322-355, 730-778).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    EU27,
    apply_v2_columns,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize EBRD investment data.

    Parameters
    ----------
    data_dir : Path
        Directory containing ebrd-investments-1991-2024.xlsx.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    pd.DataFrame
        Standardized DataFrame with columns matching COMMON_COLUMNS.
        Non-EU rows are flagged 'non_eu' in overlap_flags.
    """
    raw = _load(data_dir, log)
    return _standardize(raw, log)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load EBRD investments (1991-2024), sheet=List, header=6."""
    ebrd_file = data_dir / 'ebrd-investments-1991-2024.xlsx'
    log.info("Loading EBRD ...")
    df = pd.read_excel(ebrd_file, sheet_name='List', header=6)
    # Drop fully empty rows (sometimes trailing rows from Excel)
    df = df.dropna(how='all')
    # Drop summary/total rows
    if 'Country' in df.columns:
        total_mask = df['Country'].astype(str).str.lower().str.contains('overall|total', na=False)
        if total_mask.any():
            log.info(f"  Dropping {total_mask.sum()} EBRD summary/total rows")
            df = df[~total_mask].copy()
    log.info(f"  EBRD raw: {len(df):,} rows, {len(df.columns)} columns")

    # Coerce finance columns
    for col in ['EBRD Finance', 'EBRD Finance - Debt',
                'EBRD Finance - Equity', 'EBRD Finance - Guarantee']:
        if col in df.columns:
            df[col] = safe_to_numeric(df[col], log, col)

    # Parse date
    if 'Original Signing Date' in df.columns:
        df['Original Signing Date'] = pd.to_datetime(
            df['Original Signing Date'], errors='coerce', dayfirst=True)

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map EBRD to common schema. Determine amount_type from sub-columns."""
    log.info("Standardising EBRD ...")

    # Determine amount_type per row
    def _ebrd_amount_type(row):
        debt = row.get('EBRD Finance - Debt', 0) or 0
        equity = row.get('EBRD Finance - Equity', 0) or 0
        guarantee = row.get('EBRD Finance - Guarantee', 0) or 0
        parts = []
        if debt > 0:
            parts.append('loan')
        if equity > 0:
            parts.append('equity')
        if guarantee > 0:
            parts.append('guarantee')
        if len(parts) == 0:
            return 'mixed'
        if len(parts) == 1:
            return parts[0]
        return 'mixed'

    out = pd.DataFrame()
    out['source'] = 'EBRD'
    out['source_record_id'] = df.index.astype(str)
    out['granularity'] = 'project'
    out['beneficiary_name'] = df.get('Operation Name', pd.Series(dtype=str))
    out['country'] = df['Country'].apply(standardize_country) if 'Country' in df.columns else ''
    out['amount_eur'] = df.get('EBRD Finance', np.nan)
    out['amount_type'] = df.apply(_ebrd_amount_type, axis=1)
    if 'Original Signing Date' in df.columns and pd.api.types.is_datetime64_any_dtype(df['Original Signing Date']):
        out['year'] = df['Original Signing Date'].dt.year
    else:
        out['year'] = None
    out['sector_description'] = df.get('Sector', pd.Series(dtype=str))
    out['nace_2digit'] = None
    out['description'] = None
    out['overlap_flags'] = ''
    orig_cols = ['Direct/Regional', 'Portfolio Class',
                 'EBRD Finance - Debt', 'EBRD Finance - Equity',
                 'EBRD Finance - Guarantee']
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer (EBRD is project-level investment — no programme structure)
    out['programme']          = None
    out['fund']               = None
    out['programming_period'] = None
    out['instrument_subtype'] = None   # amount_type captures debt/equity/guarantee
    out['policy_domain']      = df.get('Sector', pd.Series(dtype=str))

    # Audit validation layer
    out['year_paid']                  = None   # EBRD signing = commitment; no payment date
    out['flow_stage']                 = 'signed'  # col = "Original Signing Date" + "EBRD Finance"
    out['financial_instrument_class'] = out['amount_type']  # already derived: loan/equity/guarantee/mixed
    out['management_type']            = df.get('Direct/Regional', pd.Series(dtype=str)).str.lower()
    out['legal_basis']                = None
    out['budget_line_code']           = None   # EBRD = own resources
    out['budget_execution_type']      = None   # not EU budget executor

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='ifi_balance_sheet', resolution_level='project')

    out.index = df.index

    # Flag non-EU rows
    non_eu_mask = ~out['country'].isin(EU27)
    out.loc[non_eu_mask, 'overlap_flags'] = 'non_eu'
    log.info(f"  EBRD standardised: {len(out):,} rows ({non_eu_mask.sum():,} non-EU)")
    return out[COMMON_COLUMNS]
