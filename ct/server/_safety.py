"""backend: dead-man watchdog machinery and the ACTIVE ladder guard (its loop stays in _server).

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


_BACKEND_VERSION: dict = {"commit": None, "dirty": False}   # set at start-up (main)


# ── Dead-man safety watchdog — state and machinery ──────────────────────────
# See SAFETY_* in the constants section for the rules. State, not constants, so
# it lives here with the code that owns it.
_SAFETY_LOCK = threading.Lock()


_SAFETY = {
    "enabled": True,
    "active_timeout_s": SAFETY_ACTIVE_TIMEOUT_S,
    "active_fallback": SAFETY_ACTIVE_FALLBACK,
    "hv_timeout_s": SAFETY_HV_TIMEOUT_S,
}


# Last COMMAND that touched each thing, monotonic. Reads never write these.
_SAFETY_TOUCH_FIL: dict[int, float] = {}


# What the backend last COMMANDED the rails to. The STM32 pin is the truth, but
# reading it every tick is an I2C round trip on the shared link; the commanded
# state is enough to decide whether a timeout has anything to act on, and the
# off command it issues is idempotent if it turns out there was nothing on.
# FIDs whose grid MOSFET was last commanded closed (or whose open could not be
# confirmed). Not read back: the watchdog's clear-all is idempotent, so acting
# on "maybe closed" costs nothing and missing a closed one costs a lot.
_GRID_CLOSED: set[int] = set()


# What the watchdog has done, for /api/safety and the log. A count that never
# moves is how you know it has never had to act.
_SAFETY_EVENTS: list[dict] = []


def safety_touch_filaments(fids, when: float | None = None) -> None:
    """Renew the dead-man timer for these filaments. COMMANDS ONLY."""
    t = when if when is not None else time.monotonic()
    with _SAFETY_LOCK:
        for f in fids or ():
            _SAFETY_TOUCH_FIL[int(f)] = t


def _safety_record(kind: str, detail: dict) -> None:
    ev = {"kind": kind, "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"), **detail}
    with _SAFETY_LOCK:
        _SAFETY_EVENTS.append(ev)
        del _SAFETY_EVENTS[:-100]
    log.warning("safety-watchdog: %s %s", kind, detail)


def _safety_open_grid() -> None:
    """Open EVERY HV grid MOSFET on every connected controller: SHV_DISARM,
    which the firmware turns into the 74HC595 /SRCLR clear-all. The emission
    and focus rails are NOT touched -- see SAFETY_HV_TIMEOUT_S."""
    with _SAFETY_LOCK:
        closed = sorted(_GRID_CLOSED)
    results, all_ok = {}, True
    for cid, link in sorted(CONTROLLERS.items()):
        if not link or not link.client.connected:
            results[str(cid)] = {"ok": False, "error": "not connected"}
            all_ok = False
            continue
        try:
            ok = _status_ok(link.request(SHV_DISARM, b"", flags=0))
        except Exception as exc:
            results[str(cid)] = {"ok": False, "error": str(exc)}
            all_ok = False
            continue
        results[str(cid)] = {"ok": ok}
        all_ok = all_ok and ok
    if all_ok:
        with _SAFETY_LOCK:
            _GRID_CLOSED.clear()
    # Not cleared on failure: the timer was renewed by the caller, so the
    # clear is retried after another hv_timeout_s instead of being forgotten.
    _safety_record("grid_off" if all_ok else "grid_off_failed",
                   {"reason": "no HV grid command within the timeout",
                    "filaments": closed, "results": results})


def _safety_schedule_running() -> tuple[bool, str | None]:
    """(a schedule is running somewhere, or why that could not be determined).

    A RUN IS ACTIVE CONTROL. The host pre-heats, arms, and then the firmware
    drives the plan for minutes with no further host command -- the client is
    only polling, and polls do not renew by design. Without this the watchdog
    would walk the filaments back MID-RUN, and it would do it by calling
    prep_filaments() directly, which bypasses the "running -- disarm first"
    guard that the single-filament endpoint applies for exactly this reason.
    So the run would be corrupted while HV was firing.

    Checked only when a timer has already expired, not every tick: one
    SHV_GET_STATUS per connected controller at the moment of decision costs
    far less than the same read at 1 Hz forever on the shared link.
    """
    for cid, link in sorted(CONTROLLERS.items()):
        if not link or not link.client.connected:
            continue
        try:
            st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
        except Exception as exc:
            return False, f"controller {cid}: {exc}"
        if st and st.get("state") in (1, 2):   # 1 = armed, 2 = running
            # Armed counts: an armed schedule waiting for its trigger is
            # deliberate control, and clearing the grid would disarm it.
            return True, None
    return False, None


def note_power_state(fids, state: int) -> None:
    now = time.monotonic()
    for f in fids:
        LAST_POWER_STATE[int(f)] = (int(state), now)
    # Every power-state COMMAND funnels through here, which makes it the one
    # place the dead-man timer has to be renewed from. Reads do not reach it.
    safety_touch_filaments(fids, now)


def ladder_blocks_active(fid: int, arrival: str | None = None,
                         arrival_known: bool = False) -> str | None:
    """None if ACTIVE is allowed for this filament, else why not.

    `arrival` is the CC loop's own verdict from 0x3A, when the caller has it.
    Being COMMANDED to IDLE is not the same as having REACHED it: a shorted
    board accepts the IDLE write (the write lands, the output never comes up),
    so state alone let ACTIVE through on a filament that never warmed. That was
    observed on the shorted CH2.8 -- IDLE reported cannot_start and ACTIVE was
    then permitted. Requiring `settled` closes it; the whole point of promoting
    from IDLE is that the filament is actually warm.
    """
    known = LAST_POWER_STATE.get(int(fid))
    if known is None:
        return ("power state unknown to this backend (no state commanded since "
                "connect, or the controller reconnected) — run the ladder "
                "STOP→SLEEP→STANDBY→IDLE first")
    st, when = known
    if st == POWER_STATE_ACTIVE:
        return None          # re-commanding a new target while already ACTIVE
    if st != POWER_STATE_IDLE:
        return (f"currently at {power_state_name(st)}; ACTIVE may only be entered "
                f"from IDLE(4) — going straight to firing current damages the "
                f"filament, and in vacuum that is unrepairable. Ladder: "
                f"STOP→SLEEP→STANDBY→IDLE→(settle)→ACTIVE")
    if not arrival_known:
        return None          # older firmware reports no arrival: state-only check
    if arrival == "settled":
        return None
    return (f"commanded to IDLE but the CC loop reports '{arrival}', not settled "
            f"— the filament has not actually reached idle current, and promoting "
            f"an unwarmed filament to firing current is what this guard prevents")


def _with_dead_stopped(out: dict, stopped: dict) -> dict:
    """Fold the STOP sent to dead filaments (see DEAD_SLEEP_IS_STOP) into a
    SLEEP batch's result: they are listed apart in dead_stopped, and a STOP
    that did not land fails the batch like any other failure."""
    if not stopped:
        return out
    out["dead_stopped"] = list(stopped.get("touched") or [])
    out["failed"] = list(out.get("failed") or []) + list(stopped.get("failed") or [])
    out["applied"] = int(out.get("applied") or 0) + int(stopped.get("applied") or 0)
    out["ok"] = bool(out.get("ok")) and bool(stopped.get("ok"))
    return out


# Every name above, for `from ... import *` (underscore names included).
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
