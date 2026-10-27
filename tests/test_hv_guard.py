#!/usr/bin/env python3
"""A grid switch that will not turn off takes the HV down. Offline: the
emission reading, the force-off and the rail switch are fakes.

2026-10-27 12:18: FID 20's grid MOSFET was blown. Its OFF read-back was 0 (the
165 reads the 595, upstream of the gate), so the per-pulse verify and the grid
sweep saw nothing while the emission supply carried its current for 70 s.

    python3 tests/test_hv_guard.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

QUIET = S._hv_guard_quiet       # the real one, before setup() replaces it

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Rig:
    """reading(): what the ADS says now. force_open() may fix it (a 595 that
    flipped) or not (a blown FET)."""

    def __init__(self, base=12.45, focus=-204.5):
        self.ma, self.focus, self.emiss_v = base, focus, -197.8
        self.fixable = True
        self.base = base
        self.forced, self.rails, self.quiet = 0, [], True

    def read(self):
        return {"ok": True, "emiss_i_ma": self.ma, "focus_v": self.focus, "emiss_v": self.emiss_v}

    def force(self, link, cid, masks, reason=""):
        self.forced += 1
        if self.fixable:
            self.ma = self.base
        return {"ok": True}

    def rail(self, host, ch, on):
        self.rails.append((ch, on))
        return {"ok": True}


class _Cl:
    connected = True


class _Link:
    client = _Cl()
    host = "esp"


def setup(rig):
    S._HVG_HIST.clear()
    S._HVG_HIGH_SINCE[0] = None
    S._HV_GUARD.update(enabled=True, tripped=None)
    S._hv_guard_read = rig.read
    S._hv_guard_quiet = lambda now: rig.quiet
    S.force_grid_off = rig.force
    S.stm32_hv_enable_set = rig.rail
    S._note_forced_open = lambda *a, **k: None
    S._safety_record = lambda *a, **k: None
    S._hv_guard_read_fresh = rig.read
    S.CONTROLLERS.clear()
    S.CONTROLLERS.update({1: _Link(), 2: _Link()})
    S.LAST_POWER_STATE.clear()
    rig.stopped = []
    S._safety_fallback_filaments = lambda fids, state: rig.stopped.append((sorted(fids), int(state)))
    S._hv_guard_stm32_flags = lambda: rig.stm32
    rig.stm32 = {"ok": True, "flags": 0, "reason": 5}
    rig.guard_ops = []

    def _g(host, op=None, **k):
        rig.guard_ops.append(op)
        return {"ok": True, "latched": op != "clear", "thr_counts": 36,
                "trip_base_x16": 1049 * 16, "trip_mean_x16": 1096 * 16}
    S.stm32_hv_guard = _g
    rig.after = [1096 * 16] * 3                       # STM32 window means once the HV is back
    rig.rail_back = -197.8
    S._hv_guard_wait_rail = lambda host, pre_v: rig.rail_back
    S._hv_guard_verify_counts = lambda host: list(rig.after)
    S._HVG_LAST_EMISS_V[0] = -197.8
    rig.fb = [[0] * 8, [0] * 8]                       # 165 read-back per controller (1, 2)
    S._hv_bytes = lambda link: ([0] * 8, list(rig.fb[0 if link is S.CONTROLLERS.get(1) else 1]))
    S.hv_preflight_switches = lambda reason: {"ok": True, "opened": {}, "unchecked": []}
    S.used_channel_mask = lambda cid: 0x3F
    S._HVG_RECOVERIES.clear()
    S._HVG_RAILS.update(emission=True, focus=True)


def run(rig, seconds, t0, step=1.0):
    """Tick once per `step` s from t0; return (last result, end time)."""
    out, t = None, t0
    while t < t0 + seconds:
        r = S.hv_guard_tick(t)
        out = r or out
        t += step
    return out, t


# 1. A switch that flipped (595): forced open, current comes down -> cleared, HV stays on.
rig = Rig()
setup(rig)
_, t = run(rig, 15, 0.0)
rig.ma = 16.4
r, t = run(rig, 5, t)
check("595 flip: forced open and cleared", r and r["action"] == "cleared" and rig.forced == 2, r)
check("...HV left on", rig.rails == [] and S._HV_GUARD["tripped"] is None, rig.rails)
check("...and no heating touched", rig.stopped == [], rig.stopped)

# 2. A blown FET: still high with every switch open -> both rails off, latched, suspects named.
rig = Rig()
setup(rig)
S._LAST_HOT.clear()
S._LAST_HOT.update({3: 0.0, 20: 14.0})
rig.fixable = False
_, t = run(rig, 15, 0.0)
rig.ma = 16.4
r, t = run(rig, 5, t)
check("blown FET: tripped", r and r["action"] == "tripped", r)
check("...emission AND focus switched off", ("emission", False) in rig.rails and ("focus", False) in rig.rails,
      rig.rails)
check("...latched, with the suspects most recent first", S._HV_GUARD["tripped"]
      and S._HV_GUARD["tripped"]["suspects"] == [20, 3], S._HV_GUARD["tripped"])
check("...message says HV OFF and how to clear", "HV OFF" in S._HV_GUARD["tripped"]["message"]
      and "/api/hv-guard" in S._HV_GUARD["tripped"]["message"])
check("...heating of the filaments hot within 15 s set to STOP (20; not 3, 19 s cold)",
      rig.stopped == [([20], int(S.POWER_STATE_STOP))], rig.stopped)
n = len(rig.rails)
run(rig, 10, t)
check("...latched: no further action while tripped", len(rig.rails) == n and rig.forced == 2)

# 2b. Only what is hot now or within 60 s is stopped; an old one is left alone.
rig = Rig()
setup(rig)
S._LAST_HOT.clear()
S._LAST_HOT.update({7: 0.0})                 # hot 100+ s before the trip
S.LAST_POWER_STATE[33] = (int(S.POWER_STATE_VOLTAGE), 0.0)   # hot right now
rig.fixable = False
_, t = run(rig, 100, 0.0)
rig.ma = 16.4
r, t = run(rig, 5, t)
check("trip stops the filament hot NOW, not one hot 100 s ago", rig.stopped == [([33], int(S.POWER_STATE_STOP))]
      and r["heating_stopped"] == [33] and r["suspects"][0] == 33, (rig.stopped, r.get("suspects")))

# 3. Not sustained long enough -> nothing.
rig = Rig()
setup(rig)
_, t = run(rig, 15, 0.0)
rig.ma = 16.4
r, t = run(rig, 2, t)
rig.ma = rig.base
r2, t = run(rig, 5, t)
check("2 s rise -> nothing", r is None and r2 is None and rig.forced == 0, (r, r2))

# 4. Not quiet (a pulse train / an armed run) -> not acted on, not taken as baseline.
rig = Rig()
setup(rig)
_, t = run(rig, 15, 0.0)
rig.quiet, rig.ma = False, 30.0
r, t = run(rig, 10, t)
check("pulses in progress -> ignored", r is None and rig.forced == 0, r)
check("...and not in the baseline", max(m for _, m, _f in S._HVG_HIST) < 13, S._HVG_HIST[-3:])

# 5. Focus moved (the standing current follows it) -> new baseline, no trip.
rig = Rig(base=10.09, focus=-198.6)
setup(rig)
_, t = run(rig, 15, 0.0)
rig.ma, rig.focus = 12.46, -206.5
r, t = run(rig, 20, t)
check("focus change -> baseline restarts, no action", r is None and rig.forced == 0, r)

# 6. Rail off -> nothing, history cleared.
rig = Rig()
setup(rig)
_, t = run(rig, 15, 0.0)
rig.emiss_v, rig.ma = -1.6, 0.1
r, t = run(rig, 10, t)
check("emission rail off -> nothing, baseline dropped", r is None and not S._HVG_HIST and rig.forced == 0, r)

# 7. Too little quiet history -> a rise under the ceiling may not act yet ...
rig = Rig()
setup(rig)
_, t = run(rig, 4, 0.0)
rig.ma = 15.0
r, t = run(rig, 5, t)
check("baseline < 10 s old, 15 mA -> no action", r is None and rig.forced == 0, r)

# 7b. ... but over the ceiling it acts with no baseline at all: HV switched on
#     with a switch already conducting would otherwise learn the fault (08:31).
rig = Rig()
setup(rig)
rig.ma, rig.fixable = 22.6, False
r, t = run(rig, 5, 0.0)
check("no baseline, 22.6 mA (over the 16 mA ceiling) -> tripped", r and r["action"] == "tripped"
      and r["baseline_ma"] is None and "ceiling" in S._HV_GUARD["tripped"]["message"], r)
rig = Rig()
setup(rig)
rig.ma = 22.6
r, t = run(rig, 5, 0.0)
check("no baseline, 22.6 mA, forced open fixes it -> cleared", r and r["action"] == "cleared", r)

# 8. Disabled -> nothing.
rig = Rig()
setup(rig)
S._HV_GUARD["enabled"] = False
_, t = run(rig, 15, 0.0)
rig.ma, rig.fixable = 20.0, False
r, t = run(rig, 6, t)
check("disabled -> nothing", r is None and rig.forced == 0 and rig.rails == [], r)

# 9. A restart ends the loop (see test_grid_sweep for the others).
import threading  # noqa: E402
S.HV_GUARD_TICK_S = 0.01
S._STOPPING.set()
try:
    th = threading.Thread(target=S._hv_guard_loop, daemon=True)
    th.start()
    th.join(timeout=3.0)
    check("_hv_guard_loop ends once the process is restarting", not th.is_alive())
finally:
    S._STOPPING.clear()

# 10. What counts as "between pulses".
S.board_monitor_snapshot = lambda cid: {"run_owns": True}      # both controllers armed
S.CONTROLLERS.clear()
S.CONTROLLERS.update({1: _Link(), 2: _Link()})
S._GRID_CLOSED.clear()
now = 1000.0
S._SIM_STATE.update(running=False, ended_at=990.0, started_at=-1e9)
check("armed, no sim for ages (external triggers) -> busy", not QUIET(now))
S._SIM_STATE.update(started_at=985.0)
check("armed, sim-driven, train over -> quiet (the heating wait)", QUIET(now))
S._SIM_STATE.update(running=True)
check("sim train running -> busy", not QUIET(now))
S._SIM_STATE.update(running=False, ended_at=999.0)
check("1 s after the train -> busy (its last pulse)", not QUIET(now))
S._SIM_STATE.update(ended_at=990.0)
S._GRID_CLOSED.add(20)
check("a switch the host closed -> busy", not QUIET(now))
S._GRID_CLOSED.clear()
S.board_monitor_snapshot = lambda cid: {"run_owns": False}
S._SIM_STATE.update(started_at=-1e9)
check("nothing armed, no sim -> quiet", QUIET(now))

# 11. STM32 trip; switches forced open, HV put back, current STILL over the
#     baseline (the measured current decides -- the 165 is not trusted): a
#     MOSFET nothing can open -> HV off again, STOP within 15 s, latched.
rig = Rig()
setup(rig)
S._LAST_HOT.clear()
S._LAST_HOT.update({20: 99.0, 16: 40.0})       # 16 was hot a minute ago: innocent (15:21 case)
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("still over after force-open + HV back -> tripped, source stm32", r and r["action"] == "tripped"
      and S._HV_GUARD["tripped"]["source"] == "stm32" and "cannot be turned off" in r["message"], r and r.get("message"))
check("...every switch forced open (both controllers)", rig.forced == 2, rig.forced)
check("...HV was put back to check, then off again", rig.rails == [("emission", True), ("focus", True),
                                                                  ("emission", False), ("focus", False)], rig.rails)
check("...only the filament hot within 15 s to STOP (not 16, a minute cold)",
      rig.stopped == [([20], int(S.POWER_STATE_STOP))], rig.stopped)
n = rig.forced
S.hv_guard_tick(101.0)
check("...handled once: latched, nothing repeated", rig.forced == n)

# 12. Current back at the baseline once the switches were forced open: it was
#     a 595 flip and it is fixed -> rails stay on, no heat touched, no latch.
rig = Rig()
setup(rig)
S._LAST_HOT.clear()
S._LAST_HOT.update({16: 95.0})
rig.after = [1050 * 16, 1049 * 16, 1051 * 16]
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("current back at baseline -> recovered, not latched", r and r["action"] == "recovered"
      and S._HV_GUARD["tripped"] is None, r)
check("...STM32 latch cleared, emission and focus back on (and left on)", "clear" in rig.guard_ops
      and rig.rails == [("emission", True), ("focus", True)], (rig.guard_ops, rig.rails))
check("...no heating touched", rig.stopped == [], rig.stopped)

# 12a. The 165 says a switch is closed but the current is fine -> still recovered
#      (the 165 is only logged), and the reverse: 165 all open but current up -> tripped.
rig = Rig()
setup(rig)
rig.fb[0] = [0, 0, 0, 0, 0, 1, 0, 0]
rig.after = [1050 * 16] * 3
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("165 reads a switch closed, current fine -> recovered; the 165 only logged",
      r and r["action"] == "recovered" and "CH6.1" in r["read_back_165"], r and r.get("read_back_165"))

# 12b. Rails that were off before stay off.
rig = Rig()
setup(rig)
S._HVG_RAILS.update(emission=True, focus=False)
rig.after = [1050 * 16] * 3
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("...only the rails that were on come back", r and r["action"] == "recovered"
      and rig.rails == [("emission", True)], rig.rails)

# 12c. Too many recoveries -> the next one latches without a second HV ON.
rig = Rig()
setup(rig)
S._HVG_RECOVERIES[:] = [50.0, 60.0, 70.0]
rig.after = [1050 * 16] * 3
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("4th within 10 min -> latched, HV not put back", r and r["action"] == "tripped"
      and "recoveries already" in r["message"] and ("emission", True) not in rig.rails, r and r.get("message"))

# 12d. The rail never comes back -> cannot check -> latched.
rig = Rig()
setup(rig)
rig.rail_back = None
rig.after = [1050 * 16] * 3
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("rail did not come back -> latched, HV off again", r and r["action"] == "tripped"
      and "did not come back" in r["message"] and rig.rails[-2:] == [("emission", False), ("focus", False)],
      (r and r.get("message"), rig.rails))

rig = Rig()
setup(rig)
rig.stm32 = {"ok": True, "flags": 0, "reason": 8}
r = S.hv_guard_tick(100.0)
check("reason 8 without the flag (latch cleared; reason is first-wins until reset) -> not a trip",
      r is None and rig.forced == 0, r)

# 13. No heating while the HV is back on to measure (the user, 2026-10-27):
#     a VOLTAGE filament goes to its IDLE before the rails come back, and any
#     ACTIVE/VOLTAGE request during the check is refused; lifted afterwards.
rig = Rig()
setup(rig)
S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_VOLTAGE), 0.0)
S.ACTIVE_DEADLINES.clear()
S.ACTIVE_DEADLINES[20] = (1e12, 1100)
order, refused_during = [], []


def _wait():
    order.append("cooled" if S.ACTIVE_DEADLINES.get(20, (1e12,))[0] < 1e11 else "not cooled")
    S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_IDLE), 0.0)   # what the deadline loop would do
    return []


S._wait_not_hot = _wait
_rail = S.stm32_hv_enable_set


def _rail_check(host, ch, on):
    if on:
        order.append("rails on")
        refused_during.append(S.heat_refusal("test"))
    return _rail(host, ch, on)


S.stm32_hv_enable_set = _rail_check
rig.after = [1050 * 16] * 3
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("VOLTAGE filament sent to its IDLE (1100 mA) before the HV came back",
      order[:2] == ["cooled", "rails on"] and r and r.get("cooled_to_idle") == [20], (order, r and r.get("cooled_to_idle")))
check("...heating refused while the HV is on for the check",
      refused_during and all(x and "HV guard check in progress" in x for x in refused_during), refused_during)
S._SPELL["pressure_torr"], S._SPELL["pressure_at"] = 6e-6, S.time.monotonic()
check("...and allowed again once the check is over", S.heat_refusal("test") is None, S.heat_refusal("test"))

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
