"""backend: recorders: ADC measurement, run reports, detector arm/disarm, scans.

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
        # Recorded pulses whose background_n was 0 — i.e. whose `integral` is
        # the raw in-envelope sum with nothing subtracted. Counted so a run
        # that recorded nothing but unusable charges says so while it is
        # running, instead of at whatever point someone opens the .csv.
        self.pulse_no_bg = 0
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
            self.pulse_no_bg = 0
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
                #
                # background_n / background_gap ride along with every row because
                # integral = Σx − (F−R)·mean_bg: the charge is only as good as the
                # background that was subtracted from it, and a .csv that records
                # the charge but not that is a file of numbers nobody can qualify
                # later. background_n 0 means NO background was measured — that
                # row's `integral` is the raw in-envelope sum with nothing taken
                # off, i.e. not a charge — and an EMPTY cell means the firmware
                # does not report the field at all. Those are different: do not
                # read a blank as 0 when analysing these files.
                f.write("id,t_us,on_us,peak,plateau,bg,post_bg,bg_sigma4,integral,"
                        "background_n,background_gap,rate_hz,recv_ms\n")
                since = 0
                while not self._pulse_stop.is_set():
                    r = pulse_events_get(host, since)
                    if r.get("ok"):
                        evs = r.get("events") or []
                        for e in evs:
                            def _cell(v):
                                # None -> empty cell, never 0: post_bg, rate_hz
                                # and background_n/background_gap are all
                                # legitimately absent on older firmware, and 0 is
                                # a real value for every one of them (a real
                                # post-pulse current, and a real "no background
                                # was measured" / "no settle gap was left").
                                return "" if v is None else v
                            f.write("{id},{t_us},{on_us},{peak},{plateau},{bg},{post_bg},{bg_sigma4},{integral},"
                                    "{background_n},{background_gap},{rate_hz},{recv_ms}\n".format(
                                id=e.get("id", ""), t_us=e.get("t_us", ""), on_us=e.get("on_us", ""),
                                peak=e.get("peak", ""), plateau=e.get("plateau", ""), bg=e.get("bg", ""),
                                post_bg=_cell(e.get("post_bg")),
                                bg_sigma4=e.get("bg_sigma4", ""),
                                integral=e.get("integral", ""),
                                background_n=_cell(e.get("background_n")),
                                background_gap=_cell(e.get("background_gap")),
                                rate_hz=_cell(e.get("rate_hz")),
                                recv_ms=e.get("recv_ms", "")))
                            since = e.get("id", since)
                            self.pulse_count += 1
                            # `is not None and == 0`, never a bare falsy test:
                            # None (firmware does not report it) must not be
                            # counted as "no background was measured".
                            bn = e.get("background_n")
                            if bn is not None and int(bn) == 0:
                                self.pulse_no_bg += 1
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
                    "no_background": self.pulse_no_bg,
                    "pulse_file": self.pulse_path.name if self.pulse_path else None,
                    "still_armed_for": still_armed_for}

    def status(self) -> dict[str, Any]:
        dur = (time.time() - self.started) if (self.started and self.active) else None
        return {"recording": self.active, "pulse_events": self.pulse_count,
                "no_background": self.pulse_no_bg,
                "duration_s": round(dur, 1) if dur else None,
                "pulse_file": self.pulse_path.name if self.pulse_path else None,
                "error": self._last_error}


RECORDER = MeasurementRecorder()


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
        d = RUN_REPORT_DIR
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
    "LOCK_TTL_MAX_S", "LOG_DIR", "MeasurementRecorder", "NO_FILAMENT", "PING_PAYLOAD",
    "PING_TYPE", "POLL_PAUSE_MAX_S", "POWER_SLOTS", "POWER_STATE_ACTIVE",
    "POWER_STATE_IDLE", "POWER_STATE_NAMES", "POWER_STATE_SLEEP", "POWER_STATE_STANDBY",
    "POWER_STATE_STOP", "POWER_STATE_VOLTAGE", "Path", "PowerState", "RECORDER",
    "RECORD_DIR", "RUN_RECORDER", "RUN_REPORT_DIR", "RunRecorder",
    "SAFETY_ACTIVE_FALLBACK", "SAFETY_ACTIVE_TIMEOUT_S", "SAFETY_HV_TIMEOUT_S",
    "SAFETY_TICK_S", "SCAN_TELEMETRY_PERIOD_MS", "SCOPE_PER_CONTROLLER",
    "SET_EVENT_CONFIG", "SHV_ARM", "SHV_CAPABILITY", "SHV_CLEAR_TABLE", "SHV_DISARM",
    "SHV_EMIT_CHUNK", "SHV_FAULT_POLICY", "SHV_GET_ACTIVE_LIST", "SHV_GET_CONFIG",
    "SHV_GET_PULSE_LOG", "SHV_GET_STATUS", "SHV_GET_TABLE_INFO", "SHV_HEAT_CHUNK",
    "SHV_HEAT_CLEAR", "SHV_HEAT_GET_INFO", "SHV_HEAT_SET_ENTRIES",
    "SHV_SET_ACTIVE_LIST", "SHV_SET_CONFIG", "SHV_SET_ENTRIES", "SHV_TRIGGER_DELAY",
    "STATE_DIR", "STATIC_DIR", "TELEMETRY_MODE_CACHED", "TPS_STATUS_TIMEOUT_S",
    "TYPE_NAMES", "TcpProtocolClient", "ThreadingHTTPServer", "UART_STATUS_NAMES",
    "_DEAD_LOCK", "_DETECTOR_LOCK", "_DETECTOR_USERS", "_DIAG_CHIPS",
    "_DailySizeRotatingHandler", "_EM_I_FULL_MA", "_HV_DS_CH", "_HV_FULL_V",
    "_OCP_MA_PER_CODE", "_OCP_SENSE_RESISTOR_OHMS", "_ORDER_LOCK",
    "_SINGLE_0X3A_TRUSTED", "_TPS_IOUT_LIMIT_REG", "_adc_window_autoarm",
    "_detector_is_converting", "_setup_logging", "_write_run_report", "adc_get_burst",
    "adc_pulse_arm", "adc_pulse_diag", "adc_pulse_disarm", "adc_ready_arm",
    "adc_ready_disarm", "adc_ready_renew", "adc_ready_status", "adc_ring_peek",
    "adc_ring_start", "adc_ring_stop", "adc_ring_window", "adc_ring_window_data",
    "adc_spi_shot_arm", "adc_spi_shot_data", "annotations", "build_command_payload",
    "copy", "csv", "datetime", "detector_arm", "detector_disarm", "do_scan", "enum",
    "fetch_bridge_info", "fetch_stm32_status", "json", "log", "logging", "os",
    "parse_power_state", "power_state_name", "primary_local_ip", "pulse_events_get",
    "scan_for_bridge", "stm32_adc_window", "stm32_ads1115", "stm32_ds3502_get",
    "stm32_ds3502_set", "stm32_hv_clear_target", "stm32_hv_enable_set",
    "stm32_hv_get_target", "stm32_hv_set_target", "stm32_hv_status",
    "sync_get_burst_status", "sync_get_status", "sync_post_abort", "sync_post_burst",
    "sync_post_burst_stop", "sync_post_config", "sync_post_fire", "threading", "time",
]
