# Methodology: EU Subsidies Beneficiary Analysis

## 1. Data Sources and Collection

This analysis draws on 12 publicly available EU data sources, comprising approximately 27 million rows of subsidy, loan, grant, and contract records spanning 2000--2024. Each source was retrieved in its original format and stored unmodified prior to harmonization.

### 1.1 Primary Sources

**Transparency Award Module (TAM).** The European Commission's state aid transparency database, containing 2.1 million individual aid award decisions reported by Member States for the period 2000--2023. Records include beneficiary name, NACE sector classification, granting authority, aid instrument, and awarded amount in EUR. The raw file is tab-separated with latin-1 encoding. Compound NACE descriptions (semicolon-delimited) are split during harmonization.

**TAM National Supplements.** Four Member State portals provide granular award-level data not fully captured in the central TAM:

- *Spain (BDNS)*: ~4.72 million rows from the Base de Datos Nacional de Subvenciones, retrieved via paginated API (pageSize=1000, ~6,300 pages). Cached locally for reproducibility.
- *Poland (SUDOP)*: ~15.7 million rows from the System Udostępniania Danych o Pomocy Publicznej.
- *Romania*: National state aid register supplement.
- *Slovenia*: National state aid register supplement.

**Financial Transparency System (FTS).** ~1.4 million rows of EU budget expenditure (contracts and grants), sourced from 18 yearly Excel files (2007--2024) published by the European Commission. Amount column: "Beneficiary's contracted amount (EUR)". No NACE codes are available.

**Kohesio.** ~1.9 million EU Cohesion Policy project records from the Commission's unified cohesion data platform.

**European Investment Bank (EIB).** ~26,000 loan and investment records. The raw dataset (EIB.xlsx) contains no native project identifiers; sequential row indices serve as `source_record_id`. Notably, the `beneficiary_name` field in this source contains the project title rather than the borrower's name, necessitating dedicated enrichment (Section 3).

**European Bank for Reconstruction and Development (EBRD).** ~6,000 project records. As with EIB, the beneficiary field contains project titles.

**European Structural and Investment Funds (ESIF).** Two programming periods are treated as separate source packages: ESIF 2014--2020 and ESIF 2021--2027.

**Climate, Infrastructure and Environment Executive Agency (CINEA).** Project-level grant and contract data from CINEA-managed programmes.

**Recovery and Resilience Facility (RRF).** Measure-level expenditure data. This source contains no beneficiary names---all records are aggregated at the national reform/investment measure level. Included for completeness but excluded from entity-level matching.

**RESEARCH (CORDIS).** Horizon 2020 and Horizon Europe project participations sourced from the Community Research and Development Information Service.

**EU State Aid Scoreboard.** Aggregated Member State aid expenditure by instrument type and objective. Used as contextual reference for Gross Grant Equivalent conversion rates; excluded from primary record counts via flag-based exclusion.

### 1.2 Reference Lists

- **ORBIS Top 1000**: Bureau van Dijk's top 1,000 automotive companies by revenue (sheet "Results", column "Company name Latin alphabet").
- **EV Volumes**: ~329 cleaned company names active in electric vehicle manufacturing or supply.

---

## 2. Harmonization

All 12 sources are standardized to a canonical 36-column schema (Schema v2) via source-specific harmonization modules orchestrated by `harmonize_all.py`. The schema covers entity resolution, flow taxonomy, fiscal classification, and flag-based exclusions.

### 2.1 Entity Name Cleaning

Each raw beneficiary name undergoes the following normalization pipeline:

1. **Legal suffix stripping**: Trailing corporate designators (GmbH, S.A., Ltd., SRL, AB, etc.) are removed. Leading tokens are preserved to avoid false conflation (e.g., "AB Volvo" retains "AB" since it is a leading token, not a trailing legal suffix).
2. **Whitespace normalization**: Consecutive whitespace collapsed; leading/trailing whitespace trimmed.
3. **Case normalization**: All names lowercased for matching purposes; original casing preserved in a separate column.
4. **Parenthetical content**: Stripped before fuzzy matching to reduce noise from subsidiary descriptors.

### 2.2 Entity Resolution

Deterministic entity IDs are generated from the tuple (`entity_name_clean`, `country_code`). This ensures that the same legal entity appearing in multiple sources receives a consistent identifier prior to matching against reference lists.

### 2.3 Currency Conversion

Records denominated in non-EUR currencies are converted using ECB reference exchange rates (`ecb_fx_rates.py`). The applicable rate is selected based on the award or commitment date.

### 2.4 Overlap Detection

Several EU data sources report overlapping instruments. In particular, Structural Funds expenditure may appear in both TAM (as notified state aid) and Kohesio (as cohesion policy). Potential overlaps are flagged at the row level using source-pair heuristics (matching beneficiary, country, approximate amount, and time window). Overlap flags are advisory; no automatic deduplication is performed.

### 2.5 Flag-Based Exclusion

No rows are deleted from the harmonized dataset. Instead, rows excluded from primary analysis carry `is_primary_record = False` and a human-readable `exclude_reason`. Exclusion criteria are defined in `MasterConfig` and include:

- Scoreboard aggregate rows (no beneficiary-level detail).
- Duplicate records identified during overlap detection.
- Records with missing or zero amounts where required.

---

## 3. Pre-Matching Enrichment

Two enrichment steps are applied before entity matching to improve beneficiary identification in sources with incomplete name data.

### 3.1 CORDIS Organization Join

For RESEARCH-sourced records, a bulk join against the CORDIS organization dataset links project participation IDs to structured organization names and metadata. This achieves an 80.3% match rate for Horizon Europe/2020 projects. An API-based backfill (`cordis_api_backfill.py`) targets the remaining 10,754 unmatched projects, achieving partial coverage (41%).

### 3.2 EIB Promoter Scraping

Since the EIB raw data lacks true beneficiary names, a sitemap-based scraper (`eib_promoter_scraper.py`) retrieves promoter information from 16,869 EIB project pages. Promoter data is extracted from the HTML structure (`eib-list__row--body` elements). Of the pages scraped, 9,353 (56%) contain usable promoter names, achieving a 98.2% title-to-record match rate against the harmonized EIB dataset.

---

## 4. Master Dataset Construction

The master dataset is assembled by `master_builder.py`, which:

1. Concatenates all standardized source files from the harmonization output directory.
2. Applies `MasterConfig` flag-based exclusions.
3. Writes `master_dataset.csv` and canonical reference copies to `data_master/`.

**Result**: ~27 million total rows, of which ~25.7 million are primary records and ~661,000 are excluded (flagged, not deleted).

A numerical integrity gate verifies that per-source row counts and EUR totals match between the harmonized inputs and the assembled master.

---

## 5. Entity Matching

Entity matching identifies which rows in the master dataset correspond to companies on the user-supplied reference list. The matching pipeline (`generic_matcher.py`) uses a two-layer approach with a deduplication optimization.

### 5.1 Deduplication Optimization

Rather than matching all ~27 million rows individually, the pipeline extracts ~920,000 unique `entity_name_clean` values, performs matching on this reduced set, and joins results back to the full dataset. This reduces runtime by approximately 97%.

### 5.2 Layer A: Name Matching

**Exact match.** Case-insensitive string equality between the cleaned beneficiary name and the cleaned reference name.

**Fuzzy match.** For non-exact candidates, the pipeline applies `rapidfuzz.fuzz.token_set_ratio` with the following controls:

- *Trivial-token filtering*: Common tokens with no discriminative value (the, of, und, de, sa, ltd, gmbh, etc.) are excluded from the similarity computation to reduce false positives from legal boilerplate---particularly prevalent in Italian and German entity names.
- *Token inverted index*: A pre-built index maps content tokens to reference list entries, enabling rapid candidate pre-filtering without exhaustive pairwise comparison.
- *Thresholds*: Exact (100), High (85--99), Medium (75--84). Matches below 75 are discarded.
- *Short-name guard*: Entity names of 5 characters or fewer are matched by exact equality only, as fuzzy scores are unreliable at short string lengths.

### 5.3 Layer B: Contextual Matching

**Description regex.** For rows unmatched by Layer A, the pipeline applies compiled regex patterns against project description and metadata fields. This captures cases where the beneficiary name is absent or generic but the project description references a known automotive company.

A `match_quality` flag distinguishes contextual matches from name-based matches, as description-field matching carries higher false positive risk (e.g., "Samsung tablet" matching Samsung SDI, "Michelin star" matching Michelin).

**EIB/EBRD title extraction (Layer B+).** Since EIB and EBRD records use project titles as beneficiary names, a dedicated extraction step parses company names from title strings using domain-specific patterns.

### 5.4 Confidence Classification

Each match is assigned a confidence tier:

| Tier | Criteria |
|------|----------|
| High | Exact name match, or fuzzy score >= 85 with corroborating country/sector |
| Medium | Fuzzy score 75--84, or high-score match without corroboration |
| Lower | Contextual-only match, or short-name fuzzy match |

### 5.5 False Positive Controls

- **Contextual blocklists**: Known false-positive entity pairs are explicitly excluded.
- **Beneficiary regex patterns**: Source-specific patterns filter out institutional names misidentified as corporate entities.
- **Known FP pairs**: A curated list of entity-reference pairs confirmed as false positives during manual review.

---

## 6. Group Consolidation

Matched entities are rolled up to corporate group level using a curated `PARENT_GROUPS` dictionary that maps entity names to ultimate parent groups.

- `_clean_for_group()` strips trailing legal suffixes only (preserving leading tokens such as "AB" in "AB Volvo").
- `assign_parent_group()` applies longest-match-first logic to handle overlapping group names (e.g., "Volvo" vs. "Volvo Car" vs. "AB Volvo").
- Major groups include Stellantis (28 matched entities), VW Group (33), Mercedes-Benz (8), and Renault (8).

---

## 7. Gross Grant Equivalent (GGE)

To enable cross-instrument comparison, face values are converted to Gross Grant Equivalent using conversion rates derived from the EU State Aid Scoreboard methodology:

| Instrument | GGE Rate |
|-----------|----------|
| Grants | 100% |
| Loans | 15% |
| Guarantees | 10% |
| Equity participations | 100% |
| Tax advantages | 15% |

GGE values represent the estimated subsidy content of each instrument and are reported alongside face values throughout the analysis.

---

## 8. Quality Assurance

### 8.1 Numerical Integrity

Per-source row counts and EUR totals are verified at each pipeline stage. Any discrepancy between harmonized source files and the assembled master dataset halts the pipeline.

### 8.2 Overlap Risk Flagging

Rows identified as potential cross-source duplicates are flagged but not removed. The overlap candidate count is reported in all summary statistics to bound the maximum overcount risk.

### 8.3 Structural Audit

A post-matching structural audit (`structural_audit.py`) generates a 7-section diagnostic covering source composition, match confidence distribution, group concentration (HHI, Gini, Top-5 share), and instrument mix.

---

## 9. Limitations

1. **RRF beneficiary gap.** Recovery and Resilience Facility data is published at the measure level only, with no beneficiary names. Entity-level analysis of RRF expenditure would require scraping national RRF portals, which is beyond the scope of this dataset.

2. **Project-title ambiguity.** EIB and EBRD raw data use project titles in the beneficiary field. While the EIB promoter scraper recovers true borrower names for 56% of projects, the remainder rely on title-based heuristic extraction with higher error rates.

3. **Fuzzy matching coverage.** Highly abbreviated entity names, acronyms, or names transliterated across scripts may fall below matching thresholds. The short-name exact-match guard trades recall for precision in these cases.

4. **Overlap resolution.** Cross-source overlaps (notably TAM-Kohesio for Structural Funds, and potential TAM-ESIF overlaps) are flagged but not automatically deduplicated. Summary statistics report overlap candidate counts to bound the risk, but true double-counting rates are unknown without manual case-by-case review.

5. **Temporal snapshots.** Company reference lists (ORBIS, EV Volumes) reflect a single point in time. Corporate restructuring, M&A activity, and name changes occurring after list extraction may cause missed matches for historical records.

6. **TAM supplement completeness.** National supplements (Spain, Poland, Romania, Slovenia) improve coverage for those Member States but equivalent granular data is not available for all 27 EU members. Cross-country comparisons should account for this asymmetry.

7. **Original column preservation.** For high-volume TAM supplements (Spain: 4.7M rows, Poland: 15.7M rows), original source columns are slimmed to 3--4 essential fields (measure ID, form code, NIF) to keep file sizes tractable. Full original metadata is available in the raw source files.

---

---

## 10. Overlap, Double-Counting, and Deduplication

### 10.1 Flag-Based Exclusion

No rows are deleted from the master dataset. Every record carries two fields: `is_primary_record` (boolean) and `exclude_reason` (string). Headline totals always filter to `is_primary_record == True`. Exclusion decisions are transparent, auditable, and reversible — the full record is preserved.

### 10.2 Structural Overlaps Resolved by Default

The following overlaps are resolved at master dataset construction time via `MasterConfig` defaults:

| Overlap | Resolution |
|---------|-----------|
| `SCOREBOARD` | Excluded — aggregated form of the same state aid data captured in TAM |
| `ESIF` programme-level | Excluded — overlaps with Kohesio project-level records; Kohesio is preferred |
| FTS rows flagged `research_programme_overlap` | Excluded when RESEARCH source is active |
| Non-EU EIB/EBRD rows | Excluded by default (`eu27_only_loans`) |

All exclusions are recorded in `exclude_reason` and visible in the master dataset.

### 10.3 Post-Match Deduplication

After the matching phase, enrichment outputs (FTS-CORDIS bridge, ETS, IPCEI, EIB promoter) are integrated against core match results using `source_record_id` deduplication. An enrichment row that duplicates a directly-matched record is dropped before consolidation. This is logged and auditable in `enrichment_stats.json`.

### 10.4 Residual Risks

The following overlaps are **not automatically resolved** and are disclosed here:

- **RRF vs. TAM**: RRF records are planning-level allocations, not payment records. They are not deduplicated against TAM by design. If future TAM entries cover the same instruments, overlap is possible.
- **Multi-source aggregation is not double-counting**: A beneficiary appearing in both Kohesio (cohesion grant) and EIB (loan) received two distinct instruments. Including both is correct. GGE conversion normalizes cross-instrument comparison.
- **Face value vs. GGE**: Face values should be treated as upper bounds on total support. Use GGE for cross-instrument and cross-source comparisons.

---

## Appendix: Pipeline Architecture

```
data/raw/                     Raw source files (unmodified)
    |
    v  src/data_cleaning/harmonization/
data/processed/               Standardized CSVs (36-col schema)
    |
    v  src/data_cleaning/master/builder.py
data/processed/master_dataset.parquet    ~27M rows
    |
    v  src/data_extraction/matching/generic_matcher.py
data/processed/match_output/{run}/       match_log.csv
    |
    v  src/data_extraction/matching/consolidation.py
data/processed/match_output/{run}/       consolidated_matches.csv
                                         charts/
                                         T1–T8_summary_*.csv
```

All intermediate outputs are deterministic given fixed inputs.
