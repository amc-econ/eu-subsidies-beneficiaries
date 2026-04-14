#!/usr/bin/env python3
"""
IPCEI Enrichment — PDF-grounded per-company amounts
=====================================================
Matches a user-supplied company list against per-company aid rows extracted
from the EC IPCEI state-aid decision PDFs. Amounts come from the decision
documents themselves (bracket-range midpoints where the EC redacts exact
figures), **not** from a manually-curated reference CSV.

This module is a thin wrapper around
`src.enrichment.ipcei_pdf_parser.run_ipcei_pdf_extraction`, which does the
actual PDF parsing. The wrapper's job is to:

1. Trigger PDF extraction (or load a cached result).
2. Match the extracted company names against the user's reference list.
3. Produce a `ipcei_matched_participants.csv` with consolidation-ready
   columns: every row carries `source = 'TAM'` (IPCEI participations are
   individually notified as state aid), `ipcei_ticker = <IPCEI name>` so the
   row can still be identified downstream, and `amount_confidence` so
   charts can distinguish confirmed from bracket-range values.

History:
  Before the PDF parser landed, this module read per-company amounts from
  `data/reference/ipcei_participants.csv`, whose `amount_eur_est` column was
  a manual estimate (`amount_confidence = 'estimated'` on every row). That
  path is gone. See research/PIPELINE_AUDIT.md H6.
"""

import logging
import re
import sys
import time
from pathlib import Path

import pandas as pd

from src.paths import ENRICHMENT_DIR, REPO_ROOT
from src.enrichment.ipcei_pdf_parser import run_ipcei_pdf_extraction

sys.stdout.reconfigure(encoding='utf-8')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
log = logging.getLogger(__name__)


DEFAULT_PDF_DIR = REPO_ROOT / 'data' / 'reference' / 'ipcei_decisions'


def _clean_name(name: str) -> str:
    """Lowercase + strip punctuation + drop trailing legal suffixes.

    Shared (lightweight) normalizer for matching PDF-extracted company names
    to a user-supplied reference list. Intentionally simpler than the main
    generic_matcher pipeline because IPCEI participant lists are small and
    the PDF-extracted names are already clean.
    """
    s = str(name).strip().lower()
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = re.sub(
        r'\b(s\.?p\.?a\.?|s\.?r\.?l\.?|gmbh|ag|ltd|inc|plc|llc|corp|'
        r'n\.?v\.?|b\.?v\.?|a\.?s\.?|se|sa|oy|co|kg|e\.?v\.?|\u0026\s*co)\b\.?',
        ' ', s,
    )
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def _load_reference_names(company_list_csv, aliases_json=None) -> dict:
    """Return {clean_name: original_name} — the reference lookup."""
    import json
    df = pd.read_csv(company_list_csv)
    name_col = next((c for c in df.columns if 'name' in c.lower()), df.columns[0])
    names = df[name_col].dropna().astype(str).str.strip().tolist()
    if aliases_json and Path(aliases_json).exists():
        with open(aliases_json, encoding='utf-8') as f:
            aliases = json.load(f)
        for canonical, alias_list in aliases.items():
            names.append(canonical)
            names.extend(alias_list)
    lookup = {}
    for n in names:
        c = _clean_name(n)
        if c and len(c) >= 2:
            lookup.setdefault(c, n)
    return lookup


def _match_pdf_names_to_reference(pdf_names: list[str], reference: dict) -> dict:
    """Return {pdf_name: (reference_name, match_method)}."""
    try:
        from rapidfuzz import fuzz
        _have_rapidfuzz = True
    except ImportError:
        _have_rapidfuzz = False

    results: dict[str, tuple[str, str]] = {}
    for pdf_name in pdf_names:
        pc = _clean_name(pdf_name)
        if not pc or len(pc) < 3:
            continue
        if pc in reference:
            results[pdf_name] = (reference[pc], 'exact')
            continue
        # Conservative substring match (both directions), then optional fuzzy.
        best_name, best_method, best_score = None, None, 0
        if len(pc) >= 5:
            for ref_clean, ref_orig in reference.items():
                if len(ref_clean) < 5:
                    continue
                if min(len(pc), len(ref_clean)) / max(len(pc), len(ref_clean)) < 0.4:
                    continue
                if pc in ref_clean or ref_clean in pc:
                    if best_score < 95:
                        best_name, best_method, best_score = ref_orig, 'substring', 95
        if _have_rapidfuzz and best_score < 90:
            for ref_clean, ref_orig in reference.items():
                s = fuzz.token_sort_ratio(pc, ref_clean)
                if s > best_score and s >= 85:
                    best_name, best_method, best_score = ref_orig, 'fuzzy', s
        if best_name:
            results[pdf_name] = (best_name, best_method)
    return results


def run_ipcei_enrichment(
    company_list_csv,
    aliases_json=None,
    output_dir=None,
    pdf_dir=None,
) -> pd.DataFrame:
    """Match a user company list against the IPCEI decision PDFs.

    Parameters
    ----------
    company_list_csv : str or Path
        Path to the user's company list CSV. The first column containing
        'name' is used as the canonical-name column.
    aliases_json : str or Path, optional
        Path to an aliases JSON of the form
        ``{"Canonical": ["alias1", "alias2"], ...}``.
    output_dir : str or Path, optional
        Enrichment output directory. Defaults to `ENRICHMENT_DIR`.
    pdf_dir : str or Path, optional
        Directory holding the 12 IPCEI decision PDFs. Defaults to the
        shipped `data/reference/ipcei_decisions/` folder.

    Returns
    -------
    pd.DataFrame
        Matched participants with amounts, ready for consolidation. Empty if
        no matches are found. Every row carries `source = 'TAM'` and a
        non-empty `ipcei_ticker` so downstream code can identify the row
        as an IPCEI participation without losing its state-aid lineage.
    """
    t0 = time.time()
    output_dir = Path(output_dir) if output_dir else ENRICHMENT_DIR
    output_dir.mkdir(parents=True, exist_ok=True)
    pdf_dir = Path(pdf_dir) if pdf_dir else DEFAULT_PDF_DIR

    log.info('=' * 70)
    log.info('IPCEI ENRICHMENT (PDF-grounded)')
    log.info('=' * 70)

    # Step 1: PDF extraction (cached on disk — cheap to re-run).
    extracted_df = run_ipcei_pdf_extraction(pdf_dir=pdf_dir, output_dir=output_dir)
    if extracted_df.empty:
        log.warning("  No IPCEI PDF data extracted. Skipping enrichment.")
        return pd.DataFrame()

    # Step 2: Match PDF-extracted names against the user's reference list.
    reference = _load_reference_names(company_list_csv, aliases_json)
    log.info(f"  Reference company list: {len(reference):,} clean names")

    pdf_names = extracted_df['company_name'].dropna().unique().tolist()
    matches = _match_pdf_names_to_reference(pdf_names, reference)
    log.info(f"  Matched: {len(matches)} of {len(pdf_names)} PDF names")

    if not matches:
        log.info("  No IPCEI participants matched — nothing to write.")
        return pd.DataFrame()

    # Step 3: Build consolidation-ready rows for matched participants only.
    matched_df = extracted_df[extracted_df['company_name'].isin(matches.keys())].copy()
    matched_df['match_reference_name'] = matched_df['company_name'].map(
        lambda n: matches[n][0]
    )
    matched_df['ipcei_match_method'] = matched_df['company_name'].map(
        lambda n: matches[n][1]
    )

    # Amount handling (§6.5 / plan A-3 no-invention principle):
    #   amount_eur_low  : bracket low bound from the PDF (e.g. 5 from "[5-10]")
    #   amount_eur_high : bracket high bound
    #   amount_eur      : the CONSERVATIVE lower bound, NOT the midpoint. The
    #                     paper's headline totals then provably understate
    #                     rather than fabricate point estimates. Rows with
    #                     ``amount_confidence = 'exact_from_pdf'`` have
    #                     low == high so the semantics are unchanged for
    #                     exact matches.
    # The old ``aid_nominal_mid_eur`` field is preserved in the CSV for
    # readers who want to recover the midpoint view, but it is no longer
    # the default amount_eur value.
    matched_df['amount_eur_low'] = matched_df['aid_nominal_low_eur']
    matched_df['amount_eur_high'] = matched_df['aid_nominal_high_eur']
    matched_df['amount_eur'] = matched_df['amount_eur_low']

    # Consolidation-ready columns.
    # Internal source stays 'IPCEI_state_aid' so the existing
    # `_flag_ipcei_tam_overlap` dedup step in consolidation.py (which flags
    # central-TAM rows that duplicate an IPCEI participation) continues to
    # operate. In the three-bucket user-facing taxonomy (state aid / EU funds
    # / IFIs) IPCEI rows sit inside "state aid" — both 'TAM' and
    # 'IPCEI_state_aid' map to the same bucket in downstream summary tables
    # and charts. The new `ipcei_ticker` column preserves the IPCEI identity
    # for downstream aggregation and chart labelling.
    matched_df['source'] = 'IPCEI_state_aid'
    matched_df['ipcei_ticker'] = matched_df['ipcei']
    matched_df['beneficiary_name'] = matched_df['company_name']
    matched_df['financial_instrument_class'] = 'grant'
    matched_df['fiscal_source_type'] = 'national_budget'
    matched_df['granularity'] = 'entity'
    matched_df['match_type'] = 'ipcei_reference'
    matched_df['source_record_id'] = matched_df['sa_case']

    # Year from the approval_date field carried through the PDF map.
    def _year_from_date(d):
        try:
            return int(str(d)[:4])
        except (TypeError, ValueError):
            return None
    matched_df['year'] = matched_df['approval_date'].apply(_year_from_date)

    out_path = output_dir / 'ipcei_matched_participants.csv'
    matched_df.to_csv(out_path, index=False)
    log.info(f"  Saved: {out_path}")

    with_amt = matched_df['amount_eur'].notna().sum()
    total_with_amt = matched_df.loc[matched_df['amount_eur'].notna(), 'amount_eur'].sum()
    log.info(f"  Matched rows: {len(matched_df)} total, {with_amt} with PDF-extracted amounts")
    if with_amt:
        log.info(f"  Total matched aid (bracket midpoints): EUR {total_with_amt:,.0f}")
    log.info("  amount_confidence breakdown:")
    for c, n in matched_df['amount_confidence'].value_counts().items():
        log.info(f"    {c:20s}: {n}")

    log.info(f"\n  Runtime: {time.time() - t0:.1f}s")
    return matched_df


def main():
    import argparse
    p = argparse.ArgumentParser(description='IPCEI enrichment — match company list against IPCEI decision PDFs')
    p.add_argument('--company-list', '-c', required=True, help='Path to company list CSV')
    p.add_argument('--aliases', '-a', help='Path to aliases JSON')
    p.add_argument('--output-dir', '-o', help='Output directory')
    p.add_argument('--pdf-dir', help='Directory holding IPCEI decision PDFs')
    args = p.parse_args()
    run_ipcei_enrichment(
        args.company_list,
        aliases_json=args.aliases,
        output_dir=args.output_dir,
        pdf_dir=args.pdf_dir,
    )


if __name__ == '__main__':
    main()
