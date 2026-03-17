#!/usr/bin/env python3
"""
Generic IPCEI Enrichment
==========================
Matches a user-supplied company list against IPCEI participant reference data.
Ships with curated participant data for 6 IPCEIs (Batteries 1&2, Microelectronics
1&2, Hy2Tech, Hy2Move) but accepts custom participant CSVs.

Usage:
  # As library
  from src.data_extraction.enrichment.ipcei_reference import run_ipcei_enrichment
  run_ipcei_enrichment('my_companies.csv')

  # CLI
  python -m src.data_extraction.enrichment.ipcei_reference --company-list my_companies.csv
"""

import pandas as pd
import json
import re
import sys
import logging
import time
from pathlib import Path

from src.paths import ENRICHMENT_DIR, REPO_ROOT

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Default reference data shipped with the repo
DEFAULT_OVERVIEW = REPO_ROOT / 'data' / 'reference' / 'ipcei_overview.csv'
DEFAULT_PARTICIPANTS = REPO_ROOT / 'data' / 'reference' / 'ipcei_participants.csv'


def _load_company_names(company_list_csv, aliases_json=None):
    """Load company names from CSV + optional aliases JSON."""
    df = pd.read_csv(company_list_csv)
    name_col = next((c for c in df.columns if 'name' in c.lower()), df.columns[0])
    names = df[name_col].dropna().str.strip().tolist()
    if aliases_json and Path(aliases_json).exists():
        with open(aliases_json) as f:
            aliases = json.load(f)
        for canonical, alias_list in aliases.items():
            names.append(canonical)
            names.extend(alias_list)
    return [n.lower().strip() for n in names if len(n) > 1]


def _fuzzy_match_participant(participant_name, company_names):
    """Check if an IPCEI participant matches any company in the list."""
    p = participant_name.lower().strip()
    # Also try extracting the core name (before parenthetical)
    p_core = re.sub(r'\([^)]*\)', '', p).strip()

    for name in company_names:
        n = name.lower().strip()
        # Exact or substring match
        if n in p or p in n or n in p_core or p_core in n:
            return name
        # Token overlap (for multi-word names)
        p_tokens = set(p_core.split())
        n_tokens = set(n.split())
        if len(n_tokens) >= 2 and n_tokens.issubset(p_tokens):
            return name
        if len(p_tokens) >= 2 and p_tokens.issubset(n_tokens):
            return name
    return None


def run_ipcei_enrichment(
    company_list_csv,
    aliases_json=None,
    output_dir=None,
    participants_csv=None,
    overview_csv=None,
):
    """Match company list against IPCEI participant reference data.

    Parameters
    ----------
    company_list_csv : str or Path
        Path to the user's company list CSV.
    aliases_json : str or Path, optional
        Path to company aliases JSON.
    output_dir : str or Path, optional
        Output directory. Defaults to ENRICHMENT_DIR.
    participants_csv : str or Path, optional
        Path to IPCEI participants CSV. Defaults to shipped reference data.
    overview_csv : str or Path, optional
        Path to IPCEI overview CSV. Defaults to shipped reference data.

    Returns
    -------
    tuple of (matched_df, overview_df)
    """
    t0 = time.time()
    output_dir = Path(output_dir) if output_dir else ENRICHMENT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    participants_csv = Path(participants_csv) if participants_csv else DEFAULT_PARTICIPANTS
    overview_csv = Path(overview_csv) if overview_csv else DEFAULT_OVERVIEW

    log.info("=" * 70)
    log.info("IPCEI ENRICHMENT")
    log.info("=" * 70)

    # Load reference data
    if not participants_csv.exists():
        log.error(f"IPCEI participants file not found: {participants_csv}")
        return pd.DataFrame(), pd.DataFrame()

    overview = pd.read_csv(overview_csv) if overview_csv.exists() else pd.DataFrame()
    participants = pd.read_csv(participants_csv)
    log.info(f"  IPCEI programmes: {len(overview)}")
    log.info(f"  IPCEI participants: {len(participants)}")

    # Load company names
    company_names = _load_company_names(company_list_csv, aliases_json)
    log.info(f"  Company list: {len(company_names)} names")

    # Match participants against company list
    matched = []
    unmatched = []
    for _, row in participants.iterrows():
        match = _fuzzy_match_participant(row['company'], company_names)
        if match:
            row_dict = row.to_dict()
            row_dict['matched_company'] = match
            matched.append(row_dict)
        else:
            unmatched.append(row['company'])

    matched_df = pd.DataFrame(matched)
    log.info(f"  Matched: {len(matched_df)} participants")
    log.info(f"  Unmatched: {len(unmatched)} participants")

    if len(matched_df) > 0:
        # Format for consolidation
        matched_df['amount_eur'] = matched_df.get('amount_eur_est', pd.Series(dtype=float))
        matched_df['source'] = 'IPCEI_state_aid'
        matched_df['beneficiary_name'] = matched_df['company']
        matched_df['financial_instrument_class'] = 'grant'
        matched_df['fiscal_source_type'] = 'national_budget'

        # Year from overview
        if len(overview) > 0:
            sa_year = {}
            for _, ov_row in overview.iterrows():
                if 'sa_case' in ov_row and 'approval_date' in ov_row:
                    sa_year[ov_row.get('ipcei_name', '')] = int(str(ov_row['approval_date'])[:4])
            matched_df['year'] = matched_df['ipcei'].map(sa_year)

        # Save
        out_path = output_dir / 'ipcei_matched_participants.csv'
        matched_df.to_csv(out_path, index=False)
        log.info(f"  Saved: {out_path}")

        # Summary
        with_amounts = matched_df[matched_df['amount_eur'].notna()]
        log.info(f"\n  Matched with known amounts: {len(with_amounts)}")
        log.info(f"  Total known: EUR {with_amounts['amount_eur'].sum():,.0f}")
        for _, r in with_amounts.sort_values('amount_eur', ascending=False).iterrows():
            log.info(f"    {r['company']:35s} {r['ipcei']:30s} EUR {r['amount_eur']:>14,.0f}")

    # Save overview
    if len(overview) > 0:
        overview.to_csv(output_dir / 'ipcei_overview.csv', index=False)

    elapsed = time.time() - t0
    log.info(f"\n  Runtime: {elapsed:.1f}s")
    return matched_df, overview


def main():
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description='IPCEI enrichment — match company list against IPCEI participants')
    parser.add_argument('--company-list', '-c', required=True, help='Path to company list CSV')
    parser.add_argument('--aliases', '-a', help='Path to aliases JSON')
    parser.add_argument('--output-dir', '-o', help='Output directory')
    parser.add_argument('--participants', help='Custom IPCEI participants CSV')
    args = parser.parse_args()
    run_ipcei_enrichment(args.company_list, args.aliases, args.output_dir, args.participants)


if __name__ == '__main__':
    main()
