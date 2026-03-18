"""
harmonization/eib.py
====================
Standardize EIB (European Investment Bank) project data.

Source type:    EIB project-level lending
Granularity:    Project-level (one row per signed project)
Amount:         Signed Amount (EUR)
Overlap:        EIB uses own-resources (not EU budget) — minimal overlap with FTS.
                ~134 EIB projects referencing EU guarantee/InvestEU/EFSI are flagged
                'potential_fts_overlap' by flag_overlaps().
                EIB and EBRD are separate institutions — no overlap between them.
Inclusion:      Optionally filtered to EU-27 countries for EU-only analysis
                (controlled by master_builder.py: eu27_only_loans flag).
                NOT merged into grant logic — loans are a separate instrument.

Moved verbatim from analysis.py load_eib() + standardize_eib() (lines 289-319, 703-727).
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
    Load and standardize EIB project data.

    Parameters
    ----------
    data_dir : Path
        Directory containing EIB.xlsx.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    pd.DataFrame
        Standardized DataFrame with columns matching COMMON_COLUMNS.
    """
    raw = _load(data_dir, log)
    return _standardize(raw, log)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load EIB project data, parse string amounts."""
    eib_file = data_dir / 'EIB.xlsx'
    log.info("Loading EIB ...")
    df = pd.read_excel(eib_file)
    log.info(f"  EIB raw: {len(df):,} rows, {len(df.columns)} columns")

    # Drop summary/total rows
    if 'Name' in df.columns:
        total_mask = df['Name'].astype(str).str.strip().str.lower().isin(['total', 'grand total'])
        if total_mask.any():
            log.info(f"  Dropping {total_mask.sum()} EIB summary/total rows")
            df = df[~total_mask].copy()

    # Parse Signed Amount (strings like "25,000,000" or possibly with euro sign)
    if 'Signed Amount' in df.columns:
        df['Signed Amount'] = safe_to_numeric(df['Signed Amount'], log, 'Signed Amount')

    # Parse Signature Date
    if 'Signature Date' in df.columns:
        df['Signature Date'] = pd.to_datetime(df['Signature Date'], errors='coerce',
                                               dayfirst=True)

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map EIB to common schema."""
    log.info("Standardising EIB ...")

    out = pd.DataFrame()
    out['source'] = 'EIB'
    out['source_record_id'] = df.index.astype(str)
    out['granularity'] = 'project'
    out['beneficiary_name'] = df.get('Name', pd.Series(dtype=str))
    out['country'] = df['Country or Territory'].apply(standardize_country) if 'Country or Territory' in df.columns else ''
    out['amount_eur'] = df.get('Signed Amount', np.nan)
    out['amount_type'] = 'loan'
    out['year'] = df['Signature Date'].dt.year if 'Signature Date' in df.columns and pd.api.types.is_datetime64_any_dtype(df['Signature Date']) else None
    out['sector_description'] = df.get('Sector', pd.Series(dtype=str))
    out['nace_2digit'] = None
    out['description'] = df.get('Description', pd.Series(dtype=str))
    out['overlap_flags'] = ''
    orig_cols = ['Region']
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer (EIB is project-level lending — no programme structure)
    out['programme']          = None
    out['fund']               = None
    out['programming_period'] = None
    out['instrument_subtype'] = None   # amount_type='loan' already captures this
    out['policy_domain']      = df.get('Sector', pd.Series(dtype=str))

    # Audit validation layer
    out['year_paid']                  = None   # EIB signing = commitment; no payment date
    out['flow_stage']                 = 'signed'  # col = "Signed Amount"
    out['financial_instrument_class'] = 'loan'   # EIB = lending institution (Art. 309 TFEU)
    out['management_type']            = 'direct'  # EIB directly manages its lending
    out['legal_basis']                = None
    out['budget_line_code']           = None   # EIB uses own resources, not EU budget
    out['budget_execution_type']      = None   # not EU budget executor

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='ifi_balance_sheet', resolution_level='project')

    out.index = df.index
    log.info(f"  EIB standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
