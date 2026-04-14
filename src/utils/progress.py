"""Progress ticker + resumable checkpoint primitives for long-running jobs.

Designed for overnight runs where the user wants to know whether the job is
alive, where it's up to, when it's expected to finish, and whether it can
resume after an interruption. Every long-running stage in the pipeline
(PDF downloads, PDF parsing, web scraping, embedding passes) should wrap
its main loop in a `ProgressTicker` and persist per-item state via
`Checkpoint`.

Philosophy: stdlib only, no dependencies, cheap, resumable, noisy enough
that `tail -f run.log` in the morning tells you exactly what happened.
"""
from __future__ import annotations

import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Iterable, Iterator

log = logging.getLogger(__name__)


class ProgressTicker:
    """Rolling-average progress reporter with ETA.

    Emits a one-line log message every `every` ticks (and on `finalise()`).
    The output format intentionally mirrors `sa_pdf_parser`'s
    `[PDF dl progress]` line so a reader grep'ing `progress` catches both.

    Usage
    -----
    >>> tk = ProgressTicker(total=1000, name='EIB scrape', every=50)
    >>> for item in items:
    ...     t0 = time.time()
    ...     ok = do_work(item)
    ...     tk.tick(success=ok, latency=time.time() - t0)
    >>> tk.finalise()
    """

    def __init__(
        self,
        total: int,
        name: str,
        every: int = 25,
        window: int = 50,
        logger: logging.Logger | None = None,
    ) -> None:
        self.total = max(int(total), 0)
        self.name = name
        self.every = max(int(every), 1)
        self._log = logger or log
        self._t_start = time.time()
        self._t_last = self._t_start
        self._i = 0
        self._n_ok = 0
        self._n_fail = 0
        self._latencies: deque[float] = deque(maxlen=max(window, 1))

    def tick(self, success: bool = True, latency: float | None = None) -> None:
        self._i += 1
        if success:
            self._n_ok += 1
        else:
            self._n_fail += 1
        if latency is not None and latency >= 0:
            self._latencies.append(float(latency))
        if self._i % self.every == 0 or self._i == self.total:
            self._emit()

    def _emit(self) -> None:
        elapsed = time.time() - self._t_start
        processed = self._i
        success_pct = (self._n_ok / processed * 100) if processed else 0.0
        avg_lat = (
            sum(self._latencies) / len(self._latencies)
            if self._latencies
            else 0.0
        )
        remaining = max(self.total - processed, 0)
        rate = processed / elapsed if elapsed > 0 else 0.0
        eta_sec = (remaining / rate) if rate > 0 else 0.0
        eta_min = eta_sec / 60
        self._log.info(
            f"    [{self.name} progress] {processed}/{self.total} "
            f"({success_pct:.0f}% ok, avg {avg_lat:.2f}s/item, "
            f"rate {rate:.1f}/s, ETA {eta_min:.1f} min)"
        )
        self._t_last = time.time()

    def finalise(self) -> None:
        elapsed = time.time() - self._t_start
        self._log.info(
            f"  [{self.name} done] {self._i}/{self.total} in {elapsed/60:.1f} min "
            f"({self._n_ok} ok, {self._n_fail} failed)"
        )

    @property
    def processed(self) -> int:
        return self._i

    @property
    def elapsed(self) -> float:
        return time.time() - self._t_start


class Checkpoint:
    """JSON-backed resumable state for per-item work.

    Stores a dict keyed by item id with an arbitrary JSON-serialisable
    result payload. Use `.pending()` to filter an iterable of items down
    to those not yet processed, and `.mark()` to record completion.

    The file on disk is a newline-delimited JSON log (append-only) so
    crash-safety is a `fsync()` per record. On load, the log is replayed
    into an in-memory dict; the last record for a given key wins.

    Usage
    -----
    >>> ckpt = Checkpoint(Path('data/cache/eib_scrape.ckpt.jsonl'))
    >>> for item in ckpt.pending(all_items, key=lambda x: x['id']):
    ...     result = scrape(item)
    ...     ckpt.mark(item['id'], result)
    >>> ckpt.close()
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = {}
        self._fh = None
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        with self.path.open('r', encoding='utf-8') as f:
            for line_no, raw in enumerate(f, 1):
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                    key = rec.get('k')
                    if key is not None:
                        self._state[str(key)] = rec.get('v')
                except json.JSONDecodeError:
                    log.warning(
                        f"Checkpoint {self.path.name}: skipping malformed "
                        f"line {line_no}"
                    )
        log.info(
            f"  [checkpoint] {self.path.name}: loaded {len(self._state)} "
            f"completed items"
        )

    def _open_append(self) -> None:
        if self._fh is None or self._fh.closed:
            self._fh = self.path.open('a', encoding='utf-8', buffering=1)

    def done(self, key: str) -> bool:
        return str(key) in self._state

    def get(self, key: str, default: Any = None) -> Any:
        return self._state.get(str(key), default)

    def mark(self, key: str, value: Any = None) -> None:
        key_s = str(key)
        self._state[key_s] = value
        self._open_append()
        self._fh.write(json.dumps({'k': key_s, 'v': value}, default=str))
        self._fh.write('\n')
        self._fh.flush()

    def pending(
        self,
        items: Iterable[Any],
        key=lambda x: x,
    ) -> Iterator[Any]:
        for item in items:
            k = str(key(item))
            if k not in self._state:
                yield item

    def __len__(self) -> int:
        return len(self._state)

    def close(self) -> None:
        if self._fh is not None and not self._fh.closed:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> 'Checkpoint':
        return self

    def __exit__(self, *exc) -> None:
        self.close()
