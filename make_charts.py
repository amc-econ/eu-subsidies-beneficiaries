#!/usr/bin/env python3
"""Make a few good-looking charts from a match_companies.py run.

This is optional and never runs automatically. After you have run

    python src/match_companies.py --company-list my_companies.csv

run

    python make_charts.py

to render a handful of clean summary charts (PNG) into the run's `charts/`
folder. It reads the small, ready-made T*.csv summary tables that the match
step already writes, so it is fast and easy to change.

The style is deliberately editorial - direct value labels, no gridlines or
boxed-in axes, a single colour ramp - so the output looks presentable rather
than like a default plot. Want different charts? This file is meant to be
copied and edited: each chart below is a few lines on a pandas DataFrame, and
the full row-level data is in consolidated_matches.csv if you want to plot
something else entirely. Point it at a custom output directory with:

    python make_charts.py path/to/output_dir

Needs matplotlib:  pip install matplotlib
"""

import argparse
import sys
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent
DEFAULT_DIR = REPO_ROOT / 'data' / 'processed' / 'match_output'

TOP_N = 15
ACCENT = '#1f4e79'       # deep editorial blue (single-series charts)
INK = '#222222'          # near-black for labels
MUTED = '#8a8a8a'        # grey for subtitles / units


def _money(x):
    """A short euro label: €12.3B / €450M / €12K / €900."""
    a = abs(x)
    if a >= 1e9:
        return f'€{x / 1e9:.1f}B'
    if a >= 1e6:
        return f'€{x / 1e6:.0f}M'
    if a >= 1e3:
        return f'€{x / 1e3:.0f}K'
    return f'€{x:.0f}'


def _load(out_dir, name):
    """Read a summary table if present, else return None."""
    path = out_dir / name
    if not path.exists():
        print(f'  skip: {name} not found')
        return None
    return pd.read_csv(path)


def main():
    p = argparse.ArgumentParser(description='Render summary charts from a match run.')
    p.add_argument('output_dir', nargs='?', default=str(DEFAULT_DIR),
                   help='match output directory '
                        '(default: data/processed/match_output)')
    args = p.parse_args()

    # matplotlib is the only extra dependency, and only this script needs it.
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        print('Charts need matplotlib - run: pip install matplotlib')
        return 0

    out_dir = Path(args.output_dir).resolve()
    if not out_dir.exists():
        print(f'No such directory: {out_dir}')
        print('Run src/match_companies.py first.')
        return 1

    charts_dir = out_dir / 'charts'
    charts_dir.mkdir(parents=True, exist_ok=True)

    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Segoe UI', 'Helvetica Neue', 'Arial',
                            'Liberation Sans', 'DejaVu Sans'],
        'font.size': 11,
        'figure.dpi': 150,
        'savefig.dpi': 150,
        'figure.facecolor': 'white',
        'axes.facecolor': 'white',
        'text.color': INK,
    })
    blues = plt.get_cmap('Blues')
    made = []

    def _titles(ax, title, subtitle):
        ax.set_title(title, loc='left', fontsize=14.5, fontweight='bold',
                     color='#111111', pad=26)
        if subtitle:
            ax.text(0, 1.0, subtitle, transform=ax.transAxes, va='bottom',
                    ha='left', fontsize=10.5, color=MUTED)

    def _save(fig, fname):
        fig.savefig(charts_dir / fname, dpi=150, bbox_inches='tight',
                    facecolor='white')
        plt.close(fig)
        made.append(fname)
        print(f'  wrote {fname}')

    def ranked_bars(df, label_col, title, subtitle, fname):
        """Sorted horizontal bars with value labels and a colour ramp."""
        d = df.sort_values('total_eur', ascending=False).head(TOP_N)[::-1]
        vals = d['total_eur'].to_numpy()
        labels = d[label_col].astype(str).tolist()
        n = len(vals)
        vmax = vals.max() if n else 0
        # largest bar darkest, fading down the ranking
        colors = [blues(0.40 + 0.52 * (i / max(n - 1, 1))) for i in range(n)]
        fig, ax = plt.subplots(figsize=(9, max(3.2, 0.5 * n + 1.6)))
        ax.barh(range(n), vals, color=colors, edgecolor='none')
        for i, v in enumerate(vals):
            ax.text(v + vmax * 0.01, i, _money(v), va='center', ha='left',
                    fontsize=9.5, color=INK)
        ax.set_yticks(range(n))
        ax.set_yticklabels(labels, fontsize=10.5)
        ax.set_xlim(0, vmax * 1.18)
        ax.set_xticks([])
        for s in ax.spines.values():
            s.set_visible(False)
        ax.tick_params(length=0)
        ax.margins(y=0.01)
        _titles(ax, title, subtitle)
        _save(fig, fname)

    def year_bars(df, title, subtitle, fname):
        """Vertical time-series bars with value labels above each bar."""
        d = df.dropna(subset=['year']).sort_values('year')
        years = d['year'].astype(int).astype(str).tolist()
        vals = d['total_eur'].to_numpy()
        vmax = vals.max() if len(vals) else 0
        fig, ax = plt.subplots(figsize=(11, 6))
        ax.bar(range(len(vals)), vals, color=ACCENT, width=0.72, edgecolor='none')
        for i, v in enumerate(vals):
            ax.text(i, v + vmax * 0.015, _money(v), ha='center', va='bottom',
                    fontsize=9, color=INK)
        ax.set_xticks(range(len(vals)))
        ax.set_xticklabels(years, fontsize=10)
        ax.set_ylim(0, vmax * 1.12)
        ax.set_yticks([])
        for name in ('top', 'left', 'right'):
            ax.spines[name].set_visible(False)
        ax.spines['bottom'].set_color('#cccccc')
        ax.tick_params(length=0)
        _titles(ax, title, subtitle)
        _save(fig, fname)

    # 1) Support over time.
    t4 = _load(out_dir, 'T4_by_year.csv')
    if t4 is not None and {'year', 'total_eur'} <= set(t4.columns):
        year_bars(t4, 'Support by year', 'Annual total, all instruments (EUR)',
                  'annual_total.png')

    # 2) Top beneficiaries (first column is the entity / group name).
    t6 = _load(out_dir, 'T6_top_entities.csv')
    if t6 is not None and 'total_eur' in t6.columns:
        n = min(TOP_N, len(t6))
        ranked_bars(t6, t6.columns[0], f'Top {n} beneficiaries',
                    'Total support received (EUR)', 'top_beneficiaries.png')

    # 3) Support by data source.
    t1 = _load(out_dir, 'T1_by_source.csv')
    if t1 is not None and {'source', 'total_eur'} <= set(t1.columns):
        ranked_bars(t1, 'source', 'Support by data source',
                    'Total support (EUR)', 'by_source.png')

    # 4) Support by financial instrument.
    t3 = _load(out_dir, 'T3_by_instrument.csv')
    if t3 is not None and {'financial_instrument_class', 'total_eur'} <= set(t3.columns):
        ranked_bars(t3, 'financial_instrument_class', 'Support by instrument',
                    'Total support (EUR)', 'by_instrument.png')

    if made:
        print(f'\nDone. {len(made)} chart(s) in {charts_dir}')
    else:
        print(f'\nNo charts made - no summary tables found in {out_dir}.')
        print('Run src/match_companies.py first.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
