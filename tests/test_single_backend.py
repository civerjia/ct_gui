#!/usr/bin/env python3
"""Only one backend per port. A second one started while the first runs must
exit at once, say why, and touch nothing (no update, no watchdog, no "backend
start" line) -- 2026-10-05 11:06 a second backend updated the files under a
running one and logged that it had started.

    python3 tests/test_single_backend.py
"""
import _path  # noqa: F401

import json
import os
import subprocess
import sys
import time
import urllib.request

PORT = 8797
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, CT_NO_AUTO_UPDATE="1", CT_NO_AUTO_CONNECT="1", CT_SLEW_KEEP="0",
           CT_GUI_PORT=str(PORT), CT_GUI_HOST="127.0.0.1")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def version():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/version", timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


first = subprocess.Popen([sys.executable, "backend.py"], cwd=HERE, env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        if version():
            break
        time.sleep(0.25)
    check("first backend serves", bool(version()))
    t0 = time.monotonic()
    second = subprocess.run([sys.executable, "backend.py"], cwd=HERE, env=ENV,
                            capture_output=True, text=True, timeout=30)
    dt = time.monotonic() - t0
    check("second backend exits with an error", second.returncode == 1, str(second.returncode))
    check("...at once, not after the bind retries", dt < 8, f"{dt:.1f}s")
    check("...saying another backend has the port",
          "already running on port" in second.stderr, second.stderr[-300:])
    check("...and never claims to have started", "listening on" not in second.stdout + second.stderr)
    check("first backend still serves", bool(version()))
finally:
    first.terminate()
    try:
        first.wait(10)
    except subprocess.TimeoutExpired:
        first.kill()

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
