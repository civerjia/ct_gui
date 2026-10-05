"""The remote CTClient: a thin proxy that runs every call inside the backend.

    ct = CTClient("192.168.8.218", client_id="my_script")   # this is what you get

Every method call, property read and `with` block on `ct` is sent to the
backend, which executes it on a real CTClient of its own (ct/server/_remote.py)
and sends the result back -- the same value and type a local client would
have returned. The client's logic therefore runs as the BACKEND's code: when
the backend restarts on a new version, the next call this script makes uses
it. Before, every program importing CTClient had to be restarted as well.

What stays local:
  * static/class methods and constants (pure formulas, no hardware);
  * a call that is given a CALLABLE (progress=..., on_armed=...): a function
    cannot be sent, so that one call runs on a local client, as before.

Liveness: a background thread pings the backend every PING_S. If this process
dies, the backend notices within ~15 s and closes the session as a crashed
script would be closed: open `with` blocks are exited (lease released,
teardown STOPs run) and the dead-man keepalive stops.

Opt out with CT_CLIENT_LOCAL=1 (environment), or CTClient(..., _local=True).
"""
from __future__ import annotations

import atexit
import inspect
import threading
import weakref
from typing import Any

# Standard library only: the subset of `requests` this client uses (ct/_http.py).
# Named `requests` so every call site reads as before.
from ct import _http as requests  # noqa: N812

from ct.remote_codec import decode, encode

from . import _base

PING_S = 3.0
_BUILTIN_EXC = {e.__name__: e for e in (ValueError, TypeError, KeyError, IndexError,
                                        RuntimeError, AttributeError, TimeoutError,
                                        LookupError, ZeroDivisionError, OSError)}


def _has_callable(v: Any) -> bool:
    if callable(v) and not isinstance(v, type):
        return True
    if isinstance(v, dict):
        return any(_has_callable(x) for x in v.values())
    if isinstance(v, (list, tuple, set, frozenset)):
        return any(_has_callable(x) for x in v)
    return False


class _RemoteCM:
    """Local handle for a context manager living in the backend session."""

    def __init__(self, proxy: "RemoteCTClient", cm_id: int) -> None:
        self._p, self._id = proxy, cm_id

    def __enter__(self):
        return self._p._rp_request({"kind": "enter", "cm": self._id})

    def __exit__(self, et, ev, tb):
        try:
            return bool(self._p._rp_request({"kind": "exit", "cm": self._id,
                                              "exc_type": et.__name__ if et else None,
                                              "exc": str(ev) if ev else None}))
        except Exception as exc:              # never mask the block's own exception
            if et is None:
                raise
            print(f"[ct remote] leaving the with-block on the backend failed: {exc}")
            return False


class RemoteCTClient:
    """See the module docstring. Behaves like CTClient; isinstance checks
    against CTClient are the one visible difference."""

    @classmethod
    def open(cls, *args, **kwargs) -> "RemoteCTClient | None":
        """Build a proxy for CTClient(*args, **kwargs), or None when the backend
        has no remote API (older backend) -- the caller then falls back to a
        local client."""
        from ._client import CTClient
        try:
            bound = inspect.signature(CTClient.__init__).bind(None, *args, **kwargs)
        except TypeError:
            return None                        # let the local constructor report it
        bound.apply_defaults()
        a = bound.arguments
        base = f"http://{a['host']}:{a['port']}"
        init = {"timeout": a["timeout"], "record": a["record"], "keepalive": a["keepalive"]}
        try:
            r = requests.post(base + "/api/remote/open",
                              json={"client_id": a["client_id"], "init": init},
                              headers={"X-CT-Client": a["client_id"]}, timeout=(5, 30))
            if r.status_code != 200 or not r.json().get("ok"):
                return None
            sid = r.json()["sid"]
        except Exception:
            return None
        return cls(base, a["client_id"], init, sid)

    def __init__(self, base: str, client_id: str, init: dict, sid: str) -> None:
        d = self.__dict__
        d["base"], d["client_id"] = base, client_id
        d["_rp_init"], d["_rp_sid"] = init, sid
        # One HTTP session per script thread: calls from different threads go
        # out (and run in the backend) concurrently, as on a local client.
        d["_rp_tls"] = threading.local()
        d["_rp_kinds"] = {}
        d["_rp_local_client"] = None
        d["_rp_stop"] = threading.Event()
        d["_rp_state"] = {"keepalive": False, "order": None}   # see _rp_reopen
        t = threading.Thread(target=RemoteCTClient._rp_pinger,
                             args=(weakref.ref(self), base, client_id, d["_rp_stop"]),
                             name="ct_remote_ping", daemon=True)
        t.start()
        weakref.finalize(self, RemoteCTClient._rp_close_session, base, client_id,
                         d["_rp_stop"], self._rp_sid_box())
        atexit.register(self._rp_atexit)

    # ── session plumbing ────────────────────────────────────────────────
    def _rp_sid_box(self) -> dict:
        box = self.__dict__.setdefault("_rp_box", {"sid": self._rp_sid})
        return box

    def _rp_atexit(self) -> None:
        RemoteCTClient._rp_close_session(self.base, self.client_id, self._rp_stop,
                                         self._rp_sid_box())

    @staticmethod
    def _rp_close_session(base: str, client_id: str, stop: threading.Event, box: dict) -> None:
        stop.set()
        sid = box.get("sid")
        if not sid:
            return
        box["sid"] = None
        try:
            requests.post(base + "/api/remote/close", json={"sid": sid},
                          headers={"X-CT-Client": client_id}, timeout=5)
        except Exception:
            pass

    @staticmethod
    def _rp_pinger(ref, base: str, client_id: str, stop: threading.Event) -> None:
        http = requests.Session()               # own session: requests is not thread-safe
        http.headers.update({"X-CT-Client": client_id})
        while not stop.wait(PING_S):
            self = ref()
            if self is None:
                return
            sid = self._rp_sid_box().get("sid")
            del self
            if not sid:
                continue
            try:
                http.post(base + "/api/remote/ping", json={"sid": sid}, timeout=5)
            except Exception:
                pass

    def _rp_reopen(self) -> None:
        """A new session for this proxy (the old one is gone: backend restart
        or a long silence). The new backend-side client is rebuilt to be the
        SAME client the script had:
          * its filament numbering is the one this script was using -- the
            backend restores its saved order only onto unchanged wiring, and
            adopting a different one would point the script's filament numbers
            at other filaments. Installed in the
            client only (as a local client always kept its own snapshot), never
            pushed to the backend;
          * if it was keeping the dead-man watchdog alive, it carries on, so
            filaments that were heating are not dropped to SLEEP."""
        r = requests.post(self.base + "/api/remote/open",
                          json={"client_id": self.client_id, "init": self._rp_init},
                          headers={"X-CT-Client": self.client_id}, timeout=(5, 30))
        j = r.json()
        if not j.get("ok"):
            raise _base.CTConnectionError(f"could not reopen the remote session: {j}")
        self._rp_sid_box()["sid"] = j["sid"]
        self.__dict__["_rp_sid"] = j["sid"]
        st = dict(self._rp_state)
        if st.get("order") is not None:
            self._rp_send({"kind": "call", "name": "_adopt_order",
                           "args": encode([{"ok": True, "order": st["order"], "epoch": None}]),
                           "kwargs": encode({})})
        if st.get("keepalive"):
            self._rp_send({"kind": "call", "name": "_ensure_keepalive",
                           "args": encode([5]), "kwargs": encode({})})   # 5 = ACTIVE: energising
        print(f"[ct remote] backend session re-opened for {self.client_id!r}"
              + (" (filament numbering kept)" if st.get("order") is not None else "")
              + (" (watchdog keepalive resumed)" if st.get("keepalive") else ""), flush=True)

    def _rp_http(self) -> requests.Session:
        tls = self._rp_tls
        if getattr(tls, "http", None) is None:
            tls.http = requests.Session()
            tls.http.headers.update({"X-CT-Client": self.client_id})
        return tls.http

    def _rp_send(self, body: dict) -> dict:
        """One raw request to the current session; returns the JSON reply."""
        ptid = threading.get_ident()
        payload = {**body, "sid": self._rp_sid_box().get("sid"), "ptid": ptid}
        try:
            r = self._rp_http().post(self.base + "/api/remote/call", json=payload,
                                     timeout=(5, None))
            return r.json()
        except KeyboardInterrupt:
            # Ctrl-C must stop the CALL, not just this wait: the backend raises
            # KeyboardInterrupt inside it so its own cleanup runs, as it would
            # have in this process. Then the interrupt carries on here.
            try:
                requests.post(self.base + "/api/remote/cancel",
                              json={"sid": payload["sid"], "ptid": ptid},
                              headers={"X-CT-Client": self.client_id}, timeout=5)
            except Exception:
                pass
            raise
        except requests.exceptions.RequestException as exc:
            raise _base.CTConnectionError(f"backend unreachable at {self.base}: {exc}") from exc

    def _rp_request(self, body: dict) -> Any:
        """Send one request to this proxy's session; decode the value or raise
        the error the backend reported. A session that no longer exists (the
        backend restarted -- e.g. to update itself) is reopened and the request
        sent again: it was never executed, so sending it again is safe."""
        for attempt in (1, 2):
            j = self._rp_send(body)
            if isinstance(j.get("state"), dict):
                self.__dict__["_rp_state"] = j["state"]
            if j.get("expired") and attempt == 1:
                if body.get("kind") in ("enter", "exit"):
                    # A with-block opened in the lost session cannot be resumed:
                    # its exit actions (lease release, teardown STOPs) did not
                    # run. Say so -- never pretend the block closed cleanly.
                    raise _base.CTError(
                        "the backend restarted inside this `with` block, so its exit "
                        "actions (lease release / STOP teardown) could not run there. "
                        "The backend's watchdog drops unrenewed ACTIVE to SLEEP and opens "
                        "the grid; check the rig state before going on")
                self._rp_reopen()
                continue
            if j.get("ok"):
                return decode(j.get("value"), self_obj=self,
                              make_cm=lambda i: _RemoteCM(self, i))
            exc_info = j.get("exc")
            if exc_info:
                name, msg = exc_info.get("type", "Error"), exc_info.get("message", "")
                cls = getattr(_base, name, None)
                if isinstance(cls, type) and issubclass(cls, Exception):
                    raise cls(msg)
                if name in _BUILTIN_EXC:
                    raise _BUILTIN_EXC[name](msg)
                raise _base.CTError(f"{name}: {msg}")
            raise _base.CTError(j.get("error") or f"remote call failed: {j}")
        raise _base.CTConnectionError("remote session could not be reopened")

    def _rp_local(self):
        """A local client for the calls that cannot be sent (they carry a callable)."""
        c = self.__dict__["_rp_local_client"]
        if c is None:
            from ._client import CTClient
            host, port = self.base.split("//", 1)[1].rsplit(":", 1)
            c = CTClient(host, port=int(port), client_id=self.client_id, _local=True,
                         **self._rp_init)
            self.__dict__["_rp_local_client"] = c
        return c

    def _rp_kind(self, name: str) -> str:
        kinds = self.__dict__["_rp_kinds"]
        if name in kinds:
            return kinds[name]
        from ._client import CTClient
        static = inspect.getattr_static(CTClient, name, None)
        if isinstance(static, property):
            k = "property"
        elif isinstance(static, (staticmethod, classmethod)):
            k = "local"
        elif inspect.isfunction(static):
            k = "method"
        elif static is not None:
            k = "local"                          # a class constant
        else:
            k = self._rp_request({"kind": "describe", "name": name})
        kinds[name] = k
        return k

    # ── the API surface ─────────────────────────────────────────────────
    def __getattr__(self, name: str) -> Any:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        kind = self._rp_kind(name)
        if kind == "local":
            from ._client import CTClient
            return getattr(CTClient, name)
        if kind in ("property", "value"):
            return self._rp_request({"kind": "get", "name": name})
        if kind == "missing":
            raise AttributeError(f"CTClient has no attribute {name!r}")

        def method(*args, **kwargs):
            if _has_callable(args) or _has_callable(kwargs):
                return getattr(self._rp_local(), name)(*args, **kwargs)
            return self._rp_request({"kind": "call", "name": name,
                                     "args": encode(list(args), self_obj=self),
                                     "kwargs": encode(kwargs, self_obj=self)})
        method.__name__ = name
        return method

    def __setattr__(self, name: str, value: Any) -> None:
        if name in self.__dict__ or name.startswith("_rp_"):
            self.__dict__[name] = value
            return
        self._rp_request({"kind": "set", "name": name, "value": encode(value, self_obj=self)})

    def __repr__(self) -> str:
        return f"<CTClient {self.client_id!r} -- remote, runs in the backend at {self.base}>"

    def __dir__(self):
        from ._client import CTClient
        return sorted(set(dir(CTClient)) | set(self.__dict__))
