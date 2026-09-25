"""backend: schedule checks and trigger delay.

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
from ._wire import *  # noqa: F401,F403
from ._link import *  # noqa: F401,F403


# ---- PUSHED telemetry (firmware -> host, no request) -------------------------
# During a scan the firmware PUSHES EVENT_TELEMETRY frames (cached currents, no
# I2C) every ~50 ms. The host just receives them (client._events) and reads the
# latest — no request round-trip, so the live view can update at ~20 fps.
_LIVE_PUSH: set = set()               # controllers (1-based) with the push enabled


def _trigger_delay_one(link: "ControllerLink", delay_us=None) -> dict:
    payload = _u16(int(delay_us)) if delay_us is not None else b""
    raw = link.request(SHV_TRIGGER_DELAY, payload).get("raw") or []
    # response: [status, us_lo, us_hi, applies] = 4 bytes
    if raw and raw[0] == 0 and len(raw) >= 4:
        return {"ok": True, "delayUs": _le(raw, 1, 2), "applies": bool(raw[3])}
    return {"ok": False, "error": "no valid trigger-delay reply"}


def trigger_delay_all(delay_us=None) -> dict:
    """GET or SET the trigger delay on EVERY connected controller, as one value.

    There is deliberately no per-controller form. The master's envelope frames
    the other controller's pulses (docs/two_controller_operation.md §3), and the
    two only line up if both apply the same delay: master at 3000 us and
    controller 2 at 0 puts a 1 ms controller-2 pulse entirely outside the
    window, where it reads as a dead MOSFET. So a SET writes all of them, and
    every call reads all of them back and compares.

    `delayUs` is the common value, or None when they disagree -- never one
    board's number standing in for the rig's. An RP2350 reset zeroes its delay,
    so a disagreement after a set usually means a board restarted since.
    """
    per = {}
    for cid, link in sorted(CONTROLLERS.items()):
        if not link.client.connected:
            continue
        try:
            per[str(cid)] = _trigger_delay_one(link, delay_us)
        except Exception as exc:
            per[str(cid)] = {"ok": False, "error": str(exc)}
    if not per:
        return {"ok": False, "error": "no controller connected", "controllers": {}}
    bad = {c: r.get("error", "failed") for c, r in per.items() if not r.get("ok")}
    values = {r.get("delayUs") for r in per.values() if r.get("ok")}
    out = {"controllers": per,
           "applies": all(r.get("applies") for r in per.values() if r.get("ok")),
           "consistent": not bad and len(values) == 1}
    out["delayUs"] = next(iter(values)) if out["consistent"] else None
    if bad:
        out.update(ok=False, error=f"trigger delay could not be read/written on "
                                   f"controller(s) {sorted(bad)}: {bad}")
    elif len(values) != 1:
        shown = {c: r["delayUs"] for c, r in per.items()}
        out.update(ok=False, error=f"controllers disagree on the trigger delay "
                                   f"{shown} — the master's envelope will not line up "
                                   f"with the other board's pulses. A board that "
                                   f"reset reads 0. Set it again (it writes all).")
    else:
        out["ok"] = True
    return out


def trigger_delay_mismatch() -> str | None:
    """Why arming now would be wrong, or None. Only meaningful with more than
    one controller connected; with one there is nothing to disagree with."""
    if sum(1 for l in CONTROLLERS.values() if l.client.connected) < 2:
        return None
    r = trigger_delay_all()
    return None if r.get("ok") else r.get("error")


# Which FIDs the last successful download actually put in each controller's
# emission table. /api/arm needs it: marking a filament dead AFTER a download
# must not leave a loaded table that would still fire it, and the backend has no
# other way to know what the table contains (SHV_GET_TABLE_INFO returns counts,
# not entries). Deliberately NOT persisted -- it describes what is in the
# firmware's RAM right now, and a backend restart is no evidence about that.
LOADED_EMIT_FIDS: dict[int, set[int]] = {}


# The wire plan last downloaded to each controller, and the table CRC the
# hardware reported for it. Held here rather than in the client so a SECOND
# script run can skip a re-download it does not need -- previously every fresh
# process started with an empty cache and paid for the download again.
#
# Same non-persistence rule as LOADED_EMIT_FIDS: this describes what is in
# firmware RAM right now. It is a fast-path HINT only -- reuse still confirms
# against the live CRC before skipping, because another actor's same-shaped
# schedule must not slip past, and a reflash empties the table without telling
# anyone here.
LOADED_PLAN: dict[int, dict] = {}


LOADED_CRC: dict[int, int] = {}


# Changes on every backend start. A client that cached an order can compare
# this and know the backend forgot, rather than assuming its own snapshot is
# still shared.
ORDER_EPOCH: str = f"{int(time.time())}-{os.getpid()}"


def check_heating_plan(heating) -> list[str]:
    """Reasons this heating plan must not be downloaded, empty if it is fine."""
    problems: list[str] = []
    for n, h in enumerate(heating or []):
        try:
            st = int(h.get("state"))
            fil = int(h.get("filament"))
            arg = int(h.get("arg", h.get("milliamps", 0)))
        except (TypeError, ValueError):
            problems.append(f"heating[{n}]: unreadable state/filament/arg: {h!r}")
            continue
        if st == POWER_STATE_VOLTAGE:
            problems.append(f"heating[{n}] (filament {fil}): Voltage mode is not "
                            f"allowed in a schedule — the firmware rejects it")
        if st == POWER_STATE_ACTIVE and arg < ACTIVE_FLOOR_MA:
            problems.append(f"heating[{n}] (filament {fil}): ACTIVE {arg} mA is "
                            f"below the {ACTIVE_FLOOR_MA} mA floor")
        if st == POWER_STATE_IDLE and arg > IDLE_CEILING_MA:
            problems.append(f"heating[{n}] (filament {fil}): IDLE {arg} mA is above "
                            f"the {IDLE_CEILING_MA} mA ceiling — the RP2350 clamps "
                            f"it silently, so the schedule would run at "
                            f"{IDLE_CEILING_MA} mA and nothing would say so")
    return problems


# ---- Scan simulation --------------------------------------------------------
# Auto-generate the whole sync-pulse train, PACED over the scan duration, in a
# background thread (so the HTTP handler never blocks for ~30 s). Fire the HEAD
# of the sync chain (P1); the RP2350 chain (P1 SyncOut -> P2 SyncIn) propagates
# it, so both controllers advance per pulse. The GUI reads schedule progress via
# /api/run-status and fire progress via /api/sync/simulate-status.
_SIM_STATE: dict = {"running": False, "fired": 0, "count": 0, "stop": False, "controller": None}


_SIM_LOCK = threading.Lock()


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
    "CONTROLLERS", "ControllerLink", "DEAD_STATE_PATH", "DEFAULT_CHANNELS",
    "DEFAULT_GROUP_SIZE", "ENERGISING_STATES", "ESPCMD", "EVENT_TELEMETRY_ENABLE_BIT",
    "EspCmdClient", "FILAMENTS_PER_CONTROLLER", "FILAMENT_COUNT", "FLAG_SINGLE",
    "GEOMETRY", "HTTPStatus", "HV_REFRESH_FEEDBACK", "HV_SET_SHIFT_HZ",
    "IDLE_CEILING_MA", "LAST_POWER_STATE", "LOADED_CRC", "LOADED_EMIT_FIDS",
    "LOADED_PLAN", "LOCK_TTL_DEFAULT_S", "LOCK_TTL_MAX_S", "LOG_DIR", "NO_FILAMENT",
    "ORDER_EPOCH", "PING_PAYLOAD", "PING_TYPE", "POLL_PAUSE_MAX_S", "POWER_SLOTS",
    "POWER_STATE_ACTIVE", "POWER_STATE_IDLE", "POWER_STATE_NAMES", "POWER_STATE_SLEEP",
    "POWER_STATE_STANDBY", "POWER_STATE_STOP", "POWER_STATE_VOLTAGE", "Path",
    "PowerState", "RECORD_DIR", "RUN_REPORT_DIR", "SAFETY_ACTIVE_FALLBACK",
    "SAFETY_ACTIVE_TIMEOUT_S", "SAFETY_HV_TIMEOUT_S", "SAFETY_TICK_S",
    "SCAN_TELEMETRY_PERIOD_MS", "SCOPE_PER_CONTROLLER", "SET_EVENT_CONFIG", "SHV_ARM",
    "SHV_CAPABILITY", "SHV_CLEAR_TABLE", "SHV_DISARM", "SHV_EMIT_CHUNK",
    "SHV_FAULT_POLICY", "SHV_GET_ACTIVE_LIST", "SHV_GET_CONFIG", "SHV_GET_PULSE_LOG",
    "SHV_GET_STATUS", "SHV_GET_TABLE_INFO", "SHV_HEAT_CHUNK", "SHV_HEAT_CLEAR",
    "SHV_HEAT_GET_INFO", "SHV_HEAT_SET_ENTRIES", "SHV_SET_ACTIVE_LIST",
    "SHV_SET_CONFIG", "SHV_SET_ENTRIES", "SHV_TRIGGER_DELAY", "STATE_DIR", "STATIC_DIR",
    "TELEMETRY_MODE_CACHED", "TPS_STATUS_TIMEOUT_S", "TYPE_NAMES", "TcpProtocolClient",
    "ThreadingHTTPServer", "UART_STATUS_NAMES", "_DEAD_LOCK", "_DIAG_CHIPS",
    "_DailySizeRotatingHandler", "_EM_I_FULL_MA", "_HV_DS_CH", "_HV_FULL_V",
    "_LIVE_PUSH", "_OCP_MA_PER_CODE", "_OCP_SENSE_RESISTOR_OHMS", "_ORDER_LOCK",
    "_SIM_LOCK", "_SIM_STATE", "_SINGLE_0X3A_TRUSTED", "_TPS_IOUT_LIMIT_REG",
    "_coerce_bytes", "_coerce_int", "_is_read_command", "_le", "_pipeline_reliable",
    "_popcount", "_setup_logging", "_status_err", "_status_ok", "_suppress",
    "_trigger_delay_one", "_u16", "_u32", "_unpack_spi_shot", "adc_get_burst",
    "adc_pulse_arm", "adc_pulse_diag", "adc_pulse_disarm", "adc_ready_arm",
    "adc_ready_disarm", "adc_ready_renew", "adc_ready_status", "adc_ring_peek",
    "adc_ring_start", "adc_ring_stop", "adc_ring_window", "adc_ring_window_data",
    "adc_spi_shot_arm", "adc_spi_shot_data", "annotations", "build_command_payload",
    "build_payload", "check_heating_plan", "copy", "csv", "datetime",
    "decode_shv_status", "enum", "fetch_bridge_info", "fetch_stm32_status", "json",
    "log", "logging", "os", "parse_power_state", "power_state_name", "primary_local_ip",
    "pulse_events_get", "scan_for_bridge", "stm32_adc_window", "stm32_ads1115",
    "stm32_ds3502_get", "stm32_ds3502_set", "stm32_hv_clear_target",
    "stm32_hv_enable_set", "stm32_hv_get_target", "stm32_hv_set_target",
    "stm32_hv_status", "sync_get_burst_status", "sync_get_status", "sync_post_abort",
    "sync_post_burst", "sync_post_burst_stop", "sync_post_config", "sync_post_fire",
    "threading", "time", "trigger_delay_all", "trigger_delay_mismatch",
]
