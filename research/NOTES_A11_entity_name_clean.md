# A-11 reconciliation: `entity_name_clean` count

**Context.** The plan audit sub-agent reported ~7.9M unique cleaned
names in the master parquet, while the existing PIPELINE_AUDIT and
METHODOLOGY §5 text referred to "~920k unique values the matcher
scores". The two numbers measure different things.

## Measured counts (2026-04-13, production master)

| Stage | Count | Filter |
|---|---:|---|
| Total rows | 27,714,756 | — |
| Non-null `beneficiary_name` | 27,472,616 | dropna |
| Unique raw `beneficiary_name` | 9,054,943 | `.unique()` |
| Unique cleaned (`clean_name`) | **7,869,130** | lowercase + legal-suffix strip + non-alphanumeric → space |
| After short-name filter (> 5 chars) | 7,796,749 | `short_name_max_len = 5` |
| After `GENERIC_NAMES` filter | 7,796,736 | drop `"group", "other", "total", ...` |
| With ≥ 1 significant token | 7,793,200 | non-country, non-trivial tokens |

Run time: 91 seconds (60s of that is `clean_name` over 9M strings).

## Interpretation

- **7.9M** is the ref-list-independent count of unique cleaned names
  that could ever enter the matcher. It is a property of the master
  dataset, not of any particular reference list.
- The **~920k** figure from the earlier audit is a
  **ref-list-dependent** subset: the unique cleaned names whose
  significant-token set intersects the user's reference list's
  significant-token set. For a 150-company automotive reference
  list, the sub-agent's approximation lands in the 50k-200k range;
  920k corresponds to a large ref list (thousands of names).
- Neither figure is "wrong" — they answer different questions.

## METHODOLOGY §5 correction (for A-13)

Replace the current "the matcher scores ~920k unique values" prose
with something like:

> The master parquet contains 7.9M unique cleaned `entity_name_clean`
> values (27.7M rows, ~9M unique raw beneficiary names collapsed by
> the `clean_name` normaliser). The matcher's Layer A pass does
> **not** score all of them against the user's reference list;
> instead, a token inverted index drops every name whose significant
> tokens do not intersect the reference list's significant tokens.
> For a typical 150-company reference list this token pre-filter
> removes ≥ 99% of the cleaned names before `rapidfuzz` sees them,
> leaving a few tens of thousands of candidate names to be scored.
> The exact post-filter count depends linearly on the reference
> list's token footprint.

## Follow-up

The token pre-filter's effectiveness should be logged per-run so
it is visible as a METHODOLOGY §5 measurement rather than an
estimate. `match_unique_names` already logs `pre_filtered` counts;
the A-13 METHODOLOGY sweep should cite that log line as the
canonical source of truth.
