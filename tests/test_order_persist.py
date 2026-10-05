#!/usr/bin/env python3
"""The filament order (USER_INDEX -> FID) survives a backend restart, like the
dead mask -- but only onto the wiring it was set against. Real backends,
started and stopped, on a scratch state directory.

    python3 tests/test_order_persist.py
"""
import _path  # noqa: F401

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PORT = 8798
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = Path(os.environ["CT_STATE_DIR"])
ENV = dict(os.environ, CT_NO_AUTO_UPDATE="1", CT_NO_AUTO_CONNECT="1", CT_SLEW_KEEP="0",
           CT_GUI_PORT=str(PORT), CT_GUI_HOST="127.0.0.1")
BASE = f"http://127.0.0.1:{PORT}"
FAILS = []
ORDER = list(range(96))
ORDER[52], ORDER[63] = ORDER[63], ORDER[52]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def call(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", "X-CT-Client": "test"})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read())


def start():
    p = subprocess.Popen([sys.executable, "backend.py"], cwd=HERE, env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    for _ in range(80):
        try:
            call("/api/version")
            return p
        except Exception:
            time.sleep(0.25)
    raise SystemExit("backend did not start")


def stop(p):
    p.terminate()
    try:
        p.wait(10)
    except subprocess.TimeoutExpired:
        p.kill()


p = start()
try:
    r = call("/api/filament-order", {"order": ORDER})
    check("order set", r.get("ok") and not r.get("identity"), str(r)[:200])
    saved = json.loads((STATE / "filament_order.json").read_text())
    check("saved to disk with the wiring it was set for",
          saved.get("order") == ORDER and saved.get("mapping_fingerprint"), str(saved)[:200])
finally:
    stop(p)

p = start()
try:
    r = call("/api/filament-order")
    check("restored after a restart", r.get("order") == ORDER and not r.get("identity"), str(r)[:200])
    check("...and says it came from disk", "restored from disk" in (r.get("set_by") or ""), r.get("set_by"))
    check("...on unchanged wiring", r.get("mapping_changed") is False, str(r.get("mapping_changed")))
finally:
    stop(p)

# Another wiring: the saved fingerprint no longer matches.
saved = json.loads((STATE / "filament_order.json").read_text())
saved["mapping_fingerprint"] = "000000000000"
(STATE / "filament_order.json").write_text(json.dumps(saved))
p = start()
try:
    r = call("/api/filament-order")
    check("NOT restored onto a different wiring -- identity instead", r.get("identity") is True, str(r)[:200])
    r = call("/api/filament-order", {"order": None})
    check("clearing works", r.get("identity") is True)
finally:
    stop(p)
p = start()
try:
    r = call("/api/filament-order")
    check("a cleared order stays cleared after a restart", r.get("identity") is True, str(r)[:200])
finally:
    stop(p)

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
