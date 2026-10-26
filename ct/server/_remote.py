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
import queue
import threading
import time
import traceback
import uuid
from typing import Any

from ct.remote_codec import decode, encode

log = logging.getLogger("ct_gui")

SESSION_IDLE_S = 15.0          # no call and no ping for this long -> the script is gone
SESSION_DEAD_CALL_S = 30.0     # a call running, no ping this long -> interrupt it (script died)
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
        # Two scripts may share a client_id (two combined_clients): the session
        # id tells their filaments apart for the dead-man keepalive.
        self.ct._s.headers["X-CT-Session"] = self.sid
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
        self.jobs: dict[str, "_CallJob"] = {}   # calls waiting on a script callback
        self.closed = False
        self.version: dict = {}                 # the proxy's ct_gui version, as it said

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


def _backend_version() -> dict:
    try:
        from ._safety import _BACKEND_VERSION
        return dict(_BACKEND_VERSION)
    except Exception:
        return {}


def version_check(client_v: dict | None) -> dict:
    """Is the proxy's own code older than this backend's? The client LOGIC
    runs here, so an old proxy mostly still gets new behaviour -- but the
    proxy itself (and anything a script imports from ct beside CTClient) is
    whatever that process loaded at start. 2026-10-13: a proxy too old to
    send callbacks ran every on_armed fire on its own stale client and 60 of
    62 read-back failures came from it. Compared by content tree."""
    mine = _backend_version()
    cv = client_v if isinstance(client_v, dict) else {}
    if not cv.get("tree"):
        return {"stale": True, "client": None, "backend": mine,
                "why": "the client sent no version: its ct_gui predates the "
                       "version check (2026-10-14) and runs on-armed fires on its own old code"}
    if mine.get("tree") and cv["tree"] != mine["tree"]:
        return {"stale": True, "client": cv, "backend": mine,
                "why": f"client runs ct_gui {cv.get('commit') or cv['tree']}, "
                       f"backend {mine.get('commit') or mine['tree']}"}
    return {"stale": False, "client": cv, "backend": mine}


def open_session(body: dict) -> dict:
    client_id = str(body.get("client_id") or "remote")
    s = _Session(client_id, body.get("init") or {})
    vc = version_check(body.get("version"))
    s.version = vc.get("client") or {}
    with _LOCK:
        _SESS[s.sid] = s
    _start_reaper()
    log.info("remote session %s opened for %s", s.sid[:8], client_id)
    if vc["stale"]:
        log.warning("remote client %s runs OLD ct_gui code (%s) -- restart that program "
                    "to load the backend's version", client_id, vc["why"])
    return {"ok": True, "sid": s.sid, "idle_s": SESSION_IDLE_S,
            "callbacks": True,              # this backend calls functions back (see call())
            "version": vc}


def ping(body: dict) -> dict:
    s = _SESS.get(str(body.get("sid")))
    if not s:
        return {"ok": False, "error": "no such session (expired?)", "expired": True}
    s.last = time.monotonic()
    return {"ok": True}


def _has_callback(v: Any) -> bool:
    if isinstance(v, dict):
        return v.get("__ct__") == "callback" or any(_has_callback(x) for x in v.values())
    if isinstance(v, list):
        return any(_has_callback(x) for x in v)
    return False


_CALLBACK_EXC = {e.__name__: e for e in (KeyboardInterrupt, ValueError, TypeError, KeyError,
                                         RuntimeError, TimeoutError, OSError)}


class _CallJob:
    """A call given functions (on_armed=, progress=): it runs in its own thread
    here, and each time it calls one of them the HTTP request carrying the
    call answers {"callback": ...}. The proxy runs the function in the script
    and posts its result to /api/remote/callback, whose answer is the next
    callback or the call's result. So the call is the BACKEND's code even
    with callbacks -- before, it ran on the script's own client, whose code
    was whatever that program loaded at start."""

    def __init__(self, s: _Session, body: dict, ptid: int) -> None:
        self.s, self.body, self.ptid = s, body, ptid
        self.cid = uuid.uuid4().hex
        self.events: queue.Queue = queue.Queue()     # job -> HTTP: callback / done
        self.replies: queue.Queue = queue.Queue()    # HTTP -> job: a callback's result
        self.waiting = False                         # in the script, not here
        self.thread = threading.Thread(target=self._main, name=f"remote_call_{self.cid[:6]}",
                                       daemon=True)

    def stub(self, fid: int):
        def call_back(*args, **kwargs):
            s = self.s
            self.events.put({"ok": True, "callback": {
                "cid": self.cid, "id": fid, "args": s.enc(list(args)), "kwargs": s.enc(kwargs)}})
            self.waiting = True
            try:
                while True:
                    try:
                        r = self.replies.get(timeout=1.0)
                        break
                    except queue.Empty:
                        if s.closed:
                            raise RuntimeError("remote script gone while its callback ran")
            finally:
                self.waiting = False
                s.last = time.monotonic()
            if r.get("exc"):
                e = r["exc"]
                cls = _CALLBACK_EXC.get(e.get("type"), RuntimeError)
                msg = e.get("message", "")
                raise cls(msg if cls is not RuntimeError else f"{e.get('type')}: {msg}")
            return s.dec(r.get("value"))
        return call_back

    def _main(self) -> None:
        s = self.s
        with s.lock:
            s.workers[self.ptid] = threading.get_ident()
        try:
            name = str(self.body["name"])
            dec = lambda v: decode(v, self_obj=s.ct, make_cm=lambda i: s.cms[i],   # noqa: E731
                                   make_fn=self.stub)
            out = getattr(s.ct, name)(*dec(self.body.get("args") or []),
                                      **dec(self.body.get("kwargs") or {}))
            res = {"ok": True, "value": s.enc(out), "state": s.state()}
        except BaseException as e:
            res = {**_exc(e), "state": s.state()}
        finally:
            with s.lock:
                if s.workers.get(self.ptid) == threading.get_ident():
                    del s.workers[self.ptid]
        self.events.put(res)

    def next_event(self) -> dict:
        """Wait for the job's next callback or its result."""
        ev = self.events.get()
        if "callback" not in ev:
            with self.s.lock:
                self.s.jobs.pop(self.cid, None)
                self.s.busy -= 1
                self.s.last = time.monotonic()
        return ev


def call(body: dict) -> dict:
    """{sid, ptid, kind, ...}: kind = call {name, args, kwargs} | get {name} |
    set {name, value} | describe {name} | enter {cm} | exit {cm, exc_type, exc}.
    ptid = the script thread making the call (for Ctrl-C). A call whose
    arguments carry functions runs as a _CallJob (callbacks)."""
    s = _SESS.get(str(body.get("sid")))
    if not s:
        return {"ok": False, "expired": True,
                "error": "remote session not found -- the backend restarted or the "
                         "session timed out; the proxy will open a new one"}
    s.last = time.monotonic()
    ptid = int(body.get("ptid") or 0)
    if body.get("kind", "call") == "call" and (_has_callback(body.get("args"))
                                                or _has_callback(body.get("kwargs"))):
        job = _CallJob(s, body, ptid)
        with s.lock:
            s.calls += 1
            s.busy += 1
            s.jobs[job.cid] = job
        job.thread.start()
        return job.next_event()
    with s.lock:
        s.calls += 1
        s.busy += 1
        prev = s.workers.get(ptid)              # a call made FROM a callback nests
        s.workers[ptid] = threading.get_ident()
    try:
        return _run(s, body)
    except BaseException as e:                 # every error goes back to the script
        return {**_exc(e), "state": s.state()}
    finally:
        with s.lock:
            if s.workers.get(ptid) == threading.get_ident():
                if prev is not None:
                    s.workers[ptid] = prev
                else:
                    del s.workers[ptid]
            s.busy -= 1
            s.last = time.monotonic()


def callback_result(body: dict) -> dict:
    """{sid, cid, value | exc}: what the script's function returned (or
    raised); answers with the call's next callback or its result."""
    s = _SESS.get(str(body.get("sid")))
    job = s.jobs.get(str(body.get("cid"))) if s else None
    if not job:
        return {"ok": False, "error": "no such call waiting for a callback (session closed?)"}
    s.last = time.monotonic()
    job.replies.put({"value": body.get("value"), "exc": body.get("exc")})
    return job.next_event()


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
    s.closed = True                             # a call waiting on a callback gives up
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
            # A call waiting on the script's callback is not busy HERE: if
            # the script stops pinging meanwhile it is gone.
            gone = [s for s in _SESS.values()
                    if now - s.last > SESSION_IDLE_S
                    and (not s.busy or any(j.waiting for j in list(s.jobs.values())))]
            for s in gone:
                _SESS.pop(s.sid, None)
            # A script that DIED during a long call (scan_view, a whole
            # rotation) left that call running in a backend thread, heating,
            # with nobody to stop it: busy sessions were never reaped. No ping
            # for SESSION_DEAD_CALL_S -> interrupt its calls exactly as Ctrl-C
            # would (their finally blocks run: disarm, IDLE); once they unwind
            # the session is idle and the next tick reaps it.
            stuck = [s for s in _SESS.values()
                     if s.busy and s.workers and now - s.last > SESSION_DEAD_CALL_S
                     and not getattr(s, "interrupted", False)]
        for s in stuck:
            s.interrupted = True
            for tid in list(s.workers.values()):
                ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(tid),
                                                           ctypes.py_object(KeyboardInterrupt))
            log.warning("remote session %s (%s): no ping for %.0f s during a call -- "
                        "the script is gone; its call was interrupted", s.sid[:8],
                        s.client_id, now - s.last)
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


def activity() -> list[str]:
    """Sessions doing something a restart would cut: a call in progress, or a
    `with` block open (lease, session(), energised()). For the auto-update."""
    with _LOCK:
        live = list(_SESS.values())
    out = []
    for s in live:
        if s.busy:
            out.append(f"{s.client_id}: a call in progress")
        elif s.entered:
            out.append(f"{s.client_id}: inside a with-block")
    return out


def sessions() -> list[dict]:
    now = time.monotonic()
    with _LOCK:
        live = list(_SESS.values())
    return [{"sid": s.sid[:8], "client": s.client_id, "idle_s": round(now - s.last, 1),
             "calls": s.calls, "open_with_blocks": len(s.entered),
             "version": s.version.get("commit") or s.version.get("tree"),
             "stale": version_check(s.version or None)["stale"]} for s in live]
