#!/usr/bin/env python3
"""
FTS Deep Mining — Entity Discovery in Unmatched FTS Rows
==========================================================
Scans all FTS rows in the harmonized master dataset to find rows matching
a user-supplied company list that the current entity matcher misses.

Strategy:
  1. Entity name matching: known company names in beneficiary_name
     that fail matching due to country suffixes or subsidiary naming
  2. Optional keyword scanning: sector-specific keywords in description
     and original_columns fields (only if sector_keywords provided)
  3. High-value audit: top unmatched rows by EUR for manual review
  4. Alias recommendation: suggests new alias additions

Outputs:
  output/fts_entity_candidates.csv    -- rows with known companies in beneficiary
  output/fts_keyword_matches.csv      -- rows with sector keywords in text (if keywords provided)
  output/fts_highvalue_unmatched.csv   -- top unmatched by EUR
  output/fts_recommended_aliases.csv   -- suggested new alias additions
  output/fts_mining_summary.txt        -- human-readable summary

Usage:
  from src.enrichment.fts_deep_mining import run_fts_deep_mining
  run_fts_deep_mining('companies.csv', 'matched.csv')
"""

import json
import re
import sys
import logging
from pathlib import Path
from collections import defaultdict

import pandas as pd

from src.paths import PROCESSED_DIR, ENRICHMENT_DIR, MATCH_OUTPUT_DIR, read_master_chunked

sys.stdout.reconfigure(encoding='utf-8')


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


def _build_keyword_patterns(sector_keywords):
    """Build tier1/tier2 regex from a sector_keywords dict."""
    tier1_re = None
    tier2_re = None
    if sector_keywords:
        tier1_list = sector_keywords.get('tier1', [])
        tier2_list = sector_keywords.get('tier2', [])
        if tier1_list:
            tier1_re = re.compile('|'.join(tier1_list), re.IGNORECASE)
        if tier2_list:
            tier2_re = re.compile('|'.join(tier2_list), re.IGNORECASE)
    return tier1_re, tier2_re


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------
def clean_name(name) -> str:
    """Minimal cleaning for comparison."""
    if pd.isna(name):
        return ''
    s = str(name).strip().lower()
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def extract_oc_fields(oc_str) -> dict:
    """Extract fields from original_columns JSON."""
    if pd.isna(oc_str) or not str(oc_str).strip():
        return {}
    try:
        d = json.loads(str(oc_str)) if isinstance(oc_str, str) else oc_str
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def _load_existing_matches(matched_csv):
    """Load already-matched source_record_ids to exclude."""
    matched_csv = Path(matched_csv)
    if matched_csv.exists():
        try:
            df = pd.read_csv(matched_csv, usecols=['source', 'source_record_id'],
                             low_memory=True)
            fts_matched = df[df['source'] == 'FTS']
            return set(fts_matched['source_record_id'].astype(str))
        except Exception as e:
            logging.getLogger(__name__).warning(f"  Could not load existing matches: {e}")
    return set()


def run_fts_deep_mining(company_list_csv, matched_csv, output_dir=None, sector_keywords=None):
    """
    Run FTS deep mining for a given company list.

    Parameters
    ----------
    company_list_csv : str or Path
        Path to CSV containing company names.
    matched_csv : str or Path
        Path to CSV of already-matched rows (must have 'source' and 'source_record_id' columns).
    output_dir : str or Path, optional
        Output directory. Defaults to ENRICHMENT_DIR from src.paths.
    sector_keywords : dict, optional
        Dict with 'tier1' and 'tier2' keys, each a list of regex pattern strings.
        If None, keyword matching is skipped and only entity name matching is performed.
    """
    out_dir = Path(output_dir) if output_dir else ENRICHMENT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger(__name__)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(out_dir / 'fts_deep_mining.log', mode='w', encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )

    log.info("=" * 70)
    log.info("FTS DEEP MINING — Starting")
    log.info("=" * 70)

    # Build company regex from CSV
    company_regex = _build_company_regex(company_list_csv)
    log.info(f"  Company list: {company_list_csv}")

    # Build keyword patterns (optional)
    tier1_re, tier2_re = _build_keyword_patterns(sector_keywords)
    use_keywords = tier1_re is not None or tier2_re is not None
    if use_keywords:
        log.info(f"  Sector keywords: tier1={len(sector_keywords.get('tier1', []))}, "
                 f"tier2={len(sector_keywords.get('tier2', []))}")
    else:
        log.info("  No sector keywords provided — keyword matching skipped")

    # Load existing matches to exclude
    matched_ids = _load_existing_matches(matched_csv)
    log.info(f"  Existing matched FTS IDs: {len(matched_ids):,}")

    # Process master dataset in chunks
    keyword_results = []
    entity_results = []
    highvalue_results = []
    total_fts = 0
    total_fts_unmatched = 0
    total_fts_eur = 0.0
    total_fts_unmatched_eur = 0.0

    load_cols = [
        'source', 'source_record_id', 'beneficiary_name', 'entity_name_clean',
        'country', 'amount_eur', 'year', 'description', 'original_columns',
        'is_primary_record', 'programme', 'fund',
        'financial_instrument_class', 'nace_2digit',
    ]

    log.info(f"\nScanning master dataset...")
    chunk_n = 0
    for chunk in read_master_chunked(columns=load_cols, chunksize=250_000):
        chunk_n += 1
        # Filter to FTS primary rows only
        fts_mask = (
            (chunk['source'] == 'FTS') &
            (chunk['is_primary_record'].astype(str).str.lower() == 'true')
        )
        fts = chunk[fts_mask].copy()
        if len(fts) == 0:
            continue

        total_fts += len(fts)
        total_fts_eur += fts['amount_eur'].sum()

        # Exclude already-matched rows
        already_matched = fts['source_record_id'].astype(str).isin(matched_ids)
        unmatched = fts[~already_matched].copy()
        total_fts_unmatched += len(unmatched)
        total_fts_unmatched_eur += unmatched['amount_eur'].sum()

        if len(unmatched) == 0:
            continue

        # --- 1. Optional keyword scanning ---
        desc_series = unmatched['description'].fillna('')
        ben_series = unmatched['beneficiary_name'].fillna('')
        combined_text = desc_series + ' ' + ben_series
        prog_series = unmatched['programme'].fillna('')
        combined_text = combined_text + ' ' + prog_series

        oc_text = unmatched['original_columns'].apply(
            lambda x: ' '.join(str(v) for v in extract_oc_fields(x).values())
        )
        combined_text = combined_text + ' ' + oc_text

        tier1_match = pd.Series(False, index=unmatched.index)
        tier2_match = pd.Series(False, index=unmatched.index)

        if use_keywords:
            if tier1_re:
                tier1_match = combined_text.str.contains(tier1_re, na=False)
            if tier2_re:
                tier2_match = combined_text.str.contains(tier2_re, na=False) & ~tier1_match

            for idx in unmatched.index[tier1_match]:
                row = unmatched.loc[idx]
                text = combined_text.loc[idx]
                m = tier1_re.search(text)
                keyword_results.append({
                    'source_record_id': row['source_record_id'],
                    'beneficiary_name': row['beneficiary_name'],
                    'country': row['country'],
                    'amount_eur': row['amount_eur'],
                    'year': row['year'],
                    'description': str(row['description'])[:200],
                    'programme': row['programme'],
                    'keyword_matched': m.group(0) if m else '',
                    'keyword_tier': 'tier1',
                    'entity_name_clean': row['entity_name_clean'],
                })

            for idx in unmatched.index[tier2_match]:
                row = unmatched.loc[idx]
                text = combined_text.loc[idx]
                m = tier2_re.search(text) if tier2_re else None
                keyword_results.append({
                    'source_record_id': row['source_record_id'],
                    'beneficiary_name': row['beneficiary_name'],
                    'country': row['country'],
                    'amount_eur': row['amount_eur'],
                    'year': row['year'],
                    'description': str(row['description'])[:200],
                    'programme': row['programme'],
                    'keyword_matched': m.group(0) if m else '',
                    'keyword_tier': 'tier2',
                    'entity_name_clean': row['entity_name_clean'],
                })

        # --- 2. Entity extraction in beneficiary_name ---
        ben_clean = ben_series.apply(clean_name)
        company_match_mask = ben_clean.str.contains(company_regex, na=False)
        for idx in unmatched.index[company_match_mask]:
            row = unmatched.loc[idx]
            ben = str(row['beneficiary_name'])
            desc = str(row.get('description', ''))
            m = company_regex.search(clean_name(ben))
            matched_company = m.group(0) if m else ''

            # Context validation: if keywords available, check for sector context
            has_sector_ctx = True
            if tier1_re:
                has_sector_ctx = bool(tier1_re.search(desc))

            entity_results.append({
                'source_record_id': row['source_record_id'],
                'beneficiary_name': ben,
                'entity_name_clean': row['entity_name_clean'],
                'country': row['country'],
                'amount_eur': row['amount_eur'],
                'year': row['year'],
                'description': desc[:200],
                'programme': row['programme'],
                'matched_company': matched_company,
                'has_sector_context': has_sector_ctx,
                'confidence': 'high' if has_sector_ctx or not use_keywords else 'medium',
            })

        # --- 3. High-value unmatched collection ---
        hv_mask = unmatched['amount_eur'] > 1_000_000
        for idx in unmatched.index[hv_mask]:
            row = unmatched.loc[idx]
            highvalue_results.append({
                'source_record_id': row['source_record_id'],
                'beneficiary_name': row['beneficiary_name'],
                'entity_name_clean': row['entity_name_clean'],
                'country': row['country'],
                'amount_eur': row['amount_eur'],
                'year': row['year'],
                'description': str(row['description'])[:200],
                'programme': row['programme'],
                'nace_2digit': row['nace_2digit'],
            })

        log.info(f"  Chunk {chunk_n}: {len(fts):,} FTS rows, "
                 f"{len(unmatched):,} unmatched, "
                 f"T1={tier1_match.sum()}, T2={tier2_match.sum()}")

    # --- Assemble outputs ---
    log.info(f"\n{'='*70}")
    log.info("RESULTS")
    log.info(f"{'='*70}")

    # Keywords
    kw_df = pd.DataFrame(keyword_results)
    if not kw_df.empty:
        kw_df = kw_df.sort_values('amount_eur', ascending=False)
        kw_df.to_csv(out_dir / 'fts_keyword_matches.csv', index=False, encoding='utf-8')
        log.info(f"\nKeyword matches: {len(kw_df):,} rows")
        log.info(f"  Tier 1 (high-confidence): {(kw_df['keyword_tier']=='tier1').sum():,} rows, "
                 f"EUR {kw_df[kw_df['keyword_tier']=='tier1']['amount_eur'].sum():,.0f}")
        log.info(f"  Tier 2 (medium): {(kw_df['keyword_tier']=='tier2').sum():,} rows, "
                 f"EUR {kw_df[kw_df['keyword_tier']=='tier2']['amount_eur'].sum():,.0f}")

        kw_counts = kw_df['keyword_matched'].value_counts().head(20)
        log.info(f"\n  Top keywords: {dict(kw_counts)}")

    # Entity candidates
    ent_df = pd.DataFrame(entity_results)
    if not ent_df.empty:
        ent_df = ent_df.drop_duplicates(subset=['source_record_id', 'matched_company'])
        ent_df = ent_df.sort_values('amount_eur', ascending=False)
        ent_df.to_csv(out_dir / 'fts_entity_candidates.csv', index=False, encoding='utf-8')
        log.info(f"\nEntity candidates: {len(ent_df):,} rows")

        company_stats = ent_df.groupby('matched_company').agg(
            rows=('amount_eur', 'count'),
            total_eur=('amount_eur', 'sum'),
        ).sort_values('total_eur', ascending=False)
        log.info(f"\n  Per company:")
        for company, row in company_stats.iterrows():
            log.info(f"    {company:25s}: {row['rows']:5,} rows, EUR {row['total_eur']:>15,.0f}")

        # Generate alias recommendations
        alias_recs = []
        for company, group in ent_df.groupby('matched_company'):
            unique_names = group['entity_name_clean'].unique()
            for name in unique_names:
                if name:
                    alias_recs.append({
                        'entity_name_clean': name,
                        'recommended_canonical': company,
                        'rows': len(group[group['entity_name_clean'] == name]),
                        'total_eur': group[group['entity_name_clean'] == name]['amount_eur'].sum(),
                        'sample_beneficiary': group[group['entity_name_clean'] == name]['beneficiary_name'].iloc[0],
                    })

        if alias_recs:
            rec_df = pd.DataFrame(alias_recs).sort_values('total_eur', ascending=False)
            rec_df.to_csv(out_dir / 'fts_recommended_aliases.csv', index=False, encoding='utf-8')
            log.info(f"\n  Recommended new aliases: {len(rec_df):,}")
            for _, row in rec_df.head(30).iterrows():
                log.info(f"    {row['entity_name_clean']:50s} -> {row['recommended_canonical']:20s} "
                         f"(EUR {row['total_eur']:>12,.0f}, {row['rows']} rows)")

    # High-value unmatched
    hv_df = pd.DataFrame(highvalue_results)
    if not hv_df.empty:
        hv_df = hv_df.sort_values('amount_eur', ascending=False)
        hv_top = hv_df.head(500)
        hv_top.to_csv(out_dir / 'fts_highvalue_unmatched.csv', index=False, encoding='utf-8')
        log.info(f"\nHigh-value unmatched (>EUR 1M): {len(hv_df):,} rows, "
                 f"EUR {hv_df['amount_eur'].sum():,.0f}")
        log.info(f"  Top 10:")
        for _, row in hv_df.head(10).iterrows():
            log.info(f"    {str(row['beneficiary_name'])[:50]:50s} | "
                     f"{row['country']} | EUR {row['amount_eur']:>15,.0f} | "
                     f"{str(row['description'])[:60]}")

    # --- Summary ---
    kw_tier1_count = (kw_df['keyword_tier'] == 'tier1').sum() if not kw_df.empty else 0
    kw_tier2_count = (kw_df['keyword_tier'] == 'tier2').sum() if not kw_df.empty else 0
    kw_total_eur = kw_df['amount_eur'].sum() if not kw_df.empty else 0
    ent_high = len(ent_df[ent_df['confidence'] == 'high']) if not ent_df.empty else 0
    ent_medium = len(ent_df[ent_df['confidence'] == 'medium']) if not ent_df.empty else 0
    ent_total_eur = ent_df['amount_eur'].sum() if not ent_df.empty else 0
    ent_companies = ent_df['matched_company'].nunique() if not ent_df.empty else 0
    alias_count = len(alias_recs) if 'alias_recs' in dir() else 0
    hv_count = len(hv_df) if not hv_df.empty else 0
    hv_eur = hv_df['amount_eur'].sum() if not hv_df.empty else 0

    summary = f"""
FTS DEEP MINING — SUMMARY
{'='*70}

Total FTS rows (primary):     {total_fts:>10,}
Total FTS EUR:                {total_fts_eur:>18,.0f}
Already matched:              {total_fts - total_fts_unmatched:>10,}
Unmatched rows:               {total_fts_unmatched:>10,}
Unmatched EUR:                {total_fts_unmatched_eur:>18,.0f}

KEYWORD MATCHES
  Tier 1 (high-confidence):   {kw_tier1_count:>10,} rows
  Tier 2 (medium):            {kw_tier2_count:>10,} rows
  Total keyword EUR:          {kw_total_eur:>18,.0f}

ENTITY CANDIDATES
  High-confidence:            {ent_high:>10,} rows
  Medium-confidence:          {ent_medium:>10,} rows
  Total entity EUR:           {ent_total_eur:>18,.0f}
  Unique companies found:     {ent_companies:>10,}

RECOMMENDED ALIAS ADDITIONS: {alias_count:>10,}

HIGH-VALUE UNMATCHED (>EUR 1M)
  Total rows:                 {hv_count:>10,}
  Total EUR:                  {hv_eur:>18,.0f}
"""
    log.info(summary)

    with open(out_dir / 'fts_mining_summary.txt', 'w', encoding='utf-8') as f:
        f.write(summary)

    return kw_df, ent_df, hv_df


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='FTS Deep Mining')
    parser.add_argument('company_list_csv', help='Path to company list CSV')
    parser.add_argument('matched_csv', help='Path to already-matched rows CSV')
    parser.add_argument('--output-dir', help='Output directory')
    parser.add_argument('--sector-keywords-json', help='Path to JSON with tier1/tier2 keyword lists')
    args = parser.parse_args()

    sk = None
    if args.sector_keywords_json:
        with open(args.sector_keywords_json) as f:
            sk = json.load(f)

    run_fts_deep_mining(args.company_list_csv, args.matched_csv, args.output_dir, sk)
