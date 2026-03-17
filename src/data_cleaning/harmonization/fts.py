"""
harmonization/fts.py
====================
Standardize FTS (Financial Transparency System) — EU Commission direct spending.

Source type:    EU budget direct spending contracts and grants
Granularity:    Contract/commitment-level (one row per legal commitment)
Amount:         "Beneficiary's contracted amount (EUR)" — apostrophe may be curly or straight
Overlap:        ~38% of FTS rows are in Horizon/H2020/FP7 programmes → overlap with RESEARCH.
                These rows are flagged 'research_programme_overlap' by flag_overlaps().
                When using FTS + RESEARCH together, exclude flagged rows to avoid double-counting.
                ~1% of FTS rows are in structural fund programmes → potential Kohesio overlap.
Inclusion:      Included in headline totals; exclude research_programme_overlap rows
                when RESEARCH source is active (controlled by master_builder.py).

Moved verbatim from analysis.py load_fts() + standardize_fts() (lines 248-286, 659-700).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    classify_fiscal_source,
    classify_instrument,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)

# FTS covers 2007-2024 (18 yearly Excel files)
FTS_YEAR_RANGE = range(2007, 2025)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize all FTS yearly Excel files.

    Merges 18 annual files, resolves the amount column name (Unicode apostrophe
    variants), and maps to COMMON_COLUMNS schema.

    Parameters
    ----------
    data_dir : Path
        Directory containing {year}_FTS_dataset_en.xlsx files.
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
    """Load and merge 18 yearly FTS Excel files."""
    log.info("Loading FTS (18 yearly files) ...")
    frames = []
    for yr in FTS_YEAR_RANGE:
        fpath = data_dir / f'{yr}_FTS_dataset_en.xlsx'
        if not fpath.exists():
            log.warning(f"  FTS file missing: {fpath.name}")
            continue
        df_yr = pd.read_excel(fpath)
        df_yr['fts_year'] = yr
        frames.append(df_yr)
        log.info(f"  {fpath.name}: {len(df_yr):,} rows")

    df = pd.concat(frames, ignore_index=True)
    log.info(f"  FTS merged: {len(df):,} rows, {len(df.columns)} columns")

    # Find the amount column (apostrophe may be curly \u2019 or straight ')
    amt_col = None
    for c in df.columns:
        if 'contracted amount' in c.lower() and 'beneficiary' in c.lower() and 'estimated' not in c.lower():
            amt_col = c
            break
    if amt_col:
        df[amt_col] = safe_to_numeric(df[amt_col], log, amt_col)
        log.info(f"  FTS amount column: {repr(amt_col)}")
    else:
        log.warning("  FTS: could not find beneficiary contracted amount column!")

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map FTS to common schema."""
    log.info("Standardising FTS ...")
    # Find amount column dynamically (curly vs straight apostrophe)
    amt_col = None
    for c in df.columns:
        if 'contracted amount' in c.lower() and 'beneficiary' in c.lower() and 'estimated' not in c.lower():
            amt_col = c
            break

    out = pd.DataFrame()
    out['source'] = 'FTS'
    # Use Reference of Legal Commitment if available, else row index
    lc_col = None
    for c in df.columns:
        if 'reference' in c.lower() and 'legal commitment' in c.lower():
            lc_col = c
            break
    if lc_col:
        out['source_record_id'] = df[lc_col].astype(str)
    else:
        out['source_record_id'] = df.index.astype(str)
    out['granularity'] = 'contract'
    out['beneficiary_name'] = df.get('Name of beneficiary', pd.Series(dtype=str))
    out['country'] = df['Beneficiary country'].apply(standardize_country) if 'Beneficiary country' in df.columns else ''
    out['amount_eur'] = df[amt_col] if amt_col else np.nan
    out['amount_type'] = 'mixed'  # FTS has grants, contracts, etc.
    out['year'] = df.get('fts_year', df.get('Year', pd.Series(dtype='Int64')))
    out['sector_description'] = df.get('Programme name', pd.Series(dtype=str))
    out['nace_2digit'] = None
    out['description'] = df.get('Subject of grant or contract', pd.Series(dtype=str))
    out['overlap_flags'] = ''
    # Pack original columns
    orig_cols = ['Budget', 'Funding type', 'Responsible department',
                 'Budget line name', 'Budget line number', 'Management type',
                 'Type of contract*', 'Beneficiary type', 'Expense type']
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer
    out['programme']          = df.get('Programme name', pd.Series(dtype=str))
    out['fund']               = df.get('Budget line name', pd.Series(dtype=str))
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('Funding type', pd.Series(dtype=str))
    out['policy_domain']      = None

    # Audit validation layer
    out['year_paid']                  = None   # amount = contracted, not paid
    out['flow_stage']                 = 'contracted'  # col = "Beneficiary's contracted amount (EUR)"
    out['financial_instrument_class'] = df.get('Funding type', pd.Series(dtype=str)).apply(classify_instrument)
    out['management_type']            = df.get('Management type', pd.Series(dtype=str))
    out['legal_basis']                = None   # Reference (Budget) is budget ref, not legal basis
    out['budget_line_code']           = df.get('Budget line number', pd.Series(dtype=str))
    out['budget_execution_type']      = df.get('Expense type', pd.Series(dtype=str))

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    # FTS fiscal_source_type depends on management_type (per-record)
    out['fiscal_source_type'] = [
        classify_fiscal_source('FTS', mt) for mt in out['management_type']
    ]
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, resolution_level='beneficiary')

    out.index = df.index
    log.info(f"  FTS standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
