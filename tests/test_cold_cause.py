#!/usr/bin/env python3
"""An ACTIVE refused as "not warm yet" says WHY the filament is cold.

2026-10-08: filament 8 was refused ACTIVE three times ("not warm yet, CC loop
ramping") and the firmware called it "open" -- but its output sat at 536 mV:
the voltage had never been applied. The refusal now carries the board's own
reading, from the board monitor: output never came up / output up but no
current (open) / board not present.

    python3 tests/test_cold_cause.py
"""
import _path  # noqa: F401

import json
import os
import threading
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def rows_with(**board):
    rows = [{"channel": ch, "mux_port": m, "label": f"CH{ch + 1}.{m + 1}", "present": True,
             "bus_mV": 5000, "current_mA": 1200, "oe": True, "tps_fault": False,
             "iso_enabled": True} for ch in range(8) for m in range(8)]
    rows[8].update(board)          # channel 1, port 0 = CH2.1
    return rows


S.filament_to_board = lambda f: (0, 1, 0, None)
cases = [
    ("never came up", dict(bus_mV=536, current_mA=2, oe=False), "never came up", "536 mV"),
    ("open", dict(bus_mV=6200, current_mA=3), "OPEN", "6200 mV"),
    ("not present", dict(present=False, bus_mV=None, current_mA=None), "not present", "P1 CH2.1"),
    ("warming normally", dict(bus_mV=3000, current_mA=900), None, None),
]
for name, board, want, also in cases:
    S.monitor_board_rows = lambda cid, b=board: (rows_with(**b), {"fresh": True})
    row, cause = S._cold_board_cause(8)
    if want is None:
        check(f"{name}: no cause added", cause == "", cause)
    else:
        check(f"{name}: '{want}' named, with the reading", want in cause and also in cause, cause)
S.monitor_board_rows = lambda cid: (rows_with(bus_mV=536, current_mA=2, oe=False), {"fresh": True})
check("oe off is named", "TPS output enable OFF" in S._cold_board_cause(8)[1], S._cold_board_cause(8)[1])
S.monitor_board_rows = lambda cid: (rows_with(bus_mV=536, current_mA=2), {"fresh": False})
check("stale monitor data -> no claim", S._cold_board_cause(8)[1] == "")

# Through the endpoint: the refusal carries the cause and the board reading.
S.monitor_board_rows = lambda cid: (rows_with(bus_mV=536, current_mA=2, oe=False), {"fresh": True})
S.ladder_blocks_active = lambda *a, **k: "not warm yet: at IDLE for 932 s, CC loop 'ramping', no current reading."
srv = ThreadingHTTPServer(("127.0.0.1", 0), S.CtHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/filament-state",
                             data=json.dumps({"filament": 8, "state": 5, "arg": 2700}).encode(),
                             headers={"Content-Type": "application/json", "X-CT-Client": "t"})
r = json.loads(urllib.request.urlopen(req, timeout=10).read())
srv.shutdown()
check("refusal names the cause", "never came up" in r.get("error", "") and "not warm yet" in r.get("error", ""), r)
check("...and returns the board reading", (r.get("board") or {}).get("bus_mV") == 536, r.get("board"))

# A refusal for another reason (not at IDLE) still reports the measured V/I.
S.monitor_board_rows = lambda cid: (rows_with(bus_mV=812, current_mA=0), {"fresh": True})
S.ladder_blocks_active = lambda *a, **k: "currently at STANDBY(3); ACTIVE may only be entered from IDLE(4)"
srv = ThreadingHTTPServer(("127.0.0.1", 0), S.CtHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/filament-state",
                             data=json.dumps({"filament": 8, "state": 5, "arg": 2700}).encode(),
                             headers={"Content-Type": "application/json", "X-CT-Client": "t"})
r = json.loads(urllib.request.urlopen(req, timeout=10).read())
srv.shutdown()
check("STANDBY refusal also reports measured V/I",
      "currently at STANDBY" in r["error"] and "measured P1 CH2.1: 812 mV, 0 mA" in r["error"], r["error"])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
