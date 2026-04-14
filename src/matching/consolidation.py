#!/usr/bin/env python3
"""
Generic Post-Matching Consolidation Pipeline
==============================================
Integrates entity matching results with enrichment outputs into
publication-grade consolidated datasets and summary tables.

Works for ANY company list — automotive, pharma, semiconductors, etc.
Sector-specific analysis (nationality, sector tags) is optional and
configured via JSON files.

Pipeline:
  1. Load core match_log.csv
  2. Integrate enrichment outputs (FTS-CORDIS, EIB promoter, IPCEI, ETS)
  3. Deduplicate across sources
  4. Compute GGE (Gross Grant Equivalent)
  5. Assess match quality
  6. Optionally assign parent groups (if parent_groups JSON provided)
  7. Build summary tables + concentration metrics
  8. Save all outputs

Usage:
  from src.matching.consolidation import consolidate
  consolidate(match_log, output_dir, parent_groups='parent_groups.json')
"""

import pandas as pd
import numpy as np
import json
import re
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ============================================================================
# Deduplication configuration (L2)
# ============================================================================
# Consolidated in one place so future per-source-pair tuning does not require
# patching three separate function signatures. Defaults match the previously
# hardcoded values so existing runs are byte-identical.

@dataclass
class DedupConfig:
    """Configurable thresholds for cross-source deduplication (L2).

    Every threshold was previously hardcoded inside `_flag_*` functions in
    this module; L2 promoted them here so notebook callers can override any
    single threshold without monkey-patching. See `research/PIPELINE_AUDIT.md`
    H3 for why these should eventually be empirically validated against the
    PDF-confirmed gold set.
    """

    # Year window (in years, inclusive) for all (company, country, year)
    # joins used by dedup functions. Applied in both the document-backed
    # path (_flag_pdf_cofin_overlaps) and the heuristic fallback
    # (_flag_cofinancing_overlaps) as well as _flag_ipcei_tam_overlap.
    pdf_cofin_year_window: int = 2
    heuristic_cofin_year_window: int = 2
    ipcei_tam_year_window: int = 2

    # Plausibility ratio band for the heuristic TAM↔KOHESIO fallback.
    # KOHESIO/TAM ratio must be in [min, max] to flag as an overlap.
    # Lower bound excludes coincidental matches; upper bound covers
    # multi-year KOHESIO disbursements against a single annual TAM notification.
    heuristic_cofin_ratio_min: float = 0.01
    heuristic_cofin_ratio_max: float = 1.50

    # IPCEI↔TAM amount tolerance — the two databases can differ because IPCEI
    # reference amounts are at EC approval time while TAM notifications reflect
    # the final notified amount.
    ipcei_tam_amount_tolerance: float = 0.20

    # FTS↔INNOVFUND identical-transaction tolerance. The two databases record
    # the same grant with amounts that should agree to the cent.
    fts_innovfund_amount_tolerance: float = 0.001


DEFAULT_DEDUP_CONFIG = DedupConfig()


# ============================================================================
# GGE (Gross Grant Equivalent) — EU Scoreboard rates
# ============================================================================

GGE_RATES = {
    'grant': 1.00, 'subsidy': 1.00, 'procurement': 1.00,
    'equity': 1.00, 'debt_relief': 1.00, 'other': 1.00,
    'mixed': 0.50, 'loan': 0.15, 'guarantee': 0.10, 'tax_advantage': 0.15,
}
REPAYABLE_ADVANCE_RATE = 0.90

# Counter for unknown/missing instrument classes encountered during a run.
# Reset by consolidate() before Phase 4 and reported at the end of it.
_GGE_UNKNOWN_COUNTS: dict[str, int] = {}


# ============================================================================
# NATIONALITY / ORIGIN BLOCK HELPERS
# ============================================================================

EU_COUNTRIES = frozenset({
    'AT', 'BE', 'BG', 'CY', 'CZ', 'DE', 'DK', 'EE', 'ES', 'FI',
    'FR', 'GR', 'HR', 'HU', 'IE', 'IT', 'LT', 'LU', 'LV', 'MT',
    'NL', 'PL', 'PT', 'RO', 'SE', 'SI', 'SK',
})

COUNTRY_BLOCK_MAP = {
    'CN': 'CN', 'HK': 'CN', 'MO': 'CN',
    'US': 'US',
    'JP': 'JP',
    'KR': 'KR',
    'TW': 'Other', 'IN': 'Other', 'SG': 'Other', 'IL': 'Other',
    'GB': 'Other', 'CH': 'Other', 'NO': 'Other', 'IS': 'Other',
    'CA': 'Other', 'AU': 'Other', 'NZ': 'Other',
    'BR': 'Other', 'MX': 'Other', 'AR': 'Other',
    'ZA': 'Other', 'TR': 'Other', 'SA': 'Other', 'AE': 'Other',
    'RU': 'Other', 'UA': 'Other',
}


def _country_to_block(iso2: str) -> str:
    """Map an ISO-2 country code to an origin block (EU / CN / US / JP / KR / Other / Unknown)."""
    if not iso2 or (isinstance(iso2, float)):
        return 'Unknown'
    iso2 = str(iso2).strip().upper()
    if not iso2 or iso2 in ('NAN', 'NONE', ''):
        return 'Unknown'
    if iso2 in EU_COUNTRIES:
        return 'EU'
    return COUNTRY_BLOCK_MAP.get(iso2, 'Other')


def _gge_rate_and_source(row):
    """Return (rate, source) for a single row.

    Sources:
        'measured'  — instrument is in GGE_RATES; rate comes from the table.
        'measured_repayable' — loan marked repayable-advance, uses REPAYABLE_ADVANCE_RATE.
        'unknown'   — instrument is empty or not in the table. rate is NaN
                      so ``amount_eur * rate`` propagates NaN and the row is
                      excluded from GGE headline totals (§6.5 no-invention
                      principle: we do not fabricate a GGE for instruments
                      we have not classified).

    The unknown counter is still populated so Phase 4 can log a summary.
    """
    import math
    inst = str(row.get('financial_instrument_class', '')).lower().strip()
    subtype = str(row.get('instrument_subtype', '')).lower()
    if inst == 'loan' and 'repayable' in subtype:
        return REPAYABLE_ADVANCE_RATE, 'measured_repayable'
    if inst in GGE_RATES:
        return GGE_RATES[inst], 'measured'
    key = inst if inst else '<empty>'
    _GGE_UNKNOWN_COUNTS[key] = _GGE_UNKNOWN_COUNTS.get(key, 0) + 1
    return math.nan, 'unknown'


def _gge_rate(row):
    """Backwards-compatible shim returning just the rate."""
    rate, _ = _gge_rate_and_source(row)
    return rate


# ============================================================================
# NAME CLEANING + GROUP ROLLUP
# ============================================================================

def _clean_for_group(name: str) -> str:
    """Clean company name for group matching.
    Only strips trailing legal suffixes (not leading ones like 'AB' in 'AB Volvo').
    """
    s = str(name).strip().lower()
    s = re.sub(r'\([^)]*\)', '', s)
    s = re.sub(
        r'\b(?:s\.?p\.?a\.?|s\.?r\.?l\.?|gmbh|ag|ltd|inc|plc|llc|corp|'
        r'n\.?v\.?|b\.?v\.?|a\.?s\.?|se|sa|oy|co|kg|e\.?v\.?)\b\.?\s*$', '', s
    )
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    return re.sub(r'\s+', ' ', s).strip()


def assign_parent_group(ref_name: str, parent_groups: dict) -> str:
    """Map a reference name to its parent group using longest-match-first.

    Parameters
    ----------
    ref_name : str
        The entity/company reference name to look up.
    parent_groups : dict
        Mapping of {group_name: [member_name, ...]} where member names
        are lowercase strings.

    Returns the group name, or the original ref_name if no match.
    """
    cleaned = _clean_for_group(ref_name)
    best_group = None
    best_len = 0
    for group, members in parent_groups.items():
        for member in members:
            if member in cleaned or cleaned in member:
                if len(member) > best_len:
                    best_len = len(member)
                    best_group = group
    return best_group if best_group else ref_name


# ============================================================================
# MATCH QUALITY ASSESSMENT
# ============================================================================

_GENERIC_IFI_TITLE_TOKENS = frozenset({
    'project', 'programme', 'program', 'framework', 'loan', 'facility', 'fund',
    'investment', 'infrastructure', 'european', 'eib', 'ebrd', 'finance',
    'financing', 'guarantee', 'support', 'co-financing', 'cofinancing',
    'energy', 'transport', 'water', 'waste', 'health', 'climate', 'green',
    'renewable', 'research', 'innovation', 'development', 'sme', 'smes',
    'midcap', 'midcaps', 'ltd', 'group', 'bank', 'scheme',
})


def assess_match_quality(df, prefix='match'):
    """Flag likely false positives from description-only and IFI-title matching.

    Two independent suspect checks run here:

    1. **Description-only matches in EU-fund rows.** For KOHESIO / FTS / CINEA
       rows that matched on the `description` field rather than on
       `entity_name`, the reference company's significant tokens must appear
       in `beneficiary_name`. If not, the row is re-tagged
       `suspect_description_only` (e.g. "Samsung tablet" matching Samsung SDI,
       "Michelin star" matching Michelin).

    2. **IFI title extractions (M5).** EIB / EBRD rows are intentionally
       excluded from check 1 because their `beneficiary_name` field *is* the
       project title, not a company name — the naive test would spuriously
       fail on every good match. Instead, rows with `match_type =
       eib_title_extraction` are re-tagged `suspect_eib_title` when the
       reference name is too generic to carry any signal from a project
       title: either the cleaned reference name is a single short token, or
       all of its significant tokens are common IFI-project boilerplate
       (project, programme, energy, infrastructure, …). These rows still
       appear in the consolidated output with `dc_preferred=True`; the flag
       is advisory only, so that downstream audits can filter them out.
    """
    log.info("Assessing match quality...")
    ref_col = f'{prefix}_reference_name'
    matched_on_col = f'{prefix}_matched_on'

    if 'match_quality' not in df.columns:
        df['match_quality'] = 'confirmed'

    # ---- check 1: description-only matches in EU-fund rows ----
    desc_mask = (
        df.get(matched_on_col, pd.Series(dtype=str)).eq('description') &
        df['source'].isin(['KOHESIO', 'FTS', 'CINEA'])
    )

    def _ref_in_ben(row):
        ref = str(row.get(ref_col, '')).lower().strip()
        ben = str(row.get('beneficiary_name', '')).lower().strip()
        ref_clean = re.sub(
            r'\b(s\.?p\.?a\.?|s\.?r\.?l\.?|gmbh|ag|ltd|inc|plc|sa|se|kft|zrt|srl|n\.?v\.?|b\.?v\.?)\b\.?',
            '', ref
        ).strip()
        ref_words = [w for w in ref_clean.split() if len(w) > 2]
        if not ref_words:
            return True
        return any(w in ben for w in ref_words)

    if desc_mask.sum() > 0:
        check_rows = df[desc_mask]
        ref_found = check_rows.apply(_ref_in_ben, axis=1)
        suspect_desc_mask = desc_mask & ~df.index.isin(check_rows[ref_found].index)
        df.loc[suspect_desc_mask, 'match_quality'] = 'suspect_description_only'
        n_desc = suspect_desc_mask.sum()
        eur_desc = df.loc[suspect_desc_mask, 'amount_eur'].sum()
        log.info(f"  Suspect (description-only EU-fund rows): {n_desc:,} rows, EUR {eur_desc/1e6:.0f}M")
    else:
        log.info("  No description-matched EU-fund rows to check")

    # ---- check 2: IFI title extractions (M5) ----
    if 'match_type' in df.columns:
        eib_title_mask = (
            df['source'].isin(['EIB', 'EBRD'])
            & df['match_type'].fillna('').str.contains('eib_title', case=False, na=False)
        )
    else:
        eib_title_mask = pd.Series(False, index=df.index)

    if eib_title_mask.sum() > 0:
        def _ifi_ref_suspect(row):
            ref = str(row.get(ref_col, '')).lower().strip()
            ref = re.sub(
                r'\b(s\.?p\.?a\.?|s\.?r\.?l\.?|gmbh|ag|ltd|inc|plc|sa|se|kft|zrt|srl|'
                r'n\.?v\.?|b\.?v\.?|plc|llc)\b\.?',
                '', ref,
            ).strip()
            tokens = [t for t in re.split(r'\W+', ref) if len(t) > 2]
            # Single-token short reference → unsafe to infer a title match
            # (too many title words could accidentally satisfy it).
            if len(tokens) <= 1:
                return True
            # Every significant token is generic IFI-project vocabulary.
            non_generic = [t for t in tokens if t not in _GENERIC_IFI_TITLE_TOKENS]
            if not non_generic:
                return True
            return False

        eib_check = df[eib_title_mask]
        suspect_eib_idx = eib_check[eib_check.apply(_ifi_ref_suspect, axis=1)].index
        df.loc[suspect_eib_idx, 'match_quality'] = 'suspect_eib_title'
        n_eib = len(suspect_eib_idx)
        eur_eib = df.loc[suspect_eib_idx, 'amount_eur'].sum()
        log.info(
            f"  Suspect (IFI title extraction with generic reference name): "
            f"{n_eib:,} rows, EUR {eur_eib/1e6:.0f}M"
        )

    n_suspect_total = (df['match_quality'] != 'confirmed').sum()
    log.info(f"  Confirmed rows: {len(df) - n_suspect_total:,}")

    return df


# ============================================================================
# ENRICHMENT INTEGRATION
# ============================================================================

def _build_company_list_regex(company_list_csv, aliases_json=None):
    """Build a compiled regex from a company list CSV + optional aliases.

    This produces the same regex the fts_cordis_bridge uses, giving consistent
    company identification across the pipeline.

    Uses a minimum length of 4 chars and blocks common English words that
    appear as company names but create massive false positives when searching
    CORDIS consortium text (e.g., "Group", "Smart", "Other", "Mobility").
    """
    import json as _json

    # Common words that appear in company lists but are too generic for
    # substring matching against CORDIS consortium names
    _BLOCKLIST = {
        'group', 'other', 'smart', 'mobility', 'alpine', 'global', 'systems',
        'international', 'technology', 'technologies', 'digital', 'innovation',
        'energy', 'power', 'green', 'new', 'first', 'general', 'national',
        'advanced', 'modern', 'future', 'life', 'next', 'open', 'best',
        'metro', 'urban', 'city', 'plus', 'one', 'star', 'blue', 'ideal',
        'china', 'india', 'europe', 'asia', 'super', 'auto', 'motor',
        'motors', 'cars', 'electric', 'vehicle', 'vehicles',
    }

    df = pd.read_csv(company_list_csv)
    name_col = next((c for c in df.columns if 'name' in c.lower()), df.columns[0])
    names = df[name_col].dropna().str.strip().tolist()

    if aliases_json and Path(aliases_json).exists():
        with open(aliases_json) as f:
            aliases = _json.load(f)
        # Support both formats:
        #   {canonical: [alias1, alias2, ...]}  — list values
        #   {alias: canonical}                  — string values (variant→canonical)
        for k, v in aliases.items():
            if k.startswith('_'):
                continue  # skip _comment keys
            if isinstance(v, list):
                names.extend(v)
            elif isinstance(v, str):
                names.append(k)    # the alias key itself
                names.append(v)    # the canonical value

    # Min length 4 to avoid matching "ab", "gac", etc.
    # Blocklist common words that cause FP in consortium text
    patterns = sorted(
        set(n.lower() for n in names if len(n) >= 4 and n.lower() not in _BLOCKLIST),
        key=len, reverse=True,
    )
    patterns = [re.escape(p) for p in patterns]
    if not patterns:
        return re.compile(r'(?!)')
    return re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.I)


def _build_ref_regex(core_df, ref_col):
    """Build a compiled regex from the reference names in core match results.

    This allows enrichment rows (FTS-CORDIS, EIB) to be attributed to the
    same companies identified by the entity matcher — fully generic, works
    for any sector.
    """
    if ref_col not in core_df.columns:
        return re.compile(r'(?!)')  # match nothing
    names = core_df[ref_col].dropna().str.strip().unique().tolist()
    patterns = sorted(set(n.lower() for n in names if len(n) > 2), key=len, reverse=True)
    patterns = [re.escape(p) for p in patterns]
    if not patterns:
        return re.compile(r'(?!)')
    return re.compile(r'\b(' + '|'.join(patterns) + r')\b', re.I)


def _extract_company_from_cordis_row(row, company_re):
    """Extract the relevant company name from an FTS-CORDIS enrichment row.

    Uses the signal column to decide where to look:
    - beneficiary_name: the FTS beneficiary IS the company → extract from beneficiary
    - cordis_company: the company is a CORDIS project partner → extract from company_names
    - topic_keyword: no specific company → mark as unattributed

    Falls back to beneficiary_name if no regex match found.
    """
    signal = str(row.get('signal', ''))
    ben = str(row.get('beneficiary_name', ''))
    companies = str(row.get('company_names', ''))

    # Priority 1: beneficiary IS the company
    if signal == 'beneficiary_name':
        m = company_re.search(ben)
        if m:
            return m.group(1).strip()
        # No regex hit — use cleaned beneficiary name
        return ben.split('*')[0].strip()

    # Priority 2: CORDIS participant is the company
    if signal in ('cordis_company', 'company+topic'):
        m = company_re.search(companies)
        if m:
            return m.group(1).strip()
        # Fallback: try beneficiary
        m2 = company_re.search(ben)
        if m2:
            return m2.group(1).strip()
        return 'unattributed_consortium_rd'

    # Priority 3: topic keyword only — no specific company
    return 'unattributed_rd'


# Minimum schema every enrichment CSV should honour (M2).
# `required` columns must be present. If they're absent, the CSV is loaded but
# rejected by `integrate_enrichment` with a visible warning — silently merging
# a malformed CSV into the core dataframe is a common way for schema drift to
# corrupt headline totals without anyone noticing. The `amount_col` is the
# column that should be non-NaN on rows we want to count toward totals; if all
# rows have NaN in it, we warn (the CSV may still be merged as name-only
# coverage, but with explicit awareness).
ENRICHMENT_SCHEMA: dict[str, dict] = {
    # Only the four enrichment outputs that integrate_enrichment actually
    # reads are listed here. FTS deep mining and high-value forensics write
    # their own diagnostic files; they don't flow through this pipeline.
    'fts_cordis': {
        'required': ['source_record_id'],
        'amount_col': 'amount_eur',
    },
    'eib_promoter': {
        'required': ['source_record_id'],
        'amount_col': 'amount_eur',
    },
    'ets_free_allocation': {
        'required': ['matched_company'],
        # ETS has no standard amount column — it's annual free allocations,
        # merged via its own specialised block below.
        'amount_col': None,
    },
    'ipcei': {
        'required': ['company_name', 'ipcei', 'sa_case'],
        'amount_col': 'amount_eur',
    },
    'sa_adhoc': {
        # Ad hoc state-aid decision pre-load (H8). Required columns are
        # minimal because the parser emits name-only rows when amount
        # extraction fails — the row is still useful as an audit hint.
        'required': ['sa_case', 'match_reference_name', 'extracted_beneficiary'],
        'amount_col': 'amount_eur',
    },
}


def _validate_enrichment_schema(df: pd.DataFrame, label: str, schema_key: str) -> bool:
    """Validate an enrichment DataFrame against ENRICHMENT_SCHEMA.

    Returns True if the frame is safe to merge into the core dataframe. On
    failure, logs a warning identifying the missing columns and returns False
    so the caller can skip the merge. Never raises — schema drift should be
    visible in the run log, not a pipeline-halting exception.
    """
    schema = ENRICHMENT_SCHEMA.get(schema_key)
    if schema is None or df.empty:
        return True
    required = schema.get('required', [])
    missing = [c for c in required if c not in df.columns]
    if missing:
        log.warning(
            f"    {label}: enrichment schema check FAILED — missing required columns {missing}. "
            f"This CSV will be skipped rather than merged into the core dataframe."
        )
        return False
    amount_col = schema.get('amount_col')
    if amount_col and amount_col in df.columns:
        n_with_amt = df[amount_col].notna().sum()
        n_rows = len(df)
        if n_with_amt == 0:
            log.warning(
                f"    {label}: schema check — {n_rows:,} rows but 0 with a non-NaN "
                f"`{amount_col}`. These rows cannot contribute to headline totals."
            )
        elif n_with_amt < n_rows * 0.5:
            log.info(
                f"    {label}: schema check — {n_with_amt:,}/{n_rows:,} rows have "
                f"`{amount_col}` (the rest are name-only coverage)."
            )
    # Duplicate source_record_id check — catches the most common schema drift:
    # an enrichment script emitting the same row twice, usually because two
    # joins matched the same upstream record.
    if 'source_record_id' in df.columns:
        n_dup = df['source_record_id'].astype(str).duplicated().sum()
        if n_dup > 0:
            log.warning(
                f"    {label}: {n_dup:,} duplicate source_record_id values "
                f"(may inflate counts — check the upstream enrichment script)."
            )
    return True


def _load_enrichment_csv(path, label, schema_key: str | None = None):
    """Load an enrichment CSV and validate it against the schema contract (M2).

    If `schema_key` is provided, the CSV is validated against the corresponding
    entry in `ENRICHMENT_SCHEMA`. On schema failure, an empty DataFrame is
    returned so the caller skips the merge — the warning has already been
    logged.
    """
    if not path.exists():
        log.info(f"  {label}: not found at {path}")
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    eur_col = 'amount_eur' if 'amount_eur' in df.columns else 'total_eur_free' if 'total_eur_free' in df.columns else None
    eur_str = f"EUR {df[eur_col].sum():,.0f}" if eur_col else "(no EUR column)"
    log.info(f"  {label}: {len(df):,} rows, {eur_str}")
    if schema_key:
        if not _validate_enrichment_schema(df, label, schema_key):
            return pd.DataFrame()
    return df


def integrate_enrichment(core_df, enrichment_dir, prefix='match',
                         company_list_csv=None, aliases_json=None):
    """Load and integrate all enrichment outputs into the core match set.

    Enrichment scripts output CSVs in enrichment_dir with standardized columns.
    This function loads them, deduplicates against core matches by source_record_id,
    and concatenates.

    Parameters
    ----------
    core_df : pd.DataFrame
        Core match_log results from the entity matcher.
    enrichment_dir : Path
        Directory containing enrichment output CSVs.
    prefix : str
        Column prefix used by the matcher (e.g., 'automotive', 'match').
    company_list_csv : Path | None
        Optional path to the original company list CSV used by the matcher.
        If provided, builds a more accurate regex for entity extraction
        in enrichment rows.
    aliases_json : Path | None
        Optional path to aliases JSON for the company list.

    Returns
    -------
    combined : pd.DataFrame
        Consolidated dataset with all sources integrated.
    integration_stats : dict
        Statistics about each enrichment source.
    """
    if enrichment_dir is None or not Path(enrichment_dir).exists():
        log.info("  No enrichment directory found — skipping enrichment integration")
        return core_df, {}

    enrichment_dir = Path(enrichment_dir)
    stats = {}
    ref_col = f'{prefix}_reference_name'
    type_col = f'{prefix}_type'
    score_col = f'{prefix}_score'

    combined = core_df.copy()
    core_sids = set(core_df['source_record_id'].astype(str).unique())

    # Seed optional columns that enrichment rows may carry so they survive the
    # `combined.columns.intersection(...)` concat pattern used below. Adding
    # them here with sensible defaults lets enrichment writers (IPCEI PDF
    # parser, etc.) propagate per-row metadata without each one having to
    # monkey-patch the core schema.
    if 'ipcei_ticker' not in combined.columns:
        combined['ipcei_ticker'] = ''
    if 'amount_confidence' not in combined.columns:
        # 'measured' is the default for every row that came from a harmonized
        # source with an actual amount_eur value. Enrichment rows that write
        # bracket-range midpoints (IPCEI PDF parser) override this with
        # 'range_from_pdf' / 'exact_from_pdf' / 'redacted' so downstream code
        # can distinguish measured values from PDF-derived approximations.
        combined['amount_confidence'] = 'measured'
    if 'is_adhoc_preloaded' not in combined.columns:
        # Marker for rows injected by the ad hoc state-aid pre-loader (H8).
        # False on every harmonized row; True only on rows that came from
        # `sa_adhoc_matched.csv`. Downstream charts can filter on this column
        # to separate pre-2016 historical coverage from post-2016 TAM.
        combined['is_adhoc_preloaded'] = False
    # Bracket-range bounds for amounts that come as [low - high] in a
    # source (currently only IPCEI PDF extraction). For every other row
    # ``amount_eur_low == amount_eur_high == amount_eur``. This lets the
    # §6.5 no-invention posture be enforced downstream — headline totals
    # can be summed on ``amount_eur_low`` for a provable lower bound.
    if 'amount_eur_low' not in combined.columns:
        combined['amount_eur_low'] = combined['amount_eur']
    if 'amount_eur_high' not in combined.columns:
        combined['amount_eur_high'] = combined['amount_eur']

    # Build a company regex for entity extraction in enrichment rows.
    # Prefer the original company list CSV (short canonical names that match
    # CORDIS legal names better) over the matcher's reference_name column.
    if company_list_csv and Path(company_list_csv).exists():
        _company_re = _build_company_list_regex(company_list_csv, aliases_json)
        log.info(f"  Company regex built from {Path(company_list_csv).name}")
    else:
        _company_re = _build_ref_regex(core_df, ref_col)
        log.info(f"  Company regex built from core reference names (no company_list_csv provided)")

    # --- FTS-CORDIS bridge ---
    fts_cordis_path = enrichment_dir / 'fts_via_cordis.csv'
    if not fts_cordis_path.exists():
        fts_cordis_path = enrichment_dir / 'fts_automotive_via_cordis.csv'
    fts_cordis = _load_enrichment_csv(fts_cordis_path, 'FTS-CORDIS', schema_key='fts_cordis')
    if len(fts_cordis) > 0:
        fts_cordis['_sid'] = fts_cordis['source_record_id'].astype(str)
        fts_fts_ids = set(combined.loc[combined['source'] == 'FTS', 'source_record_id'].astype(str))
        before = len(fts_cordis)
        fts_cordis = fts_cordis[~fts_cordis['_sid'].isin(fts_fts_ids)].drop(columns=['_sid'])
        log.info(f"    FTS-CORDIS dedup: {before - len(fts_cordis)} overlap, {len(fts_cordis)} new")

        if len(fts_cordis) > 0:
            fts_cordis['source'] = 'FTS_CORDIS'
            fts_cordis['financial_instrument_class'] = fts_cordis.get(
                'financial_instrument_class', pd.Series('grant', index=fts_cordis.index))
            fts_cordis['fiscal_source_type'] = 'eu_budget_direct'

            # Extract the actual company name from each row based on signal type.
            # For 'beneficiary_name' signal: the FTS beneficiary IS the company.
            # For 'cordis_company' signal: the company is in the CORDIS company_names
            #   column (pipe-delimited), NOT the FTS beneficiary (which is often a
            #   university or research org in the same consortium).
            fts_cordis[ref_col] = fts_cordis.apply(
                lambda row: _extract_company_from_cordis_row(row, _company_re), axis=1
            )

            # Drop rows where no company could be identified — these are consortium
            # projects where no participant matched the company list.
            before_attr = len(fts_cordis)
            fts_cordis = fts_cordis[~fts_cordis[ref_col].isin(
                ['unattributed_consortium_rd', 'unattributed_rd']
            )].copy()
            log.info(f"    FTS-CORDIS attribution: {len(fts_cordis)} attributed, "
                     f"{before_attr - len(fts_cordis)} unattributed dropped")

            fts_cordis[type_col] = 'fts_cordis_' + fts_cordis.get(
                'signal', pd.Series('unknown', index=fts_cordis.index)).fillna('unknown')
            fts_cordis[score_col] = fts_cordis.get('signal', pd.Series('', index=fts_cordis.index)).map({
                'beneficiary_name': 100.0, 'cordis_company': 90.0,
                'company+topic': 95.0, 'topic_keyword': 70.0,
            }).fillna(50.0)
            # Tag match quality by signal
            fts_cordis['match_quality'] = fts_cordis.get('signal', pd.Series('', index=fts_cordis.index)).map({
                'beneficiary_name': 'confirmed',
                'cordis_company': 'cordis_consortium',
                'company+topic': 'cordis_consortium',
                'topic_keyword': 'topic_only',
            }).fillna('unclassified')

            _align_and_concat(combined, fts_cordis)
            combined = pd.concat([combined, fts_cordis[combined.columns.intersection(fts_cordis.columns)]],
                                 ignore_index=True)
            stats['fts_cordis'] = {'rows': len(fts_cordis), 'eur': fts_cordis['amount_eur'].sum()}

    # --- EIB promoter ---
    eib_path = enrichment_dir / 'eib_enriched.csv'
    eib = _load_enrichment_csv(eib_path, 'EIB promoter', schema_key='eib_promoter')
    if len(eib) > 0:
        eib['_sid'] = eib['source_record_id'].astype(str)
        core_eib_ids = set(combined.loc[combined['source'] == 'EIB', 'source_record_id'].astype(str))
        before = len(eib)
        eib = eib[~eib['_sid'].isin(core_eib_ids)].drop(columns=['_sid'])
        log.info(f"    EIB dedup: {before - len(eib)} overlap, {len(eib)} new")

        if len(eib) > 0:
            eib['source'] = 'EIB'
            eib['financial_instrument_class'] = 'loan'
            eib['fiscal_source_type'] = 'ifi_balance_sheet'
            combined = pd.concat([combined, eib[combined.columns.intersection(eib.columns)]],
                                 ignore_index=True)
            stats['eib_promoter'] = {'rows': len(eib), 'eur': eib['amount_eur'].sum()}

    # --- IPCEI (PDF-grounded) ---
    # ipcei_reference.run_ipcei_enrichment writes ipcei_matched_participants.csv
    # with per-company aid amounts extracted from the 12 IPCEI decision PDFs.
    # Amounts are bracket-range midpoints (EC redacts exact figures in public
    # decision text); amount_confidence tags each row as exact_from_pdf,
    # range_from_pdf, or redacted. A non-empty ipcei_ticker column identifies
    # which IPCEI programme the row belongs to. See src/enrichment/ipcei_pdf_parser.py.
    ipcei_path = enrichment_dir / 'ipcei_matched_participants.csv'
    if not ipcei_path.exists():
        ipcei_path = enrichment_dir / 'ipcei_automotive_participants.csv'
    ipcei = _load_enrichment_csv(ipcei_path, 'IPCEI', schema_key='ipcei')
    if len(ipcei) > 0 and 'amount_eur' in ipcei.columns:
        ipcei_with_amounts = ipcei[ipcei['amount_eur'].notna() & (ipcei['amount_eur'] > 0)]
        if len(ipcei_with_amounts) > 0:
            ipcei_with_amounts = ipcei_with_amounts.copy()
            # Defaults (the PDF-parser wrapper already sets these but be safe
            # in case an older enrichment CSV is being re-used):
            if 'source' not in ipcei_with_amounts.columns or ipcei_with_amounts['source'].isna().all():
                ipcei_with_amounts['source'] = 'IPCEI_state_aid'
            if 'financial_instrument_class' not in ipcei_with_amounts.columns:
                ipcei_with_amounts['financial_instrument_class'] = 'grant'
            if 'fiscal_source_type' not in ipcei_with_amounts.columns:
                ipcei_with_amounts['fiscal_source_type'] = 'national_budget'
            if 'ipcei_ticker' not in ipcei_with_amounts.columns:
                ipcei_with_amounts['ipcei_ticker'] = ipcei_with_amounts.get(
                    'ipcei', pd.Series('', index=ipcei_with_amounts.index)
                ).fillna('')
            if 'amount_confidence' not in ipcei_with_amounts.columns:
                ipcei_with_amounts['amount_confidence'] = 'range_from_pdf'

            # Populate matching columns so group assignment works.
            if ref_col not in ipcei_with_amounts.columns or ipcei_with_amounts[ref_col].isna().all():
                ipcei_with_amounts[ref_col] = ipcei_with_amounts.get(
                    'match_reference_name',
                    ipcei_with_amounts.get('company_name',
                        ipcei_with_amounts.get('company', pd.Series('', index=ipcei_with_amounts.index)))
                ).fillna('')
            if type_col not in ipcei_with_amounts.columns:
                ipcei_with_amounts[type_col] = 'ipcei_reference'
            if score_col not in ipcei_with_amounts.columns:
                ipcei_with_amounts[score_col] = 100

            combined = pd.concat(
                [combined, ipcei_with_amounts[combined.columns.intersection(ipcei_with_amounts.columns)]],
                ignore_index=True)
            n_range = (ipcei_with_amounts['amount_confidence'] == 'range_from_pdf').sum() if 'amount_confidence' in ipcei_with_amounts.columns else 0
            n_exact = (ipcei_with_amounts['amount_confidence'] == 'exact_from_pdf').sum() if 'amount_confidence' in ipcei_with_amounts.columns else 0
            log.info(f"    IPCEI confidence: {n_exact} exact_from_pdf, {n_range} range_from_pdf")
            stats['ipcei'] = {'rows': len(ipcei_with_amounts), 'eur': ipcei_with_amounts['amount_eur'].sum()}

    # --- Ad hoc state-aid decision pre-load (H8) ---
    # sa_adhoc_parser.py writes one row per matched ad hoc decision. Each row
    # carries the SA code, the beneficiary extracted from the decision title,
    # the match against the user's reference list, and (when regex could
    # extract it) an EUR amount from the decision PDF. Rows without an
    # extracted amount are kept as audit hints — downstream's `amount_eur > 0`
    # filter drops them from headline totals automatically.
    #
    # SA-code de-duplication: any SA code that already appears in a TAM row
    # flowing out of harmonization is dropped here, because the TAM row is
    # authoritative and more granular. The preload is additive coverage only.
    adhoc_path = enrichment_dir / 'sa_adhoc_matched.csv'
    adhoc = _load_enrichment_csv(adhoc_path, 'Ad hoc preload', schema_key='sa_adhoc')
    if len(adhoc) > 0:
        existing_tam_sa = set(
            combined.loc[combined['source'] == 'TAM', 'source_record_id']
            .astype(str)
            .map(lambda s: s.strip())
            .unique()
        )
        before = len(adhoc)
        adhoc['sa_case'] = adhoc['sa_case'].astype(str).str.strip()
        adhoc = adhoc[~adhoc['sa_case'].isin(existing_tam_sa)].copy()
        log.info(
            f"    Ad hoc dedup vs existing TAM: {before - len(adhoc)} already in TAM, "
            f"{len(adhoc)} genuinely new"
        )
        if len(adhoc) > 0:
            # Populate consolidation-ready columns.
            adhoc['source'] = 'TAM'
            adhoc['source_record_id'] = adhoc['sa_case']
            adhoc['beneficiary_name'] = adhoc.get('extracted_beneficiary', '')
            adhoc['financial_instrument_class'] = adhoc.get(
                'financial_instrument_class', pd.Series('grant', index=adhoc.index)
            ).fillna('grant')
            adhoc['fiscal_source_type'] = 'national_budget'
            adhoc['is_adhoc_preloaded'] = True
            if 'amount_confidence' not in adhoc.columns:
                adhoc['amount_confidence'] = 'not_extracted'
            # Matcher metadata — so group assignment and match-quality checks
            # treat these rows like any other matched row.
            if ref_col not in adhoc.columns or adhoc[ref_col].isna().all():
                adhoc[ref_col] = adhoc.get('match_reference_name', '')
            if type_col not in adhoc.columns:
                adhoc[type_col] = 'sa_adhoc_preload'
            if score_col not in adhoc.columns:
                adhoc[score_col] = 100

            combined = pd.concat(
                [combined, adhoc[combined.columns.intersection(adhoc.columns)]],
                ignore_index=True,
            )
            n_with_amt = adhoc['amount_eur'].notna().sum() if 'amount_eur' in adhoc.columns else 0
            log.info(
                f"    Ad hoc preload: {len(adhoc)} rows merged, "
                f"{n_with_amt} with an extracted amount, "
                f"{len(adhoc) - n_with_amt} name-only (will not contribute to headline totals)"
            )
            stats['sa_adhoc'] = {
                'rows': len(adhoc),
                'rows_with_amount': int(n_with_amt),
                'eur': float(adhoc.loc[adhoc['amount_eur'].notna(), 'amount_eur'].sum())
                       if 'amount_eur' in adhoc.columns else 0.0,
            }

    # --- ETS (reported separately, not added to main total by default) ---
    ets_path = enrichment_dir / 'ets_matched_companies.csv'
    if not ets_path.exists():
        ets_path = enrichment_dir / 'ets_automotive_companies.csv'
    ets = _load_enrichment_csv(ets_path, 'EU ETS', schema_key='ets_free_allocation')
    if len(ets) > 0 and 'total_eur_free' in ets.columns:
        stats['ets'] = {'rows': len(ets), 'eur': ets['total_eur_free'].sum(),
                        'note': 'Reported separately (implicit subsidy via free carbon allowances)'}

    log.info(f"  Combined after enrichment: {len(combined):,} rows, EUR {combined['amount_eur'].sum():,.0f}")
    return combined, stats


def _align_and_concat(target_df, source_df):
    """Add missing columns to source_df to match target_df schema."""
    for col in target_df.columns:
        if col not in source_df.columns:
            source_df[col] = 'unknown' if target_df[col].dtype == 'object' else np.nan


# ============================================================================
# SUMMARY TABLES
# ============================================================================

def build_summary_tables(df, group_summary=None, prefix='match'):
    """Build generic summary tables applicable to any sector."""
    tables = {}
    ref_col = f'{prefix}_reference_name'

    # Ensure ref_col exists
    if ref_col not in df.columns:
        for candidate in ['automotive_reference_name', 'match_reference_name', 'reference_name']:
            if candidate in df.columns:
                ref_col = candidate
                break

    # T1: By source
    tables['T1_by_source'] = df.groupby('source').agg(
        total_eur=('amount_eur', 'sum'),
        rows=('amount_eur', 'count'),
        n_entities=(ref_col, 'nunique'),
    ).reset_index().sort_values('total_eur', ascending=False)

    # T2: By country
    if 'country' in df.columns:
        tables['T2_by_country'] = df.groupby('country').agg(
            total_eur=('amount_eur', 'sum'),
            rows=('amount_eur', 'count'),
            n_entities=(ref_col, 'nunique'),
        ).reset_index().sort_values('total_eur', ascending=False)

    # T3: By financial instrument
    tables['T3_by_instrument'] = df.groupby('financial_instrument_class').agg(
        total_eur=('amount_eur', 'sum'),
        rows=('amount_eur', 'count'),
        n_entities=(ref_col, 'nunique'),
    ).reset_index().sort_values('total_eur', ascending=False)

    # T4: By year
    if 'year' in df.columns:
        df_yr = df[df['year'].notna()].copy()
        tables['T4_by_year'] = df_yr.groupby('year').agg(
            total_eur=('amount_eur', 'sum'),
            rows=('amount_eur', 'count'),
            n_entities=(ref_col, 'nunique'),
        ).reset_index()

    # T5: By fiscal source type
    if 'fiscal_source_type' in df.columns:
        tables['T5_by_fiscal_source'] = df.groupby('fiscal_source_type').agg(
            total_eur=('amount_eur', 'sum'),
            rows=('amount_eur', 'count'),
        ).reset_index().sort_values('total_eur', ascending=False)

    # T6: Top 30 entities
    entity_totals = df.groupby(ref_col).agg(
        total_eur=('amount_eur', 'sum'),
        rows=('amount_eur', 'count'),
        n_sources=('source', 'nunique'),
        n_countries=('country', 'nunique') if 'country' in df.columns else ('source', 'nunique'),
    ).reset_index().sort_values('total_eur', ascending=False)
    tables['T6_top_entities'] = entity_totals.head(30)

    # T7: Year × instrument time series
    if 'year' in df.columns:
        df_yr = df[df['year'].notna()].copy()
        tables['T7_year_x_instrument'] = df_yr.groupby(['year', 'financial_instrument_class']).agg(
            total_eur=('amount_eur', 'sum'),
        ).reset_index()

    # T8: Pre/post 2020 shift
    if 'year' in df.columns:
        df_yr = df[df['year'].notna()].copy()
        df_yr['period'] = df_yr['year'].apply(lambda y: 'pre_2020' if y < 2020 else 'post_2020')
        tables['T8_period_shift'] = df_yr.groupby('period').agg(
            total_eur=('amount_eur', 'sum'),
            rows=('amount_eur', 'count'),
            n_entities=(ref_col, 'nunique'),
        ).reset_index()

    # Group-level tables (if groups assigned)
    if group_summary is not None and 'parent_group' in df.columns:
        # TG1: Group ranking
        tables['TG1_group_ranking'] = group_summary

        # TG2: Group × source
        g2 = df.groupby(['parent_group', 'source']).agg(
            total_eur=('amount_eur', 'sum'),
        ).reset_index()
        g2_pivot = g2.pivot_table(index='parent_group', columns='source',
                                   values='total_eur', fill_value=0)
        g2_pivot['total'] = g2_pivot.sum(axis=1)
        tables['TG2_group_x_source'] = g2_pivot.sort_values('total', ascending=False)

        # TG3: Group × instrument
        g3 = df.groupby(['parent_group', 'financial_instrument_class']).agg(
            total_eur=('amount_eur', 'sum'),
        ).reset_index()
        g3_pivot = g3.pivot_table(index='parent_group', columns='financial_instrument_class',
                                   values='total_eur', fill_value=0)
        g3_pivot['total'] = g3_pivot.sum(axis=1)
        tables['TG3_group_x_instrument'] = g3_pivot.sort_values('total', ascending=False)

        # TG4: Group × country
        if 'country' in df.columns:
            g4 = df.groupby(['parent_group', 'country']).agg(
                total_eur=('amount_eur', 'sum'),
            ).reset_index()
            g4_pivot = g4.pivot_table(index='parent_group', columns='country',
                                       values='total_eur', fill_value=0)
            g4_pivot['total'] = g4_pivot.sum(axis=1)
            tables['TG4_group_x_country'] = g4_pivot.sort_values('total', ascending=False)

    return tables


# ============================================================================
# CONCENTRATION METRICS
# ============================================================================

def build_concentration_metrics(df, col):
    """Compute HHI, Top5%, Gini concentration metrics.

    Parameters
    ----------
    df : pd.DataFrame
    col : str
        Column to group by (entity name or parent_group).
    """
    company_eur = df.groupby(col)['amount_eur'].sum().sort_values(ascending=False)
    total = company_eur.sum()
    n = len(company_eur)
    if n == 0 or total == 0:
        return {'count': 0, 'total_eur': 0, 'hhi': 0, 'top5_pct': 0, 'gini': 0}

    shares = company_eur / total
    hhi = (shares ** 2).sum() * 10000
    top1 = shares.iloc[0] * 100 if n >= 1 else 0
    top5 = shares.iloc[:5].sum() * 100 if n >= 5 else 0
    top10 = shares.iloc[:10].sum() * 100 if n >= 10 else 0
    top20 = shares.iloc[:20].sum() * 100 if n >= 20 else 0

    cum_share = shares.sort_values().cumsum()
    gini = 1 - 2 * cum_share.sum() / n if n > 0 else 0

    return {
        'count': n,
        'total_eur': float(total),
        'hhi': round(float(hhi), 1),
        'top1_pct': round(float(top1), 1),
        'top5_pct': round(float(top5), 1),
        'top10_pct': round(float(top10), 1),
        'top20_pct': round(float(top20), 1),
        'gini': round(float(gini), 3),
        'top1_name': str(company_eur.index[0]) if n >= 1 else '',
        'top5_names': [str(x) for x in company_eur.index[:5]],
    }


# ============================================================================
# PHASE 2b: CROSS-SOURCE DEDUPLICATION HELPERS
# ============================================================================

def _load_programme_map(match_log_path: Path) -> dict:
    """Load programme + fund from enriched.csv co-located with match_log.csv.

    Returns {source_record_id_str: (programme, fund)} dict.
    """
    enriched = Path(match_log_path).parent / 'enriched.csv'
    if not enriched.exists():
        return {}
    peek = pd.read_csv(enriched, nrows=0)
    cols = [c for c in ['source_record_id', 'programme', 'fund'] if c in peek.columns]
    if 'source_record_id' not in cols:
        return {}
    df = pd.read_csv(enriched, usecols=cols, low_memory=False)
    progs = df.get('programme', pd.Series([''] * len(df)))
    funds = df.get('fund', pd.Series([''] * len(df)))
    return {
        str(sid): (str(prog or ''), str(fund or ''))
        for sid, prog, fund in zip(df['source_record_id'], progs, funds)
    }


def _dedup_fts_identical_transactions(df: pd.DataFrame, prog_map: dict) -> pd.DataFrame:
    """Set dc_preferred=False for FTS rows that are confirmed echoes of INNOVFUND or CINEA.

    FTS captures budget payment outflows — the same Innovation Fund or CINEA-managed
    grant appears in both FTS (as a payment line) and the authoritative award database
    (INNOVFUND or CINEA). The authoritative row is kept (dc_preferred=True); the FTS
    echo is flagged (dc_preferred=False).

    Amount tolerance: ≤ 0.1% (same grant to the cent; year may differ by 1-2 years).
    FTS/CINEA: shared project ID is definitive; amount check not applied.

    Does NOT remove rows — preserves all data for research.
    """
    IF_KW = ['innovation fund', 'o.0.1']
    CINEA_KW = ['connecting europe', 'cef', 'life', 'environment and climate action',
                'emfaf', 'maritime, fisheries']

    fts_mask = df['source'] == 'FTS'
    if not fts_mask.any():
        return df

    # Map programme to each FTS row
    fts_idx = df.index[fts_mask]
    fts_prog = df.loc[fts_idx, 'source_record_id'].astype(str).map(
        lambda sid: prog_map.get(sid, ('', ''))[0].lower()
    )

    # --- FTS / INNOVFUND (amount match ≤ 0.1%) ---
    if_mask_local = fts_prog.map(lambda p: any(kw in p for kw in IF_KW))
    if_fts_idx = fts_idx[if_mask_local]
    if len(if_fts_idx) > 0:
        innov = df[df['source'] == 'INNOVFUND'][['match_reference_name', 'source_record_id', 'amount_eur']]
        for idx in if_fts_idx:
            row = df.loc[idx]
            matches = innov[innov['match_reference_name'] == row['match_reference_name']]
            for _, innov_row in matches.iterrows():
                try:
                    a = float(row['amount_eur'])
                    b = float(innov_row['amount_eur'])
                except (ValueError, TypeError):
                    continue
                if max(abs(a), abs(b)) == 0:
                    continue
                if abs(a - b) / max(abs(a), abs(b)) <= 0.001:
                    df.at[idx, 'dc_preferred'] = False
                    existing = df.at[idx, 'dc_flag']
                    df.at[idx, 'dc_flag'] = (existing + '|' if existing else '') + 'confirmed_duplicate:fts_innovfund'
                    df.at[idx, 'cofinancing_partner_id'] = str(innov_row['source_record_id'])
                    break

    # --- FTS / CINEA (shared project ID = definitive) ---
    cinea_mask_local = fts_prog.map(lambda p: any(kw in p for kw in CINEA_KW))
    cinea_fts_idx = fts_idx[cinea_mask_local]
    if len(cinea_fts_idx) > 0:
        cinea_ids = set(df.loc[df['source'] == 'CINEA', 'source_record_id'].astype(str))
        fts_shared = df.loc[cinea_fts_idx, 'source_record_id'].astype(str)
        drop_local = fts_shared[fts_shared.isin(cinea_ids)].index
        for idx in drop_local:
            df.at[idx, 'dc_preferred'] = False
            existing = df.at[idx, 'dc_flag']
            df.at[idx, 'dc_flag'] = (existing + '|' if existing else '') + 'confirmed_duplicate:fts_cinea'

    return df


# ---------------------------------------------------------------------------
# Fund alias mapping — shared by _flag_cofinancing_overlaps and
# _flag_pdf_cofin_overlaps.  Keys = canonical acronyms (matching sa_cofin_fund
# values produced by sa_pdf_parser).  Values = lowercase substrings that may
# appear in any source's 'fund' column.
#
# Coverage: expanded 2026-03-25 to include every EU funding instrument observed
# in the consolidated output's 'fund' column across all sources (FTS, KOHESIO,
# CINEA, INNOVFUND, etc.).  Administrative budget lines (buildings, equipment,
# publications office) are intentionally excluded — they are not EU funding
# programmes and would create false positives.
#
# National-language coverage: expanded 2026-04-12 to close the gaps listed in
# research/PIPELINE_AUDIT.md finding H1.  Each canonical fund now carries its
# principal Polish, Czech, Hungarian, Romanian, Slovak, Slovenian, and Baltic
# variants in addition to the EN/FR/DE/ES/IT forms.
# ---------------------------------------------------------------------------
_FUND_ALIASES: dict[str, set] = {
    # --- Cohesion / structural funds ---
    'ERDF':       {'erdf', 'feder', 'efre', 'fesr', 'efrr', 'fedr',
                   'european regional development',
                   'europejski fundusz rozwoju regionalnego',
                   'evropsky fond pro regionalni rozvoj',
                   'europai regionalis fejlesztesi alap'},
    'ESF':        {'esf', 'esf+', 'esf plus', 'fse', 'esza',
                   'european social fund', 'youth employment initiative',
                   'europejski fundusz spoleczny',
                   'evropsky socialni fond',
                   'europai szocialis alap'},
    'CF':         {'cohesion fund', 'fonds de cohesion', 'fonds de cohésion',
                   'koh\u00e4sionsfonds', 'fundusz spojnosci',
                   'fond soudrznosti', 'kohezios alap'},
    'JTF':        {'jtf', 'just transition', 'ftr',
                   'fonds pour une transition juste',
                   'fundusz sprawiedliwej transformacji',
                   'fond pro spravedlivou transformaci',
                   'meltanyos atallasi alap'},
    'ESIF':       {'esif', 'structural and investment', 'structural funds'},
    'INTERREG':   {'interreg'},

    # --- Recovery / resilience ---
    'RRF':        {'rrf', 'recovery and resilience'},

    # --- Agriculture / rural ---
    'EAFRD':      {'eafrd', 'feader'},
    'EAGF':       {'eagf', 'feaga'},
    'EMFAF':      {'emfaf', 'european maritime, fisheries and aquaculture',
                   'maritime, fisheries'},

    # --- Research / innovation ---
    # Horizon Europe budget lines in FTS / CORDIS use the cluster naming
    # scheme "Cluster N — <domain>"; raw-data audit (plan §4 / L3) found
    # ~335M master rows carrying these cluster strings with no ERDF-style
    # alias. Adding the cluster prefixes and the main ERC / MSCA / JRC
    # research-overhead lines closes the biggest remaining FTS fund-
    # attribution gap.
    'HORIZON':    {'horizon 2020', 'horizon europe', 'h2020', 'horizon-',
                   'research framework programme', 'euratom',
                   'nuclear fission', 'joint research centre',
                   'research programme for coal', 'research programme for steel',
                   # --- Horizon Europe clusters (Pillar II) ---
                   'cluster health',
                   'cluster culture',
                   'cluster civil security',
                   'cluster digital',
                   'cluster climate',
                   'cluster food',
                   'cluster industry',
                   # --- Marie Skłodowska-Curie + ERC + widening ---
                   'marie sklodowska', 'marie curie', 'msca',
                   'european research council', 'erc grant',
                   'widening participation', 'teaming', 'twinning',
                   # --- Horizon operational / management lines ---
                   'non-nuclear actions of jrc',
                   'non nuclear actions of jrc',
                   'completion of previous research programmes',
                   'other management expenditure for research'},
    'EIC':        {'european innovation council', 'eic'},
    'INNOVFUND':  {'innovation fund', 'innovfund', 'innovation fund (if)'},

    # --- Transport / infrastructure / environment ---
    'CEF':        {'connecting europe facility', 'cef',
                   'trans-european networks', 'trans-european transport'},
    'LIFE':       {'life+', 'life programme', 'programme life',
                   'programme for the environment and climate action',
                   'environment and climate action',
                   'completion of previous programmes in the field of environment'},

    # --- Competitiveness / digital / skills ---
    'ERASMUS':    {'erasmus+', 'erasmus plus', 'lifelong learning programme',
                   'promoting learning mobility'},
    'COSME':      {'cosme', 'competitiveness and innovation framework',
                   'entrepreneurship and innovation programme'},
    'DEP':        {'digital europe programme', 'dep',
                   'european cybersecurity', 'artificial intelligence'},
    'SKILLS':     {'skills'},

    # --- External / neighbourhood ---
    'ENI':        {'european neighbourhood', 'neighbourhood instrument',
                   'neighbourhood and partnership'},
    'IPA':        {'ipa', 'pre-accession', 'candidate countries', 'cards'},

    # --- Defence / security ---
    'EDIDP':      {'edidp', 'european defence industrial development',
                   'defence industrial reinforcement', 'defence research',
                   'military mobility'},

    # --- Humanitarian / crisis ---
    'UCPM':       {'union civil protection mechanism', 'humanitarian aid',
                   'instrument for stability', 'crisis response'},
}



def _flag_cofinancing_overlaps(
    df: pd.DataFrame,
    year_win: int = 2,
    ratio_min: float = 0.01,
    ratio_max: float = 1.50,
) -> pd.DataFrame:
    """Flag TAM rows where a KOHESIO or RRF record exists for the same entity+country.

    TAM = total approved national state aid (EU co-financing + national share).
    KOHESIO = EU co-financing share only (typically 50-85% of total investment).
    These are structurally different views of the same underlying investment.

    Amount check: ratio KOHESIO/TAM must be in [0.01, 1.50] — not a traditional
    tolerance but a plausibility check (KOHESIO is always a fraction of TAM, but
    multi-year KOHESIO disbursements can exceed a single annual TAM commitment).

    Fund check (KOHESIO only): if the KOHESIO row has a non-empty 'fund' column,
    we require that fund to match one of the known EU structural fund aliases in
    _FUND_ALIASES. This prevents false positives where two unrelated records from
    different programmes happen to share a beneficiary name and a plausible amount
    ratio. If the KOHESIO 'fund' column is empty, the amount-ratio check alone is
    used (conservative: do not discard uncertain cases).

    TAM rows are marked dc_preferred=False; KOHESIO/RRF rows stay dc_preferred=True.
    """
    # Flat set of all known structural fund substrings (lowercase) from _FUND_ALIASES.
    # Compiled into a word-boundary alternation so short aliases ("dep", "esf",
    # "eic", "cef", "ipa") do not false-positive on unrelated substrings like
    # "participation" → "ipa" or "developpement" → "dep". Sorted longest-first
    # so the alternation prefers the most specific match.
    _known_fund_aliases: set = {alias for aliases in _FUND_ALIASES.values() for alias in aliases}
    _fund_alias_re = re.compile(
        r'(?<!\w)(?:' + '|'.join(
            re.escape(a) for a in sorted(_known_fund_aliases, key=len, reverse=True)
        ) + r')(?!\w)',
        re.IGNORECASE,
    )

    def _kohesio_fund_is_structural(fund_val) -> bool:
        """Return True if fund_val is empty (unknown) or matches a known EU structural fund."""
        s = str(fund_val or '').lower().strip()
        if not s:
            return True  # no fund info — do not discard on this basis
        return bool(_fund_alias_re.search(s))

    join_cols = ['match_reference_name', 'country']
    if 'match_reference_name' not in df.columns:
        return df
    # Only consider TAM rows not yet flagged by PDF-backed dedup (which is authoritative).
    # This makes the heuristic a true fallback — it only fires on residual unflagged TAM rows.
    tam_mask = (df['source'] == 'TAM') & df['dc_preferred']
    tam = df[tam_mask][join_cols + ['source_record_id', 'year', 'amount_eur']].copy()
    if tam.empty:
        return df

    # M4 cleanup (2026-04-14): the historical 'RRF' entry in this loop
    # never fires in practice. The EC RRF source writes
    # beneficiary_name = pd.NA on every row (see
    # src/harmonization/rrf.py) so no RRF row is ever assigned a
    # match_reference_name and the join below returns zero matches.
    # National-level RRF beneficiary data comes through
    # rrf_italia_domani.py and rrf_national_top100.py — those rows
    # use source codes like 'RRF_IT' and 'RRF_NAT_TOP100' and do not
    # need the heuristic co-financing join against TAM. Removed from
    # the loop to prevent future maintainers from assuming a live
    # path exists. Plan audit H7 + M4.
    for preferred_src in ['KOHESIO']:
        src_cols = join_cols + ['source_record_id', 'year', 'amount_eur']
        # Include 'fund' column for KOHESIO if available — used for structural-fund filter
        if preferred_src == 'KOHESIO' and 'fund' in df.columns:
            src_cols = src_cols + ['fund']
        other = df[df['source'] == preferred_src][src_cols].copy()
        if other.empty:
            continue
        merged = tam.merge(other, on=join_cols, suffixes=('_tam', '_other'))
        if merged.empty:
            continue

        # Year filter
        y_t = pd.to_numeric(merged['year_tam'], errors='coerce')
        y_o = pd.to_numeric(merged['year_other'], errors='coerce')
        both_known = y_t.notna() & y_o.notna()
        merged = merged[~(both_known & ((y_t - y_o).abs() > year_win))].copy()
        if merged.empty:
            continue

        # Plausibility ratio check — KOHESIO should be in [ratio_min, ratio_max] × TAM
        a = pd.to_numeric(merged['amount_eur_tam'], errors='coerce')
        b = pd.to_numeric(merged['amount_eur_other'], errors='coerce')
        valid = a.notna() & b.notna() & (a > 0) & (b > 0)
        ratio = (b / a).where(valid)
        plausible = valid & (ratio >= ratio_min) & (ratio <= ratio_max)
        merged = merged[plausible].copy()
        if merged.empty:
            continue

        # Fund filter (KOHESIO only): drop pairs where KOHESIO fund is non-empty
        # but does not match any known EU structural fund.
        if preferred_src == 'KOHESIO' and 'fund' in merged.columns:
            fund_ok = merged['fund'].map(_kohesio_fund_is_structural)
            merged = merged[fund_ok].copy()
        if merged.empty:
            continue

        tam_ids = set(merged['source_record_id_tam'].astype(str))
        id_map = dict(zip(
            merged['source_record_id_tam'].astype(str),
            merged['source_record_id_other'].astype(str),
        ))
        flag_str = f'cofinancing_overlap:tam_{preferred_src.lower()}'
        mask = (df['source'] == 'TAM') & df['source_record_id'].astype(str).isin(tam_ids)
        # §6.5 / plan audit A-6: heuristic dedup is AUDIT-ONLY. We no
        # longer toggle ``dc_preferred`` — we set ``heuristic_flag`` so
        # the row stays in the headline view but is visible to readers
        # of the audit CSV. The PDF-grounded ``_flag_pdf_cofin_overlaps``
        # remains the only path that can exclude a row from the headline.
        if 'heuristic_flag' not in df.columns:
            df['heuristic_flag'] = ''
        df.loc[mask, 'heuristic_flag'] = df.loc[mask, 'heuristic_flag'].map(
            lambda x: (x + '|' if x else '') + flag_str
        )
        df.loc[mask, 'cofinancing_partner_id'] = (
            df.loc[mask, 'source_record_id'].astype(str).map(id_map)
        )

    return df


def _flag_ipcei_tam_overlap(df: pd.DataFrame, amount_tol: float = 0.20, year_win: int = 2) -> pd.DataFrame:
    """Flag TAM rows that correspond to IPCEI state aid decisions.

    IPCEI (Important Projects of Common European Interest) state aid is notified
    to the EC individually (SA.xxxxx) AND appears in the IPCEI reference database.
    Both databases thus capture the same state aid decision. The IPCEI_state_aid
    row is authoritative (project-level context); the TAM row is the national
    notification echo.

    Amount tolerance: ≤ 20% — IPCEI estimated amounts at EC approval may differ
    from notified SA amounts in TAM. Year window ±2 years.
    """
    ipcei = df[df['source'] == 'IPCEI_state_aid']
    tam = df[df['source'] == 'TAM']
    if ipcei.empty or tam.empty:
        return df

    joined = ipcei.merge(
        tam, on=['match_reference_name', 'country'],
        suffixes=('_ipcei', '_tam'), how='inner',
    )
    if joined.empty:
        return df

    # Year filter
    y_i = pd.to_numeric(joined['year_ipcei'], errors='coerce')
    y_t = pd.to_numeric(joined['year_tam'], errors='coerce')
    both = y_i.notna() & y_t.notna()
    joined = joined[~(both & ((y_i - y_t).abs() > year_win))].copy()

    # Amount filter
    a = pd.to_numeric(joined['amount_eur_ipcei'], errors='coerce')
    b = pd.to_numeric(joined['amount_eur_tam'], errors='coerce')
    valid = a.notna() & b.notna() & (a > 0) & (b > 0)
    joined = joined[valid].copy()
    if joined.empty:
        return df

    denom = joined['amount_eur_ipcei'].combine(joined['amount_eur_tam'], lambda x, y: max(abs(x), abs(y)))
    diff = (joined['amount_eur_ipcei'] - joined['amount_eur_tam']).abs() / denom
    joined = joined[diff <= amount_tol].copy()
    if joined.empty:
        return df

    tam_ids = set(joined['source_record_id_tam'].astype(str))
    mask = (df['source'] == 'TAM') & df['source_record_id'].astype(str).isin(tam_ids)
    df.loc[mask, 'dc_preferred'] = False
    df.loc[mask, 'dc_flag'] = df.loc[mask, 'dc_flag'].map(
        lambda x: (x + '|' if x else '') + 'confirmed_duplicate:ipcei_tam'
    )
    return df


def _dedup_same_record_multicountry(df: pd.DataFrame) -> pd.DataFrame:
    """Flag rows where the same source record appears under multiple countries.

    Some sources (notably KOHESIO) represent multi-country projects once per
    partner country — the same source_record_id + beneficiary + amount + year
    combination appears with different country values. These are structural
    duplicates: keep the first occurrence, mark the rest dc_preferred=False.

    This is a holistic structural filter. It applies to any source and any
    company list; no per-entity or per-source configuration is needed.
    """
    key = ['source_record_id', 'entity_name_clean', 'amount_eur', 'year']
    available = [c for c in key if c in df.columns]
    if len(available) < len(key):
        return df
    valid = df[available].notna().all(axis=1) & (df['amount_eur'] > 0)
    dup_mask = df[valid].duplicated(subset=available, keep='first')
    dup_idx = df[valid].index[dup_mask]
    n = len(dup_idx)
    if n > 0:
        df.loc[dup_idx, 'dc_preferred'] = False
        df.loc[dup_idx, 'dc_flag'] = df.loc[dup_idx, 'dc_flag'].map(
            lambda x: (x + '|' if x else '') + 'same_record_multicountry'
        )
        log.info(f"  Multi-country dedup: {n} rows flagged (same record, multiple countries)")
    return df


def _dedup_exact_rows(df: pd.DataFrame, ref_col: str = 'match_reference_name') -> pd.DataFrame:
    """Drop rows that are exact duplicates on all key identification fields.

    Guards against consolidation producing identical output rows when the
    same source record is processed twice (e.g. via two enrichment paths).
    """
    key = ['source', 'source_record_id', 'entity_name_clean', 'amount_eur', 'year', 'country', ref_col]
    key = [c for c in key if c in df.columns]
    before = len(df)
    df = df.drop_duplicates(subset=key, keep='first').copy()
    n = before - len(df)
    if n > 0:
        log.info(f"  Exact row dedup: removed {n} exact duplicate rows")
    return df


def _add_attribution_type(df: pd.DataFrame, prefix: str = 'match') -> pd.DataFrame:
    """Add attribution_type column classifying how each amount is linked to the entity.

    Values:
      direct            — entity received or is accountable for the amount
      consortium_partner — FTS_CORDIS row where the matched entity is NOT the
                           beneficiary but a consortium partner; amount is attributed
                           to the matched entity only because it triggered the match
      contextual        — matched via description/topic keyword, not entity name
      inferred          — matched via EIB title or other indirect signal

    Consortium partner rows are set dc_preferred=False — they inflate totals.
    """
    type_col = f'{prefix}_type'
    df['attribution_type'] = 'direct'

    if type_col in df.columns:
        mtype = df[type_col].fillna('')
        df.loc[(df['source'] == 'FTS_CORDIS') & mtype.str.contains('cordis_company', na=False),
               'attribution_type'] = 'consortium_partner'
        df.loc[(df['source'] == 'FTS_CORDIS') & mtype.str.contains('topic_keyword', na=False),
               'attribution_type'] = 'contextual'

    if 'match_type' in df.columns:
        df.loc[df['match_type'].str.contains('contextual|topic_only', case=False, na=False),
               'attribution_type'] = 'contextual'
        df.loc[df['match_type'].str.contains('eib_title|inferred', case=False, na=False),
               'attribution_type'] = 'inferred'

    cp_mask = df['attribution_type'] == 'consortium_partner'
    df.loc[cp_mask, 'dc_preferred'] = False
    df.loc[cp_mask, 'dc_flag'] = df.loc[cp_mask, 'dc_flag'].map(
        lambda x: (x + '|' if x else '') + 'consortium_partner_attribution'
    )
    return df


def _flag_pdf_cofin_overlaps(df: pd.DataFrame, year_win: int = 2) -> pd.DataFrame:
    """Flag TAM rows as double-counting where PDF evidence confirms EU fund co-financing
    AND another EU fund source row exists for the same beneficiary+country+year.

    Generalized: searches ALL non-TAM sources (KOHESIO, RRF, FTS, CINEA, ESIF_2014,
    ESIF_2027, etc.), not just KOHESIO. This reflects the reality that state aid can
    be co-financed by any EU fund, and we have a 'fund' column for all sources.

    Produces per-source flags: cofinancing_overlap:tam_<source_lower>_pdf
    e.g. cofinancing_overlap:tam_kohesio_pdf, cofinancing_overlap:tam_fts_pdf

    Requires sa_cofin_fund / sa_cofin_level columns added by SACofinParser.enrich_dataframe().
    If those columns are absent (enrichment not run), returns df unchanged — safe no-op.

    Only fires when sa_cofin_level == 'confirmed'. 'conditional' is ignored.
    Fund match logic: if the other source's 'fund' column is empty, trust the PDF alone.
    If non-empty, require the fund to match a canonical alias in _FUND_ALIASES.
    """
    if 'sa_cofin_fund' not in df.columns or 'sa_cofin_level' not in df.columns:
        return df
    if 'match_reference_name' not in df.columns:
        return df

    join_cols = ['match_reference_name', 'country']

    tam_confirmed = df[
        (df['source'] == 'TAM')
        & (df['sa_cofin_level'] == 'confirmed')
        & (df['sa_cofin_fund'].fillna('') != '')
    ][join_cols + ['source_record_id', 'year', 'sa_cofin_fund']].copy()
    if tam_confirmed.empty:
        return df

    fund_col_present = 'fund' in df.columns
    other_cols = join_cols + ['source', 'source_record_id', 'year']
    if fund_col_present:
        other_cols = other_cols + ['fund']

    non_tam = df[df['source'] != 'TAM'][other_cols].copy()
    if non_tam.empty:
        return df

    merged = tam_confirmed.merge(
        non_tam,
        on=join_cols,
        suffixes=('_tam', '_other'),
    )
    if merged.empty:
        return df

    # Year filter: keep pairs where both years unknown OR within year_win
    y_t = pd.to_numeric(merged['year_tam'], errors='coerce')
    y_o = pd.to_numeric(merged['year_other'], errors='coerce')
    both_known = y_t.notna() & y_o.notna()
    merged = merged[~(both_known & ((y_t - y_o).abs() > year_win))].copy()
    if merged.empty:
        return df

    # Fund match: empty fund on other side → trust PDF; non-empty → match via _FUND_ALIASES
    def _funds_match(row) -> bool:
        other_fund = str(row.get('fund', '') or '').lower().strip()
        if not other_fund:
            return True  # no fund column data — PDF evidence is sufficient
        tam_funds = {
            f.strip().upper()
            for f in str(row.get('sa_cofin_fund', '') or '').split(',')
            if f.strip()
        }
        for canonical in tam_funds:
            aliases = _FUND_ALIASES.get(canonical, {canonical.lower()})
            if any(alias in other_fund for alias in aliases):
                return True
        return False

    keep = merged.apply(_funds_match, axis=1)
    merged = merged[keep].copy()
    if merged.empty:
        return df

    # Ensure dc_flag is string (guard against NaN when loading from CSV)
    df['dc_flag'] = df['dc_flag'].fillna('').astype(str)

    total_flagged = 0
    for source_name, src_merged in merged.groupby('source'):
        tam_ids = set(src_merged['source_record_id_tam'].astype(str))
        id_map = dict(zip(
            src_merged['source_record_id_tam'].astype(str),
            src_merged['source_record_id_other'].astype(str),
        ))
        flag_str = f'cofinancing_overlap:tam_{source_name.lower()}_pdf'
        mask = (df['source'] == 'TAM') & df['source_record_id'].astype(str).isin(tam_ids)
        df.loc[mask, 'dc_preferred'] = False
        df.loc[mask, 'dc_flag'] = df.loc[mask, 'dc_flag'].map(
            lambda x: (x + '|' if x else '') + flag_str
        )
        df.loc[mask, 'cofinancing_partner_id'] = (
            df.loc[mask, 'source_record_id'].astype(str).map(id_map)
        )
        n = mask.sum()
        total_flagged += n
        if n > 0:
            log.info(f"    PDF co-fin overlap ({source_name}): {n} TAM rows flagged")

    if total_flagged > 0:
        log.info(f"  PDF co-fin overlaps total: {total_flagged} TAM rows flagged")
    return df


# ============================================================================
# MAIN CONSOLIDATION ENTRY POINT
# ============================================================================

def consolidate(
    match_log_csv: Path,
    output_dir: Path,
    parent_groups: dict | Path | None = None,
    enrichment_dir: Path | None = None,
    prefix: str = 'match',
    company_list_csv: Path | None = None,
    aliases_json: Path | None = None,
    run_pdf_enrichment: bool = True,
    pdf_cache_dir: Path | None = None,
    use_llm: bool = False,
    dedup_config: DedupConfig | None = None,
) -> pd.DataFrame:
    """Run the full generic consolidation pipeline.

    Parameters
    ----------
    match_log_csv : Path
        Path to the match_log.csv from generic_matcher.
    output_dir : Path
        Where to save consolidated outputs.
    parent_groups : dict | Path | None
        Optional parent group definitions. If Path, loads from JSON.
        Format: {"Group Name": ["member1", "member2", ...], ...}
    enrichment_dir : Path | None
        Directory containing enrichment output CSVs.
    prefix : str
        Column prefix used by the matcher (e.g., 'automotive', 'match').
    run_pdf_enrichment : bool
        If True, download and parse SA decision PDFs to detect EU fund co-financing
        for TAM rows. Results are used by _flag_pdf_cofin_overlaps(). Default True
        (aligned with the CLI default in run_pipeline.py; pass False to skip).
    pdf_cache_dir : Path | None
        Directory to cache downloaded SA PDFs. Defaults to a repo-level cache at
        REPO_ROOT/data/cache/sa_decisions so that PDFs accumulate across runs.
    use_llm : bool
        If True, use Claude Haiku as Tier 3 fallback in PDF enrichment when regex
        finds no signal. Requires ANTHROPIC_API_KEY. Only used if run_pdf_enrichment=True.
    dedup_config : DedupConfig | None
        Optional override for cross-source dedup thresholds (year windows, amount
        ratio bands, IPCEI tolerance). Defaults to DEFAULT_DEDUP_CONFIG, which
        matches the previously hardcoded values. See DedupConfig for the full
        list of tunables.

    Returns
    -------
    pd.DataFrame
        The consolidated dataset.
    """
    import sys
    sys.stdout.reconfigure(encoding='utf-8')

    t0 = time.time()
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # L2: resolve dedup configuration. The default preserves the legacy
    # hardcoded thresholds, so behaviour is unchanged unless a caller passes
    # an explicit DedupConfig override.
    if dedup_config is None:
        dedup_config = DEFAULT_DEDUP_CONFIG

    ref_col = f'{prefix}_reference_name'
    type_col = f'{prefix}_type'
    score_col = f'{prefix}_score'

    log.info("=" * 70)
    log.info("CONSOLIDATION PIPELINE")
    log.info("=" * 70)

    # ------------------------------------------------------------------
    # Phase 1: Load core matches
    # ------------------------------------------------------------------
    log.info("\nPhase 1: Loading core matches...")
    match_log_csv = Path(match_log_csv)
    if not match_log_csv.exists():
        log.error(f"Match log not found: {match_log_csv}")
        return pd.DataFrame()

    core = pd.read_csv(match_log_csv, low_memory=False)
    log.info(f"  Core matches: {len(core):,} rows, EUR {core['amount_eur'].sum():,.0f}")

    # ------------------------------------------------------------------
    # Phase 2: Enrichment integration
    # ------------------------------------------------------------------
    log.info("\nPhase 2: Enrichment integration...")
    combined, enrichment_stats = integrate_enrichment(
        core, enrichment_dir, prefix=prefix,
        company_list_csv=company_list_csv, aliases_json=aliases_json,
    )

    # ------------------------------------------------------------------
    # Phase 2c: SA PDF co-financing enrichment (optional)
    # Downloads EC state aid decision PDFs and extracts EU fund co-financing
    # evidence. Must run BEFORE dedup so _flag_pdf_cofin_overlaps has data.
    # Activate with: run_pipeline.py --pdf-enrichment [--use-llm]
    # ------------------------------------------------------------------
    if run_pdf_enrichment:
        log.info("\nPhase 2c: SA PDF co-financing enrichment... (ON — pass "
                 "run_pdf_enrichment=False or CLI --no-pdf-enrichment to skip)")
        from src.enrichment.sa_pdf_parser import SACofinParser
        from src.enrichment.sa_case_lookup import SACaseLookup
        # Resolve repo root by walking up from match_log_csv until we find case-data-SA.json
        # (depth varies: default pipeline puts it at .parent×4, sector subfolders at .parent×5)
        _candidate = Path(match_log_csv).resolve()
        _sa_json = None
        for _ in range(8):
            _candidate = _candidate.parent
            if (_candidate / 'case-data-SA.json').exists():
                _sa_json = _candidate / 'case-data-SA.json'
                break
        if _sa_json is None:
            _sa_json = Path(match_log_csv).resolve().parent.parent.parent.parent / 'case-data-SA.json'

        # Ensure sa_case column exists — normalise TAM source_record_id to SA.XXXXX format
        from src.enrichment.sa_case_lookup import normalise_sa
        if 'sa_case' not in combined.columns:
            combined['sa_case'] = ''
        tam_mask = combined['source'] == 'TAM'
        combined.loc[tam_mask, 'sa_case'] = (
            combined.loc[tam_mask, 'source_record_id'].astype(str).map(normalise_sa)
        )

        sa_lookup = SACaseLookup(_sa_json).load()
        # PDF cache defaults to a repo-level shared directory so that downloaded
        # decision PDFs accumulate across runs — SA codes are immutable, so
        # re-running on the same (or overlapping) company lists pays the download
        # cost only once. Override with the `pdf_cache_dir` argument if needed.
        if pdf_cache_dir:
            pdf_cache = Path(pdf_cache_dir)
        else:
            try:
                from src.paths import REPO_ROOT as _REPO
                pdf_cache = _REPO / 'data' / 'cache' / 'sa_decisions'
            except Exception:
                pdf_cache = output_dir / 'sa_decisions'
        pdf_cache.mkdir(parents=True, exist_ok=True)
        log.info(f"  PDF cache: {pdf_cache}")
        parser = SACofinParser(cache_dir=pdf_cache, use_llm=use_llm)
        combined = parser.enrich_dataframe(combined, sa_lookup)
    else:
        log.info("\nPhase 2c: SA PDF co-financing enrichment... SKIPPED "
                 "(run_pdf_enrichment=False). Cross-source dedup will fall back "
                 "to the heuristic amount-ratio path only.")

    # ------------------------------------------------------------------
    # Phase 2b: Reference name normalisation + cross-source dedup
    # ------------------------------------------------------------------
    log.info("\nPhase 2b: Reference name normalisation & cross-source dedup...")

    # Normalise match_reference_name to consistent upper-case so chart groupby
    # operations are not split by capitalisation differences in the input CSV
    # (e.g. "UMICORE" vs "Umicore" from different alias rows).
    if ref_col in combined.columns:
        combined[ref_col] = combined[ref_col].str.strip().str.upper()

    # Initialise dedup columns
    combined['dc_flag'] = ''
    combined['dc_preferred'] = True
    # heuristic_flag is a *separate* audit column populated by the
    # amount-ratio heuristic in `_flag_cofinancing_overlaps`. It does
    # NOT touch `dc_preferred`, so heuristic-flagged rows remain in
    # the headline view. This enforces the §6.5 principle: only
    # document-grounded evidence moves a row out of the headline.
    combined['heuristic_flag'] = ''
    combined['attribution_type'] = 'direct'
    combined['cofinancing_partner_id'] = ''

    # Attach programme + fund from enriched.csv (dropped during standard consolidation)
    prog_map = _load_programme_map(match_log_csv)
    n_mapped = sum(1 for v in prog_map.values() if v[0])
    log.info(f"  Programme map: {n_mapped:,} rows with programme info")
    combined['programme'] = combined['source_record_id'].astype(str).map(
        lambda sid: prog_map.get(sid, ('', ''))[0]
    ).fillna('')
    combined['fund'] = combined['source_record_id'].astype(str).map(
        lambda sid: prog_map.get(sid, ('', ''))[1]
    ).fillna('')

    # Ensure dedup functions can find 'match_reference_name' regardless of prefix
    if ref_col != 'match_reference_name' and ref_col in combined.columns:
        combined['match_reference_name'] = combined[ref_col]

    # Apply dedup logic (each function sets dc_preferred=False + dc_flag on affected rows)
    # Priority: PDF-backed dedup is authoritative — runs first.
    # Heuristic is fallback only — skips TAM rows already flagged by PDF.
    # L2: thresholds come from DedupConfig so a caller can override any single
    # value without touching the function bodies.
    combined = _dedup_fts_identical_transactions(combined, prog_map)
    combined = _flag_pdf_cofin_overlaps(
        combined, year_win=dedup_config.pdf_cofin_year_window,
    )
    combined = _flag_cofinancing_overlaps(
        combined,
        year_win=dedup_config.heuristic_cofin_year_window,
        ratio_min=dedup_config.heuristic_cofin_ratio_min,
        ratio_max=dedup_config.heuristic_cofin_ratio_max,
    )
    combined = _dedup_same_record_multicountry(combined)
    combined = _flag_ipcei_tam_overlap(
        combined,
        amount_tol=dedup_config.ipcei_tam_amount_tolerance,
        year_win=dedup_config.ipcei_tam_year_window,
    )
    combined = _add_attribution_type(combined, prefix=prefix)

    n_not_preferred = (~combined['dc_preferred']).sum()
    n_flagged = (combined['dc_flag'] != '').sum()
    eur_preferred = combined.loc[combined['dc_preferred'], 'amount_eur'].sum()
    log.info(f"  dc_preferred=False: {n_not_preferred:,} rows "
             f"({n_not_preferred / len(combined) * 100:.1f}%)")
    log.info(f"  dc_flag set:        {n_flagged:,} rows")
    log.info(f"  EUR in preferred rows: {eur_preferred:,.0f}")

    # ------------------------------------------------------------------
    # Phase 3: Match quality assessment
    # ------------------------------------------------------------------
    log.info("\nPhase 3: Match quality assessment...")
    combined = assess_match_quality(combined, prefix=prefix)

    # ------------------------------------------------------------------
    # Phase 4: GGE calculation + face/gge column split
    # ------------------------------------------------------------------
    # We publish two parallel headline columns:
    #   amount_eur_face  — raw EUR face value from the source. Always
    #                      populated; this is the headline number the
    #                      paper reports.
    #   amount_eur_gge   — face value multiplied by the instrument's
    #                      GGE rate (grant 1.0, loan 0.15, guarantee
    #                      0.10, ...). NaN for rows whose instrument
    #                      class is not in GGE_RATES — under the §6.5
    #                      no-invention principle we do not fabricate
    #                      a GGE for an unclassified instrument.
    # ``gge_rate_source`` is 'measured' / 'measured_repayable' /
    # 'unknown' so downstream users can filter.
    # ``amount_eur`` stays populated as an alias of ``amount_eur_face``
    # for backwards compatibility with existing analysis code.
    # ------------------------------------------------------------------
    log.info("\nPhase 4: Computing GGE + publishing face/GGE columns...")
    _GGE_UNKNOWN_COUNTS.clear()
    _rate_source_pairs = combined.apply(_gge_rate_and_source, axis=1, result_type='expand')
    combined['gge_rate'] = _rate_source_pairs[0]
    combined['gge_rate_source'] = _rate_source_pairs[1]
    combined['amount_eur_face'] = combined['amount_eur']
    combined['amount_eur_gge'] = combined['amount_eur_face'] * combined['gge_rate']
    # Keep ``amount_gge`` populated for legacy consumers (same values).
    combined['amount_gge'] = combined['amount_eur_gge']
    total_face = combined['amount_eur_face'].sum()
    total_gge = combined['amount_eur_gge'].sum()
    n_measured = int((combined['gge_rate_source'] == 'measured').sum())
    n_repayable = int((combined['gge_rate_source'] == 'measured_repayable').sum())
    n_unknown = int((combined['gge_rate_source'] == 'unknown').sum())
    log.info(f"  Face value: EUR {total_face/1e9:.1f}B (all rows)")
    log.info(
        f"  GGE value:  EUR {total_gge/1e9:.1f}B "
        f"(measured={n_measured:,}, repayable={n_repayable:,}, unknown={n_unknown:,})"
    )
    if _GGE_UNKNOWN_COUNTS:
        total_unknown = sum(_GGE_UNKNOWN_COUNTS.values())
        log.warning(
            f"  GGE: {total_unknown:,} rows have unknown/missing "
            f"financial_instrument_class; amount_eur_gge is NaN for these rows "
            f"(they still appear in face-value totals). Classify these instruments "
            f"in harmonization or add them to GGE_RATES."
        )
        for inst, count in sorted(_GGE_UNKNOWN_COUNTS.items(), key=lambda x: -x[1]):
            eur_mask = combined['financial_instrument_class'].fillna('').str.lower().str.strip().eq(
                inst if inst != '<empty>' else ''
            )
            eur_affected = combined.loc[eur_mask, 'amount_eur_face'].sum()
            log.warning(
                f"    instrument_class={inst!r}: {count:,} rows, EUR {eur_affected/1e6:.0f}M face"
            )

    # ------------------------------------------------------------------
    # Phase 5: Parent group assignment (optional)
    # ------------------------------------------------------------------
    group_summary = None
    if parent_groups is not None:
        log.info("\nPhase 5: Parent group assignment...")
        if isinstance(parent_groups, (str, Path)):
            pg_path = Path(parent_groups)
            if pg_path.exists():
                with open(pg_path) as f:
                    parent_groups = json.load(f)
                log.info(f"  Loaded {len(parent_groups)} groups from {pg_path.name}")
            else:
                log.warning(f"  parent_groups file not found: {pg_path}")
                parent_groups = None

        if parent_groups:
            combined['parent_group'] = combined[ref_col].apply(
                lambda x: assign_parent_group(x, parent_groups)
            )
            n_groups = combined['parent_group'].nunique()
            n_entities = combined[ref_col].nunique()
            log.info(f"  {n_entities} entities → {n_groups} groups")

            group_summary = combined.groupby('parent_group').agg(
                n_entities=(ref_col, 'nunique'),
                n_rows=('amount_eur', 'count'),
                total_eur=('amount_eur', 'sum'),
                total_gge=('amount_gge', 'sum'),
                n_countries=('country', 'nunique') if 'country' in combined.columns else ('source', 'nunique'),
                n_sources=('source', 'nunique'),
            ).reset_index().sort_values('total_eur', ascending=False)

            log.info("\n  Top 20 groups:")
            for i, (_, row) in enumerate(group_summary.head(20).iterrows()):
                log.info(f"    {i+1:2d}. {row['parent_group']:35s} EUR {row['total_eur']/1e9:>6.1f}B "
                         f"(GGE {row['total_gge']/1e9:.1f}B, {row['n_entities']:.0f} entities)")
    else:
        log.info("\nPhase 5: No parent groups provided — skipping group rollup")

    # ------------------------------------------------------------------
    # Phase 5b: Infer origin_block from hq_country (if company list has it)
    # ------------------------------------------------------------------
    if company_list_csv and Path(company_list_csv).exists() and 'origin_block' not in combined.columns:
        try:
            cl_peek = pd.read_csv(company_list_csv, nrows=0)
            if 'hq_country' in cl_peek.columns:
                cl = pd.read_csv(company_list_csv)
                name_col = cl.columns[0]
                hq_map = dict(zip(cl[name_col].str.strip(), cl['hq_country'].fillna('')))
                group_col = 'parent_group' if 'parent_group' in combined.columns else ref_col
                combined['origin_block'] = combined[group_col].map(
                    lambda g: _country_to_block(hq_map.get(str(g).strip(), ''))
                )
                combined['hq_country'] = combined[group_col].map(
                    lambda g: str(hq_map.get(str(g).strip(), '')).strip().upper() or 'Unknown'
                )
                combined['origin_desc'] = combined['origin_block']
                n_known = (combined['origin_block'] != 'Unknown').sum()
                log.info(f"\nPhase 5b: origin_block inferred from hq_country "
                         f"({n_known:,} / {len(combined):,} rows mapped)")
        except Exception as e:
            log.warning(f"  hq_country inference skipped: {e}")

    # ------------------------------------------------------------------
    # Phase 5c: Split the consolidated frame into the deduped "headline"
    # view and the full "audit" view BEFORE building summary tables or
    # concentration metrics. This fixes plan audit finding L1 and the
    # §6.5 no-invention principle: headline numbers must correspond to
    # (a) rows the pipeline itself has not flagged as duplicates, and
    # (b) rows that are not tagged as low-confidence matches.
    #
    # The headline frame is built by this filter chain:
    #   1. ``dc_preferred == True`` — excludes every row that any dedup
    #      step (PDF-grounded, heuristic fallback, FTS identical-txn, or
    #      IPCEI-TAM overlap) marked as a duplicate. This is the single
    #      biggest correctness fix in the whole audit.
    #   2. ``match_quality`` not in the suspect set — excludes
    #      ``suspect_description_only``, ``suspect_eib_title``, and any
    #      future ``suspect_contextual_generic`` flag.
    # ------------------------------------------------------------------
    log.info("\nPhase 5c: Splitting headline vs audit views...")
    _dc_mask = combined['dc_preferred'] if 'dc_preferred' in combined.columns else True
    _suspect_values = {'suspect_description_only', 'suspect_eib_title',
                       'suspect_contextual_generic'}
    if 'match_quality' in combined.columns:
        _mq_mask = ~combined['match_quality'].fillna('').isin(_suspect_values)
    else:
        _mq_mask = True
    # Anonymised / bucket sentinel filter. Populated by the master
    # builder via ``apply_anonymised_column`` on every source. Default
    # to False when the column is missing (older master parquets).
    if 'is_anonymised' in combined.columns:
        _anon_mask = ~combined['is_anonymised'].fillna(False).astype(bool)
    else:
        _anon_mask = True
    headline_mask = _dc_mask & _mq_mask & _anon_mask
    combined_audit = combined  # full, unfiltered — preserved for publication
    combined_headline = combined_audit[headline_mask].copy()
    n_excluded_dc = int((~_dc_mask).sum()) if isinstance(_dc_mask, pd.Series) else 0
    n_excluded_mq = int((~_mq_mask).sum()) if isinstance(_mq_mask, pd.Series) else 0
    n_excluded_anon = int((~_anon_mask).sum()) if isinstance(_anon_mask, pd.Series) else 0
    log.info(
        f"  headline: {len(combined_headline):,} rows / "
        f"audit: {len(combined_audit):,} rows "
        f"(dc_preferred=False: {n_excluded_dc:,}, suspect match_quality: "
        f"{n_excluded_mq:,}, anonymised: {n_excluded_anon:,})"
    )
    if len(combined_audit):
        face_full = float(combined_audit['amount_eur'].sum())
        face_headline = float(combined_headline['amount_eur'].sum())
        delta_pct = (face_full - face_headline) / face_full * 100 if face_full else 0
        log.info(
            f"  face value: headline EUR {face_headline/1e9:.1f}B vs "
            f"audit EUR {face_full/1e9:.1f}B ({delta_pct:+.1f}% delta dropped)"
        )

    # ------------------------------------------------------------------
    # Phase 6: Summary tables (headline view only)
    # ------------------------------------------------------------------
    log.info("\nPhase 6: Building summary tables (headline view)...")
    tables = build_summary_tables(combined_headline, group_summary=group_summary, prefix=prefix)

    for name, tbl in tables.items():
        if isinstance(tbl, pd.DataFrame):
            tbl.to_csv(output_dir / f'{name}.csv')
            log.info(f"  {name}.csv ({len(tbl)} rows)")

    # ------------------------------------------------------------------
    # Phase 7: Concentration metrics (headline view only)
    # ------------------------------------------------------------------
    log.info("\nPhase 7: Concentration metrics (headline view)...")
    metrics = {}
    metrics['entity_level'] = build_concentration_metrics(combined_headline, ref_col)
    if group_summary is not None and 'parent_group' in combined_headline.columns:
        metrics['group_level'] = build_concentration_metrics(combined_headline, 'parent_group')

    log.info(f"  Entity: HHI={metrics['entity_level']['hhi']}, "
             f"Top5={metrics['entity_level']['top5_pct']}%, "
             f"Gini={metrics['entity_level']['gini']}")
    if 'group_level' in metrics:
        log.info(f"  Group:  HHI={metrics['group_level']['hhi']}, "
                 f"Top5={metrics['group_level']['top5_pct']}%, "
                 f"Gini={metrics['group_level']['gini']}")

    with open(output_dir / 'concentration_metrics.json', 'w') as f:
        json.dump(metrics, f, indent=2, default=str)

    # ------------------------------------------------------------------
    # Phase 8: Save outputs
    #
    # We now publish TWO CSVs:
    #   consolidated_matches.csv       — headline view; every row
    #                                    contributes to published totals.
    #   consolidated_matches_audit.csv — audit view; every matched row
    #                                    INCLUDING rows flagged as
    #                                    duplicate / suspect / heuristic-
    #                                    only. Readers who want to see
    #                                    what the pipeline excluded and
    #                                    why read this one.
    # The headline CSV is the one the methodology paper cites.
    # ------------------------------------------------------------------
    log.info("\nPhase 8: Saving outputs...")
    combined_headline = _dedup_exact_rows(combined_headline, ref_col=ref_col)
    combined_audit = _dedup_exact_rows(combined_audit, ref_col=ref_col)
    combined_headline.to_csv(output_dir / 'consolidated_matches.csv', index=False)
    combined_audit.to_csv(output_dir / 'consolidated_matches_audit.csv', index=False)
    log.info(f"  consolidated_matches.csv       ({len(combined_headline):,} rows — headline)")
    log.info(f"  consolidated_matches_audit.csv ({len(combined_audit):,} rows — audit)")
    # Keep ``combined`` bound to the headline view for the rest of the
    # function so the end-of-run summary reports headline numbers.
    combined = combined_headline

    if group_summary is not None:
        group_summary.to_csv(output_dir / 'group_summary.csv', index=False)
        log.info(f"  group_summary.csv ({len(group_summary)} groups)")

    # Save enrichment stats
    if enrichment_stats:
        with open(output_dir / 'enrichment_stats.json', 'w') as f:
            json.dump(enrichment_stats, f, indent=2, default=str)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    elapsed = time.time() - t0
    log.info("\n" + "=" * 70)
    log.info("CONSOLIDATION COMPLETE")
    log.info("=" * 70)
    log.info(f"  Total: EUR {combined['amount_eur'].sum()/1e9:.1f}B face value, "
             f"EUR {combined['amount_gge'].sum()/1e9:.1f}B GGE")
    log.info(f"  Entities: {combined[ref_col].nunique()}")
    if 'parent_group' in combined.columns:
        log.info(f"  Groups: {combined['parent_group'].nunique()}")
    log.info(f"  Sources: {combined['source'].nunique()}")
    log.info(f"  Tables: {len(tables)}")
    log.info(f"  Runtime: {elapsed:.1f}s")

    return combined
