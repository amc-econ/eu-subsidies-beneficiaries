"""
harmonization/tam.py
====================
Standardize TAM (Transparency Aid Module) — individual state aid awards.

Source type:    Individual award notifications
Granularity:    Award-level (one row per state aid award)
Amount:         GRANTED_AMOUNT_FROM_EUR (filled from NOMINAL_AMOUNT_EUR_FROM where null)
Overlap:        Scoreboard contains aggregated TAM data — flagged in flag_overlaps().
                TAM is the preferred granular source; Scoreboard is for cross-validation only.
Inclusion:      Always included in headline totals (grant instrument).
NACE:           SECTOR_SD contains text descriptions — no numeric NACE codes.
                Split compound semicolon-delimited sector values before classifying.

Moved verbatim from analysis.py load_tam() + standardize_tam() (lines 206-245, 626-656).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    TAM_MEGA_SCHEMES,
    classify_instrument,
    apply_v2_columns,
    extract_year,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize TAM state aid data.

    Flags mega-scheme rows (aggregation artefacts) via is_primary_record=False,
    fills missing GRANTED amounts from NOMINAL, and maps to COMMON_COLUMNS schema.

    Parameters
    ----------
    data_dir : Path
        Directory containing TAM.dsv.
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
    """Load TAM state aid data, flag mega-schemes, return raw DataFrame."""
    tam_file = data_dir / 'TAM.dsv'
    log.info("Loading TAM ...")
    df = pd.read_csv(tam_file, sep='\t', encoding='latin-1', low_memory=False,
                     on_bad_lines='warn')
    log.info(f"  TAM raw: {len(df):,} rows, {len(df.columns)} columns")

    # Coerce amounts
    for col in ['GRANTED_AMOUNT_FROM_EUR', 'NOMINAL_AMOUNT_EUR_FROM']:
        if col in df.columns:
            df[col] = safe_to_numeric(df[col], log, col)

    # Fill missing granted with nominal
    if 'NOMINAL_AMOUNT_EUR_FROM' in df.columns:
        mask = df['GRANTED_AMOUNT_FROM_EUR'].isna() & df['NOMINAL_AMOUNT_EUR_FROM'].notna()
        df.loc[mask, 'GRANTED_AMOUNT_FROM_EUR'] = df.loc[mask, 'NOMINAL_AMOUNT_EUR_FROM']
        log.info(f"  Filled {mask.sum():,} missing GRANTED from NOMINAL")

    # Flag mega-schemes (kept in data, excluded downstream via is_primary_record)
    df['_is_mega_scheme'] = df['AID_MEASURE_ID'].isin(TAM_MEGA_SCHEMES)
    n_mega = df['_is_mega_scheme'].sum()
    mega_eur = df.loc[df['_is_mega_scheme'], 'GRANTED_AMOUNT_FROM_EUR'].sum()
    log.info(f"  Mega-scheme rows: {n_mega:,} (EUR {mega_eur:,.0f}) — flagged, NOT dropped")
    log.info(f"  TAM total: {len(df):,} rows")

    # Parse year for use in standardizer
    if 'DATE_GRANTED' in df.columns:
        df['_year'] = extract_year(df['DATE_GRANTED'])

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map TAM to common schema."""
    log.info("Standardising TAM ...")
    amt_col = 'GRANTED_AMOUNT_FROM_EUR'
    year_col = '_year'

    out = pd.DataFrame()
    out['source'] = 'TAM'
    out['source_record_id'] = df['AID_MEASURE_ID'].astype(str)
    out['granularity'] = 'award'
    out['beneficiary_name'] = df['BENEFICIARY_NAME'].fillna(
        df.get('BENEFICIARY_NAME_ENGLISH', pd.Series(dtype=str)))
    out['country'] = df['BENEFICIARY_MS'].apply(standardize_country)
    out['amount_eur'] = df[amt_col]
    out['amount_type'] = 'grant'
    out['year'] = df[year_col] if year_col in df.columns else extract_year(df['DATE_GRANTED'])
    out['sector_description'] = df.get('SECTOR_SD', pd.Series(dtype=str))
    out['nace_2digit'] = None  # TAM has text descriptions, not numeric NACE
    out['description'] = df.get('OBJECTIVE', pd.Series(dtype=str))
    out['overlap_flags'] = ''
    # Pack a selection of original columns
    orig_cols = ['AID_INSTRUMENT', 'AM_TITLE', 'AM_TITLE_EN', 'GRANTING_AUTHORITY_NAME',
                 'REGION_SD', 'COUNTRY_SD', 'OBJECTIVE']
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer
    out['programme']          = df.get('AM_TITLE_EN', df.get('AM_TITLE', pd.Series(dtype=str)))
    out['fund']               = None
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('AID_INSTRUMENT', pd.Series(dtype=str))
    out['policy_domain']      = df.get('OBJECTIVE', pd.Series(dtype=str))

    # Audit validation layer
    out['year_paid']                  = None   # TAM has no payment date; year = DATE_GRANTED
    out['flow_stage']                 = 'granted'  # col = GRANTED_AMOUNT_FROM_EUR
    out['financial_instrument_class'] = df['AID_INSTRUMENT'].apply(classify_instrument)
    out['management_type']            = None   # ENTRUSTED_ENTITY 0.3% non-null — unusable
    out['legal_basis']                = None   # no legal basis field in TAM
    out['budget_line_code']           = None   # state aid — not EU budget
    out['budget_execution_type']      = None   # state aid — not EU budget execution

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    # Flag mega-schemes
    if '_is_mega_scheme' in df.columns:
        mega = df['_is_mega_scheme'].values
        out.loc[mega, 'exclude_reason'] = 'mega_scheme_artefact'
        out.loc[mega, 'is_primary_record'] = False
    apply_v2_columns(out, fiscal_source_type='national_budget', resolution_level='beneficiary')

    out.index = df.index
    log.info(f"  TAM standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
