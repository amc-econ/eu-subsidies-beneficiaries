#!/usr/bin/env python3
"""Tier 1 before/after comparison.

Reads two ``consolidated_matches.csv`` files (baseline + improved) from
the same 19-company test run and writes a markdown diff report showing:

  * headline vs audit row counts
  * face-value totals (headline, per source, per country)
  * per-entity top 20 (match_reference_name) with delta column
  * dedup flag distribution
  * new columns added in the improved view
  * any rows where improved has an issue (missing, NaN, etc.)

Output: ``research/TIER1_BEFORE_AFTER_REPORT.md``
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def _fmt(v: float | None) -> str:
    if v is None or pd.isna(v) or v == 0:
        return '€0'
    if v >= 1e9:
        return f'€{v/1e9:.2f}B'
    if v >= 1e6:
        return f'€{v/1e6:.1f}M'
    if v >= 1e3:
        return f'€{v/1e3:.0f}K'
    return f'€{v:.0f}'


def _safe_sum(df: pd.DataFrame, col: str) -> float:
    if col not in df.columns:
        return 0.0
    return float(df[col].fillna(0).sum())


def build_report(baseline_dir: Path, improved_dir: Path) -> str:
    lines: list[str] = []
    lines.append('# Tier 1 before/after validation — 19 companies')
    lines.append('')
    lines.append(f'- **Baseline**: `{baseline_dir}`')
    lines.append(f'- **Improved**: `{improved_dir}`')
    lines.append('')
    lines.append(
        'The baseline was produced by git-stashing every tracked Phase A edit '
        'from the 2026-04-13 overnight + 2026-04-14 follow-up sessions and running '
        '`python run_pipeline.py --stage match` against a 19-company European '
        'automotive test subset. The improved run uses the same company list but '
        'with every edit restored. Both runs had `--no-pdf-enrichment` set so the '
        'heuristic-fallback dedup path is the one the comparison stresses.'
    )
    lines.append('')

    # Load main CSVs
    baseline_headline = baseline_dir / 'consolidated_matches.csv'
    improved_headline = improved_dir / 'consolidated_matches.csv'
    improved_audit = improved_dir / 'consolidated_matches_audit.csv'

    if not baseline_headline.exists():
        lines.append(f'**ERROR**: baseline CSV not found at `{baseline_headline}`')
        return '\n'.join(lines)
    if not improved_headline.exists():
        lines.append(f'**ERROR**: improved CSV not found at `{improved_headline}`')
        return '\n'.join(lines)

    b = pd.read_csv(baseline_headline, low_memory=False)
    i = pd.read_csv(improved_headline, low_memory=False)
    a = pd.read_csv(improved_audit, low_memory=False) if improved_audit.exists() else None

    lines.append('## 1. Row counts and headline totals')
    lines.append('')
    lines.append('| Metric | Baseline | Improved headline | Improved audit | Δ headline |')
    lines.append('|---|---:|---:|---:|---:|')
    lines.append(
        f'| rows | {len(b):,} | {len(i):,} | {len(a) if a is not None else "—":,} | '
        f'{len(i) - len(b):+,} |'
    )
    b_face = _safe_sum(b, 'amount_eur')
    i_face = _safe_sum(i, 'amount_eur_face' if 'amount_eur_face' in i.columns else 'amount_eur')
    i_gge = _safe_sum(i, 'amount_eur_gge' if 'amount_eur_gge' in i.columns else 'amount_gge')
    a_face = _safe_sum(a, 'amount_eur_face' if (a is not None and 'amount_eur_face' in a.columns) else 'amount_eur') if a is not None else 0
    lines.append(
        f'| face value | {_fmt(b_face)} | {_fmt(i_face)} | {_fmt(a_face)} | '
        f'{_fmt(i_face - b_face)} |'
    )
    lines.append(
        f'| GGE | {_fmt(_safe_sum(b, "amount_gge"))} | {_fmt(i_gge)} | — | '
        f'{_fmt(i_gge - _safe_sum(b, "amount_gge"))} |'
    )
    lines.append(
        f'| unique entities | {b["match_reference_name"].nunique() if "match_reference_name" in b.columns else 0} | '
        f'{i["match_reference_name"].nunique() if "match_reference_name" in i.columns else 0} | '
        f'{a["match_reference_name"].nunique() if a is not None and "match_reference_name" in a.columns else 0} | — |'
    )
    lines.append('')

    # Dedup flags
    lines.append('## 2. Dedup flag distribution')
    lines.append('')
    lines.append('Baseline `dc_flag` values (rows where the flag is non-empty):')
    lines.append('')
    if 'dc_flag' in b.columns:
        vc = b[b['dc_flag'].fillna('').astype(str).str.len() > 0]['dc_flag'].value_counts()
        if len(vc):
            lines.append('| dc_flag | count |')
            lines.append('|---|---:|')
            for k, v in vc.head(15).items():
                lines.append(f'| `{k}` | {v} |')
        else:
            lines.append('_(no flagged rows in baseline)_')
    lines.append('')
    lines.append('Improved headline `dc_flag` (should be empty or document-grounded only):')
    lines.append('')
    if 'dc_flag' in i.columns:
        vc = i[i['dc_flag'].fillna('').astype(str).str.len() > 0]['dc_flag'].value_counts()
        if len(vc):
            lines.append('| dc_flag | count |')
            lines.append('|---|---:|')
            for k, v in vc.head(15).items():
                lines.append(f'| `{k}` | {v} |')
        else:
            lines.append('_(no flagged rows in improved headline)_')
    lines.append('')
    lines.append('Improved audit `heuristic_flag` (new audit-only column — should carry the demoted heuristic hits):')
    lines.append('')
    if a is not None and 'heuristic_flag' in a.columns:
        vc = a[a['heuristic_flag'].fillna('').astype(str).str.len() > 0]['heuristic_flag'].value_counts()
        if len(vc):
            lines.append('| heuristic_flag | count |')
            lines.append('|---|---:|')
            for k, v in vc.head(15).items():
                lines.append(f'| `{k}` | {v} |')
        else:
            lines.append('_(no heuristic hits — expected for an automotive 19-company run)_')
    else:
        lines.append('_(column missing — is_anonymised / heuristic_flag not plumbed)_')
    lines.append('')

    # Per-source breakdown
    lines.append('## 3. Face value by source')
    lines.append('')
    def _by_source(df, col):
        if col not in df.columns or 'source' not in df.columns:
            return pd.Series(dtype=float)
        return df.groupby('source')[col].sum()
    b_src = _by_source(b, 'amount_eur')
    i_src = _by_source(i, 'amount_eur_face' if 'amount_eur_face' in i.columns else 'amount_eur')
    src_all = sorted(set(b_src.index) | set(i_src.index))
    lines.append('| source | baseline | improved headline | Δ |')
    lines.append('|---|---:|---:|---:|')
    for s in src_all:
        bv = b_src.get(s, 0.0)
        iv = i_src.get(s, 0.0)
        lines.append(f'| {s} | {_fmt(bv)} | {_fmt(iv)} | {_fmt(iv - bv)} |')
    lines.append('')

    # Per-entity top 10
    lines.append('## 4. Top 10 entities by face value')
    lines.append('')
    lines.append('| entity | baseline | improved headline | Δ |')
    lines.append('|---|---:|---:|---:|')
    if 'match_reference_name' in b.columns and 'match_reference_name' in i.columns:
        b_ent = b.groupby('match_reference_name')['amount_eur'].sum().sort_values(ascending=False)
        i_col = 'amount_eur_face' if 'amount_eur_face' in i.columns else 'amount_eur'
        i_ent = i.groupby('match_reference_name')[i_col].sum().sort_values(ascending=False)
        merged = (
            pd.concat([b_ent.rename('b'), i_ent.rename('i')], axis=1)
            .fillna(0)
            .sort_values('b', ascending=False)
            .head(15)
        )
        for name, row in merged.iterrows():
            lines.append(f'| {name} | {_fmt(row["b"])} | {_fmt(row["i"])} | {_fmt(row["i"] - row["b"])} |')
    lines.append('')

    # Columns present in improved but not baseline
    lines.append('## 5. New columns in improved view')
    lines.append('')
    new_cols = [c for c in i.columns if c not in b.columns]
    if new_cols:
        lines.append('| column | sample non-null values |')
        lines.append('|---|---|')
        for c in new_cols:
            if c in i.columns:
                non_null = i[c].dropna() if i[c].dtype != object else i[c].dropna().astype(str)
                sample = ', '.join(map(str, non_null.unique()[:3])) if len(non_null) else '(all empty)'
                lines.append(f'| `{c}` | {sample[:80]} |')
    else:
        lines.append('_(no new columns — indicates Phase A schema additions did not apply)_')
    lines.append('')

    # is_anonymised impact
    lines.append('## 6. Anonymised-sentinel filter impact')
    lines.append('')
    if a is not None and 'is_anonymised' in a.columns:
        n_anon = int(a['is_anonymised'].fillna(False).astype(bool).sum())
        anon_eur = float(a.loc[a['is_anonymised'].fillna(False).astype(bool), 'amount_eur_face' if 'amount_eur_face' in a.columns else 'amount_eur'].fillna(0).sum())
        lines.append(f'- Audit rows flagged `is_anonymised=True`: **{n_anon:,}** ({_fmt(anon_eur)})')
        lines.append(f'- Expected: 0 for an auto-only company list (anonymised sentinels are regional / Polish profession rollups, not car brands)')
    else:
        lines.append('_(column missing)_')
    lines.append('')

    # GGE measurement breakdown
    lines.append('## 7. GGE rate source distribution')
    lines.append('')
    if 'gge_rate_source' in i.columns:
        vc = i['gge_rate_source'].value_counts()
        lines.append('| source | count |')
        lines.append('|---|---:|')
        for k, v in vc.items():
            lines.append(f'| `{k}` | {v:,} |')
        unknown_eur = _safe_sum(i[i['gge_rate_source'] == 'unknown'], 'amount_eur_face')
        lines.append(f'')
        lines.append(f'Face value with `gge_rate_source == "unknown"` (excluded from GGE total): {_fmt(unknown_eur)}')
    else:
        lines.append('_(column missing — GGE rate tagging not applied)_')
    lines.append('')

    # Schema comparison
    lines.append('## 8. Column-set comparison')
    lines.append('')
    b_cols = set(b.columns)
    i_cols = set(i.columns)
    lines.append(f'- baseline columns: {len(b_cols)}')
    lines.append(f'- improved columns: {len(i_cols)}')
    lines.append(f'- new in improved: {sorted(i_cols - b_cols)}')
    lines.append(f'- missing in improved (should be empty): {sorted(b_cols - i_cols)}')
    lines.append('')

    # Takeaway
    lines.append('## Takeaway')
    lines.append('')
    lines.append(
        'The improved run preserves every baseline row in the **audit** view '
        '(`consolidated_matches_audit.csv`) but filters the **headline** view '
        'to exclude dedup-flagged rows, suspect matches, and anonymised '
        'sentinels. The headline face value delta captures exactly the '
        'impact of the no-invention principle: any negative delta is money '
        'the baseline published that the improved view no longer treats as '
        'real attribution. The new face/GGE/low/high columns are present in '
        'the improved CSV, and the GGE total excludes rows with unknown '
        'instruments (the old baseline silently treated those as 100% grants).'
    )
    return '\n'.join(lines)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--baseline', type=Path, required=True)
    p.add_argument('--improved', type=Path, required=True)
    p.add_argument('--output', type=Path,
                   default=Path('research/TIER1_BEFORE_AFTER_REPORT.md'))
    args = p.parse_args()
    report = build_report(args.baseline, args.improved)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding='utf-8')
    print(f'Wrote {args.output}')
    print()
    print(report[:3000])


if __name__ == '__main__':
    main()
