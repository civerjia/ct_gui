#!/usr/bin/env python3
"""scan_view = fire_single_pulse() per filament, in order, with the next k
filaments pre-heated to their own VOLTAGE; every fire_single_pulse argument
passed through unchanged; nothing left hot on any exit. Offline: the hardware
calls are faked.

    python3 tests/test_scan_view.py
"""
import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402
import ct.server._safety as SAF  # noqa: E402
from ct.server._common import POWER_STATE_IDLE, POWER_STATE_VOLTAGE  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


time.sleep = lambda s: None
MV = {23: 7688, 24: 7698, 25: 8228, 26: 7700}


def make(fail_on=(), raise_in=None):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    log = []

    def voltage_one(f, mv, active_s=None, then_idle_ma=None, **kw):
        log.append(("preheat", f, mv, then_idle_ma))
        return {"ok": True}

    def idle_one(f, ma, **kw):
        log.append(("idle", f, ma))
        return {"ok": True}

    def fire(f, **kw):
        log.append(("fire", f, kw))
        if raise_in == f:
            raise KeyboardInterrupt()
        if callable(kw.get("on_armed")):
            kw["on_armed"]()
        if f in fail_on:
            return {"ok": False, "fired": 0, "error": f"filament {f} not settled"}
        return {"ok": True, "fired": kw["num_pulses"], "records": [{}] * kw["num_pulses"]}

    ct.voltage_one, ct.idle_one, ct.fire_single_pulse = voltage_one, idle_one, fire
    return ct, log


ALL = dict(num_pulses=20, width_us=15000, pulse_interval_ms=None, trigger_timeout_ms=30,
           max_on_ms=66, total_ms=12000, trigger="ext", shot_wait_timeout_s=9.0, verify=True,
           reuse=False, measure=True, rate_hz=1000000, bg_gap_us=100.0, bg_window_us=50.0,
           heat_timeout_s=6.0, settle_ms=1000)

# --- normal run: order, pre-heating, every argument passed through ------------
ct, log = make()
armed = []
r = ct.scan_view([23, 24, 25, 26], active_mv=MV, idle_ma=1100, k=2,
                 on_armed=lambda f: armed.append(f), **ALL)
fires = [e for e in log if e[0] == "fire"]
check("fires every filament once, in the given order", [e[1] for e in fires] == [23, 24, 25, 26],
      [e[1] for e in fires])
check("each fire_single_pulse gets its OWN voltage and idle_ma",
      all(e[2]["active_mv"] == MV[e[1]] and e[2]["idle_ma"] == 1100 for e in fires))
check("every fire_single_pulse argument passed through unchanged",
      all(all(e[2][k] == v for k, v in ALL.items()) for e in fires),
      [(k, fires[0][2].get(k), v) for k, v in ALL.items() if fires[0][2].get(k) != v])
check("on_armed is called with the filament", armed == [23, 24, 25, 26], armed)
pre = [(e[1]) for e in log if e[0] == "preheat"]
check("the next k=2 are pre-heated before each fire, each once", pre == [24, 25, 26], pre)
check("pre-heat uses that filament's voltage and the IDLE to fall back to",
      all(e[2] == MV[e[1]] and e[3] == 1100 for e in log if e[0] == "preheat"))
first_fire_of = {f: next(i for i, e in enumerate(log) if e[0] == "fire" and e[1] == f) for f in MV}
first_pre_of = {e[1]: i for i, e in reversed(list(enumerate(log))) if e[0] == "preheat"}
check("a filament is pre-heated before its own fire", all(first_pre_of[f] < first_fire_of[f] for f in first_pre_of))
check("result: ok, 4 fired, one result per filament",
      r["ok"] and r["fired"] == 4 and [x["filament"] for x in r["results"]] == [23, 24, 25, 26], r)

# --- k=0: a plain loop ---------------------------------------------------------
ct, log = make()
r = ct.scan_view([23, 24], active_mv=MV, idle_ma=1100, k=0)
check("k=0: no pre-heating", not [e for e in log if e[0] == "preheat"] and r["ok"])

# --- refusals before anything is touched ---------------------------------------
ct, log = make()
r1 = ct.scan_view([23, 24], active_mv=7000, idle_ma=1100)
r2 = ct.scan_view([23, 24], active_mv={23: 7000}, idle_ma=1100)
r3 = ct.scan_view([23, 24], active_mv=MV, idle_ma=1100, trigger="ext")
check("a single voltage is refused", not r1["ok"] and "every filament" in r1["error"], r1)
check("a missing voltage is refused, naming the filament", not r2["ok"] and "[24]" in r2["error"], r2)
check("trigger='ext' without on_armed is refused", not r3["ok"] and "on_armed" in r3["error"], r3)
check("...and nothing was touched", not log, log)

# --- a failure: stop, or skip --------------------------------------------------
ct, log = make(fail_on=(24,))
r = ct.scan_view([23, 24, 25, 26], active_mv=MV, idle_ma=1100, k=2, on_error="stop")
check("on_error='stop': nothing after the failed filament fires",
      [e[1] for e in log if e[0] == "fire"] == [23, 24] and r["skipped"] == [25, 26]
      and r["failed"] == [24] and not r["ok"], r)
check("...and the pre-heated ones go back to IDLE",
      sorted(e[1] for e in log if e[0] == "idle") == [25, 26], [e for e in log if e[0] == "idle"])

ct, log = make(fail_on=(24,))
r = ct.scan_view([23, 24, 25, 26], active_mv=MV, idle_ma=1100, k=2, on_error="skip")
check("on_error='skip': the rest still fires", [e[1] for e in log if e[0] == "fire"] == [23, 24, 25, 26]
      and r["failed"] == [24] and r["fired"] == 3 and not r["ok"], r)

# --- Ctrl-C mid-way: nothing left hot ------------------------------------------
ct, log = make(raise_in=24)
try:
    ct.scan_view([23, 24, 25, 26], active_mv=MV, idle_ma=1100, k=2)
except KeyboardInterrupt:
    pass
check("Ctrl-C mid-fire: the firing one and the pre-heated ones back to IDLE",
      sorted(e[1] for e in log if e[0] == "idle") == [24, 25, 26], [e for e in log if e[0] == "idle"])

# --- scan_views: per view, left_out, stop -------------------------------------
ct, log = make(fail_on=(24,))
moved = []
views = [{"view": 0, "center": 24, "filaments": [23, 24, 25]},
         {"view": 1, "center": 25, "filaments": [24, 25, 26]}]
r = ct.scan_views(views, active_mv=MV, idle_ma=1100, k=1, on_error="skip",
                  move_to=lambda v: moved.append(v["view"]), move_s=3.0)
check("scan_views: motor per view; a failed filament is left out of later views",
      moved == [0, 1] and [e[1] for e in log if e[0] == "fire"] == [23, 24, 25, 25, 26]
      and r["left_out"] == [24], (moved, [e[1] for e in log if e[0] == "fire"], r.get("left_out")))
ct, log = make(fail_on=(24,))
r = ct.scan_views(views, active_mv=MV, idle_ma=1100, on_error="stop")
check("scan_views on_error='stop': the run ends at the failed view",
      not r["ok"] and r["views_total"] == 2 and len(r["per_view"]) == 1, r)

# --- backend ladder: VOLTAGE may be re-commanded while hot ---------------------
SAF.LAST_POWER_STATE[901] = (POWER_STATE_VOLTAGE, time.monotonic())
SAF.LAST_POWER_STATE[902] = (POWER_STATE_IDLE, time.monotonic())
check("ladder: a filament already at VOLTAGE may be set to VOLTAGE again",
      SAF.ladder_blocks_active(901) is None, SAF.ladder_blocks_active(901))
check("ladder: from IDLE it is still checked as before",
      SAF.ladder_blocks_active(902, arrival="cannot_start", arrival_known=True,
                               current_ma=0, target_ma=1100) is not None)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
