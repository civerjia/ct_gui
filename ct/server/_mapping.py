"""backend: FilamentMapping -- the active-list filament <-> board map.

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


class FilamentMapping:
    """Host-owned filament<->power mapping (firmware active-list model). Each global
    filament 0..95 is assigned to controller 0/1 (or left unassigned); a controller
    packs its filaments into power slots 0..63 in ascending filament order, slot k
    -> channel k>>3 / position k&7. Editable; default = alternating groups of
    `group_size`. Downloaded to each controller as the 64-byte ShvSetActiveList."""

    def __init__(self, group_size: int = DEFAULT_GROUP_SIZE) -> None:
        # Per-controller set of BROKEN channels (0-indexed) to skip when packing
        # filaments into power slots. A skipped channel's 8 slots are left empty
        # so filaments flow into the next good channel — e.g. skip ch5 (index 4)
        # and 48 filaments land on channels 0-3,5,6 (= CH1-4,6,7) instead of 0-5.
        self.skip_channels: dict[int, set] = {0: set(), 1: set()}
        self.set_default(group_size)

    def set_skip_channels(self, controller: int, channels) -> None:
        """channels = iterable of 0-indexed channel numbers to leave empty (broken)."""
        if controller in (0, 1):
            self.skip_channels[controller] = {int(c) for c in channels if 0 <= int(c) < 8}
            self._recompute()

    def set_default(self, group_size: int) -> None:
        gs = max(1, int(group_size))
        self.group_size = gs
        self.assignment = [((f // gs) % 2) for f in range(FILAMENT_COUNT)]  # 0=P1, 1=P2
        self._recompute()

    def set_assignment(self, assignment, group_size=None) -> None:
        """assignment = list[96] of 0/1/None (controller per global filament)."""
        a = [c if c in (0, 1) else None for c in list(assignment)[:FILAMENT_COUNT]]
        a += [None] * (FILAMENT_COUNT - len(a))
        self.assignment = a
        if group_size is not None:
            self.group_size = max(1, int(group_size))
        self._recompute()

    def _recompute(self) -> None:
        self._ctrl_fils = {0: [], 1: []}
        for f, c in enumerate(self.assignment):
            if c in (0, 1):
                self._ctrl_fils[c].append(f)
        self.slot_of: dict[int, int] = {}          # filament -> slot
        self._board_to_fil: dict[tuple, int] = {}  # (controller, slot) -> filament
        self.overflow = {0: [], 1: []}             # filaments past slot 63 (can't fire)
        for c in (0, 1):
            skip = self.skip_channels.get(c, set())
            # Usable slots = those whose channel (slot>>3) isn't skipped, in order.
            valid_slots = [s for s in range(POWER_SLOTS) if (s >> 3) not in skip]
            for i, f in enumerate(sorted(self._ctrl_fils[c])):
                if i >= len(valid_slots):
                    self.overflow[c].append(f)          # past the good slots -> can't fire
                    continue
                slot = valid_slots[i]
                self.slot_of[f] = slot
                self._board_to_fil[(c, slot)] = f

    def controller(self, filament: int):
        c = self.assignment[filament] if 0 <= filament < FILAMENT_COUNT else None
        return c if c in (0, 1) else None

    def board(self, filament: int):
        """filament -> (controller, channel, position) or None (unassigned/overflow)."""
        c = self.controller(filament)
        if c is None or filament not in self.slot_of:
            return None
        s = self.slot_of[filament]
        return c, s >> 3, s & 0x7

    def filament_for_board(self, controller: int, channel: int, position: int):
        return self._board_to_fil.get((controller, channel * 8 + position))

    def filaments(self, controller: int):
        return list(self._ctrl_fils[controller])

    def active_list(self, controller: int) -> bytes:
        out = bytearray([NO_FILAMENT] * POWER_SLOTS)
        for f in self._ctrl_fils[controller]:
            s = self.slot_of.get(f)
            if s is not None and s < POWER_SLOTS:
                out[s] = f & 0xFF
        return bytes(out)

    def channels_used(self, controller: int):
        return sorted({self.slot_of[f] >> 3 for f in self._ctrl_fils[controller] if f in self.slot_of})

    def channel_mask(self, controller: int) -> int:
        m = 0
        for ch in self.channels_used(controller):
            m |= (1 << ch)
        return m

    def as_dict(self) -> dict:
        rows = []
        for f in range(FILAMENT_COUNT):
            b = self.board(f)
            rows.append({
                "filament": f,
                "controller": self.controller(f),     # 0 / 1 / None
                "slot": self.slot_of.get(f),
                "channel": b[1] if b else None,
                "position": b[2] if b else None,
            })
        return {
            "group_size": self.group_size,
            "counts": {"1": len(self._ctrl_fils[0]), "2": len(self._ctrl_fils[1])},
            "overflow": {"1": self.overflow[0], "2": self.overflow[1]},
            "channel_mask": {"1": self.channel_mask(0), "2": self.channel_mask(1)},
            "skip_channels": {"1": sorted(self.skip_channels[0]), "2": sorted(self.skip_channels[1])},
            "filaments": rows,
        }


def order_validate(seq) -> str | None:
    """Reason this is not a usable order, or None. Checked HERE as well as in
    the client: the backend is what other clients read it back from, so a
    non-permutation stored here would corrupt every one of them, not just the
    caller that sent it."""
    n = FILAMENT_COUNT
    if not isinstance(seq, list) or len(seq) != n:
        return f"expected a list of exactly {n} integers, got {type(seq).__name__} " \
               f"of length {len(seq) if hasattr(seq, '__len__') else '?'}"
    try:
        vals = [int(v) for v in seq]
    except (TypeError, ValueError):
        return "entries must be integers"
    bad = [(i, v) for i, v in enumerate(vals) if not (0 <= v < n)]
    if bad:
        return "value(s) outside 0..%d: %s" % (
            n - 1, ", ".join(f"order[{i}]={v}" for i, v in bad[:8]))
    if len(set(vals)) != n:
        seen: dict[int, int] = {}
        dupes = []
        for i, v in enumerate(vals):
            if v in seen:
                dupes.append(f"FID {v} claimed by both USER_INDEX {seen[v]} and {i}")
            else:
                seen[v] = i
        return "not one-to-one — " + "; ".join(dupes[:6])
    return None


# Every name above, for `from ... import *` (underscore names included).
# A LITERAL list, not computed: editors (Pylance/pyright) read __all__
# statically, and a computed one left every star-imported helper
# "not defined" -- goto definition stopped working. tests/test_star_exports.py
# fails if this falls out of step with the module's globals.
__all__ = [
    "ACTIVE_FLOOR_MA", "ALL_BOARDS_MASK", "Any", "BRIDGE_DOWN_REMIND_S", "BRIDGE_PORT",
    "BaseHTTPRequestHandler", "CALIB_DIR", "CH_FILAMENT_CURRENTS",
    "CH_GET_BOARD_BITMAPS", "CH_GET_BOARD_CACHE", "CH_GET_BOARD_HEALTH",
    "CH_GET_CACHED_CURRENTS", "CH_GET_DIAGNOSIS", "CH_GET_I2C_ENABLE_MASK",
    "CH_GET_INA219", "CH_GET_PRESENT", "CH_READ_TCA9554", "CH_RESET_MUX",
    "CH_SET_I2C_ENABLE_MASK", "CH_SET_POWER_STATE", "CH_TCA9554_SELF_TEST",
    "DEAD_STATE_PATH", "DEFAULT_CHANNELS", "DEFAULT_GROUP_SIZE", "ENERGISING_STATES",
    "ESPCMD", "EVENT_TELEMETRY_ENABLE_BIT", "EspCmdClient", "FILAMENTS_PER_CONTROLLER",
    "FILAMENT_COUNT", "FLAG_SINGLE", "FilamentMapping", "GEOMETRY", "HTTPStatus",
    "HV_REFRESH_FEEDBACK", "HV_SET_SHIFT_HZ", "IDLE_CEILING_MA", "LAST_POWER_STATE",
    "LOCK_TTL_DEFAULT_S", "LOCK_TTL_MAX_S", "LOG_DIR", "NO_FILAMENT", "PING_PAYLOAD",
    "PING_TYPE", "POLL_PAUSE_MAX_S", "POWER_SLOTS", "POWER_STATE_ACTIVE",
    "POWER_STATE_IDLE", "POWER_STATE_NAMES", "POWER_STATE_SLEEP", "POWER_STATE_STANDBY",
    "POWER_STATE_STOP", "POWER_STATE_VOLTAGE", "Path", "PowerState", "RECORD_DIR",
    "RUN_REPORT_DIR", "SAFETY_ACTIVE_FALLBACK", "SAFETY_ACTIVE_TIMEOUT_S",
    "SAFETY_HV_TIMEOUT_S", "SAFETY_TICK_S", "SCAN_TELEMETRY_PERIOD_MS",
    "SCOPE_PER_CONTROLLER", "SET_EVENT_CONFIG", "SHV_ARM", "SHV_CAPABILITY",
    "SHV_CLEAR_TABLE", "SHV_DISARM", "SHV_EMIT_CHUNK", "SHV_FAULT_POLICY",
    "SHV_GET_ACTIVE_LIST", "SHV_GET_CONFIG", "SHV_GET_PULSE_LOG", "SHV_GET_STATUS",
    "SHV_GET_TABLE_INFO", "SHV_HEAT_CHUNK", "SHV_HEAT_CLEAR", "SHV_HEAT_GET_INFO",
    "SHV_HEAT_SET_ENTRIES", "SHV_SET_ACTIVE_LIST", "SHV_SET_CONFIG", "SHV_SET_ENTRIES",
    "SHV_TRIGGER_DELAY", "STATE_DIR", "STATIC_DIR", "TELEMETRY_MODE_CACHED",
    "TPS_STATUS_TIMEOUT_S", "TYPE_NAMES", "TcpProtocolClient", "ThreadingHTTPServer",
    "UART_STATUS_NAMES", "_DEAD_LOCK", "_DIAG_CHIPS", "_DailySizeRotatingHandler",
    "_EM_I_FULL_MA", "_HV_DS_CH", "_HV_FULL_V", "_OCP_MA_PER_CODE",
    "_OCP_SENSE_RESISTOR_OHMS", "_ORDER_LOCK", "_SINGLE_0X3A_TRUSTED",
    "_TPS_IOUT_LIMIT_REG", "_setup_logging", "adc_get_burst", "adc_pulse_arm",
    "adc_pulse_diag", "adc_pulse_disarm", "adc_ready_arm", "adc_ready_disarm",
    "adc_ready_renew", "adc_ready_status", "adc_ring_peek", "adc_ring_start",
    "adc_ring_stop", "adc_ring_window", "adc_ring_window_data", "adc_spi_shot_arm",
    "adc_spi_shot_data", "annotations", "build_command_payload", "copy", "csv",
    "datetime", "enum", "fetch_bridge_info", "fetch_stm32_status", "json", "log",
    "logging", "order_validate", "os", "parse_power_state", "power_state_name",
    "primary_local_ip", "pulse_events_get", "scan_for_bridge", "stm32_adc_window",
    "stm32_ads1115", "stm32_ds3502_get", "stm32_ds3502_set", "stm32_hv_clear_target",
    "stm32_hv_enable_set", "stm32_hv_get_target", "stm32_hv_set_target",
    "stm32_hv_status", "sync_get_burst_status", "sync_get_status", "sync_post_abort",
    "sync_post_burst", "sync_post_burst_stop", "sync_post_config", "sync_post_fire",
    "threading", "time",
]
