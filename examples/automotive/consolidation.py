#!/usr/bin/env python3
"""
AUTOMOTIVE-SPECIFIC ANALYSIS SUPPLEMENT
=========================================
Runs nationality, sector, and narrative analysis on top of the
already-consolidated output from the generic consolidation pipeline.

The generic pipeline (src/pipeline/consolidation/) handles:
  - Loading match_log + enrichment integration (EIB, FTS-CORDIS, IPCEI)
  - Deduplication, match quality assessment
  - GGE computation
  - Parent-group rollup (using parent_groups.json)
  - Summary tables (G1-G5, S1-S7, F1-F5), concentration metrics

This module adds:
  - Nationality/origin classification (COMPANY_NATIONALITY)
  - Sector tagging (SECTOR_TAGS)
  - Nationality tables (N1-N5)
  - Sector/battery tables (B1-B7)
  - C4 nationality sunburst chart
  - Automotive narrative report

Usage:
  python -m examples.automotive.consolidation [output_dir]
"""

import pandas as pd
import numpy as np
import sys
import json
import logging
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ============================================================================
# CONFIG LOADING (from JSON files, not hardcoded)
# ============================================================================

CONFIG_DIR = Path(__file__).parent / 'config'


def _load_json(name: str) -> dict:
    path = CONFIG_DIR / name
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def _load_nationality() -> dict:
    """Load company_nationality.json and convert to tuple format.

    JSON stores {group: {origin_block, hq_country, description}}.
    Returns {group: (origin_block, hq_country, description)}.
    """
    raw = _load_json('company_nationality.json')
    return {
        group: (info['origin_block'], info['hq_country'], info['description'])
        for group, info in raw.items()
    }


COMPANY_NATIONALITY = _load_nationality()
SECTOR_TAGS = _load_json('sector_tags.json')


# ============================================================================
# NATIONALITY ASSIGNMENT
# ============================================================================

def assign_nationality(group_name: str, origin_region: str = None, hq_country: str = None) -> tuple:
    """Return (origin_block, hq_country, description) for a parent group."""
    if group_name in COMPANY_NATIONALITY:
        return COMPANY_NATIONALITY[group_name]
    if origin_region == 'chinese':
        return ('CN', 'CN', 'Chinese')
    elif origin_region == 'other_non_eu':
        return ('Other', 'Other', 'Other non-EU')
    if hq_country:
        from src.matching.consolidation import _country_to_block
        block = _country_to_block(hq_country)
        if block != 'Unknown':
            return (block, hq_country.upper(), hq_country.upper())
    return ('Other', 'Unknown', 'Unknown origin')


def _enrich_nationality_and_sector(df: pd.DataFrame) -> pd.DataFrame:
    """Add origin_block, hq_country, origin_desc, and sector_tag columns."""
    # Build per-group nationality lookup
    group_nat = {}
    for grp in df['parent_group'].unique():
        grp_rows = df[df['parent_group'] == grp]
        common_origin = None
        if 'origin_region' in grp_rows.columns and len(grp_rows['origin_region'].mode()) > 0:
            common_origin = grp_rows['origin_region'].mode().iloc[0]
        group_nat[grp] = assign_nationality(grp, common_origin)

    df['origin_block'] = df['parent_group'].map(
        lambda g: group_nat.get(g, ('Other', 'Unknown', 'Unknown origin'))[0])
    df['hq_country'] = df['parent_group'].map(
        lambda g: group_nat.get(g, ('Other', 'Unknown', 'Unknown origin'))[1])
    df['origin_desc'] = df['parent_group'].map(
        lambda g: group_nat.get(g, ('Other', 'Unknown', 'Unknown origin'))[2])
    df['sector_tag'] = df['parent_group'].map(SECTOR_TAGS).fillna('other_automotive')
    return df


# ============================================================================
# NATIONALITY TABLES (N1-N5)
# ============================================================================

def build_nationality_tables(df: pd.DataFrame, group_summary: pd.DataFrame) -> dict:
    """Build nationality/origin analysis tables."""
    tables = {}

    # Ensure group_summary has nationality columns
    if 'origin_block' not in group_summary.columns:
        gs = group_summary.merge(
            df[['parent_group', 'origin_block', 'hq_country', 'origin_desc']].drop_duplicates('parent_group'),
            on='parent_group', how='left',
        )
    else:
        gs = group_summary

    # N1: By origin block
    n1 = df.groupby('origin_block').agg(
        n_groups=('parent_group', 'nunique'),
        n_entities=('automotive_reference_name', 'nunique'),
        total_eur=('amount_eur', 'sum'),
        rows=('amount_eur', 'count'),
    ).reset_index().sort_values('total_eur', ascending=False)
    n1['pct'] = (n1['total_eur'] / n1['total_eur'].sum() * 100).round(1)
    tables['N1_by_origin_block'] = n1

    # N2: By origin x instrument
    n2 = df.groupby(['origin_block', 'financial_instrument_class']).agg(
        total_eur=('amount_eur', 'sum'),
    ).reset_index()
    n2_pivot = n2.pivot_table(
        index='origin_block', columns='financial_instrument_class',
        values='total_eur', fill_value=0)
    n2_pivot['total'] = n2_pivot.sum(axis=1)
    tables['N2_origin_x_instrument'] = n2_pivot

    # N3: By origin x source
    n3 = df.groupby(['origin_block', 'source']).agg(
        total_eur=('amount_eur', 'sum'),
    ).reset_index()
    n3_pivot = n3.pivot_table(
        index='origin_block', columns='source',
        values='total_eur', fill_value=0)
    n3_pivot['total'] = n3_pivot.sum(axis=1)
    tables['N3_origin_x_source'] = n3_pivot

    # N4: Top companies by nationality
    n4 = gs[['parent_group', 'origin_block', 'origin_desc', 'total_eur']].copy()
    n4 = n4.sort_values('total_eur', ascending=False)
    tables['N4_groups_by_nationality'] = n4

    # N5: EU vs non-EU per-company average
    eu = df[df['origin_block'] == 'EU']
    non_eu = df[df['origin_block'] != 'EU']
    avg_eu = eu.groupby('parent_group')['amount_eur'].sum().mean() if len(eu) > 0 else 0
    avg_non_eu = non_eu.groupby('parent_group')['amount_eur'].sum().mean() if len(non_eu) > 0 else 0
    tables['N5_eu_vs_non_eu_avg'] = pd.DataFrame([
        {'block': 'EU', 'avg_eur_per_group': avg_eu,
         'n_groups': eu['parent_group'].nunique()},
        {'block': 'Non-EU', 'avg_eur_per_group': avg_non_eu,
         'n_groups': non_eu['parent_group'].nunique()},
    ])

    return tables


# ============================================================================
# SECTOR / BATTERY TABLES (B1-B7)
# ============================================================================

def build_sector_tables(df: pd.DataFrame, group_summary: pd.DataFrame) -> dict:
    """Build sector-level analytical tables (battery vs OEM vs supplier etc.)."""
    tables = {}

    # B1: Sector overview
    tables['B1_sector_overview'] = df.groupby('sector_tag').agg(
        total_eur=('amount_eur', 'sum'),
        rows=('amount_eur', 'count'),
        n_entities=('automotive_reference_name', 'nunique'),
        n_groups=('parent_group', 'nunique'),
    ).reset_index().sort_values('total_eur', ascending=False)

    # B2-B5: Battery value chain detail
    batt_mask = df['sector_tag'].isin(['battery', 'battery_materials'])
    if batt_mask.sum() > 0:
        batt = df[batt_mask]

        tables['B2_battery_group_ranking'] = batt.groupby('parent_group').agg(
            sector_tag=('sector_tag', 'first'),
            total_eur=('amount_eur', 'sum'),
            rows=('amount_eur', 'count'),
            n_sources=('source', 'nunique'),
            origin_block=('origin_block', 'first'),
        ).reset_index().sort_values('total_eur', ascending=False)

        tables['B3_battery_x_source'] = batt.groupby(['parent_group', 'source']).agg(
            total_eur=('amount_eur', 'sum'),
        ).reset_index().sort_values('total_eur', ascending=False)

        tables['B4_battery_x_instrument'] = batt.groupby(
            ['parent_group', 'financial_instrument_class']
        ).agg(total_eur=('amount_eur', 'sum')).reset_index().sort_values('total_eur', ascending=False)

        batt_yr = batt[batt['year'].notna()]
        if len(batt_yr) > 0:
            tables['B5_battery_annual'] = batt_yr.groupby('year').agg(
                total_eur=('amount_eur', 'sum'),
                n_groups=('parent_group', 'nunique'),
            ).reset_index()

    # B6: Sector x instrument cross-tab
    tables['B6_sector_x_instrument'] = df.groupby(
        ['sector_tag', 'financial_instrument_class']
    ).agg(total_eur=('amount_eur', 'sum')).reset_index()

    # B7: Sector x origin
    tables['B7_sector_x_origin'] = df.groupby(['sector_tag', 'origin_block']).agg(
        total_eur=('amount_eur', 'sum'),
        n_groups=('parent_group', 'nunique'),
    ).reset_index()

    return tables


# ============================================================================
# NATIONALITY SUNBURST CHART (C4)
# ============================================================================

def _try_png(fig, path):
    """Try to write PNG, skip if kaleido not installed."""
    try:
        fig.write_image(path)
    except Exception:
        pass


def generate_nationality_chart(group_summary: pd.DataFrame, vis_dir: Path) -> list:
    """Generate C4 nationality sunburst chart."""
    try:
        import plotly.express as px
    except ImportError:
        log.warning("plotly not installed -- skipping nationality chart")
        return []

    vis_dir.mkdir(parents=True, exist_ok=True)
    charts = []

    nat_data = group_summary[group_summary['total_eur'] > 0].copy()
    if len(nat_data) == 0 or 'origin_block' not in nat_data.columns:
        log.warning("No nationality data available for sunburst")
        return []

    fig = px.sunburst(
        nat_data, path=['origin_block', 'parent_group'],
        values='total_eur',
        title='EU Automotive Support by Nationality & Group',
        color='origin_block',
        color_discrete_map={
            'EU': '#1f77b4', 'JP': '#ff7f0e', 'KR': '#2ca02c',
            'US': '#d62728', 'CN': '#9467bd', 'CN-owned': '#8c564b',
            'Other': '#7f7f7f',
        },
    )
    fig.update_layout(height=600, width=700)
    fig.write_html(vis_dir / 'C4_nationality_sunburst.html')
    _try_png(fig, vis_dir / 'C4_nationality_sunburst.png')
    charts.append('C4_nationality_sunburst')

    log.info(f"  Generated {len(charts)} automotive chart(s): {', '.join(charts)}")
    return charts


# ============================================================================
# AUTOMOTIVE NARRATIVE REPORT
# ============================================================================

def generate_narrative(df: pd.DataFrame, group_summary: pd.DataFrame,
                       tables: dict) -> str:
    """Generate automotive-specific analytical narrative report."""
    total_eur = df['amount_eur'].sum()
    n_entities = df['automotive_reference_name'].nunique()
    n_groups = df['parent_group'].nunique()
    n_sources = df['source'].nunique()

    grants = df[df['financial_instrument_class'] == 'grant']['amount_eur'].sum()
    loans = df[df['financial_instrument_class'] == 'loan']['amount_eur'].sum()
    other = total_eur - grants - loans

    eu_eur = df[df['origin_block'] == 'EU']['amount_eur'].sum()
    non_eu_eur = df[df['origin_block'] != 'EU']['amount_eur'].sum()

    # GGE total if available
    gge_str = ''
    if 'amount_gge' in df.columns:
        total_gge = df['amount_gge'].sum()
        gge_str = f"\n- **EUR {total_gge/1e9:.1f}B in Grant-equivalent (GGE)** (Scoreboard methodology)"

    # Time periods
    df_yr = df[df['year'].notna()].copy()
    pre = df_yr[df_yr['year'] < 2020]['amount_eur'].sum()
    post = df_yr[df_yr['year'] >= 2020]['amount_eur'].sum()

    narrative = f"""# EU Automotive Industrial Support: A Comprehensive Financial Anatomy

## Executive Summary

The European Union's automotive sector has received **EUR {total_eur/1e9:.1f} billion** in identifiable
public financial support across {n_entities} entities ({n_groups} corporate groups) from
{n_sources} data sources.

This figure comprises:
- **EUR {grants/1e9:.1f}B in confirmed grants** (state aid, EU budget, cohesion funds)
- **EUR {loans/1e9:.1f}B in concessional loans** (EIB, EBRD)
- **EUR {other/1e9:.1f}B in other instruments** (equity, guarantees, planned allocations){gge_str}

---

## 1. Who Receives: Group-Level Concentration

| Rank | Group | EUR (B) | Sources | Nationality |
|------|-------|---------|---------|-------------|
"""
    for i, (_, row) in enumerate(group_summary.head(15).iterrows()):
        desc = row.get('origin_desc', '')
        n_src = row.get('n_sources', '')
        narrative += f"| {i+1} | {row['parent_group']} | {row['total_eur']/1e9:.1f} | {n_src} | {desc} |\n"

    narrative += f"""
---

## 2. Nationality & Origin Analysis

EU-headquartered companies receive **{eu_eur/total_eur*100:.0f}%** of total support (EUR {eu_eur/1e9:.1f}B),
while non-EU companies receive **{non_eu_eur/total_eur*100:.0f}%** (EUR {non_eu_eur/1e9:.1f}B).

"""
    if 'N1_by_origin_block' in tables:
        for _, row in tables['N1_by_origin_block'].iterrows():
            narrative += (f"- **{row['origin_block']}**: EUR {row['total_eur']/1e9:.1f}B "
                         f"({row['pct']:.0f}%, {row['n_groups']:.0f} groups)\n")

    narrative += f"""
Non-EU companies (Toyota, Nissan, Ford, Hyundai-Kia) receive EU support primarily through
manufacturing investments in EU member states, consistent with EU industrial policy goals
of maintaining production and employment in Europe.

---

## 3. Sector Breakdown

"""
    if 'B1_sector_overview' in tables:
        for _, row in tables['B1_sector_overview'].iterrows():
            pct = row['total_eur'] / total_eur * 100
            narrative += (f"- **{row['sector_tag']}**: EUR {row['total_eur']/1e9:.1f}B "
                         f"({pct:.0f}%, {row['n_groups']:.0f} groups)\n")

    # Battery section
    if 'B2_battery_group_ranking' in tables:
        batt_total = tables['B2_battery_group_ranking']['total_eur'].sum()
        narrative += f"""
### Battery Value Chain

The battery/materials segment received **EUR {batt_total/1e9:.1f}B**, reflecting the EU's push
to establish sovereign battery manufacturing capacity via IPCEI state aid and Innovation Fund grants.

"""

    narrative += f"""
---

## 4. Structural Shift: Pre vs Post 2020

- **Pre-2020** (traditional automotive): EUR {pre/1e9:.1f}B
- **Post-2020** (green transition): EUR {post/1e9:.1f}B

"""
    if post > 0 and pre > 0:
        narrative += (f"The post-2020 period shows a **{post/pre:.1f}x increase** in support intensity, "
                     f"driven by the European Green Deal, Fit for 55, and post-COVID recovery instruments.\n")

    narrative += f"""
---

## 5. Methodological Notes

1. **No double-counting**: TAM, FTS, Kohesio, EIB/EBRD, RRF, CINEA, IPCEI, and FTS-CORDIS
   are distinct instruments with different fiscal sources.

2. **Loan vs grant**: EIB loans are not grants. The subsidy element is the interest
   rate differential (typically 50-150 bps below market). For EUR {loans/1e9:.1f}B in loans,
   the estimated subsidy equivalent is EUR {loans/1e9 * 0.02:.1f}-{loans/1e9 * 0.05:.1f}B.

3. **Entity matching**: Multi-layer matching pipeline with exact, fuzzy, and contextual
   methods, followed by group-level consolidation.

---

*Generated: {time.strftime('%Y-%m-%d %H:%M')} | Data sources: TAM, FTS, Kohesio, EIB, EBRD, RRF,
CINEA Innovation Fund, EU Scoreboard, CORDIS, EU ETS (EUTL), IPCEI reference*
"""
    return narrative


# ============================================================================
# MAIN ENTRY POINT
# ============================================================================

def run_automotive_extras(consolidated_dir: Path):
    """Run automotive-specific analysis on already-consolidated data.

    Reads consolidated_matches.csv and group_summary.csv from consolidated_dir,
    adds nationality and sector columns, builds sector/nationality tables,
    generates narrative, saves automotive-specific outputs.
    """
    t0 = time.time()
    consolidated_dir = Path(consolidated_dir)
    auto_dir = consolidated_dir / 'automotive'
    auto_dir.mkdir(parents=True, exist_ok=True)
    vis_dir = auto_dir / 'charts'
    vis_dir.mkdir(parents=True, exist_ok=True)

    log.info("=" * 70)
    log.info("AUTOMOTIVE-SPECIFIC ANALYSIS SUPPLEMENT")
    log.info("=" * 70)

    # ------------------------------------------------------------------
    # Load consolidated data from generic pipeline
    # ------------------------------------------------------------------
    matches_path = consolidated_dir / 'consolidated_matches.csv'
    groups_path = consolidated_dir / 'group_summary.csv'

    if not matches_path.exists():
        log.error(f"consolidated_matches.csv not found at {matches_path}")
        raise FileNotFoundError(f"Run the generic consolidation pipeline first: {matches_path}")

    log.info(f"Loading consolidated data from {consolidated_dir}")
    df = pd.read_csv(matches_path, low_memory=False)
    log.info(f"  Loaded {len(df):,} rows, EUR {df['amount_eur'].sum()/1e9:.1f}B")

    # Ensure required columns exist
    ref_col = 'automotive_reference_name'
    if ref_col not in df.columns:
        # Fall back to generic column name
        for fallback in ['reference_name', 'matched_name']:
            if fallback in df.columns:
                df[ref_col] = df[fallback]
                break
        else:
            log.error(f"No reference name column found in consolidated data")
            raise KeyError(f"Expected '{ref_col}' column in {matches_path}")

    if 'parent_group' not in df.columns:
        log.error("parent_group column missing -- generic pipeline should set this")
        raise KeyError("Expected 'parent_group' column in consolidated data")

    # Load group summary (from generic pipeline)
    if groups_path.exists():
        group_summary = pd.read_csv(groups_path, low_memory=False)
        log.info(f"  Loaded group_summary: {len(group_summary)} groups")
    else:
        log.warning("group_summary.csv not found, building from consolidated data")
        group_summary = df.groupby('parent_group').agg(
            n_entities=(ref_col, 'nunique'),
            n_rows=('amount_eur', 'count'),
            total_eur=('amount_eur', 'sum'),
            n_countries=('country', 'nunique'),
            n_sources=('source', 'nunique'),
        ).reset_index().sort_values('total_eur', ascending=False)

    # ------------------------------------------------------------------
    # PHASE 1: Enrich with nationality and sector tags
    # ------------------------------------------------------------------
    log.info("\nAdding nationality and sector classifications...")
    df = _enrich_nationality_and_sector(df)

    # Enrich group_summary too
    nat_lookup = {
        grp: assign_nationality(grp)
        for grp in group_summary['parent_group'].unique()
    }
    group_summary['origin_block'] = group_summary['parent_group'].map(
        lambda g: nat_lookup.get(g, ('EU', 'Unknown', 'Unknown'))[0])
    group_summary['hq_country'] = group_summary['parent_group'].map(
        lambda g: nat_lookup.get(g, ('EU', 'Unknown', 'Unknown'))[1])
    group_summary['origin_desc'] = group_summary['parent_group'].map(
        lambda g: nat_lookup.get(g, ('EU', 'Unknown', 'Unknown'))[2])
    group_summary['sector_tag'] = group_summary['parent_group'].map(
        SECTOR_TAGS).fillna('other_automotive')

    # Log sector summary
    sector_counts = df.groupby('sector_tag').agg(
        groups=('parent_group', 'nunique'),
        eur=('amount_eur', 'sum'),
    )
    for tag, row in sector_counts.iterrows():
        log.info(f"    {tag:25s}: {row['groups']:3.0f} groups, EUR {row['eur']/1e9:.1f}B")

    # ------------------------------------------------------------------
    # PHASE 2: Nationality tables (N1-N5)
    # ------------------------------------------------------------------
    log.info("\nBuilding nationality analysis tables...")
    nat_tables = build_nationality_tables(df, group_summary)

    # ------------------------------------------------------------------
    # PHASE 3: Sector / battery tables (B1-B7)
    # ------------------------------------------------------------------
    log.info("Building sector & battery value chain tables...")
    sector_tables = build_sector_tables(df, group_summary)

    all_tables = {**nat_tables, **sector_tables}

    # Save all automotive-specific tables
    for name, tbl in all_tables.items():
        path = auto_dir / f'{name}.csv'
        if isinstance(tbl, pd.DataFrame):
            tbl.to_csv(path)
            log.info(f"  {name}.csv ({len(tbl)} rows)")

    # ------------------------------------------------------------------
    # PHASE 4: Nationality sunburst chart
    # ------------------------------------------------------------------
    log.info("\nGenerating automotive charts...")
    charts = generate_nationality_chart(group_summary, vis_dir)

    # ------------------------------------------------------------------
    # PHASE 5: Automotive narrative
    # ------------------------------------------------------------------
    log.info("\nGenerating automotive narrative...")
    narrative = generate_narrative(df, group_summary, all_tables)
    narrative_path = auto_dir / 'automotive_narrative.md'
    with open(narrative_path, 'w', encoding='utf-8') as f:
        f.write(narrative)
    log.info(f"  Saved automotive_narrative.md ({len(narrative):,} chars)")

    # ------------------------------------------------------------------
    # Save enriched data with nationality/sector columns
    # ------------------------------------------------------------------
    enriched_path = auto_dir / 'consolidated_with_nationality.csv'
    df.to_csv(enriched_path, index=False)
    log.info(f"  Saved consolidated_with_nationality.csv ({len(df):,} rows)")

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    total = df['amount_eur'].sum()
    eu_eur = df[df['origin_block'] == 'EU']['amount_eur'].sum()

    log.info("\n" + "=" * 70)
    log.info("AUTOMOTIVE EXTRAS SUMMARY")
    log.info("=" * 70)
    log.info(f"  Total: EUR {total/1e9:.1f}B across {df['parent_group'].nunique()} groups")
    log.info(f"  EU-headquartered: EUR {eu_eur/1e9:.1f}B ({eu_eur/total*100:.0f}%)")
    log.info(f"  Tables: {len(all_tables)} (N1-N5, B1-B7)")
    log.info(f"  Charts: {len(charts)}")
    log.info(f"  Output dir: {auto_dir}")

    elapsed = time.time() - t0
    log.info(f"  Runtime: {elapsed:.1f}s")

    return {
        'tables': all_tables,
        'charts': charts,
        'narrative_path': narrative_path,
        'enriched_path': enriched_path,
        'auto_dir': auto_dir,
    }


def main():
    """Standalone entry point. Reads consolidated_dir from argv or uses default."""
    if len(sys.argv) > 1:
        consolidated_dir = Path(sys.argv[1])
    else:
        # Default: look for the standard pipeline output location
        try:
            from src.paths import MATCH_OUTPUT_DIR
            consolidated_dir = MATCH_OUTPUT_DIR / 'automotive' / 'consolidated'
        except ImportError:
            consolidated_dir = Path('output') / 'matching' / 'automotive' / 'consolidated'

    run_automotive_extras(consolidated_dir)


if __name__ == '__main__':
    main()
