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
        self.ev_ring, self.ev_next, self.ev_cap = [], 1, 128   # the ESP32's pulse-event ring

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
            e = {"id": self.ev_next, "filament": f, "integral_mams": 1.0}
            if f not in getattr(self, "no_current", ()):
                e["emission_ma"] = 0.4              # the path conducted
            self.ev_ring = (self.ev_ring + [e])[-self.ev_cap:]
            self.ev_next += 1
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


# The rig's REAL filament order (GET /api/filament-order, 2026-10-30): YOUR
# number -> FID. Not identity, on purpose: with an identity fake a numbering
# bug cannot show (scan_view matched pulse-log records on the FID for weeks).
ORDER = [16, 17, 19, 18, 20, 21, 22, 23, 8, 9, 10, 11, 12, 13, 14, 15, 0, 1, 3, 2, 4, 5, 6,
         7, 24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43,
         44, 45, 46, 47, 60, 61, 62, 63, 64, 65, 66, 67, 68, 69, 70, 71, 48, 49, 51, 52, 53,
         54, 55, 56, 57, 58, 59, 72, 50, 73, 74, 75, 76, 77, 78, 79, 80, 81, 82, 83, 84, 85,
         86, 87, 88, 89, 90, 91, 92, 93, 94, 95]
USER_OF = {fid: u for u, fid in enumerate(ORDER)}


def site(fid):
    """The hardware place of a FID (what the backend's mapping says)."""
    return {"controller": 1 if fid < 48 else 2, "channel": (fid % 48) // 8,
            "position": fid % 8, "slot": fid % 48}


def make(rig: Rig):
    """The client is REAL down to its _get/_post: every numbering crossing
    (_fid_of, _user_index_of, _plan_to_fids, shv_pulse_log, the mapping) runs
    as on the rig. The fake backend below speaks FID / hardware sites only and
    translates with its OWN copy of the order, so a client that sends or reads
    the wrong numbering lands on the wrong filament here. The Rig model and its
    log are in YOUR numbering, as the assertions are."""
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    ct._adopt_order({"ok": True, "order": ORDER, "epoch": "test", "token": None})
    ct._dead_cache = {ORDER[u] for u in rig.dead}       # the mask is physical (FID)
    ct._refresh_dead = lambda force=False: None
    ct.get_mapping = lambda: {"ok": True, "mapping": {"filaments": [
        {"filament": fid, **{**site(fid), "controller": site(fid)["controller"] - 1}}
        for fid in range(96)]}}
    ct._envelope_companion = lambda c: None if c == 1 else 1
    ct.status = lambda: {"master": 1, "controllers": {"1": {"connected": True},
                                                       "2": {"connected": True}}}
    ct.hv_status = lambda: {"ok": True, "emission_on": rig.hv, "focus_on": rig.hv}

    def get(path, timeout=None):
        if path == "/api/hv-guard":
            return {"tripped": rig.tripped, "vacuum": {"within_limit": True}}
        if path.startswith("/api/filament-currents?filament="):
            u = USER_OF[int(path.rsplit("=", 1)[1])]
            return {"ok": True, "filaments": {str(ORDER[u]): {
                "current_mA": 2000, "cc_mode": 3 if u in rig.fault_cc else 0}}}
        if path.startswith("/api/filament-perf"):
            return {"ok": True, "filaments": {}}
        return {}
    ct._get = get

    def download(plan):
        fplan, _dead = ct._plan_to_fids(plan)            # the real crossing
        rig.rows = [{**e, "filament": USER_OF[int(e["filament"])]} for e in fplan["emission"]]
        rig.config = plan["config"]
        rig.log.append(("download", [e["filament"] for e in rig.rows], plan["config"].get("gated")))
        return {"ok": True}
    ct.download = download
    ct._verify_retrying_timeouts = lambda plan: {"ok": True}

    def shv_status(c=1):
        if getattr(rig, "pending", 0) > 0:          # edges arriving over time (30 ms apart)
            rig.pending -= 1
            rig.edges(1)
        return {"state": 4 if rig.fault else rig.state, "stopReason": 5 if rig.fault else 0,
                "totalPulsesDone": rig.done, "entryIndex": rig.cur, "gated": True,
                "unsafeSlots": 0, "off_mismatches": 0, "droppedEdges": rig.dropped,
                "suppressedNotHot": 0}
    ct.shv_status = shv_status
    ct.shot_measure_arm = lambda shot, rate_hz, **kw: {"ok": True, "since": rig.ev_next - 1}
    ct.ready_disarm = lambda: {"ok": True}
    ct.pulse_events_ma = lambda since=0: {"ok": True, "events": [e for e in rig.ev_ring
                                                                   if e["id"] > since]}

    def pulse_log(c, start):
        """The firmware's log: FIDs, on the controller that owns the FID."""
        recs = [{"filament": ORDER[u], "suppressed_not_hot": False,
                 "on_mismatch": u in getattr(rig, "mismatch", ()), "hv_stuck_on": False,
                 "heat_meas_mA": 2000}
                for u, k in rig.counted.items() for _ in range(k)
                if site(ORDER[u])["controller"] == c]
        if getattr(rig, "log_unreadable", False):
            recs = []
        page = recs[start:start + 9]                 # the real firmware: ~9 a page
        rig.log_reads = getattr(rig, "log_reads", []) + [(c, start, len(page), rig.state in (1, 2))]
        return {"ok": True, "records": page, "total": len(recs)}

    def filament_state(body):
        u = USER_OF[int(body["filament"])]
        state, arg = int(body["state"]), body.get("arg")
        if state == VOLTAGE and getattr(rig, "not_warm", {}).get(u, 0) > 0:
            rig.not_warm[u] -= 1
            return {"ok": False, "error": f"filament {body['filament']} may not go to timed "
                                          f"VOLTAGE: not warm yet: at IDLE for 40 s"}
        flaky = getattr(rig, "idle_bus_error", {})
        if state == IDLE and flaky.get(u, 0) > 0 and rig.state in (1, 2):
            flaky[u] -= 1
            return {"ok": False, "error": f"P{site(ORDER[u])['controller']} did not apply "
                                          f"IDLE(4) (firmware status BUS_ERROR)"}
        if state == VOLTAGE and u in getattr(rig, "refuse_voltage", ()):
            return {"ok": False, "error": "currently at SLEEP(2); ACTIVE may only be "
                                          "entered from IDLE(4)"}
        if state == 2:
            rig.log.append(("sleep", u))
            return {"ok": True}
        rig.log.append(("state", u, state, arg))
        if state == VOLTAGE:
            rig.raised[u] = Clock.t
        else:
            rig.raised.pop(u, None)
        return {"ok": True}

    def post(path, body, timeout=None):
        if path == "/api/filament-state":
            return filament_state(body)
        if path == "/api/shv" and body.get("op") == "pulse_log":
            return pulse_log(int(body["controller"]), int(body.get("start") or 0))
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
                # Hardware bits: channel / position of each FID, its state by
                # YOUR number through the backend's own copy of the order.
                c = body["controller"]
                raw = [0, 8]
                for ch in range(8):
                    hot = intr = 0
                    for p in range(8):
                        if ch * 8 + p >= 48:          # CH7/CH8: no filament mapped
                            continue
                        u = USER_OF[(0 if c == 1 else 48) + ch * 8 + p]
                        if rig.hot(u):
                            hot |= 1 << p
                        if u in rig.interrupted and u in rig.raised:
                            intr |= 1 << p
                    raw += [hot, intr]
                return {"ok": True, "response": {"raw": raw + [0, 0, 0, 0]}}
        if path == "/api/arm":
            rig.state = 1
            rig.cur, rig.in_row, rig.released = 0, 0, {}   # an ARM starts the table over
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

# --- data lost: NOT skipped -- a fake turn (2026-10-30, the user: never skip a
# filament): its trigger still runs for the detector, no HV, the edges dropped --
rig = Rig(hot_after={31: None})
calls = []
r = make(rig).scan_view([30, 31, 32], trigger="ext", on_armed=ext_on_armed(rig, calls), **ARGS)
check("never hot: on_armed still called for it (fake turn), its row not released",
      [f for f, _ in calls] == [30, 31, 32] and ("skip", 1) in rig.log and ("release", 1) not in rig.log
      and r["rows"][1].get("fake_armed") and "not hot" in r["rows"][1]["skipped"], r)
check("...no HV for it: its 3 edges dropped as ignored_edges, not counted, not dropped_edges",
      31 not in rig.counted and r["ignored_edges"] == 3 and r["dropped_edges"] == 0
      and r["rows"][1]["ignored_edges"] == 3, (rig.counted, r["ignored_edges"], r["dropped_edges"]))
check("...the next filament fired its own 3, none of the fake ones; data marked lost, not ok",
      r["rows"][2]["fired"] == 3 and rig.counted.get(32) == 3 and r["skipped"] == [31]
      and not r["ok"] and 31 not in rig.raised and not r.get("stopped"), r)

from ct.client._base import Result  # noqa: E402
txt, full = repr(Result(r)), Result(r).full()
check("print: one line per filament, the fake turn named, per-pulse detail only in full()",
      txt.count("\n") < 25 and "FAKE TURN" in txt and "r.full()" in txt
      and "tOnUs" not in txt and "tOnUs" in full, txt)

rig = Rig(interrupted=[34], fault_cc=[34])
calls = []
r = make(rig).scan_view([33, 34, 35], trigger="ext", on_armed=ext_on_armed(rig, calls), **ARGS)
check("output fault: fake turn, IDLE during the run, SLEEP after the disarm",
      [f for f, _ in calls] == [33, 34, 35] and "output fault" in r["rows"][1]["skipped"]
      and r["rows"][1].get("fake_armed") and 34 not in rig.counted
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
check("rails switched off mid-run: not scan_view's business, the run goes on",
      not r.get("stopped") and len(rig.counted) == 5, (r.get("stopped"), rig.counted))
rig = Rig()


def hv_off_last(f):
    rig.edges(3)
    if f == 63:
        rig.hv = False


r = make(rig).scan_view([62, 63], trigger="ext", on_armed=hv_off_last, **ARGS)
check("rails off during the LAST filament: not stopped", not r.get("stopped"), r.get("stopped"))

# --- a refused raise is not retried for 10 s -----------------------------------
rig = Rig()
rig.refuse_voltage = {71}
ct = make(rig)
t_start = Clock.t
# (refused one first: the fake walks ONE shared row pointer, so it cannot skip the last row)
r = ct.scan_view([71, 70], trigger="sim", **ARGS)
check("a refused VOLTAGE: fake turn at once (no 10 s retry), the rest fires",
      r["skipped"] == [71] and "SLEEP" in r["rows"][0]["skipped"] and r["rows"][0].get("fake_armed")
      and r["rows"][1]["fired"] == 3 and 71 not in rig.counted
      and Clock.t - t_start < 9.0, (r.get("skipped"), round(Clock.t - t_start, 2), r.get("stopped")))

# --- refused before anything is touched -----------------------------------------
for name, kw, want in (
        ("a single voltage", dict(active_mv=7000), "every filament"),
        ("a missing voltage", dict(active_mv={60: 7000}), "[61]"),
        ("ext without on_armed", dict(trigger="ext"), "on_armed")):
    rig = Rig()
    a = {**ARGS, **kw}
    r = make(rig).scan_view([60, 61], **a)
    check(f"refused: {name}", not r["ok"] and want in (r.get("error") or "") and not rig.log, r.get("error"))
# --- measure: more pulses than the ESP32's 128-event ring -------------------
FILS = list(range(20, 30))                      # 10 x 20 = 200 pulses
rig = Rig()
r = make(rig).scan_view(FILS, trigger="ext", on_armed=lambda f: rig.edges(20), measure=True,
                        **{**ARGS, "num_pulses": 20})
own = all(r2["measured"] is not None and len(r2["measured"]) == 20
          and all(e["filament"] == r2["filament"] for e in r2["measured"]) for r2 in r["rows"])
check("measure, 200 pulses through a 128 ring: every row has its OWN 20 events",
      own and r["measured_events"] == 200 and not r.get("measure_lost"),
      [(x["filament"], x["measured"] and len(x["measured"])) for x in r["rows"]])

rig = Rig()
rig.ev_cap = 10                                 # overrun between reads
r = make(rig).scan_view(FILS, trigger="ext", on_armed=lambda f: rig.edges(20), measure=True,
                        **{**ARGS, "num_pulses": 20})
wrong = [x["filament"] for x in r["rows"] if x["measured"] is not None
         and any(e["filament"] != x["filament"] for e in x["measured"])]
check("ring overrun: lost rows have NO measurement, never another row's",
      not wrong and r.get("measure_lost") and any(x.get("measure_lost") for x in r["rows"]),
      (wrong, r.get("measure_note")))

# --- scan_views: every view runs every filament (2026-10-30: none left out) ----
rig = Rig(hot_after={72: None})                 # 72 never gets hot
# (skipped rows first: the fake walks ONE shared row pointer, so it cannot skip the last row)
V = [{"view": 0, "filaments": [72, 71, 70]}, {"view": 1, "filaments": [72, 71, 70]}]
calls = []
r = make(rig).scan_views(V, active_mv=MV, idle_ma=1100, settle_ms=500, trigger="ext",
                         on_armed=ext_on_armed(rig, calls), num_pulses=3, width_us=1000,
                         heat_timeout_s=3.0, shot_wait_timeout_s=2.0)
v0, v1 = r["results"]
check("scan_views: never-hot 72 is NOT left out -- a fake turn in both views, the rest fired",
      [f for f, _ in calls] == [72, 71, 70, 72, 71, 70] and r["failed"] == {72: [0, 1]}
      and r["left_out"] == [] and all(x["fired"] == 3 for v in (v0, v1) for x in v["rows"][1:]),
      (r["failed"], [f for f, _ in calls]))

# --- sim: the backend still says "already running" just after a train --------
rig = Rig()
rig.sim_busy = 2
r = make(rig).scan_view([62, 63], trigger="sim", pulse_interval_ms=30, **ARGS)
check("sim 'already running' tail: waited out, nothing skipped",
      r["ok"] and not r["skipped"] and r["fired"] == 2 and rig.sim_busy == 0, r)

# --- ON read-back mismatch: judged by the measured current (2026-10-30 test7) ---
rig = Rig()
rig.mismatch, rig.no_current = {21, 22}, {22}
r = make(rig).scan_view([20, 21, 22], trigger="ext", on_armed=lambda f: rig.edges(3), measure=True,
                        **ARGS)
by = {x["filament"]: x for x in r["rows"]}
check("read-back mismatch, current flowed -> readback_unreliable, not on_mismatch",
      not by[21].get("on_mismatch") and by[21].get("readback_unreliable") == [0, 1, 2], by[21])
check("read-back mismatch, no current -> on_mismatch kept, run not ok",
      by[22].get("on_mismatch") and not by[22].get("readback_unreliable") and not r["ok"], by[22])
rig = Rig()
rig.mismatch = {21}
r = make(rig).scan_view([20, 21], trigger="ext", on_armed=lambda f: rig.edges(3), measure=True, **ARGS)
check("only a wrong read-back -> run ok", r["ok"], [(x["filament"], x.get("on_mismatch")) for x in r["rows"]])
rig = Rig()
rig.mismatch = {21}
r = make(rig).scan_view([20, 21], trigger="ext", on_armed=lambda f: rig.edges(3), **ARGS)
check("read-back mismatch without measure -> on_mismatch kept, not ok",
      not r["ok"] and r["rows"][1].get("on_mismatch"), r["rows"][1])

# --- IDLE refused once with BUS_ERROR: tried again, no stale error -------------
for fails, name in ((1, "once"), (999, "all through the run")):
    rig = Rig()
    rig.idle_bus_error = {70: fails}
    r = make(rig).scan_view([70, 71], trigger="sim", **ARGS)
    idle70 = [x for x in rig.log if x[:3] == ("state", 70, 4)]
    check(f"IDLE BUS_ERROR {name}: IDLE applied, no idle_errors left",
          idle70 and not r.get("idle_errors"), (r.get("idle_errors"), idle70))

# --- NUMBERING: filaments whose number is not their FID (the rig's order) ------
# 0->16, 1->17, 16->0, 17->1, 2->19, 60->48 (other controller), 72->50.
REN = [0, 1, 16, 17, 2, 60, 72]
rig = Rig()
rig.mismatch, rig.no_current = {16}, {16}             # a switch that really did not close
r = make(rig).scan_view(REN, trigger="ext", on_armed=lambda f: rig.edges(3), measure=True, **ARGS)
by = {x["filament"]: x for x in r["rows"]}
check("numbering: the schedule fired YOUR filaments, in YOUR order",
      [e for e in rig.log if e[0] == "download"][0][1] == REN and r["fired"] == len(REN),
      ([e for e in rig.log if e[0] == "download"], r.get("fired")))
check("numbering: every row's board is its FID's hardware place",
      all(x["board"] == "P{controller} CH{ch}.{pos}".format(
          controller=site(ORDER[x["filament"]])["controller"],
          ch=site(ORDER[x["filament"]])["channel"] + 1,
          pos=site(ORDER[x["filament"]])["position"] + 1) for x in r["rows"]),
      [(x["filament"], x["board"]) for x in r["rows"]])
check("numbering: every row has ITS OWN pulse-log records (heating snapshot, checks)",
      all(len(x["records"]) == 3 and all(rec["filament"] == x["filament"] for rec in x["records"])
          and not x.get("records_short") for x in r["rows"]),
      [(x["filament"], len(x["records"])) for x in r["rows"]])
check("numbering: the stuck switch is reported on #16 and nowhere else",
      [x["filament"] for x in r["rows"] if x.get("on_mismatch")] == [16] and not r["ok"],
      [(x["filament"], x.get("on_mismatch")) for x in r["rows"]])
check("numbering: VOLTAGE went to each filament at ITS voltage",
      all(e[3] == MV[e[1]] for e in rig.log if e[0] == "state" and e[2] == VOLTAGE)
      and {e[1] for e in rig.log if e[0] == "state" and e[2] == VOLTAGE} == set(REN),
      [e for e in rig.log if e[0] == "state" and e[2] == VOLTAGE])
check("numbering: all ended at IDLE", not rig.raised, rig.raised)
rig = Rig(dead=[16])
r = make(rig).scan_view([16, 0], **ARGS)
check("numbering: dead #16 (FID 0) gets the fake turn and no schedule row, #0 (FID 16) fires",
      [e for e in rig.log if e[0] == "download"][0][1] == [0] and r["rows"][0].get("fake_armed")
      and "dead" in r["rows"][0]["skipped"] and r["rows"][1]["fired"] == 3 and 16 not in rig.counted,
      ([e for e in rig.log if e[0] == "download"], [(x["filament"], x.get("skipped")) for x in r["rows"]]))
rig = Rig(dead=[60, 61])
r = make(rig).scan_view([60, 61], **ARGS)
check("every filament dead: refused, nothing touched", not r["ok"] and "dead" in (r.get("error") or "")
      and not rig.log, r.get("error"))

rig = Rig()
rig.log_unreadable = True                             # the pulse log could not be read
r = make(rig).scan_view([20, 21], trigger="ext", on_armed=lambda f: rig.edges(3), **ARGS)
check("no pulse-log records -> records_short, not ok",
      not r["ok"] and all(x.get("records_short") for x in r["rows"]), r["rows"][0].get("records_short"))

# --- the pulse log is read DURING the run, not all after the last pulse ------
# (2026-10-30: 660 records = 75 pages read after the last pulse, 3.3 s of tail)
FL = [20, 21, 22, 23, 60, 61, 62, 63]
rig = Rig(hot_after={f: 3.0 for f in FL})         # ~3 s to hot, as on the rig (test7 3-6 s)
r = make(rig).scan_view(FL, trigger="ext", on_armed=lambda f: setattr(rig, "pending", 20),
                        **{**ARGS, "num_pulses": 20})
during = sum(1 for x in rig.log_reads if x[3])
after = sum(1 for x in rig.log_reads if not x[3])
check("pulse log: most pages read during the run, the rest at the end",
      during >= 10 and after <= 8 and r["ok"], (during, after, r.get("stopped")))
check("pulse log: no empty page read during the run",
      all(x[2] > 0 for x in rig.log_reads if x[3]), [x for x in rig.log_reads if x[3] and not x[2]])
check("pulse log: every row still has exactly its own 20 records",
      all(len(x["records"]) == 20 and all(q["filament"] == x["filament"] for q in x["records"])
          for x in r["rows"]), [(x["filament"], len(x["records"])) for x in r["rows"]])

rig = Rig(hv=False)
r = make(rig).scan_view([60, 61], **ARGS)
check("rails off at the start: not refused (operator's job)", r["ok"] and r["fired"] == 2, r.get("error"))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
