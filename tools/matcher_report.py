#!/usr/bin/env python3
"""Precision / recall / confusion matrix for a labelled gold set.

Consumes a labelled ``gold_set.csv`` (output of
:mod:`tools.gold_set_sample` after manual review) and the originating
``match_log.csv``, computes per-layer and per-source precision, and
writes:

    * ``stdout``      — a text report
    * ``gold_report.json``       — machine-readable metrics
    * ``gold_report.html``       — single-page HTML with the tables
      coloured by precision band

Only rows where ``expected_correct ∈ {'0', '1'}`` contribute to the
metrics. ``unclear`` rows are retained in the counts as "skipped".

Usage
-----
    python tools/matcher_report.py \\
        --gold tests/gold/gold_set_v1.csv \\
        --match-log data/processed/match_output/<prefix>/match_log.csv \\
        --output-dir tests/gold/reports/v1
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


LAYER_MAP = {
    'exact': 'Layer A (exact)',
    'fuzzy_high': 'Layer A (fuzzy_high)',
    'fuzzy_medium': 'Layer A (fuzzy_medium)',
    'contextual_exact': 'Layer B (contextual)',
    'eib_title_extraction': 'Layer B+ (IFI title)',
}


def _norm_label(val) -> str | None:
    if pd.isna(val):
        return None
    s = str(val).strip().lower()
    if s in ('1', 'true', 'yes', 'y', 'correct'):
        return 'correct'
    if s in ('0', 'false', 'no', 'n', 'incorrect', 'fp'):
        return 'incorrect'
    if s in ('unclear', 'skip', '?', ''):
        return 'unclear'
    return None


def _precision(tp: int, fp: int) -> float:
    denom = tp + fp
    return float(tp) / denom if denom else float('nan')


def _wilson_lower(tp: int, total: int, z: float = 1.96) -> float:
    """95% Wilson lower bound for a binomial proportion (precision)."""
    if total == 0:
        return float('nan')
    p = tp / total
    denom = 1 + z * z / total
    centre = p + z * z / (2 * total)
    margin = z * (((p * (1 - p) + z * z / (4 * total)) / total) ** 0.5)
    return (centre - margin) / denom


def build_report(gold_path: Path, match_log_path: Path) -> dict:
    gold = pd.read_csv(gold_path, low_memory=False)
    gold['_label'] = gold['expected_correct'].apply(_norm_label)
    print(
        f'  gold rows: {len(gold):,}  '
        f'labelled: {(gold["_label"].isin(["correct","incorrect"])).sum()}  '
        f'unclear: {(gold["_label"]=="unclear").sum()}  '
        f'unlabelled: {gold["_label"].isna().sum()}'
    )

    ml = pd.read_csv(match_log_path, low_memory=False)

    # Join gold labels to the match log on (source, source_record_id) so
    # we can correlate against the live match_type / match_score columns.
    join_cols = [c for c in ('source', 'source_record_id') if c in gold.columns and c in ml.columns]
    if not join_cols:
        print('  WARNING: no join columns — metrics will use gold file only')

    per_layer: dict[str, dict] = {}
    per_source_layer: dict[str, dict] = {}

    labelled = gold[gold['_label'].isin(['correct', 'incorrect'])].copy()
    labelled['_tp'] = (labelled['_label'] == 'correct').astype(int)
    labelled['_fp'] = (labelled['_label'] == 'incorrect').astype(int)

    # --- Per-layer ---
    for layer, layer_label in LAYER_MAP.items():
        sub = labelled[labelled['match_type'] == layer]
        tp = int(sub['_tp'].sum())
        fp = int(sub['_fp'].sum())
        per_layer[layer_label] = {
            'tp': tp, 'fp': fp, 'total': tp + fp,
            'precision': _precision(tp, fp),
            'wilson_lower_95': _wilson_lower(tp, tp + fp),
        }

    # --- Per source × layer ---
    if 'source' in labelled.columns:
        for source in sorted(labelled['source'].dropna().unique()):
            src_rows = labelled[labelled['source'] == source]
            per_source_layer[source] = {}
            for layer, layer_label in LAYER_MAP.items():
                sub = src_rows[src_rows['match_type'] == layer]
                tp = int(sub['_tp'].sum())
                fp = int(sub['_fp'].sum())
                per_source_layer[source][layer_label] = {
                    'tp': tp, 'fp': fp, 'total': tp + fp,
                    'precision': _precision(tp, fp),
                    'wilson_lower_95': _wilson_lower(tp, tp + fp),
                }

    # --- Overall ---
    overall_tp = int(labelled['_tp'].sum())
    overall_fp = int(labelled['_fp'].sum())
    overall = {
        'tp': overall_tp, 'fp': overall_fp, 'total': overall_tp + overall_fp,
        'precision': _precision(overall_tp, overall_fp),
        'wilson_lower_95': _wilson_lower(overall_tp, overall_tp + overall_fp),
    }

    return {
        'gold_path': str(gold_path),
        'match_log_path': str(match_log_path),
        'gold_size': int(len(gold)),
        'labelled': int((gold['_label'].isin(['correct', 'incorrect'])).sum()),
        'unclear': int((gold['_label'] == 'unclear').sum()),
        'unlabelled': int(gold['_label'].isna().sum()),
        'overall': overall,
        'per_layer': per_layer,
        'per_source_layer': per_source_layer,
    }


def print_text(report: dict) -> None:
    print()
    print('=' * 78)
    print('MATCHER GOLD-SET REPORT')
    print('=' * 78)
    print(f'gold set:  {report["gold_path"]}')
    print(f'match log: {report["match_log_path"]}')
    print(f'rows:      {report["gold_size"]:,} ({report["labelled"]:,} labelled, '
          f'{report["unclear"]:,} unclear, {report["unlabelled"]:,} unlabelled)')
    print()
    o = report['overall']
    print(f'OVERALL precision: {o["precision"]:.1%}  ({o["tp"]}/{o["total"]}) '
          f'[95% Wilson lower: {o["wilson_lower_95"]:.1%}]')
    print()
    print('Per layer:')
    for layer, metrics in report['per_layer'].items():
        if metrics['total'] == 0:
            print(f'  {layer:30s}  (no labelled rows)')
            continue
        p = metrics['precision']
        marker = '!' if (p < 0.9) else ' '
        print(f'{marker} {layer:30s}  precision={p:.1%} '
              f'({metrics["tp"]}/{metrics["total"]}) '
              f'[lower95={metrics["wilson_lower_95"]:.1%}]')
    print()
    print('Per source × layer:')
    for source, layers in report['per_source_layer'].items():
        print(f'  {source}:')
        for layer, metrics in layers.items():
            if metrics['total'] == 0:
                continue
            p = metrics['precision']
            marker = '!' if (p < 0.9) else ' '
            print(f'  {marker} {layer:28s}  precision={p:.1%} '
                  f'({metrics["tp"]}/{metrics["total"]})')
    print()


def write_html(report: dict, out: Path) -> None:
    def _row(name, m, indent=0):
        if m['total'] == 0:
            return ''
        p = m['precision']
        colour = '#b7e1cd' if p >= 0.95 else '#fce5cd' if p >= 0.85 else '#f4c7c3'
        pad = '&nbsp;' * (indent * 4)
        return (
            f'<tr style="background:{colour}"><td>{pad}{name}</td>'
            f'<td>{m["tp"]}/{m["total"]}</td>'
            f'<td>{p:.1%}</td>'
            f'<td>{m["wilson_lower_95"]:.1%}</td></tr>'
        )

    rows = [_row('OVERALL', report['overall'])]
    for layer, m in report['per_layer'].items():
        rows.append(_row(layer, m, indent=1))
    for source, layers in report['per_source_layer'].items():
        rows.append(f'<tr><td colspan="4"><b>{source}</b></td></tr>')
        for layer, m in layers.items():
            rows.append(_row(layer, m, indent=2))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Matcher gold report</title>
<style>body{{font-family:sans-serif;max-width:900px;margin:2em auto}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ccc;padding:6px 10px;text-align:left}}</style></head>
<body>
<h1>Matcher gold-set report</h1>
<p>gold set: <code>{report['gold_path']}</code><br>
match log: <code>{report['match_log_path']}</code><br>
rows: {report['gold_size']:,} ({report['labelled']:,} labelled,
{report['unclear']:,} unclear, {report['unlabelled']:,} unlabelled)</p>
<table>
<tr><th>cell</th><th>TP/total</th><th>precision</th><th>Wilson lower 95%</th></tr>
{''.join(rows)}
</table>
</body></html>"""
    out.write_text(html, encoding='utf-8')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--gold', required=True, type=Path)
    p.add_argument('--match-log', required=True, type=Path)
    p.add_argument('--output-dir', type=Path, default=Path('tests/gold/reports/latest'))
    args = p.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    report = build_report(args.gold, args.match_log)
    print_text(report)
    (args.output_dir / 'gold_report.json').write_text(
        json.dumps(report, indent=2), encoding='utf-8'
    )
    write_html(report, args.output_dir / 'gold_report.html')
    print(f'Wrote: {args.output_dir / "gold_report.json"}')
    print(f'Wrote: {args.output_dir / "gold_report.html"}')


if __name__ == '__main__':
    main()
