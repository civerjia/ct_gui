#!/usr/bin/env python3
"""Backend: a TIMED VOLTAGE(6) (active_s) is heating without the CC loop and
gets ACTIVE's guards (2026-10-14): range vs the rig's voltage cap, IDLE-first
ladder guard, single-filament only; an untimed VOLTAGE is unchanged. Real
backend process on a spare port, no hardware.

    python3 tests/test_timed_voltage_backend.py
"""
import _path  # noqa: F401

import os
import signal
import subprocess
import sys
import time

from ct import _http as requests

PORT = 8797
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, CT_NO_AUTO_UPDATE="1", CT_NO_AUTO_CONNECT="1", CT_SLEW_KEEP="0",
           CT_GUI_PORT=str(PORT), CT_GUI_HOST="127.0.0.1", CT_AUTO_UPDATE="0")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


p = subprocess.Popen([sys.executable, "backend.py"], cwd=HERE, env=ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
try:
    for _ in range(60):
        try:
            requests.get(BASE + "/api/version", timeout=1)
            break
        except Exception:
            time.sleep(0.25)

    def fs(**body):
        return requests.post(BASE + "/api/filament-state", json=body,
                             headers={"X-CT-Client": "tv-test"}, timeout=10).json()

    r = fs(filament=5, state=6, arg=10000, active_s=5, then_idle_ma=1200)
    check("timed VOLTAGE above the 9.5 V cap (10 V) refused (firmware would clamp silently)",
          not r["ok"] and "out of range" in r["error"] and "9500" in r["error"], r)
    r = fs(filament=5, state=6, arg=500, active_s=5, then_idle_ma=1200)
    check("timed VOLTAGE below the 0.8 V floor refused", not r["ok"] and "out of range" in r["error"], r)
    r = fs(filament=5, state=6, arg=7600, active_s=5, then_idle_ma=1200)
    check("timed VOLTAGE from an unknown state: ladder-blocked like ACTIVE",
          not r["ok"] and r.get("ladder_blocked") and "timed VOLTAGE" in r["error"], r)
    r = fs(filament=5, state=6, arg=7600)
    check("untimed VOLTAGE: not ladder-blocked (debug setting as before)",
          not r.get("ladder_blocked") and "out of range" not in str(r.get("error")), r)
    r = fs(filament=5, state=4, arg=1200, active_s=5)
    check("active_s on IDLE still refused, names both states",
          not r["ok"] and "ACTIVE(5) or VOLTAGE(6)" in r["error"], r)
    r = requests.post(BASE + "/api/filament-prep",
                      json={"state": 6, "arg": 7600, "filaments": [5, 6], "active_s": 5,
                            "then_idle_ma": 1200},
                      headers={"X-CT-Client": "tv-test"}, timeout=10).json()
    check("batch timed VOLTAGE refused (single-filament only)",
          not r["ok"] and "single-filament" in str(r.get("error")), r)
finally:
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
