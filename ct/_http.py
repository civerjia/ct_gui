"""A small HTTP client on the standard library -- the part of `requests` the
CT client uses, so nothing that imports ct needs a third-party package.

Since the CTClient runs inside the backend (ct/server/_remote.py) the backend
imports the client too, and a backend or a script on a Python without
`requests` failed with an import error that an update could not fix. Only
what the client calls is here:

    s = Session(); s.headers.update({...})
    r = s.get(url, timeout=2.0)
    r = s.post(url, json={...}, timeout=(5, None))   # (connect, read); None = no limit
    r.status_code, r.reason, r.ok, r.text, r.json()
    ConnectionError / Timeout, both RequestException

Keep-alive: one connection per thread per host, reopened when the server has
closed it, so a 20 Hz poll does not open 20 TCP connections a second.
"""
from __future__ import annotations

import http.client
import json as _json
import socket
import threading
from typing import Any
from urllib.parse import urlencode, urlsplit


class RequestException(OSError):
    """Any failure to get an HTTP response."""


class ConnectionError(RequestException):   # noqa: A001 -- the name callers expect
    """Could not reach the server (refused, unreachable, reset)."""


class Timeout(RequestException):
    """Connecting or waiting for the response took longer than allowed."""


class exceptions:   # noqa: N801 -- mirrors requests.exceptions.RequestException
    RequestException = RequestException
    ConnectionError = ConnectionError
    Timeout = Timeout


class Response:
    def __init__(self, status: int, reason: str, body: bytes, headers) -> None:
        self.status_code = status
        self.reason = reason
        self.content = body
        self.headers = dict(headers)

    @property
    def ok(self) -> bool:
        return self.status_code < 400

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", "replace")

    def json(self) -> Any:
        return _json.loads(self.content.decode("utf-8"))


def _split_timeout(timeout) -> tuple[float | None, float | None]:
    if isinstance(timeout, (tuple, list)):
        return (timeout[0], timeout[1])
    return (timeout, timeout)


class Session:
    """Thread-safe: every thread uses its own connection."""

    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self._tls = threading.local()

    def _conn(self, host: str, port: int, connect_timeout) -> http.client.HTTPConnection:
        conns = getattr(self._tls, "conns", None)
        if conns is None:
            conns = self._tls.conns = {}
        c = conns.get((host, port))
        if c is None:
            c = http.client.HTTPConnection(host, port, timeout=connect_timeout)
            conns[(host, port)] = c
        return c

    def _drop(self, host: str, port: int) -> None:
        c = getattr(self._tls, "conns", {}).pop((host, port), None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass

    def request(self, method: str, url: str, json: Any = None, timeout=None,
                headers: dict | None = None, params: dict | None = None) -> Response:
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        u = urlsplit(url)
        host, port = u.hostname or "localhost", u.port or 80
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        hdrs = {**self.headers, **(headers or {})}
        body = None
        if json is not None:
            body = _json.dumps(json).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        connect_t, read_t = _split_timeout(timeout)
        # One retry, only when a REUSED connection turns out to have been closed
        # by the server between requests (the normal end of keep-alive). Never
        # after the request may have been processed.
        for attempt in (1, 2):
            fresh = getattr(self._tls, "conns", {}).get((host, port)) is None
            c = self._conn(host, port, connect_t)
            try:
                if c.sock is None:
                    c.timeout = connect_t
                    c.connect()
                c.sock.settimeout(connect_t)
                c.request(method, path, body=body, headers=hdrs)
                c.sock.settimeout(read_t)
                r = c.getresponse()
                data = r.read()
                if r.getheader("Connection", "").lower() == "close":
                    self._drop(host, port)
                return Response(r.status, r.reason, data, r.getheaders())
            except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError,
                    http.client.ImproperConnectionState) as exc:
                # A reused connection that is no longer usable: the server
                # closed it, or an earlier request on it never finished (e.g.
                # interrupted by Ctrl-C). Nothing of THIS request was processed.
                self._drop(host, port)
                if attempt == 1 and not fresh:
                    continue
                raise ConnectionError(f"{method} {url}: {exc}") from exc
            except (socket.timeout, TimeoutError) as exc:
                self._drop(host, port)
                raise Timeout(f"{method} {url}: timed out") from exc
            except OSError as exc:
                self._drop(host, port)
                raise ConnectionError(f"{method} {url}: {exc}") from exc
            except http.client.HTTPException as exc:
                self._drop(host, port)
                raise RequestException(f"{method} {url}: {exc}") from exc
            except BaseException:
                # KeyboardInterrupt (a cancelled call) or anything else mid-way:
                # the connection is in an unknown state -- never reuse it.
                self._drop(host, port)
                raise
        raise ConnectionError(f"{method} {url}: connection lost")   # not reached

    def get(self, url: str, timeout=None, headers: dict | None = None,
            params: dict | None = None) -> Response:
        return self.request("GET", url, timeout=timeout, headers=headers, params=params)

    def post(self, url: str, json: Any = None, timeout=None, headers: dict | None = None,
             params: dict | None = None) -> Response:
        return self.request("POST", url, json=json, timeout=timeout, headers=headers, params=params)

    def close(self) -> None:
        for c in list(getattr(self._tls, "conns", {}).values()):
            try:
                c.close()
            except Exception:
                pass
        self._tls.conns = {}


def post(url: str, json: Any = None, timeout=None, headers: dict | None = None,
         params: dict | None = None) -> Response:
    """One request on a throw-away connection (requests.post)."""
    s = Session()
    try:
        return s.post(url, json=json, timeout=timeout, headers=headers, params=params)
    finally:
        s.close()


def get(url: str, timeout=None, headers: dict | None = None,
        params: dict | None = None) -> Response:
    s = Session()
    try:
        return s.get(url, timeout=timeout, headers=headers, params=params)
    finally:
        s.close()
