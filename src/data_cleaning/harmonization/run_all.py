"""
harmonize_all.py
================
Canonical harmonization driver for the EU Subsidies pipeline.

Responsibilities:
    1. Call standardize() on each harmonization module (8 core + CINEA/ESIF).
    2. Run flag_overlaps() to add cross-source overlap markers.
    3. Write standardized_{SOURCE}.csv to output_dir for all core sources.
    4. Write standardized_CINEA.csv (non-HORIZON) and standardized_INNOVFUND.csv.
    5. Write ESIF programme files as contextual-only (marked in filename and metadata).
    6. Write overlap_matrix.csv, diagnostic_report.txt, pipeline_metadata.json.
    7. Write dimensional_cardinality_table.csv.
    8. Write profile_{source}.json for each source.

This module replaces the main() function of the legacy analysis.py.
analysis.py is now a thin backward-compatible shim that calls run() here.

All economic logic and overlap interpretation is preserved verbatim from analysis.py.
Do NOT change flag_overlaps(), profile_source(), or write_diagnostic_report() without
updating the corresponding interpretation notes in README_ARCHITECTURE.md.
"""

import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

from . import cinea as cinea_mod
from . import ebrd as ebrd_mod
from . import eib as eib_mod
from . import esif_2014 as esif_2014_mod
from . import esif_2027 as esif_2027_mod
from . import fts as fts_mod
from . import kohesio as kohesio_mod
from . import research as research_mod
from . import rrf as rrf_mod
from . import scoreboard as scoreboard_mod
from . import tam as tam_mod
from . import tam_supplements as tam_supplements_mod
from .utils import EU27, extract_year, validate_schema


# ---------------------------------------------------------------------------
# LOGGING SETUP
# ---------------------------------------------------------------------------

def setup_logging(output_dir: Path) -> logging.Logger:
    """Initialize logger with file + stdout handlers."""
    output_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger('subsidies_harmonization')
    log.setLevel(logging.DEBUG)
    log.handlers.clear()

    fmt = logging.Formatter('%(asctime)s  %(levelname)-8s  %(message)s',
                            datefmt='%H:%M:%S')

    ch = logging.StreamHandler(sys.stdout)
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    log.addHandler(ch)

    fh = logging.FileHandler(output_dir / 'pipeline.log', mode='w', encoding='utf-8')
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    log.addHandler(fh)

    return log


# ---------------------------------------------------------------------------
# PROFILER (moved verbatim from analysis.py lines 549-619)
# ---------------------------------------------------------------------------

def profile_source(
    df: pd.DataFrame,
    source_name: str,
    amount_col: str | None,
    date_col: str | None,
    country_col: str | None,
    log: logging.Logger,
) -> dict:
    """Generate profiling statistics for a loaded source."""
    stats: dict = {
        'source': source_name,
        'rows': len(df),
        'columns': list(df.columns),
        'dtypes': {c: str(df[c].dtype) for c in df.columns},
    }

    # Amount stats
    if amount_col and amount_col in df.columns:
        amt = df[amount_col].dropna()
        stats['amount'] = {
            'column': amount_col,
            'count_non_null': int(amt.count()),
            'count_null': int(df[amount_col].isna().sum()),
            'count_zero': int((amt == 0).sum()),
            'count_negative': int((amt < 0).sum()),
            'sum': float(amt.sum()),
            'mean': float(amt.mean()) if len(amt) > 0 else 0,
            'median': float(amt.median()) if len(amt) > 0 else 0,
            'min': float(amt.min()) if len(amt) > 0 else 0,
            'max': float(amt.max()) if len(amt) > 0 else 0,
            'p01': float(amt.quantile(0.01)) if len(amt) > 0 else 0,
            'p25': float(amt.quantile(0.25)) if len(amt) > 0 else 0,
            'p75': float(amt.quantile(0.75)) if len(amt) > 0 else 0,
            'p99': float(amt.quantile(0.99)) if len(amt) > 0 else 0,
        }
    else:
        stats['amount'] = None

    # Date/year coverage
    if date_col and date_col in df.columns:
        years = extract_year(df[date_col]).dropna()
        if len(years) > 0:
            stats['year_range'] = {'min': int(years.min()), 'max': int(years.max())}
            stats['year_distribution'] = {
                str(int(k)): int(v) for k, v in years.value_counts().sort_index().items()
            }
        else:
            stats['year_range'] = None
    else:
        stats['year_range'] = None

    # Country coverage
    if country_col and country_col in df.columns:
        stats['countries'] = {
            'unique': int(df[country_col].nunique()),
            'top_10_by_count': {str(k): int(v) for k, v in df[country_col].value_counts().head(10).items()},
        }
        if amount_col and amount_col in df.columns:
            top_by_amt = df.groupby(country_col)[amount_col].sum().sort_values(
                ascending=False).head(10)
            stats['countries']['top_10_by_amount'] = {
                k: float(v) for k, v in top_by_amt.items()}
    else:
        stats['countries'] = None

    # Missing value summary
    stats['missing_pct'] = {
        c: round(df[c].isna().sum() / len(df) * 100, 1) for c in df.columns
    }

    # Duplicate rows
    stats['duplicate_rows'] = int(df.duplicated().sum())

    if stats['amount']:
        log.info(f"  Profile {source_name}: {stats['rows']:,} rows, "
                 f"amount sum = EUR {stats['amount']['sum']:,.0f}")
    else:
        log.info(f"  Profile {source_name}: {stats['rows']:,} rows, no amount column")

    return stats


# ---------------------------------------------------------------------------
# OVERLAP FLAGGING (moved verbatim from analysis.py lines 974-1075)
# ---------------------------------------------------------------------------

def flag_overlaps(datasets: dict[str, pd.DataFrame], log: logging.Logger) -> dict:
    """
    Cross-reference datasets and set overlap_flags.

    Modifies DataFrames in place by appending flags to the overlap_flags column.
    Returns a dict of overlap statistics for reporting.

    Overlap logic (DO NOT MODIFY — see README_ARCHITECTURE.md for rationale):
      1. TAM <-> Scoreboard: shared AID_MEASURE_ID case numbers
      2. FTS <-> Research:   FTS rows in Horizon/H2020/FP7 programmes
      3. EIB -> FTS:         EIB rows referencing EU guarantee/InvestEU/EFSI
      4. EBRD non-EU:        already flagged in standardize_ebrd() — logged here
      5. RRF -> TAM:         RRF grant measures (planned vs actual, informational)
      6. FTS <-> Kohesio:    FTS rows in structural/cohesion/ERDF/ESF programmes
    """
    log.info("=== Overlap Flagging ===")
    overlap_stats: dict = {}

    # --- 1. TAM <-> Scoreboard ---
    if 'TAM' in datasets and 'SCOREBOARD' in datasets:
        tam_ids = set(datasets['TAM']['source_record_id'].dropna().unique())
        sb_ids = set(datasets['SCOREBOARD']['source_record_id'].dropna().unique())
        shared = tam_ids & sb_ids
        log.info(f"  TAM-Scoreboard: {len(shared):,} shared case IDs")
        log.info(f"    TAM-only: {len(tam_ids - sb_ids):,}, Scoreboard-only: {len(sb_ids - tam_ids):,}")

        # Flag TAM rows that overlap with Scoreboard
        mask_tam = datasets['TAM']['source_record_id'].isin(shared)
        datasets['TAM'].loc[mask_tam, 'overlap_flags'] = datasets['TAM'].loc[
            mask_tam, 'overlap_flags'].apply(
            lambda x: (x + ',scoreboard_overlap').strip(',') if x else 'scoreboard_overlap')

        # Flag Scoreboard rows that overlap with TAM
        mask_sb = datasets['SCOREBOARD']['source_record_id'].isin(shared)
        datasets['SCOREBOARD'].loc[mask_sb, 'overlap_flags'] = datasets['SCOREBOARD'].loc[
            mask_sb, 'overlap_flags'].apply(
            lambda x: (x + ',tam_overlap').strip(',') if x else 'tam_overlap')

        overlap_stats['tam_scoreboard'] = {
            'shared_case_ids': len(shared),
            'tam_only': len(tam_ids - sb_ids),
            'scoreboard_only': len(sb_ids - tam_ids),
            'tam_flagged_rows': int(mask_tam.sum()),
            'scoreboard_flagged_rows': int(mask_sb.sum()),
        }

    # --- 2. FTS <-> Research ---
    if 'FTS' in datasets:
        prog_col = 'sector_description'  # mapped from Programme name
        if prog_col in datasets['FTS'].columns:
            research_pattern = r'(?i)(?:horizon|h2020|fp7|framework programme)'
            mask_fts_research = datasets['FTS'][prog_col].astype(str).str.contains(
                research_pattern, na=False)
            datasets['FTS'].loc[mask_fts_research, 'overlap_flags'] = datasets['FTS'].loc[
                mask_fts_research, 'overlap_flags'].apply(
                lambda x: (x + ',research_programme_overlap').strip(',') if x else 'research_programme_overlap')
            log.info(f"  FTS-Research: {mask_fts_research.sum():,} FTS rows flagged as research programme")
            overlap_stats['fts_research'] = {
                'fts_research_rows': int(mask_fts_research.sum()),
                'fts_total_rows': len(datasets['FTS']),
                'pct': round(mask_fts_research.sum() / len(datasets['FTS']) * 100, 1),
            }

    # --- 3. EIB potential FTS overlap ---
    if 'EIB' in datasets:
        desc_col = 'description'
        if desc_col in datasets['EIB'].columns:
            eu_pattern = r'(?i)analy(?:eu guarantee|european fund|investeu|efsi|eu budget)'
            mask_eib = datasets['EIB'][desc_col].astype(str).str.contains(
                eu_pattern, na=False)
            datasets['EIB'].loc[mask_eib, 'overlap_flags'] = datasets['EIB'].loc[
                mask_eib, 'overlap_flags'].apply(
                lambda x: (x + ',potential_fts_overlap').strip(',') if x else 'potential_fts_overlap')
            log.info(f"  EIB-FTS: {mask_eib.sum():,} EIB rows flagged as potential FTS overlap")
            overlap_stats['eib_fts'] = {
                'eib_flagged_rows': int(mask_eib.sum()),
            }

    # --- 4. EBRD non-EU already flagged in standardize_ebrd ---
    if 'EBRD' in datasets:
        non_eu = (datasets['EBRD']['overlap_flags'].str.contains('non_eu', na=False)).sum()
        log.info(f"  EBRD: {non_eu:,} non-EU rows (already flagged)")
        overlap_stats['ebrd_non_eu'] = {'non_eu_rows': int(non_eu)}

    # --- 5. RRF potential_tam_overlap already flagged in standardize_rrf ---
    if 'RRF' in datasets:
        rrf_tam = (datasets['RRF']['overlap_flags'].str.contains(
            'potential_tam_overlap', na=False)).sum()
        log.info(f"  RRF-TAM: {rrf_tam:,} RRF grant measures flagged as potential TAM overlap")
        overlap_stats['rrf_tam'] = {'rrf_flagged_rows': int(rrf_tam)}

    # --- 6. Kohesio <-> FTS ---
    if 'KOHESIO' in datasets and 'FTS' in datasets:
        prog_col = 'sector_description'
        if prog_col in datasets['FTS'].columns:
            esif_pattern = r'(?i)(?:structural|cohesion|erdf|esf|interreg|regional development)'
            mask_fts_esif = datasets['FTS'][prog_col].astype(str).str.contains(
                esif_pattern, na=False)
            datasets['FTS'].loc[mask_fts_esif, 'overlap_flags'] = datasets['FTS'].loc[
                mask_fts_esif, 'overlap_flags'].apply(
                lambda x: (x + ',potential_kohesio_overlap').strip(',') if x else 'potential_kohesio_overlap')
            log.info(f"  FTS-Kohesio: {mask_fts_esif.sum():,} FTS rows in structural fund programmes")
            overlap_stats['fts_kohesio'] = {
                'fts_esif_rows': int(mask_fts_esif.sum()),
            }

    return overlap_stats


# ---------------------------------------------------------------------------
# DIAGNOSTIC REPORT (moved verbatim from analysis.py lines 1082-1234)
# ---------------------------------------------------------------------------

def write_diagnostic_report(
    profiles: dict,
    overlap_stats: dict,
    output_dir: Path,
    log: logging.Logger,
) -> str:
    """Write a human-readable diagnostic report."""
    lines = []
    lines.append("=" * 80)
    lines.append("EU SUBSIDIES DATA CONSOLIDATION - DIAGNOSTIC REPORT")
    lines.append(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append("=" * 80)
    lines.append("")

    # --- Per-source summary table ---
    lines.append("1. SOURCE OVERVIEW")
    lines.append("-" * 80)
    header = f"{'Source':<12} {'Rows':>12} {'Total EUR':>20} {'Type':<12} {'Granularity':<12} {'Years':<12} {'Countries':>6}"
    lines.append(header)
    lines.append("-" * 80)

    for name, prof in profiles.items():
        rows = prof.get('rows', 0)
        total = prof.get('amount', {}).get('sum', 0) if prof.get('amount') else 0
        yr_range = prof.get('year_range', None)
        yr_str = f"{yr_range['min']}-{yr_range['max']}" if yr_range else 'N/A'
        ctry = prof.get('countries', {}).get('unique', 0) if prof.get('countries') else 0

        type_map = {
            'TAM': ('grants', 'award'),
            'FTS': ('mixed', 'contract'),
            'EIB': ('loans', 'project'),
            'EBRD': ('loans/eq', 'project'),
            'RRF': ('planned', 'measure'),
            'SCOREBOARD': ('aggregate', 'case/year'),
            'RESEARCH': ('eu_contrib', 'project'),
        }
        typ, gran = type_map.get(name, ('unknown', 'unknown'))

        lines.append(f"{name:<12} {rows:>12,} {total:>20,.0f} {typ:<12} {gran:<12} {yr_str:<12} {ctry:>6}")

    lines.append("")

    # --- Per-source detail ---
    lines.append("2. SOURCE DETAILS")
    lines.append("-" * 80)
    for name, prof in profiles.items():
        lines.append(f"\n  [{name}]")
        if prof.get('amount'):
            a = prof['amount']
            lines.append(f"    Amount column: {a['column']}")
            lines.append(f"    Non-null: {a['count_non_null']:,}, Null: {a['count_null']:,}, "
                         f"Zero: {a['count_zero']:,}, Negative: {a['count_negative']:,}")
            lines.append(f"    Sum: EUR {a['sum']:,.0f}")
            lines.append(f"    Mean: EUR {a['mean']:,.0f}, Median: EUR {a['median']:,.0f}")
            lines.append(f"    Min: EUR {a['min']:,.0f}, Max: EUR {a['max']:,.0f}")
            lines.append(f"    P1: EUR {a['p01']:,.0f}, P25: EUR {a['p25']:,.0f}, "
                         f"P75: EUR {a['p75']:,.0f}, P99: EUR {a['p99']:,.0f}")
        if prof.get('countries'):
            c = prof['countries']
            lines.append(f"    Countries: {c['unique']} unique")
            lines.append(f"    Top by count: {dict(list(c['top_10_by_count'].items())[:5])}")
            if 'top_10_by_amount' in c:
                top5 = {k: f"EUR {v:,.0f}" for k, v in list(c['top_10_by_amount'].items())[:5]}
                lines.append(f"    Top by amount: {top5}")
        lines.append(f"    Duplicate rows: {prof.get('duplicate_rows', 0):,}")

    lines.append("")

    # --- Overlap analysis ---
    lines.append("3. OVERLAP ANALYSIS")
    lines.append("-" * 80)

    if 'tam_scoreboard' in overlap_stats:
        o = overlap_stats['tam_scoreboard']
        lines.append(f"\n  TAM <-> Scoreboard:")
        lines.append(f"    Shared case IDs: {o['shared_case_ids']:,}")
        lines.append(f"    TAM-only case IDs: {o['tam_only']:,}")
        lines.append(f"    Scoreboard-only case IDs: {o['scoreboard_only']:,}")
        lines.append(f"    TAM rows flagged: {o['tam_flagged_rows']:,}")
        lines.append(f"    Scoreboard rows flagged: {o['scoreboard_flagged_rows']:,}")
        lines.append(f"    RECOMMENDATION: Use TAM for granular analysis. "
                     f"Scoreboard useful for cross-validation only.")

    if 'fts_research' in overlap_stats:
        o = overlap_stats['fts_research']
        lines.append(f"\n  FTS <-> Research:")
        lines.append(f"    FTS rows in research programmes: {o['fts_research_rows']:,} "
                     f"({o['pct']}% of FTS)")
        lines.append(f"    RECOMMENDATION: When using FTS + Research together, "
                     f"exclude FTS rows flagged 'research_programme_overlap'.")

    if 'eib_fts' in overlap_stats:
        o = overlap_stats['eib_fts']
        lines.append(f"\n  EIB -> FTS:")
        lines.append(f"    EIB rows with potential FTS overlap: {o['eib_flagged_rows']:,}")
        lines.append(f"    RECOMMENDATION: EIB is primarily own-resource lending, "
                     f"minimal overlap with FTS budget.")

    if 'ebrd_non_eu' in overlap_stats:
        o = overlap_stats['ebrd_non_eu']
        lines.append(f"\n  EBRD geographic scope:")
        lines.append(f"    Non-EU rows: {o['non_eu_rows']:,}")
        lines.append(f"    RECOMMENDATION: Filter on country for EU-only analysis.")

    if 'rrf_tam' in overlap_stats:
        o = overlap_stats['rrf_tam']
        lines.append(f"\n  RRF -> TAM:")
        lines.append(f"    RRF grant measures (potential TAM overlap): {o['rrf_flagged_rows']:,}")
        lines.append(f"    RECOMMENDATION: RRF is planned allocations (measure-level), "
                     f"TAM is actual awards (beneficiary-level). Cannot precisely deduplicate.")

    if 'fts_kohesio' in overlap_stats:
        o = overlap_stats['fts_kohesio']
        lines.append(f"\n  FTS <-> Kohesio:")
        lines.append(f"    FTS rows in structural fund programmes: {o['fts_esif_rows']:,}")
        lines.append(f"    RECOMMENDATION: FTS covers direct EU budget spending, Kohesio covers "
                     f"shared-management (ESIF/cohesion). Minimal overlap expected but flagged.")

    lines.append("")

    # --- Recommendations ---
    lines.append("4. USAGE RECOMMENDATIONS")
    lines.append("-" * 80)
    lines.append("""
  For grant/subsidy analysis:
    - Primary: TAM (individual state aid awards) + FTS (EU direct spending)
    - Exclude FTS rows flagged 'research_programme_overlap' to avoid double-counting
      with Research source
    - Do NOT add Scoreboard on top of TAM (same underlying data)

  For loan/investment analysis:
    - EIB (EU development bank loans)
    - EBRD (development bank loans/equity, filter to EU-27 if needed)

  For planned allocation analysis:
    - RRF (national recovery plan measures, not actual disbursements)
    - Note: Some RRF implementations may appear in TAM as actual awards

  For cross-validation:
    - Scoreboard vs TAM: compare case-level totals
    - FTS research rows vs Research projects: compare programme-level totals

  For automotive sector filtering (downstream):
    - TAM: use SECTOR_SD text matching (split on semicolons!)
    - RRF: use NACE 2-digit code = 29 (motor vehicles) or 45 (trade/repair)
    - FTS/EIB/EBRD: use description/sector text matching
    - Research: use keywords/topics text matching
""")

    lines.append("=" * 80)

    report_text = '\n'.join(lines)
    report_path = output_dir / 'diagnostic_report.txt'
    report_path.write_text(report_text, encoding='utf-8')
    log.info(f"Diagnostic report: {report_path}")
    return report_text


# ---------------------------------------------------------------------------
# DIMENSIONAL CARDINALITY TABLE
# ---------------------------------------------------------------------------

def write_dimensional_cardinality_table(
    datasets: dict[str, pd.DataFrame],
    output_dir: Path,
    log: logging.Logger,
) -> pd.DataFrame:
    """Write dimensional_cardinality_table.csv — unique values per column per source."""
    rows = []
    for source, df in datasets.items():
        for col in df.columns:
            rows.append({
                'source': source,
                'column': col,
                'dtype': str(df[col].dtype),
                'n_unique': int(df[col].nunique()),
                'n_null': int(df[col].isna().sum()),
                'pct_null': round(df[col].isna().sum() / max(len(df), 1) * 100, 1),
            })
    result = pd.DataFrame(rows)
    out_path = output_dir / 'dimensional_cardinality_table.csv'
    result.to_csv(out_path, index=False, encoding='utf-8-sig')
    log.info(f"  {out_path.name}")
    return result


# ---------------------------------------------------------------------------
# MAIN ORCHESTRATION
# ---------------------------------------------------------------------------

def run(
    data_dir: Path,
    output_dir: Path,
    log: logging.Logger,
) -> dict[str, pd.DataFrame]:
    """
    Run the full harmonization pipeline.

    Parameters
    ----------
    data_dir : Path
        Directory containing all raw data files and subdirectories.
    output_dir : Path
        Directory where standardized CSVs and reports will be written.
    log : logging.Logger
        Logger instance.

    Returns
    -------
    dict[str, pd.DataFrame]
        Standardized DataFrames keyed by source name.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    start_time = datetime.now()

    log.info("=" * 60)
    log.info("EU Subsidies Harmonization Pipeline")
    log.info("=" * 60)

    # ---- Step 1: Standardize all 8 core sources --------------------------------
    log.info("\n=== STANDARDIZATION ===")

    core_sources = [
        ('TAM',        tam_mod,        'GRANTED_AMOUNT_FROM_EUR', 'DATE_GRANTED',         'BENEFICIARY_MS'),
        ('FTS',        fts_mod,        'amount_eur',              'year',                 'country'),
        ('EIB',        eib_mod,        'amount_eur',              'year',                 'country'),
        ('EBRD',       ebrd_mod,       'amount_eur',              'year',                 'country'),
        ('RRF',        rrf_mod,        'amount_eur',              None,                   'country'),
        ('SCOREBOARD', scoreboard_mod, 'amount_eur',              'year',                 'country'),
        ('RESEARCH',   research_mod,   'amount_eur',              'year',                 None),
        ('KOHESIO',    kohesio_mod,    'amount_eur',              'year',                 'country'),
        ('TAM_SUPPLEMENTS', tam_supplements_mod, 'amount_eur',       'year',                 'country'),
    ]

    datasets: dict[str, pd.DataFrame] = {}
    profiles: dict[str, dict] = {}

    for name, module, amt_col_raw, date_col_raw, country_col_raw in core_sources:
        try:
            std_df = module.standardize(data_dir, log)
            validate_schema(std_df, name, log)
            datasets[name] = std_df
            # Profile using standardized columns (amount_eur, year, country)
            prof = profile_source(
                std_df, name,
                'amount_eur',
                'year' if 'year' in std_df.columns else None,
                'country' if 'country' in std_df.columns else None,
                log,
            )
            profiles[name] = prof
            # Save profile JSON
            prof_path = output_dir / f'profile_{name.lower()}.json'
            with open(prof_path, 'w', encoding='utf-8') as f:
                json.dump(prof, f, indent=2, default=str, ensure_ascii=False)
        except Exception as e:
            import traceback
            log.error(f"FAILED to standardise {name}: {e}")
            log.error(traceback.format_exc())

    log.info(f"\nStandardised {len(datasets)}/{len(core_sources)} core sources")

    # ---- Step 2: CINEA (returns tuple of 3 DataFrames) -------------------------
    log.info("\n=== CINEA ===")
    cinea_df = innovfund_df = horizon_df = None
    try:
        cinea_df, innovfund_df, horizon_df = cinea_mod.standardize(data_dir, log)
        if cinea_df is not None and not cinea_df.empty:
            validate_schema(cinea_df, 'CINEA', log)
        if innovfund_df is not None and not innovfund_df.empty:
            validate_schema(innovfund_df, 'INNOVFUND', log)
    except Exception as e:
        import traceback
        log.error(f"FAILED to standardise CINEA: {e}")
        log.error(traceback.format_exc())

    # ---- Step 3: Overlap flagging ----------------------------------------------
    log.info("\n=== OVERLAP DETECTION ===")
    overlap_stats = flag_overlaps(datasets, log)

    # ---- Step 4: Write core standardized CSVs ----------------------------------
    log.info("\n=== SAVING OUTPUTS ===")
    for name, df in datasets.items():
        out_path = output_dir / f'standardized_{name.lower()}.csv'
        df.to_csv(out_path, index=False, encoding='utf-8-sig')
        log.info(f"  {out_path.name}: {len(df):,} rows")

    # Write CINEA outputs
    if cinea_df is not None and not cinea_df.empty:
        cinea_path = output_dir / 'standardized_cinea.csv'
        cinea_df.to_csv(cinea_path, index=False, encoding='utf-8-sig')
        log.info(f"  standardized_cinea.csv: {len(cinea_df):,} rows")

    if innovfund_df is not None and not innovfund_df.empty:
        innov_path = output_dir / 'standardized_innovfund.csv'
        innovfund_df.to_csv(innov_path, index=False, encoding='utf-8-sig')
        log.info(f"  standardized_innovfund.csv: {len(innovfund_df):,} rows")

    # ESIF programme files: contextual only — load but mark clearly
    log.info("\n=== ESIF (CONTEXTUAL ONLY) ===")
    for esif_tag, esif_module in [('esif_2014', esif_2014_mod), ('esif_2027', esif_2027_mod)]:
        try:
            esif_std, _ = esif_module.standardize(data_dir, log)
            if esif_std is not None and not esif_std.empty:
                validate_schema(esif_std, esif_tag.upper(), log)
                esif_path = output_dir / f'standardized_{esif_tag}_contextual_only.csv'
                esif_std.to_csv(esif_path, index=False, encoding='utf-8-sig')
                log.info(f"  {esif_path.name}: {len(esif_std):,} rows [CONTEXTUAL ONLY]")
        except Exception as e:
            log.warning(f"  Could not write {esif_tag}: {e}")

    # ---- Step 5: Overlap matrix CSV --------------------------------------------
    overlap_path = output_dir / 'overlap_matrix.csv'
    if overlap_stats:
        rows_list = []
        for key, val in overlap_stats.items():
            row = {'overlap_pair': key}
            row.update(val)
            rows_list.append(row)
        pd.DataFrame(rows_list).to_csv(overlap_path, index=False, encoding='utf-8-sig')
        log.info(f"  {overlap_path.name}")

    # ---- Step 6: Diagnostic report --------------------------------------------
    write_diagnostic_report(profiles, overlap_stats, output_dir, log)

    # ---- Step 7: Dimensional cardinality table --------------------------------
    write_dimensional_cardinality_table(datasets, output_dir, log)

    # ---- Step 8: Pipeline metadata --------------------------------------------
    meta_out = {
        'schema_version': '2.0',
        'timestamp': datetime.now().isoformat(),
        'duration_seconds': (datetime.now() - start_time).total_seconds(),
        'sources_standardised': list(datasets.keys()),
        'source_row_counts': {k: len(v) for k, v in datasets.items()},
        'source_totals_eur': {k: float(v['amount_eur'].sum()) for k, v in datasets.items()},
        'cinea_rows': len(cinea_df) if cinea_df is not None else 0,
        'innovfund_rows': len(innovfund_df) if innovfund_df is not None else 0,
        'contextual_only_sources': ['ESIF_2014', 'ESIF_2027', 'SCOREBOARD'],
        'interpretation_notes': {
            'RRF': 'measure-level planned allocation, NOT deduplicated against TAM',
            'SCOREBOARD': 'aggregated TAM data, excluded from headline totals',
            'ESIF': 'programme-level aggregates, overlap with Kohesio, contextual use only',
            'CINEA_HORIZON': 'excluded (already in RESEARCH/FTS)',
            'INNOVFUND': 'EU ETS revenues, genuinely additive',
        },
    }
    meta_path = output_dir / 'pipeline_metadata.json'
    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump(meta_out, f, indent=2, ensure_ascii=False)
    log.info(f"  {meta_path.name}")

    # ---- Summary --------------------------------------------------------------
    elapsed = (datetime.now() - start_time).total_seconds()
    log.info(f"\n{'=' * 60}")
    log.info(f"DONE in {elapsed:.1f}s")
    log.info(f"Output directory: {output_dir}")
    total_rows = sum(len(v) for v in datasets.values())
    total_eur = sum(float(v['amount_eur'].sum()) for v in datasets.values())
    log.info(f"Total: {total_rows:,} rows across {len(datasets)} sources, EUR {total_eur:,.0f}")
    log.info(f"{'=' * 60}")

    return datasets


# ---------------------------------------------------------------------------
# CLI ENTRY POINT
# ---------------------------------------------------------------------------

def main(data_dir: Path | None = None, output_dir: Path | None = None) -> None:
    """Run harmonization pipeline from the command line or as a library call."""
    if data_dir is None or output_dir is None:
        # Default: repo root is 4 parents up from src/data_cleaning/harmonization/run_all.py
        import os
        project_root = Path(os.environ.get(
            'SUBSIDIES_PROJECT_ROOT',
            Path(__file__).resolve().parent.parent.parent.parent
        ))
        if data_dir is None:
            data_dir = project_root / 'data' / 'raw'
        if output_dir is None:
            output_dir = project_root / 'data' / 'processed'
    log = setup_logging(output_dir)
    run(data_dir=data_dir, output_dir=output_dir, log=log)


if __name__ == '__main__':
    main()
