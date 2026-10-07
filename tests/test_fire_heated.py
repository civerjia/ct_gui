#!/usr/bin/env python3
"""fire_single_pulse(active_ma=...): ACTIVE only for the shot.

2026-10-07: liuxing_api raised ACTIVE, then fire_single_pulse armed the
detector, checked/downloaded the schedule, armed and read status -- all at
firing current -- and read the pulse log and detector events before the
script dropped back to IDLE: 4-9 s at ACTIVE for a 7 ms pulse. With active_ma
every setup step runs at IDLE, ACTIVE comes after the arm, and IDLE comes back
the moment the run is over. Offline: the hardware calls are recorded.

    python3 tests/test_fire_heated.py
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


def make(active_ok=True, run="complete", idle_ok=True):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    calls = []
    seen = {}

    def active_one(f, ma, **k):
        calls.append("active")
        seen["active_kw"] = k
        return ({"ok": True, "heating": {"ok": True, "measured_ma": ma}} if active_ok
                else {"ok": False, "error": "ladder"})

    def idle_one(f, ma, **k):
        calls.append("idle")
        seen["idle_ma"] = ma
        return {"ok": idle_ok} if idle_ok else {"ok": False, "error": "no answer"}

    def fire_core(filament, **k):
        seen["total_ms"] = k["total_ms"]
        calls.append("setup")                     # download / arm / status
        try:
            k["on_armed"]()
        except BaseException as exc:
            calls.append("disarm")
            return {"ok": False, "fired": 0, "error": f"on_armed raised ({exc})"}
        calls.append("trigger")
        if run == "raise":
            raise RuntimeError("link died mid-run")
        k["after_fire"]()
        calls.append("pulse_log")
        return {"ok": run == "complete", "fired": 1, "records": [{}]}

    ct.active_one = active_one
    ct.idle_one = idle_one
    ct._fire_core = fire_core
    ct.ready_arm = lambda *a, **k: (calls.append("ready_arm"), {"ok": True})[1]
    ct.ready_disarm = lambda *a, **k: calls.append("ready_disarm")
    ct.pulse_cursor = lambda: 0
    ct._collect_pulse_events = lambda since, n: (calls.append("collect"), ([{"plateau_ma": 40}], 1228))[1]
    return ct, calls, seen


KW = dict(width_us=7000, inter_pulse_ms=100, max_on_ms=165, total_ms=6000, timeout_s=6.0)

# 1. measure=True: setup at IDLE, ACTIVE just before the trigger, IDLE before the reads.
ct, calls, seen = make()
r = ct.fire_single_pulse(16, measure=True, active_ma=2700, idle_ma=1300, **KW)
print("  ", calls)
check("ok", r["ok"], r)
check("detector + schedule set up BEFORE ACTIVE",
      calls.index("ready_arm") < calls.index("active") and calls.index("setup") < calls.index("active"))
check("trigger right after ACTIVE", calls.index("trigger") == calls.index("active") + 1, calls)
check("IDLE before the pulse log and the detector read",
      calls.index("idle") < calls.index("pulse_log") < calls.index("collect"), calls)
check("IDLE exactly once", calls.count("idle") == 1, calls)
check("back to idle_ma", seen["idle_ma"] == 1300)
check("firmware total clock covers the heat-up", seen["total_ms"] == 6000 + 5000 + 1000, seen["total_ms"])
check("backend backstop: timed ACTIVE back to idle_ma",
      seen["active_kw"].get("then_idle_ma") == 1300 and seen["active_kw"].get("active_s", 0) > 6.0
      and seen["active_kw"].get("verify") is True, seen["active_kw"])
check("heating feedback and ACTIVE time reported",
      r.get("heating", {}).get("ok") and isinstance(r.get("active_s"), float), r)

# 2. measure=False path too.
ct, calls, seen = make()
r = ct.fire_single_pulse(16, measure=False, active_ma=2700, idle_ma=1300, **KW)
check("measure=False: same order", calls == ["setup", "active", "trigger", "idle", "pulse_log"], calls)

# 3. ACTIVE refused -> nothing fired, and no IDLE either (it never left its state;
#    an IDLE could heat a filament that was below IDLE).
ct, calls, seen = make(active_ok=False)
r = ct.fire_single_pulse(16, measure=True, active_ma=2700, idle_ma=1300, **KW)
check("ACTIVE refused -> no trigger", "trigger" not in calls and not r["ok"], calls)
check("...and no IDLE sent", "idle" not in calls, calls)

# 4. Exception mid-run -> IDLE from the finally.
ct, calls, seen = make(run="raise")
try:
    ct.fire_single_pulse(16, measure=False, active_ma=2700, idle_ma=1300, **KW)
    raised = False
except RuntimeError:
    raised = True
check("exception mid-run still drops to IDLE", raised and calls[-1] == "idle", calls)

# 5. Setup fails before the arm -> ACTIVE never raised, IDLE never sent.
ct, calls, seen = make()
ct._fire_core = lambda f, **k: (calls.append("setup"), {"ok": False, "error": "download failed"})[1]
r = ct.fire_single_pulse(16, measure=False, active_ma=2700, idle_ma=1300, **KW)
check("failed setup -> filament never touched", calls == ["setup"] and not r["ok"], calls)

# 6. IDLE drop fails -> retried once, and the result says so.
ct, calls, seen = make(idle_ok=False)
r = ct.fire_single_pulse(16, measure=False, active_ma=2700, idle_ma=1300, **KW)
check("failed IDLE retried once", calls.count("idle") == 2, calls)
check("...and reported, not ok", not r["ok"] and "IDLE" in r.get("error", ""), r)

# 7. idle_ma is required.
ct, calls, seen = make()
r = ct.fire_single_pulse(16, measure=False, active_ma=2700, **KW)
check("active_ma without idle_ma refused, nothing sent", not r["ok"] and not calls, (r, calls))

# 8. Without active_ma nothing changes.
ct, calls, seen = make()
r = ct.fire_single_pulse(16, measure=False, **KW)
check("no active_ma -> no power calls", "active" not in calls and "idle" not in calls, calls)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
