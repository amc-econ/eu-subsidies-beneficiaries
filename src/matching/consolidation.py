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
from pathlib import Path

log = logging.getLogger(__name__)


# ============================================================================
# GGE (Gross Grant Equivalent) — EU Scoreboard rates
# ============================================================================

GGE_RATES = {
    'grant': 1.00, 'subsidy': 1.00, 'procurement': 1.00,
    'equity': 1.00, 'debt_relief': 1.00, 'other': 1.00,
    'mixed': 0.50, 'loan': 0.15, 'guarantee': 0.10, 'tax_advantage': 0.15,
}
REPAYABLE_ADVANCE_RATE = 0.90


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


def _gge_rate(row):
    """Compute GGE rate for a single row based on financial instrument."""
    inst = str(row.get('financial_instrument_class', '')).lower().strip()
    subtype = str(row.get('instrument_subtype', '')).lower()
    if inst == 'loan' and 'repayable' in subtype:
        return REPAYABLE_ADVANCE_RATE
    return GGE_RATES.get(inst, 1.0)


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

def assess_match_quality(df, prefix='match'):
    """Flag likely false positives from description-only matching.

    For KOHESIO/FTS/CINEA rows matched on 'description' (not entity_name),
    checks if the reference name appears in beneficiary_name. If not, flags
    as suspect (e.g., "Samsung tablet" matching Samsung SDI).

    EIB description matches are NOT flagged because EIB beneficiary_name
    is the project title, not the company name.
    """
    log.info("Assessing match quality...")
    ref_col = f'{prefix}_reference_name'
    matched_on_col = f'{prefix}_matched_on'

    if 'match_quality' not in df.columns:
        df['match_quality'] = 'confirmed'

    desc_mask = (
        df.get(matched_on_col, pd.Series(dtype=str)).eq('description') &
        df['source'].isin(['KOHESIO', 'FTS', 'CINEA'])
    )

    if desc_mask.sum() == 0:
        log.info("  No description-matched KOHESIO/FTS rows to check")
        return df

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

    check_rows = df[desc_mask]
    ref_found = check_rows.apply(_ref_in_ben, axis=1)
    suspect_mask = desc_mask & ~df.index.isin(check_rows[ref_found].index)
    df.loc[suspect_mask, 'match_quality'] = 'suspect_description_only'

    n_suspect = suspect_mask.sum()
    eur_suspect = df.loc[suspect_mask, 'amount_eur'].sum()
    log.info(f"  Confirmed: {len(df) - n_suspect:,} rows")
    log.info(f"  Suspect (description-only): {n_suspect:,} rows, EUR {eur_suspect/1e6:.0f}M")

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


def _load_enrichment_csv(path, label):
    """Load an enrichment CSV if it exists."""
    if not path.exists():
        log.info(f"  {label}: not found at {path}")
        return pd.DataFrame()
    df = pd.read_csv(path, low_memory=False)
    eur_col = 'amount_eur' if 'amount_eur' in df.columns else 'total_eur_free' if 'total_eur_free' in df.columns else None
    eur_str = f"EUR {df[eur_col].sum():,.0f}" if eur_col else "(no EUR column)"
    log.info(f"  {label}: {len(df):,} rows, {eur_str}")
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
    fts_cordis = _load_enrichment_csv(fts_cordis_path, 'FTS-CORDIS')
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
    eib = _load_enrichment_csv(eib_path, 'EIB promoter')
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

    # --- IPCEI ---
    ipcei_path = enrichment_dir / 'ipcei_matched_participants.csv'
    if not ipcei_path.exists():
        ipcei_path = enrichment_dir / 'ipcei_automotive_participants.csv'
    ipcei = _load_enrichment_csv(ipcei_path, 'IPCEI')
    if len(ipcei) > 0 and 'amount_eur' in ipcei.columns:
        ipcei_with_amounts = ipcei[ipcei['amount_eur'].notna() & (ipcei['amount_eur'] > 0)]
        if len(ipcei_with_amounts) > 0:
            ipcei_with_amounts = ipcei_with_amounts.copy()
            ipcei_with_amounts['source'] = 'IPCEI_state_aid'
            ipcei_with_amounts['financial_instrument_class'] = 'grant'
            ipcei_with_amounts['fiscal_source_type'] = 'national_budget'

            # Populate matching columns so group assignment works
            if ref_col not in ipcei_with_amounts.columns or ipcei_with_amounts[ref_col].isna().all():
                ipcei_with_amounts[ref_col] = ipcei_with_amounts.get(
                    'matched_company', ipcei_with_amounts.get('company', pd.Series('', index=ipcei_with_amounts.index))
                ).fillna('')
            if type_col not in ipcei_with_amounts.columns:
                ipcei_with_amounts[type_col] = 'ipcei_reference'
            if score_col not in ipcei_with_amounts.columns:
                ipcei_with_amounts[score_col] = 100

            combined = pd.concat(
                [combined, ipcei_with_amounts[combined.columns.intersection(ipcei_with_amounts.columns)]],
                ignore_index=True)
            stats['ipcei'] = {'rows': len(ipcei_with_amounts), 'eur': ipcei_with_amounts['amount_eur'].sum()}

    # --- ETS (reported separately, not added to main total by default) ---
    ets_path = enrichment_dir / 'ets_matched_companies.csv'
    if not ets_path.exists():
        ets_path = enrichment_dir / 'ets_automotive_companies.csv'
    ets = _load_enrichment_csv(ets_path, 'EU ETS')
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
    # Phase 3: Match quality assessment
    # ------------------------------------------------------------------
    log.info("\nPhase 3: Match quality assessment...")
    combined = assess_match_quality(combined, prefix=prefix)

    # ------------------------------------------------------------------
    # Phase 4: GGE calculation
    # ------------------------------------------------------------------
    log.info("\nPhase 4: Computing GGE (Gross Grant Equivalent)...")
    combined['gge_rate'] = combined.apply(_gge_rate, axis=1)
    combined['amount_gge'] = combined['amount_eur'] * combined['gge_rate']
    total_face = combined['amount_eur'].sum()
    total_gge = combined['amount_gge'].sum()
    log.info(f"  Face value: EUR {total_face/1e9:.1f}B")
    log.info(f"  GGE value:  EUR {total_gge/1e9:.1f}B")

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
    # Phase 6: Summary tables
    # ------------------------------------------------------------------
    log.info("\nPhase 6: Building summary tables...")
    tables = build_summary_tables(combined, group_summary=group_summary, prefix=prefix)

    for name, tbl in tables.items():
        if isinstance(tbl, pd.DataFrame):
            tbl.to_csv(output_dir / f'{name}.csv')
            log.info(f"  {name}.csv ({len(tbl)} rows)")

    # ------------------------------------------------------------------
    # Phase 7: Concentration metrics
    # ------------------------------------------------------------------
    log.info("\nPhase 7: Concentration metrics...")
    metrics = {}
    metrics['entity_level'] = build_concentration_metrics(combined, ref_col)
    if group_summary is not None and 'parent_group' in combined.columns:
        metrics['group_level'] = build_concentration_metrics(combined, 'parent_group')

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
    # ------------------------------------------------------------------
    log.info("\nPhase 8: Saving outputs...")
    combined.to_csv(output_dir / 'consolidated_matches.csv', index=False)
    log.info(f"  consolidated_matches.csv ({len(combined):,} rows)")

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
