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

import json
import os
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

# reuse the WiFi GUI transport/protocol layer
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "wifi_gui"))
from net_protocol import (  # noqa: E402
    BRIDGE_PORT,
    TcpProtocolClient,
    build_command_payload,
    fetch_bridge_info,
    fetch_stm32_status,
    scan_for_bridge,
    sync_post_fire,
    sync_post_config,
    sync_post_abort,
    sync_get_status,
    adc_get_burst,
    adc_spi_shot_arm,
    adc_spi_shot_data,
    adc_ring_start,
    adc_ring_stop,
    adc_ring_peek,
    adc_ring_tap_start,
    adc_ring_tap_stop,
    adc_ring_window,
    adc_ring_window_data,
    adc_pulse_arm,
    adc_pulse_disarm,
    primary_local_ip,
    AdcUdpListener,
    EspCmdClient,
    pulse_events_get,
    stm32_ds3502_get,
    stm32_ds3502_set,
    stm32_hv_enable_set,
    stm32_hv_status,
    stm32_ads1115,
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


def build_payload(command: str, b: dict):
    """Return (frame_type, flags, payload) for a single-board command."""
    ch = int(b.get("channel", 0)) & 0xFF
    mux = int(b.get("mux_port", 0)) & 0xFF
    if command == "CH_SET_POWER_STATE":      # 0x35: ch,mux,state,arg16 (IDLE/ACTIVE→mA, VOLTAGE→mV)
        return 0x35, FLAG_SINGLE, bytes([ch, mux, int(b["state"]) & 0xFF]) + _u16(int(b.get("arg", 0)))
    if command == "CH_GET_POWER_STATE":      # 0x36: ch,mux -> status,ch,mux,state,faultKind
        return 0x36, FLAG_SINGLE, bytes([ch, mux])
    if command == "CH_STARTUP_OCP":          # 0x37: get(empty) / set(mA16) startup OCP floor
        return (0x37, 0, _u16(int(b["threshold_mA"]))) if b.get("set") else (0x37, 0, b"")
    if command == "CH_SET_TPS_VOLTAGE":      # 0x22: ch,mux,mV16,enable
        return 0x22, FLAG_SINGLE, bytes([ch, mux]) + _u16(int(b["millivolts"])) + bytes([1 if b.get("enable_after_set", True) else 0])
    if command == "CH_SET_TPS_OCP_THRESHOLD":  # 0x28: ch,mux,mA16 (direct IOUT_LIMIT)
        return 0x28, FLAG_SINGLE, bytes([ch, mux]) + _u16(int(b["threshold_mA"]))
    if command == "CH_GET_INA219":           # 0x24: ch,mux -> status,ch,mux,present,busMv16,mA16
        return 0x24, FLAG_SINGLE, bytes([ch, mux])
    if command == "HV_GET_ALL_BYTES":        # 0x13: desired[8]+feedback[8]
        return 0x13, 0, b""
    if command == "HV_SET_BIT":              # 0x10: ch,bit,value,verifyMode
        mode = 2 if b.get("force") else (1 if b.get("verify", True) else 0)
        return 0x10, 0, bytes([ch, int(b["bit"]) & 0xFF, 1 if b.get("value") else 0, mode])
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
        self.set_default(group_size)

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
            for slot, f in enumerate(sorted(self._ctrl_fils[c])):
                if slot >= POWER_SLOTS:
                    self.overflow[c].append(f)
                    continue
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
            "filaments": rows,
        }


MAPPING = FilamentMapping()


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
CH_FILAMENT_CURRENTS = 0x39
CH_SET_POWER_STATE = 0x35        # ch,mux,state,arg16 (Idle/Active→mA, Voltage→mV)
CH_SET_I2C_ENABLE_MASK = 0x34
CH_GET_INA219 = 0x24
CH_GET_PRESENT = 0x25            # I2C presence scan (mux/tps/ina/io per board)
CH_GET_DIAGNOSIS = 0x2E         # deep diagnosis: addr-ACK / reg-read / operational
CH_GET_I2C_ENABLE_MASK = 0x2F   # read the channel enable mask
CH_RESET_MUX = 0x5F             # pulse TCA9548A reset + re-detect (power-cutting)
CH_TCA9554_SELF_TEST = 0x60     # per-pin TCA9554 toggle test
CH_READ_TCA9554 = 0x61          # read-only TCA9554 Config/Input/Output dump
ALL_BOARDS_MASK = bytes([0xFF] * 8)
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
    out: dict[str, Any] = {"channel_mask": read_channel_mask(link)}
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


def board_snapshot(link: "ControllerLink", controller: int, channels=DEFAULT_CHANNELS) -> list:
    """64-board snapshot for the boards matrix: present/tps/ina presence (0x25),
    iso/tps enable + fault (0x26), and INA219 V/I (0x24). Returns 64 board dicts."""
    boards = {}
    for ch in range(8):
        for mux in range(8):
            boards[(ch, mux)] = {
                "channel": ch, "mux_port": mux, "label": f"CH{ch + 1}.{mux + 1}",
                "present": False, "mux_present": False, "tps_present": False, "ina_present": False,
                "iso_enabled": False, "tps_enabled": False, "tps_fault": False,
                "hv_overcurrent": False, "bus_mV": 0, "current_mA": 0,
            }

    def apply_slice(raw, start, field):
        if len(raw) < start + 8:
            return
        for ch in range(8):
            for mux in range(8):
                if raw[start + ch] & (1 << mux):
                    boards[(ch, mux)][field] = True

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
    except Exception:
        pass
    try:
        resp = link.client.send_request(CH_GET_BOARD_BITMAPS, bytes(ALL_BOARDS_MASK), timeout=3.0)
        if resp.get("status_code") == 0x00:
            raw = resp.get("raw") or []     # status, targeted[8], iso_en, tps_en, tps_fault, hv_oc
            apply_slice(raw, 9, "iso_enabled")
            apply_slice(raw, 17, "tps_enabled")
            apply_slice(raw, 25, "tps_fault")
            apply_slice(raw, 33, "hv_overcurrent")
    except Exception:
        pass
    try:
        for fil, t in read_telemetry(link, controller).items():
            b = MAPPING.board(fil)
            if not b:
                continue
            ch, mux = b[1], b[2]
            boards[(ch, mux)].update(bus_mV=t["bus_mV"], current_mA=t["current_mA"],
                                     ina_present=t["present"], present=t["present"])
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


def read_telemetry(link: "ControllerLink", controller: int, channels=None) -> dict:
    """Batch-read every populated board's INA219 (multi-board paged 0x24) and map
    local (channel, mux) → global filament 0-95 via the active-list MAPPING."""
    used = MAPPING.channels_used(controller)
    mask = bytearray(8)
    for c in used:
        mask[c] = 0xFF
    out: dict[int, dict] = {}
    page_start, max_entries, guard = 0, 16, 0
    while guard < 16:
        guard += 1
        payload = bytes(mask) + bytes([page_start & 0xFF, max_entries & 0xFF])
        resp = link.request(CH_GET_INA219, payload, flags=0, timeout=1.5)
        dec = resp.get("decoded") if isinstance(resp, dict) else None
        entries = (dec or {}).get("entries", [])
        total = (dec or {}).get("total_matching_entries", 0)
        for e in entries:
            ch, mux = e.get("channel"), e.get("mux_port")
            fil = MAPPING.filament_for_board(controller, ch, mux)
            if fil is None:
                continue
            out[fil] = {
                "index": fil, "present": bool(e.get("present")),
                "bus_mV": e.get("bus_mV", 0), "current_mA": e.get("current_mA", 0),
            }
        if not entries or len(out) >= total or len(entries) < max_entries:
            break
        page_start += len(entries)
    return out

SHV_EMIT_CHUNK = 64    # emission entries per frame (64*4+3 = 259 B)
SHV_HEAT_CHUNK = 56    # firmware caps ShvHeatSetEntries at 56


def _status_ok(resp) -> bool:
    raw = resp.get("raw") if isinstance(resp, dict) else None
    return bool(raw) and raw[0] == 0x00


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
        for e in entries:
            ent += bytes([int(e["filament"]) & 0xFF, int(e["numPulses"]) & 0xFF]) + _u16(int(e["width"]))
        ok = True
        for start in range(0, len(entries), SHV_EMIT_CHUNK):
            cnt = min(SHV_EMIT_CHUNK, len(entries) - start)
            payload = _u16(start) + bytes([cnt]) + bytes(ent[start * 4:(start + cnt) * 4])
            ok = _status_ok(link.request(SHV_SET_ENTRIES, payload)) and ok
        return {"ok": ok, "count": len(entries)}
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
    }

STATIC_DIR = Path(__file__).resolve().parent / "static"
CALIB_DIR = Path(__file__).resolve().parent / "calibration"   # emission-current calibration records


def _hv_lut_path(chan: str) -> Path:
    """Stable per-master, per-channel HV wiper→voltage LUT file. The HV/DS3502
    board lives on the MASTER controller, so the LUT is keyed by master id +
    channel; changing the master selects a different LUT."""
    ch = "focus" if str(chan).startswith("f") else "emission"
    return CALIB_DIR / f"hv_lut_p{MASTER}_{ch}.json"
PING_TYPE = 0x01
PING_PAYLOAD = (0xCAFEF00D).to_bytes(4, "little")

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
        self._poll_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.rp_last = 0.0                       # unix time of last good PING
        self.rp_rtt_ms: float | None = None
        self.stm: dict[str, Any] = {}
        self.bridge_name: str | None = None       # ESP32 AP SSID (MAC-derived identity)

    def connect(self, host: str) -> None:
        with self._lock:
            self._stop_poll()
            self.client.connect(host, BRIDGE_PORT)
            self.host = host
            self.rp_last = 0.0
            self.rp_rtt_ms = None
            self.stm = {}
            self.bridge_name = None
            self._running = True
            self._poll_thread = threading.Thread(target=self._poll, daemon=True)
            self._poll_thread.start()
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

    def _poll(self) -> None:
        while self._running and self.client.connected:
            t0 = time.monotonic()
            try:
                self.client.send_request(PING_TYPE, PING_PAYLOAD, timeout=0.6)
                self.rp_last = time.time()
                self.rp_rtt_ms = (time.monotonic() - t0) * 1000.0
            except Exception:
                pass
            host = self.host
            if host:
                try:
                    self.stm = fetch_stm32_status(host)
                except Exception as exc:
                    self.stm = {"ever_seen": False, "error": str(exc)}
            time.sleep(1.0)

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
# Measurement recorder. A session records BOTH streams to host files while a
# capture runs: (1) the raw STM32 ADC waveform via the ring UDP/TCP tap →
# rec_<ts>_adc.bin (u16 LE), and (2) the STM32 per-pulse measurements polled
# from /pulse_events → rec_<ts>_pulses.csv. start() arms ring+tap on the master
# host; stop() tears it down and finalizes the files.
# ---------------------------------------------------------------------------
RECORD_DIR = Path(__file__).resolve().parent / "recordings"
RECORD_PORT = 3336   # host TCP port the ESP32 ring tap connects back to


class MeasurementRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.listener = AdcUdpListener()
        self.active = False
        self.host: str | None = None
        self.adc_path: Path | None = None
        self.pulse_path: Path | None = None
        self.pulse_count = 0
        self.started: float | None = None
        self._pulse_thread: threading.Thread | None = None
        self._pulse_stop = threading.Event()
        self._last_error: str | None = None

    def start(self, host: str, rate_hz: int, decim: int) -> dict[str, Any]:
        with self._lock:
            if self.active:
                return {"ok": False, "error": "already recording"}
            ip = primary_local_ip()
            if not ip:
                return {"ok": False, "error": "could not determine host IP for the tap"}
            RECORD_DIR.mkdir(exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            self.adc_path = RECORD_DIR / f"rec_{ts}_adc.bin"
            self.pulse_path = RECORD_DIR / f"rec_{ts}_pulses.csv"
            # 1. listener (TCP server) recording raw samples to the .bin file
            r = self.listener.start(RECORD_PORT, record_path=str(self.adc_path))
            if not r.get("ok"):
                return {"ok": False, "error": f"listener: {r.get('error')}"}
            # 2. ring + tap on the device → connects back to ip:RECORD_PORT
            rs = adc_ring_start(host, rate_hz)
            if not rs.get("ok"):
                self.listener.stop()
                return {"ok": False, "error": f"ring start: {rs.get('error') or rs.get('message')}"}
            tp = adc_ring_tap_start(host, ip, RECORD_PORT, decim)
            if not tp.get("ok"):
                adc_ring_stop(host)
                self.listener.stop()
                return {"ok": False, "error": f"tap start: {tp.get('error') or tp.get('message')}"}
            # 3. pulse-event recorder thread → .csv
            self.pulse_count = 0
            self._pulse_stop.clear()
            self._pulse_thread = threading.Thread(
                target=self._pulse_loop, args=(host,), name="rec_pulses", daemon=True)
            self._pulse_thread.start()
            self.active = True
            self.host = host
            self.started = time.time()
            self._last_error = None
            return {"ok": True, "adc_file": self.adc_path.name, "pulse_file": self.pulse_path.name,
                    "host_ip": ip, "port": RECORD_PORT, "rate_hz": rate_hz, "decim": decim}

    def _pulse_loop(self, host: str) -> None:
        try:
            with open(self.pulse_path, "w") as f:
                f.write("id,t_us,on_us,peak,plateau,bg,bg_sigma4,integral,recv_ms\n")
                since = 0
                while not self._pulse_stop.is_set():
                    r = pulse_events_get(host, since)
                    if r.get("ok"):
                        evs = r.get("events") or []
                        for e in evs:
                            f.write("{id},{t_us},{on_us},{peak},{plateau},{bg},{bg_sigma4},{integral},{recv_ms}\n".format(
                                id=e.get("id", ""), t_us=e.get("t_us", ""), on_us=e.get("on_us", ""),
                                peak=e.get("peak", ""), plateau=e.get("plateau", ""), bg=e.get("bg", ""),
                                bg_sigma4=e.get("bg_sigma4", ""),
                                integral=e.get("integral", ""), recv_ms=e.get("recv_ms", "")))
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
            try:
                adc_ring_tap_stop(host)
                adc_ring_stop(host)
            except Exception:
                pass
            self._pulse_stop.set()
            self.listener.stop()
            if self._pulse_thread is not None:
                self._pulse_thread.join(timeout=2.0)
                self._pulse_thread = None
            self.active = False
            st = self.listener.status()
            return {"ok": True, "adc_samples": st.get("samples", 0),
                    "pulse_events": self.pulse_count,
                    "adc_file": self.adc_path.name if self.adc_path else None,
                    "pulse_file": self.pulse_path.name if self.pulse_path else None}

    def status(self) -> dict[str, Any]:
        st = self.listener.status()
        dur = (time.time() - self.started) if (self.started and self.active) else None
        return {"recording": self.active,
                "adc_samples": st.get("samples", 0), "adc_packets": st.get("packets", 0),
                "drops": st.get("drops_device", 0), "missed_packets": st.get("missed_packets", 0),
                "rate_hz_obs": st.get("rate_hz_obs"), "pulse_events": self.pulse_count,
                "duration_s": round(dur, 1) if dur else None,
                "adc_file": self.adc_path.name if self.adc_path else None,
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
def download_to_controller(link: "ControllerLink", controller: int, plan: dict,
                           channels=None) -> dict:
    steps: list[dict] = []

    def step(name, resp):
        steps.append({"step": name, "ok": _status_ok(resp)})
        return _status_ok(resp)

    # 1. active-filament list (64-byte power-slot -> global filament) + channel mask
    step("active_list", link.request(SHV_SET_ACTIVE_LIST, MAPPING.active_list(controller), flags=0))
    step("mask", link.request(CH_SET_I2C_ENABLE_MASK, bytes([MAPPING.channel_mask(controller) & 0xFF]), flags=0))

    # 2. per-filament IDLE/ACTIVE current calibration (this controller's boards)
    for fil, cur in (plan.get("currents") or {}).items():
        f = int(fil)
        ctrl, ch, pos, _ = filament_to_board(f)
        if ctrl != controller:
            continue
        payload = bytes([ch, pos]) + _u16(int(cur.get("idle_mA", 0))) + _u16(int(cur.get("active_mA", 0)))
        link.request(CH_FILAMENT_CURRENTS, payload, flags=FLAG_SINGLE)
    steps.append({"step": "currents", "ok": True})

    # 3. config
    cfg = plan.get("config") or {}
    cfg_payload = (_u32(int(cfg.get("interPulseMs", 3000))) + _u16(int(cfg.get("maxOnMs", 40)))
                   + _u32(int(cfg.get("totalMs", 60000))) + bytes([int(cfg.get("triggerEdge", 0)) & 0xFF]))
    step("config", link.request(SHV_SET_CONFIG, cfg_payload, flags=0))

    # 4. emission table — the SAME full global list to BOTH controllers; the entry
    # carries the GLOBAL filament 0-95 (firmware decode_ reverse-maps via the
    # active list; a not-mine filament is counted but not fired).
    step("emit_clear", link.request(SHV_CLEAR_TABLE, b"", flags=0))
    emit = plan.get("emission") or []
    ent = bytearray()
    for e in emit:
        ent += bytes([int(e["filament"]) & 0xFF, int(e["numPulses"]) & 0xFF]) + _u16(int(e["widthUs"]))
    n = len(emit)
    for start in range(0, n, SHV_EMIT_CHUNK):
        count = min(SHV_EMIT_CHUNK, n - start)
        body = _u16(start) + bytes([count]) + bytes(ent[start * 4:(start + count) * 4])
        step(f"emit[{start}]", link.request(SHV_SET_ENTRIES, body, flags=0))

    # 5. heating deltas — only THIS controller's filaments, local (ch, pos)
    step("heat_clear", link.request(SHV_HEAT_CLEAR, b"", flags=0))
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
        body = _u16(start) + bytes([count]) + bytes(hent[start * 8:(start + count) * 8])
        step(f"heat[{start}]", link.request(SHV_HEAT_SET_ENTRIES, body, flags=0))

    ok = all(s["ok"] for s in steps)
    return {"controller": controller, "ok": ok, "emit": n, "heat": hn, "steps": steps}


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
    is a logical 0-95 list (only this controller's are touched); None = every
    populated board the controller owns. `currents` maps filament→mA for the
    Idle/Active arg (falls back to default_arg)."""
    currents = currents or {}
    if filaments is None:
        fils = MAPPING.filaments(controller)
    else:
        fils = [int(f) for f in filaments
                if filament_to_board(int(f))[0] == controller]
    # Per-filament outcome: a bad power channel makes CH_SET_POWER_STATE fail for
    # THAT filament only. Track which failed (global indices) so callers can skip
    # + report them instead of aborting the whole batch.
    applied, failed = 0, []
    for f in fils:
        _, ch, pos, _ = filament_to_board(f)
        arg = int(currents.get(str(f), currents.get(f, default_arg)))
        payload = bytes([ch, pos, int(state) & 0xFF]) + _u16(arg)
        try:
            ok1 = _status_ok(link.request(CH_SET_POWER_STATE, payload, flags=FLAG_SINGLE, timeout=2.0))
        except Exception:
            ok1 = False
        if ok1:
            applied += 1
        else:
            failed.append(int(f))
    return {"controller": controller, "ok": not failed, "applied": applied,
            "failed": failed, "state": int(state)}


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

    # --- GET ----------------------------------------------------------------
    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/geometry":
            self._json(GEOMETRY)
        elif path == "/api/scan":
            self._json({"results": do_scan()})
        elif path == "/api/status":
            self._json({"controllers": {str(k): c.status() for k, c in CONTROLLERS.items()},
                        "master": MASTER})
        elif path == "/api/mapping":
            self._json({"ok": True, "mapping": MAPPING.as_dict()})
        elif path == "/api/telemetry":
            # Real per-filament telemetry for the ring: batch INA219 (V/I) per
            # connected controller, merged by the board map. Plus the live firing
            # filament from ShvGetStatus (no batch power-state read exists).
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
                # Skip the INA mux sweep while firing — it shares the I2C bus and
                # would stall pulses (simple_hv_schedule_design.md). ShvGetStatus
                # carries the firing filament during a run.
                if not running:
                    try:
                        rows.update(read_telemetry(link, cid - 1))
                    except Exception:
                        pass
            self._json({"telemetry": list(rows.values()), "firing": firing, "run": run_state})
        elif path == "/api/board-snapshot":
            # 64-board matrix for the selected controller (?controller=N).
            q = self.path.split("?", 1)
            cid = 1
            if len(q) > 1:
                for kv in q[1].split("&"):
                    if kv.startswith("controller="):
                        cid = int(kv.split("=", 1)[1] or 1)
            link = CONTROLLERS.get(cid)
            if not link or not link.client.connected:
                self._json({"ok": False, "error": "controller not connected", "boards": []})
            else:
                try:
                    self._json({"ok": True, "boards": board_snapshot(link, cid - 1)})
                except Exception as exc:
                    self._json({"ok": False, "error": str(exc), "boards": []})
        elif path == "/api/hv-snapshot":
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
            host, err = self._master_host()
            if err:
                self._json({"ok": False, "error": err})
            else:
                self._json(pulse_events_get(host, int(self._query().get("since", "0"))))
        elif path == "/api/ringpulse/events":
            since = int(self._query().get("since", "0"))
            self._json({"ok": True, "events": ESPCMD.events_since(since), **ESPCMD.status()})
        elif path == "/api/record/status":
            self._json({"ok": True, **RECORDER.status()})
        elif path == "/api/record/download":
            # Serve a recorded file from RECORD_DIR (raw .bin or pulses .csv).
            name = os.path.basename(self._query().get("file", ""))
            fpath = RECORD_DIR / name
            if not name or not fpath.is_file():
                self._json({"ok": False, "error": "no such recording"}, HTTPStatus.NOT_FOUND)
            else:
                data = fpath.read_bytes()
                ctype = "text/csv" if name.endswith(".csv") else "application/octet-stream"
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Disposition", f"attachment; filename={name}")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        elif path == "/api/stm32/ads1115":
            host, err = self._master_host()
            self._json({"ok": False, "error": err} if err else stm32_ads1115(host))
        elif path == "/api/stm32/hv-status":
            host, err = self._master_host()
            self._json({"ok": False, "error": err} if err else stm32_hv_status(host))
        elif path == "/api/stm32/ds3502":
            host, err = self._master_host()
            self._json({"ok": False, "error": err} if err else stm32_ds3502_get(host, self._query().get("ch", "ev")))
        elif path == "/api/sync/status":
            host, err = self._target_host()
            self._json({"ok": False, "error": err} if err else sync_get_status(host))
        elif path == "/api/stm32/hv-target":
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
        elif path == "/api/run-status":
            # Poll ShvGetStatus (0x79) from each connected controller. totalPulsesDone
            # is the shared global playhead; filamentIndex is the live firing filament.
            out = {}
            for k, c in CONTROLLERS.items():
                if not c.client.connected:
                    out[str(k)] = {"connected": False}
                    continue
                try:
                    st = decode_shv_status(c.request(SHV_GET_STATUS, b"", timeout=1.0))
                    out[str(k)] = {"connected": True, "status": st}
                except Exception as exc:
                    out[str(k)] = {"connected": True, "error": str(exc)}
            self._json({"controllers": out})
        else:
            self._serve_static(path)

    # --- POST ---------------------------------------------------------------
    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        body = self._read_json()
        try:
            if path == "/api/connect":
                cid = int(body.get("controller", 0))
                link = CONTROLLERS.get(cid)
                if not link:
                    return self._json({"ok": False, "error": "bad controller"}, HTTPStatus.BAD_REQUEST)
                host = str(body.get("host", "")).strip()
                if not host:
                    return self._json({"ok": False, "error": "no host"}, HTTPStatus.BAD_REQUEST)
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
                if "assignment" in body:
                    MAPPING.set_assignment(body["assignment"], body.get("group_size"))
                elif "group_size" in body:
                    MAPPING.set_default(int(body["group_size"]))
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
                channels = body.get("channels") or DEFAULT_CHANNELS
                results = []
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results.append(download_to_controller(link, cid - 1, plan, channels))
                    except Exception as exc:
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
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                if not results:
                    return self._json({"ok": False, "error": "no controller connected"}, HTTPStatus.OK)
                self._json({"ok": all(r.get("match") for r in results.values()), "results": results})
            elif path == "/api/arm":
                repeats = int(body.get("repeats", 1))
                payload = _u16(max(1, repeats))
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
                results = {}
                for cid, link in CONTROLLERS.items():
                    if not link.client.connected:
                        continue
                    try:
                        results[str(cid)] = {"ok": _status_ok(link.request(SHV_DISARM, b"", flags=0))}
                    except Exception as exc:
                        results[str(cid)] = {"ok": False, "error": str(exc)}
                self._json({"ok": True, "results": results})
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
                self._json({"ok": all(r.get("ok") for r in results.values()), "results": results,
                            "failed": failed, "applied": applied})
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
                        res = {"channel_mask": read_channel_mask(link)}
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
            elif path == "/api/channel-mask":
                # Set the channel enable mask on connected controllers (0x34).
                mask = int(body.get("mask", 0x3F)) & 0xFF
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
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ring_start(host, int(body.get("rate", 1000000))))
            elif path == "/api/adc/ring-stop":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_ring_stop(host))
            elif path == "/api/adc/pulse-arm":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_pulse_arm(host, int(body.get("rate", 1000000))))
            elif path == "/api/adc/pulse-disarm":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(adc_pulse_disarm(host))
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
                self._json(ESPCMD.disarm())
            elif path == "/api/record/start":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                rate = max(1, int(body.get("rate", 1000000)))
                decim = max(1, int(body.get("decim", 1)))
                self._json(RECORDER.start(host, rate, decim))
            elif path == "/api/record/stop":
                self._json(RECORDER.stop())
            elif path == "/api/stm32/ds3502-set":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(stm32_ds3502_set(host, str(body.get("ch", "ev")), int(body.get("wiper", 0))))
            elif path == "/api/stm32/hv-enable":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(stm32_hv_enable_set(host, str(body.get("ch", "emission")), bool(body.get("on"))))
            elif path == "/api/stm32/hv-set-target":
                host, err = self._master_host()
                if err:
                    return self._json({"ok": False, "error": err}, HTTPStatus.OK)
                self._json(stm32_hv_set_target(host, str(body.get("chan", "emission")),
                                               int(body.get("target", 0)), int(body.get("tol", 4)),
                                               int(body.get("max_step", 1))))
            elif path == "/api/stm32/hv-clear-target":
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
            elif path == "/api/shv":
                link = CONTROLLERS.get(int(body.get("controller", 0)))
                if not link or not link.client.connected:
                    return self._json({"ok": False, "error": "controller not connected"}, HTTPStatus.OK)
                self._json(shv_op(link, body))
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
    host = os.environ.get("CT_GUI_HOST", "127.0.0.1")
    port = int(os.environ.get("CT_GUI_PORT", "8770"))
    server = ThreadingHTTPServer((host, port), CtHandler)
    print(f"CT GUI server listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for c in CONTROLLERS.values():
            c.disconnect()


if __name__ == "__main__":
    main()
