#!/usr/bin/env python3
"""Remote calls given FUNCTIONS (on_armed=, progress=) run in the backend and
call the functions back in the script (2026-10-14). Before, such a call ran
on the script's own client -- the code that program loaded at start: on
2026-10-13 combined_backend, started 10-12, fired every on_armed shot on old
code and 60 of 62 read-back failures came from it.

In-process: the proxy's transport is wired straight to ct/server/_remote.py,
whose session client is a fake.

    python3 tests/test_remote_callbacks.py
"""
import _path  # noqa: F401

import os
import threading

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client import _remote as cr  # noqa: E402
from ct.server import _remote as sr  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Fake:
    """Stands in for the backend session's CTClient."""
    FILAMENT_COUNT = 4

    def __init__(self):
        self.log, self.cleaned = [], False

    def fire(self, f, on_armed=None, progress=None):
        self.log.append(("armed", f, threading.current_thread().name))
        got = on_armed() if on_armed else None
        for i in range(3):
            if progress:
                progress(i, {"f": f, "i": i})
        return {"ok": True, "armed_returned": got}

    def guarded(self, on_armed):
        try:
            on_armed()
            return {"ok": True}
        finally:
            self.cleaned = True

    def nested(self, cb):
        return cb(self)              # the callback gets the client itself

    def ping(self):
        return "pong"


def make(callbacks=True):
    sess = sr.open_session({"client_id": "t", "init": {"keepalive": False},
                            "version": cr._my_version()})
    s = sr._SESS[sess["sid"]]
    fake = Fake()
    s.ct = fake
    p = cr.RemoteCTClient("http://127.0.0.1:1", "t", {}, sess["sid"])
    p.__dict__["_rp_callbacks"] = callbacks and sess["callbacks"]
    p.__dict__["_rp_kinds"].update(fire="method", guarded="method", nested="method", ping="method")

    def send(body, path="/api/remote/call"):
        body = {**body, "sid": sess["sid"], "ptid": threading.get_ident()}
        return sr.callback_result(body) if path.endswith("callback") else sr.call(body)
    p.__dict__["_rp_send"] = send
    return p, fake, sess


# 1. on_armed + progress run here, in the calling thread; the call runs there.
p, fake, sess = make()
check("open: backend says it calls back, same code not stale",
      sess["callbacks"] and not sess["version"]["stale"], sess)
seen, me = [], threading.current_thread().name
r = p.fire(5, on_armed=lambda: (seen.append(("armed", threading.current_thread().name)), 42)[1],
           progress=lambda i, row: seen.append((i, row["f"])))
check("call ran in the backend session (its own thread)",
      fake.log and fake.log[0][2].startswith("remote_call_"), fake.log)
check("on_armed ran here, in the calling thread, its return value went back",
      seen[0] == ("armed", me) and r["armed_returned"] == 42, (seen, r))
check("progress called back with its arguments, in order", seen[1:] == [(0, 5), (1, 5), (2, 5)], seen)
check("session not busy afterwards, no job left",
      sr._SESS[sess["sid"]].busy == 0 and not sr._SESS[sess["sid"]].jobs)

# 2. A callback that raises: the call sees the error and its cleanup runs.
p, fake, _ = make()


def bad():
    raise ValueError("camera not ready")
try:
    p.guarded(bad)
    check("callback error reaches the script", False)
except ValueError as e:
    check("callback error reaches the script as the same type", "camera not ready" in str(e), e)
check("...and the backend call's finally ran", fake.cleaned)

# 3. Ctrl-C inside the callback unwinds the backend call, then raises here.
p, fake, _ = make()


def ctrl_c():
    raise KeyboardInterrupt()
try:
    p.guarded(ctrl_c)
    check("Ctrl-C in on_armed raises KeyboardInterrupt", False)
except KeyboardInterrupt:
    check("Ctrl-C in on_armed raises KeyboardInterrupt", True)
check("...after the backend call unwound (cleanup ran)", fake.cleaned)

# 4. The client handed to a callback is the proxy; calls made from it work.
p, fake, _ = make()
r = p.nested(lambda c: c.ping())
check("callback gets the proxy; a call from inside it works", r == "pong", r)

# 5. Older backend (no callbacks): the old local path, unchanged.
p, fake, _ = make(callbacks=False)
calls = []
p.__dict__["_rp_local_client"] = type("L", (), {
    "fire": lambda self, f, on_armed=None: calls.append("local") or "local",
    "reload_filament_order": lambda self: None})()
check("old backend: runs locally", p.fire(1, on_armed=lambda: None) == "local" and calls)

# 6. Script gone while its callback runs: the backend call is released.
p, fake, sess = make()
s = sr._SESS[sess["sid"]]
job_body = {"sid": sess["sid"], "ptid": 1, "kind": "call", "name": "guarded",
            "kwargs": {"__ct__": "dict", "k": ["on_armed"], "v": [{"__ct__": "callback", "id": 1}]}}
ev = sr.call(job_body)
check("callback event returned to the script", "callback" in ev, ev)
sr.close({"sid": sess["sid"]})
job = list(s.jobs.values())[0]
res = job.next_event()
check("closed mid-callback: call fails, cleanup ran",
      not res["ok"] and "gone" in res["exc"]["message"] and fake.cleaned, res)

# 7. Version check.
check("no version -> stale (pre-2026-10-14 client)", sr.version_check(None)["stale"])
sr_v = sr._backend_version
sr._backend_version = lambda: {"commit": "new1234", "tree": "aaaa"}
try:
    v = sr.version_check({"commit": "old5678", "tree": "bbbb"})
    check("different tree -> stale, both named", v["stale"] and "old5678" in v["why"] and "new1234" in v["why"], v)
    check("same tree -> fine", not sr.version_check({"tree": "aaaa"})["stale"])
finally:
    sr._backend_version = sr_v

import io, contextlib  # noqa: E401,E402
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    cr._warn_stale("combined_backend", {"version": {"stale": True, "why": "client runs ct_gui old5678"}})
check("stale -> loud warning naming the program", "combined_backend" in buf.getvalue()
      and "Restart" in buf.getvalue(), buf.getvalue())

# 8. Backend down when the program builds its client: still a PROXY (no
#    session yet), never a local client running this process's code.
from ct.client._client import CTClient  # noqa: E402
from ct.client import _base  # noqa: E402
buf = io.StringIO()
with contextlib.redirect_stderr(buf):
    c = CTClient("127.0.0.1", port=1, client_id="combined_backend")
check("backend down at construction -> proxy, not local",
      type(c).__name__ == "RemoteCTClient" and c._rp_sid_box()["sid"] is None, type(c))
try:
    c.read_ads_all()
    check("call while still down raises CTConnectionError", False)
except _base.CTConnectionError:
    check("call while still down raises CTConnectionError", True)
c._rp_stop.set()

for sid in list(sr._SESS):
    sr.close({"sid": sid})
print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
