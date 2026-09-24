"""backend: read the backend machine's logs over the LAN.

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


# ── Remote log reading ──────────────────────────────────────────────────────
# The backend may run on another machine than the person debugging it. These
# let any client on the LAN read what is under LOG_DIR -- backend.log (and its
# dated roll-overs) and the call records of scripts that ran ON THIS MACHINE
# (logs/client/*.jsonl). READ-ONLY, no lease: they touch no hardware.
# Confined to LOG_DIR and to .log / .log.<date> / .jsonl files; a path that
# resolves outside it is refused. A read is capped at LOG_READ_MAX_BYTES from
# the END of the file (tail semantics), so a huge file cannot tie up the server.
LOG_READ_MAX_BYTES = 8 * 1024 * 1024


LOG_READ_DEFAULT_TAIL = 500


LOG_READ_MAX_TAIL = 20000


def _log_file_ok(p: Path) -> bool:
    name = p.name
    return p.is_file() and (name.endswith(".jsonl") or name.endswith(".log") or ".log." in name)


def list_log_files() -> list[dict]:
    out = []
    if LOG_DIR.is_dir():
        for p in sorted(LOG_DIR.rglob("*")):
            if _log_file_ok(p):
                st = p.stat()
                out.append({"path": p.relative_to(LOG_DIR).as_posix(), "size": st.st_size,
                            "mtime": datetime.datetime.fromtimestamp(st.st_mtime).astimezone()
                                     .isoformat(timespec="seconds")})
    return out


def read_log_file(rel: str, tail: int, grep: str | None) -> dict:
    """Last `tail` lines of one log file (after an optional substring filter)."""
    root = LOG_DIR.resolve()
    try:
        p = (root / rel).resolve()
        p.relative_to(root)               # raises if it escapes LOG_DIR
    except (ValueError, OSError):
        return {"ok": False, "error": f"not a log path: {rel!r}"}
    if not _log_file_ok(p):
        return {"ok": False, "error": f"no such log file: {rel!r} (see /api/logs)"}
    size = p.stat().st_size
    start = max(0, size - LOG_READ_MAX_BYTES)
    with open(p, "rb") as fh:
        fh.seek(start)
        data = fh.read()
    lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]                 # first line is cut mid-way
    if grep:
        lines = [ln for ln in lines if grep in ln]
    tail = max(1, min(int(tail), LOG_READ_MAX_TAIL))
    matched = len(lines)
    return {"ok": True, "path": p.relative_to(root).as_posix(), "size": size,
            "lines": lines[-tail:], "matched": matched, "returned": min(tail, matched),
            "scanned_from_byte": start, "grep": grep or None}


# Every name above, for `from ... import *` (underscore names included).
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
