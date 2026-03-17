"""
harmonization/tam_ro.py
=======================
Standardize Romanian state aid data (supplement to TAM).

Source:  Romanian Competition Council state aid register
File:    data_raw/State_aid_romania_100k.xlsx
Rows:    ~81K awards (recipients > EUR 100K)
Amounts: In RON (lei) — converted to EUR using annual average ECB rates.
Country: RO (hardcoded)
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .ecb_fx_rates import convert_ron_series
from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    classify_instrument,
    extract_year,
    pack_originals,
    safe_to_numeric,
)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load and standardize Romanian state aid data."""
    raw = _load(data_dir, log)
    return _standardize(raw, log)


def _load(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load Romanian state aid Excel."""
    path = data_dir / 'State_aid_romania_100k.xlsx'
    log.info("Loading TAM_RO (Romania state aid) ...")
    df = pd.read_excel(path)
    df.columns = df.columns.str.strip()  # trailing space on 'Date of aid granting'
    log.info(f"  TAM_RO raw: {len(df):,} rows, {len(df.columns)} columns")

    # Coerce amounts
    df['Aid granted per category (lei)'] = safe_to_numeric(
        df['Aid granted per category (lei)'], log, 'Aid granted per category (lei)')

    # Parse year
    df['_year'] = extract_year(df['Date of aid granting'])

    # Convert RON → EUR
    df['_amount_eur'] = convert_ron_series(
        df['Aid granted per category (lei)'], df['_year'])

    total_ron = df['Aid granted per category (lei)'].sum()
    total_eur = df['_amount_eur'].sum()
    log.info(f"  RON {total_ron:,.0f} → EUR {total_eur:,.0f} (annual avg ECB rates)")
    log.info(f"  Unique beneficiaries: {df['Beneficiary name'].nunique():,}")
    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map Romanian state aid to common schema."""
    log.info("Standardising TAM_RO ...")

    out = pd.DataFrame(index=df.index)
    out['source'] = 'TAM'
    out['source_record_id'] = df['Reference of the aid measure'].fillna(
        df['Id_act'].astype(str)).astype(str)
    out['granularity'] = 'award'
    out['beneficiary_name'] = df['Beneficiary name']
    out['country'] = 'RO'
    out['amount_eur'] = df['_amount_eur']
    out['amount_type'] = 'grant'
    out['year'] = df['_year']
    out['sector_description'] = df.get('Category of aid accessed', pd.Series(dtype=str))
    # NACE: 4-digit integer → extract first 2 digits
    nace_raw = pd.to_numeric(df.get('Main field of activity'), errors='coerce')
    out['nace_2digit'] = (nace_raw // 100).where(nace_raw.notna())
    out['description'] = df.get('Objective', pd.Series(dtype=str))
    out['overlap_flags'] = ''

    # Pack original columns — minimal set for traceability
    orig_cols = ['Id_act', 'Id_measure']
    avail = [c for c in orig_cols if c in df.columns]
    packed = df[avail].copy()
    packed['_src'] = 'tam_ro'
    out['original_columns'] = packed.apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure
    out['programme'] = df.get('Name of the accessed aid measure', pd.Series(dtype=str))
    out['fund'] = None
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('Aid granting instrument (lei)', pd.Series(dtype=str))
    out['policy_domain'] = df.get('Category of aid accessed', pd.Series(dtype=str))

    # Audit validation
    out['year_paid'] = None
    out['flow_stage'] = 'granted'
    out['financial_instrument_class'] = df['Aid granting instrument (lei)'].apply(
        classify_instrument)
    out['management_type'] = None
    out['legal_basis'] = None
    out['budget_line_code'] = None
    out['budget_execution_type'] = None

    # Schema v2
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='national_budget',
                     resolution_level='beneficiary')

    out.index = df.index
    log.info(f"  TAM_RO standardised: {len(out):,} rows, EUR {out['amount_eur'].sum():,.0f}")
    return out[COMMON_COLUMNS]
