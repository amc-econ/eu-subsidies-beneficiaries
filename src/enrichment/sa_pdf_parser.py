"""
SA PDF Parser — Tier B co-financing extraction
================================================
Downloads EC state aid decision PDFs and searches for EU structural fund
co-financing mentions. This gives document-grounded evidence that a TAM
state aid row has a corresponding KOHESIO entry — far stronger than the
heuristic amount-ratio check.

The pipeline runs AFTER entity matching and ONLY for SA codes that appear
in matched TAM rows for the current run. For a 150-company list this is
typically 50–200 SA codes, not the full ~60,000-case corpus.

Usage (standalone):
    from src.enrichment.sa_pdf_parser import SACofinParser
    parser = SACofinParser(cache_dir='data/processed/sa_decisions')
    result = parser.parse_sa_code('SA.107094', pdf_links)
    # result = {'sa_code': 'SA.107094', 'cofin_funds': ['ERDF'], ...}

Usage (from consolidation):
    from src.enrichment.sa_pdf_parser import enrich_tam_cofin
    combined = enrich_tam_cofin(combined, sa_lookup, cache_dir=...)
    # Adds columns: sa_cofin_fund, sa_cofin_evidence, sa_pdf_status,
    #               sa_cofin_level, sa_gber_table_funds, sa_cofin_section_found

Dependencies:
    pdfplumber  (preferred — handles column layouts)
    pdfminer.six  (fallback)
    requests
    pymupdf4llm  (optional — upgraded backend; pip install pymupdf4llm)
    langdetect  (optional — for language detection)

Install:
    pip install pdfplumber requests
    pip install pymupdf4llm      # strongly recommended for best results

Design notes:
  - PDFs are cached locally under cache_dir / {SA_CODE}.pdf
  - Extraction backends: pymupdf4llm (preferred) → pdfplumber → pdfminer.six
    pymupdf4llm produces structured markdown: headings, un-broken paragraphs,
    and inline picture text (OCR) for image-based PDFs. This eliminates the
    line-wrapping issue that required \\s+ in all multi-word regex patterns.
  - Non-English PDFs: still attempt regex on original text — EU fund names
    have consistent cross-language abbreviations (ERDF/FEDER/EFRE, ESF/FSE/ESF)
  - Rate limiting: 1 request/sec, 3 retries with exponential back-off
  - Partial failures (download error, parse error) are logged and skipped;
    they do not block the rest of the pipeline run

Detection levels (sa_cofin_level column):
  'confirmed'    — the decision explicitly states the measure IS co-financed
                   by the named EU fund. Actionable for dedup.
  'conditional'  — the decision uses conditional/potential language ("may be",
                   "to the extent", "does not exclude"). Present in many
                   Temporary Framework decisions as a compliance clause even
                   when co-financing is not planned. Low confidence.
  ''             — no EU fund co-financing mention found

PDF format patterns observed across CRM v2 sample (57 English PDFs, 2017–2024):
  1. IPCEI decisions:  dedicated paragraph "Co-financing by a Union fund" with
     a numbered heading in the decision body. E.g.: "Poland and France are
     considering seeking co-financing from the European Regional Development Fund."
  2. RRF-backed measures: explicit budget attribution paragraph. E.g.: "The
     measure will be financed by the RRF", "totally made available through the
     RRF", "co-financed with the Spanish national budget and with RRF funds."
  3. Structural fund schemes (Hungary, Finland COVID): direct declaration.
     E.g.: "The scheme is co-financed by the ERDF, ESF, Cohesion Fund (CF)..."
  4. TCTF/Temp Framework conditional: boilerplate compliance clause saying rules
     will be respected IF co-financing occurs. E.g.: "To the extent the scheme
     is co-financed by the ERDF, ESF..." — NOT confirmed.
  5. State resources test boilerplate: "financed entirely from State resources
     or partly financed by the Union" — standard legal test; NOT co-financing.
  6. GBER notification form table: standardised summary table at the top of
     scheme decisions. Contains a row "If co-financed by Community funds" with
     fund names and EUR amounts. When all amounts are 0, no co-financing is
     planned. When amounts > 0, co-financing is confirmed.
     E.g.: "If co-financed by Community funds  FEDER - 0 EUR  Feader - 0 EUR"
     This can appear as image-embedded text in some PDFs (extracted via OCR
     in pymupdf4llm picture text) or as selectable text (pdfplumber).

False-positive traps to watch for:
  - 'feder' matches 'Federale' (Dutch/French for federal) — use word-boundary
  - 'esf' matches other acronyms — require non-alpha after ESF
  - 'structural funds' appears in legal citations and footnotes
  - RRF appears in footnote citations about the RRF Regulation
  - "Co-financing by the aid beneficiaries" (IPCEI Communication requirement)
    looks like a co-financing section heading but is about BENEFICIARY contribution,
    not EU fund co-financing — must require "Union fund" in heading search
  - "funded by the ERDF Regulation" or "established by the ERDF" — legislative
    citations, not actual co-financing; mitigated by requiring beneficiary-facing
    language (measure/aid/project "is funded by", not "fund is funded by")
"""

import io
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EU fund name patterns — used to identify WHICH fund is mentioned near a
# confirmed co-financing phrase.
#
# Notes on false-positive traps:
#   - 'feder' (ERDF in Romance languages) also matches 'Federale' (Dutch/French
#     for 'federal') and appears in minister address blocks — use word boundary.
#   - 'esf' matches many acronyms — require [^A-Za-z] after ESF.
#   - 'rrf' — very common in footnote citations; rely on COFIN_CONFIRMED to gate.
# ---------------------------------------------------------------------------
EU_FUND_PATTERNS: Dict[str, re.Pattern] = {
    # All multi-word fund names use \s+ to tolerate any line-wrapping artifacts.
    # Single acronyms require word-boundary / non-alpha suffix to avoid partial matches.
    'ERDF': re.compile(
        r'european\s+regional\s+development\s+fund'
        r'|erdf(?=[^a-z])'
        r'|\befre\b'                  # EFRE (German abbreviation)
        r'|\bfeder\b',                # FEDER (French/Spanish) — \b excludes Federale/Federal
        re.IGNORECASE,
    ),
    'ESF': re.compile(
        r'european\s+social\s+fund'
        r'|esf(?:\+|\s+plus)?(?=[^a-z])'
        r'|fse(?=[^a-z])'            # FSE (French/Italian/Spanish)
        r'|fonds\s+social\s+europ[eé]en',
        re.IGNORECASE,
    ),
    'CF': re.compile(
        r'cohesion\s+fund'
        r'|fonds\s+de\s+coh[eé]sion'
        r'|koh[aä]sionsfonds',
        re.IGNORECASE,
    ),
    'JTF': re.compile(
        r'just\s+transition\s+fund'
        r'|jtf(?=[^a-z])'
        r'|fonds\s+pour\s+une\s+transition\s+juste'
        r'|fonds\s+f[uü]r\s+einen\s+gerechten\s+[Üü]bergang',
        re.IGNORECASE,
    ),
    'RRF': re.compile(
        r'recovery\s+and\s+resilience\s+facility'
        r'|rrf(?=[^a-z])'
        r'|facilit[eé]\s+pour\s+la\s+reprise\s+et\s+la\s+r[eé]silience'
        r'|aufbau-?\s+und\s+resilienzfazilität',
        re.IGNORECASE,
    ),
    'ESIF': re.compile(
        r'european\s+structural\s+and\s+investment\s+fund'
        r'|esif(?=[^a-z])'
        r'|structural\s+and\s+investment\s+funds?'
        r'|fonds\s+structurels',
        re.IGNORECASE,
    ),
    'INTERREG': re.compile(r'\binterreg\b', re.IGNORECASE),
    'EAFRD': re.compile(
        r'european\s+agricultural\s+fund\s+for\s+rural\s+development'
        r'|eafrd(?=[^a-z])'
        r'|\bfeader\b',              # FEADER (French acronym)
        re.IGNORECASE,
    ),
    'EAGF': re.compile(
        r'european\s+agricultural\s+guarantee\s+fund'
        r'|eagf(?=[^a-z])'
        r'|\bfeaga\b',               # FEAGA (French acronym)
        re.IGNORECASE,
    ),
    'EMFAF': re.compile(
        r'european\s+maritime,?\s+fisheries\s+and\s+aquaculture\s+fund'
        r'|emfaf(?=[^a-z])'
        r'|european\s+maritime\s+and\s+fisheries\s+fund'  # pre-2021 name
        r'|emff(?=[^a-z])',
        re.IGNORECASE,
    ),
    # Innovation Fund — confirmed to appear in automotive SA PDFs (2 hits / 80 PDFs)
    'INNOVFUND': re.compile(
        r'(?:eu\s+)?innovation\s+fund(?:\s+for)?'
        r'|innovfund(?=[^a-z])'
        r'|innovation\s+fund\s+\(if\)',
        re.IGNORECASE,
    ),
    # Connecting Europe Facility — appears in FTS source; occasional SA co-financing
    'CEF': re.compile(
        r'connecting\s+europe\s+facilit(?:y|é)'
        r'|cef(?=[^a-z])',
        re.IGNORECASE,
    ),
    # LIFE — environment/climate fund; managed by CINEA
    'LIFE': re.compile(
        r'\blife\+\b'
        r'|programme\s+for\s+the\s+environment\s+and\s+climate\s+action'
        r'|life\s+programme(?=[^a-z])',
        re.IGNORECASE,
    ),
    # Horizon Europe / H2020 — research instrument; rare in SA co-financing
    'HORIZON': re.compile(
        r'horizon\s+(?:2020|europe)'
        r'|h2020(?=[^a-z])',
        re.IGNORECASE,
    ),
}

# ---------------------------------------------------------------------------
# CONFIRMED co-financing patterns (Level 1 — actionable for dedup).
#
# These patterns fire only when the decision explicitly states the measure IS
# co-financed by a named EU fund. Patterns derived from manual review of 57
# English-language SA decision PDFs (CRM v2 TAM list, 2017–2024).
#
# Document types covered:
#   A) IPCEI decisions: dedicated "Co-financing by a Union fund" paragraph.
#      E.g.: "will be using co-financing from the European Regional Development Fund"
#   B) RRF-backed measures: budget attribution paragraph.
#      E.g.: "financed by the RRF", "totally made available through the RRF",
#            "co-financed with the Spanish national budget and with RRF funds"
#   C) Structural fund schemes: direct declaration.
#      E.g.: "co-financed by the ERDF, ESF, Cohesion Fund"
#   D) Mixed: "partly co-financed from the ESF" (Poland wage subsidies)
#   E) Direct "funded/financed by [structural fund]" without "co-" prefix
#      E.g.: "The project is funded by the ERDF under the Operational Programme"
#   F) Contribution/grant language
#      E.g.: "grant from the ERDF", "contribution from the Cohesion Fund"
#   G) Operational programme linkage
#      E.g.: "under the ERDF Operational Programme for [region]"
# ---------------------------------------------------------------------------
COFIN_CONFIRMED_RE = re.compile(
    # A. IPCEI section header or exact IPCEI phrasing
    r'Co-financing\s+by\s+(?:a|the)\s+Union\s+fund'
    r'|(?:will\s+be\s+using|seeking|plans?\s+to\s+seek|intends?\s+to\s+seek)\s+'
    r'co.?financ\w*\s+from\s+the\s+(?:european|erdf|esf|cohesion|jtf|rrf|structural)'
    # B. RRF explicit budget attribution (not just footnote citation)
    # "funded/financed by/through the Recovery and Resilience Facility"
    r'|(?:will\s+be\s+|shall\s+be\s+|is\s+)?(?:funded|financed)\s+(?:by|through)\s+the\s+'
    r'(?:recovery\s+and\s+resilience\s+facility|rrf[^a-zA-Z])'
    # "totally made available through the Recovery..."
    r'|totally\s+made\s+available\s+(?:through|by)\s+the\s+'
    r'(?:recovery\s+and\s+resilience|rrf[^a-zA-Z])'
    # C. Direct "co-financed by/from [EU fund]" (present or future tense)
    r'|(?:is|are|shall\s+be|will\s+be)\s+(?:partly\s+|fully\s+|entirely\s+)?'
    r'co.?financ\w+\s+(?:by|from)\s+the\s+'
    r'(?:european|recovery\s+and\s+resilience|erdf|esf[^a-z]|'
    r'cohesion\s+fund|just\s+transition|jtf[^a-z]|rrf[^a-z]|esif[^a-z]|structural)'
    # D. "co-financed with [words...] and with [EU fund]" — Spain RRF pattern
    # Allows up to ~15 words between "with" and the fund name
    r'|co.?financ\w+\s+with(?:\s+\S+){1,15}\s+(?:recovery\s+and\s+resilience|rrf[^a-zA-Z])'
    # E. "partly co-financed from the ESF/ERDF" — Poland pattern
    r'|partly\s+co.?financ\w+\s+from\s+the\s+'
    r'(?:european|erdf|esf[^a-z]|cohesion|jtf|rrf)'
    # F. Plain "funded/financed by the [structural fund]" — no "co-" prefix
    # Targets investment aid under an EU operational programme
    # Deliberately excludes RRF (already covered above with tighter patterns)
    r'|(?:funded|financed|supported)\s+(?:by|through|via|from)\s+the\s+'
    r'(?:erdf[^a-z]|esf[^a-z]|cohesion\s+fund|just\s+transition\s+fund|jtf[^a-z]|'
    r'european\s+regional\s+development|european\s+social\s+fund|'
    r'european\s+agricultural\s+fund|eafrd[^a-z])'
    # G. Grant/contribution explicitly from a structural fund
    r'|(?:grant|contribution|payment|financing)\s+(?:of\s+|from\s+)?the\s+'
    r'(?:erdf[^a-z]|esf[^a-z]|cohesion\s+fund|just\s+transition\s+fund|jtf[^a-z]|'
    r'european\s+regional|european\s+social\s+fund|eafrd[^a-z])',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# CONDITIONAL co-financing patterns (Level 2 — lower confidence).
#
# These fire when the document uses hedging or compliance language: "may be",
# "to the extent", "does not exclude". Common in Temporary Framework COVID-19
# decisions where a compliance clause says IF EU funds are involved THEN these
# rules apply — even when no actual co-financing is planned.
#
# Also catches: "in the event of co-financing by ... ERDF"
# ---------------------------------------------------------------------------
COFIN_CONDITIONAL_RE = re.compile(
    r'to\s+the\s+extent\s+(?:the\s+)?(?:scheme\s+)?(?:is\s+)?co.?financ'
    r'|does\s+not\s+exclude\s+that.{0,150}?(?:esif|erdf|esf|structural\s+fund|cohesion)'
    r'|may\s+be\s+co.?financ\w+\s+by\s+(?:the\s+)?(?:european|esif|erdf|structural)'
    r'|in\s+the\s+event\s+of\s+co.?financ'
    r'|consider(?:ing)?\s+(?:to\s+)?(?:seek(?:ing)?|apply(?:ing)?\s+for)\s+co.?financ'
    r'|intend(?:s|ing)?\s+to\s+(?:partly\s+)?financ\w+\s+(?:through|via|with|from)\s+(?:the\s+)?'
    r'(?:recovery\s+and\s+resilience|rrf[^a-z])',
    re.IGNORECASE | re.DOTALL,
)

# ---------------------------------------------------------------------------
# Boilerplate exclusion — these phrases appear in virtually every SA decision
# as part of the state resources test, cumulation rules, or document headers.
# They do NOT indicate actual EU fund co-financing.
#
# We blank out these spans before running COFIN_CONFIRMED_RE so the embedded
# "is co-financed by ERDF" tail of a conditional sentence doesn't trigger a
# false confirmed detection (e.g. SA.56995 Finland COVID TF compliance clause).
# ---------------------------------------------------------------------------
COFIN_BOILERPLATE_RE = re.compile(
    # Standard state resources test
    r'financ\w+\s+entirely\s+from\s+state\s+resources\s+or\s+partly\s+financ\w+\s+by\s+the\s+union'
    r'|financ\w+\s+through\s+state\s+resources'
    # TF compliance clause: "to the extent the scheme is co-financed by [long fund list]."
    # The list of funds can span 400+ chars. Strip the full sentence.
    r'|to\s+the\s+extent\s+(?:that\s+)?(?:the\s+)?(?:scheme\s+)?(?:is\s+)?co.?financ\w+\s+by[^.]{0,600}\.'
    # "does not exclude that the measure would be financed by ESIF in the future."
    r'|(?:does\s+not\s+exclude\s+that[^.]{0,200}\.)'
    # Generic: "partly financed by the Union/ESIF" without an explicit fund name (boilerplate)
    r'|partly\s+financ\w+\s+by\s+the\s+(?:union|esif[^a-z])'
    # EC document header — first line of every SA decision PDF
    r'|this\s+text\s+is\s+made\s+available\s+for\s+information\s+purposes\s+only'
    # Authenticity disclaimer — appears mid-document in translated decisions (EN + FR)
    r'|only\s+the\s+\w+\s+text\s+is\s+(?:authentic|available\s+and\s+authentic)'
    r'|seul\s+le\s+texte\s+.{0,30}?\s+fait\s+foi'
    # Brussels date headers — pymupdf4llm renders these as ## headings / top-of-page text
    # E.g. "Brussels, 9.12.2019" or "Brussels, 14 June 2023"
    r'|Brussels,?\s+\d{1,2}[\s\./\\]+[\w\s\.]+\d{4}'
    # SG Secretariat reference numbers (Commission internal references)
    r'|SG\s*[\(\[]\s*\d{4}\s*[\)\]]\s*[\dA-Z/]+'
    # Commission C(YEAR)NUMBER decision numbering in page headers
    r'|C\s*\(\s*\d{4}\s*\)\s*\d+\s*(?:final)?'
    # "does not constitute an official publication" footer note
    r'|does\s+not\s+constitute\s+an\s+official\s+publication',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# GBER notification form table detection
#
# GBER (General Block Exemption Regulation) scheme decisions begin with a
# standardised summary table submitted by the Member State. This table contains
# a "If co-financed by Community funds" row listing fund names and EUR amounts.
#
# When amounts are 0 EUR → no co-financing planned (form placeholder only).
# When amounts are > 0 EUR → confirmed co-financing for that fund.
#
# The table may appear:
#   a) As selectable text (pdfplumber extracts it directly)
#   b) As an embedded image (pymupdf4llm OCR extracts it in picture text blocks)
#   In both cases the concatenated text looks like:
#   "If co-financed by Community funds FEDER - 0 EUR Feader - 0 EUR"
# ---------------------------------------------------------------------------
_GBER_TABLE_ANCHOR_RE = re.compile(
    r'if\s+co-?financ\w*\s+by\s+community\s+funds?',
    re.IGNORECASE,
)

# Matches individual fund-amount pairs in the GBER table
# Handles: "FEDER - 0 EUR", "FEDER  -  0 EUR", "FEDER: 0 EUR", "FEDER 0 EUR"
_GBER_FUND_ROW_RE = re.compile(
    r'\b(FEAGA|FEDER|Feader|ERDF|EFRE|FSE|ESF[+\+]?|JTF|CF|RRF|INTERREG|EAFRD|FEADER)\b'
    r'\s*[-–:]\s*([\d\s\.\,]+)\s*EUR',
    re.IGNORECASE,
)

# Maps GBER form acronyms (often in national language) to our canonical fund names
_GBER_FUND_MAP = {
    'FEAGA': 'EAGF',      # European Agricultural Guarantee Fund (FR: FEAGA)
    'FEDER': 'ERDF',      # Fonds Européen de Développement Régional (FR)
    'FEADER': 'EAFRD',    # Fonds Européen Agricole pour le Développement Rural (FR)
    'EFRE': 'ERDF',       # Europäischer Fonds für regionale Entwicklung (DE)
    'FSE': 'ESF',         # Fonds Social Européen (FR) / Fondo Sociale Europeo (IT)
    'ESF+': 'ESF+',
    'ERDF': 'ERDF',
    'ESF': 'ESF',
    'CF': 'CF',
    'JTF': 'JTF',
    'RRF': 'RRF',
    'INTERREG': 'INTERREG',
    'EAFRD': 'EAFRD',
}

# ---------------------------------------------------------------------------
# Section heading detection (for pymupdf4llm markdown output)
#
# When pymupdf4llm renders the PDF as markdown, section headings appear as
# ## Heading text. We look for headings that specifically mention EU funds,
# NOT generic "co-financing" headings (which could be "Co-financing by the
# aid beneficiaries" — a different IPCEI Communication requirement).
# ---------------------------------------------------------------------------
_COFIN_SECTION_HEADING_RE = re.compile(
    r'^#{1,4}\s+(?:.*?)'
    r'(?:union\s+fund'
    r'|european\s+(?:regional|social|structural|agricultural)'
    r'|erdf|esf[^a-z]|cohesion\s+fund|just\s+transition\s+fund'
    r'|jtf[^a-z]|rrf[^a-z]|esif[^a-z]|structural\s+(?:fund|investment)'
    r')',
    re.IGNORECASE | re.MULTILINE,
)

# Extract up to N characters around a match for the evidence snippet
_SNIPPET_RADIUS = 200


# ---------------------------------------------------------------------------
# LLM prompt for Tier 3 co-financing extraction
#
# Sent to Claude Haiku when regex tiers find no signal. The excerpt is a
# smart 4000-char window around co-financing keyword density — not the
# full document — so token cost is bounded (~$0.0003/call at Haiku prices).
# ---------------------------------------------------------------------------
COFIN_LLM_PROMPT = """\
You are analysing an EU state aid decision (case {sa_code}).
Extract EU fund co-financing information from the following excerpt.

Respond with JSON only, no prose:
{{
  "fund_names": ["ERDF", "ESF"],
  "level": "confirmed",
  "evidence": "verbatim 1-2 sentence quote from text"
}}

Rules:
- "level" must be exactly one of: "confirmed", "conditional", or ""
- "confirmed": the decision explicitly states the measure IS co-financed by the named EU fund
- "conditional": hedging language ("may be", "to the extent", "does not exclude", "considering seeking")
- "": no EU fund co-financing mentioned at all
- fund_names: use canonical acronyms — ERDF, ESF, CF, JTF, RRF, EAFRD, EAGF, ESIF, INTERREG
  (FEDER/EFRE = ERDF; FSE = ESF; Feader/FEADER = EAFRD; FEAGA = EAGF)
- Ignore: state resources test boilerplate ("financed entirely from State resources or partly
  financed by the Union"), legislative citations, footnote references to regulations,
  "does not constitute an official publication" footers
- If fund_names is empty, level must be ""

Excerpt from {sa_code}:
{excerpt}"""


class SACofinParser:
    """Download and parse SA decision PDFs to detect EU fund co-financing.

    Parameters
    ----------
    cache_dir : str or Path
        Local directory to cache downloaded PDFs. Files are named {SA_CODE}.pdf.
    request_delay : float
        Seconds to wait between HTTP requests (rate limiting). Default 1.0.
    max_retries : int
        Maximum download retry attempts per PDF. Default 3.
    timeout : int
        HTTP request timeout in seconds. Default 30.
    use_llm : bool
        If True, call Claude Haiku as a Tier 3 fallback when regex finds no signal.
        Requires ANTHROPIC_API_KEY environment variable. Default False.
    """

    def __init__(
        self,
        cache_dir: str | Path = 'data/processed/sa_decisions',
        request_delay: float = 1.0,
        max_retries: int = 3,
        timeout: int = 30,
        use_llm: bool = False,
    ):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_delay = request_delay
        self.max_retries = max_retries
        self.timeout = timeout
        self.use_llm = use_llm
        self._last_request_time: float = 0.0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def parse_sa_code(self, sa_code: str, pdf_links: List[dict]) -> dict:
        """Download and parse PDFs for an SA code; return co-financing result.

        Parameters
        ----------
        sa_code : str
            Normalised SA code (e.g. 'SA.107094').
        pdf_links : list of dict
            Output of SACaseLookup.get_pdf_links(sa_code). Each dict has
            'url', 'lang', 'name', 'sent', 'decision'.

        Returns
        -------
        dict with keys:
            sa_code             : str
            cofin_funds         : list[str]  — e.g. ['ERDF', 'ESF']
            cofin_level         : str        — 'confirmed' | 'conditional' | ''
            cofin_evidence      : str        — text snippet(s) from PDF
            gber_table_funds    : dict       — {fund: amount_eur} from GBER table (>0 only)
            cofin_section_found : bool       — True if dedicated EU fund section located
            pdf_status          : str        — 'parsed' | 'no_pdf' | 'download_failed' |
                                               'parse_failed' | 'non_english_no_match'
            pdf_url             : str        — URL of the PDF that was parsed
            pdf_lang            : str        — language of parsed PDF
            extraction_backend  : str        — 'pymupdf4llm' | 'pdfplumber' | 'pdfminer'
            llm_used            : bool       — True if Tier 3 LLM was invoked
        """
        result = {
            'sa_code': sa_code,
            'cofin_funds': [],
            'cofin_level': '',
            'cofin_evidence': '',
            'gber_table_funds': {},
            'cofin_section_found': False,
            'pdf_status': 'no_pdf',
            'pdf_url': '',
            'pdf_lang': '',
            'extraction_backend': '',
            'llm_used': False,
        }

        if not pdf_links:
            return result

        # Prefer English, prefer full-decision-text attachments (name=WLAL)
        ordered = sorted(pdf_links, key=lambda x: (
            0 if x['lang'] in ('en', 'EN') else 1,
            0 if x.get('name') == 'WLAL' else 1,
        ))

        for link_info in ordered:
            url = link_info['url']
            lang = link_info.get('lang', '')
            result['pdf_url'] = url
            result['pdf_lang'] = lang

            # Try cache first
            pdf_bytes = self._load_cached(sa_code, url)
            if pdf_bytes is None:
                pdf_bytes = self._download(url)
                if pdf_bytes is None:
                    result['pdf_status'] = 'download_failed'
                    continue
                self._save_cache(sa_code, url, pdf_bytes)

            # --- Extraction ---
            # pymupdf4llm (preferred): structured markdown with headings and
            # picture text (OCR from image-embedded tables).
            # pdfplumber/pdfminer: plain text fallback.
            md_text = self._extract_markdown(pdf_bytes)
            if md_text is not None:
                text = md_text
                result['extraction_backend'] = 'pymupdf4llm'
            else:
                text = self._extract_text(pdf_bytes)
                result['extraction_backend'] = 'pdfplumber'

            if text is None:
                result['pdf_status'] = 'parse_failed'
                continue

            # --- Tier 0: GBER notification form table ---
            # Parse the standardised EC form table for "If co-financed by
            # Community funds FUND - AMOUNT EUR" entries. Only counts if > 0 EUR.
            gber_funds = self._detect_gber_table(text)
            result['gber_table_funds'] = gber_funds

            # --- Tier 1+2: prose and section detection ---
            # If pymupdf4llm markdown is available, look for a dedicated EU fund
            # co-financing section heading (e.g. "## Financing from the ERDF").
            # If found, run detection on that section first for higher precision.
            section_text = self._find_cofin_section(md_text) if md_text else None
            result['cofin_section_found'] = section_text is not None

            found_funds, cofin_level, evidence = self._detect_cofin(
                text, section_text=section_text
            )

            # Merge GBER table into prose results
            if gber_funds:
                for fund in gber_funds:
                    if fund not in found_funds:
                        found_funds.append(fund)
                if not cofin_level:
                    # GBER table confirmed, but prose had no signal
                    cofin_level = 'confirmed'
                    gber_str = '; '.join(
                        f'{k}: {v:,.0f} EUR' for k, v in gber_funds.items()
                    )
                    evidence = f'[GBER table] {gber_str}'

            result['pdf_status'] = 'parsed'
            result['cofin_funds'] = found_funds
            result['cofin_level'] = cofin_level
            result['cofin_evidence'] = evidence

            if not found_funds and lang not in ('en', 'EN', ''):
                result['pdf_status'] = 'non_english_no_match'

            # --- Tier 3: LLM fallback ---
            # Triggered when regex found nothing AND use_llm=True.
            # Works for non-English PDFs and footnote-broken sentences that
            # defeat the regex approach.
            if self.use_llm and not result['cofin_funds']:
                excerpt = self._extract_cofin_excerpt(text)
                llm_funds, llm_level, llm_evidence = self._detect_cofin_llm(
                    excerpt, sa_code=sa_code
                )
                result['llm_used'] = True
                if llm_funds:
                    result['cofin_funds'] = llm_funds
                    result['cofin_level'] = llm_level
                    result['cofin_evidence'] = f'[LLM] {llm_evidence}'
                    if result['pdf_status'] == 'non_english_no_match':
                        result['pdf_status'] = 'parsed'

            return result  # stop after first successfully parsed PDF

        return result

    # ------------------------------------------------------------------
    # Batch entry point (called from consolidation)
    # ------------------------------------------------------------------

    def enrich_dataframe(self, df, sa_lookup) -> 'pd.DataFrame':
        """Add sa_cofin_* columns to a consolidated_matches-style dataframe.

        Only processes TAM rows whose SA code is in the lookup.

        Parameters
        ----------
        df : pd.DataFrame
            Must have 'source', 'sa_case' columns.
        sa_lookup : SACaseLookup
            Loaded SA case index.
        """
        import pandas as pd

        df['sa_cofin_fund'] = ''
        df['sa_cofin_level'] = ''
        df['sa_cofin_evidence'] = ''
        df['sa_pdf_status'] = ''
        df['sa_gber_table_funds'] = ''
        df['sa_cofin_section_found'] = False
        df['sa_llm_used'] = False

        tam_mask = (df['source'] == 'TAM') & df['sa_case'].fillna('').str.startswith('SA.')
        if not tam_mask.any():
            log.info("  SA PDF parser: no TAM rows with SA codes — skipping")
            return df

        unique_sa = df.loc[tam_mask, 'sa_case'].dropna().unique()
        log.info(f"  SA PDF parser: processing {len(unique_sa)} unique SA codes ...")

        parsed: Dict[str, dict] = {}
        for sa_code in unique_sa:
            links = sa_lookup.get_pdf_links(sa_code)
            result = self.parse_sa_code(sa_code, links)
            parsed[sa_code] = result
            status = result['pdf_status']
            funds = ','.join(result['cofin_funds'])
            level = result.get('cofin_level', '')
            backend = result.get('extraction_backend', '')
            log.info(
                f"    {sa_code}: status={status} backend={backend}"
                + (f" level={level} funds={funds}" if funds else '')
            )

        for idx in df.index[tam_mask]:
            sa_code = df.at[idx, 'sa_case']
            r = parsed.get(sa_code, {})
            df.at[idx, 'sa_cofin_fund'] = ','.join(r.get('cofin_funds', []))
            df.at[idx, 'sa_cofin_level'] = r.get('cofin_level', '')
            df.at[idx, 'sa_cofin_evidence'] = r.get('cofin_evidence', '')
            df.at[idx, 'sa_pdf_status'] = r.get('pdf_status', '')
            df.at[idx, 'sa_gber_table_funds'] = str(r.get('gber_table_funds', {})) or ''
            df.at[idx, 'sa_cofin_section_found'] = r.get('cofin_section_found', False)
            df.at[idx, 'sa_llm_used'] = r.get('llm_used', False)

        n_with_cofin = (df['sa_cofin_fund'] != '').sum()
        n_llm = df['sa_llm_used'].sum()
        log.info(f"  SA PDF parser: {n_with_cofin} TAM rows with co-financing detected"
                 + (f" ({n_llm} via LLM)" if n_llm else ""))
        return df

    # ------------------------------------------------------------------
    # Internal helpers — extraction backends
    # ------------------------------------------------------------------

    def _extract_markdown(self, pdf_bytes: bytes) -> Optional[str]:
        """Extract PDF as structured markdown using pymupdf4llm.

        pymupdf4llm produces markdown with:
        - ## headings for section titles
        - Un-broken paragraphs (no mid-phrase newlines)
        - '**----- Start of picture text -----**' blocks for image-embedded text
          (OCR-extracted; used in GBER forms that are submitted as scanned images)

        Returns None if pymupdf4llm is not installed or extraction fails.
        The caller falls back to _extract_text() (pdfplumber/pdfminer) in that case.
        """
        try:
            import pymupdf4llm  # noqa: F401
            import fitz
        except ImportError:
            return None

        try:
            fitz.TOOLS.mupdf_display_errors(False)  # suppress ICC profile warnings
            doc = fitz.open(stream=pdf_bytes, filetype='pdf')
            md = pymupdf4llm.to_markdown(doc)
            doc.close()
            if not md or not md.strip():
                return None
            # Normalise pymupdf4llm-specific markup for downstream regex:
            # - Replace <br> (from picture text) with newline
            # - Remove picture text block markers (keep content)
            md = md.replace('<br>', '\n')
            md = re.sub(
                r'\*\*-{3,}\s*(?:Start|End)\s+of\s+picture\s+text\s*-{3,}\*\*',
                '',
                md,
                flags=re.IGNORECASE,
            )
            md = re.sub(r'\*\*==> picture \[.*?\] .*?<==\*\*', '', md)
            return md
        except Exception as exc:
            log.debug(f"    pymupdf4llm extraction failed: {exc}")
            return None

    def _extract_text(self, pdf_bytes: bytes) -> Optional[str]:
        """Extract text from PDF bytes. Tries pdfplumber then pdfminer."""
        # --- pdfplumber (preferred) ---
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                parts = []
                for page in pdf.pages:
                    t = page.extract_text()
                    if t:
                        parts.append(t)
            if parts:
                return '\n'.join(parts)
        except ImportError:
            pass  # fall through to pdfminer
        except Exception as exc:
            log.debug(f"    pdfplumber failed: {exc}")

        # --- pdfminer fallback ---
        try:
            from pdfminer.high_level import extract_text as pm_extract
            text = pm_extract(io.BytesIO(pdf_bytes))
            if text and text.strip():
                return text
        except ImportError:
            log.warning("  Neither pdfplumber nor pdfminer.six installed. "
                        "pip install pdfplumber")
        except Exception as exc:
            log.debug(f"    pdfminer failed: {exc}")

        return None

    # ------------------------------------------------------------------
    # Internal helpers — LLM tier (Tier 3)
    # ------------------------------------------------------------------

    def _extract_cofin_excerpt(self, text: str, max_chars: int = 4000) -> str:
        """Return the text window most likely to contain co-financing information.

        Scans for co-financing keyword positions, finds the max_chars-sized window
        with the highest keyword density, and returns that window. Falls back to
        the first max_chars characters if no keywords are found.

        This keeps LLM token cost bounded even for 300-page IPCEI decisions.
        """
        # Keywords that signal co-financing content
        kw_re = re.compile(
            r'co.?financ|funded\s+by|financed\s+by|financed\s+through'
            r'|erdf|esf[^a-z]|cohesion\s+fund|just\s+transition'
            r'|recovery\s+and\s+resilience|rrf[^a-z]|eafrd|eagf'
            r'|feder\b|feader\b|feaga\b',
            re.IGNORECASE,
        )
        positions = [m.start() for m in kw_re.finditer(text)]
        if not positions:
            return text[:max_chars]

        # Slide a window and count keyword hits
        best_start = 0
        best_count = 0
        half = max_chars // 2
        for pos in positions:
            start = max(0, pos - half)
            end = start + max_chars
            count = sum(1 for p in positions if start <= p < end)
            if count > best_count:
                best_count = count
                best_start = start

        return text[best_start: best_start + max_chars]

    def _detect_cofin_llm(
        self,
        excerpt: str,
        sa_code: str = '',
    ) -> Tuple[List[str], str, str]:
        """Call Claude Haiku to extract EU fund co-financing from a text excerpt.

        Returns (funds_list, level, evidence) — same shape as _detect_cofin().
        Returns ([], '', '') on any error (import failure, API error, parse error).

        Requires ANTHROPIC_API_KEY environment variable.
        """
        try:
            import anthropic
        except ImportError:
            log.warning("  LLM tier: 'anthropic' package not installed. pip install anthropic")
            return [], '', ''

        import json as _json

        prompt = COFIN_LLM_PROMPT.format(
            sa_code=sa_code or 'unknown',
            excerpt=excerpt,
        )
        try:
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model='claude-haiku-4-5-20251001',
                max_tokens=512,
                messages=[{'role': 'user', 'content': prompt}],
            )
            raw = msg.content[0].text.strip()
            # Strip markdown code fence if present
            if raw.startswith('```'):
                raw = re.sub(r'^```\w*\n?', '', raw)
                raw = re.sub(r'\n?```$', '', raw)
            data = _json.loads(raw)
            funds = [str(f).upper() for f in data.get('fund_names', []) if f]
            level = str(data.get('level', '')).lower()
            if level not in ('confirmed', 'conditional', ''):
                level = ''
            evidence = str(data.get('evidence', ''))
            return funds, level, evidence
        except Exception as exc:
            log.debug(f"    LLM tier error for {sa_code}: {exc}")
            return [], '', ''

    # ------------------------------------------------------------------
    # Internal helpers — structural parsing
    # ------------------------------------------------------------------

    def _detect_gber_table(self, text: str) -> Dict[str, float]:
        """Parse GBER notification table for co-financing fund amounts.

        The GBER summary table submitted by the Member State contains a row:
          "If co-financed by Community funds  FEDER - 0 EUR  Feader - 0 EUR"
        This may be on one line (pdfplumber) or multi-line (pymupdf4llm picture text).

        Returns {canonical_fund_name: amount_eur} for funds with amount > 0 EUR only.
        Empty dict if no GBER table found, or all amounts are zero (no commitment).
        """
        anchor = _GBER_TABLE_ANCHOR_RE.search(text)
        if not anchor:
            return {}

        # Search the 400 chars after the anchor for fund-amount pairs
        # (covers all fund rows even when they wrap across multiple lines)
        block = text[anchor.end(): anchor.end() + 400]
        results: Dict[str, float] = {}
        for m in _GBER_FUND_ROW_RE.finditer(block):
            fund_raw = m.group(1).upper().rstrip('+')
            # Normalise ESF+ separately (strip was too aggressive above)
            raw_orig = m.group(1).upper()
            if raw_orig in ('ESF+', 'ESF\u207a'):
                fund_raw_key = 'ESF+'
            else:
                fund_raw_key = fund_raw
            canonical = _GBER_FUND_MAP.get(fund_raw_key, fund_raw_key)
            amt_str = re.sub(r'[\s]', '', m.group(2)).replace(',', '.')
            try:
                amt = float(amt_str)
                if amt > 0:
                    results[canonical] = amt
            except ValueError:
                pass

        return results

    def _find_cofin_section(self, md_text: str) -> Optional[str]:
        """Find and return text of an EU fund co-financing section in markdown.

        Looks for ## headings that specifically mention EU fund names
        (not generic "co-financing" which could be beneficiary co-financing).

        Returns the section body text, or None if no such section is found.
        """
        if not md_text:
            return None
        heading_match = _COFIN_SECTION_HEADING_RE.search(md_text)
        if not heading_match:
            return None
        section_start = heading_match.end()
        next_heading = re.search(r'\n#{1,4}\s+', md_text[section_start:])
        section_end = (
            section_start + next_heading.start()
            if next_heading
            else len(md_text)
        )
        section = md_text[section_start:section_end].strip()
        return section if section else None

    # ------------------------------------------------------------------
    # Internal helpers — co-financing detection
    # ------------------------------------------------------------------

    def _detect_cofin(
        self,
        text: str,
        section_text: Optional[str] = None,
    ) -> Tuple[List[str], str, str]:
        """Search text for EU fund co-financing mentions.

        Returns (found_funds, cofin_level, evidence_snippet).

        Strategy:
        1. Strip boilerplate spans from the text.
        2. If a dedicated EU fund section was found (via _find_cofin_section),
           run COFIN_CONFIRMED_RE on that section first. Higher precision,
           fewer false positives from legal boilerplate in the rest of the document.
        3. Run COFIN_CONFIRMED_RE on full text (Tier 1 — confirmed).
        4. If no confirmed match, run COFIN_CONDITIONAL_RE (Tier 2 — conditional).
        5. For each match, extract a ±200-char evidence snippet and scan ±1500
           chars for EU fund name patterns to identify which fund(s) are involved.
        """
        # Strip boilerplate spans so they don't inflate context matches.
        # Replace with spaces to preserve string positions for snippet extraction.
        clean_text = text
        for bm in COFIN_BOILERPLATE_RE.finditer(text):
            clean_text = (
                clean_text[:bm.start()]
                + ' ' * (bm.end() - bm.start())
                + clean_text[bm.end():]
            )

        evidence_parts: List[str] = []
        found_funds: List[str] = []
        cofin_level = ''

        # --- Tier 1: confirmed co-financing ---
        # If a dedicated EU fund section was provided, search it first.
        # Otherwise search the full document.
        search_texts = []
        if section_text:
            clean_section = section_text
            for bm in COFIN_BOILERPLATE_RE.finditer(section_text):
                clean_section = (
                    clean_section[:bm.start()]
                    + ' ' * (bm.end() - bm.start())
                    + clean_section[bm.end():]
                )
            search_texts.append(('section', clean_section, section_text))
        search_texts.append(('full', clean_text, text))

        confirmed_matches = []
        for _label, clean, raw in search_texts:
            confirmed_matches = list(COFIN_CONFIRMED_RE.finditer(clean))
            if confirmed_matches:
                # Use this context for fund identification
                cofin_level = 'confirmed'
                for cm in confirmed_matches[:3]:
                    snip_s = max(0, cm.start() - _SNIPPET_RADIUS)
                    snip_e = min(len(raw), cm.end() + _SNIPPET_RADIUS)
                    snippet = raw[snip_s:snip_e].replace('\n', ' ').strip()

                    # Fund names within ±1500 chars — wide window for IPCEI decisions
                    # where the fund is named in the same paragraph but far from header
                    ctx_s = max(0, cm.start() - 1500)
                    ctx_e = min(len(clean), cm.end() + 1500)
                    context = clean[ctx_s:ctx_e]
                    for fund_name, fund_pat in EU_FUND_PATTERNS.items():
                        if fund_name not in found_funds and fund_pat.search(context):
                            found_funds.append(fund_name)

                    if snippet:
                        evidence_parts.append(f"...{snippet}...")
                break  # don't double-count

        # --- Tier 2: conditional co-financing (only if Tier 1 didn't fire) ---
        if not confirmed_matches:
            cond_match = COFIN_CONDITIONAL_RE.search(clean_text)
            if cond_match:
                cofin_level = 'conditional'
                snip_s = max(0, cond_match.start() - _SNIPPET_RADIUS)
                snip_e = min(len(text), cond_match.end() + _SNIPPET_RADIUS)
                snippet = text[snip_s:snip_e].replace('\n', ' ').strip()
                if snippet:
                    evidence_parts.append(f"...{snippet}...")

                ctx_s = max(0, cond_match.start() - 400)
                ctx_e = min(len(clean_text), cond_match.end() + 400)
                context = clean_text[ctx_s:ctx_e]
                for fund_name, fund_pat in EU_FUND_PATTERNS.items():
                    if fund_pat.search(context):
                        found_funds.append(fund_name)

        evidence = ' | '.join(evidence_parts[:3])
        return found_funds, cofin_level, evidence

    # ------------------------------------------------------------------
    # Internal helpers — caching and download
    # ------------------------------------------------------------------

    def _cache_path(self, sa_code: str, url: str) -> Path:
        """Return local cache path for a PDF."""
        safe = sa_code.replace('.', '_').replace('/', '_')
        url_tail = url.split('/')[-1].replace('.pdf', '')
        return self.cache_dir / f"{safe}__{url_tail}.pdf"

    def _load_cached(self, sa_code: str, url: str) -> Optional[bytes]:
        p = self._cache_path(sa_code, url)
        if p.exists():
            return p.read_bytes()
        return None

    def _save_cache(self, sa_code: str, url: str, data: bytes) -> None:
        p = self._cache_path(sa_code, url)
        p.write_bytes(data)

    def _download(self, url: str) -> Optional[bytes]:
        """Download a PDF with rate limiting and retries."""
        try:
            import requests
        except ImportError:
            log.warning("  'requests' not installed — PDF download unavailable. pip install requests")
            return None

        # Rate limiting
        elapsed = time.time() - self._last_request_time
        if elapsed < self.request_delay:
            time.sleep(self.request_delay - elapsed)

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = requests.get(url, timeout=self.timeout, headers={
                    'User-Agent': 'Mozilla/5.0 (research; subsidy-analysis)'
                })
                self._last_request_time = time.time()
                resp.raise_for_status()
                return resp.content
            except requests.HTTPError as exc:
                log.warning(f"    PDF {url}: HTTP {exc.response.status_code} (attempt {attempt})")
            except Exception as exc:
                log.warning(f"    PDF {url}: {exc} (attempt {attempt})")
            if attempt < self.max_retries:
                time.sleep(2 ** attempt)  # exponential back-off

        return None


    # ------------------------------------------------------------------
    # Technology keyword extraction (for Output 1 — text attribution)
    # ------------------------------------------------------------------

    # Maps technology sector → list of regex patterns (case-insensitive).
    # Patterns are designed to match within EC state aid decision text.
    # ── Tech keyword patterns ──────────────────────────────────────────────
    # Two tiers: 'strong' patterns are highly specific to the sector;
    # 'moderate' patterns are plausible but could appear in generic energy
    # policy preambles. detect_tech_keywords() weights them differently.
    # Multilingual coverage: EN, DE, FR, ES, IT, NL, PL, DA, FI, SV, CS/SK.
    TECH_KEYWORD_PATTERNS: dict = {
        'wind': {
            'strong': [
                r'\bwind\s*(energy|power|farm|turbine|park|offshore|onshore)\b',
                r'\boffshore\s+wind\b', r'\bonshore\s+wind\b',
                r'\bwind\s+generation\b',
                # DE
                r'\bwindenergie\b', r'\bwindkraft(?:anlage)?\b', r'\bwindpark\b',
                # FR
                r'\b[eé]olien(?:ne)?s?\b', r'\bparc\s+[eé]olien\b',
                # ES
                r'\bparque\s+e[oó]lico\b', r'\benerg[ií]a\s+e[oó]lica\b',
                # NL
                r'\bwindmolenpark\b',
                # DA/SV
                r'\bvindkraft\b', r'\bvindenergi\b', r'\bvindm[oø]lle\b',
                # FI
                r'\btuulivoima\b',
                # PL
                r'\bfarma?\s+wiatrow[aey]\b', r'\benergi[aą]\s+wiatrow[aą]\b',
            ],
            'moderate': [
                r'\brenewable\s+energy\b', r'\berneuerbare\s+energie\b',
                r'\b[eé]nergie\s+renouvelable\b',
                r'\benerg[ií]as?\s+renovable\b',
                r'\bclean\s+energy\b', r'\bgreen\s+energy\b',
                r'\belectricity\s+generation\b',
                r'\bvedvarende\s+energi\b',  # DA
            ],
        },
        'solar': {
            'strong': [
                r'\bsolar\s*(energy|power|panel|cell|module|photovoltaic|pv)\b',
                r'\bphotovoltaic\b', r'\b(?:PV|CSP)\s+(plant|module|installation|farm)\b',
                r'\bsolar\s+(?:farm|park)\b',
                # DE
                r'\bsolarenergie\b', r'\bphotovoltaik\b', r'\bsolaranlage\b',
                # FR
                r'\bsolaire\b', r'\bphotovolta[iï]que\b',
                # ES/IT
                r'\bfotovoltaic[ao]?\b',
                # PL
                r'\bfotowoltaic\w*\b',
            ],
            'moderate': [
                r'\brenewable\s+energy\b', r'\berneuerbare\s+energie\b',
                r'\b[eé]nergie\s+renouvelable\b',
                r'\bclean\s+energy\b', r'\bgreen\s+energy\b',
                r'\belectricity\s+generation\b',
            ],
        },
        'hydrogen': {
            'strong': [
                r'\bhydrogen\b', r'\belectrolys(?:er|is|eur)\b', r'\belectrolyzer\b',
                r'\bfuel\s+cell\b', r'\bgreen\s+hydrogen\b',
                r'\bpower.to.(?:gas|x|hydrogen)\b',
                r'\bipcei\s+hy2(?:tech|use|infra|move)\b',
                # DE
                r'\bwasserstoff\b', r'\belektrolyse\b', r'\bbrennstoffzelle\b',
                # FR
                r'\bhydrog[eè]ne\b', r'\bpile\s+[aà]\s+combustible\b',
                # ES
                r'\bhidr[oó]geno\b',
                # IT
                r'\bidrogeno\b',
                # PL
                r'\bwod[oó]r\b', r'\belektrolizer\b',
                # NL
                r'\bwaterstof\b',
            ],
            'moderate': [
                r'\bdecarboni[sz]\w*\b', r'\bclean\s+energy\b',
                r'\benergy\s+transition\b',
                r'\bgreen\s+gas\b',
            ],
        },
        'nuclear': {
            'strong': [
                r'\bnuclear\b', r'\batomic\s+energy\b', r'\bSMR\b',
                r'\bfission\b', r'\breactor\b', r'\buranium\b',
                # FR
                r'\bnucl[eé]aire\b',
                # DE
                r'\bnuklear\b', r'\bkernkraft\b', r'\bkernenergie\b', r'\batomkraft\b',
                r'\bnuclear\s+(?:decommission|waste)\b',
                r'\bspent\s+fuel\b', r'\buran(?:ium)?\b',
                # FI
                r'\bydinvoima\b', r'\bydinenergia\b',
                # CS/SK
                r'\bjadrov\w*\b',
                # PL
                r'\bj[aą]drow\w*\b', r'\belektrowni[aąe]\s+j[aą]drow\w*\b',
            ],
            'moderate': [
                r'\bbaseload\b', r'\blow\s+carbon\s+electricity\b',
                r'\benergy\s+security\b',
                r'\bmining\b', r'\bbergbau\b', r'\bmineral\b',
            ],
        },
        'hydroelectric': {
            'strong': [
                r'\bhydroelectric\b', r'\bhydropower\b', r'\bhydro.?electric\b',
                r'\bpumped.storage\b', r'\brun.of.river\b',
                # DE
                r'\bwasserkraft\b', r'\bpumpspeicher\b',
                # FR
                r'\bhydro[eé]lectr\w+\b',
                # ES/IT
                r'\bhidroel[eé]ctric\w*\b', r'\bidroelettric\w*\b',
            ],
            'moderate': [
                r'\brenewable\s+energy\b', r'\bclean\s+energy\b',
            ],
        },
        'grid': {
            'strong': [
                r'\btransmission\s+grid\b', r'\belectricity\s+grid\b',
                r'\binterconnect(?:or|ion)\b', r'\bhigh.voltage\b',
                r'\b(?:TSO|DSO)\b', r'\bsubmarine\s+cable\b', r'\bsubsea\s+cable\b',
                r'\btransmission\s+system\s+operator\b',
                r'\bpower\s+line\b', r'\belectricity\s+infrastructure\b',
                # DE
                r'\bstromnetz\b', r'\bhochspannung\w*\b',
                # FR
                r'\br[eé]seau\s+[eé]lectrique\b',
                # FI
                r'\bs[aä]hk[oö]verkko\b',
                # NL
                r'\belektriciteitsnet\b',
            ],
            'moderate': [
                r'\benergy\s+infrastructure\b',
                r'\belectricity\s+market\b', r'\bstrommarkt\b',
                r'\bpower\s+system\b',
            ],
        },
        'geothermal': {
            'strong': [
                r'\bgeothermal\b', r'\bdeep\s+heat\b',
                # DE
                r'\bgeothermie\b', r'\berdw[aä]rme\b',
                # FR
                r'\bg[eé]otherm\w+\b',
            ],
            'moderate': [
                r'\brenewable\s+heat\b', r'\bclean\s+heat\b',
            ],
        },
        'steel': {
            'strong': [
                r'\bgreen\s+steel\b', r'\bhydrogen.based\s+steelmaking\b',
                r'\bdirect\s+reduc(?:ed|tion)\s+iron\b', r'\bDRI\b',
                r'\belectric\s+arc\s+furnace\b', r'\bEAF\b',
                r'\bsteel\s+(?:plant|mill|production|decarboni[sz])\w*\b',
                r'\biron\s+and\s+steel\b',
                # DE
                r'\bstahl\b', r'\beisen\s+und\s+stahl\b', r'\bhochofen\b',
                r'\blichtbogenofen\b',
                # FR
                r'\bacier\b', r'\bsid[eé]rurg\w+\b',
                # ES
                r'\bacero\b',
                # IT
                r'\bacciaio\b',
                # PL
                r'\bstal(?:owni[aąe])?\b',
                # NACE codes in text
                r'\bC\.?24\.?[123]\b',
                r'\bmanufacture\s+of\s+basic\s+iron\b',
            ],
            'moderate': [
                r'\bets\b', r'\bemission\s+trading\b', r'\bcarbon\s+leakage\b',
                r'\bindirect\s+emission\b',
                r'\benergy\s+intensive\b', r'\bstrompreiskompensation\b',
                r'\bmanufacture\s+of\s+basic\s+metals\b', r'\bmetallurg\w+\b',
                r'\beeg\b', r'\brenewable\s+energy\s+surcharge\b',
                r'\bstromsteuer\b', r'\belectricity\s+tax\b',
                r'\benergiesteuer\b', r'\benergy\s+tax\b',
            ],
        },
        'iron': {
            'strong': [
                r'\bgreen\s+iron\b', r'\bHBI\b', r'\bhot\s+briquetted\s+iron\b',
                # DE
                r'\beisenschwamm\b', r'\bheiss\s*brikettiertes\s+eisen\b',
            ],
            'moderate': [
                r'\biron\s+ore\b', r'\beisenerz\b',
            ],
        },
    }

    def _extract_text_first_n_pages(self, pdf_bytes: bytes, max_pages: int = 8) -> Optional[str]:
        """Extract text from the first N pages of a PDF.

        Uses pdfplumber (page-by-page) as the primary backend since pymupdf4llm
        extracts the whole document and is slower for partial reads.
        """
        try:
            import pdfplumber
            with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
                parts = []
                for page in pdf.pages[:max_pages]:
                    t = page.extract_text()
                    if t:
                        parts.append(t)
            if parts:
                return '\n'.join(parts)
        except ImportError:
            pass
        except Exception as exc:
            log.debug(f"    pdfplumber (N-page) failed: {exc}")

        # fallback to full extraction
        return self._extract_text(pdf_bytes) or self._extract_markdown(pdf_bytes)

    def detect_tech_keywords(self, text: str) -> dict:
        """Scan text for technology keywords and return per-sector matches.

        Strong keyword hits are weighted 3x relative to moderate hits when
        determining the primary sector. A sector needs at least one strong
        hit to be counted as 'found'; moderate-only matches are flagged but
        not included in techs_found.

        Returns
        -------
        dict with keys:
            techs_found       : list[str]  — sectors with strong hits
            techs_moderate    : list[str]  — sectors with only moderate hits
            tech_evidence     : dict       — {sector: [snippet, ...]}
            primary_tech      : str        — sector with highest weighted score
            weighted_scores   : dict       — {sector: float} for all matched sectors
            has_strong_hit    : dict       — {sector: bool}
        """
        STRONG_WEIGHT = 3.0
        MODERATE_WEIGHT = 1.0

        hits: dict[str, list[str]] = {}
        weighted: dict[str, float] = {}
        has_strong: dict[str, bool] = {}

        for sector, tier_dict in self.TECH_KEYWORD_PATTERNS.items():
            sector_hits = []
            sector_score = 0.0
            sector_has_strong = False

            for tier, patterns in tier_dict.items():
                weight = STRONG_WEIGHT if tier == 'strong' else MODERATE_WEIGHT
                for pat in patterns:
                    for m in re.finditer(pat, text, re.IGNORECASE):
                        start = max(0, m.start() - 60)
                        end = min(len(text), m.end() + 60)
                        snippet = text[start:end].replace('\n', ' ').strip()
                        sector_hits.append(snippet)
                        sector_score += weight
                        if tier == 'strong':
                            sector_has_strong = True

            if sector_hits:
                hits[sector] = sector_hits[:3]
                weighted[sector] = sector_score
                has_strong[sector] = sector_has_strong

        # Sectors with at least one strong hit
        strong_sectors = sorted(s for s, v in has_strong.items() if v)
        # Sectors with only moderate hits
        moderate_only = sorted(s for s in hits if s not in strong_sectors)

        primary = max(weighted, key=weighted.get) if weighted else ''

        return {
            'techs_found': strong_sectors,
            'techs_moderate': moderate_only,
            'tech_evidence': hits,
            'primary_tech': primary,
            'weighted_scores': weighted,
            'has_strong_hit': has_strong,
        }

    def parse_sa_code_tech(self, sa_code: str, pdf_links: list[dict],
                           max_pages: int = 20) -> dict:
        """Download PDFs for an SA code and extract technology keywords.

        Scans ALL available language versions (not just the first success)
        and merges hits across them. This catches keywords in the original-
        language decision that the English summary may omit. Increased
        default max_pages from 8→20 to cover substantive sections of long
        IPCEI decisions.

        Returns
        -------
        dict with keys:
            sa_code, pdf_status, pdf_url, pdf_lang, extraction_backend,
            techs_found, techs_moderate, tech_evidence, primary_tech,
            weighted_scores, has_strong_hit, langs_scanned, full_text
        """
        result = {
            'sa_code': sa_code,
            'pdf_status': 'no_pdf',
            'pdf_url': '',
            'pdf_lang': '',
            'extraction_backend': '',
            'techs_found': [],
            'techs_moderate': [],
            'tech_evidence': {},
            'primary_tech': '',
            'weighted_scores': {},
            'has_strong_hit': {},
            'langs_scanned': [],
            'full_text': '',  # merged text from all PDFs (for beneficiary name check)
        }

        if not pdf_links:
            return result

        # Prefer English first, then WLAL (full decision), then other langs
        ordered = sorted(pdf_links, key=lambda x: (
            0 if x['lang'] in ('en', 'EN') else 1,
            0 if x.get('name') == 'WLAL' else 1,
        ))

        # Accumulate results across all language versions
        merged_weighted: dict[str, float] = {}
        merged_evidence: dict[str, list[str]] = {}
        merged_has_strong: dict[str, bool] = {}
        merged_text_parts: list[str] = []
        any_parsed = False

        for link_info in ordered:
            url = link_info['url']
            lang = link_info.get('lang', '')

            pdf_bytes = self._load_cached(sa_code, url)
            if pdf_bytes is None:
                pdf_bytes = self._download(url)
                if pdf_bytes is None:
                    continue
                self._save_cache(sa_code, url, pdf_bytes)

            text = self._extract_text_first_n_pages(pdf_bytes, max_pages=max_pages)
            if text is None:
                continue

            any_parsed = True
            result['langs_scanned'].append(lang)
            merged_text_parts.append(text)

            # Record first successful URL/lang for backward compatibility
            if not result['pdf_url']:
                result['pdf_url'] = url
                result['pdf_lang'] = lang

            tech_result = self.detect_tech_keywords(text)

            # Merge: accumulate weighted scores and evidence across languages
            for sector, score in tech_result.get('weighted_scores', {}).items():
                merged_weighted[sector] = merged_weighted.get(sector, 0) + score
                if sector not in merged_evidence:
                    merged_evidence[sector] = []
                merged_evidence[sector].extend(tech_result['tech_evidence'].get(sector, []))
                # Keep max 3 snippets per sector
                merged_evidence[sector] = merged_evidence[sector][:3]
                if tech_result['has_strong_hit'].get(sector, False):
                    merged_has_strong[sector] = True

        if any_parsed:
            result['extraction_backend'] = 'pdfplumber'
            result['pdf_status'] = 'parsed'

            strong_sectors = sorted(s for s, v in merged_has_strong.items() if v)
            moderate_only = sorted(s for s in merged_weighted if s not in strong_sectors)
            primary = max(merged_weighted, key=merged_weighted.get) if merged_weighted else ''

            result['techs_found'] = strong_sectors
            result['techs_moderate'] = moderate_only
            result['tech_evidence'] = merged_evidence
            result['primary_tech'] = primary
            result['weighted_scores'] = merged_weighted
            result['has_strong_hit'] = merged_has_strong
            result['full_text'] = '\n\n'.join(merged_text_parts)
        else:
            result['pdf_status'] = 'download_failed'

        return result


# ---------------------------------------------------------------------------
# Convenience function for use in consolidation.py
# ---------------------------------------------------------------------------

def enrich_tam_cofin(
    df,
    sa_lookup,
    cache_dir: str | Path = 'data/processed/sa_decisions',
    use_llm: bool = False,
) -> 'pd.DataFrame':
    """Add PDF co-financing columns to a consolidated_matches dataframe.

    This is the Tier B entry point called optionally from consolidation.
    Adds seven columns:
      sa_cofin_fund          — comma-separated EU fund names found in decision PDF
      sa_cofin_level         — 'confirmed' | 'conditional' | ''
      sa_cofin_evidence      — text snippet(s) from PDF supporting the finding
      sa_pdf_status          — parse outcome per SA code
      sa_gber_table_funds    — dict-as-string: {fund: amount_eur} from GBER table
      sa_cofin_section_found — True if dedicated EU fund co-financing section found
      sa_llm_used            — True if Tier 3 LLM was invoked for that SA code

    Parameters
    ----------
    df : pd.DataFrame
    sa_lookup : SACaseLookup  (already loaded)
    cache_dir : path to PDF cache directory
    use_llm : bool
        If True, call Claude Haiku when regex finds no signal. Requires ANTHROPIC_API_KEY.
    """
    parser = SACofinParser(cache_dir=cache_dir, use_llm=use_llm)
    return parser.enrich_dataframe(df, sa_lookup)
