#!/usr/bin/env python3
"""
Static web backend for the Multi-Source CT control GUI.

For now this only serves the static frontend (the geometry view runs entirely
in the browser). It is intentionally shaped like ``tools/wifi_gui/backend.py``
so the hardware-control routes (ESP32 bridge / RP2350B schedule firing) can be
grafted on later under ``/api/*`` without reworking the server.
"""

from __future__ import annotations

import json
import os
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC_DIR = Path(__file__).resolve().parent / "static"

# Machine geometry — single source of truth shared with the frontend so a future
# /api/geometry route and the JS constants never drift apart.
GEOMETRY = {
    "n_filaments": 96,
    "source_diameter_mm": 341,
    "detector_diameter_mm": 280,
    "collimator_coverage": 35,
    "detector_pixels": 256,
    "detector_pixel_mm": 0.1,
    "gantry_max_deg": 10,
    "filament0_axis": "y+",
}


class CtHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # quieter console
        pass

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/geometry":
            self._send_json(GEOMETRY)
            return
        self._serve_static(path)

    def _send_json(self, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, path: str) -> None:
        if path == "/":
            path = "/index.html"
        file_path = (STATIC_DIR / path.lstrip("/")).resolve()
        if not str(file_path).startswith(str(STATIC_DIR)) or not file_path.exists():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(file_path.suffix, "text/plain")
        data = file_path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        # No caching so an edited static file is always served fresh.
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def main() -> None:
    host = os.environ.get("CT_GUI_HOST", "127.0.0.1")
    port = int(os.environ.get("CT_GUI_PORT", "8770"))
    server = ThreadingHTTPServer((host, port), CtHandler)
    print(f"CT GUI server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
