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
    fetch_wifi_diag,
    mark_low_priority,
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


FORCE_OFF_DEADLINE_S = 15.0      # keep trying this long before reporting a switch stuck ON


def _hv_bytes(link: "ControllerLink") -> tuple[list[int], list[int]] | None:
    """(desired[8], feedback[8]) with feedback REALLY re-read from the 165s
    (0x14 mask 0xFF), desired from 0x13. None if either read fails."""
    fb = link.request(HV_REFRESH_FEEDBACK, bytes([0xFF]), flags=0, timeout=2.0)
    fraw = fb.get("raw") if isinstance(fb, dict) else None
    if not fraw or len(fraw) < 9 or fraw[0] != 0x00:
        return None
    cur = link.request(0x13, b"", flags=0, timeout=2.0)
    craw = cur.get("raw") if isinstance(cur, dict) else None
    if not craw or len(craw) < 9 or craw[0] != 0x00:
        return None
    return list(craw[1:9]), list(fraw[1:9])


def force_grid_off(link: "ControllerLink", controller: int, masks: dict[int, int] | None = None,
                   reason: str = "") -> dict:
    """Open HV grid switches and do not stop until the hardware SAYS they are open.

    `masks` = {channel: bitmask} to open; None = every switch on the controller.
    An OFF whose outcome is unknown (timeout, refused, no read-back) is never
    taken as done: a switch left closed puts HV on a filament nobody asked for.
    Each round writes the zeros, then confirms with a real 165 read-back AND the
    firmware's desired byte. From the second round on it also sends SHV_DISARM
    first -- the /SRCLR clear-all, which works even while the PIO owns the shift
    pins (when HV_SET_* is refused) -- and keeps going until FORCE_OFF_DEADLINE_S.
    Returns {ok, rounds, disarmed, still_on: {ch: mask}, error?}."""
    want = {int(c): int(m) & 0xFF for c, m in (masks or {c: 0xFF for c in range(8)}).items()
            if int(m) & 0xFF}
    link.hold_monitor()
    deadline = time.monotonic() + FORCE_OFF_DEADLINE_S
    rounds, disarmed, still_on, last_err = 0, False, dict(want), None
    while True:
        rounds += 1
        last_err = None
        try:
            if rounds > 1:
                link.request(SHV_DISARM, b"", flags=0, timeout=2.0)
                disarmed = True
            if set(want) == set(range(8)) and all(m == 0xFF for m in want.values()):
                link.request(0x15, bytes([0xFF]) + bytes(8) + bytes([2]), flags=0, timeout=3.0)
            else:
                for ch, m in want.items():
                    for bit in range(8):
                        if m & (1 << bit):
                            link.request(0x10, bytes([ch, bit, 0, 2]), flags=0, timeout=2.0)
            got = _hv_bytes(link)
            if got is None:
                last_err = "read-back failed"
            else:
                desired, feedback = got
                still_on = {ch: (desired[ch] | feedback[ch]) & m for ch, m in want.items()
                            if (desired[ch] | feedback[ch]) & m}
                if not still_on:
                    break
                last_err = "switch still reads ON"
        except Exception as exc:
            last_err = str(exc)
        if time.monotonic() >= deadline:
            break
        time.sleep(0.2)
    ok = not still_on and last_err is None
    out = {"ok": ok, "rounds": rounds, "disarmed": disarmed}
    if ok:
        if rounds > 1 or reason:
            log.warning("HV force-off P%d %s: confirmed OPEN after %d round(s)%s (%s)",
                        controller, _masks_text(want), rounds,
                        ", SHV_DISARM used" if disarmed else "", reason or "requested")
    else:
        out["still_on"] = {str(c): m for c, m in still_on.items()}
        out["error"] = (f"Power {controller}: could NOT confirm HV switch(es) "
                        f"{_masks_text(still_on)} open after {rounds} tries in "
                        f"{FORCE_OFF_DEADLINE_S:.0f} s ({last_err}) -- treat them as CLOSED; "
                        f"turn emission/focus off")
        log.error("HV force-off FAILED: %s (%s)", out["error"], reason or "requested")
    return out


def _masks_text(masks: dict[int, int]) -> str:
    """{0: 0b1100} -> 'CH1.3, CH1.4' (1-based, as the GUI names them); a whole
    channel is 'CH2 (all)', every channel 'all switches'."""
    if len(masks) == 8 and all(m & 0xFF == 0xFF for m in masks.values()):
        return "all switches"
    names = []
    for c, m in sorted(masks.items()):
        if m & 0xFF == 0xFF:
            names.append(f"CH{c + 1} (all)")
        else:
            names += [f"CH{c + 1}.{b + 1}" for b in range(8) if m & (1 << b)]
    return ", ".join(names) or "none"


def _safety_open_grid() -> None:
    """Open EVERY HV grid MOSFET on every connected controller, confirmed from
    the 165 read-back (force_grid_off: zero write, then SHV_DISARM's /SRCLR
    clear-all if that does not take). The emission and focus rails are NOT
    touched -- see SAFETY_HV_TIMEOUT_S."""
    with _SAFETY_LOCK:
        closed = sorted(_GRID_CLOSED)
    results, all_ok = {}, True
    for cid, link in sorted(CONTROLLERS.items()):
        if not link or not link.client.connected:
            results[str(cid)] = {"ok": False, "error": "not connected"}
            all_ok = False
            continue
        r = force_grid_off(link, cid, None, reason="dead-man: no HV grid command within the timeout")
        results[str(cid)] = r
        all_ok = all_ok and bool(r.get("ok"))
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
            st = shv_status_retry(link)
        except Exception as exc:
            return False, f"controller {cid}: {exc}"
        if st and st.get("state") in (1, 2):   # 1 = armed, 2 = running
            # Armed counts: an armed schedule waiting for its trigger is
            # deliberate control, and clearing the grid would disarm it.
            return True, None
    return False, None


# The IDLE current (mA) each FID was last commanded to -- what a timed ACTIVE
# returns to when the caller does not name one.
LAST_IDLE_MA: dict[int, int] = {}


# Timed ACTIVE: FID -> (monotonic deadline, IDLE mA to return to). Set by the
# state endpoints when an ACTIVE command carries active_s; the deadline loop
# walks the filament back to IDLE when it passes. ANY later state command for
# that FID clears it (note_power_state), so a STOP or a new IDLE always wins.
ACTIVE_DEADLINES: dict[int, tuple[float, int]] = {}


# When each FID last LEFT ACTIVE (monotonic). A filament that was at firing
# current moments ago is hot, so the "IDLE must have settled" part of the
# ACTIVE guard does not apply to it for WARM_AFTER_ACTIVE_S: coming down from
# ACTIVE the CC loop keeps adjusting while the filament cools, and every
# re-sent IDLE restarts its settle test, so a hot filament could be refused
# ACTIVE for tens of seconds (2026-10-02 17:26, filament 16).
LAST_ACTIVE_LEFT: dict[int, float] = {}
WARM_AFTER_ACTIVE_S = 30.0

# When each FID ENTERED IDLE (monotonic) -- from another state, not on a
# re-sent IDLE: the same IDLE again does not cool a filament, so it must not
# restart the "held at IDLE long enough" clock the ACTIVE guard uses.
IDLE_SINCE: dict[int, float] = {}


def note_active_transition(fid: int, old_state, new_state: int, when: float) -> None:
    """Record leaving ACTIVE and entering IDLE. Caller holds _SAFETY_LOCK."""
    if old_state == POWER_STATE_ACTIVE and int(new_state) != POWER_STATE_ACTIVE:
        LAST_ACTIVE_LEFT[int(fid)] = when
    if int(new_state) == POWER_STATE_IDLE:
        if old_state != POWER_STATE_IDLE or int(fid) not in IDLE_SINCE:
            IDLE_SINCE[int(fid)] = when
    else:
        IDLE_SINCE.pop(int(fid), None)


# Who last commanded each filament's power state (the "owner"). A keepalive
# with no explicit filament list renews ONLY its sender's filaments: it used to
# renew every filament, so when the client that held a filament at ACTIVE
# crashed, any other client that had ever energised anything (a monitor that
# once set IDLE) kept that ACTIVE alive forever (2026-10-07, two
# combined_clients). The owner is the request's session (X-CT-Session, set by
# backend-side remote sessions) or its X-CT-Client, captured per request thread.
POWER_OWNER: dict[int, str] = {}
_REQUEST_OWNER = threading.local()


def set_request_owner(owner: str | None) -> None:
    """Called at the top of every POST: whose commands this thread carries."""
    _REQUEST_OWNER.owner = owner


def request_owner() -> str | None:
    return getattr(_REQUEST_OWNER, "owner", None)


def owned_filaments(owner: str) -> list[int]:
    with _SAFETY_LOCK:
        return [f for f, o in POWER_OWNER.items() if o == owner]


def note_power_state(fids, state: int, args=None) -> None:
    """Record CONFIRMED power-state commands. `args`: the mA each FID was given
    ({fid: mA}, or one number for all) -- kept for IDLE, as a timed ACTIVE's
    default return current."""
    now = time.monotonic()
    owner = request_owner()
    with _SAFETY_LOCK:
        for f in fids:
            if owner:
                POWER_OWNER[int(f)] = owner
            prev = LAST_POWER_STATE.get(int(f))
            note_active_transition(int(f), prev[0] if prev else None, int(state), now)
            LAST_POWER_STATE[int(f)] = (int(state), now)
            ACTIVE_DEADLINES.pop(int(f), None)
            if int(state) == POWER_STATE_IDLE and args is not None:
                a = args.get(int(f)) if isinstance(args, dict) else args
                if a is not None:
                    LAST_IDLE_MA[int(f)] = int(a)
    # Every power-state COMMAND funnels through here, which makes it the one
    # place the dead-man timer has to be renewed from. Reads do not reach it.
    safety_touch_filaments(fids, now)


def set_active_deadlines(fids, active_s: float, idle_ma: dict) -> None:
    """Start the timed-ACTIVE clock for FIDs that just LANDED at ACTIVE. Call
    after note_power_state (which clears any previous deadline)."""
    until = time.monotonic() + float(active_s)
    with _SAFETY_LOCK:
        for f in fids:
            ACTIVE_DEADLINES[int(f)] = (until, int(idle_ma[int(f)]))


# A filament held at IDLE this long, whose measured current is within
# IDLE_WARM_TOL_MA (or IDLE_WARM_TOL_FRAC of target, whichever is larger) of its
# target, IS warm even if the CC loop's arrival flag says "ramping".
IDLE_WARM_HOLD_S = 10.0
IDLE_WARM_TOL_MA = 100
IDLE_WARM_TOL_FRAC = 0.08


def ladder_blocks_active(fid: int, arrival: str | None = None,
                         arrival_known: bool = False,
                         current_ma: float | None = None,
                         target_ma: float | None = None) -> str | None:
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
    held_s = time.monotonic() - IDLE_SINCE.get(int(fid), when)
    # "ramping" is not only "still warming up". The firmware clears its
    # settled flag whenever the current drifts more than 60 mA off target
    # (kCurrentLoopReengageBandMa) and only sets it again after 4 flat reads --
    # a filament with a noisy contact flips to "ramping" for a moment after an
    # hour at IDLE (filament 8, 2026-10-05 10:50, at 1280/1300 mA). The guard is
    # about WARMTH, so ask the current: held at IDLE a while and measured near
    # its target is warm. A board re-armed from the floor (revive, fault retry)
    # or one that never came up (short, open) is far below target and is still
    # refused.
    if (arrival == "ramping" and held_s >= IDLE_WARM_HOLD_S
            and current_ma is not None and target_ma):
        tol = max(IDLE_WARM_TOL_MA, IDLE_WARM_TOL_FRAC * float(target_ma))
        if abs(float(current_ma) - float(target_ma)) <= tol:
            return None
    left = LAST_ACTIVE_LEFT.get(int(fid))
    if (arrival == "ramping" and left is not None
            and time.monotonic() - left <= WARM_AFTER_ACTIVE_S):
        # Still hot from ACTIVE: the settle requirement exists for a filament
        # that has not been warmed, which this one has. "capped" is still
        # refused (the output cannot reach the current at all).
        return None
    meas = (f"measured {current_ma:.0f} mA of {target_ma:.0f} mA target"
            if current_ma is not None and target_ma else "no current reading")
    return (f"not warm yet: at IDLE for {held_s:.0f} s, CC loop '{arrival}', {meas}. "
            f"ACTIVE is allowed once the loop settles, or after "
            f"{IDLE_WARM_HOLD_S:.0f} s at IDLE within "
            f"{IDLE_WARM_TOL_MA} mA of target — promoting an unwarmed filament "
            f"to firing current is what this guard prevents")


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
# A LITERAL list, not computed: editors (Pylance/pyright) read __all__
# statically, and a computed one left every star-imported helper
# "not defined" -- goto definition stopped working. tests/test_star_exports.py
# fails if this falls out of step with the module's globals.
__all__ = [
    "DOWNLOAD_FRAME_TIMEOUTS_S",
    "DOWNLOAD_WINDOW",
    "DOWNLOAD_PARALLEL",
    "POWER_OWNER", "_REQUEST_OWNER", "set_request_owner", "request_owner", "owned_filaments",
    "RETRY_TIMEOUTS_S", "request_retry", "shv_status_retry",
    "deque",
    "ACTIVE_FLOOR_MA", "ALL_BOARDS_MASK", "Any", "BRIDGE_DOWN_REMIND_S", "BRIDGE_PORT",
    "BaseHTTPRequestHandler", "CALIB_DIR", "CH_FILAMENT_CURRENTS",
    "CH_GET_BOARD_BITMAPS", "CH_GET_BOARD_CACHE", "CH_GET_BOARD_HEALTH",
    "CH_GET_CACHED_CURRENTS", "CH_GET_DIAGNOSIS", "CH_GET_I2C_ENABLE_MASK",
    "CH_GET_INA219", "CH_GET_PRESENT", "CH_READ_TCA9554", "CH_RESET_MUX",
    "CH_SET_I2C_ENABLE_MASK", "CH_SET_POWER_STATE", "CH_TCA9554_SELF_TEST",
    "CONTROLLERS", "ControllerLink", "DEAD_STATE_PATH", "ORDER_STATE_PATH", "DEFAULT_CHANNELS",
    "DEFAULT_GROUP_SIZE", "ENERGISING_STATES", "ESPCMD", "EVENT_TELEMETRY_ENABLE_BIT",
    "EspCmdClient", "FILAMENTS_PER_CONTROLLER", "FILAMENT_COUNT", "FLAG_SINGLE",
    "GEOMETRY", "HTTPStatus", "HV_REFRESH_FEEDBACK", "HV_SET_SHIFT_HZ",
    "ACTIVE_DEADLINES", "IDLE_CEILING_MA", "IDLE_SINCE", "IDLE_WARM_HOLD_S", "IDLE_WARM_TOL_FRAC", "IDLE_WARM_TOL_MA", "LAST_ACTIVE_LEFT", "LAST_IDLE_MA", "LAST_POWER_STATE", "LOCK_TTL_DEFAULT_S", "LOCK_TTL_MAX_S",
    "LOG_DIR", "NO_FILAMENT", "PING_PAYLOAD", "PING_TYPE", "LINK_SILENT_S", "MONITOR_YIELD_S", "WIFI_DIAG_PERIOD_S", "POLL_PAUSE_MAX_S",
    "POWER_SLOTS", "POWER_STATE_ACTIVE", "POWER_STATE_IDLE", "POWER_STATE_NAMES",
    "POWER_STATE_SLEEP", "POWER_STATE_STANDBY", "POWER_STATE_STOP",
    "POWER_STATE_VOLTAGE", "Path", "PowerState", "RECORD_DIR", "RUN_REPORT_DIR",
    "SAFETY_ACTIVE_FALLBACK", "SAFETY_ACTIVE_TIMEOUT_S", "SAFETY_HV_TIMEOUT_S",
    "SAFETY_TICK_S", "SCAN_TELEMETRY_PERIOD_MS", "SCOPE_PER_CONTROLLER",
    "SET_EVENT_CONFIG", "SHV_ARM", "SHV_CAPABILITY", "SHV_CLEAR_TABLE", "SHV_DISARM",
    "SHV_EMIT_CHUNK", "SHV_FAULT_POLICY", "SHV_GET_ACTIVE_LIST", "SHV_GET_CONFIG",
    "SHV_GET_PULSE_LOG", "SHV_GET_STATUS", "SHV_GET_TABLE_INFO", "SHV_HEAT_CHUNK",
    "SHV_HEAT_CLEAR", "SHV_HEAT_GET_INFO", "SHV_HEAT_SET_ENTRIES",
    "SHV_SET_ACTIVE_LIST", "SHV_SET_CONFIG", "SHV_SET_ENTRIES", "SHV_TRIGGER_DELAY",
    "STATE_DIR", "STATIC_DIR", "TELEMETRY_MODE_CACHED", "TPS_STATUS_TIMEOUT_S",
    "TYPE_NAMES", "TcpProtocolClient", "ThreadingHTTPServer", "UART_STATUS_NAMES",
    "_BACKEND_VERSION", "_DEAD_LOCK", "_DIAG_CHIPS", "_DailySizeRotatingHandler",
    "_EM_I_FULL_MA", "_GRID_CLOSED", "_HV_DS_CH", "_HV_FULL_V", "_OCP_MA_PER_CODE",
    "_OCP_SENSE_RESISTOR_OHMS", "_ORDER_LOCK", "_SAFETY", "_SAFETY_EVENTS",
    "_SAFETY_LOCK", "_SAFETY_TOUCH_FIL", "_SINGLE_0X3A_TRUSTED", "_TPS_IOUT_LIMIT_REG",
    "_coerce_bytes", "_coerce_int", "_is_read_command", "_le", "_pipeline_reliable",
    "_popcount", "_safety_open_grid", "FORCE_OFF_DEADLINE_S", "_hv_bytes", "force_grid_off", "_masks_text", "_safety_record", "_safety_schedule_running",
    "_setup_logging", "_status_err", "_status_ok", "_suppress", "_u16", "_u32",
    "_unpack_spi_shot", "_with_dead_stopped", "adc_get_burst", "adc_pulse_arm",
    "adc_pulse_diag", "adc_pulse_disarm", "adc_ready_arm", "adc_ready_disarm",
    "adc_ready_renew", "adc_ready_status", "adc_ring_peek", "adc_ring_start",
    "adc_ring_stop", "adc_ring_window", "adc_ring_window_data", "adc_spi_shot_arm",
    "adc_spi_shot_data", "annotations", "build_command_payload", "build_payload",
    "copy", "csv", "datetime", "decode_shv_status", "enum", "fetch_bridge_info",
    "fetch_stm32_status", "fetch_wifi_diag", "mark_low_priority", "json", "ladder_blocks_active", "log", "logging",
    "WARM_AFTER_ACTIVE_S", "note_active_transition", "note_power_state", "os",
    "set_active_deadlines", "parse_power_state", "power_state_name",
    "primary_local_ip", "pulse_events_get", "safety_touch_filaments", "scan_for_bridge",
    "stm32_adc_window", "stm32_ads1115", "stm32_ds3502_get", "stm32_ds3502_set",
    "stm32_hv_clear_target", "stm32_hv_enable_set", "stm32_hv_get_target",
    "stm32_hv_set_target", "stm32_hv_status", "sync_get_burst_status",
    "sync_get_status", "sync_post_abort", "sync_post_burst", "sync_post_burst_stop",
    "sync_post_config", "sync_post_fire", "threading", "time",
]
