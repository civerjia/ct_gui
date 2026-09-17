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

from net_protocol import (
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

# ---------------------------------------------------------------------------
# Command frame builder — matches RP2350bFilamentController/docs/power_state_and_cc.md
# (the firmware protocol is ahead of the WiFi GUI's net_protocol, so we build
# these payloads here rather than via build_command_payload).
# ---------------------------------------------------------------------------
FLAG_SINGLE = 0x10  # kTargetIsSingleBoard


def _u16(v: int) -> bytes:
    return int(v).to_bytes(2, "little")


# ---------------------------------------------------------------------------
# Logging. There was none: everything went to stdout and died with the terminal,
# so a fault that happened overnight -- or a 40 kV arc that reset the STM32 while
# nobody was watching -- left no record at all. File-backed now, with the console
# output preserved so nothing that used to be visible stops being visible.
LOG_DIR = Path(__file__).resolve().parent / "logs"
log = logging.getLogger("ct_gui")


def _setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / "backend.log", maxBytes=4_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    log.propagate = False


def build_payload(command: str, b: dict):
    """Return (frame_type, flags, payload) for a single-board command."""
    ch = int(b.get("channel", 0)) & 0xFF
    mux = int(b.get("mux_port", 0)) & 0xFF
    if command == "CH_SET_POWER_STATE":      # 0x35
        st = int(b["state"]) & 0xFF
        arg = _u16(int(b.get("arg", 0)))
        # Multi-board form [mask0..7, state, arg16] (flags=0) — the firmware loops
        # the mask internally, so a whole batch is ONE command / one round-trip
        # instead of one per board. Single-board [ch,mux,state,arg16] otherwise.
        if b.get("board_mask") is not None:
            mask = bytes((int(x) & 0xFF) for x in list(b["board_mask"])[:8])
            mask = mask + bytes(8 - len(mask))          # pad to 8
            return 0x35, 0, mask + bytes([st]) + arg
        return 0x35, FLAG_SINGLE, bytes([ch, mux, st]) + arg
    if command == "CH_GET_POWER_STATE":      # 0x36: ch,mux -> status,ch,mux,state,faultKind
        return 0x36, FLAG_SINGLE, bytes([ch, mux])
    if command == "CH_STARTUP_OCP":          # 0x37: get(empty) / set(mA16) startup OCP floor
        return (0x37, 0, _u16(int(b["threshold_mA"]))) if b.get("set") else (0x37, 0, b"")
    if command == "CH_SET_TPS_VOLTAGE":      # 0x22: ch,mux,mV16,enable
        return 0x22, FLAG_SINGLE, bytes([ch, mux]) + _u16(int(b["millivolts"])) + bytes([1 if b.get("enable_after_set", True) else 0])
    if command == "CH_SET_TPS_OCP_THRESHOLD":  # 0x28: ch,mux,mA16 (direct IOUT_LIMIT)
        return 0x28, FLAG_SINGLE, bytes([ch, mux]) + _u16(int(b["threshold_mA"]))
    if command == "CH_GET_TPS_STATUS":       # 0x23 mask form: mask[8] -> status +
        # seven per-board bitmaps. Payload 57 bytes:
        #   status@0, targeted@1, present@9, enabled@17, fault@25,
        #   hv_oc@33, valid@41, struggling@49   (8 bytes each after status)
        # struggling is the one that matters here: set after 3 consecutive
        # failed revives, cleared the instant the output comes back. It is the
        # only signal that distinguishes a SHORT, which never sets the CC mode's
        # fault bits (see wait_for_current).
        mask = bytes((int(x) & 0xFF) for x in list(b.get("board_mask", [0xFF] * 8))[:8])
        return 0x23, 0, mask + bytes(8 - len(mask))
    if command == "CH_GET_INA219":           # 0x24: ch,mux -> status,ch,mux,present,busMv16,mA16
        return 0x24, FLAG_SINGLE, bytes([ch, mux])
    if command == "CH_GET_CACHED_CURRENTS":  # 0x3A single form: ch,mux ->
        # status,ch,mux,mode,measured16,target16. Use this for ONE filament only
        # (e.g. wait_for_current's poll): it's one tiny frame instead of the
        # paged all-board sweep. For MANY boards always use the bulk/paged
        # read_cached_currents_by_board — never loop this per board, that floods
        # the one shared bridge link and starves the CC loop.
        return 0x3A, FLAG_SINGLE, bytes([ch, mux])
    if command == "HV_GET_ALL_BYTES":        # 0x13: desired[8]+feedback[8]
        return 0x13, 0, b""
    if command == "HV_SET_BIT":              # 0x10: ch,bit,value,verifyMode
        mode = 2 if b.get("force") else (1 if b.get("verify", True) else 0)
        return 0x10, 0, bytes([ch, int(b["bit"]) & 0xFF, 1 if b.get("value") else 0, mode])
    if command == "HV_SET_MULTI_CHANNEL":    # 0x15: channelMask, values[8], writeMode
        # -> status, appliedMask, verifiedMask, failedMask, desired[8], feedback[8]
        mode = 2 if b.get("force") else (1 if b.get("verify", True) else 0)
        chmask = int(b.get("channel_mask", 0)) & 0xFF
        values = bytes((int(x) & 0xFF) for x in list(b.get("values") or [0] * 8)[:8])
        values = values + bytes(8 - len(values))
        return 0x15, 0, bytes([chmask]) + values + bytes([mode])
    if command == "HV_PULSE":                # 0x16: ch,bit,width_us32,verifyMode
        return 0x16, FLAG_SINGLE, bytes([ch, int(b.get("bit", mux)) & 0xFF]) + int(b.get("width_us", 0)).to_bytes(4, "little") + bytes([int(b.get("verify_mode", 0)) & 0xFF])
    raise ValueError(f"unknown command {command}")


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
FILAMENTS_PER_CONTROLLER = 48     # nominal (alt-12 default: 4 groups of 12 / controller)
SCOPE_PER_CONTROLLER = 64         # power slots per controller (8 channels x 8 positions)
FILAMENT_COUNT = 96               # global filament indices 0..95
POWER_SLOTS = 64                  # firmware kSimpleHvFilamentsPerController
NO_FILAMENT = 0xFF                # active-list "unused power slot" sentinel
DEFAULT_GROUP_SIZE = 12           # alternating-12: 0-11->P1, 12-23->P2, 24-35->P1, ...
DEFAULT_CHANNELS = [0, 1, 2, 3, 4, 5]   # legacy default (channels now derived from MAPPING)


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "little")


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


# Bound-schedule / config opcodes (firmware ahead of net_protocol; built here).
SHV_SET_ACTIVE_LIST = 0x70   # payload = 64 bytes: power slot k -> global filament (0xFF unused)
SHV_GET_ACTIVE_LIST = 0x71   # -> OK + 64-byte power->filament map
SHV_CLEAR_TABLE = 0x72
SHV_SET_ENTRIES = 0x73
SHV_GET_TABLE_INFO = 0x74
SHV_SET_CONFIG = 0x75
SHV_GET_CONFIG = 0x76
SHV_ARM = 0x77
SHV_DISARM = 0x78
SHV_GET_STATUS = 0x79
SHV_GET_PULSE_LOG = 0x7A
SHV_CAPABILITY = 0x7B
SHV_HEAT_CLEAR = 0x7C
SHV_HEAT_SET_ENTRIES = 0x7D
SHV_HEAT_GET_INFO = 0x7E      # -> OK + u16 heatCount + u16 maxHeatEntries
SHV_FAULT_POLICY = 0x81       # GET/SET board+mismatch policies; response has faulted-slots bitmask
SHV_TRIGGER_DELAY = 0x82      # GET/SET SyncIn trigger delay (µs); response reports whether it applies
HV_SET_SHIFT_HZ = 0x80        # SET-only: 165-readback bit-bang SCK frequency (Hz), clamped 100-2e6
CH_FILAMENT_CURRENTS = 0x39
CH_SET_POWER_STATE = 0x35        # ch,mux,state,arg16 (Idle/Active→mA, Voltage→mV)
CH_SET_I2C_ENABLE_MASK = 0x34
CH_GET_INA219 = 0x24
HV_REFRESH_FEEDBACK = 0x14      # REALLY re-reads the 74HC165 (payload 0xFF = all 8
                                 # channels in one frame). 0x13 HV_GET_ALL_BYTES is a
                                 # CACHE COPY -- never use it to confirm a read-back.
CH_GET_CACHED_CURRENTS = 0x3A    # CC-loop cached currents, NO I2C (run-safe telemetry)
CH_GET_PRESENT = 0x25            # I2C presence scan (mux/tps/ina/io per board)
CH_GET_DIAGNOSIS = 0x2E         # deep diagnosis: addr-ACK / reg-read / operational
CH_GET_I2C_ENABLE_MASK = 0x2F   # read the channel enable mask
CH_RESET_MUX = 0x5F             # pulse TCA9548A reset + re-detect (power-cutting)
CH_TCA9554_SELF_TEST = 0x60     # per-pin TCA9554 toggle test
CH_READ_TCA9554 = 0x61          # read-only TCA9554 Config/Input/Output dump
ALL_BOARDS_MASK = bytes([0xFF] * 8)

# TPS55289 IOUT_LIMIT register (tps55289_registers.h kIoutLimitAddr) — used to
# read back CH_SET_TPS_OCP_THRESHOLD (0x28), which is SET-only in firmware, via
# CH_READ_TPS_REGISTER (0x29). Decode matches the firmware's own SET-side
# encoding (tps55289.cpp setOcpThresholdAmps/Millivolts): mA -> mV
# (= mA * kOcpSenseResistorOhms) -> code (= round(mV / 0.5 mV LSB)), register =
# kIoutLimitEnable(0x80) | (code & kIoutLimitMask(0x7F)).
_TPS_IOUT_LIMIT_REG = 0x02
_OCP_SENSE_RESISTOR_OHMS = 0.015   # tps55289_board_constants::kOcpSenseResistorOhms
_OCP_MA_PER_CODE = 0.5 / _OCP_SENSE_RESISTOR_OHMS   # ≈ 33.33 mA/code
_DIAG_CHIPS = ["mux", "tps", "ina", "enable_io", "fault_io", "iso_io", "hv_io"]


def _popcount(mask_list) -> int:
    return sum(bin(b & 0xFF).count("1") for b in (mask_list or []))


# All I2C diagnostics mirror the WiFi GUI's tested backend: check the response
# status byte and SURFACE the firmware status name on any non-Ok reply, rather
# than silently returning zeros (which read as "didn't work").
def _status_err(resp, name: str):
    if resp is None:
        return f"{name}: no response"
    if resp.get("status_code") != 0x00:
        return f"{name} returned {resp.get('status')}"
    return None


def read_channel_mask(link: "ControllerLink"):
    """Read the controller's I2C channel enable mask (which channels are used)."""
    try:
        resp = link.client.send_request(CH_GET_I2C_ENABLE_MASK, b"", timeout=1.5)
        if resp.get("status_code") != 0x00:
            return None
        return (resp.get("decoded") or {}).get("mask")
    except Exception:
        return None


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


CH_GET_BOARD_BITMAPS = 0x26     # iso/tps enable + tps fault + hv overcurrent masks


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


# Does this firmware flag a FAILED single-board 0x3A sample, or return a bare 0?
# Keyed by controller; None = not probed yet. See _single_read_is_trusted().
_SINGLE_0X3A_TRUSTED: dict[int, bool] = {}


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


# ---- PUSHED telemetry (firmware -> host, no request) -------------------------
# During a scan the firmware PUSHES EVENT_TELEMETRY frames (cached currents, no
# I2C) every ~50 ms. The host just receives them (client._events) and reads the
# latest — no request round-trip, so the live view can update at ~20 fps.
SET_EVENT_CONFIG = 0x06
EVENT_TELEMETRY_ENABLE_BIT = 0x08     # kEventEnableTelemetry (1<<3)
TELEMETRY_MODE_CACHED = 2             # firmware kTelemetryModeCached
SCAN_TELEMETRY_PERIOD_MS = 50         # ~20 fps push cadence
_LIVE_PUSH: set = set()               # controllers (1-based) with the push enabled
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

SHV_EMIT_CHUNK = 64    # emission entries per frame (64*4+3 = 259 B). Bigger chunks
                       # didn't help — the bottleneck is RP2350 per-frame service
                       # latency, not frame count (it processes a bigger frame
                       # proportionally slower while busy with I2C).
SHV_HEAT_CHUNK = 56    # firmware caps ShvHeatSetEntries at 56


def _status_ok(resp) -> bool:
    raw = resp.get("raw") if isinstance(resp, dict) else None
    return bool(raw) and raw[0] == 0x00


def _pipeline_reliable(link, reqs, window=8, timeout=2.5, retries=2, on_progress=None):
    """Pipeline `reqs` (fast when the link is fast), then SERIALLY retry any frame
    that failed/timed out. On a slow or variable link send_pipeline silently drops
    frames (per-frame deadlines expire while the ESP32 serializes the burst); the
    single-request path is reliable, so failed slots are re-sent one at a time with
    a longer timeout. Returns decoded responses in request order."""
    results = link.client.send_pipeline(reqs, window=window, timeout=timeout, on_progress=on_progress)
    for _ in range(max(0, retries)):
        bad = [i for i, r in enumerate(results) if not (isinstance(r, dict) and _status_ok(r))]
        if not bad:
            break
        for i in bad:
            ft, payload, flags = reqs[i]
            try:
                results[i] = link.client.send_request(ft, payload, flags=flags, timeout=3.0)
            except Exception as exc:
                results[i] = {"ok": False, "error": str(exc)}
    return results


def _le(raw, off, n):
    return sum(raw[off + i] << (8 * i) for i in range(n))


def _unpack_spi_shot(raw: bytes, n: int, bits: int) -> list[int]:
    """Decode a STM32 SPI-shot payload into raw ADC samples. 12-bit is packed
    3 bytes / 2 samples (must match adc_spi.cpp pack_pair); 16-bit is u16 LE."""
    if bits == 12:
        out: list[int] = []
        bi = 0
        while len(out) + 1 < n and bi + 2 < len(raw):
            b0, b1, b2 = raw[bi], raw[bi + 1], raw[bi + 2]
            out.append(b0 | ((b1 & 0x0F) << 8))
            out.append((b1 >> 4) | (b2 << 4))
            bi += 3
        if len(out) < n and bi + 1 < len(raw):
            out.append(raw[bi] | ((raw[bi + 1] & 0x0F) << 8))
        return out
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, len(raw) - 1, 2)]


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
                if off + 12 > len(raw):
                    break
                recs.append({"filament": raw[off], "flags": raw[off + 1], "seq": _le(raw, off + 2, 2),
                             "tOnUs": _le(raw, off + 4, 4), "durationUs": _le(raw, off + 8, 2)})
                off += 12
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
        # GET or SET the SyncIn->fire trigger delay (µs). Request body:
        # delay_us (optional). Response: {ok, delayUs, applies} -- "applies"
        # is False when the live fire path (e.g. PIO precision mode) can't
        # honour the delay, so a set can't silently do nothing.
        payload = _u16(int(body["delay_us"])) if "delay_us" in body else b""
        raw = link.request(SHV_TRIGGER_DELAY, payload).get("raw") or []
        # response: [status, us_lo, us_hi, applies] = 4 bytes
        if raw and raw[0] == 0 and len(raw) >= 4:
            return {"ok": True, "delayUs": _le(raw, 1, 2), "applies": bool(raw[3])}
        return {"ok": False}
    return {"ok": False, "error": "unknown op"}


def decode_shv_status(resp) -> dict[str, Any] | None:
    """Decode a ShvGetStatus (0x79) response payload."""
    raw = resp.get("raw") if isinstance(resp, dict) else None
    if not raw or len(raw) < 21 or raw[0] != 0x00:
        return None
    p = bytes(raw)
    le16 = lambda o: p[o] | (p[o + 1] << 8)
    le32 = lambda o: p[o] | (p[o + 1] << 8) | (p[o + 2] << 16) | (p[o + 3] << 24)
    return {
        "state": p[1],
        "stopReason": p[2],
        "entryIndex": le16(3),
        "filamentIndex": p[5],            # current GLOBAL filament (0xFF = none)
        "pulsesThisFilament": p[6],
        "entryCount": le16(7),
        "totalPulsesTarget": le32(9),
        "totalPulsesDone": le32(13),      # the global playhead cursor
        "elapsedMs": le32(17),
        "faultFilament": p[21] if len(p) > 21 else 0xFF,
        # Appended firing-path capability byte (older RP2350B firmware omits
        # it -- treat missing as unknown, not "precision off"). bit0 matters
        # most for anything watching kReadyOut (ready_relay's hardware-synced
        # ADC trigger): that envelope is produced ONLY in PIO precision mode,
        # and precision silently falls back to bit-bang if PIO failed to
        # init at boot or `pio off` was issued -- a run then looks completely
        # normal from shv_status while the envelope never appears, AND
        # (for a multi-pulse entry) the schedule needs one external SyncIn
        # trigger PER pulse instead of auto-firing the whole entry from one.
        "capabilityFlags": p[22] if len(p) > 22 else None,
        "precisionMode": bool(p[22] & 0x01) if len(p) > 22 else None,
        "pulseEnvelopeAvailable": bool(p[22] & 0x02) if len(p) > 22 else None,
        "shvTriggerDelayApplies": bool(p[22] & 0x04) if len(p) > 22 else None,
        "shvTriggerDelayNonzero": bool(p[22] & 0x08) if len(p) > 22 else None,
        # Appended re-stage/trigger diagnostics (RP2350 firmware 2197472+;
        # older firmware's shorter response leaves these None, not 0 -- don't
        # conflate "not reported" with "reported zero"). See uncounted's
        # semantics specifically: it distinguishes "the PIO fired the pulse
        # but the GPIO edge-count ISR missed it" (uncounted>0, bookkeeping
        # bug) from "the trigger edge genuinely never arrived" (uncounted==0,
        # edges short of target) -- these look identical from totalPulsesDone
        # alone and need this field to tell apart.
        "triggerEdges": le32(23) if len(p) >= 27 else None,
        "uncounted": le32(27) if len(p) >= 31 else None,
        "underfed": le32(31) if len(p) >= 35 else None,
        "mismatches": le32(35) if len(p) >= 39 else None,
        # rbSaturated (firmware 7528a75+): times the 165 read-back FIFO was
        # found full when a pulse was accounted. NOT independent of
        # uncounted -- a discarded read-back makes the FIFO level understate
        # how many pulses fired, so when rbSaturated>0 the recovery
        # under-credits. Per the RP2350 session: uncounted is exact only
        # when rbSaturated==0; otherwise treat it as a LOWER BOUND, not a
        # count.
        "rbSaturated": le32(39) if len(p) >= 43 else None,
    }

STATIC_DIR = Path(__file__).resolve().parent / "static"
CALIB_DIR = Path(__file__).resolve().parent / "calibration"   # emission-current calibration records
STATE_DIR = Path(__file__).resolve().parent / "state"         # operator decisions that must outlive a restart


# ── Dead filaments ───────────────────────────────────────────────────────────
# "dead" means THIS FILAMENT MUST NOT BE ENERGISED. The board it sits on may be
# perfectly fine; the filament is the thing that is faulty. It is an operator
# decision, never an inference -- in particular it is NOT presence: a board
# going offline and coming back says nothing about whether its filament is
# usable, so nothing here ever clears an entry automatically.
#
# It lives here rather than in the client for two reasons, and the second is
# the real one:
#   1. a client-side set dies with the script, so the next script energises a
#      filament someone already determined was bad;
#   2. a client-side set is only enforced by clients that bother to. The GUI, a
#      curl, someone else's script -- all could energise a dead filament, and
#      backend.py would carry it out. Storage here is convenience; ENFORCEMENT
#      here is the point.
#
# Stored in FID space (the canonical 0..95 the firmware agrees on), never in a
# client's own numbering: a faulty filament is faulty regardless of any
# per-script remapping, and a set stored in one script's numbering would mean
# something different to the next. Clients cross at their own boundary.
DEAD_STATE_PATH = STATE_DIR / "dead_fids.json"
_DEAD_LOCK = threading.Lock()
# fid -> {"reason": str, "by": str, "at": iso8601}. Provenance is not decoration:
# an entry that can never expire and blocks energising needs to say why, or in
# three months nobody knows why 55 is off and nobody dares clear it.
DEAD_FIDS: dict[int, dict] = {}

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
ENERGISING_STATES = frozenset({3, 4, 5, 6})   # STANDBY, IDLE, ACTIVE, VOLTAGE

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
POWER_STATE_ACTIVE = 5
POWER_STATE_IDLE = 4
# fid -> (state, monotonic when it was commanded). The backend is the ONLY
# writer to the bridge (single-client TCP), so what it last commanded is what
# the hardware has -- except across a reconnect, where the board may have been
# reflashed and reset to STOP. Cleared there, and an UNKNOWN state refuses
# ACTIVE rather than allowing it: not knowing must not read as permission.
LAST_POWER_STATE: dict[int, tuple[int, float]] = {}

# ── ACTIVE current floor ─────────────────────────────────────────────────────
# ACTIVE below the IDLE operating current is refused. The firmware clamps IDLE
# at 2 A but deliberately does NOT clamp ACTIVE, so this is the only guard on
# that direction, and the RP2350 side asked for it to live here.
#
# 1500 mA is the IDLE operating current this bench runs at, per the user. It is
# not a constant read out of the firmware -- there is none -- so if the idle
# operating point changes, change this with it.
ACTIVE_FLOOR_MA = 1500

# Schedules may not select Voltage mode: ShvHeatSetEntries rejects state 6 with
# BadArgument and refuses the whole batch. Caught here first so the caller is
# told WHICH entry is wrong instead of getting a batch-level reject. Direct
# board control (CH_SET_POWER_STATE via /api/cmd) may still use Voltage -- it is
# a bench/calibration mode, and only SCHEDULES are restricted.
POWER_STATE_VOLTAGE = 6


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
    return problems


def note_power_state(fids, state: int) -> None:
    now = time.monotonic()
    for f in fids:
        LAST_POWER_STATE[int(f)] = (int(state), now)


def ladder_blocks_active(fid: int) -> str | None:
    """None if ACTIVE is allowed for this filament, else why not."""
    known = LAST_POWER_STATE.get(int(fid))
    if known is None:
        return ("power state unknown to this backend (no state commanded since "
                "connect, or the controller reconnected) — run the ladder "
                "STOP→SLEEP→STANDBY→IDLE first")
    st, when = known
    if st in (POWER_STATE_IDLE, POWER_STATE_ACTIVE):
        return None
    return (f"currently at power state {st}; ACTIVE may only be entered from "
            f"IDLE(4) — going straight to firing current damages the filament, "
            f"and in vacuum that is unrepairable")


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
_HV_FULL_V: dict[str, float] = {"emission": 350.0, "focus": 495.0}
_HV_DS_CH:  dict[str, str]   = {"emission": "ev",  "focus": "fv"}
_EM_I_FULL_MA = 85.7   # mA at wiper 127 on the "ei" DS3502 channel


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
        if T <= mono[0]["m"]:
            return mono[0]["w"], sign * mono[0]["m"], "lut"
        if T >= mono[-1]["m"]:
            return mono[-1]["w"], sign * mono[-1]["m"], "lut(clamped)"
        for i in range(len(mono) - 1):
            a, b = mono[i], mono[i + 1]
            if a["m"] <= T <= b["m"]:
                f = 0.0 if b["m"] == a["m"] else (T - a["m"]) / (b["m"] - a["m"])
                w = round(a["w"] + f * (b["w"] - a["w"]))
                return w, sign * T, "lut"
    full = _HV_FULL_V.get(chan, 350.0)
    w = max(0, min(127, round(abs(mag_v) / full * 127)))
    return w, -abs(mag_v), "linear(no-lut)"
PING_TYPE = 0x01
PING_PAYLOAD = (0xCAFEF00D).to_bytes(4, "little")
POLL_PAUSE_MAX_S = 15.0   # max time a background-PING pause survives without a re-arm

GEOMETRY = {
    "n_filaments": 96,
    "source_diameter_mm": 436,
    "detector_diameter_mm": 308,
    "collimator_coverage": 35,
    "detector_pixels": 256,
    "detector_pixel_mm": 0.1,
    "gantry_max_deg": 10,
    "filament0_axis": "y+",
    "controllers": 2,
    "channels_used": 6,
    "boards_per_channel": 8,
}



class ControllerLink:
    """One ESP32 bridge + its RP2350B/STM32 liveness."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.client = TcpProtocolClient()
        self.host: str | None = None
        self._running = False
        # Suppress the PING during exclusive bench ops. A DEADLINE (monotonic),
        # not a sticky bool: if the GUI never sends "resume" (page reload /
        # navigation mid-test), the pause auto-expires so the heartbeat can never
        # be killed permanently. The GUI re-arms it while a test is actually running.
        self._poll_pause_until = 0.0
        self._poll_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.rp_last = 0.0                       # unix time of last good PING
        self.rp_rtt_ms: float | None = None
        self.stm: dict[str, Any] = {}
        self.bridge_name: str | None = None       # ESP32 AP SSID (MAC-derived identity)
        self._last_stm_uptime: int | None = None  # for restart detection, see _note_stm_reset
        self._stm_resets = 0

    def connect(self, host: str) -> None:
        with self._lock:
            self._stop_poll()
            self.client.connect(host, BRIDGE_PORT)
            self.host = host
            self._poll_pause_until = 0.0             # a fresh connection always polls
            self.rp_last = 0.0
            self.rp_rtt_ms = None
            self.stm = {}
            self.bridge_name = None
            self._running = True
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()
        # Forget any cached firmware-capability probe for this controller: a
        # reconnect is exactly what a reflash looks like from here, and the probe
        # result is a property of the FIRMWARE, not of the host. Without this a
        # backend that probed an old firmware stays on the slow (paged) path for
        # its whole lifetime even after the RP2350 is flashed -- conservative, but
        # it silently never gives the fast path back. VERIFIED 2026-09-16: the
        # probe did cache False against pre-5bff25c firmware and needed a restart.
        for _cid, _lnk in list(CONTROLLERS.items()):
            if _lnk is self:
                _SINGLE_0X3A_TRUSTED.pop(_cid - 1, None)
                break
        else:                                  # not registered yet (startup): clear all
            _SINGLE_0X3A_TRUSTED.clear()
        self._read_identity(host)

    def _read_identity(self, host: str) -> None:
        """Read the ESP32 AP SSID so two controllers can be told apart."""
        try:
            info = fetch_bridge_info(host)
            self.bridge_name = info.get("ap_ssid") if info else None
        except Exception:
            self.bridge_name = None

    def disconnect(self) -> None:
        with self._lock:
            self._stop_poll()
            with _suppress():
                self.client.disconnect()
            self.host = None

    def request(self, frame_type: int, payload: bytes = b"", flags: int = 0, timeout: float = 2.0):
        """Send one framed command and return the decoded response (raises if down)."""
        if not self.client.connected:
            raise RuntimeError(f"{self.name} not connected")
        return self.client.send_request(frame_type, payload, flags=flags, timeout=timeout)

    def _stop_poll(self) -> None:
        self._running = False
        t = self._poll_thread
        self._poll_thread = None
        if t and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=0.4)

    def set_poll_paused(self, paused: bool) -> None:
        """Pause/resume the background PING. Held by exclusive bench ops (e.g. the
        HV switch toggle test) so their per-command round-trips don't queue behind
        the 1 Hz PING on the shared bridge socket / request lock. Pausing arms a
        short deadline (POLL_PAUSE_MAX_S) that the caller re-arms while its op runs;
        if the caller dies the pause auto-expires, so the heartbeat always returns."""
        self._poll_pause_until = (time.monotonic() + POLL_PAUSE_MAX_S) if paused else 0.0

    def _poll(self) -> None:
        # Loop on _running (NOT connected): the ESP32 bridge is single-client with
        # a ~6s TCP keepalive, so a transient WiFi/CPU stall, an STM32 reboot, or
        # heavy HTTP polling starving the bridge task makes it stop() the socket.
        # Without in-place reconnect the link stays down until a manual Scan &
        # Connect ("master frequently loses connection"). self.host is cleared only
        # by an explicit disconnect(), so we auto-heal on drops but stay down when
        # the user really meant to disconnect.
        reconnecting = False
        while self._running:
            if not self.client.connected:
                host = self.host
                if not host:
                    time.sleep(1.0)
                    continue
                try:
                    self.client.connect(host, BRIDGE_PORT)   # connect() cleans up half-open state
                    if reconnecting:
                        print(f"[{self.name}] bridge reconnected to {host}", flush=True)
                    reconnecting = False
                    self.rp_last = 0.0
                    self.rp_rtt_ms = None
                except Exception:
                    if not reconnecting:
                        print(f"[{self.name}] bridge down, reconnecting to {host}…", flush=True)
                    log.warning("%s: bridge down, reconnecting to %s", self.name, host)
                    reconnecting = True
                    time.sleep(1.0)
                    continue
            t0 = time.monotonic()
            if t0 >= self._poll_pause_until:
                try:
                    self.client.send_request(PING_TYPE, PING_PAYLOAD, timeout=0.6)
                    self.rp_last = time.time()
                    self.rp_rtt_ms = (time.monotonic() - t0) * 1000.0
                except Exception:
                    pass
            host = self.host
            if host:
                try:
                    stm = fetch_stm32_status(host)
                    self._note_stm_reset(stm)
                    self.stm = stm
                except Exception as exc:
                    self.stm = {"ever_seen": False, "error": str(exc)}
            time.sleep(1.0)

    def _note_stm_reset(self, stm: dict) -> None:
        """Log every STM32 restart WITH ITS CAUSE, at the moment it happens.

        uptime going backwards is the only evidence a restart occurred, and it is
        gone a second later when the next poll overwrites it. The cause matters
        more than the fact: a watchdog timeout, a brown-out and a real exception
        are three different faults with three different fixes, and in the field
        (40 kV arcing) nobody is watching a console when it happens."""
        up = stm.get("stm_uptime_ms")
        if up is None:
            return
        prev = self._last_stm_uptime
        self._last_stm_uptime = up
        if prev is None or up >= prev:
            return
        self._stm_resets += 1
        cause = stm.get("reset_cause")
        # "unknown" / None means the firmware did not report a cause. Logging a
        # bare 0 or an empty string there would read as "no cause", which is a
        # claim we were never given.
        if not cause or cause == "unknown":
            log.warning("%s: STM32 RESET #%d — cause NOT REPORTED by this firmware "
                        "(uptime %s -> %s ms)", self.name, self._stm_resets, prev, up)
            return
        detail = ""
        if stm.get("fault_pc") is not None:
            detail = f" fault={stm.get('fault_type')} pc=0x{int(stm['fault_pc']):08X}"
        log.warning("%s: STM32 RESET #%d — cause=%s%s (uptime %s -> %s ms)",
                    self.name, self._stm_resets, cause, detail, prev, up)

    def status(self) -> dict[str, Any]:
        now = time.time()
        rp_age = None if self.rp_last == 0 else (now - self.rp_last) * 1000.0
        stm = self.stm or {}
        return {
            "name": self.name,
            "connected": self.client.connected,
            "host": self.host,
            "bridge_name": self.bridge_name,
            "rp2350": {"age_ms": rp_age, "rtt_ms": self.rp_rtt_ms},
            "stm32": {
                "ever_seen": bool(stm.get("ever_seen")),
                "age_ms": stm.get("age_ms"),
                "error": stm.get("error"),
                "reset_cause": stm.get("reset_cause"),
                "fault_type": stm.get("fault_type"),
                "fault_pc": stm.get("fault_pc"),
                "resets_observed": self._stm_resets,
            },
        }


class _suppress:
    def __enter__(self): return self
    def __exit__(self, *a): return True


CONTROLLERS: dict[int, ControllerLink] = {
    1: ControllerLink("Power 1"),
    2: ControllerLink("Power 2"),
}

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
LOCK_TTL_DEFAULT_S = 30.0
LOCK_TTL_MAX_S = 600.0

_ACCESS_LOCK = threading.Lock()
_LEASE: dict[str, Any] = {"owner": None, "expires": 0.0, "note": ""}
_CLIENTS: dict[str, dict[str, Any]] = {}   # client id -> last-seen bookkeeping

# POST paths that never reach the hardware link (pure host-side bookkeeping) or
# must stay reachable while somebody holds the lease — coordination first.
_UNGATED_POSTS = {
    "/api/lock",
    "/api/master",            # which controller is master: host-side routing only
    "/api/schedule",          # stages the plan in this process
    "/api/poll-pause",        # heartbeat hint, self-expiring, no hardware write
    "/api/calibration/save",  # writes a host file
    "/api/hv-lut/save",       # writes a host file
    # Read-only hardware queries that happen to be POSTs. A lease reserves the
    # right to CHANGE the hardware, not to look at it — so the GUI's background
    # presence/diagnosis polling keeps working while a script drives the bench.
    "/api/present",           # CH_GET_PRESENT     — I2C presence scan
    "/api/diagnosis",         # CH_GET_DIAGNOSIS   — per-chip classification
    "/api/tca9554-read",      # CH_READ_TCA9554    — expander registers
    "/api/verify-schedule",   # ShvGetTableInfo / ShvHeatGetInfo readback
}


def _is_read_command(name: str) -> bool:
    """True for the protocol commands that only READ (CH_GET_*, HV_GET_*,
    ShvGetStatus, …). Those stay allowed while another client holds the lease —
    a lease reserves the right to CHANGE the hardware, not to look at it."""
    n = (name or "").upper()
    if "SET" in n or "WRITE" in n or "CLEAR" in n:
        return False
    return "GET" in n or "READ" in n


def _lease_snapshot() -> dict[str, Any]:
    with _ACCESS_LOCK:
        owner, left = _LEASE["owner"], _LEASE["expires"] - time.monotonic()
        if not owner or left <= 0:
            return {"held": False, "owner": None, "note": "", "expires_in_s": 0.0}
        return {"held": True, "owner": owner, "note": _LEASE["note"],
                "expires_in_s": round(left, 2)}


def _lease_acquire(owner: str, ttl: float, note: str = "", steal: bool = False) -> bool:
    """Take (or renew) the lease. The holder renewing always succeeds; another
    client succeeds only once the current lease has expired — or with steal."""
    ttl = max(1.0, min(float(ttl or LOCK_TTL_DEFAULT_S), LOCK_TTL_MAX_S))
    with _ACCESS_LOCK:
        cur, left = _LEASE["owner"], _LEASE["expires"] - time.monotonic()
        if cur and cur != owner and left > 0 and not steal:
            return False
        _LEASE.update(owner=owner, expires=time.monotonic() + ttl, note=str(note or ""))
        return True


def _lease_release(owner: str, force: bool = False) -> bool:
    with _ACCESS_LOCK:
        if _LEASE["owner"] and _LEASE["owner"] != owner and not force:
            return False
        _LEASE.update(owner=None, expires=0.0, note="")
        return True


def _lease_blocking(owner: str) -> dict[str, Any] | None:
    """The lease snapshot when someone ELSE holds it right now, else None."""
    snap = _lease_snapshot()
    return snap if snap["held"] and snap["owner"] != owner else None


def _note_client(client: str, addr: str, path: str) -> None:
    with _ACCESS_LOCK:
        rec = _CLIENTS.setdefault(client, {"id": client, "requests": 0})
        rec["requests"] += 1
        rec["address"] = addr
        rec["last_path"] = path
        rec["last_seen"] = time.time()
        if len(_CLIENTS) > 64:   # bench tool — keep the roster from growing forever
            for k, v in sorted(_CLIENTS.items(), key=lambda kv: kv[1]["last_seen"])[:16]:
                if k != client:
                    _CLIENTS.pop(k, None)


def _coerce_bytes(value: Any) -> bytes:
    """Payload for /api/raw: hex string ('0a1b', '0a 1b', '0x0a,0x1b') or a byte list."""
    if value is None or value == "":
        return b""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value)
    if isinstance(value, list):
        return bytes(int(v) & 0xFF for v in value)
    if isinstance(value, str):
        s = value.replace("0x", "").replace(",", " ").strip()
        if " " in s:
            return bytes(int(p, 16) & 0xFF for p in s.split() if p)
        if len(s) % 2:
            raise ValueError("hex payload must have an even number of digits")
        return bytes.fromhex(s)
    raise ValueError("payload must be a hex string or a list of bytes")


def _coerce_int(value: Any, default: int = 0) -> int:
    """Accept 0x79 / '0x79' / '121' / 121 — external callers write frame types
    in whatever base the protocol doc uses."""
    if value is None:
        return default
    if isinstance(value, str):
        return int(value, 0)
    return int(value)

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
RECORD_DIR = Path(__file__).resolve().parent / "recordings"

# The STM32 pulse detector is ONE physical ADC shared by two independent GUI
# controls with their own arm/disarm buttons: the Per-pulse "Stream" button
# (/api/adc/pulse-arm|disarm) and "Record measurement" (MeasurementRecorder,
# below). Each used to arm/disarm it directly -- so starting Record while
# Stream was running, then stopping EITHER one, silently disarmed the ADC out
# from under the other (Stream kept polling with no new events and no error;
# Record's .csv just stopped growing). Reference-counted per host so the real
# arm/disarm only happens on a 0->1 / 1->0 transition of the user set.
_DETECTOR_USERS: dict[str, set[str]] = {}
_DETECTOR_LOCK = threading.Lock()


def _adc_window_autoarm(host: str, n: int) -> dict[str, Any]:
    """adc_window, arming the high-speed ADC first if it isn't already running.

    adc_window summarises a window of an ALREADY-RUNNING sample stream; it is not
    a one-shot read, because the STM32's ADC only converts while armed (TIM1 +
    circular DMA). So a plain click on the GUI's ADC card used to fail with a
    bare 409 and leave the operator to know, from nowhere, that they had to arm
    something first. One click should just work.

    If the arm doesn't take, say so specifically: "accepted the arm but is not
    converting" is a different fault from "could not arm" and from "not armed",
    and the card is where someone will actually read it."""
    r = stm32_adc_window(host, n)
    if r.get("ok") or "not streaming" not in str(r.get("error", "")):
        return r                                   # worked, or failed for another reason
    arm = adc_pulse_arm(host, 1000000)
    if not arm.get("ok"):
        return {"ok": False, "auto_arm": "failed",
                "error": f"ADC was not streaming and arming it failed: "
                         f"{arm.get('error') or arm.get('message') or arm}"}
    time.sleep(0.25)
    if not _detector_is_converting(host):
        return {"ok": False, "auto_arm": "accepted-but-not-converting",
                "error": "ADC was not streaming; the STM32 ACCEPTED the arm but is "
                         "still not converting (detector_continuous_active=false, "
                         "sample count not advancing). The arm command is returning "
                         "OK without starting the ADC — this is upstream of the "
                         "summary and needs the STM32 side."}
    r = stm32_adc_window(host, n)
    if isinstance(r, dict):
        r["auto_armed"] = True
    return r


def _detector_is_converting(host: str) -> bool:
    """Is the STM32 ADC actually producing samples right now?

    Asks the hardware instead of trusting bookkeeping. detector_continuous_active
    plus a rising sample count is the real answer; hs_adc_state is NOT (CONFIG
    sets it to 1 and ARM to 2, and the ADC can be running in either). Returns
    False when it cannot tell -- an unanswerable probe must not read as "yes"."""
    try:
        a = adc_pulse_diag(host)
        if not a.get("ok"):
            return False
        s0 = ((a.get("stm32") or {}).get("samples_seen"))
        if s0 is None:
            return False
        time.sleep(0.12)
        b = adc_pulse_diag(host)
        st = (b.get("stm32") or {})
        if st.get("detector_continuous_active") is not True:
            return False
        s1 = st.get("samples_seen")
        return s1 is not None and s1 > s0
    except Exception:
        return False


def detector_arm(host: str, rate_hz: int, user: str) -> dict[str, Any]:
    """Arm the shared STM32 pulse detector for `user` ('stream'/'record').
    Only the first user actually arms the hardware; a later joiner shares
    that arm as-is -- if it wanted a different rate_hz, that's ignored (one
    ADC, one rate) and `shared`/`other_users` is set so the GUI can say so."""
    with _DETECTOR_LOCK:
        users = _DETECTOR_USERS.setdefault(host, set())
        # The refcount is a BELIEF about the hardware, not the hardware. It goes
        # stale whenever something disarms outside this bookkeeping (the relay's
        # own disarm, an STM32 reboot, a crash), and then this returned
        # {"ok": True, "shared": True, "other_users": []} -- claiming to share an
        # arm with nobody, having armed nothing. Every downstream "measured 0
        # pulses" after that was unattributable. So: only skip the real arm when
        # the hardware itself says it is converting.
        if users and _detector_is_converting(host):
            others = users - {user}
            users.add(user)
            return {"ok": True, "shared": True, "other_users": sorted(others)}
        if users:
            users.clear()          # stale bookkeeping; re-arm for real
        r = adc_pulse_arm(host, rate_hz)
        if not r.get("ok"):
            return r
        users.add(user)
        return {"ok": True}


def detector_disarm(host: str, user: str) -> dict[str, Any]:
    """Release `user`'s claim on the shared detector arm; only actually
    disarms the hardware once no other user still wants it armed."""
    with _DETECTOR_LOCK:
        users = _DETECTOR_USERS.setdefault(host, set())
        users.discard(user)
        if users:
            return {"ok": True, "shared": True, "still_armed_for": sorted(users)}
        return adc_pulse_disarm(host)


class MeasurementRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = False
        self.host: str | None = None
        self.pulse_path: Path | None = None
        self.pulse_count = 0
        self.started: float | None = None
        self._pulse_thread: threading.Thread | None = None
        self._pulse_stop = threading.Event()
        self._last_error: str | None = None

    def start(self, host: str, rate_hz: int) -> dict[str, Any]:
        with self._lock:
            if self.active:
                return {"ok": False, "error": "already recording"}
            RECORD_DIR.mkdir(exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self.pulse_path = RECORD_DIR / f"rec_{ts}_pulses.csv"
            pa = detector_arm(host, rate_hz, "record")
            if not pa.get("ok"):
                return {"ok": False, "error": f"pulse arm: {pa.get('error') or pa.get('message')}"}
            self.pulse_count = 0
            self._pulse_stop.clear()
            self._pulse_thread = threading.Thread(
                target=self._pulse_loop, args=(host,), name="rec_pulses", daemon=True)
            self._pulse_thread.start()
            self.active = True
            self.host = host
            self.started = time.time()
            self._last_error = None
            return {"ok": True, "pulse_file": self.pulse_path.name, "rate_hz": rate_hz,
                    "shared": pa.get("shared", False), "other_users": pa.get("other_users", [])}

    def _pulse_loop(self, host: str) -> None:
        try:
            with open(self.pulse_path, "w") as f:
                # rate_hz is part of the row, not the header: on_us and integral
                # are SAMPLE COUNTS, so without the rate each sample was taken
                # at, a saved recording cannot be turned back into time or
                # charge. It can also change between pulses, so one value in a
                # header comment would not be enough. Empty cell = the firmware
                # did not report it (do not backfill 1e6 when reading these).
                f.write("id,t_us,on_us,peak,plateau,bg,post_bg,bg_sigma4,integral,rate_hz,recv_ms\n")
                since = 0
                while not self._pulse_stop.is_set():
                    r = pulse_events_get(host, since)
                    if r.get("ok"):
                        evs = r.get("events") or []
                        for e in evs:
                            def _cell(v):
                                # None -> empty cell, never 0: post_bg and
                                # rate_hz are both legitimately absent, and 0 is
                                # a real post-pulse current.
                                return "" if v is None else v
                            f.write("{id},{t_us},{on_us},{peak},{plateau},{bg},{post_bg},{bg_sigma4},{integral},{rate_hz},{recv_ms}\n".format(
                                id=e.get("id", ""), t_us=e.get("t_us", ""), on_us=e.get("on_us", ""),
                                peak=e.get("peak", ""), plateau=e.get("plateau", ""), bg=e.get("bg", ""),
                                post_bg=_cell(e.get("post_bg")),
                                bg_sigma4=e.get("bg_sigma4", ""),
                                integral=e.get("integral", ""),
                                rate_hz=_cell(e.get("rate_hz")),
                                recv_ms=e.get("recv_ms", "")))
                            since = e.get("id", since)
                            self.pulse_count += 1
                        if evs:
                            f.flush()
                    self._pulse_stop.wait(0.3)
        except Exception as exc:   # keep the session alive; surface in status
            self._last_error = f"pulse recorder: {exc}"

    def stop(self) -> dict[str, Any]:
        with self._lock:
            if not self.active:
                return {"ok": True, "already": True}
            host = self.host
            still_armed_for: list[str] = []
            try:
                dd = detector_disarm(host, "record")
                still_armed_for = dd.get("still_armed_for", [])
            except Exception:
                pass
            self._pulse_stop.set()
            if self._pulse_thread is not None:
                self._pulse_thread.join(timeout=2.0)
                self._pulse_thread = None
            self.active = False
            return {"ok": True, "pulse_events": self.pulse_count,
                    "pulse_file": self.pulse_path.name if self.pulse_path else None,
                    "still_armed_for": still_armed_for}

    def status(self) -> dict[str, Any]:
        dur = (time.time() - self.started) if (self.started and self.active) else None
        return {"recording": self.active, "pulse_events": self.pulse_count,
                "duration_s": round(dur, 1) if dur else None,
                "pulse_file": self.pulse_path.name if self.pulse_path else None,
                "error": self._last_error}


RECORDER = MeasurementRecorder()

# ESP32 framed-protocol client (TCP 3334) for the Mode-2 fire-correlated
# per-pulse source: RING_PULSE_ARM/DISARM + pushed RING_PULSE_EVENT frames.
ESPCMD = EspCmdClient()


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

# ---- Scan simulation --------------------------------------------------------
# Auto-generate the whole sync-pulse train, PACED over the scan duration, in a
# background thread (so the HTTP handler never blocks for ~30 s). Fire the HEAD
# of the sync chain (P1); the RP2350 chain (P1 SyncOut -> P2 SyncIn) propagates
# it, so both controllers advance per pulse. The GUI reads schedule progress via
# /api/run-status and fire progress via /api/sync/simulate-status.
_SIM_STATE: dict = {"running": False, "fired": 0, "count": 0, "stop": False, "controller": None}
_SIM_LOCK = threading.Lock()


class RunRecorder:
    """Accumulates per-filament heating feedback during a schedule run so an
    end-of-run report can confirm each filament that fired actually reached its
    ACTIVE current. Fed from /api/telemetry (piggybacks the existing poll — no
    extra bridge traffic); during a run those samples are the CC-loop CACHED
    currents (no I2C). A filament counts as CONFIRMED if its peak measured current
    came within CONFIRM_MARGIN_MA of the active setpoint it was commanded to."""
    CONFIRM_MARGIN_MA = 150       # peak within this of the active target = confirmed
    ACTIVE_MIN_TARGET_MA = 2000   # a commanded target >= this means "was driven ACTIVE"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.active = False
        self.started = 0.0
        self.peak: dict[int, int] = {}    # filament -> peak measured mA over the run
        self.last: dict[int, int] = {}
        self.tgt: dict[int, int] = {}     # filament -> peak commanded target mA (= active setpoint)
        self.samples = 0
        self.expect: set[int] = set()
        self.active_mA = 2900
        self.report: dict | None = None

    def start(self, expect=None, active_mA: int = 2900) -> None:
        with self._lock:
            self.active = True
            self.started = time.time()
            self.peak, self.last, self.tgt = {}, {}, {}
            self.samples = 0
            self.expect = set(int(x) for x in (expect or []))
            self.active_mA = int(active_mA or 2900)
            self.report = None

    def observe(self, rows) -> None:
        with self._lock:
            if not self.active:
                return
            self.samples += 1
            for r in rows:
                fil = r.get("index")
                if fil is None:
                    continue
                mA = int(r.get("current_mA") or 0)
                self.last[fil] = mA
                if mA > self.peak.get(fil, -1 << 30):
                    self.peak[fil] = mA
                tg = int(r.get("target_mA") or 0)
                if tg > self.tgt.get(fil, 0):
                    self.tgt[fil] = tg   # max target seen = the ACTIVE setpoint

    def stop(self) -> dict | None:
        with self._lock:
            if not self.active:
                return self.report
            self.active = False
            self.report = self._finalize()
        _write_run_report(self.report)
        return self.report

    def _finalize(self) -> dict:
        rows = []
        for fil in sorted(set(self.peak) | self.expect):
            peak = self.peak.get(fil, 0)
            ptgt = self.tgt.get(fil, 0)
            was_active = (fil in self.expect) or (ptgt >= self.ACTIVE_MIN_TARGET_MA)
            aim = ptgt if ptgt >= self.ACTIVE_MIN_TARGET_MA else self.active_mA
            confirmed = was_active and peak >= (aim - self.CONFIRM_MARGIN_MA)
            rows.append({"filament": fil, "expected": fil in self.expect,
                         "was_active": was_active, "peak_mA": peak,
                         "last_mA": self.last.get(fil, 0), "aim_mA": aim,
                         "confirmed": bool(confirmed)})
        active_rows = [r for r in rows if r["was_active"]]
        confirmed = [r for r in active_rows if r["confirmed"]]
        missing = [r["filament"] for r in active_rows if not r["confirmed"]]
        return {
            "generated": time.time(),
            "duration_s": round(time.time() - self.started, 1),
            "samples": self.samples,
            "n_active": len(active_rows),
            "n_confirmed": len(confirmed),
            "missing": missing,
            "rows": rows,
        }

    def status(self) -> dict:
        with self._lock:
            return {"active": self.active, "samples": self.samples,
                    "n_tracked": len(self.peak), "report": self.report}


RUN_RECORDER = RunRecorder()


def _write_run_report(report: dict | None) -> None:
    """Persist the run report as a human-readable text file next to the backend."""
    if not report:
        return
    try:
        d = Path(__file__).resolve().parent / "run_reports"
        d.mkdir(exist_ok=True)
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime(report.get("generated", time.time())))
        lines = [f"RUN POWER-STATE REPORT  {ts}",
                 f"duration {report['duration_s']}s · {report['samples']} samples · "
                 f"{report['n_confirmed']}/{report['n_active']} active filaments confirmed >= target-"
                 f"{RunRecorder.CONFIRM_MARGIN_MA}mA",
                 ""]
        if report["missing"]:
            lines.append(f"!! NOT CONFIRMED ACTIVE: {report['missing']}")
            lines.append("")
        lines.append(" fil  expected  peak mA  last mA  aim mA  verdict")
        for r in report["rows"]:
            if not r["was_active"] and not r["expected"]:
                continue
            lines.append(f" {r['filament']:>3}  {('yes' if r['expected'] else '  -'):>8}  "
                         f"{r['peak_mA']:>7}  {r['last_mA']:>7}  {r['aim_mA']:>6}  "
                         f"{'OK' if r['confirmed'] else 'MISS !!'}")
        (d / f"run_{ts}.txt").write_text("\n".join(lines) + "\n")
    except Exception:
        pass


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
    heat = [h for h in (plan.get("heating") or [])
            if filament_to_board(int(h["filament"]))[0] == controller]
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
    if emit_dead:
        out["dead_skipped"] = emit_dead
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
    if int(state) == POWER_STATE_ACTIVE:
        allowed = []
        for f in fils:
            why = ladder_blocks_active(f)
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
        return {"controller": controller, "ok": True, "applied": 0, "failed": [],
                "state": int(state), "touched": [], "not_this_controller": not_this_controller,
                "unslotted": unslotted, "dead_skipped": dead_skipped,
                "ladder_blocked": ladder_blocked, "ladder_reasons": ladder_reasons}
    groups: dict = {}   # (channel, arg) -> OR'd mask byte for that channel
    members: dict = {}  # (channel, arg) -> [filament]
    for f in fils:
        _, ch, pos, _ = filament_to_board(f)
        if ch is None:            # unslotted/overflow filament (past slot 63, or a
            unslotted.append(f)   # skipped channel) -> has no power slot to address
            continue
        v = currents.get(str(f), currents.get(f, default_arg))
        arg = int(v if v is not None else default_arg)   # tolerate an explicit null
        groups[(ch, arg)] = groups.get((ch, arg), 0) | (1 << pos)
        members.setdefault((ch, arg), []).append(f)
    reqs, keys = [], []
    for (ch, arg), chmask in groups.items():
        m = bytearray(8)
        m[ch] = chmask & 0xFF
        reqs.append((CH_SET_POWER_STATE, bytes(m) + bytes([int(state) & 0xFF]) + _u16(arg), 0))
        keys.append((ch, arg))
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
        ch, _arg = key
        raw = r.get("raw") if isinstance(r, dict) else None
        appl = raw[9 + ch] if (raw and len(raw) >= 9 + ch + 1) else 0
        for f in members[key]:
            _, _fch, fpos, _ = filament_to_board(f)
            if appl & (1 << fpos):
                applied += 1
                landed.append(int(f))
            else:
                failed.append(int(f))
    # Record only what the firmware CONFIRMED it applied. Recording the intent
    # would let a failed write leave the backend believing a filament is at
    # IDLE, which is exactly the belief the ACTIVE guard depends on.
    note_power_state(landed, state)
    return {"controller": controller, "ok": not failed and not unslotted, "applied": applied,
            "total": len(fils), "failed": failed, "state": int(state),
            "touched": fils, "not_this_controller": not_this_controller,
            "unslotted": unslotted, "dead_skipped": dead_skipped,
            "ladder_blocked": ladder_blocked, "ladder_reasons": ladder_reasons}


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


def do_scan() -> list[dict[str, Any]]:
    """Every host with :3333 open (the ESP32 bridge), newest scan."""
    out = []
    for rec in scan_for_bridge(probe_controller=True):
        bridge = rec.get("bridge", {})
        if not bridge.get("port_open"):
            continue
        out.append({
            "host": rec.get("host"),
            "name": bridge.get("name"),
            "port_open": True,
            "controller_responsive": rec.get("controller", {}).get("responsive", False),
        })
    return out


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

        elif path == "/api/clients":
            # Everyone that has called this API recently — so a program can see
            # it is not alone on the bench before it starts driving hardware.
            with _ACCESS_LOCK:
                clients = sorted(_CLIENTS.values(), key=lambda r: r["last_seen"], reverse=True)
                clients = [dict(r, idle_s=round(time.time() - r["last_seen"], 1)) for r in clients]
            self._json({"ok": True, "you": self._client(), "clients": clients,
                        "lock": _lease_snapshot()})
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
                running = False
                try:
                    st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                    if st:
                        run_state[str(cid)] = st
                        running = st.get("state") == 2
                        fi = st.get("filamentIndex")
                        if running and fi is not None and fi != 0xFF and fi < FILAMENT_COUNT:
                            # firmware filamentIndex IS the global filament 0-95
                            firing.append(fi)
                except Exception:
                    pass
                # Always read the CC-loop CACHED currents (0x3A, no I2C) here, not
                # the live INA sweep. While firing this avoids sharing the I2C bus
                # and stalling pulses (the original reason for this branch); at
                # idle, the live sweep's own cost (CH_GET_PRESENT-equivalent probe
                # + several paged round-trips every ~1s poll) was the single
                # biggest consumer of the shared RP2350 link -- see the cross-
                # session thread with rp2350bfilamentcontroller-39 on the ~10s
                # /api/cmd lag this caused. present now reflects CC-loop
                # regulation state (mode != 0), not raw I2C presence, so a board
                # that's plugged in but idle/voltage-mode shows as not-present
                # here -- same tradeoff already accepted for the "running" case,
                # now applied uniformly. The Boards matrix's own CH_GET_PRESENT
                # (a separate, slower-cadence poll) is still the source of truth
                # for physical presence.
                try:
                    if want_live and not running:
                        rows.update(read_telemetry(link, cid - 1))
                    else:
                        rows.update(read_cached_telemetry(link, cid - 1))
                except Exception:
                    pass
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
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                self._json({"ok": False, "error": "controller not connected", "boards": []})
            else:
                try:
                    self._json({"ok": True, "vi_only": vi_only, "cached": cached,
                                "boards": board_snapshot(link, cid - 1, vi_only=vi_only, cached=cached)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc), "boards": []})
        elif path == "/api/present-filaments":
            # Which GLOBAL filament indices have a physically-present board, across
            # all connected controllers. The GUI uses this to one-click disable the
            # absent ones so the schedule fits the bench (arm rejects absent boards).
            present = []
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
                except Exception:
                    pass
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
            self._json({"ok": True, "present": present, "count": len(present),
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
                try:
                    dec = link.request(0x13, b"", flags=0, timeout=2.0).get("decoded") or {}
                    self._json({"ok": True, "desired": dec.get("desired", [0] * 8), "feedback": dec.get("feedback", [0] * 8)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc)})
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
            self._json({"ok": False, "error": err} if err else stm32_ads1115(host))
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
                self._json(stm32_hv_status(host))
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
        elif path == "/api/run-status":
            # Poll ShvGetStatus (0x79) from each connected controller. totalPulsesDone
            # is the shared global playhead; filamentIndex is the live firing filament.
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
            self._json({"controllers": out})
        else:
            self._serve_static(path)

    # --- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        global SCAN_MASK   # read (diagnosis branch) + written (channel-mask branch)
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        client = self._client(body)
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
                cid = int(body.get("controller", MASTER))
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
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        resp = link.request(SHV_ARM, payload, flags=0)
                        raw = resp.get("raw") if isinstance(resp, dict) else None
                        reject = raw[1] if raw and len(raw) > 1 else None
                        results[str(cid)] = {"ok": bool(raw) and raw[0] == 0 and reject == 0, "reject": reject}
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": all(r.get("ok") for r in results.values()) if results else False,
                            "results": results})
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
                self._json({"ok": True, "results": results})
            elif path == "/api/filament-state":
                # TRUE single-board CH_SET_POWER_STATE (0x35, FLAG_SINGLE) — one
                # filament, one frame, no masking/grouping machinery. This is the
                # RP2350's own single-board wire format, distinct from the batched
                # board_mask form /api/filament-prep uses even for a 1-filament
                # call. Use this for isolated single-filament control.
                filament = int(body.get("filament", -1))
                state = int(body.get("state", 0))
                if state < 1 or state > 6:
                    return self._json({"ok": False, "error": "bad state"}, HTTPStatus.OK)
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
                if state == POWER_STATE_ACTIVE:
                    why = ladder_blocks_active(filament)
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
                try:
                    st = decode_shv_status(link.request(SHV_GET_STATUS, b"", timeout=1.0))
                    if st and st.get("state") == 2:
                        return self._json({"ok": False, "error": "running — disarm first"}, HTTPStatus.OK)
                except Exception:
                    pass
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
                self._json({"ok": _status_ok(resp) and applied, "filament": filament,
                            "controller": cid, "channel": ch, "mux_port": pos,
                            "state": state, "arg": arg})
            elif path == "/api/filament-prep":
                # CT-scan prep ladder — apply one PowerState to a batch of
                # filaments across BOTH connected controllers. Refused while a
                # schedule is running (would fight the executor's heating).
                state = int(body.get("state", 0))
                if state < 1 or state > 6:
                    return self._json({"ok": False, "error": "bad state"}, HTTPStatus.OK)
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
                touched = {int(f) for r in results.values() for f in (r.get("touched") or [])}
                excluded = ([int(f) for f in filaments if int(f) not in touched]
                            if filaments is not None else [])
                self._json({"ok": all(r.get("ok") for r in results.values()) and not excluded,
                            "results": results, "failed": failed, "applied": applied,
                            "excluded": excluded})
            elif path == "/api/ocp-threshold":
                # Per-board TPS55289 IOUT_LIMIT (steady-state OCP threshold, mA)
                # for a batch of filaments across BOTH connected controllers.
                # body: {filaments: [...]|None, threshold_ma: int}. No native
                # batch opcode exists — loops one frame per filament.
                try:
                    threshold_ma = int(body.get("threshold_ma"))
                except (TypeError, ValueError):
                    return self._json({"ok": False, "error": "threshold_ma required"}, HTTPStatus.OK)
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
                # CH_STARTUP_OCP (0x37) wire format: [] get, [mA16] set startup
                # only, [mA16,mA16] set both — build_payload only covers the
                # 2-byte set form, so build the frame directly here.
                payload = _u16(startup_ma)
                if "steady_ma" in body:
                    payload += _u16(int(body["steady_ma"]))
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
                lines = ["filament" + ("," + ",".join(cols) if cols else "")]
                for fil, curve in sorted(curves.items(), key=lambda kv: int(kv[0])):
                    for pt in curve:
                        lines.append(str(fil) + "".join("," + str(pt.get(k, "")) for k in cols))
                base.with_suffix(".csv").write_text("\n".join(lines) + "\n")
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
                mask = int(body.get("mask", 0x3F)) & 0xFF
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
                # Bench test: pulse SyncIn `count` times via the ESP32 bridge's
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
                pbg = body.get("post_bg_gap")
                pbn = body.get("post_bg_n")
                self._json(adc_ready_arm(host, int(body.get("rate", 1000000)),
                                         int(body.get("n_samples", 2000)),
                                         None if pbg is None else int(pbg),
                                         None if pbn is None else int(pbn)))
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
                self._json(stm32_hv_enable_set(host, str(body.get("ch", "emission")), bool(body.get("on"))))
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
                count = max(1, int(body.get("count", 1)))
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
                    return self._json({"ok": False, "status": status_byte, "raw": raw}, HTTPStatus.OK)
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
        Only WRITES are gated: /api/cmd and /api/power-cmd carrying a read-only
        command pass, as does anything that never touches the link."""
        if path in _UNGATED_POSTS:
            return None
        held = _lease_blocking(client)
        if held is None:
            return None
        if path in ("/api/cmd", "/api/power-cmd") and _is_read_command(body.get("command", "")):
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
            return json.loads(raw or b"{}")
        except (ValueError, json.JSONDecodeError):
            return {}

    def _json(self, obj, status: HTTPStatus = HTTPStatus.OK) -> None:
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
    host = os.environ.get("CT_GUI_HOST", "0.0.0.0")
    port = int(os.environ.get("CT_GUI_PORT", "8770"))
    _setup_logging()
    log.info("=== backend start — listening on http://%s:%d ===", host, port)
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
