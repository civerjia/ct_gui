"""ct_simple_control.py — thin HTTP client for the CT power-controller backend.

The backend process (backend.py) owns all hardware binding and business logic.
This module is a convenience wrapper around its HTTP API for third-party scripts.

Usage:
    from ct_simple_control import CTClient, CTError

    ct = CTClient("192.168.1.100")
    with ct.lease(ttl=120, note="auto test"):
        ct.idle_all(currents={5: 2500})
        ct.active_one(5, current_ma=2900)
        ct.set_emission_v(30)        # backend loads LUT and writes DS3502
        ct.enable_emission(True)
        print(ct.read_emission_v())
        result = ct.fire_single_pulse(filament=5, num_pulses=1, width_us=1000)
        ct.enable_emission(False)
        ct.stop_all()

Dependencies: pip install requests
"""

import time
from contextlib import contextmanager

import requests

# ── exceptions ────────────────────────────────────────────────────────────────

class CTError(Exception):
    """Base error for all ct_simple_control failures."""

class CTConnectionError(CTError):
    """Backend unreachable."""

class CTLeaseError(CTError):
    """Write lease held by another client."""

class CTTimeoutError(CTError):
    """Operation did not complete within the allowed time."""

# ── SHV schedule state constants (ShvGetStatus.state) ────────────────────────

SHV_IDLE     = 0
SHV_ARMED    = 1
SHV_RUNNING  = 2
SHV_COMPLETE = 3
SHV_FAULT    = 4

# ── power state constants ─────────────────────────────────────────────────────

STOP    = 1
SLEEP   = 2
STANDBY = 3
IDLE    = 4
ACTIVE  = 5


# ── client ────────────────────────────────────────────────────────────────────

class CTClient:
    """Thin HTTP client for the CT backend. All hardware logic lives server-side."""

    def __init__(
        self,
        host: str = "localhost",
        port: int = 5000,
        client_id: str = "ct_simple_control",
        timeout: float = 5.0,
    ):
        self.base = f"http://{host}:{port}"
        self.client_id = client_id
        self.timeout = timeout
        self._s = requests.Session()
        self._s.headers.update({
            "X-CT-Client": client_id,
            "Content-Type": "application/json",
        })

    # ── HTTP helpers ──────────────────────────────────────────────────────────

    def _post(self, path: str, body: dict, timeout: float | None = None) -> dict:
        try:
            r = self._s.post(self.base + path, json=body,
                             timeout=timeout or self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.ConnectionError as exc:
            raise CTConnectionError(f"Cannot reach backend {self.base}: {exc}") from exc
        except CTError:
            raise
        except Exception as exc:
            raise CTError(f"POST {path}: {exc}") from exc

    def _get(self, path: str, timeout: float | None = None) -> dict:
        try:
            r = self._s.get(self.base + path, timeout=timeout or self.timeout)
            r.raise_for_status()
            return r.json()
        except requests.ConnectionError as exc:
            raise CTConnectionError(f"Cannot reach backend {self.base}: {exc}") from exc
        except CTError:
            raise
        except Exception as exc:
            raise CTError(f"GET {path}: {exc}") from exc

    def _ok(self, resp: dict, label: str = "") -> dict:
        if not resp.get("ok"):
            err = resp.get("error") or str(resp)
            raise CTError(f"{label}: {err}" if label else err)
        return resp

    # ── lease ─────────────────────────────────────────────────────────────────

    def acquire_lease(self, ttl: float = 60.0, note: str = "") -> None:
        """Acquire an exclusive write lock (blocks other GUI writes while held)."""
        r = self._post("/api/lock", {"action": "acquire", "ttl": ttl, "note": note})
        if not r.get("ok"):
            snap = r.get("lease") or {}
            raise CTLeaseError(
                f"Lease held by '{snap.get('owner')}' "
                f"({snap.get('expires_in_s', '?')} s remaining)"
            )

    def release_lease(self) -> None:
        """Release the lease. Safe to call when not held."""
        self._post("/api/lock", {"action": "release"})

    def renew_lease(self, ttl: float = 60.0) -> None:
        """Extend the current lease before it expires."""
        self._post("/api/lock", {"action": "acquire", "ttl": ttl})

    @contextmanager
    def lease(self, ttl: float = 60.0, note: str = ""):
        """Context manager: acquire lease on enter, release on exit."""
        self.acquire_lease(ttl=ttl, note=note)
        try:
            yield self
        finally:
            self.release_lease()

    # ── power state ───────────────────────────────────────────────────────────

    def _prep(self, state: int, filaments=None,
              currents: dict | None = None, arg: int = 0) -> dict:
        body: dict = {"state": state, "arg": arg}
        if filaments is not None:
            body["filaments"] = [int(f) for f in filaments]
        if currents:
            body["currents"] = {str(k): int(v) for k, v in currents.items()}
        r = self._post("/api/filament-prep", body, timeout=20.0)
        # "no controller connected" is a hard error; partial board failures are
        # expected on benches without full hardware and are returned as-is.
        if not r.get("ok") and r.get("error"):
            raise CTError(f"power_state={state}: {r['error']}")
        return r

    def stop_all(self, filaments=None) -> dict:
        """STOP all (or listed) filaments — HV off, heating off."""
        return self._prep(STOP, filaments)

    def sleep_all(self, filaments=None) -> dict:
        """SLEEP all (or listed) filaments."""
        return self._prep(SLEEP, filaments)

    def standby_all(self, filaments=None) -> dict:
        """STANDBY all (or listed) filaments."""
        return self._prep(STANDBY, filaments)

    def idle_all(self, filaments=None, currents: dict | None = None) -> dict:
        """IDLE all (or listed) filaments (warm pool). currents: {filament: mA}."""
        return self._prep(IDLE, filaments, currents)

    def active_one(self, filament: int, current_ma: float) -> dict:
        """Promote one filament to ACTIVE at the given current (mA)."""
        return self._prep(ACTIVE, [filament], {filament: int(current_ma)})

    def active_all(self, filaments=None, currents: dict | None = None) -> dict:
        """ACTIVE all (or listed) filaments."""
        return self._prep(ACTIVE, filaments, currents)

    # ── HV voltage / current set ──────────────────────────────────────────────

    def set_emission_v(self, volts: float) -> dict:
        """Set emission HV to |volts| V (output is negative).

        Backend loads the calibrated LUT, interpolates the DS3502 wiper, and
        writes it. Falls back to a linear approximation when no LUT is saved.
        Returns {"ok", "wiper", "expect_v", "method"}.
        """
        return self._ok(
            self._post("/api/hv/set-v", {"chan": "emission", "volts": abs(volts)}),
            "set_emission_v",
        )

    def set_focus_v(self, volts: float) -> dict:
        """Set focus HV to |volts| V (output is negative).

        Returns {"ok", "wiper", "expect_v", "method"}.
        """
        return self._ok(
            self._post("/api/hv/set-v", {"chan": "focus", "volts": abs(volts)}),
            "set_focus_v",
        )

    def set_emission_i(self, ma: float) -> dict:
        """Set emission current reference (0–85.7 mA). Linear DS3502 scale.

        Returns {"ok", "wiper", "expect_ma"}.
        """
        return self._ok(
            self._post("/api/hv/set-i", {"ma": abs(ma)}),
            "set_emission_i",
        )

    # ── HV readback (ADS1115) ─────────────────────────────────────────────────

    def read_ads_all(self) -> dict:
        """Read all four ADS1115 channels.

        Keys: emiss_v (V), emiss_i_ma (mA), focus_v (V), ref_mv (mV),
              codes ([int×4] raw counts), mv ([float×4] pin voltages).
        """
        return self._ok(self._get("/api/stm32/ads1115"), "read_ads_all")

    def read_emission_v(self) -> float:
        """Measured emission voltage (V, negative)."""
        return float(self.read_ads_all()["emiss_v"])

    def read_emission_i(self) -> float:
        """Measured emission current (mA)."""
        return float(self.read_ads_all()["emiss_i_ma"])

    def read_focus_v(self) -> float:
        """Measured focus voltage (V, negative)."""
        return float(self.read_ads_all()["focus_v"])

    # ── HV enable / disable ───────────────────────────────────────────────────

    def enable_emission(self, on: bool) -> dict:
        """Enable or disable the emission HV output."""
        return self._ok(
            self._post("/api/stm32/hv-enable", {"ch": "emission", "on": on}),
        )

    def enable_focus(self, on: bool) -> dict:
        """Enable or disable the focus HV output."""
        return self._ok(
            self._post("/api/stm32/hv-enable", {"ch": "focus", "on": on}),
        )

    def hv_status(self) -> dict:
        """HV pin states: {emission_on, focus_on, ads1115_alert, amc3301_diag}."""
        return self._ok(self._get("/api/stm32/hv-status"))

    # ── SHV schedule — low-level ──────────────────────────────────────────────

    def _shv(self, controller: int, body: dict, timeout: float | None = None) -> dict:
        return self._post("/api/shv", {"controller": controller, **body}, timeout)

    def shv_clear(self, controller: int = 1) -> None:
        self._ok(self._shv(controller, {"op": "clear_table"}), "shv_clear")

    def shv_push_active_list(self, controller: int = 1) -> None:
        self._ok(self._shv(controller, {"op": "push_active_list"}), "shv_push_active_list")

    def shv_set_entry(self, controller: int, filament: int,
                      num_pulses: int = 1, width_us: int = 1000) -> None:
        self._ok(self._shv(controller, {
            "op": "set_entries",
            "entries": [{"filament": int(filament),
                         "numPulses": int(num_pulses),
                         "width": int(width_us)}],
        }), "shv_set_entry")

    def shv_set_config(self, controller: int, inter_pulse_ms: int = 3000,
                       max_on_ms: int = 40, total_ms: int = 30000) -> None:
        self._ok(self._shv(controller, {
            "op": "set_config",
            "interPulseMs": int(inter_pulse_ms),
            "maxOnMs": int(max_on_ms),
            "totalMs": int(total_ms),
        }), "shv_set_config")

    def shv_arm(self, controller: int = 1, repeats: int = 1) -> dict:
        r = self._shv(controller, {"op": "arm", "repeats": int(repeats)})
        if not r.get("ok"):
            raise CTError(f"shv_arm rejected (code {r.get('reject')}) — "
                          "check active list and filament power states")
        return r

    def shv_disarm(self, controller: int = 1) -> None:
        self._shv(controller, {"op": "disarm"})

    def shv_status(self, controller: int = 1) -> dict:
        """SHV status: {state, filamentIndex, totalPulsesDone, elapsedMs, …}."""
        return self._shv(controller, {"op": "status"}).get("status") or {}

    def shv_pulse_log(self, controller: int = 1, start: int = 0) -> list[dict]:
        """Fired pulse records: [{filament, seq, tOnUs, durationUs, flags}, …]."""
        return self._shv(controller, {"op": "pulse_log",
                                      "start": int(start)}).get("records") or []

    # ── SHV schedule — single-filament pulse (high-level) ────────────────────

    def fire_single_pulse(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,
        controller: int = 1,
        trigger: str = "sim",
        timeout_s: float = 15.0,
    ) -> dict:
        """Download a one-entry schedule, arm it, fire, and verify.

        Args:
            filament:       0–95 global filament index.
            num_pulses:     pulses in the burst.
            width_us:       pulse width (µs).
            inter_pulse_ms: minimum gap between SyncIn edges (ms).
            max_on_ms:      firmware safety guard — abort if HV on > this (ms).
            total_ms:       overall schedule timeout (ms).
            controller:     1 or 2.
            trigger:        "sim" — ESP32 generates SyncIn pulse(s);
                            "ext" — caller supplies the external SyncIn edge.
            timeout_s:      polling timeout before raising CTTimeoutError.

        Returns:
            {"ok": bool, "fired": int, "records": [...], "status": {...}}
        """
        self.shv_disarm(controller)
        self.shv_clear(controller)
        self.shv_push_active_list(controller)
        self.shv_set_entry(controller, filament, num_pulses, width_us)
        self.shv_set_config(controller, inter_pulse_ms, max_on_ms, total_ms)
        self.shv_arm(controller, repeats=1)

        if trigger == "sim":
            self._post("/api/sync/simulate", {
                "count": int(num_pulses),
                "interval_ms": float(max(inter_pulse_ms, 10)),
                "controller": int(controller),
            }, timeout=10.0)

        deadline = time.monotonic() + timeout_s
        state = SHV_IDLE
        while time.monotonic() < deadline:
            st = self.shv_status(controller)
            state = st.get("state", SHV_IDLE)
            if state == SHV_FAULT:
                raise CTError(
                    f"SHV fault on controller {controller}: "
                    f"filament {st.get('faultFilament')}, reason {st.get('stopReason')}"
                )
            if state == SHV_COMPLETE:
                logs = self.shv_pulse_log(controller)
                fired = [r for r in logs if r.get("filament") == filament]
                return {"ok": bool(fired), "fired": len(fired),
                        "records": fired, "status": st}
            time.sleep(0.05)

        self.shv_disarm(controller)
        raise CTTimeoutError(
            f"fire_single_pulse timed out after {timeout_s} s (state={state})"
        )
