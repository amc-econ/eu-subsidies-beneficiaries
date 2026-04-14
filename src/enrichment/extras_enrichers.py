"""
enrichment/extras_enrichers.py
==============================
Per-source populators for the schema-v3 ``extra_fields_json`` column.

The ``extra_fields_json`` column (added to ``COMMON_COLUMNS`` in the
2026-04-13 schema rewrite) is a per-row JSON object string that lets
harmonization layers carry arbitrarily rich source-specific metadata
into the master dataset without bloating the 38-column canonical
schema. Until now no harmonizer populated it.

This module is the first set of per-source populators. Each function
takes a harmonized DataFrame (one source at a time) and returns the
same frame with ``extra_fields_json`` updated on the rows that have
additional metadata available. Harmonizers that have not been
upgraded leave the column at its default ``'{}'``; nothing breaks.

**What each enricher reads:**

    enrich_cordis
        Companion files `organization.xlsx`, `topics.xlsx`,
        `euroSciVoc.xlsx` in the same directory as `project.xlsx`.
        Adds: topics codes, EuroSciVoc keywords, TRL level (if
        present), coordinator country, participant count,
        start/end dates.

    enrich_kohesio
        No companion files. KOHESIO exposes a SPARQL endpoint at
        ``kohesio.ec.europa.eu/sparql`` that returns intervention
        field codes and green/digital tagging per project. Scaffold
        only — the SPARQL query body is documented but not run.

    enrich_fts
        FTS publishes a bulk JSON dump at
        ``ec.europa.eu/budget/financial-transparency/download.html``
        with the full budget-line hierarchy and commitment vs payment
        dates. Scaffold only.

    enrich_innovfund
        Innovation Fund publishes a companion Excel with CO₂
        abatement estimates per project. Scaffold only until the
        file is cached in ``data/reference/innovfund/``.

**Usage**

All four functions are idempotent. Run them at the end of each
harmonizer's ``_standardize()`` after ``apply_v2_columns`` but before
the final ``return``:

    from src.enrichment.extras_enrichers import enrich_cordis
    out = enrich_cordis(out, data_dir, log)

Plan audit §6.6 item 10, Tier 3 Phase B extensions.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


def _merge_extras(df: pd.DataFrame, row_updates: dict[int, dict]) -> pd.DataFrame:
    """Merge per-row extras into the ``extra_fields_json`` column.

    ``row_updates`` maps ``df.index`` values to a dict of new
    key/value pairs. Existing JSON on each row is preserved; new
    keys are merged in; empty values are skipped.
    """
    if 'extra_fields_json' not in df.columns:
        df['extra_fields_json'] = '{}'
    if not row_updates:
        return df

    def _apply(idx, current):
        extras = {}
        if isinstance(current, str) and current and current != '{}':
            try:
                loaded = json.loads(current)
                if isinstance(loaded, dict):
                    extras = loaded
            except json.JSONDecodeError:
                pass
        update = row_updates.get(idx, {})
        for k, v in update.items():
            if v in (None, '', [], {}):
                continue
            extras[k] = v
        return json.dumps(extras, ensure_ascii=False) if extras else '{}'

    df['extra_fields_json'] = [
        _apply(i, df.at[i, 'extra_fields_json']) for i in df.index
    ]
    return df


# ---------------------------------------------------------------------------
# CORDIS (project.xlsx + companions)
# ---------------------------------------------------------------------------

def enrich_cordis(df: pd.DataFrame, data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """Enrich CORDIS-harmonized rows with topics, EuroSciVoc keywords,
    coordinator country, participant count, start/end dates.

    Reads from ``data_dir``:
        project.xlsx          → start/end date, totalCost, legalBasis
        organization.xlsx     → coordinator name + country, participant list
        topics.xlsx           → H2020 / HE topic codes
        euroSciVoc.xlsx       → EuroSciVoc keyword path per project

    Every join key is the project ID (CORDIS ``id`` column, which
    harmonization emits as ``source_record_id``). Missing companion
    files are logged and skipped — the function is safe to call even
    when only the primary project file is available.
    """
    if len(df) == 0:
        return df

    source_rows = df[df['source'] == 'RESEARCH']
    if source_rows.empty:
        return df

    organizations_path = data_dir / 'organization.xlsx'
    topics_path = data_dir / 'topics.xlsx'
    euroscivoc_path = data_dir / 'euroSciVoc.xlsx'
    project_path = data_dir / 'project.xlsx'

    org_by_proj: dict[str, dict] = {}
    if organizations_path.exists():
        try:
            org = pd.read_excel(organizations_path,
                                usecols=lambda c: c in (
                                    'projectID', 'projectAcronym', 'role',
                                    'organisationID', 'name',
                                    'shortName', 'activityType',
                                    'country', 'ecContribution',
                                ))
            # Coordinator name/country + aggregated participant list
            for pid, grp in org.groupby('projectID'):
                coord = grp[grp['role'].astype(str).str.lower() == 'coordinator']
                coord_name = coord['name'].iloc[0] if len(coord) else ''
                coord_country = coord['country'].iloc[0] if len(coord) else ''
                org_by_proj[str(pid)] = {
                    'coordinator_name': coord_name,
                    'coordinator_country': coord_country,
                    'participant_count': int(len(grp)),
                    'participant_countries': sorted({
                        str(c).strip() for c in grp['country'].dropna() if str(c).strip()
                    }),
                }
            log.info(f'  CORDIS org enrichment: {len(org_by_proj):,} projects mapped')
        except Exception as exc:
            log.warning(f'  CORDIS organization.xlsx read failed: {exc}')

    topics_by_proj: dict[str, list[str]] = {}
    if topics_path.exists():
        try:
            topics = pd.read_excel(topics_path,
                                   usecols=lambda c: c in ('projectID', 'topic', 'title'))
            for pid, grp in topics.groupby('projectID'):
                topics_by_proj[str(pid)] = sorted({
                    str(t).strip() for t in grp['topic'].dropna() if str(t).strip()
                })
            log.info(f'  CORDIS topic enrichment: {len(topics_by_proj):,} projects mapped')
        except Exception as exc:
            log.warning(f'  CORDIS topics.xlsx read failed: {exc}')

    esv_by_proj: dict[str, list[str]] = {}
    if euroscivoc_path.exists():
        try:
            esv = pd.read_excel(euroscivoc_path,
                                usecols=lambda c: c in (
                                    'projectID', 'euroSciVocPath',
                                    'euroSciVocTitle', 'euroSciVocDescription',
                                ))
            path_col = 'euroSciVocPath' if 'euroSciVocPath' in esv.columns else 'euroSciVocTitle'
            if path_col in esv.columns:
                for pid, grp in esv.groupby('projectID'):
                    esv_by_proj[str(pid)] = sorted({
                        str(p).strip() for p in grp[path_col].dropna() if str(p).strip()
                    })[:10]  # cap each row to 10 keywords
            log.info(f'  CORDIS EuroSciVoc enrichment: {len(esv_by_proj):,} projects mapped')
        except Exception as exc:
            log.warning(f'  CORDIS euroSciVoc.xlsx read failed: {exc}')

    project_extras: dict[str, dict] = {}
    if project_path.exists():
        try:
            proj = pd.read_excel(project_path,
                                 usecols=lambda c: c in (
                                     'id', 'acronym', 'startDate', 'endDate',
                                     'totalCost', 'legalBasis', 'masterCall',
                                     'subCall', 'frameworkProgramme',
                                 ))
            for _, row in proj.iterrows():
                pid = str(row.get('id', '')).strip()
                if not pid:
                    continue
                project_extras[pid] = {
                    'acronym': str(row.get('acronym', '') or '').strip(),
                    'startDate': str(row.get('startDate', '') or '')[:10],
                    'endDate': str(row.get('endDate', '') or '')[:10],
                    'totalCost': float(row['totalCost']) if pd.notna(row.get('totalCost')) else None,
                    'legalBasis': str(row.get('legalBasis', '') or '').strip(),
                    'frameworkProgramme': str(row.get('frameworkProgramme', '') or '').strip(),
                    'masterCall': str(row.get('masterCall', '') or '').strip(),
                }
            log.info(f'  CORDIS project-level enrichment: {len(project_extras):,} projects mapped')
        except Exception as exc:
            log.warning(f'  CORDIS project.xlsx re-read failed: {exc}')

    # Build the per-index row-updates dict.
    row_updates: dict = {}
    for idx in source_rows.index:
        pid = str(df.at[idx, 'source_record_id']).strip()
        extras: dict = {}
        if pid in org_by_proj:
            extras.update(org_by_proj[pid])
        if pid in topics_by_proj:
            extras['topics'] = topics_by_proj[pid]
        if pid in esv_by_proj:
            extras['euroscivoc'] = esv_by_proj[pid]
        if pid in project_extras:
            for k, v in project_extras[pid].items():
                if v not in (None, '', [], {}):
                    extras[k] = v
        if extras:
            row_updates[idx] = extras

    n_enriched = len(row_updates)
    log.info(
        f'  CORDIS extras: {n_enriched:,}/{len(source_rows):,} rows enriched '
        f'({n_enriched / max(len(source_rows), 1) * 100:.0f}%)'
    )
    return _merge_extras(df, row_updates)


# ---------------------------------------------------------------------------
# KOHESIO SPARQL (scaffold only)
# ---------------------------------------------------------------------------

# KOHESIO publishes a SPARQL endpoint at ``kohesio.ec.europa.eu/sparql``
# that exposes the full 2014-2020 and 2021-2027 cohesion policy dataset
# as RDF. Query shape for intervention field + green/digital tags:
#
#   PREFIX kohesio: <https://linkedopendata.eu/prop/direct/>
#   PREFIX wd: <https://linkedopendata.eu/entity/>
#   SELECT ?project ?interventionField ?isGreen ?isDigital
#   WHERE {
#     ?project kohesio:P10 ?interventionField .
#     OPTIONAL { ?project kohesio:P1822 ?isGreen }
#     OPTIONAL { ?project kohesio:P1823 ?isDigital }
#     FILTER (?project = wd:Q<KOHESIO_project_id>)
#   }
#
# The query should be batched ~100 projects per request. A full run
# for ~2M KOHESIO projects is ~20k requests, ~2 hours at 10 req/s.
# Cached per project in ``data/cache/kohesio_sparql/<project_id>.json``.

KOHESIO_SPARQL_URL = 'https://kohesio.ec.europa.eu/sparql'


def enrich_kohesio(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Scaffold. See module docstring + KOHESIO_SPARQL_URL above.

    Implementation outline:
      1. Collect unique ``source_record_id`` values for KOHESIO rows.
      2. Batch them into SPARQL queries of ~100 IDs each (VALUES
         clause).
      3. POST to ``KOHESIO_SPARQL_URL`` with the query above.
      4. Parse the SPARQL JSON response into ``{project_id: {...}}``.
      5. Cache per-batch under ``data/cache/kohesio_sparql/``.
      6. Merge into ``extra_fields_json`` via ``_merge_extras``.

    Not implemented in v1 — requires live SPARQL endpoint testing.
    """
    n = int((df['source'] == 'KOHESIO').sum())
    log.info(f'  KOHESIO enrichment: scaffold only, {n:,} rows untouched')
    return df


# ---------------------------------------------------------------------------
# FTS JSON deep enricher (scaffold)
# ---------------------------------------------------------------------------

# FTS publishes a bulk JSON dump with the full budget-line hierarchy
# and commitment vs payment dates per row. The CSV export flattens
# this into a single ``year`` column which loses the commit/pay
# distinction. A live v1 enricher would download the JSON, build a
# ``{record_id: {commit_date, pay_date, budget_line_hierarchy}}``
# map, and merge into extras.
#
# URL: https://ec.europa.eu/budget/financial-transparency-system/download.html
# (serves per-year zipped JSON; the API URL shape has changed over the
# years, so needs a discovery step before the first run).


def enrich_fts(df: pd.DataFrame, log: logging.Logger) -> pd.DataFrame:
    """Scaffold. See module docstring."""
    n = int((df['source'] == 'FTS').sum())
    log.info(f'  FTS enrichment: scaffold only, {n:,} rows untouched')
    return df


# ---------------------------------------------------------------------------
# Innovation Fund CO₂ savings (scaffold)
# ---------------------------------------------------------------------------

INNOVFUND_CO2_FILENAME = 'innovfund_co2_savings.xlsx'


def enrich_innovfund(df: pd.DataFrame, data_dir: Path, log: logging.Logger) -> pd.DataFrame:
    """If ``innovfund_co2_savings.xlsx`` is present in ``data_dir``,
    merge per-project CO₂ abatement estimates into ``extra_fields_json``.

    The Innovation Fund publishes a companion spreadsheet alongside
    its annual selection decisions with columns:
        projectID, projectName, awardedAmountEur,
        co2AvoidedMtCO2e, co2AvoidedYears,
        innovationScore, technologyReadinessLevel, …

    Not yet running live because the file is not shipped in the repo.
    Drop it into ``data/reference/innovfund/`` and the function becomes
    a no-op to a one-join-away enrichment.
    """
    co2_path = data_dir / 'reference' / 'innovfund' / INNOVFUND_CO2_FILENAME
    if not co2_path.exists():
        n = int((df['source'] == 'INNOVFUND').sum())
        log.info(
            f'  INNOVFUND enrichment: {co2_path.name} not found, '
            f'{n:,} rows untouched'
        )
        return df

    try:
        co2 = pd.read_excel(co2_path)
    except Exception as exc:
        log.warning(f'  INNOVFUND co2 file read failed: {exc}')
        return df

    id_col = next(
        (c for c in co2.columns if c.lower() in ('projectid', 'project_id', 'id')),
        None,
    )
    if not id_col:
        log.warning('  INNOVFUND co2 file has no recognisable project ID column')
        return df

    extras_by_id: dict[str, dict] = {}
    for _, row in co2.iterrows():
        pid = str(row[id_col]).strip()
        if not pid:
            continue
        extras_by_id[pid] = {
            k: (float(v) if isinstance(v, (int, float)) else str(v))
            for k, v in row.items()
            if k != id_col and pd.notna(v) and str(v).strip()
        }

    source_mask = df['source'] == 'INNOVFUND'
    row_updates: dict = {}
    for idx in df.index[source_mask]:
        pid = str(df.at[idx, 'source_record_id']).strip()
        if pid in extras_by_id:
            row_updates[idx] = extras_by_id[pid]

    log.info(
        f'  INNOVFUND extras: {len(row_updates):,}/{int(source_mask.sum()):,} rows enriched'
    )
    return _merge_extras(df, row_updates)
