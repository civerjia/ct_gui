#!/usr/bin/env python3
"""Spellman V/I and the vacuum, read-only from control2's spellman_backend;
the 2.5e-5 Torr interlock; the slow turn-off warning. Offline: messages are
fed straight to the parser, the rails and the force-off are fakes.

    python3 tests/test_vacuum_spellman.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def reset():
    with S._SPELL_LOCK:
        for k in ("kv", "kv_at", "ma", "ma_at", "status_bits", "status_at",
                  "pressure_torr", "pressure_at", "pressure_raw"):
            S._SPELL[k] = None
    S._SPELL_MA_HIST.clear()
    S._VAC_OVER.update(n=0, last_at=None)
    S._HV_GUARD.update(enabled=True, tripped=None)


msg = S._spellman_on_message
now = S.time.monotonic()

# 1. Parsing. [] (its parse failed) is NOT a reading -- never a made-up 0.
reset()
msg({"type": "spellman_response", "cmd": "GetkVMonitor", "data": []}, now)
msg({"type": "spellman_response", "cmd": "GetmAMonitor", "data": []}, now)
h = S.hv_supply_snapshot()
check("empty kV / mA replies -> no reading (None), not 0", h["kv"] is None and h["ma"] is None, h)
msg({"type": "spellman_response", "cmd": "GetkVMonitor", "data": 59.8}, now)
msg({"type": "spellman_response", "cmd": "GetmAMonitor", "data": 0.012}, now)
msg({"type": "spellman_response", "cmd": "GetStatus", "data": [1, 0, 1, 0]}, now)
h = S.hv_supply_snapshot()
check("kV, mA, status bits read", h["kv"] == 59.8 and h["ma"] == 0.012 and h["status_bits"] == [1, 0, 1, 0], h)
msg({"type": "pressure_point", "data": {"pressure_torr": 5.9e-6, "raw": "VAC=5.9e-06"}}, now)
v = S.vacuum_snapshot()
check("pressure read, within the 2.5e-5 limit", v["pressure_torr"] == 5.9e-6 and v["within_limit"] is True, v)

# 2. Stale -> no reading.
reset()
msg({"type": "pressure_point", "data": {"pressure_torr": 5.9e-6}}, now - 60)
v = S.vacuum_snapshot()
check("a 60 s old reading -> pressure None, within_limit None", v["pressure_torr"] is None
      and v["within_limit"] is None and v["age_s"] >= 59, v)

# 3. The interlock's refusal.
reset()
r = S.vacuum_refusal("emission ON")
check("no reading -> refused (unknown is not good)", r and "no pressure reading" in r, r)
msg({"type": "pressure_point", "data": {"pressure_torr": 3.1e-5}}, S.time.monotonic())
r = S.vacuum_refusal("emission ON")
check("3.1e-5 Torr -> refused, names value and limit", r and "3.10e-05" in r and "2.5e-05" in r, r)
msg({"type": "pressure_point", "data": {"pressure_torr": 6e-6}}, S.time.monotonic())
check("6e-6 Torr -> allowed", S.vacuum_refusal("emission ON") is None)
S.VACUUM_INTERLOCK = False
reset()
check("CT_VACUUM_INTERLOCK=0 -> never refused", S.vacuum_refusal("emission ON") is None)
S.VACUUM_INTERLOCK = True

# 4. Running over the limit: two NEW readings -> rails off, hot filaments to IDLE, latched.
rails = []
S.stm32_hv_enable_set = lambda host, ch, on: (rails.append((ch, on)), {"ok": True})[1]
S._safety_record = lambda *a, **k: None
S._safety_fallback_filaments = lambda fids, st: None


class _Cl:
    connected = True


class _L:
    client = _Cl()
    host = "esp"


S.CONTROLLERS.clear()
S.CONTROLLERS.update({1: _L()})
reset()
S.LAST_POWER_STATE.clear()
S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_VOLTAGE), 0.0)
S.ACTIVE_DEADLINES.clear()
S.ACTIVE_DEADLINES[20] = (1e12, 1200)
t = S.time.monotonic()    # freshness is judged against the real clock
msg({"type": "pressure_point", "data": {"pressure_torr": 4e-5}}, t)
r1 = S._vacuum_tick(t + 0.5)
r1b = S._vacuum_tick(t + 1.0)            # same reading again: not counted
check("one over-limit reading (seen twice) -> no trip yet", r1 is None and r1b is None and not rails)
msg({"type": "pressure_point", "data": {"pressure_torr": 4.2e-5}}, t + 2)
r2 = S._vacuum_tick(t + 2.5)
check("second NEW over-limit reading -> tripped, source vacuum", r2 and r2["action"] == "tripped"
      and S._HV_GUARD["tripped"]["source"] == "vacuum", r2)
check("...emission and focus switched off", ("emission", False) in rails and ("focus", False) in rails, rails)
check("...the VOLTAGE filament is due back to its IDLE 1200 mA now",
      S.ACTIVE_DEADLINES.get(20) == (t + 2.5, 1200) and r2["to_idle"] == [20], S.ACTIVE_DEADLINES)

# 5. The slow turn-off warning (FID 20: +2.43 mA 1-5 s after its 09:02 shot).
def tail_case(after_ma):
    S._HVG_HIST.clear()
    for k in range(10):
        S._HVG_HIST.append((100.0 + k, 12.45, -204.5))
    S.LAST_POWER_STATE.clear()
    S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_VOLTAGE), 0.0)
    with S._SIM_LOCK:
        S._SIM_STATE.update(running=True, started_at=110.0, ended_at=0.0)
    S._tail_on_train_start(110.0)
    with S._SIM_LOCK:
        S._SIM_STATE.update(running=False, ended_at=111.0)
    out = None
    for k in range(8):
        out = S._tail_tick(111.0 + k, after_ma) or out
    return out


w = tail_case(14.9)
check("+2.45 mA 1-5 s after the train -> warning naming the filament",
      w and w["action"] == "warning" and w["filaments"] == [20] and 2.3 < w["tail_ma"] < 2.6, w)
check("normal fall -> nothing", tail_case(12.47) is None)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
