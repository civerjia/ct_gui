#!/usr/bin/env python3
"""A measured fire_single_pulse never fires into an HV that is off, and a shot
the HV guard interrupted is a failure, not a reading. Offline: the backend
answers and the fire are fakes.

2026-10-29 13:17-14:51: big arcs took the HV down for ~15 s at a time; the
shots fired meanwhile measured ~0 mA and liuxing_api's tuner raised ten
filaments for it (#86 to 9563 mV, past the 9.5 V cap).

    python3 tests/test_fire_hv_ready.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.client._schedule as SCH  # noqa: E402
from ct_simple_control import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self):
        self.t = 0.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make(guards, hv=None, during=None):
    """guards: successive /api/hv-guard answers (the last one repeats).
    during: the guard answer after the shot."""
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    seq = list(guards)
    fired = []
    state = {"after": False}

    def _get(path, *a, **k):
        if path == "/api/hv-guard":
            if state["after"] and during is not None:
                return during
            return seq.pop(0) if len(seq) > 1 else seq[0]
        return {}
    ct._get = _get
    ct.hv_status = lambda: hv if hv is not None else {"emission_on": True, "focus_on": True}

    def run(*a, **k):
        fired.append(1)
        state["after"] = True
        return {"ok": True, "fired": 1, "records": [{}], "measured": [{"emission_ma": 5.0}]}
    ct._fire_single_pulse_run = run
    return ct, fired


clock = Clock()
SCH.time.monotonic, SCH.time.sleep = clock.monotonic, clock.sleep
KW = dict(num_pulses=1, width_us=7000, pulse_interval_ms=100, max_on_ms=165, total_ms=6000,
          trigger="sim", measure=True)
QUIET = {"tripped": None, "trips": 3}
AUTO = {"tripped": {"source": "spellman_big_arc", "auto_recover": True, "waiting_for": "kV not steady"},
        "trips": 4}

# 1. Quiet, HV on -> fired, ok.
ct, fired = make([QUIET])
r = ct.fire_single_pulse(5, **KW)
check("HV on, guard quiet -> fired, ok", fired and r["ok"], r)

# 2. A big arc is being waited out -> waits, then fires once the HV is back.
ct, fired = make([AUTO, AUTO, AUTO, {"tripped": None, "trips": 4}])
t0 = clock.t
r = ct.fire_single_pulse(5, **KW)
check("big arc in progress -> waited, then fired", fired and r["ok"] and clock.t - t0 >= 1.0, (r, clock.t - t0))

# 3. Still tripped after 30 s -> not fired, says why.
ct, fired = make([AUTO])
r = ct.fire_single_pulse(5, **KW)
check("not back within 30 s -> not fired, error names the wait", not fired and not r["ok"]
      and "30 s" in r["error"], r)

# 4. A latch that needs a person -> not fired at once.
ct, fired = make([{"tripped": {"source": "stm32", "auto_recover": None, "message": "MOSFET on"}, "trips": 5}])
t0 = clock.t
r = ct.fire_single_pulse(5, **KW)
check("manual latch -> not fired, no wait", not fired and not r["ok"] and clock.t == t0
      and "needs a person" in r["error"], r)

# 5. HV switched off (no trip) -> not fired.
ct, fired = make([QUIET], hv={"emission_on": False, "focus_on": True})
r = ct.fire_single_pulse(5, **KW)
check("HV off -> not fired", not fired and not r["ok"] and "HV is off" in r["error"], r)

# 6. The guard acted during the shot -> a failure, the measurement kept but not trusted.
ct, fired = make([QUIET], during={"tripped": None, "trips": 4})
r = ct.fire_single_pulse(5, **KW)
check("guard acted during the shot -> ok False, hv_off", fired and not r["ok"] and r.get("hv_off")
      and "during this shot" in r["error"], r)

# 7. measure=False -> no HV requirement (switch tests fire without HV).
ct, fired = make([AUTO], hv={"emission_on": False, "focus_on": False})
r = ct.fire_single_pulse(5, **{**KW, "measure": False})
check("measure=False -> fired regardless", fired and r["ok"], r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
