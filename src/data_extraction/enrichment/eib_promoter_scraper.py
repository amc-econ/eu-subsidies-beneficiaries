#!/usr/bin/env python3
"""
EIB Promoter Name Scraper v2 — Sitemap-Based Enrichment
=========================================================
Scrapes EIB project pages to build a comprehensive title → promoter mapping,
then matches our automotive-relevant EIB projects.

The raw EIB.xlsx has NO project IDs — only sequential row indices as source_record_id.
This script discovers real 8-digit project IDs via the EIB sitemap, fetches each
project page, extracts title + promoter, and builds a reusable lookup.

Strategy:
1. Fetch sitemap → 16K+ project IDs (8-digit YYYYNNNN format)
2. For each project page, extract <title> (= project name) + promoter from HTML
3. Build lookup: title → (project_id, promoter)
4. Match our automotive EIB projects by title
5. Output enrichment table

Cached, resumable, rate-limited (0.5s default).

Usage:
  python -m src.data_extraction.enrichment.eib_promoter_scraper [--limit N] [--rate-limit 0.5]
"""

import pandas as pd
import re
import sys
import json
import time
import logging
import argparse
import signal
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    sys.exit("ERROR: requests required. Install with: pip install requests")

from src.paths import REPO_ROOT, PROCESSED_DIR, ENRICHMENT_DIR

CACHE_DIR    = REPO_ROOT / 'cache' / 'eib_pages'
OUTPUT_DIR   = ENRICHMENT_DIR
LOOKUP_FILE  = OUTPUT_DIR / 'eib_title_promoter_lookup.json'

EIB_CSV = PROCESSED_DIR / 'standardized_EIB.csv'
SITEMAP_URL = 'https://www.eib.org/en/sitemaps/dynamic/plr/project.xml'

# Graceful shutdown
_shutdown = False

def _handle_signal(sig, frame):
    global _shutdown
    _shutdown = True
    log.info("Shutdown requested — finishing current request...")

signal.signal(signal.SIGINT, _handle_signal)
signal.signal(signal.SIGTERM, _handle_signal)


def fetch_sitemap_ids(session: requests.Session) -> list:
    """Fetch all EIB project IDs from the sitemap."""
    log.info(f"Fetching sitemap: {SITEMAP_URL}")
    resp = session.get(SITEMAP_URL, timeout=60)
    resp.raise_for_status()
    ids = re.findall(r'/en/projects/all/(\d+)', resp.text)
    log.info(f"  Found {len(ids):,} project IDs in sitemap")
    return ids


def scrape_project_page(project_id: str, session: requests.Session) -> dict:
    """Fetch an EIB project page, extract title + promoter."""
    cache_file = CACHE_DIR / f'{project_id}.json'
    if cache_file.exists():
        try:
            with open(cache_file, encoding='utf-8') as f:
                data = json.load(f)
            # Only use cache if it has the 'title' field (v2 format)
            if 'title' in data:
                return data
        except (json.JSONDecodeError, KeyError):
            pass  # Re-fetch if cache is corrupt or old format

    url = f'https://www.eib.org/en/projects/all/{project_id}'
    try:
        resp = session.get(url, timeout=30)
        if resp.status_code != 200:
            result = {
                'project_id': project_id, 'status': resp.status_code,
                'title': None, 'promoter': None,
            }
            _save_cache(cache_file, result)
            return result

        html = resp.text

        # Extract title from <title> tag
        m = re.search(r'<title[^>]*>([^<]+)</title>', html)
        title = m.group(1).strip() if m else None

        # Extract promoter from HTML structure
        promoter = _extract_promoter(html)

        result = {
            'project_id': project_id,
            'status': 200,
            'title': title,
            'promoter': promoter,
            'url': url,
        }

    except requests.Timeout:
        result = {
            'project_id': project_id, 'status': 'timeout',
            'title': None, 'promoter': None,
        }
    except Exception as e:
        result = {
            'project_id': project_id, 'status': 'error',
            'title': None, 'promoter': None, 'error': str(e),
        }

    _save_cache(cache_file, result)
    return result


def _extract_promoter(html: str) -> str:
    """Extract promoter name from EIB project page HTML.

    The HTML structure (confirmed Feb 2026):
        <div class="eib-list__row eib-list__row--header">
            <div class="eib-list__column">
                <div class="eib-typography__secondary-label">Project name</div>
            </div>
            <div class="eib-list__column">
                <div class="eib-typography__secondary-label">Promoter - financial intermediary</div>
            </div>
        </div>
        <div class="eib-list__row eib-list__row--body">
            <div class="eib-list__column">
                <div class="eib-typography__data-sheet--x-small">PROJECT NAME</div>
            </div>
            <div class="eib-list__column">
                <div class="eib-typography__data-sheet--x-small">PROMOTER NAME</div>
            </div>
        </div>
    """
    # Primary pattern: look for the body row after "Promoter - financial intermediary"
    # Extract both data-sheet values — first is project name, second is promoter
    m = re.search(
        r'Promoter\s*[-\u2013\u2014]\s*[Ff]inancial\s+[Ii]ntermediary.*?'
        r'eib-list__row--body.*?'
        r'eib-typography__data-sheet--x-small[^>]*>\s*([^<]+).*?'
        r'eib-typography__data-sheet--x-small[^>]*>\s*([^<]+)',
        html, re.DOTALL
    )
    if m:
        promoter = m.group(2).strip()
        if promoter and len(promoter) > 1:
            return promoter

    # Fallback: look for any "Promoter" heading followed by a company-like name
    m = re.search(
        r'(?:Promoter|Borrower)\s*(?:[-\u2013\u2014:]\s*)?'
        r'(?:[Ff]inancial\s+[Ii]ntermediary\s*)?'
        r'.*?eib-typography__data-sheet[^>]*>\s*([A-Z][^<]{3,80})',
        html, re.DOTALL
    )
    if m:
        promoter = m.group(1).strip()
        if promoter and len(promoter) > 1:
            return promoter

    return None


def _save_cache(path: Path, data: dict):
    """Save cache file with error handling."""
    try:
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"  Cache write failed for {path.name}: {e}")


def build_title_lookup(session: requests.Session, project_ids: list,
                       rate_limit: float = 0.5, limit: int = 0) -> dict:
    """
    Fetch project pages and build title → {project_id, promoter} lookup.
    Resumable via per-page JSON cache.
    """
    lookup = {}
    fetched = 0
    cached = 0
    errors = 0

    if limit > 0:
        project_ids = project_ids[:limit]

    total = len(project_ids)
    log.info(f"Building title→promoter lookup from {total:,} project pages...")

    for i, pid in enumerate(project_ids):
        if _shutdown:
            log.info(f"Shutdown: stopping after {i} pages")
            break

        cache_file = CACHE_DIR / f'{pid}.json'
        was_cached = cache_file.exists()

        # Check if cache has v2 format (with 'title')
        if was_cached:
            try:
                with open(cache_file, encoding='utf-8') as f:
                    data = json.load(f)
                if 'title' in data:
                    cached += 1
                    if data.get('title') and data.get('status') == 200:
                        lookup[data['title'].upper()] = {
                            'project_id': pid,
                            'promoter': data.get('promoter'),
                        }
                    if (i + 1) % 500 == 0:
                        log.info(f"  Progress: {i+1:,}/{total:,} (fetched={fetched}, cached={cached}, lookup={len(lookup)}, errors={errors})")
                    continue
            except (json.JSONDecodeError, KeyError):
                pass  # Re-fetch

        # Fetch the page
        result = scrape_project_page(pid, session)
        fetched += 1

        if result.get('status') == 200 and result.get('title'):
            lookup[result['title'].upper()] = {
                'project_id': pid,
                'promoter': result.get('promoter'),
            }
        elif result.get('status') not in (200, 404):
            errors += 1

        if (i + 1) % 100 == 0:
            log.info(f"  Progress: {i+1:,}/{total:,} (fetched={fetched}, cached={cached}, lookup={len(lookup)}, errors={errors})")

        time.sleep(rate_limit)

    log.info(f"  Completed: {fetched} fetched, {cached} cached, {errors} errors")
    log.info(f"  Lookup size: {len(lookup):,} titles with promoter data")

    return lookup


def match_automotive_projects(lookup: dict) -> pd.DataFrame:
    """Match our automotive EIB projects against the title→promoter lookup."""
    log.info("Loading EIB standardized data...")
    eib = pd.read_csv(EIB_CSV, low_memory=False)
    eib_primary = eib[eib['is_primary_record'] == True].copy()
    log.info(f"  Total EIB rows: {len(eib):,}, primary: {len(eib_primary):,}")

    # Get unique titles
    title_col = 'beneficiary_name'
    unique_titles = eib_primary[title_col].dropna().unique()
    log.info(f"  Unique EIB project titles: {len(unique_titles):,}")

    # Match against lookup
    results = []
    matched = 0
    promoter_found = 0

    for title in unique_titles:
        key = title.strip().upper()
        if key in lookup:
            entry = lookup[key]
            matched += 1
            if entry.get('promoter'):
                promoter_found += 1
            results.append({
                'project_title': title,
                'eib_project_id': entry['project_id'],
                'promoter': entry.get('promoter'),
            })

    log.info(f"  Title matches: {matched:,} / {len(unique_titles):,}")
    log.info(f"  With promoter: {promoter_found:,}")

    return pd.DataFrame(results)


def main():
    parser = argparse.ArgumentParser(description='EIB Promoter Scraper v2')
    parser.add_argument('--limit', type=int, default=0,
                        help='Limit number of pages to fetch (0=all)')
    parser.add_argument('--rate-limit', type=float, default=0.5,
                        help='Seconds between requests (default 0.5)')
    parser.add_argument('--match-only', action='store_true',
                        help='Skip fetching, just match from existing cache')
    args = parser.parse_args()

    t0 = time.time()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    log.info("=" * 70)
    log.info("EIB PROMOTER SCRAPER v2 — Sitemap-Based")
    log.info("=" * 70)

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (research-project; EU subsidy analysis) Python/3.11',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })

    if args.match_only:
        # Build lookup from cache only
        log.info("Match-only mode: building lookup from cache...")
        lookup = {}
        cache_files = list(CACHE_DIR.glob('*.json'))
        for cf in cache_files:
            try:
                with open(cf, encoding='utf-8') as f:
                    data = json.load(f)
                if data.get('title') and data.get('status') == 200:
                    lookup[data['title'].upper()] = {
                        'project_id': data['project_id'],
                        'promoter': data.get('promoter'),
                    }
            except (json.JSONDecodeError, KeyError):
                pass
        log.info(f"  Loaded {len(lookup):,} entries from {len(cache_files):,} cache files")
    else:
        # Fetch sitemap and scrape
        project_ids = fetch_sitemap_ids(session)
        lookup = build_title_lookup(
            session, project_ids,
            rate_limit=args.rate_limit,
            limit=args.limit,
        )

    # Save full lookup
    with open(LOOKUP_FILE, 'w', encoding='utf-8') as f:
        json.dump(lookup, f, ensure_ascii=False, indent=1)
    log.info(f"  Saved lookup: {LOOKUP_FILE}")

    # Count promoter coverage
    with_promoter = sum(1 for v in lookup.values() if v.get('promoter'))
    log.info(f"  Projects with promoter: {with_promoter:,} / {len(lookup):,} ({100*with_promoter/max(len(lookup),1):.0f}%)")

    # Match automotive projects
    matches_df = match_automotive_projects(lookup)
    if len(matches_df) > 0:
        out_path = OUTPUT_DIR / 'eib_promoter_lookup.csv'
        matches_df.to_csv(out_path, index=False)
        log.info(f"  Saved: {out_path}")

        # Also join back to get amounts
        eib = pd.read_csv(EIB_CSV, low_memory=False)
        eib_primary = eib[eib['is_primary_record'] == True].copy()

        enriched = eib_primary.merge(
            matches_df, left_on='beneficiary_name', right_on='project_title', how='inner'
        )
        with_promoter_rows = enriched[enriched['promoter'].notna()]
        log.info(f"\n  Enriched EIB rows with promoter: {len(with_promoter_rows):,}")
        log.info(f"  EUR covered: {with_promoter_rows['amount_eur'].sum():,.0f}")

        out_enriched = OUTPUT_DIR / 'eib_enriched.csv'
        enriched.to_csv(out_enriched, index=False)
        log.info(f"  Saved: {out_enriched}")

    # Summary
    log.info("")
    log.info("=" * 70)
    log.info("EIB SCRAPER SUMMARY")
    log.info("=" * 70)
    log.info(f"Lookup entries: {len(lookup):,}")
    log.info(f"With promoter: {with_promoter:,}")
    if len(matches_df) > 0:
        promo = matches_df['promoter'].notna().sum()
        log.info(f"EIB titles matched: {len(matches_df):,}")
        log.info(f"  With promoter: {promo}")
        if promo > 0:
            log.info("")
            log.info("Sample promoters found:")
            for _, r in matches_df[matches_df['promoter'].notna()].head(30).iterrows():
                log.info(f"  {r['project_title'][:50]:50s} → {r['promoter'][:45]}")

    elapsed = time.time() - t0
    log.info(f"\nRuntime: {elapsed:.1f}s")


if __name__ == '__main__':
    main()
