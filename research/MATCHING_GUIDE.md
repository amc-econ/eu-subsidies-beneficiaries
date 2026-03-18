# Entity Matching Guide

The generic matcher (`src/matching/generic_matcher.py`) accepts any company list and matches it against the master subsidy dataset.

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

For sources with rich text fields (FTS descriptions, EIB project titles):
- Builds a single regex from all reference names (>= 6 chars, minus blocklisted words)
- Scans `description`, `original_columns`, and source-specific text fields
- The matcher tags these `contextual_exact` with lower confidence

### Layer B+: Title Extraction (EIB/EBRD)

For EIB and EBRD rows where `beneficiary_name` is the project title:
- Extracts potential company names from the title text
- Matches extracted names against the reference list

## Output Files

The matcher produces in the output directory:

| File | Description |
|------|------------|
| `{prefix}_match_log.csv` | Full match log: every matched row with match type, confidence, scores |
| `{prefix}_match_summary.txt` | Human-readable summary: counts, EUR totals, by source, by type |
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
| `eib_title_extraction` | For EIB and EBRD rows, `beneficiary_name` contains the project title rather than a company name. The regex scans the title to extract a company reference. `attribution_type = inferred`. |

**Enrichment**

Rows added by the post-match enrichment scripts, not by the entity matcher. Each carries its own `match_type` to distinguish it from Layer A/B results.

| `match_type` | Description |
|---|---|
| `fts_cordis_beneficiary_name` | FTS-CORDIS bridge. FTS grant IDs are extracted from FTS payment descriptions and joined to CORDIS participant records. This value means the FTS beneficiary name itself matched the reference company: direct receipt. |
| `fts_cordis_cordis_company` | FTS-CORDIS bridge. The reference company appears in the CORDIS consortium member list for this grant, but is not the entity that received the FTS payment (often a university or coordinating body). Always `dc_preferred=False`, `attribution_type=consortium_partner`. |
| `ipcei_reference` | Matched against the curated IPCEI participant list (batteries, hydrogen, microelectronics IPCEI programmes). Amounts are notified state aid figures from EC approval decisions. |

## Full Pipeline (match + enrich + consolidate)

When you run `--stage match`, the pipeline automatically executes:

### Post-Match Enrichment

Five enrichment scripts run automatically after matching:

| Script | What it does |
|--------|-------------|
| FTS-CORDIS bridge | Extracts CORDIS grant IDs from FTS descriptions, joins to participant data |
| EU ETS free allocation | Matches company names against EU ETS installation records |
| IPCEI reference | Matches against IPCEI participant reference data (batteries, microelectronics, hydrogen) |
| FTS deep mining | Text-mines FTS descriptions for company name mentions |
| High-value forensics | Audits top unmatched rows (>EUR 500K) for potential missed matches |

### Consolidation

The consolidation step produces:

| Output | Description |
|--------|------------|
| `consolidated_matches.csv` | All matches with GGE, match quality, cross-source deduplication flags, and attribution classification. Filter on `dc_preferred=True` for headline totals. |
| `group_summary.csv` | Group-level summary (if parent_groups configured) |
| `concentration_metrics.json` | HHI, Top5%, Gini at entity and group level |
| `T1-T8 summary tables` | By source, country, instrument, year, fiscal source, top entities |
| `charts/` | 6 matplotlib charts |

### Cross-Source Deduplication

Several EU databases capture the same underlying financial flow from different angles. The consolidation phase detects these overlaps, marks the lower-authority row `dc_preferred=False`, and sets `dc_flag` to record which pattern was detected. No rows are deleted. Charts and summary tables use `dc_preferred = True` rows only.

**Detection criteria**:

| Source pair | What overlaps | Detection | Tolerance |
|-------------|--------------|-----------|-----------|
| FTS ↔ INNOVFUND | FTS records budget outflow; INNOVFUND records the award decision. Same grant, two rows. | FTS `programme` contains `'Innovation Fund'`; same entity; amount comparison | ≤ 0.1% amount ratio. Year window not used — award and payment years routinely differ. |
| FTS ↔ CINEA | FTS records payment; CINEA programme DB records the same project. | FTS `programme` contains CINEA keyword (CEF, LIFE, EMFAF); shared `source_record_id` | Exact project ID match. No amount check — partial payments and tranches mean amounts differ. |
| TAM ↔ KOHESIO | TAM = total national state aid (EU share + national share). KOHESIO = EU co-financing share only. Same investment, two angles. | Same entity + country; year ±2; plausibility ratio check | KOHESIO/TAM ratio 1–150%. Lower bound excludes coincidental matches; upper bound covers multi-year KOHESIO disbursements against a single TAM commitment. |
| TAM ↔ RRF | RRF recovery grants sometimes notified as state aid in TAM. | Same logic as TAM ↔ KOHESIO | Same ratio 1–150% |
| IPCEI ↔ TAM | IPCEI project aid receives an SA.XXXXX reference and appears in both the IPCEI reference database and TAM. | Same entity + country; year ±2; amount proximity | ≤ 20% amount ratio. IPCEI estimated amounts at EC approval may differ from notified SA amounts. |
| Consortium partner | FTS-CORDIS bridge attributes consortium grant to matched entity, but the entity is one of multiple consortium members — not the sole beneficiary. | `match_type = fts_cordis_cordis_company` | No amount check. Classification by match type, not proximity. |

**Columns added to `consolidated_matches.csv`**:

| Column | Values | Description |
|--------|--------|-------------|
| `dc_preferred` | `True` / `False` | `True` = include in headline EUR totals and charts |
| `dc_flag` | pipe-delimited strings or empty | Which overlap pattern was detected |
| `attribution_type` | `direct` / `consortium_partner` / `contextual` / `inferred` | How the amount is linked to the matched entity |
| `programme` | string | FTS programme name; semantic link to INNOVFUND / CINEA |
| `cofinancing_partner_id` | source_record_id or empty | Cross-reference to the preferred counterpart row |

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
| `confirmed_duplicate:fts_innovfund` | FTS budget outflow echoes an INNOVFUND award (same grant, two DBs) |
| `confirmed_duplicate:fts_cinea` | FTS payment echoes a CINEA project record (shared project ID) |
| `cofinancing_overlap:tam_kohesio` | TAM = total national aid; KOHESIO = EU co-financing share of the same investment |
| `cofinancing_overlap:tam_rrf` | TAM row overlaps with an RRF recovery grant |
| `confirmed_duplicate:ipcei_tam` | IPCEI state aid decision also notified as SA.XXXXX in TAM |
| `consortium_partner_attribution` | FTS-CORDIS row attributed to a consortium member, not the direct beneficiary |
| `same_record_multicountry` | Same source record (same ID + entity + amount + year) appears under multiple country codes — structural artifact of multi-country KOHESIO projects; all but the first occurrence are flagged |

EIB/EBRD loans alongside grants from TAM or other sources are kept as `dc_preferred=True` — loans are repayable and GGE conversion already applies lower rates (15–10% vs 100% for grants). @Antoine I think this merits some thinking. IMy impression is that TAM grant/loan figures are reprensetative of the state aid component only, even though it could be for projects that are also recieving loans from IFIs.

**False positive controls**: pass via
`MatchConfig(false_positive_pairs=..., beneficiary_fp_patterns=...)`. See
`examples/automotive/config.py` for the pattern.

### GGE (Gross Grant Equivalent)

Face values are converted to subsidy-equivalent values using EU State Aid Scoreboard rates:

| Instrument | GGE Rate |
|-----------|----------|
| Grant | 100% |
| Loan | 15% |
| Guarantee | 10% |
| Equity | 100% |
| Tax advantage | 15% |

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
