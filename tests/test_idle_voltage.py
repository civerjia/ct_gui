#!/usr/bin/env python3
"""IDLE in voltage mode (2026-11-02): the firmware's VOLTAGE(6) at the
filament's IDLE voltage, known to the backend as an IDLE -- the ladder guard,
timed returns, the HV guard's "hot" and the IDLE voltage table. Offline: the
hardware calls are fakes, the bookkeeping is the real one.

    python3 tests/test_idle_voltage.py
"""

import _path  # noqa: F401

import os
import tempfile
import threading
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_STATE_DIR"] = tempfile.mkdtemp(prefix="ct_idle_v_")

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


V, IDLE, ACTIVE = S.POWER_STATE_VOLTAGE, S.POWER_STATE_IDLE, S.POWER_STATE_ACTIVE


def back_in_time(f, s):
    """As if filament f entered IDLE s seconds ago."""
    S.IDLE_SINCE[f] = time.monotonic() - s


# --- bookkeeping: a voltage IDLE is an IDLE, not heating ----------------------
S.note_power_state([3], IDLE, {3: 1200})
S.note_power_state([3], V, {3: 2400}, idle_v=True)
check("voltage IDLE: firmware state kept as VOLTAGE", S.LAST_POWER_STATE[3][0] == V)
check("...is_idle, not is_hot", S.is_idle(3) and not S.is_hot(3))
check("...remembered as the IDLE to go back to (mV)",
      isinstance(S.LAST_IDLE[3], S.IdleMv) and S.LAST_IDLE[3] == 2400)
check("...CC IDLE -> voltage IDLE keeps the IDLE clock (it did not cool)", 3 in S.IDLE_SINCE)
S.note_power_state([3], V, {3: 7600})          # a plain (heating) VOLTAGE
check("a plain VOLTAGE is hot, not an IDLE", S.is_hot(3) and not S.is_idle(3)
      and 3 not in S.IDLE_VOLT_NOW and 3 not in S.IDLE_SINCE)
check("...the IDLE to go back to is still the voltage one", S.LAST_IDLE[3] == 2400
      and isinstance(S.LAST_IDLE[3], S.IdleMv))
S.note_power_state([3], V, {3: 2400}, idle_v=True)
check("heating VOLTAGE -> voltage IDLE counts as leaving heat",
      time.monotonic() - S.LAST_ACTIVE_LEFT.get(3, 0) < 1.0)

# --- the ladder guard ----------------------------------------------------------
S.IDLE_MV_TABLE[5] = {"mV": 2500, "mA": 1180}
S.note_power_state([5], V, {5: 2500}, idle_v=True)       # from nothing: cold
back_in_time(5, 2)
why = S.ladder_blocks_active(5, "settled", True, 1100, 2500)
check("voltage IDLE arrived but held 2 s: refused (voltage arrives whatever the temperature)",
      why and "not warm yet" in why and "voltage IDLE 2500 mV" in why, why)
back_in_time(5, 12)
check("held 12 s, arrived, current there: allowed",
      S.ladder_blocks_active(5, "settled", True, 1100, 2500) is None)
check("'settling' (arrived, inside the settle time) counts as arrived",
      S.ladder_blocks_active(5, "settling", True, 1100, 2500) is None)
why = S.ladder_blocks_active(5, "settled", True, 40, 2500)
check("open filament (voltage there, 40 mA): refused, names the current",
      why and "40 mA" in why and "590" in why, why)
why = S.ladder_blocks_active(5, "ramping", True, 1100, 2500)
check("voltage not arrived: refused", why and "'ramping'" in why, why)
S.note_power_state([6], IDLE, {6: 1200})
back_in_time(6, 60)
S.note_power_state([6], V, {6: 2400}, idle_v=True)
check("warm CC IDLE switched to voltage IDLE: allowed at once",
      S.ladder_blocks_active(6, "settled", True, 1150, 2400) is None)
S.note_power_state([7], V, {7: 7600})
S.note_power_state([7], V, {7: 2400}, idle_v=True)
check("just down from a heating VOLTAGE: warm (WARM_AFTER_ACTIVE_S)",
      S.ladder_blocks_active(7, "ramping", True, 1500, 2400) is None)
S.note_power_state([8], S.POWER_STATE_STANDBY, {8: 0})
check("STANDBY still refused", "STANDBY" in (S.ladder_blocks_active(8) or ""))

# --- timed returns --------------------------------------------------------------
S.IDLE_MV_TABLE[9] = {"mV": 2300}
t, e = S._parse_timed_active({"active_s": 5, "then_idle_voltage": True}, V, [9],
                             allow_voltage=True)
check("then_idle_voltage: the filament's own IDLE voltage", e is None
      and isinstance(t["idle_ma"][9], S.IdleMv) and t["idle_ma"][9] == 2300, (t, e))
t, e = S._parse_timed_active({"active_s": 5, "then_idle_voltage": True}, V, [9, 44],
                             allow_voltage=True)
check("then_idle_voltage with no IDLE voltage known: refused, named", e and "[44]" in e, e)
t, e = S._parse_timed_active({"active_s": 5, "then_idle_mv": 6000}, V, [9], allow_voltage=True)
check("then_idle_mv over the voltage IDLE ceiling: refused", e and "out of range" in e, e)
t, e = S._parse_timed_active({"active_s": 5, "then_idle_mv": 2300, "then_idle_ma": 1200},
                             V, [9], allow_voltage=True)
check("then_idle_mv AND then_idle_ma: refused", e and "not several" in e, e)
t, e = S._parse_timed_active({"then_idle_voltage": True}, V, [9], allow_voltage=True)
check("then_idle_voltage without active_s: refused", e and "needs active_s" in e, e)
t, e = S._parse_timed_active({"active_s": 5}, V, [3], allow_voltage=True)
check("no return given: back to the LAST IDLE, here the voltage one",
      e is None and isinstance(t["idle_ma"][3], S.IdleMv) and t["idle_ma"][3] == 2400, (t, e))


# The deadline loop walks a timed VOLTAGE back to a voltage IDLE.
class FakeClient:
    connected = True


class FakeLink:
    client = FakeClient()


calls = []


def fake_prep(link, c0, state, fids, currents=None, default_arg=0, channels=None, idle_v=False):
    calls.append((state, list(fids), dict(currents or {}), idle_v))
    S.note_power_state(fids, V if idle_v else state, currents, idle_v=idle_v)
    return {"landed": list(fids)}


real_prep = S.prep_filaments
S.prep_filaments = fake_prep
S._safety_schedule_running = lambda: (False, None)
real_ftb = S.filament_to_board
S.filament_to_board = lambda f: (0, 0, int(f) % 8, None)
S.CONTROLLERS.clear()
S.CONTROLLERS[1] = FakeLink()
S._safety_record = lambda kind, detail: calls.append(("event", kind, detail))
threading.Thread(target=S._active_deadline_loop, daemon=True).start()

S.note_power_state([10], V, {10: 7600})
S.set_active_deadlines([10], 0.3, {10: S.IdleMv(2350)})
check("timed snapshot says then_idle_mv", (S.safety_snapshot()["timed_active"].get("10") or {})
      .get("then_idle_mv") == 2350, S.safety_snapshot()["timed_active"])
time.sleep(0.7)
back = [c for c in calls if c[0] == IDLE]
check("timed VOLTAGE ran out -> voltage IDLE at its mV", back and back[0][1] == [10]
      and back[0][2] == {10: 2350} and back[0][3] is True, calls)
check("...recorded as a voltage IDLE", S.is_idle(10) and S.IDLE_VOLT_NOW.get(10) == 2350)

# Mixed returns on one controller: one CC frame, one voltage frame.
calls.clear()
S.note_power_state([11, 12], ACTIVE, {11: 2600, 12: 2600})
S.set_active_deadlines([11], 0.2, {11: 1200})
S.set_active_deadlines([12], 0.2, {12: S.IdleMv(2450)})
time.sleep(0.6)
modes = sorted((c[3], tuple(c[1])) for c in calls if c[0] == IDLE)
check("CC and voltage returns sent apart", modes == [(False, (11,)), (True, (12,))], calls)

# The HV guard / vacuum trip: hot ones back to THEIR IDLE, a voltage IDLE left alone.
calls.clear()
S.note_power_state([13], V, {13: 2400}, idle_v=True)
S.note_power_state([14], V, {14: 2400}, idle_v=True)
S.note_power_state([14], V, {14: 7700})
to_idle, no_idle = S.drop_hot_to_idle(time.monotonic())
check("drop_hot_to_idle: a voltage IDLE is not hot", 13 not in to_idle and 14 in to_idle,
      (to_idle, no_idle))
time.sleep(0.4)
check("...the hot one back to its voltage IDLE", any(c[0] == IDLE and c[1] == [14]
                                                       and c[3] is True and c[2] == {14: 2400}
                                                       for c in calls), calls)

# --- prep_filaments: the real one, with a link that applies what it is sent -------
S.prep_filaments = real_prep
S.filament_to_board = real_ftb
sent = []


class EchoClient:
    connected = True

    def send_request(self, ft, payload, flags=0, timeout=3.0):
        sent.append((payload[8], int.from_bytes(payload[9:11], "little"), bytes(payload[:8])))
        return {"status": "OK", "raw": [0] + list(payload[:8]) + list(payload[:8]) + [0] * 8}


class EchoLink:
    client = EchoClient()


ctl0 = [f for f in range(96) if (S.filament_to_board(f) or (None,))[0] == 0][:4]
a, b, c, d = ctl0
S.IDLE_MV_TABLE.clear()
S.IDLE_MV_TABLE.update({a: {"mV": 2400}, b: {"mV": 2600}, d: {"mV": 5000}})
r = S.prep_filaments(EchoLink(), 0, IDLE, [a, b, c, d], idle_v=True)
check("batch voltage IDLE: sent as VOLTAGE(6), one frame per voltage",
      sorted((st, mv) for st, mv, _m in sent) == [(6, 2400), (6, 2600)], sent)
check("...no IDLE voltage known / over the ceiling: refused and named",
      sorted(r["ladder_blocked"]) == sorted([c, d]) and "no IDLE voltage" in r["ladder_reasons"][str(c)]
      and "ceiling" in r["ladder_reasons"][str(d)] and not r["ok"], r)
check("...reported as IDLE in voltage mode with each one's mV",
      r["state"] == IDLE and r["idle_voltage"] and r["idle_mv"] == {str(a): 2400, str(b): 2600}, r)
check("...recorded as voltage IDLEs", S.IDLE_VOLT_NOW.get(a) == 2400 and S.IDLE_VOLT_NOW.get(b) == 2600
      and S.LAST_POWER_STATE[a][0] == V)
sent.clear()
r = S.prep_filaments(EchoLink(), 0, IDLE, [a], currents={a: 1200})
check("plain IDLE (no idle_v) unchanged: IDLE(4) at mA, voltage IDLE cleared",
      sent and sent[0][0] == 4 and sent[0][1] == 1200 and a not in S.IDLE_VOLT_NOW and S.is_idle(a), sent)

# --- learning the IDLE voltage from the CC IDLE -------------------------------------
reads = iter(range(1000))


def fake_rows(cid):
    k = next(reads)
    rows = []
    for f in (a, b, c):
        _c0, ch, pos, _ = S.filament_to_board(f)
        mv = {a: 2400 + (k % 3) * 10, b: 2600, c: 2500}[f]
        ma = {a: 1185, b: 1190 if k % 2 else 400, c: 1180}[f]   # b: half the reads far off
        rows.append({"channel": ch, "mux_port": pos, "power_state": 4, "bus_mV": mv,
                     "current_mA": ma})
    return rows, {"fresh": True}


S.monitor_board_rows = fake_rows
S.IDLE_LEARN_GAP_S = 0.0
S.CONTROLLERS.clear()
S.CONTROLLERS[1] = EchoLink()
S.note_power_state([a, b], IDLE, {a: 1200, b: 1200})
S.note_power_state([c], S.POWER_STATE_STANDBY, {c: 0})
S.IDLE_MV_TABLE.clear()
r = S.idle_voltage_learn([a, b, c])
check("learned from the reads at its CC IDLE: the median mV",
      r["learned"].get(str(a), {}).get("mV") == 2410 and r["learned"][str(a)]["at_mA"] == 1200, r)
check("half the reads off target: not learned, says why",
      str(b) in r["skipped"] and "within" in r["skipped"][str(b)], r)
check("not at a CC IDLE: not learned", "not at a CC IDLE" in r["skipped"].get(str(c), ""), r)
# After a backend restart nothing was commanded here: the firmware's own target.
S.LAST_POWER_STATE.pop(a, None)
S.LAST_IDLE_MA.pop(a, None)
S.read_cached_telemetry = lambda link, c0: {a: {"cc_mode": 1, "target_mA": 1200}}
r2 = S.idle_voltage_learn([a])
check("after a restart: learned against the firmware's CC target",
      r2["learned"].get(str(a), {}).get("at_mA") == 1200, r2)
S.read_cached_telemetry = lambda link, c0: {}
r2 = S.idle_voltage_learn([a])
check("...and with no target anywhere: skipped, not guessed",
      "no CC IDLE target" in r2["skipped"].get(str(a), ""), r2)
S.note_power_state([a, b], IDLE, {a: 1200, b: 1200})
check("saved and loaded back", S.IDLE_MV_STATE_PATH.exists() and (S.IDLE_MV_TABLE.clear() or True)
      and (S._idle_mv_load() or True) and S.IDLE_MV_TABLE.get(a, {}).get("mV") == 2410)
r = S.idle_voltage_set({a: 9000})
check("set by hand over the ceiling: refused, nothing changed",
      not r["ok"] and S.IDLE_MV_TABLE[a]["mV"] == 2410, r)
r = S.idle_voltage_set({a: 2450, b: 2550})
check("set by hand", r["ok"] and S.IDLE_MV_TABLE[a]["mV"] == 2450 and S.IDLE_MV_TABLE[b]["mV"] == 2550)
r = S.idle_voltage_set({b: None})
check("None removes one", r["ok"] and b not in S.IDLE_MV_TABLE)

# --- a backend restart still knows which VOLTAGE is an IDLE ------------------------
import json  # noqa: E402
S.note_power_state([a], V, {a: 2450}, idle_v=True)
saved = json.loads(S.IDLE_MV_STATE_PATH.read_text())
check("voltage IDLE saved the moment it lands", saved["at_voltage_idle"].get(str(a)) == 2450, saved)
S.note_power_state([b], V, {b: 2550}, idle_v=True)      # b was a voltage IDLE too...
# "restart": memory gone, the firmware still says VOLTAGE for a, IDLE for b.
S.LAST_POWER_STATE.clear(); S.IDLE_VOLT_NOW.clear(); S.IDLE_SINCE.clear()
S._idle_mv_load()
check("restored from the file", S.IDLE_VOLT_NOW.get(a) == 2450 and S.IDLE_VOLT_NOW.get(b) == 2550,
      S.IDLE_VOLT_NOW)
_, cha, pa, _ = S.filament_to_board(a)
_, chb, pb, _ = S.filament_to_board(b)
S._adopt_firmware_power_states(0, {(cha, pa): {"known": True, "power_state": V},
                                   (chb, pb): {"known": True, "power_state": IDLE}},
                               time.monotonic())
check("adopted VOLTAGE that was saved as a voltage IDLE: an IDLE, not hot",
      S.is_idle(a) and not S.is_hot(a) and a in S.IDLE_SINCE)
check("...one the firmware now reports at CC IDLE: the claim is dropped (and saved)",
      b not in S.IDLE_VOLT_NOW and S.is_idle(b)
      and str(b) not in json.loads(S.IDLE_MV_STATE_PATH.read_text())["at_voltage_idle"])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
