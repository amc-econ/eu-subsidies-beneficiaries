"""
harmonization/tam_si.py
=======================
Standardize Slovenian state aid data (supplement to TAM).

Source:  Slovenian Ministry of Finance state aid register
Files:   data_raw/state_aid_slovenia_500k_june2023.xlsx  (awards > EUR 500K, 2016-2023)
         data_raw/state_aid_slovenia_covid_100k.xlsx     (COVID/Ukraine TF awards > EUR 100K, 2020-2024)
Amounts: Already in EUR.
Country: SI (hardcoded)
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    classify_instrument,
    extract_year,
    pack_originals,
    safe_to_numeric,
)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load and standardize Slovenian state aid data from 2 Excel files."""
    frames = []

    # File 1: Awards > EUR 500K (2016-2023)
    f1 = data_dir / 'state_aid_slovenia_500k_june2023.xlsx'
    if f1.exists():
        df1 = _load_file(f1, header_row=6, log=log, label='SI_500K')
        frames.append(df1)

    # File 2: COVID/Ukraine Temporary Framework awards > EUR 100K (2020-2024)
    f2 = data_dir / 'state_aid_slovenia_covid_100k.xlsx'
    if f2.exists():
        df2 = _load_file(f2, header_row=2, log=log, label='SI_COVID')
        frames.append(df2)

    if not frames:
        log.warning("  No Slovenian state aid files found")
        return pd.DataFrame(columns=COMMON_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    log.info(f"  TAM_SI combined: {len(combined):,} rows before dedup")

    # Deduplicate on (beneficiary, date, amount) — 500K file is subset of larger awards
    before = len(combined)
    combined = combined.drop_duplicates(
        subset=['_beneficiary', '_date', '_amount'], keep='first')
    log.info(f"  Deduplication: {before} → {len(combined)} rows ({before - len(combined)} removed)")

    return _standardize(combined, log)


def _load_file(path: Path, header_row: int, log: logging.Logger,
               label: str) -> pd.DataFrame:
    """Load a single Slovenian Excel file."""
    log.info(f"  Loading {label}: {path.name}")
    df = pd.read_excel(path, header=header_row)
    # Drop fully-empty rows (header padding)
    df = df.dropna(how='all').reset_index(drop=True)
    log.info(f"    {len(df):,} rows, EUR {df['Znesek'].sum():,.0f}")

    # Normalise key columns for dedup
    df['_beneficiary'] = df['Popolno ime'].astype(str).str.strip().str.lower()
    df['_date'] = pd.to_datetime(df['Datum odobritve DT'], errors='coerce')
    df['_amount'] = safe_to_numeric(df['Znesek'], log, 'Znesek')
    df['_year'] = extract_year(df['Datum odobritve DT'])
    df['_label'] = label

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map Slovenian state aid to common schema."""
    log.info("Standardising TAM_SI ...")

    out = pd.DataFrame(index=df.index)
    out['source'] = 'TAM'
    # SA number from MSEC column (may be semicolon-separated, take as-is)
    sa_col = 'Matična številka priglasitve MSEC' if 'Matična številka priglasitve MSEC' in df.columns else None
    if sa_col is None:
        # Try ASCII-ish variant
        for c in df.columns:
            if 'priglasitve MSEC' in c:
                sa_col = c
                break
    if sa_col:
        out['source_record_id'] = df[sa_col].fillna('').astype(str)
    else:
        out['source_record_id'] = df.index.astype(str)

    out['granularity'] = 'award'
    out['beneficiary_name'] = df['Popolno ime']
    out['country'] = 'SI'
    out['amount_eur'] = df['_amount']
    out['amount_type'] = 'grant'
    out['year'] = df['_year']

    # NACE: SKD 2-digit ID (5-digit code like 29100 → extract first 2 digits)
    nace_col = None
    for c in df.columns:
        if 'SKD 2. nivo ID' in c:
            nace_col = c
            break
    if nace_col:
        nace_raw = pd.to_numeric(df[nace_col], errors='coerce')
        # Handle both formats: 29100 (divide by 1000) or 29.1 (take floor)
        out['nace_2digit'] = np.where(
            nace_raw > 1000, nace_raw // 1000,
            np.where(nace_raw > 100, nace_raw // 100, nace_raw)
        )
        out['nace_2digit'] = out['nace_2digit'].where(nace_raw.notna())
    else:
        out['nace_2digit'] = None

    # Sector description from text NACE column
    nace_text_col = None
    for c in df.columns:
        if 'SKD 2. nivo' in c and 'ID' not in c:
            nace_text_col = c
            break
    out['sector_description'] = df.get(nace_text_col, pd.Series(dtype=str)) if nace_text_col else None

    # Description from 'Namen' (purpose)
    out['description'] = df.get('Namen', pd.Series(dtype=str))
    out['overlap_flags'] = ''

    # Pack original columns
    orig_keys = ['Instrument', 'Kategorija', 'Namen', 'Dajalec', 'Popolno ime',
                 'Bruto znesek', 'Znesek']
    # Also include scheme name (differs between files)
    for extra in ['Naziv', 'Naziv sheme']:
        if extra in df.columns:
            orig_keys.append(extra)
    avail = [c for c in orig_keys if c in df.columns]
    packed = df[avail].copy()
    packed['_tam_supplement_source'] = 'tam_si'
    out['original_columns'] = packed.apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure
    # Scheme name: 'Naziv' in 500K file, 'Naziv sheme' in COVID file
    if 'Naziv sheme' in df.columns and 'Naziv' in df.columns:
        out['programme'] = df['Naziv'].fillna(df['Naziv sheme'])
    elif 'Naziv' in df.columns:
        out['programme'] = df['Naziv']
    elif 'Naziv sheme' in df.columns:
        out['programme'] = df['Naziv sheme']
    else:
        out['programme'] = None

    out['fund'] = None
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('Instrument', pd.Series(dtype=str))

    # Kategorija = aid category = policy_domain
    out['policy_domain'] = df.get('Kategorija', pd.Series(dtype=str))

    # Audit validation
    out['year_paid'] = None
    out['flow_stage'] = 'granted'
    out['financial_instrument_class'] = df['Instrument'].apply(classify_instrument)
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

    out.index = range(len(out))
    log.info(f"  TAM_SI standardised: {len(out):,} rows, EUR {out['amount_eur'].sum():,.0f}")
    return out[COMMON_COLUMNS]
