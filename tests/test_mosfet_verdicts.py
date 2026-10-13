#!/usr/bin/env python3
"""mosfet_test / mosfet_sweep verdicts, including SHORT.

2026-10-13: filaments 81 and 90 drew 57-60 mA cold (~58x the diode path).
mosfet_test called that "inconclusive", mosfet_sweep "dead / not conducting".
Both must say SHORT. Offline: the fire is a fake returning a fixed net current.

    python3 tests/test_mosfet_verdicts.py
"""
import _path  # noqa: F401

import contextlib
import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(per_f):
    """per_f: {filament: function(volts) -> net mA}"""
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    state = {"v": 0.0}
    ct._mosfet_targets = lambda fs: (sorted(per_f), [])
    ct.set_emission_i = lambda ma: {"ok": True}

    def set_v(v):
        state["v"] = abs(v)
        return {"ok": True}
    ct.set_emission_v = set_v
    ct.enable_emission = lambda on: {"ok": True}
    ct.hv_status = lambda: {"ok": True, "emission_on": True}

    def rail(v, timeout_s=3.0):
        state["v"] = abs(v)
        return -abs(v), None
    ct._mosfet_rail = rail
    ct.energised = lambda *f, **k: contextlib.nullcontext()
    ct.stop_all = lambda *a, **k: {"ok": True}
    ct.sleep_all = lambda *a, **k: {"ok": True}
    ct._hv_off_problem = lambda: None
    ct._mosfet_fire = lambda f, **k: {"nets": [per_f[f](state["v"])] * 3, "on_mismatch": False,
                                      "error": None}
    return ct


diode = lambda v: (v - 2.82) / 100.0          # mA: two diodes + 100 kOhm
short = lambda v: 58.0                         # 81/90 cold
dead = lambda v: 0.01

ct = make({1: diode, 81: short, 45: dead})
r = ct.mosfet_test(emission_v=100)
check("mosfet_test: healthy pass, short SHORT, open dead",
      r["results"][1]["verdict"] == "pass" and r["results"][81]["verdict"] == "short"
      and r["results"][45]["verdict"] == "dead", {f: x["verdict"] for f, x in r["results"].items()})
check("mosfet_test: short note says so, counted", "short" in (r["results"][81]["note"] or "")
      and r["counts"]["short"] == 1 and any("filament 81: short" in p for p in r["problems"]), r["problems"])

ct = make({1: diode, 81: short, 45: dead})
r = ct.mosfet_sweep()
check("mosfet_sweep: healthy pass, short SHORT (was 'dead'), open dead",
      r["results"][1]["verdict"] == "pass" and r["results"][81]["verdict"] == "short"
      and r["results"][45]["verdict"] == "dead", {f: (x["verdict"], x["note"]) for f, x in r["results"].items()})
check("mosfet_sweep: counted", r["counts"]["short"] == 1 and r["counts"]["dead"] == 1, r["counts"])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
