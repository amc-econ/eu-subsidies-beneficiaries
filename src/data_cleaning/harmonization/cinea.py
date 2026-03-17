"""
harmonization/cinea.py
=======================
Standardize CINEA (European Climate, Infrastructure and Environment Executive Agency) data
from the Massexport.xlsx file.

Source type:    Beneficiary-level EU programme grants
Granularity:    Beneficiary × project level
Amount:         'Participants EU contribution'
Coverage:       Programmes: INNOVFUND, CEF, LIFE, RENEWFM, EMFAF, JTM-PSLF, HORIZON, etc.

CRITICAL INCLUSION RULES — DO NOT MODIFY:
    1. HORIZON is EXCLUDED: HORIZON funding is already captured in RESEARCH (CORDIS)
       and partially in FTS. Including it here would double-count.
    2. INNOVFUND is FULLY ADDITIVE: funded from EU ETS carbon auction revenues,
       NOT from the EU budget. It does NOT appear in FTS. Treat as genuinely new.
    3. CEF, LIFE, EMFAF, RENEWFM: potentially overlap with FTS. Flagged
       'possible_fts_overlap'. Overlap check performed in check_cinea_fts_overlap().
    4. Aggregate 'TOTALS' rows are excluded to prevent double-counting.

Returns three DataFrames:
    - cinea_df:     All non-HORIZON rows (written as standardized_CINEA.csv)
    - innovfund_df: INNOVFUND rows only (written as standardized_INNOVFUND.csv)
    - horizon_df:   HORIZON rows only (NOT written to CSV — used for overlap checking
                    against RESEARCH in check_cinea_research_overlap())

Moved verbatim from deep_validation_analysis_v2.py standardize_cinea() (line 440).
"""

import logging
from pathlib import Path

import pandas as pd

from .utils import (
    CINEA_EXCLUDE_PROGRAMMES,
    CINEA_STATUS_MAP,
    COMMON_COLUMNS,
    VALID_FLOW_STAGES,
    apply_v2_columns,
)


def standardize(
    data_dir: Path,
    log: logging.Logger,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """
    Load and standardize CINEA Massexport.xlsx (sheet MyWorkSheet-1).

    Excludes HORIZON programme (already in RESEARCH/FTS).
    Key programme of interest: INNOVFUND (genuinely new — EU ETS revenues, not in FTS).

    Parameters
    ----------
    data_dir : Path
        Directory containing Massexport.xlsx.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]
        (cinea_df, innovfund_df, horizon_df)
        - cinea_df:     all non-HORIZON rows in common schema
        - innovfund_df: INNOVFUND rows only
        - horizon_df:   HORIZON rows (for overlap check, NOT written to CSV)
    """
    massexport_file = data_dir / 'Massexport.xlsx'
    if not massexport_file.exists():
        log.warning(f"Massexport.xlsx not found at {massexport_file}. Skipping CINEA.")
        return None, None, None

    log.info(f"Loading CINEA Massexport: {massexport_file.name} ...")
    try:
        raw = pd.read_excel(massexport_file, sheet_name='MyWorkSheet-1', engine='openpyxl')
    except Exception as e:
        log.error(f"  Failed to read Massexport.xlsx: {e}")
        return None, None, None

    log.info(f"  Raw CINEA: {len(raw):,} rows, programmes: "
             f"{raw['Programme'].unique().tolist() if 'Programme' in raw.columns else 'N/A'}")

    # Separate HORIZON (for overlap check) from non-HORIZON
    # Also exclude 'Totals' aggregate rows to prevent double-counting
    if 'Programme' in raw.columns:
        prog_upper = raw['Programme'].str.upper().str.strip()
        horizon_raw = raw[prog_upper == 'HORIZON'].copy()
        # Exclude both HORIZON and aggregate TOTALS rows
        non_horizon = raw[~prog_upper.isin({'HORIZON', 'TOTALS'})].copy()
        totals_rows = (prog_upper == 'TOTALS').sum()
        log.info(f"  HORIZON rows: {len(horizon_raw):,} | Totals rows excluded: {totals_rows:,} | "
                 f"Non-HORIZON rows: {len(non_horizon):,}")
    else:
        horizon_raw = pd.DataFrame()
        non_horizon = raw.copy()

    def _build_cinea_std(df: pd.DataFrame, source_tag: str = 'CINEA') -> pd.DataFrame:
        """Build standardized schema from CINEA rows."""
        if df.empty:
            return pd.DataFrame()
        std = pd.DataFrame()
        std['source'] = source_tag

        # Beneficiary name
        if 'Participant legal name' in df.columns:
            std['beneficiary_name'] = df['Participant legal name'].astype(str).str.strip()
        else:
            std['beneficiary_name'] = 'Unknown'

        # Country: extract 2-letter code from NUTS (first 2 chars)
        if 'Participant NUTS code' in df.columns:
            std['country'] = df['Participant NUTS code'].astype(str).str[:2].str.upper()
            std['country'] = std['country'].where(std['country'].str.match(r'^[A-Z]{2}$'), None)
        else:
            std['country'] = None

        # Amount
        if 'Participants EU contribution' in df.columns:
            std['amount_eur'] = pd.to_numeric(df['Participants EU contribution'], errors='coerce').fillna(0)
        else:
            std['amount_eur'] = 0.0

        # Amount type: varies by programme
        if 'Programme' in df.columns:
            std['amount_type'] = df['Programme'].apply(lambda p: _cinea_amount_type(str(p)))
        else:
            std['amount_type'] = 'eu_grant'

        # Year
        if 'Call year' in df.columns:
            std['year'] = pd.to_numeric(df['Call year'], errors='coerce')
        else:
            std['year'] = None

        # Sector / description
        if 'Programme name' in df.columns:
            std['sector_description'] = df['Programme name'].astype(str)
        else:
            std['sector_description'] = None

        if 'Project title' in df.columns:
            std['description'] = df['Project title'].astype(str)
        else:
            std['description'] = None

        # Source record ID: project number
        if 'Project number' in df.columns:
            std['source_record_id'] = df['Project number'].astype(str)
        else:
            std['source_record_id'] = None

        # Overlap flags
        if 'Programme' in df.columns:
            std['overlap_flags'] = df['Programme'].apply(lambda p: _cinea_overlap_flag(str(p)))
        else:
            std['overlap_flags'] = ''

        std['granularity'] = 'beneficiary'
        std['nace_2digit'] = None
        std['original_columns'] = None

        # Programme/fund structure layer
        std['programme']          = df['Programme'].astype(str) if 'Programme' in df.columns else None
        std['fund']               = df['Programme name'].astype(str) if 'Programme name' in df.columns else None
        std['programming_period'] = df['Financial framework'].astype(str) if 'Financial framework' in df.columns else None
        std['instrument_subtype'] = df['Subprogramme'].astype(str) if 'Subprogramme' in df.columns else None
        std['policy_domain']      = None

        # Audit validation layer
        std['year_paid']                  = None   # no separate payment date
        # Map raw Project status to canonical flow_stage
        if 'Project status' in df.columns:
            raw_status = df['Project status'].str.lower()
            std['flow_stage'] = raw_status.map(CINEA_STATUS_MAP).fillna('ongoing')
        else:
            raw_status = pd.Series(dtype=str, index=df.index)
            std['flow_stage'] = 'ongoing'
        std['financial_instrument_class'] = 'grant'   # CINEA programmes = EU grants
        std['management_type']            = 'direct'  # executive agency = direct management (Reg. 58/2003)
        std['legal_basis']                = None   # no legal basis field in CINEA data
        std['budget_line_code']           = None   # call acronym is not budget nomenclature
        std['budget_execution_type']      = 'operational'  # project funding = operational

        # --- Schema v2 columns ---
        std['flow_stage_confidence'] = raw_status.apply(
            lambda s: 'verified' if pd.notna(s) and s in VALID_FLOW_STAGES else 'inferred')
        std['flow_stage_assumption'] = raw_status.apply(
            lambda s: f'Mapped from CINEA Project status: {s}' if pd.notna(s) and s not in VALID_FLOW_STAGES else None)
        std['exclude_reason'] = None
        std['is_primary_record'] = True
        apply_v2_columns(std, fiscal_source_type='eu_budget_direct', resolution_level='beneficiary')

        std.reset_index(drop=True, inplace=True)
        return std[COMMON_COLUMNS]

    def _cinea_amount_type(programme: str) -> str:
        p = programme.upper()
        if p == 'INNOVFUND':
            return 'eu_grant_innovfund'
        elif p == 'CEF':
            return 'eu_grant_cef'
        elif p == 'LIFE':
            return 'eu_grant_life'
        elif p in ('RENEWFM', 'EMFAF', 'JTM-PSLF'):
            return 'eu_grant_other'
        else:
            return 'eu_grant'

    def _cinea_overlap_flag(programme: str) -> str:
        p = programme.upper()
        if p in ('CEF', 'LIFE', 'EMFAF', 'RENEWFM'):
            return 'possible_fts_overlap'
        elif p == 'INNOVFUND':
            # INNOVFUND is funded from EU ETS revenues — confirmed NOT in FTS
            return 'confirmed_new:ets_revenues_not_in_fts'
        else:
            return ''

    # Build standardized DFs
    # IMPORTANT: Exclude INNOVFUND from cinea_df to avoid double-counting.
    # INNOVFUND is written separately as standardized_INNOVFUND.csv.
    if 'Programme' in non_horizon.columns:
        innovfund_mask = non_horizon['Programme'].str.upper() == 'INNOVFUND'
        cinea_no_innov = non_horizon[~innovfund_mask].copy()
        innovfund_raw = non_horizon[innovfund_mask].copy()
        innovfund_df = _build_cinea_std(innovfund_raw, 'INNOVFUND')
    else:
        cinea_no_innov = non_horizon.copy()
        innovfund_df = pd.DataFrame()

    cinea_df = _build_cinea_std(cinea_no_innov, 'CINEA')
    horizon_df = _build_cinea_std(horizon_raw, 'CINEA_HORIZON')

    if cinea_df is not None and not cinea_df.empty:
        log.info(f"  CINEA non-HORIZON: {len(cinea_df):,} rows, "
                 f"EUR {cinea_df['amount_eur'].sum():,.0f}")
    if innovfund_df is not None and not innovfund_df.empty:
        log.info(f"  INNOVFUND: {len(innovfund_df):,} rows, "
                 f"EUR {innovfund_df['amount_eur'].sum():,.0f}")
    if horizon_df is not None and not horizon_df.empty:
        log.info(f"  CINEA HORIZON (excluded): {len(horizon_df):,} rows, "
                 f"EUR {horizon_df['amount_eur'].sum():,.0f}")

    return cinea_df, innovfund_df, horizon_df
