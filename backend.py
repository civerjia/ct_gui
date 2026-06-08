#!/usr/bin/env python3
"""
Web backend for the Multi-Source CT control GUI.

Manages TWO ESP32 bridges (one per power controller). Each bridge exposes the
RP2350B framed protocol on TCP :3333 and the STM32 transparent bridge on :80
(/stm32). A controller carries a filament-index `offset` (0 for filaments
0-47, 48 for 48-95) so a global filament index maps onto the right ESP32.

The protocol/transport layer is reused verbatim from the sibling WiFi GUI
(`../wifi_gui/net_protocol.py`); two independent `TcpProtocolClient`s run side
by side. Liveness for the badges: RP2350 = periodic PING through the bridge,
STM32 = the device's HTTP /stm32 status (age_ms).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# reuse the WiFi GUI transport/protocol layer
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "wifi_gui"))
from net_protocol import (  # noqa: E402
    BRIDGE_PORT,
    TcpProtocolClient,
    fetch_stm32_status,
    scan_for_bridge,
)

# ---------------------------------------------------------------------------
# Command frame builder — matches RP2350bFilamentController/docs/power_state_and_cc.md
# (the firmware protocol is ahead of the WiFi GUI's net_protocol, so we build
# these payloads here rather than via build_command_payload).
# ---------------------------------------------------------------------------
FLAG_SINGLE = 0x10  # kTargetIsSingleBoard


def _u16(v: int) -> bytes:
    return int(v).to_bytes(2, "little")


def build_payload(command: str, b: dict):
    """Return (frame_type, flags, payload) for a single-board command."""
    ch = int(b.get("channel", 0)) & 0xFF
    mux = int(b.get("mux_port", 0)) & 0xFF
    if command == "CH_SET_POWER_STATE":      # 0x35: ch,mux,state,arg16 (IDLE/ACTIVE→mA, VOLTAGE→mV)
        return 0x35, FLAG_SINGLE, bytes([ch, mux, int(b["state"]) & 0xFF]) + _u16(int(b.get("arg", 0)))
    if command == "CH_GET_POWER_STATE":      # 0x36: ch,mux -> status,ch,mux,state,faultKind
        return 0x36, FLAG_SINGLE, bytes([ch, mux])
    if command == "CH_STARTUP_OCP":          # 0x37: get(empty) / set(mA16) startup OCP floor
        return (0x37, 0, _u16(int(b["threshold_mA"]))) if b.get("set") else (0x37, 0, b"")
    if command == "CH_SET_TPS_VOLTAGE":      # 0x22: ch,mux,mV16,enable
        return 0x22, FLAG_SINGLE, bytes([ch, mux]) + _u16(int(b["millivolts"])) + bytes([1 if b.get("enable_after_set", True) else 0])
    if command == "CH_SET_TPS_OCP_THRESHOLD":  # 0x28: ch,mux,mA16 (direct IOUT_LIMIT)
        return 0x28, FLAG_SINGLE, bytes([ch, mux]) + _u16(int(b["threshold_mA"]))
    if command == "CH_GET_INA219":           # 0x24: ch,mux -> status,ch,mux,present,busMv16,mA16
        return 0x24, FLAG_SINGLE, bytes([ch, mux])
    if command == "HV_GET_ALL_BYTES":        # 0x13: desired[8]+feedback[8]
        return 0x13, 0, b""
    if command == "HV_SET_BIT":              # 0x10: ch,bit,value,verifyMode
        mode = 2 if b.get("force") else (1 if b.get("verify", True) else 0)
        return 0x10, 0, bytes([ch, int(b["bit"]) & 0xFF, 1 if b.get("value") else 0, mode])
    if command == "HV_PULSE":                # 0x16: ch,bit,width_us32,verifyMode
        return 0x16, FLAG_SINGLE, bytes([ch, int(b.get("bit", mux)) & 0xFF]) + int(b.get("width_us", 0)).to_bytes(4, "little") + bytes([int(b.get("verify_mode", 0)) & 0xFF])
    raise ValueError(f"unknown command {command}")

STATIC_DIR = Path(__file__).resolve().parent / "static"
PING_TYPE = 0x01
PING_PAYLOAD = (0xCAFEF00D).to_bytes(4, "little")

GEOMETRY = {
    "n_filaments": 96,
    "source_diameter_mm": 341,
    "detector_diameter_mm": 280,
    "collimator_coverage": 35,
    "detector_pixels": 256,
    "detector_pixel_mm": 0.1,
    "gantry_max_deg": 10,
    "filament0_axis": "y+",
    "controllers": 2,
    "channels_used": 6,
    "boards_per_channel": 8,
}


class ControllerLink:
    """One ESP32 bridge + its RP2350B/STM32 liveness, with a filament offset."""

    def __init__(self, name: str, default_offset: int) -> None:
        self.name = name
        self.client = TcpProtocolClient()
        self.host: str | None = None
        self.offset = default_offset
        self._running = False
        self._poll_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.rp_last = 0.0                       # unix time of last good PING
        self.rp_rtt_ms: float | None = None
        self.stm: dict[str, Any] = {}

    def connect(self, host: str, offset: int) -> None:
        with self._lock:
            self._stop_poll()
            self.client.connect(host, BRIDGE_PORT)
            self.host = host
            self.offset = int(offset)
            self.rp_last = 0.0
            self.rp_rtt_ms = None
            self.stm = {}
            self._running = True
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()

    def disconnect(self) -> None:
        with self._lock:
            self._stop_poll()
            with _suppress():
                self.client.disconnect()
            self.host = None

    def set_offset(self, offset: int) -> None:
        self.offset = int(offset)

    def _stop_poll(self) -> None:
        self._running = False
        t = self._poll_thread
        self._poll_thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=0.4)

    def _poll(self) -> None:
        while self._running and self.client.connected:
            t0 = time.monotonic()
            try:
                self.client.send_request(PING_TYPE, PING_PAYLOAD, timeout=0.6)
                self.rp_last = time.time()
                self.rp_rtt_ms = (time.monotonic() - t0) * 1000.0
            except Exception:
                pass
            host = self.host
            if host:
                try:
                    self.stm = fetch_stm32_status(host)
                except Exception as exc:
                    self.stm = {"ever_seen": False, "error": str(exc)}
            time.sleep(1.0)

    def status(self) -> dict[str, Any]:
        now = time.time()
        rp_age = None if self.rp_last == 0 else (now - self.rp_last) * 1000.0
        stm = self.stm or {}
        return {
            "name": self.name,
            "connected": self.client.connected,
            "host": self.host,
            "offset": self.offset,
            "rp2350": {"age_ms": rp_age, "rtt_ms": self.rp_rtt_ms},
            "stm32": {
                "ever_seen": bool(stm.get("ever_seen")),
                "age_ms": stm.get("age_ms"),
                "error": stm.get("error"),
            },
        }


class _suppress:
    def __enter__(self): return self
    def __exit__(self, *a): return True


CONTROLLERS: dict[int, ControllerLink] = {
    1: ControllerLink("Power 1", 0),
    2: ControllerLink("Power 2", 48),
}

STAGED_SCHEDULE: list = []  # last schedule uploaded from the GUI


def do_scan() -> list[dict[str, Any]]:
    """Every host with :3333 open (the ESP32 bridge), newest scan."""
    out = []
    for rec in scan_for_bridge(probe_controller=True):
        bridge = rec.get("bridge", {})
        if not bridge.get("port_open"):
            continue
        out.append({
            "host": rec.get("host"),
            "name": bridge.get("name"),
            "port_open": True,
            "controller_responsive": rec.get("controller", {}).get("responsive", False),
        })
    return out


class CtHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    # --- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/geometry":
            self._json(GEOMETRY)
        elif path == "/api/scan":
            self._json({"results": do_scan()})
        elif path == "/api/status":
            self._json({"controllers": {str(k): c.status() for k, c in CONTROLLERS.items()}})
        else:
            self._serve_static(path)

    # --- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        try:
            if path == "/api/connect":
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                host = str(body.get("host", "")).strip()
                if not host:
                    return self._json({"ok": False, "error": "no host"}, HTTPStatus.BAD_REQUEST)
                link.connect(host, int(body.get("offset", link.offset)))
                self._json({"ok": True, "status": link.status()})
            elif path == "/api/disconnect":
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                link.disconnect()
                self._json({"ok": True, "status": link.status()})
            elif path == "/api/offset":
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                link.set_offset(int(body.get("offset", link.offset)))
                self._json({"ok": True, "status": link.status()})
            elif path == "/api/cmd":
                # Proxy a single RP2350B protocol command to one controller.
                # body: {controller, command, channel, mux_port, target, ...}
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.OK)
                if not link.client.connected:
                    return self._json({"ok": False, "error": f"{link.name} not connected"}, HTTPStatus.OK)
                command = body.get("command", "")
                try:
                    frame_type, flags, payload = build_payload(command, body)
                    resp = link.client.send_request(frame_type, payload, flags=flags, timeout=2.0)
                    self._json({"ok": True, "response": resp})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
            elif path == "/api/schedule":
                # Stage the scan schedule. For now we just validate + retain it;
                # streaming it to the RP2350B schedule table (0x70-0x7B) lands
                # once a controller is connected.
                rows = body.get("rows", [])
                if len(rows) > 8192:
                    return self._json({"ok": False, "error": "exceeds 8192 rows"}, HTTPStatus.OK)
                global STAGED_SCHEDULE
                STAGED_SCHEDULE = rows
                self._json({"ok": True, "rows": len(rows)})
            else:
                self._json({"ok": False, "error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as exc:  # connect failures (refused/timeout) land here
            self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)

    # --- helpers ------------------------------------------------------------
    def _read_json(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            return json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _json(self, obj, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with _suppress():
            self.wfile.write(data)

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
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        with _suppress():
            self.wfile.write(data)


def main() -> None:
    host = os.environ.get("CT_GUI_HOST", "127.0.0.1")
    port = int(os.environ.get("CT_GUI_PORT", "8770"))
    server = ThreadingHTTPServer((host, port), CtHandler)
    print(f"CT GUI server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for c in CONTROLLERS.values():
            c.disconnect()


if __name__ == "__main__":
    main()
