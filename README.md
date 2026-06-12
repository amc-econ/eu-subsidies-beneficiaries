# EU Subsidies Beneficiaries

A unified beneficiary-level dataset of public subsidies in the European
Union: state aid, EU funds, and development bank lending, with resolved
beneficiary names and grant-equivalent amounts.

## Use

```
pip install -r requirements.txt
python match_companies.py --company-list my_companies.csv
```

Replace `my_companies.csv` with your own list: a `company_name` column,
optionally a `country` column. The dataset (~1.7 GB) downloads on first
run; results land in `data/processed/match_output/`.

Developed at Bruegel by Antoine Mathieu Collin and Benjamin Bjerkan-Wade.
MIT license.
