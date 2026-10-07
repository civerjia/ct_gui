#!/usr/bin/env python3
"""A keepalive renews only its own client's filaments.

2026-10-07: two combined_clients. One held a filament at ACTIVE and crashed;
the other only monitored -- but it had once set IDLE, so its background
keepalive was running, and a keepalive with no filament list renewed EVERY
filament. The crashed client's ACTIVE never timed out. Both shared the
client_id "combined_backend"; the backend-side remote session id (X-CT-Session)
is what tells them apart.

    python3 tests/test_keepalive_owner.py
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

import ct.server._server as S  # noqa: E402
import ct.server._safety as SF  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


srv = ThreadingHTTPServer(("127.0.0.1", 0), S.CtHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()


def post(path, body, client, session=None):
    hdr = {"Content-Type": "application/json", "X-CT-Client": client}
    if session:
        hdr["X-CT-Session"] = session
    req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}{path}",
                                 data=json.dumps(body).encode(), headers=hdr)
    return json.loads(urllib.request.urlopen(req, timeout=10).read())


# Two sessions of the same client id command filaments (as /api/filament-state
# would after a confirmed command: owner taken from the request thread).
SF.LAST_POWER_STATE.clear()
SF.POWER_OWNER.clear()
SF._SAFETY_TOUCH_FIL.clear()
SF.set_request_owner("combined_backend#sessionA")
SF.note_power_state([5], S.POWER_STATE_ACTIVE)
SF.set_request_owner("combined_backend#sessionB")
SF.note_power_state([6], S.POWER_STATE_IDLE, 1300)
SF.set_request_owner(None)
check("owners recorded per session", SF.POWER_OWNER == {5: "combined_backend#sessionA",
                                                         6: "combined_backend#sessionB"}, SF.POWER_OWNER)

t5, t6 = SF._SAFETY_TOUCH_FIL[5], SF._SAFETY_TOUCH_FIL[6]
time.sleep(0.05)
# Session A has crashed. Session B (monitor) keeps sending a list-less keepalive.
r = post("/api/safety", {"keepalive": True}, "combined_backend", session="sessionB")
check("keepalive accepted", r.get("ok"), r)
time.sleep(0.05)
post("/api/safety", {"keepalive": True}, "combined_backend", session="sessionB")
check("monitor's keepalive renews ITS filament", SF._SAFETY_TOUCH_FIL[6] > t6, (t6, SF._SAFETY_TOUCH_FIL[6]))
check("...and NOT the crashed session's ACTIVE one", SF._SAFETY_TOUCH_FIL[5] == t5, (t5, SF._SAFETY_TOUCH_FIL[5]))

# A client with no session header is keyed by its client id.
SF.set_request_owner("liuxing")
SF.note_power_state([7], S.POWER_STATE_ACTIVE)
SF.set_request_owner(None)
t7 = SF._SAFETY_TOUCH_FIL[7]
time.sleep(0.05)
post("/api/safety", {"keepalive": True}, "liuxing")
check("plain client renews its own", SF._SAFETY_TOUCH_FIL[7] > t7)
check("...and still not anyone else's", SF._SAFETY_TOUCH_FIL[5] == t5)

# An explicit list is still honoured (energised(*fils) uses it).
post("/api/safety", {"keepalive": True, "filaments": [5]}, "someone")
check("explicit filament list still renews those", SF._SAFETY_TOUCH_FIL[5] > t5)

# A later command by another owner takes the filament over.
SF.set_request_owner("gui-1")
SF.note_power_state([5], S.POWER_STATE_IDLE, 1300)
SF.set_request_owner(None)
check("a new commander takes ownership", SF.POWER_OWNER[5] == "gui-1", SF.POWER_OWNER)

srv.shutdown()
SF.LAST_POWER_STATE.clear()
SF.POWER_OWNER.clear()
print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
