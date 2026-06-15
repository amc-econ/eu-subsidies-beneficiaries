# EU Subsidies Beneficiaries

A unified beneficiary-level dataset of public subsidies in the European
Union: state aid, EU funds, and development bank lending, with resolved
beneficiary names and grant-equivalent amounts.

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
pip install pandas numpy pyarrow rapidfuzz
python src/match_companies.py --company-list my_companies.csv
```

Replace `my_companies.csv` with your own list: a `company_name` column,
optionally a `country` column. The dataset (~1.7 GB) downloads on first
run; results land in `data/processed/match_output/`.

Developed at Bruegel by Antoine Mathieu Collin and Benjamin Bjerkan-Wade.
MIT license.
