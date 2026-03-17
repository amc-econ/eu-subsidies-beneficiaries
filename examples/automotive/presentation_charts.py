#!/usr/bin/env python3
"""
PRESENTATION-GRADE CHART SUITE
================================
Professional academic economics-style charts for the automotive subsidy analysis.

Charts produced (P-series = presentation):
  P01 — MFF period stacked bars by instrument (the "really nice" style)
  P02 — MFF period stacked bars by data source
  P03 — MFF period stacked bars by fiscal source
  P04 — Annual support by data source (grouped stacked bar)
  P05 — Top 15 granting countries (grants vs loans split)
  P06 — Top 20 corporate groups by GGE (subsidy value)
  P07 — Sector breakdown (2-panel: face value vs GGE)
  P08 — Instrument composition shift (pre- vs post-2020)
  P09 — Top 10 groups: face value vs GGE comparison
  P10 — Annual grants by granting country (top 6 stacked area)

Usage:
  python -m examples.automotive.presentation_charts
"""

import pandas as pd
import numpy as np
import sys
import json
import logging
import warnings
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
warnings.filterwarnings('ignore', category=FutureWarning)
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
from src.paths import MATCH_OUTPUT_DIR

CONSOL_DIR = MATCH_OUTPUT_DIR / 'automotive'
PNG_DIR    = MATCH_OUTPUT_DIR / 'automotive' / 'charts'

# ---------------------------------------------------------------------------
# GGE rates
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
# MFF periods
# ---------------------------------------------------------------------------
MFF_PERIODS = {
    'Pre-2007': (0, 2006),
    '2007–2013': (2007, 2013),
    '2014–2020': (2014, 2020),
    '2021–2027': (2021, 2027),
}

def assign_mff(year):
    if pd.isna(year): return 'Unknown'
    y = int(year)
    for name, (lo, hi) in MFF_PERIODS.items():
        if lo <= y <= hi:
            return name
    return 'Unknown'

# ---------------------------------------------------------------------------
# Load data
# ---------------------------------------------------------------------------
def load_data():
    """Load consolidated matches with derived columns."""
    consol_path = CONSOL_DIR / 'consolidated_matches.csv'
    log.info(f"Loading {consol_path}...")
    df = pd.read_csv(consol_path, low_memory=False)

    # Derived columns
    df['mff_period'] = df['year'].apply(assign_mff)

    inst_map = {
        'grant': 'Grant', 'loan': 'Loan', 'guarantee': 'Guarantee',
        'equity': 'Equity', 'procurement': 'Procurement',
        'tax_advantage': 'Tax advantage', 'subsidy': 'Grant', 'other': 'Other',
    }
    df['instrument_simple'] = df['financial_instrument_class'].map(inst_map).fillna('Other')

    df['gge_rate'] = df.apply(_gge_rate, axis=1)
    df['gge_eur'] = df['amount_eur'] * df['gge_rate']

    if 'fiscal_source_type' in df.columns:
        df['fiscal_simple'] = df['fiscal_source_type'].fillna('Unknown')
    else:
        df['fiscal_simple'] = 'Unknown'

    log.info(f"  Loaded {len(df):,} rows, EUR {df['amount_eur'].sum()/1e9:.1f}B, GGE {df['gge_eur'].sum()/1e9:.1f}B")
    return df


# ---------------------------------------------------------------------------
# Matplotlib setup — academic economics style
# ---------------------------------------------------------------------------
def setup_matplotlib():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    import matplotlib.ticker as mticker
    from matplotlib.patches import Patch

    # Professional academic style
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


# ---------------------------------------------------------------------------
# Colour palettes
# ---------------------------------------------------------------------------
INSTRUMENT_COLORS = {
    'Grant': '#2196F3',
    'Loan': '#FF9800',
    'Guarantee': '#9C27B0',
    'Equity': '#4CAF50',
    'Tax advantage': '#F44336',
    'Procurement': '#795548',
    'Other': '#607D8B',
}
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
}
FISCAL_COLORS = {
    'ifi_balance_sheet': '#FF9800',
    'national_budget': '#2196F3',
    'eu_budget_direct': '#4CAF50',
    'eu_budget_shared': '#9C27B0',
    'eu_borrowing_ngeu': '#F44336',
}
SECTOR_COLORS = {
    'oem': '#1565C0', 'semiconductor': '#6A1B9A', 'supplier': '#E65100',
    'battery': '#2E7D32', 'battery_materials': '#00695C', 'truck_oem': '#4E342E',
    'tire': '#C62828', 'hydrogen_fc': '#00838F', 'other_automotive': '#78909C',
}

def fmt_bn(x, _): return f'€{x/1e9:.0f}B'
def fmt_bn1(x, _): return f'€{x/1e9:.1f}B'

# Core automotive = OEMs + truck OEMs only. Excludes semiconductors, battery materials,
# tires, generic suppliers (thyssenkrupp, BASF, etc.) that have large non-auto revenue.
CORE_AUTO_SECTORS = {'oem', 'truck_oem', 'battery', 'ev_charging'}
PERIPHERAL_GROUPS = {
    'STMicroelectronics', 'Infineon', 'NXP Semiconductors',  # semiconductors
    'thyssenkrupp', 'BASF', 'Umicore', 'Johnson Matthey',    # materials/steel
    'Michelin', 'Pirelli', 'Bridgestone',                      # tires
    'Rheinmetall',                                              # defence
    'Solvay',                                                   # chemicals
}

def _core_auto_filter(df):
    """Filter to core automotive groups — OEMs, battery makers, EV charging."""
    if 'sector_tag' in df.columns:
        mask = df['sector_tag'].isin(CORE_AUTO_SECTORS) | ~df['parent_group'].isin(PERIPHERAL_GROUPS)
        return df[mask].copy()
    return df[~df['parent_group'].isin(PERIPHERAL_GROUPS)].copy()


def _save_png(fig, name, plt_mod):
    path = PNG_DIR / f'{name}.png'
    try:
        fig.savefig(path, dpi=200, bbox_inches='tight', facecolor='white', edgecolor='none')
    except Exception:
        fig.savefig(path, dpi=200, bbox_inches='tight')
    plt_mod.close(fig)
    log.info(f"    {name}.png")


# ============================================================================
# CHART GENERATORS
# ============================================================================

def chart_p01_mff_instrument(df, plt, mticker, Patch):
    """MFF stacked bars by instrument — the 'really nice' chart."""
    mff_order = [p for p in MFF_PERIODS if p in df['mff_period'].unique()]
    mff_inst = df.groupby(['mff_period', 'instrument_simple'])['amount_eur'].sum().unstack(fill_value=0)
    mff_inst = mff_inst.reindex(mff_order)

    inst_order = ['Grant', 'Loan', 'Guarantee', 'Tax advantage', 'Equity', 'Other']
    cols = [c for c in inst_order if c in mff_inst.columns]

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(mff_order))
    width = 0.55
    bottom = np.zeros(len(mff_order))

    for inst in cols:
        vals = mff_inst[inst].values
        bars = ax.bar(x, vals, width, bottom=bottom, label=inst,
                      color=INSTRUMENT_COLORS.get(inst, '#999'), alpha=0.88, edgecolor='white', linewidth=0.8)
        # Label significant segments
        for i, v in enumerate(vals):
            if v / df['amount_eur'].sum() > 0.03:
                ax.text(x[i], bottom[i] + v/2, f'€{v/1e9:.1f}B',
                        ha='center', va='center', fontsize=9, color='white', fontweight='bold')
        bottom += vals

    # Total labels on top
    for i, p in enumerate(mff_order):
        total = mff_inst.loc[p].sum()
        ax.text(x[i], bottom[i] + df['amount_eur'].sum()*0.008,
                f'€{total/1e9:.1f}B', ha='center', va='bottom', fontsize=11, fontweight='bold')

    ax.set_xticks(x)
    ax.set_xticklabels(mff_order, fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('EU Automotive Support by MFF Period & Instrument', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc')
    ax.set_xlim(-0.5, len(mff_order) - 0.5)

    # Source annotation
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')

    plt.tight_layout()
    _save_png(fig, 'P01_mff_instrument_stacked', plt)


def chart_p02_mff_source(df, plt, mticker, Patch):
    """MFF stacked bars by data source."""
    mff_order = [p for p in MFF_PERIODS if p in df['mff_period'].unique()]
    mff_src = df.groupby(['mff_period', 'source'])['amount_eur'].sum().unstack(fill_value=0)
    mff_src = mff_src.reindex(mff_order)

    src_order = ['EIB', 'TAM', 'IPCEI_state_aid', 'FTS_CORDIS', 'KOHESIO', 'EBRD', 'CINEA', 'FTS', 'RRF', 'INNOVFUND']
    cols = [c for c in src_order if c in mff_src.columns]

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(mff_order))
    width = 0.55
    bottom = np.zeros(len(mff_order))

    for src in cols:
        vals = mff_src[src].values
        ax.bar(x, vals, width, bottom=bottom, label=src,
               color=SOURCE_COLORS.get(src, '#999'), alpha=0.88, edgecolor='white', linewidth=0.8)
        for i, v in enumerate(vals):
            if v / df['amount_eur'].sum() > 0.03:
                ax.text(x[i], bottom[i] + v/2, f'€{v/1e9:.1f}B',
                        ha='center', va='center', fontsize=8, color='white', fontweight='bold')
        bottom += vals

    for i, p in enumerate(mff_order):
        total = mff_src.loc[p].sum()
        ax.text(x[i], bottom[i] + df['amount_eur'].sum()*0.008,
                f'€{total/1e9:.1f}B', ha='center', va='bottom', fontsize=11, fontweight='bold')

    ax.set_xticks(x)
    ax.set_xticklabels(mff_order, fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('EU Automotive Support by MFF Period & Data Source', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc', ncol=2, fontsize=9)
    ax.set_xlim(-0.5, len(mff_order) - 0.5)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P02_mff_source_stacked', plt)


def chart_p03_mff_fiscal(df, plt, mticker, Patch):
    """MFF stacked bars by fiscal source."""
    mff_order = [p for p in MFF_PERIODS if p in df['mff_period'].unique()]
    mff_fisc = df.groupby(['mff_period', 'fiscal_simple'])['amount_eur'].sum().unstack(fill_value=0)
    mff_fisc = mff_fisc.reindex(mff_order)

    fisc_order = ['ifi_balance_sheet', 'national_budget', 'eu_budget_direct', 'eu_budget_shared', 'eu_borrowing_ngeu']
    fisc_labels = ['IFI Balance Sheet (EIB/EBRD)', 'National Budget (State Aid)', 'EU Budget (Direct Mgmt)', 'EU Budget (Shared/Cohesion)', 'EU Borrowing (RRF/NGEU)']
    cols = [(c, l) for c, l in zip(fisc_order, fisc_labels) if c in mff_fisc.columns]

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(mff_order))
    width = 0.55
    bottom = np.zeros(len(mff_order))

    for c, l in cols:
        vals = mff_fisc[c].values
        ax.bar(x, vals, width, bottom=bottom, label=l,
               color=FISCAL_COLORS.get(c, '#999'), alpha=0.88, edgecolor='white', linewidth=0.8)
        for i, v in enumerate(vals):
            if v / df['amount_eur'].sum() > 0.03:
                ax.text(x[i], bottom[i] + v/2, f'€{v/1e9:.1f}B',
                        ha='center', va='center', fontsize=8, color='white', fontweight='bold')
        bottom += vals

    for i, p in enumerate(mff_order):
        total = mff_fisc.loc[p].sum()
        ax.text(x[i], bottom[i] + df['amount_eur'].sum()*0.008,
                f'€{total/1e9:.1f}B', ha='center', va='bottom', fontsize=11, fontweight='bold')

    ax.set_xticks(x)
    ax.set_xticklabels(mff_order, fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('EU Automotive Support by MFF Period & Fiscal Source', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc', fontsize=9)
    ax.set_xlim(-0.5, len(mff_order) - 0.5)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P03_mff_fiscal_stacked', plt)


def chart_p04_annual_by_source(df, plt, mticker, Patch):
    """Annual support stacked bar by data source (2007-2025)."""
    yrs = range(2007, 2026)
    src_yr = df[df['year'].between(2007, 2025)].groupby(['year', 'source'])['amount_eur'].sum().unstack(fill_value=0)
    src_order = ['EIB', 'TAM', 'IPCEI_state_aid', 'FTS_CORDIS', 'KOHESIO', 'EBRD', 'CINEA', 'FTS', 'RRF', 'INNOVFUND']
    cols = [s for s in src_order if s in src_yr.columns]

    fig, ax = plt.subplots(figsize=(14, 7))
    bottom = np.zeros(len(yrs))
    x = np.array(list(yrs))

    for src in cols:
        vals = src_yr.reindex(yrs)[src].fillna(0).values
        ax.bar(x, vals, bottom=bottom, label=src,
               color=SOURCE_COLORS.get(src, '#999'), alpha=0.88, edgecolor='white', linewidth=0.3, width=0.8)
        bottom += vals

    ax.set_xlabel('Year', fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('EU Automotive Support by Year & Data Source (2007–2025)', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc', ncol=2, fontsize=9)
    ax.set_xlim(2006.2, 2025.8)

    # Add MFF period shading
    for label, (lo, hi) in [('MFF 2007–2013', (2006.5, 2013.5)), ('MFF 2014–2020', (2013.5, 2020.5)), ('MFF 2021–2027', (2020.5, 2025.5))]:
        ax.axvspan(lo, hi, alpha=0.04, color='gray')
        ax.text((lo+hi)/2, ax.get_ylim()[1]*0.97, label, ha='center', fontsize=8, color='#888', style='italic')

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P04_annual_by_source', plt)


def chart_p05_country_ranking(df, plt, mticker, Patch):
    """Top 15 granting countries with grants vs loans breakdown."""
    c_rank = df.groupby('country')['amount_eur'].sum().sort_values(ascending=False).head(15)
    countries = c_rank.index.tolist()

    c_inst = df[df['country'].isin(countries)].groupby(['country', 'instrument_simple'])['amount_eur'].sum().unstack(fill_value=0)
    c_inst = c_inst.reindex(countries)

    fig, ax = plt.subplots(figsize=(12, 9))
    y = np.arange(len(countries))
    height = 0.6
    left = np.zeros(len(countries))

    for inst in ['Grant', 'Loan', 'Guarantee', 'Tax advantage', 'Equity', 'Other']:
        if inst in c_inst.columns:
            vals = c_inst[inst].values
            ax.barh(y, vals, height, left=left, label=inst,
                    color=INSTRUMENT_COLORS.get(inst, '#999'), alpha=0.88, edgecolor='white', linewidth=0.5)
            left += vals

    ax.set_yticks(y)
    ax.set_yticklabels(countries, fontsize=11)
    ax.invert_yaxis()
    ax.set_xlabel('EUR', fontsize=12)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('Top 15 Countries: Automotive Support by Instrument', pad=15)
    ax.legend(loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=9)

    # Value labels
    for i, total in enumerate(c_rank.values):
        ax.text(total + c_rank.max()*0.01, i, f'€{total/1e9:.1f}B', va='center', fontsize=9, fontweight='bold')

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P05_country_instrument', plt)


def chart_p06_top20_gge(df, plt, mticker, Patch):
    """Top 20 corporate groups by GGE (subsidy value), with face value comparison."""
    g_gge = df.groupby('parent_group').agg(
        face=('amount_eur', 'sum'),
        gge=('gge_eur', 'sum'),
    ).sort_values('gge', ascending=False).head(20)

    g_meta = df.groupby('parent_group')['sector_tag'].first()

    fig, ax = plt.subplots(figsize=(13, 9))
    y = np.arange(len(g_gge))
    height = 0.35

    # Face value (light, behind)
    ax.barh(y + height/2, g_gge['face'].values, height, label='Face value',
            color='#BBDEFB', alpha=0.7, edgecolor='#90CAF9', linewidth=0.5)
    # GGE (bold, in front)
    bar_colors = [SECTOR_COLORS.get(g_meta.get(g, 'other_automotive'), '#78909C') for g in g_gge.index]
    ax.barh(y - height/2, g_gge['gge'].values, height, label='GGE (subsidy value)',
            color=bar_colors, alpha=0.9, edgecolor='white', linewidth=0.5)

    ax.set_yticks(y)
    ax.set_yticklabels(g_gge.index, fontsize=10)
    ax.invert_yaxis()
    ax.set_xlabel('EUR', fontsize=12)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))
    ax.set_title('Top 20 Corporate Groups: Face Value vs Gross Grant Equivalent', pad=15)

    # GGE value labels
    for i, (face, gge) in enumerate(zip(g_gge['face'].values, g_gge['gge'].values)):
        ax.text(gge + g_gge['gge'].max()*0.01, y[i] - height/2,
                f'€{gge/1e9:.1f}B', va='center', fontsize=8, fontweight='bold')
        ax.text(face + g_gge['face'].max()*0.01, y[i] + height/2,
                f'€{face/1e9:.1f}B', va='center', fontsize=8, color='#666')

    # Sector legend
    seen = set()
    legend_items = [Patch(facecolor='#BBDEFB', edgecolor='#90CAF9', label='Face value'),
                    Patch(facecolor='#1565C0', label='GGE')]
    for g in g_gge.index:
        s = g_meta.get(g, 'other_automotive')
        if s not in seen:
            seen.add(s)
            legend_items.append(Patch(facecolor=SECTOR_COLORS.get(s, '#999'), label=s))
    ax.legend(handles=legend_items, loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=8, ncol=2)

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database, GGE rates per EU State Aid Scoreboard (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P06_top20_gge', plt)


def chart_p07_sector_dual(df, plt, mticker, Patch):
    """Sector breakdown: 2-panel face value vs GGE."""
    sec = df.groupby('sector_tag').agg(
        face=('amount_eur', 'sum'),
        gge=('gge_eur', 'sum'),
    ).sort_values('face', ascending=False)

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 7))

    y = np.arange(len(sec))
    colors = [SECTOR_COLORS.get(s, '#78909C') for s in sec.index]

    # Left: face value
    ax1.barh(y, sec['face'].values, color=colors, alpha=0.88, edgecolor='white')
    ax1.set_yticks(y)
    ax1.set_yticklabels(sec.index, fontsize=11)
    ax1.invert_yaxis()
    ax1.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax1.set_title(f'Financing Volume\n€{sec["face"].sum()/1e9:.1f}B total', fontsize=13, fontweight='bold')
    for i, v in enumerate(sec['face'].values):
        ax1.text(v + sec['face'].max()*0.01, i, f'€{v/1e9:.1f}B', va='center', fontsize=9)

    # Right: GGE
    ax2.barh(y, sec['gge'].values, color=colors, alpha=0.88, edgecolor='white')
    ax2.set_yticks(y)
    ax2.set_yticklabels(sec.index, fontsize=11)
    ax2.invert_yaxis()
    ax2.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax2.set_title(f'Gross Grant Equivalent\n€{sec["gge"].sum()/1e9:.1f}B total', fontsize=13, fontweight='bold')
    for i, v in enumerate(sec['gge'].values):
        ax2.text(v + sec['gge'].max()*0.01, i, f'€{v/1e9:.1f}B', va='center', fontsize=9)

    fig.suptitle('Automotive Support by Sector: Face Value vs Subsidy Value', fontsize=15, fontweight='bold', y=1.02)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P07_sector_face_vs_gge', plt)


def chart_p08_shift_instrument(df, plt, mticker, Patch):
    """Pre vs Post 2020: instrument composition side-by-side."""
    periods = [('Pre-2020\n(2007–2019)', (2007, 2019)), ('Post-2020\n(2020–2025)', (2020, 2025))]
    inst_order = ['Grant', 'Loan', 'Guarantee', 'Tax advantage', 'Equity', 'Other']

    fig, axes = plt.subplots(1, 2, figsize=(12, 7))

    for idx, (label, (lo, hi)) in enumerate(periods):
        sub = df[df['year'].between(lo, hi)]
        inst_data = sub.groupby('instrument_simple')['amount_eur'].sum()
        total = inst_data.sum()

        vals = [inst_data.get(i, 0) for i in inst_order]
        colors = [INSTRUMENT_COLORS.get(i, '#999') for i in inst_order]
        non_zero = [(v, c, i) for v, c, i in zip(vals, colors, inst_order) if v > 0]

        wedges, texts, autotexts = axes[idx].pie(
            [v for v, _, _ in non_zero],
            labels=[f'{i}\n€{v/1e9:.1f}B' for v, _, i in non_zero],
            colors=[c for _, c, _ in non_zero],
            autopct='%1.0f%%', startangle=90,
            textprops={'fontsize': 10},
            pctdistance=0.75,
        )
        for at in autotexts:
            at.set_fontweight('bold')
            at.set_fontsize(10)

        axes[idx].set_title(f'{label}\n€{total/1e9:.1f}B total', fontsize=13, fontweight='bold')

    fig.suptitle('Structural Shift in EU Automotive Support', fontsize=15, fontweight='bold', y=1.02)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P08_shift_instrument', plt)


def chart_p09_top10_face_vs_gge(df, plt, mticker, Patch):
    """Top 10 groups: paired face value vs GGE bars."""
    g = df.groupby('parent_group').agg(
        face=('amount_eur', 'sum'),
        gge=('gge_eur', 'sum'),
    ).sort_values('face', ascending=False).head(10)

    fig, ax = plt.subplots(figsize=(14, 7))
    x = np.arange(len(g))
    width = 0.35

    ax.bar(x - width/2, g['face'].values/1e9, width, label='Face value', color='#1565C0', alpha=0.85, edgecolor='white')
    ax.bar(x + width/2, g['gge'].values/1e9, width, label='GGE (subsidy value)', color='#FF8F00', alpha=0.85, edgecolor='white')

    ax.set_xticks(x)
    ax.set_xticklabels(g.index, rotation=35, ha='right', fontsize=10)
    ax.set_ylabel('EUR Billions', fontsize=12)
    ax.set_title('Top 10 Corporate Groups: Financing Volume vs Subsidy Value', pad=15)
    ax.legend(framealpha=0.95, edgecolor='#ccc', fontsize=11)

    # Value labels
    for i in range(len(g)):
        ax.text(x[i] - width/2, g['face'].values[i]/1e9 + 0.1,
                f'€{g["face"].values[i]/1e9:.1f}B', ha='center', fontsize=8, fontweight='bold')
        ax.text(x[i] + width/2, g['gge'].values[i]/1e9 + 0.1,
                f'€{g["gge"].values[i]/1e9:.1f}B', ha='center', fontsize=8, fontweight='bold', color='#E65100')

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database, GGE rates per EU State Aid Scoreboard (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P09_top10_face_vs_gge', plt)


def chart_p10_country_timeseries(df, plt, mticker, Patch):
    """Annual grants by top 6 granting countries — stacked area."""
    # Only grants (most policy-relevant)
    grants = df[df['instrument_simple'] == 'Grant']
    top_countries = grants.groupby('country')['amount_eur'].sum().sort_values(ascending=False).head(6).index.tolist()

    c_yr = grants[grants['country'].isin(top_countries) & grants['year'].between(2007, 2025)]\
        .groupby(['year', 'country'])['amount_eur'].sum().unstack(fill_value=0)
    c_yr = c_yr.reindex(range(2007, 2026), fill_value=0)
    c_yr = c_yr[top_countries]

    country_colors = ['#1565C0', '#C62828', '#2E7D32', '#E65100', '#6A1B9A', '#00695C']

    fig, ax = plt.subplots(figsize=(14, 7))
    ax.stackplot(c_yr.index, *[c_yr[c] for c in top_countries],
                 labels=top_countries, colors=country_colors, alpha=0.82)

    ax.set_xlabel('Year', fontsize=12)
    ax.set_ylabel('EUR (grants only)', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('Grant-Based Automotive Support by Country (Top 6, 2007–2025)', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc', fontsize=10)
    ax.set_xlim(2007, 2025)

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P10_country_grants_timeseries', plt)


def chart_p11_innovfund(df, plt, mticker, Patch):
    """Innovation Fund automotive breakdown — horizontal lollipop by project."""
    # Filter INNOVFUND + CINEA INNOVFUND rows, deduplicate by source_record_id
    inno = df[
        (df['source'].isin(['INNOVFUND', 'CINEA'])) &
        (df['programme'].str.contains('INNOVFUND', case=False, na=False))
    ].copy()
    if inno.empty:
        log.warning("  No INNOVFUND rows found, skipping P11")
        return

    # Deduplicate: keep one row per source_record_id + entity
    inno = inno.sort_values('amount_eur', ascending=False)\
               .drop_duplicates(subset=['source_record_id', 'entity_name_clean'], keep='first')
    # Drop zero-amount rows before aggregation
    inno = inno[inno['amount_eur'] > 0]

    # Aggregate by parent_group
    grp = inno.groupby('parent_group').agg(
        eur=('amount_eur', 'sum'),
        gge=('gge_eur', 'sum'),
        rows=('amount_eur', 'count'),
        countries=('country', lambda x: ', '.join(sorted(x.unique()))),
    ).sort_values('eur', ascending=True)
    grp = grp[grp['eur'] > 0]  # drop zero-amount groups

    if grp.empty:
        log.warning("  No non-zero INNOVFUND data, skipping P11")
        return

    # Sector colors for the dots
    sector_tag_map = {}
    if 'sector_tag' in df.columns:
        sector_tag_map = df.drop_duplicates('parent_group').set_index('parent_group')['sector_tag'].to_dict()

    fig, ax = plt.subplots(figsize=(12, max(6, len(grp) * 0.55 + 1.5)))
    y = np.arange(len(grp))

    # Horizontal bars (light fill) + dots (dark)
    bar_colors = [SECTOR_COLORS.get(sector_tag_map.get(g, ''), '#607D8B') for g in grp.index]
    ax.barh(y, grp['eur'].values, height=0.5, color=bar_colors, alpha=0.35, edgecolor='none')
    ax.scatter(grp['eur'].values, y, color=bar_colors, s=80, zorder=5, edgecolor='white', linewidth=1.2)

    ax.set_yticks(y)
    ax.set_yticklabels(grp.index, fontsize=11)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'€{x/1e6:.0f}M'))

    # Value labels
    max_val = grp['eur'].max()
    for i, (g, row) in enumerate(grp.iterrows()):
        label = f'€{row["eur"]/1e6:.0f}M' if row['eur'] < 1e9 else f'€{row["eur"]/1e9:.1f}B'
        ax.text(row['eur'] + max_val * 0.02, i, f'{label}  ({row["countries"]})',
                va='center', fontsize=9, color='#333')

    total = grp['eur'].sum()
    ax.set_title(f'EU Innovation Fund — Automotive Beneficiaries\n'
                 f'{len(grp)} groups, €{total/1e6:.0f}M total across {int(grp["rows"].sum())} grants',
                 fontsize=14, fontweight='bold', pad=15)
    ax.set_xlabel('Grant Amount (EUR)', fontsize=11)

    # Legend for sector colors
    used_sectors = set(sector_tag_map.get(g, '') for g in grp.index) - {''}
    if used_sectors:
        legend_patches = [Patch(facecolor=SECTOR_COLORS.get(s, '#999'), alpha=0.7, label=s.replace('_', ' ').title())
                          for s in sorted(used_sectors) if s in SECTOR_COLORS]
        if legend_patches:
            ax.legend(handles=legend_patches, loc='lower right', fontsize=9,
                      framealpha=0.95, edgecolor='#ccc', title='Sector', title_fontsize=10)

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database — CINEA Innovation Fund (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P11_innovfund_breakdown', plt)


def chart_p12_big_tickets_by_source(df, plt, mticker, Patch):
    """Per-source 'big ticket' lollipop charts — top 10 single grants per source."""
    # Load big ticket data
    bt_path = CONSOL_DIR / 'big_ticket_automotive_single_row.csv'
    if not bt_path.exists():
        log.warning("  big_ticket_automotive_single_row.csv not found, skipping P12")
        return

    bt = pd.read_csv(bt_path, low_memory=False)
    sources = sorted(bt['source'].unique())
    n_sources = len(sources)

    if n_sources == 0:
        return

    # Grid layout: 3 columns, as many rows as needed
    n_cols = 3
    n_rows = (n_sources + n_cols - 1) // n_cols
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(20, n_rows * 4.5))
    if n_rows == 1:
        axes = axes.reshape(1, -1)

    for idx, src in enumerate(sources):
        r, c = divmod(idx, n_cols)
        ax = axes[r][c]
        sub = bt[bt['source'] == src].sort_values('amount_eur', ascending=True).tail(10)
        if sub.empty:
            ax.set_visible(False)
            continue

        y = np.arange(len(sub))
        # Short beneficiary labels
        labels = []
        for _, row in sub.iterrows():
            name = str(row.get('parent_group', row['beneficiary_name']))
            if len(name) > 30:
                name = name[:28] + '..'
            labels.append(name)

        src_color = SOURCE_COLORS.get(src, '#607D8B')
        # Stem plot (lollipop)
        ax.hlines(y, 0, sub['amount_eur'].values, color=src_color, alpha=0.5, linewidth=2.5)
        ax.scatter(sub['amount_eur'].values, y, color=src_color, s=60, zorder=5,
                   edgecolor='white', linewidth=1)

        ax.set_yticks(y)
        ax.set_yticklabels(labels, fontsize=8)

        # Value labels
        max_val = sub['amount_eur'].max()
        for i, (_, row) in enumerate(sub.iterrows()):
            v = row['amount_eur']
            if v >= 1e9:
                label = f'€{v/1e9:.1f}B'
            elif v >= 1e6:
                label = f'€{v/1e6:.0f}M'
            else:
                label = f'€{v/1e3:.0f}K'
            ax.text(v + max_val * 0.03, i, label, va='center', fontsize=7.5, color='#333')

        # Source total
        total = sub['amount_eur'].sum()
        total_label = f'€{total/1e9:.1f}B' if total >= 1e9 else f'€{total/1e6:.0f}M'
        ax.set_title(f'{src}\nTop {len(sub)}: {total_label}',
                     fontsize=11, fontweight='bold', color=src_color)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f'€{x/1e9:.1f}B' if x >= 1e9 else f'€{x/1e6:.0f}M'))
        ax.tick_params(axis='x', labelsize=7)
        for spine in ['top', 'right']:
            ax.spines[spine].set_visible(False)
        ax.grid(axis='x', alpha=0.2)

    # Hide empty subplots
    for idx in range(n_sources, n_rows * n_cols):
        r, c = divmod(idx, n_cols)
        axes[r][c].set_visible(False)

    fig.suptitle('Largest Single Automotive Awards by Data Source',
                 fontsize=16, fontweight='bold', y=1.01)
    fig.text(0.99, 0.005, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P12_big_tickets_by_source', plt)


def chart_p13_big_tickets_cumulative(df, plt, mticker, Patch):
    """Cumulative big-ticket bar chart — top 15 single automotive awards across ALL sources."""
    bt_path = CONSOL_DIR / 'big_ticket_automotive_single_row.csv'
    if not bt_path.exists():
        log.warning("  big_ticket data not found, skipping P13")
        return

    bt = pd.read_csv(bt_path, low_memory=False)
    top15 = bt.sort_values('amount_eur', ascending=False).head(15).copy()
    top15 = top15.sort_values('amount_eur', ascending=True)  # ascending for horizontal bars

    fig, ax = plt.subplots(figsize=(13, 8))
    y = np.arange(len(top15))

    # Build labels: "Company (Source, Country, Year)"
    labels = []
    for _, r in top15.iterrows():
        name = str(r.get('parent_group', r['beneficiary_name']))
        if len(name) > 35:
            name = name[:33] + '..'
        yr = int(r['year']) if pd.notna(r.get('year')) else '?'
        labels.append(f'{name}  ({r["source"]}, {r.get("country","")}, {yr})')

    bar_colors = [SOURCE_COLORS.get(r['source'], '#607D8B') for _, r in top15.iterrows()]
    inst_hatches = {'loan': '///', 'grant': '', 'guarantee': '...', 'other': 'xxx'}

    bars = ax.barh(y, top15['amount_eur'].values, height=0.65, color=bar_colors,
                   alpha=0.82, edgecolor='white', linewidth=0.8)

    # Add instrument hatch overlay
    for bar, (_, r) in zip(bars, top15.iterrows()):
        h = inst_hatches.get(str(r.get('instrument', '')).lower(), '')
        if h:
            bar.set_hatch(h)
            bar.set_edgecolor('#666')

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f'€{x/1e9:.1f}B' if x >= 1e9 else f'€{x/1e6:.0f}M'))

    # Value labels
    max_val = top15['amount_eur'].max()
    for i, (_, r) in enumerate(top15.iterrows()):
        v = r['amount_eur']
        label = f'€{v/1e9:.2f}B' if v >= 1e9 else f'€{v/1e6:.0f}M'
        ax.text(v + max_val * 0.015, i, label, va='center', fontsize=9, color='#333')

    # Legend for sources
    used_sources = top15['source'].unique()
    source_patches = [Patch(facecolor=SOURCE_COLORS.get(s, '#999'), alpha=0.82, label=s)
                      for s in sorted(used_sources)]
    inst_patches = [Patch(facecolor='#ccc', hatch='///', edgecolor='#666', label='Loan'),
                    Patch(facecolor='#ccc', hatch='', edgecolor='#666', label='Grant')]
    ax.legend(handles=source_patches + inst_patches, loc='lower right', fontsize=9,
              framealpha=0.95, edgecolor='#ccc', ncol=2, title='Source / Instrument', title_fontsize=10)

    total = top15['amount_eur'].sum()
    ax.set_title(f'15 Largest Individual Automotive Awards\n'
                 f'€{total/1e9:.1f}B combined across {len(used_sources)} sources',
                 fontsize=14, fontweight='bold', pad=15)

    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P13_top15_big_tickets', plt)


# ---------------------------------------------------------------------------
# P14: MFF × Sector stacked bars
# ---------------------------------------------------------------------------
def chart_p14_mff_sector(df, plt, mticker, Patch):
    """MFF stacked bars by company sector."""
    mff_order = [p for p in MFF_PERIODS if p in df['mff_period'].unique()]
    sector_col = 'sector_tag' if 'sector_tag' in df.columns else None
    if not sector_col:
        log.warning("  No sector_tag column, skipping P14")
        return

    mff_sec = df.groupby(['mff_period', sector_col])['amount_eur'].sum().unstack(fill_value=0)
    mff_sec = mff_sec.reindex(mff_order)
    sec_order = ['oem', 'battery', 'supplier', 'semiconductor', 'truck_oem',
                 'battery_materials', 'tire', 'ev_charging', 'hydrogen_fc', 'other_automotive']
    cols = [c for c in sec_order if c in mff_sec.columns]

    fig, ax = plt.subplots(figsize=(11, 7))
    x = np.arange(len(mff_order))
    width = 0.55
    bottom = np.zeros(len(mff_order))
    for sec in cols:
        vals = mff_sec[sec].values
        label = sec.replace('_', ' ').title()
        ax.bar(x, vals, width, bottom=bottom, label=label,
               color=SECTOR_COLORS.get(sec, '#999'), alpha=0.88, edgecolor='white', linewidth=0.8)
        for i, v in enumerate(vals):
            if v / max(df['amount_eur'].sum(), 1) > 0.03:
                ax.text(x[i], bottom[i] + v/2, f'€{v/1e9:.1f}B',
                        ha='center', va='center', fontsize=8, color='white', fontweight='bold')
        bottom += vals
    for i, p in enumerate(mff_order):
        total = mff_sec.loc[p].sum()
        ax.text(x[i], bottom[i] + df['amount_eur'].sum()*0.008,
                f'€{total/1e9:.1f}B', ha='center', va='bottom', fontsize=11, fontweight='bold')

    ax.set_xticks(x)
    ax.set_xticklabels(mff_order, fontsize=12)
    ax.set_ylabel('EUR', fontsize=12)
    ax.yaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    ax.set_title('EU Automotive Support by MFF Period & Sector', pad=15)
    ax.legend(loc='upper left', framealpha=0.95, edgecolor='#ccc', fontsize=9)
    ax.set_xlim(-0.5, len(mff_order) - 0.5)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P14_mff_sector_stacked', plt)


# ---------------------------------------------------------------------------
# P12b: Big tickets by source — AGGREGATED per beneficiary
# ---------------------------------------------------------------------------
def chart_p12b_big_tickets_aggregated(df, plt, mticker, Patch):
    """Per-source big ticket lollipops — aggregated by parent_group (no duplicates)."""
    sources = sorted(s for s in df['source'].unique() if s != 'RRF')
    n_sources = len(sources)
    if n_sources == 0:
        return

    n_cols = 3
    n_rows = (n_sources + n_cols - 1) // n_cols
    fig, axes = plt.subplots(n_rows, n_cols, figsize=(20, n_rows * 4.5))
    if n_rows == 1:
        axes = axes.reshape(1, -1)

    for idx, src in enumerate(sources):
        r, c = divmod(idx, n_cols)
        ax = axes[r][c]
        src_df = df[df['source'] == src]
        grp = src_df.groupby('parent_group').agg(
            total_eur=('amount_eur', 'sum'),
            row_count=('amount_eur', 'count'),
        ).sort_values('total_eur', ascending=True).tail(10)

        if grp.empty:
            ax.set_visible(False)
            continue

        y_pos = np.arange(len(grp))
        labels = []
        for g, row in grp.iterrows():
            name = str(g)
            cnt = int(row['row_count'])
            if len(name) > 28:
                name = name[:26] + '..'
            labels.append(f'{name} ({cnt})')

        src_color = SOURCE_COLORS.get(src, '#607D8B')
        ax.hlines(y_pos, 0, grp['total_eur'].values, color=src_color, alpha=0.5, linewidth=2.5)
        ax.scatter(grp['total_eur'].values, y_pos, color=src_color, s=60, zorder=5,
                   edgecolor='white', linewidth=1)
        ax.set_yticks(y_pos)
        ax.set_yticklabels(labels, fontsize=8)

        max_val = grp['total_eur'].max()
        for i, (g, row) in enumerate(grp.iterrows()):
            v = row['total_eur']
            label = f'€{v/1e9:.1f}B' if v >= 1e9 else f'€{v/1e6:.0f}M' if v >= 1e6 else f'€{v/1e3:.0f}K'
            ax.text(v + max_val * 0.03, i, label, va='center', fontsize=7.5, color='#333')

        total = grp['total_eur'].sum()
        total_label = f'€{total/1e9:.1f}B' if total >= 1e9 else f'€{total/1e6:.0f}M'
        ax.set_title(f'{src}\nTop {len(grp)} groups: {total_label}',
                     fontsize=11, fontweight='bold', color=src_color)
        ax.xaxis.set_major_formatter(mticker.FuncFormatter(
            lambda x, _: f'€{x/1e9:.1f}B' if x >= 1e9 else f'€{x/1e6:.0f}M'))
        ax.tick_params(axis='x', labelsize=7)
        for spine in ['top', 'right']:
            ax.spines[spine].set_visible(False)
        ax.grid(axis='x', alpha=0.2)

    for idx in range(n_sources, n_rows * n_cols):
        r, c = divmod(idx, n_cols)
        axes[r][c].set_visible(False)

    fig.suptitle('Top Automotive Corporate Groups by Data Source',
                 fontsize=16, fontweight='bold', y=1.01)
    fig.text(0.99, 0.005, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P12b_big_tickets_aggregated', plt)


# ---------------------------------------------------------------------------
# Core Automotive versions — P06c, P09c, P13c
# ---------------------------------------------------------------------------
def chart_p06c_top20_gge_core(df, plt, mticker, Patch):
    """Top 20 groups by GGE — core automotive only (no semiconductors/materials/tires)."""
    core = df[~df['parent_group'].isin(PERIPHERAL_GROUPS)]
    grp = core.groupby('parent_group').agg(face=('amount_eur','sum'), gge=('gge_eur','sum'))\
              .sort_values('gge', ascending=False).head(20)
    grp = grp.sort_values('gge', ascending=True)

    fig, ax = plt.subplots(figsize=(13, 9))
    y = np.arange(len(grp))
    ax.barh(y - 0.18, grp['face'].values, height=0.35, color='#1565C0', alpha=0.6, label='Face value')
    ax.barh(y + 0.18, grp['gge'].values, height=0.35, color='#C62828', alpha=0.8, label='GGE (subsidy value)')

    ax.set_yticks(y)
    ax.set_yticklabels(grp.index, fontsize=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))
    for i, (_, row) in enumerate(grp.iterrows()):
        ax.text(row['gge'] + grp['face'].max()*0.01, i+0.18, f'€{row["gge"]/1e9:.2f}B',
                va='center', fontsize=8, color='#C62828')

    total_face = core['amount_eur'].sum()
    total_gge = core['gge_eur'].sum()
    ax.set_title(f'Top 20 Core Automotive Groups by Subsidy Value (GGE)\n'
                 f'Excl. semiconductors, materials, tires — €{total_gge/1e9:.1f}B GGE / €{total_face/1e9:.1f}B face',
                 fontsize=13, fontweight='bold', pad=15)
    ax.legend(loc='lower right', fontsize=10, framealpha=0.95, edgecolor='#ccc')
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database, GGE per EU State Aid Scoreboard (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P06c_top20_gge_core_auto', plt)


def chart_p09c_top10_core(df, plt, mticker, Patch):
    """Top 10 face vs GGE — core automotive only."""
    core = df[~df['parent_group'].isin(PERIPHERAL_GROUPS)]
    grp = core.groupby('parent_group').agg(face=('amount_eur','sum'), gge=('gge_eur','sum'))\
              .sort_values('face', ascending=False).head(10)
    grp = grp.sort_values('face', ascending=True)

    fig, ax = plt.subplots(figsize=(12, 7))
    y = np.arange(len(grp))
    ax.barh(y - 0.18, grp['face'].values, height=0.35, color='#1565C0', alpha=0.65, label='Face value')
    ax.barh(y + 0.18, grp['gge'].values, height=0.35, color='#C62828', alpha=0.8, label='GGE')
    ax.set_yticks(y)
    ax.set_yticklabels(grp.index, fontsize=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))
    for i, (_, row) in enumerate(grp.iterrows()):
        ax.text(row['face'] + grp['face'].max()*0.01, i-0.18, f'€{row["face"]/1e9:.1f}B',
                va='center', fontsize=8, color='#1565C0')
        ax.text(row['gge'] + grp['face'].max()*0.01, i+0.18, f'€{row["gge"]/1e9:.1f}B',
                va='center', fontsize=8, color='#C62828')

    ax.set_title('Top 10 Core Automotive Groups: Face Value vs Subsidy Value',
                 fontsize=13, fontweight='bold', pad=15)
    ax.legend(loc='lower right', fontsize=10, framealpha=0.95, edgecolor='#ccc')
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database, GGE per EU State Aid Scoreboard (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P09c_top10_face_vs_gge_core', plt)


def chart_p13c_big_tickets_core(df, plt, mticker, Patch):
    """Top 15 big tickets — core automotive only."""
    bt_path = CONSOL_DIR / 'big_ticket_automotive_single_row.csv'
    if not bt_path.exists():
        return

    bt = pd.read_csv(bt_path, low_memory=False)
    bt = bt[~bt['parent_group'].isin(PERIPHERAL_GROUPS)]
    top15 = bt.sort_values('amount_eur', ascending=False).head(15).copy()
    top15 = top15.sort_values('amount_eur', ascending=True)

    fig, ax = plt.subplots(figsize=(13, 8))
    y = np.arange(len(top15))

    labels = []
    for _, r in top15.iterrows():
        name = str(r.get('parent_group', r['beneficiary_name']))
        if len(name) > 35:
            name = name[:33] + '..'
        yr = int(r['year']) if pd.notna(r.get('year')) else '?'
        labels.append(f'{name}  ({r["source"]}, {r.get("country","")}, {yr})')

    bar_colors = [SOURCE_COLORS.get(r['source'], '#607D8B') for _, r in top15.iterrows()]
    inst_hatches = {'loan': '///', 'grant': '', 'guarantee': '...'}

    bars = ax.barh(y, top15['amount_eur'].values, height=0.65, color=bar_colors,
                   alpha=0.82, edgecolor='white', linewidth=0.8)
    for bar, (_, r) in zip(bars, top15.iterrows()):
        h = inst_hatches.get(str(r.get('instrument', '')).lower(), '')
        if h:
            bar.set_hatch(h)
            bar.set_edgecolor('#666')

    ax.set_yticks(y)
    ax.set_yticklabels(labels, fontsize=9)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(
        lambda x, _: f'€{x/1e9:.1f}B' if x >= 1e9 else f'€{x/1e6:.0f}M'))

    max_val = top15['amount_eur'].max()
    for i, (_, r) in enumerate(top15.iterrows()):
        v = r['amount_eur']
        label = f'€{v/1e9:.2f}B' if v >= 1e9 else f'€{v/1e6:.0f}M'
        ax.text(v + max_val * 0.015, i, label, va='center', fontsize=9, color='#333')

    used_sources = top15['source'].unique()
    source_patches = [Patch(facecolor=SOURCE_COLORS.get(s, '#999'), alpha=0.82, label=s)
                      for s in sorted(used_sources)]
    inst_patches = [Patch(facecolor='#ccc', hatch='///', edgecolor='#666', label='Loan'),
                    Patch(facecolor='#ccc', hatch='', edgecolor='#666', label='Grant')]
    ax.legend(handles=source_patches + inst_patches, loc='lower right', fontsize=9,
              framealpha=0.95, edgecolor='#ccc', ncol=2, title='Source / Instrument', title_fontsize=10)

    total = top15['amount_eur'].sum()
    ax.set_title(f'15 Largest Core Automotive Awards (excl. semis/materials/tires)\n'
                 f'€{total/1e9:.1f}B combined',
                 fontsize=14, fontweight='bold', pad=15)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)',
             ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P13c_top15_big_tickets_core', plt)


def chart_p15c_top20_aggregated_core(df, plt, mticker, Patch):
    """Top 20 core automotive groups by total face value — stacked by source."""
    core = df[~df['parent_group'].isin(PERIPHERAL_GROUPS)]
    grp = core.groupby(['parent_group', 'source'])['amount_eur'].sum().unstack(fill_value=0)
    grp['total'] = grp.sum(axis=1)
    grp = grp.sort_values('total', ascending=False).head(20)
    grp = grp.sort_values('total', ascending=True)

    src_order = ['EIB', 'TAM', 'IPCEI_state_aid', 'FTS_CORDIS', 'KOHESIO', 'EBRD',
                 'CINEA', 'FTS', 'RRF', 'INNOVFUND']
    cols = [c for c in src_order if c in grp.columns]

    fig, ax = plt.subplots(figsize=(13, 9))
    y = np.arange(len(grp))
    left = np.zeros(len(grp))

    for src in cols:
        vals = grp[src].values
        ax.barh(y, vals, left=left, height=0.6, label=src,
                color=SOURCE_COLORS.get(src, '#999'), alpha=0.85, edgecolor='white', linewidth=0.5)
        left += vals

    ax.set_yticks(y)
    ax.set_yticklabels(grp.index, fontsize=10)
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn1))

    for i, (g, row) in enumerate(grp.iterrows()):
        v = row['total']
        label = f'€{v/1e9:.2f}B' if v >= 1e9 else f'€{v/1e6:.0f}M'
        ax.text(v + grp['total'].max() * 0.01, i, label, va='center', fontsize=9, color='#333')

    row_counts = core.groupby('parent_group').size()
    for i, g in enumerate(grp.index):
        cnt = row_counts.get(g, 0)
        ax.text(-grp['total'].max() * 0.01, i, f'({cnt})', va='center', ha='right',
                fontsize=7.5, color='#999')

    total = core['amount_eur'].sum()
    ax.set_title(f'Top 20 Core Automotive Groups — Total Support by Source'
                 f'  —  €{total/1e9:.1f}B total',
                 fontsize=13, fontweight='bold', pad=15)
    ax.legend(loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=9,
              ncol=2, title='Data Source', title_fontsize=10)
    plt.tight_layout()
    _save_png(fig, 'P15c_top20_aggregated_core', plt)


def chart_p05c_country_core(df, plt, mticker, Patch):
    """Top 15 countries — core automotive only."""
    core = df[~df['parent_group'].isin(PERIPHERAL_GROUPS)]
    country_data = core.groupby(['country', 'instrument_simple'])['amount_eur'].sum().unstack(fill_value=0)
    country_totals = country_data.sum(axis=1).sort_values(ascending=False).head(15)
    country_data = country_data.reindex(country_totals.index)

    fig, ax = plt.subplots(figsize=(12, 8))
    y = np.arange(len(country_data))
    inst_order = ['Grant', 'Loan', 'Guarantee', 'Tax advantage', 'Equity', 'Other']
    left = np.zeros(len(country_data))
    for inst in inst_order:
        if inst not in country_data.columns:
            continue
        vals = country_data[inst].values
        ax.barh(y, vals, left=left, height=0.6, label=inst,
                color=INSTRUMENT_COLORS.get(inst, '#999'), alpha=0.85, edgecolor='white', linewidth=0.5)
        left += vals

    ax.set_yticks(y)
    ax.set_yticklabels(country_data.index, fontsize=11)
    ax.invert_yaxis()
    ax.xaxis.set_major_formatter(mticker.FuncFormatter(fmt_bn))
    for i, total in enumerate(country_totals.values):
        ax.text(total + country_totals.max()*0.01, i, f'€{total/1e9:.1f}B', va='center', fontsize=9)

    ax.set_title(f'Top 15 Countries — Core Automotive (excl. semis/materials/tires)\n'
                 f'€{core["amount_eur"].sum()/1e9:.1f}B total',
                 fontsize=13, fontweight='bold', pad=15)
    ax.legend(loc='lower right', framealpha=0.95, edgecolor='#ccc', fontsize=9)
    fig.text(0.99, 0.01, 'Source: Bruegel EU Subsidies Database (2026)', ha='right', fontsize=8, color='#666')
    plt.tight_layout()
    _save_png(fig, 'P05c_country_instrument_core', plt)


# ============================================================================
# MAIN
# ============================================================================

def main():
    PNG_DIR.mkdir(parents=True, exist_ok=True)
    log.info("=" * 70)
    log.info("PRESENTATION CHART SUITE — Starting")
    log.info("=" * 70)

    df = load_data()
    plt, mticker, Patch = setup_matplotlib()

    chart_count = 0

    generators = [
        ('P01: MFF × Instrument', chart_p01_mff_instrument),
        ('P02: MFF × Source', chart_p02_mff_source),
        ('P03: MFF × Fiscal', chart_p03_mff_fiscal),
        ('P04: Annual × Source', chart_p04_annual_by_source),
        ('P05: Country Ranking', chart_p05_country_ranking),
        ('P06: Top 20 GGE', chart_p06_top20_gge),
        ('P07: Sector Dual', chart_p07_sector_dual),
        ('P08: Shift Instrument', chart_p08_shift_instrument),
        ('P09: Top 10 Face vs GGE', chart_p09_top10_face_vs_gge),
        ('P10: Country Timeseries', chart_p10_country_timeseries),
        ('P11: Innovation Fund', chart_p11_innovfund),
        ('P12: Big Tickets by Source', chart_p12_big_tickets_by_source),
        ('P12b: Big Tickets Aggregated', chart_p12b_big_tickets_aggregated),
        ('P13: Top 15 Big Tickets', chart_p13_big_tickets_cumulative),
        ('P14: MFF × Sector', chart_p14_mff_sector),
        # Core automotive versions
        ('P05c: Country Core Auto', chart_p05c_country_core),
        ('P06c: Top 20 GGE Core', chart_p06c_top20_gge_core),
        ('P09c: Top 10 Face vs GGE Core', chart_p09c_top10_core),
        ('P13c: Top 15 Big Tickets Core', chart_p13c_big_tickets_core),
        ('P15c: Top 20 Aggregated Core', chart_p15c_top20_aggregated_core),
    ]

    for name, fn in generators:
        try:
            log.info(f"  {name}...")
            fn(df, plt, mticker, Patch)
            chart_count += 1
        except Exception as e:
            log.error(f"  FAILED {name}: {e}")

    log.info(f"\n{'='*70}")
    log.info(f"PRESENTATION CHARTS COMPLETE — {chart_count} charts generated")
    log.info(f"  Output: {PNG_DIR}")
    log.info(f"{'='*70}")


if __name__ == '__main__':
    main()
