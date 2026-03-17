#!/usr/bin/env python3
"""
Generic Summary Charts
========================
Generates publication-grade charts from consolidated match results.
Works for any company list — uses consolidated_matches.csv as input.

Charts produced:
  C01 — Top 20 entities/groups by GGE (horizontal bar)
  C02 — By granting country (top 15, grants vs loans split)
  C03 — By data source (bar)
  C04 — By financial instrument (donut)
  C05 — By MFF period (stacked bar by instrument)
  C06 — Annual time series (stacked area by instrument)
  C07 — Face value vs GGE comparison (top 10)
  C08 — Match quality distribution (donut)

Usage:
  from src.visualisations.summary_charts import generate_summary_charts
  generate_summary_charts(consolidated_csv, output_dir)
"""

import pandas as pd
import numpy as np
import logging
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
log = logging.getLogger(__name__)

# MFF periods
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


def _try_png(fig, path):
    """Try to write PNG, skip if kaleido not available."""
    try:
        fig.write_image(str(path))
    except Exception:
        pass


def generate_summary_charts(
    consolidated_csv: Path,
    output_dir: Path,
    prefix: str = 'match',
) -> list[str]:
    """Generate generic summary charts from consolidated match results.

    Parameters
    ----------
    consolidated_csv : Path
        Path to consolidated_matches.csv.
    output_dir : Path
        Directory to save charts (HTML + PNG).
    prefix : str
        Column prefix for reference_name column.

    Returns
    -------
    list[str]
        Names of charts generated.
    """
    try:
        import plotly.graph_objects as go
        import plotly.express as px
    except ImportError:
        log.warning("plotly not installed — skipping chart generation")
        return []

    consolidated_csv = Path(consolidated_csv)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not consolidated_csv.exists():
        log.error(f"Consolidated CSV not found: {consolidated_csv}")
        return []

    df = pd.read_csv(consolidated_csv, low_memory=False)
    log.info(f"Generating summary charts from {len(df):,} rows...")

    # Determine key columns
    ref_col = f'{prefix}_reference_name'
    if ref_col not in df.columns:
        for candidate in ['automotive_reference_name', 'match_reference_name', 'reference_name']:
            if candidate in df.columns:
                ref_col = candidate
                break

    group_col = 'parent_group' if 'parent_group' in df.columns else ref_col
    has_gge = 'amount_gge' in df.columns

    charts = []

    # Bruegel-style colors
    BLUE = '#003299'
    ORANGE = '#F5A623'
    GREEN = '#4CAF50'
    RED = '#D32F2F'
    GREY = '#9E9E9E'
    PALETTE = ['#003299', '#F5A623', '#4CAF50', '#D32F2F', '#9C27B0',
               '#00BCD4', '#FF9800', '#795548', '#607D8B', '#E91E63']

    # ---- C01: Top 20 by GGE (or face value) ----
    amount_col = 'amount_gge' if has_gge else 'amount_eur'
    amount_label = 'GGE' if has_gge else 'Face Value'
    top20 = df.groupby(group_col)[amount_col].sum().sort_values(ascending=False).head(20)

    fig = go.Figure(go.Bar(
        y=top20.index[::-1],
        x=top20.values[::-1] / 1e9,
        orientation='h',
        marker_color=BLUE,
        text=[f'\u20ac{v/1e9:.1f}B' for v in top20.values[::-1]],
        textposition='outside',
    ))
    fig.update_layout(
        title=f'Top 20 by {amount_label} (EUR Billion)',
        xaxis_title='EUR Billion',
        height=600, width=900,
        margin=dict(l=250, r=80),
        font=dict(size=12),
    )
    fig.write_html(str(output_dir / 'C01_top20.html'))
    _try_png(fig, output_dir / 'C01_top20.png')
    charts.append('C01_top20')

    # ---- C02: By granting country (top 15) ----
    if 'country' in df.columns:
        country_data = df.groupby('country').agg(
            grants=('amount_eur', lambda x: x[df.loc[x.index, 'financial_instrument_class'] == 'grant'].sum()),
            loans=('amount_eur', lambda x: x[df.loc[x.index, 'financial_instrument_class'] == 'loan'].sum()),
            other=('amount_eur', lambda x: x[~df.loc[x.index, 'financial_instrument_class'].isin(['grant', 'loan'])].sum()),
        )
        country_data['total'] = country_data.sum(axis=1)
        country_data = country_data.sort_values('total', ascending=False).head(15)

        fig = go.Figure()
        fig.add_trace(go.Bar(y=country_data.index[::-1], x=country_data['grants'].values[::-1] / 1e9,
                             name='Grants', orientation='h', marker_color=GREEN))
        fig.add_trace(go.Bar(y=country_data.index[::-1], x=country_data['loans'].values[::-1] / 1e9,
                             name='Loans', orientation='h', marker_color=BLUE))
        fig.add_trace(go.Bar(y=country_data.index[::-1], x=country_data['other'].values[::-1] / 1e9,
                             name='Other', orientation='h', marker_color=GREY))
        fig.update_layout(
            barmode='stack', title='Top 15 Countries by Support Type (EUR B)',
            xaxis_title='EUR Billion', height=500, width=900,
            margin=dict(l=100, r=80),
        )
        fig.write_html(str(output_dir / 'C02_by_country.html'))
        _try_png(fig, output_dir / 'C02_by_country.png')
        charts.append('C02_by_country')

    # ---- C03: By data source ----
    source_eur = df.groupby('source')['amount_eur'].sum().sort_values(ascending=False)
    fig = go.Figure(go.Bar(
        x=source_eur.index,
        y=source_eur.values / 1e9,
        marker_color=PALETTE[:len(source_eur)],
        text=[f'\u20ac{v/1e9:.1f}B' for v in source_eur.values],
        textposition='outside',
    ))
    fig.update_layout(
        title='Support by Data Source (EUR B)',
        yaxis_title='EUR Billion', height=450, width=800,
    )
    fig.write_html(str(output_dir / 'C03_by_source.html'))
    _try_png(fig, output_dir / 'C03_by_source.png')
    charts.append('C03_by_source')

    # ---- C04: By financial instrument (donut) ----
    inst_eur = df.groupby('financial_instrument_class')['amount_eur'].sum().sort_values(ascending=False)
    inst_colors = {
        'grant': GREEN, 'loan': BLUE, 'equity': '#9C27B0',
        'guarantee': ORANGE, 'other': GREY, 'mixed': '#795548',
        'tax_advantage': '#FF9800',
    }
    fig = go.Figure(go.Pie(
        labels=inst_eur.index,
        values=inst_eur.values,
        hole=0.4,
        marker_colors=[inst_colors.get(x, GREY) for x in inst_eur.index],
        textinfo='label+percent',
    ))
    fig.update_layout(title='Financial Instrument Composition', height=500, width=700)
    fig.write_html(str(output_dir / 'C04_by_instrument.html'))
    _try_png(fig, output_dir / 'C04_by_instrument.png')
    charts.append('C04_by_instrument')

    # ---- C05: By MFF period (stacked bar by instrument) ----
    if 'year' in df.columns:
        df_yr = df[df['year'].notna()].copy()
        df_yr['mff'] = df_yr['year'].apply(_assign_mff)
        mff_inst = df_yr.groupby(['mff', 'financial_instrument_class'])['amount_eur'].sum().reset_index()
        mff_pivot = mff_inst.pivot_table(index='mff', columns='financial_instrument_class',
                                          values='amount_eur', fill_value=0)
        mff_order = [p for p in MFF_PERIODS.keys() if p in mff_pivot.index]
        mff_pivot = mff_pivot.loc[[p for p in mff_order if p in mff_pivot.index]]

        fig = go.Figure()
        for col in mff_pivot.columns:
            fig.add_trace(go.Bar(
                x=mff_pivot.index, y=mff_pivot[col] / 1e9,
                name=col, marker_color=inst_colors.get(col, GREY),
            ))
        fig.update_layout(
            barmode='stack', title='Support by MFF Period & Instrument (EUR B)',
            yaxis_title='EUR Billion', height=450, width=800,
        )
        fig.write_html(str(output_dir / 'C05_by_mff_period.html'))
        _try_png(fig, output_dir / 'C05_by_mff_period.png')
        charts.append('C05_by_mff_period')

    # ---- C06: Annual time series (stacked area by instrument) ----
    if 'year' in df.columns:
        df_yr = df[df['year'].notna()].copy()
        yr_inst = df_yr.groupby(['year', 'financial_instrument_class'])['amount_eur'].sum().reset_index()
        yr_pivot = yr_inst.pivot_table(index='year', columns='financial_instrument_class',
                                        values='amount_eur', fill_value=0)
        fig = go.Figure()
        for col in yr_pivot.columns:
            fig.add_trace(go.Scatter(
                x=yr_pivot.index, y=yr_pivot[col] / 1e9,
                name=col, stackgroup='one', mode='lines',
                line_color=inst_colors.get(col, GREY),
            ))
        fig.update_layout(
            title='Annual Support by Instrument (EUR B)',
            xaxis_title='Year', yaxis_title='EUR Billion',
            height=450, width=900,
        )
        fig.write_html(str(output_dir / 'C06_annual_timeseries.html'))
        _try_png(fig, output_dir / 'C06_annual_timeseries.png')
        charts.append('C06_annual_timeseries')

    # ---- C07: Face value vs GGE comparison (top 10) ----
    if has_gge:
        top10_face = df.groupby(group_col)['amount_eur'].sum().sort_values(ascending=False).head(10)
        top10_gge = df.groupby(group_col)['amount_gge'].sum().loc[top10_face.index]

        fig = go.Figure()
        fig.add_trace(go.Bar(
            y=top10_face.index[::-1], x=top10_face.values[::-1] / 1e9,
            name='Face Value', orientation='h', marker_color=BLUE,
        ))
        fig.add_trace(go.Bar(
            y=top10_gge.index[::-1], x=top10_gge.values[::-1] / 1e9,
            name='GGE', orientation='h', marker_color=ORANGE,
        ))
        fig.update_layout(
            barmode='group', title='Top 10: Face Value vs GGE (EUR B)',
            xaxis_title='EUR Billion', height=500, width=900,
            margin=dict(l=250, r=80),
        )
        fig.write_html(str(output_dir / 'C07_face_vs_gge.html'))
        _try_png(fig, output_dir / 'C07_face_vs_gge.png')
        charts.append('C07_face_vs_gge')

    # ---- C08: Match quality distribution ----
    if 'match_quality' in df.columns:
        quality_counts = df['match_quality'].value_counts()
        fig = go.Figure(go.Pie(
            labels=quality_counts.index,
            values=quality_counts.values,
            hole=0.4,
            textinfo='label+percent',
        ))
        fig.update_layout(title='Match Quality Distribution', height=450, width=600)
        fig.write_html(str(output_dir / 'C08_match_quality.html'))
        _try_png(fig, output_dir / 'C08_match_quality.png')
        charts.append('C08_match_quality')

    log.info(f"Generated {len(charts)} charts: {', '.join(charts)}")
    return charts


def main():
    """CLI entry point — generate charts from consolidated_matches.csv."""
    import argparse
    logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')

    parser = argparse.ArgumentParser(description='Generate summary charts from consolidated matches')
    parser.add_argument('consolidated_csv', help='Path to consolidated_matches.csv')
    parser.add_argument('--output-dir', '-o', default=None, help='Output directory for charts')
    parser.add_argument('--prefix', default='match', help='Column prefix (default: match)')
    args = parser.parse_args()

    csv_path = Path(args.consolidated_csv)
    out = Path(args.output_dir) if args.output_dir else csv_path.parent / 'charts'
    generate_summary_charts(csv_path, out, prefix=args.prefix)


if __name__ == '__main__':
    main()
