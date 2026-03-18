#!/usr/bin/env python3
"""
CORDIS Participant Enrichment — Phase 1 (Bulk Data Join)
=========================================================
Enriches RESEARCH project-level rows with CORDIS organization-level participant data.

Input:
  - analysis_output/standardized_RESEARCH.csv  (54,579 project rows, EUR 119.4B)
  - H2020 + Horizon Europe organization Parquet files (230K org rows)

Output:
  - external_enrichment/output/cordis_participants.csv
      One row per project × participant. Columns: project_id, org_name, org_short,
      org_country, ec_contribution, activity_type, role, sme, vat_number, org_id
  - external_enrichment/output/research_enriched.csv
      Enriched version of standardized_RESEARCH.csv where matched projects are
      expanded to participant-level rows (resolution_level='beneficiary').
      Unmatched projects are retained as-is.

Does NOT modify any canonical pipeline files. This is a derived dataset.

Usage:
  python -m src.enrichment.cordis_enrichment
"""

import pandas as pd
import numpy as np
import sys
import logging
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
from src.paths import PROCESSED_DIR, ENRICHMENT_DIR

OUTPUT_DIR   = ENRICHMENT_DIR

RESEARCH_CSV = PROCESSED_DIR / 'standardized_RESEARCH.csv'

# CORDIS bulk data — downloaded from data.europa.eu / KTH-Library/cordis-data
DESKTOP = Path.home() / 'Desktop'
H2020_PARQUET = DESKTOP / 'h2020_organization.parquet'
HE_PARQUET    = DESKTOP / 'he_organization.parquet'

# Columns to keep from CORDIS org data
ORG_COLS = [
    'projectID', 'name', 'shortName', 'country', 'activityType',
    'role', 'SME', 'vatNumber', 'organisationID',
    'ecContribution', 'netEcContribution', 'totalCost',
]


def load_cordis_orgs() -> pd.DataFrame:
    """Load and combine H2020 + Horizon Europe organization data."""
    frames = []
    for path, programme in [(H2020_PARQUET, 'H2020'), (HE_PARQUET, 'HORIZON_EUROPE')]:
        if not path.exists():
            log.warning(f"  Missing: {path}")
            continue
        df = pd.read_parquet(path, columns=ORG_COLS)
        df['cordis_programme'] = programme
        frames.append(df)
        log.info(f"  Loaded {path.name}: {len(df):,} rows")

    if not frames:
        raise FileNotFoundError("No CORDIS Parquet files found")

    orgs = pd.concat(frames, ignore_index=True)
    orgs['projectID'] = pd.to_numeric(orgs['projectID'], errors='coerce').astype('Int64')
    orgs = orgs.dropna(subset=['projectID'])

    # Clean up contribution columns
    for col in ['ecContribution', 'netEcContribution', 'totalCost']:
        orgs[col] = pd.to_numeric(orgs[col], errors='coerce').fillna(0)

    log.info(f"  Total CORDIS org rows: {len(orgs):,} across {orgs['projectID'].nunique():,} projects")
    return orgs


def build_enriched_research(research: pd.DataFrame, orgs: pd.DataFrame) -> pd.DataFrame:
    """
    Join RESEARCH project rows with CORDIS org rows.
    Matched projects are expanded to N rows (one per participant).
    Unmatched projects are retained as-is.
    """
    research = research.copy()
    research['_pid'] = pd.to_numeric(research['source_record_id'], errors='coerce').astype('Int64')

    # Split into matchable and unmatchable
    cordis_pids = set(orgs['projectID'].unique())
    matched_mask = research['_pid'].isin(cordis_pids)
    matched = research[matched_mask].copy()
    unmatched = research[~matched_mask].copy()

    log.info(f"  Matched: {len(matched):,} projects ({matched['amount_eur'].sum():,.0f} EUR)")
    log.info(f"  Unmatched: {len(unmatched):,} projects ({unmatched['amount_eur'].sum():,.0f} EUR)")

    # Join: project × participants
    orgs_join = orgs.rename(columns={
        'projectID': '_pid',
        'name': 'cordis_org_name',
        'shortName': 'cordis_org_short',
        'country': 'cordis_org_country',
        'activityType': 'cordis_activity_type',
        'role': 'cordis_role',
        'SME': 'cordis_sme',
        'vatNumber': 'cordis_vat',
        'organisationID': 'cordis_org_id',
        'ecContribution': 'cordis_ec_contribution',
        'netEcContribution': 'cordis_net_ec_contribution',
        'totalCost': 'cordis_total_cost',
    })

    enriched = matched.merge(orgs_join, on='_pid', how='left')

    # Update entity fields for enriched rows
    enriched['beneficiary_name'] = enriched['cordis_org_name']
    enriched['entity_name_raw'] = enriched['cordis_org_name']
    enriched['entity_name_clean'] = enriched['cordis_org_name'].str.strip().str.lower()
    enriched['country'] = enriched['cordis_org_country']
    enriched['resolution_level'] = 'beneficiary'

    # Replace amount_eur with org-level EC contribution (avoids double-counting)
    enriched['amount_eur_project'] = enriched['amount_eur']  # keep original
    enriched['amount_eur'] = enriched['cordis_ec_contribution']

    # Map activityType to entity_type
    activity_type_map = {
        'PRC': 'company',
        'HES': 'university',
        'REC': 'university',  # research centres → university category
        'PUB': 'public_body',
        'OTH': 'unknown',
    }
    enriched['entity_type'] = enriched['cordis_activity_type'].map(activity_type_map).fillna('unknown')

    # Drop join key
    enriched = enriched.drop(columns=['_pid'])
    unmatched = unmatched.drop(columns=['_pid'])

    log.info(f"  Enriched rows: {len(enriched):,} (avg {len(enriched)/len(matched):.1f} participants/project)")
    log.info(f"  Enriched EUR (EC contributions): {enriched['amount_eur'].sum():,.0f}")

    # Combine enriched + unmatched
    result = pd.concat([enriched, unmatched], ignore_index=True)
    return result


def build_participants_table(orgs: pd.DataFrame) -> pd.DataFrame:
    """Build a standalone participants reference table."""
    return orgs.rename(columns={
        'projectID': 'project_id',
        'name': 'org_name',
        'shortName': 'org_short',
        'country': 'org_country',
        'activityType': 'activity_type',
        'role': 'role',
        'SME': 'sme',
        'vatNumber': 'vat_number',
        'organisationID': 'org_id',
        'ecContribution': 'ec_contribution',
        'netEcContribution': 'net_ec_contribution',
        'totalCost': 'total_cost',
    })


def main():
    t0 = time.time()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 70)
    log.info("CORDIS PARTICIPANT ENRICHMENT — Phase 1 (Bulk Data)")
    log.info("=" * 70)

    # Load CORDIS org data
    log.info("Loading CORDIS organization data...")
    orgs = load_cordis_orgs()

    # Activity type distribution
    log.info("  Activity type distribution:")
    for at, count in orgs['activityType'].value_counts().items():
        eur = orgs[orgs['activityType'] == at]['ecContribution'].sum()
        log.info(f"    {at}: {count:>7,} orgs, EUR {eur:>15,.0f}")

    # Load RESEARCH
    log.info("Loading standardized RESEARCH...")
    research = pd.read_csv(RESEARCH_CSV)
    log.info(f"  {len(research):,} rows, EUR {research['amount_eur'].sum():,.0f}")

    # Build participants table
    log.info("Building participants reference table...")
    participants = build_participants_table(orgs)
    out_participants = OUTPUT_DIR / 'cordis_participants.csv'
    participants.to_csv(out_participants, index=False)
    log.info(f"  Saved: {out_participants} ({len(participants):,} rows)")

    # Build enriched RESEARCH
    log.info("Building enriched RESEARCH dataset...")
    enriched = build_enriched_research(research, orgs)
    out_enriched = OUTPUT_DIR / 'research_enriched.csv'
    enriched.to_csv(out_enriched, index=False)
    log.info(f"  Saved: {out_enriched} ({len(enriched):,} rows)")

    # Summary statistics
    log.info("")
    log.info("=" * 70)
    log.info("ENRICHMENT SUMMARY")
    log.info("=" * 70)
    log.info(f"Original RESEARCH rows:        {len(research):>10,}")
    log.info(f"Enriched output rows:          {len(enriched):>10,}")
    log.info(f"  - Participant-level (enriched): {(enriched['resolution_level'] == 'beneficiary').sum():>10,}")
    log.info(f"  - Project-level (unmatched):    {(enriched['resolution_level'] == 'project').sum():>10,}")

    enriched_only = enriched[enriched['resolution_level'] == 'beneficiary']
    log.info(f"")
    log.info(f"Enriched EUR (EC contrib):     EUR {enriched_only['amount_eur'].sum():>15,.0f}")
    log.info(f"Unmatched EUR (project-level):  EUR {enriched[enriched['resolution_level'] == 'project']['amount_eur'].sum():>15,.0f}")

    log.info(f"")
    log.info(f"Entity type distribution (enriched rows only):")
    for etype, count in enriched_only['entity_type'].value_counts().items():
        eur = enriched_only[enriched_only['entity_type'] == etype]['amount_eur'].sum()
        log.info(f"  {etype:15s}: {count:>8,} rows, EUR {eur:>15,.0f}")

    log.info(f"")
    log.info(f"Company rows (PRC) in enriched: {(enriched_only['cordis_activity_type'] == 'PRC').sum():,}")
    log.info(f"Company EUR:                    EUR {enriched_only[enriched_only['cordis_activity_type'] == 'PRC']['amount_eur'].sum():,.0f}")

    elapsed = time.time() - t0
    log.info(f"")
    log.info(f"Runtime: {elapsed:.1f}s")


if __name__ == '__main__':
    main()
