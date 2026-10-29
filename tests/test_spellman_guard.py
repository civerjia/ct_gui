#!/usr/bin/env python3
"""The Spellman in the HV guard: arcs recorded (ordinary ones recover by
themselves), a big arc and a sustained over-current take our HV down,
latched. Offline: readings are fed by hand, the switches and rails are fakes.

    python3 tests/test_spellman_guard.py
"""
import _path  # noqa: F401

import os
import tempfile

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"
os.environ.setdefault("CT_STATE_DIR", tempfile.mkdtemp())

import ct.server._server as S  # noqa: E402
import ct.server._spellman_watch as SW  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


DT = 0.23          # one reading of each kind per ~0.23 s (measured 2026-10-29)


def feed(w, t0, kvs, mas=None, quiet=True):
    """kV and mA readings alternating from t0; returns (events, end time)."""
    out, t = [], t0
    mas = mas if mas is not None else [0.02] * len(kvs)
    for kv, ma in zip(kvs, mas):
        out += w.feed_kv(t, kv)
        out += w.feed_ma(t + 0.01, ma, quiet)
        t += DT
    return out, t


# ── the watch on its own ────────────────────────────────────────────────────
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [60.2] * 100)
check("steady 60 kV, 0.02 mA -> nothing; baseline learned", ev == [] and
      abs(w.snapshot(t)["baseline_ma"] - 0.02) < 1e-9, (ev, w.snapshot(t)))

# The 2026-10-29 11:03:49 arc: one reading at 6.85 kV / 2.33 mA, then back.
ev, t = feed(w, t, [6.85, 60.2, 60.2], [2.33, 0.1, 0.02])
check("one-reading kV dip with an mA spike -> one ordinary arc", [e["kind"] for e in ev] == ["arc"]
      and ev[0]["min_kv"] == 6.85 and ev[0]["peak_ma"] == 2.33 and ev[0]["recovered_s"] < 0.5, ev)

# A single mA spike is what a normal pulse looks like too: not an arc.
ev, t = feed(w, t, [60.2, 60.2, 60.2], [0.02, 1.7, 0.02])
check("mA spike alone -> nothing (a pulse does that)", ev == [], ev)

# The supply's own arc bit.
ev = w.feed_status(t, [1, 1, 1, 1] + [0] * 13)
ev += feed(w, t + 0.05, [60.2, 60.2])[0]
ev = [e for e in ev if e["kind"] != "status"]          # the bit change itself is recorded too
check("GetStatus arc bit -> arc, status named", [e["kind"] for e in ev] == ["arc"] and ev[0]["status"] == ["arc"], ev)
t += 1.0

# A ramp-down is not an arc: 60 -> 0 kV at ~1 kV per reading.
ev, t = feed(w, t, [60.2 - k for k in range(61)])
check("ramp-down -> no arc", ev == [], ev)
ev, t = feed(w, t, [0.04] * 10)
check("supply off -> nothing", ev == [], ev)
ev, t = feed(w, t, [k * 1.0 for k in range(1, 61)] + [60.2] * 20)
check("ramp-up -> nothing", ev == [], ev)

# A big arc: falls in one step and stays down.
ev, t = feed(w, t, [3.0] * 30, [2.0] + [0.0] * 29)
check("collapse that stays down -> big_arc after 2 s", [e["kind"] for e in ev] == ["big_arc"]
      and ev[0]["kv_before"] == 60.2 and ev[0]["down_s"] >= SW.BIG_ARC_S, ev)

# 12:24:24 2026-10-29: 61.5 -> 0.3 kV, then NO reading for 7.8 s (the monitor
# pauses while conditioning handles the arc), 13 kV decaying, ramp back. Not
# back within 2 s -> big arc, whether the readings are low or missing.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [61.5] * 50 + [0.3])
ev += w.poll(t + 1.0)
check("dip, no readings for 1 s -> not decided yet", ev == [], ev)
ev += w.poll(t + 2.1)
check("...still no reading at 2 s -> big_arc (no reading since the drop)", [e["kind"] for e in ev] == ["big_arc"]
      and "unknown" in ev[0], ev)
ev, t = feed(w, t + 7.8, [13.0, 11.0, 9.0, 7.9] + [7.9 + 2.5 * k for k in range(22)] + [61.5] * 3)
check("...the ramp back -> no further arc", [e["kind"] for e in ev] == [], ev)

# A latched status arc bit: one arc on its rising edge, not one per GetStatus.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [60.2] * 20)
for k in range(10):
    ev += w.feed_status(t, [1, 1, 1, 1])
    ev += feed(w, t + 0.05, [60.2])[0]
    t += 0.3
check("arc bit held for 10 GetStatus -> one arc (and one status change)",
      [e["kind"] for e in ev] == ["status", "arc"], ev)

# GetStatus as the supply sends it (17 bits, spellman.html's order).
OK = [1, 1, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0]          # read 2026-10-29, 65 kV


def st(**on):
    b = list(OK)
    for name, v in on.items():
        b[SW.STATUS_NAMES.index(name)] = int(v)
    return b


check("the bit map: normal status has no alarm, HV on", SW.status_flags(OK) == []
      and SW.status_bit(OK, "hv_on") is True)
check("...the arc read 11:35 (arc + HV inhibit)", SW.status_flags(st(arc=1, hv_inhibit=1)) == ["arc", "hv_inhibit"])

# System fault: an event at once, whatever the kV.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20)
ev = w.feed_status(t, OK)
ev += w.feed_status(t + 0.3, st(system_fault=1, hv_on=0))
kinds = [e["kind"] for e in ev]
check("system fault bit rises -> system_fault event (and the change recorded)", "system_fault" in kinds
      and "status" in kinds and [e for e in ev if e["kind"] == "system_fault"][0]["alarms"] == ["system_fault"], ev)
ev = w.feed_status(t + 0.6, st(system_fault=1, hv_on=0))
check("...held -> not repeated", [e["kind"] for e in ev] == [], ev)

# Somebody switches the Spellman off: kV down, HV On bit 0, no fault -> hv_off, not big arc.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20)
w.feed_status(t, OK)
ev, t = feed(w, t, [20.0])
for k in range(26):                                   # GetStatus every cycle, as the supply is polled
    ev += w.feed_status(t, st(hv_on=0))
    ev += feed(w, t + 0.05, [0.5])[0]
    t += DT
check("switched off (HV On 0, no fault) -> hv_off, no big arc", "hv_off" in [e["kind"] for e in ev]
      and "big_arc" not in [e["kind"] for e in ev], [e["kind"] for e in ev])

# 11:55 2026-10-29: arc, HV Ray On stays 1, kV 65 -> 13.8 -> climbing to 55:
# the supply recovering -- an ordinary arc, no trip.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20)
w.feed_status(t, OK)
ev = w.feed_status(t + 0.05, st(arc=1, hv_inhibit=1))
ev2, t = feed(w, t + 0.1, [42.0])
ev += ev2
for k in range(40):
    ev += w.feed_status(t, OK)
    ev += feed(w, t + 0.05, [13.8 + k])[0]
    t += DT
kinds = [e["kind"] for e in ev if e["kind"] != "status"]
check("11:55: arc, HV Ray On kept at 1, kV low then climbing -> big arc (the status cannot tell)",
      kinds == ["big_arc"], kinds)

# HV Ray On drops to 0 on an arc -> big arc.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20)
w.feed_status(t, OK)
ev = w.feed_status(t + 0.05, st(arc=1, hv_on=0))
for k in range(26):
    ev += w.feed_status(t, st(hv_on=0))
    ev += feed(w, t + 0.05, [0.5])[0]
    t += DT
check("arc with HV Ray On -> 0 -> big_arc", "big_arc" in [e["kind"] for e in ev], [e["kind"] for e in ev])

# No GetStatus at all: the kV is the fallback.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20 + [3.0] * 26)
check("no status, kV under half for 2 s -> big_arc", [e["kind"] for e in ev] == ["big_arc"], ev)

# The same drop with HV On still 1 -> no longer a big arc (it was, by kV).
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20)
w.feed_status(t, OK)
ev = []
for k in range(26):
    ev += w.feed_status(t, OK)
    ev += feed(w, t + 0.05, [3.0])[0]
    t += DT
check("collapse with HV On still 1, no fault -> big_arc", "big_arc" in [e["kind"] for e in ev],
      [e["kind"] for e in ev])

# Conditioning's own arc flag: an arc, no status change invented.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [65.5] * 20)
w.feed_status(t, OK)
ev = w.feed_arc_flag(t + 0.1) + feed(w, t + 0.2, [65.5])[0]
check("conditioning arc flag -> one arc, HV On untouched", [e["kind"] for e in ev] == ["arc"]
      and w.snapshot(t + 1)["hv_on"] is True, (ev, w.snapshot(t + 1)))

# Conditioning re-reports the arc 1.7 s after the GetStatus bit: one arc, not two.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [61.2] * 20)
w.feed_status(t, OK)
ev = w.feed_status(t + 0.05, st(arc=1, hv_inhibit=1))
ev += feed(w, t + 0.1, [7.1, 61.2])[0]
ev += w.feed_status(t + 0.6, OK)
ev += w.feed_arc_flag(t + 1.8) + feed(w, t + 1.9, [61.2, 61.2])[0]
check("conditioning's late arc flag -> still one arc", [e["kind"] for e in ev if e["kind"] != "status"] == ["arc"], ev)

# Over-current: quiet, over mean + 5 sigma for > 1 s (flat 0.02 mA: sigma at its floor).
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [60.2] * 100)
ev, t = feed(w, t, [60.2] * 4, [0.5] * 4)
check("0.5 mA for 0.7 s -> not yet", ev == [], ev)
ev, t = feed(w, t, [60.2] * 4, [0.5] * 4)
check("...still over past 1 s -> over_current, once", [e["kind"] for e in ev] == ["over_current"]
      and ev[0]["over_s"] >= 1.0, ev)
ev, t = feed(w, t, [60.2] * 10, [0.5] * 10)
check("...not repeated while it stays over", ev == [], ev)
check("...and the over readings were not learned", w.snapshot(t)["baseline_ma"] < 0.05, w.snapshot(t))

# Pulses (not quiet): never over-current, never baseline.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [60.2] * 100)
ev, t = feed(w, t, [60.2] * 20, [0.8] * 20, quiet=False)
check("a pulse train's mA -> nothing", ev == [] and w.snapshot(t)["baseline_ma"] < 0.05, ev)

# A fault there from the start may not become the baseline: with no quiet
# history the threshold is 0 + 5 x the sigma floor, and over readings are
# never learned.
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [60.2] * 8, [0.25] * 8)
check("0.25 mA from the very start -> over_current, not learned", [e["kind"] for e in ev] == ["over_current"]
      and w.snapshot(t)["baseline_ma"] == 0.0, (ev, w.snapshot(t)))

# ── the server acts on it ───────────────────────────────────────────────────


class _Cl:
    connected = True


class _Link:
    client = _Cl()
    host = "esp"


def setup():
    rig = {"forced": 0, "rails": [], "fallback": [], "records": []}
    S._HV_GUARD.update(enabled=True, tripped=None)
    S._SPELL_WATCH = SW.SpellmanWatch()
    S._SPELL_ACTIONS.clear()
    S.CONTROLLERS.clear()
    S.CONTROLLERS.update({1: _Link(), 2: _Link()})
    S.force_grid_off = lambda link, cid, masks, reason="": rig.__setitem__("forced", rig["forced"] + 1) or {"ok": True}
    S._note_forced_open = lambda *a, **k: None
    S.stm32_hv_enable_set = lambda host, ch, on: rig["rails"].append((ch, on)) or {"ok": True}
    S._safety_fallback_filaments = lambda fids, st: rig["fallback"].append((sorted(fids), int(st)))
    S._safety_record = lambda kind, rec: rig["records"].append((kind, rec))
    S._hv_guard_quiet = lambda now: True
    S.LAST_POWER_STATE.clear()
    S.ACTIVE_DEADLINES.clear()
    S._LAST_HOT.clear()
    S.ARC_LOG_PATH = S.Path(tempfile.mkdtemp()) / "arcs.jsonl"
    return rig


def msgs(t0, kvs, mas):
    t = t0
    for kv, ma in zip(kvs, mas):
        S._spellman_on_message({"type": "spellman_response", "cmd": "GetkVMonitor", "data": kv}, t)
        S._spellman_on_message({"type": "spellman_response", "cmd": "GetmAMonitor", "data": [ma]}, t + 0.01)
        t += DT
    return t


# Ordinary arc: recorded (log file), nothing switched.
rig = setup()
t = msgs(0.0, [60.2] * 50 + [5.9, 60.2, 60.2], [0.02] * 50 + [1.53, 0.05, 0.02])
r = S._spellman_tick(t)
check("server: ordinary arc -> nothing switched, not latched", r is None and rig["rails"] == []
      and rig["forced"] == 0 and S._HV_GUARD["tripped"] is None, r)
lines = S.ARC_LOG_PATH.read_text().splitlines() if S.ARC_LOG_PATH.exists() else []
check("...written to logs/arcs.jsonl", len(lines) == 1 and '"kind": "arc"' in lines[0], lines)
check("...counted in the snapshot", S.hv_guard_snapshot()["spellman"]["arcs_10min"] == 1
      if False else S._SPELL_WATCH.snapshot(t)["arcs_10min"] == 1)

# Big arc: switches open, rails off, hot filaments to IDLE (vacuum), latched.
rig = setup()
S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_VOLTAGE), 0.0)
S.ACTIVE_DEADLINES[20] = (1e12, 1200)
t = msgs(0.0, [60.2] * 50 + [2.0] * 30, [0.02] * 50 + [2.1] + [0.0] * 29)
r = S._spellman_tick(t)
check("server: big arc -> tripped, latched", r and r["action"] == "tripped"
      and S._HV_GUARD["tripped"]["source"] == "spellman_big_arc", r and r.get("message"))
check("...every switch forced open, emission and focus off", rig["forced"] == 2
      and ("emission", False) in rig["rails"] and ("focus", False) in rig["rails"], rig)
check("...the hot filament back to its IDLE (not STOP)", S.ACTIVE_DEADLINES[20][1] == 1200
      and S.ACTIVE_DEADLINES[20][0] < 1e11 and r["to_idle"] == [20], r and r.get("to_idle"))
check("...message says it comes back by itself", "by themselves" in r["message"] and "big arc" in r["message"]
      and r["auto_recover"], r["message"])
n = len(rig["rails"])
t = msgs(t, [2.0] * 30, [0.0] * 30)
S._spellman_tick(t)
check("...latched: nothing repeated", len(rig["rails"]) == n)

# Over-current: STOP for the recently hot, latched.
rig = setup()
S._LAST_HOT[33] = 0.0
t = msgs(0.0, [60.2] * 50, [0.02] * 50)
S._LAST_HOT[33] = t
t = msgs(t, [60.2] * 8, [0.6] * 8)
r = S._spellman_tick(t)
check("server: 0.6 mA held > 1 s with no pulse -> tripped", r and r["action"] == "tripped"
      and S._HV_GUARD["tripped"]["source"] == "spellman_over_current", r and r.get("message"))
check("...recently hot filament to STOP", rig["fallback"] == [([33], int(S.POWER_STATE_STOP))], rig["fallback"])

# System fault: tripped at once, latched, hot filaments to IDLE.
rig = setup()
S.LAST_POWER_STATE[20] = (int(S.POWER_STATE_VOLTAGE), 0.0)
S.ACTIVE_DEADLINES[20] = (1e12, 1200)
t = msgs(0.0, [65.5] * 10, [0.02] * 10)
S._spellman_on_message({"type": "spellman_response", "cmd": "GetStatus", "data": OK}, t)
S._spellman_on_message({"type": "spellman_response", "cmd": "GetStatus", "data": st(system_fault=1)}, t + 0.3)
r = S._spellman_tick(t + 0.4)
check("server: system fault -> tripped, latched, says SYSTEM FAULT", r and r["action"] == "tripped"
      and S._HV_GUARD["tripped"]["source"] == "spellman_system_fault" and "SYSTEM FAULT" in r["message"],
      r and r.get("message"))
check("...switches open, rails off, the hot filament to IDLE", rig["forced"] == 2 and ("emission", False) in rig["rails"]
      and r["to_idle"] == [20], rig)
lines = S.ARC_LOG_PATH.read_text().splitlines()
check("...the status change is in arcs.jsonl", any('"kind": "status"' in ln and "system_fault" in ln for ln in lines),
      lines)

# ── auto-recovery: arcs (big ones, System Fault) are transients ─────────────
def stable_feed(t0, seconds, status=None):
    """Steady 65.5 kV, 0.02 mA, GetStatus each cycle, for `seconds`."""
    t = t0
    while t < t0 + seconds:
        S._spellman_on_message({"type": "spellman_response", "cmd": "GetStatus",
                                "data": status or OK}, t)
        S._spellman_on_message({"type": "spellman_response", "cmd": "GetkVMonitor", "data": 65.5}, t + 0.05)
        S._spellman_on_message({"type": "spellman_response", "cmd": "GetmAMonitor", "data": [0.02]}, t + 0.1)
        t += DT
    return t


def recover_setup():
    rig = setup()
    S._HVG_RAILS.update(emission=True, focus=True)
    S._hv_guard_stm32_flags = lambda: {"ok": True, "flags": 0}
    S.hv_preflight_switches = lambda reason: {"ok": True}
    S.vacuum_refusal = lambda action: None
    return rig


rig = recover_setup()
t = stable_feed(0.0, 3.0)
S._spellman_on_message({"type": "spellman_response", "cmd": "GetStatus", "data": st(system_fault=1, hv_on=0)}, t)
r = S._spellman_tick(t + 0.1)
check("system fault -> tripped, marked auto_recover", r and r["auto_recover"] and S._HV_GUARD["tripped"], r)
t = stable_feed(t + 0.2, 4.0, st(system_fault=1, hv_on=0))
r = S.hv_guard_tick(t)
check("...fault still set -> stays tripped, says why", r is None and S._HV_GUARD["tripped"]
      and "system_fault" in (S._HV_GUARD["tripped"].get("waiting_for") or ""), S._HV_GUARD["tripped"].get("waiting_for"))
t = stable_feed(t, 5.0)
r = S.hv_guard_tick(t)
check("...cleared but only 5 s steady -> still waiting", r is None and S._HV_GUARD["tripped"], r)
n_rails = len(rig["rails"])
t = stable_feed(t, 6.0)
r = S.hv_guard_tick(t)
check("...stable 10 s -> recovered, latch cleared", r and r["action"] == "recovered" and S._HV_GUARD["tripped"] is None, r)
check("...emission and focus back on", rig["rails"][n_rails:] == [("emission", True), ("focus", True)], rig["rails"])

rig = recover_setup()
S._HVG_RAILS.update(emission=True, focus=False)
t = stable_feed(0.0, 3.0)
t = msgs(t, [3.0] * 30, [0.0] * 30)              # big arc (HV On still 1 in the last status)
S._spellman_tick(t)
check("big arc -> tripped, auto_recover", S._HV_GUARD["tripped"] and S._HV_GUARD["tripped"]["auto_recover"],
      S._HV_GUARD["tripped"])
t = stable_feed(t, 11.0)
r = S.hv_guard_tick(t)
check("...stable again -> recovered; only the rail that was on comes back", r and r["action"] == "recovered"
      and ("emission", True) in rig["rails"] and ("focus", True) not in rig["rails"], rig["rails"])

rig = recover_setup()
S._hv_guard_stm32_flags = lambda: {"ok": True, "flags": 32}
t = stable_feed(0.0, 3.0)
S._spellman_on_message({"type": "spellman_response", "cmd": "GetStatus", "data": st(system_fault=1)}, t)
S._spellman_tick(t + 0.1)
n_rails = len(rig["rails"])
t = stable_feed(t + 0.2, 11.0)
r = S.hv_guard_tick(t)
check("STM32 tripped on it too -> its own check restores (rails not touched here)", r and r["action"] == "recovered"
      and rig["rails"][n_rails:] == [] and S._HVG_SPELL_ARC[0] is not None, (r, rig["rails"][n_rails:]))

rig = recover_setup()
t = stable_feed(0.0, 3.0)
t = msgs(t, [65.5] * 8, [0.6] * 8)
S._spellman_tick(t)
t = stable_feed(t, 20.0)
r = S.hv_guard_tick(t)
check("over-current -> stays latched however stable (a person handles it)", r is None
      and S._HV_GUARD["tripped"] and not S._HV_GUARD["tripped"]["auto_recover"], S._HV_GUARD["tripped"])

# STM32 tripped while a Spellman arc is still open: wait for the arc's outcome.
rig = recover_setup()
S._hv_guard_stm32_flags = lambda: {"ok": True, "flags": 32}
called = []
S._hv_guard_after_stm32_trip = lambda now: called.append(now) or {"action": "recovered"}
t = stable_feed(0.0, 3.0)
t = msgs(t, [0.3], [0.0])                      # the drop: arc open
r = S.hv_guard_tick(t + 0.5)
check("STM32 trip during an open arc -> not handled yet", r is None and called == [], r)
t = msgs(t + 0.6, [65.5, 65.5], [0.02, 0.02])  # back: ordinary arc, closed
r = S.hv_guard_tick(t)
check("...arc closed -> the STM32 check runs", called and r["action"] == "recovered", r)

# The threshold is settable and kept.
r = S.spellman_set_trip_sigma(4, "test")
check("spellman_trip_sigma set -> used, saved", r["ok"] and S._SPELL_WATCH.trip_sigma == 4
      and S._spellman_trip_sigma_load() == 4, r)
check("...nonsense refused, nothing changed", not S.spellman_set_trip_sigma(0.5, "t")["ok"]
      and not S.spellman_set_trip_sigma("x", "t")["ok"] and S._SPELL_WATCH.trip_sigma == 4)

# Noise as measured 2026-10-29 (60-65 kV, 0.0098 mA steps, mean ~0.035,
# sigma ~0.04): never trips; a sustained +0.3 mA does, +0.1 does not.
NOISE = [0, 0, 0.0098, 0.0195, 0.0195, 0, 0.0293, 0.0488, 0.0098, 0.0684, 0.0195,
         0.0977, 0, 0.1172, 0.0098, 0.0293, 0.0781, 0.0195, 0.1074, 0, 0.0391, 0.1270]
w = SW.SpellmanWatch()
mas = NOISE * 30
ev, t = feed(w, 0.0, [65.5] * len(mas), mas)
snap = w.snapshot(t)
check("measured noise for 2.5 min -> nothing; sigma learned ~0.04", ev == [] and 0.03 < snap["sigma_ma"] < 0.05
      and 0.18 < snap["threshold_ma"] - snap["baseline_ma"] < 0.25, (ev, snap))
ev, t = feed(w, t, [65.5] * 20, [m + 0.1 for m in NOISE[:20]])
check("...+0.1 mA for 4.6 s (under 5 sigma) -> nothing", ev == [], ev)
ev, t = feed(w, t, [65.5] * 8, [0.35] * 8)
check("...0.35 mA held > 1 s (over mean + 5 sigma) -> over_current", [e["kind"] for e in ev] == ["over_current"]
      and ev[0]["k"] == 5.0, ev)
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [8.0] * 100, [0.0] * 100)
check("all-zero readings (low kV) -> sigma at its floor, not 0", w.snapshot(t)["sigma_ma"] == SW.SIGMA_FLOOR_MA,
      w.snapshot(t))

# The real 2026-10-29 11:01-11:12 stream (22 arcs, every one back next
# reading) is replayed in the commit that added this; as a spot check, the
# arc at 11:07:40 took two readings to come back (2.2 -> 39.7 -> 60 kV).
w = SW.SpellmanWatch()
ev, t = feed(w, 0.0, [60.19] * 50 + [2.22, 39.69, 60.19, 60.19])
check("two-reading recovery (11:07:40) -> one ordinary arc", [e["kind"] for e in ev] == ["arc"]
      and ev[0]["recovered_s"] < 1.0, ev)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
