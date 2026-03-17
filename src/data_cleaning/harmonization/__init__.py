"""
harmonization/
==============
Modular harmonization layer for EU subsidy data sources.

Each module exposes a standardize() function that accepts (data_dir, log) and returns
a pandas DataFrame conforming to COMMON_COLUMNS, or a tuple of DataFrames for sources
that produce multiple outputs (ESIF 2014, ESIF 2027, CINEA).

Architecture:
    utils.py        — shared constants and helpers (COMMON_COLUMNS, country map, etc.)
    tam.py          — TAM state aid awards
    fts.py          — FTS direct EU spending
    eib.py          — EIB project lending
    ebrd.py         — EBRD investment financing
    rrf.py          — RRF planned allocations (measure-level)
    scoreboard.py   — State Aid Scoreboard (contextual only)
    research.py     — CORDIS research projects
    kohesio.py      — Kohesio cohesion policy
    esif_2014.py    — ESIF 2014-2020 programme aggregates (contextual only)
    esif_2027.py    — ESIF 2021-2027 programme aggregates (contextual only)
    cinea.py        — CINEA programme grants (HORIZON excluded)

See README_ARCHITECTURE.md for full interpretation rules.
"""

from . import (  # noqa: F401
    cinea,
    ebrd,
    eib,
    esif_2014,
    esif_2027,
    fts,
    kohesio,
    research,
    rrf,
    scoreboard,
    tam,
)
from .utils import (  # noqa: F401
    CINEA_EXCLUDE_PROGRAMMES,
    COMMON_COLUMNS,
    COUNTRY_MAP,
    EU27,
    TAM_MEGA_SCHEMES_DROP,
    extract_year,
    pack_originals,
    safe_to_numeric,
    standardize_country,
)
