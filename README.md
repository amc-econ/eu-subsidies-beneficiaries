# EU Subsidies Beneficiaries

A unified beneficiary-level dataset of public subsidies in the European
Union: national state aid, EU funds under shared and direct management,
and EIB/EBRD lending, harmonised into one schema with grant-equivalent
amounts and resolved beneficiary names.

28.5 million records from 15 public registers, densest for 2014-2025.
Over 2016-2025 the data records EUR 2.7 trillion of budgetary support in
grant-equivalent terms, plus EUR 0.4 trillion of development bank lending.

## Quick start

```
git clone https://github.com/amc-econ/eu-subsidies-beneficiaries.git
cd eu-subsidies-beneficiaries
pip install -r requirements.txt
python match_companies.py --company-list my_companies.csv
```

The first run downloads the dataset (~1.7 GB) from this repository's
releases. Results land in `data/processed/match_output/`: every matched
award with source, year, instrument, amount, and grant equivalent, plus
summary tables.

## Use your own companies

Replace `my_companies.csv` with your list: a CSV with a `company_name`
column and an optional `country` column (ISO 2-letter). Nothing else
changes.

## Sources

| Family | Registers | Level |
|---|---|---|
| State aid | EU transparency register; ES, PL, RO, SI national registers | award |
| EU funds | Kohesio, FTS, CORDIS, CINEA | project / commitment |
| Lending | EIB, EBRD | operation |

## Citation

Bjerkan-Wade, B. and A. M. Collin (2026), *EU Subsidies Beneficiaries*,
Bruegel. See `CITATION.cff`.

## License

MIT
