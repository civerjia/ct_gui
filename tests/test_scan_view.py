#!/usr/bin/env python3
"""scan_view (docs/scan_view_design.md) against a fake gated RP2350: rows fire
strictly in list order, each once HOT; a filament hot early waits its turn and
then goes without a new wait; data-loss faults skip one filament and the run
goes on; dangerous faults stop it; everything ends at IDLE. Offline.

    python3 tests/test_scan_view.py
"""
import _path  # noqa: F401

import os
import threading
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    t = 1000.0


def _sleep(s):
    Clock.t += max(float(s), 0.001)


time.monotonic = lambda: Clock.t
time.sleep = _sleep

VOLTAGE, IDLE = 6, 4


class Rig:
    """A gated SHV run on two controllers (P1 = master, filaments 0..47; P2 48..95)."""

    def __init__(self, hot_after=None, interrupted=(), fault_cc=(), edges_short=(),
                 dead=(), hv=True):
        self.hot_after = dict(hot_after or {})      # filament -> s after raise (None = never)
        self.interrupted, self.fault_cc = set(interrupted), set(fault_cc)
        self.edges_short = dict(edges_short)        # filament -> edges actually sent
        self.dead, self.hv = set(dead), hv
        self.log = []
        self.raised = {}                            # filament -> raise time
        self.rows, self.cur, self.in_row = [], 0, 0
        self.released = {}                          # controller -> rows released there
        self.done, self.dropped, self.state, self.fault = 0, 0, 0, False
        self.counted = {}                           # filament -> pulses
        self.tripped = None

    # firmware model ----------------------------------------------------------
    def edges(self, k):
        for _ in range(k):
            if self.state not in (1, 2) or self.cur >= max(self.released.values() or [0]):
                self.dropped += 1
                continue
            self.state = 2
            f = self.rows[self.cur]["filament"]
            self.done += 1
            self.in_row += 1
            self.counted[f] = self.counted.get(f, 0) + 1
            if self.in_row >= self.rows[self.cur]["numPulses"]:
                self._advance()

    def _advance(self):
        self.cur += 1
        self.in_row = 0
        if self.cur >= len(self.rows):
            self.state = 3

    def row_cmd(self, c, row, action):
        """Every armed controller gets the same row command; the shared row
        pointer moves once (both controllers walk the same table in step)."""
        rel = self.released.get(c, 0)
        if action == 2 and row + 1 == rel and row < self.cur:
            return 0          # the other controller already moved the shared pointer on
        if self.state not in (1, 2):
            return 2
        first = all(v <= rel for v in self.released.values())
        if action in (0, 1):
            if row != rel or (first and row != self.cur):
                return 3
            self.released[c] = rel + 1
            if first:
                self.log.append(("release" if action == 0 else "skip", row))
                if action == 1:
                    self._advance()
            return 0
        if action == 2:
            if row + 1 != rel:
                return 3
            if row == self.cur:
                self.log.append(("skiprest", row))
                self._advance()
            return 0
        return 5

    def hot(self, f):
        if f not in self.raised or f in self.interrupted:
            return False
        after = self.hot_after.get(f, 1.0)
        return after is not None and Clock.t - self.raised[f] >= after


def make(rig: Rig):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    site = lambda f: {"controller": 1 if f < 48 else 2, "channel": (f % 48) // 8,  # noqa: E731
                      "position": f % 8, "slot": f % 48}
    ct.filament_to_board = site
    ct._is_dead = lambda f: f in rig.dead
    ct._fid_of = lambda f: f
    ct._envelope_companion = lambda c: None if c == 1 else 1
    ct.status = lambda: {"master": 1, "controllers": {"1": {"connected": True},
                                                       "2": {"connected": True}}}
    ct.hv_status = lambda: {"ok": True, "emission_on": rig.hv, "focus_on": rig.hv}
    ct._get = lambda path, timeout=None: ({"tripped": rig.tripped, "vacuum": {"within_limit": True}}
                                          if path == "/api/hv-guard" else {})
    ct.filament_perf = lambda fils=None: {"ok": True, "filaments": {}}

    def download(plan):
        rig.rows = list(plan["emission"])
        rig.config = plan["config"]
        rig.log.append(("download", [e["filament"] for e in plan["emission"]], plan["config"].get("gated")))
        return {"ok": True}
    ct.download = download
    ct._verify_retrying_timeouts = lambda plan: {"ok": True}

    def shv_status(c=1):
        return {"state": 4 if rig.fault else rig.state, "stopReason": 5 if rig.fault else 0,
                "totalPulsesDone": rig.done, "entryIndex": rig.cur, "gated": True,
                "unsafeSlots": 0, "off_mismatches": 0, "droppedEdges": rig.dropped,
                "suppressedNotHot": 0}
    ct.shv_status = shv_status
    ct.shv_pulse_log = lambda c=1: [{"filament": f, "suppressed_not_hot": False}
                                    for f, k in rig.counted.items() for _ in range(k)
                                    if (f < 48) == (c == 1)]

    def state_one(f, state, arg, op, extra=None):
        rig.log.append(("state", f, int(state), arg))
        if int(state) == VOLTAGE:
            rig.raised[f] = Clock.t
        else:
            rig.raised.pop(f, None)
        return {"ok": True}
    ct._state_one = state_one
    ct.sleep_one = lambda f, **kw: rig.log.append(("sleep", f)) or {"ok": True}
    ct.read_filament_current_cached = lambda fs: {fs: {"cc_mode": 3 if fs in rig.fault_cc else 0}}

    def post(path, body, timeout=None):
        if path == "/api/cmd":
            cmd = body["command"]
            if cmd == "CH_ARRIVAL_SETTLE":
                rig.log.append(("settle", body["controller"], body["settle_ms"],
                                body.get("max_voltage_drop_mv")))
                return {"ok": True, "response": {"raw": [0, 0, 0, 0x60, 0xEA, 0xF4, 0x01]}}
            if cmd == "SHV_ROW":
                return {"ok": True, "response": {"raw": [0, rig.row_cmd(body["controller"], body["row"],
                                                                        body["action"])]}}
            if cmd == "CH_GET_HOT":
                c = body["controller"]
                raw = [0, 8]
                for ch in range(8):
                    hot = intr = 0
                    for p in range(8):
                        f = (0 if c == 1 else 48) + ch * 8 + p
                        if rig.hot(f):
                            hot |= 1 << p
                        if f in rig.interrupted and f in rig.raised:
                            intr |= 1 << p
                    raw += [hot, intr]
                return {"ok": True, "response": {"raw": raw + [0, 0, 0, 0]}}
        if path == "/api/arm":
            rig.state = 1
            rig.log.append(("arm", tuple(body["controllers"])))
            return {"ok": True, "results": {str(c): {"ok": True} for c in body["controllers"]}}
        if path == "/api/disarm":
            rig.log.append(("disarm", tuple(body["controllers"])))
            rig.state = 0
            return {"ok": True}
        if path == "/api/sync/simulate":
            if getattr(rig, "sim_busy", 0) > 0:   # the backend's poll tail of the last train
                rig.sim_busy -= 1
                return {"ok": False, "error": "simulation already running"}
            f = rig.rows[rig.cur]["filament"] if rig.cur < len(rig.rows) else None
            rig.edges(rig.edges_short.get(f, body["count"]))
            return {"ok": True}
        return {"ok": True}
    ct._post = post
    return ct


MV = {f: 7000 + f for f in range(96)}
ARGS = dict(active_mv=MV, idle_ma=1100, settle_ms=500, num_pulses=3, width_us=1000,
            heat_timeout_s=3.0, shot_wait_timeout_s=2.0)


def ext_on_armed(rig, calls):
    def on_armed(f):
        calls.append((f, Clock.t))
        rig.edges(rig.edges_short.get(f, 3))
    return on_armed


# --- the normal run: order, hot early waits, no new wait, all to IDLE ----------
rig = Rig(hot_after={10: 2.0, 11: 0.5, 12: 0.5})
calls = []
r = make(rig).scan_view([10, 11, 12], trigger="ext", on_armed=ext_on_armed(rig, calls), **ARGS)
check("fired 10, 11, 12 in list order", [f for f, _ in calls] == [10, 11, 12], calls)
check("one gated schedule for the list, downloaded once",
      [e for e in rig.log if e[0] == "download"] == [("download", [10, 11, 12], 1)])
check("each raised to its OWN voltage",
      all(e[3] == MV[e[1]] for e in rig.log if e[0] == "state" and e[2] == VOLTAGE))
i_rel2 = rig.log.index(("release", 1))
i_idle1 = next(i for i, e in enumerate(rig.log) if e[:3] == ("state", 10, IDLE))
check("11 was hot before 10 and fired only after it; released before 10 goes IDLE",
      i_rel2 < i_idle1 and calls[1][1] - calls[0][1] < 0.5, (i_rel2, i_idle1, calls))
check("11 waited its turn hot (waited_s > 0), no new wait after 10",
      r["rows"][1]["waited_s"] > 0 and r["rows"][1]["gap_s"] < 0.5, r["rows"][1])
check("ok, all fired 3/3, nothing hot at the end",
      r["ok"] and r["fired"] == 3 and not rig.raised and all(x["fired"] == 3 for x in r["rows"]), r)
check("settle and max drop set, settle back to 0 at the end",
      ("settle", 1, 500, 500) in rig.log and ("settle", 1, 0, None) in rig.log)
check("disarmed at the end",
      any(e[0] == "disarm" for e in rig.log[rig.log.index(("release", 2)):]))

# --- preheat_ahead=N and max_hot ----------------------------------------------
rig = Rig()
make(rig).scan_view(list(range(20, 28)), trigger="sim", preheat_ahead=2, max_hot=6, **ARGS)
first_rel = rig.log.index(("release", 0))
pre = [e[1] for e in rig.log[:first_rel] if e[0] == "state" and e[2] == VOLTAGE]
check("preheat_ahead=2: rows 0..2 heated before the first release", pre == [20, 21, 22], pre)
rig = Rig()
make(rig).scan_view(list(range(20, 30)), trigger="sim", preheat_ahead=9, max_hot=3, **ARGS)
hot, peak = set(), 0
for e in rig.log:
    if e[0] == "state":
        (hot.add if e[2] == VOLTAGE else hot.discard)(e[1])
        peak = max(peak, len(hot))
check("max_hot=3 is never exceeded", peak == 3, peak)

# --- data lost: skipped, the run goes on ---------------------------------------
rig = Rig(hot_after={31: None})
calls = []
r = make(rig).scan_view([30, 31, 32], trigger="ext", on_armed=ext_on_armed(rig, calls), **ARGS)
check("never hot: skipped (not released), the rest fired, filament back to IDLE",
      [f for f, _ in calls] == [30, 32] and ("skip", 1) in rig.log and r["skipped"] == [31]
      and "not hot" in r["rows"][1]["skipped"] and 31 not in rig.raised and not r.get("stopped"), r)

rig = Rig(interrupted=[34], fault_cc=[34])
calls = []
r = make(rig).scan_view([33, 34, 35], trigger="ext", on_armed=ext_on_armed(rig, calls), **ARGS)
check("output fault: skipped, IDLE during the run, SLEEP after the disarm",
      [f for f, _ in calls] == [33, 35] and "output fault" in r["rows"][1]["skipped"]
      and rig.log.index(("sleep", 34)) > max(i for i, e in enumerate(rig.log) if e[0] == "disarm"), r)

rig = Rig(edges_short={37: 1})
calls = []
r = make(rig).scan_view([36, 37, 38], trigger="ext", on_armed=ext_on_armed(rig, calls), **ARGS)
check("edges short: the rest of that row skipped, 1 of 3 recorded, run goes on",
      ("skiprest", 1) in rig.log and r["rows"][1]["fired"] == 1 and r["partial"] == [37]
      and r["rows"][2]["fired"] == 3 and not r.get("stopped"), r)

rig = Rig()


def raising(f):
    if f == 41:
        raise RuntimeError("detector busy")
    rig.edges(3)


r = make(rig).scan_view([40, 41, 42], trigger="ext", on_armed=raising, **ARGS)
check("on_armed raised: that row's rest skipped and recorded, run goes on",
      "detector busy" in (r["rows"][1].get("skipped") or "") and r["rows"][2]["fired"] == 3
      and not r.get("stopped"), r)

# --- dangerous: stop now ------------------------------------------------------
rig = Rig()
r = make(rig).scan_view([43, 44], trigger="ext", on_armed=lambda: None, **ARGS)
check("on_armed taking no argument: stopped, nothing left hot",
      "ONE argument" in (r.get("stopped") or "") and not rig.raised, r.get("stopped"))

rig = Rig()
gate = threading.Event()
r = make(rig).scan_view([45, 46], trigger="ext", on_armed=lambda f: gate.wait(), on_armed_timeout_s=1.0,
                        **ARGS)
gate.set()
check("on_armed hanging: stopped after on_armed_timeout_s, nothing left hot",
      "on_armed_timeout_s" in (r.get("stopped") or "") and not rig.raised, r.get("stopped"))

rig = Rig()


def faulting(f):
    rig.edges(1)
    rig.fault = True


r = make(rig).scan_view([50, 51, 52], trigger="ext", on_armed=faulting, **ARGS)
check("SHV fault: stopped, disarmed, nothing left hot, 51/52 never fired",
      "SHV fault" in (r.get("stopped") or "") and not rig.raised and 51 not in rig.counted, r.get("stopped"))

rig = Rig()
r = make(rig).scan_view([53, 54, 57, 58], trigger="ext", on_armed=lambda f: None, **ARGS)
check("no edges at all: no_pulse_timeout_s stops the run",
      "no_pulse_timeout_s" in (r.get("stopped") or "") and not rig.raised, r)

rig = Rig()


def hv_off(f):
    rig.edges(3)
    rig.hv = False


r = make(rig).scan_view([55, 56, 59, 60, 61], trigger="ext", on_armed=hv_off, **ARGS)
check("HV switched off mid-run: stopped before the rest", "HV switched off" in (r.get("stopped") or "")
      and len(rig.counted) < 5 and not rig.raised, (r.get("stopped"), rig.counted))
rig = Rig()


def hv_off_last(f):
    rig.edges(3)
    if f == 63:
        rig.hv = False


r = make(rig).scan_view([62, 63], trigger="ext", on_armed=hv_off_last, **ARGS)
check("HV off during the LAST filament: not ok, stopped", not r["ok"]
      and "HV switched off" in (r.get("stopped") or ""), r.get("stopped"))

# --- a refused raise is not retried for 10 s -----------------------------------
rig = Rig()
ct = make(rig)
real = ct._state_one
ct._state_one = lambda f, state, arg, op, extra=None: (
    {"ok": False, "error": "currently at SLEEP(2); ACTIVE may only be entered from IDLE(4)"}
    if (f == 71 and state == VOLTAGE) else real(f, state, arg, op, extra))
t_start = Clock.t
r = ct.scan_view([70, 71], trigger="sim", **ARGS)
check("a refused VOLTAGE skips that filament at once (no 10 s retry), the rest fires",
      r["skipped"] == [71] and "SLEEP" in r["rows"][1]["skipped"] and r["rows"][0]["fired"] == 3
      and Clock.t - t_start < 9.0, (r.get("skipped"), round(Clock.t - t_start, 2)))

# --- refused before anything is touched -----------------------------------------
for name, kw, want in (
        ("a single voltage", dict(active_mv=7000), "every filament"),
        ("a missing voltage", dict(active_mv={60: 7000}), "[61]"),
        ("ext without on_armed", dict(trigger="ext"), "on_armed"),
        ("a dead filament", dict(), "dead")):
    rig = Rig(dead=[61] if name == "a dead filament" else [])
    a = {**ARGS, **kw}
    r = make(rig).scan_view([60, 61], **a)
    check(f"refused: {name}", not r["ok"] and want in (r.get("error") or "") and not rig.log, r.get("error"))
# --- sim: the backend still says "already running" just after a train --------
rig = Rig()
rig.sim_busy = 2
r = make(rig).scan_view([62, 63], trigger="sim", pulse_interval_ms=30, **ARGS)
check("sim 'already running' tail: waited out, nothing skipped",
      r["ok"] and not r["skipped"] and r["fired"] == 2 and rig.sim_busy == 0, r)

rig = Rig(hv=False)
r = make(rig).scan_view([60, 61], **ARGS)
check("refused: HV off at the start", "HV is not on" in (r.get("error") or "") and not rig.log, r.get("error"))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
