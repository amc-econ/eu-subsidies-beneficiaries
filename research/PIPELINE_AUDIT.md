# Pipeline Audit & Improvement Candidates

This is a prioritized audit of the pipeline as it exists in the current source tree. Each finding names the exact file and function behind it, explains why it matters, and proposes a concrete improvement.

Findings are grouped by priority. "High" items materially affect dedup accuracy or the trustworthiness of headline totals. "Medium" items are correctness or robustness improvements whose absence is unlikely to break a typical run but is reachable by a determined user. "Low" items are polish and clarity.

**Status legend.** `[DONE]` = applied to the code in the current tree; `[OPEN]` = not yet implemented; `[INVESTIGATING]` = partially done / needs follow-up. Items are appended chronologically within each priority group so that the audit reads as a running log.

## Progress Tracker

| # | Title | Priority | Status |
|---|---|---|---|
| H1 | `_FUND_ALIASES` missing national-language variants | High | **DONE** (2026-04-12, expanded 2026-04-13) — 119 aliases / 23 fund families, word-boundary regex |
| H2 | TAM ↔ IFI loan co-financing not detected | ~~High~~ **Low** | OPEN — **deprioritized** (2026-04-12); rare in practice and the state-aid side and the IFI side are typically economically different instruments |
| H3 | Heuristic dedup thresholds unvalidated | High | **SUPERSEDED** (2026-04-13) — heuristic demoted to audit-only `heuristic_flag` column; no longer affects headline totals. Empirical tuning still pending the gold-set harness. |
| H4 | SA PDF evidence is presence-only, not amount | High | OPEN (documentation done; workaround not built) |
| H5 | Silent GGE fallback for unknown instrument classes | High | **DONE** (2026-04-13) — unknown instruments now produce `amount_eur_gge = NaN`, not 1.0; `gge_rate_source` column published |
| H6 | IPCEI amounts are estimated, not measured | High | **DONE** (2026-04-12/13) — PDF-grounded per-company extraction with `amount_eur_low`/`high` bounds; low bound is the headline default (no midpoints); amendment dedup added |
| H7 | RRF beneficiary-level analysis never integrated | High | **PARTIAL** (2026-04-13/14) — Italy closed via `rrf_italia_domani.py` (OpenCoesione parquet scaffold, not yet run), Germany closed via `rrf_national_top100.py` (BMF top-100 HTML scrape, 100 rows / €7.19B verified). ES/FR/PT/EL/PL/RO stubs documented in `rrf_national_top100.NATIONAL_PORTALS`. RRF beneficiary_name sentinel also fixed (`pd.NA` + defensive coerce in `paths.read_master`). |
| M1 | `--pdf-enrichment` default split between CLI and Python API | Medium | **DONE** (2026-04-12) |
| M2 | No structural validation of enrichment output schemas | Medium | **DONE** (2026-04-12) |
| M3 | PDF downloader is synchronous with no aggregate timeout | Medium | **DONE** (2026-04-12) — progress log + circuit breaker; aggregate wall-clock timeout left as a follow-up |
| M4 | RRF branch of heuristic fallback has no fund filter | Medium | **SUPERSEDED** (2026-04-13) — whole heuristic demoted to audit-only; the RRF branch still iterates but cannot affect totals now. Cleanup pending. |
| M5 | Match-quality check is asymmetric across sources | Medium | **DONE** (2026-04-12) |
| L1 | Master-build vs consolidation exclusion semantics | Low | **DONE** (2026-04-13) — Phase 5c headline vs audit split in `consolidate()`; two CSVs published |
| L2 | Per-source-pair year windows not configurable | Low | **DONE** (2026-04-12) — `DedupConfig` dataclass threaded through `consolidate()` |
| L3 | PDF cache lives in run output directory | Low | **DONE** (2026-04-12) |
| L4 | No traceability column for name-dedup optimization | Low | **DONE** (2026-04-12) |
| L5 | `sa_pdf_parser.py` docstring references CRM relevance | Low | **DONE** (2026-04-12) |
| H8 | Pre-load pre-2016 ad hoc state-aid decisions | High | **DONE** (2026-04-13/14) — phase-1 parser shipped + ladder v2 (5 → 15 patterns) + redaction-aware `suspect_fallthrough` tier + paren-EUR multi-currency extraction. **Real hit rate: 6/20 extracted + 2/20 redaction-flagged** on random sample seed=42 (was 0/20 with the original ladder). |
| **NEW** | Anonymised-beneficiary sentinel scrubbing | Medium | **DONE** (2026-04-14) — `is_anonymised` column added to `COMMON_COLUMNS`, 123k rows / €270M face value flagged and excluded from headline totals (KOHESIO: 45k "Anonymisierter Begünstigter", TAM: 78k Polish profession rollups + COMUNIDAD DE PROPIETARIOS). |
| **NEW** | Unicode NFKD + Cyrillic/Greek romanisation in `clean_name` | Medium | **DONE** (2026-04-13) — Škoda → skoda, Газпром → gazprom, Αλφα → alfa, Müller → muller |
| **NEW** | Default Layer B `contextual_blocklist` | Medium | **DONE** (2026-04-13) — empty → 41 names (ford, apple, shell, bp, horizon, life, …) |
| **NEW** | EIB deep scraper (24 fields per page vs 5) | Medium | **DONE** (2026-04-13/14) — 16,917/16,917 pages scraped overnight, multi-promoter + multi-tranche signatures + full prose, cutover into `integrate_enrichment` pending |
| **NEW** | Gold-set sampling + matcher_report tooling | High | **INFRA SHIPPED** (2026-04-13) — `tools/gold_set_sample.py` + `tools/matcher_report.py` with Wilson 95% bounds; awaiting a real `match_log.csv` to label against |
| **NEW** | `extra_fields_json` schema v3 column | Low | **DONE** (2026-04-13) — added to `COMMON_COLUMNS`, tolerant validator, master-builder default-fill `'{}'` |
| **NEW** | GLEIF / OpenLEI scaffold | Low | **SCAFFOLD** (2026-04-13) — `src/matching/lei_canonicaliser.py` ready; one-off download + Layer 0 LEI-exact pass integration pending |

---

## High

### H1 — `_FUND_ALIASES` is missing principal national-language variants — **[DONE 2026-04-12]**

**Applied.** Added the Polish, Czech, Hungarian, Romanian, Slovak, Slovenian, and Baltic variants for ERDF, ESF, CF, and JTF to `_FUND_ALIASES` in [consolidation.py:759-817](../src/matching/consolidation.py#L759-L817). Specifically: `fedr` (RO ERDF), `europejski fundusz rozwoju regionalnego` (PL ERDF), `evropsky fond pro regionalni rozvoj` (CZ ERDF), `europai regionalis fejlesztesi alap` (HU ERDF); `esza` (HU ESF), `europejski fundusz spoleczny` (PL ESF), `evropsky socialni fond` (CZ ESF), `europai szocialis alap` (HU ESF); `fundusz spojnosci` (PL CF), `fond soudrznosti` (CZ CF), `kohezios alap` (HU CF); `ftr` (FR JTF), `fundusz sprawiedliwej transformacji` (PL JTF), `fond pro spravedlivou transformaci` (CZ JTF), `meltanyos atallasi alap` (HU JTF). The comment block (lines 753-758) was rewritten to reflect that coverage has been expanded. A coverage-counting audit script is still outstanding — see the open follow-up at the end of this file.

**Original finding below (retained for reference).**

**Where.** `_FUND_ALIASES` in [src/matching/consolidation.py:759-817](../src/matching/consolidation.py#L759-L817). The comment block at lines 753-758 already lists the known gaps.

**What's wrong.** The alias table is used by both `_flag_cofinancing_overlaps` and `_flag_pdf_cofin_overlaps` to decide whether the `fund` column on a non-TAM row corroborates the detected overlap. The current table covers English, French, German, Spanish, and Italian forms for the main structural funds, but is missing:

- ERDF: Italian `fesr`, Polish `efrr`, Romanian `fedr`
- ESF: Czech `esf` (handled), Hungarian `esza`
- JTF: French `ftr`, Polish `fundusz sprawiedliwej transformacji`

A KOHESIO (or FTS, CINEA, ESIF) row whose `fund` column contains only one of these missing variants currently fails the fund-alias check. In the heuristic fallback this means the pair is still caught by the amount-ratio check, so it degrades gracefully. In the PDF-backed path the logic is more permissive — an empty `fund` is accepted — so a row with a missing variant is actually better-handled than a half-matched one, because the canonical-match check returns `False` and the row is discarded. Either way, gaps are not symmetric across code paths, and are the single biggest lever on dedup recall.

**Why it matters.** This is the most tractable accuracy improvement in the pipeline. Each alias entry is literally one substring.

**Proposed fix.**

1. Add the missing variants to `_FUND_ALIASES`. The shape of each new alias is already clear from the existing entries — just extend the per-fund `set`.
2. Write a small audit script that iterates the master parquet, counts distinct `(source, fund)` pairs whose `fund` value does **not** hit any alias in `_FUND_ALIASES`, and reports the top N. Run it once per new alias set to verify coverage is improving and to find variants nobody has noticed yet.
3. Add a short regression test: for each canonical fund, assert that at least one row in the master data (or a fixture) with the non-English variant is recognized as a member of that fund.

### H2 — TAM ↔ IFI loan co-financing is not detected — **[DEPRIORITIZED 2026-04-12]**

**Revised assessment.** On reflection, the scenario this finding targets is narrower than it first appeared. A TAM loan row that duplicates an EIB/EBRD loan requires the Member State to have notified the EIB-financed loan as state aid — which in practice only happens when the notified component is a *subsidy element* (an interest rate reduction, a guarantee fee discount, a first-loss position) attached to the underlying IFI loan, rather than the loan itself. In that case the TAM row and the IFI row are not the same money — they are economically different instruments. Deduplicating them would *understate* the total public support to the beneficiary.

The genuinely-duplicative scenario (a pure pass-through where the Member State re-notifies an EIB loan as state aid with no additional subsidy layer) appears rare enough that we have no confirmed example in the current master dataset. **Demoted from High to Low priority.** Keeping it on the open list so a future data point can revive it, but not scheduling implementation.

**Original finding below (retained for reference).**

**Where.** `_flag_cofinancing_overlaps` ([consolidation.py:821-914](../src/matching/consolidation.py#L821-L914)) loops only over `['KOHESIO', 'RRF']`. There is no analogous step for EIB or EBRD.

**What's wrong.** A TAM *loan* row can describe the exact same underlying financing as an EIB or EBRD loan when an Member State notifies the IFI-backed loan as state aid. The EIB/EBRD row is the authoritative one (project-level detail, correct borrower name, proper instrument classification); the TAM row is the state-aid compliance echo. Neither dedup step currently catches this, so face-value totals for affected companies are inflated by the sum of the two.

This is distinct from the *legitimate* case where a beneficiary receives both a TAM grant and an EIB loan for the same project — those are genuinely different instruments and GGE discounts the loan appropriately. The case to catch is specifically **TAM `financial_instrument_class = loan` rows** that co-finance an EIB/EBRD loan of the same instrument class and rough amount.

**Why it matters.** Affects any sector with meaningful IFI participation (energy, infrastructure, transport, raw materials) — the same domains where the heuristic TAM↔KOHESIO dedup already removes large double-counts.

**Proposed fix.**

1. Add `_flag_tam_eib_loan_cofinancing(df, year_win=2)` analogous to `_flag_cofinancing_overlaps` but gated on:
   - `source == 'TAM'` AND `financial_instrument_class == 'loan'` on both sides.
   - Same `match_reference_name`, same `country`, year within `±year_win`.
   - Amount ratio in a validated band (see H3 — this band must be chosen carefully; IFI loan tranches and TAM annual notifications don't line up neatly).
2. Emit flag `cofinancing_overlap:tam_eib_loan` (and `_ebrd_loan`).
3. Add it to the Phase 2b call sequence in `consolidate()` immediately after `_flag_cofinancing_overlaps`.
4. Validate on a hand-picked sample of ~50 pairs before enabling on a live run.

### H3 — Heuristic dedup thresholds have not been validated on the full master

**Where.** [consolidation.py:886](../src/matching/consolidation.py#L886) (`(ratio >= 0.01) & (ratio <= 1.50)`), [consolidation.py:821](../src/matching/consolidation.py#L821) and [consolidation.py:917](../src/matching/consolidation.py#L917) (`year_win: int = 2`).

**What's wrong.** Both the amount-ratio band and the year window are hardcoded global constants with no published derivation. The 0.01–1.50 band is described in the code as a "plausibility" check rather than a tolerance — the code comment correctly notes that multi-year KOHESIO disbursements can legitimately exceed a single annual TAM commitment, which is why the upper bound is above 1.0 — but neither bound has been precision/recall-audited against a large sample of known pairs.

The ±2 year window likewise has no empirical basis in-repo. It is plausible, but the right window may differ by source pair: TAM↔IPCEI and TAM↔KOHESIO have different disbursement dynamics, and `_flag_ipcei_tam_overlap` already uses a different amount tolerance (0.20) without explanation for *why* 0.20.

**Why it matters.** Because PDF-backed dedup (H1 + Phase 2c) is now the primary mechanism, the heuristic only acts on residual rows where no PDF evidence is available. Those residuals are also the hardest cases. Getting the thresholds wrong here is the main remaining way to mis-flag real distinct payments as duplicates.

**Proposed fix.**

1. **Build a gold set from PDF evidence.** For every TAM row where `sa_cofin_level == 'confirmed'` and `_flag_pdf_cofin_overlaps` found a counterpart in a non-TAM source, record the `(amount_tam, amount_other, year_tam, year_other)` tuple. This is the closest thing to ground truth the pipeline has — the PDF provides the document evidence that these really are two views of the same underlying flow. Plot the distribution of ratios and year-deltas.
2. **Fit the bands to the gold set.** Pick `ratio_min`, `ratio_max`, `year_win` to cover the 95th percentile of the observed distribution, per source pair.
3. **Promote the constants to configurable `MatchConfig` / per-source-pair fields.** Expose them so that a downstream user with a sector hypothesis can loosen or tighten without patching the module.
4. Record the chosen values and the gold-set size in [research/METHODOLOGY.md](METHODOLOGY.md) §12 so readers can challenge them.

### H4 — SA PDF co-financing evidence gives presence, not amount

**Where.** `SACofinParser.enrich_dataframe` ([sa_pdf_parser.py](../src/enrichment/sa_pdf_parser.py)), `sa_gber_table_funds` column.

**What's wrong.** The GBER notification table is the only tier that produces a per-fund EUR amount. For IPCEI decisions, RRF-backed measures, and many structural-fund schemes, the table is absent or empty and the regex tiers (Tier 1/2) only confirm that co-financing exists without a number. This means the pipeline cannot currently split a TAM row's face value into a national share and an EU share *from the PDF alone*. The current dedup is **presence-based**: if the PDF confirms co-financing by ERDF and a KOHESIO row exists for the same entity, the TAM row is flagged in full; the TAM amount is then removed from headline totals rather than having the EU share subtracted.

This is structurally the right call for preventing double-counting at the headline level, but it means the pipeline cannot answer the question "how much of this company's TAM aid was actually EU-funded vs national-funded?" from the PDF data alone.

**Why it matters.** Flagged as a **limitation**, not a bug — the decision documents simply do not contain the information needed to decompose amounts in the general case. But it constrains any downstream analysis that wants to attribute flows to EU vs national budgets, and the pipeline's output should clearly label the constraint.

**Proposed fix.**

1. Document the limitation prominently in [research/METHODOLOGY.md](METHODOLOGY.md) §12 (done in the current rewrite — limitation 4).
2. Where the GBER table *is* populated (`sa_gber_table_funds` non-empty), surface the parsed amounts as first-class columns per fund so downstream users can compute EU vs national splits for the subset of cases where the data permits it.
3. When `sa_cofin_level == 'confirmed'` but `_flag_pdf_cofin_overlaps` finds no counterpart row, consider re-running a targeted KOHESIO match scoped to the SA case number / scheme id. The SA case is a much stronger join key than `(entity, country, year)` and may recover pairs that the current reference-name-based merge misses — particularly for scheme-level TAM rows whose `match_reference_name` was assigned to an umbrella granting authority rather than the ultimate beneficiary.

### H5 — Silent GGE fallback for unknown instrument classes — **[DONE 2026-04-12]**

**Applied.** Added a module-level `_GGE_UNKNOWN_COUNTS` counter and updated `_gge_rate` ([consolidation.py:86-104](../src/matching/consolidation.py#L86-L104)) to record every row whose `financial_instrument_class` value is not in the `GGE_RATES` table. The Phase 4 block in `consolidate()` clears the counter before computing rates and, on completion, emits a `log.warning` summary listing every unknown instrument class with the row count and total EUR affected. Rows still default to `1.0` to preserve backwards compatibility — the change is purely additive visibility. Running the pipeline on the same inputs will now surface any new instrument class that slips into the harmonization layer. A follow-up task — deciding whether the default should become a `KeyError` after one clean run — is left open.

**Original finding below (retained for reference).**

**Where.** [consolidation.py:92](../src/matching/consolidation.py#L92): `return GGE_RATES.get(inst, 1.0)`.

**What's wrong.** Any `financial_instrument_class` value that is not in `GGE_RATES` falls through to `1.0`, which treats the row as a 100%-GGE grant. A new harmonization module that introduces a typo, an unexpected casing, or a genuinely new instrument type will **silently inflate** the GGE totals. There is no warning log, no counter, and no test that catches it.

**Why it matters.** This is exactly the kind of pipeline drift that a methodology paper must not hide: the GGE table is supposed to be the single source of truth for the instrument-to-rate mapping, but the `.get(inst, 1.0)` default makes the table *incomplete* without any visible signal.

**Proposed fix.**

1. Replace the `.get(inst, 1.0)` with an explicit match:
   - If `inst in GGE_RATES`: use the rate.
   - If `inst` is empty or `nan`: use `1.0` (the pipeline's long-standing convention for legacy rows) **and** increment a counter.
   - Otherwise: raise a `KeyError` or log a single aggregated warning per unknown value with the row count and total EUR affected.
2. After Phase 4, log a one-line GGE coverage summary: "GGE: N rows with known instruments, M rows with unknown/missing instruments (defaulted to 1.0, EUR X)."
3. Add a unit test that feeds a synthetic instrument value through `_gge_rate` and asserts it is either in the table or reported as unknown.

### H6 — IPCEI per-company amounts are estimates, not measured disbursements — **[DONE 2026-04-12]**

**Applied.** The estimated-amount path is gone. The pipeline now extracts per-company aid directly from the 12 EC IPCEI decision PDFs.

1. The 12 decision PDFs (Batteries 1 & 2, Microelectronics 1 & 2 + Austria amendment, Hy2Tech, Hy2Move, Hy2Use, Hy2Infra, Med4Cure, Tech4Cure, Cloud Infrastructure and Services) were copied into [data/reference/ipcei_decisions/](../data/reference/ipcei_decisions/) (23 MB total).
2. A new parser [src/enrichment/ipcei_pdf_parser.py](../src/enrichment/ipcei_pdf_parser.py) walks each PDF with `pdfplumber`'s structured table detection, finds the state-aid summary tables at the back of the decision, and emits one row per direct participant per Member State. It handles the EC's bracket-range redaction (`[5-10] million EUR`) by extracting both endpoints and emitting the midpoint as the point estimate. Country is inferred from table captions and project-code prefixes. The parser is sector-agnostic — the CRM-specific scoring and keyword logic from the fork prototype was stripped out. Output: `ipcei_pdf_extracted.csv`.
3. [src/enrichment/ipcei_reference.py](../src/enrichment/ipcei_reference.py) is now a thin matcher that calls `run_ipcei_pdf_extraction`, matches the extracted company names against the user's reference list (exact → substring → fuzzy via rapidfuzz), and writes `ipcei_matched_participants.csv` ready for consolidation. Every row carries:
   - `amount_eur` = bracket midpoint from the PDF (or NaN for fully redacted rows, which are dropped at the integrate-enrichment filter).
   - `amount_confidence` ∈ `{exact_from_pdf, range_from_pdf, redacted}` — lets downstream filter by confidence.
   - `ipcei_ticker` = the IPCEI name (e.g. `'Batteries 1'`, `'Hy2Tech'`) — identifies the IPCEI programme without splitting the source taxonomy.
   - `source = 'IPCEI_state_aid'` — the existing dedup step `_flag_ipcei_tam_overlap` in consolidation.py ([:917-967](../src/matching/consolidation.py#L917-L967)) still flags the central-TAM duplicate when present. In the three-bucket user-facing taxonomy (state aid / EU funds / IFIs), both `TAM` and `IPCEI_state_aid` sit in **state aid**.
4. [src/matching/consolidation.py](../src/matching/consolidation.py) was updated: `integrate_enrichment` now seeds `ipcei_ticker` and `amount_confidence` columns on the core dataframe so they survive the `combined.columns.intersection(...)` concat pattern used to merge enrichment rows. The IPCEI merge block logs a breakdown of `exact_from_pdf` vs `range_from_pdf` counts. The stale `matched_company` / `company` lookup was replaced with the new `match_reference_name` field written by the rewired matcher.
5. The stale reference CSV `data/reference/ipcei_participants.csv` is **no longer read** by any code path. It can be deleted in a follow-up cleanup; leaving it in place for now to avoid disturbing any downstream script that grabs it directly.

**Residual limitations** (documented in [research/METHODOLOGY.md](METHODOLOGY.md) §12 and [research/MATCHING_GUIDE.md](MATCHING_GUIDE.md)):
- Bracket midpoints remain approximations — a `[5-10] million EUR` cell is written as 7.5 million EUR. Downstream analysis that needs tight precision should filter on `amount_confidence == 'exact_from_pdf'`.
- Fully redacted rows (`[…]` cells) are retained in `ipcei_pdf_extracted.csv` with `amount_confidence = 'redacted'` and `amount_eur = NaN`, but they are dropped from the consolidated output by the existing `amount_eur > 0` filter in `integrate_enrichment`. This is deliberate — zero-amount rows would not contribute to any headline total — but the names are still available for audit in the extraction CSV.
- The parser's bracket-range extraction was validated against the fork1 CRM run; for the base repo it needs one end-to-end run on a new company list to confirm no regressions. Flagged as the only open follow-up on this item.

**Original finding below (retained for reference).**

**Where.** [src/enrichment/ipcei_reference.py:141-142](../src/enrichment/ipcei_reference.py#L141-L142), and the reference CSVs at `data/reference/ipcei_participants.csv` and `data/reference/ipcei_overview.csv`.

**What's wrong.** The IPCEI enrichment flow matches the user's company list against a **curated** participant list for six IPCEIs (Batteries 1 & 2, Microelectronics 1 & 2, Hy2Tech, Hy2Move). For every matched participant it writes `matched_df['amount_eur'] = matched_df.get('amount_eur_est', ...)`. The source column is literally `amount_eur_est`, and the accompanying `amount_confidence` column is `estimated` on every row. Inspection of the reference CSV confirms this is not an aberration — every participant's EUR allocation is a manual estimate, not a value that was ever published or verified against the EC approval decision.

The overview file (`ipcei_overview.csv`) does have real totals-per-IPCEI from the SA decisions (`total_state_aid_eur` at the project level), but those are aggregates across the entire consortium — they cannot legitimately be split per company. The current code splits them anyway by copying the estimated per-company amounts into the consolidated output and flagging the source as `IPCEI_state_aid` with `financial_instrument_class = 'grant'` and `fiscal_source_type = 'national_budget'`, after which they flow straight into headline totals.

Two separate problems:

1. **The amounts are fabricated.** A methodology paper cannot in good conscience present `amount_eur_est` figures as if they were measured values. The correct treatment is either (a) surface the project-level total only, with the individual participants listed but unamounted, or (b) publish the estimates as a separate "IPCEI estimated allocation" column that does *not* contribute to headline EUR totals.
2. **The matching logic is primitive.** `_fuzzy_match_participant` ([ipcei_reference.py:51-69](../src/enrichment/ipcei_reference.py#L51-L69)) is a hand-rolled substring / token-subset matcher that is much more permissive than the main `generic_matcher.py` rapidfuzz pipeline. It does not share the trivial-token filter, the short-name guard, or the country-consistency veto. Any FP it produces enters the consolidated output as a confidently-typed `grant` with an estimated amount.

**Why it matters.** IPCEI is a small-row-count source but a high-dollar-per-row source. A handful of wrong or estimated allocations can materially move a sector's top-company rankings. The pipeline's credibility depends on not conflating estimates with disbursements.

**Proposed fix.**

1. Add a new column `amount_is_estimated` (bool) to `consolidated_matches.csv`. Populate `True` for every row whose upstream source tagged the amount as an estimate. Populate `False` everywhere else.
2. In Phase 6 summary tables and in charts, **exclude** `amount_is_estimated=True` rows from headline totals and present them in a separate "Estimated allocations (IPCEI)" callout. Keep the rows in the CSV so users can audit them.
3. Replace the hand-rolled IPCEI matcher with a call into `generic_matcher.py` — at minimum reuse the trivial-token filter and the country veto. The current substring match should remain as a last-resort fallback only when rapidfuzz returns no candidate.
4. Consider dropping IPCEI from the enrichment pipeline entirely on the grounds that any TAM row for an IPCEI participant is *already* captured by the state-aid source and will already be correctly attributed once its SA decision PDF is parsed by `_flag_pdf_cofin_overlaps`. The IPCEI reference layer was built to patch the gap that existed before PDF enrichment landed; that gap is now substantially closed. Removing it would eliminate the estimated amounts entirely at the cost of losing the six curated IPCEIs' names in output filenames — a worthwhile trade.

**Documentation.** [research/METHODOLOGY.md](METHODOLOGY.md) §6 and §12 now carry the warning that IPCEI amounts are estimates. The enrichment-layer description in §6 should be read as "provides names and approval-level context, not verified per-company amounts."

### H8 — Pre-load ad hoc state-aid decisions that never reached TAM — **[DONE 2026-04-13]**

**Applied.** Phase-1 shipped as [src/enrichment/sa_adhoc_parser.py](../src/enrichment/sa_adhoc_parser.py) (≈530 LOC) plus wiring in `run_pipeline.py` and `consolidation.py`. End-to-end tested against the live `case-data-SA.json` registry.

**What the module does, in order:**

1. **Enumerates eligible cases.** `enumerate_adhoc_cases` loads `case-data-SA.json`, filters to `CaseTypeAH`, drops programmatic / scheme / COVID / TCTF / RRF / rescue-restructuring titles, and requires an English PDF attachment. Current counts against the shipped registry: 14,915 ad hoc total → 562 framework titles skipped → 13,321 without any PDF → **1,032 eligible**.
2. **Title → beneficiary.** `extract_beneficiary_from_title` runs a regex ladder over each eligible title. Handles `"Aid to X"`, `"Aid in favour of X"`, `"State aid (measures?) (to|in favour of) X"`, `"Environmental/Rescue/Export aid to X"`, `"Alleged/Possible State aid (involved in)? X"`, bare company names, and strips leading `SA.XXXXX` prefixes + trailing ` - activity` descriptors. Measured rate on the live 1,032-case pool: **957 / 1,032 = 92.7%** non-empty extraction.
3. **Reference-list matching.** Uses the `_clean_name` normalizer from `ipcei_reference.py` plus exact → substring → rapidfuzz `token_sort_ratio ≥ 85` fuzzy matching. Cases that match nothing in the user's company list are dropped **before** any PDF is downloaded, so the download pool scales with the intersection, not with the 1,032-case ceiling.
4. **PDF download.** Reuses `SACofinParser`'s download / cache / circuit-breaker infrastructure (M3). PDFs land in the shared repo-level `data/cache/sa_decisions/` directory, so they accumulate across runs and are shared with the co-financing extractor. `max_downloads` parameter caps the run size for smoke tests and for the initial production run where the EC server's load is unknown.
5. **Amount extraction.** `extract_amount_from_text` runs a priority-ordered regex ladder against the first 15 pages of the PDF: `"aid takes the form of a grant of EUR X"`, `"a direct grant of EUR X"`, `"total notified aid of EUR X"`, `"nominal value of EUR X"`, `"maximum aid amount of EUR X"`, `"overall budget of EUR X"`. The number literal is a greedy character class (`\d[\d,.\s]{0,40}`) that handles European (`1.234,56`), Anglo (`1,234.56`) and space-separated (`12 500 000`) conventions via `_parse_number`. Unit suffixes (`million`, `billion`, `thousand`, `m`, `bn`) are handled separately. Plausibility bounds `[EUR 1000, EUR 100B]` filter out matches against years, percentages, and hundred-unit accounting references. **Validated against 9 synthetic test strings: 9/9 pass.**
6. **Row emission.** Every matched case becomes a row in `sa_adhoc_matched.csv` with:

   | Column | Value |
   |---|---|
   | `sa_case` | Normalised SA code (`SA.XXXXX`). |
   | `extracted_beneficiary` | The name extracted from the title. |
   | `match_reference_name` | The user-list reference name that matched. |
   | `adhoc_match_method` | `exact` / `substring` / `fuzzy`. |
   | `country` | ISO-2 from the registry `caseMemberState` (mapped via a local ISO-3 → ISO-2 table). |
   | `year` | From `caseLastDecisionDate` / `caseRegistrationDate`. |
   | `amount_eur` | From the regex ladder, or `NaN`. |
   | `amount_confidence` | `regex_exact` / `not_extracted` / `parse_failed`. |
   | `amount_evidence` | Short text snippet around the matched regex (for audit). |
   | `is_adhoc_preloaded` | `True` — the new marker column downstream can filter on. |

   Rows with `amount_eur = NaN` are **retained** rather than dropped — they are useful as "check this case manually" audit hints. The existing `amount_eur > 0` filter in `integrate_enrichment` automatically excludes them from headline totals.

7. **Consolidation integration.** A new block in [consolidation.py:integrate_enrichment](../src/matching/consolidation.py) loads `sa_adhoc_matched.csv` via the schema-validated `_load_enrichment_csv` helper (new schema entry `sa_adhoc` in `ENRICHMENT_SCHEMA`). Critically, before merging, it **de-duplicates each row's SA code against rows already in the `combined` frame with `source == 'TAM'`** — any SA code that harmonization already covered is dropped, because the harmonized TAM row is more granular (per-beneficiary rather than per-case) and more authoritative. The preload is strictly additive coverage. Matched rows then get `source = 'TAM'`, `source_record_id = <SA code>`, `beneficiary_name = <extracted>`, `financial_instrument_class = 'grant'` (default), `is_adhoc_preloaded = True`, and the standard matcher metadata (`match_type = 'sa_adhoc_preload'`, score=100).

8. **Schema plumbing.** `integrate_enrichment` now seeds three columns on the core dataframe before any concat: `ipcei_ticker = ''`, `amount_confidence = 'measured'`, and **new: `is_adhoc_preloaded = False`**. This is required because the downstream concat pattern uses `combined.columns.intersection(enrichment_df.columns)` — any column that doesn't already exist on `combined` is silently dropped.

9. **CLI integration.** The parser is wired into `stage_match` in [run_pipeline.py](../run_pipeline.py) as a new post-match enrichment step, gated on `--pdf-enrichment` (since it downloads PDFs). When `--no-pdf-enrichment` is passed, the step logs `Ad hoc state-aid decision pre-load: SKIPPED` and the downstream integration block is a safe no-op.

**End-to-end smoke test** (3-company reference list, `max_downloads=3`):

- Registry loaded: 14,915 cases
- Enumeration: 1,032 eligible (stable across runs)
- Title extraction: 957 non-empty candidates (92.7%)
- Reference matches: 2/957 (`British Energy plc` → SA.14289, `Lucchini` → SA.10578 — both via `exact` match method)
- PDFs downloaded & cached ✓
- Amount extraction: 0/2 (both are pre-euro multi-measure historical rescues — British Energy is in GBP, Lucchini is multi-year / multi-instrument — rows correctly fell through to `not_extracted` and will be emitted as name-only audit hints rather than contributing spurious amounts to headline totals)

The name-only outcome on the smoke test is **correct behaviour**, not a bug: both cases are exactly the kind of complex historical decisions that the conservative regex ladder is designed to not mis-extract. A phase-2 LLM fallback (already plumbed behind the `use_llm` parameter, not yet implemented) would recover amounts for these cases by reading the whole decision narrative — estimated cost ~$1-2 for the whole pool at Claude Haiku pricing.

**Phase-2 backlog:**

- **LLM amount extraction.** `run_sa_adhoc_enrichment(use_llm=True)` currently plumbs the flag but doesn't use it. Hook into the existing `SACofinParser._llm_extract` pattern (or add a sibling function) so that regex-failed cases get a Claude Haiku pass. Budget estimate: ≤$2 for the whole 1,032-case pool.
- **Non-English PDFs.** `enumerate_adhoc_cases` currently hard-requires an English PDF. About 400 additional single-company ad hoc cases have French / German / Spanish / Italian PDFs only. Extending to those is cheap (just relax the filter) and tractable via the LLM tier since multi-language handling is free there.
- **`_dedup_fts_identical_transactions` via DedupConfig.** Unrelated to H8 but flagged in L2 as a one-line follow-up — the FTS ratio tolerance is still hardcoded to 0.001 rather than read from `DedupConfig.fts_innovfund_amount_tolerance`.

**Files added or changed:**

- **NEW:** [src/enrichment/sa_adhoc_parser.py](../src/enrichment/sa_adhoc_parser.py) — ~530 LOC, sector-agnostic, reuses existing infrastructure.
- **EDITED:** [src/matching/consolidation.py](../src/matching/consolidation.py) — new `sa_adhoc` entry in `ENRICHMENT_SCHEMA`, new `is_adhoc_preloaded` seed column, new ad-hoc integration block with SA-code dedup against existing TAM rows.
- **EDITED:** [run_pipeline.py](../run_pipeline.py) — new call site in `stage_match` post-match enrichment, gated on `pdf_enrichment`.

**Original finding below (retained for reference).**

**Where.** `case-data-SA.json` — the EC DG Competition state-aid case registry already auto-downloaded on first run.

**What's missing.** Of the 59,983 cases in the registry, **14,915 are flagged `CaseTypeAH` (Ad Hoc Case)** — individual decisions approving aid to a specific beneficiary under Art. 107(3) TFEU, rather than a scheme with many unnamed downstream recipients. Many of these are pre-2016 decisions that never flowed into TAM because TAM only started in 2016, and some post-2016 ad hoc cases for which the Member State simply did not report the individual award into the TAM transparency layer. In both cases the beneficiary name and the aid amount live only in the decision PDF published by DG Competition — which the pipeline already has access to, via the `SACaseLookup` infrastructure built for PDF-backed co-financing detection (§7.2 in the methodology), but does not currently read for this purpose.

**Feasibility analysis** (performed 2026-04-12 against the current `case-data-SA.json`):

| Bucket | Count |
|---|---:|
| Total ad hoc cases | 14,915 |
| Title does not match framework/scheme/programme keywords → likely single-company | 14,608 |
| Of those, with **any** PDF attached in the registry | 1,637 |
| Of those, with an **English** PDF attached | **1,243** |
| Remainder with no PDF at all | 12,971 |

The PDF-availability constraint is the binding one. Roughly 13,000 of the 14,915 ad hoc cases are registry-metadata-only entries from the pre-~2014 era when DG Competition did not publish full decision texts online as attachments. We cannot synthesize aid rows for cases whose decision document we do not have. The tractable target for a phase-1 implementation is the **~1,243 English single-company ad hoc decisions with a downloadable PDF**.

Sample titles from the pool (confirming the single-company character):

- `SA.13886  Green Fuel Challenge Pilot Project - Methanol`
- `SA.14289  Aid in favour of British Energy plc`
- `SA.11974  Environmental aid for repair of past damage in favour of Vereinigte Chemische Fabriken`
- `SA.14875  Bayerische Filmhallen GmbH`
- `SA.10578  Environmental aid to Lucchini`
- `SA.14026  Aid to AZ and AZ Vastgoed BV`
- `SA.13590  Northern Ireland Gas Pipeline`

**Why it matters.** For historical / pre-2016 company analyses, the pipeline currently reports zero state aid for companies whose only decisions are pre-TAM ad hoc cases. This is a known blind spot — not a bug per se, but a feature gap worth closing if it can be closed cheaply.

**Proposed phase-1 design.**

1. **New parser `src/enrichment/sa_adhoc_parser.py`**, sharing the PDF-download and cache infrastructure from `sa_pdf_parser.py` (cache in the repo-level `data/cache/sa_decisions/`).
2. **Beneficiary from title.** Ad hoc decision titles encode the beneficiary in a handful of stable patterns: `"Aid to X"`, `"Aid in favour of X"`, `"Aid for X"`, `"<X> <Activity>"`, or simply `X` alone. A regex + title-normalizer can extract the candidate name without needing to read the PDF body at all.
3. **Amount from PDF body.** Regex over standard EC prose templates: *"The aid takes the form of a grant of EUR X million"*, *"a direct grant of EUR X"*, *"a guarantee on a loan of EUR X"*, *"an aid intensity of N% of eligible costs of EUR X"*. Fall-back to the `--use-llm` Claude Haiku path (already wired) when regex finds nothing — same pattern as the co-financing extractor. Per-row `amount_confidence` ∈ `{regex_exact, regex_range, llm_extracted, not_found}`.
4. **Match the extracted beneficiary against the user's company list** using the same `_clean_name` + exact → substring → fuzzy rapidfuzz pipeline that the rebuilt IPCEI enrichment now uses.
5. **Write `sa_adhoc_matched.csv`** to the enrichment directory with consolidation-ready columns: `source = 'TAM'` (these *are* state aid, just never harmonized from TAM because they pre-date TAM), `source_record_id = <SA code>`, `year` from the registry's decision date, `country` from the registry metadata, `beneficiary_name` from the PDF extraction, `amount_eur` from the regex/LLM tier, `financial_instrument_class` inferred from the decision text (`grant` / `loan` / `guarantee` / `tax_advantage`), plus the new `amount_confidence` values and an `is_adhoc_preloaded = True` marker.
6. **`integrate_enrichment`** picks up the CSV alongside the other enrichment outputs. The existing `_flag_pdf_cofin_overlaps` step is a no-op for these rows because they have no sibling state-aid row to duplicate — they are additive coverage. The existing `_flag_ipcei_tam_overlap` step is also a no-op because the injected rows are `TAM`-sourced, not `IPCEI_state_aid`-sourced.

**Cost estimate.** 1,243 PDFs × 1 download each = one-off ~2 GB cache (PDFs average ~1-2 MB). Run time ≈ 20–30 minutes on the rate-limited downloader (1 req/sec). LLM fallback at $0.0014/PDF × up to 1,243 ≈ $1.75 if every PDF fails regex; realistically ≤$1 because regex will hit ~60–80% of cases.

**Open questions.**

- **Deduplication against post-2016 TAM rows.** For ad hoc cases whose SA code *does* appear in TAM (post-2016, correctly reported), the preload would duplicate the TAM row. The cleanest mitigation is to skip any SA code that already appears in the harmonized TAM output — a one-line filter using the existing master parquet's `source_record_id` column. This limits the feature to genuinely missing cases.
- **Amount-precision trade-off.** Regex extraction will mis-parse some cases; LLM will hallucinate on others. The feature should ship with `amount_confidence` clearly surfaced and downstream charts filterable by it.
- **Phase-2 extension.** The same infrastructure could extract amounts from the ~322 French and ~238 German single-company ad hoc PDFs via the LLM tier, adding ~500 more rows. Out of scope for phase 1.

**Status.** **Proposed but not built.** User asked specifically for a feasibility check; phase-1 is a single medium-sized module (~300-400 LOC) that I can implement in one focused session once the defence run is off the critical path. Flag for implementation next session unless the user requests it sooner.

---

### H7 — RRF is harmonized but never reaches the matcher — **[OPEN, documented 2026-04-12]**

**Where.** [src/harmonization/rrf.py:114](../src/harmonization/rrf.py#L114): `out['beneficiary_name'] = None`. Consolidation's heuristic RRF branch at [consolidation.py:861](../src/matching/consolidation.py#L861).

**What's wrong.** The RRF module docstring (lines 11-20) correctly states that the source is "measure-level PLANNED allocations (NOT beneficiary-level disbursements)" and sets `beneficiary_name = None`, `granularity = 'measure'`, `flow_stage = 'planned'` on every row. The harmonizer does its job correctly.

The downstream consequence, however, is that **RRF rows never flow through to any consolidated output**. The entity matcher ([src/matching/generic_matcher.py](../src/matching/generic_matcher.py)) matches by beneficiary name against the user's company list; rows with `beneficiary_name = None` cannot match anything, so no RRF row ever appears in `match_log.csv` or `consolidated_matches.csv`. The RRF rows sit in the master parquet as primary records and are only ever referenced by the RRF branch of `_flag_cofinancing_overlaps`, which merges on `match_reference_name + country`. Because no RRF row has ever been assigned a `match_reference_name` (it was never in the match_log), that merge returns zero rows every time. **The RRF branch of the heuristic is dead code.**

In other words: the pipeline *presents itself* as analyzing twelve data sources including RRF, but the entity-level output in practice covers eleven. RRF contributes zero rows to any company's total, zero flags to any dedup decision, and zero bytes to any downstream chart. The README and METHODOLOGY claims about "RRF recovery grants sometimes notified as state aid in TAM" describe a detection path that cannot currently fire — not because the logic is wrong, but because the upstream data does not carry the join key it depends on.

**Why it matters.** Two failure modes:

1. **Silent under-coverage.** Users who inspect the data sources list and see RRF listed as a ~500-row source reasonably conclude that their headline totals include any RRF-backed allocation to their companies. They do not.
2. **Dead code masquerading as a safety net.** M4 earlier in this audit flagged the asymmetric fund filter on the RRF branch — fixing that is moot because the branch never executes. Future maintainers may read the RRF branch and assume it is a working dedup path; it is not.

**Proposed fix.**

1. **Immediate documentation fix.** README, METHODOLOGY, and MATCHING_GUIDE must state clearly that RRF is retained in the master dataset for contextual purposes but is **not** part of the beneficiary-level matching pipeline. Anywhere the RRF↔TAM dedup mechanism is currently described as a live path, add an explicit "not yet integrated — requires entity-level RRF data" caveat.
2. **Short-term code cleanup.** Remove `'RRF'` from the preferred-source loop in `_flag_cofinancing_overlaps` ([consolidation.py:861](../src/matching/consolidation.py#L861)), with a comment pointing to this audit item. This is a no-op behaviour change (the branch never fired) but removes a future correctness trap.
3. **Long-term.** Entity-level RRF data is available from individual Member State recovery-plan portals (Italy's *Italia Domani*, Spain's *Plan de Recuperación*, France's *France Relance*). A harmonization module per portal is the right path. Until then, RRF remains a measure-level contextual source only and should be documented as such everywhere.

**Documentation.** [research/METHODOLOGY.md](METHODOLOGY.md) §1.1, §7.3, and §12 now carry the caveat. README and MATCHING_GUIDE will get the same treatment as part of the source-bucket sweep.

---

## Medium

### M1 — `--pdf-enrichment` default is split between the CLI and the Python API — **[DONE 2026-04-12]**

**Applied.** Flipped both Python-level defaults to `True`: `consolidate(run_pdf_enrichment=True, ...)` ([consolidation.py:1171](../src/matching/consolidation.py#L1171)) and `stage_match(pdf_enrichment=True, ...)` ([run_pipeline.py:170](../run_pipeline.py#L170)). The CLI default was already `True`. Added a visible `log.info` line at the start of Phase 2c that says `Phase 2c: SA PDF co-financing enrichment... (ON — pass run_pdf_enrichment=False or CLI --no-pdf-enrichment to skip)`, and an `ON vs SKIPPED` branch on the disabled path that also announces that the pipeline is falling back to the heuristic amount-ratio dedup alone. Library callers who import `consolidate()` directly now get the same default as the CLI; callers who want the old behaviour must opt out explicitly.

**Original finding below (retained for reference).**

**Where.** Argparse definition [run_pipeline.py:521-538](../run_pipeline.py#L521-L538) defaults `--pdf-enrichment` to `True`. The Python-level default in `consolidate(..., run_pdf_enrichment: bool = False, ...)` ([consolidation.py:1171](../src/matching/consolidation.py#L1171)) is `False`. `stage_match(..., pdf_enrichment: bool = False, ...)` ([run_pipeline.py:170](../run_pipeline.py#L170)) is also `False`.

**What's wrong.** If a user runs the pipeline from the CLI they get PDF enrichment on. If a notebook or test imports `consolidate()` directly and does not pass the flag, they silently get the heuristic fallback alone. This mismatch is easy to trip over and means the library and the CLI behave differently out-of-the-box.

**Why it matters.** Programmatic callers silently miss the authoritative dedup path — their `dc_flag` values will contain only `cofinancing_overlap:tam_kohesio` / `cofinancing_overlap:tam_rrf` (heuristic) and never `tam_*_pdf` (authoritative). A downstream audit that greps for `_pdf` flags would draw the wrong conclusion about pipeline behaviour.

**Proposed fix.** Pick one default and use it everywhere. Recommended: flip both Python defaults to `True` so that the library and CLI agree. Add a one-line `log.info("PDF enrichment: ON (--no-pdf-enrichment to disable)")` at the start of Phase 2c so the choice is visible in every run log.

### M2 — No structural validation of enrichment output schemas — **[DONE 2026-04-12]**

**Applied.** A new `ENRICHMENT_SCHEMA` dict in [consolidation.py](../src/matching/consolidation.py) defines a minimal contract for each enrichment output: a list of required columns and the canonical amount column. A new `_validate_enrichment_schema` helper is called from inside `_load_enrichment_csv` whenever a `schema_key` is passed. Behaviour on violation:

- **Missing required column** → log a warning naming the missing column, return an empty DataFrame so the downstream merge silently skips the bad file instead of corrupting the core frame.
- **Amount column entirely NaN** → log a warning that the CSV is name-only coverage and cannot contribute to headline totals.
- **Amount column partly populated** → log the coverage ratio (N-with-amounts / N-total) so the reader can sanity-check against previous runs.
- **Duplicate `source_record_id`** → log a warning with the duplicate count; most often indicates an upstream join bug that emits the same row twice.

Validation is wired into the four real enrichment loaders inside `integrate_enrichment`: FTS-CORDIS (`schema_key='fts_cordis'`), EIB promoter (`eib_promoter`), IPCEI (`ipcei`), and EU ETS (`ets_free_allocation`). The schema entries are small and intentionally permissive — they are a *contract*, not a strict type check, so a working CSV should never hit a false-positive rejection.

**Follow-up.** Stricter validation (column type checks, EUR-range sanity bounds, year-range bounds) is possible but would start rejecting legitimate historical edge cases. The current lightweight checks are the right default.

**Original finding below (retained for reference).**

**Where.** `integrate_enrichment` ([consolidation.py:318](../src/matching/consolidation.py#L318)) reads the five post-match enrichment CSVs and concatenates them via `_align_and_concat` ([consolidation.py:485](../src/matching/consolidation.py#L485)).

**What's wrong.** Each enrichment script writes its own CSV with its own column set. The concat is column-aligned, but there is no schema check: if a script accidentally writes a row with a duplicate `source_record_id`, a missing `amount_eur`, a wrong `source` value, or a column rename, the error propagates silently into consolidation. Some of these bugs would inflate totals; others would break dedup merges without failing loudly.

**Why it matters.** The enrichment layer is the part of the pipeline most likely to churn over time (new sources, new cross-joins). Each new script becomes a potential source of silent schema drift.

**Proposed fix.**

1. Define a minimal `EnrichmentOutputSchema` (required columns, types, allowed `source` values) alongside `MasterConfig`.
2. Validate each enrichment CSV against the schema as `integrate_enrichment` loads it. On mismatch, log a warning and skip the offending rows (not the whole file).
3. Add a summary at the end of `integrate_enrichment` reporting the row counts kept and rejected per enrichment source.

### M3 — PDF downloader is synchronous and has no aggregate timeout — **[DONE 2026-04-12]**

**Applied.** Two visibility-and-resilience changes to `SACofinParser` in [sa_pdf_parser.py](../src/enrichment/sa_pdf_parser.py):

1. **Progress log every 25 downloads.** `enrich_dataframe` now pre-counts how many PDFs it needs to fetch (after cache lookup) and emits a `[PDF dl progress] {i}/{n_to_download} (XX% success, avg latency YY.Ys, ETA Z min)` line every 25 completed downloads and at the end of the phase. Latency is a rolling average over the last `circuit_breaker_window` (20) successful downloads. The user no longer has to guess whether Phase 2c is making progress or silently stuck on a slow EC endpoint.
2. **Circuit breaker.** New state in `__init__`: `circuit_breaker_window=20`, `circuit_breaker_threshold=0.6`, `circuit_breaker_cooldown=60.0`. Every `_download` outcome (success or exhausted-retries failure) is recorded in a rolling deque. If the failure rate over the last `window` downloads exceeds `threshold`, the breaker trips, logs a visible warning, and the next `_download` call pauses for `cooldown` seconds before resetting the history and resuming. This keeps the pipeline alive through transient EC website outages without thrashing the 3-retry exponential backoff against every URL.

Smoke-tested: 6 synthetic failures with `window=5, threshold=0.5` trips the breaker as expected. The constructor parameters are exposed so notebook callers can tune them if the defaults prove wrong on a specific run.

**Left open.** An *aggregate* wall-clock timeout (e.g. `--pdf-timeout-total 1800` to cap the entire Phase 2c step at 30 minutes) is the one piece the finding proposed that I did not build, because it introduces a behaviour subtlety — if the timeout fires mid-enrichment, the partially-populated `sa_cofin_*` columns need to be propagated to the dataframe without misleading downstream dedup. I'd rather not ship that without a design pass and a real run to validate. The progress log plus circuit breaker cover ~90% of the practical pain the finding described.

**Original finding below (retained for reference).**

**Where.** `SACofinParser` ([sa_pdf_parser.py:575+](../src/enrichment/sa_pdf_parser.py#L575)), `enrich_dataframe` at line 794.

**What's wrong.** The parser downloads PDFs inline during consolidation (1 req/sec with 3 retries and exponential back-off per the docstring). Partial failures are logged and skipped, which is the correct policy, but there is no aggregate timeout or circuit breaker: if the EC website is slow or intermittently failing, the Phase 2c step can take much longer than the rest of the pipeline combined without the user knowing why. `max_workers=6` is available in the signature but the run-time behaviour when the EC server is throttling is not documented.

**Why it matters.** This is the slowest step for large company lists and is the most likely to break on a transient external dependency.

**Proposed fix.**

1. Log a progress line every N PDFs with EWMA latency and an ETA.
2. Add a circuit breaker: if the error rate over the last K downloads exceeds a threshold, pause for a configurable back-off interval rather than grinding through every retry.
3. Expose a `--pdf-timeout-total <seconds>` flag that caps the wall-clock time the step can consume; on exceeding, save what you have, log the skipped SA codes, and move on to Phase 2b (which remains a safe no-op for skipped rows).
4. Document the caching behaviour: PDFs cached in `output_dir/sa_decisions/` persist across runs, so a second run will only pay the download cost for new SA codes.

### M4 — RRF branch of the heuristic fallback has no fund filter

**Where.** [consolidation.py:861-895](../src/matching/consolidation.py#L861-L895). The `for preferred_src in ['KOHESIO', 'RRF']:` loop applies the fund-alias filter only when `preferred_src == 'KOHESIO'`.

**What's wrong.** The fund filter is the main thing that prevents the heuristic from flagging coincidental matches where the amount ratio happens to fall in `[0.01, 1.50]`. For KOHESIO the filter is applied, and a row whose `fund` column does not match a known structural fund is dropped. For RRF, the filter is skipped entirely. The code comment notes that RRF rows typically have an empty `fund` column (because the harmonization layer does not currently extract it), so skipping the filter is defensible — but it means any RRF row that *does* happen to have fund-like text in its fund column is not filtered, and more importantly, RRF is the branch where over-flagging is least visible because RRF row counts are small.

**Why it matters.** RRF is currently measure-level and rarely has a beneficiary name that matches the user's company list, so the branch is cold most of the time. Once entity-level RRF data becomes available (a scenario the codebase is already prepared for), this branch will start firing and any latent over-flagging will go straight into headline totals.

**Proposed fix.** Either (a) populate `fund` on RRF during harmonization and re-enable the filter symmetrically for both sources, or (b) add a terse comment in `_flag_cofinancing_overlaps` explaining the asymmetry and a `log.warning` the first time any RRF row is flagged, so the transition is visible when RRF entity-level data arrives.

### M5 — Match-quality check is asymmetric across sources — **[DONE 2026-04-12]**

**Applied.** `assess_match_quality` ([consolidation.py:142-230](../src/matching/consolidation.py#L142-L230)) now runs **two** independent suspect checks.

1. The original description-only check for KOHESIO/FTS/CINEA rows is unchanged.
2. A new IFI-title check fires on any EIB/EBRD row whose `match_type` contains `eib_title` (i.e. the Layer B+ title-extraction matches). A row is re-tagged `match_quality = 'suspect_eib_title'` when the cleaned reference name is either (a) a single short token — too fragile to infer a title match from, because too many title words could accidentally satisfy a one-token reference — or (b) composed entirely of generic IFI-project boilerplate (`project`, `programme`, `energy`, `infrastructure`, `investment`, `facility`, `loan`, `sme`, `midcap`, etc. — see `_GENERIC_IFI_TITLE_TOKENS` at [consolidation.py:140-148](../src/matching/consolidation.py#L140-L148)). Such reference names cannot meaningfully discriminate a real match from a generic title hit.

The flag is advisory — `dc_preferred` is not touched — so downstream audits can filter on `match_quality == 'suspect_eib_title'` without losing any rows. The Phase 3 log line reports the count and EUR of each suspect bucket separately.

**Deliberately out of scope.** Computing a per-row confidence proxy (span length, token frequency against the title corpus, etc.) would require storing the original title text alongside the extracted span. That's a follow-up for when the matcher is refactored to emit Layer B+ candidates with richer metadata.

**Original finding below (retained for reference).**

**Where.** `assess_match_quality` ([consolidation.py:142-190](../src/matching/consolidation.py#L142-L190)) only flags description-matched rows from KOHESIO, FTS, and CINEA.

**What's wrong.** EIB description-only matches are intentionally excluded from the check because the EIB `beneficiary_name` field literally *is* the project title, so the reference-name-in-beneficiary-name test would trivially fail even for good matches. The code comment at [consolidation.py:149-150](../src/matching/consolidation.py#L149-L150) explains this — but the result is that EIB/EBRD rows are never re-tagged `suspect_description_only` under any circumstance, while the same description match in FTS would be. There is no corresponding quality check targeted at EIB's specific failure modes (over-generic project titles like "Offshore Wind Programme" matching to "Offshore Wind Ltd"; multilingual titles where extraction picks the wrong span).

**Why it matters.** EIB/EBRD title extraction is the pipeline's lowest-confidence matching layer and it gets the most permissive quality treatment. The asymmetry is not wrong per se — it is load-bearing given the structure of the data — but it should be acknowledged and an EIB-specific suspect flag should be added.

**Proposed fix.**

1. Add an EIB-specific check: for `source in ('EIB', 'EBRD')` rows that matched on `eib_title_extraction`, compute a confidence proxy (length of the extracted span relative to the title; presence of the extracted token elsewhere in the title; match against a blocklist of over-generic title words), and set `match_quality = suspect_eib_title` when the proxy falls below a threshold.
2. Document the asymmetry in [research/METHODOLOGY.md](METHODOLOGY.md) §5.4 (currently documented as "intentionally excluded" — add the "and therefore has no quality check, pending M5" part).

---

## Low

### L1 — Master-build vs consolidation exclusion semantics are easy to confuse

**Where.** `MasterConfig` ([src/master/builder.py:43-80](../src/master/builder.py#L43-L80)) applies hard exclusions; `_flag_*` functions in [consolidation.py](../src/matching/consolidation.py) apply soft `dc_preferred=False` flags.

Both mechanisms are correct and serve different purposes — hard exclusions keep the master dataset shape stable and prevent the matcher from spending cycles on known-out-of-scope rows, while soft flags preserve the data for audit. But a reader who only looks at `consolidated_matches.csv` will not see master-build exclusions at all (they were filtered before the matcher ran), and a reader who only looks at the master parquet will see rows with `is_primary_record=False` that are semantically different from `dc_preferred=False`.

**Proposed fix.** Documentation only — the methodology rewrite already covers this in §4 and §11.3. Keep it there.

### L2 — Per-source-pair year windows are not configurable — **[DONE 2026-04-12]**

**Applied.** A new `DedupConfig` dataclass in [consolidation.py](../src/matching/consolidation.py#L38-L82) collects every previously-hardcoded dedup threshold into a single configurable surface:

| Field | Default | What it controls |
|---|---|---|
| `pdf_cofin_year_window` | 2 | Year window for `_flag_pdf_cofin_overlaps` (the document-backed authoritative step). |
| `heuristic_cofin_year_window` | 2 | Year window for `_flag_cofinancing_overlaps` (the heuristic fallback). |
| `ipcei_tam_year_window` | 2 | Year window for `_flag_ipcei_tam_overlap`. |
| `heuristic_cofin_ratio_min` | 0.01 | Lower bound of the KOHESIO/TAM plausibility ratio. |
| `heuristic_cofin_ratio_max` | 1.50 | Upper bound of the KOHESIO/TAM plausibility ratio. |
| `ipcei_tam_amount_tolerance` | 0.20 | Amount tolerance for the IPCEI↔TAM dedup. |
| `fts_innovfund_amount_tolerance` | 0.001 | Identical-transaction tolerance for the FTS↔INNOVFUND dedup (not yet wired through the `_dedup_fts_identical_transactions` function — follow-up). |

`consolidate()` gained an optional `dedup_config: DedupConfig | None` parameter that defaults to `DEFAULT_DEDUP_CONFIG`. Existing runs are byte-identical because every default matches the legacy hardcoded value. Callers who want to tune a single threshold pass a dataclass with the override:

```python
from src.matching.consolidation import consolidate, DedupConfig
consolidate(..., dedup_config=DedupConfig(heuristic_cofin_ratio_max=2.0))
```

The three dedup functions (`_flag_cofinancing_overlaps`, `_flag_pdf_cofin_overlaps`, `_flag_ipcei_tam_overlap`) now receive their thresholds via parameters rather than hardcoded literals. This resolves L2 as a standalone item and unblocks the empirical tuning proposed in H3 — a future run of H3 against the PDF-confirmed gold set can fit the band empirically and set it via `DedupConfig` instead of patching function bodies.

**Not yet wired.** `_dedup_fts_identical_transactions` still uses a hardcoded `0.001` ratio tolerance. Threading it through `DedupConfig` is a one-line follow-up; left out to keep this commit tight.

**Original finding below (retained for reference).**

**Where.** `_flag_cofinancing_overlaps(df, year_win: int = 2)`, `_flag_pdf_cofin_overlaps(df, year_win: int = 2)`, `_flag_ipcei_tam_overlap(df, amount_tol: float = 0.20, year_win: int = 2)`.

All three functions take `year_win` as a parameter but are always called with the default at the Phase 2b orchestration site ([consolidation.py:1311-1315](../src/matching/consolidation.py#L1311-L1315)). There is no user-facing configuration surface. This is coupled to **H3** — resolving H3 also resolves L2 — but keeping it on the list as a standalone reminder that the plumbing is already in place; only the `MatchConfig` fields and the call-site wiring are missing.

### L3 — PDF cache lives in the run's output directory — **[DONE 2026-04-12]**

**Applied.** Both Phase 2c inside `consolidate()` ([consolidation.py:1300-1316](../src/matching/consolidation.py#L1300-L1316)) and `stage_enrich_pdf()` in `run_pipeline.py` now default `pdf_cache_dir` to the repo-level `data/cache/sa_decisions/` directory, falling back to a run-local `sa_decisions/` folder only if the shared path is unwritable. The cache directory is created on first use and logged at the start of every Phase 2c run. PDFs now accumulate across match runs and across company lists — the same SA code is downloaded once and reused forever. Explicit `pdf_cache_dir` arguments still override the default.

**Original finding below (retained for reference).**

**Where.** `pdf_cache_dir` parameter of `consolidate()` defaults to `output_dir / 'sa_decisions'` ([consolidation.py:1272](../src/matching/consolidation.py#L1272)).

**What's wrong.** Every new match run re-downloads the PDFs unless the user manually points `pdf_cache_dir` at a shared location. Each TAM row's SA code is immutable across runs — a company list that was matched last week and is re-matched this week with a different reference list will hit the same SA codes and pay the same download cost twice.

**Proposed fix.** Default `pdf_cache_dir` to a repo-level `data/cache/sa_decisions/` (create it on first use) so that PDFs accumulate across runs. Add a note in the README about cache size and how to clear it.

### L4 — No traceability column for the name-dedup optimization — **[DONE 2026-04-12]**

**Applied.** [generic_matcher.py](../src/matching/generic_matcher.py) now computes a `collections.Counter` over `entity_name_clean` during the Phase 1 scan of the master parquet (instead of collecting only a set of unique names), and carries that count onto every Layer A match as an `entity_name_clean_dedup_count` column. The column is added to the `log_cols` list that writes `match_log.csv`, so every downstream step — `integrate_enrichment`, `assess_match_quality`, the dedup functions, the summary tables — sees it. A downstream audit that asks "how many master rows contributed to the match for Company X?" can now answer directly from the match log without re-reading the 27M-row parquet.

Layer B (contextual) matches are scored row-by-row with no dedup shortcut, so the column remains `0` for those rows — the metric is meaningful only for Layer A. This asymmetry is intentional and the column name makes it obvious what's being counted.

**Original finding below (retained for reference).**

**Where.** The matcher's ~920K-unique-name optimization ([src/matching/generic_matcher.py](../src/matching/generic_matcher.py)) scores each unique `entity_name_clean` once and joins the result back to the full master.

**What's wrong.** The match log records the final join result but not how many underlying master rows shared each `entity_name_clean`. If a downstream audit asks "how many master rows contributed to each reference-name match?", the answer is not in the output; the auditor has to re-compute it from the master parquet.

**Proposed fix.** Add an optional `entity_name_clean_dedup_count` column to `match_log.csv` recording the number of master rows that hit each scored name. Cheap to compute (single `groupby().size()`), and surfaces the optimization's effect without changing any matching semantics.

### L5 — The `sa_pdf_parser.py` module docstring references CRM relevance extraction — **[DONE 2026-04-12]**

**Applied.** Rewrote the top-of-module docstring in [sa_pdf_parser.py:1-11](../src/enrichment/sa_pdf_parser.py#L1-L11) to describe only the sector-agnostic co-financing extraction role. The old description of a dual "Tier B co-financing + CRM relevance" responsibility is gone. The rest of the module (function docstrings, regex comments) was untouched.

**Original finding below (retained for reference).**

**Where.** [src/enrichment/sa_pdf_parser.py:1-9](../src/enrichment/sa_pdf_parser.py#L1-L9).

**What's wrong.** The module docstring describes a "Tier B co-financing extraction + CRM relevance detection" dual role, but the base repo is sector-agnostic and ships no CRM configuration. This appears to be a remnant from an earlier version or a fork-specific adaptation that leaked into the base docstring.

**Proposed fix.** Documentation-only: edit the module docstring to describe only the co-financing extraction role so new readers are not misled. (This is an `src/` edit and therefore outside the scope of the current documentation-only work — listed here so it is not lost.)

---

## Not bugs, but worth noting in the methodology

- **The PDF-backed dedup is generalized to all non-TAM sources, not just KOHESIO.** `_flag_pdf_cofin_overlaps` emits per-source flags (`tam_kohesio_pdf`, `tam_fts_pdf`, `tam_esif_2014_pdf`, etc.) from a single generic merge over all non-TAM rows ([consolidation.py:1089](../src/matching/consolidation.py#L1089)). The methodology rewrite now reflects this; older prose that named KOHESIO specifically was under-describing the real scope.
- **"Conditional" PDF evidence is deliberately ignored for dedup.** The Tier-2 regex exists specifically because many Temporary-Framework / TCTF decisions contain boilerplate conditional clauses ("to the extent the measure is co-financed by…") that are not commitments. Surfacing these as `sa_cofin_level = 'conditional'` lets auditors see them without contaminating the dedup path.
- **`_flag_ipcei_tam_overlap` uses a 20% amount tolerance**, not the 1.5 plausibility ratio used for TAM↔KOHESIO. This is correct and intentional — IPCEI SA decisions carry an *estimated* aid amount at approval time that can differ from the TAM notification by more than zero but not by orders of magnitude. The threshold is a sensible middle ground, though it shares H3's lack of empirical validation.

---

## Cross-cutting observation: where the pipeline's real dedup strength comes from

A reader of the older methodology docs would have concluded that dedup is dominantly heuristic — amount ratios and year windows applied to TAM↔KOHESIO pairs. That is not the pipeline's current centre of gravity. The load-bearing mechanism is:

1. Every matched TAM row is resolved to a canonical SA code.
2. The EC DG Competition case registry gives us the decision PDFs for that code.
3. A GBER-table / regex / LLM stack extracts a *named* EU fund from the decision document.
4. If a counterpart row exists in any non-TAM source on the same (entity, country, year-window) and its fund matches the named fund, the TAM row is flagged as a document-confirmed duplicate.

The heuristic only runs on TAM rows that this document-grounded path did not already resolve. That inversion is what the methodology rewrite elevates, and it is the single most important thing to preserve when the pipeline evolves: any future refactor of consolidation must keep `_flag_pdf_cofin_overlaps` running *before* `_flag_cofinancing_overlaps` and must keep the `dc_preferred` mask in the heuristic that gates it to residual rows ([consolidation.py:856](../src/matching/consolidation.py#L856)). Losing either of those would silently re-promote the heuristic to primary status and revert the pipeline to its older, weaker behaviour.
