"""backend: bulk hardware reads and diagnostics.

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
from ._schedule import *  # noqa: F401,F403


def read_channel_mask(link: "ControllerLink"):
    """Read the controller's I2C channel enable mask (which channels are used)."""
    try:
        resp = link.client.send_request(CH_GET_I2C_ENABLE_MASK, b"", timeout=1.5)
        if resp.get("status_code") != 0x00:
            return None
        return (resp.get("decoded") or {}).get("mask")
    except Exception:
        return None


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
    bits = []   # 64-entry per-board per-chip states for the chip-health table
    for ch in range(8):
        for mux in range(8):
            bit = 1 << mux
            entry = {"channel": ch, "mux_port": mux, "label": f"CH{ch + 1}.{mux + 1}"}
            for chip in _DIAG_CHIPS:
                a = bool((dec.get(f"{chip}_addr_mask") or [0] * 8)[ch] & bit)
                r = bool((dec.get(f"{chip}_reg_mask") or [0] * 8)[ch] & bit)
                o = bool((dec.get(f"{chip}_op_mask") or [0] * 8)[ch] & bit)
                state = "op" if o else "reg" if r else "addr" if a else "missing"
                counts[state] += 1
                entry[f"{chip}_state"] = state
            bits.append(entry)
    return {"diagnosis_counts": counts, "diagnosis_bits": bits}


def run_self_test(link: "ControllerLink") -> dict:
    """Non-destructive TCA9554 self-test (CH_TCA9554_SELF_TEST 0x60). The firmware
    handler is PER-CHANNEL: request = [channel]; reply = status, channel, pass[4]
    in chip order Enable / Fault / Iso12V / HvCurrent, where 1 = the chip's
    polarity register (0x02) round-trips OK (= expander alive + addressable on I2C;
    no output load is driven). We loop the 8 channels and report per-chip pass/fail
    so a dead/unresponsive expander shows up in the chip-health matrix.

    NOTE: this is a CHIP-liveness test, not a pin-drive/HV-actuation test. A chip
    that passes here can still have a dead output pin or HV switch — that level is
    covered by the capability test (ShvCapabilityTest 0x7B verify)."""
    names = ["enable", "fault", "iso", "hv"]   # firmware chip order on the wire
    # Probe 0x60 on channel 0. Older RP2350b firmware lacks the handler and replies
    # Unsupported (the dispatch default); in that case fall back to deriving chip
    # liveness from the 0x61 register read, whose per-read ACK flags already say
    # whether each expander responds. Same per-chip pass/fail shape either way.
    try:
        probe = link.client.send_request(CH_TCA9554_SELF_TEST, bytes([0]), timeout=2.0)
    except Exception as exc:
        return {"selftest_error": str(exc)}
    praw = probe.get("raw") or []
    polarity_ok = probe.get("status_code") == 0x00 and len(praw) >= 6

    chips = []
    passed = 0
    if polarity_ok:
        for ch in range(8):
            resp = probe if ch == 0 else link.client.send_request(
                CH_TCA9554_SELF_TEST, bytes([ch]), timeout=2.0)
            raw = resp.get("raw") or []
            if resp.get("status_code") != 0x00 or len(raw) < 6:
                chips.append({"channel": ch})   # no result (mux likely dead)
                continue
            row = {"channel": ch}
            for i, name in enumerate(names):
                ok = bool(raw[2 + i])
                row[name] = ok
                passed += int(ok)
            chips.append(row)
        method = "polarity"   # 0x60 write+read+restore round-trip
    else:
        # 0x61 fallback: a chip is "alive" if ANY of its 4 register reads ACKed.
        rd = run_tca9554_read(link)
        if rd.get("tca9554_error"):
            return {"selftest_error": "0x60 unsupported; 0x61 fallback failed: " + rd["tca9554_error"]}
        for c in rd.get("tca9554_channels", []):
            row = {"channel": c.get("channel")}
            for name in names:
                chip = (c.get("chips") or {}).get(name)
                ok = bool(chip and (chip.get("ok", 0) & 0x0F))
                row[name] = ok
                passed += int(ok)
            chips.append(row)
        method = "read_ack"   # derived from 0x61 register-read ACKs

    return {
        "selftest_chips": chips,
        "selftest_method": method,
        "selftest_counts": {"chips_passed": passed, "chips_total": 8 * len(names)},
    }


def run_tca9554_read(link: "ControllerLink") -> dict:
    """Full TCA9554 register dump via CH_READ_TCA9554 (0x61). The firmware handler
    is PER-CHANNEL: request = [channel]; reply = status, channel, then 4 chips
    (enable/fault/iso/hv) × {config(0x03), input(0x00=live GPIO), output(0x01),
    polarity(0x02), ok}. We loop the 8 channels and assemble channels[8] in the
    shape the matrix renderer expects: channels[ch].chips[name] = {config, input,
    output, polarity, ok}."""
    names = ["enable", "fault", "iso", "hv"]
    channels = []
    for ch in range(8):
        try:
            resp = link.client.send_request(CH_READ_TCA9554, bytes([ch]), timeout=2.0)
        except Exception as exc:
            return {"tca9554_error": str(exc)}
        raw = resp.get("raw") or []
        if resp.get("status_code") != 0x00 or len(raw) < 22:
            if ch == 0:   # opcode unsupported / bad → surface it once
                return {"tca9554_error": _status_err(resp, "CH_READ_TCA9554") or "short reply"}
            channels.append({"channel": ch, "chips": {}})
            continue
        chips = {}
        for i, name in enumerate(names):
            base = 2 + i * 5
            chips[name] = {"config": raw[base], "input": raw[base + 1],
                           "output": raw[base + 2], "polarity": raw[base + 3], "ok": raw[base + 4]}
        channels.append({"channel": ch, "chips": chips})
    return {"tca9554_channels": channels}


def read_ina_by_board(link: "ControllerLink", channels) -> dict:
    """Multi-board paged INA219 (0x24, flags=0 = NOT single-board) over `channels`,
    keyed by (channel, mux_port). This is the host POLL primitive — independent of
    the filament map, so enabling a channel in the scan mask surfaces its boards'
    V/I directly. (The firmware's own enable mask is inert; the host decides what
    to read.) `channels` = iterable of 0-indexed channel numbers."""
    mask = bytearray(8)
    for c in channels:
        if 0 <= int(c) < 8:
            mask[int(c)] = 0xFF
    out: dict[tuple, dict] = {}
    # 33 = the confirmed max entries/page this 7-byte-entry response can carry
    # (RP2350 firmware 9973f10) -- above it writeUartFrame used to silently
    # drop the reply (no error, no data, caller waits out the full timeout).
    # Halves the page count for a 48-port sweep (3 pages -> 2).
    page_start, max_entries, guard = 0, 33, 0
    while guard < 16:
        guard += 1
        payload = bytes(mask) + bytes([page_start & 0xFF, max_entries & 0xFF])
        resp = link.request(CH_GET_INA219, payload, flags=0, timeout=1.5)
        dec = resp.get("decoded") if isinstance(resp, dict) else None
        entries = (dec or {}).get("entries", [])
        total = (dec or {}).get("total_matching_entries", 0)
        for e in entries:
            out[(e.get("channel"), e.get("mux_port"))] = {
                "present": bool(e.get("present")),
                "bus_mV": e.get("bus_mV", 0), "current_mA": e.get("current_mA", 0),
            }
        if not entries or len(out) >= total or len(entries) < max_entries:
            break
        page_start += len(entries)
    return out


def read_cached_currents_by_board(link: "ControllerLink", channels) -> dict:
    """Paged bulk read of the CC-loop CACHED currents (0x3A) over `channels`, keyed
    by (channel, mux_port). NO I2C on the firmware side — it returns the currents the
    current loop already measured — so this is SAFE to poll while a schedule is
    firing (the live INA sweep is skipped then because it stalls pulses)."""
    mask = bytearray(8)
    for c in channels:
        if 0 <= int(c) < 8:
            mask[int(c)] = 0xFF
    out: dict[tuple, dict] = {}
    # 33 = confirmed max entries/page (see read_ina_by_board's comment).
    page_start, max_entries, guard = 0, 33, 0
    while guard < 16:
        guard += 1
        payload = bytes(mask) + bytes([page_start & 0xFF, max_entries & 0xFF])
        resp = link.request(CH_GET_CACHED_CURRENTS, payload, flags=0, timeout=1.5)
        dec = resp.get("decoded") if isinstance(resp, dict) else None
        entries = (dec or {}).get("entries", [])
        total = (dec or {}).get("total_matching_entries", 0)
        for e in entries:
            out[(e.get("channel"), e.get("mux_port"))] = {
                "mode": e.get("mode", 0),
                "current_mA": e.get("measured_mA", 0),
                "target_mA": e.get("target_mA", 0),
            }
        if not entries or len(out) >= total or len(entries) < max_entries:
            break
        page_start += len(entries)
    return out


def _cached_entry(fil: int, raw_mode: int, current_mA, target_mA) -> dict:
    """Decode ONE cached-current entry into the per-filament dict shape. Shared by
    the paged bulk read and the single-board read so both return byte-identical
    fields -- a caller must never be able to tell which path served it.

    The mode byte carries the real mode in the LOW bits (0 voltage, 1 current
    Idle/Active, 2/3 fault) plus TWO independent flags, both of which must be
    masked off before the mode is read (channel_controller.h:216-224):
      0x80 kCcModeStaleFlag       -- measuredMilliAmps is not a live measurement
      0x40 kCcModeUnavailableFlag -- the sample could not be produced AT ALL
                                     (channel not ready). Distinct from stale.
    Masking only 0x80 (as this did) leaves 0x40 in the mode, so an unavailable
    board decodes as mode 64 -- nonzero, i.e. reported PRESENT and regulating.
    The bulk read sets stale|unavailable = 0xC0 on any board it failed to sample,
    so that path produced exactly this.

    current_mA is None (not 0) when stale or unavailable -- clients must
    `is not None` guard it (ct_simple_control.wait_for_current does; app.js's
    ingestTelemetry has the same guard on current_mA but NOT on state, which
    still updates from inferState() using this null -- that can still show a
    brief STOP for an ACTIVE board between measurements; pre-existing)."""
    CC_STALE, CC_UNAVAILABLE = 0x80, 0x40
    # The mode byte is FOUR fields, not one value plus two flags:
    #   bits 0-3  base mode (0 voltage, 1 current Idle/Active, 2 FaultOpen,
    #             3 FaultOcp)
    #   bits 4-5  ARRIVAL: 0x00 ramping, 0x10 settled, 0x20 capped
    #   bit  6    unavailable, bit 7 stale
    #
    # Masking only bits 6-7 (as this did) folded the arrival bits into the base
    # mode: a settled current-mode board read as 0x11 = 17 instead of 1, and a
    # faulted-and-settled one as 0x12 = 18, so any `mode in (2, 3)` fault test
    # silently stopped matching the moment the firmware started setting arrival.
    # Mask each field explicitly.
    CC_BASE_MASK, CC_ARRIVAL_MASK = 0x0F, 0x30
    cc_mode = raw_mode & CC_BASE_MASK
    arrival_bits = raw_mode & CC_ARRIVAL_MASK
    # None, not "ramping", when the sample itself is not trustworthy: an
    # unavailable board's zero bits would otherwise read as a real "still
    # ramping" from a channel that answered nothing.
    unavailable = bool(raw_mode & CC_UNAVAILABLE)
    current_valid = not (raw_mode & CC_STALE) and not unavailable
    arrival = None if unavailable else {
        0x00: "ramping", 0x10: "settled", 0x20: "capped",
    }.get(arrival_bits, f"unknown:0x{arrival_bits:02X}")
    # bus_mV is None, NOT 0. The 0x3A response has no voltage field at all --
    # status, channel, muxPort, mode, measMa, targetMa, and nothing else (measured
    # on the wire 2026-09-16). A 0 here was a number this host invented: it reads
    # as a plausible measurement and never was one, which is the exact failure
    # this whole audit was about, sitting in the function that decodes the flags
    # for it. The docstring even said "bus_mV is unavailable here" while returning
    # a value for it. Want a voltage -> 0x24 (read_telemetry / ?live=1); that is
    # the only command that carries one.
    return {"index": fil, "present": (cc_mode != 0) and not unavailable,
            "arrival": arrival, "cc_mode_raw": int(raw_mode),
            "bus_mV": None,
            "current_mA": current_mA if current_valid else None,
            "target_mA": target_mA, "cc_mode": cc_mode,
            "unavailable": unavailable, "cached": True}


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
# Live download progress, polled by the GUI while /api/download blocks. Keyed by
# controller index → {phase, done, total}. Updated per frame by the worker thread.
_DL_PROGRESS: dict[int, dict] = {}


_DL_LOCK = threading.Lock()


# Per-controller cache of the per-filament currents (idle_mA, active_mA) that we
# LAST successfully downloaded, keyed by controller index -> {filament: (idle,act)}.
# The ~48 currents frames/controller dominate the download and rarely change, so a
# re-download skips any filament whose value matches. Invalidated whenever the
# firmware might have lost them: (re)connect (reflash clears the table) and mapping
# change (a filament's board/slot moves). Only updated on a FULLY successful
# download; a partial failure clears it so the next download re-sends everything.
_DL_CURRENTS_CACHE: dict[int, dict[int, tuple]] = {}


def invalidate_currents_cache(controller: int | None = None) -> None:
    with _DL_LOCK:
        if controller is None:
            _DL_CURRENTS_CACHE.clear()
        else:
            _DL_CURRENTS_CACHE.pop(controller, None)
    # The loaded-table hints go with it. A reconnect may be to a freshly
    # reflashed board whose table is empty, and a hint that outlives the table
    # it describes is exactly how a run skips a download it needed. Callers
    # re-confirm against the live CRC anyway; this keeps the hint from being
    # confidently wrong in the first place.
    for d in (LOADED_PLAN, LOADED_CRC, LOADED_EMIT_FIDS):
        if controller is None:
            d.clear()
        else:
            d.pop(controller, None)
    # Power-state beliefs too: a reconnect may be to a board that was reflashed
    # and is back at STOP, so keeping them would let the ACTIVE guard wave
    # through a filament it thinks is at IDLE. Unknown refuses; wrong does not.
    LAST_POWER_STATE.clear()


# Every name above, for `from ... import *` (underscore names included).
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
