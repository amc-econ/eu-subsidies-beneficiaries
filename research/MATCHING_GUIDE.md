# Entity Matching Guide

The generic matcher (`src/matching/generic_matcher.py`) accepts any company list and matches it against the master dataset of European public financial support.

The master dataset covers three types of support — **state aid** (national subsidies notified to the European Commission), **EU funds** (money paid directly out of the EU budget), and **IFIs** (loans and guarantees from the European Investment Bank and European Bank for Reconstruction and Development). Below, user-facing descriptions use these three categories. The underlying per-source mechanics appear in [`research/METHODOLOGY.md`](METHODOLOGY.md); this guide is the operational reference for running the matcher and interpreting its output, not a data-source catalogue.

## Company List Format

Supply a CSV with at minimum a `company_name` column:

```csv
company_name,country,source
Volkswagen AG,DE,orbis
Tesla Inc,US,manual
CATL,,manual
```

Only `company_name` is required. Additional columns pass through to output unchanged.

## Aliases Format

Optionally supply a JSON file mapping canonical names to known aliases:

```json
{
    "Volkswagen AG": ["VW", "Volkswagen Group"],
    "Bayerische Motoren Werke AG": ["BMW", "BMW Group"],
    "Stellantis NV": ["FCA", "Fiat Chrysler", "PSA Group", "Peugeot", "Citroen", "Opel"]
}
```

Aliases are added to the exact-match lookup: a matching beneficiary name links to the canonical company.

## Running the Matcher

### Via the pipeline orchestrator

```bash
python run_pipeline.py --stage match \
    --company-list my_companies.csv \
    --aliases my_aliases.json \
    --output-dir data/processed/match_output
```

### Via Python

```python
from src.matching.generic_matcher import run_matching, MatchConfig

enriched_df, match_log_df = run_matching(
    master_csv=Path('data/processed/master_dataset.csv'),
    company_list_csv=Path('my_companies.csv'),
    aliases_json=Path('my_aliases.json'),          # optional
    output_dir=Path('data/processed/match_output'),
    config=MatchConfig(),                           # optional, uses defaults
)
```

## MatchConfig Options

```python
from dataclasses import dataclass

@dataclass
class MatchConfig:
    fuzzy_high_threshold: int = 85       # High-confidence fuzzy match
    fuzzy_medium_threshold: int = 75     # Medium-confidence fuzzy match
    token_overlap_min: int = 2           # Min shared significant tokens for fuzzy
    length_ratio_max: float = 2.5        # Max length ratio between names
    short_name_max_len: int = 5          # Names <= this length require exact match
    chunk_size: int = 250_000            # Rows per chunk when reading master CSV

    # Domain-specific filters (empty by default)
    contextual_blocklist: frozenset[str]          # Words to exclude from Layer B regex
    false_positive_pairs: frozenset[tuple[str,str]]  # Known FP (beneficiary, reference) pairs
    beneficiary_fp_patterns: dict[str, re.Pattern]   # Regex patterns that indicate FPs

    output_prefix: str = 'match'         # Prefix for output filenames
```

## Matching Layers

### Layer A: Direct Entity Matching

1. **Exact match**: `entity_name_clean` looked up directly in the reference dictionary (includes aliases)
2. **Fuzzy match**: `rapidfuzz.fuzz.token_set_ratio` with trivial-token filtering
   - Pre-filtered by token inverted index (must share >= `token_overlap_min` significant tokens)
   - Pre-filtered by length ratio (must be within `length_ratio_max`)
   - Short names (<= 5 chars) require exact match only (avoids "BMW" matching "BNW")
   - **Country consistency veto**: if the company list includes a `country` column and a `fuzzy_medium` candidate's country conflicts with the master row's country, the match is rejected. Exact and fuzzy_high matches are unaffected. This fires automatically when country data is present — no per-entity configuration required.

### Layer B: Contextual Text Matching

For rows whose beneficiary name field is blank, generic, or uninformative but whose project description mentions the real recipient (common in EU-fund payment records):
- Builds a single regex from all reference names (≥ 6 chars, minus blocklisted words)
- Scans the `description` field, packed original columns, and any source-specific text fields
- The matcher tags these `contextual_exact` with lower confidence

### Layer B+: Title Extraction (IFI loans)

IFI loan records (European Investment Bank / European Bank for Reconstruction and Development) typically store the project title in the `beneficiary_name` field rather than the borrower's name. For these rows the matcher:
- Extracts candidate company names from the title text
- Matches the extracted names against the reference list
- Tags successful matches with `eib_title_extraction` and `attribution_type = inferred`

## Output Files

The matcher produces in the output directory:

| File | Description |
|------|------------|
| `{prefix}_match_log.csv` | Full match log: every matched row with match type, confidence, scores |
| `{prefix}_match_summary.txt` | Human-readable summary: counts, EUR totals, by support type, by match type |
| `matcher.log` | Detailed execution log |

### Match Log Columns

| Column | Description |
|--------|------------|
| `{prefix}_reference_name` | Matched company from reference list |
| `{prefix}_type` | See match type table below |
| `{prefix}_score` | rapidfuzz score (0–100); 100 for exact matches, null for enrichment-sourced rows |
| All original master columns | Preserved from master_dataset |

### Match Types

The `match_type` column records which mechanism produced the match. There are three groups: Layer A (name matching), Layer B (text scanning), and enrichment (post-match sources added after the main matching pass).

**Layer A: name matching**

The matcher collects all unique `entity_name_clean` values from the master dataset (~8 million), scores them once against the reference list, then joins results back to the full 27M rows. This avoids rescoring the same name repeatedly.

| `match_type` | Score | Description |
|---|---|---|
| `exact` | 100 | Normalised beneficiary name matches a reference name or alias exactly. |
| `fuzzy_high` | 85-99 | `rapidfuzz.fuzz.token_set_ratio`. This scorer sorts and compares token sets, so word-order differences and inserted legal suffixes (SA, GmbH, plc) do not reduce the score. |
| `fuzzy_medium` | 75-84 | Same scorer at a lower threshold. More likely to need spot-checking on material amounts. If the company list includes a `country` column, any fuzzy_medium match where the reference country conflicts with the row's country is vetoed before reaching the output. |

Before fuzzy scoring, candidates are pre-filtered: a beneficiary name must share at least one significant token with the reference name to be scored at all. Tokens that are too common to be informative (the, of, ltd, gmbh, sa, group, etc.) are excluded from this index. Names of 5 characters or fewer require an exact match.

**Layer B: text scanning**

Runs only on rows that Layer A did not match. A single compiled regex built from all reference names (minimum 6 characters) is applied to free-text fields.

| `match_type` | Description |
|---|---|
| `contextual_exact` | Reference name found in the row's `description` field. The beneficiary name itself did not match. The entity is named in project context rather than as the recorded recipient. `attribution_type = contextual`. |
| `eib_title_extraction` | For IFI loan rows where `beneficiary_name` contains the project title rather than a company name. The regex scans the title to extract a company reference. `attribution_type = inferred`. |

**Enrichment**

Rows added by the post-match enrichment scripts, not by the entity matcher. Each carries its own `match_type` to distinguish it from Layer A/B results.

| `match_type` | Description |
|---|---|
| `fts_cordis_beneficiary_name` | Research-consortium bridge (EU funds). Grant IDs are extracted from EU-fund payment descriptions and joined to the research-programme participant data. This value means the payment recipient itself matched the reference company — a direct grant receipt. |
| `fts_cordis_cordis_company` | Research-consortium bridge. The reference company appears in the consortium member list for this grant but is not the entity that received the EU-fund payment (often the coordinating university or institute). Always `dc_preferred=False`, `attribution_type=consortium_partner`. |
| `ipcei_reference` | Matched against per-company aid rows **extracted directly from the 12 EC IPCEI decision PDFs** shipped under `data/reference/ipcei_decisions/`. Amounts are bracket midpoints because the EC redacts exact figures in public decision text (`[5-10] million EUR`). Each row carries an `amount_confidence` column (`exact_from_pdf` / `range_from_pdf` / `redacted`) and an `ipcei_ticker` column identifying the IPCEI programme. In the three-bucket taxonomy these are **state aid** rows. |
| `sa_adhoc_preload` | Matched via the ad hoc state-aid pre-loader — the EC registry contains individual single-company decisions (`CaseTypeAH`) that were never harmonized through the main state-aid layer, typically because they pre-date TAM (most are pre-2016). Each row carries `is_adhoc_preloaded = True`, an `amount_confidence` column (`regex_exact` for cleanly parsed amounts, `not_extracted` for name-only audit hints, `parse_failed` for PDF read errors), and an `amount_evidence` snippet for audit. `source = 'TAM'` because these decisions *are* state aid — they simply couldn't be harmonized from the transparency database the way regular TAM rows are. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H8. |

## Full Pipeline (match + enrich + consolidate)

When you run `--stage match`, the pipeline automatically executes:

### Post-Match Enrichment

Five enrichment scripts run automatically after matching:

| Script | What it does |
|--------|-------------|
| Research-consortium bridge | Extracts research-programme grant IDs from EU-fund payment descriptions and joins to consortium participant data, so individual members of a consortium grant are attributed correctly. |
| EU ETS free allocation | Matches company names against the EU Emissions Trading System free-allocation installation records. |
| IPCEI decision PDF enrichment | Extracts per-company aid directly from the 12 EC IPCEI decision PDFs shipped with the repo (`data/reference/ipcei_decisions/`). Amounts are bracket midpoints where the EC redacted the exact figure and exact values where it didn't; the per-row `amount_confidence` column distinguishes them. Rows carry an `ipcei_ticker` marker identifying which IPCEI programme they come from. |
| Ad hoc state-aid decision pre-load | Enumerates individual ad hoc state-aid decisions in the EC registry that never flowed into the harmonized state-aid data (mostly pre-2016 cases). Extracts the beneficiary name from the decision title, matches against the user's reference list, and only then downloads the decision PDF to attempt an aid amount extraction via regex. Emits one row per matched case with `is_adhoc_preloaded = True` and an `amount_confidence` column indicating whether the amount came from a clean regex match or the row is name-only (audit hint only, not contributing to headline totals). Runs only when `--pdf-enrichment` is enabled. |
| Deep text mining | Scans full EU-fund payment descriptions for company names that the direct name-match layer missed. |
| High-value forensics | Audits top unmatched rows (>EUR 500K) to surface potential missed matches. |

### Consolidation

The consolidation step produces **two** CSVs and a family of summary artefacts computed on the headline view only:

| Output | Description |
|--------|------------|
| `consolidated_matches.csv` | **Headline view.** Rows with `dc_preferred=True`, `match_quality` not in `{suspect_description_only, suspect_eib_title, suspect_contextual_generic}`, and `is_anonymised=False`. This is the CSV every chart, summary table, and concentration metric is built from; the methodology paper's headline numbers cite this file. |
| `consolidated_matches_audit.csv` | **Audit view.** Every matched row, including duplicates, suspect matches, anonymised bucket rows, and audit-only heuristic hits (`heuristic_flag` column). Readers who want to know *why* a row was excluded from the headline start here. |
| `group_summary.csv` | Group-level summary (if parent_groups configured), computed on the headline view |
| `concentration_metrics.json` | HHI, Top5%, Gini at entity and group level — headline view only |
| `T1-T8 summary tables` | By support type, country, instrument, year, fiscal source, top entities — headline view only |
| `charts/` | 6 matplotlib charts — headline view only |

**Amount columns.** As of the 2026-04-13 rewrite, consolidation publishes `amount_eur_face` (always populated face value), `amount_eur_gge` (face × GGE rate from the `GGE_RATES` table; `NaN` for rows whose `financial_instrument_class` is not in the table — no silent 100% grant default), `amount_eur_low` / `amount_eur_high` (bracket bounds for IPCEI ranges; equal to `amount_eur` for every other source), and `gge_rate_source ∈ {measured, measured_repayable, unknown}` so readers can distinguish measured GGE from empty. `amount_eur` remains populated for backwards compatibility and equals `amount_eur_face` everywhere.

### Cross-Source Deduplication

Several public databases capture the same underlying financial flow from different angles. Consolidation detects these overlaps, marks the lower-authority row `dc_preferred=False`, and sets `dc_flag` to record which pattern was detected. No rows are deleted. Charts and summary tables use `dc_preferred = True` rows only.

The overlaps that matter are between the three categories:

1. **State aid ↔ EU funds.** Many state-aid decisions are partly or wholly co-financed by an EU structural fund, a research programme, or a recovery instrument. If both the state-aid row (the national notification) and the EU-fund row (the direct budget payment) are in the dataset, they are two views of the same money.
2. **Within EU funds.** The EU's central financial transparency register and the individual programme databases (research, cohesion, climate, innovation, connecting-Europe) overlap on their own. The same grant frequently appears as an outbound budget payment in one source and as an award decision in another.
3. **Within state aid.** IPCEI participations appear both in the PDF-extracted per-company data (from the shipped IPCEI decision PDFs) and as individual SA decisions in the central state-aid database.
4. **IFIs.** IFI loans (EIB / EBRD) alongside state-aid grants for the same beneficiary are intentionally **not** deduplicated — they are genuinely separate financing instruments, and the GGE conversion already discounts loans to 15% so that their contribution to the headline total is not over-stated.

Detection runs in a **deliberate priority order** inside `consolidate()` (see [`src/matching/consolidation.py`](../src/matching/consolidation.py), Phase 2b):

1. **Identical EU-fund transactions.** Deduplicates within the EU-funds category where a payment row in one EU-fund database echoes an award row in another (Innovation Fund, CEF, LIFE, research programmes, etc.).
2. **Document-backed state-aid ↔ EU-funds dedup (authoritative).** For every matched state-aid row, the pipeline consults the EC decision PDF. When the PDF explicitly confirms that the measure **is** co-financed by a specific EU fund, and a row in any EU-fund source exists for the same company + country within a ±2-year window with a matching fund name, the state-aid row is flagged as the document-confirmed duplicate. This is the pipeline's primary deduplication mechanism and it runs before the heuristic below so that its decisions take priority.
3. **Heuristic state-aid ↔ EU-funds fallback — AUDIT ONLY.** As of the 2026-04-13 rewrite, the heuristic sets a new `heuristic_flag` column and **does NOT toggle `dc_preferred`**. It no longer affects published totals. Rows the ratio check would have flagged are visible to readers of `consolidated_matches_audit.csv` via `heuristic_flag != ''` and can be compared against the PDF-grounded decisions, but the headline view treats them as regular rows. This enforces the no-invention principle: only document evidence can exclude a row from the headline. The historical plausibility band (KOHESIO/TAM amount ratio in [0.01, 1.50], ±2 year window) is retained verbatim for the audit signal but has no impact on headline numbers. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) plan §6.5.
4. **Multi-country structural artifact.** Some EU-fund rows that cover a multi-country project are repeated once per partner country. The first occurrence is kept; the rest are flagged.
5. **State-aid within state-aid (IPCEI ↔ central database).** IPCEI project aid is notified both as an individual SA case and via the per-company PDF extraction. The PDF-extracted row is the authoritative one (it gives the per-company split that the central database collapses into an umbrella row); the central-database duplicate is flagged when amounts agree within 20% and years within ±2.

For the raw source-pair breakdown behind each of these steps — the specific databases involved, the exact function names, and the full fund-name alias table — see [`research/METHODOLOGY.md`](METHODOLOGY.md) §7. That document is the only place in the repo that uses the internal database names.

**State-aid decision PDF enrichment (Phase 2c).** The authoritative step above depends on columns (`sa_cofin_fund`, `sa_cofin_level`, `sa_cofin_evidence`, …) populated by a dedicated enrichment phase ([`consolidation.py:1246-1320`](../src/matching/consolidation.py#L1246-L1320)). For every matched state-aid row the pipeline:

1. Normalises the SA case code (`SA.XXXXX`) and looks it up in the EC state-aid case registry (`case-data-SA.json`, ~60,000 cases, auto-downloaded on first run).
2. Fetches the decision PDF (English first) and caches it under `data/cache/sa_decisions/`, shared across all runs.
3. Runs a four-tier extraction against the PDF text:
   - **Tier 0 — GBER notification table.** For scheme decisions submitted via the EC's General Block Exemption Regulation form, parses the standardised co-financing summary table at the top of the decision.
   - **Tier 1 — Confirmed prose.** Regex for declarative co-financing statements such as *"the measure is co-financed by the ERDF"* or *"totally made available through the Recovery and Resilience Facility"*. Yields `sa_cofin_level = 'confirmed'`.
   - **Tier 2 — Conditional prose.** Regex for boilerplate compliance clauses such as *"to the extent the scheme is co-financed by ERDF/ESF"* that appear in many Temporary-Framework decisions even when no co-financing is actually planned. Yields `sa_cofin_level = 'conditional'` — **not used for deduplication**; only `confirmed` drives step 2.
   - **Tier 3 — LLM fallback.** Optional Claude Haiku call, ~$0.0014/PDF. Only invoked when `--use-llm` is set and the regex tiers find nothing. Targets non-English decisions and fragmented sentences.

Phase 2c is controlled by `--pdf-enrichment` (default **on**). Disabling it silently downgrades cross-source deduplication to the heuristic step 3 alone; the Phase 2c log line makes this visible at the start of every run.

To re-run PDF enrichment on an already-consolidated CSV **without** redoing matching, use `--stage enrich-pdf --consolidated <path>`. The stage backs up the original to `*_pre_pdf.csv` and updates `dc_preferred` / `dc_flag` in place.

**Fund-name alias table.** Matching a PDF-extracted fund to a counterpart row's `fund` column uses a canonical-alias table carrying **119 entries across 23 fund families** — ERDF / ESF / ESF+ / CF / JTF / RRF / ESIF / INTERREG / EAFRD / EAGF / EMFAF / Horizon 2020 / Horizon Europe cluster labels / MSCA / ERC / JRC overhead lines / EIC / INNOVFUND / CEF / LIFE / ERASMUS / COSME / DEP / SKILLS / ENI / IPA / EDIDP / UCPM — with principal English, French, German, Spanish, Italian, Polish, Czech, Hungarian, Romanian, Slovak, Slovenian, and Baltic variants. The lookup is a **word-boundary regex** (`(?<!\w)alias(?!\w)`) rather than a substring check, which prevents short aliases (`ipa`, `dep`, `cef`, `esf`, `feder`) from silently matching unrelated substrings (`participation`, `developpement`, `federalism`). See [`src/matching/consolidation.py`](../src/matching/consolidation.py) `_FUND_ALIASES` and [`research/PIPELINE_AUDIT.md`](PIPELINE_AUDIT.md) H1 for coverage history.

**Columns added to `consolidated_matches.csv` / `consolidated_matches_audit.csv`**:

| Column | Values | Description |
|--------|--------|-------------|
| `dc_preferred` | `True` / `False` | `True` = include in headline. Toggled only by document-grounded dedup steps, never by the heuristic. |
| `dc_flag` | pipe-delimited strings or empty | Document-grounded dedup reasons (FTS-INNOVFUND identical, TAM↔<source>_pdf cofinancing, IPCEI-TAM confirmed, consortium-partner, same-record-multicountry) |
| `heuristic_flag` | pipe-delimited strings or empty | **Audit-only.** The plausibility-ratio heuristic's hits. Visible in the audit CSV; does not affect published totals. |
| `match_quality` | `ok` / `suspect_description_only` / `suspect_eib_title` / `suspect_contextual_generic` | Quality assessment. Suspect rows are excluded from the headline. |
| `is_anonymised` | `True` / `False` | `True` for rows whose beneficiary_name is a known anonymisation / bucket sentinel. Excluded from the headline. |
| `amount_eur_face` | float | Face value (always populated). |
| `amount_eur_gge` | float or `NaN` | Face × GGE rate. `NaN` when the instrument class is not in `GGE_RATES`. |
| `amount_eur_low` / `amount_eur_high` | float | Bracket bounds for ranged IPCEI rows; equal to `amount_eur` for every other row. |
| `gge_rate_source` | `measured` / `measured_repayable` / `unknown` | How the GGE rate was derived. |
| `amount_confidence` | `measured` / `exact_from_pdf` / `range_from_pdf` / `redacted` / `regex_exact` / `suspect_fallthrough` / `not_extracted` | Confidence in the amount value. |
| `attribution_type` | `direct` / `consortium_partner` / `contextual` / `inferred` | How the amount is linked to the matched entity |
| `programme` | string | EU-fund programme name from the programme map |
| `cofinancing_partner_id` | source_record_id or empty | Forensic pointer to the authoritative counterpart row when a dedup flag fires |
| `extra_fields_json` | JSON string | Source-specific per-row metadata (EIB project prose, IPCEI bounds, CORDIS topics, etc.). Always a valid JSON object string. |

**Filtering**:

```python
df_clean  = df[df['dc_preferred'] == True]                          # headline totals
df_direct = df[(df['dc_preferred'] == True) &
               (df['attribution_type'] == 'direct')]                # direct beneficiaries only
df_dupes  = df[df['dc_preferred'] == False]                         # inspect what was flagged

# Filter by specific overlap type
df_cofin  = df[df['dc_flag'].str.contains('cofinancing_overlap', na=False)]
```

**`dc_flag` values**:

| Flag | Meaning |
|------|---------|
| `confirmed_duplicate:fts_innovfund` | Within **EU funds**: a payment row echoes an Innovation Fund award decision. Amounts match to ≤ 0.1%. |
| `confirmed_duplicate:fts_cinea` | Within **EU funds**: a payment row echoes a project record from a CINEA-managed programme (Connecting Europe Facility, LIFE, EMFAF). Shared project ID is definitive. |
| `cofinancing_overlap:tam_<source>_pdf` | **Document-backed (authoritative).** The state-aid decision PDF confirms co-financing by a specific EU fund, and a matching row exists in the named EU-fund source. Runs before the heuristic step so its decisions take priority. The `<source>` suffix identifies which EU-fund database the counterpart row came from; it is generated dynamically at consolidation time from whichever source has the match, not a fixed enumeration. |
| `cofinancing_overlap:tam_kohesio` | **Heuristic fallback.** A state-aid row was matched to a cohesion-policy row on the same company + country + year window + plausible amount ratio. Only applies to state-aid rows that the document-backed step did not already flag. |
| `cofinancing_overlap:tam_rrf` | **Heuristic fallback.** A state-aid row matched a Recovery and Resilience Facility row. In practice this branch does not fire, because the Recovery and Resilience Facility currently publishes no beneficiary-level data and its rows never reach the matcher — see [`research/PIPELINE_AUDIT.md`](PIPELINE_AUDIT.md) H7. The flag is retained in the codebase as a forward-compatibility hook. |
| `confirmed_duplicate:ipcei_tam` | Within **state aid**: an IPCEI PDF-extracted row matches the same SA decision in the central state-aid database. The PDF row is the authoritative one (it carries the per-company split that the central row collapses). |
| `consortium_partner_attribution` | Research-consortium bridge flagged the row: the matched entity is one of several consortium members, not the sole beneficiary of the grant. |
| `same_record_multicountry` | Same source record (ID + entity + amount + year) appears under multiple country codes — structural artifact of multi-country EU-fund projects; the first occurrence is kept and the rest are flagged. |

**PDF-only output columns** (added by Phase 2c when `--pdf-enrichment` is on):

| Column | Values | Description |
|---|---|---|
| `sa_cofin_fund` | comma-separated canonical fund codes, or `''` | Which EU funds the SA decision PDF mentions. |
| `sa_cofin_level` | `confirmed` / `conditional` / `''` | `confirmed` drives dedup; `conditional` does not. |
| `sa_cofin_evidence` | short excerpt | The phrase that triggered the match, for audit. |
| `sa_gber_table_funds` | dict-as-string | Fund-to-amount map parsed from the GBER notification table (non-zero entries only). |
| `sa_cofin_section_found` | bool-like | Whether a dedicated co-financing section/heading was located. |
| `sa_pdf_status` | `ok` / `download_failed` / `parse_failed` / `no_pdf` / … | Per-row diagnostic. |

IFI loans alongside state-aid or EU-fund grants for the same beneficiary are intentionally kept as `dc_preferred=True` — loans are repayable and GGE conversion already applies a lower rate (15% for loans, 10% for guarantees, versus 100% for grants), so they do not over-state the headline total.

---

**False positive controls**: pass via
`MatchConfig(false_positive_pairs=..., beneficiary_fp_patterns=...)`. See
`examples/automotive/config.py` for the pattern.

### GGE (Gross Grant Equivalent)

Face values are converted to subsidy-equivalent values using EU State Aid Scoreboard rates. The full mapping (see [`src/matching/consolidation.py:42-47`](../src/matching/consolidation.py#L42-L47)):

| Financial instrument class | GGE Rate |
|-----------|----------|
| Grant, direct subsidy, procurement, equity, debt relief, other | 100% |
| Mixed (grant + loan package) | 50% |
| Loan (standard) | 15% |
| Loan with `repayable` in the subtype (repayable advance) | 90% |
| Guarantee | 10% |
| Tax advantage | 15% |

Unknown or empty instrument classes default to 100% for backwards compatibility, but the pipeline now **logs a summary warning** after Phase 4 listing every unknown class it encountered along with the row count and EUR affected, so pipeline drift is visible in the run log. See [`research/PIPELINE_AUDIT.md`](PIPELINE_AUDIT.md) H5.

## Config JSON Format

Supply a JSON config to enable parent group rollup and sector-specific enrichment:

```json
{
    "name": "my_sector",
    "parent_groups": "path/to/parent_groups.json",
    "sector_keywords": ["keyword1", "keyword2"],
    "nace_filter": ["29", "2910"],
    "match_config": {
        "output_prefix": "my_sector",
        "exact_only_names": ["short_name1", "short_name2"]
    }
}
```

### Parent Groups JSON

Maps entity names to corporate groups for group-level analysis:

```json
{
    "Volkswagen Group": ["volkswagen", "vw", "audi", "porsche", "seat", "skoda"],
    "Stellantis": ["stellantis", "fiat", "peugeot", "citroen", "opel"]
}
```

## Automotive Example

See [`examples/automotive/`](../examples/automotive/) for a worked sector analysis.

```bash
python run_pipeline.py --stage automotive
```
