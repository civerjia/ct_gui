#!/usr/bin/env python3
"""The trend plot's readings on disk: written, read back after a restart,
full rate for SERIES_RAW_KEEP_H (a week), older hours as a per-minute summary.

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
check("raw readings kept a week by default", S.SERIES_RAW_KEEP_H == 7 * 24, S.SERIES_RAW_KEEP_H)
old = now - 8 * 24 * 3600.0
for k in range(120):                                  # 2 min, 8 days ago
    S.series_add("sp_kv", 60.0 + (k % 2), old + k)
for k in range(600):                                  # the last 10 min
    S.series_add("sp_ma", 0.02, now - 600 + k)
    S.series_add("vac", 8e-6, now - 600 + k)
check("flush writes every reading", S.series_flush() == 1320)
check("...nothing twice", S.series_flush() == 0)

for d in S._SERIES.values():                          # a restart
    d.clear()
n = S.series_load(now)
check("restart: the last hour read back (not the 8 days old ones)", n == 1200
      and len(S._SERIES["sp_ma"]) == 600 and len(S._SERIES["sp_kv"]) == 0, n)

done = S.series_compact(now)
raws = sorted(p.name for p in S.SERIES_DIR.glob("raw_*.csv"))
sums = sorted(S.SERIES_DIR.glob("summary_*.csv"))
check("8 days old raw hour compacted and deleted, recent kept", len(done) >= 1
      and all(n not in raws for n in done) and raws, (done, raws))
lines = sums[0].read_text().splitlines() if sums else []
row = [ln for ln in lines if ",sp_kv," in ln]
check("...summary: one row per minute, n/min/max/mean", lines and lines[0] == "minute,name,n,min,max,mean"
      and len(row) in (2, 3) and sum(int(r.split(",")[2]) for r in row) == 120
      and all(r.split(",")[3] == "60" and r.split(",")[4] == "61" for r in row), row)

# The 24 h zoomable plot (series_range): raw from disk for an old stretch,
# the min/max envelope zoomed out; a one-sample spike survives both.
S.SERIES_DIR = Path(tempfile.mkdtemp()) / "series"
for d in S._SERIES.values():
    d.clear()
for e in S._SERIES_ENV.values():
    e.clear()
S._SERIES_PENDING.clear()
S._SERIES_HOUR_CACHE.clear()
now = time.time()
t5 = now - 5 * 3600.0
for k in range(7200):                                 # 2 h at 1 Hz, 5 h ago
    S.series_add("em_i", 12.0 + (5.0 if k == 3333 else 0.0), t5 + k)
S.series_flush()
for d in S._SERIES.values():
    d.clear()                                        # a restart: memory empty
for e in S._SERIES_ENV.values():
    e.clear()
check("envelope rebuilt from disk", S.series_env_fill(now) == 7200
      and len(S._SERIES_ENV["em_i"]) in (720, 721), len(S._SERIES_ENV["em_i"]))
r = S.series_range(["em_i"], t5 + 3000, t5 + 3600, 1000)   # 10 min, 0.6 s buckets: raw from disk
pts = r["series"]["em_i"]
check("zoomed in on an old stretch -> every raw reading, from disk", len(pts) in (600, 601)
      and max(v for _t, v in pts) == 17.0, (len(pts), r["bucket_s"]))
r = S.series_range(["em_i"], now - 24 * 3600.0, now, 500)  # 24 h: envelope
pts = r["series"]["em_i"]
check("24 h -> at most 2 points per bucket, the spike kept", len(pts) <= 1000
      and max(v for _t, v in pts) == 17.0 and min(v for _t, v in pts) == 12.0, (len(pts), r["bucket_s"]))
r = S.series_range(["em_i"], now - 48 * 3600.0, now - 30 * 3600.0, 500)
check("older than 24 h -> nothing", r["series"].get("em_i", []) == [], r["from"])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
