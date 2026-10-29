#!/usr/bin/env python3
"""The trend plot's readings on disk: written, read back after a restart,
full rate only for SERIES_RAW_KEEP_H, older hours as a per-minute summary.

    python3 tests/test_series_disk.py
"""
import _path  # noqa: F401

import os
import tempfile
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402
from pathlib import Path  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


S.SERIES_DIR = Path(tempfile.mkdtemp()) / "series"
for d in S._SERIES.values():
    d.clear()
S._SERIES_PENDING.clear()

now = time.time()
old = now - 8 * 3600.0
for k in range(120):                                  # 2 min, 8 h ago
    S.series_add("sp_kv", 60.0 + (k % 2), old + k)
for k in range(600):                                  # the last 10 min
    S.series_add("sp_ma", 0.02, now - 600 + k)
    S.series_add("vac", 8e-6, now - 600 + k)
check("flush writes every reading", S.series_flush() == 1320)
check("...nothing twice", S.series_flush() == 0)

for d in S._SERIES.values():                          # a restart
    d.clear()
n = S.series_load(now)
check("restart: the last hour read back (not the 8 h old ones)", n == 1200
      and len(S._SERIES["sp_ma"]) == 600 and len(S._SERIES["sp_kv"]) == 0, n)

done = S.series_compact(now)
raws = sorted(p.name for p in S.SERIES_DIR.glob("raw_*.csv"))
sums = sorted(S.SERIES_DIR.glob("summary_*.csv"))
check("8 h old raw hour compacted and deleted, recent kept", len(done) >= 1
      and all(n not in raws for n in done) and raws, (done, raws))
lines = sums[0].read_text().splitlines() if sums else []
row = [ln for ln in lines if ",sp_kv," in ln]
check("...summary: one row per minute, n/min/max/mean", lines and lines[0] == "minute,name,n,min,max,mean"
      and len(row) in (2, 3) and sum(int(r.split(",")[2]) for r in row) == 120
      and all(r.split(",")[3] == "60" and r.split(",")[4] == "61" for r in row), row)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
