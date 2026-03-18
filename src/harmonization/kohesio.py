"""
harmonization/kohesio.py
=========================
Standardize Kohesio cohesion policy data (ESIF project-level).

Source type:    EU cohesion policy project-level disbursements (ESIF)
Granularity:    Project-level (one row per operation, with beneficiary name)
Amount:         Project_EU_Budget (preferred) || Total_Eligible_Expenditure_amount (fallback)
Amount type:    'grant' (cohesion policy is co-financed grants)
Coverage:       78 CSV files in Kohesio/ directory (two programming periods)
                2014-2020: files matching *-pp14-20-latest.csv
                2021-2027: files matching *-pp21-27-latest.csv
Beneficiary:    Joined from beneficiary lookup files (latest_*-latest.csv, excluding pp-files)
                ~67% match rate reported in logs.

Overlap:
    ESIF 2014-2020 and ESIF 2021-2027 (programme-level aggregates) overlap with Kohesio.
    Kohesio is the preferred project-level source; ESIF programme files are contextual only.
    ESIF programme files are flagged 'contextual_only:overlaps_with_kohesio' when loaded.

Inclusion:      Always included in headline grant totals (distinct from direct EU spending/TAM).

Moved verbatim from analysis.py load_kohesio() + standardize_kohesio() (lines 443-542, 929-967).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    apply_v2_columns,
    extract_year,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)

# Columns to load from project files (union of pp14-20 and pp21-27 schemas)
KOHESIO_USE_COLS = [
    'Operation_Unique_Identifier', 'Operation_Local_Identifier',
    'Operation_Name_English', 'Country',
    'Operation_Start_Date', 'Operation_End_Date',
    'Total_Eligible_Expenditure_amount', 'Total_Eligible_Expenditure_Currency',
    'Project_EU_Budget', 'Cofinancing_Rate',
    'Beneficiary_Unique_Identifier',
    'Category_Of_Intervention', 'Category_Label',
    'Thematic_Objective_Label', 'Policy_Objective_Label',
    'Fund_Code', 'Fund_Name', 'Programme_Name', 'Programme_Code',
    'NUTS2_Label',
    'Programming_Period',
    'Operation_Summary_English',
]


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize Kohesio cohesion policy data.

    Parameters
    ----------
    data_dir : Path
        Directory containing the Kohesio/ subdirectory.
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
    """Load Kohesio cohesion policy data (ESIF project-level)."""
    kohesio_dir = data_dir / 'Kohesio'
    log.info("Loading Kohesio (Cohesion Policy) ...")

    if not kohesio_dir.exists():
        log.warning(f"  Kohesio directory not found: {kohesio_dir}")
        return pd.DataFrame()

    # --- Load beneficiary lookup ---
    log.info("  Loading beneficiary files ...")
    ben_frames = []
    ben_files = list(kohesio_dir.glob('latest_*-latest.csv'))
    ben_files = [f for f in ben_files if 'pp' not in f.name]
    for fpath in sorted(ben_files):
        try:
            bf = pd.read_csv(fpath, usecols=['Beneficiary', 'BeneficiaryLabel'],
                             dtype=str, encoding='utf-8')
            ben_frames.append(bf)
        except Exception as e:
            log.warning(f"  Skipping beneficiary file {fpath.name}: {e}")
    if ben_frames:
        ben_df = pd.concat(ben_frames, ignore_index=True).drop_duplicates(subset='Beneficiary')
        log.info(f"  Beneficiary lookup: {len(ben_df):,} unique entries")
    else:
        ben_df = pd.DataFrame(columns=['Beneficiary', 'BeneficiaryLabel'])

    # --- Load project files ---
    project_frames = []
    for pattern in ['latest_*-pp14-20-latest.csv', 'latest_*-pp21-27-latest.csv']:
        files = sorted(kohesio_dir.glob(pattern))
        for fpath in files:
            try:
                # Read only columns that exist in this file
                header = pd.read_csv(fpath, nrows=0).columns.tolist()
                cols_to_use = [c for c in KOHESIO_USE_COLS if c in header]
                df_f = pd.read_csv(fpath, usecols=cols_to_use, low_memory=False)
                # Add missing columns as NaN
                for c in KOHESIO_USE_COLS:
                    if c not in df_f.columns:
                        df_f[c] = np.nan
                project_frames.append(df_f)
                log.info(f"  {fpath.name}: {len(df_f):,} rows")
            except Exception as e:
                log.warning(f"  Skipping {fpath.name}: {e}")

    if not project_frames:
        log.warning("  No Kohesio project files loaded")
        return pd.DataFrame()

    df = pd.concat(project_frames, ignore_index=True)
    log.info(f"  Kohesio merged: {len(df):,} rows")

    # --- Join beneficiary names ---
    df = df.merge(ben_df, left_on='Beneficiary_Unique_Identifier',
                  right_on='Beneficiary', how='left')
    log.info(f"  Beneficiary names matched: {df['BeneficiaryLabel'].notna().sum():,} / {len(df):,}")

    # --- Coerce amounts ---
    for col in ['Project_EU_Budget', 'Total_Eligible_Expenditure_amount']:
        if col in df.columns:
            df[col] = safe_to_numeric(df[col], log, col)

    # Use Project_EU_Budget where available, fall back to Total_Eligible
    df['_amount_eur'] = df['Project_EU_Budget'].fillna(df['Total_Eligible_Expenditure_amount'])

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map Kohesio (cohesion policy) to common schema."""
    log.info("Standardising Kohesio ...")

    if df.empty:
        log.warning("  Kohesio: empty DataFrame, returning empty standardized result")
        return pd.DataFrame(columns=COMMON_COLUMNS)

    out = pd.DataFrame()
    out['source'] = 'KOHESIO'
    out['source_record_id'] = (
        df['Operation_Unique_Identifier'].astype(str)
        if 'Operation_Unique_Identifier' in df.columns
        else df.index.astype(str)
    )
    out['granularity'] = 'project'
    out['beneficiary_name'] = df.get('BeneficiaryLabel', pd.Series(dtype=str))

    # --- Country standardisation ---
    if 'Country' in df.columns:
        out['country'] = df['Country'].apply(standardize_country)
    else:
        out['country'] = None

    # --- Explode multi-country rows (e.g. "IT|FR") ---
    multi_mask = out['country'].astype(str).str.contains(r'\|', na=False)
    if multi_mask.any():
        out.loc[multi_mask, 'country'] = (
            out.loc[multi_mask, 'country'].str.split('|')
        )
        out = out.explode('country')
        out['country'] = out['country'].str.strip()

    out['amount_eur'] = df['_amount_eur']
    out['amount_type'] = 'grant'

    if 'Operation_Start_Date' in df.columns:
        out['year'] = extract_year(df['Operation_Start_Date'])
    else:
        out['year'] = None

    out['sector_description'] = df.get('Category_Label', pd.Series(dtype=str))
    out['nace_2digit'] = None

    name_en = df.get('Operation_Name_English', pd.Series(dtype=str)).fillna('')
    summary = df.get('Operation_Summary_English', pd.Series(dtype=str)).fillna('')
    out['description'] = name_en.where(summary == '', name_en + ' | ' + summary)

    out['overlap_flags'] = ''

    orig_cols = [
        'Fund_Code', 'Fund_Name', 'Programme_Name', 'Programme_Code',
        'Thematic_Objective_Label', 'Policy_Objective_Label',
        'Specific_Objective_Label',
        'Programming_Period', 'NUTS2_Label',
        'Cofinancing_Rate', 'Total_Eligible_Expenditure_amount',
        'Project_EU_Budget'
    ]
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1
    )

    # Programme/fund structure layer
    out['programme']          = df.get('Programme_Name', pd.Series(dtype=str))
    out['fund']               = df.get('Fund_Name', pd.Series(dtype=str))
    out['programming_period'] = df.get('Programming_Period', pd.Series(dtype=str))
    out['instrument_subtype'] = df.get('Fund_Code', pd.Series(dtype=str))
    # Policy_Objective_Label is consistent across both programming periods (pp14-20 and pp21-27)
    # Thematic_Objective_Label is 0% populated in pp21-27 so is not used here
    out['policy_domain']      = df.get('Policy_Objective_Label', pd.Series(dtype=str))

    # Audit validation layer
    out['year_paid']                  = None      # single-operation; year = start date
    out['flow_stage']                 = 'allocated'  # col = Project_EU_Budget (EU allocation)
    out['financial_instrument_class'] = 'grant'   # cohesion policy = grants (Art. 58 CPR)
    out['management_type']            = 'shared'  # cohesion = shared management (Art. 63 FR)
    out['legal_basis']                = None      # no regulation reference in Kohesio data
    out['budget_line_code']           = df.get('Programme_Code', pd.Series(dtype=str))
    out['budget_execution_type']      = 'operational'  # cohesion = operational budget

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='eu_budget_shared', resolution_level='project')

    out.index = out.index  # preserve alignment after explode

    log.info(f"  Kohesio standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
