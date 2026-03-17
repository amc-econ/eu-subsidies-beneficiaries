#!/usr/bin/env python3
"""
High-Value Unmatched Row Forensics — Generic
===============================================
Identifies the top 500 highest-value unmatched rows across ALL sources
and attempts entity extraction / classification against a user-supplied
company list.

For each row:
  - Attempts company name extraction from beneficiary + description
  - Optionally checks against sector keyword patterns
  - Optionally applies NACE sector code filtering
  - Assigns confidence levels
  - Flags potential new discoveries

Outputs:
  output/highvalue_forensics.csv      -- top 500 with analysis
  output/highvalue_summary.txt        -- human-readable summary
  output/highvalue_new_matches.csv    -- rows identified as likely matches

Usage:
  from src.data_extraction.enrichment.highvalue_forensics import run_highvalue_forensics
  run_highvalue_forensics('companies.csv', 'matched.csv')
"""

import json
import re
import sys
import logging
from pathlib import Path

import pandas as pd

from src.paths import ENRICHMENT_DIR, MATCH_OUTPUT_DIR, read_master_chunked

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
    """Build strong/medium keyword regex from a sector_keywords dict."""
    strong_re = None
    medium_re = None
    if sector_keywords:
        strong_list = sector_keywords.get('strong', sector_keywords.get('tier1', []))
        medium_list = sector_keywords.get('medium', sector_keywords.get('tier2', []))
        if strong_list:
            strong_re = re.compile('|'.join(strong_list), re.IGNORECASE)
        if medium_list:
            medium_re = re.compile('|'.join(medium_list), re.IGNORECASE)
    return strong_re, medium_re


def extract_oc_fields(oc_str) -> dict:
    if pd.isna(oc_str) or not str(oc_str).strip():
        return {}
    try:
        d = json.loads(str(oc_str)) if isinstance(oc_str, str) else oc_str
        return d if isinstance(d, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def run_highvalue_forensics(company_list_csv, matched_csv, output_dir=None, sector_keywords=None, nace_filter=None):
    """
    Run high-value unmatched row forensics for a given company list.

    Parameters
    ----------
    company_list_csv : str or Path
        Path to CSV containing company names.
    matched_csv : str or Path
        Path to CSV of already-matched rows (must have 'source_record_id' column).
    output_dir : str or Path, optional
        Output directory. Defaults to ENRICHMENT_DIR from src.paths.
    sector_keywords : dict, optional
        Dict with 'strong'/'tier1' and 'medium'/'tier2' keys, each a list of regex strings.
        If None, keyword-based signals are skipped.
    nace_filter : str, optional
        NACE code prefix for sector filtering (e.g., '29'). If None, NACE signals are skipped.
    """
    out_dir = Path(output_dir) if output_dir else ENRICHMENT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    log = logging.getLogger(__name__)
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[
            logging.FileHandler(out_dir / 'highvalue_forensics.log', mode='w', encoding='utf-8'),
            logging.StreamHandler(),
        ],
    )

    log.info("=" * 70)
    log.info("HIGH-VALUE UNMATCHED ROW FORENSICS")
    log.info("=" * 70)

    # Build company regex from CSV
    company_regex = _build_company_regex(company_list_csv)
    log.info(f"  Company list: {company_list_csv}")

    # Build keyword patterns (optional)
    strong_re, medium_re = _build_keyword_patterns(sector_keywords)
    use_keywords = strong_re is not None or medium_re is not None
    if use_keywords:
        log.info(f"  Sector keywords: strong={len(sector_keywords.get('strong', sector_keywords.get('tier1', [])))}, "
                 f"medium={len(sector_keywords.get('medium', sector_keywords.get('tier2', [])))}")
    else:
        log.info("  No sector keywords provided — keyword signals skipped")

    if nace_filter:
        log.info(f"  NACE filter: {nace_filter}")

    # Load existing match IDs to exclude
    matched_ids = set()
    matched_csv = Path(matched_csv)
    if matched_csv.exists():
        try:
            auto_df = pd.read_csv(matched_csv, usecols=['source_record_id'], low_memory=True)
            matched_ids = set(auto_df['source_record_id'].astype(str))
            log.info(f"  Loaded {len(matched_ids):,} existing match IDs")
        except Exception as e:
            log.warning(f"  Could not load existing matches: {e}")

    # First pass: collect all unmatched rows > EUR 500K across all sources
    log.info(f"\nPass 1: Collecting high-value unmatched rows from master dataset")
    load_cols = [
        'source', 'source_record_id', 'beneficiary_name', 'entity_name_clean',
        'country', 'amount_eur', 'year', 'description', 'original_columns',
        'is_primary_record', 'programme', 'fund', 'nace_2digit',
        'financial_instrument_class',
    ]

    all_hv = []
    total_rows = 0
    source_stats = {}

    for chunk in read_master_chunked(columns=load_cols, chunksize=250_000):
        primary_mask = chunk['is_primary_record'].astype(str).str.lower() == 'true'
        chunk = chunk[primary_mask].copy()
        total_rows += len(chunk)

        # Exclude already matched
        unmatched_mask = ~chunk['source_record_id'].astype(str).isin(matched_ids)
        unmatched = chunk[unmatched_mask].copy()

        # Collect >500K rows
        hv_mask = unmatched['amount_eur'] > 500_000
        hv = unmatched[hv_mask].copy()
        if len(hv) > 0:
            all_hv.append(hv)

        # Source stats
        for source in unmatched['source'].unique():
            if source not in source_stats:
                source_stats[source] = {'rows': 0, 'eur': 0.0, 'hv_rows': 0}
            s_mask = unmatched['source'] == source
            source_stats[source]['rows'] += s_mask.sum()
            source_stats[source]['eur'] += unmatched.loc[s_mask, 'amount_eur'].sum()
            source_stats[source]['hv_rows'] += (hv_mask & s_mask).sum()

    if not all_hv:
        log.info("No high-value unmatched rows found!")
        return None, None

    hv_df = pd.concat(all_hv, ignore_index=True)
    hv_df = hv_df.sort_values('amount_eur', ascending=False)
    log.info(f"  Total primary rows: {total_rows:,}")
    log.info(f"  High-value unmatched (>EUR 500K): {len(hv_df):,} rows, EUR {hv_df['amount_eur'].sum():,.0f}")

    # Take top 500
    top500 = hv_df.head(500).copy()

    # --- Analyze each row ---
    log.info(f"\nPass 2: Analyzing top {len(top500)} rows for signals...")

    analysis = []
    for idx, row in top500.iterrows():
        ben = str(row.get('beneficiary_name', '') or '')
        desc = str(row.get('description', '') or '')
        oc = extract_oc_fields(row.get('original_columns', ''))
        oc_text = ' '.join(str(v) for v in oc.values()) if oc else ''
        nace = str(row.get('nace_2digit', '') or '')
        all_text = f"{ben} {desc} {oc_text}"

        # Check signals
        has_strong_kw = bool(strong_re.search(all_text)) if strong_re else False
        has_medium_kw = bool(medium_re.search(all_text)) if medium_re else False
        has_company = bool(company_regex.search(ben))
        has_nace_match = nace[:2] == str(nace_filter)[:2] if nace and nace_filter else False

        # Determine match probability
        if has_company and (has_strong_kw or has_nace_match):
            match_probability = 'very_high'
        elif has_company:
            match_probability = 'high'
        elif has_strong_kw and has_nace_match:
            match_probability = 'high'
        elif has_strong_kw:
            match_probability = 'medium'
        elif has_medium_kw and has_nace_match:
            match_probability = 'medium'
        elif has_nace_match:
            match_probability = 'low_nace_only'
        elif has_medium_kw:
            match_probability = 'low_keyword_only'
        else:
            match_probability = 'none'

        # Extract matching keyword
        kw_match = ''
        if has_strong_kw:
            m = strong_re.search(all_text)
            kw_match = m.group(0) if m else ''
        elif has_medium_kw:
            m = medium_re.search(all_text)
            kw_match = m.group(0) if m else ''

        # Extract company match
        company_match = ''
        if has_company:
            m = company_regex.search(ben)
            company_match = m.group(0) if m else ''

        analysis.append({
            'source': row['source'],
            'source_record_id': row['source_record_id'],
            'beneficiary_name': ben[:100],
            'country': row['country'],
            'amount_eur': row['amount_eur'],
            'year': row['year'],
            'description': desc[:200],
            'programme': row.get('programme', ''),
            'nace_2digit': nace,
            'financial_instrument_class': row.get('financial_instrument_class', ''),
            'match_probability': match_probability,
            'keyword_signal': kw_match,
            'company_signal': company_match,
            'has_strong_keyword': has_strong_kw,
            'has_medium_keyword': has_medium_kw,
            'has_known_company': has_company,
            'has_nace_match': has_nace_match,
        })

    analysis_df = pd.DataFrame(analysis)
    analysis_df.to_csv(out_dir / 'highvalue_forensics.csv', index=False, encoding='utf-8')

    # Extract new matches (medium confidence or higher)
    new_matches = analysis_df[analysis_df['match_probability'].isin(
        ['very_high', 'high', 'medium']
    )]
    new_matches.to_csv(out_dir / 'highvalue_new_matches.csv', index=False, encoding='utf-8')

    # --- Summary ---
    log.info(f"\n{'='*70}")
    log.info("FORENSICS RESULTS")
    log.info(f"{'='*70}")

    prob_dist = analysis_df['match_probability'].value_counts()
    log.info(f"\nMatch probability distribution (top {len(analysis_df)} rows):")
    for prob, count in prob_dist.items():
        eur = analysis_df[analysis_df['match_probability'] == prob]['amount_eur'].sum()
        log.info(f"  {prob:25s}: {count:5,} rows, EUR {eur:>18,.0f}")

    log.info(f"\nNew candidates (medium+ confidence):")
    log.info(f"  Rows: {len(new_matches):,}")
    log.info(f"  EUR:  {new_matches['amount_eur'].sum():,.0f}")

    if len(new_matches) > 0:
        log.info(f"\n  Top 20 candidates:")
        for _, row in new_matches.head(20).iterrows():
            log.info(f"    {row['source']:8s} | {row['beneficiary_name'][:45]:45s} | "
                     f"{row['country']} | EUR {row['amount_eur']:>15,.0f} | "
                     f"{row['match_probability']} | {row['keyword_signal'] or row['company_signal']}")

    # Source distribution
    log.info(f"\nUnmatched source stats:")
    for source, stats in sorted(source_stats.items(), key=lambda x: -x[1]['eur']):
        log.info(f"  {source:12s}: {stats['rows']:>10,} rows, EUR {stats['eur']:>18,.0f}, "
                 f"HV(>500K): {stats['hv_rows']:>6,}")

    # Write summary
    summary = f"""
HIGH-VALUE UNMATCHED ROW FORENSICS — SUMMARY
{'='*70}

Total primary rows analyzed:      {total_rows:>10,}
High-value unmatched (>EUR 500K): {len(hv_df):>10,} rows
Top rows analyzed:                {len(analysis_df):>10,}

MATCH PROBABILITY DISTRIBUTION
"""
    for prob, count in prob_dist.items():
        eur = analysis_df[analysis_df['match_probability'] == prob]['amount_eur'].sum()
        summary += f"  {prob:25s}: {count:5,} rows, EUR {eur:>18,.0f}\n"

    summary += f"""
NEW CANDIDATES (medium+ confidence)
  Rows: {len(new_matches):,}
  EUR:  {new_matches['amount_eur'].sum():,.0f}

TOP CANDIDATES:
"""
    for _, row in new_matches.head(20).iterrows():
        summary += (f"  {row['source']:8s} | {row['beneficiary_name'][:45]:45s} | "
                    f"EUR {row['amount_eur']:>12,.0f} | {row['match_probability']}\n")

    with open(out_dir / 'highvalue_summary.txt', 'w', encoding='utf-8') as f:
        f.write(summary)

    return analysis_df, new_matches


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='High-Value Unmatched Row Forensics')
    parser.add_argument('company_list_csv', help='Path to company list CSV')
    parser.add_argument('matched_csv', help='Path to already-matched rows CSV')
    parser.add_argument('--output-dir', help='Output directory')
    parser.add_argument('--sector-keywords-json', help='Path to JSON with strong/medium keyword lists')
    parser.add_argument('--nace-filter', help='NACE code prefix filter (e.g., 29)')
    args = parser.parse_args()

    sk = None
    if args.sector_keywords_json:
        with open(args.sector_keywords_json) as f:
            sk = json.load(f)

    run_highvalue_forensics(args.company_list_csv, args.matched_csv, args.output_dir, sk, args.nace_filter)
