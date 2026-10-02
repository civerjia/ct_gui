"""Run CTClient methods INSIDE the backend for remote scripts.

A script's CTClient is a thin proxy (ct/client/_remote.py): every call is
sent here and executed on a real CTClient living in this process. So the
client's logic -- verify waits, dead-mask handling, schedule orchestration,
error reporting -- is the backend's code, and a backend restart updates it
for every script at its next call. Before this, a fix in the client needed
the backend AND every program that imported it restarted.

One session per proxy. Calls from different threads of the script run
concurrently, as on a local client; with-blocks are entered/exited in order. A
proxy pings while it is alive; a session not heard from for SESSION_IDLE_S is
closed as if the script had died: every `with` block it had open is exited
(lease released, session()/energised() teardown run -- STOPs included) and
its dead-man keepalive is stopped, so the watchdog works exactly as for a
script that crashed. Nothing here touches hardware itself.
"""
from __future__ import annotations

import ctypes
import itertools
import logging
import os
import threading
import time
import traceback
import uuid
from typing import Any

from ct.remote_codec import decode, encode

log = logging.getLogger("ct_gui")

SESSION_IDLE_S = 15.0          # no call and no ping for this long -> the script is gone
REAPER_TICK_S = 2.0
# Constructor arguments a proxy may pass through. host/port are not among
# them: the session always talks to THIS backend over loopback.
_INIT_KEYS = ("timeout", "record", "keepalive")


class _Session:
    def __init__(self, client_id: str, init: dict) -> None:
        from ct.client._client import CTClient
        port = int(os.environ.get("CT_GUI_PORT", "8770"))
        kw = {k: init[k] for k in _INIT_KEYS if k in init}
        self.sid = uuid.uuid4().hex
        self.client_id = client_id
        self.ct = CTClient("127.0.0.1", port=port, client_id=client_id, _local=True, **kw)
        self.lock = threading.RLock()
        self.last = time.monotonic()
        self.cms: dict[int, Any] = {}           # open/openable context managers
        self.entered: list[int] = []            # entered, in order
        self._ids = itertools.count(1)
        self.calls = 0
        self.busy = 0                           # calls in progress (reaper waits)
        # The script's thread -> the backend thread running its call. Calls
        # from different script threads run concurrently, as they did on a
        # local client; Ctrl-C cancels the one from the thread it hit.
        self.workers: dict[int, int] = {}
        self.cm_lock = threading.Lock()         # with-block bookkeeping only

    def state(self) -> dict:
        """What the proxy needs to rebuild this client if the session is lost
        (backend restart): its filament numbering and whether it is keeping
        the dead-man watchdog alive. Read locally, no request."""
        order = getattr(self.ct, "_order", None)
        return {"keepalive": getattr(self.ct, "_keepalive_stop", None) is not None,
                "order": ([int(order.get(i, i)) for i in range(self.ct.FILAMENT_COUNT)]
                          if order is not None else None)}

    def register_cm(self, cm: Any) -> int:
        with self.cm_lock:
            i = next(self._ids)
            self.cms[i] = cm
        return i

    def enc(self, v: Any) -> Any:
        return encode(v, self_obj=self.ct, register_cm=self.register_cm)

    def dec(self, v: Any) -> Any:
        return decode(v, self_obj=self.ct, make_cm=lambda i: self.cms[i])


_SESS: dict[str, _Session] = {}
_LOCK = threading.Lock()
_REAPER: dict[str, threading.Thread] = {}


def _exc(e: BaseException) -> dict:
    return {"ok": False, "exc": {"type": type(e).__name__, "message": str(e),
                                 "trace": traceback.format_exc(limit=6)}}


def open_session(body: dict) -> dict:
    client_id = str(body.get("client_id") or "remote")
    s = _Session(client_id, body.get("init") or {})
    with _LOCK:
        _SESS[s.sid] = s
    _start_reaper()
    log.info("remote session %s opened for %s", s.sid[:8], client_id)
    return {"ok": True, "sid": s.sid, "idle_s": SESSION_IDLE_S}


def ping(body: dict) -> dict:
    s = _SESS.get(str(body.get("sid")))
    if not s:
        return {"ok": False, "error": "no such session (expired?)", "expired": True}
    s.last = time.monotonic()
    return {"ok": True}


def call(body: dict) -> dict:
    """{sid, ptid, kind, ...}: kind = call {name, args, kwargs} | get {name} |
    set {name, value} | describe {name} | enter {cm} | exit {cm, exc_type, exc}.
    ptid = the script thread making the call (for Ctrl-C)."""
    s = _SESS.get(str(body.get("sid")))
    if not s:
        return {"ok": False, "expired": True,
                "error": "remote session not found -- the backend restarted or the "
                         "session timed out; the proxy will open a new one"}
    s.last = time.monotonic()
    ptid = int(body.get("ptid") or 0)
    with s.lock:
        s.calls += 1
        s.busy += 1
        s.workers[ptid] = threading.get_ident()
    try:
        return _run(s, body)
    except BaseException as e:                 # every error goes back to the script
        return {**_exc(e), "state": s.state()}
    finally:
        with s.lock:
            if s.workers.get(ptid) == threading.get_ident():
                del s.workers[ptid]
            s.busy -= 1
            s.last = time.monotonic()


def _run(s: _Session, body: dict) -> dict:
    kind = body.get("kind", "call")
    if kind == "call":
        fn = getattr(s.ct, str(body["name"]))
        out = fn(*s.dec(body.get("args") or []), **s.dec(body.get("kwargs") or {}))
        return {"ok": True, "value": s.enc(out), "state": s.state()}
    if kind == "get":
        return {"ok": True, "value": s.enc(getattr(s.ct, str(body["name"])))}
    if kind == "set":
        setattr(s.ct, str(body["name"]), s.dec(body.get("value")))
        return {"ok": True}
    if kind == "describe":
        name = str(body["name"])
        attr = getattr(type(s.ct), name, None)
        kind_of = ("property" if isinstance(attr, property) else
                   "method" if callable(getattr(s.ct, name, None)) else
                   "missing" if not hasattr(s.ct, name) else "value")
        return {"ok": True, "value": kind_of}
    if kind == "enter":
        i = int(body["cm"])
        with s.cm_lock:
            cm = s.cms[i]
        v = cm.__enter__()
        with s.cm_lock:
            s.entered.append(i)
        return {"ok": True, "value": s.enc(v)}
    if kind == "exit":
        i = int(body["cm"])
        with s.cm_lock:
            cm = s.cms.pop(i)
            if i in s.entered:
                s.entered.remove(i)
        et = body.get("exc_type")
        err = RuntimeError(f"{et}: {body.get('exc')}") if et else None
        suppressed = bool(cm.__exit__(type(err) if err else None, err, None))
        return {"ok": True, "value": suppressed}
    return {"ok": False, "error": f"unknown kind {kind!r}"}


def cancel(body: dict) -> dict:
    """Ctrl-C in the script: raise KeyboardInterrupt inside the call this
    session is running, so it unwinds exactly as it would have in the
    script's own process -- its finally blocks (HV off, STOP) run. Delivered
    at the next Python bytecode, i.e. after any blocking request in flight
    returns (bounded by that request's own timeout)."""
    s = _SESS.get(str(body.get("sid")))
    if not s:
        return {"ok": False, "error": "no such session"}
    tid = s.workers.get(int(body.get("ptid") or 0))
    if not tid:
        return {"ok": True, "running": False}
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid),
                                                   ctypes.py_object(KeyboardInterrupt))
    log.warning("remote session %s (%s): call interrupted by the script (Ctrl-C)",
                s.sid[:8], s.client_id)
    return {"ok": n == 1, "running": True}


def close(body: dict, reason: str = "closed by the script") -> dict:
    with _LOCK:
        s = _SESS.pop(str(body.get("sid")), None)
    if not s:
        return {"ok": True, "already": True}
    _teardown(s, reason)
    return {"ok": True}


def _teardown(s: _Session, reason: str) -> None:
    """Close a session as a dying script would: exit its open `with` blocks
    innermost first (their own cleanup -- lease release, STOPs -- runs), then
    stop its keepalive so the dead-man watchdog takes over."""
    with s.lock:
        err = RuntimeError(f"remote script gone: {reason}")
        for i in reversed(list(s.entered)):
            cm = s.cms.get(i)
            try:
                cm.__exit__(RuntimeError, err, None)
            except Exception as exc:
                log.warning("remote session %s: exiting a with-block failed: %s", s.sid[:8], exc)
        s.entered.clear()
        s.cms.clear()
        stop = getattr(s.ct, "_keepalive_stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                pass
            s.ct._keepalive_stop = None
    log.info("remote session %s (%s) closed: %s, %d call(s)", s.sid[:8], s.client_id,
             reason, s.calls)


def _reaper() -> None:
    while True:
        time.sleep(REAPER_TICK_S)
        now = time.monotonic()
        with _LOCK:
            gone = [s for s in _SESS.values()
                    if now - s.last > SESSION_IDLE_S and not s.busy]
            for s in gone:
                _SESS.pop(s.sid, None)
        for s in gone:
            try:
                _teardown(s, f"no ping for {SESSION_IDLE_S:g} s -- the script stopped")
            except Exception as exc:
                log.warning("remote session %s: teardown failed: %s", s.sid[:8], exc)


def _start_reaper() -> None:
    with _LOCK:
        if "t" in _REAPER and _REAPER["t"].is_alive():
            return
        t = threading.Thread(target=_reaper, name="remote_reaper", daemon=True)
        _REAPER["t"] = t
        t.start()


def sessions() -> list[dict]:
    now = time.monotonic()
    with _LOCK:
        return [{"sid": s.sid[:8], "client": s.client_id, "idle_s": round(now - s.last, 1),
                 "calls": s.calls, "open_with_blocks": len(s.entered)} for s in _SESS.values()]
