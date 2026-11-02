#!/usr/bin/env python3
"""Incident evidence: an HV-guard incident keeps every reading around it at
full resolution in logs/incidents/, beyond the 6 h raw-series limit.

    python3 tests/test_incident_evidence.py
"""
import _path  # noqa: F401

import csv
import json
import os
import tempfile
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"
os.environ.setdefault("CT_STATE_DIR", tempfile.mkdtemp())

import ct.server._server as S  # noqa: E402
import ct.server._safety as SF  # noqa: E402
from pathlib import Path  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp())
S.INCIDENT_DIR = tmp / "incidents"
S.SERIES_DIR = tmp / "series"
S.ARC_LOG_PATH = tmp / "arcs.jsonl"
S.LOG_DIR = tmp
S.CLIENT_LOG_DIR = tmp / "client"
S.CLIENT_LOG_DIR.mkdir()
for d in S._SERIES.values():
    d.clear()
S._SERIES_PENDING.clear()
S._POWER_RING.clear()
S._INCIDENT_OPEN.clear()
S._INCIDENT_DONE.clear()

now = time.time()
T = float(int(now) - 200)                            # the incident, 200 s ago
for k in range(int(S.INCIDENT_PRE_S) + 400):         # 4 Hz Spellman, 1 Hz focus
    t = T - S.INCIDENT_PRE_S - 100 + k
    for q in range(4):
        S.series_add("sp_ma", 1.05 if T - 6 <= t + q / 4 <= T else 0.01, t + q / 4)
    S.series_add("fo_v", -134.0 if T - 5 <= t <= T else -203.0, t)
boards = {(0, 1): {"present": True, "power_state": 6, "current_mA": 2458, "bus_mV": 8368, "age_ms": 12},
          (0, 2): {"present": True, "power_state": 4, "current_mA": None, "bus_mV": 2400, "age_ms": None},
          (0, 3): {"present": False}}
S.power_ring_add(1, boards, "cache+push(run)", T - 1.0)
S.power_ring_add(1, boards, "cache", T - S.INCIDENT_PRE_S - 50)     # before the window

ts = lambda t: S.datetime.datetime.fromtimestamp(t).strftime("%Y-%m-%d %H:%M:%S")   # noqa: E731
(tmp / "backend.log").write_text(
    f"{ts(T - S.INCIDENT_PRE_S - 30)} INFO  too early\n"
    f"{ts(T)} ERROR HV guard TRIPPED (Spellman over_current)\nTraceback line without a time\n"
    f"{ts(T + 30 + S.INCIDENT_POST_S + 30)} INFO  too late\n")
iso = lambda t: S.datetime.datetime.fromtimestamp(t).isoformat(timespec="milliseconds")   # noqa: E731
(S.CLIENT_LOG_DIR / "ct_client_x.jsonl").write_text(
    json.dumps({"t": iso(T - 2), "method": "read_ads_all", "result": {"focus_v": -134}}) + "\n"
    + json.dumps({"t": iso(T + 30 + S.INCIDENT_POST_S + 5), "method": "late"}) + "\n")
S.ARC_LOG_PATH.write_text(json.dumps({"kind": "arc", "kv_before": 45, "at": S.datetime.datetime.fromtimestamp(T - 3)
                                      .astimezone().isoformat(timespec="milliseconds")}) + "\n")

check("an ordinary arc is not an incident", not S.incident_note("arc", {}, T))
check("a timed-active return is not an incident", not S.incident_note("timed_active_to_idle", {}, T))
check("a guard trip that recovers by itself is not (auto_recover)",
      not S.incident_note("hv_guard_tripped", {"auto_recover": True}, T))
check("a recovered guard action is not", not S.incident_note("hv_guard_recovered", {}, T))
check("a stray-closed switch that was opened is not", not S.incident_note("grid_stray_closed", {"opened": True}, T))
check("a Spellman big arc is not (it recovers by itself)", not S.incident_note("spellman_big_arc", {}, T))
check("nothing opened by those", not S._INCIDENT_OPEN)
SF._safety_record("hv_guard_tripped", {"source": "spellman_over_current", "ma": 1.0745})
with S._INCIDENT_LOCK:
    S._INCIDENT_OPEN.update(start=T - S.INCIDENT_PRE_S, first=T, end=T + S.INCIDENT_POST_S)
    S._INCIDENT_OPEN["events"][0]["unix_t"] = T
check("_safety_record opened a capture through the hook", S._INCIDENT_OPEN.get("events")
      and S._INCIDENT_OPEN["events"][0]["kind"] == "hv_guard_tripped")
S.incident_note("grid_stray_closed", {"opened": False, "why": "second one"}, T + 30)
check("a second incident inside the window joins the same capture",
      len(S._INCIDENT_OPEN["events"]) == 2 and S._INCIDENT_OPEN["end"] == T + 30 + S.INCIDENT_POST_S)

check("nothing written while the window is still open", S.incident_flush(T + 60) == [])
out = S.incident_flush(T + 30 + S.INCIDENT_POST_S + 1)
check("written once the window has passed", len(out) == 1, out)
d = out[0] if out else tmp
check("...named after the time and the kinds", d.name.endswith("grid_stray_closed+hv_guard_tripped"), d.name)
ev = [json.loads(ln) for ln in (d / "incident.jsonl").read_text().splitlines()]
check("incident.jsonl: both events", [e["kind"] for e in ev] == ["hv_guard_tripped", "grid_stray_closed"])
rows = list(csv.DictReader(open(d / "series.csv")))
sp = [r for r in rows if r["name"] == "sp_ma"]
lo, hi = T - S.INCIDENT_PRE_S, T + 30 + S.INCIDENT_POST_S
check("series.csv: full rate, nothing thinned, only the window",
      sp and all(lo <= float(r["unix_t"]) <= hi for r in rows)
      and len(sp) == sum(1 for t, _v in S._SERIES["sp_ma"] if lo <= t <= hi), len(sp))
check("...sub-second stamps kept", any(r["unix_t"].endswith(".250") for r in sp))
check("...the focus sag is in it", any(r["name"] == "fo_v" and float(r["value"]) == -134.0 for r in rows))
pw = list(csv.DictReader(open(d / "power.csv")))
check("power.csv: the in-window reading, present boards only", len(pw) == 2
      and {r["mux"] for r in pw} == {"1", "2"}, pw)
check("...an absent value stays empty, never 0", [r for r in pw if r["mux"] == "2"][0]["current_mA"] == "")
blog = (d / "backend.log").read_text()
check("backend.log: the window's lines + their continuation, not the others",
      "TRIPPED" in blog and "Traceback line" in blog and "too early" not in blog and "too late" not in blog, blog)
cl = (d / "client.jsonl").read_text()
check("client.jsonl: the window's calls only", "read_ads_all" in cl and "late" not in cl, cl)
check("arcs.jsonl: the arc 3 s before", '"kv_before": 45' in (d / "arcs.jsonl").read_text())
check("nothing left to write", S.incident_flush(now + 3600) == [])

S._spellman_events([{"kind": "big_arc", "t": time.monotonic(), "kv_before": 45, "min_kv": 1}], time.monotonic())
check("a Spellman big arc opens no capture", not S._INCIDENT_OPEN)
S.incident_note("fallback_unreachable", {"filaments": [3]})
forced = S.incident_flush(force=True)
check("shutdown writes an open capture early rather than losing it", len(forced) == 1, forced)

# Every board-monitor reading also on disk for a week, gzipped after its hour.
S.POWER_DIR = tmp / "power"
S._POWER_PENDING.clear()
h0 = float(int(now // 3600) * 3600)
S.power_ring_add(1, boards, "cache", h0 - 7200 + 5)          # two hours ago
S.power_ring_add(1, boards, "cache", h0 + 1)                 # this hour
S.power_ring_add(1, boards, "cache", h0 - 8 * 24 * 3600 + 5)  # 8 days ago
check("power: every reading written", S.power_flush() == 3)
files = sorted(p.name for p in S.POWER_DIR.iterdir())
check("...one file per hour", len(files) == 3, files)
old_rows = (S.POWER_DIR / S._power_path(h0 - 7200 + 5).name).read_text().splitlines()
check("...header + one row per present board", old_rows[0] == S.POWER_CSV_HEADER.strip() and len(old_rows) == 3
      and old_rows[1].split(",")[5:9] == ["6", "2458", "8368", "12"], old_rows)
done = S.power_compact(h0 + 30)
names = sorted(p.name for p in S.POWER_DIR.iterdir())
check("power compact: older than a week deleted, finished hour gzipped, this hour left",
      len(done["deleted"]) == 1 and len(done["gzipped"]) == 1
      and names == sorted([S._power_path(h0 - 7200 + 5).name + ".gz", S._power_path(h0 + 1).name]), (done, names))
import gzip  # noqa: E402
gz = [p for p in S.POWER_DIR.iterdir() if p.suffix == ".gz"]
check("...the gzip holds the same rows", gz and gzip.open(gz[0], "rt").read().splitlines() == old_rows)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
