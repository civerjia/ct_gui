#!/usr/bin/env python3
"""The remote CTClient (ct/client/_remote.py) against a real backend process
started here on a spare port, with no hardware. Covers what scripts rely on:
results come back as the same types, errors as the same exceptions, a
crashed script's `with` blocks are closed by the backend, and a backend
restart keeps the script's filament numbering and watchdog keepalive.

    python3 tests/test_remote_client.py          (~45 s: includes a crash wait)
"""

import _path  # noqa: F401

import os
import signal
import subprocess
import sys
import time

import requests

PORT = 8796
BASE = f"http://127.0.0.1:{PORT}"
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV = dict(os.environ, CT_NO_AUTO_UPDATE="1", CT_NO_AUTO_CONNECT="1", CT_SLEW_KEEP="0",
           CT_GUI_PORT=str(PORT), CT_GUI_HOST="127.0.0.1")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""), flush=True)
    if not cond:
        FAILS.append(name)


def start_backend():
    p = subprocess.Popen([sys.executable, "backend.py"], cwd=HERE, env=ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            requests.get(BASE + "/api/version", timeout=1)
            return p
        except Exception:
            time.sleep(0.25)
    raise SystemExit("backend did not start")


def stop_backend(p):
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        p.kill()


os.environ.update(CT_NO_AUTO_UPDATE="1")
backend = start_backend()
try:
    from ct_simple_control import CTClient, CTLeaseError, Result

    ct = CTClient("127.0.0.1", port=PORT, client_id="remote-test")
    check("CTClient(...) is the remote proxy", type(ct).__name__ == "RemoteCTClient", repr(ct))
    st = ct.status()
    check("a result comes back as Result", isinstance(st, Result))
    check("a property comes back with its type", isinstance(ct.dead, frozenset))
    check("a class constant is local", ct.SLEW_CEILINGS == CTClient.SLEW_CEILINGS)
    check("int dict keys survive", isinstance(ct.filament_order, dict))

    other = CTClient("127.0.0.1", port=PORT, client_id="other")
    with ct.lease(ttl=30, note="t") as held:
        check("`with` returns the proxy itself", held is ct)
        try:
            with other.lease(ttl=30):
                check("second lease refused", False)
        except CTLeaseError:
            check("second lease refused with CTLeaseError", True)
    check("lease released at the end of the with-block", not ct.status()["lock"]["held"])
    try:
        ct.no_such_method()
        check("missing method raises AttributeError", False)
    except AttributeError:
        check("missing method raises AttributeError", True)

    calls = []
    ct.__dict__["_rp_local_client"] = type("L", (), {"status": lambda self, cb=None: calls.append(cb) or "local"})()
    check("a call given a callable runs locally", ct.status(cb=lambda: None) == "local" and calls)
    ct.__dict__["_rp_local_client"] = None

    # Calls from different threads run concurrently, as on a local client.
    import threading as _th
    long_call = _th.Thread(target=lambda: ct.wait_for_current(0, 1000, timeout_s=6))
    long_call.start()
    time.sleep(1.0)
    t1 = time.time()
    ct.status()
    check("another thread's call is not queued behind a long one", time.time() - t1 < 2,
          f"{time.time() - t1:.1f}s")
    long_call.join()

    # Ctrl-C stops the call in the backend too, not only the local wait.
    import threading as _th
    _th.Timer(1.5, lambda: os.kill(os.getpid(), signal.SIGINT)).start()
    t0 = time.time()
    try:
        ct.wait_for_current(0, 1000, timeout_s=30)        # polls for 30 s without hardware
        check("Ctrl-C interrupts a remote call", False)
    except KeyboardInterrupt:
        check("Ctrl-C interrupts a remote call", time.time() - t0 < 5, f"{time.time() - t0:.1f}s")
    time.sleep(1.0)
    t1 = time.time()
    ct.status()
    check("...and the backend stopped it (the session is free at once)", time.time() - t1 < 2,
          f"{time.time() - t1:.1f}s")

    # A script that dies inside a lease: the backend closes it.
    crash = ("import sys,os; sys.path.insert(0,'.'); from ct_simple_control import CTClient;"
             f"c=CTClient('127.0.0.1', port={PORT}, client_id='crasher');"
             "c.lease(ttl=120, note='crash').__enter__(); os._exit(9)")
    subprocess.run([sys.executable, "-c", crash], cwd=HERE, env=ENV, timeout=30)
    held = requests.get(BASE + "/api/lock").json()["lock"]
    check("the crashed script holds the lease at first", held.get("owner") == "crasher", str(held))
    t0 = time.time()
    while time.time() - t0 < 40 and requests.get(BASE + "/api/lock").json()["lock"]["held"]:
        time.sleep(1)
    check("the backend released it within ~20 s (its ttl was 120 s)", time.time() - t0 < 30,
          f"{time.time() - t0:.0f}s")

    # Backend restart: numbering and keepalive carried into the new session.
    order = list(range(96)); order[0], order[16] = 16, 0
    ct.set_filament_order(order)
    ct._ensure_keepalive(5)
    stop_backend(backend)
    try:
        ct.status()
        check("backend down -> CTConnectionError", False)
    except Exception as e:
        check("backend down -> CTConnectionError", type(e).__name__ == "CTConnectionError", type(e).__name__)
    backend = start_backend()
    check("after a backend restart the same proxy works", isinstance(ct.status(), Result))
    check("...with the script's filament numbering, not the backend's reset one",
          ct._fid_of(0) == 16 and requests.get(BASE + "/api/filament-order").json()["order"][0] == 0)
    check("...and the watchdog keepalive resumed", ct._rp_state["keepalive"])
finally:
    stop_backend(backend)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
