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

S._spellman_on_message({"type": "gmc_status", "data": {"connected": False, "port": "COM4"}}, clock["t"])
check("spellman_backend's gmc_status (a counter it does not have) is ignored",
      S.radiation_snapshot()["counter"] is None, S.radiation_snapshot())
S._spellman_on_message({"type": "gmc_point", "data": {"elapsed_s": 5.0, "timestamp_s": 9.0, "cpm": 23,
                                                      "delta_cpm": 4, "usvh": 0.15}}, clock["t"])
r = S.radiation_snapshot()
check("a point: cpm, usvh, delta", r["cpm"] == 23 and r["usvh"] == 0.15 and r["delta_cpm"] == 4, r)
check("...and both go to the trend", ("gmc_cpm", 23.0) in added and ("gmc_usvh", 0.15) in added, added)
check("/api/safety carries it", S.hv_guard_snapshot()["radiation"]["cpm"] == 23)

S._spellman_on_message({"type": "gmc_point", "data": {"cpm": 30, "usvh": None}}, clock["t"])
r = S.radiation_snapshot()
check("a point without usvh: the old dose is not kept next to the new count",
      r["cpm"] == 30 and r["usvh"] is None, r)

S._spellman_on_message({"type": "gmc_point", "data": {"cpm": "x"}}, clock["t"])
check("a non-number cpm changes nothing", S.radiation_snapshot()["cpm"] == 30)

# --- the counter's own WebSocket (raspberrypi5:8771, combined_backend GMCBackend) ---
S._gmc_on_message({"type": "status", "data": "Connected to /dev/ttyUSB0 (GMC-300SRe 1.17)"}, clock["t"])
S._gmc_on_message({"type": "cpm_update", "cpm": 11, "usvh": 0.0681, "factor": 161.54,
                   "version": "GMC-300SRe 1.17", "timestamp": 1793402468.4}, clock["t"])
S._gmc_on_message({"type": "cpm_update", "cpm": 14, "usvh": 0.0867, "factor": 161.54,
                   "version": "GMC-300SRe 1.17", "timestamp": 1793402469.4}, clock["t"])
r = S.radiation_snapshot()
check("cpm_update: cpm, usvh, delta from the previous reading, version + status",
      r["cpm"] == 14 and r["usvh"] == 0.0867 and r["delta_cpm"] == 3
      and r["counter"]["version"] == "GMC-300SRe 1.17" and "ttyUSB0" in r["counter"]["status"], r)
check("...plotted", ("gmc_cpm", 14.0) in added)
S._gmc_on_message({"type": "status", "data": "Serial Disconnected"}, clock["t"])
check("counter disconnected: said so", S.radiation_snapshot()["counter"]["connected"] is False)

clock["t"] += S.SPELLMAN_STALE_S + 1
r = S.radiation_snapshot()
check("readings stopped: None, with the age", r["cpm"] is None and r["usvh"] is None
      and r["age_s"] > S.SPELLMAN_STALE_S, r)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
