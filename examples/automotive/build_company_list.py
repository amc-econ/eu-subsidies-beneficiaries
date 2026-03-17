#!/usr/bin/env python3
"""
Build the automotive company list CSV from source files.

Combines Car_companies.xlsx and ev_volumes_company_list.csv, deduplicates,
and produces automotive_companies.csv in the company_lists/ directory.

The pre-built output is already included in the repo — this script only
needs to be re-run if you want to regenerate it from updated source files.

Usage:
    python -m examples.automotive.build_company_list

Output:
    examples/automotive/company_lists/automotive_companies.csv
"""

import pandas as pd
from pathlib import Path


def build_company_list(
    output_dir: Path | None = None,
) -> pd.DataFrame:
    """Combine source files into a single company_name CSV and save to output_dir."""
    lists_dir = Path(__file__).resolve().parent / 'company_lists'
    if output_dir is None:
        output_dir = lists_dir

    records = []

    # ORBIS (Car_companies.xlsx)
    orbis_path = lists_dir / 'Car_companies.xlsx'
    if orbis_path.exists():
        orbis = pd.read_excel(orbis_path, sheet_name='Results')
        for _, row in orbis.iterrows():
            name = row.get('Company name Latin alphabet', '')
            if pd.isna(name) or not str(name).strip():
                continue
            country = str(row.get('Country ISO code', '')).strip()[:2] if pd.notna(row.get('Country ISO code')) else ''
            records.append({
                'company_name': str(name).strip(),
                'country': country,
                'source': 'orbis',
            })
        print(f"ORBIS: {len(records)} companies loaded")

    # EV volumes
    ev_path = lists_dir / 'ev_volumes_company_list.csv'
    if ev_path.exists():
        ev = pd.read_csv(ev_path)
        existing = {r['company_name'].lower() for r in records}
        ev_count = 0
        for name in ev['cleaned_name'].dropna().unique():
            if str(name).strip().lower() not in existing:
                records.append({
                    'company_name': str(name).strip(),
                    'country': '',
                    'source': 'ev_volumes',
                })
                existing.add(str(name).strip().lower())
                ev_count += 1
        print(f"EV volumes: {ev_count} new companies added")

    df = pd.DataFrame(records)
    out_path = output_dir / 'automotive_companies.csv'
    df.to_csv(out_path, index=False, encoding='utf-8')
    print(f"Saved: {len(df)} companies -> {out_path}")
    return df


if __name__ == '__main__':
    build_company_list()
