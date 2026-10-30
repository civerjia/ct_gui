#!/usr/bin/env python3
"""The GMC-300S Geiger counter readings from control2's WebSocket (gmc_point /
gmc_status, spellman_backend v6): stored, plotted, and None -- never a stale
count -- once they stop. Offline: messages are fed straight to the parser.

    python3 tests/test_radiation.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


clock = {"t": 1000.0}
S.time.monotonic = lambda: clock["t"]
S.series_add = lambda name, v, t=None: added.append((name, v))
added = []

r = S.radiation_snapshot()
check("nothing yet: no count", r["cpm"] is None and r["usvh"] is None and r["age_s"] is None, r)

S._spellman_on_message({"type": "gmc_status", "data": {"connected": True, "port": "COM4", "baud": 57600,
                                                       "version": "GMC-300SRe 1.17"}}, clock["t"])
S._spellman_on_message({"type": "gmc_point", "data": {"elapsed_s": 5.0, "timestamp_s": 9.0, "cpm": 23,
                                                      "delta_cpm": 4, "usvh": 0.15}}, clock["t"])
r = S.radiation_snapshot()
check("a point: cpm, usvh, delta, counter status", r["cpm"] == 23 and r["usvh"] == 0.15
      and r["delta_cpm"] == 4 and r["counter"]["port"] == "COM4", r)
check("...and both go to the trend", ("gmc_cpm", 23.0) in added and ("gmc_usvh", 0.15) in added, added)
check("/api/safety carries it", S.hv_guard_snapshot()["radiation"]["cpm"] == 23)

S._spellman_on_message({"type": "gmc_point", "data": {"cpm": 30, "usvh": None}}, clock["t"])
r = S.radiation_snapshot()
check("a point without usvh: the old dose is not kept next to the new count",
      r["cpm"] == 30 and r["usvh"] is None, r)

S._spellman_on_message({"type": "gmc_point", "data": {"cpm": "x"}}, clock["t"])
check("a non-number cpm changes nothing", S.radiation_snapshot()["cpm"] == 30)

clock["t"] += S.SPELLMAN_STALE_S + 1
r = S.radiation_snapshot()
check("readings stopped: None, with the age", r["cpm"] is None and r["usvh"] is None
      and r["age_s"] > S.SPELLMAN_STALE_S, r)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
