#!/usr/bin/env python3
"""Launch the EIB deep scraper as a fully-detached Windows process.

Using ``subprocess.Popen`` with ``DETACHED_PROCESS |
CREATE_NEW_PROCESS_GROUP`` on Windows produces a process that is not
a child of the launching shell, so it survives after the parent
Claude Code session ends. On non-Windows platforms the same script
falls back to a ``nohup``-style detach with ``start_new_session=True``.

Logs go to ``data/cache/eib_pages/scrape.log`` (appended).

Invoke once:
    python scripts/launch_overnight_eib.py

Re-running is safe — the scraper's checkpoint skips already-done pages.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
LOG_PATH = REPO_ROOT / 'data' / 'cache' / 'eib_pages' / 'scrape.log'
LOG_PATH.parent.mkdir(parents=True, exist_ok=True)

cmd = [
    sys.executable,
    '-u',
    '-m',
    'src.enrichment.eib_deep_scraper',
    '--rate-limit', '0.5',
]

log_file = open(LOG_PATH, 'a', encoding='utf-8', buffering=1)
log_file.write('\n' + '=' * 70 + '\n')
log_file.write(f'RELAUNCH at pid {os.getpid()}\n')
log_file.write('=' * 70 + '\n')
log_file.flush()

kwargs: dict = {
    'stdout': log_file,
    'stderr': subprocess.STDOUT,
    'stdin': subprocess.DEVNULL,
    'cwd': str(REPO_ROOT),
}

if os.name == 'nt':
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    kwargs['creationflags'] = (
        DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    )
else:
    kwargs['start_new_session'] = True

proc = subprocess.Popen(cmd, **kwargs)
print(f'launched detached pid={proc.pid}')
print(f'tail with: tail -f {LOG_PATH}')
