#!/usr/bin/env python3
"""
FTS-CORDIS Cross-Reference Bridge (Generic)
=============================================
Links FTS (Financial Transparency System) rows to CORDIS participant data
by extracting grant agreement numbers from FTS descriptions.

The FTS standardized data contains ~534K research-programme rows (H2020, Horizon
Europe, FP7). For H2020 and HE rows, the description field contains the CORDIS
grant agreement number in format: "NNNNNN - ACRONYM - Title".

This script:
1. Loads FTS rows from the master dataset and extracts grant IDs from descriptions
2. Loads CORDIS participant data (data/reference/cordis_participants.csv)
3. Joins FTS rows -> CORDIS projects -> CORDIS participants
4. Identifies sector-relevant FTS expenditure via:
   a) Company name matching against a user-supplied company list
   b) CORDIS activity_type = PRC (private company)
   c) Optional sector keyword matching in project descriptions
5. Outputs enriched FTS table

Usage (as module):
  from src.enrichment.fts_cordis_bridge import run_fts_cordis_bridge
  run_fts_cordis_bridge(company_list_csv='path/to/companies.csv')

Usage (CLI):
  python -m src.enrichment.fts_cordis_bridge --company-list path/to/companies.csv
"""

import pandas as pd
import numpy as np
import re
import sys
import json
import time
import logging
from pathlib import Path

from src.paths import PROCESSED_DIR, ENRICHMENT_DIR, MATCH_OUTPUT_DIR, REPO_ROOT, read_master

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

# Default paths
DEFAULT_OUTPUT_DIR = ENRICHMENT_DIR
REFERENCE_DIR      = REPO_ROOT / 'data' / 'reference'
PARTICIPANTS       = REFERENCE_DIR / 'cordis_participants.csv'

# Grant ID extraction pattern: leading 5-9 digit number followed by " - "
GRANT_ID_RE = re.compile(r'^(\d{5,9})\s*-\s*')


# ---------------------------------------------------------------------------
# Dynamic regex builders
# ---------------------------------------------------------------------------

def _build_company_regex(company_list_csv, aliases_json=None):
    """Build a compiled regex from a CSV of company names + optional aliases JSON."""
    df = pd.read_csv(company_list_csv)
    # Find the name column (prefer columns containing 'name')
    name_col = next((c for c in df.columns if 'name' in c.lower()), df.columns[0])
    names = df[name_col].dropna().str.strip().tolist()

    # Add aliases if provided
    if aliases_json and Path(aliases_json).exists():
        with open(aliases_json) as f:
            aliases = json.load(f)
        for canonical, alias_list in aliases.items():
            names.extend(alias_list)

    # Build regex from names (escape special chars, sort longest first)
    patterns = sorted(set(n.lower() for n in names if len(n) > 2), key=len, reverse=True)
    patterns = [re.escape(p) for p in patterns]
    if not patterns:
        return re.compile(r'(?!)')  # match nothing
    return re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.I)


def _build_topic_regex(sector_keywords):
    """Build a compiled regex from a list of sector keyword strings.

    Parameters
    ----------
    sector_keywords : list[str] | None
        Plain-text keyword phrases.  If *None* or empty, returns a regex that
        matches nothing so downstream logic can skip cleanly.
    """
    if not sector_keywords:
        return re.compile(r'(?!)')  # match nothing
    patterns = sorted(set(sector_keywords), key=len, reverse=True)
    patterns = [re.escape(p) for p in patterns]
    return re.compile(r'\b(?:' + '|'.join(patterns) + r')\b', re.I)


# ---------------------------------------------------------------------------
# Core pipeline stages (algorithm identical to automotive original)
# ---------------------------------------------------------------------------

def extract_grant_ids(fts: pd.DataFrame) -> pd.DataFrame:
    """Extract CORDIS grant agreement numbers from FTS description field."""
    fts = fts.copy()

    # Identify research programme rows
    prog_mask = fts['programme'].fillna('').str.contains(
        r'Horizon 2020|Horizon Europe|Research framework', case=False, na=False
    )
    research_fts = fts[prog_mask].copy()
    log.info(f"  Research programme rows: {len(research_fts):,} ({research_fts['amount_eur'].sum():,.0f} EUR)")

    # Extract grant IDs from descriptions
    def _extract_id(desc):
        if pd.isna(desc):
            return None
        m = GRANT_ID_RE.match(str(desc).strip())
        return int(m.group(1)) if m else None

    research_fts['cordis_project_id'] = research_fts['description'].apply(_extract_id)

    has_id = research_fts['cordis_project_id'].notna()
    log.info(f"  With extractable grant ID: {has_id.sum():,} ({research_fts[has_id]['amount_eur'].sum():,.0f} EUR)")
    log.info(f"  Without grant ID: {(~has_id).sum():,}")

    # Programme breakdown
    for prog_key in ['Horizon 2020', 'Horizon Europe', 'framework']:
        mask = research_fts['programme'].str.contains(prog_key, case=False, na=False)
        sub = research_fts[mask]
        sub_ids = sub['cordis_project_id'].notna().sum()
        log.info(f"    {prog_key}: {len(sub):,} rows, {sub_ids:,} with ID ({100*sub_ids/max(len(sub),1):.0f}%)")

    return research_fts


def load_cordis_participants(participants_path: Path = PARTICIPANTS) -> pd.DataFrame:
    """Load CORDIS participant data."""
    if not participants_path.exists():
        log.warning(f"  CORDIS participants not found: {participants_path}")
        return pd.DataFrame()

    parts = pd.read_csv(participants_path, low_memory=False)
    parts['project_id'] = pd.to_numeric(parts['project_id'], errors='coerce').astype('Int64')
    log.info(f"  CORDIS participants: {len(parts):,} rows, {parts['project_id'].nunique():,} projects")
    return parts


def build_fts_cordis_bridge(research_fts: pd.DataFrame, participants: pd.DataFrame) -> pd.DataFrame:
    """Join FTS research rows with CORDIS participants via grant IDs."""
    # Only work with rows that have grant IDs
    with_ids = research_fts[research_fts['cordis_project_id'].notna()].copy()
    with_ids['cordis_project_id'] = with_ids['cordis_project_id'].astype('Int64')

    log.info(f"  FTS rows with grant IDs: {len(with_ids):,}")

    # Check overlap with CORDIS
    cordis_pids = set(participants['project_id'].dropna().unique())
    fts_pids = set(with_ids['cordis_project_id'].dropna().unique())
    overlap = fts_pids & cordis_pids

    log.info(f"  Unique FTS grant IDs: {len(fts_pids):,}")
    log.info(f"  Overlap with CORDIS: {len(overlap):,} ({100*len(overlap)/max(len(fts_pids),1):.0f}%)")

    # Join: FTS row -> CORDIS participants (via grant ID)
    # For identification, we only need company participants
    company_parts = participants[participants['activity_type'] == 'PRC'].copy()
    log.info(f"  Company participants (PRC): {len(company_parts):,}")

    # Build project-level company summary (which companies are in each project)
    project_companies = company_parts.groupby('project_id').agg(
        company_names=('org_name', lambda x: '|'.join(sorted(set(x.dropna())))),
        company_countries=('org_country', lambda x: ','.join(sorted(set(x.dropna())))),
        n_companies=('org_name', 'nunique'),
        total_company_ec=('ec_contribution', 'sum'),
    ).reset_index()

    # Join to FTS
    bridge = with_ids.merge(
        project_companies,
        left_on='cordis_project_id',
        right_on='project_id',
        how='left',
        suffixes=('', '_cordis'),
    )

    matched = bridge['company_names'].notna()
    log.info(f"  FTS rows matched to CORDIS companies: {matched.sum():,}")
    log.info(f"  FTS EUR matched: {bridge[matched]['amount_eur'].sum():,.0f}")

    return bridge


def identify_sector_fts(bridge: pd.DataFrame, company_re, topic_re) -> pd.DataFrame:
    """Identify sector-relevant FTS rows using multiple signals.

    Parameters
    ----------
    bridge : pd.DataFrame
        Output of ``build_fts_cordis_bridge``.
    company_re : re.Pattern
        Compiled regex for company name matching.
    topic_re : re.Pattern
        Compiled regex for topic / description keyword matching.
    """
    # Signal 1: CORDIS company name matches company pattern
    company_mask = bridge['company_names'].fillna('').str.contains(company_re, na=False)

    # Signal 2: FTS description/project title has sector keywords
    topic_mask = bridge['description'].fillna('').str.contains(topic_re, na=False)

    # Signal 3: FTS beneficiary name matches company pattern
    beneficiary_mask = bridge['beneficiary_name'].fillna('').str.contains(company_re, na=False)

    # Union of signals
    sector_mask = company_mask | topic_mask | beneficiary_mask

    sector_fts = bridge[sector_mask].copy()
    sector_fts['signal'] = 'other'
    sector_fts.loc[company_mask, 'signal'] = 'cordis_company'
    sector_fts.loc[topic_mask, 'signal'] = 'topic_keyword'
    sector_fts.loc[beneficiary_mask, 'signal'] = 'beneficiary_name'
    # Priority: beneficiary > cordis_company > topic
    sector_fts.loc[company_mask & topic_mask, 'signal'] = 'company+topic'
    sector_fts.loc[beneficiary_mask, 'signal'] = 'beneficiary_name'

    log.info(f"  Sector-matched FTS rows: {len(sector_fts):,}")
    log.info(f"  EUR: {sector_fts['amount_eur'].sum():,.0f}")
    log.info(f"  By signal:")
    for sig, count in sector_fts['signal'].value_counts().items():
        eur = sector_fts[sector_fts['signal'] == sig]['amount_eur'].sum()
        log.info(f"    {sig:25s}: {count:>6,} rows, EUR {eur:>14,.0f}")

    return sector_fts


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_fts_cordis_bridge(
    company_list_csv,
    aliases_json=None,
    output_dir=None,
    sector_keywords=None,
):
    """Run the full FTS-CORDIS bridge pipeline.

    Parameters
    ----------
    company_list_csv : str | Path
        Path to a CSV containing company names (must have a column with
        'name' in its header, or falls back to the first column).
    aliases_json : str | Path | None
        Optional path to a JSON mapping ``{canonical_name: [alias, ...]}``
        that supplements the company list.
    output_dir : str | Path | None
        Directory for output CSVs.  Defaults to ``ENRICHMENT_DIR``.
    sector_keywords : list[str] | None
        Optional plain-text keyword phrases for topic matching in FTS
        descriptions.  If *None*, topic signal is disabled.

    Returns
    -------
    tuple[pd.DataFrame, pd.DataFrame]
        ``(bridge, sector_fts)`` DataFrames.
    """
    t0 = time.time()
    out = Path(output_dir) if output_dir else DEFAULT_OUTPUT_DIR
    out.mkdir(parents=True, exist_ok=True)

    log.info("=" * 70)
    log.info("FTS-CORDIS CROSS-REFERENCE BRIDGE (generic)")
    log.info("=" * 70)

    # Build regexes dynamically
    log.info(f"Building company regex from {company_list_csv} ...")
    company_re = _build_company_regex(company_list_csv, aliases_json)
    topic_re = _build_topic_regex(sector_keywords)
    if sector_keywords:
        log.info(f"  Sector keywords: {len(sector_keywords)} phrases")
    else:
        log.info("  No sector keywords supplied — topic signal disabled")

    # Load FTS data from master dataset.
    #
    # MEMORY FIX (2026-04-14, v2): the historical behaviour was
    # ``master = read_master()`` — which loads all 36 columns ×
    # 27.7M rows (~15 GB RAM) then filters to FTS. A concurrent
    # pipeline run on a 32 GB machine OOM-killed during this step.
    #
    # Root cause: two heavy string-blob columns (``original_columns``
    # carrying per-source raw-JSON audit data and ``extra_fields_json``
    # carrying the schema-v3 enrichment blob) together account for
    # ~75% of the master's in-memory footprint despite only being
    # used for forensic audit. Neither is read by the matcher, the
    # dedup layer, the GGE computation, or the FTS-CORDIS bridge.
    #
    # The v2 fix drops exactly those two columns from the scan and
    # keeps all other 34 COMMON_COLUMNS fields — so
    # ``entity_name_clean``, ``entity_id``, ``flow_stage``,
    # ``nace_2digit``, ``is_primary_record``, etc. are still
    # available to downstream dedup + summary tables. Measured peak
    # RAM drops from ~15 GB to ~3.4 GB (77% reduction) with zero
    # semantic loss for any non-audit downstream consumer.
    #
    # (An earlier v1 fix used an 11-column whitelist that dropped
    # entity resolution fields too. That was over-aggressive — it
    # saved another 1 GB of RAM but broke the FTS_CORDIS-derived
    # rows' participation in Phase 2b dedup joins that read
    # ``entity_name_clean``. v2 is the correct balance.)
    log.info("Loading FTS rows from master dataset (chunked, heavy-blob-filtered)...")
    from src.harmonization.utils import COMMON_COLUMNS
    _HEAVY_DROPS = {'original_columns', 'extra_fields_json'}
    _fts_cols = [c for c in COMMON_COLUMNS if c not in _HEAVY_DROPS]
    from src.paths import read_master_chunked
    fts_chunks: list[pd.DataFrame] = []
    total_scanned = 0
    for chunk in read_master_chunked(columns=_fts_cols, chunksize=500_000):
        total_scanned += len(chunk)
        sub = chunk[chunk['source'] == 'FTS']
        if len(sub):
            fts_chunks.append(sub.copy())
    fts = pd.concat(fts_chunks, ignore_index=True) if fts_chunks else pd.DataFrame(columns=_fts_cols)
    del fts_chunks
    log.info(
        f"  Scanned {total_scanned:,} master rows; kept {len(fts):,} FTS rows "
        f"({len(_fts_cols)} columns, dropped {sorted(_HEAVY_DROPS)}); "
        f"EUR {float(fts['amount_eur'].fillna(0).sum()):,.0f}"
    )

    # Extract grant IDs
    log.info("Extracting grant IDs from descriptions...")
    research_fts = extract_grant_ids(fts)

    # Load CORDIS participants
    log.info("Loading CORDIS participants...")
    participants = load_cordis_participants()
    if len(participants) == 0:
        log.error("Cannot proceed without CORDIS participants. Run cordis_enrichment.py first.")
        return pd.DataFrame(), pd.DataFrame()

    # Build bridge
    log.info("Building FTS-CORDIS bridge...")
    bridge = build_fts_cordis_bridge(research_fts, participants)

    # Identify sector-relevant FTS rows
    log.info("Identifying sector-relevant FTS rows...")
    sector_fts = identify_sector_fts(bridge, company_re, topic_re)

    # Save outputs
    out_bridge = out / 'fts_cordis_bridge.csv'
    bridge.to_csv(out_bridge, index=False)
    log.info(f"Saved bridge: {out_bridge} ({len(bridge):,} rows)")

    out_sector = out / 'fts_via_cordis.csv'
    sector_fts.to_csv(out_sector, index=False)
    log.info(f"Saved sector matches: {out_sector} ({len(sector_fts):,} rows)")

    # Summary
    log.info("")
    log.info("=" * 70)
    log.info("FTS-CORDIS BRIDGE SUMMARY")
    log.info("=" * 70)
    log.info(f"FTS research rows: {len(research_fts):,}")
    log.info(f"  With grant IDs: {research_fts['cordis_project_id'].notna().sum():,}")
    log.info(f"  Matched to CORDIS companies: {bridge['company_names'].notna().sum():,}")
    log.info(f"")
    log.info(f"Sector identification:")
    log.info(f"  Matched FTS rows: {len(sector_fts):,}")
    log.info(f"  Matched EUR: {sector_fts['amount_eur'].sum():,.0f}")
    log.info(f"  Unique projects: {sector_fts['cordis_project_id'].nunique():,}")

    if len(sector_fts) > 0:
        log.info(f"")
        log.info(f"Top beneficiaries in FTS (via CORDIS):")
        top = sector_fts.groupby('beneficiary_name')['amount_eur'].sum().sort_values(ascending=False).head(20)
        for name, eur in top.items():
            log.info(f"  {str(name)[:55]:55s} EUR {eur:>14,.0f}")

    elapsed = time.time() - t0
    log.info(f"\nRuntime: {elapsed:.1f}s")

    return bridge, sector_fts


def main():
    """CLI entry point — uses default automotive paths for backwards compatibility."""
    import argparse

    parser = argparse.ArgumentParser(description='FTS-CORDIS Cross-Reference Bridge')
    parser.add_argument('--company-list', type=str, default=None,
                        help='Path to CSV with company names')
    parser.add_argument('--aliases', type=str, default=None,
                        help='Path to aliases JSON')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory')
    parser.add_argument('--sector-keywords', type=str, nargs='*', default=None,
                        help='Sector keyword phrases for topic matching')
    args = parser.parse_args()

    # If no company list specified, try the default automotive alias file
    company_list = args.company_list
    aliases = args.aliases

    if company_list is None:
        # Fall back to default match output location
        default_csv = MATCH_OUTPUT_DIR / 'company_list.csv'
        if default_csv.exists():
            company_list = str(default_csv)
        else:
            log.error(
                "No --company-list provided and default not found at "
                f"{default_csv}. Please supply a company list CSV."
            )
            sys.exit(1)

    if aliases is None:
        default_aliases = MATCH_OUTPUT_DIR / 'automotive_aliases.json'
        if default_aliases.exists():
            aliases = str(default_aliases)

    run_fts_cordis_bridge(
        company_list_csv=company_list,
        aliases_json=aliases,
        output_dir=args.output_dir,
        sector_keywords=args.sector_keywords,
    )


if __name__ == '__main__':
    main()
