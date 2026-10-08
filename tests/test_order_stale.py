#!/usr/bin/env python3
"""A command sent under a filament order that has since changed is refused.

2026-10-08: one CTClient set a new filament order while another kept the
snapshot it had read earlier. The client pre-heated USER_INDEX 48 through one
(-> filament 60) and fired it through the other (-> filament 61), so for over
an hour every shot in 48-60 heated one filament and fired its neighbour. Now
the order carries a revision; each POST sends the token it was translated
with, and the backend refuses a stale one (and hands back the new order).

Also: a filament order that cannot be SAVED is not applied (it used to be
applied in memory and reported as failed), and the save retries a rename that
OneDrive is holding.

    python3 tests/test_order_stale.py
"""
import _path  # noqa: F401

import os
import threading
from http.server import ThreadingHTTPServer

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402
from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


srv = ThreadingHTTPServer(("127.0.0.1", 0), S.CtHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_port


def client(name):
    return CTClient("127.0.0.1", port=PORT, client_id=name, keepalive=False, _local=True)


OLD = list(range(96))
OLD[48:59], OLD[59:72] = list(range(61, 72)), list(range(48, 61))     # 48 -> 61
NEW = list(range(96))
NEW[48:60], NEW[60:72] = list(range(60, 72)), list(range(48, 60))     # 48 -> 60

a, b = client("gui"), client("worker")
a.set_filament_order(OLD)
check("both clients start on the same order",
      a._fid_of(48) == 61 and b._fid_of(48) == 61, (a._fid_of(48), b._fid_of(48)))

# a changes the order; b still holds the old snapshot
a.set_filament_order(NEW)
check("a now maps 48 -> 60", a._fid_of(48) == 60)
check("b still maps 48 -> 61 (stale)", b._fid_of(48) == 61)

# b sends an energising command under the old order -> refused, new order adopted
r = b._post("/api/filament-state", {"filament": int(b._fid_of(48)), "state": 4, "arg": 1300})
check("stale energising command refused", not r.get("ok") and r.get("order_stale"), r)
check("...says the order changed", "filament order changed" in str(r.get("error")), r.get("error"))
check("...and b adopted the new order", b._fid_of(48) == 60, b._fid_of(48))

# the next command from b passes the order check (fails later: no hardware here)
r = b._post("/api/filament-state", {"filament": int(b._fid_of(48)), "state": 4, "arg": 1300})
check("after adopting, the next command is not refused for the order",
      not r.get("order_stale"), r)

# de-energising is never blocked by a stale order
a.set_filament_order(OLD)
r = b._post("/api/filament-state", {"filament": int(b._fid_of(48)), "state": 2, "arg": 0})
check("stale SLEEP still goes through", not r.get("order_stale"), r)
r = b._post("/api/hv-all-off", {})
check("stale hv-all-off still goes through", not r.get("order_stale"), r)

# no header (GUI / curl) -> no check
import urllib.request, json  # noqa: E401,E402
req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/filament-state",
                             data=json.dumps({"filament": 3, "state": 4, "arg": 1300}).encode(),
                             headers={"Content-Type": "application/json", "X-CT-Client": "curl"})
try:
    body = json.loads(urllib.request.urlopen(req, timeout=10).read())
except urllib.error.HTTPError as e:
    body = json.loads(e.read())
check("no X-CT-Order header -> not checked", not body.get("order_stale"), body)

# the SAME order set again (or a backend restart) does not invalidate anyone
a.set_filament_order(OLD)
b.reload_filament_order()
a.set_filament_order(OLD)
r = b._post("/api/filament-state", {"filament": int(b._fid_of(48)), "state": 4, "arg": 1300})
check("identical order set again -> not refused", not r.get("order_stale"), r)

# a save that fails is NOT applied, and the revision does not move
rev0 = S.ORDER_REV
before = list(S.FILAMENT_ORDER)
real = S._atomic_write


def deny(*a, **k):
    raise PermissionError(5, "Access is denied")


S._atomic_write = deny
r = a._post("/api/filament-order", {"order": NEW})
S._atomic_write = real
check("unsaveable order -> not ok, says so", not r.get("ok") and "not changed" in str(r.get("error")), r)
check("...and the order in force is unchanged", S.FILAMENT_ORDER == before and S.ORDER_REV == rev0,
      (S.FILAMENT_ORDER[48], S.ORDER_REV, rev0))

# the rename is retried while something (OneDrive) holds the file
import pathlib  # noqa: E402
calls = {"n": 0}
real_replace = pathlib.Path.replace


def flaky(self, target):
    calls["n"] += 1
    if calls["n"] <= 2:
        raise PermissionError(5, "Access is denied")
    return real_replace(self, target)


pathlib.Path.replace = flaky
try:
    r = a._post("/api/filament-order", {"order": NEW})
finally:
    pathlib.Path.replace = real_replace
check("rename retried past two 'Access is denied' -> saved", r.get("ok") and calls["n"] == 3, (r, calls))
check("...and applied", S.FILAMENT_ORDER[48] == 60)

srv.shutdown()
print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
