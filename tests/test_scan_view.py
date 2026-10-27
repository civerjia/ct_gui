#!/usr/bin/env python3
"""scan_view (software scan patch, 2026-10-26): one schedule per view, the
first k filaments raised to VOLTAGE, each fired only once "settled", then
dropped to IDLE while the filament k places on is raised; a filament that does
not arrive stops the view; cleanup always. Offline: every hardware call faked.

    python3 tests/test_scan_view.py
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


time.sleep = lambda s: None


def make(never_arrives=(), settle_ok=True):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    log, st = [], {"done": 0, "volt": set(), "polls": {}}

    ct._is_dead = lambda f: False
    ct.filament_to_board = lambda f: {"controller": 1 if f < 48 else 2, "slot": f % 48,
                                      "channel": (f % 48) // 8, "position": f % 8}
    ct._envelope_companion = lambda c: 1 if c == 2 else None
    ct.status = lambda: {"master": 1, "controllers": {"1": {"connected": True}, "2": {"connected": True}}}
    ct._disarm_all = lambda cs: log.append(("disarm", tuple(cs)))
    ct.download = lambda plan: (log.append(("download", [e["filament"] for e in plan["emission"]],
                                           plan["config"]["interPulseMs"])) or {"ok": True})
    ct._verify_retrying_timeouts = lambda plan: {"ok": True}
    ct.set_arrival_settle = lambda ms, controller=None: log.append(("settle", ms)) or {"ok": settle_ok, "error": "old fw"}

    st["perf"] = []
    ct.filament_perf = lambda fils=None: {"ok": True, "filaments": st.get("hist", {})}

    def post(path, body, timeout=None):
        if path == "/api/filament-perf":
            st["perf"].extend(body["records"])
            return {"ok": True}
        if path == "/api/live-push":
            log.append(("push", body.get("on"), tuple(body.get("firing") or ()), body.get("view")))
            return {"ok": True}
        if path == "/api/arm":
            log.append(("arm", tuple(body["controllers"])))
            return {"ok": True, "results": {str(c): {"ok": True} for c in body["controllers"]}}
        if path == "/api/sync/simulate":
            log.append(("trigger", body["count"], body["controller"]))
            st["done"] += body["count"]
            return {"ok": True}
        raise AssertionError(path)
    ct._post = post
    ct.simulate_sync_status = lambda: {"ok": True, "running": False}
    ct.shv_status = lambda c: {"state": 2, "totalPulsesDone": st["done"]}

    def state_one(f, state, arg, op, extra=None):
        log.append(("VOLT" if state == 6 else "IDLE", f, bool((extra or {}).get("in_run"))))
        (st["volt"].add if state == 6 else st["volt"].discard)(f)
        return {"ok": True}
    ct._state_one = state_one
    ct.idle_one = lambda f, ma, **k: log.append(("idle_one", f)) or st["volt"].discard(f) or {"ok": True}

    def cached(fs):
        if isinstance(fs, list):
            out = {}
            for g in fs:
                out.update(cached(g))
            return out
        f = fs
        st["polls"][f] = st["polls"].get(f, 0) + 1
        arr = ("ramping" if f in never_arrives or st["polls"][f] < 3 else "settled")
        if f not in st["volt"]:
            arr = "ramping"
        return {f: {"arrival": arr, "cc_mode": 0, "current_mA": 2500}}
    ct.read_filament_current_cached = cached
    ct.shv_pulse_log = lambda c: [{"filament": f, "heat_bus_mV": 8000, "heat_meas_mA": 2500}
                                  for f in range(96) if (f < 48) == (c == 1)]
    return ct, log, st


V = [3, 4, 5, 6, 7, 60]
ct, log, st = make()
r = ct.scan_view(V, active_mv={**{f: 8000 for f in V}, 60: 7800}, idle_ma=1200, settle_ms=500,
                 k=4, k_max=4)
check("whole view fired", r["ok"] and r["fired"] == 6, r.get("error"))
check("one download of the whole view, in order", [e for e in log if e[0] == "download"][0][1] == V)
check("arm both (P2 filament -> master too)", ("arm", (1, 2)) in log)
first_trig = next(i for i, e in enumerate(log) if e[0] == "trigger")
check("k=4 raised before the first trigger",
      [e[1] for e in log[:first_trig] if e[0] == "VOLT"] == V[:4], log[:first_trig])
seq = [(e[0], e[1]) for e in log if e[0] in ("VOLT", "IDLE", "trigger")]
check("after filament 3 fires: 3 to IDLE, then 7 raised",
      seq.index(("IDLE", 3)) < seq.index(("VOLT", 7)), seq)
check("mid-run changes carry in_run", all(e[2] for e in log if e[0] in ("VOLT", "IDLE")))
hot, peak = set(), 0
for e in log:
    if e[0] == "VOLT":
        hot.add(e[1]); peak = max(peak, len(hot))
    elif e[0] in ("IDLE", "idle_one"):
        hot.discard(e[1])
check("never more than 4 at VOLTAGE", peak == 4, peak)
check("triggers go to the head (master)", all(e[2] == 1 for e in log if e[0] == "trigger"))
check("settle set then cleared", [e[1] for e in log if e[0] == "settle"] == [500, 0])
check("watchdog spans the wait (> heat timeout + settle)",
      [e for e in log if e[0] == "download"][0][2] > 8500)
check("per-filament mV honoured (60 at 7800)", r["rows"][-1]["mv"] == 7800)
check("rows carry the heating at the pulse", r["rows"][0]["heat_mV"] == 8000 and r["rows"][-1]["heat_mA"] == 2500)
pushes = [e for e in log if e[0] == "push"]
check("live push held for the whole view and released",
      pushes[0][1] is True and pushes[-1][1] is False, pushes)
views = [e for e in log if e[0] == "push" and e[1] is True]
check("detector centre defaults to the middle filament", r.get("detector_center") == V[len(V) // 2], r.get("detector_center"))
check("each filament announced as firing, then cleared",
      [p[2] for p in pushes if p[1] is None][:2] == [(int(ct._fid_of(3)),), ()], pushes)
check("disarmed at the end", log[-1][0] in ("disarm", "settle") and any(e[0] == "disarm" for e in log[-3:]))

# Not arriving stops the view and fires nothing on it; everything raised dropped.
ct, log, st = make(never_arrives={5})
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, heat_timeout_s=0.0, k=4)
check("non-arrival stops the view", not r["ok"] and "filament 5" in r["error"] and r["fired"] == 0
      or (not r["ok"] and "filament" in r["error"]), r.get("error"))
ct, log, st = make(never_arrives={5})
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, k=4, heat_timeout_s=0.5)
trig_after = [e for e in log if e[0] == "trigger"]
check("filaments before 5 fired, 5 not", r["fired"] == 2 and len(trig_after) == 2, (r.get("error"), trig_after))
check("everything still at VOLTAGE dropped by idle_one after disarm", not st["volt"], st["volt"])

# Refusals before touching anything.
ct, log, st = make()
check("duplicate refused", "twice" in ct.scan_view([3, 3], 8000, 1200)["error"] and not log)
check("missing per-filament mV refused", "active_mv" in ct.scan_view([3, 4], {3: 8000}, 1200)["error"])
ct, log, st = make(settle_ok=False)
r = ct.scan_view(V, 8000, 1200, settle_ms=300)
check("old firmware with settle_ms: stopped before arming", not r["ok"] and not any(e[0] == "arm" for e in log), r)


# --- k="auto" --------------------------------------------------------------
# Measured arrival 0.6 s, slot 0.5 s: only the filaments whose turn is within
# 0.6 + 0.3 s may be hot before the first trigger -> slots 0 and 1.
ct, log, st = make()
ct._scan_est = ct._ScanEstimate(arrive_s=0.6, cycle_s=0.5)
r = ct.scan_view(V, active_mv=8000, idle_ma=1200)
first_trig = next(i for i, e in enumerate(log) if e[0] == "trigger")
check("auto: just-in-time lead (2 raised before the first trigger)",
      [e[1] for e in log[:first_trig] if e[0] == "VOLT"] == V[:2], log[:first_trig])
check("auto: whole view fired", r["ok"] and r["fired"] == 6, r.get("error"))
check("auto: estimate reported", set(r["estimate"]) == {"arrive_s", "cycle_s"}, r.get("estimate"))
check("gantt drawn, one line per filament", r["gantt"].count("\n") == len(V), r["gantt"])

# start_in_s: nothing fires before it; the first raise is timed to arrive then.
ct, log, st = make()
ct._scan_est = ct._ScanEstimate(arrive_s=0.2, cycle_s=0.5)
t = time.monotonic()
r = ct.scan_view(V[:2], active_mv=8000, idle_ma=1200, start_in_s=1.0)
check("start_in_s: first pulse not before the move ends", r["ok"] and r["start_s"] >= 1.0
      and r["rows"][0]["fired_s"] >= 1.0, r)
check("start_in_s: heating started late in the move, not at once",
      r["rows"][0]["raise_s"] >= 0.4, r["rows"][0])

# A link hiccup: the cached read fails twice, a VOLTAGE command fails once.
ct, log, st = make()
real_cached, real_state = ct.read_filament_current_cached, ct._state_one
flaky = {"reads": 2, "volts": 1}
def cached2(fs):
    if flaky["reads"]:
        flaky["reads"] -= 1
        raise ConnectionError("bridge reconnecting")
    return real_cached(fs)
def state2(f, state, arg, op, extra=None):
    if state == 6 and flaky["volts"]:
        flaky["volts"] -= 1
        return {"ok": False, "error": "board did not ACK"}
    return real_state(f, state, arg, op, extra)
ct.read_filament_current_cached, ct._state_one = cached2, state2
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, k=4)
check("hiccups retried, view completes", r["ok"] and r["fired"] == 6, r.get("error"))

# The trigger request errors but the burst DID start: it must not go out twice.
ct, log, st = make()
real_post = ct._post
once = {"n": 1}
def post2(path, body, timeout=None):
    if path == "/api/sync/simulate" and once["n"]:
        once["n"] -= 1
        real_post(path, body, timeout)          # it went out...
        raise TimeoutError("reply lost")         # ...but the reply did not come back
    return real_post(path, body, timeout)
ct._post = post2
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, k=4)
check("lost trigger reply: not re-sent", r["ok"] and len([e for e in log if e[0] == "trigger"]) == 6,
      [e for e in log if e[0] == "trigger"])

# --- per-filament table -----------------------------------------------------
ct, log, st = make()
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, settle_ms=500, k=4)
check("results recorded per filament (FID, mv, settle, arrival)",
      len(st["perf"]) == 6 and all(x["mv"] == 8000 and x["settle_ms"] == 500
                                   and x["arrive_s"] is not None for x in st["perf"]), st["perf"])
ct, log, st = make(never_arrives={5})
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, k=4, heat_timeout_s=0.3)
check("a filament that did not arrive is recorded as a failure",
      any(x.get("error") and x["filament"] == int(ct._fid_of(5)) for x in st["perf"]), st["perf"])
# Filament 6 is known to need 3 s at 8 V; the others 0.3 s: 6 is raised long
# before its turn, the others just in time.
ct, log, st = make()
slow = [{"mv": 8000, "arrive_raw_s": 3.0}] * 3
fast = [{"mv": 8000, "arrive_raw_s": 0.3}] * 3
st["hist"] = {f: {"samples": slow if f == 6 else fast} for f in V}
ct._scan_est = ct._ScanEstimate(arrive_s=0.3, cycle_s=0.5)
r = ct.scan_view(V, active_mv=8000, idle_ma=1200)
first_trig = next(i for i, e in enumerate(log) if e[0] == "trigger")
early = [e[1] for e in log[:first_trig] if e[0] == "VOLT"]
check("auto + table: the slow filament 6 is raised before the first trigger, fast ones are not",
      6 in early and 5 not in early, early)
ct, log, st = make()
st["hist"] = {6: {"samples": [{"mv": 5000, "arrive_raw_s": 9.0}] * 3}}
ct._scan_est = ct._ScanEstimate(arrive_s=0.3, cycle_s=0.5)
r = ct.scan_view(V, active_mv=8000, idle_ma=1200)
first_trig = next(i for i, e in enumerate(log) if e[0] == "trigger")
check("history at another voltage (5 V vs 8 V) is not used",
      6 not in [e[1] for e in log[:first_trig] if e[0] == "VOLT"])

# --- scan_plan / scan_views ----------------------------------------------------
ct, log, st = make()
ct.build_scan_schedule = lambda mode="stationary", **g: {"excluded": [9], "truncated": False, "emission": [
    {"filament": f, "coll": c, "ringStep": c, "gantry": 0.0} for c in (0, 1) for f in ((95, 0, 1) if c == 0 else (0, 1, 2))]}
plan = ct.scan_plan()
check("scan_plan groups by view, centre and order kept",
      plan["n_views"] == 2 and plan["views"][0] == {"view": 0, "center": 0, "filaments": [95, 0, 1]}
      and plan["views"][1]["filaments"] == [0, 1, 2] and plan["dead_left_out"] == [9], plan)

calls, moved = [], []
def fake_view(fils, **kw):
    calls.append((list(fils), kw["start_in_s"], kw["detector_center"]))
    if 1 in fils:                                   # filament 1 never arrives
        return {"ok": False, "error": "filament 1 not settled", "failed_filament": 1,
                "rows": [{"filament": fils[0], "fired_s": 1.0, "records": [{}] * 5},
                         {"filament": 1, "fired_s": None}], "fired": 1}
    return {"ok": True, "fired": len(fils), "elapsed_s": 1.0,
            "rows": [{"records": [{}] * 5} for _ in fils]}
ct.scan_view = fake_view
ct.read_filament_current_cached = lambda fs: {f: {"current_mA": 1200} for f in fs}
r = ct.scan_views(plan["views"], active_mv=5000, idle_ma=1200, num_pulses=5, move_s=3.0,
                  move_to=lambda v: moved.append(v["view"]))
check("motor started once per view", moved == [0, 1], moved)
check("a filament that does not arrive: left out, only the view's UNFIRED rest retried, no new move",
      calls[0] == ([95, 0, 1], 3.0, 0) and calls[1] == ([0], 0.0, 0)
      and calls[2] == ([0, 2], 3.0, 1), calls)
check("left_out_why says why", "not settled" in r["left_out_why"][1], r.get("left_out_why"))
check("run completes; left_out and pulses reported",
      r["ok"] and r["views_done"] == 2 and r["left_out"] == [1] and r["pulses"] == 5 + 5 + 10, r)

calls.clear()
ct.scan_view = lambda fils, **kw: calls.append(1) or {"ok": False, "error": "SHV fault", "rows": []}
r = ct.scan_views(plan["views"], active_mv=5000, idle_ma=1200)
check("any other failure stops the run (no retry, no next view)",
      not r["ok"] and len(calls) == 1 and "run stopped" in r["error"] and r["views_done"] == 0, r)

# A filament refused its VOLTAGE (board absent) is NAMED, so scan_views leaves it out.
ct, log, st = make()
real_state = ct._state_one
ct._state_one = lambda f, state, arg, op, extra=None: ({"ok": False, "error": "its board is not present"}
                                                      if (f == 6 and state == 6) else real_state(f, state, arg, op, extra))
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, k=4)
check("VOLTAGE refused -> failed_filament set", not r["ok"] and r.get("failed_filament") == 6
      and "not present" in r["error"], (r.get("failed_filament"), r.get("error")))

calls.clear()
ct.scan_view = fake_view
ct.read_filament_current_cached = lambda fs: {f: {"current_mA": (None if f == 2 else 1200)} for f in fs}
r = ct.scan_views(plan["views"][1:], active_mv=5000, idle_ma=1200)
check("no current reading before the run -> left out up front, with the reason",
      2 in r["left_out"] and "no current reading" in r["left_out_why"][2] and all(2 not in c[0] for c in calls), (r, calls))

# None = no motor: move_s=None and start_in_s=None mean 0, not a TypeError.
calls.clear()
ct.scan_view = fake_view
ct.read_filament_current_cached = lambda fs: {f: {"current_mA": 1200} for f in fs}
r = ct.scan_views(plan["views"][1:], active_mv=5000, idle_ma=1200, move_s=None, move_to=None)
check("scan_views(move_s=None, move_to=None): runs, first pulse not delayed",
      r["ok"] and calls and calls[0][1] == 0.0, (r, calls))
ct, log, st = make()
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, start_in_s=None)
check("scan_view(start_in_s=None) = 0", r.get("ok") and r.get("start_s", 0) == 0, (r.get("ok"), r.get("error"), r.get("start_s")))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
