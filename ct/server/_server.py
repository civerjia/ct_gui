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

# ===========================================================================
# CONSTANTS
#
# Gathered here rather than left beside their first use, the way
# ct_simple_control.py does it. They were spread over ~2000 lines, which made
# two things harder than locality was worth: checking whether a wire opcode is
# already defined, and seeing at a glance which values are firmware limits
# rather than choices this file gets to make.
#
# ONLY THE ASSIGNMENTS MOVED. Explanatory comments stayed with the code they
# head — the dead-filament rules, the ACTIVE ladder guard, the logging
# rationale, the pushed-telemetry note. Dragging those here would have gathered
# constants at the cost of leaving every one of those sections unlabelled.
#
# MUTABLE MODULE STATE IS DELIBERATELY ABSENT. CONTROLLERS, MAPPING,
# DEAD_FIDS, LOADED_PLAN, FILAMENT_ORDER, MASTER, the caches and the locks all
# stay where they were: they are rebound at runtime, some depend on classes
# defined further down, and moving them would change initialisation order. A
# constants section that quietly contained state would be worse than none.
# ===========================================================================

# ── Rig geometry and filament indexing ─────────────────────────────────────
# How many filaments there are, how they map onto power slots, and the
# default alternating-12 grouping. FILAMENT_COUNT is the GLOBAL index space
# (FID); a client's own USER_INDEX numbering is a separate thing it owns.


# ── Paths on disk ──────────────────────────────────────────────────────────
# All resolved against this file, never the working directory: the backend is
# started from wherever, and a relative path would scatter state across the
# filesystem depending on how it was launched.

# Directories: all under the repository root, defined once in ct/paths.py.
from ct.paths import CALIB_DIR, LOG_DIR, RECORD_DIR, RUN_REPORT_DIR, STATE_DIR  # noqa: E402
from ct.paths import WEB_DIR as STATIC_DIR  # noqa: E402

# The backend's subsystems (ct/server/_*.py), moved out of this file verbatim.
# What stays here is what reads a global reassigned at runtime -- see those modules.
from ._common import *  # noqa: F401,F403,E402
from ._wire import *  # noqa: F401,F403,E402
from ._access import *  # noqa: F401,F403,E402
from ._link import *  # noqa: F401,F403,E402
from ._logfiles import *  # noqa: F401,F403,E402
from ._mapping import *  # noqa: F401,F403,E402
from ._monitor import *  # noqa: F401,F403,E402
from ._schedule import *  # noqa: F401,F403,E402
from ._reads import *  # noqa: F401,F403,E402
from ._recording import *  # noqa: F401,F403,E402
from ._safety import *  # noqa: F401,F403,E402
from ._shared import *  # noqa: F401,F403,E402
# (RECORD_DIR: see ct/paths.py)

# ── Link timeouts ──────────────────────────────────────────────────────────


# ── Logging ────────────────────────────────────────────────────────────────


# ── Command frame pieces ───────────────────────────────────────────────────


# ── Wire opcodes — SHV schedule (0x70–0x82) ────────────────────────────────


# ── Wire opcodes — channel, HV and events ──────────────────────────────────


# ── Transfer sizing ────────────────────────────────────────────────────────
# Firmware limits, not tuning knobs: an oversized frame is dropped SILENTLY,
# so raising either of these makes a download stop part-way with nothing
# reporting an error.


# ── Firmware-pushed telemetry ──────────────────────────────────────────────


# ── Power states, and what the numbers mean ────────────────────────────────
# The ladder is STOP(1) → SLEEP(2) → STANDBY(3) → IDLE(4) → ACTIVE(5), with
# VOLTAGE(6) off to the side. The *_NAMES tables exist so a raw number never
# reaches a log line or an API reply on its own.
#
# The reasoning behind ENERGISING_STATES and the ACTIVE floor stays with the
# code that enforces them — see dead_fids()/ladder_blocks_active().


# ── HV setpoint scaling (DS3502 wipers) ────────────────────────────────────
# Full scale per rail and which DS3502 channel drives it. The focus rail's
# 495 V is not a typo for 1000: its divider clips well below nominal.


# ── TPS55289 OCP decode, and the diagnosis chip order ──────────────────────


# ── Lease and polling timing ───────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Command frame builder — matches RP2350bFilamentController/docs/power_state_and_cc.md
# (the firmware protocol is ahead of the WiFi GUI's net_protocol, so we build
# these payloads here rather than via build_command_payload).
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Host translation / planning layer — firmware ACTIVE-LIST model.
#
# The GUI works in logical/global filament index 0-95. The RP2350 firmware
# (simple_hv_schedule) addresses boards by POWER SLOT: each controller has 64
# fixed slots, slot k = channel (k>>3) / position (k&7). The host downloads a
# 64-byte ACTIVE-FILAMENT LIST per controller via ShvSetActiveList (0x70):
# byte[k] = the global filament 0-95 driven by power slot k (0xFF = unused).
# decode_(filament) reverse-maps; a filament not mapped on this controller is
# counted-but-not-fired (it belongs to the other controller). Emission entries
# carry the GLOBAL filament 0-95 directly; the heating deltas carry the LOCAL
# (channel, position) the host derives from the same mapping. Which channels are
# polled = the separate channel mask (ChSetI2CEnableMask 0x34).
#
# FilamentMapping is the host-owned, editable source of truth (default =
# alternating groups of `group_size`). See gui_operations / firmware
# simple_hv_schedule.h.
# ---------------------------------------------------------------------------


MAPPING = FilamentMapping()

# Host-side I²C POLL set: which of the 8 channels the boards matrix actually reads
# INA219 V/I from. The firmware ignores its own enable mask (it always scans all 8
# and only stores/echoes the value), so THIS host mask — set by the GUI's "channels
# enabled" control — is the real lever for what gets polled. Bit c = channel c.
# Default 0x3F = CH1-6 (matches the historical channels_used default).
SCAN_MASK = 0x3F


def _scan_channels() -> list:
    return [c for c in range(8) if SCAN_MASK & (1 << c)]


def filament_to_board(filament: int, channels=None):
    """logical 0-95 -> (controller{0,1}|None, channel, position, slot) via MAPPING
    (active-list model). `channels` is a legacy no-op kept for call compatibility."""
    b = MAPPING.board(int(filament))
    if b is None:
        return None, None, None, None
    c, ch, pos = b
    return c, ch, pos, ch * 8 + pos


                                 # channels in one frame). 0x13 HV_GET_ALL_BYTES is a
                                 # CACHE COPY -- never use it to confirm a read-back.


def run_chip_health(link: "ControllerLink") -> dict:
    """Presence scan (CH_GET_PRESENT 0x25) + channel mask. Counts read from the
    RAW response (robust), and a non-Ok status surfaces as present_error."""
    # Report the HOST poll set (SCAN_MASK) — that's what board_snapshot actually
    # reads, so it's what the "channels enabled" checkboxes should reflect. The
    # firmware's own mask (read_channel_mask) is inert and kept only for diagnostics.
    out: dict[str, Any] = {"channel_mask": SCAN_MASK, "fw_channel_mask": read_channel_mask(link)}
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
    s8 = lambda start: [int(raw[start + i]) & 0xFF if start + i < len(raw) else 0 for i in range(8)]
    out["present_hex"] = resp.get("payload_hex")
    out["present_counts"] = {
        "mux": pc(raw[9:17]), "tps": pc(raw[17:25]), "ina": pc(raw[25:33]),
        "enable_io": pc(raw[33:41]), "fault_io": pc(raw[41:49]),
        "iso_io": pc(raw[49:57]), "hv_io": pc(raw[57:65]),
    }
    # Raw per-channel masks (8 bytes each, bit N = mux port N) for the chip-health
    # table — field names match the wifi_gui renderer so the JS ports verbatim.
    out["present_masks"] = {
        "mux_present_mask": s8(9), "tps_present_mask": s8(17), "ina_present_mask": s8(25),
        "enable_io_present_mask": s8(33), "fault_io_present_mask": s8(41),
        "iso_io_present_mask": s8(49), "hv_io_present_mask": s8(57),
    }
    return out


def _read_bitmaps(link: "ControllerLink") -> dict | None:
    """CH_GET_BOARD_BITMAPS -> {(ch, mux): {field: value or None}}; None on failure."""
    # Scan channels only (CH1-6 by default): CH7/CH8 are never used.
    used = set(_scan_channels())
    mask = bytes(0xFF if c in used else 0 for c in range(8))
    resp = link.client.send_request(CH_GET_BOARD_BITMAPS, mask, timeout=3.0)
    if resp.get("status_code") != 0x00:
        return None
    raw = resp.get("raw") or []
    out: dict[tuple, dict] = {}

    def bit(start, ch, mux):
        return bool(raw[start + ch] & (1 << mux)) if len(raw) >= start + 8 else None
    for ch in range(8):
        for mux in range(8):
            row = {}
            for name, vstart, validstart in (("iso_enabled", 9, 41), ("tps_enabled", 17, 49),
                                             ("tps_fault", 25, 57), ("hv_overcurrent", 33, 65)):
                valid = bit(validstart, ch, mux)
                row[name] = bit(vstart, ch, mux) if valid else None
            out[(ch, mux)] = row
    return out


def _board_monitor_tick(cid: int, link: "ControllerLink", now: float, prev: dict) -> dict:
    snap = dict(prev) if prev else {"boards": {}, "boards_at": 0.0, "bitmaps": {},
                                    "bitmaps_at": 0.0, "status": None, "status_at": 0.0}
    hint = getattr(link, "arm_hint_at", 0.0)
    owns = (now - hint) < MONITOR_ARM_HINT_S or bool(snap.get("run_owns"))
    # Status: every tick while idle (cheap, no I2C), 1 Hz while a run owns it.
    if not owns or (now - snap["status_at"]) >= MONITOR_RUN_STATUS_S:
        try:
            st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
        except Exception:
            st = None
        if st is not None:
            snap["status"], snap["status_at"] = st, now
            owns = st.get("state") in (1, 2) or (now - hint) < MONITOR_ARM_HINT_S
    snap["run_owns"] = owns
    # Lost boards / dark channels: no I2C on the firmware side, so this one
    # runs during a schedule run too, at the same 1 Hz.
    if (now - snap.get("health_at", 0.0)) >= MONITOR_HEALTH_PERIOD_S:
        try:
            health = read_board_health(link, _scan_channels())
        except Exception:
            health = None
        if health is not None:
            snap["health"], snap["health_at"] = health, now
    if owns:
        # 1 Hz cache read (no I2C), then the push on top when it is on. No
        # bitmaps: that is I2C, and it belongs to the run.
        if (now - snap.get("run_cache_at", 0.0)) >= MONITOR_RUN_STATUS_S:
            snap["run_cache_at"] = now
            try:
                cache = read_board_cache(link, _scan_channels())
            except Exception:
                cache = None
            if cache is not None:
                snap["boards"], snap["boards_at"], snap["source"] = cache, now, "cache(run)"
        if cid in _LIVE_PUSH:
            pushed = _pushed_boards(link)
            if pushed:
                merged = dict(snap.get("boards") or {})
                for key, b in pushed.items():
                    old = merged.get(key)
                    # The push carries V/I only; keep the cache's TPS/state fields.
                    merged[key] = {**old, **{k: b[k] for k in ("present", "current_mA", "bus_mV")},
                                   "pushed": True} if old else b
                snap["boards"], snap["boards_at"], snap["source"] = merged, now, "cache+push(run)"
        return snap
    cache = read_board_cache(link, _scan_channels())
    if cache is not None:
        snap["boards"], snap["boards_at"], snap["source"] = cache, now, "cache"
    if (now - snap["bitmaps_at"]) >= MONITOR_BITMAP_PERIOD_S:
        try:
            bm = _read_bitmaps(link)
        except Exception:
            bm = None
        # Sticky per bit: a failed or invalid read keeps the last GOOD value for
        # MONITOR_BITMAP_HOLD_S, so one NAK on a noisy bus does not blink a dot.
        held = dict(snap.get("bitmaps") or {})
        for key, row in (bm or {}).items():
            cur = dict(held.get(key) or {})
            for name, v in row.items():
                if v is not None:
                    cur[name] = (v, now)
            held[key] = cur
        snap["bitmaps"], snap["bitmaps_at"] = held, now
    return snap


def _board_monitor_loop() -> None:
    while True:
        t0 = time.monotonic()
        for cid, link in list(CONTROLLERS.items()):
            try:
                if not link.client.connected:
                    with _BOARD_MON_LOCK:
                        _BOARD_MON.pop(cid, None)
                    continue
                with _BOARD_MON_LOCK:
                    prev = _BOARD_MON.get(cid)
                snap = _board_monitor_tick(cid, link, time.monotonic(), prev)
                with _BOARD_MON_LOCK:
                    _BOARD_MON[cid] = snap
            except Exception as exc:
                log.debug("board monitor controller %s: %s", cid, exc)
        time.sleep(max(0.02, MONITOR_PERIOD_S - (time.monotonic() - t0)))


def board_snapshot(link: "ControllerLink", controller: int, channels=DEFAULT_CHANNELS,
                   vi_only: bool = False, cached: bool = False) -> list:
    """64-board snapshot for the boards matrix: present/tps/ina presence (0x25),
    iso/tps enable + fault (0x26), and INA219 V/I (0x24). Returns 64 board dicts.

    vi_only=True does ONLY the INA219 read (V/I + presence) and SKIPS the two heavy
    bitmap round-trips — the values that actually change frame-to-frame. The GUI
    merges these into its cache and does a full snapshot only occasionally, so the
    matrix numbers stay live at ~1 Hz even while the single-client link is busy
    with user commands. (The enable/fault/mux bitmaps change rarely.)

    cached=True (implies vi_only) swaps that live 0x24 INA219 read for the
    zero-I2C 0x3A cached-currents read (read_cached_currents_by_board) —
    it costs the RP2350 nothing (no bus I/O at all, live or otherwise), so
    it's the mode to poll at high frequency (10 Hz) or while a schedule is
    firing. Trade-off: the CC-loop cache only has current, not bus_mV or a
    presence flag, so only current_mA is updated per board; bus_mV/present/
    ina_present carry over from the last (non-cached) snapshot — same
    "only the volatile field moves" pattern vi_only already uses for the
    bitmap fields."""
    boards = {}
    for ch in range(8):
        for mux in range(8):
            boards[(ch, mux)] = {
                "channel": ch, "mux_port": mux, "label": f"CH{ch + 1}.{mux + 1}",
                "present": False, "mux_present": False, "tps_present": False, "ina_present": False,
                "iso_enabled": False, "tps_enabled": False, "tps_fault": False,
                "hv_overcurrent": False, "bus_mV": 0, "current_mA": 0,
                # *_valid=False means that field's read failed (missing/faulty
                # chip, e.g. a board design with the HV-current chip removed) --
                # the corresponding value field is MEANINGLESS, not "confirmed
                # off". Only trust iso_enabled/etc. when its _valid twin is True.
                # See RP2350 uart_protocol.md 14.7 / firmware a0d71a3.
                "iso_enabled_valid": False, "tps_enabled_valid": False,
                "tps_fault_valid": False, "hv_overcurrent_valid": False,
                # False means this tick's current read didn't cover this board
                # (link timeout/contention, or a partial cached-read response) --
                # current_mA here is just the fresh dict's 0 default, NOT a
                # confirmed zero. The frontend must not treat it as real data;
                # see refreshBoards()'s merge, which only overwrites current_mA
                # when this is True (same "keep last value on no data" pattern
                # already used for bus_mV/present in the cached branch below).
                "current_mA_valid": False,
                # Same idea, for CH_GET_PRESENT: a failed/timed-out call on this
                # tick must not be allowed to blank the whole matrix's presence
                # (and with it the current_mA display, which both render
                # functions gate on `present`) -- the frontend keeps the last
                # known presence/bus_mV when this is False.
                "present_valid": False,
            }

    def apply_slice(raw, start, field):
        if len(raw) < start + 8:
            return
        for ch in range(8):
            for mux in range(8):
                if raw[start + ch] & (1 << mux):
                    boards[(ch, mux)][field] = True

    if not vi_only and not cached:
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
                for b in boards.values():
                    b["present_valid"] = True
        except Exception as exc:
            print(f"board_snapshot: CH_GET_PRESENT failed: {exc}")
        try:
            resp = link.client.send_request(CH_GET_BOARD_BITMAPS, bytes(ALL_BOARDS_MASK), timeout=3.0)
            if resp.get("status_code") == 0x00:
                raw = resp.get("raw") or []     # status, targeted[8], iso_en, tps_en, tps_fault,
                                                 # hv_oc, iso_valid, tps_valid, tps_fault_valid,
                                                 # hv_overcurrent_valid (firmware a0d71a3+)
                apply_slice(raw, 9, "iso_enabled")
                apply_slice(raw, 17, "tps_enabled")
                apply_slice(raw, 25, "tps_fault")
                apply_slice(raw, 33, "hv_overcurrent")
                # Validity masks are appended (older firmware's response is just
                # shorter here) -- apply_slice's len() guard already makes this a
                # no-op against pre-a0d71a3 firmware, so *_valid just stays False
                # (safe: the GUI treats that as "unknown", same as before this fix).
                apply_slice(raw, 41, "iso_enabled_valid")
                apply_slice(raw, 49, "tps_enabled_valid")
                apply_slice(raw, 57, "tps_fault_valid")
                apply_slice(raw, 65, "hv_overcurrent_valid")
            else:
                print(f"board_snapshot: CH_GET_BOARD_BITMAPS status_code={resp.get('status_code')!r}")
        except Exception as exc:
            print(f"board_snapshot: CH_GET_BOARD_BITMAPS failed: {exc}")
    # Keyed directly by (channel, mux) over the host SCAN_MASK — NOT the
    # filament map. So enabling a channel in the "channels enabled" control
    # surfaces that channel's boards (e.g. CH7) immediately, independent of
    # which filaments are mapped there.
    if cached:
        # No I2C at all — only current_mA is available from the CC-loop
        # cache; leave bus_mV/present/ina_present at their defaults above
        # (the GUI's own merge keeps its last live values for those, same
        # as it already does for the bitmap fields on a vi_only tick).
        try:
            for (ch, mux), v in read_cached_currents_by_board(link, _scan_channels()).items():
                if (ch, mux) in boards:
                    boards[(ch, mux)]["current_mA"] = v["current_mA"]
                    # 0x80 of mode = "measuredMilliAmps is NOT a live measurement"
                    # (port not in Current mode, or CC-loop re-armed and hasn't
                    # measured yet) -- RP2350 firmware 57c605b. Below that fix,
                    # this field had no defined value outside Current mode and
                    # we were rendering it as a real reading anyway (root cause
                    # of the "current flickers to 0" report -- see cross-session
                    # thread with rp2350bfilamentcontroller-39). 0x40 = the sample
                    # could not be produced at all; also not a reading. Keep this
                    # in step with _cached_entry()'s mask.
                    boards[(ch, mux)]["current_mA_valid"] = not (v.get("mode", 0) & 0xC0)
        except Exception:
            pass
    else:
        try:
            for (ch, mux), v in read_ina_by_board(link, _scan_channels()).items():
                if (ch, mux) in boards:
                    boards[(ch, mux)].update(bus_mV=v["bus_mV"], current_mA=v["current_mA"],
                                             ina_present=v["present"], present=v["present"],
                                             current_mA_valid=True)
        except Exception:
            pass
    return [boards[(ch, mux)] for ch in range(8) for mux in range(8)]


def read_telemetry(link: "ControllerLink", controller: int, channels=None) -> dict:
    """Per-FILAMENT telemetry for the ring view: read every populated board's INA219
    and map local (channel, mux) → global filament 0-95 via the active-list MAPPING.
    Filament-bound by design (the ring is indexed by filament)."""
    out: dict[int, dict] = {}
    for (ch, mux), v in read_ina_by_board(link, MAPPING.channels_used(controller)).items():
        fil = MAPPING.filament_for_board(controller, ch, mux)
        if fil is None:
            continue
        out[fil] = {"index": fil, **v}
    return out


def read_cached_telemetry(link: "ControllerLink", controller: int) -> dict:
    """Per-FILAMENT CC-loop cached currents mapped to global filament 0-95. The
    run-safe sibling of read_telemetry (no I2C). `present` is inferred from the CC
    mode (a board the loop is regulating is present); bus_mV is unavailable here.

    BULK path (paged, all used channels in one sweep). For exactly one filament
    use read_cached_telemetry_one -- different firmware command, far cheaper."""
    out: dict[int, dict] = {}
    for (ch, mux), v in read_cached_currents_by_board(link, MAPPING.channels_used(controller)).items():
        fil = MAPPING.filament_for_board(controller, ch, mux)
        if fil is None:
            continue
        out[fil] = _cached_entry(fil, v.get("mode", 0),
                                 v.get("current_mA", 0), v.get("target_mA", 0))
    return out


def _single_read_is_trusted(link: "ControllerLink", controller: int) -> bool:
    """True if this firmware's SINGLE-board 0x3A flags an unreadable board.

    Firmware 5bff25c made a failed CcCurrentSample default to stale|unavailable,
    which fixed the single-board branch; before it, the paged branch flagged a
    failed read (0xC0) while the single branch returned mode 0 / 0 mA -- a board
    that isn't there, reported as present-and-idle. VERIFIED on hardware
    2026-09-16: the flashed firmware has the paged fix but not the single one, so
    this is a live difference, not a theoretical one.

    That matters because wait_for_current() polls the single path: on such a
    firmware, stop_one(verify=True) against an absent board reports "reached
    0.0 mA" for a reading that never happened. So probe once per controller and
    fall back to the (correct, slower) paged read when the single path can't be
    trusted, rather than assuming either firmware.

    The probe: find a board the PAGED read flags unavailable and ask for it
    singly. If the single read calls it valid, the firmware lacks the fix. No
    such board (everything readable) => nothing to distinguish, assume trusted
    and re-probe later rather than caching a guess."""
    cached = _SINGLE_0X3A_TRUSTED.get(controller)
    if cached is not None:
        return cached
    try:
        paged = read_cached_telemetry(link, controller)
    except Exception:
        return True                      # can't probe now; don't cache
    probe = next((f for f, v in paged.items() if v.get("current_mA") is None), None)
    if probe is None:
        return True                      # nothing unreadable to probe with; don't cache
    ft, flags, payload = build_payload("CH_GET_CACHED_CURRENTS",
                                       {"channel": 0, "mux_port": 0})
    cid0, ch, mux, _ = filament_to_board(int(probe))
    ft, flags, payload = build_payload("CH_GET_CACHED_CURRENTS",
                                       {"channel": ch, "mux_port": mux})
    try:
        resp = link.request(ft, payload, flags=flags, timeout=1.5)
    except Exception:
        return True                      # don't cache a failed probe
    dec = resp.get("decoded") if isinstance(resp, dict) else None
    if not isinstance(dec, dict) or "mode" not in dec:
        return True
    trusted = bool(dec.get("mode", 0) & 0xC0)   # flagged it too => fix present
    _SINGLE_0X3A_TRUSTED[controller] = trusted
    if not trusted:
        print(f"[0x3A] controller {controller + 1}: firmware does NOT flag failed "
              f"single-board reads (pre-5bff25c) — routing single reads through the "
              f"paged read so an unreadable board can't report as 0 mA")
    return trusted


def read_cached_telemetry_one(link: "ControllerLink", controller: int, fil: int) -> dict:
    """SINGLE-board sibling of read_cached_telemetry: one filament, one small
    FLAG_SINGLE frame (0x3A single form) instead of the paged all-board sweep.

    Returns {fil: entry} (same shape as the bulk read, so callers can merge the
    two interchangeably) or {} if the filament has no board on this controller,
    or the read failed. Use for a single-filament poll (wait_for_current); NEVER
    loop it over many boards -- that's what the bulk read exists for."""
    cid0, ch, mux, _ = filament_to_board(int(fil))
    if cid0 != controller or ch is None:
        return {}
    if not _single_read_is_trusted(link, controller):
        # Old firmware: the single read cannot say "I could not read this", so use
        # the paged read, which can. Costs the round-trips this path exists to
        # avoid -- correctness first; the speed returns when the RP2350 is flashed.
        return {int(fil): v for f, v in read_cached_telemetry(link, controller).items()
                if f == int(fil)}
    ft, flags, payload = build_payload("CH_GET_CACHED_CURRENTS",
                                       {"channel": ch, "mux_port": mux})
    try:
        resp = link.request(ft, payload, flags=flags, timeout=1.5)
    except Exception:
        return {}
    dec = resp.get("decoded") if isinstance(resp, dict) else None
    if not isinstance(dec, dict) or "mode" not in dec:
        return {}
    return {int(fil): _cached_entry(int(fil), dec.get("mode", 0),
                                    dec.get("measured_mA", 0), dec.get("target_mA", 0))}


_PUSH_SAW_RUN = False                 # push tore-down only after a run actually started


def set_scan_telemetry(link: "ControllerLink", controller: int, enable: bool) -> None:
    """Enable/disable the firmware's CACHED telemetry PUSH for the high-fps live
    view. Enable => push every used-channel board's cached current every 50 ms
    (mode 2, no I2C). Disable => telemetry_mode 0 stops the push."""
    try:
        used = set(MAPPING.channels_used(controller))
    except Exception:
        used = set(range(6))
    mask = [0xFF if c in used else 0 for c in range(8)]
    if enable:
        body = {"event_enable_bits": EVENT_TELEMETRY_ENABLE_BIT,
                "telemetry_period_ms": SCAN_TELEMETRY_PERIOD_MS,
                "telemetry_mode": TELEMETRY_MODE_CACHED, "board_mask": mask}
    else:
        body = {"event_enable_bits": 0, "telemetry_period_ms": 0,
                "telemetry_mode": 0, "board_mask": mask}
    try:
        ft, flags, payload = build_command_payload("SET_EVENT_CONFIG", body)
        link.client.send_request(ft, payload, flags=flags, timeout=1.5)
    except Exception:
        pass
    global _PUSH_SAW_RUN
    if enable:
        _LIVE_PUSH.add(controller + 1)
        _PUSH_SAW_RUN = False
    else:
        _LIVE_PUSH.discard(controller + 1)


def read_pushed_telemetry(link: "ControllerLink", controller: int) -> dict:
    """Per-FILAMENT snapshot assembled from the firmware-PUSHED EVENT_TELEMETRY
    frames already RECEIVED (no request). Pages stream in order, so applying every
    received page's entries (oldest->newest) leaves the newest value per board.

    CAUTION: an EVENT_TELEMETRY entry is (channel, port, bus_mV, current_mA) with NO
    validity flag -- unlike the 0x3A reads, whose mode byte carries stale/unavailable
    (see _cached_entry). So the firmware cannot currently tell us a pushed value is
    bad: a failed read arrives as 0 mA, and the cached branch can push a STALE current
    as if it were live. Treat pushed currents as ADVISORY (they drive the 20 fps live
    view only) -- never verify a command against them; use wait_for_current, which
    goes through 0x3A and does get the flags.

    TELEMETRY_INVALID_MIN below is the agreed-on sentinel range (see cross-session thread with
    rp2350bfilamentcontroller-39): a per-entry flag byte would change the entry stride,
    and a version skew there misparses the WHOLE page instead of one value, so 0xFFFF
    was chosen instead. Guarding for it now is a no-op until/unless that firmware side
    lands -- it costs nothing and means we don't have to move both ends together."""
    # Sentinel RANGE, not a single value: 0xFFF0..0xFFFF all mean "no valid
    # reading", with the low bits carrying the REASON (firmware 78fabb7,
    # uart_protocol.md 16.4):
    #   0xFFFF board absent (probed, no answer)   0xFFFE measurement too old
    #   0xFFFD unavailable (channel not ready)    0xFFFC not measured by this mode
    # 0xFFFC is the one to be careful with if this ever stops collapsing them:
    # in CACHED mode bus_mV is 0xFFFC on EVERY entry, because the CC cache holds
    # a current and nothing else. Reading that as "absent" would report every
    # board missing throughout a scan. Accepting the whole range here
    # first is what makes that extension safe -- firmware only ever emits
    # 0xFFFF today, so this is a no-op now, but once it starts emitting the
    # distinct values an `== 0xFFFF` check would MISS them and render 65534 mA
    # as a real current. Widening before they narrow, never the reverse.
    # Anything in this range is ~65 A, ~20x past what these boards can draw.
    TELEMETRY_INVALID_MIN = 0xFFF0
    board: dict[tuple, tuple] = {}
    try:
        events = link.client.events()
    except Exception:
        events = []
    for ev in events:
        if ev.get("type") != "EVENT_TELEMETRY":
            continue
        dec = ev.get("decoded") or {}
        for e in dec.get("entries", []):
            board[(e.get("channel"), e.get("mux_port"))] = (e.get("current_mA", 0), e.get("bus_mV", 0))
    out: dict[int, dict] = {}
    for (ch, mux), (mA, mv) in board.items():
        fil = MAPPING.filament_for_board(controller, ch, mux)
        if fil is None:
            continue
        # present was hardcoded True here: "a board that appeared in a pushed frame
        # exists". It doesn't follow -- the firmware pages EVERY masked board, including
        # ones it failed to read, so an absent board was being reported present with a
        # 0 mA reading. Fall back to the same None-means-no-reading convention the 0x3A
        # path uses, so a consumer can't tell the two paths apart.
        #
        # CONFLATION, on purpose: the sentinel cannot separate "absent" from "present
        # but stale" -- this entry shape has no flag field, which is the whole reason
        # for the sentinel. So `present` here means "confirmed present THIS sample",
        # not "plugged in", and a live board with one stale sample does dip it. That
        # only moves the status-line count; state is no longer inferred from an
        # untrusted sample (app.js pollTelemetry passes state:null for a null
        # current), so it can't flicker a board to STOP the way it used to.
        # bus_mV is 0xFFFF for EVERY board in cached telemetry mode (the CC cache
        # holds no voltage), so it is None throughout a scan by design, not a fault.
        valid = mA < TELEMETRY_INVALID_MIN
        out[fil] = {"index": fil, "present": valid,
                    "bus_mV": mv if mv < TELEMETRY_INVALID_MIN else None,
                    "current_mA": mA if valid else None, "pushed": True}
    return out

                       # didn't help — the bottleneck is RP2350 per-frame service
                       # latency, not frame count (it processes a bigger frame
                       # proportionally slower while busy with I2C).


def shv_op(link: "ControllerLink", body: dict) -> dict:
    """Dispatch one Simple-HV-schedule operation on a controller (ShV panel)."""
    op = body.get("op")
    if op == "push_active_list":
        # push THIS controller's mapping (64-byte power->filament) to the board
        controller = int(body.get("controller", 1)) - 1
        return {"ok": _status_ok(link.request(SHV_SET_ACTIVE_LIST, MAPPING.active_list(controller)))}
    if op == "get_active_list":
        raw = link.request(SHV_GET_ACTIVE_LIST, b"").get("raw") or []
        if raw and raw[0] == 0 and len(raw) >= 1 + POWER_SLOTS:
            lst = list(raw[1:1 + POWER_SLOTS])
            return {"ok": True, "list": lst, "mapped": sum(1 for x in lst if x != NO_FILAMENT)}
        return {"ok": False}
    if op == "clear_table":
        return {"ok": _status_ok(link.request(SHV_CLEAR_TABLE, b""))}
    if op == "set_entries":
        entries = body.get("entries") or []
        ent = bytearray()
        resolved = []
        for e in entries:
            if "filament" in e:
                fil = int(e["filament"]) & 0xFF
            else:
                # Board-coord entry: resolve (controller, channel, position) → global filament
                ctrl = int(e.get("controller", 1)) - 1
                slot = int(e.get("channel", 0)) * 8 + int(e.get("position", 0))
                fil = MAPPING._board_to_fil.get((ctrl, slot), NO_FILAMENT)
            resolved.append(fil)
            ent += bytes([fil & 0xFF, int(e["numPulses"]) & 0xFF]) + _u16(int(e["width"]))
        ok = True
        for start in range(0, len(entries), SHV_EMIT_CHUNK):
            cnt = min(SHV_EMIT_CHUNK, len(entries) - start)
            payload = _u16(start) + bytes([cnt]) + bytes(ent[start * 4:(start + cnt) * 4])
            ok = _status_ok(link.request(SHV_SET_ENTRIES, payload)) and ok
        return {"ok": ok, "count": len(entries), "resolved": resolved}
    if op == "table_info":
        raw = link.request(SHV_GET_TABLE_INFO, b"").get("raw") or []
        if raw and raw[0] == 0 and len(raw) >= 7:
            return {"ok": True, "entryCount": _le(raw, 1, 2), "crc": _le(raw, 3, 4)}
        return {"ok": False}
    if op == "heat_info":
        raw = link.request(SHV_HEAT_GET_INFO, b"").get("raw") or []
        if raw and raw[0] == 0 and len(raw) >= 5:
            return {"ok": True, "heatCount": _le(raw, 1, 2), "maxHeatEntries": _le(raw, 3, 2)}
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
        # Two boards on one schedule must agree on the trigger delay, or the
        # master frames the other board's pulses in the wrong place. Checked at
        # arm, not only at set, because an RP2350 reset zeroes it in between.
        why = trigger_delay_mismatch()
        if why:
            return {"ok": False, "error": f"arm refused: {why}",
                    "trigger_delay_mismatch": True}
        # A run in progress is active control of both the filaments and the
        # rails -- that is what a run IS -- so arming renews both timers.
        safety_touch_hv()
        safety_touch_filaments(list(LAST_POWER_STATE.keys()))
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
                if off + 16 > len(raw):
                    break
                # The record is 12 bytes and only 10 were being decoded; the
                # last two hold the 165 READ-BACK, which is what makes a flagged
                # pulse judgeable at all.
                #
                # flags bits (RP2350 simple_hv_schedule.cpp, pioTick_ verify):
                #   0x01 ON read-back != the commanded byte
                #   0x02 OFF read-back was NON-ZERO -- THE HV DID NOT TURN OFF.
                #        This is the dangerous one and the only bit here that is
                #        about the pulse rather than about the verification.
                #   0x04 a read-back was UNAVAILABLE -- the pulse fired but the
                #        firmware has no evidence either way. Not a failure, and
                #        the firmware's own mismatch counter deliberately skips
                #        it.
                #
                # 0xEE is a SENTINEL the firmware writes when the ON read-back is
                # unavailable, not a bus sample -- its bits mean nothing. It only
                # ever appears with 0x04. Surfaced as None so it cannot be read
                # as a value, same reasoning as the 0xFFF0 telemetry sentinels.
                fl = raw[off + 1]
                rb = _le(raw, off + 10, 2)
                # HEATING SNAPSHOT, taken by the firmware at the instant the
                # pulse fired (record grew 12 -> 16 bytes). This is the one
                # thing a host poll could never supply: the ACTIVE window is a
                # few triggers wide, and sampling it hard enough to align
                # perturbs the ramp being sampled.
                #
                # Sentinels, decoded to None with a REASON rather than passed
                # through as numbers -- 0 is a legal current and must never
                # stand for "unknown":
                #   0xFFFF  board did not answer / not present
                #   0xFFFD  no live sample: never measured, or older than the
                #           firmware's 6 s freshness limit
                #   0xFFFC  target only: board is not current-regulated, so
                #           there is no heating setpoint
                def _heat(v):
                    if v < 0xFFF0:
                        return v, None
                    return None, {0xFFFF: "no_answer", 0xFFFD: "no_live_sample",
                                  0xFFFC: "not_current_mode"}.get(v, f"sentinel_0x{v:04X}")
                meas, meas_why = _heat(_le(raw, off + 12, 2))
                tgt, tgt_why = _heat(_le(raw, off + 14, 2))
                recs.append({"filament": raw[off], "flags": fl, "seq": _le(raw, off + 2, 2),
                             "tOnUs": _le(raw, off + 4, 4), "durationUs": _le(raw, off + 8, 2),
                             "read165": None if (fl & 0x04 and (rb & 0xFF) == 0xEE) else rb,
                             "on_mismatch": bool(fl & 0x01),
                             "hv_stuck_on": bool(fl & 0x02),
                             "unverified": bool(fl & 0x04),
                             "heat_meas_mA": meas, "heat_meas_unavailable": meas_why,
                             "heat_target_mA": tgt, "heat_target_unavailable": tgt_why})
                off += 16
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
    if op == "fault_policy":
        # GET or SET the per-run fault policy (0=stop, 1=continue).
        # Two independent policies: board (CC/OCP fault) and mismatch (HC165 read-back).
        # Request body fields: board (optional int), mismatch (optional int).
        # Response: {ok, board, mismatch, mismatchCount, faultedSlots (list of slot ints)}
        payload = b""
        if "board" in body:
            payload = bytes([int(body["board"]) & 0x01])
        if "mismatch" in body:
            payload = (payload or bytes([0])) + bytes([int(body["mismatch"]) & 0x01])
        raw = link.request(SHV_FAULT_POLICY, payload).get("raw") or []
        # response: [status, board, mismatch, mismatch_count(4 LE), slots(8 LE)] = 15 bytes
        if raw and raw[0] == 0 and len(raw) >= 15:
            slots64 = _le(raw, 7, 8)
            faulted = [i for i in range(64) if (slots64 >> i) & 1]
            controller = int(body.get("controller", 1)) - 1
            faulted_filaments = [f for s in faulted
                                 for f in (MAPPING._board_to_fil.get((controller, s)),) if f is not None]
            return {"ok": True, "board": raw[1], "mismatch": raw[2],
                    "mismatchCount": _le(raw, 3, 4), "faultedSlots": faulted,
                    "faultedFilaments": faulted_filaments}
        return {"ok": False}
    if op == "trigger_delay":
        # GET or SET the SyncIn->fire trigger delay (µs), ALWAYS on every
        # connected controller -- the `controller` in the request is ignored on
        # purpose; see trigger_delay_all(). "applies" is False when the live
        # fire path can't honour the delay, so a set can't silently do nothing.
        return trigger_delay_all(int(body["delay_us"]) if "delay_us" in body else None)
    return {"ok": False, "error": "unknown op"}


# fid -> {"reason": str, "by": str, "at": iso8601}. Provenance is not decoration:
# an entry that can never expire and blocks energising needs to say why, or in
# three months nobody knows why 55 is off and nobody dares clear it.
DEAD_FIDS: dict[int, dict] = {}


def _dead_load() -> None:
    global DEAD_FIDS
    try:
        raw = json.loads(DEAD_STATE_PATH.read_text())
        entries = raw.get("dead") if isinstance(raw, dict) else None
        if isinstance(entries, dict):
            DEAD_FIDS = {int(k): dict(v) for k, v in entries.items()
                         if 0 <= int(k) < FILAMENT_COUNT}
            log.info("dead filaments: loaded %d from %s", len(DEAD_FIDS), DEAD_STATE_PATH.name)
    except FileNotFoundError:
        pass
    except Exception as e:
        # Do NOT start with an empty mask on a parse error: that would silently
        # re-enable every filament someone disabled. Refuse to start instead.
        raise SystemExit(f"dead filament state at {DEAD_STATE_PATH} is unreadable "
                         f"({e}). Fix or move the file; refusing to start with an "
                         f"empty mask, which would re-enable disabled filaments.")


def _dead_save() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DEAD_STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"dead": {str(k): v for k, v in sorted(DEAD_FIDS.items())}}, indent=2))
    tmp.replace(DEAD_STATE_PATH)   # atomic: a crash mid-write must not truncate the mask


# PowerState values that put power ON the filament, so the ones a dead filament
# must be refused. STOP(1)/SLEEP(2) leave the output off; STANDBY(3) enables it
# at the firmware's 0.8 V floor, so it is NOT a no-power state and is included.
#
# STOP and SLEEP are deliberately ALWAYS allowed, even for a dead filament:
# refusing them would make it impossible to turn a faulty filament OFF, which
# inverts the whole point. Enforcement blocks energising, never de-energising.

# ── The ACTIVE ladder guard ──────────────────────────────────────────────────
# Jumping straight to ACTIVE (firing current) is NOT ALLOWED: it damages the
# filament, and a filament that fails inside the vacuum cannot be repaired.
# The ladder is STOP -> SLEEP -> STANDBY -> IDLE -> (settle) -> ACTIVE.
#
# Measured here 2026-09-17 on ch1.8, commanding ACTIVE 2900 mA from cold: 740 mV
# / 2067 mA of inrush, then the output COLLAPSED to 0 mV / 0 mA for ~10 s before
# the firmware's guardian revived it. That is the hazard, visible on a simulated
# load; on a real filament it is not something to find out empirically.
#
# Enforced here rather than left to callers for the same reason as the dead mask:
# the client that filters is not the only client. A bare curl or the GUI would
# otherwise put full firing current on a cold filament and this backend would
# carry it out.
# For messages. A refusal that says "currently at power state 3" makes the
# reader go look up what 3 is, which is the same bare-integer problem the SHV
# arm reject codes had -- and this one is on the path people hit while trying to
# heat a filament, so it should read without a lookup.
# RP2350 UartStatusCode, for turning a bare status byte into something a
# caller can act on. Unknown codes print as the number rather than a guess.


# ── filament order (USER_INDEX -> FID), held HERE so it outlives a script ────
# A client-side LENS, not something this backend applies: every endpoint here
# speaks FID and keeps doing so. It lives in the backend only so a second script
# sees the same numbering the first one set, instead of every run silently
# starting at identity.
#
# DELIBERATELY NOT PERSISTED TO DISK, unlike DEAD_FIDS. Those two look similar
# and must not be treated alike:
#   - dead is a property of the HARDWARE. A burnt filament is still burnt after
#     a restart, so forgetting it would be unsafe.
#   - the order is a property of a SESSION's convention. Reloading a stale
#     permutation from disk into a rig whose backplane has since been rewired
#     sends every command to the wrong filament, and nothing about that failure
#     announces itself -- every index stays in range and every call succeeds.
# Identity is the only honest default for a backend that just started, so a
# restart forgets, on purpose.
FILAMENT_ORDER: list[int] | None = None       # None = identity
ORDER_SET_BY: str = ""
ORDER_SET_AT: float = 0.0


def order_snapshot() -> dict:
    with _ORDER_LOCK:
        order = list(FILAMENT_ORDER) if FILAMENT_ORDER else list(range(FILAMENT_COUNT))
        return {"ok": True, "order": order, "identity": FILAMENT_ORDER is None,
                "epoch": ORDER_EPOCH, "set_by": ORDER_SET_BY,
                "set_at_s_ago": (round(time.monotonic() - ORDER_SET_AT, 1)
                                 if ORDER_SET_AT else None)}


# ── ACTIVE current floor ─────────────────────────────────────────────────────
# ACTIVE below the IDLE operating current is refused. The firmware clamps IDLE
# at 2 A but deliberately does NOT clamp ACTIVE, so this is the only guard on
# that direction, and the RP2350 side asked for it to live here.
#
# 1500 mA is the IDLE operating current this bench runs at, per the user. It is
# not a constant read out of the firmware -- there is none -- so if the idle
# operating point changes, change this with it.

# Schedules may not select Voltage mode: ShvHeatSetEntries rejects state 6 with
# BadArgument and refuses the whole batch. Caught here first so the caller is
# told WHICH entry is wrong instead of getting a batch-level reject. Direct
# board control (CH_SET_POWER_STATE via /api/cmd) may still use Voltage -- it is
# a bench/calibration mode, and only SCHEDULES are restricted.


_SAFETY_TOUCH_HV: float = 0.0


def safety_touch_hv(when: float | None = None) -> None:
    """Renew the dead-man timer for the HV grid MOSFETs. COMMANDS ONLY."""
    global _SAFETY_TOUCH_HV
    with _SAFETY_LOCK:
        _SAFETY_TOUCH_HV = when if when is not None else time.monotonic()


def note_grid_commanded(on: bool, fids=None, clear_all: bool = False) -> None:
    """Record grid MOSFETs commanded closed/open, and renew the timer.

    on=True: `fids` may now be closed. on=False: `fids` are open. clear_all:
    every MOSFET was cleared (SHV_DISARM)."""
    with _SAFETY_LOCK:
        if clear_all:
            _GRID_CLOSED.clear()
        elif on:
            _GRID_CLOSED.update(int(f) for f in fids or ())
        else:
            _GRID_CLOSED.difference_update(int(f) for f in fids or ())
    safety_touch_hv()


def safety_snapshot() -> dict:
    now = time.monotonic()
    with _SAFETY_LOCK:
        cfg = dict(_SAFETY)
        hv_age = (now - _SAFETY_TOUCH_HV) if _SAFETY_TOUCH_HV else None
        grid_closed = sorted(_GRID_CLOSED)
        events = list(_SAFETY_EVENTS[-20:])
        touch = dict(_SAFETY_TOUCH_FIL)
    active = {}
    for fid, (st, _when) in list(LAST_POWER_STATE.items()):
        if st != POWER_STATE_ACTIVE:
            continue
        t = touch.get(int(fid))
        # No touch recorded is NOT "just renewed" -- it means this backend has
        # never seen a command for it (e.g. it restarted while the filament was
        # already hot), which is precisely when the watchdog matters most.
        active[int(fid)] = {"idle_for_s": round(now - t, 1) if t else None,
                            "never_commanded": t is None}
    # The number stays for anything that was already reading it; the name goes
    # beside it so nothing downstream has to own a copy of the table.
    cfg["active_fallback_name"] = power_state_name(cfg["active_fallback"])
    return {"ok": True, **cfg,
            "active_filaments": active,
            "grid_closed": grid_closed,
            "hv_idle_for_s": round(hv_age, 1) if hv_age is not None else None,
            "events": events}


def _safety_fallback_filaments(fids: list[int], state: int) -> None:
    """Walk these filaments back, grouped per controller like every other
    batch write here. Best effort and logged either way: a watchdog that
    raises is a watchdog that stops watching."""
    by_ctrl: dict[int, list[int]] = {}
    for f in fids:
        c0 = filament_to_board(f)[0]
        if c0 is not None:
            by_ctrl.setdefault(int(c0), []).append(int(f))
    for c0, group in by_ctrl.items():
        link = CONTROLLERS.get(c0 + 1)
        if not link or not link.client.connected:
            _safety_record("fallback_unreachable",
                           {"filaments": group, "controller": c0 + 1,
                            "error": "controller not connected — could not walk "
                                     "these back"})
            continue
        try:
            r = prep_filaments(link, c0, int(state), group, default_arg=0)
            _safety_record("fallback", {"filaments": group, "state": int(state),
                                        "applied": r.get("applied"),
                                        "failed": r.get("failed")})
        except Exception as exc:
            _safety_record("fallback_error", {"filaments": group, "error": str(exc)})


def _safety_loop() -> None:
    # Assigned below when a timer fires, so it has to be declared -- without
    # this the whole tick raises UnboundLocalError and the watchdog logs an
    # exception every second while watching nothing.
    global _SAFETY_TOUCH_HV
    # Latches so a held-off or unconfirmable state is recorded once, not every
    # tick -- an event log that repeats the same line 60 times a minute is one
    # nobody reads.
    _safety_held_for_run = False
    _safety_unknown_run = False
    while True:
        time.sleep(SAFETY_TICK_S)
        try:
            now = time.monotonic()
            with _SAFETY_LOCK:
                if not _SAFETY["enabled"]:
                    continue
                a_to = float(_SAFETY["active_timeout_s"])
                fallback = int(_SAFETY["active_fallback"])
                hv_to = float(_SAFETY["hv_timeout_s"])
                touch = dict(_SAFETY_TOUCH_FIL)
                hv_last = _SAFETY_TOUCH_HV
                hv_on = bool(_GRID_CLOSED)
            stale = []
            for fid, (st, _w) in list(LAST_POWER_STATE.items()):
                if st != POWER_STATE_ACTIVE:
                    continue
                t = touch.get(int(fid))
                if t is None or (now - t) > a_to:
                    stale.append(int(fid))
            hv_stale = hv_on and (hv_last == 0.0 or (now - hv_last) > hv_to)
            if stale or hv_stale:
                running, unknown = _safety_schedule_running()
                if running:
                    # Renew both and say nothing: a run is the hardware being
                    # driven on purpose, and it ends on its own (the firmware
                    # enforces the plan's own totalMs).
                    with _SAFETY_LOCK:
                        for f in stale:
                            _SAFETY_TOUCH_FIL[f] = now
                        _SAFETY_TOUCH_HV = now
                    if not _safety_held_for_run:
                        _safety_record("held_for_run",
                                       {"filaments": stale, "hv": bool(hv_stale),
                                        "note": "a schedule is running — a run is "
                                                "active control, so the timers are "
                                                "renewed rather than fired"})
                        _safety_held_for_run = True
                    continue
                if unknown is not None:
                    # Could not confirm no run is in progress. Do NOT act
                    # blind: writing power states into a live run is the worse
                    # of the two failures, and a link too sick to answer this
                    # is a link the fallback could not be written over anyway.
                    # Recorded once, retried next tick.
                    if not _safety_unknown_run:
                        _safety_record("deferred_unknown_run",
                                       {"error": unknown,
                                        "note": "cannot confirm no schedule is "
                                                "running — not touching the "
                                                "hardware until it answers"})
                        _safety_unknown_run = True
                    continue
                _safety_held_for_run = False
                _safety_unknown_run = False
            if stale:
                # Clear the touch first so a controller that cannot be reached
                # does not make this fire again every tick.
                with _SAFETY_LOCK:
                    for f in stale:
                        _SAFETY_TOUCH_FIL[f] = now
                _safety_fallback_filaments(stale, fallback)
            if hv_stale:
                with _SAFETY_LOCK:
                    _SAFETY_TOUCH_HV = now
                _safety_open_grid()
        except Exception as exc:          # never let the watchdog die
            log.exception("safety-watchdog tick failed: %s", exc)


def dead_fids() -> set[int]:
    with _DEAD_LOCK:
        return set(DEAD_FIDS)


def split_dead(fids) -> tuple[list[int], list[int]]:
    """Partition an iterable of FIDs into (alive, dead), preserving order."""
    d = dead_fids()
    alive, dead = [], []
    for f in fids:
        (dead if int(f) in d else alive).append(int(f))
    return alive, dead


_dead_load()


def _hv_lut_path(chan: str) -> Path:
    """Stable per-master, per-channel HV wiper→voltage LUT file. The HV/DS3502
    board lives on the MASTER controller, so the LUT is keyed by master id +
    channel; changing the master selects a different LUT."""
    ch = "focus" if str(chan).startswith("f") else "emission"
    return CALIB_DIR / f"hv_lut_p{MASTER}_{ch}.json"


# Hardware full-scale: DS3502 wiper 127 → these output levels.


def _lut_wiper_for_v(chan: str, mag_v: float) -> tuple[int, float, str]:
    """Interpolate target magnitude → (wiper, expect_v_signed, method).

    Reads the calibrated LUT from disk when available; falls back to a linear
    approximation using the hardware full-scale constant.  Same algorithm as
    the GUI's lutWiperForV() / lutSetV() in tests.js.
    """
    fp = _hv_lut_path(chan)
    lut = None
    if fp.is_file():
        try:
            lut = json.loads(fp.read_text())
        except (ValueError, OSError):
            lut = None
    if lut and isinstance(lut.get("points"), list) and len(lut["points"]) >= 2:
        pts_raw = sorted(
            [{"w": int(p["wiper"]), "m": abs(float(p["v"]))} for p in lut["points"]],
            key=lambda p: p["w"],
        )
        mono: list[dict] = []
        max_seen = -1.0
        for p in pts_raw:
            if p["m"] >= max_seen:
                mono.append(p)
                max_seen = p["m"]
        sign = -1.0 if float(lut["points"][0]["v"]) < 0 else 1.0
        T = abs(mag_v)
        if T < mono[0]["m"]:
            return mono[0]["w"], sign * mono[0]["m"], "lut(clamped)"
        if T == mono[0]["m"]:
            return mono[0]["w"], sign * mono[0]["m"], "lut"
        if T > mono[-1]["m"]:
            return mono[-1]["w"], sign * mono[-1]["m"], "lut(clamped)"
        for i in range(len(mono) - 1):
            a, b = mono[i], mono[i + 1]
            if a["m"] <= T <= b["m"]:
                f = 0.0 if b["m"] == a["m"] else (T - a["m"]) / (b["m"] - a["m"])
                w = round(a["w"] + f * (b["w"] - a["w"]))
                return w, sign * T, "lut"
    full = _HV_FULL_V.get(chan, 350.0)
    w = max(0, min(127, round(abs(mag_v) / full * 127)))
    # expect_v from the wiper actually written, not the request -- they differ
    # when the request was out of range.
    return w, -(w / 127 * full), ("linear(no-lut,clamped)" if abs(mag_v) > full
                                  else "linear(no-lut)")


# The MASTER controller carries the STM32 HV board; all STM32/ADC commands route
# to it regardless of the per-target selection. Default Power 1; set via /api/master.
MASTER = 1

STAGED_SCHEDULE: list = []  # last schedule uploaded from the GUI

# ---------------------------------------------------------------------------
# Shared access — several programs, one bridge
# ---------------------------------------------------------------------------
# The ESP32 bridge is SINGLE-CLIENT: tcp_bridge.cpp accepts one socket on :3333
# and hard-rejects every other connect, so only one process can ever own a
# controller. This backend is that process — it owns both sockets, serializes
# every frame on the link's request lock and routes responses by seq — so any
# number of programs can share the hardware by speaking HTTP to this API
# instead of grabbing :3333 for themselves (that is why the server binds all
# interfaces and answers CORS: see main() and _json()).
#
# Reads are always free. Writes interleave frame-by-frame and are free too. A
# program that needs a stretch of UNINTERRUPTED time (a schedule download, a
# calibration sweep, an armed run) takes the cooperative LEASE below: while it
# is held only the holder may write, everyone else gets 409 + who holds it.
# The lease always expires on its own, so a client that crashes mid-run can
# never wedge the bench.


# ── Bulk TPS status (CH_GET_TPS_STATUS, mask form) ──────────────────────────

def read_tps_status(link: "ControllerLink", cid: int) -> dict:
    """One bulk CH_GET_TPS_STATUS on one controller, unpacked per filament.

    Returns {"ok", "filaments": {fid: {"present", "en", "fault", "struggling",
    "oe"}}, "oe_supported"}. Only boards the firmware actually READ appear
    (its valid mask): a board missing here was not read, which is not the same
    as "all clear". `oe` is None where the MODE register was not read, and for
    every board on firmware older than the OE read (57-byte reply) -- absent,
    never False, because False would claim the output is off.

    Byte layout: status@0, targeted@1, present@9, en@17, fault@25, hv@33,
    valid@41, struggling@49, oe@57, oe_valid@65 (8 bytes each)."""
    ft, flags, payload = build_payload("CH_GET_TPS_STATUS", {"board_mask": [0xFF] * 8})
    # 8 s, not the 2 s /api/tps-struggling uses: this reads every board (probe,
    # EN, fault, MODE) and was measured at 139 ms on controller 1 but 0.4-1.5 s
    # -- once 13.6 s, just after connecting -- on controller 2's jitterier link.
    # Only a verify calls it, never a poll, so a long wait blocks nothing.
    resp = link.client.send_request(ft, payload, flags=flags, timeout=TPS_STATUS_TIMEOUT_S)
    raw = resp.get("raw") if isinstance(resp, dict) else None
    if not raw or raw[0] != 0 or len(raw) < 49:
        return {"ok": False, "error": f"bad CH_GET_TPS_STATUS reply ({len(raw or [])} bytes)",
                "filaments": {}}
    def bit(off, ch, pos):
        return off + 8 <= len(raw) and bool(raw[off + ch] & (1 << pos))
    has_valid = len(raw) >= 49
    has_struggling = len(raw) >= 57
    has_oe = len(raw) >= 73
    out = {}
    for ch in range(8):
        for pos in range(8):
            if not bit(1, ch, pos):                       # not targeted
                continue
            if has_valid and not bit(41, ch, pos):        # not read
                continue
            f = MAPPING.filament_for_board(cid - 1, ch, pos)
            if f is None:
                continue
            out[int(f)] = {
                "present": bit(9, ch, pos), "en": bit(17, ch, pos),
                "fault": bit(25, ch, pos),
                "struggling": bit(49, ch, pos) if has_struggling else None,
                "oe": (bit(57, ch, pos) if (has_oe and bit(65, ch, pos)) else None),
            }
    return {"ok": True, "filaments": out, "oe_supported": has_oe}


# ── Audit log: one line per state-changing request ─────────────────────────
# backend.log used to record almost nothing a person did: 84 shots, a MOSFET
# test and a switch test on 2026-09-23 left 14 lines, none of them a command.
# Every POST that CHANGES something now leaves one line -- who, what, and
# whether it worked -- including writes the lease refused. Reads that happen to
# be POSTs, and the renew heartbeats, are skipped: logged, they would bury the
# commands exactly as the per-retry reconnect warnings did.


# ---------------------------------------------------------------------------
# Measurement recorder. Records the STM32 per-pulse measurements (polled from
# /pulse_events) to rec_<ts>_pulses.csv for the whole session. start() arms
# the STM32 detector directly (adc_pulse_arm, same as the GUI's Per-pulse
# "Stream" button) rather than the ADC ring — the ring puts the STM32 in
# SPI-shot mode (sink=1), which stops it emitting per-pulse events at all
# (sync_on_falling/rising_edge only calls pulse_detector_edge_rise/fall in
# detector mode, sink=0), so a ring-based recorder would silently write an
# empty pulse .csv. There is no longer a raw-ADC-waveform (.bin) recording
# mode for the same reason.
# ---------------------------------------------------------------------------


def _run_scan_sim(host: str, count: int, interval_ms: float, controller: int) -> None:
    # running/count/stop already claimed by the caller under _SIM_LOCK (atomic start).
    # HARDWARE-timed trigger train: ONE call arms the ESP32 esp_timer, which clocks
    # every pulse at the real rate. The host does NOT pace pulses — it only polls
    # progress. (The old HTTP-per-pulse loop ran ~4.6x too slow: 47 ms/pulse instead
    # of the real ~10 ms, which stretched the whole scan and tripped TotalTimeout.)
    rate_hz = int(round(1000.0 / interval_ms)) if interval_ms > 0 else 1000
    if rate_hz < 1:
        rate_hz = 1
    try:
        r = sync_post_burst(host, count, rate_hz, timeout=3.0)
        if not r.get("ok"):
            return
        time.sleep(0.3)   # let the train start before the first status poll
        while True:
            with _SIM_LOCK:
                if _SIM_STATE["stop"]:
                    try:
                        sync_post_burst_stop(host, timeout=1.5)
                    except Exception:
                        pass
                    break
            st = {}
            try:
                st = sync_get_burst_status(host, timeout=1.5)
            except Exception:
                pass
            with _SIM_LOCK:
                _SIM_STATE["fired"] = int(st.get("fired", _SIM_STATE.get("fired", 0)))
            if not st.get("running", False):
                break   # train finished (all pulses fired) or was stopped
            time.sleep(0.3)
    finally:
        with _SIM_LOCK:
            _SIM_STATE["running"] = False
        # Stop the telemetry push and finalize the power-state report.
        for c2, l2 in CONTROLLERS.items():
            if c2 in _LIVE_PUSH and l2.client.connected:
                set_scan_telemetry(l2, c2 - 1, False)
        RUN_RECORDER.stop()


def download_to_controller(link: "ControllerLink", controller: int, plan: dict,
                           channels=None) -> dict:
    # PIPELINED download: build the whole ordered frame list, then fire it with a
    # sliding window so the ~per-frame round-trip latencies overlap instead of
    # serializing. Order only matters within emit/heat (CLEAR must precede its
    # SET_ENTRIES); the firmware processes the UART stream in order, so one ordered
    # pipeline is safe. A non-OK / timed-out frame is counted as a failure and the
    # overall ok is False — the GUI's Verify (CRC) is the backstop.
    t_all = time.monotonic()
    reqs: list = []      # (frame_type, payload, flags) in send order
    labels: list = []
    cur_fil: list = []   # filament index for each req slot (None for non-current frames)

    reqs.append((SHV_SET_ACTIVE_LIST, MAPPING.active_list(controller), 0)); labels.append("active_list"); cur_fil.append(None)
    reqs.append((CH_SET_I2C_ENABLE_MASK, bytes([MAPPING.channel_mask(controller) & 0xFF]), 0)); labels.append("mask"); cur_fil.append(None)

    # Per-filament currents — the ~48 frames/controller that DOMINATE the download.
    # Skip any whose (idle, active) matches what we last downloaded to this controller
    # (cache invalidated on connect / mapping change). `want` is the full intended set
    # so the cache can be refreshed to firmware truth on a successful download.
    with _DL_LOCK:
        cached = dict(_DL_CURRENTS_CACHE.get(controller, {}))
    want: dict[int, tuple] = {}
    cur_n = 0
    cur_skipped = 0
    for fil, cur in (plan.get("currents") or {}).items():
        f = int(fil)
        ctrl, ch, pos, _ = filament_to_board(f)
        if ctrl != controller:
            continue
        val = (int(cur.get("idle_mA", 0)), int(cur.get("active_mA", 0)))
        want[f] = val
        if cached.get(f) == val:      # firmware already has this value → skip the frame
            cur_skipped += 1
            continue
        cur_n += 1
        reqs.append((CH_FILAMENT_CURRENTS,
                     bytes([ch, pos]) + _u16(val[0]) + _u16(val[1]),
                     FLAG_SINGLE))
        labels.append("currents"); cur_fil.append(f)

    cfg = plan.get("config") or {}
    reqs.append((SHV_SET_CONFIG,
                 _u32(int(cfg.get("interPulseMs", 3000))) + _u16(int(cfg.get("maxOnMs", 40)))
                 + _u32(int(cfg.get("totalMs", 60000))) + bytes([int(cfg.get("triggerEdge", 0)) & 0xFF]), 0))
    labels.append("config"); cur_fil.append(None)

    # emission table — full global list (entry carries global filament 0-95)
    reqs.append((SHV_CLEAR_TABLE, b"", 0)); labels.append("emit_clear"); cur_fil.append(None)
    emit = plan.get("emission") or []
    # Dead filaments never make it into the table. Filtered (not rejected) to
    # match every other batch path, but NAMED in the result: a schedule is a
    # committed artifact and an entry quietly vanishing from it is how you end
    # up believing a filament was scanned when it never fired.
    emit_dead = sorted({int(e["filament"]) for e in emit if int(e["filament"]) in dead_fids()})
    if emit_dead:
        emit = [e for e in emit if int(e["filament"]) not in dead_fids()]
        log.warning("download_to_controller: dropped dead filaments %s from the "
                    "emission table (controller=%d)", emit_dead, controller)
    ent = bytearray()
    for e in emit:
        ent += bytes([int(e["filament"]) & 0xFF, int(e["numPulses"]) & 0xFF]) + _u16(int(e["widthUs"]))
    n = len(emit)
    emit_frames = 0
    for start in range(0, n, SHV_EMIT_CHUNK):
        count = min(SHV_EMIT_CHUNK, n - start)
        emit_frames += 1
        reqs.append((SHV_SET_ENTRIES, _u16(start) + bytes([count]) + bytes(ent[start * 4:(start + count) * 4]), 0))
        labels.append("emit"); cur_fil.append(None)

    # heating deltas — only THIS controller's filaments (local ch, pos)
    reqs.append((SHV_HEAT_CLEAR, b"", 0)); labels.append("heat_clear"); cur_fil.append(None)
    # Dead filaments out of the HEATING table too, not just the emission table.
    # Dropping them from emission alone means a dead filament never gets HV but
    # the schedule still drives it to ACTIVE mid-run -- and "must not be
    # energised" is the whole definition of dead. The asymmetry was invisible
    # because the emission filter is the one you notice.
    heat_dead = sorted({int(h["filament"]) for h in (plan.get("heating") or [])
                        if int(h["filament"]) in dead_fids()})
    if heat_dead:
        log.warning("download_to_controller: dropped dead filaments %s from the "
                    "heating table (controller=%d)", heat_dead, controller)
    heat = [h for h in (plan.get("heating") or [])
            if int(h["filament"]) not in dead_fids()
            and filament_to_board(int(h["filament"]))[0] == controller]
    heat.sort(key=lambda h: int(h["triggerIndex"]))
    hent = bytearray()
    for h in heat:
        _, ch, pos, _ = filament_to_board(int(h["filament"]))
        hent += (_u16(int(h["triggerIndex"])) + bytes([ch, pos, int(h["state"]) & 0xFF, 0])
                 + _u16(int(h.get("milliamps", 0))))
    hn = len(heat)
    for start in range(0, hn, SHV_HEAT_CHUNK):
        count = min(SHV_HEAT_CHUNK, hn - start)
        reqs.append((SHV_HEAT_SET_ENTRIES, _u16(start) + bytes([count]) + bytes(hent[start * 8:(start + count) * 8]), 0))
        labels.append("heat"); cur_fil.append(None)

    total = len(reqs)

    def _on_prog(d):
        with _DL_LOCK:
            _DL_PROGRESS[controller] = {"phase": "pipelined", "done": d, "total": total}

    _on_prog(0)
    # Pause the background PING for this link while we own the bridge exclusively.
    # The PING competes for _request_lock every 1 s and stalls download frames for
    # up to 0.6 s each time it fires — pausing eliminates that dead time.
    link.set_poll_paused(True)
    # Pipeline depth: a deep window (8) overwhelms the ESP32 bridge's buffering on a
    # slow/variable link — frames execute on the RP2350 but their ACKs come back after
    # the per-frame deadline, so the pipeline false-fails them and serial-retries at 3 s
    # each (minutes). A shallower window lets the bridge return ACKs in time; a longer
    # per-frame timeout tolerates a late-but-valid ACK. Env-tunable to measure.
    _win = max(1, int(os.environ.get("CT_DL_WINDOW", "8")))
    _tmo = max(0.5, float(os.environ.get("CT_DL_TIMEOUT", "2.5")))
    if os.environ.get("CT_DL_PIPELINE"):
        # Legacy 8-in-flight pipeline. Measured ~50x SLOWER than serial on this bridge
        # (responses stall to ~1.2 s/frame vs ~20 ms serial). Kept behind an env flag only.
        results = _pipeline_reliable(link, reqs, window=_win, timeout=_tmo, on_progress=_on_prog)
    else:
        # Serial send: each frame round-trips in ~20 ms (with the interrupt RX ring the
        # RP2350 answers immediately), so a full ~14 KB schedule downloads in ~2 s and is
        # reliable (Verify/CRC confirms). One retry per frame covers a rare transient.
        results = []
        for i, (ft, payload, flags) in enumerate(reqs):
            r = None
            for _try in range(2):
                try:
                    r = link.client.send_request(ft, payload, flags=flags, timeout=3.0)
                    if _status_ok(r):
                        break
                except Exception as exc:
                    r = {"ok": False, "error": str(exc)}
            results.append(r)
            _on_prog(i + 1)
    oks = [_status_ok(r) if isinstance(r, dict) else False for r in results]
    fails = [labels[i] for i, x in enumerate(oks) if not x]
    ok = not fails

    # Refresh the currents cache to firmware truth. On full success the firmware
    # holds `want` (sent + skipped). On partial failure, only evict the filaments
    # whose current frames actually failed — the rest are still good in firmware.
    failed_fils = {cur_fil[i] for i, x in enumerate(oks) if not x and cur_fil[i] is not None}
    with _DL_LOCK:
        merged = dict(_DL_CURRENTS_CACHE.get(controller, {}))
        merged.update(want)                          # promote sent+skipped to cache truth
        for f in failed_fils:
            merged.pop(f, None)                      # evict only the frames that failed
        _DL_CURRENTS_CACHE[controller] = merged

    link.set_poll_paused(False)
    total_ms = round((time.monotonic() - t_all) * 1000)
    with _DL_LOCK:
        _DL_PROGRESS[controller] = {"phase": "done", "done": total, "total": total}
    per_frame = total_ms / max(1, total)
    print(f"[download] P{controller + 1}: {total_ms} ms, {total} frames "
          f"({per_frame:.0f} ms/frame) | currents {cur_n}f (+{cur_skipped} cached) · emit {emit_frames}f · heat {hn // SHV_HEAT_CHUNK + 1}f"
          + (f" · {len(fails)} FAILED: {fails[:6]}" if fails else ""), flush=True)

    # Record what is now in this controller's table, so /api/arm can re-check it
    # against the dead mask as it stands AT ARM TIME, not at download time.
    # Only on success: a failed download leaves the table in an unknown state, and
    # claiming to know its contents would be worse than admitting we do not.
    if ok:
        LOADED_EMIT_FIDS[controller] = {int(e["filament"]) for e in emit}
        LOADED_PLAN[controller] = copy.deepcopy(plan)
    else:
        LOADED_EMIT_FIDS.pop(controller, None)
        LOADED_PLAN.pop(controller, None)
    # The CRC belongs to the table we just wrote, and we have not read it yet --
    # drop any previous one rather than let a stale CRC vouch for new content.
    LOADED_CRC.pop(controller, None)
    out = {"controller": controller, "ok": ok, "emit": n, "heat": hn,
           "frames": total, "curSent": cur_n, "curCached": cur_skipped,
           "fails": len(fails), "failLabels": fails[:12],
           "timing": {"total": total_ms}}
    dropped = sorted(set(emit_dead) | set(heat_dead))
    if dropped:
        out["dead_skipped"] = dropped
    return out


# ---------------------------------------------------------------------------
# CT-scan prep ladder. Walk every filament a controller owns down a rest state
# (Stop→Sleep→Standby→Idle), then pre-heat the schedule's cold-start band to
# Active. ChSetPowerState (0x35) is single-board, so this loops the controller's
# populated boards. `state` is the PowerState (1..6); arg = mA for Idle/Active.
# ---------------------------------------------------------------------------
def prep_filaments(link: "ControllerLink", controller: int, state: int,
                   filaments=None, currents=None, default_arg: int = 0,
                   channels=DEFAULT_CHANNELS) -> dict:
    """Apply CH_SET_POWER_STATE to a set of this controller's filaments. `filaments`
    is a FID 0-95 list (only this controller's are touched); None = every
    populated board the controller owns. `currents` maps FID→mA for the
    Idle/Active arg (falls back to default_arg).

    Dead filaments are dropped here and returned in "dead_skipped". This is the
    enforcement point for the whole batch path BECAUSE it is where `None`
    expands to every populated board -- filtering in the endpoint instead would
    cover an explicit list and silently miss the broadcast case, which is the
    one that touches everything.
    """
    currents = currents or {}
    if filaments is None:
        fils = MAPPING.filaments(controller)
        not_this_controller: list[int] = []
    else:
        requested = [int(f) for f in filaments]
        fils = [f for f in requested if filament_to_board(f)[0] == controller]
        # Filaments this call was asked for but that MAPPING says belong to a
        # DIFFERENT controller (or no controller at all) -- expected/normal
        # when the caller is broadcasting one filaments= list across every
        # connected controller (the top-level /api/filament-prep handler does
        # exactly that), so this alone isn't an error. It only becomes one if
        # NO controller ends up claiming a requested filament -- the top-level
        # handler reconciles that across all `results`, see "excluded" there.
        not_this_controller = [f for f in requested if f not in fils]
    # Dead filaments out, AFTER the None expansion above so the broadcast case
    # is covered too. Only for states that energise: a dead filament must still
    # be STOPpable (see ENERGISING_STATES).
    dead_skipped: list[int] = []
    if state in ENERGISING_STATES:
        fils, dead_skipped = split_dead(fils)
        if dead_skipped:
            log.warning("prep_filaments: refused to energise dead filaments %s "
                        "(state=%d, controller=%d)", dead_skipped, state, controller)
    # DEAD_SLEEP_IS_STOP. SLEEP is not heating, but it is not off either: it
    # turns the board's isolated 12 V rail and the TPS EN pin ON. So it is not
    # in ENERGISING_STATES and a dead filament used to pass -- sleep_all(), and
    # the dead-man watchdog's ACTIVE->SLEEP fallback, powered the rail of every
    # filament the operator had marked "do not use". Skipping it is no better:
    # a dead filament left at ACTIVE would stay there. STOP is the one state
    # that is both lower and fully off, so a dead filament asked for SLEEP gets
    # STOP, and is named in dead_stopped.
    dead_stopped: dict = {}
    if int(state) == POWER_STATE_SLEEP:
        fils, dead_sleep = split_dead(fils)
        if dead_sleep:
            dead_stopped = prep_filaments(link, controller, int(POWER_STATE_STOP),
                                          dead_sleep, channels=channels)
    # ACTIVE only from IDLE — see the ladder guard. Filtered, not rejected, so a
    # batch ladder that legitimately walks most boards up is not blocked by one
    # straggler; the blocked ones are NAMED so it cannot pass silently.
    # A LIST of FIDs, like dead_skipped and failed -- not a dict keyed by a
    # stringified index. JSON turns dict keys into strings, and every other
    # filament-list field here is a plain int list that the client re-keys from
    # FID to its own numbering; a dict would have skipped that translation and
    # reported FIDs to a script using a swap. Reasons go alongside, for humans.
    ladder_blocked: list[int] = []
    ladder_reasons: dict[str, str] = {}
    # ACTIVE floor applies to the batch path too. `currents` can carry a
    # per-filament override, so check the value each filament would actually
    # get, not just the batch default -- otherwise one override slips under it.
    if int(state) == POWER_STATE_ACTIVE:
        under = []
        for f in fils:
            ma = int(currents.get(f, currents.get(str(f), default_arg)) or 0)
            if ma < ACTIVE_FLOOR_MA:
                under.append(int(f))
                ladder_reasons[str(int(f))] = (f"ACTIVE {ma} mA is below the "
                                               f"{ACTIVE_FLOOR_MA} mA floor")
        if under:
            log.warning("prep_filaments: refused ACTIVE below %d mA for %s",
                        ACTIVE_FLOOR_MA, under)
            ladder_blocked.extend(under)
            fils = [f for f in fils if int(f) not in set(under)]
    # IDLE ceiling, the mirror of the floor above and filtered the same way.
    # Refused rather than passed through because the RP2350 CLAMPS this one
    # silently (kIdleMaxMilliamps): the batch would report these filaments as
    # applied, they would run at IDLE_CEILING_MA, and any wait for the current
    # that was asked for would never finish with nothing saying why. Per
    # filament, since `currents` can carry an override that the batch default
    # does not show.
    if int(state) == POWER_STATE_IDLE:
        over = []
        for f in fils:
            ma = int(currents.get(f, currents.get(str(f), default_arg)) or 0)
            if ma > IDLE_CEILING_MA:
                over.append(int(f))
                ladder_reasons[str(int(f))] = (
                    f"IDLE {ma} mA is above the {IDLE_CEILING_MA} mA ceiling — "
                    f"the RP2350 would clamp it silently and report success")
        if over:
            log.warning("prep_filaments: refused IDLE above %d mA for %s",
                        IDLE_CEILING_MA, over)
            ladder_blocked.extend(over)
            fils = [f for f in fils if int(f) not in set(over)]
    if int(state) == POWER_STATE_ACTIVE:
        # ONE paged bulk 0x3A for every board, not a per-filament loop -- see
        # read_cached_telemetry's docstring.
        arrivals: dict[int, str | None] = {}
        arrivals_known = False
        try:
            bulk = read_cached_telemetry(link, controller) or {}
            arrivals = {int(k): (v or {}).get("arrival") for k, v in bulk.items()}
            arrivals_known = bool(bulk)
        except Exception:
            pass
        allowed = []
        for f in fils:
            why = ladder_blocks_active(
                f, arrivals.get(int(f)), arrivals_known and int(f) in arrivals)
            if why is None:
                allowed.append(f)
            else:
                ladder_blocked.append(int(f))
                ladder_reasons[str(int(f))] = why
        fils = allowed
        if ladder_blocked:
            log.warning("prep_filaments: refused ACTIVE for %s (not at IDLE) "
                        "controller=%d", ladder_blocked, controller)
    # BATCHED, not per-filament. A per-filament flood (~48 CH_SET_POWER_STATE frames
    # back-to-back) overwhelmed the RP2350's UART+I2C and tripped its 2 s watchdog
    # ("Stop all" -> RP2350 reset). Instead send ONE MASKED frame per (channel,
    # current) group: an 8-bit TCA9554 mask for that channel. STOP/SLEEP write the
    # channel's iso/EN registers once for the whole mask (setPowerStateMasked), and
    # each frame touches <=8 boards so no single command blocks the loop long enough
    # to reset. Grouped by current so Idle/Active with per-filament targets still
    # batch per channel where the target matches. Response layout (per handler):
    # [status, mask[8], applied[8], failed[8], state, ...] -> applied[] at raw[9:17].
    # unslotted: filament_to_board() matched this controller but has no channel
    # (past slot 63, or a skipped channel) -- a real, reportable exclusion, not
    # a "belongs elsewhere" case like not_this_controller above.
    unslotted: list[int] = []
    if not fils:
        # ok only if nothing was refused: every filament blocked by a guard is
        # a request that did NOT happen, not an empty success.
        return _with_dead_stopped(
            {"controller": controller, "ok": not ladder_blocked, "applied": 0, "failed": [],
             "state": int(state), "touched": [], "not_this_controller": not_this_controller,
             "unslotted": unslotted, "dead_skipped": dead_skipped,
             "ladder_blocked": ladder_blocked, "ladder_reasons": ladder_reasons},
            dead_stopped)
    # One frame per argument (current, or mV for VOLTAGE), covering every
    # channel: the firmware runs a multi-channel STANDBY/IDLE/ACTIVE/VOLTAGE
    # mask on all eight I2C buses concurrently (RP2350 setPowerStateBroadcast),
    # so a frame per channel would serialise exactly what it parallelises.
    # STOP/SLEEP are expander writes, batched per channel in the firmware.
    groups: dict = {}   # arg -> bytearray(8) channel masks
    members: dict = {}  # arg -> [filament]
    for f in fils:
        _, ch, pos, _ = filament_to_board(f)
        if ch is None:            # unslotted/overflow filament (past slot 63, or a
            unslotted.append(f)   # skipped channel) -> has no power slot to address
            continue
        v = currents.get(str(f), currents.get(f, default_arg))
        arg = int(v if v is not None else default_arg)   # tolerate an explicit null
        m = groups.setdefault(arg, bytearray(8))
        m[ch] = (m[ch] | (1 << pos)) & 0xFF
        members.setdefault(arg, []).append(f)
    reqs, keys = [], []
    for arg, m in groups.items():
        reqs.append((CH_SET_POWER_STATE, bytes(m) + bytes([int(state) & 0xFF]) + _u16(arg), 0))
        keys.append(arg)
    # Few frames (<=8, one per channel) and each is ~instant on the RP2350, so send
    # them serially via the reliable single-request path. _pipeline_reliable's
    # pipeline phase was spending ~5 s PER frame here (a 0.2 s op became 30 s) --
    # its per-frame deadlines mistime against the RP2350's interleaved heartbeat/
    # telemetry stream. The single-request path drains those cleanly.
    results = []
    for ft, payload, flags in reqs:
        try:
            results.append(link.client.send_request(ft, payload, flags=flags, timeout=3.0))
        except Exception as exc:
            results.append({"ok": False, "error": str(exc)})
    applied, failed = 0, []
    landed: list[int] = []
    for key, r in zip(keys, results):
        raw = r.get("raw") if isinstance(r, dict) else None
        for f in members[key]:
            _, fch, fpos, _ = filament_to_board(f)
            appl = raw[9 + fch] if (raw and len(raw) >= 9 + fch + 1) else 0
            if appl & (1 << fpos):
                applied += 1
                landed.append(int(f))
            else:
                failed.append(int(f))
    # Record only what the firmware CONFIRMED it applied. Recording the intent
    # would let a failed write leave the backend believing a filament is at
    # IDLE, which is exactly the belief the ACTIVE guard depends on.
    note_power_state(landed, state)
    # A filament the guards refused (ladder_blocked) was asked for and not
    # done, the same as a failed one. It used to leave ok True: an
    # idle_all(default_ma=2500) refused every filament at the 2000 mA ceiling
    # and came back ok:true, applied 0 -- a request that did nothing, reported
    # as done.
    return _with_dead_stopped(
        {"controller": controller,
         "ok": not failed and not unslotted and not ladder_blocked, "applied": applied,
         "total": len(fils), "failed": failed, "state": int(state),
         "touched": fils, "not_this_controller": not_this_controller,
         "unslotted": unslotted, "dead_skipped": dead_skipped,
         "ladder_blocked": ladder_blocked, "ladder_reasons": ladder_reasons},
        dead_stopped)


def hv_grid_set(link: "ControllerLink", controller: int, filaments,
                on: bool, force: bool) -> dict:
    """Set the ISO HV-grid switch bit for a batch of filaments (this
    controller's only). `filaments`=None -> every populated board.

    Reads the current per-channel desired byte first so untouched bits in
    the same channel byte are preserved, then writes back via
    HvSetMultiChannel (0x15) — one frame per touched channel, not one per
    filament. `force`=True uses writeMode=2 (bypasses firmware fault/verify
    checks) — same semantics as the GUI's Force checkbox."""
    # Exclusion bookkeeping, mirroring prep_filaments: a requested filament that
    # never reaches the wire must be NAMED, not dropped. This path used to return
    # ok:True having sent nothing -- so hv_grid_set(f, on=False) on an unmapped or
    # offline filament reported success while the grid switch stayed ON. On an HV
    # path a success-shaped no-op is the dangerous direction.
    not_this_controller: list[int] = []
    if filaments is None:
        fils = MAPPING.filaments(controller)
    else:
        fils = []
        for f in filaments:
            f = int(f)
            if filament_to_board(f)[0] == controller:
                fils.append(f)
            else:
                not_this_controller.append(f)
    # Dead filaments out before any bit is set -- but only when turning the grid
    # ON. Turning it OFF must always be allowed: refusing that would leave a
    # faulty filament's HV switch closed with no way to open it, which is the
    # exact opposite of what marking it dead is for. Same rule as
    # ENERGISING_STATES on the power-state path.
    dead_skipped: list[int] = []
    if on:
        fils, dead_skipped = split_dead(fils)
        if dead_skipped:
            log.warning("hv_grid_set: refused to route HV to dead filaments %s "
                        "(controller=%d)", dead_skipped, controller)
    by_ch: dict[int, list[int]] = {}
    unslotted: list[int] = []
    touched: list[int] = []
    for f in fils:
        _, ch, pos, _ = filament_to_board(f)
        if ch is None:                    # unslotted/overflow -> no board to address
            unslotted.append(f)
            continue
        by_ch.setdefault(ch, []).append(pos)
        touched.append(f)
    if not by_ch:
        return {"controller": controller, "ok": not unslotted, "applied": [], "failed": [],
                "touched": [], "not_this_controller": not_this_controller,
                "unslotted": unslotted, "dead_skipped": dead_skipped}
    ft, flags, payload = build_payload("HV_GET_ALL_BYTES", {})
    cur = link.client.send_request(ft, payload, flags=flags, timeout=2.0)
    cur_raw = cur.get("raw") if isinstance(cur, dict) else None
    desired = list(cur_raw[1:9]) if (cur_raw and len(cur_raw) >= 9) else [0] * 8
    values = list(desired)
    chmask = 0
    for ch, positions in by_ch.items():
        byte = desired[ch]
        for pos in positions:
            byte = (byte | (1 << pos)) if on else (byte & ~(1 << pos) & 0xFF)
        values[ch] = byte
        chmask |= (1 << ch)
    ft, flags, payload = build_payload("HV_SET_MULTI_CHANNEL", {
        "channel_mask": chmask, "values": values, "force": force,
    })
    resp = link.client.send_request(ft, payload, flags=flags, timeout=3.0)
    raw = resp.get("raw") if isinstance(resp, dict) else None
    # Response: status, appliedMask, verifiedMask, failedMask, desired[8], feedback[8]
    # (firmware handleHvSetMultiChannel_). appliedMask is per-CHANNEL, so on its own
    # it can only say "this channel's byte was written" -- it cannot tell you whether
    # YOUR bit within that byte actually landed. feedback[] is the 74HC165 READ-BACK,
    # i.e. what is physically on the hardware now, and it is per-bit. Comparing the
    # intended bit against feedback is the strongest confirmation this interface
    # offers and is stronger than any status byte -- especially with force=True,
    # which bypasses the firmware's own verify step entirely.
    applied_mask = raw[1] if raw and len(raw) >= 2 else 0
    feedback = list(raw[12:20]) if raw and len(raw) >= 20 else None
    applied, failed, suspect = [], [], []
    for f in touched:
        _, ch, pos, _ = filament_to_board(f)
        if not (applied_mask & (1 << ch)):
            failed.append(f)
            continue
        if feedback is not None and bool(feedback[ch] & (1 << pos)) != bool(on):
            suspect.append(f)      # disagrees on THIS read -- confirm before failing it
            continue
        applied.append(f)

    # CONFIRM BEFORE ACCUSING. With force=True (writeMode>=2, the GUI's default)
    # the firmware returns a SINGLE UNCONFIRMED 165 sample: hvSetChannelByte does
    # one hvFeedback_.readChannel() and returns immediately, skipping the mismatch
    # re-read that writeMode 0/1 get. HvController::verify()'s confirm-and-reread
    # (and its rereads/unstable counters) is a DIFFERENT path this traffic never
    # enters -- so those counters stay flat here and prove nothing.
    #
    # The 165 read is the weak link on this hardware (one shared MISO through a
    # 151, long cable, first bit sampled with no clock edge of its own), so a lone
    # disagreement is more likely a bad SAMPLE than a bit that didn't land.
    # Re-read once and only fail bits that disagree BOTH times; a bit that flips
    # between the two reads is reported as `unstable` -- that is the marginal
    # read-back signal, and it is not the same claim as "the write failed".
    # The confirming read MUST be 0x14 HV_REFRESH_FEEDBACK, never 0x13
    # HV_GET_ALL_BYTES. 0x13 does not touch the hardware -- hvGetAllBytes() just
    # copies hvState_.channels[].feedback out of the cache, so re-reading with it
    # compares a value against ITSELF: `unstable` could never fire and every
    # suspicion would be "confirmed" on the strength of nothing. That failure is
    # invisible in testing (clean runs stay clean; it only shows up as
    # over-confident mismatches you have no reason to distrust). 0x14 with
    # mask=0xFF really re-reads all 8 channels and returns them in one frame.
    mismatched, unstable = [], []
    if suspect:
        fb2 = None
        # 0x14 ALSO silently returns cache while the PIO owns the shift pins
        # (hvRefreshFeedback: a bit-bang read would move S0/S1/S2 out from under
        # the PIO and break the pulses -- correct, but undetectable in the
        # response). During an armed/running schedule the "fresh" read is
        # therefore the same no-op as 0x13, so don't attempt it: an unconfirmable
        # suspicion is UNKNOWN, not proven-failed.
        try:
            st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
            pio_busy = bool(st) and st.get("state") == 2
        except Exception:
            pio_busy = True          # can't establish it's safe -> assume it isn't
        if not pio_busy:
            try:
                again = link.request(HV_REFRESH_FEEDBACK, bytes([0xFF]), flags=0, timeout=2.0)
                araw = again.get("raw") if isinstance(again, dict) else None
                if araw and len(araw) >= 9 and araw[0] == 0x00:
                    fb2 = list(araw[1:9])
            except Exception:
                fb2 = None
        for f in suspect:
            _, ch, pos, _ = filament_to_board(f)
            if fb2 is None:
                # Could not obtain a trustworthy second read. Report UNKNOWN
                # rather than asserting the write failed -- claiming a failure we
                # did not establish is the same over-claim as claiming success.
                unstable.append(f)
            elif bool(fb2[ch] & (1 << pos)) != bool(on):
                mismatched.append(f)          # both reads agree: the bit did not land
            else:
                unstable.append(f)            # reads disagree with each other
    # `unstable` counts against ok: the bit's state is UNKNOWN, and "unknown"
    # must not read as "did what you asked" -- that is the same failure-as-a-
    # legal-value mistake this whole audit was about, and an HV grid bit is the
    # last place to make it. It stays a SEPARATE field from mismatched/failed so
    # a caller can tell "we could not confirm" from "it definitely did not land"
    # and decide its own tolerance.
    out = {"controller": controller,
           "ok": not failed and not mismatched and not unstable and not unslotted,
           "applied": applied, "failed": failed,
           "touched": touched, "not_this_controller": not_this_controller,
           "unslotted": unslotted}
    if mismatched:
        out["mismatched"] = mismatched
        out["error"] = (f"grid read-back disagrees for {mismatched} on BOTH reads — the "
                        f"channel write was accepted but the 165 feedback does not show "
                        f"the requested state")
    if unstable:
        # Deliberately does NOT fail the call: two reads that disagree with each
        # other say the READ is marginal, not that the write didn't land. Claiming
        # a failure here would be the same "value where an absence belongs" mistake
        # in the opposite direction.
        out["unstable"] = unstable
        out["warning"] = (f"165 read-back unstable for {unstable} — two reads disagreed "
                          f"with each other; treat the state of these bits as unknown, "
                          f"not as failed")
    if feedback is None:
        # Don't silently claim verification we didn't do.
        out["verified"] = False
    if dead_skipped:
        out["dead_skipped"] = dead_skipped
    return out


def set_ocp_threshold_batch(link: "ControllerLink", controller: int, filaments,
                            threshold_ma: int) -> dict:
    """Set the per-board TPS55289 IOUT_LIMIT (steady-state OCP threshold) for
    a batch of filaments. CH_SET_TPS_OCP_THRESHOLD (0x28) is single-board
    only in firmware — no masked/batch form exists like CH_SET_POWER_STATE
    — so this loops one frame per filament, same pattern as prep_filaments.
    `filaments`=None -> every populated board this controller owns."""
    # Same exclusion bookkeeping as prep_filaments / hv_grid_set. This matters
    # more here than almost anywhere else: OCP is a PROTECTION setting, so a
    # silently-skipped board means a script believes it lowered a trip point on
    # a board it never addressed.
    not_this_controller: list[int] = []
    if filaments is None:
        fils = MAPPING.filaments(controller)
    else:
        fils = []
        for f in filaments:
            f = int(f)
            if filament_to_board(f)[0] == controller:
                fils.append(f)
            else:
                not_this_controller.append(f)
    applied, failed, unslotted, touched = [], [], [], []
    for f in fils:
        _, ch, pos, _ = filament_to_board(f)
        if ch is None:
            unslotted.append(f)
            continue
        touched.append(f)
        ft, flags, payload = build_payload("CH_SET_TPS_OCP_THRESHOLD", {
            "channel": ch, "mux_port": pos, "threshold_mA": threshold_ma,
        })
        try:
            resp = link.client.send_request(ft, payload, flags=flags, timeout=2.0)
            (applied if _status_ok(resp) else failed).append(f)
        except Exception:
            failed.append(f)
    return {"controller": controller, "ok": not failed and not unslotted,
            "applied": applied, "failed": failed, "touched": touched,
            "not_this_controller": not_this_controller, "unslotted": unslotted}


class CtHandler(BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass

    # CORS preflight — other programs (and pages served from another origin)
    # call this API directly; the bridge itself is already open on the LAN.
    def do_OPTIONS(self) -> None:
        self.send_response(HTTPStatus.NO_CONTENT)
        self._cors_headers()
        self.send_header("Content-Length", "0")
        self.end_headers()

    # --- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path.startswith("/api/"):
            _note_client(self._client(), self.client_address[0], path)
        if path == "/api/geometry":
            self._json(GEOMETRY)
        elif path == "/api/scan":
            # Discover ESP32 bridges on the LAN: do_scan() probes every host with
            # TCP :3333 open, and for each hit also checks whether the RP2350
            # controller behind it answers a protocol frame. No hardware write.
            self._json({"results": do_scan()})
        elif path == "/api/lock":
            # Who (if anyone) currently holds the exclusive-write lease.
            self._json({"ok": True, "lock": _lease_snapshot(), "you": self._client()})
        elif path == "/api/loaded-schedule":
            # What the backend believes is in each controller's table, so a new
            # script run can decide whether it needs to download. A HINT: the
            # caller still confirms against the live CRC before trusting it.
            self._json({"ok": True, "space": "fid",
                        "loaded": {str(cid + 1): {"plan": LOADED_PLAN.get(cid),
                                                  "crc": LOADED_CRC.get(cid),
                                                  "emit_fids": sorted(LOADED_EMIT_FIDS.get(cid, ()))}
                                   for cid in sorted(set(LOADED_PLAN) | set(LOADED_EMIT_FIDS))}})

        elif path == "/api/tps-status":
            # Every connected controller's bulk TPS status, per filament (FID):
            # present, EN pin, fault, struggling, and the output enable -- what
            # tells STOP / SLEEP / powered apart. One bulk read per controller.
            out: dict[str, Any] = {"ok": True, "controllers": {}}
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                try:
                    out["controllers"][str(cid)] = read_tps_status(link, cid)
                except Exception as exc:
                    out["controllers"][str(cid)] = {"ok": False, "error": str(exc),
                                                    "filaments": {}}
            if not out["controllers"]:
                out = {"ok": False, "error": "no controller connected", "controllers": {}}
            elif not all(c.get("ok") for c in out["controllers"].values()):
                out["ok"] = False
            self._json(out)
        elif path == "/api/tps-struggling":
            # Which filaments the firmware cannot get STARTED: struggling is set
            # after 3 consecutive failed revives of a collapsed output and
            # cleared the instant it comes back. This is the ONLY signal that
            # catches a SHORT -- a shorted board never sets the CC mode's fault
            # bits (those need feedbackMv >= 2000, which a short cannot reach)
            # and its arrival bits stay "ramping" forever. Verified on a shorted
            # CH2.8: struggling bit set ~6.5 s after commanding IDLE while
            # mode stayed 1 and current stayed at 1 mA.
            out: dict[str, Any] = {"ok": True, "struggling": {}}
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                try:
                    ft, flags, payload = build_payload(
                        "CH_GET_TPS_STATUS", {"board_mask": [0xFF] * 8})
                    resp = link.client.send_request(ft, payload, flags=flags, timeout=2.0)
                    raw = resp.get("raw") if isinstance(resp, dict) else None
                    if not raw or len(raw) < 57:
                        # Short reply = older firmware without the mask. Absent,
                        # not "nothing is struggling": reporting an empty list
                        # would read as a clean bench.
                        out["struggling"][str(cid)] = None
                        continue
                    mask = raw[49:57]
                    fids = []
                    for ch in range(8):
                        for pos in range(8):
                            if mask[ch] & (1 << pos):
                                f = MAPPING.filament_for_board(cid - 1, ch, pos)
                                if f is not None:
                                    fids.append(int(f))
                    out["struggling"][str(cid)] = sorted(fids)
                except Exception as exc:
                    out["struggling"][str(cid)] = None
                    out.setdefault("errors", {})[str(cid)] = str(exc)
            self._json(out)

        elif path == "/api/dead-fids":
            # Filaments that must not be energised, in FID space, with the
            # provenance of each decision. See the DEAD_FIDS comment.
            with _DEAD_LOCK:
                entries = {str(k): dict(v) for k, v in sorted(DEAD_FIDS.items())}
            self._json({"ok": True, "space": "fid", "count": len(entries),
                        "dead": entries, "path": str(DEAD_STATE_PATH)})

        elif path == "/api/filament-order":
            # The USER_INDEX -> FID lens, so a new script inherits the numbering
            # the last one set. See FILAMENT_ORDER -- this is not applied here,
            # it is only remembered here, and it is NOT persisted across a
            # backend restart on purpose.
            self._json(order_snapshot())

        elif path == "/api/thermal-history":
            # How long each filament has been de-energised, so a measurement
            # that is only meaningful on a COLD filament can check its own
            # precondition instead of assuming it. A filament that was just run
            # is still hot, and its resistance reads high for minutes after the
            # power comes off -- see the client's measure_filament_resistance()
            # / sweep_filament_impedance().
            #
            # Derived from LAST_POWER_STATE, which is what this backend last
            # COMMANDED, so it carries that field's limits exactly: a filament
            # with no entry is UNKNOWN, not cold (nothing has been commanded
            # since connect, or a reconnect cleared it), and heat put in by
            # something other than this backend is invisible here.
            now = time.monotonic()
            out = {}
            for fid, (state, when) in sorted(LAST_POWER_STATE.items()):
                energised = state in ENERGISING_STATES
                out[str(fid)] = {
                    "state": state, "energising": energised,
                    "since_command_s": round(now - when, 1),
                    # None while still energised: it is not cooling yet, and a 0
                    # here would read as "just went cold" rather than "still on".
                    "cold_for_s": None if energised else round(now - when, 1),
                }
            self._json({"ok": True, "space": "fid", "filaments": out,
                        "note": "absent filament = unknown history, not cold"})

        elif path == "/api/clients":
            # Everyone that has called this API recently — so a program can see
            # it is not alone on the bench before it starts driving hardware.
            with _ACCESS_LOCK:
                clients = sorted(_CLIENTS.values(), key=lambda r: r["last_seen"], reverse=True)
                clients = [dict(r, idle_s=round(time.time() - r["last_seen"], 1)) for r in clients]
            self._json({"ok": True, "you": self._client(), "clients": clients,
                        "lock": _lease_snapshot()})
        elif path == "/api/safety":
            self._json(safety_snapshot())
        elif path == "/api/status":
            # Session snapshot: each controller's cached connection state
            # (ControllerLink.status(), no live hardware read), the current
            # MASTER (STM32-routing) selection, and the write-lease holder.
            self._json({"controllers": {str(k): c.status() for k, c in CONTROLLERS.items()},
                        "master": MASTER, "lock": _lease_snapshot(), "you": self._client()})
        elif path == "/api/mapping":
            # Read the HOST-owned filament<->power mapping (FilamentMapping.as_dict()).
            # No hardware access — this is the host's own view; it only reaches the
            # firmware's active-list table via POST /api/mapping {upload: true}.
            self._json({"ok": True, "mapping": MAPPING.as_dict()})
        elif path == "/api/telemetry" and _LIVE_PUSH:
            # PUSH mode (during a scan): the firmware streams cached currents every
            # ~50 ms; we assemble the snapshot from RECEIVED events with NO request,
            # so the frontend can poll this at ~20 fps. Geometry/firing come from the
            # separate /api/run-status poll. Synthetic run state keeps the live view fast.
            rows: dict[int, dict] = {}
            for cid in list(_LIVE_PUSH):
                link = CONTROLLERS.get(cid)
                if link and link.client.connected:
                    rows.update(read_pushed_telemetry(link, cid - 1))
            RUN_RECORDER.observe(rows.values())
            self._json({"telemetry": list(rows.values()), "firing": [],
                        "run": {str(c): {"state": 2} for c in _LIVE_PUSH}})
        elif path == "/api/telemetry":
            # Real per-filament telemetry for the ring: batch INA219 (V/I) per
            # connected controller, merged by the board map. Plus the live firing
            # filament from ShvGetStatus (no batch power-state read exists).
            #
            # ?live=1 forces the LIVE INA219 sweep (read_telemetry) instead of the
            # CC-loop cached read. The cached read is the default because it needs
            # ~1 round-trip instead of ~4-5 and is safe to poll mid-run, but it
            # carries NO bus voltage (the CC cache holds a current and nothing
            # else) -- so bus_mV is 0/cached there and the only way to get a real
            # voltage is this sweep. Making cached the default silently broke
            # read_filament_voltage(), which returned None for every filament
            # because every entry came back cached:True; read_telemetry() was left
            # with no callers at all. Still refused while a run is firing, where
            # the I2C sweep would stall pulses -- that case falls back to cached
            # and is flagged, rather than pretending a voltage exists.
            want_live = self._query().get("live") in ("1", "true", "yes")
            rows: dict[int, dict] = {}
            firing = []
            run_state = {}
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                # Served from the board monitor -- the one reader shared with the
                # boards matrix -- so this endpoint sends nothing of its own.
                snap = board_monitor_snapshot(cid) or {}
                st = snap.get("status")
                running = bool(st) and st.get("state") == 2
                if st:
                    run_state[str(cid)] = st
                    fi = st.get("filamentIndex")
                    if running and fi is not None and fi != 0xFF and fi < FILAMENT_COUNT:
                        firing.append(fi)   # firmware filamentIndex IS the global filament
                if want_live and not snap.get("run_owns"):
                    # Scripts asking for a live INA voltage (read_filament_voltage).
                    try:
                        rows.update(read_telemetry(link, cid - 1))
                    except Exception:
                        pass
                    continue
                board_rows, meta = monitor_board_rows(cid)
                if not meta["fresh"]:
                    continue      # no reading is no row -- never a row of zeros
                for b in board_rows:
                    fil = MAPPING.filament_for_board(cid - 1, b["channel"], b["mux_port"])
                    if fil is None:
                        continue
                    rows[fil] = {"index": fil, "present": b["present"],
                                 "bus_mV": b["bus_mV"], "current_mA": b["current_mA"],
                                 "age_ms": b["age_ms"], "cached": True}
            # Feed the run recorder so an end-of-run report can confirm each active
            # filament actually reached its target current. Auto start on the first
            # running poll (if a sim didn't already start it with the scheduled set)
            # and auto finalize when the run ends.
            running_now = any(s and s.get("state") == 2 for s in run_state.values())
            if running_now:
                if not RUN_RECORDER.active:
                    RUN_RECORDER.start()
                RUN_RECORDER.observe(rows.values())
            elif RUN_RECORDER.active:
                RUN_RECORDER.stop()
            self._json({"telemetry": list(rows.values()), "firing": firing, "run": run_state})
        elif path == "/api/board-snapshot":
            # 64-board matrix for the selected controller (?controller=N).
            # ?vi=1 → lightweight V/I-only refresh (skips the two bitmap reads); the
            # GUI merges it into its cache for a live ~1 Hz numbers update.
            # ?cached=1 → same merge, but current_mA comes from the zero-I2C 0x3A
            # CC-loop cache instead of a live 0x24 INA219 read — costs the RP2350
            # nothing, safe to poll fast (10 Hz) or while a schedule is firing.
            q = self.path.split("?", 1)
            cid = 1
            vi_only = False
            cached = False
            if len(q) > 1:
                for kv in q[1].split("&"):
                    if kv.startswith("controller="):
                        cid = int(kv.split("=", 1)[1] or 1)
                    elif kv.startswith("vi="):
                        vi_only = kv.split("=", 1)[1] in ("1", "true", "yes")
                    elif kv.startswith("cached="):
                        cached = kv.split("=", 1)[1] in ("1", "true", "yes")
            live = "live=1" in (q[1] if len(q) > 1 else "")
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                self._json({"ok": False, "error": "controller not connected", "boards": []})
            elif live:
                # Debug only: the old direct read (live I2C), idle only -- the
                # firmware answers Busy to its I2C queries during a run.
                try:
                    self._json({"ok": True, "live": True,
                                "boards": board_snapshot(link, cid - 1)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc), "boards": []})
            else:
                # From the board monitor -- the same data the ring shows, with no
                # request of its own (see MONITOR_*). vi_only/cached are accepted
                # for old callers and change nothing: every field is cached now.
                rows, meta = monitor_board_rows(cid)
                if meta["fresh"]:
                    self._json({"ok": True, "boards": rows, **meta})
                else:
                    self._json({"ok": False, "error": "the board monitor has no fresh data",
                                "boards": rows, **meta})
        elif path == "/api/present-filaments":
            # Which GLOBAL filament indices have a physically-present board, across
            # all connected controllers. The GUI uses this to one-click disable the
            # absent ones so the schedule fits the bench (arm rejects absent boards).
            present = []
            iso_errors: dict[str, str] = {}
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                # INA219 is powered by the isolated-12V rail, so a board reads
                # ABSENT unless iso is on first. The firmware's real presence scan
                # (runPresenceScan_ = forceEnableAllIso then scan) is serial-only,
                # so replicate it over the bridge: Sleep every board position
                # (setPowerState bundles iso on, no output current) → wait for INA
                # power-up → then read. Leaves boards at Sleep (iso on, low power).
                try:
                    prep_filaments(link, cid - 1, 2, None)   # state 2 = Sleep → iso on
                except Exception as exc:
                    # Without iso power the INA219s cannot answer, so EVERY board
                    # reads absent. Reporting that as "0 present" states a fact
                    # the scan never established.
                    iso_errors[str(cid)] = str(exc)
                time.sleep(0.4)
                # presence (CH_GET_PRESENT) still flakes on a slow link → union a
                # few reads; stop early once two passes agree on a non-empty set.
                pres, prev = set(), None
                for _ in range(5):
                    try:
                        now = {(b["channel"], b["mux_port"]) for b in board_snapshot(link, cid - 1) if b.get("present")}
                    except Exception:
                        now = set()
                    pres |= now
                    if pres and now == prev:
                        break
                    prev = now
                for fil in range(96):
                    c, ch, pos, *_ = filament_to_board(fil)
                    if c == cid - 1 and (ch, pos) in pres:
                        present.append(fil)
            present = sorted(set(present))
            # If iso power could not be applied, "0 present" is not a finding --
            # the INA219s simply had no supply to answer from. Say which
            # controllers that happened on rather than letting the count stand
            # as a measurement.
            self._json({"ok": not iso_errors, "present": present,
                        "count": len(present),
                        "iso_enable_errors": iso_errors or None,
                        "error": (f"could not power the presence rail on controller(s) "
                                  f"{sorted(iso_errors)} — a board reads ABSENT without "
                                  f"it, so this count is not a presence result"
                                  if iso_errors else None),
                        "note": "boards left at Sleep (iso on) after the scan"})
        elif path == "/api/hv-snapshot":
            # Raw per-channel ISO-grid bitmap for one controller (?controller=1|2,
            # via _target_link — not MASTER). HV_GET_ALL_BYTES (0x13) returns two
            # 8-byte arrays, one byte per channel (bit = board position 0-7):
            # desired = last-commanded 74HC595 relay state; feedback = what the
            # 74HC165 readback chain actually measured. Unresolved byte-array form
            # — see /api/hv-grid-status for the per-filament breakdown.
            link = self._target_link()
            if not link or not link.client.connected:
                self._json({"ok": False, "error": "controller not connected"})
            else:
                def _fetch_hv_snapshot(link=link):
                    try:
                        dec = link.request(0x13, b"", flags=0, timeout=2.0).get("decoded") or {}
                        return {"ok": True, "desired": dec.get("desired", [0] * 8),
                                "feedback": dec.get("feedback", [0] * 8)}
                    except Exception as exc:
                        return {"ok": False, "error": str(exc)}
                self._json(shared_read(("hv-snapshot", id(link)), SHARED_TTL_HV_SNAPSHOT_S,
                                       _fetch_hv_snapshot))
        elif path == "/api/adc/burst":
            # ESP32-local ADC (GP10, adc_sampler-driven) burst read via the
            # bridge's own /adc/burst — a DIFFERENT ADC path from
            # /api/adc/spi-shot's STM32 SPI capture, and unused in the current
            # wiring (the STM32 path is the only ADC actually in the signal
            # chain), but still functional. ?n=sample count. Routed to MASTER.
            host, err = self._master_host()
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
        elif path == "/api/adc/spi-shot":
            # Emission-current waveform: fire a bounded STM32 ADC shot over SPI
            # into PSRAM, download it, unpack (12-bit packed), return samples.
            # This is the ONLY ADC in the system — the ESP32 GP10 /adc/burst path
            # is unused. The shot also drives the STM32 pulse_detector, so
            # /api/pulse-events fills from the same capture.
            host, err = self._master_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                q = self._query()
                n = max(1, int(q.get("n", "2048")))
                rate = max(1, int(q.get("rate", "1000000")))
                budget = max(5.0, n / rate + 4.0)   # capture (n/fs) + download margin
                arm = adc_spi_shot_arm(host, n, rate, timeout=budget)
                if not arm.get("ok"):
                    self._json({"ok": False, "error": arm.get("error", "shot arm failed")})
                else:
                    data = adc_spi_shot_data(host, timeout=budget)
                    if not data.get("ok"):
                        self._json({"ok": False, "error": data.get("error", "shot download failed")})
                    else:
                        raw = data["bytes"]
                        hdr = {k.lower(): v for k, v in data.get("headers", {}).items()}
                        nhdr = int(hdr.get("x-spi-shot-samples", arm.get("n_samples", n)) or n)
                        bits = int(hdr.get("x-spi-shot-bits", 12) or 12)
                        samples = _unpack_spi_shot(raw, nhdr, bits)
                        self._json({"ok": True, "samples": samples, "rate_hz": rate,
                                    "n": len(samples), "capture_us": arm.get("capture_us", 0)})
        elif path == "/api/adc/ring-peek":
            # Live rolling waveform: newest N samples from the continuous ring
            # (u16 LE, plain). The ring streams continuously, so /api/pulse-events
            # fills from the same stream while it runs.
            host, err = self._master_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                n = max(1, int(self._query().get("n", "2048")))
                r = adc_ring_peek(host, n)
                if not r.get("ok"):
                    self._json({"ok": False, "error": r.get("error", "ring peek failed")})
                else:
                    raw = r["bytes"]
                    hdr = {k.lower(): v for k, v in r.get("headers", {}).items()}
                    samples = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw) - 1, 2)]
                    self._json({"ok": True, "samples": samples,
                                "rate_hz": int(hdr.get("x-ring-rate-hz", 0) or 0),
                                "n": len(samples),
                                "last_us": int(hdr.get("x-ring-last-us", 0) or 0),
                                "total": int(hdr.get("x-ring-total", 0) or 0),
                                "gaps": int(hdr.get("x-ring-gaps", 0) or 0)})
        elif path == "/api/pulse-events":
            # Poll new STM32-detected pulse events (from whichever capture last
            # armed the pulse_detector — a ring, pulse-arm, or spi-shot) with
            # id > ?since=. since is the host's paging cursor; the response's
            # last_id should be passed back on the next call for the delta only.
            host, err = self._master_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                self._json(pulse_events_get(host, int(self._query().get("since", "0"))))
        elif path == "/api/ringpulse/events":
            # Poll buffered RING_PULSE_EVENT frames the ESP32 pushes over the
            # separate framed esp_cmd TCP socket (port 3334) once Mode-2
            # fire-correlated capture is armed (POST /api/ringpulse/arm).
            # since is the eid paging cursor; response merges ESPCMD.status()
            # (connected/buffered/last_eid) alongside the event list.
            since = int(self._query().get("since", "0"))
            self._json({"ok": True, "events": ESPCMD.events_since(since), **ESPCMD.status()})
        elif path == "/api/record/status":
            # Status of the host-side recorder (MeasurementRecorder): running
            # flag, sample/packet/drop counters, and the .bin/.csv file names
            # for the run started by POST /api/record/start.
            self._json({"ok": True, **RECORDER.status()})
        elif path == "/api/record/download":
            # Serve a recorded pulses .csv from RECORD_DIR.
            name = os.path.basename(self._query().get("file", ""))
            fpath = RECORD_DIR / name
            if not name or not fpath.is_file():
                self._json({"ok": False, "error": "no such recording"}, HTTPStatus.NOT_FOUND)
            else:
                data = fpath.read_bytes()
                ctype = "text/csv" if name.endswith(".csv") else "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self._cors_headers()
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f"attachment; filename={name}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        elif path == "/api/stm32/ads1115":
            # Proxy to the ESP32 bridge's own STM32 sub-endpoint `/stm32/ads1115`
            # (see config_portal.cpp) — reads all 4 ADS1115 channels (raw codes,
            # mV, and engineering units) over the STM32's I2C bus. Routed to
            # whichever controller is MASTER (STM32 hangs off one power only).
            host, err = self._master_host()
            self._json({"ok": False, "error": err} if err else
                       shared_read(("stm32-ads1115", host), SHARED_TTL_STM32_S,
                                   lambda: stm32_ads1115(host)))
        elif path == "/api/stm32/adc-window":
            # Proxy to `/stm32/adc_window?n=` — a pulse-INDEPENDENT windowed
            # summary (min/max/mean/rms/std/pp) over the next n high-speed ADC
            # samples (1000 = 1 ms @ 1 MSPS). Requires the STM32 high-speed ADC
            # already streaming (arm a ring/pulse/spi-shot first) — this is
            # ground truth read straight off the STM32, not from any host cache.
            host, err = self._master_host()
            n = int(self._query().get("n", "1000"))
            if err:
                self._json({"ok": False, "error": err})
            else:
                self._json(_adc_window_autoarm(host, n))
        elif path == "/api/stm32/hv-status":
            # Proxy to `/stm32/hv_status` — the ACTUAL HV enable GPIO levels
            # (emission_on/focus_on read from the pin, not the commanded state)
            # plus ads1115_alert and amc3301_diag fault flags. This pin-level
            # read is what the GUI's Emission/Focus On/Off tiles display.
            host, err = self._master_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                self._json(shared_read(("stm32-hv-status", host), SHARED_TTL_STM32_S,
                                       lambda: stm32_hv_status(host)))
        elif path == "/api/stm32/ds3502":
            # Proxy to `/stm32/ds3502?ch=` — reads back one DS3502 digital-pot
            # wiper (0-127) over I2C. ch selects the pot: 'ev'=emission voltage,
            # 'ei'=emission current, 'fv'=focus voltage (or numeric 0|1|2).
            host, err = self._master_host()
            self._json({"ok": False, "error": err} if err else stm32_ds3502_get(host, self._query().get("ch", "ev")))
        elif path == "/api/sync/status":
            # Read the ESP32 bridge's own SyncIn/SyncOut hardware-trigger
            # config/state for ONE controller (?controller=1|2, via
            # _target_host — NOT MASTER, since each ESP32 has its own
            # SyncIn/SyncOut GPIO wiring independent of which one hosts the STM32).
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else sync_get_status(host))
        elif path == "/api/stm32/hv-target":
            # Proxy to `/stm32/hv_get_target?chan=` — reads the STM32's
            # closed-loop HV state for a channel (target ADC counts, last_adc,
            # active, at_target). The loop itself is started by POST
            # /api/stm32/hv-set-target; noted elsewhere as unreliable versus
            # the host-side LUT+direct-wiper approach POST /api/hv/set-v uses.
            host, err = self._master_host()
            self._json({"ok": False, "error": err} if err else stm32_hv_get_target(host, self._query().get("chan", "emission")))
        elif path == "/api/hv-lut":
            # Host-side HV setpoint LUT: wiper→measured-V samples from a Calibrate
            # sweep. Read by the GUI to map a target voltage to a DS3502 wiper
            # (replaces the unstable firmware closed loop). 404-as-ok={ok:false}.
            chan = self._query().get("chan", "emission")
            fp = _hv_lut_path(chan)
            if fp.is_file():
                try:
                    self._json({"ok": True, **json.loads(fp.read_text())})
                except (ValueError, OSError) as exc:
                    self._json({"ok": False, "error": f"LUT read failed: {exc}"})
            else:
                self._json({"ok": False, "error": "no LUT — calibrate first"})
        elif path == "/api/hv-grid-status":
            # Per-filament ISO HV-grid switch state (desired + measured feedback
            # bit) for one controller (?controller=1|2, default 1) — the readback
            # counterpart to POST /api/hv-grid.
            link = self._target_link()
            if not link or not link.client.connected:
                return self._json({"ok": False, "error": "controller not connected"})
            cid = int(self._query().get("controller", "1") or 1)
            ft, flags, payload = build_payload("HV_GET_ALL_BYTES", {})
            resp = link.client.send_request(ft, payload, flags=flags, timeout=2.0)
            raw = resp.get("raw") if isinstance(resp, dict) else None
            if not raw or len(raw) < 17 or raw[0] != 0:
                return self._json({"ok": False, "error": "HV_GET_ALL_BYTES failed"})
            desired, feedback = list(raw[1:9]), list(raw[9:17])
            out = {}
            for f in MAPPING.filaments(cid - 1):
                _, ch, pos, _ = filament_to_board(f)
                if ch is None:
                    continue
                out[str(f)] = {"desired": bool(desired[ch] & (1 << pos)),
                               "feedback": bool(feedback[ch] & (1 << pos))}
            self._json({"ok": True, "controller": cid, "filaments": out})
        elif path == "/api/filament-status":
            # Single-board heating status: the RP2350's own last-commanded
            # PowerState + fault kind for ONE filament (CH_GET_POWER_STATE,
            # 0x36, single-board). Distinct from /api/filament-currents
            # (measured mA, cached, no I2C) — this is state+fault, direct I2C.
            filament = int(self._query().get("filament", -1))
            cid0, ch, pos, _ = filament_to_board(filament)
            if cid0 is None or ch is None:
                return self._json({"ok": False, "error": f"filament {filament} has no board"})
            cid = cid0 + 1
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                return self._json({"ok": False, "error": f"controller {cid} not connected"})
            ft, flags, payload = build_payload("CH_GET_POWER_STATE", {"channel": ch, "mux_port": pos})
            try:
                resp = link.client.send_request(ft, payload, flags=flags, timeout=2.0)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)})
            raw = resp.get("raw") if isinstance(resp, dict) else None
            if not raw or len(raw) < 5:
                return self._json({"ok": False, "error": "bad response"})
            ok = raw[0] == 0
            self._json({"ok": ok, "filament": filament, "controller": cid,
                        "channel": ch, "mux_port": pos,
                        "state": raw[3] if ok else None,
                        "fault": raw[4] if ok else None})
        elif path == "/api/ocp-threshold":
            # Read back ONE filament's configured TPS55289 IOUT_LIMIT (steady-
            # state OCP trip current, mA), ?filament=N. CH_SET_TPS_OCP_THRESHOLD
            # (0x28) is SET-ONLY -- no matching "get" opcode -- so this reads the
            # raw register directly (CH_READ_TPS_REGISTER 0x29, reg=0x02) and
            # decodes it the SAME way the firmware's own SET path encodes it
            # (tps55289.cpp setOcpThresholdAmps/Millivolts): mA -> mV (=
            # mA * kOcpSenseResistorOhms) -> code (= round(mV / 0.5 mV LSB)),
            # register = kIoutLimitEnable(0x80) | (code & 0x7F). Single-board
            # I2C read: fine as a one-off, do NOT loop this to poll many boards.
            filament = int(self._query().get("filament", -1))
            cid0, ch, pos, _ = filament_to_board(filament)
            if cid0 is None or ch is None:
                return self._json({"ok": False, "error": f"filament {filament} has no board"})
            cid = cid0 + 1
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                return self._json({"ok": False, "error": f"controller {cid} not connected"})
            payload = bytes([ch & 0xFF, pos & 0xFF, _TPS_IOUT_LIMIT_REG, 1])
            try:
                resp = link.client.send_request(0x29, payload, flags=0, timeout=2.0)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)})
            raw = resp.get("raw") if isinstance(resp, dict) else None
            if not raw or len(raw) < 6 or raw[0] != 0x00:
                return self._json({"ok": False, "error": "bad response",
                                   "filament": filament, "controller": cid})
            regval = raw[5] & 0xFF   # width_bytes=1 -> value's low byte is the register
            enabled = bool(regval & 0x80)
            code = regval & 0x7F
            threshold_ma = round(code * _OCP_MA_PER_CODE) if enabled else 0
            self._json({"ok": True, "filament": filament, "controller": cid,
                        "channel": ch, "mux_port": pos,
                        "enabled": enabled, "threshold_ma": threshold_ma})
        elif path == "/api/ocp-startup":
            # Read the global per-controller two-stage OCP floor (?controller=1|2).
            cid = int(self._query().get("controller", "1") or 1)
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                return self._json({"ok": False, "error": f"controller {cid} not connected"})
            try:
                resp = link.client.send_request(0x37, b"", flags=0, timeout=2.0)
            except Exception as exc:
                return self._json({"ok": False, "error": str(exc)})
            raw = resp.get("raw") if isinstance(resp, dict) else None
            ok = bool(raw) and raw[0] == 0 and len(raw) >= 5
            self._json({"ok": ok, "controller": cid,
                        "startup_ma": _le(raw, 1, 2) if ok else None,
                        "steady_ma": _le(raw, 3, 2) if ok else None})
        elif path == "/api/download-progress":
            # Live download progress (the GUI polls this while /api/download blocks).
            with _DL_LOCK:
                prog = {str(c + 1): dict(v) for c, v in _DL_PROGRESS.items()}
            self._json({"ok": True, "controllers": prog})
        elif path == "/api/sync/simulate-status":
            # Scan-simulation fire progress (the GUI polls this while it runs).
            with _SIM_LOCK:
                self._json({"ok": True, **_SIM_STATE})
        elif path == "/api/run-report":
            # Power-state verification: live tracking while a run is active, and the
            # finalized report (per filament: peak current vs active target) after.
            self._json({"ok": True, **RUN_RECORDER.status()})
        elif path == "/api/cached-currents":
            # ONE paged bulk read (0x3A, no I2C) of CC-loop cached currents per
            # controller — the gentle way to read all boards. NEVER poll single-board.
            out = {}
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                try:
                    boards = read_cached_currents_by_board(link, MAPPING.channels_used(cid - 1))
                    out[str(cid)] = [{"channel": c, "mux": m, **v} for (c, m), v in sorted(boards.items())]
                except Exception as exc:
                    out[str(cid)] = {"error": str(exc)}
            self._json({"ok": True, "controllers": out})
        elif path == "/api/filament-currents":
            # Same bulk, run-safe (NO I2C) cached-currents read as above, but
            # keyed by GLOBAL filament index (0-95) across BOTH connected
            # controllers merged into one dict — matches ct_simple_control's
            # filament-index-first API. Confirms idle_one/active_one actually
            # landed at the commanded target; safe to poll even mid-run.
            #
            # ?filament=N (optional) switches to the SINGLE-board firmware command
            # (0x3A FLAG_SINGLE) — a different command, not a filtered bulk read:
            # one small frame, only the owning controller touched. Same response
            # shape either way, so a caller can use one code path for both. Omit
            # it (or ask for several) and you get the paged bulk sweep.
            q = self._query()
            one = q.get("filament")
            out = {}
            if one is not None and str(one).strip() != "":
                fil = int(one)
                cid0, _ch, _mux, _ = filament_to_board(fil)
                if cid0 is None:
                    return self._json({"ok": False, "filaments": {},
                                       "error": f"filament {fil} has no board"})
                link = CONTROLLERS.get(cid0 + 1)
                if not link or not link.client.connected:
                    return self._json({"ok": False, "filaments": {},
                                       "error": f"controller {cid0 + 1} not connected"})
                out = read_cached_telemetry_one(link, cid0, fil)
                return self._json({"ok": bool(out), "filaments": out,
                                   "single": True,
                                   **({} if out else {"error": "read failed"})})
            for cid, link in CONTROLLERS.items():
                if not link.client.connected:
                    continue
                try:
                    out.update(read_cached_telemetry(link, cid - 1))
                except Exception:
                    pass
            self._json({"ok": True, "filaments": out})
        elif path == "/api/logs":
            # Log files on THIS (the backend's) machine -- see list_log_files().
            self._json({"ok": True, "log_dir": str(LOG_DIR), "files": list_log_files()})
        elif path == "/api/logs/read":
            # ?path=<as listed>&tail=<lines>&grep=<substring> -- see read_log_file().
            from urllib.parse import unquote_plus
            q = {k: unquote_plus(v) for k, v in self._query().items()}
            try:
                tail = int(q.get("tail") or LOG_READ_DEFAULT_TAIL)
            except ValueError:
                return self._json({"ok": False, "error": "tail must be an integer"})
            self._json(read_log_file(q.get("path", "backend.log"), tail, q.get("grep") or None))
        elif path == "/api/version":
            # The commit this backend was started from (ct_update.version()),
            # so a client can tell it is talking to older or newer code.
            self._json({"ok": True, **_BACKEND_VERSION})
        elif path == "/api/run-status":
            # Poll ShvGetStatus (0x79) from each connected controller. totalPulsesDone
            # is the shared global playhead; filamentIndex is the live firing filament.
            # Shared (SHARED_TTL_RUN_STATUS_S): every GUI tab runs this poll while
            # a schedule runs, and a run is exactly when the link must stay quiet.
            def _fetch_run_status():
                out = {}
                for k, c in CONTROLLERS.items():
                    if not c.client.connected:
                        out[str(k)] = {"connected": False}
                        continue
                    # RP2350 heartbeat age — a periodic message from the RP2350; if it
                    # stops the RP2350 is dead/hung even though the ESP32 TCP link is up.
                    rp_age = None if c.rp_last == 0 else (time.time() - c.rp_last) * 1000.0
                    try:
                        st = decode_shv_status(c.request(SHV_GET_STATUS, b"", timeout=1.0))
                        out[str(k)] = {"connected": True, "status": st, "rp_age_ms": rp_age}
                    except Exception as exc:
                        out[str(k)] = {"connected": True, "error": str(exc), "rp_age_ms": rp_age}
                # Universal push teardown: tear the push down once the run has actually
                # STARTED (state 2 seen) and then stopped — guards the Armed-but-not-yet-
                # firing window right after enable. pollRunStatus polls this throughout.
                global _PUSH_SAW_RUN
                if _LIVE_PUSH:
                    any_running = any((v.get("status") or {}).get("state") == 2 for v in out.values())
                    if any_running:
                        _PUSH_SAW_RUN = True
                    elif _PUSH_SAW_RUN:
                        for c2 in list(_LIVE_PUSH):
                            l2 = CONTROLLERS.get(c2)
                            if l2 and l2.client.connected:
                                set_scan_telemetry(l2, c2 - 1, False)
                        _PUSH_SAW_RUN = False
                return out
            out = shared_read(("run-status",), SHARED_TTL_RUN_STATUS_S, _fetch_run_status)
            self._json({"controllers": out})
        else:
            self._serve_static(path)

    # --- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        # Every POST goes through the audit line in the finally -- including
        # the ones refused by the lease and the ones that raise, which are the
        # ones a post-mortem most needs. _read_json/_json note the request and
        # the reply on self; see audit_post().
        self._audit_body = None
        self._audit_resp = None
        self._audit_client = None
        self._audit_note = None
        try:
            self._do_post()
        finally:
            # A command changes what the shared reads would return: drop them,
            # so the next read -- this client's or anyone's -- is fresh.
            shared_invalidate()
            with _suppress():
                audit_post(self.path.split("?", 1)[0], self._audit_client,
                           self._audit_body, self._audit_resp, self._audit_note)

    def _do_post(self) -> None:
        global SCAN_MASK   # read (diagnosis branch) + written (channel-mask branch)
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        client = self._client(body)
        self._audit_client = client
        _note_client(client, self.client_address[0], path)
        held = self._lease_guard(path, body, client)
        if held is not None:
            return self._json(
                {"ok": False, "error": f"another client holds the write lease: {held['owner']}"
                                       + (f" ({held['note']})" if held["note"] else ""),
                 "lock": held},
                HTTPStatus.CONFLICT)
        try:
            if path == "/api/lock":
                # Cooperative exclusive-write lease. {action: acquire|renew|release|
                # status, ttl, note, steal} — acquire and renew are the same call.
                action = str(body.get("action", "acquire")).lower()
                if action in ("acquire", "renew", "lock", "release", "unlock") and client.startswith("anon@"):
                    # Two unnamed programs on the SAME machine would share the
                    # address-derived identity — one could then renew or release
                    # the other's lease, or slip a write past it. Holding the
                    # lease therefore requires saying who you are.
                    return self._json(
                        {"ok": False, "error": "identify yourself to use the lease: send an "
                                               "X-CT-Client header or a \"client\" field",
                         "you": client, "lock": _lease_snapshot()},
                        HTTPStatus.BAD_REQUEST)
                if action in ("acquire", "renew", "lock"):
                    ok = _lease_acquire(client, body.get("ttl", LOCK_TTL_DEFAULT_S),
                                        body.get("note", ""), steal=bool(body.get("steal")))
                    self._json({"ok": ok, "lock": _lease_snapshot(), "you": client,
                                **({} if ok else {"error": "held by another client"})},
                               HTTPStatus.OK if ok else HTTPStatus.CONFLICT)
                elif action not in ("status", "release", "unlock"):
                    # Unknown actions used to fall through to the status branch
                    # and come back ok=True, so a typo'd "relese" reported
                    # success while the lease stayed held.
                    return self._json(
                        {"ok": False, "you": client, "lock": _lease_snapshot(),
                         "error": f"unknown lock action {action!r}; expected "
                                  f"acquire | renew | release | status"},
                        HTTPStatus.BAD_REQUEST)
                elif action in ("release", "unlock"):
                    ok = _lease_release(client, force=bool(body.get("steal")))
                    self._json({"ok": ok, "lock": _lease_snapshot(), "you": client,
                                **({} if ok else {"error": "held by another client"})})
                else:
                    self._json({"ok": True, "lock": _lease_snapshot(), "you": client})
            elif path == "/api/raw":
                # Generic frame passthrough: full RP2350B protocol access for
                # programs that need a command this backend has no builder for.
                # {controller, type: 0x79|"0x79", payload: "hex"|[bytes], flags, timeout}
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.OK)
                if not link.client.connected:
                    return self._json({"ok": False, "error": f"{link.name} not connected"}, HTTPStatus.OK)
                try:
                    ftype = _coerce_int(body.get("type", body.get("frame_type")))
                    payload = _coerce_bytes(body.get("payload"))
                    resp = link.client.send_request(
                        ftype, payload, flags=_coerce_int(body.get("flags"), 0),
                        timeout=float(body.get("timeout", 2.0)))
                    self._json({"ok": True, "type": ftype, "name": TYPE_NAMES.get(ftype),
                                "response": resp})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
            elif path == "/api/connect":
                # Open the framed TCP session (port 3333) to one controller's
                # ESP32 bridge. body: {controller, host}. Drops the local
                # currents cache first — a reconnect may be to a freshly
                # reflashed board with an empty firmware table, so the next
                # download must re-send CH_FILAMENT_CURRENTS unconditionally.
                # The bridge accepts only ONE TCP client at a time; if another
                # client already holds the slot this reports that instead of
                # a silent offline state.
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                host = str(body.get("host", "")).strip()
                if not host:
                    return self._json({"ok": False, "error": "no host"}, HTTPStatus.BAD_REQUEST)
                # A (re)connect may be a freshly reflashed controller with an empty
                # table — drop its currents cache so the next download re-sends them.
                invalidate_currents_cache(cid - 1)
                link.connect(host)
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
                # Close the framed TCP session to one controller (body:
                # {controller}). Host-side only — no frame is sent to the RP2350;
                # the bridge notices the socket drop on its own.
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                link.disconnect()
                self._json({"ok": True, "status": link.status()})
            elif path == "/api/master":
                # Choose which controller carries the STM32 (all STM32/ADC commands
                # route here). Default Power 1.
                global MASTER
                # Explicit, not defaulted to the CURRENT master: a caller who
                # sent the wrong field name (say {"master": 7}) fell through to
                # "no change" and got ok=True, i.e. the request was ignored and
                # reported as success.
                if "controller" not in body and "master" not in body:
                    return self._json(
                        {"ok": False, "master": MASTER,
                         "error": "no controller given; send {\"controller\": 1|2}"},
                        HTTPStatus.BAD_REQUEST)
                cid = int(body.get("controller", body.get("master")))
                if cid not in CONTROLLERS:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                MASTER = cid
                self._json({"ok": True, "master": MASTER})
            elif path == "/api/mapping":
                # Edit the host filament->power mapping (active-list model). Either
                # set the alternating-group size, or a full per-filament assignment
                # (list[96] of 0/1/null). Optionally upload to connected controllers.
                global MAPPING
                # Remapping moves filaments between controllers/slots (ch,pos), so the
                # currents cache (keyed by filament) is no longer firmware truth — clear it.
                invalidate_currents_cache()
                if "assignment" in body:
                    MAPPING.set_assignment(body["assignment"], body.get("group_size"))
                elif "group_size" in body:
                    MAPPING.set_default(int(body["group_size"]))
                # Skip broken channel(s): {"skip_channels": {"1":[4], "2":[]}} or a flat
                # list applied to both controllers. 0-indexed channels (ch5 -> 4).
                if "skip_channels" in body:
                    sk = body["skip_channels"]
                    if isinstance(sk, dict):
                        for cid_s, chans in sk.items():
                            MAPPING.set_skip_channels(int(cid_s) - 1, chans or [])
                    else:
                        for c in (0, 1):
                            MAPPING.set_skip_channels(c, sk or [])
                uploaded = {}
                if body.get("upload"):
                    for cid, link in CONTROLLERS.items():
                        if not link.client.connected:
                            continue
                        try:
                            a = _status_ok(link.request(SHV_SET_ACTIVE_LIST, MAPPING.active_list(cid - 1), flags=0))
                            m = _status_ok(link.request(CH_SET_I2C_ENABLE_MASK, bytes([MAPPING.channel_mask(cid - 1) & 0xFF]), flags=0))
                            uploaded[str(cid)] = {"ok": a and m}
                        except Exception as exc:
                            uploaded[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": True, "mapping": MAPPING.as_dict(), "uploaded": uploaded})
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
                if not plan.get("emission"):
                    return self._json(
                        {"ok": False, "results": [],
                         "error": "plan has no emission rows — nothing would be "
                                  "downloaded, and a download of nothing is not a "
                                  "successful download"},
                        HTTPStatus.BAD_REQUEST)
                # Refuse the WHOLE download, not just the bad entries: a schedule
                # is a committed artifact, and silently dropping or altering a
                # heating delta changes what will actually run while the operator
                # believes they downloaded what they built.
                bad = check_heating_plan(plan.get("heating"))
                if bad:
                    return self._json({"ok": False, "error":
                                       "heating plan refused: " + "; ".join(bad[:8]),
                                       "problems": bad}, HTTPStatus.OK)
                channels = body.get("channels") or DEFAULT_CHANNELS
                # Download to each connected controller SEQUENTIALLY. Parallel transfers
                # to both bridges contend on the host's single WiFi uplink and drop each
                # other's frames (one loads, the other gets wiped to 0 — observed flapping).
                # Serializing costs ~2× wall-clock but each controller's ~108 UART
                # round-trips complete cleanly, which is what actually matters here.
                links = [(cid, link) for cid, link in CONTROLLERS.items() if link.client.connected]
                with _DL_LOCK:
                    _DL_PROGRESS.clear()   # fresh progress for the GUI poller
                results = []
                for cid, link in links:
                    try:
                        results.append(download_to_controller(link, cid - 1, plan, channels))
                    except Exception as exc:
                        link.set_poll_paused(False)   # ensure poll resumes even on exception
                        results.append({"controller": cid - 1, "ok": False, "error": str(exc)})
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                self._json({"ok": all(r.get("ok") for r in results), "results": results})
            elif path == "/api/verify-schedule":
                # Read the emission/heat tables back out of each controller and
                # compare counts to the loaded plan — confirms the download landed.
                plan = body.get("plan") or {}
                if not plan.get("emission"):
                    # 0 == 0 matches, so an empty plan "verified" — immediately
                    # before arming. Nothing to check is not checked.
                    return self._json(
                        {"ok": False, "results": {},
                         "error": "plan has no emission rows — nothing to verify, "
                                  "which is not the same as verified"},
                        HTTPStatus.BAD_REQUEST)
                emit_expected = len(plan.get("emission") or [])
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        controller = cid - 1
                        ti = link.request(SHV_GET_TABLE_INFO, b"", flags=0).get("raw") or []
                        hi = link.request(SHV_HEAT_GET_INFO, b"", flags=0).get("raw") or []
                        emit = _le(ti, 1, 2) if ti and ti[0] == 0 and len(ti) >= 7 else None
                        crc = _le(ti, 3, 4) if ti and ti[0] == 0 and len(ti) >= 7 else None
                        heat = _le(hi, 1, 2) if hi and hi[0] == 0 and len(hi) >= 5 else None
                        heat_expected = sum(1 for h in (plan.get("heating") or [])
                                            if filament_to_board(int(h["filament"]))[0] == controller)
                        results[str(cid)] = {
                            "ok": emit is not None and heat is not None,
                            "emit": emit, "crc": crc, "heat": heat,
                            "emitExpected": emit_expected, "heatExpected": heat_expected,
                            "match": emit == emit_expected and heat == heat_expected,
                        }
                        # Remember the CRC only when the table actually matches
                        # this plan: caching it on a mismatch would later let a
                        # reuse check "confirm" a table that was never right.
                        if crc is not None and results[str(cid)]["match"]:
                            LOADED_CRC[controller] = int(crc)
                        else:
                            LOADED_CRC.pop(controller, None)
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                self._json({"ok": all(r.get("match") for r in results.values()), "results": results})
            elif path == "/api/arm":
                why = trigger_delay_mismatch()
                if why:
                    return self._json({"ok": False, "error": f"arm refused: {why}",
                                       "trigger_delay_mismatch": True}, HTTPStatus.OK)
                # Arm the bound Simple-HV schedule on EVERY connected controller
                # (SHV_ARM, 0x77). body: {repeats}. Firmware validates the
                # loaded table (active-list coverage vs. board presence) before
                # arming and returns a nonzero reject code on failure — e.g. a
                # scheduled filament whose board isn't present rejects with
                # IsoOff. repeats sets how many times the bound sequence plays
                # per trigger cycle.
                repeats = int(body.get("repeats", 1))
                payload = _u16(max(1, repeats))
                # A filament marked dead AFTER the table was downloaded would
                # still fire: download-time filtering cannot see a decision made
                # later, and nothing else re-validates the loaded table. Refuse
                # the arm outright rather than filter -- there is no way to
                # remove one entry from a table that is already in firmware RAM,
                # so the only honest options are "refuse" and "fire it anyway".
                stale = {cid: sorted(fids & dead_fids())
                         for cid, fids in LOADED_EMIT_FIDS.items() if fids & dead_fids()}
                if stale:
                    log.warning("arm refused: loaded table contains dead filaments %s", stale)
                    return self._json(
                        {"ok": False, "error": "loaded schedule contains filaments marked dead "
                                               "since it was downloaded — re-download first",
                         "dead_in_table": {str(k + 1): v for k, v in stale.items()}},
                        HTTPStatus.OK)
                # Master LAST. The master forwards the trigger to the other
                # board only while it is itself armed (RP2350 9705e60), so with
                # the others armed first no edge reaches them before the master
                # is counting too -- an edge that lands in between is dropped by
                # both, not by one. Armed the other way round, or all at once in
                # dict order, a trigger between two arms leaves the boards a
                # whole entry apart for the run.
                order = sorted((cid for cid, link in CONTROLLERS.items() if link.client.connected),
                               key=lambda cid: cid == MASTER)
                # A run is active control of the filaments and the rails, same
                # as the per-board arm in shv_op: renew both dead-man timers.
                safety_touch_hv()
                safety_touch_filaments(list(LAST_POWER_STATE.keys()))
                results = {}
                for cid in order:
                    link = CONTROLLERS[cid]
                    try:
                        resp = link.request(SHV_ARM, payload, flags=0)
                        raw = resp.get("raw") if isinstance(resp, dict) else None
                        reject = raw[1] if raw and len(raw) > 1 else None
                        results[str(cid)] = {"ok": bool(raw) and raw[0] == 0 and reject == 0, "reject": reject}
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                    if not results[str(cid)]["ok"]:
                        break
                ok = bool(results) and all(r.get("ok") for r in results.values())
                if results and not ok:
                    # Half a rig armed runs half a schedule. Stop at the first
                    # failure (the master, last, is then never armed) and put
                    # back down every board that did arm.
                    for cid in order:
                        if results.get(str(cid), {}).get("ok"):
                            try:
                                results[str(cid)]["disarmed"] = _status_ok(
                                    CONTROLLERS[cid].request(SHV_DISARM, b"", flags=0))
                            except Exception as exc:
                                results[str(cid)]["disarmed"] = False
                                results[str(cid)]["disarm_error"] = str(exc)
                        elif str(cid) not in results:
                            results[str(cid)] = {"ok": False, "skipped": "an earlier board failed to arm"}
                self._json({"ok": ok, "order": order, "results": results})
            elif path == "/api/disarm":
                # Disarm the schedule engine on every connected controller
                # (SHV_DISARM, 0x78) — the firmware clears the ENTIRE HV ISO
                # relay bank instantly via ctrl_->clearAll() (74HC595 /SRCLR
                # pin), independent of whatever per-channel state
                # HV_SET_BIT/HV_SET_MULTI_CHANNEL last commanded.
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results[str(cid)] = {"ok": _status_ok(link.request(SHV_DISARM, b"", flags=0))}
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                if results and all(r.get("ok") for r in results.values()):
                    note_grid_commanded(False, clear_all=True)
                self._json({"ok": True, "results": results})
            elif path == "/api/safety":
                # Dead-man watchdog: read the state, or change the rules.
                # POST with any of enabled / active_timeout_s /
                # active_fallback / hv_timeout_s to change them; POST {} or GET
                # just reads. "keepalive": true renews without commanding
                # anything, for a caller legitimately holding a state while it
                # does its own work.
                changed = {}
                if body.get("keepalive"):
                    fids = body.get("filaments")
                    safety_touch_filaments([int(f) for f in fids] if fids
                                           else list(LAST_POWER_STATE.keys()))
                    safety_touch_hv()
                    changed["keepalive"] = True
                bad = []
                with _SAFETY_LOCK:
                    if "enabled" in body:
                        _SAFETY["enabled"] = bool(body["enabled"])
                        changed["enabled"] = _SAFETY["enabled"]
                    for key in ("active_timeout_s", "hv_timeout_s"):
                        if key in body:
                            try:
                                v = float(body[key])
                            except (TypeError, ValueError):
                                bad.append(f"{key} must be a number, got {body[key]!r}")
                                continue
                            if v <= 0:
                                bad.append(f"{key} must be positive, got {v} — "
                                           f"use enabled:false to switch the "
                                           f"watchdog off, so that turning it "
                                           f"off is a visible decision")
                                continue
                            _SAFETY[key] = v
                            changed[key] = v
                    if "active_fallback" in body:
                        # A NAME or a number. "sleep" is self-checking in a
                        # config file and a log line; 2 is not, and this field
                        # decides what an unattended filament gets dropped to.
                        st, why = parse_power_state(body["active_fallback"])
                        if st is None:
                            bad.append(f"active_fallback: {why}")
                        elif st.energising:
                            # The fallback must REMOVE power. Allowing an
                            # energising state here would make the watchdog fire
                            # from one hazard into another, which is worse than
                            # not firing at all.
                            bad.append(
                                f"active_fallback {st} is not a de-energising "
                                f"state — the watchdog may only fall back to "
                                f"{PowerState.STOP} or {PowerState.SLEEP}")
                        else:
                            _SAFETY["active_fallback"] = int(st)
                            changed["active_fallback"] = st.name
                if bad:
                    # Snapshot FIRST: it carries its own "ok": True, and
                    # spreading it after the refusal overwrote it -- the reply
                    # said ok=True and carried the reason it had refused.
                    return self._json({**safety_snapshot(), "ok": False,
                                       "error": "; ".join(bad),
                                       "changed": changed}, HTTPStatus.OK)
                self._json({**safety_snapshot(), "changed": changed})
            elif path == "/api/filament-state":
                # TRUE single-board CH_SET_POWER_STATE (0x35, FLAG_SINGLE) — one
                # filament, one frame, no masking/grouping machinery. This is the
                # RP2350's own single-board wire format, distinct from the batched
                # board_mask form /api/filament-prep uses even for a 1-filament
                # call. Use this for isolated single-filament control.
                filament = int(body.get("filament", -1))
                # Parsed defensively: a non-numeric state used to escape as
                # int()'s own "invalid literal for int() with base 10: 'idle'",
                # which names a Python builtin rather than the field or its
                # legal values.
                try:
                    state = int(body.get("state", 0))
                except (TypeError, ValueError):
                    return self._json(
                        {"ok": False, "error": f"state must be an integer 1..6 "
                                               f"({', '.join(f'{v}={n}' for v, n in sorted(POWER_STATE_NAMES.items()))}), "
                                               f"got {body.get('state')!r}"},
                        HTTPStatus.BAD_REQUEST)
                if state < 1 or state > 6:
                    return self._json(
                        {"ok": False, "error": f"state {state} out of range 1..6 "
                                               f"({', '.join(f'{v}={n}' for v, n in sorted(POWER_STATE_NAMES.items()))})"},
                        HTTPStatus.BAD_REQUEST)
                arg = int(body.get("arg", 0))
                # Enforcement, not just bookkeeping: ct_simple_control filters its
                # own dead mask before calling, but the GUI, a curl, or anyone
                # else's script does not -- and this backend would happily carry
                # the command out. Shape matches the client's contract
                # ({"ok": False, "dead": True}) so both layers look the same.
                if state in ENERGISING_STATES and filament in dead_fids():
                    with _DEAD_LOCK:
                        why = dict(DEAD_FIDS.get(filament) or {})
                    return self._json({"ok": False, "dead": True, "filament": filament,
                                       "error": f"filament {filament} is marked dead", "marked": why},
                                      HTTPStatus.OK)
                # SLEEP on a dead filament becomes STOP -- see DEAD_SLEEP_IS_STOP.
                dead_stopped = state == POWER_STATE_SLEEP and filament in dead_fids()
                if dead_stopped:
                    state, arg = int(POWER_STATE_STOP), 0
                # ACTIVE only from IDLE — see the ladder guard. Refused, not
                # filtered: a single-filament call has nothing to fall back to.
                if state == POWER_STATE_ACTIVE and arg < ACTIVE_FLOOR_MA:
                    return self._json({"ok": False, "below_active_floor": True,
                                       "filament": filament, "error":
                                       f"ACTIVE {arg} mA is below the "
                                       f"{ACTIVE_FLOOR_MA} mA floor (the idle "
                                       f"operating current) — promoting to ACTIVE "
                                       f"must not lower the current"},
                                      HTTPStatus.OK)
                # The mirror of the floor above, and the reason it is a refusal
                # rather than a clamp: the firmware already clamps this one
                # SILENTLY, so passing it through returns ok for a current that
                # will never be reached.
                if state == POWER_STATE_IDLE and arg > IDLE_CEILING_MA:
                    return self._json({"ok": False, "above_idle_ceiling": True,
                                       "filament": filament,
                                       "idle_ceiling_mA": IDLE_CEILING_MA, "error":
                                       f"IDLE {arg} mA is above the "
                                       f"{IDLE_CEILING_MA} mA ceiling — the RP2350 "
                                       f"would silently clamp it to "
                                       f"{IDLE_CEILING_MA} and report success, so a "
                                       f"wait for {arg} mA would never finish. Ask "
                                       f"for {IDLE_CEILING_MA} or less, or use "
                                       f"ACTIVE if you need more"},
                                      HTTPStatus.OK)
                if state == POWER_STATE_ACTIVE:
                    # One cheap single-board 0x3A (the CC cache, no I2C) so the
                    # guard tests where the filament IS, not only what it was told.
                    arr, arr_known = None, False
                    _c0, _ch, _p, _ = filament_to_board(filament)
                    _lk = CONTROLLERS.get((_c0 + 1) if _c0 is not None else 0)
                    if _lk and _lk.client.connected:
                        try:
                            ent = read_cached_telemetry_one(_lk, _c0, filament).get(filament)
                            if ent and "arrival" in ent:
                                arr, arr_known = ent.get("arrival"), True
                        except Exception:
                            pass   # unreadable -> fall back to the state-only check
                    why = ladder_blocks_active(filament, arr, arr_known)
                    if why:
                        log.warning("filament-state: refused ACTIVE for %d — %s", filament, why)
                        return self._json({"ok": False, "ladder_blocked": True,
                                           "filament": filament, "error":
                                           f"filament {filament} may not go to ACTIVE: {why}"},
                                          HTTPStatus.OK)
                cid0, ch, pos, _ = filament_to_board(filament)
                if cid0 is None or ch is None:
                    return self._json({"ok": False, "error": f"filament {filament} has no board"}, HTTPStatus.OK)
                cid = cid0 + 1
                link = CONTROLLERS.get(cid)
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": f"controller {cid} not connected"}, HTTPStatus.OK)
                # FAIL CLOSED. This asks "is a schedule firing right now?"
                # before changing one filament's power state. Swallowing the
                # error meant a guard that could not run was a guard that
                # passed -- the one case where the answer matters most is
                # exactly when the link is sick enough to fail the read.
                try:
                    st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                except Exception as exc:
                    return self._json(
                        {"ok": False, "error": f"cannot confirm controller {cid} is "
                                               f"not running a schedule ({exc}); "
                                               f"refusing rather than assuming idle"},
                        HTTPStatus.OK)
                if st and st.get("state") == 2:
                    return self._json({"ok": False, "error": "running — disarm first"}, HTTPStatus.OK)
                ft, flags, payload = build_payload("CH_SET_POWER_STATE", {
                    "channel": ch, "mux_port": pos, "state": state, "arg": arg,
                })
                try:
                    resp = link.client.send_request(ft, payload, flags=flags, timeout=3.0)
                except Exception as exc:
                    return self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
                raw = resp.get("raw") if isinstance(resp, dict) else None
                applied = bool(raw and len(raw) >= 4 and raw[3] == 1)
                if applied:
                    note_power_state([filament], state)
                out = {"ok": _status_ok(resp) and applied, "filament": filament,
                       "controller": cid, "channel": ch, "mux_port": pos,
                       "state": state, "arg": arg}
                if dead_stopped:
                    out["dead_stopped"] = True
                    out["note"] = (f"filament {filament} is marked dead: STOPped "
                                   f"instead of SLEEP, which would power its rail")
                self._json(out)
            elif path == "/api/filament-prep":
                # CT-scan prep ladder — apply one PowerState to a batch of
                # filaments across BOTH connected controllers. Refused while a
                # schedule is running (would fight the executor's heating).
                # Parsed defensively: a non-numeric state used to escape as
                # int()'s own "invalid literal for int() with base 10: 'idle'",
                # which names a Python builtin rather than the field or its
                # legal values.
                try:
                    state = int(body.get("state", 0))
                except (TypeError, ValueError):
                    return self._json(
                        {"ok": False, "error": f"state must be an integer 1..6 "
                                               f"({', '.join(f'{v}={n}' for v, n in sorted(POWER_STATE_NAMES.items()))}), "
                                               f"got {body.get('state')!r}"},
                        HTTPStatus.BAD_REQUEST)
                if state < 1 or state > 6:
                    return self._json(
                        {"ok": False, "error": f"state {state} out of range 1..6 "
                                               f"({', '.join(f'{v}={n}' for v, n in sorted(POWER_STATE_NAMES.items()))})"},
                        HTTPStatus.BAD_REQUEST)
                filaments = body.get("filaments")   # None = all populated boards
                currents = body.get("currents") or {}
                default_arg = int(body.get("arg", 0))
                channels = body.get("channels") or DEFAULT_CHANNELS
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                        if st and st.get("state") == 2:
                            results[str(cid)] = {"ok": False, "error": "running — disarm first"}
                            continue
                        results[str(cid)] = prep_filaments(link, cid - 1, state, filaments,
                                                           currents, default_arg, channels)
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                failed = [int(f) for r in results.values() for f in (r.get("failed") or [])]
                applied = sum(int(r.get("applied") or 0) for r in results.values())
                # Reconcile the ORIGINAL request against what every connected
                # controller actually claimed (prep_filaments()'s "touched"). A
                # filament can legitimately be "not_this_controller" for controller
                # A while being claimed by controller B -- that's normal, not an
                # error. It's only a real, reportable exclusion if NO connected
                # controller ends up touching it: its home controller isn't
                # connected, or MAPPING has no slot for it at all. Silently
                # shrinking "total"/"applied" with no trace of this was the actual
                # bug being fixed here -- a caller could request N filaments, have
                # fewer than N actually attempted, and still see ok:true with no
                # indication anything was skipped.
                touched = {int(f) for r in results.values()
                           for f in (r.get("touched") or []) + (r.get("dead_stopped") or [])}
                excluded = ([int(f) for f in filaments if int(f) not in touched]
                            if filaments is not None else [])
                blocked = sorted(int(f) for r in results.values()
                                 for f in (r.get("ladder_blocked") or []))
                out = {"ok": all(r.get("ok") for r in results.values()) and not excluded,
                       "results": results, "failed": failed, "applied": applied,
                       "excluded": excluded}
                if blocked:
                    reasons = {k: v for r in results.values()
                               for k, v in (r.get("ladder_reasons") or {}).items()}
                    out["ladder_blocked"] = blocked
                    why = sorted(set(reasons.values()))
                    out["error"] = (f"{len(blocked)} filament(s) refused and NOT commanded: "
                                    + "; ".join(why[:3]) + (" …" if len(why) > 3 else ""))
                self._json(out)
            elif path == "/api/ocp-threshold":
                # Per-board TPS55289 IOUT_LIMIT (steady-state OCP threshold, mA)
                # for a batch of filaments across BOTH connected controllers.
                # body: {filaments: [...]|None, threshold_ma: int}. No native
                # batch opcode exists — loops one frame per filament.
                try:
                    threshold_ma = int(body.get("threshold_ma"))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "threshold_ma required"}, HTTPStatus.OK)
                # Range-check here rather than letting struct raise: an
                # out-of-range value surfaced as "int too big to convert",
                # which names a Python detail instead of the actual limit, and
                # arrived buried in a per-controller results entry.
                if not (0 <= threshold_ma <= 0xFFFF):
                    return self._json(
                        {"ok": False, "error": f"threshold_ma {threshold_ma} out of "
                                               f"range 0..65535 (TPS55289 IOUT_LIMIT "
                                               f"is a 16-bit field); nothing written"},
                        HTTPStatus.BAD_REQUEST)
                filaments = body.get("filaments")   # None = all populated boards
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results[str(cid)] = set_ocp_threshold_batch(link, cid - 1, filaments, threshold_ma)
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                failed = [int(f) for r in results.values() for f in (r.get("failed") or [])]
                applied = [int(f) for r in results.values() for f in (r.get("applied") or [])]
                # See /api/hv-grid for why this reconciliation exists. OCP is a
                # protection setting, so "asked for, never written, reported ok"
                # is the worst of the three places this pattern appeared.
                excluded = []
                if filaments is not None:
                    touched = {int(f) for r in results.values() for f in (r.get("touched") or [])}
                    excluded = [int(f) for f in filaments if int(f) not in touched]
                self._json({"ok": all(r.get("ok") for r in results.values()) and not excluded,
                            "results": results, "failed": failed, "applied": applied,
                            "excluded": excluded})
            elif path == "/api/ocp-startup":
                # Global per-controller two-stage OCP floor (CH_STARTUP_OCP,
                # 0x37) — NOT per-board. STARTUP rides the cold inrush on
                # turn-on; STEADY applies ~2 s later for close-in protection.
                # body: {controller, startup_ma, steady_ma (optional)}.
                cid = int(body.get("controller", MASTER))
                link = CONTROLLERS.get(cid)
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": f"controller {cid} not connected"}, HTTPStatus.OK)
                try:
                    startup_ma = int(body.get("startup_ma"))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "startup_ma required"}, HTTPStatus.OK)
                # Range-checked before packing: _u16 on a negative raised
                # struct's own "can't convert negative int to unsigned", which
                # names the packer rather than the field or its limit.
                steady_ma = int(body["steady_ma"]) if "steady_ma" in body else None
                out_of_range = {n: v for n, v in (("startup_ma", startup_ma),
                                                  ("steady_ma", steady_ma))
                                if v is not None and not (0 <= v <= 0xFFFF)}
                if out_of_range:
                    return self._json(
                        {"ok": False, "error": f"out of range 0..65535: "
                                               f"{out_of_range}; nothing written"},
                        HTTPStatus.BAD_REQUEST)
                # CH_STARTUP_OCP (0x37) wire format: [] get, [mA16] set startup
                # only, [mA16,mA16] set both — build_payload only covers the
                # 2-byte set form, so build the frame directly here.
                payload = _u16(startup_ma)
                if steady_ma is not None:
                    payload += _u16(steady_ma)
                try:
                    resp = link.client.send_request(0x37, payload, flags=0, timeout=2.0)
                except Exception as exc:
                    return self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
                raw = resp.get("raw") if isinstance(resp, dict) else None
                ok = bool(raw) and raw[0] == 0 and len(raw) >= 5
                self._json({"ok": ok, "controller": cid,
                            "startup_ma": _le(raw, 1, 2) if ok else None,
                            "steady_ma": _le(raw, 3, 2) if ok else None})
            elif path == "/api/hv-grid":
                # ISO HV-grid switch toggle for a batch of filaments (or every
                # populated board) across BOTH connected controllers. body:
                # {filaments: [...]|None, on: bool, force: bool=true}.
                # force=true bypasses firmware fault/verify checks — same
                # semantics as the GUI's Force checkbox; use when the switch
                # feedback is unreliable or the filament is known-shorted.
                # Dead filaments are enforced in hv_grid_set() itself, not left
                # to callers: clients that filter (ct_simple_control) just never
                # reach it, and clients that do not (the GUI, a curl) are stopped
                # there rather than energising a filament someone disabled.
                on = bool(body.get("on"))
                force = bool(body.get("force", True))
                filaments = body.get("filaments")   # None = all populated boards
                # An index outside 0..95 was landing in "not_this_controller",
                # i.e. reported as belonging to the OTHER power rather than as
                # not existing -- and the top-level error came back empty, so
                # the caller saw ok=False with nothing to read.
                if filaments is not None:
                    oor = [f for f in filaments if not (0 <= int(f) < FILAMENT_COUNT)]
                    if oor:
                        return self._json(
                            {"ok": False, "results": {}, "excluded": oor,
                             "error": f"filament(s) {oor} outside 0..{FILAMENT_COUNT - 1}"},
                            HTTPStatus.BAD_REQUEST)
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results[str(cid)] = hv_grid_set(link, cid - 1, filaments, on, force)
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                applied = [f for r in results.values() for f in (r.get("applied") or [])]
                failed = [f for r in results.values() for f in (r.get("failed") or [])]
                mismatched = [f for r in results.values() for f in (r.get("mismatched") or [])]
                # Reconcile what was ASKED FOR against what any controller actually
                # touched -- same treatment /api/filament-prep already has. Without
                # it a filament belonging to a DISCONNECTED controller is dropped by
                # every loop iteration and the call returns ok:True having done
                # nothing. On an HV-off request that reads as "grid is clear" when
                # it isn't. Only meaningful when the caller named filaments; None
                # means "every populated board", which excludes nothing by
                # definition.
                excluded = []
                if filaments is not None:
                    touched = {f for r in results.values() for f in (r.get("touched") or [])}
                    excluded = [int(f) for f in filaments if int(f) not in touched]
                out = {"ok": all(r.get("ok") for r in results.values()) and not excluded,
                       "results": results, "applied": applied, "failed": failed,
                       "excluded": excluded}
                if mismatched:
                    out["mismatched"] = mismatched
                # Dead-man timer. A close that failed or did not verify may
                # still have closed, so it counts as closed; an open counts
                # only where it applied.
                if on:
                    note_grid_commanded(True, applied + failed + mismatched)
                else:
                    note_grid_commanded(False, [f for f in applied if f not in mismatched])
                self._json(out)
            elif path == "/api/calibration/save":
                # Persist an emission-current calibration to the host disk (JSON +
                # flat CSV). body: {name, params, curves:{filament:[{heatA,mA,peak}]}}.
                data = body.get("data") or body
                name = "".join(c for c in str(body.get("name", "emission_calibration")) if c.isalnum() or c in "._-") or "calibration"
                CALIB_DIR.mkdir(exist_ok=True)
                ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                base = CALIB_DIR / f"{name}_{ts}"
                base.with_suffix(".json").write_text(json.dumps(data, indent=2))
                curves = data.get("curves") or {}
                cols: list[str] = []        # union of point fields, in first-seen order
                for curve in curves.values():
                    for pt in curve:
                        for k in pt:
                            if k not in cols:
                                cols.append(k)
                # Written through csv.writer, NOT by joining on commas: a point
                # field may legitimately contain a comma (a free-text note, a
                # list), and hand-joining silently splits that value across
                # columns -- every later row field shifts by one, so the file
                # still parses and every number in it is attributed to the wrong
                # column. Quoting is the difference between a corrupt file and
                # one that says what it means.
                with base.with_suffix(".csv").open("w", newline="") as fh:
                    w = csv.writer(fh)
                    w.writerow(["filament"] + cols)
                    for fil, curve in sorted(curves.items(), key=lambda kv: int(kv[0])):
                        for pt in curve:
                            # A JSON null -> EMPTY cell, not the string "None".
                            # The GUI sends mA null for a point it could not
                            # measure (no pulse event, or an event with
                            # background_n 0, so the current would have been ~32
                            # mA of pure offset); "None" in a numeric column
                            # parses as garbage or, worse, gets cleaned to 0
                            # downstream, which is the fabricated reading this
                            # null exists to avoid.
                            w.writerow([fil] + ["" if pt.get(k) is None else pt.get(k)
                                                for k in cols])
                self._json({"ok": True, "json": str(base.with_suffix(".json")),
                            "csv": str(base.with_suffix(".csv")), "filaments": len(data.get("curves") or {})})
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
                        res = {"channel_mask": SCAN_MASK, "fw_channel_mask": read_channel_mask(link)}
                        res.update(run_diagnosis(link))
                        out[str(cid)] = res
                    except Exception as exc:
                        out[str(cid)] = {"error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/tca9554-read":
                # Full per-channel TCA9554 register dump per controller (0x61).
                out = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        out[str(cid)] = run_tca9554_read(link)
                    except Exception as exc:
                        out[str(cid)] = {"tca9554_error": str(exc)}
                self._json({"ok": bool(out), "controllers": out})
            elif path == "/api/filament-order":
                # {"order": [96 ints]} to set, {"order": null} to clear back to
                # identity. Validated here too -- every other client reads this
                # back, so a non-permutation stored here corrupts all of them.
                global FILAMENT_ORDER, ORDER_SET_BY, ORDER_SET_AT
                order = body.get("order")
                if order is None:
                    with _ORDER_LOCK:
                        FILAMENT_ORDER = None
                        ORDER_SET_BY = self._client()
                        ORDER_SET_AT = time.monotonic()
                    log.info("filament order cleared to identity by %s", self._client())
                    self._json(order_snapshot())
                else:
                    why = order_validate(order)
                    if why:
                        self._json({"ok": False, "error": f"bad order: {why}"})
                    else:
                        with _ORDER_LOCK:
                            FILAMENT_ORDER = ([int(v) for v in order]
                                              if list(map(int, order)) != list(range(FILAMENT_COUNT))
                                              else None)
                            ORDER_SET_BY = self._client()
                            ORDER_SET_AT = time.monotonic()
                        swapped = sum(1 for i, v in enumerate(order) if int(v) != i)
                        log.info("filament order set by %s (%d entries differ from identity)",
                                 self._client(), swapped)
                        self._json(order_snapshot())
            elif path == "/api/dead-fids":
                # Mark filaments as must-not-energise, or clear them. FID space.
                # body: {op: "set"|"add"|"remove", fids: [...], reason: str}
                #
                # Clearing is deliberately as explicit as marking: nothing in this
                # backend ever clears an entry on its own (see DEAD_FIDS), so the
                # only way a filament comes back is a person saying so here.
                op = str(body.get("op", "add")).lower()
                if op not in ("set", "add", "remove"):
                    return self._json({"ok": False, "error": "op must be set|add|remove"},
                                      HTTPStatus.OK)
                raw_fids = body.get("fids")
                if not isinstance(raw_fids, list):
                    return self._json({"ok": False, "error": "fids must be a list"},
                                      HTTPStatus.OK)
                try:
                    fids = [int(f) for f in raw_fids]
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "fids must be integers"},
                                      HTTPStatus.OK)
                bad = [f for f in fids if not 0 <= f < FILAMENT_COUNT]
                if bad:
                    # Reject the whole call rather than applying the valid part: a
                    # half-applied safety mask is worse than a refused one, because
                    # the caller believes all of it landed.
                    return self._json({"ok": False, "error": f"fids outside 0..{FILAMENT_COUNT - 1}: {bad}"},
                                      HTTPStatus.OK)
                reason = str(body.get("reason", "")).strip()
                if op in ("set", "add") and not reason:
                    return self._json({"ok": False, "error":
                                       "reason is required when marking a filament dead "
                                       "-- an entry nothing can clear automatically has to "
                                       "say why, or nobody will dare clear it later"},
                                      HTTPStatus.OK)
                entry = {"reason": reason, "by": self._client(),
                         "at": datetime.datetime.now().astimezone().isoformat(timespec="seconds")}
                with _DEAD_LOCK:
                    before = set(DEAD_FIDS)
                    if op == "set":
                        DEAD_FIDS.clear()
                        for f in fids:
                            DEAD_FIDS[f] = dict(entry)
                    elif op == "add":
                        for f in fids:
                            DEAD_FIDS.setdefault(f, dict(entry))   # keep the ORIGINAL provenance
                    else:
                        for f in fids:
                            DEAD_FIDS.pop(f, None)
                    after = set(DEAD_FIDS)
                    _dead_save()
                added, removed = sorted(after - before), sorted(before - after)
                if added or removed:
                    log.warning("dead filaments changed by %s: +%s -%s (reason=%r) -> now %s",
                                self._client(), added, removed, reason, sorted(after))
                return self._json({"ok": True, "space": "fid", "added": added,
                                   "removed": removed, "dead": sorted(after)}, HTTPStatus.OK)

            elif path == "/api/channel-mask":
                # Set the channel enable mask. The HOST poll set (SCAN_MASK) is the
                # real lever — board_snapshot reads INA219 V/I only for these
                # channels, so this is what makes the matrix poll (or stop polling) a
                # channel. We ALSO push it to the firmware (0x34) for completeness,
                # but the firmware ignores its own mask (always scans all 8), so that
                # part is cosmetic.
                # Refuse rather than & 0xFF: a mask of 999 became 0xE7, i.e. a
                # DIFFERENT set of channels than asked for, reported as success.
                raw_mask = int(body.get("mask", 0x3F))
                if not (0 <= raw_mask <= 0xFF):
                    return self._json(
                        {"ok": False, "error": f"mask {raw_mask} out of range "
                                               f"0..255 (one bit per channel)"},
                        HTTPStatus.BAD_REQUEST)
                mask = raw_mask
                SCAN_MASK = mask
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
                count = int(body.get("count", 1))
                if count < 1:
                    return self._json(
                        {"ok": False, "error": f"count={count} must be >= 1; nothing "
                                               f"was fired"},
                        HTTPStatus.BAD_REQUEST)
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
            elif path == "/api/poll-pause":
                # Pause/resume a controller's background PING for the duration of an
                # exclusive bench op (HV toggle test). body: {controller, paused}.
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.OK)
                link.set_poll_paused(bool(body.get("paused")))
                self._json({"ok": True, "paused": bool(body.get("paused"))})
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
            elif path == "/api/adc/ring-start":
                # Arm the STM32 for CONTINUOUS ADC capture and start the
                # ESP32's core-1 task draining DATA_READY blocks into a PSRAM
                # ring (adc_spi.cpp ring_*). The same stream also feeds the
                # STM32 pulse_detector, so /api/pulse-events fills while this
                # runs. body: {rate (Hz, default 1 MSPS)}. Routed to MASTER.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ring_start(host, int(body.get("rate", 1000000))))
            elif path == "/api/adc/ring-stop":
                # Stop the continuous ring capture started by ring-start
                # (also halts the pulse_detector feed it was driving).
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ring_stop(host))
            elif path == "/api/adc/pulse-arm":
                # Arm the STM32 ADC for pulse-detect ONLY (EVT_PULSE pushed
                # over UART) — no continuous SPI transfer to the ESP32, so it
                # doesn't load WiFi the way ring-start does. Use for per-pulse
                # measurement/calibration where only /api/pulse-events matters.
                # body: {rate (Hz, default 1 MSPS)}. Shares the arm with
                # Record measurement (detector_arm) — see its comment.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(detector_arm(host, int(body.get("rate", 1000000)), "stream"))
            elif path == "/api/adc/ready-arm":
                # Arm the pulse-envelope RELAY (and the STM32 detector inside it).
                # Distinct from /api/adc/pulse-arm, which arms only the detector:
                # without the relay, PA4 never moves and nothing gets measured.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                # ONE symmetric pair, in SAMPLES, applied to BOTH sides of the
                # envelope: settle for bg_gap, then average bg_window —
                #   <- gap -><- win ->| envelope |<- win -><- gap ->
                # (the pre-side mean is the one SUBTRACTED from the charge).
                # There is no separate post_bg_gap/post_bg_n any more; a caller
                # still sending those is sending nothing, so say so rather than
                # arming with the defaults and reporting success.
                stale = [k for k in ("post_bg_gap", "post_bg_n") if k in body]
                if stale:
                    return self._json({"ok": False, "error":
                        f"{', '.join(stale)} no longer exist — the background is one symmetric "
                        "pair applied to both sides. Send bg_gap / bg_window."}, HTTPStatus.OK)
                bgg = body.get("bg_gap")
                bgw = body.get("bg_window")
                ttl = body.get("ttl_ms")
                bgg = None if bgg is None else int(bgg)
                bgw = None if bgw is None else int(bgw)
                # Refuse out-of-range, never clamp — the ESP32 and the STM32 both
                # refuse too, and a silently clamped window would average a
                # different number of samples than the one that gets recorded as
                # the request. Checked here as well so the refusal names the
                # limit instead of arriving as a bare device 400.
                # Ranges are the STM32's raw-sample history: window 1..1024,
                # gap 0..3072, and gap+window <= 4096 per side.
                if bgw is not None and not (1 <= bgw <= 1024):
                    return self._json({"ok": False, "error":
                        f"bg_window {bgw} out of range (1..1024 samples)"}, HTTPStatus.OK)
                if bgg is not None and not (0 <= bgg <= 3072):
                    return self._json({"ok": False, "error":
                        f"bg_gap {bgg} out of range (0..3072 samples)"}, HTTPStatus.OK)
                if bgw is not None and bgg is not None and bgg + bgw > 4096:
                    return self._json({"ok": False, "error":
                        f"bg_gap + bg_window = {bgg + bgw} exceeds the STM32's 4096-sample "
                        "history; one of them has to come down"}, HTTPStatus.OK)
                self._json(adc_ready_arm(host, int(body.get("rate", 1000000)),
                                         int(body.get("n_samples", 2000)),
                                         bgg, bgw,
                                         None if ttl is None else int(ttl)))
            elif path == "/api/recover":
                # Clear state left behind by an operation that did not finish:
                # a killed script, a backend that exited before its cleanup, a
                # Ctrl-C. Those leave the pulse-envelope relay armed, the STM32
                # CS claimed, or a schedule armed — and every later run then
                # fails with "already armed" or an arm reject that looks like a
                # hardware problem.
                #
                # Reports what it FOUND as well as what it cleared: "nothing was
                # stuck" and "something was stuck and I fixed it" must not look
                # the same, or a recurring leak stays invisible.
                #
                # Deliberately does NOT de-energise filaments by default. Those
                # are the one piece of state where clearing could interrupt
                # somebody's legitimate run, and heat is not what gets a later
                # run stuck. Pass {"stop_heating": true} to include it; either
                # way the energised filaments are reported.
                host, err = self._master_host()
                found: dict[str, Any] = {}
                cleared: list[str] = []
                if not err:
                    try:
                        st = adc_ready_status(host)
                        found["ready_relay"] = st
                        if st.get("armed"):
                            adc_ready_disarm(host)
                            cleared.append("ready_relay (was armed)")
                    except Exception as exc:
                        found["ready_relay"] = {"error": str(exc)}
                else:
                    found["ready_relay"] = {"error": err}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        sh = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                        found[f"schedule_{cid}"] = ({"state": sh.get("state"),
                                                     "unsafeSlots": sh.get("unsafeSlots")}
                                                    if sh else None)
                        # state 1 = armed, 2 = running. Disarming a RUNNING
                        # schedule is the one thing here that stops work in
                        # progress, so it is reported distinctly.
                        if sh and sh.get("state") in (1, 2):
                            link.request(SHV_DISARM, b"", flags=0)
                            cleared.append(f"schedule on controller {cid} "
                                           f"(was {'running' if sh.get('state') == 2 else 'armed'})")
                    except Exception as exc:
                        found[f"schedule_{cid}"] = {"error": str(exc)}
                # Energised filaments: always reported, only stopped on request.
                hot: list[int] = []
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        for fil, ent in (read_cached_telemetry(link, cid - 1) or {}).items():
                            if (ent or {}).get("present"):
                                hot.append(int(fil))
                    except Exception:
                        pass
                found["energised_filaments"] = sorted(hot)
                if hot and bool(body.get("stop_heating")):
                    # A failed emergency STOP must never be silent: this is the
                    # call someone makes BECAUSE something is already wrong, and
                    # reporting the filaments it found while hiding that it could
                    # not stop them is the worst combination available here.
                    stop_errors = {}
                    for cid, link in CONTROLLERS.items():
                        if link.client.connected:
                            try:
                                prep_filaments(link, cid - 1, 1, None)   # STOP
                            except Exception as exc:
                                stop_errors[str(cid)] = str(exc)
                    found["stop_heating_errors"] = stop_errors or None
                    found["stop_heating_ok"] = not stop_errors
                    if stop_errors:
                        found["error"] = (f"STOP failed on controller(s) "
                                          f"{sorted(stop_errors)} — filaments may "
                                          f"still be energised")
                    cleared.append(f"stopped {len(hot)} energised filament(s)")
                if cleared:
                    log.warning("recover by %s: cleared %s", self._client(), cleared)
                self._json({"ok": True, "found": found, "cleared": cleared,
                            "was_stuck": bool(cleared)})

            elif path == "/api/adc/ready-status":
                # Armed / edges relayed. A POST only because everything in this
                # chain is; it reads nothing but ESP32 state. Needed because an
                # arm refused as "already armed" records no owner, so this is
                # the only way to see what is holding it.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ready_status(host))
            elif path == "/api/adc/ready-renew":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ready_renew(host))
            elif path == "/api/adc/ready-disarm":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ready_disarm(host))
            elif path == "/api/adc/pulse-disarm":
                # Release Stream's claim on the shared detector arm — see
                # detector_disarm: only actually disarms if Record isn't
                # also using it right now.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(detector_disarm(host, "stream"))
            elif path == "/api/adc/ring-window":
                # Continuous → trigger → retrieve: arm a trigger-aligned window
                # on the running ring (optionally firing SyncOut), download it,
                # and return the samples + the trigger index (pre).
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                pre = max(0, int(body.get("pre", 256)))
                post = max(1, int(body.get("post", 2048)))
                src = str(body.get("src", "fire"))   # fire (ESP32 GP37) | gp40 (external) | now
                # gp40 blocks for an external edge, so give it (and the HTTP call) longer.
                tmo_ms = int(body.get("timeout_ms", 5000 if src == "gp40" else 1000))
                w = adc_ring_window(host, pre, post, src, tmo_ms,
                                    timeout=max(6.0, tmo_ms / 1000 + 3.0))
                if not w.get("ok"):
                    return self._json({"ok": False, "error": w.get("error", "window failed")}, HTTPStatus.OK)
                data = adc_ring_window_data(host)
                if not data.get("ok"):
                    return self._json({"ok": False, "error": data.get("error", "window download failed")}, HTTPStatus.OK)
                raw = data["bytes"]
                hdr = {k.lower(): v for k, v in data.get("headers", {}).items()}
                samples = [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw) - 1, 2)]
                self._json({"ok": True, "samples": samples, "n": len(samples),
                            "pre": int(hdr.get("x-win-pre", w.get("pre", 0)) or 0),
                            "rate_hz": int(hdr.get("x-win-rate-hz", w.get("rate_hz", 0)) or 0),
                            "seq": int(hdr.get("x-win-seq", 0) or 0),
                            "trigger_us": int(w.get("trigger_us", 0) or 0)})
            elif path == "/api/ringpulse/arm":
                # Mode-2 fire-correlated per-pulse: ensure the ring runs, connect
                # the framed esp_cmd client (3334), then RING_PULSE_ARM.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                rs = adc_ring_start(host, max(1, int(body.get("rate", 1000000))))
                if not rs.get("ok"):
                    return self._json({"ok": False, "error": f"ring: {rs.get('error') or rs.get('message')}"}, HTTPStatus.OK)
                if not ESPCMD.connect(host):
                    return self._json({"ok": False, "error": f"esp_cmd 3334: {ESPCMD.status().get('error')}"}, HTTPStatus.OK)
                r = ESPCMD.arm(int(body.get("pre", 256)), int(body.get("post", 2048)),
                               int(body.get("thresh", 100)), int(body.get("report", 1)))
                self._json(r if r.get("ok") else {"ok": False, "error": f"arm status {r.get('status')}"})
            elif path == "/api/ringpulse/disarm":
                # Disarm the ESP32's Mode-2 fire-correlated per-pulse capture
                # (RING_PULSE_DISARM over the framed esp_cmd socket, port 3334)
                # that /api/ringpulse/arm armed.
                self._json(ESPCMD.disarm())
            elif path == "/api/record/start":
                # Start the host-side recorder (MeasurementRecorder): arms the
                # STM32 detector (adc_pulse_arm, same as Per-pulse "Stream")
                # and polls /api/pulse-events into a .csv. body: {rate (Hz)}.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                rate = max(1, int(body.get("rate", 1000000)))
                self._json(RECORDER.start(host, rate))
            elif path == "/api/record/stop":
                # Stop the recorder — disarms the detector and joins the
                # pulse-event poller from record/start; response carries the
                # final pulse count and file name (fetch via GET /api/record/download).
                self._json(RECORDER.stop())
            elif path == "/api/stm32/ds3502-set":
                # Proxy to `/stm32/ds3502` POST — writes one DS3502 digital-pot
                # wiper (0-127) directly over I2C. body: {ch, wiper}. Raw/manual
                # set; /api/hv/set-v and /api/hv/set-i wrap this with LUT/linear
                # conversion from a physical unit (volts/mA) instead.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(stm32_ds3502_set(host, str(body.get("ch", "ev")), int(body.get("wiper", 0))))
            elif path == "/api/hv/set-v":
                # LUT-based voltage set: chan=emission|focus, volts=magnitude.
                # Loads the calibrated wiper→V LUT, interpolates, writes DS3502.
                # Falls back to linear approximation when no LUT is available.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                raw_chan = str(body.get("chan", "emission"))
                chan = "focus" if raw_chan.startswith("f") else "emission"
                try:
                    mag_v = abs(float(body.get("volts", 0)))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "invalid volts"}, HTTPStatus.OK)
                wiper, expect_v, method = _lut_wiper_for_v(chan, mag_v)
                r = stm32_ds3502_set(host, _HV_DS_CH[chan], wiper)
                ok = r.get("ok", False)
                out: dict = {"ok": ok, "chan": chan, "wiper": wiper,
                             "expect_v": round(expect_v, 1), "method": method}
                # Out of range: clamped, written anyway, and SAID so -- the
                # project's rule for every clamp.
                if "clamped" in method:
                    out.update(clamped=True, requested_v=round(mag_v, 1),
                               warning=f"{chan} {mag_v:g} V is outside the settable "
                                       f"range; set to {abs(expect_v):.1f} V")
                if not ok:
                    out["error"] = r.get("error") or "DS3502 write failed"
                    # Carry the REASON through, don't flatten it into a generic
                    # failure. "no_device" means the pot is absent -- a caller
                    # must not retry that, and must not read it as "the write
                    # failed", which is what it looked like before this.
                    if r.get("reason"):
                        out["reason"] = r["reason"]
                self._json(out)
            elif path == "/api/hv/set-i":
                # Linear emission-current set: ma=target mA (0–85.7).
                # DS3502 "ei" wiper is linearly proportional to current reference.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                try:
                    ma = abs(float(body.get("ma", 0)))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "invalid ma"}, HTTPStatus.OK)
                wiper = max(0, min(127, round(ma / _EM_I_FULL_MA * 127)))
                r = stm32_ds3502_set(host, "ei", wiper)
                ok = r.get("ok", False)
                out2: dict = {"ok": ok, "wiper": wiper,
                              "expect_ma": round(wiper / 127 * _EM_I_FULL_MA, 2)}
                # Out of range: clamped to full scale, written anyway, and
                # reported -- never silently.
                if ma > _EM_I_FULL_MA:
                    out2.update(clamped=True, requested_ma=round(ma, 2),
                                max_ma=_EM_I_FULL_MA,
                                warning=f"emission current {ma:g} mA exceeds the "
                                        f"{_EM_I_FULL_MA} mA maximum; set to "
                                        f"{_EM_I_FULL_MA} mA")
                if not ok:
                    out2["error"] = r.get("error") or "DS3502 write failed"
                    if r.get("reason"):
                        out2["reason"] = r["reason"]     # see set-v above
                self._json(out2)
            elif path == "/api/stm32/hv-enable":
                # Proxy to `/stm32/hv_enable` — toggles the HV enable GPIO for
                # one channel directly (body: {ch: 'emission'|'focus', on}).
                # Direct pin control, independent of the closed-loop target
                # machinery below.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                _hv_ch, _hv_on = str(body.get("ch", "emission")), bool(body.get("on"))
                # Not watched by the dead-man timer: the rails are only ever
                # turned off by a person (see SAFETY_HV_TIMEOUT_S).
                self._json(stm32_hv_enable_set(host, _hv_ch, _hv_on))
            elif path == "/api/stm32/hv-set-target":
                # Proxy to `/stm32/hv_set_target` — starts the STM32's closed
                # HV loop for one channel: it steps the DS3502 wiper by
                # max_step per iteration until the ADS1115 reading is within
                # tol counts of target. body: {chan, target (ADC counts), tol,
                # max_step}. Noted elsewhere as unreliable versus the
                # host-side LUT+direct-wiper approach POST /api/hv/set-v uses.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(stm32_hv_set_target(host, str(body.get("chan", "emission")),
                                               int(body.get("target", 0)), int(body.get("tol", 4)),
                                               int(body.get("max_step", 1))))
            elif path == "/api/stm32/hv-clear-target":
                # Proxy to `/stm32/hv_clear_target` — stops the closed HV loop
                # for a channel (body: {chan}); the wiper is left wherever it
                # last stepped to, not reset.
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(stm32_hv_clear_target(host, str(body.get("chan", "emission"))))
            elif path == "/api/hv-lut/save":
                # Persist a HV setpoint LUT (wiper→measured-V) from a Calibrate
                # sweep. body: {chan, points:[{wiper,v}], max_mag}. Writes a stable
                # file (load reads this) plus a dated archive copy.
                chan = "focus" if str(body.get("chan", "")).startswith("f") else "emission"
                pts = body.get("points") or []
                points = [{"wiper": int(p.get("wiper", 0)), "v": float(p.get("v", 0))}
                          for p in pts if isinstance(p, dict)]
                if len(points) < 2:
                    return self._json({"ok": False, "error": "need >=2 LUT points"}, HTTPStatus.OK)
                ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
                rec = {"chan": chan, "master": MASTER, "ts": ts, "points": points,
                       "max_mag": float(body.get("max_mag", 0)) or round(max(abs(p["v"]) for p in points), 1)}
                CALIB_DIR.mkdir(exist_ok=True)
                fp = _hv_lut_path(chan)
                fp.write_text(json.dumps(rec, indent=2))
                stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
                (CALIB_DIR / f"hv_lut_p{MASTER}_{chan}_{stamp}.json").write_text(json.dumps(rec, indent=2))
                self._json({"ok": True, "path": str(fp), **rec})
            elif path in ("/api/sync/config", "/api/sync/fire", "/api/sync/abort"):
                # Three ESP32-hardware-trigger sub-endpoints for ONE controller
                # (body: {controller, ...}), each proxied to its own bridge —
                # each ESP32 has its own SyncIn/SyncOut GPIO wiring:
                #   sync/config -> POST /sync/config: sets SyncOut edge/width_us,
                #     SyncIn ready_active polarity, ext_trig_edge (fields passed
                #     through as strings, whichever keys are present in body).
                #   sync/fire   -> POST /sync/fire: manually pulses SyncOut once
                #     (bench test / single-shot, distinct from the paced
                #     background train POST /api/sync/simulate drives).
                #   sync/abort  -> POST /sync/abort: cancels any pending
                #     sync-triggered capture/wait on that ESP32.
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
            elif path == "/api/sync/simulate":
                # SIMULATE SCAN: paced, background sync-pulse train over the whole
                # scan. count = the schedule's total triggers; duration_s spreads
                # them over the real scan time (interval_ms = duration_s/count).
                # Fire the head-of-chain controller (default MASTER); the RP2350
                # chain propagates it to the other power.
                cid = int(body.get("controller", MASTER))
                link = CONTROLLERS.get(cid)
                if not link or not link.host:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                count = int(body.get("count", 1))
                if count < 1:
                    return self._json(
                        {"ok": False, "error": f"count={count} must be >= 1; nothing "
                                               f"was fired. max(1, ...) used to turn a "
                                               f"miscomputed count into one real trigger."},
                        HTTPStatus.BAD_REQUEST)
                if "interval_ms" in body:
                    interval_ms = max(0.0, float(body["interval_ms"]))
                else:
                    dur = max(0.0, float(body.get("duration_s", 0)))
                    interval_ms = (dur * 1000.0 / count) if (dur > 0 and count > 0) else 0.0
                with _SIM_LOCK:
                    if _SIM_STATE["running"]:
                        return self._json({"ok": False, "error": "simulation already running"}, HTTPStatus.OK)
                    # Claim it ATOMICALLY here (not inside the thread) so two near-
                    # simultaneous starts can't both pass the guard and double-fire.
                    _SIM_STATE.update(running=True, fired=0, count=count, stop=False, controller=cid)
                # Arm the run recorder with the scheduled filament set so the report
                # can flag any that were scheduled but never reached ACTIVE.
                RUN_RECORDER.start(expect=body.get("expect"),
                                   active_mA=int(body.get("active_mA", 2900)))
                # Turn on the firmware's CACHED telemetry PUSH (~20 fps, no I2C) on
                # every connected controller so the live view streams during the scan.
                for c2, l2 in CONTROLLERS.items():
                    if l2.client.connected:
                        set_scan_telemetry(l2, c2 - 1, True)
                threading.Thread(target=_run_scan_sim, args=(link.host, count, interval_ms, cid),
                                 daemon=True).start()
                self._json({"ok": True, "count": count, "interval_ms": interval_ms, "controller": cid})
            elif path == "/api/sync/simulate-stop":
                # Signal the background scan-simulation thread (started by
                # POST /api/sync/simulate) to stop after its current
                # iteration — sets _SIM_STATE.stop; the thread checks this
                # flag between paced sync/fire calls, it does not abort mid-pulse.
                with _SIM_LOCK:
                    _SIM_STATE["stop"] = True
                self._json({"ok": True})
            elif path == "/api/shv":
                # Single entry point for every Simple-HV-schedule engine
                # opcode (SHV_* 0x70-0x82: active-list push/read, table
                # clear/set/info, config set/get, arm/disarm, status, pulse
                # log, capability timing test, heat-table clear/set/info,
                # fault policy, trigger delay) on ONE controller. body:
                # {controller, op, ...op-specific fields} — see shv_op() for
                # the exact wire format and response shape per op.
                if body.get("op") == "trigger_delay":
                    # Rig-wide by design (trigger_delay_all): it must not depend
                    # on which controller the caller happened to name, or on
                    # that one being connected.
                    return self._json(trigger_delay_all(
                        int(body["delay_us"]) if "delay_us" in body else None))
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(shv_op(link, body))
            elif path == "/api/hv-diag165":
                # Raw 165 readback diagnostic (HvDiag165 0x7F).
                # body: {controller, channel, test_byte, settle_ms}
                # response: {ok, channel, test_byte, r0, r1, r2, r3, status}
                cid = int(body.get("controller", 1))
                link = CONTROLLERS.get(cid)
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                channel = int(body.get("channel", 0))
                test_byte = int(body.get("test_byte", 0x55))
                settle_ms = int(body.get("settle_ms", 5))
                settle_ms = max(0, min(settle_ms, 100))
                payload = bytes([channel & 0xFF, test_byte & 0xFF, settle_ms & 0xFF])
                raw = link.request(0x7F, payload, flags=0, timeout=2.0 + settle_ms * 4 / 1000).get("raw") or []
                if not raw or raw[0] != 0x00:
                    status_byte = raw[0] if raw else 0xFF
                    # A bare status byte is not an error message: the caller
                    # gets ok=False and no way to know what went wrong short of
                    # reading the firmware's status table.
                    return self._json(
                        {"ok": False, "status": status_byte, "raw": raw,
                         "error": f"ChReadHvDiag165 rejected with status "
                                  f"{status_byte} "
                                  f"({UART_STATUS_NAMES.get(status_byte, 'unknown')})"
                                  f" — check the channel is 0..7"},
                        HTTPStatus.OK)
                self._json({"ok": True, "status": 0,
                            "channel": raw[1] if len(raw) > 1 else channel,
                            "test_byte": raw[2] if len(raw) > 2 else test_byte,
                            "r0": raw[3] if len(raw) > 3 else None,
                            "r1": raw[4] if len(raw) > 4 else None,
                            "r2": raw[5] if len(raw) > 5 else None,
                            "r3": raw[6] if len(raw) > 6 else None})
            elif path == "/api/hv-shift-hz":
                # SET-only (HvSetShiftHz 0x80): the 165-readback bit-bang SCK
                # frequency, for signal-integrity testing on long cables.
                # There is no separate "get" request — the firmware always
                # requires a 4-byte set and echoes back the ACTUAL frequency
                # in effect (clamped 100 Hz-2 MHz), which may differ slightly
                # from what was requested. Survives until next reboot.
                # body: {controller, hz}
                cid = int(body.get("controller", MASTER))
                link = CONTROLLERS.get(cid)
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": f"controller {cid} not connected"}, HTTPStatus.OK)
                try:
                    hz = int(body["hz"])
                except (KeyError, TypeError, ValueError):
                    return self._json({"ok": False, "error": "hz required"}, HTTPStatus.OK)
                if not (100 <= int(hz) <= 2_000_000):
                    # The firmware CLAMPS to 100 Hz..2 MHz and echoes what it
                    # actually installed; asking for 99999999 came back ok=True
                    # at 2 MHz with nothing saying the request was not honoured.
                    return self._json(
                        {"ok": False, "error": f"hz {hz} out of range 100..2000000 "
                                               f"(the firmware clamps silently, so "
                                               f"this is refused instead)"},
                        HTTPStatus.BAD_REQUEST)
                try:
                    resp = link.client.send_request(HV_SET_SHIFT_HZ, _u32(hz), flags=0, timeout=2.0)
                except Exception as exc:
                    return self._json({"ok": False, "error": str(exc)}, HTTPStatus.OK)
                raw = resp.get("raw") if isinstance(resp, dict) else None
                ok = bool(raw) and raw[0] == 0 and len(raw) >= 5
                self._json({"ok": ok, "controller": cid,
                            "actualHz": _le(raw, 1, 4) if ok else None})
            elif path == "/api/schedule":
                # Stage the scan schedule. For now we just validate + retain it;
                # streaming it to the RP2350B schedule table (0x70-0x7B) lands
                # once a controller is connected.
                rows = body.get("rows", [])
                if not rows:
                    return self._json(
                        {"ok": False, "error": "no rows to stage; staging an empty "
                                               "schedule and reporting success hides "
                                               "the empty build"},
                        HTTPStatus.BAD_REQUEST)
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
    def _client(self, body: dict[str, Any] | None = None) -> str:
        """Who is calling. Programs identify themselves with an `X-CT-Client`
        header (or a `client` field in the body) so the lease can tell them
        apart; anything anonymous is identified by its address, which is enough
        for /api/clients and for a lease taken by an ad-hoc script."""
        cid = self.headers.get("X-CT-Client") or (body or {}).get("client")
        cid = str(cid).strip() if cid else ""
        return cid[:64] if cid else f"anon@{self.client_address[0]}"

    def _lease_guard(self, path: str, body: dict[str, Any], client: str):
        """None when this POST may proceed, else the blocking lease snapshot.
        Only WRITES are gated: every read (is_read_post) passes, as does
        anything that never touches the link (_UNGATED_POSTS)."""
        if path in _UNGATED_POSTS or is_read_post(path, body):
            return None
        held = _lease_blocking(client)
        if held is not None and is_deenergising_post(path, body):
            # Let through, and say so in the audit line: the holder's run was
            # interrupted by someone else, and that must be findable.
            self._audit_note = f"de-energising: let through the lease held by {held['owner']}"
            return None
        return held

    def _cors_headers(self) -> None:
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-CT-Client")
        self.send_header("Access-Control-Max-Age", "600")

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

    def _master_link(self):
        return CONTROLLERS.get(MASTER)

    def _master_host(self):
        link = self._master_link()
        if not link or not link.client.connected or not link.host:
            return None, f"master (Power {MASTER}) not connected"
        return link.host, None

    def _read_json(self) -> dict[str, Any]:
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n) if n else b""
            body = json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            body = {}
        self._audit_body = body
        return body

    def _json(self, obj, status: HTTPStatus = HTTPStatus.OK) -> None:
        self._audit_resp = obj
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
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
    # Bind all interfaces by default: this process owns the single-client bridge
    # sockets, so every other program on the bench reaches the hardware through
    # this API. Set CT_GUI_HOST=127.0.0.1 to keep it to this machine.
    # Self-update from GitHub before binding the port (see ct_update.py): a
    # clone that is behind fast-forwards and the backend restarts on the new
    # code. Start-up only -- a running backend is never restarted by this.
    from ct import update as ct_update
    ct_update.check_and_update()
    _BACKEND_VERSION.update(ct_update.version())
    host = os.environ.get("CT_GUI_HOST", "0.0.0.0")
    port = int(os.environ.get("CT_GUI_PORT", "8770"))
    _setup_logging()
    log.info("=== backend start — listening on http://%s:%d ===", host, port)
    # The dead-man watchdog starts with the server and outlives every client.
    # Daemon: it must never hold the process open, and it has no state worth
    # draining on the way out.
    threading.Thread(target=_safety_loop, name="safety_watchdog", daemon=True).start()
    # One reader per controller for the ring and the matrix (see MONITOR_*).
    threading.Thread(target=_board_monitor_loop, name="board_monitor", daemon=True).start()
    log.info("safety watchdog: ACTIVE -> %s after %.0fs, grid MOSFETs opened after %.0fs "
             "without an HV grid command (rails never touched; commands renew, reads do not)",
             power_state_name(SAFETY_ACTIVE_FALLBACK), SAFETY_ACTIVE_TIMEOUT_S,
             SAFETY_HV_TIMEOUT_S)
    server = ThreadingHTTPServer((host, port), CtHandler)
    print(f"CT GUI server listening on http://{host}:{port}  (log: {LOG_DIR}/backend.log)")
    if host == "0.0.0.0":
        lan = primary_local_ip()
        print(f"  open http://127.0.0.1:{port}" + (f" · shared API on http://{lan}:{port}" if lan else ""))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for c in CONTROLLERS.values():
            c.disconnect()
        # A clean stop is itself worth recording: it is what tells you, later,
        # that a gap in the log was an operator stopping the service and not a
        # crash.
        log.info("=== backend stop ===")


if __name__ == "__main__":
    main()
