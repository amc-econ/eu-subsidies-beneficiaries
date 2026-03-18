"""
harmonization/ecb_fx_rates.py
=============================
Annual average exchange rates from the ECB Statistical Data Warehouse.

Source: https://sdw.ecb.europa.eu/ (EXR.A.RON.EUR.SP00.A)
Hardcoded for reproducibility — avoids runtime API calls.
"""

import pandas as pd

# Annual average RON/EUR exchange rates (ECB reference rates)
RON_EUR_ANNUAL: dict[int, float] = {
    2007: 3.3373,
    2008: 3.6826,
    2009: 4.2399,
    2010: 4.2122,
    2011: 4.2391,
    2012: 4.4593,
    2013: 4.4190,
    2014: 4.4437,
    2015: 4.4454,
    2016: 4.4904,
    2017: 4.5688,
    2018: 4.6540,
    2019: 4.7453,
    2020: 4.8383,
    2021: 4.9215,
    2022: 4.9313,
    2023: 4.9467,
    2024: 4.9756,
    2025: 4.9770,  # YTD estimate
    2026: 4.9770,  # carry forward
}

_FALLBACK_RATE = RON_EUR_ANNUAL[max(RON_EUR_ANNUAL.keys())]


def ron_to_eur(amount_ron: float, year: int) -> float:
    """Convert a single RON amount to EUR using the annual average rate."""
    rate = RON_EUR_ANNUAL.get(int(year), _FALLBACK_RATE)
    return amount_ron / rate


def convert_ron_series(amount_series: pd.Series, year_series: pd.Series) -> pd.Series:
    """Vectorised RON→EUR conversion for a pandas DataFrame."""
    rates = year_series.map(RON_EUR_ANNUAL).fillna(_FALLBACK_RATE)
    return amount_series / rates
