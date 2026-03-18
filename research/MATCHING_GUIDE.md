# Entity Matching Guide

The generic matcher (`src/data_extraction/matching/generic_matcher.py`) accepts any company list and matches it against the master subsidy dataset. No sector-specific logic is hardcoded — all customisation is passed via configuration.

## Company List Format

Supply a CSV with at minimum a `company_name` column:

```csv
company_name,country,source
Volkswagen AG,DE,orbis
Tesla Inc,US,manual
CATL,,manual
```

Only `company_name` is required. Additional columns are preserved in the output but not used by the matcher.

## Aliases Format

Optionally supply a JSON file mapping canonical names to known aliases:

```json
{
    "Volkswagen AG": ["VW", "Volkswagen Group"],
    "Bayerische Motoren Werke AG": ["BMW", "BMW Group"],
    "Stellantis NV": ["FCA", "Fiat Chrysler", "PSA Group", "Peugeot", "Citroen", "Opel"]
}
```

Aliases are added to the exact-match lookup -- if a beneficiary name exactly matches an alias, it's linked to the canonical company.

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
from src.data_extraction.matching.generic_matcher import run_matching, MatchConfig

match_log, summary = run_matching(
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

### Layer B: Contextual Text Matching

For sources with rich text fields (FTS descriptions, EIB project titles):
- Builds a single regex from all reference names (>= 6 chars, minus blocklisted words)
- Scans `description`, `original_columns`, and source-specific text fields
- Matches are tagged `contextual_exact` with lower confidence

### Layer B+: Title Extraction (EIB/EBRD)

For EIB and EBRD rows where `beneficiary_name` is actually the project title:
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
| `{prefix}_type` | `exact`, `fuzzy_high`, `fuzzy_medium`, `contextual_exact`, `title_extraction` |
| `{prefix}_confidence` | `high`, `medium`, `lower` |
| `{prefix}_score` | rapidfuzz score (0-100), null for exact matches |
| All original master columns | Preserved from master_dataset |

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

All enrichment scripts accept the company list dynamically — no sector-specific patterns are hardcoded.

### Consolidation

The consolidation step produces:

| Output | Description |
|--------|------------|
| `consolidated_matches.csv` | All matches (core + enrichment) with GGE, match quality, optional parent group |
| `group_summary.csv` | Group-level summary (if parent_groups configured) |
| `concentration_metrics.json` | HHI, Top5%, Gini at entity and group level |
| `T1-T8 summary tables` | By source, country, instrument, year, fiscal source, top entities |
| `charts/` | 8 publication-grade Plotly charts |

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
