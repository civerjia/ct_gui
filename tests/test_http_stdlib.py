#!/usr/bin/env python3
"""ct/_http.py -- the standard-library replacement for `requests` -- behaves
the way the client relies on, against a real local HTTP server:
JSON in and out, keep-alive reuse, recovery when the server closed an idle
connection, Timeout vs ConnectionError, query params, many threads at once.
And nothing in ct imports `requests` any more.

    python3 tests/test_http_stdlib.py
"""
import _path  # noqa: F401

import builtins
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ct import _http

FAILS = []
CONNS = {"n": 0}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"          # keep-alive, like the backend

    def setup(self):
        CONNS["n"] += 1
        super().setup()

    def log_message(self, *a):
        pass

    def _send(self, obj, close=False):
        data = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        if close:
            self.send_header("Connection", "close")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass                            # the client gave up (the timeout test)

    def do_GET(self):
        if self.path.startswith("/slow"):
            time.sleep(1.0)
        if self.path.startswith("/bye"):
            self._send({"ok": True}, close=True)
            self.close_connection = True
            return
        self._send({"ok": True, "path": self.path, "client": self.headers.get("X-CT-Client")})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"null")
        self._send({"ok": True, "echo": body})


srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"

s = _http.Session()
s.headers.update({"X-CT-Client": "t"})
r = s.get(base + "/a", timeout=2)
check("GET json + session headers", r.ok and r.json()["client"] == "t", r.text)
r = s.post(base + "/p", json={"x": [1, 2]}, timeout=2)
check("POST json round trip", r.json()["echo"] == {"x": [1, 2]}, r.text)
r = s.get(base + "/q", params={"since": 5, "n": "a b"}, timeout=2)
check("params are url-encoded", r.json()["path"] == "/q?since=5&n=a+b", r.json()["path"])

CONNS["n"] = 0
for _ in range(20):
    s.get(base + "/a", timeout=2)
check("keep-alive: 20 requests, one connection", CONNS["n"] <= 1, str(CONNS["n"]))

s.get(base + "/bye", timeout=2)              # server closes after answering
r = s.get(base + "/a", timeout=2)
check("a connection the server closed is reopened", r.ok)

try:
    s.get(base + "/slow", timeout=0.2)
    check("read timeout raises Timeout", False)
except _http.Timeout:
    check("read timeout raises Timeout", True)
r = s.get(base + "/slow", timeout=(2, None))
check("(connect, None) waits as long as it takes", r.ok)

# A request interrupted half-way (Ctrl-C) must not poison the next one on the
# same thread: the remote proxy cancels calls exactly like this.
import signal  # noqa: E402
signal.signal(signal.SIGALRM, lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
signal.setitimer(signal.ITIMER_REAL, 0.2)
try:
    s.get(base + "/slow", timeout=5)
except KeyboardInterrupt:
    pass
signal.setitimer(signal.ITIMER_REAL, 0)
r = s.get(base + "/a", timeout=2)
check("after an interrupted request the next one on the thread works", r.ok)

free = socket.socket(); free.bind(("127.0.0.1", 0)); port = free.getsockname()[1]; free.close()
try:
    _http.get(f"http://127.0.0.1:{port}/", timeout=1)
    check("refused raises ConnectionError", False)
except _http.ConnectionError:
    check("refused raises ConnectionError", True)
check("both are RequestException (requests.exceptions.RequestException)",
      issubclass(_http.Timeout, _http.exceptions.RequestException)
      and issubclass(_http.ConnectionError, _http.exceptions.RequestException))

errs = []


def worker(i):
    try:
        for k in range(10):
            assert s.post(base + "/p", json={"i": i, "k": k}, timeout=5).json()["echo"] == {"i": i, "k": k}
    except Exception as exc:
        errs.append(repr(exc))


ts = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
for t in ts:
    t.start()
for t in ts:
    t.join()
check("one session used by 8 threads at once", not errs, str(errs[:2]))

real = builtins.__import__


def no_requests(name, *a, **k):
    if name == "requests" or name.startswith("requests."):
        raise ImportError("requests is not installed")
    return real(name, *a, **k)


builtins.__import__ = no_requests
try:
    import importlib
    import sys
    for m in [m for m in sys.modules if m == "ct_simple_control" or m.startswith("ct.")]:
        del sys.modules[m]
    importlib.import_module("ct_simple_control")
    importlib.import_module("ct.server._server")
    check("client and backend import without `requests`", True)
except ImportError as exc:
    check("client and backend import without `requests`", False, str(exc))
finally:
    builtins.__import__ = real
srv.shutdown()

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
