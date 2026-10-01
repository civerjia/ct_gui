#!/usr/bin/env python3
"""Timed ACTIVE (active_s): the backend returns a filament to IDLE when its
ACTIVE time runs out. Offline -- the hardware calls are replaced by fakes, so
this checks the clock and its rules, not the RP2350.

    python3 tests/test_timed_active.py
"""

import _path  # noqa: F401

import os
import threading
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeClient:
    connected = True


class FakeLink:
    client = FakeClient()


calls = []
running = {"v": False}
fail_next = {"v": 0}


def fake_prep(link, c0, state, fids, currents=None, default_arg=0, channels=None):
    calls.append((c0, state, list(fids), dict(currents or {})))
    if fail_next["v"]:
        fail_next["v"] -= 1
        return {"landed": []}
    S.note_power_state(fids, state, currents)
    return {"landed": list(fids)}


S.prep_filaments = fake_prep
S._safety_schedule_running = lambda: (running["v"], None)
S.filament_to_board = lambda f: (0, 0, int(f) % 8, None)
S.CONTROLLERS.clear()
S.CONTROLLERS[1] = FakeLink()
S._safety_record = lambda kind, detail: calls.append(("event", kind))
S.ACTIVE_DEADLINE_RETRY_S = 0.3
threading.Thread(target=S._active_deadline_loop, daemon=True).start()

# --- parse rules ------------------------------------------------------------
t, e = S._parse_timed_active({}, S.POWER_STATE_ACTIVE, [1])
check("no active_s -> not timed", t is None and e is None)
t, e = S._parse_timed_active({"active_s": 5}, S.POWER_STATE_IDLE, [1])
check("active_s refused for IDLE", t is None and "only applies to ACTIVE" in (e or ""))
t, e = S._parse_timed_active({"then_idle_ma": 1500}, S.POWER_STATE_ACTIVE, [1])
check("then_idle_ma without active_s refused", e and "needs active_s" in e)
t, e = S._parse_timed_active({"active_s": 0}, S.POWER_STATE_ACTIVE, [1])
check("active_s 0 refused", e and "out of range" in e)
t, e = S._parse_timed_active({"active_s": 5}, S.POWER_STATE_ACTIVE, None)
check("batch without filament list refused", e and "explicit filament list" in e)
S.LAST_IDLE_MA.clear()
t, e = S._parse_timed_active({"active_s": 5}, S.POWER_STATE_ACTIVE, [7])
check("unknown IDLE current refused, not defaulted", e and "no IDLE current known" in e)
t, e = S._parse_timed_active({"active_s": 5, "then_idle_ma": 9999}, S.POWER_STATE_ACTIVE, [7])
check("then_idle_ma above ceiling refused", e and "out of range" in e)

# IDLE records the current a timed ACTIVE returns to
S.note_power_state([3], S.POWER_STATE_IDLE, {3: 1450})
t, e = S._parse_timed_active({"active_s": 5}, S.POWER_STATE_ACTIVE, [3])
check("last IDLE current used by default", e is None and t["idle_ma"] == {3: 1450}, str((t, e)))

# --- expiry returns to IDLE --------------------------------------------------
calls.clear()
S.note_power_state([3], S.POWER_STATE_ACTIVE, {3: 2600})
S.set_active_deadlines([3], 0.4, {3: 1450})
time.sleep(0.25)
check("not returned before active_s", not any(c[0] == 0 for c in calls if c[0] != "event"))
time.sleep(0.5)
idle = [c for c in calls if c[0] == 0 and c[1] == S.POWER_STATE_IDLE]
check("returned to IDLE after active_s", idle and idle[0][2] == [3] and idle[0][3] == {3: 1450}, str(calls))
check("deadline cleared after return", 3 not in S.ACTIVE_DEADLINES)
check("state recorded as IDLE", S.LAST_POWER_STATE[3][0] == S.POWER_STATE_IDLE)

# --- a later command cancels it ----------------------------------------------
calls.clear()
S.note_power_state([4], S.POWER_STATE_IDLE, {4: 1300})
S.note_power_state([4], S.POWER_STATE_ACTIVE, {4: 2600})
S.set_active_deadlines([4], 0.4, {4: 1300})
S.note_power_state([4], S.POWER_STATE_STOP, {4: 0})
time.sleep(0.7)
check("STOP cancels the clock", not [c for c in calls if c[0] == 0], str(calls))

# --- held while a schedule runs, applied after --------------------------------
calls.clear()
running["v"] = True
S.note_power_state([5], S.POWER_STATE_ACTIVE, {5: 2600})
S.set_active_deadlines([5], 0.2, {5: 1200})
time.sleep(0.8)
check("held while a run is in progress", not [c for c in calls if c[0] == 0] and 5 in S.ACTIVE_DEADLINES, str(calls))
check("deferral recorded", ("event", "timed_active_deferred") in calls)
running["v"] = False
time.sleep(0.8)
check("applied once the run ended", [c for c in calls if c[0] == 0 and c[2] == [5]], str(calls))

# --- a failed write is retried -------------------------------------------------
calls.clear()
fail_next["v"] = 1
S.note_power_state([6], S.POWER_STATE_ACTIVE, {6: 2600})
S.set_active_deadlines([6], 0.1, {6: 1100})
time.sleep(1.0)
tries = [c for c in calls if c[0] == 0 and c[2] == [6]]
check("failed return retried", len(tries) >= 2 and 6 not in S.ACTIVE_DEADLINES, str(calls))
check("failure recorded", ("event", "timed_active_to_idle_failed") in calls)

print(f"{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
