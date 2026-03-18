# EU Subsidies Beneficiary Analysis

Match any list of companies against ~27 million EU subsidy records from 12 public sources.

---

## Quick Start

```bash
git clone https://github.com/amc-econ/eu-subsidies-beneficiaries.git
cd eu-subsidies-beneficiaries
pip install -r requirements.txt
python run_pipeline.py --stage match --company-list my_companies.csv
```

> The master dataset (~1.7 GB) is downloaded automatically on first run. To download in advance without running a match: `python run_pipeline.py --download-data`

Results land in `data/processed/match_output/`. Expect ~5–15 minutes on a standard laptop.

---

## Your Company List

Create a CSV with a `company_name` column — that's all that's required:

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
1. **Fuzzy matching** — exact + fuzzy match against 27M rows (rapidfuzz, two-layer)
2. **Post-match enrichment** — CORDIS research grants, EU ETS allocation, IPCEI, FTS text mining
3. **Consolidation** — GGE calculation, deduplication, summary tables, concentration metrics
4. **Charts** — 6 publication-grade charts (annual totals, by MFF period, by country, top groups by GGE, top groups by source, non-EU groups)

---

## Outputs

All results land in `data/processed/match_output/` (or pass `--output-dir` to change this):

```
match_output/
├── match_log.csv                  # Every matched row: source, amount, match score
├── consolidated_matches.csv       # Deduplicated matches with GGE values
├── group_summary.csv              # Group-level totals (if parent groups configured)
├── T1–T8_summary_*.csv            # Breakdowns by source, country, instrument, year
├── concentration_metrics.json     # HHI, Gini, top-5% share
└── charts/
    ├── 01_annual_total.png              # Annual support totals (line + fill)
    ├── P02_mff_source_stacked.png       # Support by MFF period × data source
    ├── P05_country_instrument.png       # Top 15 granting countries by instrument
    ├── P06c_top20_gge_core_auto.png     # Top 20 groups: face value vs GGE (dual bars)
    ├── P15c_top20_aggregated_core.png   # Top 20 groups by total face value × source
    └── S04_foreign_top15.png            # Top 15 non-EU groups by GGE (skipped if no hq_country)
```

**GGE (Gross Grant Equivalent)** converts face values to subsidy-equivalent: grants = 100%, loans = 15%, guarantees = 10%.

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

| Source | Description | Rows | Coverage |
|--------|------------|------|----------|
| TAM | EU Transparency Award Module (state aid decisions) | ~2.1M | 2000–2023 |
| TAM Supplements | Spain (BDNS), Poland (SUDOP), Romania, Slovenia | ~20.4M | Various |
| FTS | EU Financial Transparency System (direct grants/contracts) | ~1.4M | 2007–2024 |
| Kohesio | EU Cohesion Policy projects | ~1.9M | 2014–2020 |
| EIB | European Investment Bank loans | ~26K | 1959–2024 |
| EBRD | European Bank for Reconstruction and Development | ~6K | 1991–2024 |
| ESIF 2014 | European Structural and Investment Funds (2014–2020) | ~600K | 2014–2020 |
| ESIF 2027 | ESIF programming period 2021–2027 | ~35K | 2021–2027 |
| CINEA | Climate, Infrastructure and Environment Executive Agency | ~3K | 2021–2024 |
| RRF | Recovery and Resilience Facility (measure-level, no beneficiaries) | ~500 | 2021–2026 |
| RESEARCH | Horizon Europe / Horizon 2020 (CORDIS) | ~850K | 2014–2027 |
| Scoreboard | EU State Aid Scoreboard (contextual aggregates) | ~50K | 2000–2022 |

---

## Worked Example: Automotive

A full worked example for a sector analysis is included, using a pre-built list of 1,300+ automotive companies with 60 corporate groups, sector tags, and nationality classifications:

```bash
python run_pipeline.py --stage automotive
```

This runs the full pipeline and generates 20 sector-specific presentation charts. See [`examples/automotive/`](examples/automotive/) for the config files.

---

## Rebuilding from Raw Data

> **Note:** Requires the original raw source files in `data/raw/`. Most users should use `--stage match` instead.

If you have the original source files:

```bash
python run_pipeline.py --stage harmonize   # raw → standardized CSVs
python run_pipeline.py --stage enrich      # CORDIS + EIB pre-matching enrichment
python run_pipeline.py --stage master      # build master_dataset.parquet
```

Place raw source files in `data/raw/` first.

---

<details>
<summary>Pipeline Architecture</summary>

```
data/raw/                        [1] HARMONIZATION
  12 source files          -->   src/harmonization/
                                    18 modules, 36-column schema
                                        |
                                        v
                                 [2] PRE-MATCH ENRICHMENT
                                 src/enrichment/
                                    CORDIS org join, EIB promoter scrape
                                        |
                                        v
data/processed/                  [3] MASTER BUILD
  master_dataset.parquet   <--   src/master/builder.py
                                    ~27M rows, ~25.7M primary records
                                        |
                                        v
                                 [4] ENTITY MATCHING
                                 src/matching/generic_matcher.py
                                    Layer A: exact + fuzzy (rapidfuzz)
                                    Layer B: contextual regex on descriptions
                                    Dedup optimisation: matches ~920K unique names,
                                    joins back to full dataset (~5x speedup)
                                        |
                                        v
                                 [5] POST-MATCH ENRICHMENT (automatic)
                                    FTS-CORDIS bridge, EU ETS, IPCEI,
                                    FTS text mining, high-value forensics
                                        |
                                        v
                                 [6] CONSOLIDATION + CHARTS (automatic)
                                    GGE, dedup, group rollup, summary tables,
                                    6 publication charts
```

</details>

---

## Requirements

- Python >= 3.10
- `pip install -r requirements.txt`
- Core: pandas, numpy, rapidfuzz, pyarrow, openpyxl, requests
- Charts: matplotlib, seaborn, plotly, kaleido (optional). Charts generate only if installed: `pip install plotly kaleido matplotlib seaborn`.
