"""backend: constants, opcodes, power states, logging setup.

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



FILAMENTS_PER_CONTROLLER = 48     # nominal (alt-12 default: 4 groups of 12 / controller)


SCOPE_PER_CONTROLLER = 64         # power slots per controller (8 channels x 8 positions)


FILAMENT_COUNT = 96               # global filament indices 0..95


POWER_SLOTS = 64                  # firmware kSimpleHvFilamentsPerController


NO_FILAMENT = 0xFF                # active-list "unused power slot" sentinel


DEFAULT_GROUP_SIZE = 12           # alternating-12: 0-11->P1, 12-23->P2, 24-35->P1, ...


DEFAULT_CHANNELS = [0, 1, 2, 3, 4, 5]   # legacy default (channels now derived from MAPPING)


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


DEAD_STATE_PATH = STATE_DIR / "dead_fids.json"


TPS_STATUS_TIMEOUT_S = 8.0   # bulk CH_GET_TPS_STATUS (verify only) -- see read_tps_status()


BRIDGE_DOWN_REMIND_S = 600   # while a controller stays unreachable, re-log it this often


FLAG_SINGLE = 0x10  # kTargetIsSingleBoard


ALL_BOARDS_MASK = bytes([0xFF] * 8)


PING_TYPE = 0x01


PING_PAYLOAD = (0xCAFEF00D).to_bytes(4, "little")


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


CH_GET_CACHED_CURRENTS = 0x3A    # CC-loop cached currents, NO I2C (run-safe telemetry)


CH_GET_BOARD_CACHE = 0x3D        # every board's cached V/I/presence, NO I2C (RP2350 fw: idle monitor)
CH_GET_BOARD_HEALTH = 0x3E       # one channel's dark / lost / recovering state, NO I2C (RP2350 fw)


CH_GET_PRESENT = 0x25            # I2C presence scan (mux/tps/ina/io per board)


CH_GET_DIAGNOSIS = 0x2E         # deep diagnosis: addr-ACK / reg-read / operational


CH_GET_I2C_ENABLE_MASK = 0x2F   # read the channel enable mask


CH_RESET_MUX = 0x5F             # pulse TCA9548A reset + re-detect (power-cutting)


CH_TCA9554_SELF_TEST = 0x60     # per-pin TCA9554 toggle test


CH_READ_TCA9554 = 0x61          # read-only TCA9554 Config/Input/Output dump


CH_GET_BOARD_BITMAPS = 0x26     # iso/tps enable + tps fault + hv overcurrent masks


SET_EVENT_CONFIG = 0x06


SHV_EMIT_CHUNK = 64    # emission entries per frame (64*4+3 = 259 B). Bigger chunks


SHV_HEAT_CHUNK = 56    # firmware caps ShvHeatSetEntries at 56


EVENT_TELEMETRY_ENABLE_BIT = 0x08     # kEventEnableTelemetry (1<<3)


TELEMETRY_MODE_CACHED = 2             # firmware kTelemetryModeCached


SCAN_TELEMETRY_PERIOD_MS = 50         # ~20 fps push cadence


class PowerState(enum.IntEnum):
    """The power ladder, as an enum rather than six loose integers.

    IntEnum, not Enum: every one of these already travels over the wire and
    through JSON as a plain number, and an IntEnum member IS that number --
    `PowerState.SLEEP == 2` is True and json.dumps emits `2`. So this is a
    readability change with no protocol change, and the existing
    POWER_STATE_* spellings below keep working unchanged.

    Nothing here should be spelled as a bare digit again. A fallback state
    written as `2` in a config, a log line or a docstring is one nobody can
    check without going to find the table.
    """
    STOP = 1
    SLEEP = 2
    STANDBY = 3
    IDLE = 4
    ACTIVE = 5
    VOLTAGE = 6

    @property
    def energising(self) -> bool:
        """True if this state puts power ON the filament.

        STANDBY counts: it enables the output at the firmware's 0.8 V floor
        (~0.9 A into a real filament), so it is NOT a no-power state. STOP and
        SLEEP leave the output off.
        """
        return self >= PowerState.STANDBY

    def __str__(self) -> str:          # "SLEEP(2)" everywhere one is printed
        return f"{self.name}({self.value})"


# Long-standing spellings, kept so nothing downstream has to change at once.
POWER_STATE_STOP = PowerState.STOP


POWER_STATE_SLEEP = PowerState.SLEEP


POWER_STATE_STANDBY = PowerState.STANDBY


POWER_STATE_IDLE = PowerState.IDLE


POWER_STATE_ACTIVE = PowerState.ACTIVE


POWER_STATE_VOLTAGE = PowerState.VOLTAGE


ENERGISING_STATES = frozenset(s for s in PowerState if s.energising)


# Derived, not written out a second time: two hand-maintained copies of the
# same ladder is how one of them ends up wrong.
POWER_STATE_NAMES = {int(s): s.name for s in PowerState}


ACTIVE_FLOOR_MA = 1500


# The RP2350's own IDLE ceiling (tps55289_board_constants.h kIdleMaxMilliamps).
# Mirrored here to REFUSE, because the firmware CLAMPS: setPowerState() does
#     if (state == Idle && targetMa > kIdleMaxMilliamps) targetMa = kIdleMax...
# silently, so an IDLE 2500 mA request comes back ok, runs at 2000, and a
# verify=True wait for 2500 never arrives with nothing anywhere saying why.
# A refusal names the limit; a clamp hides it behind a legal-looking value.
#
# Note the two bounds are NOT a dividing line: 1500 is also the firmware's
# kIdleCurrentMaDefault, so 1500-2000 mA is legal for IDLE and for ACTIVE both.
IDLE_CEILING_MA = 2000


UART_STATUS_NAMES = {0: "Ok", 1: "BadFrame", 2: "BadArgument", 3: "Busy",
                     4: "Unsupported", 5: "NotReady", 6: "I2cError",
                     7: "OutOfRange", 8: "VerifyFail"}


_HV_FULL_V: dict[str, float] = {"emission": 350.0, "focus": 495.0}


_HV_DS_CH:  dict[str, str]   = {"emission": "ev",  "focus": "fv"}


_EM_I_FULL_MA = 85.7   # mA at wiper 127 on the "ei" DS3502 channel


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


# ── Dead-man safety watchdog ───────────────────────────────────────────────
# A client that dies mid-run leaves the hardware where it was: a filament at
# ACTIVE and HV grid MOSFETs closed, with nothing left to open them. Nothing
# in the lease covers this -- the lease expires, but expiry only frees WRITE
# ACCESS; it has never de-energised anything.
#
# So the backend, which outlives the client and sees every command, holds a
# dead-man timer. Miss it and the hardware is walked back on its own.
#
# WHAT COUNTS AS BEING CONTROLLED: commands, never reads. This is the whole
# design and it is not negotiable -- the GUI polls continuously and must keep
# doing so (it is the monitoring interface and has to coexist with any script),
# so if a read renewed the timer an open browser tab would hold a filament at
# firing current indefinitely with nobody in the room. Only something that
# ASKS the hardware to do something renews, plus an explicit keepalive for a
# caller that is legitimately holding a state while doing its own work.
SAFETY_ACTIVE_TIMEOUT_S = 30.0    # ACTIVE with nobody commanding -> fall back


SAFETY_ACTIVE_FALLBACK = 2        # ...to SLEEP. Not STOP: a re-warm is slow,


                                   # and SLEEP already removes the current.
                                   # Must be a non-energising state or the
                                   # timer would "fire" into another hazard.
SAFETY_HV_TIMEOUT_S = 10.0        # HV grid MOSFET closed with nobody commanding


                                   # -> every MOSFET opened (SHV_DISARM clear-all).
                                   # The watchdog NEVER touches the emission or
                                   # focus rails: turning HV off is the
                                   # operator's decision, never a timer's. It
                                   # used to drop both rails, so with the GUI
                                   # only polling, emission went off by itself
                                   # hv_timeout_s after every turn-on.
SAFETY_TICK_S = 1.0               # how often the timer is checked


LOCK_TTL_DEFAULT_S = 30.0


LOCK_TTL_MAX_S = 600.0


POLL_PAUSE_MAX_S = 15.0   # max time a background-PING pause survives without a re-arm


# ---------------------------------------------------------------------------
# Logging. There was none: everything went to stdout and died with the terminal,
# so a fault that happened overnight -- or a 40 kV arc that reset the STM32 while
# nobody was watching -- left no record at all. File-backed now, with the console
# output preserved so nothing that used to be visible stops being visible.
log = logging.getLogger("ct_gui")


class _DailySizeRotatingHandler(logging.handlers.TimedRotatingFileHandler):
    """Roll at midnight AND at a size cap, keeping the date in the filename.

    Size-only rotation (what this used to do) never produces a huge file, but
    `backend.log.3` does not say which day it covers -- finding "what happened
    on the 17th" means opening files and guessing from their contents. Date-only
    rotation fixes that and reintroduces the unbounded-file problem for a
    chatty day. Neither alone is right, and the stdlib has no handler that does
    both.

    Same-day size rolls get a numeric suffix (backend.log.2026-09-18.1) instead
    of overwriting: TimedRotatingFileHandler deletes an existing destination,
    which on a second roll within one day would silently discard that day's
    earlier entries -- losing log lines to make room for log lines.
    """

    def __init__(self, filename, max_bytes: int, backup_count: int, encoding=None):
        super().__init__(filename, when="midnight", backupCount=backup_count,
                         encoding=encoding, utc=False)
        self.max_bytes = max_bytes

    def shouldRollover(self, record) -> int:
        if super().shouldRollover(record):
            return 1
        if self.max_bytes <= 0:
            return 0
        if self.stream is None:
            self.stream = self._open()
        self.stream.seek(0, 2)
        return 1 if self.stream.tell() + len(self.format(record)) + 1 >= self.max_bytes else 0

    def rotation_filename(self, default_name: str) -> str:
        # Only ever called with the dated name; disambiguate a same-day repeat.
        if not os.path.exists(default_name):
            return default_name
        n = 1
        while os.path.exists(f"{default_name}.{n}"):
            n += 1
        return f"{default_name}.{n}"


def _setup_logging() -> None:
    LOG_DIR.mkdir(exist_ok=True)
    log.setLevel(logging.INFO)
    if log.handlers:
        return
    # 14 days kept. backupCount counts FILES, and a size roll makes an extra one
    # for that day, so this is "at least two weeks" rather than exactly 14 days.
    fh = _DailySizeRotatingHandler(
        LOG_DIR / "backend.log", max_bytes=4_000_000, backup_count=14, encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)-5s %(message)s",
                                      "%Y-%m-%d %H:%M:%S"))
    log.addHandler(fh)
    log.propagate = False


# Does this firmware flag a FAILED single-board 0x3A sample, or return a bare 0?
# Keyed by controller; None = not probed yet. See _single_read_is_trusted().
_SINGLE_0X3A_TRUSTED: dict[int, bool] = {}


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
_DEAD_LOCK = threading.Lock()


def parse_power_state(value) -> tuple["PowerState | None", str]:
    """Accept a PowerState, its number, or its name. Returns (state, why).

    Names are case-insensitive. On failure the state is None and `why` lists
    what WOULD have been accepted -- an error that names the field but not its
    legal values makes the reader go find the table, which is the thing an
    enum is supposed to have ended.
    """
    if isinstance(value, PowerState):
        return value, ""
    if isinstance(value, str):
        key = value.strip().upper()
        for st in PowerState:
            if st.name == key:
                return st, ""
        if not key.lstrip("+-").isdigit():
            return None, (f"{value!r} is not a power state — expected one of "
                          f"{', '.join(s.name for s in PowerState)}")
        value = key
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None, (f"{value!r} is not a power state — expected a name "
                      f"({', '.join(s.name for s in PowerState)}) or its number")
    try:
        return PowerState(n), ""
    except ValueError:
        return None, (f"{n} is not a power state — the ladder is "
                      f"{', '.join(str(s) for s in PowerState)}")


def power_state_name(state: int) -> str:
    """"STANDBY(3)" for a known state, "3" for anything else. Never invents a
    name for a value the ladder does not define."""
    n = POWER_STATE_NAMES.get(int(state))
    return f"{n}({int(state)})" if n else str(state)


# fid -> (state, monotonic when it was commanded). The backend is the ONLY
# writer to the bridge (single-client TCP), so what it last commanded is what
# the hardware has -- except across a reconnect, where the board may have been
# reflashed and reset to STOP. Cleared there, and an UNKNOWN state refuses
# ACTIVE rather than allowing it: not knowing must not read as permission.
LAST_POWER_STATE: dict[int, tuple[int, float]] = {}


_ORDER_LOCK = threading.Lock()


# ESP32 framed-protocol client (TCP 3334) for the Mode-2 fire-correlated
# per-pulse source: RING_PULSE_ARM/DISARM + pushed RING_PULSE_EVENT frames.
ESPCMD = EspCmdClient()


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
    "_ORDER_LOCK", "_SINGLE_0X3A_TRUSTED", "_TPS_IOUT_LIMIT_REG", "_setup_logging",
    "adc_get_burst", "adc_pulse_arm", "adc_pulse_diag", "adc_pulse_disarm",
    "adc_ready_arm", "adc_ready_disarm", "adc_ready_renew", "adc_ready_status",
    "adc_ring_peek", "adc_ring_start", "adc_ring_stop", "adc_ring_window",
    "adc_ring_window_data", "adc_spi_shot_arm", "adc_spi_shot_data", "annotations",
    "build_command_payload", "copy", "csv", "datetime", "enum", "fetch_bridge_info",
    "fetch_stm32_status", "json", "log", "logging", "os", "parse_power_state",
    "power_state_name", "primary_local_ip", "pulse_events_get", "scan_for_bridge",
    "stm32_adc_window", "stm32_ads1115", "stm32_ds3502_get", "stm32_ds3502_set",
    "stm32_hv_clear_target", "stm32_hv_enable_set", "stm32_hv_get_target",
    "stm32_hv_set_target", "stm32_hv_status", "sync_get_burst_status",
    "sync_get_status", "sync_post_abort", "sync_post_burst", "sync_post_burst_stop",
    "sync_post_config", "sync_post_fire", "threading", "time",
]
