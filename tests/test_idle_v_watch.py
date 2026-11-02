#!/usr/bin/env python3
"""Voltage IDLE overshoot watch (2026-11-02: P2 CH5.7 sat at 7.2 V for ~3 min
recorded as IDLE). Fresh readings held over the target -> voltage IDLE sent
again; one that will not come down -> SLEEP. Offline: monitor rows, the
power write and the clock are fakes; the watch is the real one.

    python3 tests/test_idle_v_watch.py
"""
import _path  # noqa: F401

import os
import tempfile

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_STATE_DIR"] = tempfile.mkdtemp(prefix="ct_idle_v_watch_")

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Client:
    connected = True


class Link:
    client = Client()


S.CONTROLLERS.clear()
S.CONTROLLERS[1] = Link()
F = 86
_, CH, POS, _ = S.filament_to_board(F) if S.filament_to_board(F)[0] == 0 else (0, 4, 6, None)
S.filament_to_board = lambda f: (0, CH, POS, None) if int(f) == F else (0, 0, int(f) % 8, None)
S.MAPPING.filament_for_board = lambda c0, ch, mux: F if (ch, mux) == (CH, POS) else None
reading = {"mv": 2600, "ma": 1150, "age": 200}
S.monitor_board_rows = lambda cid: ([{"channel": CH, "mux_port": POS, "bus_mV": reading["mv"],
                                      "current_mA": reading["ma"], "age_ms": reading["age"]}],
                                    {"fresh": True})
sent, events, slept = [], [], []


def fake_prep(link, c0, state, fids, currents=None, default_arg=0, channels=None,
              allow_dead=False, idle_v=False):
    sent.append((state, list(fids), dict(currents or {}), idle_v))
    S.note_power_state(fids, S.POWER_STATE_VOLTAGE if idle_v else state, currents, idle_v=idle_v)
    return {"landed": list(fids)}


S.prep_filaments = fake_prep
S._safety_record = lambda kind, d: events.append((kind, d))
S._safety_fallback_filaments = lambda fids, st: slept.append((list(fids), st))

S.note_power_state([F], S.POWER_STATE_VOLTAGE, {F: 2584}, idle_v=True)
t = 1000.0


def run(seconds):
    global t
    for _ in range(int(seconds)):
        S._idle_v_watch_tick(t)
        t += 1.0


run(5)
check("at its IDLE voltage: nothing done", not sent and not events)

reading.update(mv=7184, ma=2305)
run(1)
reading.update(mv=2600, ma=1150)
run(5)
check("one high reading (a corrupt read): nothing done", not sent and not events, events)

reading.update(mv=7184, ma=2305, age=9000)
run(6)
check("a stale reading does not count", not sent, sent)

reading.update(age=200)
run(2)
check("over for 2 s: not yet", not sent)
run(2)
check("over for 3 s: its voltage IDLE sent again, at ITS target",
      sent == [(S.POWER_STATE_IDLE, [F], {F: 2584}, True)], sent)
check("...recorded with the readings", events and events[-1][0] == "idle_v_overshoot"
      and events[-1][1]["measured_mv"] == 7184 and events[-1][1]["resent"], events[-1:])
check("...shown in the safety snapshot", str(F) in S.idle_v_watch_snapshot())
run(2)
check("the next correction waits a full hold again", len(sent) == 1, sent)
run(8)
check("still over: corrected again, up to 3 times", len(sent) == 3, len(sent))
run(4)
sleeps = [d for k, d in events if k == "idle_v_overshoot_sleep"]
check("will not come down: SLEEP after 3 corrections, recorded",
      slept and slept[0] == ([F], S.POWER_STATE_SLEEP) and sleeps
      and sleeps[0]["corrections"] == 3 and not sleeps[0]["again"], (slept, sleeps[:1]))
n_sent = len(sent)
run(8)      # the SLEEP did not land (the fake leaves it): still high
check("SLEEP did not take: SLEEP again, never the IDLE back up",
      len(sent) == n_sent and len(slept) >= 2 and all(st == S.POWER_STATE_SLEEP for _, st in slept)
      and "SLEEP again" in [d for k, d in events if k == "idle_v_overshoot_sleep"][-1]["message"],
      (sent[n_sent:], slept))

# Back down clears it; a filament not at voltage IDLE is not watched.
sent.clear(); slept.clear(); events.clear()
S._IDLE_V_FIXES.clear(); S._IDLE_V_OVER.clear(); S._IDLE_V_GAVE_UP.clear()
S.note_power_state([F], S.POWER_STATE_VOLTAGE, {F: 2584}, idle_v=True)
reading.update(mv=7184)
run(2)
reading.update(mv=2620)
run(1)
check("back near its target: the overshoot is cleared", F not in S._IDLE_V_OVER)
S.note_power_state([F], S.POWER_STATE_VOLTAGE, {F: 7000})      # heated on purpose
reading.update(mv=7184)
run(6)
check("heated on purpose (a plain VOLTAGE): not watched", not sent and not events, events)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
