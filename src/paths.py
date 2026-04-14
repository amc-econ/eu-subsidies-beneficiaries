"""Shared path constants for the EU Subsidies pipeline.

All scripts should import paths from here rather than computing
them via fragile Path(__file__).parent chains.
"""
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = REPO_ROOT / 'data'
RAW_DIR = DATA_DIR / 'raw'
PROCESSED_DIR = DATA_DIR / 'processed'
ENRICHMENT_DIR = PROCESSED_DIR / 'enrichment_output'
MATCH_OUTPUT_DIR = PROCESSED_DIR / 'match_output'


def master_dataset_path() -> Path:
    """Return path to master dataset, preferring parquet over CSV."""
    pq = PROCESSED_DIR / 'master_dataset.parquet'
    if pq.exists():
        return pq
    return PROCESSED_DIR / 'master_dataset.csv'


def _coerce_null_sentinels(df):
    """Coerce legacy sentinel values to real NaN on string columns.

    A small defensive step so downstream ``.isna()`` checks are
    trustworthy regardless of what the master parquet happens to
    encode. This is cheaper than rebuilding the master parquet and
    is idempotent.

    Currently targets:
        beneficiary_name: '' / 'None' / 'nan' → NaN. See plan audit
            finding L8 (RRF's ``beneficiary_name = None`` round-trips
            through parquet as ``''`` because pandas coerces object
            columns). rrf.py now emits ``pd.NA`` on build; this
            coercion cleans older parquets that were built before
            the rrf.py fix.
    """
    import pandas as pd
    if 'beneficiary_name' in df.columns:
        bn = df['beneficiary_name']
        if bn.dtype == object:
            mask = bn.isin(['', 'None', 'nan', 'NaN', 'NULL', 'null'])
            if mask.any():
                df.loc[mask, 'beneficiary_name'] = pd.NA
    return df


def read_master(columns=None, **kwargs):
    """Read master dataset (parquet or CSV) into a DataFrame."""
    import pandas as pd
    p = master_dataset_path()
    if p.suffix == '.parquet':
        df = pd.read_parquet(p, columns=columns)
    else:
        df = pd.read_csv(p, usecols=columns, low_memory=False, **kwargs)
    return _coerce_null_sentinels(df)


def read_master_chunked(columns=None, chunksize=500_000, **kwargs):
    """Yield master dataset in **true** streaming chunks.

    Memory fix (2026-04-14): the earlier implementation did
    ``df = pd.read_parquet(p, columns=columns)`` then yielded
    ``df.iloc[start:start+chunksize]`` windows. That materialises
    the full parquet in memory first, so peak RAM is ~15 GB
    regardless of ``chunksize`` — the chunking was cosmetic. The
    FTS-CORDIS bridge hit this bug when validating Phase A edits
    and OOM-killed the process.

    We now use ``pyarrow.parquet.ParquetFile.iter_batches`` which
    streams row groups off disk without materialising the full
    frame. Peak RAM for a typical chunksize (500k rows) and the
    9 columns the FTS bridge needs is ~200-400 MB instead of 15 GB.
    CSV path unchanged — pandas ``chunksize`` is already streaming.
    """
    import pandas as pd
    p = master_dataset_path()
    if p.suffix == '.parquet':
        try:
            import pyarrow.parquet as pq
            pf = pq.ParquetFile(str(p))
            # ``iter_batches`` yields pyarrow RecordBatch objects;
            # convert each to pandas and apply sentinel coercion.
            # ``columns`` filters columns at scan time, which is the
            # actual memory saving.
            for batch in pf.iter_batches(batch_size=chunksize, columns=columns):
                yield _coerce_null_sentinels(batch.to_pandas())
            return
        except ImportError:
            # Fall back to the old (memory-heavy) path if pyarrow is
            # somehow missing. Should not happen in this repo —
            # pyarrow is a required dep for the master parquet.
            df = pd.read_parquet(p, columns=columns)
            for start in range(0, len(df), chunksize):
                yield _coerce_null_sentinels(df.iloc[start:start + chunksize])
            return
    else:
        csv_kwargs = {'chunksize': chunksize, 'low_memory': False}
        if columns:
            csv_kwargs['usecols'] = columns
        csv_kwargs.update(kwargs)
        for chunk in pd.read_csv(p, **csv_kwargs):
            yield _coerce_null_sentinels(chunk)
