"""backend: who may write: the lease, the client roster, the audit log.

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
    "/api/safety",            # dead-man watchdog: read it, renew it, configure
                               # it. Ungated on purpose -- a keepalive from a
                               # client that does NOT hold the lease is still
                               # evidence that somebody is present, which is the
                               # only question this watchdog asks.
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


#: POSTs that only read, or are heartbeats. Not audited.
AUDIT_SKIP_PATHS = frozenset({
    "/api/adc/ready-status", "/api/adc/ready-renew", "/api/diagnosis",
    "/api/present", "/api/tca9554-read", "/api/verify-schedule",
    "/api/poll-pause",
})


#: /api/shv ops that only read.
AUDIT_SKIP_SHV_OPS = frozenset({
    "get_active_list", "table_info", "heat_info", "get_config", "status",
    "pulse_log", "capability",
})


AUDIT_DEDUP_S = 5.0          # identical request from the same client within this: counted, not re-logged


AUDIT_BODY_MAX = 300         # characters of request summary per line


AUDIT_ERROR_MAX = 200        # characters of error per line


_AUDIT_LAST: dict = {}       # (client, path, body-json) -> [monotonic, suppressed count]


_AUDIT_LOCK = threading.Lock()


def is_read_post(path: str, body: dict) -> bool:
    """A POST that only READS the hardware. Shared by the lease (reads are
    never gated: a lease reserves the right to CHANGE the bench, not to look at
    it) and the audit log (reads are not audited). One definition, because two
    drifted: the lease used to refuse shv_status / pulse_log / trigger-delay
    reads from anyone but the holder, so the GUI's SHV panel went blind for as
    long as a script held the lease."""
    if path in AUDIT_SKIP_PATHS and path != "/api/poll-pause":
        return True
    if path == "/api/shv":
        op = body.get("op")
        if op in AUDIT_SKIP_SHV_OPS:
            return True
        # fault_policy / trigger_delay without a value to set are reads
        if op == "fault_policy" and body.get("board") is None and body.get("mismatch") is None:
            return True
        if op == "trigger_delay" and body.get("delay_us", body.get("delayUs")) is None:
            return True
    if path in ("/api/cmd", "/api/power-cmd"):
        cmd = str(body.get("command", ""))
        if _is_read_command(cmd):
            return True
        # One opcode for both directions, told apart by the payload exactly as
        # build_command_payload does: no value = GET.
        if cmd == "CH_SLEW_RATE" and body.get("below_mV_per_s") is None:
            return True
        if cmd == "CH_STARTUP_OCP" and not body.get("set"):
            return True
    return False


#: POSTs that only take the bench DOWN. Never refused by the lease.
_DEENERGISE_PATHS = frozenset({
    "/api/disarm", "/api/sync/abort", "/api/sync/simulate-stop",
    "/api/stm32/hv-clear-target", "/api/adc/ready-disarm", "/api/adc/pulse-disarm",
    "/api/adc/ring-stop", "/api/ringpulse/disarm", "/api/record/stop",
})


def is_deenergising_post(path: str, body: dict) -> bool:
    """A POST that only turns something OFF: a STOP/SLEEP, HV off, a grid
    clear, a disarm. The lease never refuses these -- a lease exists to keep
    other clients from CHANGING what a run depends on, and a run can survive
    being stopped far better than a filament can survive not being stopped.
    Before this, a script holding the lease made the GUI's stop buttons return
    "another client holds the write lease"."""
    if path in _DEENERGISE_PATHS:
        return True

    def low_state(v) -> bool:
        try:
            return int(v) in (POWER_STATE_STOP, POWER_STATE_SLEEP)
        except (TypeError, ValueError):
            return False

    if path in ("/api/filament-prep", "/api/filament-state"):
        return low_state(body.get("state"))
    if path in ("/api/cmd", "/api/power-cmd"):
        return (str(body.get("command", "")) == "CH_SET_POWER_STATE"
                and low_state(body.get("state")))
    if path in ("/api/hv-grid", "/api/stm32/hv-enable"):
        return "on" in body and not bool(body.get("on"))
    if path == "/api/shv":
        return body.get("op") == "disarm"
    return False


def _audit_skipped(path: str, body: dict) -> bool:
    if is_read_post(path, body) or path == "/api/poll-pause":
        return True
    if path == "/api/lock" and str(body.get("action", "acquire")).lower() in ("renew", "status"):
        return True
    if path == "/api/safety" and set(body) <= {"keepalive", "filaments", "client"}:
        return True
    return False


def _audit_summary(v, depth: int = 0) -> str:
    """Compact, bounded rendering of a request body: scalars in full, long
    lists by length -- a schedule download carries hundreds of entries."""
    if isinstance(v, dict):
        if depth >= 2:
            return f"{{{len(v)} keys}}"
        return "{" + ", ".join(f"{k}: {_audit_summary(x, depth + 1)}" for k, x in v.items()
                               if k != "client") + "}"
    if isinstance(v, (list, tuple)):
        if len(v) > 8 or any(isinstance(x, (dict, list)) for x in v):
            return f"[{len(v)} items]"
        return "[" + ", ".join(_audit_summary(x, depth + 1) for x in v) + "]"
    return json.dumps(v, ensure_ascii=False) if isinstance(v, str) else str(v)


def _audit_outcome(resp) -> str:
    if not isinstance(resp, dict):
        return "-> no reply" if resp is None else "-> replied"
    if resp.get("ok") is False or resp.get("error"):
        err = str(resp.get("error") or "failed")
        if len(err) > AUDIT_ERROR_MAX:
            err = err[:AUDIT_ERROR_MAX] + "…"
        return f"-> FAILED: {err}"
    return "-> ok"


def audit_post(path: str, client, body, resp, note=None) -> None:
    """One audit line for a POST, unless it is a read or a heartbeat."""
    body = body if isinstance(body, dict) else {}
    if _audit_skipped(path, body):
        return
    summary = _audit_summary(body)
    if len(summary) > AUDIT_BODY_MAX:
        summary = summary[:AUDIT_BODY_MAX] + "…"
    outcome = _audit_outcome(resp)
    key = (client, path, summary, outcome)
    now = time.monotonic()
    with _AUDIT_LOCK:
        last = _AUDIT_LAST.get(key)
        if last is not None and now - last[0] < AUDIT_DEDUP_S:
            last[1] += 1
            last[0] = now
            return
        repeats = last[1] if last else 0
        _AUDIT_LAST[key] = [now, 0]
        if len(_AUDIT_LAST) > 512:          # bounded: forget the oldest
            for k in sorted(_AUDIT_LAST, key=lambda k: _AUDIT_LAST[k][0])[:256]:
                _AUDIT_LAST.pop(k, None)
    more = f"   (+{repeats} identical before this, not logged)" if repeats else ""
    if note:
        more += f"   [{note}]"
    level = logging.WARNING if outcome.startswith("-> FAILED") else logging.INFO
    log.log(level, "AUDIT %s POST %s %s %s%s", client or "?", path, summary, outcome, more)


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


# Every name above, for `from ... import *` (underscore names included).
__all__ = [_n for _n in list(globals()) if not _n.startswith("__")]
