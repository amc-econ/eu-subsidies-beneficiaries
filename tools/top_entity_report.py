#!/usr/bin/env python3
"""Per-entity richness report from a consolidated_matches.csv.

Reads either ``consolidated_matches.csv`` (headline) or
``consolidated_matches_audit.csv`` and prints — for the top N
entities by `amount_eur_face` — every source-specific extra field
that was packed into `extra_fields_json` during harmonization or
enrichment.

This is the first downstream consumer of the schema-v3
`extra_fields_json` column. The column was added so that
harmonizers could ship arbitrarily rich per-row metadata
(EIB project prose, CORDIS topic codes, IPCEI bracket bounds,
SA decision structured extracts, Italia Domani CUP + NUTS-3
location, …) without bloating the canonical schema. Until now
nothing read it. This tool proves the round-trip works and gives
users a way to see *why* a row ended up in their top-N list.

Usage
-----
    python tools/top_entity_report.py \
        --input data/processed/match_output/automotive/consolidated_matches.csv \
        --top 30
    python tools/top_entity_report.py \
        --input data/processed/match_output/automotive/consolidated_matches.csv \
        --group-by match_reference_name \
        --top 20 \
        --output tests/reports/top_entities.md

The report prints a markdown table with one section per entity
showing its top 10 rows by `amount_eur_face`, the source, year,
and every non-empty key from the extras JSON blob.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _load_extras(raw: object) -> dict:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return {}
    s = str(raw).strip()
    if not s or s == '{}':
        return {}
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else {}
    except json.JSONDecodeError:
        return {}


def _fmt_eur(v: float | None) -> str:
    if v is None or pd.isna(v):
        return '—'
    if v >= 1e9:
        return f'€{v/1e9:.2f}B'
    if v >= 1e6:
        return f'€{v/1e6:.1f}M'
    if v >= 1e3:
        return f'€{v/1e3:.0f}K'
    return f'€{v:.0f}'


def build_report(df: pd.DataFrame, *, group_by: str, top: int) -> str:
    amount_col = 'amount_eur_face' if 'amount_eur_face' in df.columns else 'amount_eur'
    if group_by not in df.columns:
        raise SystemExit(f'group-by column not present: {group_by}')
    if amount_col not in df.columns:
        raise SystemExit(f'amount column not present: {amount_col}')

    # Aggregate.
    totals = (
        df.groupby(group_by)[amount_col]
        .sum()
        .sort_values(ascending=False)
        .head(top)
    )

    lines: list[str] = []
    lines.append(f'# Top-{top} entities by {amount_col}\n')
    lines.append(f'Source CSV: `{df.attrs.get("source_path", "(unknown)")}`\n')
    lines.append(f'Total rows in input: {len(df):,}\n')
    lines.append(
        f'Sources present: {", ".join(sorted(df["source"].dropna().unique()))}\n'
    )
    lines.append('')

    for rank, (entity, total) in enumerate(totals.items(), start=1):
        rows = df[df[group_by] == entity].sort_values(
            amount_col, ascending=False
        ).head(10)
        n_rows = len(df[df[group_by] == entity])
        n_sources = df[df[group_by] == entity]['source'].nunique()
        lines.append(f'## {rank}. {entity}')
        lines.append(
            f'Total face value: **{_fmt_eur(total)}** '
            f'({n_rows} rows across {n_sources} source(s))'
        )
        lines.append('')
        lines.append(
            '| source | year | country | amount | instrument | extras'
            ' |'
        )
        lines.append('|---|---|---|---|---|---|')
        for _, r in rows.iterrows():
            extras = _load_extras(r.get('extra_fields_json'))
            extras_summary: list[str] = []
            for k, v in sorted(extras.items()):
                if v in (None, '', [], {}):
                    continue
                sv = str(v)
                if len(sv) > 60:
                    sv = sv[:57] + '…'
                extras_summary.append(f'`{k}`: {sv}')
            lines.append(
                '| {src} | {yr} | {co} | {amt} | {inst} | {extras} |'.format(
                    src=r.get('source', '—'),
                    yr=r.get('year', '—'),
                    co=r.get('country', '—'),
                    amt=_fmt_eur(r.get(amount_col)),
                    inst=r.get('financial_instrument_class', '—'),
                    extras=' · '.join(extras_summary[:5]) or '—',
                )
            )
        lines.append('')
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--input', type=Path, required=True,
                   help='consolidated_matches.csv or audit CSV')
    p.add_argument('--group-by', default='match_reference_name',
                   help='Column to aggregate on (default: match_reference_name)')
    p.add_argument('--top', type=int, default=20, help='Top N entities (default 20)')
    p.add_argument('--output', type=Path, default=None,
                   help='Optional: write to markdown file')
    args = p.parse_args()

    df = pd.read_csv(args.input, low_memory=False)
    df.attrs['source_path'] = str(args.input)

    report = build_report(df, group_by=args.group_by, top=args.top)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding='utf-8')
        print(f'Wrote {args.output}')
    else:
        print(report)


if __name__ == '__main__':
    main()
