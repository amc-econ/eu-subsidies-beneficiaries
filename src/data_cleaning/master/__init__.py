"""
Master dataset construction.

Loads standardized CSVs and assembles a single master DataFrame
with flag-based exclusions controlled by MasterConfig.
"""

from .builder import MasterConfig, build_master_dataset  # noqa: F401
