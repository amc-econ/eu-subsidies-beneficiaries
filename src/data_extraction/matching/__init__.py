"""
Generic entity matching for the EU Subsidies pipeline.

Accepts any company list CSV with a 'company_name' column.
No sector-specific knowledge required.
"""

from .generic_matcher import MatchConfig, run_matching  # noqa: F401
