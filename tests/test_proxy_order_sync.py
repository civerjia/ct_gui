#!/usr/bin/env python3
"""A proxy CTClient's local helper uses the same filament order as its session.

2026-10-08, combined_backend: one CTClient (a proxy). active_one() ran in the
backend session; fire_single_pulse(on_armed=...) carries a callable, so the
proxy ran it on a LOCAL client -- which had read the filament order once,
before the order was changed. Pre-heat and fire then named different
filaments (60 vs 61). The proxy now re-reads the backend's order into the local
client before every call it runs locally. Real backend, no hardware.

    python3 tests/test_proxy_order_sync.py
"""
import _path  # noqa: F401

import os
import signal
import subprocess
import sys
import time

from ct import _http as requests

PORT = 8795
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, CT_NO_AUTO_UPDATE="1", CT_NO_AUTO_CONNECT="1", CT_SLEW_KEEP="0",
           CT_GUI_PORT=str(PORT), CT_GUI_HOST="127.0.0.1")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


p = subprocess.Popen([sys.executable, "backend.py"], cwd=HERE, env=ENV,
                     stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
for _ in range(60):
    try:
        requests.get(BASE + "/api/version", timeout=1)
        break
    except Exception:
        time.sleep(0.25)
try:
    os.environ.update(CT_NO_AUTO_UPDATE="1")
    from ct_simple_control import CTClient

    OLD = list(range(96))
    OLD[48:59], OLD[59:72] = list(range(61, 72)), list(range(48, 61))     # 48 -> 61
    NEW = list(range(96))
    NEW[48:60], NEW[60:72] = list(range(60, 72)), list(range(48, 60))     # 48 -> 60

    ct = CTClient("127.0.0.1", port=PORT, client_id="proxy-order-test")
    ct.set_filament_order(OLD)
    local = ct._rp_local()
    check("local helper starts on the old order (48 -> 61)", local._fid_of(48) == 61, local._fid_of(48))

    ct.set_filament_order(NEW)               # runs in the session
    check("session now on the new order", ct.get_filament_order()[48] == 60)

    armed = []
    r = ct.fire_single_pulse(48, width_us=1000, inter_pulse_ms=300, total_ms=2000,
                             shot_wait_timeout_s=1.0, on_armed=lambda: armed.append(1))
    check("the local call ran on the new order (48 -> 60)", local._fid_of(48) == 60, local._fid_of(48))
    check("...and was not refused as stale", "filament order changed" not in str(r.get("error")), r.get("error"))
finally:
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
