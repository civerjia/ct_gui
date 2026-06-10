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
    build_command_payload,
    fetch_bridge_info,
    fetch_stm32_status,
    scan_for_bridge,
    sync_post_fire,
    sync_post_config,
    sync_post_abort,
    sync_get_status,
    adc_get_burst,
    pulse_events_get,
    stm32_ds3502_get,
    stm32_ds3502_set,
    stm32_hv_enable_set,
    stm32_hv_status,
    stm32_ads1115,
    stm32_hv_set_target,
    stm32_hv_get_target,
    stm32_hv_clear_target,
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


# ---------------------------------------------------------------------------
# Host translation / planning layer (heating_schedule_design.md §9).
#
# The GUI works in logical filament index 0-95. The firmware addresses boards by
# a GLOBAL index 0-127 = controller*64 + ch*8 + pos, each controller owning a
# 64-window via its offset (0 / 64). Default board selection = the first 6
# channels (channel mask 0x3F), so 48 of 64 positions are populated per
# controller. The emission table is the SAME full global list downloaded to both
# controllers (each fires its own scope, counts the rest -> totalPulsesDone is a
# shared global cursor); the heating deltas are split per controller in local
# (channel, position).
# ---------------------------------------------------------------------------
FILAMENTS_PER_CONTROLLER = 48
SCOPE_PER_CONTROLLER = 64
DEFAULT_CHANNELS = [0, 1, 2, 3, 4, 5]   # channel mask 0x3F


def filament_to_board(filament: int, channels=DEFAULT_CHANNELS):
    """logical 0-95 -> (controller{0,1}, channel, position, global_index)."""
    controller = 0 if filament < FILAMENTS_PER_CONTROLLER else 1
    local = filament - controller * FILAMENTS_PER_CONTROLLER   # 0..47
    channel = channels[local // 8]
    position = local % 8
    global_index = controller * SCOPE_PER_CONTROLLER + channel * 8 + position
    return controller, channel, position, global_index


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "little")


# Bound-schedule / config opcodes (firmware ahead of net_protocol; built here).
SHV_SET_OFFSET = 0x70
SHV_GET_OFFSET = 0x71
SHV_CLEAR_TABLE = 0x72
SHV_SET_ENTRIES = 0x73
SHV_GET_TABLE_INFO = 0x74
SHV_SET_CONFIG = 0x75
SHV_GET_CONFIG = 0x76
SHV_ARM = 0x77
SHV_DISARM = 0x78
SHV_GET_STATUS = 0x79
SHV_GET_PULSE_LOG = 0x7A
SHV_CAPABILITY = 0x7B
SHV_HEAT_CLEAR = 0x7C
SHV_HEAT_SET_ENTRIES = 0x7D
CH_FILAMENT_CURRENTS = 0x39
CH_SET_I2C_ENABLE_MASK = 0x34
CH_GET_INA219 = 0x24
CH_GET_PRESENT = 0x25            # I2C presence scan (mux/tps/ina/io per board)
CH_GET_DIAGNOSIS = 0x2E         # deep diagnosis: addr-ACK / reg-read / operational
CH_GET_I2C_ENABLE_MASK = 0x2F   # read the channel enable mask
CH_RESET_MUX = 0x5F             # pulse TCA9548A reset + re-detect (power-cutting)
CH_TCA9554_SELF_TEST = 0x60     # per-pin TCA9554 toggle test
ALL_BOARDS_MASK = bytes([0xFF] * 8)
_DIAG_CHIPS = ["mux", "tps", "ina", "enable_io", "fault_io", "iso_io", "hv_io"]


def _popcount(mask_list) -> int:
    return sum(bin(b & 0xFF).count("1") for b in (mask_list or []))


# All I2C diagnostics mirror the WiFi GUI's tested backend: check the response
# status byte and SURFACE the firmware status name on any non-Ok reply, rather
# than silently returning zeros (which read as "didn't work").
def _status_err(resp, name: str):
    if resp is None:
        return f"{name}: no response"
    if resp.get("status_code") != 0x00:
        return f"{name} returned {resp.get('status')}"
    return None


def read_channel_mask(link: "ControllerLink"):
    """Read the controller's I2C channel enable mask (which channels are used)."""
    try:
        resp = link.client.send_request(CH_GET_I2C_ENABLE_MASK, b"", timeout=1.5)
        if resp.get("status_code") != 0x00:
            return None
        return (resp.get("decoded") or {}).get("mask")
    except Exception:
        return None


def run_chip_health(link: "ControllerLink") -> dict:
    """Presence scan (CH_GET_PRESENT 0x25) + channel mask. Counts read from the
    RAW response (robust), and a non-Ok status surfaces as present_error."""
    out: dict[str, Any] = {"channel_mask": read_channel_mask(link)}
    try:
        resp = link.client.send_request(CH_GET_PRESENT, bytes(ALL_BOARDS_MASK), timeout=3.0)
    except Exception as exc:
        out["present_error"] = str(exc)
        return out
    err = _status_err(resp, "CH_GET_PRESENT")
    if err:
        out["present_error"] = err
        return out
    raw = resp.get("raw") or []          # status, mask[8], then 7×present[8]
    pc = lambda sl: sum(bin(b & 0xFF).count("1") for b in sl)
    out["present_hex"] = resp.get("payload_hex")
    out["present_counts"] = {
        "mux": pc(raw[9:17]), "tps": pc(raw[17:25]), "ina": pc(raw[25:33]),
        "enable_io": pc(raw[33:41]), "fault_io": pc(raw[41:49]),
        "iso_io": pc(raw[49:57]), "hv_io": pc(raw[57:65]),
    }
    return out


CH_GET_BOARD_BITMAPS = 0x26     # iso/tps enable + tps fault + hv overcurrent masks


def board_snapshot(link: "ControllerLink", controller: int, channels=DEFAULT_CHANNELS) -> list:
    """64-board snapshot for the boards matrix: present/tps/ina presence (0x25),
    iso/tps enable + fault (0x26), and INA219 V/I (0x24). Returns 64 board dicts."""
    boards = {}
    for ch in range(8):
        for mux in range(8):
            boards[(ch, mux)] = {
                "channel": ch, "mux_port": mux, "label": f"CH{ch + 1}.{mux + 1}",
                "present": False, "mux_present": False, "tps_present": False, "ina_present": False,
                "iso_enabled": False, "tps_enabled": False, "tps_fault": False,
                "hv_overcurrent": False, "bus_mV": 0, "current_mA": 0,
            }

    def apply_slice(raw, start, field):
        if len(raw) < start + 8:
            return
        for ch in range(8):
            for mux in range(8):
                if raw[start + ch] & (1 << mux):
                    boards[(ch, mux)][field] = True

    try:
        resp = link.client.send_request(CH_GET_PRESENT, bytes(ALL_BOARDS_MASK), timeout=3.0)
        if resp.get("status_code") == 0x00:
            raw = resp.get("raw") or []     # status, mask[8], mux, tps, ina, ...
            # A board is "present" iff its per-board INA219 responds. muxPresent
            # is the per-CHANNEL TCA9548 (probeDevice(kAddrMux)) — it reads true
            # for all 8 ports when the channel's mux chip exists, so it does NOT
            # indicate a plugged-in daughter-board. (Matches the WiFi GUI.)
            apply_slice(raw, 9, "mux_present")
            apply_slice(raw, 17, "tps_present")
            apply_slice(raw, 25, "ina_present")
            apply_slice(raw, 25, "present")
    except Exception:
        pass
    try:
        resp = link.client.send_request(CH_GET_BOARD_BITMAPS, bytes(ALL_BOARDS_MASK), timeout=3.0)
        if resp.get("status_code") == 0x00:
            raw = resp.get("raw") or []     # status, targeted[8], iso_en, tps_en, tps_fault, hv_oc
            apply_slice(raw, 9, "iso_enabled")
            apply_slice(raw, 17, "tps_enabled")
            apply_slice(raw, 25, "tps_fault")
            apply_slice(raw, 33, "hv_overcurrent")
    except Exception:
        pass
    try:
        for fil, t in read_telemetry(link, controller, channels).items():
            ch = channels[(fil - controller * FILAMENTS_PER_CONTROLLER) // 8]
            mux = (fil - controller * FILAMENTS_PER_CONTROLLER) % 8
            boards[(ch, mux)].update(bus_mV=t["bus_mV"], current_mA=t["current_mA"],
                                     ina_present=t["present"], present=t["present"])
    except Exception:
        pass
    return [boards[(ch, mux)] for ch in range(8) for mux in range(8)]


def run_diagnosis(link: "ControllerLink") -> dict:
    """Deep I2C diagnosis (CH_GET_DIAGNOSIS 0x2E): per board+chip classify as
    op / reg-only / addr-only / missing. Surfaces a non-Ok status as an error."""
    try:
        resp = link.client.send_request(CH_GET_DIAGNOSIS, bytes(ALL_BOARDS_MASK), timeout=5.0)
    except Exception as exc:
        return {"error": str(exc)}
    err = _status_err(resp, "CH_GET_DIAGNOSIS")
    if err:
        return {"error": err}
    dec = resp.get("decoded") or {}
    counts = {"op": 0, "reg": 0, "addr": 0, "missing": 0}
    for ch in range(8):
        for mux in range(8):
            bit = 1 << mux
            for chip in _DIAG_CHIPS:
                a = bool((dec.get(f"{chip}_addr_mask") or [0] * 8)[ch] & bit)
                r = bool((dec.get(f"{chip}_reg_mask") or [0] * 8)[ch] & bit)
                o = bool((dec.get(f"{chip}_op_mask") or [0] * 8)[ch] & bit)
                counts["op" if o else "reg" if r else "addr" if a else "missing"] += 1
    return {"diagnosis_counts": counts}


def run_self_test(link: "ControllerLink") -> dict:
    """TCA9554 toggle self-test (CH_TCA9554_SELF_TEST 0x60; drives output pins —
    bench/idle only). Surfaces a non-Ok firmware status instead of zeros."""
    try:
        resp = link.client.send_request(CH_TCA9554_SELF_TEST, bytes(ALL_BOARDS_MASK), timeout=5.0)
    except Exception as exc:
        return {"selftest_error": str(exc)}
    err = _status_err(resp, "CH_TCA9554_SELF_TEST")
    if err:
        return {"selftest_error": err}
    st = resp.get("decoded") or {}
    return {
        "selftest_counts": {
            "enable_toggle": _popcount(st.get("enable_toggle_mask")),
            "iso_toggle": _popcount(st.get("iso_toggle_mask")),
            "outputs_tested_channels": bin(int(st.get("outputs_tested_mask") or 0) & 0xFF).count("1"),
        },
    }


def read_telemetry(link: "ControllerLink", controller: int, channels=DEFAULT_CHANNELS) -> dict:
    """Batch-read every populated board's INA219 (multi-board paged 0x24) and map
    local (channel, mux) → logical filament 0-95. One controller = its 48."""
    mask = bytearray(8)
    for c in channels:
        mask[c] = 0xFF
    out: dict[int, dict] = {}
    page_start, max_entries, guard = 0, 16, 0
    while guard < 16:
        guard += 1
        payload = bytes(mask) + bytes([page_start & 0xFF, max_entries & 0xFF])
        resp = link.request(CH_GET_INA219, payload, flags=0, timeout=1.5)
        dec = resp.get("decoded") if isinstance(resp, dict) else None
        entries = (dec or {}).get("entries", [])
        total = (dec or {}).get("total_matching_entries", 0)
        for e in entries:
            ch, mux = e.get("channel"), e.get("mux_port")
            if ch not in channels:
                continue
            fil = controller * FILAMENTS_PER_CONTROLLER + channels.index(ch) * 8 + mux
            out[fil] = {
                "index": fil, "present": bool(e.get("present")),
                "bus_mV": e.get("bus_mV", 0), "current_mA": e.get("current_mA", 0),
            }
        if not entries or len(out) >= total or len(entries) < max_entries:
            break
        page_start += len(entries)
    return out

SHV_EMIT_CHUNK = 64    # emission entries per frame (64*4+3 = 259 B)
SHV_HEAT_CHUNK = 56    # firmware caps ShvHeatSetEntries at 56


def _status_ok(resp) -> bool:
    raw = resp.get("raw") if isinstance(resp, dict) else None
    return bool(raw) and raw[0] == 0x00


def _le(raw, off, n):
    return sum(raw[off + i] << (8 * i) for i in range(n))


def shv_op(link: "ControllerLink", body: dict) -> dict:
    """Dispatch one Simple-HV-schedule operation on a controller (ShV panel)."""
    op = body.get("op")
    if op == "set_offset":
        off = int(body.get("offset", 0)) & 0xFF
        ok = _status_ok(link.request(SHV_SET_OFFSET, bytes([off])))
        if ok:
            link.device_offset = off
        return {"ok": ok}
    if op == "get_offset":
        raw = link.request(SHV_GET_OFFSET, b"").get("raw") or []
        return {"ok": bool(raw) and raw[0] == 0, "offset": raw[1] if len(raw) > 1 else None}
    if op == "clear_table":
        return {"ok": _status_ok(link.request(SHV_CLEAR_TABLE, b""))}
    if op == "set_entries":
        entries = body.get("entries") or []
        ent = bytearray()
        for e in entries:
            ent += bytes([int(e["filament"]) & 0xFF, int(e["numPulses"]) & 0xFF]) + _u16(int(e["width"]))
        ok = True
        for start in range(0, len(entries), SHV_EMIT_CHUNK):
            cnt = min(SHV_EMIT_CHUNK, len(entries) - start)
            payload = _u16(start) + bytes([cnt]) + bytes(ent[start * 4:(start + cnt) * 4])
            ok = _status_ok(link.request(SHV_SET_ENTRIES, payload)) and ok
        return {"ok": ok, "count": len(entries)}
    if op == "table_info":
        raw = link.request(SHV_GET_TABLE_INFO, b"").get("raw") or []
        if raw and raw[0] == 0 and len(raw) >= 7:
            return {"ok": True, "entryCount": _le(raw, 1, 2), "crc": _le(raw, 3, 4)}
        return {"ok": False}
    if op == "set_config":
        payload = (_u32(int(body.get("interPulseMs", 3000))) + _u16(int(body.get("maxOnMs", 40)))
                   + _u32(int(body.get("totalMs", 60000))) + bytes([int(body.get("triggerEdge", 0)) & 0xFF]))
        return {"ok": _status_ok(link.request(SHV_SET_CONFIG, payload))}
    if op == "get_config":
        raw = link.request(SHV_GET_CONFIG, b"").get("raw") or []
        if raw and raw[0] == 0 and len(raw) >= 12:
            return {"ok": True, "interPulseMs": _le(raw, 1, 4), "maxOnMs": _le(raw, 5, 2),
                    "totalMs": _le(raw, 7, 4), "triggerEdge": raw[11]}
        return {"ok": False}
    if op == "arm":
        resp = link.request(SHV_ARM, _u16(max(1, int(body.get("repeats", 1)))))
        raw = resp.get("raw") or []
        reject = raw[1] if len(raw) > 1 else None
        return {"ok": bool(raw) and raw[0] == 0 and reject == 0, "reject": reject}
    if op == "disarm":
        return {"ok": _status_ok(link.request(SHV_DISARM, b""))}
    if op == "status":
        return {"ok": True, "status": decode_shv_status(link.request(SHV_GET_STATUS, b""))}
    if op == "pulse_log":
        raw = link.request(SHV_GET_PULSE_LOG, _u16(int(body.get("start", 0)))).get("raw") or []
        recs = []
        if raw and raw[0] == 0 and len(raw) >= 6:
            n, off = raw[5], 6
            for _ in range(n):
                if off + 12 > len(raw):
                    break
                recs.append({"filament": raw[off], "flags": raw[off + 1], "seq": _le(raw, off + 2, 2),
                             "tOnUs": _le(raw, off + 4, 4), "durationUs": _le(raw, off + 8, 2)})
                off += 12
            return {"ok": True, "total": _le(raw, 1, 2), "records": recs}
        return {"ok": False}
    if op == "capability":
        ch = int(body.get("channel", 0)) & 0xFF
        pairs = body.get("pairs") or []
        payload = bytes([ch, len(pairs) & 0xFF])
        for pr in pairs:
            payload += bytes([int(pr["bit"]) & 0xFF]) + _u16(int(pr["width"]))
        resp = link.request(SHV_CAPABILITY, payload, timeout=5.0)
        raw = resp.get("raw") or []   # status, reject, channel, count, [bit, measuredUs]×count
        out = {"ok": _status_ok(resp)}
        if raw and raw[0] == 0 and len(raw) >= 4:
            out["reject"] = raw[1]
            results, off = [], 4
            for _ in range(raw[3]):
                if off + 3 > len(raw):
                    break
                results.append({"bit": raw[off], "measuredUs": _le(raw, off + 1, 2)})
                off += 3
            out["results"] = results
        return out
    return {"ok": False, "error": "unknown op"}


def decode_shv_status(resp) -> dict[str, Any] | None:
    """Decode a ShvGetStatus (0x79) response payload."""
    raw = resp.get("raw") if isinstance(resp, dict) else None
    if not raw or len(raw) < 21 or raw[0] != 0x00:
        return None
    p = bytes(raw)
    le16 = lambda o: p[o] | (p[o + 1] << 8)
    le32 = lambda o: p[o] | (p[o + 1] << 8) | (p[o + 2] << 16) | (p[o + 3] << 24)
    return {
        "state": p[1],
        "stopReason": p[2],
        "entryIndex": le16(3),
        "filamentIndex": p[5],            # current GLOBAL filament (0xFF = none)
        "pulsesThisFilament": p[6],
        "entryCount": le16(7),
        "totalPulsesTarget": le32(9),
        "totalPulsesDone": le32(13),      # the global playhead cursor
        "elapsedMs": le32(17),
        "faultFilament": p[21] if len(p) > 21 else 0xFF,
    }

STATIC_DIR = Path(__file__).resolve().parent / "static"
PING_TYPE = 0x01
PING_PAYLOAD = (0xCAFEF00D).to_bytes(4, "little")

GEOMETRY = {
    "n_filaments": 96,
    "source_diameter_mm": 436,
    "detector_diameter_mm": 308,
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
        self.device_offset: int | None = None    # RP2350B flash-persisted offset (0/64)
        self.bridge_name: str | None = None       # ESP32 AP SSID (MAC-derived identity)

    def connect(self, host: str, offset: int) -> None:
        with self._lock:
            self._stop_poll()
            self.client.connect(host, BRIDGE_PORT)
            self.host = host
            self.offset = int(offset)
            self.rp_last = 0.0
            self.rp_rtt_ms = None
            self.stm = {}
            self.device_offset = None
            self.bridge_name = None
            self._running = True
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()
        self._read_identity(host)

    def _read_identity(self, host: str) -> None:
        """Read the board's flash offset (ShvGetOffset) + AP SSID so two
        controllers can be told apart and their persisted role surfaced."""
        try:
            resp = self.client.send_request(SHV_GET_OFFSET, b"", timeout=1.0)
            raw = resp.get("raw") if isinstance(resp, dict) else None
            if raw and len(raw) >= 2 and raw[0] == 0:
                self.device_offset = raw[1]
        except Exception:
            self.device_offset = None
        try:
            info = fetch_bridge_info(host)
            self.bridge_name = info.get("ap_ssid") if info else None
        except Exception:
            self.bridge_name = None

    def disconnect(self) -> None:
        with self._lock:
            self._stop_poll()
            with _suppress():
                self.client.disconnect()
            self.host = None

    def set_offset(self, offset: int) -> None:
        self.offset = int(offset)

    def request(self, frame_type: int, payload: bytes = b"", flags: int = 0, timeout: float = 2.0):
        """Send one framed command and return the decoded response (raises if down)."""
        if not self.client.connected:
            raise RuntimeError(f"{self.name} not connected")
        return self.client.send_request(frame_type, payload, flags=flags, timeout=timeout)

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
            "device_offset": self.device_offset,
            "bridge_name": self.bridge_name,
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


# ---------------------------------------------------------------------------
# Bound-schedule download (host translation layer).
#
# Plan from the GUI (logical filament indices 0-95):
#   emission : [{filament, numPulses, widthUs}]  ordered by firing sequence
#   heating  : [{filament, triggerIndex, state, milliamps}]  trigger-indexed deltas
#   currents : {filament: {idle_mA, active_mA}}
#   config   : {interPulseMs, maxOnMs, totalMs, triggerEdge}
#   repeats  : loop count
# ---------------------------------------------------------------------------
def download_to_controller(link: "ControllerLink", controller: int, plan: dict,
                           channels=DEFAULT_CHANNELS) -> dict:
    steps: list[dict] = []

    def step(name, resp):
        steps.append({"step": name, "ok": _status_ok(resp)})
        return _status_ok(resp)

    offset = controller * SCOPE_PER_CONTROLLER
    mask = 0
    for c in channels:
        mask |= (1 << c)

    # 1. offset + channel mask (board selection / I2C-skip)
    step("offset", link.request(SHV_SET_OFFSET, bytes([offset & 0xFF]), flags=0))
    step("mask", link.request(CH_SET_I2C_ENABLE_MASK, bytes([mask & 0xFF]), flags=0))

    # 2. per-filament IDLE/ACTIVE current calibration (this controller's boards)
    for fil, cur in (plan.get("currents") or {}).items():
        f = int(fil)
        ctrl, ch, pos, _ = filament_to_board(f, channels)
        if ctrl != controller:
            continue
        payload = bytes([ch, pos]) + _u16(int(cur.get("idle_mA", 0))) + _u16(int(cur.get("active_mA", 0)))
        link.request(CH_FILAMENT_CURRENTS, payload, flags=FLAG_SINGLE)
    steps.append({"step": "currents", "ok": True})

    # 3. config
    cfg = plan.get("config") or {}
    cfg_payload = (_u32(int(cfg.get("interPulseMs", 3000))) + _u16(int(cfg.get("maxOnMs", 40)))
                   + _u32(int(cfg.get("totalMs", 60000))) + bytes([int(cfg.get("triggerEdge", 0)) & 0xFF]))
    step("config", link.request(SHV_SET_CONFIG, cfg_payload, flags=0))

    # 4. emission table — the SAME full global list to BOTH controllers
    step("emit_clear", link.request(SHV_CLEAR_TABLE, b"", flags=0))
    emit = plan.get("emission") or []
    ent = bytearray()
    for e in emit:
        _, _, _, g = filament_to_board(int(e["filament"]), channels)
        ent += bytes([g & 0xFF, int(e["numPulses"]) & 0xFF]) + _u16(int(e["widthUs"]))
    n = len(emit)
    for start in range(0, n, SHV_EMIT_CHUNK):
        count = min(SHV_EMIT_CHUNK, n - start)
        body = _u16(start) + bytes([count]) + bytes(ent[start * 4:(start + count) * 4])
        step(f"emit[{start}]", link.request(SHV_SET_ENTRIES, body, flags=0))

    # 5. heating deltas — only THIS controller's filaments, local (ch, pos)
    step("heat_clear", link.request(SHV_HEAT_CLEAR, b"", flags=0))
    heat = [h for h in (plan.get("heating") or [])
            if filament_to_board(int(h["filament"]), channels)[0] == controller]
    heat.sort(key=lambda h: int(h["triggerIndex"]))
    hent = bytearray()
    for h in heat:
        _, ch, pos, _ = filament_to_board(int(h["filament"]), channels)
        hent += (_u16(int(h["triggerIndex"])) + bytes([ch, pos, int(h["state"]) & 0xFF, 0])
                 + _u16(int(h.get("milliamps", 0))))
    hn = len(heat)
    for start in range(0, hn, SHV_HEAT_CHUNK):
        count = min(SHV_HEAT_CHUNK, hn - start)
        body = _u16(start) + bytes([count]) + bytes(hent[start * 8:(start + count) * 8])
        step(f"heat[{start}]", link.request(SHV_HEAT_SET_ENTRIES, body, flags=0))

    ok = all(s["ok"] for s in steps)
    return {"controller": controller, "ok": ok, "emit": n, "heat": hn, "steps": steps}


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
        elif path == "/api/telemetry":
            # Real per-filament telemetry for the ring: batch INA219 (V/I) per
            # connected controller, merged by the board map. Plus the live firing
            # filament from ShvGetStatus (no batch power-state read exists).
            rows: dict[int, dict] = {}
            firing = []
            run_state = {}
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                running = False
                try:
                    st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                    if st:
                        run_state[str(cid)] = st
                        running = st.get("state") == 2
                        fi = st.get("filamentIndex")
                        if running and fi is not None and fi != 0xFF:
                            # firmware global index 0-127 -> logical 0-95
                            ctrl = 0 if fi < SCOPE_PER_CONTROLLER else 1
                            local = fi - ctrl * SCOPE_PER_CONTROLLER
                            firing.append(ctrl * FILAMENTS_PER_CONTROLLER + local)
                except Exception:
                    pass
                # Skip the INA mux sweep while firing — it shares the I2C bus and
                # would stall pulses (simple_hv_schedule_design.md). ShvGetStatus
                # carries the firing filament during a run.
                if not running:
                    try:
                        rows.update(read_telemetry(link, cid - 1))
                    except Exception:
                        pass
            self._json({"telemetry": list(rows.values()), "firing": firing, "run": run_state})
        elif path == "/api/board-snapshot":
            # 64-board matrix for the selected controller (?controller=N).
            q = self.path.split("?", 1)
            cid = 1
            if len(q) > 1:
                for kv in q[1].split("&"):
                    if kv.startswith("controller="):
                        cid = int(kv.split("=", 1)[1] or 1)
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                self._json({"ok": False, "error": "controller not connected", "boards": []})
            else:
                try:
                    self._json({"ok": True, "boards": board_snapshot(link, cid - 1)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc), "boards": []})
        elif path == "/api/hv-snapshot":
            link = self._target_link()
            if not link or not link.client.connected:
                self._json({"ok": False, "error": "controller not connected"})
            else:
                try:
                    dec = link.request(0x13, b"", flags=0, timeout=2.0).get("decoded") or {}
                    self._json({"ok": True, "desired": dec.get("desired", [0] * 8), "feedback": dec.get("feedback", [0] * 8)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)})
        elif path == "/api/adc/burst":
            host, err = self._target_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                n = int(self._query().get("n", "2048"))
                r = adc_get_burst(host, n)
                if not r.get("ok"):
                    self._json({"ok": False, "error": r.get("error", "burst failed")})
                else:
                    raw = r["bytes"]
                    samples = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw) - 1, 2)]
                    hdr = r.get("headers", {})
                    self._json({"ok": True, "samples": samples,
                                "rate_hz": int(hdr.get("X-ADC-Rate-Hz", hdr.get("x-adc-rate-hz", 0)) or 0),
                                "bits": int(hdr.get("X-ADC-Bits", hdr.get("x-adc-bits", 12)) or 12)})
        elif path == "/api/pulse-events":
            host, err = self._target_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                self._json(pulse_events_get(host, int(self._query().get("since", "0"))))
        elif path == "/api/stm32/ads1115":
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else stm32_ads1115(host))
        elif path == "/api/stm32/hv-status":
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else stm32_hv_status(host))
        elif path == "/api/stm32/ds3502":
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else stm32_ds3502_get(host, self._query().get("ch", "ev")))
        elif path == "/api/sync/status":
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else sync_get_status(host))
        elif path == "/api/stm32/hv-target":
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else stm32_hv_get_target(host, self._query().get("chan", "emission")))
        elif path == "/api/run-status":
            # Poll ShvGetStatus (0x79) from each connected controller. totalPulsesDone
            # is the shared global playhead; filamentIndex is the live firing filament.
            out = {}
            for k, c in CONTROLLERS.items():
                if not c.client.connected:
                    out[str(k)] = {"connected": False}
                    continue
                try:
                    st = decode_shv_status(c.request(SHV_GET_STATUS, b"", timeout=1.0))
                    out[str(k)] = {"connected": True, "status": st, "offset": c.offset}
                except Exception as exc:
                    out[str(k)] = {"connected": True, "error": str(exc)}
            self._json({"controllers": out})
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
                if not link.client.connected:
                    # Port opened but the session dropped — the bridge is single-
                    # client. Tell the user instead of a silent offline state.
                    busy = False
                    try:
                        info = fetch_bridge_info(host)
                        busy = bool(info and info.get("tcp_client_busy"))
                    except Exception:
                        pass
                    err = ("bridge slot already owned by another client — disconnect it first"
                           if busy else "bridge accepted then closed the connection")
                    return self._json({"ok": False, "error": err, "status": link.status()}, HTTPStatus.OK)
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
                off = int(body.get("offset", link.offset))
                link.set_offset(off)
                # Persist to the board's flash so its role survives reboot.
                if link.client.connected:
                    try:
                        resp = link.request(SHV_SET_OFFSET, bytes([off & 0xFF]), flags=0)
                        if _status_ok(resp):
                            link.device_offset = off
                        else:
                            return self._json({"ok": False, "error": "device rejected offset", "status": link.status()}, HTTPStatus.OK)
                    except Exception as exc:
                        return self._json({"ok": False, "error": str(exc), "status": link.status()}, HTTPStatus.OK)
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
            elif path == "/api/download":
                # Download the bound schedule + config to every connected controller.
                plan = body.get("plan") or {}
                channels = body.get("channels") or DEFAULT_CHANNELS
                results = []
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results.append(download_to_controller(link, cid - 1, plan, channels))
                    except Exception as exc:
                        results.append({"controller": cid - 1, "ok": False, "error": str(exc)})
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                self._json({"ok": all(r.get("ok") for r in results), "results": results})
            elif path == "/api/arm":
                repeats = int(body.get("repeats", 1))
                payload = _u16(max(1, repeats))
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        resp = link.request(SHV_ARM, payload, flags=0)
                        raw = resp.get("raw") if isinstance(resp, dict) else None
                        reject = raw[1] if raw and len(raw) > 1 else None
                        results[str(cid)] = {"ok": bool(raw) and raw[0] == 0 and reject == 0, "reject": reject}
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": all(r.get("ok") for r in results.values()) if results else False,
                            "results": results})
            elif path == "/api/disarm":
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results[str(cid)] = {"ok": _status_ok(link.request(SHV_DISARM, b"", flags=0))}
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": True, "results": results})
            elif path == "/api/present":
                # I2C presence scan per connected controller (read-only, safe any time).
                out = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        out[str(cid)] = run_chip_health(link)
                    except Exception as exc:
                        out[str(cid)] = {"present_error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/selftest":
                # TCA9554 toggle self-test — drives pins, so refuse while a
                # schedule is running on that controller.
                out = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                        if st and st.get("state") == 2:
                            out[str(cid)] = {"selftest_error": "controller is running a schedule — disarm first"}
                            continue
                        res = run_chip_health(link)
                        res.update(run_self_test(link))
                        out[str(cid)] = res
                    except Exception as exc:
                        out[str(cid)] = {"selftest_error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/diagnosis":
                # Deep I2C diagnosis per connected controller (read-only, ~300 ms).
                out = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        res = {"channel_mask": read_channel_mask(link)}
                        res.update(run_diagnosis(link))
                        out[str(cid)] = res
                    except Exception as exc:
                        out[str(cid)] = {"error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/channel-mask":
                # Set the channel enable mask on connected controllers (0x34).
                mask = int(body.get("mask", 0x3F)) & 0xFF
                only = body.get("controller")
                out = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected or (only and int(only) != cid):
                        continue
                    try:
                        resp = link.request(CH_SET_I2C_ENABLE_MASK, bytes([mask]), flags=0)
                        dec = resp.get("decoded") if isinstance(resp, dict) else None
                        out[str(cid)] = {"ok": _status_ok(resp), "mask": (dec or {}).get("mask", mask)}
                    except Exception as exc:
                        out[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/mux-reset":
                # Pulse TCA9548A reset + re-detect. Power-cutting (outputs drop low).
                out = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        resp = link.request(CH_RESET_MUX, ALL_BOARDS_MASK, flags=0, timeout=5.0)
                        out[str(cid)] = {"ok": _status_ok(resp)}
                    except Exception as exc:
                        out[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/trigger":
                # Bench test: pulse SyncIn `count` times via the ESP32 bridge's
                # /sync/fire. host defaults to the first connected controller.
                count = max(1, int(body.get("count", 1)))
                host = body.get("host")
                if not host:
                    for link in CONTROLLERS.values():
                        if link.client.connected and link.host:
                            host = link.host
                            break
                if not host:
                    return self._json({"ok": False, "error": "no host"}, HTTPStatus.OK)
                fired = 0
                last = {}
                for _ in range(count):
                    last = sync_post_fire(host)
                    if not last.get("ok"):
                        break
                    fired += 1
                self._json({"ok": fired == count, "fired": fired, "last": last})
            elif path == "/api/power-cmd":
                # Direct power-plane command to ONE controller, built via the
                # WiFi GUI's build_command_payload (board/HV/TPS/INA opcodes,
                # single or multi-board). body: {controller, command, ...}.
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.OK)
                if not link.client.connected:
                    return self._json({"ok": False, "error": f"{link.name} not connected"}, HTTPStatus.OK)
                try:
                    ft, flags, payload = build_command_payload(str(body.get("command", "")), body)
                    resp = link.client.send_request(ft, payload, flags=flags, timeout=2.5)
                    self._json({"ok": _status_ok(resp), "response": resp})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
            elif path == "/api/stm32/ds3502-set":
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.host:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(stm32_ds3502_set(link.host, str(body.get("ch", "ev")), int(body.get("wiper", 0))))
            elif path == "/api/stm32/hv-enable":
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.host:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(stm32_hv_enable_set(link.host, str(body.get("ch", "emission")), bool(body.get("on"))))
            elif path == "/api/stm32/hv-set-target":
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.host:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(stm32_hv_set_target(link.host, str(body.get("chan", "emission")),
                                               int(body.get("target", 0)), int(body.get("tol", 4)),
                                               int(body.get("max_step", 1))))
            elif path == "/api/stm32/hv-clear-target":
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.host:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(stm32_hv_clear_target(link.host, str(body.get("chan", "emission"))))
            elif path in ("/api/sync/config", "/api/sync/fire", "/api/sync/abort"):
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.host:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                if path == "/api/sync/fire":
                    self._json(sync_post_fire(link.host))
                elif path == "/api/sync/abort":
                    self._json(sync_post_abort(link.host))
                else:
                    fields = {k: str(body[k]) for k in ("sync_out_edge", "sync_out_width_us", "ready_active", "ext_trig_edge") if k in body}
                    self._json(sync_post_config(link.host, fields))
            elif path == "/api/shv":
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(shv_op(link, body))
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
    def _query(self) -> dict[str, str]:
        q = self.path.split("?", 1)
        out: dict[str, str] = {}
        if len(q) > 1:
            for kv in q[1].split("&"):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    out[k] = v
        return out

    def _target_link(self):
        return CONTROLLERS.get(int(self._query().get("controller", "1") or 1))

    def _target_host(self):
        link = self._target_link()
        if not link or not link.client.connected or not link.host:
            return None, "controller not connected"
        return link.host, None

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
