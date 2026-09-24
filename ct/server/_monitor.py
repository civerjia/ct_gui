"""backend: board monitor -- the cache reads and snapshot shape (its thread stays in _server).

Moved verbatim out of _server.py. It reads no global that is reassigned at
runtime (those, and everything that reads them, stay in _server.py), so a
star-imported name here can never be a stale copy.
"""
from __future__ import annotations
import copy
import csv
import enum
import datetime
import json
import logging
import logging.handlers
import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from ct.protocol import (
    BRIDGE_PORT,
    TYPE_NAMES,
    TcpProtocolClient,
    build_command_payload,
    fetch_bridge_info,
    fetch_stm32_status,
    scan_for_bridge,
    sync_post_fire,
    sync_post_config,
    sync_post_abort,
    sync_post_burst,
    sync_get_burst_status,
    sync_post_burst_stop,
    sync_get_status,
    adc_get_burst,
    adc_spi_shot_arm,
    adc_spi_shot_data,
    adc_ring_start,
    adc_ring_stop,
    adc_ring_peek,
    adc_ring_window,
    adc_ring_window_data,
    adc_pulse_arm,
    adc_pulse_diag,
    adc_ready_arm,
    adc_ready_disarm,
    adc_ready_renew,
    adc_ready_status,
    adc_pulse_disarm,
    primary_local_ip,
    EspCmdClient,
    pulse_events_get,
    stm32_ds3502_get,
    stm32_ds3502_set,
    stm32_hv_enable_set,
    stm32_hv_status,
    stm32_ads1115,
    stm32_adc_window,
    stm32_hv_set_target,
    stm32_hv_get_target,
    stm32_hv_clear_target,
)
from ct.paths import CALIB_DIR, LOG_DIR, RECORD_DIR, RUN_REPORT_DIR, STATE_DIR  # noqa: E402
from ct.paths import WEB_DIR as STATIC_DIR  # noqa: E402

from ._common import *  # noqa: F401,F403


# ── Board monitor: ONE reader per controller for the ring AND the matrix ────
# The ring (/api/telemetry) and the boards matrix (/api/board-snapshot) used to
# poll the RP2350 separately -- the matrix with live I2C (CH_GET_PRESENT, a
# paged live INA sweep) every ~500 ms, the ring with its own status + cached
# reads -- queueing on one link, timing each other out, and filling the gaps
# with 0 V. Now a single thread per controller reads the firmware's board cache
# (0x3D: no I2C -- the firmware's own power job keeps it, concurrently) and both
# endpoints serve from what it last read.
#
# A RUN IS DETERMINISTIC, and the cache must still be readable during it. While
# a schedule is armed or running the monitor drops to 1 Hz: SHV_GET_STATUS plus
# one 0x3D -- both no-I2C, but each reply costs the RP2350 core-0 UART time, so
# not at the idle rate -- and no bitmaps (TCA I2C; the firmware answers Busy to
# them during a run anyway). When the firmware's telemetry push is on, its
# newer values are laid over the 0x3D ones. Boards the run is not driving keep
# their last monitor reading, with its real age (firmware: the idle monitor is
# off during a run and does not expire them then).
MONITOR_PERIOD_S = 0.25          # 0x3D read cadence when idle


MONITOR_RUN_STATUS_S = 1.0       # status + 0x3D cadence while a run owns the board


MONITOR_BITMAP_PERIOD_S = 2.0    # iso/tps-enable/fault bitmaps (TCA reads), idle only


MONITOR_STALE_S = 2.0            # a snapshot older than this is not served as data


MONITOR_BITMAP_HOLD_S = 10.0     # a bitmap bit keeps its last GOOD value this long


MONITOR_ARM_HINT_S = 2.0         # an SHV_ARM this recent counts as a run already


_BOARD_MON_LOCK = threading.Lock()


_BOARD_MON: dict[int, dict] = {}   # controller (1-based) -> latest snapshot


def read_board_cache(link: "ControllerLink", channels) -> dict | None:
    """Paged 0x3D read over `channels`: {(ch, mux): board dict}, or None if the
    read failed (never a half-filled dict reported as complete).

    Entry (10 bytes): channel, mux, flags, powerState, current_mA i16,
    bus_mV u16, age_ms u16. flags: bit0 present, bit1 current valid, bit2 bus
    valid, bit3 from CC loop, bit4 TPS known, bit5 TPS answered, bit6 TPS OE,
    bit7 channel not ready. An invalid value is None here, never 0."""
    mask = bytearray(8)
    for c in channels:
        if 0 <= int(c) < 8:
            mask[int(c)] = 0xFF
    out: dict[tuple, dict] = {}
    page_start, guard = 0, 0
    while guard < 16:
        guard += 1
        resp = link.request(CH_GET_BOARD_CACHE, bytes(mask) + bytes([page_start & 0xFF, 64]),
                            flags=0, timeout=1.5)
        raw = (resp.get("raw") if isinstance(resp, dict) else None) or []
        if len(raw) < 4 or raw[0] != 0:
            return None
        total, returned = raw[1], raw[3]
        if len(raw) < 4 + returned * 10:
            return None
        for i in range(returned):
            e = raw[4 + i * 10: 14 + i * 10]
            flags = e[2]
            cur = int.from_bytes(bytes(e[4:6]), "little", signed=True)
            bus = int.from_bytes(bytes(e[6:8]), "little")
            age = int.from_bytes(bytes(e[8:10]), "little")
            not_ready = bool(flags & 0x80)
            tps_known = bool(flags & 0x10) and not not_ready
            out[(e[0], e[1])] = {
                "known": not not_ready,
                "present": bool(flags & 0x01) and not not_ready,
                "current_mA": cur if flags & 0x02 else None,
                "bus_mV": bus if flags & 0x04 else None,
                "from_cc_loop": bool(flags & 0x08),
                "tps_present": bool(flags & 0x20) if tps_known else None,
                "oe": bool(flags & 0x40) if tps_known else None,
                "power_state": e[3],
                "age_ms": None if age == 0xFFFF else age,
            }
        if returned == 0 or page_start + returned >= total:
            break
        page_start += returned
    return out


def _pushed_boards(link: "ControllerLink") -> dict:
    """Board-keyed newest values from the firmware's telemetry push (no request).
    Sentinels (0xFFF0..0xFFFF) are None -- see read_pushed_telemetry."""
    TELEMETRY_INVALID_MIN = 0xFFF0
    board: dict[tuple, dict] = {}
    try:
        events = link.client.events()
    except Exception:
        events = []
    for ev in events:
        if ev.get("type") != "EVENT_TELEMETRY":
            continue
        for e in (ev.get("decoded") or {}).get("entries", []):
            mA, mv = e.get("current_mA", 0), e.get("bus_mV", 0)
            valid = mA < TELEMETRY_INVALID_MIN
            board[(e.get("channel"), e.get("mux_port"))] = {
                "known": True, "present": valid,
                "current_mA": mA if valid else None,
                "bus_mV": mv if mv < TELEMETRY_INVALID_MIN else None,
                "from_cc_loop": True, "tps_present": None, "oe": None,
                "power_state": None, "age_ms": None, "pushed": True}
    return board


def board_monitor_snapshot(cid: int) -> dict | None:
    """The monitor's latest snapshot for one controller, or None."""
    with _BOARD_MON_LOCK:
        snap = _BOARD_MON.get(cid)
        return dict(snap) if snap else None


def monitor_board_rows(cid: int) -> tuple[list, dict]:
    """64 board dicts in /api/board-snapshot's shape, from the monitor. Values
    that are not readings are None (bus_mV/current_mA) or flagged invalid --
    never 0. Also returns a meta dict (age, source, run_owns)."""
    snap = board_monitor_snapshot(cid) or {}
    now = time.monotonic()
    boards = snap.get("boards") or {}
    fresh = bool(boards) and (now - snap.get("boards_at", 0.0)) <= MONITOR_STALE_S
    bitmaps = snap.get("bitmaps") or {}
    rows = []
    for ch in range(8):
        for mux in range(8):
            b = boards.get((ch, mux)) if fresh else None
            bm = bitmaps.get((ch, mux)) or {}
            row = {"channel": ch, "mux_port": mux, "label": f"CH{ch + 1}.{mux + 1}",
                   "present": bool(b and b.get("present")),
                   "ina_present": bool(b and b.get("present")),
                   "tps_present": b.get("tps_present") if b else None,
                   "oe": b.get("oe") if b else None,
                   "mux_present": bool(b and b.get("known")),
                   "bus_mV": b.get("bus_mV") if b else None,
                   "current_mA": b.get("current_mA") if b else None,
                   "age_ms": b.get("age_ms") if b else None,
                   "from_cc_loop": bool(b and b.get("from_cc_loop")),
                   "present_valid": bool(b and b.get("known")),
                   "current_mA_valid": bool(b) and b.get("current_mA") is not None}
            for name in ("iso_enabled", "tps_enabled", "tps_fault", "hv_overcurrent"):
                v = bm.get(name)
                ok = v is not None and (now - v[1]) <= MONITOR_BITMAP_HOLD_S
                row[name] = bool(v[0]) if ok else False
                row[f"{name}_valid"] = ok
            rows.append(row)
    meta = {"age_ms": round((now - snap.get("boards_at", 0.0)) * 1000) if boards else None,
            "source": snap.get("source"), "run_owns": bool(snap.get("run_owns")),
            "fresh": fresh}
    return rows, meta


# Every name above, for `from ... import *` (underscore names included).
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
