"""
harmonization/research.py
==========================
Standardize CORDIS Research projects (Horizon 2020 + Horizon Europe).

Source type:    EU-funded research project contributions
Granularity:    Project-level (one row per project, no participant breakdown)
Amount:         ecMaxContribution (EC max contribution per project)
Amount type:    'eu_contribution'

Overlap:
    Research funding flows through FTS — all FTS rows in Horizon/H2020/FP7
    programmes are flagged 'research_programme_overlap' by flag_overlaps().
    When using FTS + RESEARCH together, exclude FTS rows with that flag
    to avoid double-counting (controlled by master_builder.py).
    CINEA HORIZON rows are also excluded from CINEA source for the same reason.

Data notes:
    - Amount values use European decimal comma format (e.g. "3608915,55")
      → handled with regex replacement before numeric coercion.
    - No country column at project level; participant-level data not loaded.
    - Two files: project.xlsx (recent) + project2020.xlsx (legacy Horizon 2020).

Moved verbatim from analysis.py load_research() + standardize_research() (lines 402-440, 889-926).
"""

import logging
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import (
    COMMON_COLUMNS,
    RESEARCH_STATUS_MAP,
    VALID_FLOW_STAGES,
    apply_v2_columns,
    extract_year,
    pack_originals,
    safe_to_numeric,
)

# Research source files
RESEARCH_FILENAMES = ['project.xlsx', 'project2020.xlsx']


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """
    Load and standardize CORDIS research project data.

    Parameters
    ----------
    data_dir : Path
        Directory containing project.xlsx and project2020.xlsx.
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
    """Load research project files (CORDIS: project.xlsx + project2020.xlsx)."""
    log.info("Loading Research projects ...")
    frames = []
    for fname in RESEARCH_FILENAMES:
        fpath = data_dir / fname
        if not fpath.exists():
            log.warning(f"  Research file missing: {fpath.name}")
            continue
        df_f = pd.read_excel(fpath)
        df_f['_research_file'] = fpath.name
        frames.append(df_f)
        log.info(f"  {fpath.name}: {len(df_f):,} rows")

    df = pd.concat(frames, ignore_index=True)
    log.info(f"  Research merged: {len(df):,} rows")

    # Coerce amounts - these use European decimal commas (e.g. "3608915,55")
    for col in ['ecMaxContribution', 'totalCost']:
        if col in df.columns:
            # Replace European decimal comma with dot BEFORE stripping commas
            df[col] = df[col].astype(str).str.replace(
                r'(\d),(\d{1,2})$', r'\1.\2', regex=True)
            df[col] = safe_to_numeric(df[col], log, col)

    return df


def _standardize(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Map Research projects to common schema."""
    log.info("Standardising Research ...")

    out = pd.DataFrame()
    out['source'] = 'RESEARCH'
    out['source_record_id'] = df['id'].astype(str) if 'id' in df.columns else df.index.astype(str)
    out['granularity'] = 'project'
    out['beneficiary_name'] = None  # No participant info in these files
    out['country'] = ''  # No country in project-level data
    out['amount_eur'] = df.get('ecMaxContribution', np.nan)
    out['amount_type'] = 'eu_contribution'
    if 'startDate' in df.columns:
        out['year'] = extract_year(df['startDate'])
    else:
        out['year'] = None
    out['sector_description'] = df.get('topics', pd.Series(dtype=str))
    out['nace_2digit'] = None
    out['description'] = (
        df['acronym'].fillna('') + ' - ' + df['title'].fillna('')
        if 'acronym' in df.columns and 'title' in df.columns
        else df.get('title', pd.Series(dtype=str))
    )
    out['overlap_flags'] = 'fts_overlap'  # All research funding flows through FTS

    orig_cols = ['frameworkProgramme', 'fundingScheme', 'keywords',
                 'legalBasis', 'masterCall', 'totalCost', '_research_file']
    avail_orig = [c for c in orig_cols if c in df.columns]
    out['original_columns'] = df[avail_orig].apply(
        lambda r: pack_originals(r.to_dict()), axis=1)

    # Programme/fund structure layer
    out['programme']          = df.get('frameworkProgramme', pd.Series(dtype=str))
    out['fund']               = None
    out['programming_period'] = None
    out['instrument_subtype'] = df.get('fundingScheme', pd.Series(dtype=str))
    out['policy_domain']      = None

    # Audit validation layer
    out['year_paid']                  = None   # no payment date; ecSignatureDate ~ commitment
    # Map raw CORDIS status to canonical flow_stage
    raw_status = df.get('status', pd.Series(dtype=str)).str.lower()
    out['flow_stage'] = raw_status.map(RESEARCH_STATUS_MAP).fillna('ongoing')
    out['financial_instrument_class'] = 'grant'   # CORDIS = EU research grants
    out['management_type']            = 'direct'  # framework programmes = direct management (Art. 62 FR)
    out['legal_basis']                = df.get('legalBasis', pd.Series(dtype=str))
    out['budget_line_code']           = None   # masterCall is call ID, not budget nomenclature
    out['budget_execution_type']      = 'operational'  # R&D = operational expenditure

    # --- Schema v2 columns ---
    out['flow_stage_confidence'] = raw_status.apply(
        lambda s: 'verified' if pd.notna(s) and s in VALID_FLOW_STAGES else 'inferred')
    out['flow_stage_assumption'] = raw_status.apply(
        lambda s: f'Mapped from CORDIS status: {s}' if pd.notna(s) and s not in VALID_FLOW_STAGES else None)
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    apply_v2_columns(out, fiscal_source_type='eu_budget_direct', resolution_level='project')

    out.index = df.index
    log.info(f"  Research standardised: {len(out):,} rows")
    return out[COMMON_COLUMNS]
