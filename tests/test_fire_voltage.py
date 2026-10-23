#!/usr/bin/env python3
"""fire_single_pulse(active_mv=...): heat in VOLTAGE mode for the shot
(2026-10-14) -- no CC loop: a timed VOLTAGE after the arm, a fixed settle,
one cached check that the output is up, fire, back to idle_ma. Offline: the
hardware calls are recorded, time.sleep is a no-op.

    python3 tests/test_fire_voltage.py
"""
import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


time.sleep = lambda s: SLEPT.append(s)
SLEPT = []


def make(volt_ok=True, cached=None):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    calls, seen = [], {}

    def voltage_one(f, mv, **k):
        calls.append("voltage")
        seen["v"] = (mv, k)
        return {"ok": True} if volt_ok else {"ok": False, "error": "may not go to timed VOLTAGE"}

    def idle_one(f, ma, **k):
        calls.append("idle")
        seen["idle_ma"] = ma
        return {"ok": True}

    def fire_core(filament, **k):
        seen["total_ms"] = k["total_ms"]
        calls.append("setup")
        try:
            k["on_armed"]()
        except BaseException as exc:
            calls.append("disarm")
            return {"ok": False, "fired": 0, "error": f"on_armed raised ({exc})"}
        calls.append("trigger")
        k["after_fire"]()
        return {"ok": True, "fired": 1, "records": [{}]}

    ct.voltage_one = voltage_one
    ct.idle_one = idle_one
    ct.active_one = lambda *a, **k: calls.append("ACTIVE!") or {"ok": True}
    ct._fire_core = fire_core
    # The firmware's voltage loop: arrival goes ramping -> settled (load voltage
    # within +-150 mV). `cached` is a list of successive replies (last repeats).
    seq = list(cached) if cached is not None else [
        {"current_mA": 2300, "cc_mode": 0, "arrival": "ramping"},
        {"current_mA": 2480, "cc_mode": 0, "arrival": "ramping"},
        {"current_mA": 2510, "cc_mode": 0, "arrival": "settled"}]

    def cached_read(f):
        calls.append("poll")
        return {f: seq.pop(0) if len(seq) > 1 else seq[0]}
    ct.read_filament_current_cached = cached_read
    return ct, calls, seen


KW = dict(width_us=7000, inter_pulse_ms=100, max_on_ms=165, total_ms=6000, timeout_s=6.0)

# 1. The order: setup at IDLE, VOLTAGE after the arm, wait for ARRIVAL, trigger, IDLE.
#    2026-10-23: it used to sleep voltage_settle_s and read once -- not arrival.
ct, calls, seen = make()
SLEPT.clear()
user = []
r = ct.fire_single_pulse(16, active_mv=7600, idle_ma=1200, on_armed=lambda: user.append(1), **KW)
check("ok, fired", r["ok"] and r["fired"] == 1, r)
check("order: setup -> voltage -> polls until settled -> trigger -> idle, never ACTIVE",
      calls == ["setup", "voltage", "poll", "poll", "poll", "trigger", "idle"], calls)
mv, kw = seen["v"]
check("timed VOLTAGE at 7600 mV, back to 1200 mA", mv == 7600 and kw["then_idle_ma"] == 1200
      and kw["active_s"] > 6, seen["v"])
check("caller's on_armed ran only after arrival (no fixed settle sleep)", user == [1] and 2.0 not in SLEPT, SLEPT)
check("heating summary: voltage mode, settled, its current", r["heating"]["mode"] == "voltage"
      and r["heating"]["arrival"] == "settled" and r["heating"]["measured_ma"] == 2510
      and r["heating"]["commanded_mv"] == 7600, r["heating"])
check("total_ms grew by the arrival timeout", seen["total_ms"] == 6000 + 5000 + 1000, seen["total_ms"])

# 2. Refused (not at a settled IDLE): nothing fired, no IDLE write.
ct, calls, seen = make(volt_ok=False)
r = ct.fire_single_pulse(16, active_mv=7600, idle_ma=1200, **KW)
check("refused -> not fired, no idle", not r["ok"] and "idle" not in calls and "trigger" not in calls,
      (calls, r))

# 3. Not arrived: faulted, capped, or never settled (old firmware) -> not
#    fired, dropped to IDLE, said why.
for cached, word, kw2 in (([{"current_mA": None, "cc_mode": 3, "arrival": None}], "OCP", {}),
                          ([{"current_mA": 2900, "cc_mode": 0, "arrival": "capped"}], "at its limit", {}),
                          ([{"current_mA": 2400, "cc_mode": 0, "arrival": "ramping"}], "not reached within",
                           {"heat_timeout_s": 0.3})):
    ct, calls, seen = make(cached=cached)
    r = ct.fire_single_pulse(16, active_mv=7600, idle_ma=1200, **KW, **kw2)
    check(f"{word}: not fired, back to IDLE, said",
          not r["ok"] and "trigger" not in calls and calls[-1] == "idle"
          and word in str(r.get("error")), (calls, r.get("error")))

# 4. Both / neither idle_ma.
ct, calls, seen = make()
check("active_ma AND active_mv refused",
      "not both" in ct.fire_single_pulse(16, active_ma=2600, active_mv=7600, idle_ma=1200, **KW)["error"])
check("active_mv without idle_ma refused",
      "needs idle_ma" in ct.fire_single_pulse(16, active_mv=7600, **KW)["error"])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
