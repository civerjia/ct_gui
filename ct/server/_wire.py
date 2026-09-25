"""backend: frame building/decoding helpers.

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


def _u16(v: int) -> bytes:
    return int(v).to_bytes(2, "little")


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
    if command == "SHV_GET_STATUS":          # 0x79: empty -> status payload
        # Read-only. Exposed through /api/cmd so the RAW bytes can be inspected
        # when the payload grows a field -- decode_shv_status only ever returns
        # what it already knows how to read, so a newly appended field is
        # invisible through it by construction.
        return 0x79, 0, b""
    # Diagnostics (RP2350 fw 00397fe+). None of them does I2C except PIN_PROBE's
    # pin drive; the two pin commands are refused (Busy) while a run owns it.
    if command == "CH_GET_BOARD_HEALTH":     # 0x3E: [ch] -> dark + per-board lost/recovering
        return 0x3E, 0, bytes([ch])
    if command == "CH_GET_I2C_STATS":        # 0x3F: [ch, clear] -> bus counters (clear AFTER reading)
        return 0x3F, 0, bytes([ch, 1 if b.get("clear") else 0])
    if command == "GET_PIN_REPORT":          # 0x62: [page] -> every used GPIO (page 0 measures)
        return 0x62, 0, bytes([int(b.get("page", 0)) & 0xFF])
    if command == "PIN_PROBE":               # 0x63: drives the HV chain's SAFE pins (lease needed)
        return 0x63, 0, b""
    if command == "CH_SLEW_RATE":            # 0x3C: empty=GET, 6 bytes=SET
        # Three voltage-ramp slew rates, runtime-settable. Out-of-range CLAMPS
        # rather than rejecting, and the response is always the values IN FORCE
        # -- so a SET is its own read-back and must be read, never assumed.
        #
        # The configured number IS the real instantaneous dV/dt -- nothing to
        # scale. An earlier firmware halved it (the ramp's step clock was reset
        # on every target increase, so accumulated step credit was discarded),
        # which looked like a clean 0.44 factor because it scaled linearly. Bug,
        # fixed. Ceilings: below 2000, above/warm 5000. Defaults are NOT the
        # ceilings -- see CTClient.SLEW_DEFAULTS.
        if b.get("below_mV_per_s") is None:
            return 0x3C, 0, b""
        return 0x3C, 0, (_u16(int(b["below_mV_per_s"]))
                         + _u16(int(b["above_mV_per_s"]))
                         + _u16(int(b["warm_mV_per_s"])))
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


def _u32(v: int) -> bytes:
    return int(v).to_bytes(4, "little")


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
        # mismatches: pulses whose 165 READ-BACK did not equal the COMMANDED
        # byte -- the per-pulse hardware verify (the PIO samples the 595 outputs
        # through the 165 mid-pulse and compares). Per run, not cumulative:
        # arm() calls resetRuntime_(), so every arm starts from zero.
        #
        # A NON-ZERO VALUE IS A REAL VERIFY FAILURE. It briefly was not: for one
        # firmware revision a cross-channel schedule reported exactly one
        # mismatch per channel boundary, because the channel select was applied
        # in the trigger ISR and the 165 sample ~10 us later found the mux
        # already moved, reading 0x00. Fixed at the source (RP2350 32c70c9) --
        # hv_shift now raises a PIO IRQ once the read-back has been pushed and
        # the mux moves from that, so the verification actually happens. Measured
        # here after the fix: 0 / 3 / 15 crossings all report 0 mismatches with
        # pulse-log flags 0.
        #
        # Kept as a note because it is the shape to watch for, not because the
        # behaviour is still here: if mismatches ever tracks the number of
        # channel boundaries again, that is the regression, not a property of
        # cross-channel schedules.
        #
        # There is exactly ONE read-back per pulse (post-ON, mid-pulse). The
        # PostOnly / PrePost / PrePostLevel modes in the protocol belong to the
        # legacy HvScheduleEngine, which is not ticked -- so there is no second
        # read to fall back on, which is why the mux timing mattered at all.
        "mismatches": le32(35) if len(p) >= 39 else None,
        # rbSaturated (firmware 7528a75+): times the 165 read-back FIFO was
        # found full when a pulse was accounted. NOT independent of
        # uncounted -- a discarded read-back makes the FIFO level understate
        # how many pulses fired, so when rbSaturated>0 the recovery
        # under-credits. Per the RP2350 session: uncounted is exact only
        # when rbSaturated==0; otherwise treat it as a LOWER BOUND, not a
        # count.
        "rbSaturated": le32(39) if len(p) >= 43 else None,
        # unsafeSlots (u64 bitmap of POWER SLOTS, not filaments): which
        # scheduled slots arm() SKIPPED because they failed its safety gate
        # (IsoOff — the board's isolated 12 V rail is not on). Only ever
        # non-zero under the CONTINUE fault policy; STOP refuses the arm
        # instead. None on firmware that does not report it — absent, not
        # "nothing skipped", because those mean opposite things here.
        #
        # This is the ONLY way to tell a skipped filament from a fired one.
        # arm returns reject 0, the run proceeds, and the envelope still
        # fires for the counted trigger so the pulse index stays aligned —
        # so a skipped filament looks like a successful shot from every
        # other field. Check this before believing a pulse reached a
        # filament.
        "unsafeSlots": (int.from_bytes(p[43:51], "little")
                        if len(p) >= 51 else None),
        # off_mismatches: pulses whose OFF read-back was non-zero -- the
        # per-run count of the pulse-log's 0x02 bit, i.e. THE HV DID NOT TURN
        # OFF. The safety-relevant counter, and the only one here that is about
        # the pulse rather than its verification.
        #
        # This was already on the wire before this decoder learned about it,
        # which is how it got mistaken for a newly appended field: the payload
        # length had moved for a reason that had nothing to do with the change
        # being investigated. A healthy run reads 0 here, so the mistake showed
        # no symptom.
        "off_mismatches": le32(51) if len(p) >= 55 else None,
        # rb_dropped: the read-back ring OVERRAN -- real data loss, the CPU fell
        # behind. rb_stale: samples discarded because their pulse had already
        # been reported unverified, i.e. the pairing RECOVERING as designed
        # (about two per unverified pulse).
        #
        # These must not be conflated, and rb_stale is not interpretable alone:
        # rb_stale > 0 with rb_dropped == 0 is a HEALTHY run that absorbed a
        # late pulse. Reporting that as an error would turn the framing fix's
        # own recovery mechanism into an alarm.
        #
        # BOTH ARE CUMULATIVE -- arm does NOT zero them, unlike mismatches /
        # uncounted / underfed / triggerEdges, which resetRuntime_() clears on
        # every arm. Read them before and after and use the DELTA. Dividing the
        # absolute rb_stale by this run's unverified count gives a ratio that
        # climbs run over run and looks like a defect; by delta it is the ~2 per
        # unverified pulse it should be. Same trap as faultedFilaments.
        "rbDropped": le32(55) if len(p) >= 59 else None,
        "rbStale": le32(59) if len(p) >= 63 else None,
        # rb_irqs: pulses the HARDWARE actually fired this run (the shift SM
        # raises irq 0 after both read-back pushes, unconditionally). Per-run.
        #
        # THE INVARIANT IS `totalPulsesDone == rbIrqs` -- the CPU's bookkeeping
        # agreeing with what the hardware did. That is what the accounting fix
        # ties together, and it is the one line worth asserting on after a run.
        #
        # It is NOT `done == triggerEdges == rbIrqs`. triggerEdges counts edges
        # the ISR SAW, and on the real pin path notePrecisionTrigger() has no
        # isParked() guard -- an edge arriving mid-pulse is counted and fires
        # nothing. So triggerEdges >= done is NORMAL and means the trigger
        # source is running faster than width + recovery, not that pulses were
        # lost. Measured on the RP2350 side: done 32, edges 36, rbIrqs 32 with
        # 4 deliberate over-triggers.
        #
        # And the excess is INVISIBLE from here: the simulated-trigger path this
        # host uses DOES guard on isParked() and returns before counting, so
        # over-triggering never shows up in these numbers. Equal done/edges out
        # of /api/sync/simulate is not evidence that a real pin-triggered run
        # would be equal too.
        "rbIrqs": le32(63) if len(p) >= 67 else None,
    }


def _is_read_command(name: str) -> bool:
    """True for the protocol commands that only READ (CH_GET_*, HV_GET_*,
    ShvGetStatus, …). Those stay allowed while another client holds the lease —
    a lease reserves the right to CHANGE the hardware, not to look at it."""
    n = (name or "").upper()
    if "SET" in n or "WRITE" in n or "CLEAR" in n:
        return False
    return "GET" in n or "READ" in n


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
    "FILAMENT_COUNT", "FLAG_SINGLE", "GEOMETRY", "HTTPStatus", "HV_REFRESH_FEEDBACK",
    "HV_SET_SHIFT_HZ", "IDLE_CEILING_MA", "LAST_POWER_STATE", "LOCK_TTL_DEFAULT_S",
    "LOCK_TTL_MAX_S", "LOG_DIR", "NO_FILAMENT", "PING_PAYLOAD", "PING_TYPE",
    "POLL_PAUSE_MAX_S", "POWER_SLOTS", "POWER_STATE_ACTIVE", "POWER_STATE_IDLE",
    "POWER_STATE_NAMES", "POWER_STATE_SLEEP", "POWER_STATE_STANDBY", "POWER_STATE_STOP",
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
    "_DEAD_LOCK", "_DIAG_CHIPS", "_DailySizeRotatingHandler", "_EM_I_FULL_MA",
    "_HV_DS_CH", "_HV_FULL_V", "_OCP_MA_PER_CODE", "_OCP_SENSE_RESISTOR_OHMS",
    "_ORDER_LOCK", "_SINGLE_0X3A_TRUSTED", "_TPS_IOUT_LIMIT_REG", "_coerce_bytes",
    "_coerce_int", "_is_read_command", "_le", "_popcount", "_setup_logging",
    "_status_err", "_status_ok", "_u16", "_u32", "_unpack_spi_shot", "adc_get_burst",
    "adc_pulse_arm", "adc_pulse_diag", "adc_pulse_disarm", "adc_ready_arm",
    "adc_ready_disarm", "adc_ready_renew", "adc_ready_status", "adc_ring_peek",
    "adc_ring_start", "adc_ring_stop", "adc_ring_window", "adc_ring_window_data",
    "adc_spi_shot_arm", "adc_spi_shot_data", "annotations", "build_command_payload",
    "build_payload", "copy", "csv", "datetime", "decode_shv_status", "enum",
    "fetch_bridge_info", "fetch_stm32_status", "json", "log", "logging", "os",
    "parse_power_state", "power_state_name", "primary_local_ip", "pulse_events_get",
    "scan_for_bridge", "stm32_adc_window", "stm32_ads1115", "stm32_ds3502_get",
    "stm32_ds3502_set", "stm32_hv_clear_target", "stm32_hv_enable_set",
    "stm32_hv_get_target", "stm32_hv_set_target", "stm32_hv_status",
    "sync_get_burst_status", "sync_get_status", "sync_post_abort", "sync_post_burst",
    "sync_post_burst_stop", "sync_post_config", "sync_post_fire", "threading", "time",
]
