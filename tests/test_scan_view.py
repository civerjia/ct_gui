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

    def post(path, body, timeout=None):
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
r = ct.scan_view(V, active_mv={**{f: 8000 for f in V}, 60: 7800}, idle_ma=1200, settle_ms=500)
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
check("disarmed at the end", log[-1][0] in ("disarm", "settle") and any(e[0] == "disarm" for e in log[-3:]))

# Not arriving stops the view and fires nothing on it; everything raised dropped.
ct, log, st = make(never_arrives={5})
r = ct.scan_view(V, active_mv=8000, idle_ma=1200, heat_timeout_s=0.0)
check("non-arrival stops the view", not r["ok"] and "filament 5" in r["error"] and r["fired"] == 0
      or (not r["ok"] and "filament" in r["error"]), r.get("error"))
ct, log, st = make(never_arrives={5})
ct._sleep_polls = 0
r = ct.scan_view(V, active_mv=8000, idle_ma=1200)
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

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
