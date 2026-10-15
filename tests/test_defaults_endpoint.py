#!/usr/bin/env python3
"""GET /api/defaults: the GUI's only source for the IDLE default (ct/defaults.py),
overridable with CT_IDLE_MA on the backend (2026-10-15). Real backend on spare
local ports, no hardware.

    python3 tests/test_defaults_endpoint.py
"""
import _path  # noqa: F401

import os
import signal
import subprocess
import sys
import time

from ct import _http as requests

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def defaults_from_backend(port, extra_env):
    env = dict(os.environ, CT_NO_AUTO_UPDATE="1", CT_NO_AUTO_CONNECT="1", CT_SLEW_KEEP="0",
               CT_AUTO_UPDATE="0", CT_GUI_PORT=str(port), CT_GUI_HOST="127.0.0.1", **extra_env)
    p = subprocess.Popen([sys.executable, "backend.py"], cwd=HERE, env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    try:
        for _ in range(60):
            try:
                return requests.get(f"http://127.0.0.1:{port}/api/defaults", timeout=1).json()
            except Exception:
                time.sleep(0.25)
        return None
    finally:
        p.send_signal(signal.SIGTERM)
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()


d = defaults_from_backend(8798, {})
check("default IDLE 1200 mA, retired 1500 listed", d and d["ok"] and d["idle_ma"] == 1200
      and 1500 in d["retired_idle_ma"], d)
d = defaults_from_backend(8799, {"CT_IDLE_MA": "1300"})
check("CT_IDLE_MA=1300 on the backend -> the GUI gets 1300", d and d["idle_ma"] == 1300, d)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
