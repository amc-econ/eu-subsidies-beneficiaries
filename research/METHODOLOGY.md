# Methodology: EU Subsidies Beneficiary Analysis

This document is an operational description of the pipeline as it exists in the current source tree. Every non-trivial claim is anchored to a `file:line` reference in the repository so that it can be verified against the code.

## 1. Data Sources and Collection

The analysis draws on 12 publicly available EU data sources, ~27 million rows of subsidy, loan, grant, and contract records spanning 2000–2024. Each source is retrieved in its original format and stored unmodified before harmonization.

### 1.1 Primary Sources

**Transparency Award Module (TAM).** The European Commission's state aid transparency database, ~2.1 million individual aid award decisions reported by Member States for 2000–2023. Records include beneficiary name, NACE sector, granting authority, aid instrument, and awarded amount in EUR. TAM rows carry an SA code (`source_record_id`) that references a state aid decision in the DG Competition registry; this anchor drives PDF-backed deduplication (Section 7).

**TAM National Supplements.** Four Member State portals provide granular award-level data that the central TAM captures only partially:

- *Spain (BDNS)*: ~4.72 million rows from the Base de Datos Nacional de Subvenciones, retrieved via paginated API.
- *Poland (SUDOP)*: ~15.7 million rows from the System Udostępniania Danych o Pomocy Publicznej.
- *Romania*: National state aid register supplement.
- *Slovenia*: National state aid register supplement.

**Financial Transparency System (FTS).** ~1.4 million rows of EU budget expenditure (contracts and grants), from 18 yearly Excel files (2007–2024) published by the European Commission. Amount column: "Beneficiary's contracted amount (EUR)". No NACE codes.

**Kohesio.** ~1.9 million EU Cohesion Policy project records from the Commission's unified cohesion data platform.

**European Investment Bank (EIB).** ~26,000 loan and investment records. The `beneficiary_name` field contains the project title rather than the borrower's name, necessitating dedicated enrichment (Section 4).

**European Bank for Reconstruction and Development (EBRD).** ~6,000 project records. As with EIB, the beneficiary field contains project titles.

**European Structural and Investment Funds (ESIF).** Two programming periods are treated as separate source packages: ESIF 2014–2020 and ESIF 2021–2027. Programme-level aggregates are excluded from the master dataset by default (see Section 5) because they overlap with project-level Kohesio data.

**Climate, Infrastructure and Environment Executive Agency (CINEA).** Project-level grant and contract data from CINEA-managed programmes (CEF, LIFE, EMFAF).

**Recovery and Resilience Facility (RRF).** Measure-level planned expenditure. **This source contains no beneficiary names** — every row carries `beneficiary_name = pd.NA` and `granularity = 'measure'` straight from the harmonization layer ([src/harmonization/rrf.py:114](../src/harmonization/rrf.py#L114)). (Earlier master builds stored the sentinel as an empty string `''` via a pandas-object-column coercion bug; the read path now defensively coerces `''`, `'None'`, `'nan'` back to `NaN` in `paths.read_master` so downstream `.isna()` / `.notna()` checks are trustworthy — see [src/paths.py](../src/paths.py) `_coerce_null_sentinels`.) In consequence, **no RRF row can be assigned to a company by the entity matcher**, no RRF row reaches `match_log.csv`, and no RRF amount flows through to `consolidated_matches.csv` or any headline total. The RRF rows are retained in the master dataset as a contextual reminder that the source exists and that a future harmonizer targeting national recovery-plan portals could one day populate beneficiary names; until then RRF is effectively absent from the beneficiary-level analysis. The RRF branch of `_flag_cofinancing_overlaps` is dead code for the same reason and is kept only as a forward-compatibility hook. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H7.

**RESEARCH (CORDIS).** Horizon 2020 and Horizon Europe project participations sourced from CORDIS.

**EU State Aid Scoreboard.** Aggregated Member State aid expenditure by instrument type and objective. Used as contextual reference for GGE conversion rates; excluded from primary record counts via flag-based exclusion.

### 1.2 Reference Data Shipped With the Repo

- **`case-data-SA.json`** — the EC DG Competition state aid case registry (59,983 cases as of dataset download, ~638 MB). Indexed at runtime by [src/enrichment/sa_case_lookup.py](../src/enrichment/sa_case_lookup.py) and used to (i) resolve SA codes on TAM rows to their decision PDFs, (ii) flag IPCEI cases, and (iii) cross-check TAM row amounts against EC-reported scheme expenditure. Auto-downloaded on first run by [run_pipeline.py:96-120](../run_pipeline.py).
- **`master_dataset.parquet`** — a pre-built master dataset (~1.7 GB). Auto-downloaded on first run by [run_pipeline.py:68-93](../run_pipeline.py) so that most users never need to run the harmonization stage themselves.

### 1.3 User-Supplied Company List

The pipeline is sector-agnostic: the user supplies a CSV whose first column is `company_name` (with an optional `country` column in ISO-2 form). Everything downstream operates against this list. The only worked example shipped in the repository is automotive, under [`examples/automotive/`](../examples/automotive/).

---

## 2. Harmonization

Source-specific harmonization modules under [src/harmonization/](../src/harmonization/) standardize all 12 sources to a canonical 36-column schema (Schema v2). The schema covers entity resolution, flow taxonomy, fiscal classification, and flag-based exclusions.

### 2.1 Entity Name Cleaning

Raw beneficiary names are normalized:

1. **Legal suffix stripping**: trailing corporate designators (GmbH, S.A., Ltd., SRL, AB, etc.) are removed. Leading tokens are preserved (e.g. "AB Volvo" retains "AB" since it is a leading token). The suffix list covers Germanic, Romance, Nordic, Portuguese (*Unipessoal Lda*), Polish (*Sp. z o.o.*), Spanish (*Unipersonal*), and Italian (*Società Unipersonale*) forms. Stripped suffixes are also added to the trivial-token blocklist so that legal-form fragments cannot drive fuzzy-match scores.
2. **Whitespace normalization**: consecutive whitespace collapsed; leading/trailing whitespace trimmed.
3. **Case normalization**: lowercased for matching; original casing preserved separately.
4. **Parenthetical stripping**: parenthetical content removed before fuzzy matching.

### 2.2 Entity Resolution

Deterministic entity IDs are derived from (`entity_name_clean`, `country_code`).

### 2.3 Currency Conversion

Non-EUR amounts are converted using ECB reference exchange rates ([src/harmonization/ecb_fx_rates.py](../src/harmonization/ecb_fx_rates.py)). The applicable rate is selected by award/commitment date.

---

## 3. Pre-Matching Enrichment

Two enrichment steps run before entity matching to strengthen beneficiary identification in sources with weak name fields.

### 3.1 CORDIS Organization Join

For RESEARCH-sourced records, a bulk join against the CORDIS organization dataset links project participation IDs to structured organization names and metadata, achieving ~80% match rate for Horizon Europe/2020 projects. An API-based backfill targets remaining unmatched projects ([src/enrichment/cordis_enrichment.py](../src/enrichment/cordis_enrichment.py), [cordis_api_backfill.py](../src/enrichment/cordis_api_backfill.py)).

### 3.2 EIB Promoter Scraping

EIB raw data lacks borrower names, so a sitemap-based scraper retrieves promoter information from EIB project pages ([src/enrichment/eib_promoter_scraper.py](../src/enrichment/eib_promoter_scraper.py)). ~56% of pages yield usable promoter names, with ~98% title-to-record match rate against the harmonized EIB dataset.

---

## 4. Master Dataset Construction

[src/master/builder.py](../src/master/builder.py) concatenates the standardized CSVs and applies **flag-based exclusions** defined in `MasterConfig` ([builder.py:43-80](../src/master/builder.py#L43-L80)). No rows are deleted; excluded rows carry `is_primary_record = False` and a human-readable `exclude_reason`. The default `MasterConfig`:

| Flag | Default | Effect |
|---|---|---|
| `include_scoreboard` | `False` | Scoreboard aggregate rows excluded (same underlying awards as TAM → would double-count). |
| `include_esif_programme_level` | `False` | ESIF 2014 / ESIF 2027 programme-level aggregates excluded (overlap with project-level Kohesio). |
| `include_cinea_other` | `True` | CINEA non-HORIZON/non-INNOVFUND rows retained. |
| `include_innovfund` | `True` | Innovation Fund rows retained (genuinely additive; ETS-funded, not in FTS budget lines). |
| `exclude_fts_research_overlap` | `True` | FTS rows flagged `research_programme_overlap` excluded when RESEARCH is active. |
| `eu27_only_loans` | `True` | Non-EU-27 EIB/EBRD rows excluded from the headline dataset. |
| `exclude_covid` | `False` | COVID-flagged rows retained. |

These **master-build exclusions are hard**: an excluded row is still present in the parquet but marked `is_primary_record=False` and dropped from the entity-matching input. They are distinct from the **soft flags** applied during consolidation (Section 7), which mark rows as non-preferred without removing them.

**Result**: ~27 million total rows, of which ~25.7 million are primary records and ~661,000 are excluded (flagged, not deleted). A numerical integrity gate verifies per-source row counts and EUR totals between the harmonized inputs and the assembled master.

---

## 5. Entity Matching

The matcher ([src/matching/generic_matcher.py](../src/matching/generic_matcher.py)) identifies which master rows correspond to companies on the user's reference list.

### 5.1 Deduplication Optimization

Rather than matching the full master dataset (~27.7M rows) row by row, the pipeline collapses it to the set of unique cleaned names and scores each unique name against the reference list once. There are **~7.9M unique cleaned `entity_name_clean` values** in the current master (≈ 9.05M unique raw `beneficiary_name` values collapsed by the `clean_name` normaliser). The matcher does **not** score all 7.9M against the reference list — a significant-token inverted index is applied first, and only names that share at least one non-trivial token with the reference list's significant-token set proceed to `rapidfuzz` scoring. For a typical 150-company reference list the token pre-filter removes ≥ 99% of the 7.9M unique names, leaving a few tens of thousands of candidates to be scored. The exact post-filter count scales with the reference list's token footprint and is logged per run in `match_unique_names` (`n_prefiltered` counter). See [research/NOTES_A11_entity_name_clean.md](NOTES_A11_entity_name_clean.md) for the measured counts that back this paragraph.

### 5.2 Layer A: Name Matching

**Exact match.** Case-insensitive equality between the cleaned beneficiary name and the cleaned reference name (aliases, if supplied, are folded into the exact-match lookup).

**Fuzzy match.** `rapidfuzz.fuzz.token_set_ratio` with the following controls:

- *Trivial-token filtering*: common, non-discriminating tokens (`the`, `of`, `und`, `de`, `sa`, `ltd`, `gmbh`, …) are excluded from the similarity computation.
- *Token inverted index*: a pre-built index requires each candidate to share at least `token_overlap_min` significant tokens with the reference name before scoring (default 2).
- *Length ratio guard*: candidates are discarded if their length ratio to the reference name exceeds `length_ratio_max` (default 2.5).
- *Thresholds*: exact (100), `fuzzy_high` (85–99), `fuzzy_medium` (75–84). Matches below 75 are discarded.
- *Short-name guard*: names of 5 characters or fewer require exact match only (configurable via `short_name_max_len` and `exact_only_names`).
- *Country consistency veto*: if the company list supplies a `country` column, any `fuzzy_medium` candidate whose country conflicts with the master row is vetoed. Exact and `fuzzy_high` matches are unaffected.

### 5.3 Layer B: Contextual Matching

For rows unmatched by Layer A, the pipeline applies a single compiled regex built from all reference names (minimum 6 characters, minus blocklisted words) against free-text fields: `description`, `original_columns`, and source-specific text fields. Matches tagged `contextual_exact` carry lower confidence and `attribution_type = contextual`.

**Layer B+: EIB/EBRD title extraction.** Because EIB/EBRD `beneficiary_name` fields contain project titles rather than company names, the regex also scans those titles directly. Matches tagged `eib_title_extraction` carry `attribution_type = inferred`.

### 5.4 Match-Quality Assessment

After matching and post-match enrichment, [consolidation.py:142-190](../src/matching/consolidation.py#L142-L190) runs a match-quality check on rows that matched on the description field in KOHESIO, FTS, or CINEA. If the reference name's significant content tokens do not appear anywhere in the `beneficiary_name`, the row is re-tagged `match_quality = suspect_description_only`. EIB description matches are intentionally excluded from this check because the EIB `beneficiary_name` field *is* the project title ([consolidation.py:149-150](../src/matching/consolidation.py#L149-L150)).

### 5.5 False-Positive Controls

`MatchConfig` exposes three hooks ([src/matching/generic_matcher.py](../src/matching/generic_matcher.py)):

- `false_positive_pairs` — curated `(beneficiary, reference)` pairs confirmed as FPs.
- `beneficiary_fp_patterns` — source-specific regex patterns that disqualify matches outright.
- `contextual_blocklist` — tokens stripped from the Layer B regex.

The automotive example in [examples/automotive/config.py](../examples/automotive/config.py) is the reference pattern.

---

## 6. Post-Match Enrichment

Five enrichment scripts run automatically after matching (wired in [run_pipeline.py:241-310](../run_pipeline.py#L241-L310)):

| Script | File | What it does |
|---|---|---|
| FTS-CORDIS bridge | [fts_cordis_bridge.py](../src/enrichment/fts_cordis_bridge.py) | Extracts CORDIS grant IDs from FTS descriptions and joins to participant data. Rows added as direct beneficiary receipts (`fts_cordis_beneficiary_name`) or as consortium partner attributions (`fts_cordis_cordis_company`, which are always set `dc_preferred=False`, `attribution_type=consortium_partner`). |
| EU ETS free allocation | [ets_free_allocation.py](../src/enrichment/ets_free_allocation.py) | Matches company names against EU ETS installation records. |
| IPCEI decision PDF enrichment | [ipcei_reference.py](../src/enrichment/ipcei_reference.py) + [ipcei_pdf_parser.py](../src/enrichment/ipcei_pdf_parser.py) | Extracts per-company aid from the 12 shipped IPCEI decision PDFs (Batteries 1 & 2, Microelectronics 1 & 2 + Austria amendment, Hy2Tech, Hy2Move, Hy2Use, Hy2Infra, Med4Cure, Tech4Cure, Cloud Infrastructure and Services). The parser walks each PDF's state-aid summary tables with `pdfplumber`, extracts one row per direct participant per Member State, and handles the EC's bracket-range redaction (`[5-10] million EUR` → midpoint). Output rows carry `amount_confidence ∈ {exact_from_pdf, range_from_pdf, redacted}` so downstream can distinguish confirmed figures from bracket midpoints, and `ipcei_ticker` identifies the IPCEI programme. Internal `source` is `IPCEI_state_aid`; in the three-bucket taxonomy this sits inside **state aid**. Fully redacted rows are retained for audit but dropped from headline totals by the `amount_eur > 0` filter in `integrate_enrichment`. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H6 for the migration history. |
| FTS deep mining | [fts_deep_mining.py](../src/enrichment/fts_deep_mining.py) | Text-mines FTS descriptions for company name mentions that Layer A missed. |
| High-value forensics | [highvalue_forensics.py](../src/enrichment/highvalue_forensics.py) | Audits top unmatched rows (>EUR 500K) to surface potential missed matches. |
| Ad hoc state-aid decision pre-load | [ipcei_pdf_parser.py](../src/enrichment/sa_adhoc_parser.py) — actually [sa_adhoc_parser.py](../src/enrichment/sa_adhoc_parser.py) | Enumerates individual ad hoc state-aid decisions (`CaseTypeAH`) from the EC DG Competition registry that were never harmonized via TAM — typically pre-2016 single-company decisions. Filters out programmatic/framework/COVID/TCTF titles, extracts the beneficiary from each decision title with a regex ladder, matches it against the user's reference list, and only then downloads the decision PDF to attempt an aid amount extraction (priority-ordered regex over "a grant of EUR X million", "total notified aid of EUR X", "nominal value of EUR X", etc.). Emits name-only audit rows when the amount cannot be reliably parsed — those rows are retained in the output CSV with `amount_confidence = 'not_extracted'` but excluded from headline totals by the existing `amount_eur > 0` filter. Runs only when `--pdf-enrichment` is enabled. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H8 for the full design and the measured 92.7% title extraction rate against the current registry. |

Each script writes a CSV to the enrichment directory; the consolidation step below reads and merges them.

---

## 7. State-Aid PDF Enrichment and Cross-Source Deduplication

This is the part of the pipeline where double-counting is actually removed. It runs inside `consolidate()` ([consolidation.py:1567](../src/matching/consolidation.py#L1567)) as Phases 2a → 2c → 2b. The order matters and is load-bearing. Function line anchors below are current as of the 2026-04-13 rewrite; run `grep -n '^def ' src/matching/consolidation.py` to re-verify after any refactor.

### 7.1 Phase 2a: Enrichment Integration

The five post-match enrichment CSVs are merged into the core match log via `integrate_enrichment()` ([consolidation.py:550](../src/matching/consolidation.py#L550)). A programme/fund map is loaded from `enriched.csv` and attached to every row so that dedup functions can consult the `programme` and `fund` columns.

### 7.2 Phase 2c: SA PDF Co-financing Enrichment

Controlled by the `run_pdf_enrichment` flag (surfaced as `--pdf-enrichment` on the CLI, default **True**; see Section 9). When enabled ([consolidation.py:1246-1274](../src/matching/consolidation.py#L1246-L1274)):

1. **SA code normalization.** TAM `source_record_id` values are normalized to canonical `SA.XXXXX` form via `normalise_sa()` ([sa_case_lookup.py:67-74](../src/enrichment/sa_case_lookup.py#L67-L74)), stripping trailing procedure qualifiers like `(2018/X)`.
2. **Case-data-SA.json lookup.** `SACaseLookup` ([sa_case_lookup.py:77](../src/enrichment/sa_case_lookup.py#L77)) loads the EC DG Competition registry (59,983 cases). For each matched TAM row it yields the decision PDF URLs, sorted English-first ([sa_case_lookup.py:216-247](../src/enrichment/sa_case_lookup.py#L216-L247)).
3. **`SACofinParser.enrich_dataframe()`** ([sa_pdf_parser.py:575](../src/enrichment/sa_pdf_parser.py#L575)) downloads each PDF (cached under the repo-level `data/cache/sa_decisions/` directory shared across all runs, with per-run fall-back to `output_dir/sa_decisions/` if the shared path is unwritable; 1 req/sec, 3 retries) and runs a four-tier extraction:
   - **Tier 0 — GBER notification table.** GBER (General Block Exemption Regulation) scheme decisions begin with a standardized summary form that contains a row *"If co-financed by Community funds"* with fund names and EUR amounts. `_GBER_TABLE_ANCHOR_RE` and `_GBER_FUND_ROW_RE` ([sa_pdf_parser.py:457-481](../src/enrichment/sa_pdf_parser.py#L457-L481)) parse that table. When all amounts are zero, no co-financing is planned; when any amount exceeds zero, co-financing is confirmed with a specific EUR figure. `_GBER_FUND_MAP` ([sa_pdf_parser.py:471](../src/enrichment/sa_pdf_parser.py#L471)) normalizes national-language form acronyms (FEDER, FEADER, EFRE, etc.) to canonical fund names.
   - **Tier 1 — Confirmed prose.** Regex patterns (`COFIN_CONFIRMED_RE`) target declarative co-financing statements: *"The measure is co-financed by the ERDF"*, *"totally made available through the RRF"*, *"Co-financing by a Union fund"* (IPCEI decision heading).
   - **Tier 2 — Conditional prose.** `COFIN_CONDITIONAL_RE` catches boilerplate compliance clauses such as *"to the extent the scheme is co-financed by ERDF/ESF…"* that appear in many Temporary-Framework/TCTF decisions even when no co-financing is actually planned. These yield `sa_cofin_level = 'conditional'` and are **not** used to drive deduplication.
   - **Tier 3 — LLM fallback.** Only invoked when `use_llm=True` and regex finds no signal ([sa_pdf_parser.py:758-761](../src/enrichment/sa_pdf_parser.py#L758-L761)). Sends a bounded text window to Claude Haiku (via the `anthropic` SDK) for extraction. Cost ~$0.0014/PDF. Targets non-English PDFs and footnote-fragmented sentences that slip past the regex layer. Requires `ANTHROPIC_API_KEY`.

   The parser ships with per-fund regex (`EU_FUND_PATTERNS`, [sa_pdf_parser.py:113](../src/enrichment/sa_pdf_parser.py#L113)) covering ERDF/FEDER/EFRE, ESF/FSE, Cohesion Fund, JTF, RRF, ESIF, INTERREG, EAFRD/FEADER, EAGF/FEAGA, EMFAF/EMFF, INNOVFUND, CEF, LIFE, and Horizon. Multi-language handling is built in (English, French, German, Spanish, Italian cross-language abbreviations).

   Output columns written by `enrich_dataframe` (applied to TAM rows only):

   | Column | Values | Meaning |
   |---|---|---|
   | `sa_cofin_fund` | comma-separated canonical fund codes, or `''` | Which EU funds the PDF mentions. |
   | `sa_cofin_level` | `confirmed` / `conditional` / `''` | Whether the decision states the measure **is** co-financed (`confirmed`), merely contains boilerplate conditional language (`conditional`), or has no signal (`''`). Only `confirmed` drives dedup. |
   | `sa_cofin_evidence` | excerpted text (<200 chars) | The phrase that triggered the match, for audit. |
   | `sa_gber_table_funds` | dict-as-string `{fund: amount_eur}` | Populated when a GBER table was parsed and at least one fund had a non-zero amount. |
   | `sa_cofin_section_found` | bool-like | Whether a dedicated co-financing section/heading was located. |
   | `sa_pdf_status` | `ok` / `download_failed` / `parse_failed` / `no_pdf` / … | Per-row diagnostic. |

4. **PDF backends.** `pymupdf4llm` is preferred because it produces structured markdown with unbroken paragraphs and inline picture text (OCR); `pdfplumber` is the fallback; `pdfminer.six` is the last resort. See [sa_pdf_parser.py:38-48](../src/enrichment/sa_pdf_parser.py#L38-L48).

### 7.3 Phase 2b: Cross-Source Deduplication

Phase 2b runs five dedup/attribution functions in a **deliberate priority order**. No rows are deleted; document-grounded functions set `dc_preferred=False`, and the audit-only heuristic sets a separate `heuristic_flag` column.

1. **`_dedup_fts_identical_transactions`** ([consolidation.py:1025](../src/matching/consolidation.py#L1025)) — FTS rows that echo an INNOVFUND award or a CINEA project:
   - *FTS ↔ INNOVFUND*: FTS programme contains `"Innovation Fund"`, same `match_reference_name`, amount agreement within 0.1%. Year window is **not** applied — award and payment years routinely differ. Flag: `confirmed_duplicate:fts_innovfund`.
   - *FTS ↔ CINEA*: FTS programme contains a CINEA-managed keyword (CEF, LIFE, EMFAF, …) and the FTS `source_record_id` appears in the CINEA record set. Shared project ID is definitive; no amount check. Flag: `confirmed_duplicate:fts_cinea`.

2. **`_flag_pdf_cofin_overlaps`** ([consolidation.py:1455](../src/matching/consolidation.py#L1455)) — **the authoritative, document-grounded deduplication step, and the primary mechanism for removing TAM ↔ EU-fund double-counting**. It fires only when Phase 2c has populated `sa_cofin_fund` and `sa_cofin_level == 'confirmed'` (conditional evidence is ignored). For each such TAM row it searches **every non-TAM source** (KOHESIO, RRF, FTS, CINEA, ESIF_2014, ESIF_2027, INNOVFUND, etc.) for a row that:
   - shares the same `match_reference_name` and `country`,
   - has a `year` within ±2 of the TAM row, and
   - has a `fund` column value that matches one of the PDF-extracted funds via the `_FUND_ALIASES` table ([consolidation.py:1087](../src/matching/consolidation.py#L1087)). If the other row's `fund` field is empty, the PDF evidence is accepted on its own.

   Matched TAM rows are flagged `cofinancing_overlap:tam_<source>_pdf` (e.g. `tam_kohesio_pdf`, `tam_fts_pdf`, `tam_esif_2014_pdf`) and set `dc_preferred=False`. The counterpart row's `source_record_id` is written to `cofinancing_partner_id` for forensic traceability. If `sa_cofin_*` columns are absent (PDF enrichment was skipped), the function returns the dataframe unchanged — it is a safe no-op.

3. **`_flag_cofinancing_overlaps`** ([consolidation.py:1204](../src/matching/consolidation.py#L1204)) — **audit-only heuristic**, demoted in the 2026-04-13 rewrite to set a new `heuristic_flag` column on candidate rows rather than toggling `dc_preferred`. In the pre-demotion behaviour the heuristic was a belt-and-braces supplement that silently excluded TAM rows from headline totals when the amount ratio fell inside a plausibility band (an unvalidated `[0.01, 1.50]` guess). Under the 2026-04-13 no-invention principle (plan §6.5 "no invented numbers"), the heuristic is retained as an **informational audit signal only** — readers of `consolidated_matches_audit.csv` can filter on `heuristic_flag != ''` to see pairs the ratio check *would have* flagged, and compare them against the PDF-grounded decisions. It does not change any published total. This enforces the rule that only document-grounded evidence can move a row out of the headline view. For audit purposes the heuristic still looks for KOHESIO counterparts with:
   - same `match_reference_name` and `country`,
   - year within ±2,
   - KOHESIO/TAM amount ratio in `[0.01, 1.50]`,
   - *Fund filter*: if the KOHESIO row has a non-empty `fund` column, that fund must match a canonical alias in `_FUND_ALIASES` under the new **word-boundary regex** (no longer a substring check — see §12.5). Empty `fund` values are accepted.

   Flag: `cofinancing_overlap:tam_kohesio`. The function loop also iterates over an `RRF` preferred-source entry which emits `cofinancing_overlap:tam_rrf` on paper, but that branch **never fires in practice** (RRF has no beneficiary names — see §1.1). The branch is retained as a forward-compatibility hook for the day entity-level RRF data becomes available.

4. **`_flag_ipcei_tam_overlap`** ([consolidation.py:1321](../src/matching/consolidation.py#L1321)) — flags TAM rows whose (entity, country, year ±2) matches an `IPCEI_state_aid` row within 20% on amount. The IPCEI reference row is authoritative because it carries project-level context that the raw TAM notification does not. Flag: `confirmed_duplicate:ipcei_tam`.

Then `_add_attribution_type` ([consolidation.py:1418](../src/matching/consolidation.py#L1418)) sets `attribution_type` from the match type and demotes consortium-partner rows (`FTS_CORDIS / cordis_company`) to `dc_preferred=False` with flag `consortium_partner_attribution`.

### 7.4 Why the order matters

PDF-backed detection runs **before** the heuristic. Without the `dc_preferred` gating in step 3, the heuristic would happily re-flag (or, worse, miss) rows that the PDF parser has already resolved definitively. The gating inverts the default: the heuristic is now a *belt-and-braces* supplement that only acts on residual rows where no document evidence is available.

### 7.5 What the columns mean

The consolidated output adds:

| Column | Values | Purpose |
|---|---|---|
| `dc_preferred` | `True` / `False` | Include in headline EUR totals and charts if `True`. |
| `dc_flag` | pipe-delimited strings | Which overlap pattern was detected. Multiple flags may coexist on one row. |
| `cofinancing_partner_id` | counterpart `source_record_id` | Forensic pointer to the authoritative row. |
| `attribution_type` | `direct` / `consortium_partner` / `contextual` / `inferred` | How the amount is linked to the matched entity. |
| `programme`, `fund` | strings | FTS/KOHESIO programme and fund, attached for dedup decisions. |

Filtering convention:

```python
df_clean  = df[df['dc_preferred']]                                   # headline totals
df_direct = df[df['dc_preferred'] & (df['attribution_type'] == 'direct')]
df_dupes  = df[~df['dc_preferred']]                                  # inspect what was flagged
df_pdf    = df[df['dc_flag'].str.contains('_pdf', na=False)]         # PDF-backed flags only
```

---

## 8. Gross Grant Equivalent (GGE)

Face values are converted to subsidy-equivalent values using the rate table in [consolidation.py:42-47](../src/matching/consolidation.py#L42-L47):

| `financial_instrument_class` | GGE rate |
|---|---|
| `grant`, `subsidy`, `procurement`, `equity`, `debt_relief`, `other` | 100% |
| `mixed` | 50% |
| `loan` (default) | 15% |
| `loan` with `instrument_subtype` containing `repayable` | 90% (`REPAYABLE_ADVANCE_RATE`) |
| `guarantee` | 10% |
| `tax_advantage` | 15% |

Any unknown instrument class produces **`amount_eur_gge = NaN`** for that row, not a fabricated 100% grant equivalent ([consolidation.py](../src/matching/consolidation.py) `_gge_rate_and_source`, 2026-04-13 rewrite). Rows with an unknown instrument still contribute their face value to `amount_eur_face` headline totals, but are transparently excluded from GGE headline totals. A `gge_rate_source` column records `measured` / `measured_repayable` / `unknown` per row so downstream filters can be explicit about which rows carry a measured GGE and which do not. A log summary at the end of Phase 4 lists every unknown instrument class and the total face-value EUR affected — that is the operator's queue to classify the new instrument in `harmonization` or add it to `GGE_RATES`. This change closes audit item H5 and enforces the no-invention principle for the GGE aggregation.

---

## 9. Running the Pipeline

Entry point: [run_pipeline.py](../run_pipeline.py). Stages:

| `--stage` | What runs | Notes |
|---|---|---|
| `harmonize` | Raw → standardized CSVs | Requires `data/raw/`. |
| `enrich` | CORDIS bulk join, EIB promoter scraper | Pre-match enrichment only. |
| `master` | Build `master_dataset.parquet` | Applies `MasterConfig` exclusions. |
| `match` | Full: fuzzy match + post-match enrichment + consolidation + charts | The main user entry point. |
| `enrich-pdf` | Re-run PDF enrichment on an **already-consolidated** CSV | Loads `consolidated_matches.csv`, calls `SACofinParser.enrich_dataframe`, then re-runs `_flag_pdf_cofin_overlaps`, and saves in place. Backs the original up to `*_pre_pdf.csv`. ([run_pipeline.py:346-431](../run_pipeline.py#L346-L431)) |
| `automotive` | Worked example: builds the shipped automotive list and runs `match` with automotive-specific FP patterns | See [examples/automotive/](../examples/automotive/). |
| `all` | `harmonize` → `enrich` → `master` | Does **not** run matching. |

PDF-related CLI flags ([run_pipeline.py:521-549](../run_pipeline.py#L521-L549)):

- `--pdf-enrichment` / `--no-pdf-enrichment` — default **on**. Downloads EC state aid decision PDFs, adds the `sa_cofin_*` columns, and enables PDF-backed duplicate detection across **all** EU fund sources. PDFs are cached in `output_dir/sa_decisions/`. Requires `pip install pdfplumber pymupdf4llm`. Disabling this silently downgrades deduplication to the heuristic fallback alone.
- `--use-llm` — default off. Enables Claude Haiku as a Tier-3 fallback in PDF extraction for PDFs where regex finds no signal (non-English decisions, footnote-fragmented sentences). Requires `pip install anthropic` and `ANTHROPIC_API_KEY`. Cost ~$0.0014/PDF.

> **Library and CLI defaults are now aligned.** Both `--pdf-enrichment` at the CLI and `consolidate(..., run_pdf_enrichment=True)` ([consolidation.py:1171](../src/matching/consolidation.py#L1171)) default to `True`; `stage_match(..., pdf_enrichment=True)` ([run_pipeline.py:170](../run_pipeline.py#L170)) does too. A library caller who does not want PDF enrichment must pass `run_pdf_enrichment=False` explicitly. The start of Phase 2c logs whether enrichment is on or off so the choice is visible in every run log. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) M1.

Auto-download: the first run automatically fetches both `master_dataset.parquet` (~1.7 GB) and `case-data-SA.json` (~638 MB) from the repo's GitHub Releases ([run_pipeline.py:68-120](../run_pipeline.py#L68-L120)). Pass `--download-data` to fetch the master dataset without running any stage.

---

## 10. Outputs

Per match run, written under `data/processed/match_output/{run}/`:

| File | Description |
|---|---|
| `match_log.csv` | Every matched row: source, amount, match type, confidence, scores. |
| `consolidated_matches.csv` | Match log plus GGE, match quality, `dc_preferred`, `dc_flag`, `cofinancing_partner_id`, `attribution_type`, `programme`, `fund`, `sa_cofin_*` (if PDF enrichment ran). Filter on `dc_preferred=True` for headline totals. |
| `group_summary.csv` | Group-level totals (if `parent_groups` configured). |
| `T1…T8_summary_*.csv` | Breakdowns by source, country, instrument, year, fiscal source, top entities. |
| `concentration_metrics.json` | HHI, Top-5 share, Gini at entity and group level. |
| `charts/` | Summary matplotlib charts. |
| `sa_decisions/` | Cached PDFs (persists across runs). |

---

## 11. Quality Assurance

### 11.0 Headline view vs audit view — the two consolidated CSVs

As of the 2026-04-13 rewrite the pipeline publishes **two** consolidated CSVs, not one:

| File | Rows | Use |
|---|---|---|
| `consolidated_matches.csv` | **Headline view.** Rows with `dc_preferred == True` **and** `match_quality` not in `{suspect_description_only, suspect_eib_title, suspect_contextual_generic}`. | Published totals, summary tables (T1–T8, TG1–TG5), concentration metrics (HHI, Top5, Gini), the methodology paper's headline numbers. |
| `consolidated_matches_audit.csv` | **Audit view.** Every matched row including duplicates, suspect matches, and rows flagged by the audit-only heuristic. | Reproducibility, forensic review, reviewer questions of the form "why did you exclude row X?". |

This split replaces the previous single-CSV pattern that summed over `combined` without filtering on `dc_preferred`. Under the earlier behaviour the methodology paper's headline numbers silently ignored every dedup decision the pipeline made; the fix is the single highest-impact correctness change in the 2026-04-13 rewrite (plan audit finding L1). Phase 6 summary tables and Phase 7 concentration metrics are now built against the headline view only; the audit CSV is written alongside so the pipeline remains fully reproducible.

**Which rows are in the audit CSV but not the headline CSV?**

- Rows flagged `dc_preferred = False` by any of the document-grounded dedup steps (`_flag_pdf_cofin_overlaps`, `_dedup_fts_identical_transactions`, `_flag_ipcei_tam_overlap`, `_dedup_same_record_multicountry`, `_add_attribution_type` consortium-partner demotion).
- Rows with `match_quality ∈ {suspect_description_only, suspect_eib_title, suspect_contextual_generic}`.
- Rows flagged by the audit-only `heuristic_flag` **remain in both views** — the heuristic no longer toggles `dc_preferred` and is visible to readers as an extra column only.

This is deliberately conservative. The paper's published number is a *provable lower bound* rather than an optimistic estimate: every exclusion corresponds to a documented reason in `consolidated_matches_audit.csv`, and every "this would also count" claim can be verified by running an SQL-style filter on the audit CSV.

### 11.1 Numerical Integrity

Per-source row counts and EUR totals are verified at each pipeline stage. Any discrepancy between harmonized source files and the assembled master dataset halts the pipeline.

### 11.2 Match-Quality Assessment

Description-matched rows in KOHESIO/FTS/CINEA are checked against `beneficiary_name` (Section 5.4); suspect rows are tagged and are **excluded from the headline CSV** under the 2026-04-13 binary-exclusion rule (plan §6.5). Readers who want to inspect suspect matches should use `consolidated_matches_audit.csv`.

### 11.3 Structural Overlaps Resolved by Default

| Overlap | Stage | Resolution |
|---|---|---|
| Scoreboard aggregate rows | Master build | Excluded (aggregated form of TAM). |
| ESIF programme-level | Master build | Excluded (overlaps with Kohesio project-level). |
| FTS `research_programme_overlap` | Master build | Excluded when RESEARCH is active. |
| Non-EU-27 EIB/EBRD | Master build | Excluded by default. |
| FTS ↔ INNOVFUND | Consolidation (Phase 2b) | `dc_preferred=False` on FTS side when amounts match ≤0.1%. |
| FTS ↔ CINEA | Consolidation (Phase 2b) | `dc_preferred=False` on FTS side when project IDs match. |
| **TAM ↔ any non-TAM source (PDF-backed)** | Consolidation (Phase 2c + 2b) | **Authoritative**. `dc_preferred=False` on TAM when SA decision PDF confirms EU fund co-financing and a counterpart exists (entity, country, year ±2, fund match). |
| TAM ↔ KOHESIO / RRF (audit-only heuristic) | Consolidation (Phase 2b) | Audit-only `heuristic_flag` column; does **not** toggle `dc_preferred`. Flags TAM rows where KOHESIO/TAM amount ratio ∈ [0.01, 1.50] and year within ±2. Visible in `consolidated_matches_audit.csv` only. |
| IPCEI_state_aid ↔ TAM | Consolidation (Phase 2b) | `dc_preferred=False` on TAM when amounts match within 20% and years within ±2. |
| Multi-country KOHESIO artifact | Consolidation (Phase 2b) | First occurrence kept; rest flagged `same_record_multicountry`. |
| FTS-CORDIS consortium partner | Consolidation (Phase 2b) | `dc_preferred=False`; `attribution_type=consortium_partner`. |

### 11.4 Residual Overlap Risks (Intentional)

- **EIB/EBRD loans alongside TAM grants.** A beneficiary appearing in both EIB (loan) and TAM (grant) genuinely received two different instruments; they are not the same money. GGE conversion already discounts loans (15%) and guarantees (10%) relative to grants (100%). Keeping both is correct. *Open audit item*: TAM loans that are themselves co-financed by an IFI loan (EIB/EBRD) are not currently detected — see the audit.
- **TAM ↔ KOHESIO pairs outside the plausibility ratio.** The heuristic only flags ratios in `[0.01, 1.50]`. Pairs outside this band (including TAM rows with corrupt EUR values from failed currency conversions) are not flagged.
- **RRF vs. TAM at the entity level.** RRF is currently measure-level with no beneficiary names, so most TAM↔RRF overlaps cannot be detected at all. `_flag_cofinancing_overlaps` handles RRF as a second pass after KOHESIO so that entity-level RRF data will be deduplicated automatically once it becomes available.
- **Face value vs. GGE.** Face values are upper bounds on total support. Use GGE for cross-instrument comparisons.

---

## 12. Limitations

1. **RRF is absent from the entity-level analysis.** The Recovery and Resilience Facility publishes only measure-level planned allocations. The pipeline's RRF harmonizer reflects this — it writes `beneficiary_name = None` on every row — and so RRF rows cannot be assigned to any company by the entity matcher. **No RRF row reaches `match_log.csv` or `consolidated_matches.csv`.** The source is retained in the master dataset for context only; any downstream chart or summary table filters RRF out by construction. Entity-level RRF would require a harmonizer per Member State recovery-plan portal (e.g. Italy's *Italia Domani*, Spain's *Plan de Recuperación*, France's *France Relance*). Until those are built, a company's headline total in this pipeline **does not include any RRF allocation** regardless of whether the company is a beneficiary of a national recovery plan. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H7.

2. **Pre-2016 ad hoc state-aid coverage is partial and amount extraction is bounded.** The harmonized state-aid layer (TAM) only covers decisions from 2016 onward. For pre-2016 single-company decisions, the pipeline runs a best-effort **ad hoc pre-loader** (`sa_adhoc_parser.py`) that pulls candidate beneficiary names and amounts from the EC DG Competition registry's decision PDFs. The feature is bounded by three constraints that the headline-number reader must understand:
    - **(a) PDF availability**: only ~1,032 of the 14,915 `CaseTypeAH` cases in the registry have an English PDF attached at all — the remaining ~13,000 are pre-2014 registry-metadata-only entries with no document to parse.
    - **(b) Amount-extraction hit rate**: against a random 20-case sample (seed 42) from the 1,032 eligible cases, the regex ladder extracts a valid aid amount from **5/20 (25%)** of PDFs and safely flags another **2/20 (10%)** via the redaction-aware `suspect_fallthrough` tier described below. The remaining 13/20 fall into three unavoidable buckets: (i) 4 cases have no EUR mention in the first 15 pages at all (typically IPCEI Hy2Tech country annexes where only the national-currency figure appears); (ii) 2 cases are denominated in a non-EUR currency (GBP for the UK Green Investment Bank, PLN for Polish gas infrastructure); (iii) 7 cases use aid-amount phrasings that the current 10-pattern ladder does not cover — these are tractable extensions, tracked in Phase B. The 20-case run output is persisted at `data/cache/sa_decisions/adhoc_validation.json` for reproducibility. Methodology-paper readers should interpret the feature as **additive coverage with documented bounds**, not as a complete pre-2016 index.
    - **(c) Redaction-aware confidence (`suspect_fallthrough`)**: DG COMP's non-confidential decision texts replace aid amounts with bracket redaction markers (`EUR [...]` or `[...] million EUR`). A naïve regex ladder would silently fall through the redaction marker and lock onto a later number (a budget figure, an eligible-cost total, a year) — producing a wrong value indistinguishable from a clean extraction. The parser detects redaction markers earlier in the text than a candidate match, discards the numeric value, and tags the row `amount_confidence = 'suspect_fallthrough'`. The row appears in the output as a name-only audit hint and is not counted in headline totals. The 20-case sample validated the redaction detection on 2 real PDFs; see the PIPELINE_AUDIT for the run log.
    Name-only rows (including `not_extracted`, `suspect_fallthrough`, and `not_parseable`) appear in `consolidated_matches_audit.csv` for manual review but do not contribute to published totals. See [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H8.

3. **IPCEI per-company amounts are ranges, not point estimates.** As of the 2026-04-12/13 rewrites (see [research/PIPELINE_AUDIT.md](PIPELINE_AUDIT.md) H6 and plan §6.5), the IPCEI enrichment layer no longer reads a manually-curated estimate CSV and **no longer defaults to a bracket midpoint as the headline amount**. It extracts per-company aid directly from the 12 EC IPCEI decision PDFs shipped under [data/reference/ipcei_decisions/](../data/reference/ipcei_decisions/) using `pdfplumber`'s structured table detection. The EC redacts exact aid figures in public decision text by replacing numbers with bracket ranges such as `[5-10] million EUR`; the parser captures both endpoints and writes them as `amount_eur_low` and `amount_eur_high` on the consolidated row. The row's `amount_eur` is populated with the **low bound** (the conservative estimate), not the midpoint — this ensures headline totals provably understate rather than fabricate point values, consistent with the §6.5 no-invention principle. Downstream consumers that want the midpoint view can compute `(amount_eur_low + amount_eur_high) / 2` themselves; consumers that need to account for the upper bound of the uncertainty can sum `amount_eur_high`. The `amount_confidence` column still takes the values `exact_from_pdf` (a single number was printed; `low == high`), `range_from_pdf` (bracket), or `redacted` (fully redacted cell, dropped from headline totals by the `amount_eur > 0` filter). The `ipcei_ticker` column identifies which IPCEI programme (e.g. `Batteries 1`, `Hy2Tech`) each row belongs to.
2. **Project-title ambiguity in IFI data.** EIB/EBRD raw data use project titles in the beneficiary field. The EIB promoter scraper recovers true names for ~56% of projects; the remainder rely on title-based heuristic extraction with higher error rates.
3. **Fuzzy-matching coverage.** Highly abbreviated names, acronyms, and transliterated names may fall below thresholds. The short-name exact-match guard trades recall for precision.
4. **PDF co-financing evidence confirms presence, not amount.** The SA decision text reliably names *which* EU funds co-finance a measure, but GBER tables are empty for most scheme-level / IPCEI / RRF-backed decisions, so the decision documents rarely permit an EU-share decomposition in EUR on top of the TAM row. This is a known structural limitation of the source data, not a bug. Deduplication uses *presence* (which is all that is needed for the flagging decision); it does not attempt to split the TAM amount into national and EU shares.
5. **`_FUND_ALIASES` coverage.** The alias table now carries 119 entries across 23 canonical fund families, including the Baltic/V4 structural-fund variants (Polish `efrr`, Czech `evropsky fond`, Hungarian `europai regionalis fejlesztesi alap`, Romanian `fedr`, etc.) and the Horizon Europe Pillar-II cluster labels (`Cluster Health`, `Cluster Digital`, `Cluster Climate`, `Cluster Food`, `Cluster Industry`, `Cluster Civil Security`, `Cluster Culture`), plus `Marie Skłodowska-Curie`, `European Research Council`, and the JRC research-overhead budget lines. The lookup is now a **word-boundary regex** (`(?<!\w)(alias)(?!\w)`) rather than a substring check — this fixes a silent false-positive risk where short aliases (`ipa`, `dep`, `cef`, `esf`, `feder`) could match unrelated substrings (`participation`, `developpement`, `federalism`). Unknown fund strings still fall through to the amount-ratio check.
6. **TAM ↔ IFI loan co-financing not yet detected.** The pipeline currently deduplicates TAM state aid against EU-fund grants, but not against IFI loans (EIB/EBRD) that may co-finance the same underlying project. Listed as an open audit item.
7. **Dedup thresholds are not published.** The 0.01-1.50 amount ratio and ±2 year window used by the heuristic have never been precision/recall-validated against a labelled sample. In the 2026-04-13 rewrite the heuristic was demoted to an audit-only `heuristic_flag` column (see §7 above), so this open item no longer affects headline numbers: the heuristic cannot take a row out of the headline view under any threshold. Empirical tuning is still scheduled for the planned gold-set harness and is the entry point for re-promoting the heuristic if a large-scale validation ever supports it.
8. **PDF downloader is synchronous.** Phase 2c downloads PDFs inline with consolidation. An EC website outage delays (though does not block — partial failures are logged and skipped) the run. An asynchronous retry layer is listed in the audit.
9. **GGE unknown-instrument handling is transparent.** Unknown `financial_instrument_class` values produce `amount_eur_gge = NaN` (excluded from GGE totals) rather than defaulting to 100% grant. See §8 above. This closes audit H5.
10. **Temporal snapshots.** Company reference lists reflect a single point in time. Corporate restructuring, M&A, and name changes after list extraction may cause missed matches for historical records.
11. **TAM supplement completeness.** National supplements cover four Member States; equivalent granular data is not available for all 27.
12. **Original column preservation.** For high-volume TAM supplements, original source columns are slimmed to 3–4 essential fields (measure ID, form code, NIF). Full original metadata remains in the raw source files.

---

## Appendix: Pipeline Architecture

```
data/raw/                             [1] HARMONIZATION
  12 source files                -->  src/harmonization/
                                        18 modules → 36-col schema
                                             │
                                             v
                                      [2] PRE-MATCH ENRICHMENT
                                      src/enrichment/cordis_enrichment.py
                                      src/enrichment/eib_promoter_scraper.py
                                             │
                                             v
data/processed/                       [3] MASTER BUILD
  master_dataset.parquet         <--  src/master/builder.py
                                        ~27M rows, ~25.7M primary
                                        flag-based MasterConfig exclusions
                                             │
                                             v
                                      [4] ENTITY MATCHING
                                      src/matching/generic_matcher.py
                                        Layer A: exact + fuzzy (rapidfuzz)
                                        Layer B: contextual regex on descriptions
                                        Layer B+: EIB/EBRD title extraction
                                             │
                                             v
                                      [5] POST-MATCH ENRICHMENT (auto)
                                      src/enrichment/ — FTS-CORDIS, ETS,
                                      IPCEI, FTS deep mining, HV forensics
                                             │
                                             v
                                      [6] CONSOLIDATION
                                      src/matching/consolidation.py
                                        Phase 2a: integrate enrichment CSVs
                                        Phase 2c: SA PDF enrichment
                                          ├ case-data-SA.json lookup
                                          ├ Tier 0 GBER table parse
                                          ├ Tier 1 confirmed prose regex
                                          ├ Tier 2 conditional prose regex
                                          └ Tier 3 LLM fallback (optional)
                                        Phase 2b: cross-source dedup
                                          1. FTS↔INNOVFUND / FTS↔CINEA
                                          2. PDF-backed (authoritative)
                                          3. heuristic fallback (gated)
                                          4. multi-country artifact
                                          5. IPCEI↔TAM
                                        Phases 3-8: match quality, GGE,
                                        group rollup, summary tables,
                                        concentration, output
                                             │
                                             v
data/processed/match_output/{run}/
  match_log.csv, consolidated_matches.csv,
  T1-T8_summary_*.csv, concentration_metrics.json,
  charts/, sa_decisions/ (PDF cache)
```

All intermediate outputs are deterministic given fixed inputs and a fixed `MatchConfig`.
