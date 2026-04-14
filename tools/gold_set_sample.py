#!/usr/bin/env python3
"""Gold-set sampling harness for the entity matcher.

Given an existing ``match_log.csv`` from a pipeline run, samples a
stratified subset of matched rows for manual labelling. Output is a
CSV with an empty ``expected_correct`` column that a reviewer fills
in with ``1`` / ``0`` / ``unclear``. The resulting labelled file is
fed to :mod:`tools.matcher_report` for precision / recall reporting
per layer and per source.

Stratification
--------------
The default sample is 1,500 rows split across:

    * 5 sources (TAM, KOHESIO, FTS, EIB, EBRD)
    * 3 match layers (exact / fuzzy / contextual)
    * 4 amount quartiles

Each cell gets ``target // n_cells`` rows (rounded down) plus some
hard-case categories bolted on:

    * suspect_description_only
    * suspect_eib_title
    * fuzzy_medium matches at or near the threshold
    * short reference names (≤ 8 chars)

The output carries enough columns for the reviewer to make a judgment
(``entity_name_raw``, ``source``, ``country``, ``year``, ``amount_eur``,
``match_reference_name``, ``match_type``, ``match_score``) and an
``evidence_hint`` column with a per-row URL back to the source if one
can be constructed.

Usage
-----
    python tools/gold_set_sample.py \\
        --match-log data/processed/match_output/<prefix>/match_log.csv \\
        --output tests/gold/gold_set_v1.csv \\
        --n 1500 --seed 42
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


SOURCES = ('TAM', 'KOHESIO', 'FTS', 'EIB', 'EBRD')
LAYERS = ('exact', 'fuzzy', 'contextual')
QUARTILES = 4

HARD_CASE_BUCKETS = (
    'suspect_description_only',
    'suspect_eib_title',
    'fuzzy_medium_low_score',
    'short_reference',
)

SAMPLE_COLUMNS = [
    'sa_case',
    'source',
    'source_record_id',
    'entity_name_raw',
    'entity_name_clean',
    'country',
    'year',
    'amount_eur',
    'match_reference_name',
    'match_type',
    'match_score',
    'match_quality',
    'description',
    'stratum',
    'evidence_hint',
    'expected_correct',  # filled in by reviewer: '1' / '0' / 'unclear'
    'reviewer_notes',
]


def _classify_layer(match_type: str) -> str:
    mt = (match_type or '').lower()
    if mt == 'exact':
        return 'exact'
    if mt.startswith('fuzzy'):
        return 'fuzzy'
    if 'contextual' in mt or 'eib_title' in mt or 'description' in mt:
        return 'contextual'
    return 'other'


def _amount_quartile(amount: float, edges: np.ndarray) -> int:
    if pd.isna(amount) or amount <= 0:
        return -1
    for i, edge in enumerate(edges):
        if amount <= edge:
            return i
    return len(edges)


def _build_evidence_hint(row) -> str:
    """Return a URL or identifier the reviewer can click to verify the match."""
    src = str(row.get('source', '')).upper()
    sid = str(row.get('source_record_id', '')).strip()
    sa = str(row.get('sa_case', '')).strip()
    if src == 'EIB' and sid.isdigit():
        return f'https://www.eib.org/en/projects/all/{sid}'
    if src == 'TAM' and sa.startswith('SA.'):
        code = sa.replace('.', '')
        return f'https://ec.europa.eu/competition/state_aid/register/ii/by_case_nr_{code[2:]}.html'
    return ''


def sample(
    match_log_path: Path,
    n: int,
    seed: int,
    output: Path,
) -> None:
    log = match_log_path
    df = pd.read_csv(log, low_memory=False)
    print(f'  loaded {len(df):,} rows from {log}')

    df['layer'] = df['match_type'].apply(_classify_layer)

    # Quartile edges across the whole frame (log-spaced would be nicer
    # but quartile is plenty for stratification).
    amounts = df['amount_eur'].dropna().values
    if len(amounts) > 0:
        edges = np.quantile(amounts[amounts > 0], [0.25, 0.50, 0.75])
    else:
        edges = np.array([1e5, 1e6, 1e7])
    df['quartile'] = df['amount_eur'].apply(lambda a: _amount_quartile(a, edges))
    df['stratum'] = df['source'] + '/' + df['layer'] + '/q' + df['quartile'].astype(str)

    rng = np.random.default_rng(seed)

    cells: list[pd.DataFrame] = []
    # Primary stratified sample: source × layer × quartile.
    n_cells = len(SOURCES) * len(LAYERS) * QUARTILES
    per_cell = max(1, (n - 200) // n_cells)  # leave 200 for hard cases
    for src in SOURCES:
        for layer in LAYERS:
            for q in range(QUARTILES):
                stratum = f'{src}/{layer}/q{q}'
                sub = df[df['stratum'] == stratum]
                take = min(per_cell, len(sub))
                if take:
                    cells.append(sub.sample(n=take, random_state=int(rng.integers(1 << 30))))

    # Hard-case sampling (top-up regardless of primary stratification).
    hard_targets = {
        'suspect_description_only': 50,
        'suspect_eib_title': 50,
        'fuzzy_medium_low_score': 50,
        'short_reference': 50,
    }
    hard_frames: list[pd.DataFrame] = []
    if 'match_quality' in df.columns:
        sd = df[df['match_quality'] == 'suspect_description_only']
        hard_frames.append(sd.sample(n=min(hard_targets['suspect_description_only'], len(sd)),
                                     random_state=int(rng.integers(1 << 30))))
        se = df[df['match_quality'] == 'suspect_eib_title']
        hard_frames.append(se.sample(n=min(hard_targets['suspect_eib_title'], len(se)),
                                     random_state=int(rng.integers(1 << 30))))
    fm = df[(df['match_type'] == 'fuzzy_medium') & (df['match_score'].between(75, 80))]
    hard_frames.append(fm.sample(n=min(hard_targets['fuzzy_medium_low_score'], len(fm)),
                                 random_state=int(rng.integers(1 << 30))))
    if 'match_reference_name' in df.columns:
        short = df[df['match_reference_name'].astype(str).str.len() <= 8]
        hard_frames.append(short.sample(n=min(hard_targets['short_reference'], len(short)),
                                        random_state=int(rng.integers(1 << 30))))

    all_sampled = pd.concat(cells + hard_frames, ignore_index=True)
    # Drop duplicates on source_record_id (hard-case bucket may overlap primary).
    if 'source_record_id' in all_sampled.columns:
        all_sampled = all_sampled.drop_duplicates(subset=['source_record_id', 'source'])
    print(f'  sampled {len(all_sampled):,} unique rows (target {n})')

    # Trim or pad.
    if len(all_sampled) > n:
        all_sampled = all_sampled.sample(n=n, random_state=seed)
    else:
        deficit = n - len(all_sampled)
        if deficit > 0:
            leftover = df[~df.index.isin(all_sampled.index)]
            if len(leftover):
                extra = leftover.sample(n=min(deficit, len(leftover)), random_state=seed)
                all_sampled = pd.concat([all_sampled, extra], ignore_index=True)

    all_sampled['evidence_hint'] = all_sampled.apply(_build_evidence_hint, axis=1)
    all_sampled['expected_correct'] = ''
    all_sampled['reviewer_notes'] = ''

    cols = [c for c in SAMPLE_COLUMNS if c in all_sampled.columns]
    output.parent.mkdir(parents=True, exist_ok=True)
    all_sampled[cols].to_csv(output, index=False)

    # Summary
    print(f'\nGold set v1 written to {output}')
    print(f'  rows: {len(all_sampled):,}')
    print(f'  strata covered: {all_sampled["stratum"].nunique()}')
    print(f'  sources: {all_sampled["source"].value_counts().to_dict()}')
    print(f'  layers:  {all_sampled["layer"].value_counts().to_dict()}')
    if 'match_quality' in all_sampled.columns:
        mq = all_sampled[all_sampled['match_quality'].isin(
            ['suspect_description_only', 'suspect_eib_title']
        )]
        print(f'  suspect rows (hard cases): {len(mq)}')
    print()
    print(
        'Label the expected_correct column as one of:\n'
        '  "1"       — correct match (beneficiary is the right entity)\n'
        '  "0"       — false positive (beneficiary is unrelated)\n'
        '  "unclear" — evidence insufficient, skip in metrics\n'
        f'Then run: python tools/matcher_report.py --gold {output} '
        f'--match-log {match_log_path}'
    )


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--match-log', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    p.add_argument('--n', type=int, default=1500)
    p.add_argument('--seed', type=int, default=42)
    args = p.parse_args()
    sample(args.match_log, args.n, args.seed, args.output)


if __name__ == '__main__':
    main()
