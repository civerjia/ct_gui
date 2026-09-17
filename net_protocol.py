#!/usr/bin/env python3
"""
UART protocol transport and frame codec for the RP2350B filament controller,
reached over TCP via the ESP32-S3 Pico bridge.

This module is the WiFi-GUI counterpart of the UART GUI's `uart_protocol.py`.
The frame codec, decoder branches, and `build_command_payload` are byte-for-byte
the same — the ESP32 bridge is a transparent byte pipe (see the bridge project's
`architecture.md`). Only the transport layer is different: `socket` instead of
`pyserial`.

If you add a new UART command or event in the firmware, keep this file and the
UART GUI's `uart_protocol.py` in sync. The two files deliberately do not share
a Python package — the GUIs are meant to be independently copy-deployable onto a
bench laptop without dragging each other's transport deps along.
"""

from __future__ import annotations

import json
import re
import socket
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import suppress
from dataclasses import dataclass
from html import unescape
from queue import Empty, Queue
from typing import Any


SOF0 = 0xA5
SOF1 = 0x5A
VERSION = 0x01
MAX_PAYLOAD = 512
TPS_OCP_SENSE_RESISTOR_OHMS = 0.015

FLAG_ACK_REQUIRED = 1 << 0
FLAG_IS_RESPONSE = 1 << 1
FLAG_IS_ERROR = 1 << 2
FLAG_IS_EVENT = 1 << 3
FLAG_TARGET_IS_SINGLE_BOARD = 1 << 4


TYPE_NAMES = {
    0x01: "PING",
    0x02: "GET_INFO",
    0x03: "GET_STATUS",
    0x04: "CLEAR_FAULTS",
    0x05: "ENTER_SAFE_STATE",
    0x06: "SET_EVENT_CONFIG",
    0x07: "GET_EVENT_CONFIG",
    0x10: "HV_SET_BIT",
    0x11: "HV_SET_CHANNEL_BYTE",
    0x12: "HV_GET_CHANNEL_BYTE",
    0x13: "HV_GET_ALL_BYTES",
    0x14: "HV_REFRESH_FEEDBACK",
    0x15: "HV_SET_MULTI_CHANNEL",
    0x16: "HV_PULSE",
    0x20: "CH_SET_ISO_ENABLE",
    0x21: "CH_SET_TPS_ENABLE",
    0x22: "CH_SET_TPS_VOLTAGE",
    0x23: "CH_GET_TPS_STATUS",
    0x24: "CH_GET_INA219",
    0x3A: "CH_GET_CACHED_CURRENTS",
    0x25: "CH_GET_PRESENT",
    0x26: "CH_GET_BOARD_BITMAPS",
    0x27: "CH_GET_TPS_STATUS_PAGE",
    0x28: "CH_SET_TPS_OCP_THRESHOLD",
    0x29: "CH_READ_TPS_REGISTER",
    0x2A: "CH_WRITE_TPS_REGISTER",
    0x2B: "CH_READ_INA219_REGISTER",
    0x2C: "CH_WRITE_INA219_REGISTER",
    0x2D: "CH_SET_INA219_PGA",
    0x2E: "CH_GET_DIAGNOSIS",
    0x5F: "CH_RESET_MUX",
    0x60: "CH_TCA9554_SELF_TEST",
    0x61: "CH_READ_TCA9554",
    0x2F: "CH_GET_I2C_ENABLE_MASK",
    0x34: "CH_SET_I2C_ENABLE_MASK",
    0x30: "RUN_SCAN",
    0x31: "RUN_SELF_TEST",
    0x32: "RUN_HV_SELF_TEST",
    0x33: "RUN_I2C_SELF_TEST",
    0x40: "EVENT_FAULT",
    0x41: "EVENT_STATE_CHANGE",
    0x42: "EVENT_TEST_RESULT",
    0x43: "EVENT_TELEMETRY",
    0x44: "EVENT_SCHEDULE_STATE",
    0x45: "EVENT_SCHEDULE_COMPLETE",
    0x46: "EVENT_SCHEDULE_ERROR",
    0x47: "EVENT_HEARTBEAT",
    0x50: "HV_SCHED_GET_CAPS",
    0x51: "HV_SCHED_GET_ENTRY",
    0x52: "HV_SCHED_SET_ENTRY",
    0x53: "HV_SCHED_GET_ENABLE_MATRIX",
    0x54: "HV_SCHED_SET_ENABLE_MATRIX",
    0x55: "HV_SCHED_SET_ENABLE_BIT",
    0x56: "HV_SCHED_CLEAR",
    0x57: "HV_SCHED_GET_STATUS",
    0x58: "HV_SCHED_ARM",
    0x59: "HV_SCHED_START",
    0x5A: "HV_SCHED_STOP",
    0x5B: "HV_SCHED_DISARM",
    0x5C: "HV_SCHED_GET_EVENT_LOG",
    0x5D: "HV_SCHED_CLEAR_EVENT_LOG",
    0x5E: "HV_SCHED_CLEAR_ERRORS",
}


STATUS_NAMES = {
    0x00: "OK",
    0x01: "BAD_FRAME",
    0x02: "BAD_CRC",
    0x03: "BAD_VERSION",
    0x04: "UNKNOWN_TYPE",
    0x05: "BAD_LENGTH",
    0x06: "BAD_ARGUMENT",
    0x07: "INVALID_CHANNEL",
    0x08: "INVALID_BIT",
    0x09: "INVALID_MUX_PORT",
    0x0A: "NOT_READY",
    0x0B: "BUS_ERROR",
    0x0C: "VERIFY_FAIL",
    0x0D: "FAULT_ACTIVE",
    0x0E: "UNSUPPORTED",
    0x0F: "BUSY",
    0x10: "INTERNAL_ERROR",
    0x11: "STATE_CONFLICT",
}


SCHEDULE_STATE_NAMES = {
    0: "Idle",
    1: "Armed",
    2: "Running",
    3: "Complete",
    4: "Fault",
}


SCHEDULE_REJECT_REASONS = {
    0: "None",
    1: "TpsDisabled",
    2: "TpsFault",
    3: "IsoOff",
    4: "InvalidEntry",
    5: "PulseBelowMin",
}


SCHEDULE_VERIFY_MODES = {
    0: "POST_ONLY",
    1: "PRE_POST",
    2: "PRE_POST_LEVEL",
    3: "NONE",
}


def decode_u16(payload: bytes, offset: int) -> int:
    return payload[offset] | (payload[offset + 1] << 8)


def decode_u32(payload: bytes, offset: int) -> int:
    return (
        payload[offset]
        | (payload[offset + 1] << 8)
        | (payload[offset + 2] << 16)
        | (payload[offset + 3] << 24)
    )


def status_name(code: int | None) -> str | None:
    return STATUS_NAMES.get(code, None) if code is not None else None


def u16(value: int) -> bytes:
    return int(value).to_bytes(2, "little", signed=False)


def u32(value: int) -> bytes:
    return int(value).to_bytes(4, "little", signed=False)


def parse_mask(mask_value: Any) -> bytes:
    if isinstance(mask_value, (bytes, bytearray)) and len(mask_value) == 8:
        return bytes(mask_value)
    if isinstance(mask_value, list) and len(mask_value) == 8:
        return bytes(int(v) & 0xFF for v in mask_value)
    if isinstance(mask_value, str):
        parts = [part.strip() for part in mask_value.replace(",", " ").split() if part.strip()]
        if len(parts) != 8:
            raise ValueError("board mask string must contain 8 bytes")
        return bytes(int(part, 0) & 0xFF for part in parts)
    raise ValueError("board mask must be an 8-byte list or string")


def all_boards_mask() -> list[int]:
    return [0xFF] * 8


def single_board_mask(channel: int, mux_port: int) -> list[int]:
    mask = [0] * 8
    mask[channel] = 1 << mux_port
    return mask


def popcount8(value: int) -> int:
    value &= 0xFF
    count = 0
    while value:
        count += value & 1
        value >>= 1
    return count


def count_mask_bits(mask: list[int]) -> int:
    return sum(popcount8(v) for v in mask)


def crc16_ccitt_false(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


@dataclass
class Frame:
    version: int
    type: int
    flags: int
    seq: int
    payload: bytes

    def encode(self) -> bytes:
        header = bytes(
            [
                self.version,
                self.type,
                self.flags,
                self.seq,
                len(self.payload) & 0xFF,
                (len(self.payload) >> 8) & 0xFF,
            ]
        )
        crc = crc16_ccitt_false(header + self.payload)
        return bytes([SOF0, SOF1]) + header + self.payload + crc.to_bytes(2, "little")


class FrameParser:
    def __init__(self) -> None:
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[Frame]:
        self.buffer.extend(data)
        frames: list[Frame] = []
        while True:
            if len(self.buffer) < 8:
                return frames
            sof = self.buffer.find(bytes([SOF0, SOF1]))
            if sof < 0:
                self.buffer.clear()
                return frames
            if sof > 0:
                del self.buffer[:sof]
            if len(self.buffer) < 8:
                return frames
            version = self.buffer[2]
            ftype = self.buffer[3]
            flags = self.buffer[4]
            seq = self.buffer[5]
            length = self.buffer[6] | (self.buffer[7] << 8)
            if length > MAX_PAYLOAD:
                del self.buffer[:2]
                continue
            total = 2 + 6 + length + 2
            if len(self.buffer) < total:
                return frames
            payload = bytes(self.buffer[8 : 8 + length])
            expected = int.from_bytes(self.buffer[8 + length : total], "little")
            actual = crc16_ccitt_false(bytes(self.buffer[2 : 8 + length]))
            if actual == expected:
                frames.append(Frame(version, ftype, flags, seq, payload))
                del self.buffer[:total]
            else:
                del self.buffer[:2]


class TcpProtocolClient:
    """
    TCP-transport equivalent of the UART GUI's `SerialProtocolClient`. The
    public surface (connect / disconnect / send_request / state / history /
    events / latest) is kept identical so the backend helpers port over
    line-for-line.
    """

    def __init__(self) -> None:
        self._socket: socket.socket | None = None
        self._host: str | None = None
        self._port: int | None = None
        self._lock = threading.Lock()
        self._request_lock = threading.Lock()
        self._pending: dict[tuple[int, int], Queue[Frame]] = {}
        self._seq = 1
        self._reader_thread: threading.Thread | None = None
        self._running = False
        self._parser = FrameParser()
        self._history: deque[dict[str, Any]] = deque(maxlen=200)
        self._events: deque[dict[str, Any]] = deque(maxlen=200)
        self._latest: dict[str, Any] = {
            "info": None,
            "status": None,
            "hv": None,
            "last_event": None,
            "schedule_status": None,
            "schedule_caps": None,
        }
        self._transport_error: str | None = None
        # Independent of TCP state: True once we have seen ANY framed RX from
        # the RP2350B since the current connect() — a valid response or an
        # unsolicited event. The bridge itself never produces frames, so this
        # is a pure controller-liveness signal.
        self._controller_responsive: bool = False
        self._last_controller_rx_ts: float | None = None

    def connect(self, host: str, port: int = 3333, connect_timeout: float = 4.0) -> None:
        self.disconnect()
        self._parser = FrameParser()
        self._transport_error = None
        self._controller_responsive = False
        self._last_controller_rx_ts = None
        sock = socket.create_connection((host, int(port)), timeout=connect_timeout)
        # Short read timeout gives the reader loop a regular chance to notice
        # disconnect/shutdown requests without blocking indefinitely. Mirrors
        # the serial client's 50 ms read timeout.
        sock.settimeout(0.05)
        # Small writes should leave promptly — framing is complete per send.
        with suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._socket = sock
        self._host = host
        self._port = int(port)
        self._running = True
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def disconnect(self) -> None:
        self._running = False
        sock = self._socket
        self._socket = None
        if sock is not None:
            with suppress(Exception):
                sock.shutdown(socket.SHUT_RDWR)
            with suppress(Exception):
                sock.close()
        self._pending.clear()
        thread = self._reader_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            with suppress(Exception):
                thread.join(timeout=0.2)
        self._reader_thread = None
        self._host = None
        self._port = None

    @property
    def connected(self) -> bool:
        return self._socket is not None and self._running

    def events(self) -> list:
        """Snapshot of recently RECEIVED unsolicited event frames (decoded). Used to
        consume firmware-PUSHED telemetry without issuing a request."""
        return list(self._events)

    def state(self) -> dict[str, Any]:
        return {
            "connected": self.connected,
            "host": self._host if self.connected else None,
            "port": self._port if self.connected else None,
            "history": list(self._history),
            "events": list(self._events),
            "latest": self._latest,
            "transport_error": self._transport_error,
            "controller_responsive": self._controller_responsive,
            "last_controller_rx_ts": self._last_controller_rx_ts,
        }

    def send_request(
        self,
        frame_type: int,
        payload: bytes = b"",
        flags: int = 0,
        timeout: float = 1.0,
    ) -> dict[str, Any]:
        sock = self._socket
        if not self.connected or sock is None:
            raise RuntimeError("Bridge is not connected")
        with self._request_lock:
            with self._lock:
                seq = self._seq & 0xFF
                self._seq = (self._seq + 1) & 0xFF
                full_flags = FLAG_ACK_REQUIRED | (flags & FLAG_TARGET_IS_SINGLE_BOARD)
                frame = Frame(VERSION, frame_type, full_flags, seq, payload)
                queue: Queue[Frame] = Queue(maxsize=1)
                key = (seq, frame_type)
                self._pending[key] = queue
                try:
                    sock.sendall(frame.encode())
                except OSError as exc:
                    self._pending.pop(key, None)
                    self._transport_error = str(exc)
                    raise RuntimeError(f"TCP send failed: {exc}") from exc
                self._append_history("tx", frame)
            try:
                response = queue.get(timeout=timeout)
            except Empty as exc:
                self._pending.pop(key, None)
                raise TimeoutError(
                    f"Timed out waiting for response to {TYPE_NAMES.get(frame_type, hex(frame_type))}"
                ) from exc
            decoded = self._decode_frame(response)
            self._update_latest(decoded)
            return decoded

    def send_pipeline(self, requests, window: int = 8, timeout: float = 2.5,
                      on_progress=None):
        """Send many ACK'd frames with up to `window` in flight at once, so the
        per-frame round-trip latencies overlap instead of serializing. `requests`
        is a list of (frame_type, payload, flags); returns decoded responses in
        request order. A per-frame timeout/transport error becomes
        {'ok': False, 'error': ...} in that slot (the batch is not aborted).
        on_progress(done) is called as each response lands."""
        sock = self._socket
        if not self.connected or sock is None:
            raise RuntimeError("Bridge is not connected")
        n = len(requests)
        results: list = [None] * n
        inflight: list = []   # (index, key, queue) in send order
        done = 0
        with self._request_lock:   # exclusive for the whole batch (no PING interleave)
            nxt = 0
            while nxt < n or inflight:
                while len(inflight) < max(1, window) and nxt < n:
                    ft, payload, flags = requests[nxt]
                    with self._lock:
                        seq = self._seq & 0xFF
                        self._seq = (self._seq + 1) & 0xFF
                        full_flags = FLAG_ACK_REQUIRED | (flags & FLAG_TARGET_IS_SINGLE_BOARD)
                        frame = Frame(VERSION, ft, full_flags, seq, payload)
                        q: Queue[Frame] = Queue(maxsize=1)
                        key = (seq, ft)
                        self._pending[key] = q
                        try:
                            sock.sendall(frame.encode())
                        except OSError as exc:
                            self._pending.pop(key, None)
                            self._transport_error = str(exc)
                            raise RuntimeError(f"TCP send failed: {exc}") from exc
                        self._append_history("tx", frame)
                    inflight.append((nxt, key, q))
                    nxt += 1
                idx, key, q = inflight.pop(0)
                try:
                    resp = q.get(timeout=timeout)
                    results[idx] = self._decode_frame(resp)
                except Empty:
                    self._pending.pop(key, None)
                    results[idx] = {"ok": False, "error": "timeout"}
                done += 1
                if on_progress is not None:
                    on_progress(done)
        return results

    def _reader_loop(self) -> None:
        while self._running:
            sock = self._socket
            if sock is None:
                return
            try:
                data = sock.recv(4096)
            except socket.timeout:
                continue
            except (OSError, ConnectionError) as exc:
                self._transport_error = str(exc)
                self._running = False
                if self._socket is sock:
                    with suppress(Exception):
                        sock.close()
                    self._socket = None
                self._pending.clear()
                return
            if not data:
                # Orderly peer shutdown — bridge closed the socket.
                self._transport_error = "bridge closed the connection"
                self._running = False
                if self._socket is sock:
                    with suppress(Exception):
                        sock.close()
                    self._socket = None
                self._pending.clear()
                return
            for frame in self._parser.feed(data):
                self._append_history("rx", frame)
                self._controller_responsive = True
                self._last_controller_rx_ts = time.time()
                if frame.flags & FLAG_IS_EVENT:
                    event = self._decode_frame(frame)
                    self._events.append(event)
                    self._latest["last_event"] = event
                    continue
                key = (frame.seq, frame.type)
                queue = self._pending.pop(key, None)
                if queue is not None:
                    queue.put(frame)

    def _append_history(self, direction: str, frame: Frame) -> None:
        self._history.append(
            {
                "ts": time.time(),
                "dir": direction,
                "type": TYPE_NAMES.get(frame.type, hex(frame.type)),
                "flags": frame.flags,
                "seq": frame.seq,
                "payload_hex": frame.payload.hex(" "),
            }
        )

    def _decode_frame(self, frame: Frame) -> dict[str, Any]:
        payload = frame.payload
        is_event = bool(frame.flags & FLAG_IS_EVENT)
        # Event packets have no status byte — their payload starts with event data.
        # Only response frames carry a leading status byte.
        status = (payload[0] if payload else None) if not is_event else None
        decoded: dict[str, Any] = {
            "type": TYPE_NAMES.get(frame.type, hex(frame.type)),
            "seq": frame.seq,
            "flags": frame.flags,
            "status": STATUS_NAMES.get(status, None) if status is not None else None,
            "status_code": status,
            "payload_hex": payload.hex(" "),
            "raw": list(payload),
        }
        ok = status == 0x00
        single = bool(frame.flags & FLAG_TARGET_IS_SINGLE_BOARD)
        type_name = decoded["type"]

        if type_name in {"CH_GET_PRESENT", "CH_RESET_MUX"} and ok:
            if single and len(payload) >= 10:
                decoded["decoded"] = {
                    "channel": payload[1],
                    "mux_port": payload[2],
                    "mux_present": bool(payload[3]),
                    "tps_present": bool(payload[4]),
                    "ina_present": bool(payload[5]),
                    "enable_io_present": bool(payload[6]),
                    "fault_io_present": bool(payload[7]),
                    "iso_io_present": bool(payload[8]),
                    "hv_io_present": bool(payload[9]),
                }
            elif len(payload) >= 1 + 8 * 8:
                labels = [
                    "targeted_mask",
                    "mux_present_mask",
                    "tps_present_mask",
                    "ina_present_mask",
                    "enable_io_present_mask",
                    "fault_io_present_mask",
                    "iso_io_present_mask",
                    "hv_io_present_mask",
                ]
                decoded["decoded"] = {
                    label: list(payload[1 + i * 8 : 1 + (i + 1) * 8])
                    for i, label in enumerate(labels)
                }
        elif type_name in {"CH_GET_I2C_ENABLE_MASK", "CH_SET_I2C_ENABLE_MASK"} and ok and len(payload) >= 2:
            decoded["decoded"] = {"mask": payload[1]}
        elif type_name == "CH_TCA9554_SELF_TEST" and ok and len(payload) >= 42:
            # status(1) + targeted(8) + enable_toggle(8) + iso_toggle(8)
            #   + fault_live(8) + hv_live(8) + outputs_tested(1).
            # Each plane: byte=channel, bit=muxPort. outputs_tested: bit=channel.
            labels = [
                "targeted_mask",
                "enable_toggle_mask",
                "iso_toggle_mask",
                "fault_live_mask",
                "hv_live_mask",
            ]
            out = {
                label: list(payload[1 + i * 8 : 1 + (i + 1) * 8])
                for i, label in enumerate(labels)
            }
            out["outputs_tested_mask"] = payload[41]
            decoded["decoded"] = out
        elif type_name == "CH_READ_TCA9554" and ok and len(payload) >= 1 + 8 + 160:
            # status(1) + targeted(8) + per channel[8]: 4 chips × 5 bytes
            #   {config, input, output, polarity, okMask}. Chip order fixed:
            #   enable, fault, iso, hv. okMask bits: 1=config 2=input 4=output 8=pol.
            chip_names = ["enable", "fault", "iso", "hv"]
            channels = []
            for ch in range(8):
                base = 9 + ch * 20
                chips = {}
                for i, name in enumerate(chip_names):
                    off = base + i * 5
                    chips[name] = {
                        "config": payload[off + 0],
                        "input": payload[off + 1],
                        "output": payload[off + 2],
                        "polarity": payload[off + 3],
                        "ok": payload[off + 4],
                    }
                channels.append({"channel": ch, "chips": chips})
            decoded["decoded"] = {
                "targeted_mask": list(payload[1:9]),
                "channels": channels,
            }
        elif type_name == "CH_GET_DIAGNOSIS" and ok:
            # Single-board: status + ch + muxPort + 7 addr + 7 reg + 7 op = 24 bytes.
            # Multi-board:  status + targeted(8) + 7×8 addr + 7×8 reg + 7×8 op = 177 bytes.
            if single and len(payload) >= 24:
                decoded["decoded"] = {
                    "channel": payload[1],
                    "mux_port": payload[2],
                    "addr": {
                        "mux": bool(payload[3]),
                        "tps": bool(payload[4]),
                        "ina": bool(payload[5]),
                        "enable_io": bool(payload[6]),
                        "fault_io": bool(payload[7]),
                        "iso_io": bool(payload[8]),
                        "hv_io": bool(payload[9]),
                    },
                    "reg": {
                        "mux": bool(payload[10]),
                        "tps": bool(payload[11]),
                        "ina": bool(payload[12]),
                        "enable_io": bool(payload[13]),
                        "fault_io": bool(payload[14]),
                        "iso_io": bool(payload[15]),
                        "hv_io": bool(payload[16]),
                    },
                    "op": {
                        "mux": bool(payload[17]),
                        "tps": bool(payload[18]),
                        "ina": bool(payload[19]),
                        "enable_io": bool(payload[20]),
                        "fault_io": bool(payload[21]),
                        "iso_io": bool(payload[22]),
                        "hv_io": bool(payload[23]),
                    },
                }
            elif len(payload) >= 1 + 22 * 8:
                chip_labels = [
                    "mux", "tps", "ina",
                    "enable_io", "fault_io", "iso_io", "hv_io",
                ]
                out: dict[str, Any] = {
                    "targeted_mask": list(payload[1:9]),
                }
                offset = 9
                for suffix in ("_addr_mask", "_reg_mask", "_op_mask"):
                    for chip in chip_labels:
                        out[f"{chip}{suffix}"] = list(payload[offset : offset + 8])
                        offset += 8
                decoded["decoded"] = out
        elif type_name == "CH_GET_BOARD_BITMAPS" and ok and len(payload) >= 1 + 5 * 8:
            labels = [
                "targeted_mask",
                "iso_enable_mask",
                "tps_enable_mask",
                "tps_fault_mask",
                "hv_overcurrent_mask",
            ]
            decoded["decoded"] = {
                label: list(payload[1 + i * 8 : 1 + (i + 1) * 8])
                for i, label in enumerate(labels)
            }
        elif type_name == "CH_GET_INA219" and ok:
            if single and len(payload) >= 8:
                decoded["decoded"] = {
                    "channel": payload[1],
                    "mux_port": payload[2],
                    "present": bool(payload[3]),
                    "bus_mV": decode_u16(payload, 4),
                    "current_mA": decode_u16(payload, 6),
                }
            elif len(payload) >= 4:
                total = payload[1]
                page_start = payload[2]
                count = payload[3]
                entries = []
                offset = 4
                for _ in range(count):
                    if offset + 7 > len(payload):
                        break
                    entries.append(
                        {
                            "channel": payload[offset],
                            "mux_port": payload[offset + 1],
                            "present": bool(payload[offset + 2]),
                            "bus_mV": decode_u16(payload, offset + 3),
                            "current_mA": decode_u16(payload, offset + 5),
                        }
                    )
                    offset += 7
                decoded["decoded"] = {
                    "total_matching_entries": total,
                    "page_start": page_start,
                    "returned_entries": count,
                    "entries": entries,
                }
        elif type_name == "CH_GET_CACHED_CURRENTS" and ok:
            # CC-loop cached currents (no I2C). entry = ch,mux,mode(u8),
            # measured_mA(int16), target_mA(u16) = 6 bytes.
            def _s16(v):
                return v - 0x10000 if v >= 0x8000 else v
            if single and len(payload) >= 7:
                decoded["decoded"] = {
                    "channel": payload[1],
                    "mux_port": payload[2],
                    "mode": payload[3],
                    "measured_mA": _s16(decode_u16(payload, 4)),
                    "target_mA": decode_u16(payload, 6) if len(payload) >= 8 else 0,
                }
            elif len(payload) >= 4:
                total = payload[1]
                page_start = payload[2]
                count = payload[3]
                entries = []
                offset = 4
                # entry = ch(1)+mux(1)+mode(1)+measured(2)+target(2) = 7 bytes
                for _ in range(count):
                    if offset + 7 > len(payload):
                        break
                    entries.append({
                        "channel": payload[offset],
                        "mux_port": payload[offset + 1],
                        "mode": payload[offset + 2],
                        "measured_mA": _s16(decode_u16(payload, offset + 3)),
                        "target_mA": decode_u16(payload, offset + 5),
                    })
                    offset += 7
                decoded["decoded"] = {
                    "total_matching_entries": total,
                    "page_start": page_start,
                    "returned_entries": count,
                    "entries": entries,
                }
        elif type_name == "CH_GET_TPS_STATUS" and ok and single and len(payload) >= 7:
            decoded["decoded"] = {
                "channel": payload[1],
                "mux_port": payload[2],
                "present": bool(payload[3]),
                "enabled": bool(payload[4]),
                "fault_active": bool(payload[5]),
                "hv_overcurrent_active": bool(payload[6]),
            }
        elif type_name == "CH_GET_TPS_STATUS_PAGE" and ok and len(payload) >= 4:
            total = payload[1]
            page_start = payload[2]
            count = payload[3]
            entries = []
            offset = 4
            for _ in range(count):
                if offset + 10 > len(payload):
                    break
                entries.append(
                    {
                        "channel": payload[offset],
                        "mux_port": payload[offset + 1],
                        "present": bool(payload[offset + 2]),
                        "enabled": bool(payload[offset + 3]),
                        "fault_active": bool(payload[offset + 4]),
                        "hv_overcurrent_active": bool(payload[offset + 5]),
                        "raw_status": payload[offset + 6],
                        "operating_mode": payload[offset + 7],
                        "requested_mV": decode_u16(payload, offset + 8),
                    }
                )
                offset += 10
            decoded["decoded"] = {
                "total_matching_entries": total,
                "page_start": page_start,
                "returned_entries": count,
                "entries": entries,
            }
        elif type_name in {"CH_READ_TPS_REGISTER", "CH_WRITE_TPS_REGISTER"} and ok and len(payload) >= 7:
            decoded["decoded"] = {
                "channel": payload[1],
                "mux_port": payload[2],
                "reg": payload[3],
                "width_bytes": payload[4],
                "value": decode_u16(payload, 5),
            }
        elif type_name in {"CH_READ_INA219_REGISTER", "CH_WRITE_INA219_REGISTER"} and ok and len(payload) >= 6:
            decoded["decoded"] = {
                "channel": payload[1],
                "mux_port": payload[2],
                "reg": payload[3],
                "value": decode_u16(payload, 4),
            }
        elif type_name == "CH_SET_INA219_PGA" and ok:
            if single and len(payload) >= 5:
                decoded["decoded"] = {
                    "channel": payload[1],
                    "mux_port": payload[2],
                    "applied": bool(payload[3]),
                    "pga_code": payload[4],
                }
            elif len(payload) >= 26:
                decoded["decoded"] = {
                    "targeted_mask": list(payload[1:9]),
                    "applied_mask": list(payload[9:17]),
                    "failed_mask": list(payload[17:25]),
                    "pga_code": payload[25],
                }
        elif type_name == "HV_GET_ALL_BYTES" and ok and len(payload) >= 17:
            decoded["decoded"] = {
                "desired": list(payload[1:9]),
                "feedback": list(payload[9:17]),
            }
        elif type_name == "HV_PULSE" and ok and len(payload) >= 29:
            decoded["decoded"] = {
                "applied_mask": list(payload[1:9]),
                "on_error_mask": list(payload[9:17]),
                "off_error_mask": list(payload[17:25]),
                "measured_width_us": decode_u32(payload, 25),
            }
        elif type_name == "HV_SCHED_GET_CAPS" and ok and len(payload) >= 23:
            decoded["decoded"] = {
                "max_entries": payload[1],
                "entry_size_bytes": payload[2],
                "event_record_size_bytes": payload[3],
                "event_log_depth": decode_u16(payload, 4),
                "min_verified_pulse_us_post_only": decode_u16(payload, 6),
                "min_verified_pulse_us_pre_post": decode_u16(payload, 8),
                "min_verified_pulse_us_pre_post_level": decode_u16(payload, 10),
                "latch_latency_post_only_us": decode_u16(payload, 12),
                "latch_latency_pre_post_us": decode_u16(payload, 14),
                "latch_latency_pre_post_level_us": decode_u16(payload, 16),
                "max_run_duration_us": decode_u32(payload, 18),
                "feature_bits": payload[22],
            }
        elif type_name == "HV_SCHED_GET_ENTRY" and ok and len(payload) >= 24:
            decoded["decoded"] = {
                "channel": payload[1],
                "bit": payload[2],
                "start_delay_us": decode_u32(payload, 3),
                "on_duration_us": decode_u32(payload, 7),
                "off_duration_us": decode_u32(payload, 11),
                "repeat_count": decode_u16(payload, 15),
                "duration_tolerance_us": decode_u16(payload, 17),
                "verify_mode": payload[19],
                "verify_mode_name": SCHEDULE_VERIFY_MODES.get(payload[19], "unknown"),
                "reserved": list(payload[20:23]),
                "enabled": bool(payload[23]),
            }
        elif type_name == "HV_SCHED_SET_ENTRY" and ok and len(payload) >= 3:
            decoded["decoded"] = {
                "channel": payload[1],
                "bit": payload[2],
            }
        elif type_name == "HV_SCHED_GET_ENABLE_MATRIX" and ok and len(payload) >= 9:
            decoded["decoded"] = {"enable": list(payload[1:9])}
        elif type_name == "HV_SCHED_SET_ENABLE_BIT" and ok and len(payload) >= 4:
            decoded["decoded"] = {
                "channel": payload[1],
                "bit": payload[2],
                "enabled": bool(payload[3]),
            }
        elif type_name == "HV_SCHED_GET_STATUS" and ok and len(payload) >= 36:
            state_code = payload[1]
            decoded["decoded"] = {
                "state": state_code,
                "state_name": SCHEDULE_STATE_NAMES.get(state_code, "unknown"),
                "t_since_start_us": decode_u32(payload, 2),
                "active_mask": list(payload[6:14]),
                "on_error_mask": list(payload[14:22]),
                "off_error_mask": list(payload[22:30]),
                "event_log_fill": decode_u16(payload, 30),
                "event_log_seq_next": decode_u32(payload, 32),
            }
        elif type_name == "HV_SCHED_ARM" and len(payload) >= 4:
            decoded["decoded"] = {
                "reject_channel": payload[1],
                "reject_bit": payload[2],
                "reject_reason": payload[3],
                "reject_reason_name": SCHEDULE_REJECT_REASONS.get(payload[3], "unknown"),
            }
        elif type_name == "HV_SCHED_START" and ok and len(payload) >= 21:
            decoded["decoded"] = {
                "t_start_us_since_boot": decode_u32(payload, 1),
                "active_mask": list(payload[5:13]),
                "rejected_mask": list(payload[13:21]),
            }
        elif type_name == "HV_SCHED_STOP" and ok and len(payload) >= 25:
            decoded["decoded"] = {
                "active_mask_after": list(payload[1:9]),
                "on_error_mask": list(payload[9:17]),
                "off_error_mask": list(payload[17:25]),
            }
        elif type_name == "HV_SCHED_GET_EVENT_LOG" and ok and len(payload) >= 7:
            seq_next = decode_u32(payload, 1)
            returned = decode_u16(payload, 5)
            entries = []
            offset = 7
            for _ in range(returned):
                if offset + 20 > len(payload):
                    break
                entries.append(
                    {
                        "seq": decode_u32(payload, offset),
                        "t_pre_us": decode_u32(payload, offset + 4),
                        "t_post_us": decode_u32(payload, offset + 8),
                        "channel": payload[offset + 12],
                        "bit": payload[offset + 13],
                        "edge_dir": payload[offset + 14],
                        "pre_bit": payload[offset + 15],
                        "post_bit": payload[offset + 16],
                        "flags": payload[offset + 17],
                    }
                )
                offset += 20
            decoded["decoded"] = {
                "seq_next": seq_next,
                "returned_entries": returned,
                "entries": entries,
            }
        elif type_name == "HV_SCHED_CLEAR_ERRORS" and ok and len(payload) >= 25:
            decoded["decoded"] = {
                "cleared_mask": list(payload[1:9]),
                "on_error_mask_after": list(payload[9:17]),
                "off_error_mask_after": list(payload[17:25]),
            }
        elif type_name == "EVENT_SCHEDULE_STATE" and len(payload) >= 7:
            decoded["decoded"] = {
                "old_state": payload[0],
                "new_state": payload[1],
                "old_state_name": SCHEDULE_STATE_NAMES.get(payload[0], "unknown"),
                "new_state_name": SCHEDULE_STATE_NAMES.get(payload[1], "unknown"),
                "t_since_boot_ms": decode_u32(payload, 2),
                "trigger": payload[6],
            }
        elif type_name == "EVENT_SCHEDULE_COMPLETE" and len(payload) >= 20:
            decoded["decoded"] = {
                "t_since_start_us": decode_u32(payload, 0),
                "completed_mask": list(payload[4:12]),
                "error_mask": list(payload[12:20]),
            }
        elif type_name == "EVENT_SCHEDULE_ERROR" and len(payload) >= 11:
            decoded["decoded"] = {
                "channel": payload[0],
                "bit": payload[1],
                "kind": payload[2],
                "t_since_start_us": decode_u32(payload, 3),
                "seq": decode_u32(payload, 7),
            }
        elif type_name == "EVENT_TELEMETRY" and len(payload) >= 21:
            # Layout: mode(1) board_mask(8) cycle_id(1) total(1) page_start(1)
            #         hv_feedback(8) returned(1) [ch(1) mux(1) bus_mV(2) curr_mA(2)]×N
            returned = payload[20]
            entries = []
            off = 21
            for _ in range(returned):
                if off + 6 > len(payload):
                    break
                entries.append({
                    "channel": payload[off],
                    "mux_port": payload[off + 1],
                    "bus_mV": decode_u16(payload, off + 2),
                    "current_mA": decode_u16(payload, off + 4),
                })
                off += 6
            decoded["decoded"] = {
                "telemetry_mode": payload[0],
                "board_mask": list(payload[1:9]),
                "cycle_id": payload[9],
                "total_matching_entries": payload[10],
                "page_start": payload[11],
                "hv_feedback": list(payload[12:20]),
                "returned_entries": returned,
                "entries": entries,
            }
        elif type_name == "EVENT_HEARTBEAT" and len(payload) >= 7:
            decoded["decoded"] = {
                "device_uptime_ms": decode_u32(payload, 0),
                "app_state": payload[4],
                "fault_bits": payload[5],
                "schedule_state": payload[6],
                "schedule_state_name": SCHEDULE_STATE_NAMES.get(payload[6], "unknown"),
            }
        return decoded

    def _update_latest(self, decoded: dict[str, Any]) -> None:
        type_name = decoded["type"]
        if type_name == "GET_INFO":
            self._latest["info"] = decoded
        elif type_name == "GET_STATUS":
            self._latest["status"] = decoded
        elif type_name in {"HV_GET_ALL_BYTES", "HV_GET_CHANNEL_BYTE", "HV_REFRESH_FEEDBACK"}:
            self._latest["hv"] = decoded
        elif type_name == "HV_SCHED_GET_STATUS":
            self._latest["schedule_status"] = decoded
        elif type_name == "HV_SCHED_GET_CAPS":
            self._latest["schedule_caps"] = decoded


def build_command_payload(command: str, body: dict[str, Any]) -> tuple[int, int, bytes]:
    command = command.upper()
    single = body.get("target") == "single"
    flags = FLAG_TARGET_IS_SINGLE_BOARD if single else 0

    def ocp_threshold_ma() -> int:
        return int(body.get("threshold_mA", body.get("milliamps", 0)))

    if command == "PING":
        return 0x01, 0, int(body.get("tag", 0)).to_bytes(4, "little")
    if command == "GET_INFO":
        return 0x02, 0, b""
    if command == "GET_STATUS":
        return 0x03, 0, b""
    if command == "CLEAR_FAULTS":
        return 0x04, 0, bytes([int(body.get("mask", 0)) & 0xFF])
    if command == "ENTER_SAFE_STATE":
        return 0x05, 0, bytes([int(body.get("reason", 0)) & 0xFF])
    if command == "GET_EVENT_CONFIG":
        return 0x07, 0, b""
    if command == "SET_EVENT_CONFIG":
        payload = bytes([int(body.get("event_enable_bits", 0)) & 0xFF])
        payload += u16(int(body.get("telemetry_period_ms", 0)))
        payload += bytes([int(body.get("telemetry_mode", 0)) & 0xFF])
        payload += parse_mask(body.get("board_mask", [0] * 8))
        return 0x06, 0, payload
    if command == "RUN_SCAN":
        return 0x30, 0, b""
    if command == "RUN_SELF_TEST":
        return 0x31, 0, b""
    if command == "RUN_HV_SELF_TEST":
        return 0x32, 0, b""
    if command == "RUN_I2C_SELF_TEST":
        return 0x33, 0, b""

    if command == "HV_SET_BIT":
        # writeMode (on the wire, final byte): 0=NoVerify, 1=Verify, 2=Force.
        # `force=True` overrides `verify` to send mode 2 (bench-debug path
        # that skips the auto-clear on mismatch).
        mode = 2 if body.get("force", False) else (1 if body.get("verify", True) else 0)
        payload = bytes(
            [
                int(body["channel"]) & 0xFF,
                int(body["bit"]) & 0xFF,
                1 if body.get("value", False) else 0,
                mode,
            ]
        )
        return 0x10, 0, payload
    if command == "HV_SET_CHANNEL_BYTE":
        mode = 2 if body.get("force", False) else (1 if body.get("verify", True) else 0)
        payload = bytes(
            [
                int(body["channel"]) & 0xFF,
                int(body["value"]) & 0xFF,
                mode,
            ]
        )
        return 0x11, 0, payload
    if command == "HV_GET_CHANNEL_BYTE":
        return 0x12, 0, bytes([int(body["channel"]) & 0xFF])
    if command == "HV_GET_ALL_BYTES":
        return 0x13, 0, b""
    if command == "HV_REFRESH_FEEDBACK":
        channel = body.get("channel", 0xFF)
        return 0x14, 0, bytes([int(channel) & 0xFF])
    if command == "HV_SET_MULTI_CHANNEL":
        values = body.get("values", [0] * 8)
        if len(values) != 8:
            raise ValueError("values must contain 8 channel bytes")
        payload = bytes([int(body.get("channel_mask", 0)) & 0xFF])
        payload += bytes(int(v) & 0xFF for v in values)
        mode = 2 if body.get("force", False) else (1 if body.get("verify", True) else 0)
        payload += bytes([mode])
        return 0x15, 0, payload

    if command == "HV_PULSE":
        verify_mode = int(body.get("verify_mode", 0)) & 0xFF
        width_us = int(body.get("width_us", 0))
        if single:
            channel = int(body["channel"]) & 0xFF
            bit = int(body.get("bit", body.get("mux_port", 0))) & 0xFF
            payload = bytes([channel, bit]) + u32(width_us) + bytes([verify_mode])
            return 0x16, FLAG_TARGET_IS_SINGLE_BOARD, payload
        hv_mask = parse_mask(body.get("hv_mask", body.get("board_mask", [0] * 8)))
        payload = hv_mask + u32(width_us) + bytes([verify_mode])
        return 0x16, 0, payload

    if single:
        channel = int(body["channel"]) & 0xFF
        mux = int(body["mux_port"]) & 0xFF
        if command in {"CH_SET_ISO_ENABLE", "CH_SET_TPS_ENABLE"}:
            payload = bytes([channel, mux, 1 if body.get("enable", False) else 0])
            return (0x20 if command == "CH_SET_ISO_ENABLE" else 0x21), flags, payload
        if command == "CH_SET_TPS_VOLTAGE":
            payload = (
                bytes([channel, mux])
                + u16(int(body["millivolts"]))
                + bytes([1 if body.get("enable_after_set", True) else 0])
            )
            return 0x22, flags, payload
        if command == "CH_SET_TPS_OCP_THRESHOLD":
            payload = bytes([channel, mux]) + u16(ocp_threshold_ma())
            return 0x28, flags, payload
        if command == "CH_READ_TPS_REGISTER":
            payload = bytes(
                [
                    channel,
                    mux,
                    int(body["reg"]) & 0xFF,
                    int(body.get("width_bytes", 1)) & 0xFF,
                ]
            )
            return 0x29, 0, payload
        if command == "CH_WRITE_TPS_REGISTER":
            payload = bytes(
                [
                    channel,
                    mux,
                    int(body["reg"]) & 0xFF,
                    int(body.get("width_bytes", 1)) & 0xFF,
                ]
            ) + u16(int(body["value"]))
            return 0x2A, 0, payload
        if command == "CH_READ_INA219_REGISTER":
            payload = bytes([channel, mux, int(body["reg"]) & 0xFF])
            return 0x2B, 0, payload
        if command == "CH_WRITE_INA219_REGISTER":
            payload = bytes([channel, mux, int(body["reg"]) & 0xFF]) + u16(int(body["value"]))
            return 0x2C, 0, payload
        if command == "CH_SET_INA219_PGA":
            payload = bytes([channel, mux, int(body["pga_code"]) & 0xFF])
            return 0x2D, flags, payload
        if command in {"CH_GET_TPS_STATUS", "CH_GET_INA219", "CH_GET_PRESENT", "CH_GET_DIAGNOSIS"}:
            cmd_type = {"CH_GET_TPS_STATUS": 0x23, "CH_GET_INA219": 0x24,
                        "CH_GET_PRESENT": 0x25, "CH_GET_DIAGNOSIS": 0x2E}[command]
            return cmd_type, flags, bytes([channel, mux])

    mask = parse_mask(body.get("board_mask", [0] * 8))
    if command in {"CH_SET_ISO_ENABLE", "CH_SET_TPS_ENABLE"}:
        payload = mask + bytes([1 if body.get("enable", False) else 0])
        return (0x20 if command == "CH_SET_ISO_ENABLE" else 0x21), 0, payload
    if command == "CH_SET_TPS_VOLTAGE":
        payload = (
            mask
            + u16(int(body["millivolts"]))
            + bytes([1 if body.get("enable_after_set", True) else 0])
        )
        return 0x22, 0, payload
    if command == "CH_SET_TPS_OCP_THRESHOLD":
        payload = mask + u16(ocp_threshold_ma())
        return 0x28, 0, payload
    if command == "CH_SET_INA219_PGA":
        payload = mask + bytes([int(body["pga_code"]) & 0xFF])
        return 0x2D, 0, payload
    if command == "CH_GET_TPS_STATUS":
        return 0x23, 0, mask
    if command == "CH_GET_TPS_STATUS_PAGE":
        return (
            0x27,
            0,
            mask
            + bytes([int(body.get("page_start", 0)) & 0xFF, int(body.get("max_entries", 8)) & 0xFF]),
        )
    if command == "CH_GET_INA219":
        return (
            0x24,
            0,
            mask
            + bytes([int(body.get("page_start", 0)) & 0xFF, int(body.get("max_entries", 8)) & 0xFF]),
        )
    if command == "CH_GET_PRESENT":
        return 0x25, 0, mask
    if command == "CH_GET_DIAGNOSIS":
        return 0x2E, 0, mask
    if command == "CH_GET_BOARD_BITMAPS":
        return 0x26, 0, mask
    if command == "CH_GET_I2C_ENABLE_MASK":
        return 0x2F, 0, b""
    if command == "CH_SET_I2C_ENABLE_MASK":
        # Single-byte mask. Bit N = 1 means channel N enabled.
        return 0x34, 0, bytes([int(body.get("mask", 0xFF)) & 0xFF])

    if command == "HV_SCHED_GET_CAPS":
        return 0x50, 0, b""
    if command == "HV_SCHED_GET_ENTRY":
        return 0x51, 0, bytes([int(body["channel"]) & 0xFF, int(body["bit"]) & 0xFF])
    if command == "HV_SCHED_SET_ENTRY":
        reserved = body.get("reserved", [0, 0, 0])
        if len(reserved) != 3:
            raise ValueError("reserved must contain 3 bytes")
        payload = bytes([int(body["channel"]) & 0xFF, int(body["bit"]) & 0xFF])
        payload += u32(int(body.get("start_delay_us", 0)))
        payload += u32(int(body.get("on_duration_us", 0)))
        payload += u32(int(body.get("off_duration_us", 0)))
        payload += u16(int(body.get("repeat_count", 0)))
        payload += u16(int(body.get("duration_tolerance_us", 20)))
        payload += bytes([int(body.get("verify_mode", 0)) & 0xFF])
        payload += bytes(int(r) & 0xFF for r in reserved)
        return 0x52, 0, payload
    if command == "HV_SCHED_GET_ENABLE_MATRIX":
        return 0x53, 0, b""
    if command == "HV_SCHED_SET_ENABLE_MATRIX":
        enable = parse_mask(body.get("enable", body.get("board_mask", [0] * 8)))
        return 0x54, 0, enable
    if command == "HV_SCHED_SET_ENABLE_BIT":
        return 0x55, 0, bytes(
            [
                int(body["channel"]) & 0xFF,
                int(body["bit"]) & 0xFF,
                1 if body.get("enabled", False) else 0,
            ]
        )
    if command == "HV_SCHED_CLEAR":
        return 0x56, 0, b""
    if command == "HV_SCHED_GET_STATUS":
        return 0x57, 0, b""
    if command == "HV_SCHED_ARM":
        # ARM payload byte = trigger mode (NOT "force"):
        #   0 = software-startable  (fires on HV_SCHED_START)
        #   1 = wait for hardware trigger on SyncIn (fires on a SyncOut edge)
        mode = int(body.get("arm_mode", body.get("force", 0))) & 0xFF
        return 0x58, 0, bytes([mode])
    if command == "HV_SCHED_START":
        subset = parse_mask(body.get("subset_mask", [0xFF] * 8))
        return 0x59, 0, subset
    if command == "HV_SCHED_STOP":
        subset = parse_mask(body.get("subset_mask", [0xFF] * 8))
        return 0x5A, 0, subset
    if command == "HV_SCHED_DISARM":
        return 0x5B, 0, b""
    if command == "HV_SCHED_GET_EVENT_LOG":
        payload = u32(int(body.get("since_seq", 0)))
        payload += u16(int(body.get("max_entries", 16)))
        return 0x5C, 0, payload
    if command == "HV_SCHED_CLEAR_EVENT_LOG":
        return 0x5D, 0, b""
    if command == "HV_SCHED_CLEAR_ERRORS":
        path_mask = parse_mask(body.get("path_mask", [0xFF] * 8))
        return 0x5E, 0, path_mask

    raise ValueError(f"Unsupported UART command: {command}")


# --------------------------------------------------------------------------- #
# Bridge + controller discovery
# --------------------------------------------------------------------------- #
#
# Two distinct things can be alive or silent at any ESP32 bridge address:
#
#   1. The ESP32 itself — it runs the `config_portal` HTTP server on :80 and
#      the raw TCP byte pipe on :3333. The portal is served entirely by the
#      ESP32 and exposes per-unit identity (AP SSID `CTPower-XXXXXX`), the
#      current STA IP, and whether the :3333 slot is already owned by a
#      client. These facts are meaningful even when the RP2350B behind the
#      bridge is dead or missing.
#
#   2. The RP2350B controller — reachable *through* the bridge's :3333 byte
#      pipe. It speaks the UART protocol in `uart_protocol.md`. A PING frame
#      round-trip (sub-second on LAN when the controller is up) is the only
#      way to confirm it is actually running.
#
# The scanner reports both facets separately so the GUI can tell a user "the
# bridge is fine but your RP2350B isn't responding" vs. "nothing at all at
# this address". Subnet sweep: (a) 192.168.4.0/24 always, since that's the AP
# fallback, and (b) the /24 of whatever interface routes to the internet.

BRIDGE_PORT = 3333
BRIDGE_HTTP_PORT = 80
BRIDGE_HTTP_TIMEOUT = 0.6

_AP_SSID_RE = re.compile(r"CTPower-[0-9A-Fa-f]{6}")
_ROW_RE = re.compile(
    r"<tr>\s*<th>([^<]+)</th>\s*<td>(.*?)</td>\s*</tr>",
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(value: str) -> str:
    return unescape(_TAG_RE.sub("", value)).strip()


def fetch_bridge_info(host: str, timeout: float = BRIDGE_HTTP_TIMEOUT) -> dict[str, Any] | None:
    """
    GET http://{host}/ and extract the config_portal status table. Returns None
    if the HTTP request fails or the response doesn't look like the portal.

    The portal page renders `<tr><th>Label</th><td>Value</td></tr>` rows, so
    this is a simple scrape rather than a full HTML parse. Keys we care about
    are normalized into lowercase snake_case for downstream use.
    """
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310 — trusted LAN, plain HTTP
            body = resp.read(8192).decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None

    # Hard sanity check: the portal's title + the CTPower-XXXXXX pattern must
    # both appear, or we treat this as "some other HTTP server on :80".
    if "CT Power Controller Bridge" not in body and "CT Power Bridge" not in body:
        return None
    ssid_match = _AP_SSID_RE.search(body)
    if ssid_match is None:
        return None

    info: dict[str, Any] = {
        "ap_ssid": ssid_match.group(0),
        "ap_ip": None,
        "sta_connected": None,
        "sta_ip": None,
        "tcp_port": None,
        "tcp_client_busy": None,
    }
    for label, raw_value in _ROW_RE.findall(body):
        key = label.strip().lower()
        value = _strip_html(raw_value)
        if key == "ap ip":
            info["ap_ip"] = value
        elif key == "sta":
            info["sta_connected"] = value.lower().startswith("connected")
        elif key == "sta ip":
            info["sta_ip"] = value
        elif key == "tcp port":
            try:
                info["tcp_port"] = int(value)
            except ValueError:
                pass
        elif key == "tcp client":
            info["tcp_client_busy"] = value.lower().startswith("yes")
    return info


def _primary_local_ip() -> str | None:
    """
    Infer the primary IPv4 by opening a UDP socket towards a public address.
    No traffic is actually sent — the OS just fills in the source address. This
    fails when there is no upstream route (e.g. joined to the ESP32 AP with no
    internet), in which case we return None and fall back to the AP subnet.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.settimeout(0.3)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        return ip if isinstance(ip, str) and ip.count(".") == 3 else None
    except OSError:
        return None
    finally:
        sock.close()


def _candidate_hosts() -> list[str]:
    candidates: set[str] = set()
    # ESP32 AP gateway is always .1 in its own subnet.
    for i in range(1, 255):
        candidates.add(f"192.168.4.{i}")

    primary = _primary_local_ip()
    if primary is not None:
        parts = primary.split(".")
        if len(parts) == 4:
            base = ".".join(parts[:3])
            for i in range(1, 255):
                candidates.add(f"{base}.{i}")
            # Exclude our own IP so we don't waste a probe.
            candidates.discard(primary)

    def sort_key(ip: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in ip.split("."))
        except ValueError:
            return (256,)

    return sorted(candidates, key=sort_key)


def _probe_tcp(host: str, port: int, timeout: float) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def _probe_controller(host: str, port: int, timeout: float) -> dict[str, Any]:
    """
    Connect to the bridge, send a PING, and measure whether the RP2350B
    behind it answers. Returns `{responsive: bool, rtt_ms: int | None}`.

    This deliberately avoids borrowing the live `TcpProtocolClient` — the
    scan must be safe to run while the GUI has no session open, and must
    not leave probes contending for the bridge's single client slot beyond
    this short window.
    """
    seq = 0x73
    frame = Frame(VERSION, 0x01, FLAG_ACK_REQUIRED, seq, (0xCAFEF00D).to_bytes(4, "little")).encode()
    parser = FrameParser()
    start = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(frame)
            deadline = start + timeout
            while time.monotonic() < deadline:
                try:
                    data = sock.recv(512)
                except socket.timeout:
                    break
                if not data:
                    break
                for reply in parser.feed(data):
                    if reply.type == 0x01 and reply.seq == seq:
                        return {
                            "responsive": True,
                            "rtt_ms": int((time.monotonic() - start) * 1000),
                        }
    except (OSError, socket.timeout):
        return {"responsive": False, "rtt_ms": None}
    return {"responsive": False, "rtt_ms": None}


def inspect_host(
    host: str,
    port: int = BRIDGE_PORT,
    connect_timeout: float = 0.25,
    controller_timeout: float = 0.5,
    http_timeout: float = BRIDGE_HTTP_TIMEOUT,
    probe_controller: bool = True,
) -> dict[str, Any]:
    """
    Gather ESP32-side and RP2350B-side facts about one host. This is the
    per-candidate worker used by `scan_for_bridge()`, but also callable
    directly when the UI wants to refresh a single row.
    """
    port_open = _probe_tcp(host, port, connect_timeout)

    # ESP32 side: talk to the portal on :80 regardless of whether :3333 is
    # open — it's possible (rarely) for the TCP bridge to be restarting while
    # the portal is up, and the UI still wants to show the bridge's name.
    bridge_info = fetch_bridge_info(host, timeout=http_timeout)
    bridge: dict[str, Any] = {
        "port_open": port_open,
        "http_reachable": bridge_info is not None,
        "name": bridge_info.get("ap_ssid") if bridge_info else None,
        "ap_ip": bridge_info.get("ap_ip") if bridge_info else None,
        "sta_connected": bridge_info.get("sta_connected") if bridge_info else None,
        "sta_ip": bridge_info.get("sta_ip") if bridge_info else None,
        "tcp_port": bridge_info.get("tcp_port") if bridge_info else None,
        "tcp_client_busy": bridge_info.get("tcp_client_busy") if bridge_info else None,
    }
    # "Bridge is here" = any evidence from either channel.
    bridge["present"] = port_open or bridge_info is not None

    # RP2350B side: only try the PING if :3333 is open AND the portal doesn't
    # already tell us someone else owns the slot. Probing a busy slot will
    # always report responsive=False and may kick the real client.
    controller: dict[str, Any] = {
        "responsive": False,
        "rtt_ms": None,
        "skipped_reason": None,
    }
    if probe_controller and port_open and not bridge.get("tcp_client_busy"):
        controller.update(_probe_controller(host, port, controller_timeout))
    elif not port_open:
        controller["skipped_reason"] = "tcp_closed"
    elif bridge.get("tcp_client_busy"):
        controller["skipped_reason"] = "slot_busy"
    elif not probe_controller:
        controller["skipped_reason"] = "not_requested"

    return {
        "host": host,
        "port": port,
        "bridge": bridge,
        "controller": controller,
    }


def scan_for_bridge(
    port: int = BRIDGE_PORT,
    connect_timeout: float = 0.25,
    controller_timeout: float = 0.5,
    http_timeout: float = BRIDGE_HTTP_TIMEOUT,
    max_workers: int = 128,
    probe_controller: bool = True,
) -> list[dict[str, Any]]:
    """
    Return every host where either the TCP bridge port or the HTTP portal
    answers. Each record carries separate `bridge` and `controller` blocks so
    the caller can distinguish "ESP32 is up, RP2350B is silent" from "nothing
    at all". The scan is parallelized per-candidate, so wall time is roughly
    max(connect_timeout, http_timeout, controller_timeout) on a cold LAN.
    """
    candidates = _candidate_hosts()

    # Stage 1: cheap TCP probe to narrow down the list. HTTP scrape + PING are
    # too slow to run against every /24 entry.
    opened: list[str] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_probe_tcp, host, port, connect_timeout): host for host in candidates}
        for fut in as_completed(futures):
            if fut.result():
                opened.append(futures[fut])

    def ip_sort(ip: str) -> tuple[int, ...]:
        try:
            return tuple(int(part) for part in ip.split("."))
        except ValueError:
            return (256,)

    opened.sort(key=ip_sort)
    if not opened:
        return []

    # Stage 2: per-host inspection (HTTP + optional PING), parallelized.
    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(opened)))) as pool:
        futs = {
            pool.submit(
                inspect_host,
                host,
                port,
                connect_timeout,
                controller_timeout,
                http_timeout,
                probe_controller,
            ): host
            for host in opened
        }
        by_host: dict[str, dict[str, Any]] = {}
        for fut in as_completed(futs):
            host = futs[fut]
            try:
                by_host[host] = fut.result()
            except Exception as exc:  # pragmatic: don't let one bad host poison the scan
                by_host[host] = {
                    "host": host,
                    "port": port,
                    "bridge": {"present": True, "port_open": True, "http_reachable": False,
                               "name": None, "error": str(exc)},
                    "controller": {"responsive": False, "rtt_ms": None, "skipped_reason": "exception"},
                }
    for host in opened:
        results.append(by_host[host])
    return results


# -----------------------------------------------------------------------------
# ADC bridge HTTP helpers (talk to the device's :80 portal)
# -----------------------------------------------------------------------------
#
# The device exposes:
#   GET  /adc                        -> JSON {have, raw, total, total_hi, rate_hz, bits}
#   GET  /adc/burst?n=N              -> application/octet-stream of N * u16 LE
#   GET  /adc/stream                 -> JSON {active, packets, samples, drops}
#   POST /adc/stream/start?host=&port= (form-encoded body or query) -> text/plain
#   POST /adc/stream/stop                                            -> text/plain
#
# These helpers run from the GUI host (the laptop), not the device. They proxy
# through to the device's port 80. None of them are TCP-bridge aware — they
# don't touch the :3333 byte pipe and are safe to call while connected.

ADC_DEFAULT_HTTP_TIMEOUT = 1.0


def _http_get(url: str, timeout: float, accept_binary: bool = False) -> tuple[int, bytes, dict[str, str]]:
    """GET that returns (status, body_bytes, headers_dict). Headers are lowercased."""
    req = urllib.request.Request(url, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 trusted LAN
        body = resp.read()
        headers = {k.lower(): v for k, v in resp.headers.items()}
        return resp.status, body, headers


def _http_post_form(url: str, fields: dict[str, str], timeout: float) -> tuple[int, str]:
    """POST application/x-www-form-urlencoded. Returns (status, text)."""
    data = urllib.parse.urlencode(fields).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        # Surface the device's body as the message — endpoints reply text/plain.
        return exc.code, exc.read().decode("utf-8", errors="replace") if exc.fp else str(exc)


def adc_get_snapshot(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc"
    try:
        status, body, _ = _http_get(url, timeout=timeout)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        return {"ok": True, "snapshot": json.loads(body.decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_get_stream_status(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/stream"
    try:
        status, body, _ = _http_get(url, timeout=timeout)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        return {"ok": True, "device_stream": json.loads(body.decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


# -----------------------------------------------------------------------------
# STM32 status.
#
# NOTE: the previous (uncommitted) implementation here was a _Stm32BridgeClient
# that held a persistent TCP connection to the ESP32's :3335 transparent
# bridge and cached the 1 Hz EVT_HEARTBEAT frames, so the GUI saw real
# heartbeat cadence. That code was lost to a stray `git checkout`. This is a
# simpler HTTP-poll replacement against the device's /stm32 endpoint
# (config_portal handleStm32Status). Functionally equivalent for the badge;
# poll-rate rather than push. The cached-bridge version can be re-added later
# if the synthesized-tick smoothing matters.
# -----------------------------------------------------------------------------

STM32_BRIDGE_PORT = 3335


def fetch_stm32_status(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """GET the STM32 heartbeat/link status from the device's /stm32 endpoint."""
    if not host:
        return {"ever_seen": False, "age_ms": 0, "error": "no host"}
    try:
        status, body, _ = _http_get(f"http://{host}:{BRIDGE_HTTP_PORT}/stm32", timeout)
        if status != 200:
            return {"ever_seen": False, "age_ms": 0, "error": f"HTTP {status}"}
        return json.loads(body.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError) as exc:
        return {"ever_seen": False, "age_ms": 0, "error": str(exc)}


def _post_result(status: int, text: str) -> dict[str, Any]:
    """Standard result for a plain-text POST to the ESP32 bridge.

    On failure the reason goes in "error" -- the key every caller actually
    checks -- and not only in "message". This dict used to be hand-built at
    14 separate call sites and every one of them set "message" alone, so a
    failed call surfaced as a bare ok:False while the device's own
    explanation ("hsadc_config failed (UART)", "ds3502_set failed") was
    dropped on the floor. Each site had to REMEMBER to report the reason;
    none did. Building it in one place is what stops the next one forgetting.
    """
    ok = status == 200
    body = text.strip()
    out = {"ok": ok, "status": status, "message": body}
    if not ok:
        out["error"] = f"HTTP {status}: {body or '(no detail)'}"
        return out
    # Some of these endpoints answer with JSON carrying their OWN ok/error, and
    # deliberately use HTTP 200 for "the request succeeded and the answer is no"
    # (same reasoning as sendNoDevice: a definite answer is not a transport
    # failure). Basing ok purely on the status code then reported a refused
    # operation as a success with the real answer stringified into "message" --
    # observed on ready_renew when nothing was armed: {"ok": True, "message":
    # '{"ok":false,"error":"not armed"}'}. Honour the embedded verdict.
    if body.startswith("{"):
        try:
            inner = json.loads(body)
        except ValueError:
            return out
        if isinstance(inner, dict):
            if inner.get("ok") is False:
                out["ok"] = False
                out["error"] = str(inner.get("error") or "device reported ok:false")
            # Merge the device's own fields up so callers do not have to parse
            # "message" themselves. Never overwrite ok/status/message/error.
            for k, v in inner.items():
                if k not in ("ok", "status", "message", "error"):
                    out.setdefault(k, v)
    return out


def _stm32_get_json(host: str, path: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        status, body, _ = _http_get(f"http://{host}:{BRIDGE_HTTP_PORT}{path}", timeout)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}: {body.decode('utf-8','replace').strip()}"}
        return json.loads(body.decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # urllib RAISES on 4xx/5xx, so the branch above never runs for them and
        # str(exc) is only "HTTP Error 409: Conflict" -- the device's own
        # explanation is sitting in the response body and was being thrown away.
        # That cost real debugging time: the ESP32 replies "adc_window rejected
        # (ADC not streaming? arm HSADC first)", which says exactly what to do,
        # and the user saw a bare status name instead.
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()
        except Exception:
            pass
        return {"ok": False, "status": exc.code,
                "error": f"HTTP {exc.code}: {detail}" if detail else str(exc)}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def _stm32_post(host: str, path: str, fields: dict[str, str],
                timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        status, text = _http_post_form(f"http://{host}:{BRIDGE_HTTP_PORT}{path}", fields, timeout)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        ok = status == 200 and body.get("ok", status == 200)
        out = {"ok": ok, "status": status, **body, "message": text.strip()}
        # On failure carry the REASON in "error", the key every caller checks.
        # This used to set only "message", so a failed POST came back as a bare
        # ok:False with no explanation -- e.g. enable_emission() reported failure
        # while the ESP32 had said exactly why ("hv enable failed (UART)"). The
        # GET sibling (_stm32_get_json) always set "error"; this one never did,
        # so the two halves of the same module disagreed about how a failure is
        # reported. Don't clobber an "error" the device itself supplied.
        if not ok and not out.get("error"):
            detail = text.strip() or f"HTTP {status}"
            out["error"] = f"HTTP {status}: {detail}" if status != 200 else detail
        return out
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def stm32_ds3502_get(host: str, ch: str) -> dict[str, Any]:
    """Read a DS3502 wiper. ch = 0|1|2|ev|ei|fv."""
    return _stm32_get_json(host, f"/stm32/ds3502?ch={ch}")


def stm32_ds3502_set(host: str, ch: str, wiper: int) -> dict[str, Any]:
    """Set a DS3502 wiper (0..127)."""
    return _stm32_post(host, "/stm32/ds3502", {"ch": str(ch), "wiper": str(int(wiper))})


def stm32_hv_status(host: str) -> dict[str, Any]:
    """Read actual HV pin state and safety flags (emission_on, focus_on, ads1115_alert, amc3301_diag)."""
    return _stm32_get_json(host, "/stm32/hv_status")


def stm32_hv_enable_set(host: str, ch: str, on: bool) -> dict[str, Any]:
    """Enable/disable an HV output. ch = 'emission' | 'focus'."""
    return _stm32_post(host, "/stm32/hv_enable", {"ch": str(ch), "on": "1" if on else "0"})


def stm32_ads1115(host: str) -> dict[str, Any]:
    """Read the 4 ADS1115 channels (raw codes, mV, engineering units)."""
    return _stm32_get_json(host, "/stm32/ads1115")


def stm32_adc_window(host: str, n: int = 1000) -> dict[str, Any]:
    """Pulse-INDEPENDENT windowed ADC summary (STM32 ground truth) over the next
    `n` samples (1000 = 1 ms @ 1 MSPS). Returns n/min/max/mean/rms/std/pp. Requires
    the high-speed ADC to be streaming (arm HSADC in detector mode first)."""
    return _stm32_get_json(host, f"/stm32/adc_window?n={int(n)}", timeout=1.5)


def stm32_hv_set_target(host: str, chan: str, target: int, tol: int = 4, max_step: int = 1) -> dict[str, Any]:
    """Closed-loop HV: drive DS3502 until ADS1115 ≈ `target` (counts). chan = 'emission'|'focus'."""
    return _stm32_post(host, "/stm32/hv_set_target",
                       {"chan": str(chan), "target": str(int(target)),
                        "tol": str(int(tol)), "max_step": str(int(max_step))})


def stm32_hv_get_target(host: str, chan: str) -> dict[str, Any]:
    """Read the closed-loop target/state for a channel (target, last_adc, active, at_target)."""
    return _stm32_get_json(host, f"/stm32/hv_get_target?chan={chan}")


def stm32_hv_clear_target(host: str, chan: str) -> dict[str, Any]:
    """Disable closed-loop for a channel (wiper stays put)."""
    return _stm32_post(host, "/stm32/hv_clear_target", {"chan": str(chan)})


def stm32_i2c_scan(host: str) -> dict[str, Any]:
    """Scan the STM32's I2C bus (0x08..0x77); returns ACKed 7-bit addresses."""
    return _stm32_post(host, "/stm32/i2c_scan", {}, timeout=4.0)


def stm32_connect(host: str, port: int = STM32_BRIDGE_PORT) -> dict[str, Any]:
    """Compat shim. The HTTP-poll status path needs no persistent connection;
    return the current status so the connect flow has something to show."""
    _ = port
    return fetch_stm32_status(host)


def stm32_disconnect() -> None:
    """Compat shim — nothing to tear down in the HTTP-poll model."""
    return None


def stm32_connected() -> bool:
    return False


# -----------------------------------------------------------------------------
# Multi-device sync / trigger (device /sync/* endpoints)
# -----------------------------------------------------------------------------

def sync_get_status(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        status, body, _ = _http_get(f"http://{host}:{BRIDGE_HTTP_PORT}/sync/status", timeout)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        return {"ok": True, **json.loads(body.decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def sync_post_config(host: str, fields: dict[str, str],
                     timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        status, text = _http_post_form(
            f"http://{host}:{BRIDGE_HTTP_PORT}/sync/config", fields, timeout)
        if status != 200:
            return {"ok": False, "error": text.strip() or f"HTTP {status}"}
        return {"ok": True, **json.loads(text)}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def sync_post_fire(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    status, text = _http_post_form(f"http://{host}:{BRIDGE_HTTP_PORT}/sync/fire", {}, timeout)
    return _post_result(status, text)


def sync_post_abort(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    status, text = _http_post_form(f"http://{host}:{BRIDGE_HTTP_PORT}/sync/abort", {}, timeout)
    return _post_result(status, text)


def sync_post_burst(host: str, count: int, rate_hz: int,
                    timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Arm the HARDWARE-timed pulse train: ONE call, the ESP32 esp_timer emits
    `count` SyncOut pulses at `rate_hz` and stops itself. No HTTP per pulse — the
    cadence is hardware-timed at the real trigger rate."""
    status, text = _http_post_form(f"http://{host}:{BRIDGE_HTTP_PORT}/sync/burst",
                                   {"count": str(int(count)), "rate_hz": str(int(rate_hz))}, timeout)
    try:
        body = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        body = {"error": text.strip()}
    return {"ok": status == 200, "status": status, **body}


def sync_get_burst_status(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    try:
        status, body, _ = _http_get(f"http://{host}:{BRIDGE_HTTP_PORT}/sync/burst/status", timeout)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        return {"ok": True, **json.loads(body.decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def sync_post_burst_stop(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    status, text = _http_post_form(f"http://{host}:{BRIDGE_HTTP_PORT}/sync/burst/stop", {}, timeout)
    return _post_result(status, text)


def sync_post_capture(host: str, fields: dict[str, str],
                      timeout: float = 60.0) -> dict[str, Any]:
    """Run a synced capture. Longer default timeout — the device blocks for
    the shot duration (+ external-trigger wait) before replying."""
    try:
        status, text = _http_post_form(
            f"http://{host}:{BRIDGE_HTTP_PORT}/sync/capture", fields, timeout)
        body = {}
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            pass
        return {"ok": status == 200 and body.get("ok", False),
                "status": status, **body,
                "message": body.get("err", text.strip())}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def sync_post_stream_capture(host: str, fields: dict[str, str],
                             timeout: float = 60.0) -> dict[str, Any]:
    """Run a combined approach-A synced acquisition: the device streams
    n_samples of STM32 SPI ADC over UDP and pulses SyncOut mid-stream to fire
    the HV schedule. Blocks for the whole window (~n_samples/rate_hz s), so the
    caller should size the timeout from those fields."""
    n = int(fields.get("n_samples", 0) or 0)
    r = int(fields.get("rate_hz", 0) or 0)
    if n and r:
        timeout = max(timeout, (n / r) + 3.0)
    try:
        status, text = _http_post_form(
            f"http://{host}:{BRIDGE_HTTP_PORT}/sync/stream_capture", fields, timeout)
        body = {}
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            pass
        return {"ok": status == 200 and body.get("ok", False),
                "status": status, **body,
                "message": body.get("err", text.strip())}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_get_burst(host: str, count: int,
                  timeout: float = max(ADC_DEFAULT_HTTP_TIMEOUT, 2.0)) -> dict[str, Any]:
    """
    Fetch the most recent `count` samples as raw bytes. Returns a dict with
    keys (ok, status, bytes, headers). The device sends `application/octet-stream`
    of count*2 bytes (little-endian u16); the response also carries
    X-ADC-Rate-Hz / X-ADC-Bits / X-ADC-Count headers we forward verbatim.
    """
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/burst?n={int(count)}"
    try:
        status, body, headers = _http_get(url, timeout=timeout, accept_binary=True)
        if status != 200:
            return {"ok": False, "status": status, "error": body.decode("utf-8", errors="replace")}
        return {"ok": True, "status": 200, "bytes": body, "headers": headers}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_post_stream_start(host: str, dest_host: str, dest_port: int,
                          source: str = "sampler",
                          rate_hz: int | None = None,
                          n_samples: int | None = None,
                          timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/stream/start"
    fields: dict[str, str] = {"host": dest_host,
                              "port": str(int(dest_port)),
                              "source": str(source)}
    if rate_hz is not None:
        fields["rate_hz"] = str(int(rate_hz))
    if n_samples is not None:
        fields["n_samples"] = str(int(n_samples))
    if source == "spi" and rate_hz and n_samples:
        timeout = max(timeout, (n_samples / rate_hz) + 1.5)
    try:
        status, text = _http_post_form(url, fields, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_post_stream_stop(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/stream/stop"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def primary_local_ip() -> str | None:
    """Public re-export of `_primary_local_ip` for backend host inference."""
    return _primary_local_ip()


# -----------------------------------------------------------------------------
# Passive UDP listener for the ADC stream
# -----------------------------------------------------------------------------
#
# The device's UDP wire format (see firmware include/adc_stream.h Header):
#   <I I Q q q I I H B B I  =  48 bytes
#     magic(4) seq(4) first_index(8) t_first_us(8) t_last_us(8)
#     rate_hz(4) dropped(4) count(2) bits(1) reserved(1) pad(4)
# followed by `count` little-endian u16 samples (12-bit data in low bits).
#
# The listener is intentionally lightweight: it does not buffer samples — only
# decodes headers and tracks rolling statistics. That is enough for the GUI to
# answer "are packets actually arriving and how fast" without dragging WebSocket
# / SSE / live plotting into this module. If/when a live plot is wanted, this is
# the natural place to add a ring of samples.

_ADC_HDR_FMT = "<IIQqqIIHBBI"
_ADC_HDR_LEN = struct.calcsize(_ADC_HDR_FMT)
assert _ADC_HDR_LEN == 48, f"unexpected adc header size {_ADC_HDR_LEN}"
ADC_MAGIC          = 0x31434441  # 'ADC1' — legacy alias
ADC_MAGIC_SAMPLER  = 0x31434441  # 'ADC1' — ESP32 internal ADC
ADC_MAGIC_SPI      = 0x32434441  # 'ADC2' — STM32 SPI source
ADC_MAGIC_SYNTH    = 0x53434441  # 'ADCS' — synthetic load generator
ADC_VALID_MAGICS = {ADC_MAGIC_SAMPLER, ADC_MAGIC_SPI, ADC_MAGIC_SYNTH}
ADC_MAGIC_NAMES = {
    ADC_MAGIC_SAMPLER: "sampler",
    ADC_MAGIC_SPI:     "spi",
    ADC_MAGIC_SYNTH:   "synth",
}


class AdcUdpListener:
    """Threaded TCP listener for the device's ADC stream.

    Despite the legacy class name, this is now a TCP server: the device
    opens a single inbound connection at stream-start time and writes
    framed packets (48-byte Header + packed 12-bit payload). TCP's flow
    control replaces UDP's drop-on-overflow semantics — no samples are
    lost when the link transiently stalls.

    Wire payload is 12-bit packed (header.bits == 12). Unpack happens on
    the fly so samples_snapshot() callers still receive 16-bit u16-LE
    bytes exactly as before."""

    # Rolling sample-byte ring of unpacked 16-bit samples.
    _BUF_BYTES = 32 * 1024  # = 16384 u16 samples

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._port: int = 0
        self._sample_buf = bytearray(self._BUF_BYTES)
        self._sample_head = 0     # next write offset into _sample_buf (bytes)
        self._sample_filled = 0   # bytes ever written, capped at _BUF_BYTES
        self._record_fp = None    # optional file sink (raw u16 LE) for recording
        self._reset_stats()

    def _reset_stats(self) -> None:
        self._stats: dict[str, Any] = {
            "active": False,
            "port": 0,
            "packets": 0,
            "samples": 0,
            "drops_device": 0,
            "missed_packets": 0,  # gaps in `seq`
            "bad_magic": 0,
            "last_seq": None,
            "last_first_index": None,
            "last_t_first_us": None,
            "last_t_last_us": None,
            "last_rate_hz": None,
            "last_bits": None,
            "last_count": None,
            "last_recv_unix": None,
            "rate_hz_obs": None,    # samples/sec observed on the wire
            "buffered_samples": 0,  # u16 samples currently in the ring
            "session_started_unix": None,
        }

    def is_active(self) -> bool:
        with self._lock:
            return self._running

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._stats)

    def start(self, port: int, record_path: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._running:
                return {"ok": False, "error": "listener already running"}
            if record_path:
                try:
                    self._record_fp = open(record_path, "wb", buffering=1024 * 256)
                except OSError as exc:
                    return {"ok": False, "error": f"record file: {exc}"}
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                with suppress(OSError):
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
                sock.bind(("0.0.0.0", int(port)))
                sock.listen(1)
                sock.settimeout(0.4)
            except OSError as exc:
                if self._record_fp is not None:
                    with suppress(Exception):
                        self._record_fp.close()
                    self._record_fp = None
                return {"ok": False, "error": str(exc)}
            self._sock = sock
            self._port = int(port)
            self._running = True
            self._reset_stats()
            self._sample_head = 0
            self._sample_filled = 0
            self._stats["active"] = True
            self._stats["port"] = self._port
            self._stats["session_started_unix"] = time.time()
            self._thread = threading.Thread(target=self._run, name="adc_udp_rx", daemon=True)
            self._thread.start()
            return {"ok": True, "port": self._port}

    def samples_snapshot(self, n: int) -> bytes:
        """
        Return the most recent `n` samples as little-endian u16 bytes
        (length = 2*n_actual, where n_actual = min(n, samples in ring)).
        Cheap, holds the listener lock only long enough to copy out.
        """
        if n <= 0:
            return b""
        want_bytes = min(int(n) * 2, self._BUF_BYTES)
        with self._lock:
            avail = min(self._sample_filled, want_bytes)
            if avail == 0:
                return b""
            head = self._sample_head
            start = (head - avail) % self._BUF_BYTES
            if start < head:
                return bytes(self._sample_buf[start:head])
            # Ring wrap.
            return bytes(self._sample_buf[start:]) + bytes(self._sample_buf[:head])

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                return {"ok": True}
            self._running = False
            sock = self._sock
            self._sock = None
        if sock is not None:
            with suppress(OSError):
                sock.close()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._record_fp is not None:   # rx thread has joined → safe to close
            with suppress(Exception):
                self._record_fp.flush()
                self._record_fp.close()
            self._record_fp = None
        with self._lock:
            self._stats["active"] = False
        return {"ok": True}

    def _run(self) -> None:
        # TCP listener: accept one device connection at a time, then parse
        # framed records (48-byte header + packed 12-bit payload) until the
        # peer closes (stream stopped) or stop() is called.
        first_pkt: tuple[int, int] | None = None
        last_seq: int | None = None
        while True:
            with self._lock:
                if not self._running:
                    return
                lsock = self._sock
            if lsock is None:
                return
            try:
                conn, _addr = lsock.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            conn.settimeout(0.4)
            try:
                while True:
                    with self._lock:
                        if not self._running:
                            break
                    hdr_bytes = self._recv_exact(conn, _ADC_HDR_LEN)
                    if hdr_bytes is None:
                        break
                    try:
                        hdr = struct.unpack(_ADC_HDR_FMT, hdr_bytes)
                    except struct.error:
                        with self._lock:
                            self._stats["bad_magic"] += 1
                        break
                    (magic, seq, first_index, t_first_us, t_last_us,
                     rate_hz, dropped, count, bits, _, _) = hdr
                    if magic not in ADC_VALID_MAGICS:
                        with self._lock:
                            self._stats["bad_magic"] += 1
                        break

                    payload_len = self._payload_bytes(count, bits)
                    payload = self._recv_exact(conn, payload_len) \
                        if payload_len > 0 else b""
                    if payload is None:
                        break
                    samples_u16le = (self._unpack_12bit_to_u16le(payload, count)
                                     if bits == 12 else payload)
                    source_name = ADC_MAGIC_NAMES.get(magic)

                    now = time.time()
                    with self._lock:
                        self._stats["packets"] += 1
                        self._stats["samples"] += count
                        self._stats["drops_device"] += dropped
                        ring_len = len(samples_u16le)
                        if ring_len > 0:
                            head = self._sample_head
                            end = head + ring_len
                            if end <= self._BUF_BYTES:
                                self._sample_buf[head:end] = samples_u16le
                                self._sample_head = end % self._BUF_BYTES
                            else:
                                first = self._BUF_BYTES - head
                                self._sample_buf[head:] = samples_u16le[:first]
                                self._sample_buf[:ring_len - first] = \
                                    samples_u16le[first:]
                                self._sample_head = ring_len - first
                            self._sample_filled = min(
                                self._sample_filled + ring_len, self._BUF_BYTES
                            )
                            self._stats["buffered_samples"] = \
                                self._sample_filled // 2
                        if last_seq is not None:
                            gap = (seq - last_seq - 1) & 0xFFFFFFFF
                            if gap and gap < 0x7FFFFFFF:
                                self._stats["missed_packets"] += gap
                        last_seq = seq
                        self._stats["last_seq"] = seq
                        self._stats["last_first_index"] = first_index
                        self._stats["last_t_first_us"] = t_first_us
                        self._stats["last_t_last_us"] = t_last_us
                        self._stats["last_rate_hz"] = rate_hz
                        self._stats["last_bits"] = bits
                        self._stats["last_count"] = count
                        self._stats["last_recv_unix"] = now
                        self._stats["last_source"] = source_name
                        if first_pkt is None:
                            first_pkt = (first_index, t_first_us)
                        fi0, ts0 = first_pkt
                        dt = t_last_us - ts0
                        if dt > 0:
                            self._stats["rate_hz_obs"] = (
                                (first_index + count - fi0) * 1_000_000.0 / dt
                            )
                    # File sink (recording): write every received sample (raw
                    # u16 LE) outside the stats lock so disk I/O can't stall
                    # status()/snapshot(). Only this rx thread writes it.
                    fp = self._record_fp
                    if fp is not None and samples_u16le:
                        try:
                            fp.write(samples_u16le)
                        except OSError:
                            pass
            finally:
                with suppress(OSError):
                    conn.close()
                first_pkt = None
                last_seq = None

    @staticmethod
    def _payload_bytes(count: int, bits: int) -> int:
        """Wire bytes for `count` samples at given bit depth. 12-bit packs
        2 samples per 3 bytes, with a 2-byte tail for an odd count."""
        if bits == 12:
            return (count // 2) * 3 + (2 if count & 1 else 0)
        return count * 2

    @staticmethod
    def _unpack_12bit_to_u16le(packed: bytes, count: int) -> bytes:
        """Convert packed 12-bit payload to a flat u16-LE byte string of
        length count*2. Matches firmware adc_stream.cpp::packSamples12."""
        out = bytearray(count * 2)
        di = 0
        si = 0
        for _ in range(count // 2):
            b0 = packed[si]; b1 = packed[si + 1]; b2 = packed[si + 2]
            s0 = b0 | ((b1 & 0x0F) << 8)
            s1 = (b1 >> 4) | (b2 << 4)
            out[di]     = s0 & 0xFF
            out[di + 1] = (s0 >> 8) & 0x0F
            out[di + 2] = s1 & 0xFF
            out[di + 3] = (s1 >> 8) & 0x0F
            di += 4; si += 3
        if count & 1:
            b0 = packed[si]; b1 = packed[si + 1]
            s0 = b0 | ((b1 & 0x0F) << 8)
            out[di]     = s0 & 0xFF
            out[di + 1] = (s0 >> 8) & 0x0F
        return bytes(out)

    def _recv_exact(self, conn: socket.socket, n: int) -> bytes | None:
        """Read exactly n bytes or None if peer closed / stop()."""
        buf = bytearray(n)
        off = 0
        while off < n:
            with self._lock:
                if not self._running:
                    return None
            try:
                got = conn.recv_into(memoryview(buf)[off:], n - off)
            except socket.timeout:
                continue
            except OSError:
                return None
            if got == 0:
                return None
            off += got
        return bytes(buf)


# -----------------------------------------------------------------------------
# Triggered-capture HTTP helpers (Mode 1)
# -----------------------------------------------------------------------------
#
# Maps to the device's:
#   GET  /adc/capture                 -> JSON {state, trigger, pre, post, ...}
#   POST /adc/capture/arm             -> arm software or GPIO trigger; blocks
#                                        until window is collected or timeout
#   GET  /adc/capture/data            -> raw u16 LE bytes; multiple GETs are safe
#   POST /adc/capture/clear           -> discard buffer, allow next arm

def adc_capture_info(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/capture"
    try:
        status, body, _ = _http_get(url, timeout=timeout)
        if status != 200:
            return {"ok": False, "error": f"HTTP {status}"}
        return {"ok": True, "capture": json.loads(body.decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError, json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_capture_arm(host: str, pre: int, post: int,
                    trigger: str = "software",
                    pin: int | None = None,
                    edge: str = "rising",
                    timeout_ms: int | None = None,
                    http_timeout: float = 70.0) -> dict[str, Any]:
    """
    `http_timeout` must comfortably exceed the device's `timeout_ms` plus
    the post-fill duration. The device's GPIO-arm caps the trigger wait
    at 60 s, so 70 s default is a safe ceiling.
    """
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/capture/arm"
    fields = {"pre": str(int(pre)), "post": str(int(post)), "trigger": trigger}
    if trigger == "gpio":
        if pin is None:
            return {"ok": False, "error": "pin is required for gpio trigger"}
        fields["pin"] = str(int(pin))
        fields["edge"] = edge
        if timeout_ms is not None:
            fields["timeout_ms"] = str(int(timeout_ms))
    try:
        status, text = _http_post_form(url, fields, http_timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_capture_data(host: str, timeout: float = 10.0) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/capture/data"
    try:
        status, body, headers = _http_get(url, timeout=timeout, accept_binary=True)
        if status != 200:
            return {"ok": False, "status": status,
                    "error": body.decode("utf-8", errors="replace")}
        return {"ok": True, "status": 200, "bytes": body, "headers": headers}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def pulse_events_get(host: str, since: int = 0,
                     timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Fetch new STM32-measured pulse events with id > `since`. The host
    poll cursor lives in `last_id`; callers should persist it and pass it
    back as `since` to get only the delta on subsequent calls."""
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/pulse_events?since={int(since)}"
    try:
        status, body, _headers = _http_get(url, timeout=timeout)
        if status != 200:
            return {"ok": False, "status": status,
                    "error": body.decode("utf-8", errors="replace")}
        return {"ok": True, **json.loads(body.decode("utf-8"))}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError,
            json.JSONDecodeError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_spi_shot_arm(host: str, n_samples: int, rate_hz: int,
                     timeout: float = 20.0) -> dict[str, Any]:
    """Fire a bounded STM32→ESP32 SPI shot into PSRAM. Blocks ~n/fs s while the
    STM32 ADC streams, then returns the device JSON (ok, n_samples, rate_hz,
    run_id, capture_us, preamble). The same shot drives the STM32 pulse_detector,
    so /pulse_events fills during it."""
    url = (f"http://{host}:{BRIDGE_HTTP_PORT}/adc/spi_shot_arm"
           f"?n_samples={int(n_samples)}&rate_hz={int(rate_hz)}")
    try:
        status, text = _http_post_form(url, {}, timeout)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        if status != 200:
            return {"ok": False, "status": status,
                    "error": text.strip() or f"HTTP {status}", **body}
        return {"ok": True, "status": 200, **body}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_spi_shot_data(host: str, timeout: float = 10.0) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/spi_shot_data"
    try:
        status, body, headers = _http_get(url, timeout=timeout, accept_binary=True)
        if status != 200:
            return {"ok": False, "status": status,
                    "error": body.decode("utf-8", errors="replace")}
        return {"ok": True, "status": 200, "bytes": body, "headers": headers}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


# ---- Continuous rolling-ring STM32 ADC capture (adc_spi.cpp ring_*) ----------
# ring_start arms the STM32 for continuous capture; a core-1 task drains
# DATA_READY blocks into a PSRAM ring. The same continuous stream feeds the
# STM32 pulse_detector, so /pulse_events fills while the ring runs.

def adc_ring_start(host: str, rate_hz: int,
                   timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/start?rate_hz={int(rate_hz)}"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ring_stop(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/stop"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ring_peek(host: str, n: int,
                  timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Newest `n` ring samples as raw u16 LE bytes + X-Ring-* headers."""
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/peek?n={int(n)}"
    try:
        status, body, headers = _http_get(url, timeout=timeout, accept_binary=True)
        if status != 200:
            return {"ok": False, "status": status,
                    "error": body.decode("utf-8", errors="replace")}
        return {"ok": True, "status": 200, "bytes": body, "headers": headers}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ring_tap_start(host: str, dest_ip: str, port: int, decim: int = 1,
                       timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Stream the running ring to dest_ip:port over the adc_stream TCP socket
    (decimated to fs/decim) for host-side recording of the raw ADC waveform."""
    url = (f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/tap_start"
           f"?host={dest_ip}&port={int(port)}&decim={int(decim)}")
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ring_tap_stop(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/tap_stop"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_pulse_diag(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """STM32 pulse-detector diagnostics + ESP32-side RX counters. Used to ask the
    hardware whether the ADC is actually converting, rather than trusting any
    bookkeeping about whether something armed it."""
    return _stm32_get_json(host, "/pulse_diag", timeout)


def adc_ready_arm(host: str, rate_hz: int = 1000000, n_samples: int = 2000,
                  post_bg_gap: int | None = None, post_bg_n: int | None = None,
                  ttl_ms: int | None = None,
                  timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Arm the RP2350->STM32 pulse-envelope relay AND (inside it) the STM32
    detector. This is what makes a fired pulse actually get MEASURED: the
    detector times each pulse from the real envelope on PA4, which only moves
    while the relay is mirroring GPIO39 -> GPIO34. adc_pulse_arm() alone arms the
    detector but not the relay, so PA4 never moves and a fire yields 0 events."""
    url = (f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ready_arm"
           f"?rate_hz={int(rate_hz)}&n_samples={int(n_samples)}")
    # Omitted entirely rather than sent as 0: the ESP32 falls back to its own
    # defaults for an absent param, and 0 means something different (post_bg_n=0
    # is "do not measure"). Passing 0 to mean "unspecified" would silently turn
    # the measurement off.
    if post_bg_gap is not None: url += f"&post_bg_gap={int(post_bg_gap)}"
    if post_bg_n   is not None: url += f"&post_bg_n={int(post_bg_n)}"
    # Same omit-vs-zero rule: ttl_ms=0 explicitly DISABLES the auto-disarm, so
    # sending 0 for "unspecified" would turn off the very recovery it is for.
    if ttl_ms       is not None: url += f"&ttl_ms={int(ttl_ms)}"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ready_renew(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Push the relay's auto-disarm deadline out by its TTL. For a run that
    legitimately outlasts it; an ordinary fire finishes well inside the
    default."""
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ready_renew"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ready_disarm(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ready_disarm"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ready_status(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    return _stm32_get_json(host, "/adc/ready_status", timeout)


def adc_pulse_arm(host: str, rate_hz: int = 1000000,
                  timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    """Arm the STM32 ADC for pulse-detect ONLY (EVT_PULSE over UART) — no ESP32
    continuous SPI read, so it won't load WiFi like the ring does. Use for
    per-pulse measurement/calibration."""
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/pulse_arm?rate_hz={int(rate_hz)}"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_pulse_disarm(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/pulse_disarm"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ring_window(host: str, pre: int, post: int, src: str = "fire",
                    timeout_ms: int = 1000, timeout: float = 6.0) -> dict[str, Any]:
    """Trigger-aligned window extract from the running ring. src:
      'fire' → ESP32 pulses GP37 (sync_io::firePulse) and uses that exact edge µs;
      'gp40' → block up to timeout_ms for the external RP2350-echoed GP40 edge;
      'now'  → software trigger at the current time.
    Returns the device JSON (ok, pre, count, rate_hz, trigger_us)."""
    qs = f"?pre={int(pre)}&post={int(post)}&timeout_ms={int(timeout_ms)}"
    if src == "gp40":
        qs += "&src=gp40"
    elif src == "fire":
        qs += "&fire=1"
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/window{qs}"
    try:
        status, text = _http_post_form(url, {}, timeout)
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = {}
        if status != 200:
            return {"ok": False, "status": status, "error": text.strip() or f"HTTP {status}"}
        return {"ok": True, "status": 200, **body}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def adc_ring_window_data(host: str, timeout: float = 6.0) -> dict[str, Any]:
    """The last extracted window as raw u16 LE bytes + X-Win-* headers."""
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/ring/window_data"
    try:
        status, body, headers = _http_get(url, timeout=timeout, accept_binary=True)
        if status != 200:
            return {"ok": False, "status": status, "error": body.decode("utf-8", errors="replace")}
        return {"ok": True, "status": 200, "bytes": body, "headers": headers}
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}


def primary_local_ip() -> str | None:
    """Public alias for the host's primary IPv4 (the address the ESP32 ring tap
    connects back to). Falls back to None when there's no upstream route."""
    return _primary_local_ip()


# ---- ESP32-native framed command protocol (esp_cmd, TCP 3334) ---------------
# SOF 0xA5 0x5C | VER op flags seq len_lo len_hi | payload | CRC16-CCITT(LE).
# Used here for the Mode-2 fire-correlated per-pulse path: RING_PULSE_ARM /
# DISARM (request/response) + RING_PULSE_EVENT (pushed per GP40 edge). A
# persistent reader thread keeps the socket open so pushed events arrive.
ESP_CMD_PORT = 3334
_ESP_SOF0, _ESP_SOF1 = 0xA5, 0x5C
_ESP_OP_PING = 0x01
_ESP_OP_RING_PULSE_ARM = 0x45
_ESP_OP_RING_PULSE_DISARM = 0x46
_ESP_OP_RING_PULSE_EVENT = 0x47
_ESP_F_RESP = 0x02
_ESP_F_ERR = 0x04
_ESP_F_EVENT = 0x08
# RING_PULSE_EVENT payload: seq u32 | t_edge_us i64 | fs_hz u32 | n u32 |
#   pre u32 | baseline u16 | peak u16 | peak_index u32 | width u32 | integral i64
_ESP_EVENT_FMT = "<IqIIIHHIIq"
_ESP_EVENT_LEN = struct.calcsize(_ESP_EVENT_FMT)  # 44


def _esp_crc16(b: bytes) -> int:
    c = 0xFFFF
    for x in b:
        c ^= (x << 8) & 0xFFFF
        for _ in range(8):
            c = ((c << 1) ^ 0x1021) & 0xFFFF if (c & 0x8000) else (c << 1) & 0xFFFF
    return c


class EspCmdClient:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._host: str | None = None
        self._seq = 1
        self._events: list[dict[str, Any]] = []   # assigned monotonic 'eid'
        self._event_next = 1
        self._pending: dict[int, list] = {}        # seq -> [status_or_None, Event]
        self._last_error: str | None = None

    def is_connected(self) -> bool:
        with self._lock:
            return self._running

    def connect(self, host: str) -> bool:
        with self._lock:
            if self._running and self._host == host:
                return True
            self._close_locked()
            try:
                s = socket.create_connection((host, ESP_CMD_PORT), timeout=2.0)
                s.settimeout(0.4)
            except OSError as exc:
                self._last_error = str(exc)
                return False
            self._sock = s
            self._host = host
            self._running = True
            self._thread = threading.Thread(target=self._run, name="esp_cmd_rx", daemon=True)
            self._thread.start()
            return True

    def _close_locked(self) -> None:
        self._running = False
        if self._sock is not None:
            with suppress(OSError):
                self._sock.close()
            self._sock = None

    def disconnect(self) -> None:
        with self._lock:
            self._close_locked()
            th = self._thread
            self._thread = None
        if th is not None:
            th.join(timeout=1.0)

    def _frame(self, op: int, payload: bytes, seq: int, flags: int = 0x01) -> bytes:
        body = bytes([0x01, op, flags, seq & 0xFF, len(payload) & 0xFF,
                      (len(payload) >> 8) & 0xFF]) + payload
        c = _esp_crc16(body)
        return bytes([_ESP_SOF0, _ESP_SOF1]) + body + bytes([c & 0xFF, (c >> 8) & 0xFF])

    def request(self, op: int, payload: bytes = b"", timeout: float = 2.0) -> dict[str, Any]:
        with self._lock:
            if not self._running or self._sock is None:
                return {"ok": False, "error": "not connected"}
            seq = self._seq & 0xFF
            self._seq = (self._seq + 1) & 0xFF
            ev = threading.Event()
            self._pending[seq] = [None, ev]
            try:
                self._sock.sendall(self._frame(op, payload, seq, flags=0x01))
            except OSError as exc:
                self._pending.pop(seq, None)
                return {"ok": False, "error": str(exc)}
        if not ev.wait(timeout):
            with self._lock:
                self._pending.pop(seq, None)
            return {"ok": False, "error": "response timeout"}
        with self._lock:
            status = self._pending.pop(seq, [None])[0]
        return {"ok": status == 0x00, "status": status}

    def arm(self, pre: int, post: int, thresh: int, report: int) -> dict[str, Any]:
        payload = struct.pack("<IIHB", int(pre) & 0xFFFFFFFF, int(post) & 0xFFFFFFFF,
                              int(thresh) & 0xFFFF, int(report) & 0xFF)
        return self.request(_ESP_OP_RING_PULSE_ARM, payload)

    def disarm(self) -> dict[str, Any]:
        return self.request(_ESP_OP_RING_PULSE_DISARM)

    def events_since(self, since: int) -> list[dict[str, Any]]:
        with self._lock:
            return [e for e in self._events if e["eid"] > since]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {"connected": self._running, "host": self._host,
                    "buffered": len(self._events), "last_eid": self._event_next - 1,
                    "error": self._last_error}

    def _run(self) -> None:
        buf = bytearray()
        while True:
            with self._lock:
                if not self._running:
                    return
                sock = self._sock
            if sock is None:
                return
            try:
                chunk = sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                with self._lock:
                    self._running = False
                return
            if not chunk:   # peer closed
                with self._lock:
                    self._running = False
                return
            buf.extend(chunk)
            self._drain(buf)

    def _drain(self, buf: bytearray) -> None:
        while True:
            i = buf.find(b"\xA5\x5C")
            if i < 0:
                if len(buf) > 4096:
                    del buf[:-1]
                return
            if i:
                del buf[:i]
            if len(buf) < 8:
                return
            op = buf[3]; flags = buf[4]; seq = buf[5]
            plen = buf[6] | (buf[7] << 8)
            total = 2 + 6 + plen + 2
            if len(buf) < total:
                return
            body = bytes(buf[2:8 + plen])
            crc = buf[8 + plen] | (buf[9 + plen] << 8)
            payload = bytes(buf[8:8 + plen])
            del buf[:total]
            if _esp_crc16(body) != crc:
                continue
            self._dispatch(op, flags, seq, payload)

    def _dispatch(self, op: int, flags: int, seq: int, payload: bytes) -> None:
        if flags & _ESP_F_EVENT:
            if op == _ESP_OP_RING_PULSE_EVENT and len(payload) >= _ESP_EVENT_LEN:
                (pseq, t_edge_us, fs_hz, n, pre, baseline, peak, peak_index,
                 width, integral) = struct.unpack_from(_ESP_EVENT_FMT, payload, 0)
                with self._lock:
                    self._events.append({
                        "eid": self._event_next, "seq": pseq, "t_edge_us": t_edge_us,
                        "fs_hz": fs_hz, "n": n, "pre": pre, "baseline": baseline,
                        "peak": peak, "peak_index": peak_index, "width": width,
                        "integral": integral})
                    self._event_next += 1
                    if len(self._events) > 1024:
                        self._events = self._events[-512:]
            return
        if flags & _ESP_F_RESP:
            status = payload[0] if payload else 0xFF
            with self._lock:
                p = self._pending.get(seq)
                if p is not None:
                    p[0] = status
                    p[1].set()


def adc_capture_clear(host: str, timeout: float = ADC_DEFAULT_HTTP_TIMEOUT) -> dict[str, Any]:
    url = f"http://{host}:{BRIDGE_HTTP_PORT}/adc/capture/clear"
    try:
        status, text = _http_post_form(url, {}, timeout)
        return _post_result(status, text)
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError) as exc:
        return {"ok": False, "error": str(exc)}
