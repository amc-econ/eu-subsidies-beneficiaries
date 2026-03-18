"""
harmonization/utils.py
======================
Shared constants and helper functions used by all harmonization modules.

COMMON_COLUMNS defines the canonical schema that every standardize() function
must return.  The ordering is intentional and must not change.

Schema v2 (2026-02-21): Added 11 columns for flow taxonomy, fiscal
classification, flag-based exclusions, entity resolution, and resolution level.
"""

import hashlib
import json
import logging
import re
from pathlib import Path

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# CANONICAL OUTPUT SCHEMA
# ---------------------------------------------------------------------------

COMMON_COLUMNS: list[str] = [
    'source', 'source_record_id', 'granularity', 'beneficiary_name',
    'country', 'amount_eur', 'amount_type', 'year',
    'sector_description', 'nace_2digit', 'description',
    'overlap_flags', 'original_columns',
    # Programme/fund structure layer (all nullable — sources without structure emit None)
    'programme', 'fund', 'programming_period', 'instrument_subtype', 'policy_domain',
    # Audit validation layer (all nullable — for MFF ceiling / EU budget cross-checks)
    'year_paid',                   # year of payment/expenditure (distinct from year=grant/signing)
    'flow_stage',                  # granted|contracted|allocated|signed|planned|expenditure|ongoing|closed|terminated
    'financial_instrument_class',  # normalised: grant|loan|guarantee|equity|tax_advantage|subsidy|procurement|mixed|other
    'management_type',             # direct|indirect|shared (EU Financial Regulation Art. 62-63)
    'legal_basis',                 # regulation/treaty reference (e.g. HORIZON.1.2, Regulation (EU) 2021/241)
    'budget_line_code',            # EU budget nomenclature code (XX XX XX XX format for FTS)
    'budget_execution_type',       # operational|administrative (EU budget execution classification)
    # --- Schema v2: structural robustness columns ---
    'flow_stage_group',            # commitment|disbursement|planning (derived from flow_stage)
    'flow_stage_confidence',       # verified|inferred|missing
    'flow_stage_assumption',       # text explaining inference (nullable)
    'fiscal_source_type',          # eu_budget_direct|eu_budget_shared|eu_borrowing|ifi_balance_sheet|national_budget
    'exclude_reason',              # mega_scheme_artefact|scoreboard_tam_overlap|esif_kohesio_overlap|... (nullable)
    'is_primary_record',           # True|False — replaces hard row exclusions
    'entity_name_raw',             # = beneficiary_name, unchanged
    'entity_name_clean',           # lowercase, no legal suffixes, collapsed whitespace
    'entity_id',                   # sha256(entity_name_clean|country)[:16]
    'entity_type',                 # company|public_body|ngo|university|individual|consortium|unknown
    'resolution_level',            # beneficiary|project|scheme|measure|country|aggregate
]


# ---------------------------------------------------------------------------
# EU-27 MEMBER STATES (ISO-2)
# ---------------------------------------------------------------------------

EU27: set[str] = {
    'AT', 'BE', 'BG', 'HR', 'CY', 'CZ', 'DK', 'EE', 'FI', 'FR',
    'DE', 'GR', 'HU', 'IE', 'IT', 'LV', 'LT', 'LU', 'MT', 'NL',
    'PL', 'PT', 'RO', 'SK', 'SI', 'ES', 'SE',
}


# ---------------------------------------------------------------------------
# COUNTRY NAME -> ISO-2 MAPPING
# ---------------------------------------------------------------------------

COUNTRY_MAP: dict[str, str] = {
    # Full names (title case)
    'Austria': 'AT', 'Belgium': 'BE', 'Bulgaria': 'BG', 'Croatia': 'HR',
    'Cyprus': 'CY', 'Czech Republic': 'CZ', 'Czechia': 'CZ',
    'Denmark': 'DK', 'Estonia': 'EE', 'Finland': 'FI', 'France': 'FR',
    'Germany': 'DE', 'Greece': 'GR', 'Hungary': 'HU', 'Ireland': 'IE',
    'Italy': 'IT', 'Latvia': 'LV', 'Lithuania': 'LT', 'Luxembourg': 'LU',
    'Malta': 'MT', 'Netherlands': 'NL', 'Poland': 'PL', 'Portugal': 'PT',
    'Romania': 'RO', 'Slovakia': 'SK', 'Slovenia': 'SI', 'Spain': 'ES',
    'Sweden': 'SE',
    # Non-EU but relevant
    'United Kingdom': 'GB', 'Norway': 'NO', 'Switzerland': 'CH',
    'Iceland': 'IS', 'Liechtenstein': 'LI', 'Turkey': 'TR',
    'Serbia': 'RS', 'Montenegro': 'ME', 'North Macedonia': 'MK',
    'Albania': 'AL', 'Bosnia and Herzegovina': 'BA',
    'Moldova': 'MD', 'Ukraine': 'UA', 'Georgia': 'GE',
    'Armenia': 'AM', 'Azerbaijan': 'AZ', 'Belarus': 'BY',
    'Russia': 'RU', 'Russian Federation': 'RU',
    'Kosovo': 'XK',
    # EBRD-specific
    'Turkmenistan': 'TM', 'Uzbekistan': 'UZ', 'Tajikistan': 'TJ',
    'Kyrgyz Republic': 'KG', 'Kazakhstan': 'KZ', 'Mongolia': 'MN',
    'Egypt': 'EG', 'Morocco': 'MA', 'Tunisia': 'TN', 'Jordan': 'JO',
    'Lebanon': 'LB', 'West Bank and Gaza': 'PS',
    # EU code variants
    'EL': 'GR',  # Greece uses EL in EU contexts
    'UK': 'GB',
    # The Netherlands variant
    'The Netherlands': 'NL',
    # ISO-2 passthrough (already correct)
    **{c: c for c in EU27},
    'GB': 'GB', 'NO': 'NO', 'CH': 'CH', 'IS': 'IS', 'LI': 'LI',
    'TR': 'TR', 'RS': 'RS', 'ME': 'ME', 'MK': 'MK', 'AL': 'AL',
    'BA': 'BA', 'MD': 'MD', 'UA': 'UA', 'GE': 'GE', 'AM': 'AM',
    'AZ': 'AZ', 'BY': 'BY', 'RU': 'RU', 'XK': 'XK',
    'TM': 'TM', 'UZ': 'UZ', 'TJ': 'TJ', 'KG': 'KG', 'KZ': 'KZ',
    'MN': 'MN', 'EG': 'EG', 'MA': 'MA', 'TN': 'TN', 'JO': 'JO',
    'LB': 'LB', 'PS': 'PS',
}


# ---------------------------------------------------------------------------
# TAM MEGA-SCHEMES TO DROP BEFORE PROCESSING
# These entries represent aggregate reporting artefacts, not real individual
# awards, and would massively inflate totals if retained.
# ---------------------------------------------------------------------------

TAM_MEGA_SCHEMES: list[str] = [
    'SA.38348', 'SA.56863', 'SA.56963',
    'SA.104722', 'SA.103791', 'SA.105001', 'SA.39078',
]

# Backward-compatible alias (used by existing code referencing the old name)
TAM_MEGA_SCHEMES_DROP = TAM_MEGA_SCHEMES


# ---------------------------------------------------------------------------
# CINEA PROGRAMMES EXCLUDED FROM NON-HORIZON COUNT
# HORIZON is already captured in the RESEARCH source (CORDIS).
# ---------------------------------------------------------------------------

CINEA_EXCLUDE_PROGRAMMES: set[str] = {'HORIZON'}


# ---------------------------------------------------------------------------
# NORMALISED FINANCIAL INSTRUMENT TAXONOMY
# Maps raw AID_INSTRUMENT (TAM), Funding type (FTS), aid_instrument (Scoreboard)
# values into 8 canonical classes. All keys are lowercase.
# ---------------------------------------------------------------------------

INSTRUMENT_CLASS_MAP: dict[str, str] = {
    # GRANT family
    'direct grant':              'grant',
    'direct grant/ interest rate subsidy': 'grant',
    'reimbursable grant':        'grant',
    'grant':                     'grant',
    'endorsed grant':            'grant',
    'budget support':            'grant',
    'recovery and resilience facility (non-repayable financial support)': 'grant',
    'prize':                     'grant',
    # LOAN family
    'soft loan':                 'loan',
    'loan/ repayable advances':  'loan',
    'repayable advances':        'loan',
    # GUARANTEE family
    'guarantee':                 'guarantee',
    'guarantee (where appropriate with a reference to the commission decision (10))': 'guarantee',
    'guarantee (where appropriate with a reference to the commission decision (9))':  'guarantee',
    # EQUITY family
    'provision of risk capital':           'equity',
    'provision of risk finance':           'equity',
    'other forms of equity intervention':  'equity',
    'equity instruments':                  'equity',
    'hybrid capital instruments (convertible bonds)': 'equity',
    'subordinated debt':                   'equity',
    'recapitalisation':                    'equity',
    'shareholdings or equity participations in international financial institutions': 'equity',
    # TAX family
    'tax advantage or tax exemption':       'tax_advantage',
    'other forms of tax advantage':         'tax_advantage',
    'tax allowance':                        'tax_advantage',
    'tax rate reduction':                   'tax_advantage',
    'tax base reduction':                   'tax_advantage',
    'tax deferment':                        'tax_advantage',
    'fiscal measure':                       'tax_advantage',
    'reduction of social security contributions': 'tax_advantage',
    # SUBSIDY family
    'subsidised services':       'subsidy',
    'interest subsidy':          'subsidy',
    'contribution agreement':    'subsidy',
    'contribution to traditional agency and public-private partnerships (ppp)': 'subsidy',
    'contribution to executive agency': 'subsidy',
    # PROCUREMENT family
    'procurement contract':      'procurement',
    'r&d experts and external experts': 'procurement',
    'endorsed procurement':      'procurement',
    'programme estimates':       'procurement',
    'administrative expenditure except procurement and european schools': 'procurement',
    'european schools':          'procurement',
    'delegation agreement':      'procurement',
    # DEBT RELIEF
    'debt write-off':            'debt_relief',
    # CATCH-ALL
    'financial instruments':     'mixed',
    'other':                     'other',
    'others':                    'other',
    'trust funds':               'other',
    'membership fees':           'other',
    # --- ROMANIAN INSTRUMENTS (TAM supplement: RO) ---
    'fonduri nerambursabile':                          'grant',
    'garanții subvenționate':                          'guarantee',
    'garantii subventionate':                          'guarantee',  # ASCII fallback
    'dobânzi subvenționate':                           'subsidy',
    'dobanzi subventionate':                           'subsidy',    # ASCII fallback
    'credite cu dobânzi subvenționate':                'loan',
    'alocatii bugetare':                               'grant',
    'bonusuri și subvenții':                           'subsidy',
    'creditul bugetar':                                'grant',
    'scutiri/exceptări de la plata unor taxe/impozite': 'tax_advantage',
    'scutiri/exceptari de la plata unor taxe/impozite': 'tax_advantage',  # ASCII
    'capital de risc':                                  'equity',
    'compensări':                                       'other',
    'compensari':                                       'other',     # ASCII fallback
    # --- SLOVENIAN INSTRUMENTS (TAM supplement: SI) ---
    'dotacije':                                         'grant',
    'davčne oprostitve, izjeme in olajšave':           'tax_advantage',
    'davcne oprostitve, izjeme in olajsave':           'tax_advantage',  # ASCII
    'ugodna posojila':                                  'loan',
    'znižanje prispevkov za socialno varnost':          'tax_advantage',
    'znizanje prispevkov za socialno varnost':          'tax_advantage',  # ASCII
    'kapitalske naložbe':                               'equity',
    'kapitalske nalozbe':                               'equity',   # ASCII
    'garancije':                                        'guarantee',
    'subvencioniranje obrestne mere':                   'subsidy',
    # --- SPANISH INSTRUMENTS (TAM supplement: ES / BDNS) ---
    'subvención':                                       'grant',
    'subvencion':                                       'grant',    # ASCII
    'subvenciones':                                     'grant',
    'préstamo':                                         'loan',
    'prestamo':                                         'loan',     # ASCII
    'préstamos':                                        'loan',
    'prestamos':                                        'loan',
    'garantía':                                         'guarantee',
    'garantia':                                         'guarantee',
    'bonificación de intereses':                        'subsidy',
    'bonificacion de intereses':                        'subsidy',
    'beneficio fiscal':                                 'tax_advantage',
    'aportación de capital':                            'equity',
    'aportacion de capital':                            'equity',
    # --- POLISH INSTRUMENTS (TAM supplement: PL / SUDOP) ---
    'dotacje':                                          'grant',
    'subwencje':                                        'grant',
    'pożyczki preferencyjne':                           'loan',
    'pozyczki preferencyjne':                           'loan',     # ASCII
    'gwarancje':                                        'guarantee',
    'poręczenia':                                       'guarantee',
    'poreczenia':                                       'guarantee',
    'ulgi podatkowe':                                   'tax_advantage',
    'zwolnienia podatkowe':                             'tax_advantage',
    'udziały kapitałowe':                               'equity',
    'udzialy kapitalowe':                               'equity',
}


def classify_instrument(raw_value: object) -> str | None:
    """Map raw instrument string to canonical class. Case-insensitive."""
    if pd.isna(raw_value) or str(raw_value).strip() == '':
        return None
    return INSTRUMENT_CLASS_MAP.get(str(raw_value).strip().lower(), 'other')


# ---------------------------------------------------------------------------
# HELPER FUNCTIONS (moved verbatim from analysis.py lines 138-199)
# ---------------------------------------------------------------------------

def standardize_country(value: object) -> str:
    """Map country name/code to ISO-2. Returns original string if no mapping found."""
    if pd.isna(value):
        return ''
    s = str(value).strip()
    # Try exact match first
    if s in COUNTRY_MAP:
        return COUNTRY_MAP[s]
    # Try title case
    if s.title() in COUNTRY_MAP:
        return COUNTRY_MAP[s.title()]
    # Try upper
    if s.upper() in COUNTRY_MAP:
        return COUNTRY_MAP[s.upper()]
    return s  # Return original if no mapping


def safe_to_numeric(
    series: pd.Series,
    log: logging.Logger | None = None,
    col_name: str = '',
) -> pd.Series:
    """Robustly convert a series to numeric, handling commas/spaces/currency symbols."""
    if series.dtype in (np.float64, np.int64, float, int):
        return series
    cleaned = series.astype(str).str.replace('\u20ac', '', regex=False)  # euro sign
    cleaned = cleaned.str.replace('$', '', regex=False)
    cleaned = cleaned.str.replace(',', '', regex=False)
    cleaned = cleaned.str.replace(' ', '', regex=False)
    cleaned = cleaned.str.strip()
    result = pd.to_numeric(cleaned, errors='coerce')
    n_failed = result.isna().sum() - series.isna().sum()
    if n_failed > 0 and log:
        log.warning(f"  {col_name}: {n_failed:,} values failed numeric conversion")
    return result


def extract_year(date_series: pd.Series) -> pd.Series:
    """Extract year from a date series (handles datetime, string dates, and plain years)."""
    if pd.api.types.is_datetime64_any_dtype(date_series):
        return date_series.dt.year
    # Check if already numeric years (BEFORE trying datetime parsing,
    # since pd.to_datetime converts integers like 2007 to epoch nanoseconds)
    numeric = pd.to_numeric(date_series, errors='coerce')
    if ((numeric >= 1990) & (numeric <= 2030)).sum() > len(date_series) * 0.3:
        return numeric.astype('Int64')
    # Try parsing as datetime strings
    parsed = pd.to_datetime(date_series, errors='coerce', dayfirst=True)
    return parsed.dt.year


def pack_originals(row_dict: dict) -> str:
    """Pack a dict of original columns into a JSON string."""
    clean: dict = {}
    for k, v in row_dict.items():
        if pd.isna(v):
            continue
        if isinstance(v, (np.integer, np.int64)):
            clean[k] = int(v)
        elif isinstance(v, (np.floating, np.float64)):
            clean[k] = float(v)
        elif isinstance(v, pd.Timestamp):
            clean[k] = v.isoformat()
        else:
            clean[k] = str(v)
    return json.dumps(clean, ensure_ascii=False)


# ---------------------------------------------------------------------------
# SCHEMA v2: CONTROLLED VOCABULARIES
# ---------------------------------------------------------------------------

VALID_FLOW_STAGES: set[str] = {
    'granted', 'contracted', 'allocated', 'signed',
    'planned', 'expenditure', 'ongoing', 'closed', 'terminated',
}

FLOW_STAGE_GROUP_MAP: dict[str, str] = {
    'granted':     'commitment',
    'contracted':  'commitment',
    'allocated':   'commitment',
    'signed':      'commitment',
    'planned':     'planning',
    'expenditure': 'disbursement',
    'ongoing':     'commitment',   # project lifecycle status — funds are committed
    'closed':      'commitment',   # project completed — funds were committed
    'terminated':  'commitment',   # project stopped early — funds were committed
}

# Map raw CORDIS 'status' values to canonical flow_stage
RESEARCH_STATUS_MAP: dict[str, str] = {
    'signed':     'signed',
    'active':     'ongoing',
    'closed':     'closed',
    'terminated': 'terminated',
}

# Map raw CINEA 'Project status' values to canonical flow_stage
CINEA_STATUS_MAP: dict[str, str] = {
    'ongoing':    'ongoing',
    'closed':     'closed',
    'terminated': 'terminated',
    'signed':     'signed',
}

VALID_FISCAL_SOURCE_TYPES: set[str] = {
    'eu_budget_direct', 'eu_budget_shared', 'eu_borrowing',
    'ifi_balance_sheet', 'national_budget',
}

VALID_EXCLUDE_REASONS: set[str] = {
    'mega_scheme_artefact', 'scoreboard_tam_overlap',
    'esif_kohesio_overlap', 'research_programme_overlap',
    'non_eu', 'covid',
}

VALID_ENTITY_TYPES: set[str] = {
    'company', 'public_body', 'ngo', 'university',
    'individual', 'consortium', 'unknown',
}

VALID_RESOLUTION_LEVELS: set[str] = {
    'beneficiary', 'project', 'scheme', 'measure',
    'country', 'aggregate',
}


# ---------------------------------------------------------------------------
# SCHEMA v2: HELPER FUNCTIONS
# ---------------------------------------------------------------------------

def derive_flow_stage_group(flow_stage: object) -> str | None:
    """Map flow_stage to its conceptual group."""
    if pd.isna(flow_stage) or str(flow_stage).strip() == '':
        return None
    return FLOW_STAGE_GROUP_MAP.get(str(flow_stage).strip().lower())


def classify_fiscal_source(source: str, management_type: object = None) -> str:
    """Classify fiscal origin per-record.

    Most sources map 1:1 to a fiscal type.  FTS depends on management_type.
    """
    s = source.upper()
    if s in ('TAM', 'SCOREBOARD'):
        return 'national_budget'
    if s == 'FTS':
        mt = str(management_type).strip().lower() if pd.notna(management_type) else ''
        if 'shared' in mt:
            return 'eu_budget_shared'
        return 'eu_budget_direct'
    if s in ('KOHESIO', 'ESIF_2014', 'ESIF_2027'):
        return 'eu_budget_shared'
    if s in ('EIB', 'EBRD'):
        return 'ifi_balance_sheet'
    if s == 'RRF':
        return 'eu_borrowing'
    # RESEARCH, CINEA, INNOVFUND
    return 'eu_budget_direct'


# Legal suffixes to strip from entity names (longest first)
_LEGAL_SUFFIXES = [
    r'gesellschaft\s+mit\s+beschr[aä]nkter\s+haftung',
    r'societe\s+anonyme', r'societ[aà]\s+per\s+azioni',
    r'limited\s+liability\s+company',
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
    r'aps', r'a/s',
    r'srl', r'spa', r'nv', r'bv',
    # Space-separated variants
    r's\sp\sa', r's\sr\sl', r's\sr\so', r'b\sv', r'n\sv',
]
_LEGAL_SUFFIX_PATTERN = re.compile(
    r'\b(?:' + '|'.join(_LEGAL_SUFFIXES) + r')\b\.?',
    re.IGNORECASE,
)


def clean_entity_name(name: object) -> str:
    """Normalize an entity name: lowercase, strip legal suffixes, punctuation, whitespace.

    Returns empty string for NaN/blank inputs.
    """
    if pd.isna(name):
        return ''
    s = str(name).strip().lower()
    if not s:
        return ''
    # Strip legal suffixes
    s = _LEGAL_SUFFIX_PATTERN.sub(' ', s)
    # Remove punctuation (keep letters, digits, spaces)
    s = re.sub(r'[^a-z0-9\s]', ' ', s)
    # Collapse whitespace
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def generate_entity_id(clean_name: str, country: object) -> str | None:
    """Generate a deterministic entity ID from cleaned name + country.

    Returns None if clean_name is empty.
    """
    if not clean_name:
        return None
    c = str(country) if pd.notna(country) else ''
    return hashlib.sha256(f'{clean_name}|{c}'.encode('utf-8')).hexdigest()[:16]


# Entity type classification rules (order matters: first match wins)
# ---------------------------------------------------------------------------
# Priority order: university > research_org (→university) > public_body >
#   ngo > consortium > individual > company > unknown
#
# "company" is detected via legal suffixes — the broadest net, placed last
# before the unknown fallback.  The _LEGAL_SUFFIXES list (used for name
# cleaning) is reused: if a name contains a recognised legal suffix, the
# entity is almost certainly a company.
#
# Changelog 2026-02-27: Expanded from 4 rules (university, public_body,
# ngo, consortium) to 7 rules (+individual, +company via legal suffix
# reuse, +expanded multilingual patterns for all types).
# ---------------------------------------------------------------------------

_ENTITY_TYPE_RULES: list[tuple[re.Pattern, str]] = [
    # --- 1. UNIVERSITY / RESEARCH (→ 'university') ---
    (re.compile(
        r'\b(?:'
        # Core university terms (all EU languages)
        r'universit\w*|universidad|universiteit|univerzit|'
        r'hochschule|fachhochschule|politecnico|politechnika|'
        r'akademi[ea]|acad[eé]m|'
        r'college(?!\s+(?:street|road|park|hill))|'  # avoid street names
        # Research institutes (mapped to university since no separate type)
        r'research\s+(?:centre|center|institute|institut)|'
        r'institut\b|istituto|instytut|instituut|instituto|'
        r'forsknings|'
        r'fraunhofer|leibniz|helmholtz|max[\s-]planck|'
        r'cnrs|inria|inserm|cea\b|infn\b|'
        r'technische\s+universit|'
        r'ecole\s+(?:normale|polytechnique|nationale|centrale)|'
        r'grandes?\s+[eé]coles?'
        r')\b', re.I), 'university'),

    # --- 2. PUBLIC BODY ---
    (re.compile(
        r'\b(?:'
        # Government / ministries
        r'ministry|ministerio|minist[eè]re|ministerium|ministerstvo|'
        r'government|gouvernement|gobierno|governo|regierung|'
        # Sub-national government
        r'kommun(?:e|al)?|city\s+of|ville\s+de|citt[aà]\s+di|ciudad\s+de|'
        r'region\b|r[eé]gion\b|departement|d[eé]partement|'
        r'gemeinde|landkreis|bezirk|kreis\b|'
        r'ayuntamiento|diputaci[oó]n|provincia\b|'
        r'prefeitura|prefecture|pr[eé]fecture|'
        r'municipality|municipalit[eé]|municipio|obec\b|'
        r'mairie|conseil\s+(?:g[eé]n[eé]ral|r[eé]gional|d[eé]partemental)|'
        r'voivodeship|wojew[oó]dztw|'
        # Agencies and authorities
        r'agency|agence|agenzia|agentur|agentschap|'
        r'authority|autorit[eéaà]|beh[oö]rde|'
        r'public\s+body|amt\s+|office\s+(?:national|f[eé]d[eé]ral)|'
        r'(?:bundes|landes)(?:amt|anstalt|ministerium)|'
        # Courts, parliament, embassies
        r'tribunal|court\s+of|parlement|parliament|parlamento|'
        r'embassy|ambassade|botschaft|'
        # Hospitals and healthcare (public institutions)
        r'hospital|h[oô]pital|krankenhaus|klinik(?:um)?|'
        r'ospedale|szpit|nemocnic|bolnic|'
        r'centre\s+hospitalier|'
        # Schools (pre-university)
        r'school\b|schule\b|[eé]cole(?!\s+(?:normale|polytechnique|nationale|centrale))|'
        r'colegio|scuola|szko[lł]|'
        r'gymnasium\b|lyc[eé]e|liceo|'
        # Museums, libraries, cultural institutions
        r'museum|mus[eé]e|museo|muzeum|'
        r'library|biblioth[eè]que|biblioteca|bibliothek|'
        r'theatre|th[eé][aâ]tre|teatro|'
        # State enterprises / statutory bodies
        r'state\s+(?:enterprise|agency|forest|railway)|'
        r'statny\s+podnik|'  # Slovak/Czech state enterprise
        r'narodni\s+podnik|'
        r'public\s+(?:service|enterprise|utility|institution)|'
        # EU institutions
        r'european\s+commission|european\s+parliament|'
        r'europ[eä]ische\s+kommission'
        r')\b', re.I), 'public_body'),

    # --- 3. NGO / CIVIL SOCIETY ---
    (re.compile(
        r'\b(?:'
        r'foundation|fondation|stiftung|fondazione|fundaci[oó]n|fundacja|'
        r'ngo\b|ong\b|'
        r'association|associazione|asociaci[oó]n|vereniging|verband|'
        r'verein\b|'
        r'charity|charit[eé]|'
        r'red\s+cross|croix[\s-]rouge|rotes\s+kreuz|'
        r'not[\s-]?for[\s-]?profit|non[\s-]?profit|'
        r'gemeinn[uü]tzig|'  # German non-profit marker
        r'ggmbh\b|gemeinn[uü]e?tzig\w*\s+gmbh|'  # gemeinnützige GmbH (incl. ASCII ue→ü)
        r'caritas|diakonie|'
        r'welfare|wohlfahrt|bienestar|'
        r'humanitarian|humanit[aä]r|'
        r'f[oö]rderverein|hilfswerk'
        r')\b', re.I), 'ngo'),

    # --- 4. CONSORTIUM ---
    (re.compile(
        r'\b(?:consortium|consorzio|konsortium|konsorcjum|groupement|'
        r'agrupamento|sdru[zž]en[ií]'
        r')\b', re.I), 'consortium'),

    # --- 5. INDIVIDUAL (person-name heuristics) ---
    # Title prefixes that strongly indicate a natural person
    (re.compile(
        r'(?:^|\b)(?:'
        r'(?:Ing|Dr|Prof|Mag|Mgr|Bc|Dipl|Herr|Frau|Mr|Mrs|Ms|Dott)\b\.?'
        r')', re.I), 'individual'),
]

# Czech/Slovak state enterprise: ",\s*s.\s*p.\s*" at end of string (separate from
# main public_body regex because $ anchors conflict with \b word-boundary wrapper)
_STATE_ENTERPRISE_PATTERN = re.compile(r',\s*s\.?\s*p\.?\s*\.?\s*$', re.I)

# --- 6. COMPANY detection via legal suffix ---
# Reuses the _LEGAL_SUFFIXES list already defined for name cleaning.
# Additional EU-specific patterns that aren't in the cleaning list
# (e.g. Baltic, Czech/Slovak, Hungarian, Portuguese, Spanish forms).
_COMPANY_LEGAL_PATTERN = re.compile(
    r'\b(?:'
    # Already in _LEGAL_SUFFIXES (core):
    + '|'.join(_LEGAL_SUFFIXES) +
    r'|'
    # Additional EU legal forms not in _LEGAL_SUFFIXES
    r'sarl|eurl|snc\b|sci\b|'                     # French
    r'lda\b|limitada|sociedade|'                    # Portuguese
    r's\.?\s*l\.?\b|sociedad\b|'                    # Spanish (S.L.)
    r's\.?\s*n\.?\s*c\.?|'                          # Italian partnership
    r'spol\.?\s*s\.?\s*r\.?\s*o\.?|'               # Czech/Slovak (spol. s r.o.)
    r'uab\b|'                                       # Lithuania
    r'sia\b|'                                       # Latvia
    r'o[uü]\b|'                                     # Estonia (OÜ)
    r'korl[aá]tolt\s+felel[oő]ss[eé]g|t[aá]rsas[aá]g|'  # Hungarian (Kft/Zrt long)
    r'handelsbolag|kommanditbolag|'                 # Swedish partnership forms
    r'kommanditgesellschaft|'                       # German KG (long form)
    r'offene\s+handelsgesellschaft|'                # German OHG
    r'cooperati[ev][ea]?|coop[eé]rative?|genossenschaft|' # Cooperatives
    r'werkst[aä]tt|fabrik\b|'                       # German industrial suffixes
    r'teoranta|'                                    # Irish (Teo)
    r'holding|group\b|'                             # Corporate group markers
    r'enterprises?|'                                # Enterprise suffix
    r'company\b'                                    # Explicit "company"
    r')\b\.?',
    re.IGNORECASE,
)


def classify_entity_type(name: object) -> str:
    """Rule-based entity type classification from name patterns.

    Returns 'unknown' for NaN or when no rule matches.

    Classification order (first match wins):
      1. university (includes research institutes)
      2. public_body (government, agencies, hospitals, schools, museums)
      3. ngo (foundations, associations, non-profits)
      4. consortium
      5. individual (title prefixes: Ing., Dr., Prof., etc.)
      6. company (legal suffixes: GmbH, S.A., Ltd, S.p.A., etc.)
      7. unknown (fallback)

    Changelog 2026-02-27: Added company detection via legal suffix matching,
    individual detection via title prefixes, expanded multilingual patterns
    for university/public_body/ngo.  Reduced unknown rate from ~96% to
    estimated ~40-50%.
    """
    if pd.isna(name):
        return 'unknown'
    s = str(name)
    # Check ordered rules (university → public_body → ngo → consortium → individual)
    for pattern, etype in _ENTITY_TYPE_RULES:
        if pattern.search(s):
            return etype
    # Czech/Slovak state enterprise: "Lesy CR, s.p." (separate anchor-based check)
    if _STATE_ENTERPRISE_PATTERN.search(s):
        return 'public_body'
    # Company detection (broadest net — any legal suffix match)
    if _COMPANY_LEGAL_PATTERN.search(s):
        return 'company'
    return 'unknown'


def apply_entity_columns(out: pd.DataFrame) -> None:
    """Populate entity_name_raw, entity_name_clean, entity_id, entity_type
    from beneficiary_name and country columns in-place."""
    out['entity_name_raw'] = out['beneficiary_name']
    out['entity_name_clean'] = out['beneficiary_name'].apply(clean_entity_name)
    out['entity_id'] = [
        generate_entity_id(n, c)
        for n, c in zip(out['entity_name_clean'], out['country'])
    ]
    out['entity_type'] = out['beneficiary_name'].apply(classify_entity_type)


def apply_v2_columns(
    out: pd.DataFrame,
    *,
    fiscal_source_type: str | None = None,
    resolution_level: str,
    exclude_reason: str | None = None,
    is_primary: bool = True,
) -> None:
    """Assign all schema-v2 columns in one call.

    Derives flow_stage_group from existing flow_stage.
    Handles entity columns from beneficiary_name + country.
    """
    # Task 1: flow taxonomy
    out['flow_stage_group'] = out['flow_stage'].apply(derive_flow_stage_group)
    # flow_stage_confidence and flow_stage_assumption set by caller before this

    # Task 2: fiscal origin
    if fiscal_source_type is not None:
        out['fiscal_source_type'] = fiscal_source_type
    # If not set, caller must have set it already (e.g. FTS per-record)

    # Task 3: exclusion flags
    if out.get('exclude_reason') is None or 'exclude_reason' not in out.columns:
        out['exclude_reason'] = exclude_reason
    if 'is_primary_record' not in out.columns:
        out['is_primary_record'] = is_primary

    # Task 4: entity resolution
    apply_entity_columns(out)

    # Task 5: resolution level
    out['resolution_level'] = resolution_level


def validate_schema(df: pd.DataFrame, source_name: str, log: logging.Logger) -> int:
    """Validate schema v2 controlled vocabularies. Logs warnings, returns warning count."""
    warnings = 0

    # Check all COMMON_COLUMNS present
    missing = set(COMMON_COLUMNS) - set(df.columns)
    if missing:
        log.warning(f"  [{source_name}] Missing columns: {missing}")
        warnings += 1

    # Validate flow_stage
    if 'flow_stage' in df.columns:
        invalid = df['flow_stage'].dropna().unique()
        bad = [v for v in invalid if v not in VALID_FLOW_STAGES]
        if bad:
            log.warning(f"  [{source_name}] Invalid flow_stage values: {bad[:5]}")
            warnings += 1

    # Validate fiscal_source_type
    if 'fiscal_source_type' in df.columns:
        invalid = df['fiscal_source_type'].dropna().unique()
        bad = [v for v in invalid if v not in VALID_FISCAL_SOURCE_TYPES]
        if bad:
            log.warning(f"  [{source_name}] Invalid fiscal_source_type: {bad[:5]}")
            warnings += 1

    # Validate resolution_level
    if 'resolution_level' in df.columns:
        invalid = df['resolution_level'].dropna().unique()
        bad = [v for v in invalid if v not in VALID_RESOLUTION_LEVELS]
        if bad:
            log.warning(f"  [{source_name}] Invalid resolution_level: {bad[:5]}")
            warnings += 1

    # Validate entity_type
    if 'entity_type' in df.columns:
        invalid = df['entity_type'].dropna().unique()
        bad = [v for v in invalid if v not in VALID_ENTITY_TYPES]
        if bad:
            log.warning(f"  [{source_name}] Invalid entity_type: {bad[:5]}")
            warnings += 1

    if warnings == 0:
        log.info(f"  [{source_name}] Schema validation: OK")
    return warnings
