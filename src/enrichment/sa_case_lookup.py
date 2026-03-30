"""
SA Case Lookup
==============
Lightweight index over case-data-SA.json — the EC DG Competition registry of all
state aid cases (59,983 cases as of dataset download).

Loads the JSON once and exposes fast lookup functions used by:
  - consolidation.py  → IPCEI/TAM SA-code dedup, TAM expenditure validation
  - sa_pdf_parser.py  → PDF attachment URL retrieval

JSON structure per case:
  {
    "SA.XXXXX": {
      "metadata": {
        "caseTitle": [...],
        "caseOriginalTitle": [...],
        "caseMemberState": ["{\"code\":\"CountryDEU\",\"label\":\"Germany\"}"],
        "caseAidCategory": ["{\"code\":\"CaseTypeS\",\"label\":\"Scheme\"}"],
        "caseAidInstruments": [...],
        "caseExpenditures": ["{\"amount\":\"12345.67\",\"year\":2021,\"currency\":\"EUR\"}"],
        "caseLinks": ["SA.YYYYY"],
        ...
      },
      "decisions": [
        {
          "metadata": {...},
          "decisionAttachments": [
            {
              "metadata": {
                "attachmentLink": ["https://ec.europa.eu/.../SA.pdf"],
                "attachmentLanguage": ["en"],
                "attachmentName": ["WLAL"],
                ...
              }
            }
          ]
        }
      ]
    }
  }

IMPORTANT — caseExpenditures unit:
  Amounts in caseExpenditures are in MILLIONS EUR (M€), not EUR.
  All get_expenditures() return values are already converted to EUR.

Usage:
  from src.enrichment.sa_case_lookup import SACaseLookup

  lookup = SACaseLookup('case-data-SA.json')
  print(lookup.is_ipcei('SA.54794'))          # True
  print(lookup.get_expenditures('SA.107094')) # {2021: 1234567.0, ...}
  print(lookup.get_pdf_links('SA.107094'))    # [{'url': ..., 'lang': 'en', ...}]
"""

import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional

log = logging.getLogger(__name__)

# SA code suffix variants: SA.52028(2018/X), SA.54808(2019/N) etc.
_SA_SUFFIX_RE = re.compile(r'\s*\(\d{4}/[A-Z]+\)\s*$')


def normalise_sa(code: str) -> str:
    """Strip trailing procedure qualifier from SA code.

    E.g. 'SA.52028(2018/X)' → 'SA.52028'
         'SA.54808(2019/N)' → 'SA.54808'
         'SA.107094'        → 'SA.107094'
    """
    return _SA_SUFFIX_RE.sub('', str(code).strip())


class SACaseLookup:
    """Index of EC state aid case metadata for fast lookup by SA code.

    Parameters
    ----------
    json_path : str or Path
        Path to case-data-SA.json.
    """

    def __init__(self, json_path):
        self._path = Path(json_path)
        self._data: Dict = {}
        self._ipcei_sa_codes: Dict[str, dict] = {}   # sa_code → {title, member_state}
        self._expenditures: Dict[str, Dict[int, float]] = {}  # sa_code → {year: eur}
        self._loaded = False

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> 'SACaseLookup':
        """Load and index the JSON. Returns self for chaining."""
        if self._loaded:
            return self
        if not self._path.exists():
            log.warning(f"SA case JSON not found: {self._path}. SA lookups will be no-ops.")
            self._loaded = True
            return self

        log.info(f"Loading SA case data from {self._path.name} ...")
        with open(self._path, encoding='utf-8') as f:
            self._data = json.load(f)

        n_ipcei = 0
        n_exp = 0
        n_unresolved = 0

        for sa_code, v in self._data.items():
            meta = v.get('metadata', {})

            # --- IPCEI index ---
            titles = meta.get('caseTitle', []) + meta.get('caseOriginalTitle', [])
            if any('ipcei' in t.lower() for t in titles):
                state_raw = meta.get('caseMemberState', ['{}'])[0]
                try:
                    state = json.loads(state_raw)
                except (json.JSONDecodeError, TypeError):
                    state = {}
                self._ipcei_sa_codes[sa_code] = {
                    'title': titles[0] if titles else '',
                    'member_state_code': state.get('code', ''),
                    'member_state_label': state.get('label', ''),
                    'last_decision': (meta.get('caseLastDecisionDate') or [''])[0],
                }
                n_ipcei += 1

            # --- Expenditure index (convert M€ → EUR) ---
            raw_exps = meta.get('caseExpenditures', [])
            if raw_exps:
                year_map: Dict[int, float] = {}
                for e in raw_exps:
                    try:
                        parsed = json.loads(e) if isinstance(e, str) else e
                        yr = int(parsed['year'])
                        amt_eur = float(parsed['amount']) * 1_000_000  # M€ → EUR
                        year_map[yr] = amt_eur
                    except (KeyError, ValueError, json.JSONDecodeError):
                        continue
                if year_map:
                    self._expenditures[sa_code] = year_map
                    n_exp += 1

        log.info(
            f"  SA case index: {len(self._data):,} cases, "
            f"{n_ipcei} IPCEI, {n_exp:,} with expenditure data"
        )

        self._loaded = True
        return self

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_ipcei(self, sa_code: str) -> bool:
        """Return True if this SA code is an IPCEI state aid case.

        Normalises the code before lookup (strips procedure qualifiers).
        """
        norm = normalise_sa(sa_code)
        return norm in self._ipcei_sa_codes

    def get_ipcei_info(self, sa_code: str) -> Optional[dict]:
        """Return IPCEI metadata dict or None if not an IPCEI case."""
        return self._ipcei_sa_codes.get(normalise_sa(sa_code))

    def get_ipcei_sa_codes(self) -> Dict[str, dict]:
        """Return full {sa_code: info} dict for all IPCEI cases."""
        return self._ipcei_sa_codes

    def get_expenditures(self, sa_code: str) -> Dict[int, float]:
        """Return {year: amount_eur} for a case, or {} if not found.

        Amounts are in EUR (already converted from M€ in source).
        """
        return self._expenditures.get(normalise_sa(sa_code), {})

    def validate_tam_amount(
        self,
        sa_code: str,
        tam_year: int,
        tam_amount_eur: float,
    ) -> str:
        """Cross-check a TAM row's amount against the case's EC-reported expenditure.

        TAM records the amount for one beneficiary within a scheme; caseExpenditures
        records total scheme spending for the year. So TAM amount should be ≤ scheme
        expenditure. Ratios > 2.0 suggest a mismatch.

        Returns
        -------
        'confirmed'     TAM amount ≤ scheme expenditure for the year
        'plausible'     TAM amount 1.0–2.0× scheme expenditure (multi-year vs annual)
        'inconsistent'  TAM amount > 2× scheme expenditure
        'no_data'       No expenditure data for this SA code or year
        """
        exps = self.get_expenditures(sa_code)
        if not exps or tam_year not in exps:
            return 'no_data'
        scheme_eur = exps[tam_year]
        if scheme_eur <= 0:
            return 'no_data'
        ratio = tam_amount_eur / scheme_eur
        if ratio <= 1.0:
            return 'confirmed'
        if ratio <= 2.0:
            return 'plausible'
        return 'inconsistent'

    def get_pdf_links(self, sa_code: str) -> List[dict]:
        """Return list of PDF attachment dicts for a case.

        Each dict has:
          url      : str   — full HTTPS link to the EC decision PDF
          lang     : str   — language code (e.g. 'en', 'fr', 'de')
          name     : str   — attachment name (e.g. 'WLAL' = full decision text)
          sent     : str   — date the attachment was sent (YYYY-MM-DD)
          decision : str   — decision number / reference

        Sorted by language preference: English first, then alphabetical.
        """
        norm = normalise_sa(sa_code)
        v = self._data.get(norm, {})
        results = []
        for decision in v.get('decisions', []):
            dec_ref = decision.get('metadata', {}).get('metadataReference', [''])[0]
            for att in decision.get('decisionAttachments', []):
                m = att.get('metadata', {})
                links = m.get('attachmentLink', [])
                if not links:
                    continue
                results.append({
                    'url':      links[0],
                    'lang':     (m.get('attachmentLanguage') or [''])[0].lower(),
                    'name':     (m.get('attachmentName') or [''])[0],
                    'sent':     (m.get('attachmentSentDate') or [''])[0],
                    'decision': dec_ref,
                })
        # English first
        results.sort(key=lambda x: (0 if x['lang'] in ('en', 'EN') else 1, x['lang']))
        return results

    def get_linked_cases(self, sa_code: str) -> List[str]:
        """Return list of SA codes linked to this case (e.g. scheme → individual app)."""
        norm = normalise_sa(sa_code)
        v = self._data.get(norm, {})
        return [l for l in v.get('metadata', {}).get('caseLinks', []) if l != norm]

    def case_exists(self, sa_code: str) -> bool:
        """Return True if the SA code exists in the index."""
        return normalise_sa(sa_code) in self._data

    def log_unresolved(self, sa_codes, label: str = '') -> None:
        """Log SA codes that could not be resolved after normalisation.

        Call this with the set of SA codes from matched TAM rows to surface gaps.
        """
        unresolved = [c for c in sa_codes if c and not self.case_exists(c)]
        if unresolved:
            tag = f' [{label}]' if label else ''
            log.warning(
                f"  SA lookup{tag}: {len(unresolved)} unresolved codes "
                f"(logged below — not blocking):"
            )
            for c in sorted(unresolved):
                log.warning(f"    unresolved: {c!r}")
        else:
            log.info(f"  SA lookup: all codes resolved" + (f' [{label}]' if label else ''))
