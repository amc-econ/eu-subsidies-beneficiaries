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


def read_master(columns=None, **kwargs):
    """Read master dataset (parquet or CSV) into a DataFrame."""
    import pandas as pd
    p = master_dataset_path()
    if p.suffix == '.parquet':
        return pd.read_parquet(p, columns=columns)
    return pd.read_csv(p, usecols=columns, low_memory=False, **kwargs)


def read_master_chunked(columns=None, chunksize=500_000, **kwargs):
    """Yield master dataset in chunks. Truly memory-safe: peak memory is one
    batch (chunksize rows x len(columns)), NOT the whole file.

    For parquet this streams row-group batches via pyarrow.iter_batches rather
    than loading the entire file and slicing (which would defeat the purpose on
    a ~27M-row master).
    """
    import pandas as pd
    p = master_dataset_path()
    if p.suffix == '.parquet':
        import pyarrow.parquet as pq
        pf = pq.ParquetFile(p)
        for batch in pf.iter_batches(batch_size=chunksize, columns=columns):
            yield batch.to_pandas()
    else:
        csv_kwargs = {'chunksize': chunksize, 'low_memory': False}
        if columns:
            csv_kwargs['usecols'] = columns
        csv_kwargs.update(kwargs)
        yield from pd.read_csv(p, **csv_kwargs)
