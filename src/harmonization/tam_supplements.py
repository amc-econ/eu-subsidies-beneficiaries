"""
harmonization/tam_supplements.py
================================
Orchestrator for 4 TAM supplement countries: Romania, Slovenia, Spain, Poland.

These countries are missing from the main TAM dataset and are loaded from
national state aid registers / APIs. All outputs use source='TAM' so they
merge seamlessly with the existing TAM data in downstream processing.

Usage:
    Called by harmonize_all.py as a registered source.
    Produces standardized_tam_supplements.csv.
"""

import logging
from pathlib import Path

import pandas as pd

from .utils import COMMON_COLUMNS

# Lazy imports to avoid circular dependencies and allow partial execution
# (e.g., if Spain data hasn't been scraped yet)


def standardize(data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Load and standardize all 4 TAM supplement countries."""
    log.info("\n=== TAM SUPPLEMENTS (RO, SI, ES, PL) ===")

    frames = []
    countries = [
        ('harmonization.tam_ro', 'Romania (RO)'),
        ('harmonization.tam_si', 'Slovenia (SI)'),
        ('harmonization.tam_es', 'Spain (ES)'),
        ('harmonization.tam_pl', 'Poland (PL)'),
    ]

    for module_path, label in countries:
        try:
            import importlib
            mod = importlib.import_module(module_path)
            df = mod.standardize(data_dir, log)
            if df is not None and not df.empty:
                # Force source = TAM for all supplements
                df['source'] = 'TAM'
                frames.append(df)
                log.info(f"  {label}: {len(df):,} rows, "
                         f"EUR {df['amount_eur'].sum():,.0f}")
            else:
                log.warning(f"  {label}: no data (skipped)")
        except Exception as e:
            import traceback
            log.error(f"  FAILED: {label}: {e}")
            log.error(traceback.format_exc())

    if not frames:
        log.warning("  No TAM supplements loaded")
        return pd.DataFrame(columns=COMMON_COLUMNS)

    combined = pd.concat(frames, ignore_index=True)
    log.info(f"\n  TAM supplements total: {len(combined):,} rows, "
             f"EUR {combined['amount_eur'].sum():,.0f}")
    log.info(f"  Countries: {combined['country'].value_counts().to_dict()}")
    return combined[COMMON_COLUMNS]
