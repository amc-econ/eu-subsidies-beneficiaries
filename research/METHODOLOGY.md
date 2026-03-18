# Methodology: EU Subsidies Beneficiary Analysis

## 1. Data Sources and Collection

This analysis draws on 12 publicly available EU data sources, comprising approximately 27 million rows of subsidy, loan, grant, and contract records spanning 2000--2024. Each source was retrieved in its original format and stored unmodified prior to harmonization.

### 1.1 Primary Sources

**Transparency Award Module (TAM).** The European Commission's state aid transparency database, containing 2.1 million individual aid award decisions reported by Member States for the period 2000--2023. Records include beneficiary name, NACE sector classification, granting authority, aid instrument, and awarded amount in EUR.

**TAM National Supplements.** Four Member State portals provide granular award-level data not fully captured in the central TAM:

- *Spain (BDNS)*: ~4.72 million rows from the Base de Datos Nacional de Subvenciones, retrieved via paginated API.
- *Poland (SUDOP)*: ~15.7 million rows from the System Udostępniania Danych o Pomocy Publicznej.
- *Romania*: National state aid register supplement.
- *Slovenia*: National state aid register supplement.

**Financial Transparency System (FTS).** ~1.4 million rows of EU budget expenditure (contracts and grants), sourced from 18 yearly Excel files (2007--2024) published by the European Commission. Amount column: "Beneficiary's contracted amount (EUR)". No NACE codes are available.

**Kohesio.** ~1.9 million EU Cohesion Policy project records from the Commission's unified cohesion data platform.

**European Investment Bank (EIB).** ~26,000 loan and investment records. The `beneficiary_name` field contains the project title rather than the borrower's name, necessitating dedicated enrichment (Section 3).

**European Bank for Reconstruction and Development (EBRD).** ~6,000 project records. As with EIB, the beneficiary field contains project titles.

**European Structural and Investment Funds (ESIF).** Two programming periods are treated as separate source packages: ESIF 2014--2020 and ESIF 2021--2027.

**Climate, Infrastructure and Environment Executive Agency (CINEA).** Project-level grant and contract data from CINEA-managed programmes.

**Recovery and Resilience Facility (RRF).** Measure-level expenditure data. This source contains no beneficiary names---all records are aggregated at the national reform/investment measure level. Included for completeness but excluded from entity-level matching.

**RESEARCH (CORDIS).** Horizon 2020 and Horizon Europe project participations sourced from the Community Research and Development Information Service.

**EU State Aid Scoreboard.** Aggregated Member State aid expenditure by instrument type and objective. Used as contextual reference for Gross Grant Equivalent conversion rates; excluded from primary record counts via flag-based exclusion.

### 1.2 Reference Lists

The following are used in the automotive worked example (`examples/automotive/`):

- **ORBIS Top 1000**: Bureau van Dijk's top 1,000 automotive companies by revenue (sheet "Results", column "Company name Latin alphabet").
- **EV Volumes**: ~329 cleaned company names active in electric vehicle manufacturing or supply.

---

## 2. Harmonization

All 12 sources are standardized to a canonical 36-column schema (Schema v2) via source-specific harmonization modules orchestrated by `harmonize_all.py`. The schema covers entity resolution, flow taxonomy, fiscal classification, and flag-based exclusions.

### 2.1 Entity Name Cleaning

Each raw beneficiary name undergoes the following normalization pipeline:

1. **Legal suffix stripping**: Trailing corporate designators (GmbH, S.A., Ltd., SRL, AB, etc.) are removed. Leading tokens are preserved to avoid false conflation (e.g., "AB Volvo" retains "AB" since it is a leading token, not a trailing legal suffix). The suffix list covers all principal forms across EU member state legal systems, including Portuguese (*Unipessoal Lda*), Polish (*Sp. z o.o.*, abbreviated *Zoo*), Spanish (*Unipersonal*), and Italian (*Società Unipersonale*) in addition to Germanic, Romance, and Nordic forms. Tokens classified as legal suffixes are also added to the trivial-token blocklist to prevent legal form fragments from driving fuzzy-match scores.
2. **Whitespace normalization**: Consecutive whitespace collapsed; leading/trailing whitespace trimmed.
3. **Case normalization**: All names lowercased for matching purposes; original casing preserved in a separate column.
4. **Parenthetical content**: Stripped before fuzzy matching to reduce noise from subsidiary descriptors.

### 2.2 Entity Resolution

Deterministic entity IDs are generated from the tuple (`entity_name_clean`, `country_code`), enabling consistent cross-source identification.

### 2.3 Currency Conversion

Records denominated in non-EUR currencies are converted using ECB reference exchange rates (`ecb_fx_rates.py`). The applicable rate is selected based on the award or commitment date.

### 2.4 Cross-Source Deduplication

Several EU databases capture the same underlying financial flow from different angles. The consolidation phase (Phase 2b in `consolidation.py`) applies structural deduplication across five confirmed overlap patterns:

| Source pair | Overlap type | Resolution |
|-------------|-------------|------------|
| FTS + INNOVFUND | FTS records the budget outflow; INNOVFUND records the award decision. Same grant, two rows. | FTS row marked `dc_preferred=False`; INNOVFUND row is authoritative. Match criterion: same entity, amount within 0.1%. |
| FTS + CINEA | FTS records payment; CINEA programme DB records the same project. | FTS row marked `dc_preferred=False` when project IDs match exactly. |
| TAM + KOHESIO | TAM = total notified state aid (EU share + national share). KOHESIO = EU co-financing share only. Same investment, different angles. | TAM row marked `dc_preferred=False` when a KOHESIO record exists for the same entity and country within ±2 years and the KOHESIO amount is 1–150% of the TAM amount. |
| TAM + RRF | Recovery grants sometimes notified as state aid in TAM. | Same logic as TAM + KOHESIO. |
| IPCEI_state_aid + TAM | IPCEI project aid receives an SA.XXXXX reference and appears in both the IPCEI reference database and TAM. | TAM row marked `dc_preferred=False` when amounts match within 20% and years are within ±2. IPCEI row is authoritative. |

FTS-CORDIS enrichment rows where `match_type = fts_cordis_cordis_company` represent consortium partner allocations attributed to the matched entity, not money the entity directly received. These are assigned `attribution_type = consortium_partner` and `dc_preferred = False`.

No rows are removed. All data is preserved in `consolidated_matches.csv`. The `dc_preferred` column (`True`/`False`) marks which rows to include in headline EUR totals and charts. The `dc_flag` column records which overlap pattern was detected (pipe-delimited when multiple apply). Charts and summary tables default to `dc_preferred = True` rows.

### 2.5 Flag-Based Exclusion

No rows are deleted from the harmonized dataset. Instead, rows excluded from primary analysis carry `is_primary_record = False` and a human-readable `exclude_reason`. Exclusion criteria are defined in `MasterConfig` and include:

- Scoreboard aggregate rows (no beneficiary-level detail).
- Duplicate records identified during overlap detection.
- Records with missing or zero amounts where required.

---

## 3. Pre-Matching Enrichment

Two enrichment steps are applied before entity matching to improve beneficiary identification in sources with incomplete name data.

### 3.1 CORDIS Organization Join

For RESEARCH-sourced records, a bulk join against the CORDIS organization dataset links project participation IDs to structured organization names and metadata, achieving an 80.3% match rate for Horizon Europe/2020 projects. An API-based backfill targets remaining unmatched projects.

### 3.2 EIB Promoter Scraping

Since the EIB raw data lacks true beneficiary names, a sitemap-based scraper retrieves promoter information from EIB project pages. Of pages scraped, 56% contain usable promoter names, with a 98.2% title-to-record match rate against the harmonized EIB dataset.

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

**Description regex.** For rows unmatched by Layer A, the pipeline applies compiled regex patterns against project description and metadata fields. This captures cases where the beneficiary name is absent or generic but the project description contains a company from the reference list.

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

Matched entities are rolled up to corporate group level using a curated parent-groups dictionary. Legal suffixes are stripped before lookup; overlapping group names (e.g., "Volvo" vs. "Volvo Car" vs. "AB Volvo") are resolved by longest-match-first.

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

### 8.2 Cross-Source Deduplication and Overlap Flagging

Confirmed cross-source overlaps are resolved during consolidation (Phase 2b) using the `dc_preferred` and `dc_flag` columns described in Section 2.4. Rows with `dc_preferred = False` are excluded from headline totals and charts but retained in the dataset for research use. Summary statistics report the EUR volume at `dc_preferred = False` to bound any residual overlap risk from cases falling outside the matching criteria.

### 8.3 Structural Audit

A post-matching structural audit generates a 7-section diagnostic covering source composition, match confidence distribution, group concentration (HHI, Gini, Top-5 share), and instrument mix.

### 8.4 Structural Overlaps Resolved by Default

The following overlaps are resolved automatically. Master dataset exclusions apply at construction time; consolidation-phase deduplication applies at post-match time via `dc_preferred`.

| Overlap | Stage | Resolution |
|---------|-------|-----------|
| `SCOREBOARD` | Master build | Excluded — aggregated form of the same state aid data captured in TAM |
| `ESIF` programme-level | Master build | Excluded — overlaps with Kohesio project-level records; Kohesio is preferred |
| FTS rows flagged `research_programme_overlap` | Master build | Excluded when RESEARCH source is active |
| Non-EU EIB/EBRD rows | Master build | Excluded by default |
| FTS rows with Innovation Fund programme | Consolidation | `dc_preferred=False` when matching INNOVFUND row exists (amount within 0.1%) |
| FTS rows with CEF / LIFE / EMFAF programme | Consolidation | `dc_preferred=False` when matching CINEA row shares project ID |
| TAM rows where KOHESIO covers same entity + country | Consolidation | `dc_preferred=False`; KOHESIO amount 1–150% of TAM, year within ±2 |
| TAM rows matching IPCEI state aid decision | Consolidation | `dc_preferred=False`; amount within 20%, year within ±2 |
| FTS-CORDIS consortium partner rows | Consolidation | `dc_preferred=False`; `attribution_type=consortium_partner` |

Master build exclusions are recorded in `exclude_reason` and visible in the master dataset. Consolidation deduplication is recorded in `dc_flag` and visible in `consolidated_matches.csv`.

### 8.5 Residual Overlap Risks

The following cases are intentionally not deduplicated:

- **EIB / EBRD loans alongside grants**: A beneficiary appearing in both EIB (loan) and TAM (grant) received two distinct financial instruments. These are not the same money — loans are repayable; GGE conversion already applies lower rates (15% for loans, 10% for guarantees vs 100% for grants). Including both is correct.
- **TAM / KOHESIO outside matching criteria**: TAM rows are only flagged if a KOHESIO counterpart meets the plausibility ratio (KOHESIO = 1–150% of TAM) within a ±2 year window. Pairs outside these bounds — including cases where a TAM entry has an astronomically corrupt EUR value from a failed currency conversion — are not flagged.
- **RRF vs. TAM (structural)**: RRF records in this dataset are at the national measure level with no beneficiary names. Once entity-level RRF data becomes available, overlap logic applies automatically (the `_flag_cofinancing_overlaps` function already handles RRF as a second pass after KOHESIO).
- **Face value vs. GGE**: Face values are upper bounds on total support. Use GGE for cross-instrument comparisons.

---

## 9. Limitations

1. **RRF beneficiary gap.** Recovery and Resilience Facility data is published at the measure level only, with no beneficiary names. Entity-level analysis of RRF expenditure would require scraping national RRF portals, which is beyond the scope of this dataset.

2. **Project-title ambiguity.** EIB and EBRD raw data use project titles in the beneficiary field. While the EIB promoter scraper recovers true borrower names for 56% of projects, the remainder rely on title-based heuristic extraction with higher error rates.

3. **Fuzzy matching coverage.** Highly abbreviated entity names, acronyms, or names transliterated across scripts may fall below matching thresholds. The short-name exact-match guard trades recall for precision in these cases.

4. **Overlap resolution.** Cross-source overlaps (notably TAM-Kohesio for Structural Funds, and potential TAM-ESIF overlaps) are flagged but not automatically deduplicated. Summary statistics report overlap candidate counts to bound the risk, but true double-counting rates are unknown without manual case-by-case review.

5. **Temporal snapshots.** Company reference lists reflect a single point in time. Corporate restructuring, M&A activity, and name changes occurring after list extraction may cause missed matches for historical records.

6. **TAM supplement completeness.** National supplements (Spain, Poland, Romania, Slovenia) improve coverage for those Member States but equivalent granular data is not available for all 27 EU members. Cross-country comparisons should account for this asymmetry.

7. **Original column preservation.** For high-volume TAM supplements (Spain: 4.7M rows, Poland: 15.7M rows), original source columns are slimmed to 3--4 essential fields (measure ID, form code, NIF) to keep file sizes tractable. Full original metadata is available in the raw source files.

---

## Appendix: Pipeline Architecture

```
data/raw/                     Raw source files (unmodified)
    |
    v  src/harmonization/
data/processed/               Standardized CSVs (36-col schema)
    |
    v  src/master/builder.py
data/processed/master_dataset.parquet    ~27M rows
    |
    v  src/matching/generic_matcher.py
data/processed/match_output/{run}/       match_log.csv
    |
    v  src/matching/consolidation.py
data/processed/match_output/{run}/       consolidated_matches.csv
                                         charts/
                                         T1–T8_summary_*.csv
```

All intermediate outputs are deterministic given fixed inputs.
