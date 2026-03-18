"""
harmonization/scoreboard.py
============================
Standardize State Aid Scoreboard — aggregated state aid expenditure.

Source type:    Aggregated state aid (case × year level totals)
Granularity:    Aggregate (one row per case ID × year of expenditure)
Amount:         expenditure_in_million_EUR × 1,000,000 → EUR

CRITICAL INCLUSION RULE — DO NOT MODIFY:
    The Scoreboard covers the same underlying awards as TAM but in aggregated form.
    Approximately 1.43 million TAM rows share case IDs with 15,400 Scoreboard rows.
    Scoreboard is EXCLUDED from headline totals by default (MasterConfig.include_scoreboard=False).
    It is retained for cross-validation against TAM aggregate totals only.
    Do NOT add Scoreboard on top of TAM — this would massively double-count grants.

Moved verbatim from analysis.py load_scoreboard() + standardize_scoreboard() (lines 379-399, 844-886).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    classify_instrument,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize State Aid Scoreboard data.

    Parameters
    ----------
    data_dir : Path
        Directory containing StateAidScoreboard.xlsx.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    pd.DataFrame
        Standardized DataFrame with columns matching COMMON_COLUMNS.
        NOTE: This source is excluded from headline totals by default.
    """
    raw = _load(data_dir, log)
    return _standardize(raw, log)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _load(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load State Aid Scoreboard (aggregated case/year data)."""
    scoreboard_file = data_dir / 'StateAidScoreboard.xlsx'
    log.info("Loading Scoreboard ...")
    df = pd.read_excel(scoreboard_file)
    log.info(f"  Scoreboard raw: {len(df):,} rows, {len(df.columns)} columns")

    # Amount is in MILLIONS of EUR
    if 'expenditure_in_million_EUR' in df.columns:
        df['expenditure_in_million_EUR'] = safe_to_numeric(
            df['expenditure_in_million_EUR'], log, 'expenditure_in_million_EUR')
        df['expenditure_eur'] = df['expenditure_in_million_EUR'] * 1_000_000

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map Scoreboard to common schema."""
    log.info("Standardising Scoreboard ...")

    # Map aid_instrument to amount_type
    def _sb_amount_type(val):
        if pd.isna(val):
            return 'grant'
        s = str(val).lower()
        if 'grant' in s or 'direct' in s:
            return 'grant'
        if 'guarantee' in s:
            return 'guarantee'
        if 'loan' in s or 'soft' in s:
            return 'loan'
        if 'equity' in s:
            return 'equity'
        if 'tax' in s:
            return 'grant'  # tax advantages are effectively grants
        return 'grant'

    out = pd.DataFrame()
    out['source'] = 'SCOREBOARD'
    out['source_record_id'] = df.get('state_aid_case_number', df.index).astype(str)
    out['granularity'] = 'aggregate'
    out['beneficiary_name'] = None  # Aggregated, no beneficiary
    out['country'] = df['member_state'].apply(standardize_country) if 'member_state' in df.columns else ''
    out['amount_eur'] = df.get('expenditure_eur', np.nan)
    out['amount_type'] = df['aid_instrument'].apply(_sb_amount_type) if 'aid_instrument' in df.columns else 'grant'
    out['year'] = df.get('year_of_expenditure', pd.Series(dtype='Int64'))
    out['sector_description'] = df.get('scoreboard_objective', pd.Series(dtype=str))
    out['nace_2digit'] = None
    out['description'] = df.get('type_of_aid', pd.Series(dtype=str))
    out['overlap_flags'] = ''

    orig_cols = ['case_type', 'aid_instrument', 'scoreboard_objective', 'type_of_aid']
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer
    out['programme']          = None
    out['fund']               = None
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('aid_instrument', pd.Series(dtype=str))
    out['policy_domain']      = df.get('scoreboard_objective', pd.Series(dtype=str))

    # Audit validation layer
    out['year_paid']                  = df.get('year_of_expenditure', pd.Series(dtype=str))
    out['flow_stage']                 = 'expenditure'  # col = "expenditure_in_million_EUR" = actual spending
    out['financial_instrument_class'] = df['aid_instrument'].apply(classify_instrument) if 'aid_instrument' in df.columns else None
    out['management_type']            = None   # case_type (GBER/Notified) != management_type
    out['legal_basis']                = None
    out['budget_line_code']           = None   # state aid = not EU budget
    out['budget_execution_type']      = None   # state aid = not EU budget execution

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = 'scoreboard_tam_overlap'
    out['is_primary_record'] = False  # Scoreboard overlaps TAM — never primary
    apply_v2_columns(out, fiscal_source_type='national_budget', resolution_level='aggregate')

    out.index = df.index
    log.info(f"  Scoreboard standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
