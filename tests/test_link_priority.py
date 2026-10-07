#!/usr/bin/env python3
"""Fire set-up is not held up by background reads, offline:
- the per-link request lock lets a normal request go ahead of every waiting
  low-priority one (board monitor, PING), and a low one still runs when
  nothing else wants the link;
- the board monitor sends nothing while a schedule op holds the link, and the
  last snapshot stays servable through the hold;
- /api/disarm runs the boards in parallel (one slow board no longer delays
  the other) and honours a `controllers` subset;
- /api/arm with `controllers` arms only those, master last, and checks the
  trigger delay once.

    python3 tests/test_link_priority.py
"""

import _path  # noqa: F401

import json
import os
import threading
import time
import urllib.request
from http.server import ThreadingHTTPServer

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

from ct.protocol import _PriorityLock, mark_low_priority  # noqa: E402
import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# --- the priority lock ---------------------------------------------------------
lock = _PriorityLock()
order = []
holder_in = threading.Event()
release_holder = threading.Event()


def holder():
    with lock:
        holder_in.set()
        release_holder.wait(2)


def low(tag):
    mark_low_priority()
    with lock:
        order.append(tag)
        time.sleep(0.01)


def normal(tag):
    with lock:
        order.append(tag)


threading.Thread(target=holder).start()
holder_in.wait(1)
lows = [threading.Thread(target=low, args=(f"low{i}",)) for i in range(3)]
for t in lows:
    t.start()
time.sleep(0.05)                       # the low ones are queued first...
n = threading.Thread(target=normal, args=("CORE",))
n.start()
time.sleep(0.05)
release_holder.set()                   # ...but the core command goes next
for t in lows + [n]:
    t.join(2)
check("a core request goes ahead of queued background reads",
      order and order[0] == "CORE", str(order))
check("the background reads still all run afterwards", sorted(order[1:]) == ["low0", "low1", "low2"],
      str(order))

order.clear()
t = threading.Thread(target=low, args=("alone",))
t.start()
t.join(1)
check("a background read runs when nothing else wants the link", order == ["alone"], str(order))


# --- fake links -----------------------------------------------------------------
class _Cl:
    connected = True


class FakeLink:
    def __init__(self, name, delay):
        self.client = _Cl()
        self.name = name
        self.delay = delay
        self.monitor_hold_until = 0.0
        self.sent = []

    def hold_monitor(self, seconds=S.MONITOR_YIELD_S):
        self.monitor_hold_until = max(self.monitor_hold_until, time.monotonic() + seconds)

    def request(self, ft, payload=b"", flags=0, timeout=2.0):
        self.sent.append(ft)
        time.sleep(self.delay)
        return {"status_code": 0, "raw": [0, 0, 0, 0, 0]}


# --- monitor yields while a schedule op holds the link -------------------------
lk = FakeLink("P2", 0.0)
prev = {"boards": {(0, 0): {"known": True}}, "boards_at": time.monotonic(), "bitmaps": {},
        "bitmaps_at": 0.0, "status": None, "status_at": 0.0}
lk.hold_monitor(1.0)
snap = S._board_monitor_tick(2, lk, time.monotonic(), prev)
check("monitor sends nothing while the link is held", lk.sent == [], str(lk.sent))
check("...and marks the snapshot as held", snap.get("held_until", 0) > time.monotonic())

# --- /api/disarm in parallel, /api/arm subset -----------------------------------
S.CONTROLLERS.clear()
S.CONTROLLERS[1] = FakeLink("P1", 0.4)
S.CONTROLLERS[2] = FakeLink("P2", 0.4)
S.note_grid_commanded = lambda *a, **k: None
delay_checks = []
S.trigger_delay_mismatch = lambda: delay_checks.append(1) or None
srv = ThreadingHTTPServer(("127.0.0.1", 0), S.CtHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(path, body):
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}",
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", "X-CT-Client": "test"})
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


t0 = time.monotonic()
r = post("/api/disarm", {})
dt = time.monotonic() - t0
check("disarm of two boards takes one board's time, not two", dt < 0.7, f"{dt:.2f}s")
check("disarm ok and both answered", r.get("ok") and set(r["results"]) == {"1", "2"}, str(r))
for l in S.CONTROLLERS.values():
    l.sent.clear()
r = post("/api/disarm", {"controllers": [2]})
check("disarm subset touches only that board",
      S.CONTROLLERS[2].sent and not S.CONTROLLERS[1].sent, str(r))

for l in S.CONTROLLERS.values():
    l.sent.clear()
S.MASTER = 1
S.LOADED_EMIT_FIDS.clear()
r = post("/api/arm", {"repeats": 1, "controllers": [2, 1]})
check("arm subset: both armed, master last", r.get("ok") and r.get("order") == [2, 1], str(r))
check("trigger delay checked once for the whole arm", len(delay_checks) == 1, str(delay_checks))
for l in S.CONTROLLERS.values():
    l.sent.clear()
r = post("/api/arm", {"repeats": 1, "controllers": [2]})
check("arm of one board leaves the other alone",
      r.get("order") == [2] and not S.CONTROLLERS[1].sent, str(r))
r = post("/api/arm", {"repeats": 1, "controllers": [3]})
check("arm naming a board that is not connected arms nothing", not r.get("ok")
      and "not connected" in (r.get("error") or ""), str(r))

# Failures say which board and why -- they were logged as "FAILED: failed".
class Dead(FakeLink):
    def request(self, *a, **k):
        raise TimeoutError("Timed out waiting for response to 0x78")


S.CONTROLLERS[1] = Dead("P1", 0)
r = post("/api/disarm", {"controllers": [1]})
check("a failed disarm names the board and the reason",
      not r["ok"] and "Power 1" in (r.get("error") or "") and "0x78" in r["error"], str(r))
S.CONTROLLERS[1] = FakeLink("P1", 0)
real_dl = S.download_to_controller
S.download_to_controller = lambda link, c0, plan, ch, **k: (
    {"controller": c0, "ok": False, "error": "Power 1: 1 of 6 frames not accepted (config: no answer)"}
    if c0 == 0 else {"controller": c0, "ok": True})
r = post("/api/download", {"plan": {"emission": [{"filament": 1, "numPulses": 1, "widthUs": 100}],
                                    "config": {}, "heating": []}})
S.download_to_controller = real_dl
check("a failed download says which controller and which frame",
      not r["ok"] and "Power 1" in (r.get("error") or "") and "config" in r["error"], str(r))
srv.shutdown()

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
