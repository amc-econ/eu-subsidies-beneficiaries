# EU Subsidies Beneficiaries

A unified beneficiary-level dataset of public subsidies in the European
Union: state aid, EU funds, and development bank lending, with resolved
beneficiary names and grant-equivalent amounts.

For more detail — what the dataset is, how it is built, and what it has been
used for — see the overview deck, [presentation.pdf](presentation.pdf).

## What's inside

It brings the EU's scattered subsidy registers into one beneficiary-level
table:

- **State aid** — the EU Transparency Award Module and national registers
  (Spain, Poland, Romania, Slovenia) below the EU threshold
- **Cohesion policy** — the European Structural and Investment Funds, at
  project level
- **Directly managed EU funds** — the Commission's Financial Transparency
  System, by programme (Horizon, CEF, LIFE, Erasmus+, and more)
- **Research** — Horizon participations from CORDIS
- **Climate and infrastructure** — CINEA grants, including the Innovation Fund
- **Development banks** — EIB and EBRD lending

Each row is one beneficiary–support relation: who received what, in which
instrument (grant, loan, guarantee, equity, tax advantage), at both face
value and grant equivalent, in which year and under which programme, financed
by whom. Beneficiaries are entity-resolved with national identifiers where
available, so the table joins cleanly to firm-level data. Coverage runs over
several decades and is densest for recent years.

## Use

```
pip install -r requirements.txt
python src/match_companies.py --company-list my_companies.csv
```

Replace `my_companies.csv` with your own list: a `company_name` column,
optionally a `country` column. The dataset (~1.7 GB) downloads on first
run; results land in `data/processed/match_output/`.

## Columns

Each row is one beneficiary–support relation. The columns you'll use:

| Column | Meaning |
|---|---|
| `match_reference_name` | the company/group from your list this row is attributed to |
| `beneficiary_name` | recipient name as it appears in the source register |
| `amount_eur` | support at face value, EUR |
| `amount_gge` | support as grant equivalent, EUR — comparable across instruments |
| `financial_instrument_class` | Grant, Loan, Guarantee, Equity, Tax advantage, Other |
| `year` | year of the award |
| `country` | granting country (ISO-2) |
| `source` | register of origin: TAM, KOHESIO, FTS, EIB, EBRD, CINEA, RRF, IPCEI, ESIF |
| `fund` | EU fund behind it: ERDF, ESF, Cohesion Fund, RRF |
| `fiscal_source_type` | EU or national financing |
| `parent_group` | corporate group the entity rolls up to |
| `dc_preferred` | the canonical row — sum `amount_eur` where this is true |

Totals run over `dc_preferred` rows. The dataset keeps cross-source duplicates
(state aid also recorded as an EU fund, say) but marks all but one
`dc_preferred = False`; the `T1`–`T8` tables and `concentration_metrics.json`
already apply this.

Developed at Bruegel by Antoine Mathieu Collin and Benjamin Bjerkan-Wade.
MIT license.
