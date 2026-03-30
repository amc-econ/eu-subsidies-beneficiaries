"""
Facility × Subsidy Intensity Analysis
======================================
Computes subsidy intensity metrics for automotive facilities (vehicle + battery)
by joining investment data from facilities_develop_export.xlsx against pipeline
subsidy outputs in consolidated_matches.csv.

Intended use-case: after running the matching pipeline on an automotive company
list, use this script to compare EU subsidy support against announced facility
investments and capacity — providing a EUR-per-EUR and EUR-per-GWh intensity view.

Inputs
------
- facilities_develop_export.xlsx   (repo root)
    Sheet: 'All Facilities'
    Filtered to product_lv1 ∈ {'vehicle', 'battery'}
    Key columns: inst_canon, iso2, main_investment_eur, main_capacity_gwh

- consolidated_matches.csv         (data/processed/match_output/ or custom path)
    Filtered to dc_preferred == True
    Key columns: match_reference_name, country, amount_gge

Output
------
- data/processed/facility_subsidy_intensity.csv
    One row per matched (company, country) pair.
    Columns: inst_canon, iso2, match_reference_name, total_investment_eur,
             total_capacity_gwh, n_facilities, product_types,
             total_gge, n_subsidy_rows,
             subsidy_intensity_investment, subsidy_intensity_capacity

Usage
-----
    python src/analysis/facility_subsidy_intensity.py
    python src/analysis/facility_subsidy_intensity.py --consolidated path/to/consolidated_matches.csv
    python src/analysis/facility_subsidy_intensity.py --facilities path/to/other_facilities.xlsx

Design notes
------------
- Uses only main_investment_eur / main_capacity_gwh (not phase columns) as the
  representative investment figure per facility. Phase-level columns exist but
  are messy and inconsistently populated. main_* is the cleaned aggregate.
- Company name matching: exact match first (lowercased + stripped), then fuzzy
  via rapidfuzz token_set_ratio at threshold >= 85. Country (iso2 vs country)
  must match to prevent cross-country false positives. Unmatched facilities
  are written to a separate unmatched log.
- No amount matching required — this is a join between investment data and
  subsidy data, not a deduplication operation.
"""

import argparse
import logging
import sys
from pathlib import Path

import pandas as pd

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

FACILITIES_XLSX = REPO_ROOT / 'facilities_develop_export.xlsx'
CONSOLIDATED_DEFAULT = REPO_ROOT / 'data' / 'processed' / 'match_output' / 'consolidated_matches.csv'
OUTPUT_CSV = REPO_ROOT / 'data' / 'processed' / 'facility_subsidy_intensity.csv'

PRODUCT_FILTER = {'vehicle', 'battery'}
FUZZY_THRESHOLD = 85


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise_name(s) -> str:
    """Lowercase + strip for name matching."""
    return str(s or '').lower().strip()


def _normalise_country(s) -> str:
    """Uppercase ISO-2 for country matching."""
    return str(s or '').upper().strip()


def _build_fuzzy_match(fac_names: list[str], sub_names: list[str]) -> dict[str, str]:
    """Return {fac_norm → sub_name} for fuzzy matches above FUZZY_THRESHOLD.

    Uses rapidfuzz token_set_ratio — the same algorithm used by generic_matcher.py
    in the main pipeline. Only matches that score >= FUZZY_THRESHOLD are kept.
    """
    try:
        from rapidfuzz import fuzz, process
    except ImportError:
        log.warning("rapidfuzz not installed — fuzzy matching disabled. pip install rapidfuzz")
        return {}

    mapping = {}
    for fac_norm in fac_names:
        result = process.extractOne(
            fac_norm,
            sub_names,
            scorer=fuzz.token_set_ratio,
            score_cutoff=FUZZY_THRESHOLD,
        )
        if result is not None:
            matched_sub_norm, score, _ = result
            mapping[fac_norm] = matched_sub_norm
            log.debug(f"  fuzzy match: '{fac_norm}' → '{matched_sub_norm}' (score={score:.0f})")
    return mapping


def load_facilities(xlsx_path: Path) -> pd.DataFrame:
    """Load and aggregate facilities data.

    Returns one row per (inst_canon, iso2) with summed investment + capacity.
    """
    log.info(f"Loading facilities from {xlsx_path.name} ...")
    df = pd.read_excel(xlsx_path, sheet_name='All Facilities')
    log.info(f"  Raw rows: {len(df):,}")

    df = df[df['product_lv1'].isin(PRODUCT_FILTER)].copy()
    log.info(f"  After filtering to {PRODUCT_FILTER}: {len(df):,} rows")

    # Coerce numeric columns
    df['main_investment_eur'] = pd.to_numeric(df['main_investment_eur'], errors='coerce')
    df['main_capacity_gwh'] = pd.to_numeric(df['main_capacity_gwh'], errors='coerce')

    # Normalise country
    df['iso2'] = df['iso2'].apply(_normalise_country)

    agg = df.groupby(['inst_canon', 'iso2'], dropna=False).agg(
        total_investment_eur=('main_investment_eur', 'sum'),
        total_capacity_gwh=('main_capacity_gwh', 'sum'),
        n_facilities=('_id', 'count'),
        product_types=('product_lv1', lambda x: ','.join(sorted(x.dropna().unique()))),
    ).reset_index()

    log.info(f"  Aggregated: {len(agg):,} (inst_canon, iso2) pairs")
    return agg


def load_subsidies(csv_path: Path) -> pd.DataFrame:
    """Load and aggregate preferred subsidy rows by (match_reference_name, country).

    Returns one row per company × country with summed GGE.
    """
    log.info(f"Loading consolidated subsidies from {csv_path.name} ...")
    df = pd.read_csv(csv_path, low_memory=False)
    log.info(f"  Raw rows: {len(df):,}")

    df = df[df['dc_preferred'] == True].copy()
    log.info(f"  After dc_preferred filter: {len(df):,} rows")

    df['amount_gge'] = pd.to_numeric(df['amount_gge'], errors='coerce').fillna(0)
    df['country'] = df['country'].apply(_normalise_country)

    agg = df.groupby(['match_reference_name', 'country'], dropna=False).agg(
        total_gge=('amount_gge', 'sum'),
        n_subsidy_rows=('amount_gge', 'count'),
    ).reset_index()

    log.info(f"  Aggregated: {len(agg):,} (company, country) subsidy pairs")
    return agg


def match_companies(
    fac_agg: pd.DataFrame,
    sub_agg: pd.DataFrame,
) -> pd.DataFrame:
    """Match inst_canon → match_reference_name with exact then fuzzy logic.

    Country (iso2 == country) must match. Returns a merged DataFrame with
    both facility and subsidy data plus intensity metrics.
    """
    # Build normalised lookup structures
    fac_agg = fac_agg.copy()
    fac_agg['_fac_norm'] = fac_agg['inst_canon'].apply(_normalise_name)
    fac_agg['_fac_ctry'] = fac_agg['iso2'].apply(_normalise_country)

    sub_agg = sub_agg.copy()
    sub_agg['_sub_norm'] = sub_agg['match_reference_name'].apply(_normalise_name)
    sub_agg['_sub_ctry'] = sub_agg['country'].apply(_normalise_country)

    # Step 1: Exact match on (normalised_name, country)
    sub_exact_map = {
        (row['_sub_norm'], row['_sub_ctry']): row['match_reference_name']
        for _, row in sub_agg.iterrows()
    }
    fac_agg['_exact_match'] = fac_agg.apply(
        lambda r: sub_exact_map.get((r['_fac_norm'], r['_fac_ctry'])), axis=1
    )

    unmatched_mask = fac_agg['_exact_match'].isna()
    n_exact = (~unmatched_mask).sum()
    log.info(f"  Exact matches: {n_exact:,} / {len(fac_agg):,}")

    # Step 2: Fuzzy match on remaining rows (per country bucket to reduce false positives)
    if unmatched_mask.any():
        fac_unmatched = fac_agg[unmatched_mask].copy()
        fuzzy_results = {}

        for ctry, ctry_fac in fac_unmatched.groupby('_fac_ctry'):
            ctry_sub = sub_agg[sub_agg['_sub_ctry'] == ctry]
            if ctry_sub.empty:
                continue
            fac_norms = ctry_fac['_fac_norm'].tolist()
            sub_norms = ctry_sub['_sub_norm'].tolist()
            # Build norm → original name mapping for sub side
            sub_norm_to_orig = dict(zip(ctry_sub['_sub_norm'], ctry_sub['match_reference_name']))
            fuzzy_norm_map = _build_fuzzy_match(fac_norms, sub_norms)
            for fac_norm, sub_norm in fuzzy_norm_map.items():
                fuzzy_results[fac_norm] = sub_norm_to_orig[sub_norm]

        fac_agg.loc[unmatched_mask, '_fuzzy_match'] = (
            fac_agg.loc[unmatched_mask, '_fac_norm'].map(fuzzy_results)
        )
        n_fuzzy = fac_agg['_fuzzy_match'].notna().sum()
        log.info(f"  Fuzzy matches: {n_fuzzy:,}")
    else:
        fac_agg['_fuzzy_match'] = None

    # Combine exact + fuzzy into single resolved name column
    fac_agg['_resolved_name'] = fac_agg['_exact_match'].combine_first(fac_agg['_fuzzy_match'])

    # Log unmatched
    unmatched = fac_agg[fac_agg['_resolved_name'].isna()]
    if not unmatched.empty:
        log.warning(f"  {len(unmatched):,} facility companies unmatched — not in subsidy output:")
        for _, row in unmatched.iterrows():
            log.warning(f"    '{row['inst_canon']}' ({row['iso2']})")

    # Keep only matched rows
    matched = fac_agg[fac_agg['_resolved_name'].notna()].copy()
    log.info(f"  Total matched: {len(matched):,} / {len(fac_agg):,} facility rows")

    # Merge on (resolved_name, country).
    # _resolved_name contains the original-case match_reference_name, so join
    # against _sub_ref_name (not _sub_norm which is lowercased).
    merged = matched.merge(
        sub_agg.rename(columns={'match_reference_name': '_sub_ref_name'}),
        left_on=['_resolved_name', '_fac_ctry'],
        right_on=['_sub_ref_name', '_sub_ctry'],
        how='left',
    )

    return merged, unmatched


def compute_intensity(merged: pd.DataFrame) -> pd.DataFrame:
    """Compute subsidy intensity ratios and tidy output columns."""
    merged = merged.copy()

    # EUR per EUR invested
    inv = pd.to_numeric(merged['total_investment_eur'], errors='coerce')
    gge = pd.to_numeric(merged['total_gge'], errors='coerce')
    merged['subsidy_intensity_investment'] = (gge / inv).where(inv > 0)

    # EUR per GWh capacity
    cap = pd.to_numeric(merged['total_capacity_gwh'], errors='coerce')
    merged['subsidy_intensity_capacity'] = (gge / cap).where(cap > 0)

    output_cols = [
        'inst_canon', 'iso2', '_sub_ref_name',
        'total_investment_eur', 'total_capacity_gwh', 'n_facilities', 'product_types',
        'total_gge', 'n_subsidy_rows',
        'subsidy_intensity_investment', 'subsidy_intensity_capacity',
    ]
    output_cols = [c for c in output_cols if c in merged.columns]
    result = merged[output_cols].rename(columns={'_sub_ref_name': 'match_reference_name'})
    result = result.sort_values('total_gge', ascending=False).reset_index(drop=True)
    return result


def run(
    facilities_path: Path = FACILITIES_XLSX,
    consolidated_path: Path = CONSOLIDATED_DEFAULT,
    output_path: Path = OUTPUT_CSV,
) -> pd.DataFrame:
    """Main entry point — load, match, compute, save."""
    if not facilities_path.exists():
        log.error(f"Facilities file not found: {facilities_path}")
        return pd.DataFrame()
    if not consolidated_path.exists():
        log.error(
            f"Consolidated matches not found: {consolidated_path}\n"
            "Run the matching pipeline first: python run_pipeline.py --stage match ..."
        )
        return pd.DataFrame()

    fac_agg = load_facilities(facilities_path)
    sub_agg = load_subsidies(consolidated_path)

    log.info("\nMatching facility companies to subsidy beneficiaries...")
    merged, unmatched = match_companies(fac_agg, sub_agg)

    result = compute_intensity(merged)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    result.to_csv(output_path, index=False)
    log.info(f"\nSaved {len(result):,} rows -> {output_path}")

    # Summary
    n_with_inv = result['subsidy_intensity_investment'].notna().sum()
    n_with_cap = result['subsidy_intensity_capacity'].notna().sum()
    log.info(f"  Rows with investment intensity: {n_with_inv}")
    log.info(f"  Rows with capacity intensity:   {n_with_cap}")
    if n_with_inv > 0:
        med_inv = result['subsidy_intensity_investment'].median()
        log.info(f"  Median subsidy/investment ratio: {med_inv:.3f}")

    return result


if __name__ == '__main__':
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        stream=sys.stdout,
    )

    parser = argparse.ArgumentParser(
        description='Compute subsidy intensity for vehicle+battery facilities.'
    )
    parser.add_argument(
        '--facilities',
        default=str(FACILITIES_XLSX),
        help='Path to facilities Excel file (default: facilities_develop_export.xlsx)',
    )
    parser.add_argument(
        '--consolidated',
        default=str(CONSOLIDATED_DEFAULT),
        help='Path to consolidated_matches.csv (default: data/processed/match_output/consolidated_matches.csv)',
    )
    parser.add_argument(
        '--output',
        default=str(OUTPUT_CSV),
        help='Output CSV path (default: data/processed/facility_subsidy_intensity.csv)',
    )
    args = parser.parse_args()

    run(
        facilities_path=Path(args.facilities),
        consolidated_path=Path(args.consolidated),
        output_path=Path(args.output),
    )
