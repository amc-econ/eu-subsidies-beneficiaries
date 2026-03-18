#!/usr/bin/env python3
"""
Generic Summary Charts
========================
Generates publication-grade matplotlib charts from consolidated match results.
Works for any company list — uses consolidated_matches.csv as input.

Charts produced:
  01_annual_total          — Annual support totals (line + fill, integer x-axis)
  P02_mff_source_stacked   — MFF period × data source (stacked bar)
  P05_country_instrument   — Top 15 granting countries × instrument (stacked bar)
  P06c_top20_gge_core_auto — Top 20 groups by GGE (optional peripheral exclusion)
  P15c_top20_aggregated    — Top 20 groups by total face value × source
  S04_foreign_top15        — Top 15 non-EU groups by GGE (skipped if no origin_block)

Usage:
  from src.visualisations.summary_charts import generate_summary_charts
  generate_summary_charts(consolidated_csv, output_dir)
"""

import pandas as pd
import numpy as np
import logging
import sys
import re
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# MFF periods
# ---------------------------------------------------------------------------
MFF_PERIODS = {
    'Pre-2007': (0, 2006),
    '2007\u20132013': (2007, 2013),
    '2014\u20132020': (2014, 2020),
    '2021\u20132027': (2021, 2027),
}


def _assign_mff(year):
    if pd.isna(year):
        return 'Unknown'
    y = int(year)
    for name, (lo, hi) in MFF_PERIODS.items():
        if lo <= y <= hi:
            return name
    return 'Unknown'


# ---------------------------------------------------------------------------
# GGE rates (EU State Aid Scoreboard methodology)
# ---------------------------------------------------------------------------
GGE_RATES = {
    'grant': 1.00, 'subsidy': 1.00, 'procurement': 1.00,
    'equity': 1.00, 'debt_relief': 1.00, 'other': 1.00,
    'mixed': 0.50, 'loan': 0.15, 'guarantee': 0.10, 'tax_advantage': 0.15,
}
REPAYABLE_ADVANCE_RATE = 0.90


def _gge_rate(row):
    inst = str(row.get('financial_instrument_class', '')).lower().strip()
    subtype = str(row.get('instrument_subtype', '')).lower()
    if inst == 'loan' and 'repayable' in subtype:
        return REPAYABLE_ADVANCE_RATE
    return GGE_RATES.get(inst, 1.0)


# ---------------------------------------------------------------------------
# Colour palettes
# ---------------------------------------------------------------------------
SOURCE_COLORS = {
    'TAM': '#1565C0',
    'EIB': '#E65100',
    'KOHESIO': '#2E7D32',
    'EBRD': '#6A1B9A',
    'CINEA': '#C62828',
    'FTS': '#00695C',
    'RRF': '#AD1457',
    'INNOVFUND': '#9E9D24',
    'FTS_CORDIS': '#00838F',
    'IPCEI_state_aid': '#4E342E',
    'RESEARCH': '#546E7A',
    'ESIF': '#37474F',
}

INSTRUMENT_COLORS = {
    'Grant': '#2196F3',
    'Loan': '#FF9800',
    'Guarantee': '#9C27B0',
    'Equity': '#4CAF50',
    'Tax advantage': '#F44336',
    'Procurement': '#795548',
    'Other': '#607D8B',
}

ORIGIN_COLORS = {
    'EU': '#1565C0',
    'CN': '#C62828',
    'CN-owned': '#E65100',
    'US': '#2E7D32',
    'JP': '#6A1B9A',
    'KR': '#00695C',
    'Other': '#78909C',
}


def fmt_bn(x, _):  return f'\u20ac{x/1e9:.0f}B'
def fmt_bn1(x, _): return f'\u20ac{x/1e9:.1f}B'


def _lbl(label: str) -> str:
    """Return 'label ' (trailing space) if label non-empty, else ''."""
    return f'{label} ' if label else ''


# ---------------------------------------------------------------------------
# Matplotlib setup — academic economics style
# ---------------------------------------------------------------------------
def _setup_matplotlib():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    from matplotlib.patches import Patch

    plt.rcParams.update({
        'figure.figsize': (12, 7),
        'figure.dpi': 200,
        'font.size': 11,
        'font.family': 'sans-serif',
        'axes.titlesize': 14,
        'axes.titleweight': 'bold',
        'axes.labelsize': 12,
        'legend.fontsize': 10,
        'figure.facecolor': 'white',
        'axes.facecolor': 'white',
        'axes.grid': True,
        'grid.alpha': 0.25,
        'grid.linewidth': 0.5,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.linewidth': 0.8,
    })
    return plt, mticker, Patch


def _save_png(fig, name, output_dir, plt_mod):
    path = Path(output_dir) / f'{name}.png'
    try:
        fig.savefig(path, dpi=200, bbox_inches='tight', facecolor='white', edgecolor='none')
    except Exception:
        fig.savefig(path, dpi=200, bbox_inches='tight')
    plt_mod.close(fig)
    log.info(f'    saved {name}.png')


# ---------------------------------------------------------------------------
# Chart functions
# ---------------------------------------------------------------------------

def _chart_01_annual_total(df, output_dir, plt, mticker, label):
    """Annual EU support totals — line + fill, integer x-axis."""
    yr = df[df['year'].notna()].copy()
    yr['year'] = yr['year'].astype(int)
    yearly = yr.groupby('year')['amount_eur'].sum().sort_index()
    if yearly.empty:
        log.warning('  01_annual_total: no year data, skipping')
        return

    min_y, max_y = int(yearly.index.min()), int(yearly.index.max())

    fig, ax = plt.subplots(figsize=(13, 6))
    ax.plot(yearly.index, yearly.values, 'o-', color='#1565C0', linewidth=2, markersize=5)
    ax.fill_between(yearly.index, yearly.values, alpha=0.15, color='#1565C0')

    ax.set_title(f'EU {_lbl(label)}Support: Annual Totals', pad=15)
    ax.set_xlabel('Year', fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    # Integer year ticks — no fractional years
    ax.xaxis.set_major_locator(mticker.MaxNLocator(integer=True))
    ax.set_xlim(min_y - 0.5, max_y + 0.5)

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, '01_annual_total', output_dir, plt)


def _chart_p02_mff_source_stacked(df, output_dir, plt, mticker, Patch, label):
    """MFF period × data source stacked bars."""
    mff_order = [p for p in MFF_PERIODS if p in df['mff_period'].unique()]
    if not mff_order:
        log.warning('  P02: no mff_period data, skipping')
        return

    mff_src = df.groupby(['mff_period', 'source'])['amount_eur'].sum().unstack(fill_value=0)
    mff_src = mff_src.reindex(mff_order)

    src_order = ['EIB', 'TAM', 'IPCEI_state_aid', 'FTS_CORDIS', 'KOHESIO',
                 'EBRD', 'CINEA', 'FTS', 'RRF', 'INNOVFUND', 'RESEARCH', 'ESIF']
    cols = [c for c in src_order if c in mff_src.columns]
    cols += [c for c in mff_src.columns if c not in cols]

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(mff_order))
    width = 0.55
    bottom = np.zeros(len(mff_order))
    total_eur = df['amount_eur'].sum()

    for src in cols:
        vals = mff_src[src].values
        ax.bar(x, vals, width, bottom=bottom, label=src,
               color=SOURCE_COLORS.get(src, '#90A4AE'), alpha=0.88,
               edgecolor='white', linewidth=0.8)
        for i, v in enumerate(vals):
            if total_eur > 0 and v / total_eur > 0.03:
                ax.text(x[i], bottom[i] + v / 2, f'\u20ac{v/1e9:.1f}B',
                        ha='center', va='center', fontsize=8, color='white', fontweight='bold')
        bottom += vals

    for i, p in enumerate(mff_order):
        total = mff_src.loc[p].sum()
        ax.text(x[i], bottom[i] + total_eur * 0.008,
                f'\u20ac{total/1e9:.1f}B', ha='center', va='bottom', fontsize=11, fontweight='bold')

    ax.set_xticks(x)
    ax.set_xticklabels(mff_order, fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title(f'EU {_lbl(label)}Support by MFF Period & Data Source', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc', ncol=2, fontsize=9)
    ax.set_xlim(-0.5, len(mff_order) - 0.5)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P02_mff_source_stacked', output_dir, plt)


def _chart_p05_country_instrument(df, output_dir, plt, mticker, Patch, label):
    """Top 15 granting countries × instrument (stacked horizontal bar)."""
    if 'country' not in df.columns:
        log.warning('  P05: no country column, skipping')
        return

    c_rank = df.groupby('country')['amount_eur'].sum().sort_values(ascending=False).head(15)
    if c_rank.empty:
        return
    countries = c_rank.index.tolist()

    c_inst = (df[df['country'].isin(countries)]
              .groupby(['country', 'instrument_simple'])['amount_eur']
              .sum().unstack(fill_value=0))
    c_inst = c_inst.reindex(countries)

    fig, ax = plt.subplots(figsize=(12, 9))
    y = np.arange(len(countries))
    left = np.zeros(len(countries))

    for inst in ['Grant', 'Loan', 'Guarantee', 'Tax advantage', 'Equity', 'Other']:
        if inst in c_inst.columns:
            vals = c_inst[inst].values
            ax.barh(y, vals, 0.6, left=left, label=inst,
                    color=INSTRUMENT_COLORS.get(inst, '#999'), alpha=0.88,
                    edgecolor='white', linewidth=0.5)
            left += vals

    ax.set_yticks(y)
    ax.set_yticklabels(countries, fontsize=11)
    ax.invert_yaxis()
    ax.set_xlabel('EUR', fontsize=12)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title(f'Top 15 Countries: {_lbl(label)}Support by Instrument', pad=15)
    ax.legend(loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=9)

    for i, total in enumerate(c_rank.values):
        ax.text(total + c_rank.max() * 0.01, i,
                f'\u20ac{total/1e9:.1f}B', va='center', fontsize=9, fontweight='bold')

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P05_country_instrument', output_dir, plt)


def _chart_p06c_top20_gge(df, output_dir, plt, mticker, Patch, label, peripheral_groups):
    """Top 20 groups by GGE — dual bars (face value + GGE)."""
    if 'parent_group' not in df.columns:
        log.warning('  P06c: no parent_group column, skipping')
        return

    core = df[~df['parent_group'].isin(peripheral_groups)].copy() if peripheral_groups else df.copy()
    grp = (core.groupby('parent_group')
               .agg(face=('amount_eur', 'sum'), gge=('gge_eur', 'sum'))
               .sort_values('gge', ascending=False).head(20))
    if grp.empty:
        return
    grp = grp.sort_values('gge', ascending=True)

    fig, ax = plt.subplots(figsize=(13, 9))
    y = np.arange(len(grp))
    ax.barh(y - 0.18, grp['face'].values, height=0.35,
            color='#1565C0', alpha=0.6, label='Face value')
    ax.barh(y + 0.18, grp['gge'].values, height=0.35,
            color='#C62828', alpha=0.8, label='GGE (subsidy value)')

    ax.set_yticks(y)
    ax.set_yticklabels(grp.index, fontsize=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))

    for i, (_, row) in enumerate(grp.iterrows()):
        ax.text(row['gge'] + grp['face'].max() * 0.01, i + 0.18,
                f'\u20ac{row["gge"]/1e9:.2f}B', va='center', fontsize=8, color='#C62828')

    total_gge = core['gge_eur'].sum()
    total_face = core['amount_eur'].sum()
    excl_note = '  —  excl. peripheral groups' if peripheral_groups else ''
    ax.set_title(
        f'Top 20 {_lbl(label)}Groups by Subsidy Value (GGE){excl_note}\n'
        f'\u20ac{total_gge/1e9:.1f}B GGE / \u20ac{total_face/1e9:.1f}B face value',
        fontsize=13, fontweight='bold', pad=15,
    )
    ax.legend(loc='lower right', fontsize=10, framealpha=0.95, edgecolor='#ccc')
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database, GGE per EU State Aid Scoreboard (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P06c_top20_gge_core_auto', output_dir, plt)


def _chart_p15c_top20_aggregated(df, output_dir, plt, mticker, Patch, label, peripheral_groups):
    """Top 20 groups by total face value — stacked by data source."""
    if 'parent_group' not in df.columns:
        log.warning('  P15c: no parent_group column, skipping')
        return

    core = df[~df['parent_group'].isin(peripheral_groups)].copy() if peripheral_groups else df.copy()
    grp = core.groupby(['parent_group', 'source'])['amount_eur'].sum().unstack(fill_value=0)
    grp['total'] = grp.sum(axis=1)
    grp = grp.sort_values('total', ascending=False).head(20)
    grp = grp.sort_values('total', ascending=True)

    src_order = ['EIB', 'TAM', 'IPCEI_state_aid', 'FTS_CORDIS', 'KOHESIO',
                 'EBRD', 'CINEA', 'FTS', 'RRF', 'INNOVFUND', 'RESEARCH', 'ESIF']
    cols = [c for c in src_order if c in grp.columns]
    cols += [c for c in grp.columns if c not in cols and c != 'total']

    fig, ax = plt.subplots(figsize=(13, 9))
    y = np.arange(len(grp))
    left = np.zeros(len(grp))

    for src in cols:
        vals = grp[src].values
        ax.barh(y, vals, left=left, height=0.6, label=src,
                color=SOURCE_COLORS.get(src, '#90A4AE'), alpha=0.85,
                edgecolor='white', linewidth=0.5)
        left += vals

    ax.set_yticks(y)
    ax.set_yticklabels(grp.index, fontsize=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))

    for i, (g, row) in enumerate(grp.iterrows()):
        v = row['total']
        lbl = f'\u20ac{v/1e9:.2f}B' if v >= 1e9 else f'\u20ac{v/1e6:.0f}M'
        ax.text(v + grp['total'].max() * 0.01, i, lbl, va='center', fontsize=9, color='#333')

    row_counts = core.groupby('parent_group').size()
    for i, g in enumerate(grp.index):
        cnt = row_counts.get(g, 0)
        ax.text(-grp['total'].max() * 0.01, i, f'({cnt})', va='center', ha='right',
                fontsize=7.5, color='#999')

    total = core['amount_eur'].sum()
    excl_note = '  —  excl. peripheral groups' if peripheral_groups else ''
    ax.set_title(
        f'Top 20 {_lbl(label)}Groups — Total Support by Source{excl_note}'
        f'  —  \u20ac{total/1e9:.1f}B total',
        fontsize=13, fontweight='bold', pad=15,
    )
    ax.legend(loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=9,
              ncol=2, title='Data Source', title_fontsize=10)
    plt.tight_layout()
    _save_png(fig, 'P15c_top20_aggregated_core', output_dir, plt)


def _chart_s04_foreign_top15(df, output_dir, plt, mticker, Patch, label):
    """Top 15 non-EU corporate groups by GGE. Skipped if origin_block absent."""
    if 'origin_block' not in df.columns:
        log.info('  S04_foreign_top15: skipped (no origin_block — add hq_country to company list to enable)')
        return

    foreign = df[df['origin_block'].notna() & ~df['origin_block'].isin(['EU', 'Unknown', ''])].copy()
    if foreign.empty or foreign['parent_group'].nunique() < 1:
        log.info('  S04_foreign_top15: skipped (no non-EU groups found)')
        return

    has_origin_desc = 'origin_desc' in foreign.columns

    agg_cols = {'gge_eur': ('gge_eur', 'sum'), 'face_eur': ('amount_eur', 'sum')}
    foreign_groups = foreign.groupby(['parent_group', 'origin_block']).agg(**agg_cols)
    if has_origin_desc:
        foreign_groups['origin_desc'] = foreign.groupby(
            ['parent_group', 'origin_block'])['origin_desc'].first()
    foreign_groups = foreign_groups.sort_values('gge_eur', ascending=False).reset_index()

    top15 = foreign_groups.head(15).copy()
    top15 = top15.sort_values('gge_eur', ascending=True)

    fig, ax = plt.subplots(figsize=(13, 8))
    y = np.arange(len(top15))

    # CN-owned bars share the CN color
    colors = [ORIGIN_COLORS.get('CN' if o == 'CN-owned' else o, '#78909C')
              for o in top15['origin_block'].values]
    ax.barh(y, top15['gge_eur'].values, height=0.55, color=colors,
            alpha=0.85, edgecolor='white', linewidth=0.8)

    # Y-axis labels: group name + origin description in parentheses
    labels = []
    for _, row in top15.iterrows():
        name = str(row['parent_group'])
        if has_origin_desc:
            desc = str(row.get('origin_desc', row['origin_block']))
            desc = desc.replace('Chinese-owned', 'Chinese').replace('Other non-EU', 'Other')
            if '(' in name:
                inner = name[name.index('(') + 1:name.rindex(')')]
                if inner.lower() in desc.lower():
                    name = name[:name.index('(')].strip()
                    desc = re.sub(r'\s*\(' + re.escape(inner) + r'\)', '', desc,
                                  flags=re.IGNORECASE).strip()
        else:
            desc = str(row['origin_block'])
        labels.append(f'{name}  ({desc})')

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9.5)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))

    for i, (_, row) in enumerate(top15.iterrows()):
        v = row['gge_eur']
        val_lbl = f'\u20ac{v/1e9:.2f}B' if v >= 1e9 else f'\u20ac{v/1e6:.0f}M'
        ax.text(v + top15['gge_eur'].max() * 0.01, i, val_lbl,
                va='center', fontsize=8.5, fontweight='bold')

    ax.set_title(f'Top 15 Non-EU {_lbl(label)}Groups by Subsidy Value (GGE)',
                 fontsize=13, fontweight='bold', pad=15)

    # Legend: merge CN + CN-owned into "Chinese"
    seen_o = set()
    legend_items = []
    for o in top15['origin_block'].values:
        legend_key = 'Chinese' if o in ('CN', 'CN-owned') else o
        color_key = 'CN' if o in ('CN', 'CN-owned') else o
        if legend_key not in seen_o:
            seen_o.add(legend_key)
            legend_items.append(
                Patch(facecolor=ORIGIN_COLORS.get(color_key, '#999'), label=legend_key)
            )
    ax.legend(handles=legend_items, loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=9)

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'S04_foreign_top15', output_dir, plt)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_summary_charts(
    consolidated_csv: Path,
    output_dir: Path,
    prefix: str = 'match',
    label: str = '',
    peripheral_groups: frozenset = frozenset(),
) -> list:
    """Generate publication-quality summary charts from consolidated match results.

    Parameters
    ----------
    consolidated_csv : Path
        Path to consolidated_matches.csv.
    output_dir : Path
        Directory to save PNG charts.
    prefix : str
        Column prefix for reference_name column (e.g. 'automotive', 'match').
    label : str
        Sector label inserted into chart titles, e.g. 'Automotive'.
        Leave empty for generic (sector-agnostic) titles.
    peripheral_groups : frozenset
        Group names to exclude from P06c/P15c charts (e.g. semiconductors,
        tires for automotive). Empty = include all groups.

    Returns
    -------
    list[str]
        Names of charts successfully generated.
    """
    try:
        plt, mticker, Patch = _setup_matplotlib()
    except ImportError as exc:
        log.warning(f'matplotlib not available — skipping chart generation: {exc}')
        return []

    consolidated_csv = Path(consolidated_csv)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not consolidated_csv.exists():
        log.error(f'Consolidated CSV not found: {consolidated_csv}')
        return []

    with open(consolidated_csv, 'r', encoding='utf-8', errors='replace') as _f:
        _sample = _f.read(4096)
    _sep = ';' if _sample.count(';') > _sample.count(',') else ','
    df = pd.read_csv(consolidated_csv, sep=_sep, low_memory=False)
    log.info(f'Generating summary charts from {len(df):,} rows...')

    # Coerce numeric columns (guards against object dtype from semicolon CSVs)
    for _col in ('amount_eur', 'amount_gge', 'gge_rate'):
        if _col in df.columns:
            df[_col] = pd.to_numeric(df[_col], errors='coerce')

    # ---- Derived columns ----
    if 'year' in df.columns:
        df['mff_period'] = df['year'].apply(_assign_mff)
    else:
        df['mff_period'] = 'Unknown'

    inst_map = {
        'grant': 'Grant', 'loan': 'Loan', 'guarantee': 'Guarantee',
        'equity': 'Equity', 'procurement': 'Procurement',
        'tax_advantage': 'Tax advantage', 'subsidy': 'Grant',
        'other': 'Other', 'mixed': 'Other',
    }
    if 'financial_instrument_class' in df.columns:
        df['instrument_simple'] = df['financial_instrument_class'].map(inst_map).fillna('Other')
    else:
        df['instrument_simple'] = 'Other'

    # GGE: prefer pre-computed column, else compute from instrument class
    if 'amount_gge' in df.columns:
        df['gge_eur'] = df['amount_gge']
    elif 'financial_instrument_class' in df.columns:
        df['gge_eur'] = df.apply(_gge_rate, axis=1) * df['amount_eur']
    else:
        df['gge_eur'] = df['amount_eur']
        log.warning('  No GGE column or instrument class found — using face value as GGE proxy')

    # Determine group column
    ref_col = f'{prefix}_reference_name'
    if ref_col not in df.columns:
        for candidate in ['automotive_reference_name', 'match_reference_name', 'reference_name']:
            if candidate in df.columns:
                ref_col = candidate
                break
    if 'parent_group' not in df.columns and ref_col in df.columns:
        df['parent_group'] = df[ref_col]

    log.info(f'  Total: \u20ac{df["amount_eur"].sum()/1e9:.1f}B face / \u20ac{df["gge_eur"].sum()/1e9:.1f}B GGE')

    charts = []
    chart_fns = [
        ('01_annual_total',
         lambda: _chart_01_annual_total(df, output_dir, plt, mticker, label)),
        ('P02_mff_source_stacked',
         lambda: _chart_p02_mff_source_stacked(df, output_dir, plt, mticker, Patch, label)),
        ('P05_country_instrument',
         lambda: _chart_p05_country_instrument(df, output_dir, plt, mticker, Patch, label)),
        ('P06c_top20_gge_core_auto',
         lambda: _chart_p06c_top20_gge(df, output_dir, plt, mticker, Patch, label, peripheral_groups)),
        ('P15c_top20_aggregated_core',
         lambda: _chart_p15c_top20_aggregated(df, output_dir, plt, mticker, Patch, label, peripheral_groups)),
        ('S04_foreign_top15',
         lambda: _chart_s04_foreign_top15(df, output_dir, plt, mticker, Patch, label)),
    ]

    for name, fn in chart_fns:
        try:
            log.info(f'  {name}...')
            fn()
            charts.append(name)
        except Exception as exc:
            log.warning(f'  Chart {name} failed: {exc}')

    log.info(f'Generated {len(charts)}/{len(chart_fns)} charts in {output_dir}')
    return charts


def main():
    """CLI entry point — generate charts from consolidated_matches.csv."""
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    parser = argparse.ArgumentParser(description='Generate summary charts from consolidated matches')
    parser.add_argument('consolidated_csv', help='Path to consolidated_matches.csv')
    parser.add_argument('--output-dir', '-o', default=None, help='Output directory for charts')
    parser.add_argument('--prefix', default='match', help='Column prefix (default: match)')
    parser.add_argument('--label', default='', help='Sector label for chart titles (e.g. Automotive)')
    args = parser.parse_args()

    csv_path = Path(args.consolidated_csv)
    out = Path(args.output_dir) if args.output_dir else csv_path.parent / 'charts'
    generate_summary_charts(csv_path, out, prefix=args.prefix, label=args.label)


if __name__ == '__main__':
    main()
