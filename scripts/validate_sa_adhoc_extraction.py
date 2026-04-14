#!/usr/bin/env python3
"""Real-PDF validation run for ``sa_adhoc_parser.extract_amount_from_text``.

Samples 20 random eligible ad hoc cases from ``case-data-SA.json``
(seed=42 for reproducibility), downloads each PDF via the
``SACofinParser`` cache infrastructure (reused, so this is free if the
cache is already warm), runs the amount extraction ladder on the first
15 pages, and prints a per-case hit table plus aggregate numbers.

Why this script exists: the amount regex ladder was originally
validated on 9 synthetic strings (plan audit A-9). The 20-PDF run
gives an honest real-world hit rate that can be cited in METHODOLOGY
§12. Output is also written to
``data/cache/sa_decisions/adhoc_validation.json`` for the morning
report.
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# Allow running the script directly without ``python -m``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.paths import REPO_ROOT
from src.enrichment.sa_adhoc_parser import (
    enumerate_adhoc_cases,
    extract_amount_from_text,
    extract_beneficiary_from_title,
)
from src.utils.progress import ProgressTicker

try:
    import requests
except ImportError:
    sys.exit('requests not installed')

CACHE_DIR = REPO_ROOT / 'data' / 'cache' / 'sa_decisions' / 'validation'
OUTPUT_PATH = REPO_ROOT / 'data' / 'cache' / 'sa_decisions' / 'adhoc_validation.json'


def _fetch_pdf(sa_case: str, url: str) -> Path | None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    safe = sa_case.replace('.', '_').replace('/', '_')
    out = CACHE_DIR / f'{safe}.pdf'
    if out.exists() and out.stat().st_size > 0:
        return out
    try:
        resp = requests.get(url, timeout=45, headers={
            'User-Agent': 'Mozilla/5.0 (research-project) Python/3.11',
        })
        if resp.status_code == 200 and resp.content[:4] == b'%PDF':
            out.write_bytes(resp.content)
            time.sleep(1.0)  # polite rate-limit
            return out
        print(f'  {sa_case}: HTTP {resp.status_code}, {len(resp.content)} bytes')
        return None
    except Exception as exc:
        print(f'  {sa_case}: download error {exc}')
        return None


def _extract_pdf_text(cache_path: Path, max_pages: int = 15) -> str | None:
    try:
        import pdfplumber
    except ImportError:
        return None
    try:
        with pdfplumber.open(str(cache_path)) as pdf:
            pages = pdf.pages[:max_pages]
            out = []
            for p in pages:
                t = p.extract_text()
                if t:
                    out.append(t)
            return '\n'.join(out)
    except Exception as exc:
        print(f'  pdfplumber error on {cache_path.name}: {exc}')
        return None


_CURRENCY_RXS = {
    'GBP': re.compile(r'\b(?:GBP|pound\s+sterling|£)\b', re.IGNORECASE),
    'USD': re.compile(r'\b(?:USD|US\$|\$\d|United\s+States\s+dollar)\b', re.IGNORECASE),
    'PLN': re.compile(r'\bPLN\b|\bzloty\b|Polish\s+zloty', re.IGNORECASE),
    'HUF': re.compile(r'\bHUF\b|Hungarian\s+forint', re.IGNORECASE),
    'CZK': re.compile(r'\bCZK\b|Czech\s+koruna', re.IGNORECASE),
    'RON': re.compile(r'\bRON\b|Romanian\s+leu', re.IGNORECASE),
    'BGN': re.compile(r'\bBGN\b|Bulgarian\s+lev', re.IGNORECASE),
}
_EUR_RX = re.compile(r'\b(?:EUR|euros?)\b|\u20AC', re.IGNORECASE)
_EUR_NUMBER_RX = re.compile(r'(?:EUR|\u20AC)\s*\d', re.IGNORECASE)


def classify_failure(text: str | None, val, conf: str) -> str:
    import re as _re
    if conf == 'regex_exact':
        return 'extracted'
    if conf == 'suspect_fallthrough':
        return 'redaction_fallthrough'
    if text is None:
        return 'pdf_parse_failed'
    has_eur = bool(_EUR_RX.search(text))
    # Detect any non-EUR currency the decision mentions (word-boundary to avoid
    # matching substrings like "iron" / "environment").
    currencies = [code for code, rx in _CURRENCY_RXS.items() if rx.search(text)]
    if not has_eur and currencies:
        return f'non_EUR_currency:{"+".join(currencies).lower()}'
    if not has_eur:
        return 'no_EUR_mention'
    if _EUR_NUMBER_RX.search(text):
        return 'ladder_miss_format'
    return 'no_amount_in_text'


def main(limit: int = 20, seed: int = 42, case_file: Path | None = None):
    case_file = case_file or (REPO_ROOT / 'case-data-SA.json')
    print(f'Loading case registry from {case_file} ...')
    cases = enumerate_adhoc_cases(case_file)
    print(f'  eligible ad hoc cases: {len(cases):,}')

    random.seed(seed)
    sample = random.sample(list(cases), min(limit, len(cases)))
    print(f'  sampled {len(sample)} cases (seed={seed})')

    ticker = ProgressTicker(total=len(sample), name='sa_adhoc val', every=5)

    results = []
    failure_counts: dict[str, int] = {}
    for i, case in enumerate(sample):
        sa_code = case.get('sa_case') or 'unknown'
        title = case.get('title', '') or ''
        url = case.get('pdf_url', '')
        beneficiary = extract_beneficiary_from_title(title)
        pdf_path = _fetch_pdf(sa_code, url) if url else None
        text = _extract_pdf_text(pdf_path) if pdf_path else None
        if text:
            val, conf, snippet = extract_amount_from_text(text)
        else:
            val, conf, snippet = None, 'not_extracted', ''

        bucket = classify_failure(text, val, conf)
        failure_counts[bucket] = failure_counts.get(bucket, 0) + 1

        results.append({
            'sa_code': sa_code,
            'title': title[:80],
            'extracted_beneficiary': beneficiary,
            'amount_eur': val,
            'amount_confidence': conf,
            'evidence_snippet': snippet[:160],
            'bucket': bucket,
        })
        ticker.tick(success=(conf == 'regex_exact'))

    ticker.finalise()

    # --------------- Report ---------------
    print()
    print('=' * 80)
    print('SA AD HOC AMOUNT EXTRACTION — 20-PDF REAL VALIDATION')
    print('=' * 80)
    print(f'Sample size: {len(sample)}')
    n_extracted = sum(1 for r in results if r['amount_confidence'] == 'regex_exact')
    n_fallthrough = sum(
        1 for r in results if r['amount_confidence'] == 'suspect_fallthrough'
    )
    print(f'  extracted:            {n_extracted}')
    print(f'  suspect_fallthrough:  {n_fallthrough}')
    print(f'  failed/not-extracted: {len(sample) - n_extracted - n_fallthrough}')
    print()
    print('Failure bucket breakdown:')
    for b, c in sorted(failure_counts.items(), key=lambda x: -x[1]):
        print(f'  {c:3d}  {b}')
    print()
    print('Per-case results:')
    print(f'{"sa_code":12s} {"bucket":25s} {"eur":>15s}  title')
    for r in results:
        eur = f"{r['amount_eur']:,.0f}" if r['amount_eur'] else '-'
        print(
            f'{r["sa_code"]:12s} {r["bucket"]:25s} {eur:>15s}  {r["title"][:50]}'
        )

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_PATH.open('w', encoding='utf-8') as f:
        json.dump({
            'sample_size': len(sample),
            'seed': seed,
            'extracted': n_extracted,
            'suspect_fallthrough': n_fallthrough,
            'failure_buckets': failure_counts,
            'results': results,
        }, f, ensure_ascii=False, indent=1, default=str)
    print()
    print(f'Wrote: {OUTPUT_PATH}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--seed', type=int, default=42)
    args = parser.parse_args()
    main(limit=args.limit, seed=args.seed)
