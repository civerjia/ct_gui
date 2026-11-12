#!/usr/bin/env python3
"""Spellman V/I and the vacuum, read-only from control2's spellman_backend;
the 2.5e-5 Torr interlock; the slow turn-off warning. Offline: messages are
fed straight to the parser, the rails and the force-off are fakes.

    python3 tests/test_vacuum_spellman.py
"""
import _path  # noqa: F401

import os

import tempfile

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"
os.environ["CT_STATE_DIR"] = tempfile.mkdtemp(prefix="ct_vac_")   # the saved limit goes here

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
    S._VAC_OVER.update(n=0, last_at=None, slept=False, tried_at=-1e9)
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
slept = []


def _fake_fallback(fids, st):
    slept.append((list(fids), int(st)))
    for f in fids:
        S.LAST_POWER_STATE[f] = (int(st), 0.0)


S._safety_fallback_filaments = _fake_fallback
S.MAPPING.filaments = lambda c: [20, 21, 22, 23] if c == 0 else []


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
S.LAST_POWER_STATE[21] = (int(S.POWER_STATE_IDLE), 0.0)
S.LAST_POWER_STATE[22] = (int(S.POWER_STATE_SLEEP), 0.0)
S.LAST_POWER_STATE[23] = (int(S.POWER_STATE_STOP), 0.0)
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
check("...every heated filament to SLEEP (IDLE too), STOP/SLEEP ones left alone, timed ACTIVE dropped",
      r2["to_sleep"] == [20, 21] and slept == [([20, 21], int(S.POWER_STATE_SLEEP))]
      and 20 not in S.ACTIVE_DEADLINES, (r2.get("to_sleep"), slept))
check("...confirmed, so no retry", S._VAC_OVER["slept"] and not r2["sleep_not_confirmed"], r2)
r3 = S._vacuum_tick(t + 4.5)
check("still over, latched and slept -> nothing more", r3 is None and len(slept) == 1, r3)
r = S.vacuum_heat_refusal("IDLE")
check("IDLE refused (vacuum latch)", r and "vacuum latch" in r, r)

# 4b. Latched by something else first: over the limit still puts heating to SLEEP.
reset()
slept.clear()
S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_IDLE), 0.0)
S._HV_GUARD["tripped"] = {"source": "spellman"}
t = S.time.monotonic()
msg({"type": "pressure_point", "data": {"pressure_torr": 4e-5}}, t)
S._vacuum_tick(t + 0.5)
msg({"type": "pressure_point", "data": {"pressure_torr": 4e-5}}, t + 2)
r4 = S._vacuum_tick(t + 2.5)
check("guard already latched -> heating still to SLEEP", r4 and r4["action"] == "slept"
      and 20 in r4["to_sleep"] and S._HV_GUARD["tripped"]["source"] == "spellman", r4)

# 4c. Latched: recovering does NOT bring heating back -- a person clears it,
# and only once the vacuum is within the limit. Survives a restart.
check("latched and saved", S._VAC_LATCH[0] and S._vacuum_latch_load(), S._VAC_LATCH[0])
check("still over -> clear refused", "still over" in (S.vacuum_latch_clear("test") or ""))
msg({"type": "pressure_point", "data": {"pressure_torr": 6e-6}}, S.time.monotonic())
S._vacuum_tick(S.time.monotonic())
r = S.vacuum_heat_refusal("IDLE")
check("recovered but latched -> IDLE still refused", r and "vacuum latch" in r, r)
S._HV_GUARD["tripped"] = None
check("...ACTIVE too, even with the HV latch cleared", "vacuum latch" in (S.heat_refusal("ACTIVE") or ""))
check("...and the GUI still sees it tripped", S.hv_guard_snapshot()["tripped"]["source"] == "vacuum")
check("recovered -> a person clears it", S.vacuum_latch_clear("test") is None and S._VAC_LATCH[0] is None
      and S._vacuum_latch_load() is None)
check("cleared -> IDLE allowed, next excursion sleeps again",
      S.vacuum_heat_refusal("IDLE") is None and not S._VAC_OVER["slept"])
reset()
check("no reading -> IDLE NOT refused (IDLE keeps the vacuum)", S.vacuum_heat_refusal("IDLE") is None)

# 4d. No reading at all for VACUUM_MISSING_TRIP_S trips like over-pressure.
reset()
slept.clear()
rails.clear()
S._VAC_LATCH[0] = None
S.LAST_POWER_STATE[21] = (int(S.POWER_STATE_IDLE), 0.0)
t = S.time.monotonic()
S._VAC_T0 = t - 1000                     # backend up long ago
msg({"type": "pressure_point", "data": {"pressure_torr": 6e-6}}, t - 30)   # last one 30 s ago
check("30 s without a reading -> nothing yet", S._vacuum_tick(t) is None and not slept)
r5 = S._vacuum_tick(t + 31)
check("61 s without a reading -> HV off, heating SLEEP, latched", r5 and r5["action"] == "tripped"
      and ("emission", False) in rails and 21 in r5["to_sleep"] and S._VAC_LATCH[0]
      and "no vacuum reading" in r5["message"], r5)
check("...then quiet (slept, latched)", S._vacuum_tick(t + 40) is None and len(slept) == 1)
check("...latch not clearable without a fresh reading", "no fresh" in (S.vacuum_latch_clear("t") or ""))
check("...HV ON refused by the latch", "vacuum latch" in (S.vacuum_refusal("emission ON") or ""))
msg({"type": "pressure_point", "data": {"pressure_torr": 6e-6}}, S.time.monotonic())
check("reading back -> a person can clear it", S.vacuum_latch_clear("t") is None)
reset()
S._VAC_BYPASS["until"] = S.time.monotonic() + 600
S._VAC_T0 = S.time.monotonic()
check("just started, no reading yet -> waits the 60 s from start-up",
      S._vacuum_tick(S.time.monotonic() + 30) is None)
S._VAC_T0 = S.time.monotonic() - 1000
check("debug bypass -> no reading never trips", S._vacuum_tick(S.time.monotonic() + 3600) is None)
S._VAC_BYPASS["until"] = None

# 4e. The guard tick reaches the vacuum check even when latched by something else.
reset()
slept.clear()
S._VAC_LATCH[0] = None
S.LAST_POWER_STATE[21] = (int(S.POWER_STATE_IDLE), 0.0)
S._HV_GUARD["tripped"] = {"source": "grid"}
t = S.time.monotonic()
msg({"type": "pressure_point", "data": {"pressure_torr": 4e-5}}, t)
S.hv_guard_tick(t + 0.5)
msg({"type": "pressure_point", "data": {"pressure_torr": 4e-5}}, t + 2)
S.hv_guard_tick(t + 2.5)
check("hv_guard_tick while latched (grid) -> vacuum still SLEEPs heating", slept and 21 in slept[0][0], slept)
S._VAC_LATCH[0] = None
S._vacuum_latch_save()

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

# 6. The limit can be changed, is saved, and the interlock follows it.
reset()
msg({"type": "pressure_point", "data": {"pressure_torr": 3.0e-5}}, S.time.monotonic())
check("3e-5 Torr over the default 2.5e-5 -> refused", S.vacuum_refusal("emission ON") is not None)
r = S.vacuum_set_limit(5e-5, "test")
check("limit set to 5e-5, saved", r["ok"] and r["limit_torr"] == 5e-5 and r["was"] == 2.5e-5 and r["saved"], r)
check("...3e-5 now allowed", S.vacuum_refusal("emission ON") is None)
check("...and it is what a restart would load", S._vacuum_limit_load() == 5e-5)
check("...shown in /api/vacuum and /api/series", S.vacuum_snapshot()["limit_torr"] == 5e-5
      and S.series_get(["vac"])["vacuum_limit_torr"] == 5e-5)
r = S.vacuum_set_limit(5, "test")
check("5 Torr (a typo) -> refused, unchanged", not r["ok"] and S.vacuum_limit() == 5e-5, r)
r = S.vacuum_set_limit("abc", "test")
check("not a number -> refused", not r["ok"] and "number" in r["error"], r)
S.vacuum_set_limit(2.5e-5, "test")

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
