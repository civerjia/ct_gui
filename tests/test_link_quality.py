#!/usr/bin/env python3
"""A controller link that is open but silent is closed and reconnected, and
/api/status reports the bridge's WiFi signal and recent latency. Offline: a
fake bridge (accepts TCP, never answers) and a fake ESP32 HTTP server.

2026-10-05 17:20 Power 1 sat "connected" for minutes with nothing coming back:
reconnecting only happened when the socket closed, and auto-connect only fills
empty slots.

    python3 tests/test_link_quality.py
"""
import _path  # noqa: F401

import json
import os
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.protocol as P  # noqa: E402
import ct.server._link as L  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


# Fake bridge: accepts, reads, never answers.
accepted = []
bridge = socket.socket()
bridge.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
bridge.bind(("127.0.0.1", 0))
bridge.listen(8)


def serve_bridge():
    while True:
        try:
            c, _ = bridge.accept()
        except OSError:
            return
        accepted.append(c)
        threading.Thread(target=lambda c=c: [c.recv(4096) for _ in iter(int, 1)], daemon=True).start()


threading.Thread(target=serve_bridge, daemon=True).start()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = {"/stm32": {"ever_seen": True, "age_ms": 5},
                "/wifi/diag": {"rssi": -67, "channel": 6, "sta_bandwidth": "HT20", "ps": "none"}
                }.get(self.path)
        data = json.dumps(body or {}).encode()
        self.send_response(200 if body else 404)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


http = ThreadingHTTPServer(("127.0.0.1", 0), H)
threading.Thread(target=http.serve_forever, daemon=True).start()

L.BRIDGE_PORT = bridge.getsockname()[1]
P.BRIDGE_HTTP_PORT = http.server_port
L.LINK_SILENT_S = 2.0
L.WIFI_DIAG_PERIOD_S = 0.5
L.BRIDGE_DOWN_REMIND_S = 0.0

link = L.ControllerLink("Power 9")
link.connect("127.0.0.1")
time.sleep(6.5)
check("a connected-but-silent bridge is closed and reconnected", len(accepted) >= 2, str(len(accepted)))

q = link.status()["link"]
check("WiFi signal reported", q["rssi_dbm"] == -67 and q["channel"] == 6, str(q))
check("ESP32 HTTP latency measured", q["esp32_http_avg_ms"] is not None and q["esp32_http_requests"] > 0, str(q))
check("RP2350 timeouts counted (it never answered)", (q["rp2350_timeouts"] or 0) > 0, str(q))
check("RP2350 average is absent, not 0, when nothing answered", q["rp2350_avg_ms"] is None, str(q))
link._running = False
http.shutdown()
bridge.close()

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
