#!/usr/bin/env python3
"""ct.emission_short_scan() / ct.focus_leak_scan(): the GUI's tests 2 and 3,
ported 2026-10-13. Offline: a fake rig whose ADS emission current and focus
voltage follow which grid switches are closed.

    python3 tests/test_short_leak_scans.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Rig:
    """6 filaments: 0-2 on P1 CH1, 3-5 on P1 CH2. shorts: +mA each when closed;
    leaks: closing pulls focus to emission. ADS emission has a 2.0 mA offset."""

    def __init__(self, shorts=(), leaks=(), rail_ok=True, focus_stuck=False, raise_on=None):
        self.shorts, self.leaks = set(shorts), set(leaks)
        self.rail_ok, self.focus_stuck, self.raise_on = rail_ok, focus_stuck, raise_on
        self.closed, self.calls = set(), []
        self.v = 30.0

    def install(self, ct):
        rig, C = self, self.calls
        ct._mosfet_targets = lambda fs: ([0, 1, 2, 3, 4, 5] if fs is None else list(fs), [])
        ct.filament_to_board = lambda f: {"controller": 1, "channel": 0 if f < 3 else 1, "position": f % 3}
        ct.stop_all = lambda *a, **k: (C.append("stop"), {"ok": True})[1]
        ct.sleep_all = lambda *a, **k: (C.append("sleep"), {"ok": True})[1]
        ct.enable_focus = lambda on: (C.append(("focus", on)), {"ok": True})[1]
        ct.set_emission_i = lambda ma: {"ok": True}
        ct.set_emission_v = lambda v: {"ok": True}
        ct.enable_emission = lambda on: (C.append(("emission", on)), {"ok": True})[1]
        ct._mosfet_rail = lambda v, timeout_s=3.0: ((-abs(v), None) if rig.rail_ok
                                                     else (-3.0, "rail did not come up"))
        ct._hv_off_problem = lambda: (C.append(("emission", False)), None)[1]
        ct._post = lambda path, body=None, timeout=None: (C.append(("all-off", path)), {"ok": True})[1]

        def grid_all(fils, on=False, force=True):
            if rig.raise_on and on and rig.raise_on in fils:
                raise RuntimeError("link died")
            (rig.closed.update if on else rig.closed.difference_update)(fils)
            C.append(("grid", tuple(fils), on))
            return {"ok": True}
        ct.hv_grid_set_all = grid_all
        ct.hv_grid_off_all = lambda fils, force=True: grid_all(fils, on=False)
        ct.hv_grid_set = lambda f, on, force=True: grid_all([f], on=on)

        def ads():
            i = 2.0 + 1.0 * len(rig.closed) + sum(20.0 for f in rig.closed if f in rig.shorts)
            focus = 30.0 if (rig.closed & rig.leaks) else (8.0 if rig.focus_stuck and C.count("x") else 1.0)
            return {"ok": True, "focus_v": -focus, "emiss_v": -rig.v, "emiss_i_ma": i}
        ct.read_ads_all = ads


def make(**kw):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    rig = Rig(**kw)
    rig.install(ct)
    return ct, rig


def fast(**kw):
    return {"settle_s": 0.0, **kw}


# 1. Short scan: clean rig -> two channel scans, nothing alone, all ok.
ct, rig = make()
r = ct.emission_short_scan(**fast())
check("clean: ok, 6 ok, baseline subtracted (3 closed -> +3 mA, under 5)",
      r["ok"] and r["counts"] == {"ok": 6, "short": 0, "unmeasured": 0}
      and r["baseline_ma"] == 2.0 and r["channels"]["P1 CH1"]["di_ma"] == 3.0, r)
check("clean: no per-filament phase", not any(c[0] == "grid" and len(c[1]) == 1 for c in rig.calls))

# 2. A short on filament 4: only CH2 goes per filament, 4 is the short.
ct, rig = make(shorts=[4])
r = ct.emission_short_scan(**fast())
check("short: CH2 suspect, CH1 clean", r["channels"]["P1 CH2"]["suspect"]
      and not r["channels"]["P1 CH1"]["suspect"], r["channels"])
check("short: 4 is SHORT (+21 mA), 3 and 5 ok alone",
      r["results"][4]["verdict"] == "short" and r["results"][4]["di_ma"] == 21.0
      and r["results"][3]["alone"] and r["results"][3]["verdict"] == "ok", r["results"])
check("short: not ok, named", not r["ok"] and any("SHORT" in p and "filament 4" in p for p in r["problems"]),
      r["problems"])

# 3. Ends with every switch confirmed open and the rail OFF -- also on error.
check("teardown: all-off then emission off",
      ("all-off", "/api/hv-all-off") in rig.calls and rig.calls[-1] == ("emission", False), rig.calls[-3:])
ct, rig = make(raise_on=4, shorts=[4])
try:
    ct.emission_short_scan(**fast())
    raised = False
except RuntimeError:
    raised = True
check("exception: raised, still all-off + rail off",
      raised and ("all-off", "/api/hv-all-off") in rig.calls and rig.calls[-1] == ("emission", False))

# 4. Rail does not come up: nothing closed, rail off.
ct, rig = make(rail_ok=False)
r = ct.emission_short_scan(**fast())
check("rail not up: no switch closed, rail off",
      not any(c[0] == "grid" and c[2] for c in rig.calls if c[0] == "grid")
      and not r["ok"] and rig.calls[-1] == ("emission", False), rig.calls)

# 5. Leak scan: focus OFF set (not refused), leak on 2 found, others ok.
ct, rig = make(leaks=[2])
r = ct.focus_leak_scan(**fast())
check("leak: focus turned OFF by the scan itself", ("focus", False) in rig.calls)
check("leak: 2 is LEAK (ratio 1.0), others ok",
      r["results"][2]["verdict"] == "leak" and r["results"][2]["ratio"] == 1.0
      and all(r["results"][f]["verdict"] == "ok" for f in (0, 1, 3, 4, 5)), r["results"])
check("leak: not ok, named; baseline returned", not r["ok"] and r["drift_bad"] is False
      and any("LEAK" in p for p in r["problems"]), r["problems"])
check("leak: one switch at a time, each opened again",
      [c for c in rig.calls if c[0] == "grid"][:2] == [("grid", (0,), True), ("grid", (0,), False)])

ct, rig = make()
r = ct.focus_leak_scan(**fast())
check("leak: clean rig -> ok", r["ok"] and r["counts"]["ok"] == 6, r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
