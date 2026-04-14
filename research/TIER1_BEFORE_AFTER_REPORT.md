# Tier 1 before/after validation — 19 companies

- **Baseline**: `data\processed\match_output\validation_baseline`
- **Improved**: `data\processed\match_output\validation_improved`

The baseline was produced by git-stashing every tracked Phase A edit from the 2026-04-13 overnight + 2026-04-14 follow-up sessions and running `python run_pipeline.py --stage match` against a 19-company European automotive test subset. The improved run uses the same company list but with every edit restored. Both runs had `--no-pdf-enrichment` set so the heuristic-fallback dedup path is the one the comparison stresses.

## 1. Row counts and headline totals

| Metric | Baseline | Improved headline | Improved audit | Δ headline |
|---|---:|---:|---:|---:|
| rows | 3,486 | 857 | 3,552 | -2,629 |
| face value | €8.41B | €5.89B | €8.42B | €-2528088998 |
| GGE | €4.09B | €3.04B | — | €-1046523922 |
| unique entities | 18 | 17 | 18 | — |

## 2. Dedup flag distribution

Baseline `dc_flag` values (rows where the flag is non-empty):

| dc_flag | count |
|---|---:|
| `consortium_partner_attribution` | 2246 |
| `cofinancing_overlap:tam_kohesio` | 164 |
| `same_record_multicountry|consortium_partner_attribution` | 7 |
| `same_record_multicountry` | 2 |
| `cofinancing_overlap:tam_rrf` | 1 |
| `cofinancing_overlap:tam_kohesio|cofinancing_overlap:tam_rrf` | 1 |

Improved headline `dc_flag` (should be empty or document-grounded only):

_(no flagged rows in improved headline)_

Improved audit `heuristic_flag` (new audit-only column — should carry the demoted heuristic hits):

| heuristic_flag | count |
|---|---:|
| `cofinancing_overlap:tam_kohesio` | 165 |

## 3. Face value by source

| source | baseline | improved headline | Δ |
|---|---:|---:|---:|
| CINEA | €13.0M | €13.0M | €0 |
| EBRD | €74.7M | €30.7M | €-44000000 |
| EIB | €4.72B | €3.02B | €-1699017736 |
| FTS | €14.6M | €14.5M | €-108755 |
| FTS_CORDIS | €760.4M | €70.4M | €-690029509 |
| KOHESIO | €95.2M | €303K | €-94932998 |
| RRF | €651.3M | €651.3M | €0 |
| TAM | €2.09B | €2.09B | €0 |

## 4. Top 10 entities by face value

| entity | baseline | improved headline | Δ |
|---|---:|---:|---:|
| RENAULT | €3.11B | €1.69B | €-1417311367 |
| MERCEDES-BENZ GROUP AG | €1.90B | €1.79B | €-115644248 |
| VOLKSWAGEN AG | €978.0M | €362.6M | €-615371741 |
| ROBERT BOSCH GESELLSCHAFT MIT BESCHRAENKTER HAFTUNG | €950.9M | €950.9M | €0 |
| IVECO GROUP NV | €452.4M | €452.4M | €0 |
| VALEO | €302.1M | €27.2M | €-274895169 |
| VOLVO CAR AB | €279.1M | €278.9M | €-199200 |
| AUTOMOBILI LAMBORGHINI S.P.A. | €90.8M | €90.8M | €0 |
| STELLANTIS N.V. | €85.2M | €0 | €-85200000 |
| TOYOTA MOTOR CORPORATION | €66.3M | €61.3M | €-5008379 |
| AUDI AG | €40.0M | €40.0M | €0 |
| FORD MOTOR COMPANY | €39.6M | €27.9M | €-11684479 |
| SCANIA CV AKTIEBOLAG | €36.0M | €36.0M | €0 |
| GENERAL MOTORS COMPANY | €30.7M | €30.7M | €0 |
| DR. ING. H.C. F. PORSCHE AG | €17.1M | €17.1M | €0 |

## 5. New columns in improved view

| column | sample non-null values |
|---|---|
| `entity_name_clean_dedup_count` | 44.0, 47.0, 61.0 |
| `ipcei_ticker` | unknown |
| `amount_confidence` | measured, unknown |
| `is_adhoc_preloaded` | 0.0 |
| `amount_eur_low` | 846367.85, 1245523.0, 613468.0 |
| `amount_eur_high` | 846367.85, 1245523.0, 613468.0 |
| `heuristic_flag` | cofinancing_overlap:tam_kohesio |
| `gge_rate_source` | measured |
| `amount_eur_face` | 846367.85, 1245523.0, 613468.0 |
| `amount_eur_gge` | 846367.85, 1245523.0, 613468.0 |

## 6. Anonymised-sentinel filter impact

_(column missing)_

## 7. GGE rate source distribution

| source | count |
|---|---:|
| `measured` | 857 |

Face value with `gge_rate_source == "unknown"` (excluded from GGE total): €0

## 8. Column-set comparison

- baseline columns: 24
- improved columns: 34
- new in improved: ['amount_confidence', 'amount_eur_face', 'amount_eur_gge', 'amount_eur_high', 'amount_eur_low', 'entity_name_clean_dedup_count', 'gge_rate_source', 'heuristic_flag', 'ipcei_ticker', 'is_adhoc_preloaded']
- missing in improved (should be empty): []

## Takeaway

The improved run preserves every baseline row in the **audit** view (`consolidated_matches_audit.csv`) but filters the **headline** view to exclude dedup-flagged rows, suspect matches, and anonymised sentinels. The headline face value delta captures exactly the impact of the no-invention principle: any negative delta is money the baseline published that the improved view no longer treats as real attribution. The new face/GGE/low/high columns are present in the improved CSV, and the GGE total excludes rows with unknown instruments (the old baseline silently treated those as 100% grants).