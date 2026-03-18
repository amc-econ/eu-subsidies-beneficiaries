#!/usr/bin/env python3
"""
GENERIC ENTITY MATCHER — Two-Layer Matching Pipeline
=====================================================
Layer A: Direct entity matching (entity_name_clean vs reference list)
Layer B: Contextual text matching (description, original_columns fields)

Forked from automotive_matcher.py (v6.4) — all automotive-specific constants,
sector buckets, and company classifications have been extracted.

This matcher accepts ANY company list CSV with a 'company_name' column and
optional aliases JSON. No sector-specific knowledge required.

KEY OPTIMISATION: Deduplicates entity names FIRST (~920K unique names vs 5.2M rows),
matches the unique set, then joins results back. Reduces fuzzy matching calls by ~5x.

Layer B uses a single precompiled regex of all reference names to scan text fields
in one pass — no per-row × per-ref inner loop.

Reads master_dataset.csv in chunks (memory-safe for 6+ GB files).
Uses rapidfuzz for fuzzy matching. No ML dependencies.

Usage:
    from src.matching.generic_matcher import run_matching, MatchConfig

    run_matching(
        master_csv=Path('data/processed/master_dataset.csv'),
        company_list_csv=Path('my_companies.csv'),    # needs 'company_name' column
        aliases_json=Path('my_aliases.json'),          # optional
        output_dir=Path('output/'),
    )
"""

import json
import logging
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd


def _resolve_master(master_csv: Path) -> Path:
    """Resolve master dataset path, preferring .parquet over .csv."""
    if master_csv.exists():
        return master_csv
    # Try parquet variant
    pq_path = master_csv.with_suffix('.parquet')
    if pq_path.exists():
        return pq_path
    # Try csv variant if parquet was given
    if master_csv.suffix == '.parquet':
        csv_path = master_csv.with_suffix('.csv')
        if csv_path.exists():
            return csv_path
    return master_csv  # will fail later with a clear error


def _read_chunks(path: Path, columns=None, chunksize=250_000, dtype=None):
    """Read a CSV or Parquet file in chunks. Yields DataFrames."""
    if path.suffix == '.parquet':
        # Stream parquet row-groups via pyarrow — never loads the full file into RAM.
        # Peak memory ≈ chunksize rows × n_columns, not the full file.
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(path)
        for batch in pf.iter_batches(batch_size=chunksize, columns=columns):
            yield batch.to_pandas()
    else:
        # CSV: chunked read
        kwargs = {'chunksize': chunksize, 'low_memory': True}
        if columns:
            kwargs['usecols'] = columns
        if dtype:
            kwargs['dtype'] = dtype
        yield from pd.read_csv(path, **kwargs)

try:
    from rapidfuzz import fuzz, process
except ImportError:
    sys.exit("ERROR: rapidfuzz required. Install with: pip install rapidfuzz")

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# MATCH CONFIGURATION
# ---------------------------------------------------------------------------

@dataclass
class MatchConfig:
    """
    Controls matching thresholds and behaviour.

    All defaults mirror the proven automotive matcher settings.
    Override for domain-specific tuning.
    """

    # Fuzzy matching thresholds
    fuzzy_high_threshold: int = 85
    fuzzy_medium_threshold: int = 75
    token_overlap_min: int = 2
    length_ratio_max: float = 2.5
    short_name_max_len: int = 5
    chunk_size: int = 250_000

    # Names that should only match via exact lookup (too short/ambiguous for fuzzy).
    # Empty by default — the short_name_max_len guard already handles short names.
    # Sector-specific examples (e.g. automotive acronyms) should be passed via config.
    exact_only_names: frozenset[str] = field(default_factory=frozenset)

    # Names excluded from the reference list (too generic)
    master_exclude: frozenset[str] = field(default_factory=lambda: frozenset({
        'group', 'other', 'others', 'mobility', 'gac', 'total',
        'grand total', 'all others', 'rest',
    }))

    # Names blocked from Layer B contextual matching (common words)
    contextual_blocklist: frozenset[str] = field(default_factory=lambda: frozenset())

    # Known false positive pairs: frozenset of (ref_clean, entity_clean) tuples
    false_positive_pairs: frozenset[tuple[str, str]] = field(default_factory=lambda: frozenset())

    # Beneficiary name FP patterns: dict of ref_name → compiled regex
    # If a matched row's beneficiary_name matches the regex, the match is vetoed
    beneficiary_fp_patterns: dict[str, re.Pattern] = field(default_factory=dict)

    # Source-specific fields for Layer B contextual matching
    source_context_fields: dict[str, list[str]] = field(default_factory=lambda: {
        'TAM':      ['AM_TITLE', 'AM_TITLE_EN', 'GRANTING_AUTHORITY_NAME'],
        'FTS':      ['Subject of grant or contract', 'Budget line name'],
        'EIB':      ['Region'],
        'EBRD':     ['Direct/Regional', 'Portfolio Class'],
        'KOHESIO':  [],
        'CINEA':    [],
        'INNOVFUND': [],
        'RESEARCH': [],
        'RRF':      ['Component Name', 'Measure Description'],
    })

    # Sources worth running Layer B on
    layer_b_sources: frozenset[str] = field(default_factory=lambda: frozenset({
        'TAM', 'FTS', 'EIB', 'EBRD', 'KOHESIO', 'CINEA', 'INNOVFUND',
    }))

    # Prefix for output columns (e.g., 'match_flag', 'match_score', etc.)
    output_prefix: str = 'match'


# ---------------------------------------------------------------------------
# Columns needed from master_dataset (keeps RAM low)
# ---------------------------------------------------------------------------
LOAD_COLS = [
    'source', 'source_record_id', 'beneficiary_name', 'country',
    'amount_eur', 'year', 'description', 'original_columns',
    'financial_instrument_class', 'flow_stage_group', 'flow_stage',
    'fiscal_source_type',
    'is_primary_record', 'entity_name_raw', 'entity_name_clean',
    'entity_id', 'entity_type', 'sector_description', 'nace_2digit',
    'exclude_reason', 'programme', 'fund',
]


# ---------------------------------------------------------------------------
# COUNTRY TOKENS — too generic to count as "significant" for token overlap
# ---------------------------------------------------------------------------
COUNTRY_TOKENS = frozenset({
    'germany', 'france', 'italy', 'spain', 'poland', 'romania',
    'netherlands', 'belgium', 'czech', 'austria', 'sweden',
    'hungary', 'portugal', 'greece', 'finland', 'denmark',
    'ireland', 'croatia', 'slovakia', 'slovenia', 'lithuania',
    'latvia', 'estonia', 'luxembourg', 'malta', 'cyprus', 'bulgaria',
    'europe', 'european', 'central', 'eastern', 'western', 'northern',
    'united', 'kingdom', 'uk', 'global', 'international',
    'italia', 'italiana', 'italiano', 'hungaria', 'hungaro',
    'deutschland', 'deutsche', 'deutscher', 'francais', 'francaise',
    'espana', 'espanol', 'portuguesa', 'ceska', 'cesky',
    'polska', 'polski', 'romana', 'slovensko', 'slovensky',
    'nederland', 'belgique', 'osterreich', 'schweiz', 'suisse',
    'wroclaw', 'brandenburg', 'sachsen', 'navarra', 'saarland',
})

# Tokens that inflate token_set_ratio when shared between unrelated entities.
TRIVIAL_TOKENS = frozenset({
    # Generic business words
    'company', 'manufacturing', 'group', 'holding', 'holdings',
    'corporation', 'limited', 'international', 'industries',
    'enterprise', 'enterprises', 'incorporated', 'association',
    # German legal fragments
    'gesellschaft', 'beschrankter', 'haftung', 'aktiengesellschaft',
    'kommanditgesellschaft', 'offene', 'handelsgesellschaft',
    # Italian legal fragments
    'societa', 'azioni', 'responsabilita', 'limitata',
    'forma', 'abbreviata', 'oppure', 'breve',
    # Hungarian legal fragments (abbreviated + full-form)
    'reszvenytarsasag', 'zartkoruen', 'mukodo', 'korlatolt',
    'felelossegu', 'tarsasag', 'nyilvanosan',
    'gyarto', 'forgalmazo', 'gyartasi', 'szolgaltato',
    'kereskedelmi', 'gepjarmu', 'alkatresz',
    # French/Spanish/Portuguese legal
    'societe', 'anonyme', 'responsabilite', 'limitee',
    'sociedad', 'anonima', 'responsabilidad',
    # Portuguese/Spanish sole-member forms
    'unipessoal', 'lda', 'unipersonal',
    # Polish sp. z o.o.
    'zoo',
    # Generic industry words
    'systems', 'technology', 'technologies', 'engineering',
    'services', 'solutions', 'components', 'parts',
    'production', 'produktion', 'werke', 'fabrik',
})

GENERIC_NAMES = frozenset({
    'systems', 'energy', 'finance', 'transport',
    'technology', 'engineering', 'manufacturing', 'industry',
    'electric', 'green', 'innovation', 'development',
    'infrastructure', 'logistics', 'digital', 'services',
})


# ---------------------------------------------------------------------------
# Cleaning — exact replica of harmonization/utils.clean_entity_name
# ---------------------------------------------------------------------------
_LEGAL_SUFFIXES = [
    r'gesellschaft\s+mit\s+beschr[äa]nkter\s+haftung',
    r'societe\s+anonyme', r'limited\s+liability\s+company',
    r'corporation', r'incorporated', r'limited',
    r'aktiengesellschaft', r'aktiebolag',
    r'gmbh\s*&?\s*co\.?\s*kg', r'gmbh',
    r's\s*\.?\s*p\s*\.?\s*a\.?', r's\s*\.?\s*r\s*\.?\s*l\.?',
    r's\s*\.?\s*r\s*\.?\s*o\.?', r's\s*\.?\s*a\s*\.?\s*s\.?',
    r's\s*\.?\s*a\s*\.?\s*r\s*\.?\s*l\.?',
    r'n\s*\.?\s*v\.?', r'b\s*\.?\s*v\.?',
    r's\.?a\.?', r'a\.?s\.?', r'a\.?g\.?',
    r'plc', r'ltd', r'llc', r'inc', r'corp',
    r'co\.?', r'ag', r'ab', r'se', r'sa', r'as',
    r'oy', r'oyj', r'ehf', r'hf',
    r'd\.?o\.?o\.?', r'sp\.?\s*z\.?\s*o\.?\s*o\.?',
    r'kft', r'zrt', r'nyrt', r'bt',
    # Hungarian full-form legal suffixes
    r'z[áa]rtk[öo]r[űu]en\s+m[űu]k[öo]d[öo]\s+r[ée]szv[ée]nyt[áa]rsas[áa]g',
    r'korl[áa]tolt\s+felel[őo]ss[ée]g[űu]\s+t[áa]rsas[áa]g',
    r'nyilv[áa]nosan\s+m[űu]k[öo]d[öo]\s+r[ée]szv[ée]nyt[áa]rsas[áa]g',
    r'gy[áa]rt[óo]\s+[ée]s\s+forgalmaz[óo]\s+korl[áa]tolt\s+felel[őo]ss[ée]g[űu]\s+t[áa]rsas[áa]g',
    r'aps', r'a/s',
    r'srl', r'spa', r'nv', r'bv',
    r's\sp\sa', r's\sr\sl', r's\sr\so', r'b\sv', r'n\sv',
    # Portuguese legal forms (unipessoal lda = sole-member limited liability)
    r'unipessoal\s+lda', r'unipessoal',
    r'lda',               # Portuguese Limitada
    r'unipersonal',       # Spanish equivalent
    r'societa\s+unipersonale',  # Italian equivalent
    # Polish sp. z o.o. abbreviation
    r'zoo',
]
_LEGAL_SUFFIX_RE = re.compile(
    r'\b(?:' + '|'.join(_LEGAL_SUFFIXES) + r')\b\.?',
    re.IGNORECASE,
)


def clean_name(name) -> str:
    """Clean entity name: lowercase, strip legal suffixes, normalize whitespace."""
    if pd.isna(name):
        return ''
    s = str(name).strip().lower()
    if not s:
        return ''
    s = re.sub(r'\([^)]*\)', ' ', s)
    s = _LEGAL_SUFFIX_RE.sub(' ', s)
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def significant_tokens(name: str) -> set[str]:
    """Extract significant tokens (not country names, not trivial legal words)."""
    return {t for t in name.split()
            if t not in COUNTRY_TOKENS
            and t not in TRIVIAL_TOKENS
            and len(t) > 1}


# ---------------------------------------------------------------------------
# Reference list builder — GENERIC: accepts any CSV with company_name column
# ---------------------------------------------------------------------------
def build_reference_list(
    company_list_csv: Path,
    aliases_json: Path | None = None,
    config: MatchConfig | None = None,
) -> tuple[pd.DataFrame, dict]:
    """
    Build reference list from a generic company CSV.

    Parameters
    ----------
    company_list_csv : Path
        CSV with at least a 'company_name' column.
        Optional columns: 'country', 'source' (for tracking provenance).
    aliases_json : Path, optional
        JSON file mapping alias → canonical name.
    config : MatchConfig, optional

    Returns
    -------
    ref_df : pd.DataFrame
        Columns: ref_name_raw, ref_name_clean, ref_country, ref_source
    aliases : dict
        alias → canonical name mapping
    """
    if config is None:
        config = MatchConfig()

    records = []

    # Load company list
    companies = pd.read_csv(company_list_csv)
    if 'company_name' not in companies.columns:
        raise ValueError(f"company_list_csv must have a 'company_name' column. "
                         f"Found: {list(companies.columns)}")

    has_country = 'country' in companies.columns
    has_source = 'source' in companies.columns

    for _, row in companies.iterrows():
        raw = row['company_name']
        if pd.isna(raw) or not str(raw).strip():
            continue
        clean = clean_name(raw)
        if not clean or clean in config.master_exclude:
            continue
        country = str(row['country']).strip()[:2] if has_country and pd.notna(row.get('country')) else ''
        source = str(row['source']).strip() if has_source and pd.notna(row.get('source')) else 'company_list'
        records.append({
            'ref_name_raw': str(raw).strip(),
            'ref_name_clean': clean,
            'ref_country': country,
            'ref_source': source,
        })

    # Load aliases
    aliases = {}
    if aliases_json and aliases_json.exists():
        with open(aliases_json, 'r', encoding='utf-8') as f:
            aliases = json.load(f)
        aliases = {k: v for k, v in aliases.items() if not k.startswith('_')}

        # Add alias canonical names not already in reference list
        existing_clean = {r['ref_name_clean'] for r in records}
        for canonical in set(aliases.values()):
            canonical_clean = clean_name(canonical)
            if canonical_clean and canonical_clean not in existing_clean and canonical_clean not in config.master_exclude:
                records.append({
                    'ref_name_raw': canonical,
                    'ref_name_clean': canonical_clean,
                    'ref_country': '',
                    'ref_source': 'aliases',
                })
                existing_clean.add(canonical_clean)

    ref_df = pd.DataFrame(records)
    log.info(f"Reference list: {len(ref_df)} entries from {company_list_csv.name}")
    return ref_df, aliases


def build_exact_lookup(ref_df: pd.DataFrame, aliases: dict) -> dict:
    """Build exact name → (canonical_clean, notes) lookup including aliases."""
    lookup = {}
    for clean in ref_df['ref_name_clean'].unique():
        lookup[clean] = (clean, '')
    for alias_key, canonical in aliases.items():
        canonical_clean = clean_name(canonical)
        if canonical_clean in lookup or canonical_clean in set(ref_df['ref_name_clean']):
            alias_clean = clean_name(alias_key)
            if alias_clean and alias_clean not in lookup:
                lookup[alias_clean] = (canonical_clean, f'alias:{alias_clean}')
    return lookup


# ---------------------------------------------------------------------------
# PHASE 1: Match unique entity names (Layer A)
# ---------------------------------------------------------------------------
def build_token_index(ref_clean_list: list[str]) -> dict[str, set[str]]:
    """Build inverted index: token → set of ref names containing that token."""
    index = defaultdict(set)
    for ref in ref_clean_list:
        for token in significant_tokens(ref):
            index[token].add(ref)
    return index


def match_unique_names(
    unique_names: list[str],
    ref_clean_set: set[str],
    ref_clean_list: list[str],
    aliases: dict,
    exact_lookup: dict,
    config: MatchConfig,
) -> dict:
    """
    Match a list of unique entity_name_clean values against the reference list.

    Returns dict: entity_name_clean → (ref_clean, score, match_type, notes)
    """
    results = {}
    n_exact = 0
    n_fuzzy = 0
    n_skip = 0
    n_prefiltered = 0

    token_index = build_token_index(ref_clean_list)
    all_ref_tokens = set(token_index.keys())

    total = len(unique_names)
    log_interval = max(total // 20, 1)

    for i, name in enumerate(unique_names):
        if i > 0 and i % log_interval == 0:
            log.info(f"    Progress: {i:,}/{total:,} ({100*i/total:.0f}%) — "
                     f"exact={n_exact:,}, fuzzy={n_fuzzy:,}")

        if not name:
            continue

        # Exact lookup (includes aliases)
        if name in exact_lookup:
            ref_clean, notes = exact_lookup[name]
            results[name] = (ref_clean, 100.0, 'exact', notes)
            n_exact += 1
            continue

        # Generic name skip
        if name in GENERIC_NAMES:
            n_skip += 1
            continue

        # Short name: exact only
        if len(name) <= config.short_name_max_len or name in config.exact_only_names:
            n_skip += 1
            continue

        # Pre-filter: does this name share any significant token with refs?
        name_tokens = significant_tokens(name)
        shared_tokens = name_tokens & all_ref_tokens
        if len(shared_tokens) < 1:
            n_prefiltered += 1
            continue

        # Get candidate refs (those sharing at least one token)
        candidates = set()
        for token in shared_tokens:
            candidates.update(token_index[token])
        candidate_list = list(candidates)

        # Fuzzy matching against candidates only
        result = process.extractOne(
            name, candidate_list,
            scorer=fuzz.token_set_ratio,
            score_cutoff=config.fuzzy_medium_threshold,
        )
        if result is None:
            continue

        matched_ref, score, _idx = result

        # Validate: token overlap
        ref_tokens = significant_tokens(matched_ref)
        overlap = name_tokens & ref_tokens
        if len(overlap) < config.token_overlap_min:
            continue

        # Validate: length ratio
        ratio = max(len(name), len(matched_ref)) / max(min(len(name), len(matched_ref)), 1)
        if ratio > config.length_ratio_max:
            continue

        # Validate: not a known false positive
        is_fp = False
        for fp_ref, fp_ben in config.false_positive_pairs:
            if (matched_ref == fp_ref and name == fp_ben) or (name == fp_ref and matched_ref == fp_ben):
                is_fp = True
                break
        if is_fp:
            continue

        match_type = 'fuzzy_high' if score >= config.fuzzy_high_threshold else 'fuzzy_medium'
        results[name] = (matched_ref, score, match_type, '')
        n_fuzzy += 1

    log.info(f"  Unique name matching: {n_exact:,} exact, {n_fuzzy:,} fuzzy, "
             f"{n_skip:,} skipped, {n_prefiltered:,} pre-filtered")
    return results


# ---------------------------------------------------------------------------
# PHASE 2: Layer B — precompiled regex contextual matching
# ---------------------------------------------------------------------------
def build_context_regex(
    ref_clean_list: list[str],
    aliases: dict,
    config: MatchConfig,
) -> tuple[re.Pattern, dict]:
    """Build a single compiled regex matching any reference name (>= 6 chars) as whole-word."""
    MIN_CTX_LEN = 6

    terms = {}
    for ref in ref_clean_list:
        if len(ref) >= MIN_CTX_LEN and ref not in config.contextual_blocklist:
            terms[ref] = ref
    for alias_key, canonical in aliases.items():
        alias_clean = clean_name(alias_key)
        canonical_clean = clean_name(canonical)
        if (len(alias_clean) >= MIN_CTX_LEN
                and alias_clean not in config.contextual_blocklist
                and canonical_clean in set(ref_clean_list)):
            if alias_clean not in terms:
                terms[alias_clean] = canonical_clean

    sorted_terms = sorted(terms.keys(), key=len, reverse=True)

    if not sorted_terms:
        # Return a regex that never matches
        pattern = re.compile(r'(?!)')
        return pattern, terms

    escaped = [re.escape(t) for t in sorted_terms]
    pattern = re.compile(r'\b(' + '|'.join(escaped) + r')\b')

    log.info(f"  Layer B regex: {len(sorted_terms)} searchable terms "
             f"(min {MIN_CTX_LEN} chars, {len(config.contextual_blocklist)} blocklisted)")
    return pattern, terms


# ---------------------------------------------------------------------------
# Main matching pipeline
# ---------------------------------------------------------------------------
def run_matching(
    master_csv: Path,
    company_list_csv: Path,
    aliases_json: Path | None = None,
    output_dir: Path | None = None,
    config: MatchConfig | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Run the full two-layer matching pipeline.

    Parameters
    ----------
    master_csv : Path
        Path to master_dataset.csv (the harmonized master dataset).
    company_list_csv : Path
        CSV with at least a 'company_name' column.
    aliases_json : Path, optional
        JSON file mapping alias names to canonical names.
    output_dir : Path, optional
        Directory for output files. Defaults to current directory.
    config : MatchConfig, optional
        Matching configuration. Defaults to MatchConfig().

    Returns
    -------
    enriched_df : pd.DataFrame
        Matched rows with match metadata columns.
    match_log_df : pd.DataFrame
        One row per match with key columns.
    """
    if config is None:
        config = MatchConfig()
    if output_dir is None:
        output_dir = Path('.')

    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = config.output_prefix

    # Setup file logging
    fh = logging.FileHandler(output_dir / 'matcher.log', mode='w', encoding='utf-8')
    fh.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
    log.addHandler(fh)
    if not any(isinstance(h, logging.StreamHandler) for h in log.handlers):
        log.addHandler(logging.StreamHandler())
    log.setLevel(logging.INFO)

    t0 = time.time()
    log.info("=" * 70)
    log.info("ENTITY MATCHER — Starting (generic, two-layer)")
    log.info("=" * 70)

    # --- Build reference structures ---
    ref_df, aliases = build_reference_list(company_list_csv, aliases_json, config)
    ref_clean_set = set(ref_df['ref_name_clean'].unique())
    ref_clean_list = sorted(ref_clean_set)
    exact_lookup = build_exact_lookup(ref_df, aliases)
    ref_clean_to_raw = dict(zip(ref_df['ref_name_clean'], ref_df['ref_name_raw']))

    log.info(f"Reference: {len(ref_clean_set)} unique cleaned names, "
             f"{len(exact_lookup)} exact lookup entries (incl. aliases)")

    # Precompile Layer B regex
    ctx_pattern, ctx_term_map = build_context_regex(ref_clean_list, aliases, config)

    # --- PHASE 1: Collect unique entity names across all chunks ---
    master_path = _resolve_master(master_csv)
    log.info(f"\nPhase 1: Scanning unique entity names from {master_path}")
    if not master_path.exists():
        log.error(f"Master dataset not found: {master_path}")
        return pd.DataFrame(), pd.DataFrame()

    unique_names = set()
    chunk_count = 0
    total_rows = 0
    total_primary = 0

    for chunk in _read_chunks(
        master_path,
        columns=['entity_name_clean', 'is_primary_record'],
        chunksize=500_000,
    ):
        chunk_count += 1
        total_rows += len(chunk)
        mask = chunk['is_primary_record'].astype(str).str.lower() == 'true'
        total_primary += mask.sum()
        names = chunk.loc[mask, 'entity_name_clean'].dropna().unique()
        unique_names.update(names)

    log.info(f"  Total rows: {total_rows:,}, primary: {total_primary:,}")
    log.info(f"  Unique entity names to match: {len(unique_names):,}")

    # Match all unique names at once
    name_results = match_unique_names(
        list(unique_names), ref_clean_set, ref_clean_list, aliases, exact_lookup, config
    )
    log.info(f"  Layer A results: {len(name_results):,} names matched")

    # --- PHASE 2: Apply results to full dataset + Layer B contextual ---
    log.info(f"\nPhase 2: Applying matches and running Layer B contextual...")
    all_match_records = []
    all_enriched_chunks = []
    total_matched = 0

    # Column names
    col_flag = f'{prefix}_flag'
    col_score = f'{prefix}_score'
    col_type = f'{prefix}_type'
    col_matched_on = f'{prefix}_matched_on'
    col_ref_name = f'{prefix}_reference_name'
    col_notes = f'{prefix}_notes'

    # Pre-build Layer A lookup as a DataFrame for vectorised merge
    if name_results:
        la_df = pd.DataFrame([
            {'_ec': k, '_ref_clean': v[0], '_score': v[1], '_mtype': v[2], '_notes': v[3]}
            for k, v in name_results.items()
        ])
    else:
        la_df = pd.DataFrame(columns=['_ec', '_ref_clean', '_score', '_mtype', '_notes'])

    for chunk in _read_chunks(
        master_path,
        columns=LOAD_COLS,
        chunksize=config.chunk_size,
        dtype={'year': 'str', 'nace_2digit': 'str'},
    ):
        primary_mask = chunk['is_primary_record'].astype(str).str.lower() == 'true'
        chunk = chunk[primary_mask].copy()
        if len(chunk) == 0:
            continue

        # Initialize output columns
        chunk[col_flag] = False
        chunk[col_score] = 0.0
        chunk[col_type] = 'none'
        chunk[col_matched_on] = ''
        chunk[col_ref_name] = ''
        chunk[col_notes] = ''

        # --- Apply Layer A results (vectorised via merge) ---
        ec_series = chunk['entity_name_clean'].fillna('')
        chunk['_ec'] = ec_series
        merged = chunk[['_ec']].merge(la_df, on='_ec', how='left')

        layer_a_mask = merged['_ref_clean'].notna()
        la_idx = chunk.index[layer_a_mask.values]
        if len(la_idx) > 0:
            chunk.loc[la_idx, col_flag] = True
            chunk.loc[la_idx, col_score] = merged.loc[layer_a_mask.values, '_score'].values
            chunk.loc[la_idx, col_type] = merged.loc[layer_a_mask.values, '_mtype'].values
            chunk.loc[la_idx, col_matched_on] = 'entity_name_clean'
            ref_raws = merged.loc[layer_a_mask.values, '_ref_clean'].map(
                lambda x: ref_clean_to_raw.get(x, x)
            ).values
            chunk.loc[la_idx, col_ref_name] = ref_raws
            chunk.loc[la_idx, col_notes] = merged.loc[layer_a_mask.values, '_notes'].values

        # --- False positive veto ---
        if len(la_idx) > 0 and config.beneficiary_fp_patterns:
            fp_veto_count = 0
            for veto_ref, veto_re in config.beneficiary_fp_patterns.items():
                ref_mask = chunk.loc[la_idx, col_ref_name].apply(
                    lambda x: clean_name(x) == veto_ref or clean_name(x).startswith(veto_ref + ' ')
                )
                if not ref_mask.any():
                    continue
                candidate_idx = la_idx[ref_mask.values]
                ben_series = chunk.loc[candidate_idx, 'beneficiary_name'].fillna('')
                fp_hit = ben_series.apply(lambda x: bool(veto_re.search(str(x))))
                veto_idx = candidate_idx[fp_hit.values]
                if len(veto_idx) > 0:
                    chunk.loc[veto_idx, col_flag] = False
                    chunk.loc[veto_idx, col_type] = 'vetoed_false_positive'
                    chunk.loc[veto_idx, col_notes] = f'fp_veto:{veto_ref}'
                    fp_veto_count += len(veto_idx)
            if fp_veto_count > 0:
                log.info(f"    FP veto: {fp_veto_count} false positives blocked")

        chunk.drop(columns=['_ec'], inplace=True)

        # --- Layer B: Contextual matching on description ---
        unmatched_mask = (~chunk[col_flag]) & chunk['source'].isin(config.layer_b_sources)
        unmatched = chunk.loc[unmatched_mask]
        ctx_count = 0

        if len(unmatched) > 0:
            desc_series = unmatched['description'].fillna('')
            desc_clean = desc_series.apply(clean_name)
            matches = desc_clean.str.extract(ctx_pattern, expand=False)
            matched_mask = matches.notna()
            ctx_idx = unmatched.index[matched_mask.values]

            if len(ctx_idx) > 0:
                matched_terms = matches[matched_mask].values
                ref_cleans = [ctx_term_map[t] for t in matched_terms]
                ref_raws = [ref_clean_to_raw.get(rc, rc) for rc in ref_cleans]
                is_alias = [t != rc for t, rc in zip(matched_terms, ref_cleans)]
                scores = [90.0 if a else 95.0 for a in is_alias]
                notes = [f'{"alias_" if a else ""}match_in_description' for a in is_alias]

                chunk.loc[ctx_idx, col_flag] = True
                chunk.loc[ctx_idx, col_score] = scores
                chunk.loc[ctx_idx, col_type] = 'contextual_exact'
                chunk.loc[ctx_idx, col_matched_on] = 'description'
                chunk.loc[ctx_idx, col_ref_name] = ref_raws
                chunk.loc[ctx_idx, col_notes] = notes
                ctx_count = len(ctx_idx)

            # Layer B+: For EIB/EBRD, scan beneficiary_name (= project title)
            eib_unmatched_mask = (~chunk[col_flag]) & chunk['source'].isin({'EIB', 'EBRD'})
            eib_unmatched = chunk.loc[eib_unmatched_mask]
            eib_ctx_count = 0
            if len(eib_unmatched) > 0:
                title_clean = eib_unmatched['entity_name_raw'].fillna('').apply(clean_name)
                title_matches = title_clean.str.extract(ctx_pattern, expand=False)
                title_matched_mask = title_matches.notna()
                title_idx = eib_unmatched.index[title_matched_mask.values]
                if len(title_idx) > 0:
                    tterms = title_matches[title_matched_mask].values
                    tref_cleans = [ctx_term_map[t] for t in tterms]
                    tref_raws = [ref_clean_to_raw.get(rc, rc) for rc in tref_cleans]
                    tis_alias = [t != rc for t, rc in zip(tterms, tref_cleans)]
                    tscores = [88.0 if a else 92.0 for a in tis_alias]
                    tnotes = [f'{"alias_" if a else ""}eib_project_title_extraction' for a in tis_alias]
                    chunk.loc[title_idx, col_flag] = True
                    chunk.loc[title_idx, col_score] = tscores
                    chunk.loc[title_idx, col_type] = 'eib_title_extraction'
                    chunk.loc[title_idx, col_matched_on] = 'entity_name_raw'
                    chunk.loc[title_idx, col_ref_name] = tref_raws
                    chunk.loc[title_idx, col_notes] = tnotes
                    eib_ctx_count = len(title_idx)
                    ctx_count += eib_ctx_count

            # Layer B fallback: scan original_columns for sources with extra fields
            still_unmatched_mask = (~chunk[col_flag]) & chunk['source'].isin(
                {s for s, fields in config.source_context_fields.items() if fields}
            )
            still_unmatched_idx = chunk.index[still_unmatched_mask]
            oc_count = 0

            for idx in still_unmatched_idx:
                source = str(chunk.at[idx, 'source'])
                extra_fields = config.source_context_fields.get(source, [])
                if not extra_fields:
                    continue
                oc = chunk.at[idx, 'original_columns']
                if pd.isna(oc) or not str(oc).strip():
                    continue
                try:
                    oc_dict = json.loads(str(oc)) if isinstance(oc, str) else oc
                    if not isinstance(oc_dict, dict):
                        continue
                except (json.JSONDecodeError, TypeError):
                    continue
                for field_name in extra_fields:
                    val = oc_dict.get(field_name, '')
                    if not val or pd.isna(val) or not str(val).strip():
                        continue
                    text_clean = clean_name(str(val))
                    if not text_clean or len(text_clean) < 4:
                        continue
                    m = ctx_pattern.search(text_clean)
                    if m:
                        matched_term = m.group(1)
                        ref_clean = ctx_term_map[matched_term]
                        ia = matched_term != ref_clean
                        oc_field = f'original_columns.{field_name}'
                        chunk.at[idx, col_flag] = True
                        chunk.at[idx, col_score] = 90.0 if ia else 95.0
                        chunk.at[idx, col_type] = 'contextual_exact'
                        chunk.at[idx, col_matched_on] = oc_field
                        chunk.at[idx, col_ref_name] = ref_clean_to_raw.get(ref_clean, ref_clean)
                        chunk.at[idx, col_notes] = f'{"alias_" if ia else ""}match_in_{oc_field}'
                        oc_count += 1
                        break
            ctx_count += oc_count

        matched = chunk[col_flag].sum()
        total_matched += matched
        layer_a_count = matched - ctx_count
        log.info(f"  Chunk: {len(chunk):,} primary rows → "
                 f"{matched:,} matched (Layer A: {layer_a_count:,}, Layer B: {ctx_count:,})")

        # Collect match log
        matched_rows = chunk[chunk[col_flag]].copy()
        if len(matched_rows) > 0:
            log_cols = [
                'source', 'source_record_id', 'year', 'country', 'amount_eur',
                'financial_instrument_class', 'flow_stage_group',
                'fiscal_source_type',
                'entity_name_raw', 'entity_name_clean',
                col_ref_name, col_score, col_type, col_matched_on, col_notes,
            ]
            all_match_records.append(matched_rows[log_cols])
            all_enriched_chunks.append(matched_rows)

    # --- Assemble outputs ---
    elapsed = time.time() - t0
    log.info(f"\nMatching complete in {elapsed:.0f}s")
    log.info(f"Total primary rows: {total_primary:,}")
    log.info(f"Matches: {total_matched:,}")

    if all_match_records:
        match_log_df = pd.concat(all_match_records, ignore_index=True)
        match_log_path = output_dir / 'match_log.csv'
        match_log_df.to_csv(match_log_path, index=False, encoding='utf-8')
        log.info(f"Match log: {len(match_log_df):,} rows → {match_log_path}")
    else:
        match_log_df = pd.DataFrame()
        log.warning("No matches found!")

    if all_enriched_chunks:
        enriched_df = pd.concat(all_enriched_chunks, ignore_index=True)
        enriched_path = output_dir / 'enriched.csv'
        enriched_df.to_csv(enriched_path, index=False, encoding='utf-8')
        log.info(f"Enriched: {len(enriched_df):,} rows → {enriched_path}")
    else:
        enriched_df = pd.DataFrame()

    # --- Summary ---
    lines = []
    lines.append("=" * 70)
    lines.append("ENTITY MATCHING SUMMARY")
    lines.append("=" * 70)
    lines.append(f"Total primary rows:     {total_primary:,}")
    lines.append(f"Matches:                {total_matched:,}")
    lines.append(f"Runtime:                {elapsed:.0f}s")

    if len(match_log_df) > 0:
        total_eur = match_log_df['amount_eur'].sum()
        lines.append(f"Total matched EUR:      {total_eur:,.0f}")

        lines.append(f"\nBy match type:")
        for mt, grp in match_log_df.groupby(col_type):
            lines.append(f"  {mt:22s}: {len(grp):>8,} rows, EUR {grp['amount_eur'].sum():>15,.0f}")

        lines.append(f"\nBy source:")
        for src, grp in match_log_df.groupby('source'):
            lines.append(f"  {src:12s}: {len(grp):>8,} rows, EUR {grp['amount_eur'].sum():>15,.0f}")

        lines.append(f"\nTop 20 matched companies by EUR:")
        top = (match_log_df.groupby(col_ref_name)['amount_eur']
               .agg(['sum', 'count']).sort_values('sum', ascending=False).head(20))
        for name, row in top.iterrows():
            lines.append(f"  {name:40s}: EUR {row['sum']:>15,.0f} ({int(row['count']):,} rows)")

        n_unique = match_log_df[col_ref_name].nunique()
        lines.append(f"\nUnique matched entities: {n_unique}")

    summary = '\n'.join(lines)
    log.info('\n' + summary)
    with open(output_dir / 'match_summary.txt', 'w', encoding='utf-8') as f:
        f.write(summary)

    # Clean up file handler
    log.removeHandler(fh)
    fh.close()

    return enriched_df, match_log_df
