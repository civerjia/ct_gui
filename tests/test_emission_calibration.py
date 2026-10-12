#!/usr/bin/env python3
"""CTClient.emission_calibration: the GUI's test 5, run in the backend.

2026-10-11: the GUI's emission calibration was its own old JS loop (direct
DS3502 wiper, HV_PULSE, the ring detector, a blind 200 ms settle, ACTIVE held
across levels, filaments left at SLEEP). What it does now, as the user set it:
hold ACTIVE at the START current until stable, command ACTIVE END ONCE and fire the
pulse train straight through the climb -- no steps, nothing stopping in
between; every pulse paired with the heating current at that pulse. Never
refuse a starting condition (emission, focus on/off + V, filament state) --
set it; never cool an already-warm filament; IDLE before any read-back; STOP
only on a named error. Offline: every hardware call is a recording fake.

    python3 tests/test_emission_calibration.py
"""
import _path  # noqa: F401

import contextlib
import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402
from ct.client._base import STOP, SLEEP, STANDBY, IDLE, ACTIVE  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Rig:
    """state: {filament: power state}. start: {filament: "ok" | "slow" |
    "fault" | "short"} -- what verify reports when the START current is set.
    board_fault: boards reporting a fault after the ramp. lose: events
    dropped (unpaired). raise_at: filament whose END command raises."""

    def __init__(self, state=None, start=None, hv_on=False, raise_at=None, dead=(),
                 board_fault=(), lose=0, focus_reads=None):
        self.state = dict(state or {})
        self.start = dict(start or {})
        self.hv = {"emission_on": hv_on, "focus_on": False}
        self.raise_at, self.dead, self.board_fault = raise_at, set(dead), set(board_fault)
        self.lose, self.focus_reads = lose, focus_reads
        self.calls, self.shots = [], []

    def heating(self, f, ma):
        v = self.start.get(f, "ok")
        if v == "ok":
            return {"ok": True, "measured_ma": ma - 5, "arrival": "settled"}
        if v == "slow":
            return {"ok": False, "measured_ma": ma - 400, "arrival": "ramping"}
        if v == "fault":
            return {"ok": False, "faulted": True, "measured_ma": 3, "error": "cc_mode 2 open"}
        return {"ok": False, "cannot_start": True, "measured_ma": 1, "error": "struggling"}

    def install(self, ct):
        rig, C = self, self.calls
        ct._live_user_indices = lambda: sorted(rig.state)
        ct._is_dead = lambda f: int(f) in rig.dead
        ct._mosfet_targets = lambda fs: ([int(f) for f in fs if int(f) not in rig.dead], [])
        ct.hv_status = lambda: {"ok": True, **rig.hv}
        ct.set_emission_i = lambda ma: (C.append(("limit", ma)), {"ok": True})[1]
        ct.set_emission_v = lambda v: (C.append(("emission_v", v)), {"ok": True})[1]

        def enable_emission(on):
            rig.hv["emission_on"] = on
            C.append(("emission", on))
            return {"ok": True}

        def enable_focus(on):
            rig.hv["focus_on"] = on
            C.append(("focus", on))
            return {"ok": True}
        ct.enable_emission, ct.enable_focus = enable_emission, enable_focus
        ct.set_focus_v = lambda v: (C.append(("focus_v", v)), {"ok": True})[1]
        ct.read_focus_v = lambda: rig.focus_reads
        ct._mosfet_rail = lambda v, timeout_s=3.0: (-abs(v), None)
        ct._resolve_pedestal = lambda *a: (2.0, {"source": "diode_formula", "ma": 2.0})
        ct.read_board_status = lambda f: {"ok": True, "state": rig.state[f],
                                          "fault": 1 if f in rig.board_fault else 0,
                                          "fault_name": "open" if f in rig.board_fault else "none"}

        def state_one(f, st, arg, why, *a):
            C.append(("state", f, st.name))
            rig.state[f] = int(st)
            return {"ok": True}
        ct._state_one = state_one

        def idle_one(f, ma, verify=False, timeout_s=5.0, **k):
            C.append(("idle", f, ma, verify))
            rig.state[f] = IDLE
            return {"ok": True, **({"heating": rig.heating(f, ma)} if verify else {})}
        ct.idle_one = idle_one

        def stop_one(f, **k):
            C.append(("STOP", f))
            rig.state[f] = STOP
            return {"ok": True}
        ct.stop_one = stop_one

        def active_one(f, ma, verify=False, **k):
            C.append(("active", f, ma, verify))
            if rig.raise_at == f and not verify:
                raise RuntimeError("link died")
            rig.state[f] = ACTIVE
            return {"ok": True, **({"heating": rig.heating(f, ma)} if verify else {})}
        ct.active_one = active_one

        def shot_prepare(f, **k):
            C.append(("arm", f))
            return {"ok": True, "f": f, "k": k, "since": 0, "measuring": True}
        ct.shot_prepare = shot_prepare
        ct.shot_measure_arm = lambda shot, **k: shot
        ct.shot_arm = lambda shot: shot

        def shot_trigger(shot):
            C.append(("train", shot["f"]))
            rig.shots.append(shot)
            return shot
        ct.shot_trigger = shot_trigger
        ct.shot_wait = lambda shot, timeout_s=15.0: shot
        n_of = lambda shot: shot["k"]["num_pulses"]
        ct.shot_records = lambda shot: (C.append(("read", shot["f"])), {"ok": True, "fired": n_of(shot)})[1]

        def shot_measured(shot, res, prefetched=None):
            C.append(("events", shot["f"], prefetched is not None))
            return {**res, "ref_mv": 1200, "measured": [
                {"id": i + 1, "plateau_net_ma": 2.0 + i * 0.5, "integral_mams": 3.0,
                 "on_us": 1000, "bg_ma": 0.1, "bg_sigma4": 0}
                for i in range(n_of(shot) - rig.lose)]}
        ct.shot_measured = shot_measured
        ct.shot_abort = lambda shot: None
        ct._firing_controller = lambda f, c: 1
        # the firmware's per-pulse heating snapshot: climbing 1800 -> 2500
        ct.shv_pulse_log = lambda c: [
            {"filament": rig.shots[-1]["f"], "seq": i, "heat_target_mA": 2500,
             "heat_meas_mA": min(2500, 1800 + 100 * i)}
            for i in range(rig.shots[-1]["k"]["num_pulses"])]
        ct.save_emission_curves = lambda name, res, params: (
            C.append(("save", sorted(res))), {"ok": True, "json": "x.json"})[1]


def run(rig, **kw):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    rig.install(ct)
    return ct.emission_calibration(**kw)


def of(calls, kind, f=None):
    return [c for c in calls if c[0] == kind and (f is None or c[1] == f)]


def seq(calls, f):
    return [c[0] for c in calls if len(c) > 1 and c[1] == f]


# 1. Already IDLE: not cooled. ACTIVE start (stable) -> ACTIVE end ONCE -> train -> IDLE -> read.
rig = Rig({5: IDLE})
r = run(rig)                                     # 1800 -> 2500, 40 pulses, 100 ms
check("IDLE filament: no ladder, no IDLE re-command, no STOP",
      not of(rig.calls, "state") and not of(rig.calls, "STOP") and seq(rig.calls, 5)[0] == "active")
check("ACTIVE 1800 (stable) -> arm -> ACTIVE 2500 -> train -> IDLE -> read",
      seq(rig.calls, 5)[:7] == ["active", "arm", "active", "train", "idle", "read", "events"],
      seq(rig.calls, 5))
check("start is ACTIVE, waited for; end is ACTIVE, ONCE, no wait",
      of(rig.calls, "active") == [("active", 5, 1800, True), ("active", 5, 2500, False)],
      of(rig.calls, "active"))
sh = rig.shots[0]["k"]
check("one train: 40 pulses 100 ms apart", sh["num_pulses"] == 40 and sh["inter_pulse_ms"] == 100, sh)
row = r["results"][5]
check("one point per pulse, x = heating at that pulse",
      len(row["points"]) == 40 and [p["heat_mA"] for p in row["points"][:3]] == [1800, 1900, 2000]
      and row["points"][-1]["heat_mA"] == 2500, [p["heat_mA"] for p in row["points"][:8]])
check("pedestal subtracted", row["points"][1]["emission_ma"] == 0.5, row["points"][1])
check("ended at IDLE 1200, ok, saved",
      row["end"] == "idle" and of(rig.calls, "idle", 5)[-1] == ("idle", 5, 1200, False)
      and r["ok"] and of(rig.calls, "save"), (row["end"], r["problems"]))
check("short train: events read once at the end", of(rig.calls, "events")[0][2] is False)

# 2. Cold filaments: up the ladder from where they are to IDLE (stable), then ACTIVE start.
rig = Rig({1: STOP, 2: STANDBY})
run(rig)
check("STOP -> SLEEP -> STANDBY -> IDLE 1200 (verify) -> ACTIVE 1800 (verify)",
      [c for c in rig.calls if c[1:2] == (1,) and c[0] in ("state", "idle", "active")][:4]
      == [("state", 1, "SLEEP"), ("state", 1, "STANDBY"), ("idle", 1, 1200, True),
          ("active", 1, 1800, True)], rig.calls)
check("STANDBY -> IDLE 1200 -> ACTIVE 1800",
      [c for c in rig.calls if c[1:2] == (2,) and c[0] in ("state", "idle", "active")][:2]
      == [("idle", 2, 1200, True), ("active", 2, 1800, True)], rig.calls)

# 3. Already ACTIVE (any current): straight to ACTIVE start, nothing else.
rig = Rig({3: ACTIVE})
run(rig)
check("ACTIVE filament: ACTIVE 1800 directly, no IDLE first",
      [c for c in rig.calls if c[1:2] == (3,) and c[0] in ("state", "idle", "active")][:2]
      == [("active", 3, 1800, True), ("active", 3, 2500, False)], rig.calls)

# 4. Start current not stable: no train, back to IDLE (not STOP), next one runs.
rig = Rig({3: IDLE, 4: IDLE}, start={3: "slow"})
r = run(rig)
check("not stable: no train, IDLE, no STOP",
      not of(rig.calls, "train", 3) and of(rig.calls, "idle", 3)[-1] == ("idle", 3, 1200, False)
      and not of(rig.calls, "STOP"), rig.calls)
check("...says why", "not stable" in (r["results"][3]["stop_reason"] or ""), r["results"][3])
check("...next filament still measured", of(rig.calls, "train", 4))

# 5. Errors -> STOP, named; the run continues.
for kind, rk, word in (("fault", {"start": {3: "fault"}}, "cc_mode"),
                       ("short", {"start": {3: "short"}}, "struggling"),
                       ("board fault after the ramp", {"board_fault": (3,)}, "open")):
    rig = Rig({3: IDLE, 4: IDLE}, **rk)
    r = run(rig)
    check(f"{kind}: STOPPED", of(rig.calls, "STOP") == [("STOP", 3)] and r["results"][3]["end"] == "stop",
          rig.calls)
    check(f"{kind}: error names it", word in (r["results"][3]["error"] or ""), r["results"][3])
    check(f"{kind}: next filament still measured", of(rig.calls, "train", 4))

# 6. Exception: STOPPED, rails off, run stops, re-raised.
rig = Rig({3: IDLE, 4: IDLE}, raise_at=3)
try:
    run(rig)
    raised = False
except RuntimeError:
    raised = True
check("exception: re-raised, STOP, both rails OFF, next untouched",
      raised and ("STOP", 3) in rig.calls and ("emission", False) in rig.calls
      and ("focus", False) in rig.calls and not of(rig.calls, "arm", 4), rig.calls)

# 7. Starting conditions SET, not refused; clamps reported.
rig = Rig({0: IDLE}, hv_on=True)
r = run(rig, start_ma=1400, end_ma=1400, idle_ma=1600, width_us=30000, pulse_gap_ms=10, pulses=300)
p = r["params"]
check("emission already ON: not refused", r["results"] and ("limit", 55.0) in rig.calls)
check("start raised to the ACTIVE floor, end above it, idle <= start, gap and pulses fixed -- and said",
      p["start_ma"] == 1500 and p["end_ma"] == 1600 and p["idle_ma"] == 1500
      and p["pulse_gap_ms"] == 35 and p["pulses"] == 255 and len(r["notes"]) == 5, (p, r["notes"]))

# 7b. Emission: limit and voltage written BEFORE the enable, then read back.
rig = Rig({0: IDLE})
run(rig, emission_v=180, limit_ma=40)
check("emission: limit -> voltage -> ON (no moment at an old setpoint)",
      rig.calls[:3] == [("limit", 40.0), ("emission_v", 180.0), ("emission", True)], rig.calls[:4])

# 8. Focus as asked; both rails off at the end.
rig = Rig({0: IDLE}, focus_reads=-348.0)
r = run(rig, focus_on=True, focus_v=350)
check("focus ON: -350 set, on, read back, before the first filament",
      rig.calls.index(("focus_v", 350.0)) < rig.calls.index(("focus", True))
      < rig.calls.index(of(rig.calls, "arm")[0]) and r["params"]["focus_v_read"] == -348.0, rig.calls)
check("...both OFF at the end", rig.calls[-3:-1] == [("focus", False), ("emission", False)], rig.calls[-4:])
rig = Rig({0: IDLE}, focus_reads=-10.0)
run(rig, focus_on=True, focus_v=350)
check("focus not reached -> nothing heated, rails off",
      not of(rig.calls, "idle") and rig.calls[-2:] == [("focus", False), ("emission", False)], rig.calls)
rig = Rig({0: IDLE})
run(rig)
check("focus OFF requested: off before the first filament",
      rig.calls.index(("focus", False)) < rig.calls.index(of(rig.calls, "arm")[0]), rig.calls)

# 9. Abort between filaments; partial saved, rails off.
rig = Rig({0: IDLE, 1: IDLE, 2: IDLE})
done = []
r = run(rig, progress=lambda f, pts, row: done.append(f), abort=lambda: len(done) >= 1)
check("abort after the first filament", [c[1] for c in of(rig.calls, "train")] == [0] and r["aborted"])
check("...saved, rails off", ("save", [0]) in rig.calls and ("emission", False) in rig.calls)

# 10. Dead skipped and named; empty range touches nothing.
rig = Rig({0: IDLE, 1: IDLE}, dead=(1,))
r = run(rig)
check("dead skipped and named", not of(rig.calls, "arm", 1) and any("dead" in x for x in r["notes"]))
rig = Rig({0: IDLE})
r = run(rig, first=50, last=60)
check("empty range: nothing touched", not rig.calls and not r["ok"], rig.calls)

# 11. A lost event: cannot pair -> emission kept, NO heating current, said.
rig = Rig({0: IDLE}, lose=1)
r = run(rig, pulses=10)
row = r["results"][0]
check("unpaired: no x invented, marked unusable, reason given",
      len(row["points"]) == 9 and all(p["heat_mA"] is None and not p["usable"] for p in row["points"])
      and "cannot pair" in (row["stop_reason"] or ""), row["stop_reason"])

# 12. Long train: events also read in the background and handed to shot_measured.
rig = Rig({0: IDLE})
ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
rig.install(ct)
ct.pulse_events_ma = lambda since: {"ok": True, "events": []}
ct._CAL_RING_SAFE = 10
ct.emission_calibration(pulses=20)
check("over the ring: background reader used", of(rig.calls, "events")[0][2] is True)

# 13. shot_measured(prefetched): collects only the rest, after the last id.
ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
asked = []
ct._collect_pulse_events = lambda since, want, grace: (asked.append((since, want)),
                                                       ([{"id": 9}] * want, 1199))[1]
ct.ready_disarm = lambda: None
out = ct.shot_measured({"measuring": True, "num_pulses": 5, "since": 0},
                       {"ok": True, "fired": 5}, prefetched=([{"id": 3}, {"id": 4}], 1200))
check("prefetched 2 of 5: asks for 3 after id 4",
      asked == [(4, 3)] and len(out["measured"]) == 5 and out["ok"], (asked, out))

# 14. _ramp_points (shared with emission_ramp): one point per pulse, paired by order.
ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
pts = ct._ramp_points([{"plateau_net_ma": 5.0, "integral_mams": 5.0, "on_us": 1000}],
                      [{"heat_meas_mA": 2222, "heat_target_mA": 2500, "seq": 7}], 2.0, 1200)
check("ramp point: heat from the log, emission net - pedestal",
      pts[0]["heat_mA"] == 2222 and pts[0]["emission_ma"] == 3.0 and pts[0]["usable"], pts)

# 15. end-state ladder walk (emission_vs_heating / emission_ramp)
states = []
ct._state_one = lambda f, st, arg, why, *a: states.append((st, arg))
ct._walk_to_end_state(7, "idle", 1200, "t")
check("end state idle: SLEEP -> STANDBY -> IDLE", states == [(SLEEP, 0), (STANDBY, 0), (IDLE, 1200)], states)

# 16. backend job: refusals are other jobs only; start -> status -> result.
import ct.server._server as srv  # noqa: E402

srv._HEAT_SHORT["running"] = True
check("refused while heat-short runs", not srv.emission_cal_start({})["ok"])
srv._HEAT_SHORT["running"] = False
orig_lb, orig_sr = srv._lease_blocking, srv._safety_schedule_running
srv._lease_blocking = lambda owner: {"owner": "someone", "note": "scan"}
r = srv.emission_cal_start({})
check("refused while another client holds the lease", not r["ok"] and "someone" in r["error"], r)
srv._lease_blocking = lambda owner: None
srv._safety_schedule_running = lambda: (True, None)
check("refused while a schedule is armed/running", not srv.emission_cal_start({})["ok"])
srv._safety_schedule_running = lambda: (False, None)


def fake_cal(self, progress=None, abort=None, **kw):
    time.sleep(0.05)
    row = {"points": [{"heat_mA": 1800 + 100 * i, "emission_ma": 1.0, "usable": True}
                      for i in range(4)], "end": "idle", "error": None, "stop_reason": None,
           "active_s": 4.1, "fired": 4, "start_settled_ma": 1795}
    progress(9, row["points"], row)
    return {"ok": True, "params": kw, "notes": [], "problems": [], "aborted": False,
            "results": {9: dict(row)}, "saved": {"ok": True, "json": "j"}}


orig, orig_lease = CTClient.emission_calibration, CTClient.lease
CTClient.emission_calibration = fake_cal
CTClient.lease = lambda self, ttl=60, note="": contextlib.nullcontext()
try:
    s = srv.emission_cal_start({"first": 9, "end_ma": 2600, "focus_on": True, "bogus": 1})
    check("job started, unknown keys dropped",
          s["ok"] and s["params"] == {"first": 9, "end_ma": 2600, "focus_on": True}, s)
    check("second start refused while running", not srv.emission_cal_start({})["ok"])
    for _ in range(100):
        st = srv.emission_cal_status()
        if not st["running"]:
            break
        time.sleep(0.02)
    check("status: every pulse as a shot + a done row",
          len(st["shots"]) == 4 and st["done"][0]["end"] == "idle" and st["done"][0]["fired"] == 4
          and st["done"][0]["start_settled_ma"] == 1795, st)
    check("per-pulse points not duplicated in the result",
          "points" not in st["result"]["results"][9] and st["result"]["results"][9]["n_points"] == 4, st)
finally:
    CTClient.emission_calibration, CTClient.lease = orig, orig_lease
    srv._lease_blocking, srv._safety_schedule_running = orig_lb, orig_sr

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
