"""
harmonization/rrf_italia_domani.py
==================================
Italy-specific RRF (PNRR) beneficiary-level harmonizer.

**Why this module exists.** The base ``harmonization/rrf.py`` imports
the EC's measure-level planned-allocation dataset and therefore
cannot assign beneficiary names — every row carries
``beneficiary_name = NaN`` and RRF is effectively absent from the
beneficiary-level analysis (plan audit finding H7 / §6.5 / §11.0).

The Italian government publishes full project-level PNRR data
through OpenCoesione's open data portal, and that data **does**
carry per-project beneficiary identifiers ("Soggetto Attuatore",
"Soggetto Titolare"). This module closes the H7 gap for Italian
beneficiaries by reading the OpenCoesione ``progetti_esteso``
parquet export, filtering to PNRR-scope rows, and mapping to the
common schema so the matcher can score them against the user's
reference list.

**Source.** https://opencoesione.gov.it/it/opendata/progetti_esteso.parquet
(CC BY 4.0, bimonthly refresh, ~277 MB compressed, project-level).
The PNRR scope is selected via ``OC_POLITICA == 'PNRR'`` or
``OC_CICLO_FINANZIAMENTO`` containing "PNRR" — column name depends
on the exact parquet vintage and is auto-detected.

**Beneficiary fields.** OpenCoesione carries several candidate
beneficiary columns depending on row type:

    OC_COD_SOGG_ATT        — attuatore code
    OC_DENOM_SOGG_ATT      — attuatore legal name (primary)
    OC_COD_SOGG_PROG       — programmatore code
    OC_DENOM_SOGG_PROG     — programmatore name
    OC_COD_CUP             — Codice Unico di Progetto (primary project ID)

We prefer ``OC_DENOM_SOGG_ATT`` (the implementing body) because that
is the entity actually receiving and spending the funds. Rows
without a non-trivial attuatore fall back to the programmatore.

**Status.** **Scaffold only, not run yet.** This module is wired
into the harmonization package but the actual parquet download
(~277 MB) has been deliberately deferred so it does not compete
with the overnight EIB scrape for bandwidth. To enable:

    python -m src.harmonization.rrf_italia_domani --download
    python run_pipeline.py --stage harmonize --source rrf_italia_domani

Plan audit: phase C item 19, "RRF Italia Domani PoC adapter".
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

from .utils import COMMON_COLUMNS, apply_v2_columns, pack_originals, standardize_country

log = logging.getLogger(__name__)

try:
    import requests
except ImportError:
    requests = None

DATASET_URL = (
    'https://opencoesione.gov.it/it/opendata/progetti_esteso.parquet'
)
CACHE_DIR_NAME = 'rrf_italia_domani'  # relative to data/cache/
SOURCE_TAG = 'RRF_IT'
PNRR_MARKERS = ('PNRR', 'RRF', 'Recovery and Resilience', 'Piano Nazionale di Ripresa')


def _cache_dir(repo_root: Path) -> Path:
    d = repo_root / 'data' / 'cache' / CACHE_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def download_parquet(repo_root: Path, force: bool = False) -> Path:
    """Fetch the OpenCoesione progetti_esteso parquet dump.

    ~277 MB. Cached — subsequent runs are no-ops. Rate limit is not
    applied because OpenCoesione is a government open-data portal
    with published daily-scale quotas.
    """
    if requests is None:
        raise RuntimeError('requests not installed')
    target = _cache_dir(repo_root) / 'progetti_esteso.parquet'
    if target.exists() and not force:
        size_mb = target.stat().st_size / 1e6
        log.info(f'  OpenCoesione parquet cache hit: {target.name} ({size_mb:.0f} MB)')
        return target
    log.info(f'  Downloading OpenCoesione progetti_esteso from {DATASET_URL}')
    t0 = time.time()
    with requests.get(DATASET_URL, stream=True, timeout=600) as r:
        r.raise_for_status()
        total = 0
        with target.open('wb') as f:
            for chunk in r.iter_content(chunk_size=1 << 20):
                if not chunk:
                    continue
                f.write(chunk)
                total += len(chunk)
    log.info(f'  Saved {total/1e6:.0f} MB in {time.time()-t0:.0f}s to {target}')
    return target


def _detect_pnrr_mask(df: pd.DataFrame) -> pd.Series:
    """Auto-detect the column(s) that identify PNRR rows."""
    candidate_cols = [
        c for c in df.columns
        if any(tok in c.upper() for tok in ('POLITICA', 'CICLO', 'FONDO', 'PROGRAMMA'))
    ]
    log.info(f'  PNRR detection: checking {len(candidate_cols)} candidate columns')
    mask = pd.Series(False, index=df.index)
    for col in candidate_cols:
        col_str = df[col].astype(str)
        col_mask = pd.Series(False, index=df.index)
        for marker in PNRR_MARKERS:
            col_mask |= col_str.str.contains(marker, case=False, na=False)
        if col_mask.any():
            log.info(f'    {col}: {int(col_mask.sum()):,} PNRR rows')
            mask |= col_mask
    return mask


def _first_existing_col(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for c in candidates:
        if c in df.columns:
            return c
    return None


def standardize(repo_root: Path, log: logging.Logger) -> pd.DataFrame:
    """Load the OpenCoesione parquet, filter to PNRR, map to common schema."""
    pq_path = _cache_dir(repo_root) / 'progetti_esteso.parquet'
    if not pq_path.exists():
        log.warning(
            f'  OpenCoesione parquet not cached — run '
            f'`python -m src.harmonization.rrf_italia_domani --download` first'
        )
        return pd.DataFrame(columns=COMMON_COLUMNS)

    log.info(f'Loading OpenCoesione progetti_esteso parquet ({pq_path.name}) ...')
    df = pd.read_parquet(pq_path)
    log.info(f'  loaded {len(df):,} rows, {len(df.columns)} columns')

    mask = _detect_pnrr_mask(df)
    pnrr = df[mask].copy()
    log.info(f'  PNRR subset: {len(pnrr):,} rows '
             f'({len(pnrr) / max(len(df), 1) * 100:.1f}% of total)')
    if len(pnrr) == 0:
        return pd.DataFrame(columns=COMMON_COLUMNS)

    # Identify best-available columns (schema evolves between vintages).
    cup_col = _first_existing_col(pnrr, ['OC_COD_CUP', 'COD_CUP', 'CUP'])
    id_col = cup_col or pnrr.columns[0]
    att_col = _first_existing_col(pnrr, [
        'OC_DENOM_SOGG_ATT', 'DENOM_SOGG_ATT', 'SOGGETTO_ATTUATORE',
    ])
    prog_col = _first_existing_col(pnrr, [
        'OC_DENOM_SOGG_PROG', 'DENOM_SOGG_PROG', 'SOGGETTO_PROGRAMMATORE',
    ])
    amount_col = _first_existing_col(pnrr, [
        'OC_FINANZ_TOT_PUB_NETTO', 'OC_FINANZ_TOT_PUBBLICO',
        'FINANZ_TOT_PUBBLICO_NETTO', 'COSTO_AMMESSO',
    ])
    title_col = _first_existing_col(pnrr, [
        'OC_TITOLO_PROGETTO', 'TITOLO_PROGETTO', 'TITOLO',
    ])
    year_col = _first_existing_col(pnrr, [
        'OC_ANNO_INIZIO_PROGETTO', 'ANNO_INIZIO_PROGETTO', 'ANNO_INIZIO',
    ])
    nace_col = _first_existing_col(pnrr, ['OC_COD_ATECO', 'COD_ATECO', 'ATECO'])
    theme_col = _first_existing_col(pnrr, [
        'OC_TEMA_SINTETICO', 'TEMA_SINTETICO', 'TEMA',
    ])
    # PNRR mixes grants (component 1) and loans (component 2). The
    # distinction lives in either ``OC_FONDO_COMUNITARIO`` (fund code
    # marker), ``OC_NATURA_PROGETTO`` / ``OC_TIPO_OPERAZIONE`` (operation
    # type), or the OpenCoesione "tipo_aiuto" / "strumento" fields.
    # Auto-detect whichever is present; rows we can't classify default
    # to ``grant`` (the modal case).
    instrument_col = _first_existing_col(pnrr, [
        'OC_TIPO_STRUMENTO', 'OC_STRUMENTO_AGEVOLATIVO', 'OC_NATURA_CUP',
        'OC_TIPO_OPERAZIONE', 'TIPO_STRUMENTO', 'STRUMENTO', 'TIPO_AIUTO',
    ])

    log.info(
        f'  column map: id={id_col} beneficiary={att_col or prog_col} '
        f'amount={amount_col} title={title_col} year={year_col}'
    )

    out = pd.DataFrame()
    out['source'] = SOURCE_TAG
    out['source_record_id'] = pnrr[id_col].astype(str)
    out['granularity'] = 'project'
    # Primary beneficiary: attuatore; fall back to programmatore
    if att_col:
        out['beneficiary_name'] = pnrr[att_col].astype(str).str.strip()
        if prog_col:
            empty = out['beneficiary_name'].isin(['', 'nan', 'None'])
            out.loc[empty, 'beneficiary_name'] = (
                pnrr.loc[empty, prog_col].astype(str).str.strip()
            )
    elif prog_col:
        out['beneficiary_name'] = pnrr[prog_col].astype(str).str.strip()
    else:
        out['beneficiary_name'] = pd.NA
    out['country'] = 'IT'
    out['amount_eur'] = pd.to_numeric(pnrr[amount_col], errors='coerce') if amount_col else np.nan
    out['amount_type'] = 'planned_allocation'
    out['year'] = pd.to_numeric(pnrr[year_col], errors='coerce').astype('Int64') if year_col else pd.NA
    out['sector_description'] = pnrr[theme_col] if theme_col else None
    out['nace_2digit'] = pnrr[nace_col].astype(str).str[:2] if nace_col else None
    out['description'] = pnrr[title_col] if title_col else None
    out['overlap_flags'] = ''
    orig_cols = [c for c in (cup_col, att_col, prog_col, theme_col, nace_col)
                 if c and c not in (id_col,)]
    out['original_columns'] = pnrr[orig_cols].apply(
        lambda r: pack_originals(r.to_dict()), axis=1
    ) if orig_cols else ''

    # Programme / fund layer
    out['programme'] = 'PNRR / Italia Domani'
    out['fund'] = 'RRF'
    out['programming_period'] = '2021-2027'
    out['instrument_subtype'] = None
    out['policy_domain'] = pnrr[theme_col] if theme_col else None

    # Audit / validation layer
    out['year_paid'] = None
    out['flow_stage'] = 'planned'
    # Classify the instrument per row from OpenCoesione's strumento /
    # tipo_operazione fields when available. Italian instrument labels:
    # 'Prestito' / 'Mutuo' / 'Finanziamento a tasso agevolato' → loan
    # 'Garanzia' / 'Controgaranzia' → guarantee
    # 'Credito d'imposta' / 'Agevolazione fiscale' → tax_advantage
    # 'Partecipazione' / 'Capitale di rischio' → equity
    # everything else → grant (the modal case; pure-grant PNRR investments)
    def _classify_it_instrument(raw) -> str:
        if raw is None:
            return 'grant'
        try:
            if pd.isna(raw):
                return 'grant'
        except (TypeError, ValueError):
            pass
        s = str(raw).lower().strip()
        if not s:
            return 'grant'
        if any(k in s for k in ('prestito', 'mutuo', 'finanziament')):
            return 'loan'
        if any(k in s for k in ('garanzia', 'controgaranzia')):
            return 'guarantee'
        if any(k in s for k in ('credito d', 'credito di imposta',
                                 'agevolazione fiscale', 'fiscal')):
            return 'tax_advantage'
        if any(k in s for k in ('partecipazione', 'capitale di rischio', 'equity')):
            return 'equity'
        return 'grant'

    if instrument_col:
        out['financial_instrument_class'] = pnrr[instrument_col].apply(_classify_it_instrument)
    else:
        out['financial_instrument_class'] = 'grant'
    out['management_type'] = 'shared'
    out['legal_basis'] = 'Regulation (EU) 2021/241'
    out['budget_line_code'] = None
    out['budget_execution_type'] = None

    # Schema v2
    out['flow_stage_confidence'] = 'verified'
    out['flow_stage_assumption'] = None
    out['exclude_reason'] = None
    out['is_primary_record'] = True
    out['is_anonymised'] = False
    apply_v2_columns(out, fiscal_source_type='eu_borrowing', resolution_level='project')

    # Schema v3: extra_fields_json carries the full OpenCoesione row for audit
    import json
    def _pack_extras(i: int) -> str:
        row = pnrr.iloc[i]
        extras = {}
        for k in ('OC_COD_CUP', 'OC_LINK', 'OC_DESCR_PROGRAMMA',
                  'OC_DENOM_REGIONE', 'OC_DENOM_PROVINCIA', 'OC_DENOM_COMUNE',
                  'OC_DATA_INIZIO', 'OC_DATA_FINE',
                  'OC_TEMA_SINTETICO', 'OC_COD_ATECO'):
            if k in row.index and pd.notna(row[k]):
                extras[k] = str(row[k])[:200]
        return json.dumps(extras, ensure_ascii=False) if extras else '{}'
    out['extra_fields_json'] = [_pack_extras(i) for i in range(len(out))]

    out.index = pnrr.index
    log.info(f'  RRF_IT standardised: {len(out):,} rows')
    return out[COMMON_COLUMNS]


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
    )
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--download', action='store_true',
                   help='Fetch the OpenCoesione parquet (~277 MB) into the cache')
    p.add_argument('--standardize', action='store_true',
                   help='Read the cached parquet and emit standardized_RRF_IT.csv')
    p.add_argument('--repo-root', type=Path, default=None)
    args = p.parse_args()

    repo_root = args.repo_root or Path(__file__).resolve().parent.parent.parent
    if args.download:
        download_parquet(repo_root)
    if args.standardize:
        df = standardize(repo_root, log)
        out_dir = repo_root / 'data' / 'processed'
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / 'standardized_RRF_IT.csv'
        df.to_csv(out_path, index=False)
        log.info(f'Wrote {len(df):,} rows to {out_path}')


if __name__ == '__main__':
    main()
