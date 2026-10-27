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
    S.stm32_hv_guard = lambda host, op=None, **k: {"ok": True, "latched": True, "trip_base_x16": 1049 * 16,
                                                    "trip_mean_x16": 1096 * 16}


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
check("...heating of the recently hot filaments set to STOP (no heat, no electrons)",
      rig.stopped == [([3, 20], int(S.POWER_STATE_STOP))], rig.stopped)
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

# 11. The STM32's own guard tripped (it already took the HV down): the backend
#     opens every switch, stops the heat, latches, names the suspects.
rig = Rig()
setup(rig)
S._LAST_HOT.clear()
S._LAST_HOT.update({20: 99.0})
rig.stm32 = {"ok": True, "flags": 24 | 32, "reason": 8}
r = S.hv_guard_tick(100.0)
check("STM32 trip seen: tripped here too, source stm32", r and r["action"] == "tripped"
      and S._HV_GUARD["tripped"]["source"] == "stm32", r)
check("...every switch forced open (both controllers)", rig.forced == 2, rig.forced)
check("...the hot filament to STOP", rig.stopped == [([20], int(S.POWER_STATE_STOP))], rig.stopped)
check("...rise from the STM32's own numbers (~2 mA)", r and 1.8 < r["rise_ma_approx"] < 2.2, r.get("rise_ma_approx"))
check("...the HV was not switched again by the host (the STM32 did it)", rig.rails == [], rig.rails)
n = rig.forced
S.hv_guard_tick(101.0)
check("...handled once: latched, nothing repeated", rig.forced == n)
rig = Rig()
setup(rig)
rig.stm32 = {"ok": True, "flags": 0, "reason": 8}
r = S.hv_guard_tick(100.0)
check("reason 8 without the flag (latch cleared; reason is first-wins until reset) -> not a trip",
      r is None and rig.forced == 0, r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
