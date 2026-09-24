"""backend: shared_read -- N GUI tabs cost the hardware what one does.

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


# ── Shared reads: N GUIs cost the hardware what ONE does ───────────────────
# Every GUI tab polls the same status endpoints on its own timer, so two tabs
# used to put twice the requests on the RP2350 link (run status, HV grid bits)
# and on the master ESP32's fragile HTTP server (ADS1115, HV pin status).
# shared_read() makes those reads single-flight with a short TTL: within the
# TTL every caller gets the one result, and concurrent callers wait for the one
# read in flight instead of each starting their own. The hardware sees at most
# 1/TTL reads per key however many tabs are open.
#
# A script that COMMANDS and then reads must never be handed the value from
# before its command: every POST (every command) drops the whole cache when it
# completes -- see do_POST.
SHARED_TTL_RUN_STATUS_S = 0.4    # /api/run-status: <= 2.5 Hz per controller


SHARED_TTL_STM32_S = 0.1         # /api/stm32/ads1115, /api/stm32/hv-status: <= 10 Hz each


SHARED_TTL_HV_SNAPSHOT_S = 1.0   # /api/hv-snapshot (HV_GET_ALL_BYTES)


_SHARED_GUARD = threading.Lock()


_SHARED_LOCKS: dict = {}


_SHARED_VALS: dict = {}


def shared_read(key, ttl_s: float, fetch):
    """fetch() at most once per ttl_s for `key`, shared by every caller."""
    with _SHARED_GUARD:
        lk = _SHARED_LOCKS.setdefault(key, threading.Lock())
    with lk:
        hit = _SHARED_VALS.get(key)
        if hit is not None and (time.monotonic() - hit[0]) <= ttl_s:
            return hit[1]
        value = fetch()
        _SHARED_VALS[key] = (time.monotonic(), value)
        return value


def shared_invalidate() -> None:
    """Drop every shared read -- called after each command (POST)."""
    with _SHARED_GUARD:
        _SHARED_VALS.clear()


# Every name above, for `from ... import *` (underscore names included).
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
