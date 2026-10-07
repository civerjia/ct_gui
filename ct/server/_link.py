"""backend: ControllerLink -- one bridge connection, with its poll thread.

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
from collections import deque
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


#: Timeouts for successive tries of a request that is safe to repeat. A late
#: reply is the normal failure on this link -- the master ESP32 has been seen
#: holding an RP2350 reply for 0.72 s while it served STM32 HTTP traffic, and
#: the RP2350 itself answers in >1 s while every board runs its CC loop -- so a
#: single 1 s try turned a slow answer into "cannot confirm", "disarm failed"
#: and failed downloads (2026-10-06, liuxing_api run: 9 + 7 + many).
RETRY_TIMEOUTS_S = (1.0, 2.0, 3.0)


def request_retry(link, frame_type: int, payload: bytes = b"", flags: int = 0,
                  timeouts=RETRY_TIMEOUTS_S, ok=None):
    """link.request() for a frame that is safe to send again (a read, a disarm,
    an idempotent write): one try per entry of `timeouts`, each longer than the
    last. `ok(resp)` decides success (default: any decoded response). Raises the
    last error if every try fails -- never returns a made-up answer."""
    last: Exception | None = None
    for t in timeouts:
        try:
            resp = link.request(frame_type, payload, flags=flags, timeout=t)
        except Exception as exc:
            last = exc
            continue
        if ok is None or ok(resp):
            return resp
        last = RuntimeError(f"0x{frame_type:02X} answered but not OK: {resp.get('raw') if isinstance(resp, dict) else resp}")
    raise last if last else RuntimeError("no try made")


def shv_status_retry(link):
    """ShvGetStatus (0x79), retried (request_retry). Raises if no try answered:
    callers that gate on "is a schedule running?" must still FAIL CLOSED."""
    return decode_shv_status(request_retry(link, SHV_GET_STATUS, b""))


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
        # Schedule ops (disarm/download/verify/arm) take precedence over the
        # board monitor: every request on this link queues on one lock, and a
        # monitor read the RP2350 answers slowly made each fire step wait
        # behind it. Until this deadline the monitor sends nothing here.
        self.monitor_hold_until = 0.0
        # The bridge ESP32's own link: /wifi/diag every WIFI_DIAG_PERIOD_S, and
        # the round trip of every HTTP request made to it (the 1 Hz /stm32
        # read included) -- the ESP32 alone, to tell a weak WiFi from a busy
        # RP2350 behind it. (monotonic t, ms or None for a failed request)
        self.wifi: dict[str, Any] = {}
        self._wifi_at = 0.0
        self._http_hist: deque = deque(maxlen=240)

    def hold_monitor(self, seconds: float = MONITOR_YIELD_S) -> None:
        """Keep the board monitor off this link for `seconds` (renewed by every
        schedule op, so a fire's whole set-up is covered; an armed run is then
        covered by the arm hint)."""
        self.monitor_hold_until = max(self.monitor_hold_until, time.monotonic() + seconds)

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
        if frame_type == SHV_ARM:
            # The board monitor stops requesting the moment a run is armed,
            # before its next status read confirms it (see _board_monitor_tick).
            self.arm_hint_at = time.monotonic()
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
        # Logged on the TRANSITIONS, not per attempt. This used to write one
        # warning per 1 s retry: a controller left switched off produced
        # 17-22k identical lines a day (99.7% of the file), burying everything
        # else -- and the recovery was only printed, never logged, so the log
        # showed an outage without an end. Now: one line when it goes down,
        # one when it comes back (with how long and how many attempts), and a
        # reminder every BRIDGE_DOWN_REMIND_S while it stays down.
        reconnecting = False
        mark_low_priority()     # the PING gives way to every command (see _PriorityLock)
        down_since = 0.0
        attempts = 0
        reminded = 0.0
        connected_at = time.monotonic()
        silent_noted = 0.0
        while self._running:
            if not self.client.connected:
                host = self.host
                if not host:
                    time.sleep(1.0)
                    continue
                try:
                    self.client.connect(host, BRIDGE_PORT)   # connect() cleans up half-open state
                    if reconnecting:
                        down_s = time.monotonic() - down_since
                        print(f"[{self.name}] bridge reconnected to {host}", flush=True)
                        log.warning("%s: bridge RECONNECTED to %s after %.0f s down "
                                    "(%d attempts)", self.name, host, down_s, attempts)
                    reconnecting = False
                    self.rp_last = 0.0
                    self.rp_rtt_ms = None
                    connected_at = time.monotonic()
                except Exception as exc:
                    now = time.monotonic()
                    attempts += 1
                    if not reconnecting:
                        down_since = reminded = now
                        attempts = 1
                        print(f"[{self.name}] bridge down, reconnecting to {host}…", flush=True)
                        log.warning("%s: bridge DOWN, reconnecting to %s (%s)",
                                    self.name, host, exc)
                    elif now - reminded >= BRIDGE_DOWN_REMIND_S:
                        reminded = now
                        log.warning("%s: bridge still down — %s unreachable for %.0f s "
                                    "(%d attempts; last: %s)", self.name, host,
                                    now - down_since, attempts, exc)
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
                # A link that is "connected" but carries nothing: the TCP socket
                # to the bridge is open, yet no frame of any kind (PING answer,
                # command answer, pushed telemetry) has come back for
                # LINK_SILENT_S. Seen 2026-10-05 17:20 on Power 1 after two power
                # cycles: nothing reconnected it, because reconnecting only
                # happens when the socket closes and auto-connect only fills
                # EMPTY slots. Close it here; the branch above reconnects.
                # Not while the PING is paused (a GUI test holds it off).
                last_rx = max(self.rp_last, getattr(self.client, "_last_controller_rx_ts", 0.0) or 0.0)
                silent = time.time() - last_rx if last_rx else time.monotonic() - connected_at
                if silent >= LINK_SILENT_S and time.monotonic() - connected_at >= LINK_SILENT_S:
                    now = time.monotonic()
                    if now - silent_noted >= BRIDGE_DOWN_REMIND_S:
                        silent_noted = now
                        log.warning("%s: bridge %s connected but SILENT for %.0f s (no "
                                    "answer from the RP2350) -- closing it to reconnect",
                                    self.name, self.host, silent)
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
                    continue
            host = self.host
            if host:
                t_http = time.monotonic()
                try:
                    stm = fetch_stm32_status(host)
                    self._note_stm_reset(stm)
                    self.stm = stm
                except Exception as exc:
                    stm = self.stm = {"ever_seen": False, "error": str(exc)}
                err = str(stm.get("error") or "")
                failed = "timed out" in err or "refused" in err or "unreachable" in err.lower()
                self._http_hist.append((time.monotonic(),
                                        None if failed else (time.monotonic() - t_http) * 1000.0))
                if time.monotonic() - self._wifi_at >= WIFI_DIAG_PERIOD_S:
                    self._wifi_at = time.monotonic()
                    w = fetch_wifi_diag(host)
                    w["at"] = time.time()
                    self.wifi = w
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

    def link_quality(self, window_s: float = 60.0) -> dict[str, Any]:
        """WiFi signal and recent latency, the ESP32 alone and the RP2350
        behind it. None fields when there is nothing to report, never 0."""
        cut = time.monotonic() - window_s
        h = [ms for t, ms in list(self._http_hist) if t >= cut]
        ok = sorted(ms for ms in h if ms is not None)
        w = self.wifi or {}
        t = self.client.timing_summary(window_s)
        return {
            "window_s": window_s,
            "rssi_dbm": w.get("rssi"), "ap_rssi_dbm": w.get("ap_rssi"),
            "channel": w.get("channel"), "bandwidth": w.get("sta_bandwidth") or w.get("bandwidth"),
            "power_save": w.get("ps"),
            "wifi_error": w.get("error"),
            "wifi_age_s": round(time.time() - w["at"], 1) if w.get("at") else None,
            "esp32_http_avg_ms": round(sum(ok) / len(ok), 1) if ok else None,
            "esp32_http_p95_ms": round(ok[min(len(ok) - 1, int(0.95 * (len(ok) - 1) + 0.5))], 1) if ok else None,
            "esp32_http_failed": sum(1 for ms in h if ms is None),
            "esp32_http_requests": len(h),
            "rp2350_avg_ms": (t.get("answer_ms") or {}).get("avg"),
            "rp2350_p95_ms": (t.get("answer_ms") or {}).get("p95"),
            "rp2350_timeouts": t.get("timeouts"),
            "rp2350_requests": t.get("requests"),
        }

    def status(self) -> dict[str, Any]:
        now = time.time()
        rp_age = None if self.rp_last == 0 else (now - self.rp_last) * 1000.0
        stm = self.stm or {}
        return {
            "name": self.name,
            "connected": self.client.connected,
            "host": self.host,
            "bridge_name": self.bridge_name,
            "rp2350": {"age_ms": rp_age, "rtt_ms": self.rp_rtt_ms,
                       # rtt_ms above includes queueing behind other requests;
                       # this splits queue wait from the controller's answer time.
                       "timing": self.client.timing_summary()},
            "link": self.link_quality(),
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


# Every name above, for `from ... import *` (underscore names included).
# A LITERAL list, not computed: editors (Pylance/pyright) read __all__
# statically, and a computed one left every star-imported helper
# "not defined" -- goto definition stopped working. tests/test_star_exports.py
# fails if this falls out of step with the module's globals.
__all__ = [
    "DOWNLOAD_FRAME_TIMEOUTS_S",
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
    "IDLE_CEILING_MA", "LAST_POWER_STATE", "LOCK_TTL_DEFAULT_S", "LOCK_TTL_MAX_S",
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
    "_DEAD_LOCK", "_DIAG_CHIPS", "_DailySizeRotatingHandler", "_EM_I_FULL_MA",
    "_HV_DS_CH", "_HV_FULL_V", "_OCP_MA_PER_CODE", "_OCP_SENSE_RESISTOR_OHMS",
    "_ORDER_LOCK", "_SINGLE_0X3A_TRUSTED", "_TPS_IOUT_LIMIT_REG", "_coerce_bytes",
    "_coerce_int", "_is_read_command", "_le", "_pipeline_reliable", "_popcount",
    "RETRY_TIMEOUTS_S", "request_retry", "shv_status_retry",
    "_setup_logging", "_status_err", "_status_ok", "_suppress", "_u16", "_u32",
    "_unpack_spi_shot", "adc_get_burst", "adc_pulse_arm", "adc_pulse_diag",
    "adc_pulse_disarm", "adc_ready_arm", "adc_ready_disarm", "adc_ready_renew",
    "adc_ready_status", "adc_ring_peek", "adc_ring_start", "adc_ring_stop",
    "adc_ring_window", "adc_ring_window_data", "adc_spi_shot_arm", "adc_spi_shot_data",
    "annotations", "build_command_payload", "build_payload", "copy", "csv", "datetime", "deque",
    "decode_shv_status", "enum", "fetch_bridge_info", "fetch_stm32_status", "fetch_wifi_diag", "mark_low_priority", "json",
    "log", "logging", "os", "parse_power_state", "power_state_name", "primary_local_ip",
    "pulse_events_get", "scan_for_bridge", "stm32_adc_window", "stm32_ads1115",
    "stm32_ds3502_get", "stm32_ds3502_set", "stm32_hv_clear_target",
    "stm32_hv_enable_set", "stm32_hv_get_target", "stm32_hv_set_target",
    "stm32_hv_status", "sync_get_burst_status", "sync_get_status", "sync_post_abort",
    "sync_post_burst", "sync_post_burst_stop", "sync_post_config", "sync_post_fire",
    "threading", "time",
]
