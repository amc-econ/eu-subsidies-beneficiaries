# EU Subsidies Beneficiary Analysis

Match any list of companies against ~27 million records of European public financial support, grouped into three types:

1. **State aid** — national subsidies notified to the European Commission and published in the EC Transparency Award Module and Member State supplements.
2. **EU funds** — money paid out directly from the EU budget (cohesion policy, research and innovation, CEF/LIFE, Innovation Fund, etc.).
3. **IFIs** — loans and guarantees from European international financial institutions (European Investment Bank and European Bank for Reconstruction and Development).

For a user of this repository these are the three categories of public financial flows you need to understand. The pipeline's internal data-source names (TAM, FTS, Kohesio, CORDIS, EIB, etc.) exist for operational plumbing only and appear in the methodology document when technical detail is needed.

---

## Quick Start

```bash
git clone https://github.com/amc-econ/eu-subsidies-beneficiaries.git
cd eu-subsidies-beneficiaries
pip install -r requirements.txt
python run_pipeline.py --stage match --company-list my_companies.csv
```

> The master dataset (~1.7 GB) and the EC state aid case registry `case-data-SA.json` (~638 MB) are downloaded automatically on first run. To fetch the master dataset in advance without running a match: `python run_pipeline.py --download-data`

Results land in `data/processed/match_output/`. Expect ~30 minutes on a standard laptop.

---

## Your Company List

Create a CSV with a `company_name` column:

```csv
company_name
Volkswagen AG
Airbus SE
BASF SE
```

Add an optional `country` column (ISO 2-letter codes) to reduce false positives:

```csv
company_name,country
Volkswagen AG,DE
Airbus SE,FR
BASF SE,DE
```

**Using Excel?** Open your file → File → Save As → choose "CSV UTF-8 (.csv)".

A working 10-company sample is included at [`my_companies.csv`](my_companies.csv). Run it as-is to verify your setup.

---

## Run

```bash
python run_pipeline.py --stage match --company-list path/to/your_companies.csv
```

The pipeline automatically runs in sequence:
1. **Entity matching** — exact + fuzzy name match against all ~27M rows (rapidfuzz, two-layer: a direct name-matching layer and a contextual text-scanning layer for rows whose beneficiary name is hidden inside project descriptions or project titles, such as many IFI loan records).
2. **Post-match enrichment** — additional passes that close coverage gaps: a research-grant consortium bridge, an EU ETS free-allocation join, an IPCEI participant look-up extracted from decision PDFs, deeper text-mining of EU-fund payment descriptions, a forensic sweep of high-value unmatched rows, and an **ad hoc state-aid decision pre-loader** that surfaces pre-2016 single-company decisions missing from the harmonized state-aid layer. See [research/METHODOLOGY.md](research/METHODOLOGY.md) §6 for the detail.
3. **State-aid decision PDF enrichment** — for every matched state-aid row, the pipeline downloads the EC decision PDF (cached under `data/cache/sa_decisions/`) and extracts EU-fund co-financing evidence using a GBER-table parser, regex on confirmed/conditional prose, and an optional LLM fallback. This produces the document-grounded signal that drives cross-source deduplication between **state aid** and **EU funds**. On by default; disable with `--no-pdf-enrichment`.
4. **Consolidation** — Face-value + Gross-Grant-Equivalent conversion with parallel `amount_eur_face` and `amount_eur_gge` columns, document-grounded cross-source deduplication (state-aid decision PDFs name the EU fund co-financing a measure, and matched TAM↔non-TAM pairs are flagged `dc_preferred=False`), **headline-vs-audit view split**: summary tables and concentration metrics run on the headline view only, while the audit CSV preserves every row including dedup-flagged / suspect / anonymised-sentinel rows for reviewer inspection. The amount-ratio heuristic is **audit-only** (`heuristic_flag` column) and never affects published totals. Unknown financial instrument classes produce `amount_eur_gge = NaN` rather than a silent 100% grant default. IPCEI bracket-redacted amounts emit `amount_eur_low` / `amount_eur_high` pairs and use the low bound as the conservative headline.
5. **Output** — **Two** consolidated CSVs: `consolidated_matches.csv` is the headline view (every row contributes to published totals); `consolidated_matches_audit.csv` is the full audit view including duplicates, suspect matches, and anonymised-sentinel rows for forensic review. Plus six summary charts (annual totals, by multiannual financial framework period, by country, top groups by GGE, top groups by source type, non-EU groups) computed on the headline view.

**PDF enrichment flags:**

- `--pdf-enrichment` / `--no-pdf-enrichment` — On by default. Downloads state-aid decision PDFs for matched state-aid rows, caches them in the shared `data/cache/sa_decisions/` directory so they accumulate across runs, and populates `sa_cofin_fund`, `sa_cofin_level`, `sa_cofin_evidence` columns on the consolidated output. Disabling it downgrades cross-source deduplication to a heuristic amount-ratio fallback alone. Requires `pip install pdfplumber pymupdf4llm`.
- `--use-llm` — Off by default. Enables a Claude Haiku Tier-3 fallback for PDFs where the regex finds no signal (non-English decisions, footnote-fragmented sentences). Requires `pip install anthropic` and `ANTHROPIC_API_KEY`. Cost ~$0.0014/PDF.
- `--stage enrich-pdf --consolidated <path>` — Re-runs PDF enrichment on an already-consolidated CSV **without** redoing matching. Backs the original up to `*_pre_pdf.csv` and updates `dc_preferred` / `dc_flag` in place. Useful after updating the fund-alias table or expanding the state-aid case index.

---

## Outputs

All results land in `data/processed/match_output/` (or pass `--output-dir` to change this):

```
match_output/
├── match_log.csv                  # Every matched row: type of support, amount, match score
├── consolidated_matches.csv       # Deduplicated matches with GGE values
├── group_summary.csv              # Group-level totals (if parent groups configured)
├── T1–T8_summary_*.csv            # Breakdowns by support type, country, instrument, year
├── concentration_metrics.json     # HHI, Gini, top-5% share
└── charts/
    ├── 01_annual_total.png              # Annual support totals
    ├── P02_mff_source_stacked.png       # Support by MFF period × support type
    ├── P05_country_instrument.png       # Top 15 granting countries by instrument
    ├── P06c_top20_gge_core_auto.png     # Top 20 groups: face value vs GGE
    ├── P15c_top20_aggregated_core.png   # Top 20 groups by total face value × support type
    └── S04_foreign_top15.png            # Top 15 non-EU groups by GGE
```

**GGE (Gross Grant Equivalent)** converts face values to subsidy-equivalent: grants and direct subsidies count at 100% of face value, loans at 15%, guarantees at 10%, tax advantages at 15%. This makes a loan from an IFI directly comparable to a state-aid cash grant.

---

## Optional: Aliases and Config

**Aliases** map variant names to a canonical name — useful for subsidiaries or alternate spellings:

```json
{
    "Volkswagen AG": ["VW", "Volkswagen Group", "VW AG"],
    "Stellantis NV": ["Fiat", "Peugeot", "Opel", "Citroën"]
}
```

**Config JSON** enables parent group rollup and sector-specific text mining:

```json
{
    "parent_groups": "path/to/parent_groups.json",
    "sector_keywords": ["electric vehicle", "battery", "hydrogen"],
    "nace_filter": ["29", "2910"]
}
```

```bash
python run_pipeline.py --stage match \
    --company-list my_companies.csv \
    --aliases my_aliases.json \
    --config my_config.json
```

Full details: [research/MATCHING_GUIDE.md](research/MATCHING_GUIDE.md) (config and matching), [research/METHODOLOGY.md](research/METHODOLOGY.md) (data sources).

---

## Data Sources

The ~27 million rows harmonized into the master dataset are grouped into three types of support:

| Support type | What it is | Approximate size | Coverage |
|---|---|---|---|
| **State aid** | National subsidies (grants, tax advantages, loans, guarantees, equity, etc.) notified to the European Commission by the Member States. Sourced from the EC's central state-aid transparency database plus four higher-granularity national supplements (Spain, Poland, Romania, Slovenia). | ~22.5M rows | 2000–2024 |
| **EU funds** | Money paid or committed out of the EU budget directly: cohesion policy projects, research and innovation grants (Horizon Europe / Horizon 2020), Connecting Europe Facility, LIFE, the Innovation Fund, and operational payments from the central EU financial transparency register. | ~4.8M rows | 2007–2027 |
| **IFIs** | Loans and guarantees from European international financial institutions — the European Investment Bank and the European Bank for Reconstruction and Development. Non-EU-27 projects are excluded by default. | ~32K rows | 1959–2024 |

Two categories intentionally do **not** flow through to headline totals:

- **State-aid scoreboard aggregates** are excluded at master-build time because they are a higher-level aggregation of the same underlying state-aid decisions — including them would double-count the grants they summarize.
- **The Recovery and Resilience Facility — partially covered (IT / DE).** The EC RRF source publishes only measure-level planned allocations, so those rows carry `beneficiary_name = pd.NA` and never reach the entity matcher. Country-specific adapters close this blind spot:
    - **Italy**: granular project-level data (attuatore / CUP / amounts) is pulled from the OpenCoesione parquet dump (`progetti_esteso.parquet`, CC BY 4.0) via `src/harmonization/rrf_italia_domani.py` and filtered to PNRR scope.
    - **Germany**: the Article-9a top-100 recipients list published by Bundesfinanzministerium is scraped via `src/harmonization/rrf_national_top100.py`. Registered sentinel scrubbing excludes anonymised bucket rows.
    - **ES / FR / PT / EL / PL / RO**: stubs documented in `rrf_national_top100.py` with per-country notes (PDF-only, aggregates-only, or unsurveyed). Any row from a country whose adapter has not been implemented is still absent from the matcher. See [research/PIPELINE_AUDIT.md](research/PIPELINE_AUDIT.md) finding H7 and [research/METHODOLOGY.md](research/METHODOLOGY.md) §12.
- **IPCEI participations** are extracted **directly from the 12 EC IPCEI decision PDFs** shipped under [data/reference/ipcei_decisions/](data/reference/ipcei_decisions/). Each per-company row carries an `amount_confidence` column (`exact_from_pdf` / `range_from_pdf` / `redacted`), an `ipcei_ticker` naming the IPCEI programme, and new `amount_eur_low` / `amount_eur_high` columns for rows where the EC redacts exact figures as bracket ranges. The default `amount_eur` uses the **low bound** (conservative) — not the midpoint — consistent with the no-invention principle. Readers who want the midpoint view compute it downstream; readers who want the upper bound sum `amount_eur_high`. Amendment decisions (e.g. Microelectronics 1 + Austria amendment) are automatically deduped against the primary decision. See [research/PIPELINE_AUDIT.md](research/PIPELINE_AUDIT.md) H6 for the migration history.
- **Anonymised / bucket beneficiaries.** ~123k rows across the master carry `beneficiary_name` values that are anonymisation sentinels (`Anonymisierter Begünstigter (Deutschland)`, `COMUNIDAD DE PROPIETARIOS`, ~20 Polish profession rollups like `USŁUGI TRANSPORTOWE` / `TAXI OSOBOWE` / `GABINET STOMATOLOGICZNY`). These are flagged `is_anonymised=True` at master-build time and excluded from the headline view — they never should have been matching candidates. Still preserved in the audit CSV. See `src/harmonization/utils.py::is_anonymised_beneficiary` for the pattern list.

Additional reference data shipped via GitHub Releases and auto-downloaded on first run:

- **EC state-aid case registry** (`case-data-SA.json`, ~638 MB, ~60,000 cases). Indexed at runtime and used to resolve each matched state-aid row to its EC decision PDF, flag IPCEI cases, and cross-check notified amounts against EC-reported scheme expenditure. This registry is what makes document-grounded deduplication possible.

For a per-source breakdown (raw database names, columns, internal flags) see [research/METHODOLOGY.md](research/METHODOLOGY.md) §1. The methodology document is the only place in the repo that uses the operational source names.

---

## Worked Example: Automotive

A full worked example for a sector analysis is included, using a pre-built list of 1,300+ automotive companies with 60 corporate groups, sector tags, and nationality classifications:

```bash
python run_pipeline.py --stage automotive
```

This runs the full pipeline and generates 20 sector-specific charts. See [`examples/automotive/`](examples/automotive/) for the config files.

---

## Rebuilding from Raw Data

> **Note:** Requires the original raw source files in `data/raw/`. Most users should use `--stage match` instead.

If you have the original source files:

```bash
python run_pipeline.py --stage harmonize   # raw → standardized CSVs
python run_pipeline.py --stage enrich      # pre-match enrichment (research-consortium join, IFI promoter scrape)
python run_pipeline.py --stage master      # build master_dataset.parquet
```

Place raw source files in `data/raw/` first.

---

<details>
<summary>Pipeline Architecture</summary>

```
data/raw/                        [1] HARMONIZATION
  State aid + EU funds     -->   src/harmonization/
  + IFI raw files                   standardize everything to a
                                    single 36-column canonical schema
                                        |
                                        v
                                 [2] PRE-MATCH ENRICHMENT
                                 src/enrichment/
                                    research-consortium join,
                                    IFI promoter scrape
                                        |
                                        v
data/processed/                  [3] MASTER BUILD
  master_dataset.parquet   <--   src/master/builder.py
                                    ~27M rows, ~25.7M primary records
                                    (scoreboard aggregates + non-EU IFI
                                     rows excluded by default)
                                        |
                                        v
                                 [4] ENTITY MATCHING
                                 src/matching/generic_matcher.py
                                    Layer A: exact + fuzzy name match
                                    Layer B: contextual regex on project
                                      descriptions and IFI project titles
                                    Dedup optimisation: scores ~920K unique
                                    names once, joins back to 27M rows
                                        |
                                        v
                                 [5] POST-MATCH ENRICHMENT (automatic)
                                    research-consortium bridge,
                                    EU ETS free allocation, IPCEI look-up,
                                    deep text mining of EU fund payments,
                                    high-value forensics sweep
                                        |
                                        v
                                 [6] CONSOLIDATION + CHARTS (automatic)
                                    Phase 2a: integrate enrichment CSVs
                                    Phase 2c: state-aid decision PDF
                                      enrichment
                                      (GBER table → regex prose → LLM)
                                    Phase 2b: cross-source dedup
                                      (document-backed authoritative,
                                       then heuristic fallback)
                                    Phases 3-8: GGE, match quality,
                                    group rollup, summary tables,
                                    six publication charts
```

</details>

---

## Requirements

- Python >= 3.10
- `pip install -r requirements.txt`
- Core: pandas, numpy, rapidfuzz, pyarrow, openpyxl, requests
- Charts: matplotlib, seaborn, plotly, kaleido (optional). Charts generate only if installed: `pip install plotly kaleido matplotlib seaborn`.
