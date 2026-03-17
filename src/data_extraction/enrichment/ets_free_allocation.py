#!/usr/bin/env python3
"""
EU ETS Free Allocation — Generic Enrichment
==============================================
Extracts free allocation data from the EU ETS (EUTL) database for a given
company list.

The EU ETS gives free emission allowances to energy-intensive industries.
This script:

1. Loads the EUTL installation + compliance data from the ZIP download
2. Identifies matching installations via:
   a) Optional NACE code filter (e.g., '29' for motor vehicle manufacturing)
   b) Company name matching against a user-supplied company list CSV
3. Joins with yearly compliance data to get free allocation amounts
4. Converts allowances to EUR using average annual EUA spot prices
5. Outputs a company-level summary

Data source: https://euets.info/ (EUTL database, Oct 2024 snapshot)
ZIP: data/reference/eutl_2024_202410.zip

Usage:
  from src.data_extraction.enrichment.ets_free_allocation import run_ets_enrichment
  run_ets_enrichment('companies.csv', aliases_json='aliases.json', nace_filter='29')
"""

import pandas as pd
import numpy as np
import re
import sys
import json
import time
import logging
import zipfile
from pathlib import Path

from src.paths import RAW_DIR, ENRICHMENT_DIR, REPO_ROOT

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Look for EUTL ZIP in data/reference/ first, then data/raw/ for backwards compatibility
REFERENCE_DIR = REPO_ROOT / 'data' / 'reference'
EUTL_ZIP = REFERENCE_DIR / 'eutl_2024_202410.zip'
if not EUTL_ZIP.exists():
    EUTL_ZIP = RAW_DIR / 'eutl_2024_202410.zip'

# Average annual EUA spot prices (EUR/tCO2)
# Sources: ICE/ECX, Sandbag, Ember, market data aggregators
EUA_PRICES = {
    2005: 22, 2006: 18, 2007: 1,   # Phase 1 (collapsed in 2007)
    2008: 22, 2009: 13, 2010: 15, 2011: 13, 2012: 8,  # Phase 2
    2013: 4,  2014: 6,  2015: 8,  2016: 5,  2017: 6,  # Phase 3 early
    2018: 16, 2019: 25, 2020: 25,  # Phase 3 later
    2021: 53, 2022: 81, 2023: 85, 2024: 65,  # Phase 4
    2025: 70,  # Estimate
}


def _build_company_regex(company_list_csv, aliases_json=None):
    """Build a compiled regex matching all company names from the CSV and optional aliases."""
    df = pd.read_csv(company_list_csv)
    name_col = next((c for c in df.columns if 'name' in c.lower()), df.columns[0])
    names = df[name_col].dropna().str.strip().tolist()
    if aliases_json and Path(aliases_json).exists():
        with open(aliases_json) as f:
            aliases = json.load(f)
        for canonical, alias_list in aliases.items():
            names.extend(alias_list)
    patterns = sorted(set(n.lower() for n in names if len(n) > 2), key=len, reverse=True)
    patterns = [re.escape(p) for p in patterns]
    if not patterns:
        return re.compile(r'(?!)')
    return re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.I)


def _normalize_company(name: str, company_regex) -> str:
    """Extract a canonical company name from installation name using the company regex."""
    if pd.isna(name):
        return 'Unknown'
    s = name.strip()
    m = company_regex.search(s)
    if m:
        return m.group(0).strip()
    return s


def load_eutl_data():
    """Load installation + compliance data from EUTL ZIP."""
    if not EUTL_ZIP.exists():
        raise FileNotFoundError(f"EUTL ZIP not found: {EUTL_ZIP}")

    zf = zipfile.ZipFile(EUTL_ZIP)

    log.info("Loading EUTL data from ZIP...")
    inst = pd.read_csv(zf.open('installation.csv'), low_memory=False)
    comp = pd.read_csv(zf.open('compliance.csv'), low_memory=False)

    log.info(f"  Installations: {len(inst):,}")
    log.info(f"  Compliance rows: {len(comp):,}")

    return inst, comp


def identify_matching_installations(inst: pd.DataFrame, company_regex, nace_filter=None) -> pd.DataFrame:
    """Identify installations by NACE code filter and/or company name matching."""
    inst['nace_str'] = inst['nace_id'].astype(str)

    # Strategy 1: NACE filter (optional)
    if nace_filter is not None:
        nace_mask = inst['nace_str'].str.startswith(str(nace_filter))
    else:
        nace_mask = pd.Series(False, index=inst.index)

    # Strategy 2: Company name matching
    name_mask = inst['name'].fillna('').str.contains(company_regex, na=False)

    # Union of both strategies
    combined_mask = nace_mask | name_mask
    matched_inst = inst[combined_mask].copy()

    # Add identification method
    matched_inst['match_method'] = 'both'
    matched_inst.loc[nace_mask & ~name_mask, 'match_method'] = 'nace_only'
    matched_inst.loc[~nace_mask & name_mask, 'match_method'] = 'name_only'

    # Normalize company names
    matched_inst['company'] = matched_inst['name'].apply(
        lambda n: _normalize_company(n, company_regex)
    )

    if nace_filter is not None:
        log.info(f"  NACE {nace_filter} installations: {nace_mask.sum()}")
    log.info(f"  Name-matched installations: {name_mask.sum()}")
    log.info(f"  Union (deduplicated): {len(matched_inst)}")
    log.info(f"  Unique companies: {matched_inst['company'].nunique()}")

    return matched_inst


def build_allocation_table(matched_inst: pd.DataFrame, comp: pd.DataFrame) -> pd.DataFrame:
    """Join matched installations with compliance data to get free allocation."""
    matched_ids = set(matched_inst['id'].astype(str))
    comp['installation_id'] = comp['installation_id'].astype(str)

    matched_comp = comp[comp['installation_id'].isin(matched_ids)].copy()

    # Merge with installation info
    inst_cols = ['id', 'name', 'registry_id', 'nace_id', 'nace_str',
                 'city', 'match_method', 'company']
    matched_comp = matched_comp.merge(
        matched_inst[inst_cols].rename(columns={'id': 'installation_id'}),
        on='installation_id', how='left'
    )

    # Add EUR values
    matched_comp['eua_price'] = matched_comp['year'].map(EUA_PRICES).fillna(50)
    matched_comp['eur_free_allocation'] = matched_comp['allocatedFree'] * matched_comp['eua_price']
    matched_comp['eur_total_allocation'] = matched_comp['allocatedTotal'] * matched_comp['eua_price']
    matched_comp['eur_verified_emissions'] = matched_comp['verified'] * matched_comp['eua_price']

    log.info(f"  Matched compliance rows: {len(matched_comp):,}")
    log.info(f"  Year range: {matched_comp['year'].min()} - {matched_comp['year'].max()}")

    return matched_comp


def run_ets_enrichment(company_list_csv, aliases_json=None, output_dir=None, nace_filter=None):
    """
    Run ETS free allocation enrichment for a given company list.

    Parameters
    ----------
    company_list_csv : str or Path
        Path to CSV containing company names. The first column with 'name' in
        its header (case-insensitive) is used; otherwise the first column.
    aliases_json : str or Path, optional
        Path to JSON mapping canonical names to alias lists.
    output_dir : str or Path, optional
        Output directory. Defaults to ENRICHMENT_DIR from src.paths.
    nace_filter : str, optional
        NACE code prefix to filter installations (e.g., '29' for motor vehicles).
        If None, only company name matching is used.
    """
    t0 = time.time()
    out_dir = Path(output_dir) if output_dir else ENRICHMENT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("=" * 70)
    log.info("EU ETS FREE ALLOCATION — ENRICHMENT")
    log.info("=" * 70)

    # Build company regex from CSV
    company_regex = _build_company_regex(company_list_csv, aliases_json)
    log.info(f"  Company list: {company_list_csv}")
    if aliases_json:
        log.info(f"  Aliases: {aliases_json}")
    if nace_filter:
        log.info(f"  NACE filter: {nace_filter}")

    # Load data
    inst, comp = load_eutl_data()

    # Identify matching installations
    log.info("Identifying matching installations...")
    matched_inst = identify_matching_installations(inst, company_regex, nace_filter)

    # Build allocation table
    log.info("Building allocation table...")
    matched_comp = build_allocation_table(matched_inst, comp)

    # === OUTPUT 1: Installation-level detail ===
    out_detail = out_dir / 'ets_matched_detail.csv'
    detail_cols = [
        'installation_id', 'name', 'company', 'registry_id', 'city',
        'nace_id', 'match_method', 'year',
        'allocatedFree', 'allocatedTotal', 'verified',
        'eur_free_allocation', 'eur_total_allocation', 'eur_verified_emissions',
    ]
    matched_comp[detail_cols].to_csv(out_detail, index=False)
    log.info(f"Saved detail: {out_detail}")

    # === OUTPUT 2: Company x year summary ===
    company_year = matched_comp.groupby(['company', 'year']).agg(
        installations=('installation_id', 'nunique'),
        countries=('registry_id', 'nunique'),
        free_allowances=('allocatedFree', 'sum'),
        total_allowances=('allocatedTotal', 'sum'),
        verified_emissions=('verified', 'sum'),
        eur_free=('eur_free_allocation', 'sum'),
        eur_total=('eur_total_allocation', 'sum'),
    ).reset_index()
    out_company_year = out_dir / 'ets_matched_company_year.csv'
    company_year.to_csv(out_company_year, index=False)
    log.info(f"Saved company x year: {out_company_year}")

    # === OUTPUT 3: Company total summary ===
    company_total = matched_comp.groupby('company').agg(
        installations=('installation_id', 'nunique'),
        countries=('registry_id', lambda x: ','.join(sorted(x.unique()))),
        first_year=('year', 'min'),
        last_year=('year', 'max'),
        total_free_allowances=('allocatedFree', 'sum'),
        total_verified_emissions=('verified', 'sum'),
        total_eur_free=('eur_free_allocation', 'sum'),
    ).reset_index().sort_values('total_eur_free', ascending=False)
    out_company = out_dir / 'ets_matched_companies.csv'
    company_total.to_csv(out_company, index=False)
    log.info(f"Saved company summary: {out_company}")

    # === SUMMARY ===
    log.info("")
    log.info("=" * 70)
    log.info("EU ETS FREE ALLOCATION — SUMMARY")
    log.info("=" * 70)

    # Filter to valid allocation years (exclude future zeros)
    valid = matched_comp[matched_comp['allocatedFree'] > 0]

    total_free = valid['allocatedFree'].sum()
    total_eur = valid['eur_free_allocation'].sum()
    total_verified = valid['verified'].sum()
    total_eur_verified = valid['eur_verified_emissions'].sum()

    log.info(f"Matched installations: {matched_inst['id'].nunique()}")
    log.info(f"  In {matched_inst['registry_id'].nunique()} countries")
    log.info(f"  {matched_inst['company'].nunique()} unique companies")
    log.info(f"")
    log.info(f"Free allocation (2005-2024):")
    log.info(f"  Total allowances: {total_free:,.0f}")
    log.info(f"  EUR value (at annual EUA price): EUR {total_eur:,.0f}")
    log.info(f"")
    log.info(f"Verified emissions:")
    log.info(f"  Total: {total_verified:,.0f} tCO2")
    log.info(f"  EUR value: EUR {total_eur_verified:,.0f}")
    log.info(f"")
    log.info(f"Net benefit (free alloc - emissions): EUR {total_eur - total_eur_verified:,.0f}")

    log.info(f"")
    log.info(f"Top 20 companies by free allocation value:")
    for _, r in company_total.head(20).iterrows():
        log.info(f"  {r['company']:40s} EUR {r['total_eur_free']:>14,.0f}  ({r['installations']:2d} inst, {r['countries']})")

    log.info(f"")
    log.info(f"Phase breakdown:")
    for phase, (yr_from, yr_to) in [('Phase 1 (2005-07)', (2005, 2007)),
                                      ('Phase 2 (2008-12)', (2008, 2012)),
                                      ('Phase 3 (2013-20)', (2013, 2020)),
                                      ('Phase 4 (2021-24)', (2021, 2024))]:
        mask = (valid['year'] >= yr_from) & (valid['year'] <= yr_to)
        phase_eur = valid[mask]['eur_free_allocation'].sum()
        phase_alloc = valid[mask]['allocatedFree'].sum()
        log.info(f"  {phase}: {phase_alloc:>12,.0f} allowances, EUR {phase_eur:>14,.0f}")

    elapsed = time.time() - t0
    log.info(f"\nRuntime: {elapsed:.1f}s")

    return matched_comp, company_total


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='EU ETS Free Allocation Enrichment')
    parser.add_argument('company_list_csv', help='Path to company list CSV')
    parser.add_argument('--aliases', help='Path to aliases JSON')
    parser.add_argument('--output-dir', help='Output directory')
    parser.add_argument('--nace-filter', help='NACE code prefix filter (e.g., 29)')
    args = parser.parse_args()
    run_ets_enrichment(args.company_list_csv, args.aliases, args.output_dir, args.nace_filter)
