"""CTClient: HV grid switches, HV set/readback/enable, SHV run policy.

One part of the client class, split out of one 9900-line file by section:
    HV grid switch (ISO relay) — Force toggle
    SHV run policy & HV bit-bang diagnostics
    HV voltage / current set
    HV readback (ADS1115)
    HV enable / disable

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # editors only: at run time _client.py binds CTClient into this module
    from ._client import CTClient


class _HvMixin:
    # ── HV grid switch (ISO relay) — Force toggle ────────────────────────────

    def hv_grid_set(self, filament: int, on: bool, force: bool = True) -> dict:
        """Toggle ONE filament's HV isolation switch. Returns
        {"ok": False, "dead": True, ...} if the filament is dead — does
        not raise.

        force=True (default) uses writeMode=2 — bypasses the firmware's
        fault/verify checks, matching the GUI's "Force" checkbox. Use when
        the switch feedback is unreliable or the filament is known-shorted.
        force=False uses verify mode (writeMode=1) instead.

        Returns {"ok", "applied": [filament] or [], "failed": [...]}.
        """
        # on=False is de-energising -- allowed for a dead filament, same reason
        # as STOP above. Refusing it would leave a faulty filament's grid switch
        # closed with no way to open it.
        if on and self._is_dead(filament):
            return self._dead_result(filament)
        r = self._post("/api/hv-grid", {"filaments": [self._fid_of(filament)],
                                        "on": bool(on), "force": bool(force)})
        return self._reindex_response(r, keys=("applied", "failed", "excluded", "touched",
                                            "not_this_controller", "unslotted",
                                            "mismatched", "unstable"))

    def hv_grid_set_all(self,
                        filaments=None,     # None = every populated board
                                             # (minus dead mask)
                        on: bool = False,   # switch state to command
                        force: bool = True) -> dict:  # bypass firmware fault/
                                                       # verify checks (True) vs
                                                       # require verify (False)
        """Toggle the HV isolation switch for all (or listed) filaments.

        Dead-masked filaments are always stripped before the request is sent
        — see set_dead()/add_dead(). filaments=None targets every populated
        board (minus dead). force=True bypasses firmware fault/verify checks.

        Returns {"ok", "results": {controller: {...}}, "applied": [...], "failed": [...]}.
        """
        # dead_skipped is computed on USER_INDEX BEFORE _live() crosses to FID,
        # so it reads back in YOUR numbering. Without it, hv_grid_off_all([5]) with
        # 5 dead-masked returned a bare ok:True while the grid switch stayed ON --
        # a success-shaped no-op on an HV path.
        requested = None if filaments is None else [int(f) for f in filaments]
        dead = self.dead   # bound once — property, see _live()
        dead_skipped = [] if requested is None else [f for f in requested if f in dead]
        live = self._live(filaments)   # already crossed to FID
        if live is not None and len(live) == 0:
            # ok:False -- the caller named filaments and NONE were commanded.
            return {"ok": False, "applied": [], "failed": [], "skipped_dead": True,
                    "dead_skipped": dead_skipped, "requested": requested,
                    "error": "every requested filament is dead-masked — nothing sent"}
        body: dict = {"on": bool(on), "force": bool(force)}
        if live is not None:
            body["filaments"] = live
        r = self._reindex_response(self._post("/api/hv-grid", body, timeout=20.0),
                                 keys=("applied", "failed", "excluded", "touched",
                                            "not_this_controller", "unslotted",
                                            "mismatched", "unstable"))
        if dead_skipped:
            r = {**r, "dead_skipped": dead_skipped}
        return r

    def hv_grid_off_all(self, filaments=None, force: bool = True) -> dict:
        """Convenience: turn OFF the HV isolation switch for all (or listed)
        filaments, excluding the dead mask. Same as hv_grid_set_all(on=False)."""
        return self.hv_grid_set_all(filaments, on=False, force=force)

    def hv_grid_status(self, controller: int = 1) -> dict:
        """Read per-filament HV-grid switch state on one controller.

        Returns {"ok", "filaments": {str(filament): {"desired": bool, "feedback": bool}}}.
        `desired` is the last commanded state; `feedback` is the switch's own
        sense line (a mismatch flags a stuck/dead switch).
        """
        r = self._get(f"/api/hv-grid-status?controller={controller}")
        # Backend replies keyed by FID (as a string) -- re-key to
        # USER_INDEX so this always matches YOUR numbering.
        if isinstance(r.get("filaments"), dict):
            r["filaments"] = {str(self._user_index_of(int(k))): v
                              for k, v in r["filaments"].items()}
        return r

    def hv_grid_clear_all(self) -> dict:
        """Instantly zero EVERY HV grid output on every connected controller.

        Uses the hardware 74HC595 /SRCLR clear pin (async shift-register
        clear) — bypasses the normal per-bit shift-and-latch write path
        entirely, so it is the fastest possible way to kill all HV grid
        outputs at once. This is a whole-chain hardware operation, not a
        per-filament one: the dead mask does NOT apply here — every
        channel is zeroed regardless of which filaments are marked dead.

        Use this as an emergency "kill everything now" — e.g. before
        walking away from the bench, or if a switch is behaving
        unexpectedly and you want a known-clean starting point. Also used
        internally by session()'s teardown.

        Returns {"ok", "results": {controller: {"ok": bool}}}.
        """
        return self._post("/api/disarm", {})

    # ── SHV run policy & HV bit-bang diagnostics ──────────────────────────────


    def _get_slew_rates_one(self, controller: int = 1) -> dict:
        """Read the three voltage-ramp slew rates, in mV/s.

        Returns {"ok", "below_mV_per_s", "above_mV_per_s", "warm_mV_per_s"}:
        `below` applies under 2 V, `above` above 2 V from cold, `warm` above 2 V
        on a warm restart (the IDLE<->ACTIVE transition a scan actually uses).

        The configured number IS the real instantaneous dV/dt -- no conversion,
        and nothing to scale for display. (An earlier firmware halved it: the
        ramp's step clock was reset on every target increase, and the CC loop
        re-arms a higher target every 20 ms, so 10 ms of accumulated step credit
        was discarded each time -- the ramp stepped on a 20 ms cadence while
        sizing each step for 10 ms. It scaled linearly, so it looked like a
        clean 0.44 constant. It was a bug and it is fixed.)

        A MEASURED full IDLE->ACTIVE transition averages BELOW the setting, and
        that is correct rather than a discrepancy: peak dV/dt never exceeds the
        configured rate, median runs ~91% of it, and the CC loop's fine trim at
        the operating point is deliberately slow.
        """
        r = self._post("/api/cmd", {"controller": int(controller),
                                    "command": "CH_SLEW_RATE"}, timeout=10.0)
        raw = ((r.get("response") or {}).get("raw")) if r.get("ok") else None
        if not raw or len(raw) < 7 or raw[0] != 0:
            return {"ok": False, "error": r.get("error") or "bad CH_SLEW_RATE response"}
        le = lambda o: raw[o] | (raw[o + 1] << 8)
        return {"ok": True, "below_mV_per_s": le(1), "above_mV_per_s": le(3),
                "warm_mV_per_s": le(5)}

    # SOFTWARE floor, well above the firmware's own 1 mV/s. The firmware will
    # happily install 1, which is not "unlimited" but "ramp 10 V in about three
    # hours" -- and it came back ok=True, so the bench sat at 1/1/1 until a
    # readback caught it. 400 is the stock `below` rate, so clamping up to it
    # leaves a working bench rather than a stalled one.
    _SLEW_MIN_MV_PER_S = 400

    def _set_slew_rates_one(self, below_mV_per_s: int, above_mV_per_s: int,
                       warm_mV_per_s: int, controller: int = 1) -> dict:
        """Set all three slew rates (mV/s). See get_slew_rates for what each is.

        Anything below _SLEW_MIN_MV_PER_S (400 mV/s) is CLAMPED UP to it, and
        the clamp is reported. The firmware's own floor is 1 mV/s and it accepts
        it silently -- that is not "no limit", it is "ramp 10 V in about three
        hours", and it came back ok=True, so the bench sat at 1/1/1 until a
        readback caught it. Clamping up to the stock rate leaves a working bench
        instead of a stalled one.

        Above the ceilings it also CLAMPS rather than failing (below 2000,
        above/warm 5000), so the return is the values actually IN FORCE, not
        what you asked for -- check them, and check `clamped`.

        THE CEILINGS ARE NOT THE DEFAULTS; use SLEW_DEFAULTS for that.
        """
        # Clamp up to the software floor BEFORE sending. `asked` below keeps the
        # caller's ORIGINAL numbers, so the existing `clamped` flag still fires
        # when what came back differs from what was requested -- a clamp the
        # caller never learns about is the failure this whole guard exists for.
        requested = (int(below_mV_per_s), int(above_mV_per_s), int(warm_mV_per_s))
        floored = {n: v for n, v in zip(("below_mV_per_s", "above_mV_per_s",
                                         "warm_mV_per_s"), requested)
                   if v < self._SLEW_MIN_MV_PER_S}
        below_mV_per_s = max(int(below_mV_per_s), self._SLEW_MIN_MV_PER_S)
        above_mV_per_s = max(int(above_mV_per_s), self._SLEW_MIN_MV_PER_S)
        warm_mV_per_s  = max(int(warm_mV_per_s),  self._SLEW_MIN_MV_PER_S)
        # Clamp to the u16 wire range here. The firmware clamps to its own
        # ceilings, but a value over 65535 would fail to serialise and the call
        # would error instead of clamping, contradicting the contract the
        # read-back is built on.
        w = lambda v: max(0, min(65535, int(v)))
        asked = (w(below_mV_per_s), w(above_mV_per_s), w(warm_mV_per_s))
        r = self._post("/api/cmd", {"controller": int(controller),
                                    "command": "CH_SLEW_RATE",
                                    "below_mV_per_s": asked[0],
                                    "above_mV_per_s": asked[1],
                                    "warm_mV_per_s": asked[2]}, timeout=10.0)
        raw = ((r.get("response") or {}).get("raw")) if r.get("ok") else None
        if not raw or len(raw) < 7 or raw[0] != 0:
            return {"ok": False, "error": r.get("error") or "bad CH_SLEW_RATE response"}
        le = lambda o: raw[o] | (raw[o + 1] << 8)
        got = (le(1), le(3), le(5))
        return {"ok": True, "below_mV_per_s": got[0], "above_mV_per_s": got[1],
                "warm_mV_per_s": got[2],
                # Compared against what was actually SENT (post-u16 clamp), so a
                # request of 99999 reports clamped rather than being measured
                # against a number that never reached the wire.
                "clamped": got != asked,
                # Which bands this client raised to the floor, and what they
                # were. Separate from `clamped` (which also covers the
                # firmware's own ceiling clamp) so the two causes stay apart.
                "floored_to_min": floored or None,
                "floor_mV_per_s": self._SLEW_MIN_MV_PER_S}

    def _get_fault_policy_one(self, controller: int = 1) -> dict:
        """Read the per-run fault policy: two INDEPENDENT stop/continue
        switches for a run that hits trouble.

        - "board": what to do on a CC/OCP hardware fault (0=stop the run,
          1=continue, logging it).
        - "mismatch": what to do on an HC165 shift-register read-back
          mismatch (0=stop, 1=continue).

        Also reports which boards have faulted — the only record of that under
        a "continue" policy. NOTE it is CUMULATIVE: `arm` does not clear it, so
        it answers "which filaments have ever faulted since this controller came
        up", not "which faulted this run". (Verified: the list is identical
        before and after a run with no faults.) The per-run counters --
        mismatches/uncounted/underfed/triggerEdges -- ARE zeroed by arm, and
        shv_status's `faultFilament` is the single filament that stopped the
        current run. Three different questions.

        With unpopulated slots on the bench you almost certainly want board=1:
        the default stops the whole run at the first fault, and an empty slot
        promoted to ACTIVE is an open circuit and faults by definition.
        Measured on a 16-pulse scan with 14 empty slots: board=0 gave
        done=2/16 (Fault), board=1 gave 16/16.

        Returns {"ok", "board": 0|1, "mismatch": 0|1, "mismatchCount": int,
        "faultedSlots": [raw slot ints, 8*channel+position],
        "faultedFilaments": [global filament indices, your USER_INDEX numbering]}.
        """
        r = self._shv(controller, {"op": "fault_policy"})
        if r.get("ok") and "faultedFilaments" in r:
            r["faultedFilaments"] = [self._user_index_of(f) for f in r["faultedFilaments"]]
        return r

    def _set_fault_policy_one(self, controller: int = 1,
                         board: int | None = None,       # 0=stop the run,
                                                          # 1=continue, on a
                                                          # CC/OCP hardware fault
                         mismatch: int | None = None) -> dict:  # 0=stop, 1=continue,
                                                                  # on an HC165
                                                                  # read-back mismatch
        """Set one or both fault policies before arming a run. Omit either
        argument to leave that policy unchanged. Returns the same shape as
        get_fault_policy() (the values now in effect)."""
        body: dict = {"op": "fault_policy"}
        if board is not None:
            body["board"] = int(board)
        if mismatch is not None:
            body["mismatch"] = int(mismatch)
        r = self._shv(controller, body)
        if r.get("ok") and "faultedFilaments" in r:
            r["faultedFilaments"] = [self._user_index_of(f) for f in r["faultedFilaments"]]
        return r

    def get_trigger_delay(self) -> dict:
        """Read the trigger delay from EVERY connected controller, as one value.

        Returns {"ok", "delayUs", "applies", "consistent", "controllers": {
        "1": {...}, "2": {...}}}. `delayUs` is the common value, or None when
        the boards disagree -- ok is then False, because a rig whose two halves
        apply different delays frames the second controller's pulses in the
        wrong place. An RP2350 reset zeroes its delay, so a disagreement usually
        means a board restarted; set_trigger_delay() again to fix it.
        """
        return self._shv(1, {"op": "trigger_delay"})

    def set_trigger_delay(self, delay_us: int) -> dict:
        """Set the SyncIn->fire trigger delay (µs), uint16 0-65535, on EVERY
        connected controller at once. There is no per-controller form, on
        purpose: the master's envelope frames the second controller's pulses,
        and the two only line up if both apply the same delay. The backend
        writes all of them, reads all of them back, and refuses to arm while
        they disagree. Returns the same shape as get_trigger_delay().

        Returns {"ok", "delayUs": int, "applies": bool} — ALWAYS check
        "applies": a set that isn't honoured by the live fire path still
        returns "ok": True (the SETTING was stored) but "applies": False
        (it won't actually change when pulses fire) — this call can't
        silently do nothing without you being able to tell.
        """
        err = self._range_error("delay_us", int(delay_us), self._U16_MAX)
        if err:
            return {"ok": False, "error": err}
        return self._shv(1, {"op": "trigger_delay", "delay_us": int(delay_us)})

    def read_hv_diag165(self, controller: int = 1,
                        channel: int = 0,          # 0-7, which HV channel's
                                                    # 165 shift register to test
                        test_byte: int = 0x55,     # bit pattern written and
                                                    # read back (bench diagnostic
                                                    # only — no HV needs to be on)
                        settle_ms: int = 5) -> dict:  # wait between write and
                                                        # read-back, 0-100
        """Raw HC165 shift-register readback diagnostic for one channel —
        writes `test_byte`, reads it back twice, and clears. Safe at any
        time (doesn't touch HV/heating) — a bench/signal-integrity check,
        not something you'd call during normal operation.

        Returns {"ok", "channel", "test_byte", "r0", "r1", "r2", "r3"}:
        r0 = baseline after clearAll, r1 = first read after the raw write,
        r2 = repeat read (repeatability check), r3 = read after final clear.
        All four should read back as `test_byte` (r0/r3 as 0) if the shift
        chain and cabling are healthy.
        """
        return self._post("/api/hv-diag165", {
            "controller": int(controller), "channel": int(channel),
            "test_byte": int(test_byte), "settle_ms": int(settle_ms),
        }, timeout=5.0)

    def _set_hv_shift_hz_one(self, hz: int, controller: int = 1) -> dict:
        """Set the HC165 readback bit-bang SCK frequency (Hz) — for
        signal-integrity testing on long cables (e.g. drop it to 1 kHz to
        see a spike-free waveform on a scope). SET-ONLY: there is no
        separate "read current value" request — the firmware always
        requires a fresh value and echoes back the ACTUAL frequency now in
        effect (clamped 100 Hz-2 MHz, so what you asked for and what you
        get may differ slightly). Survives until the next reboot.

        Returns {"ok", "controller", "actualHz"}.
        """
        return self._post("/api/hv-shift-hz", {"controller": int(controller), "hz": int(hz)})

    # ── HV voltage / current set ──────────────────────────────────────────────

    def set_emission_v(self, volts: float) -> dict:
        """Set emission HV to |volts| V (output is negative).

        Backend loads the calibrated LUT, interpolates the DS3502 wiper, and
        writes it. Falls back to a linear approximation when no LUT is saved.
        Outside the settable range it is CLAMPED, written anyway, and the
        result says so: {"clamped": True, "requested_v", "warning"}.
        Returns {"ok", "wiper", "expect_v", "method"} (+ clamp fields).
        """
        return self._post("/api/hv/set-v", {"chan": "emission", "volts": abs(volts)})

    def set_focus_v(self, volts: float) -> dict:
        """Set focus HV to |volts| V (output is negative). Clamped like
        set_emission_v, and reported the same way.

        Returns {"ok", "wiper", "expect_v", "method"} (+ clamp fields).
        """
        return self._post("/api/hv/set-v", {"chan": "focus", "volts": abs(volts)})

    def set_emission_i(self, ma: float) -> dict:
        """Set emission current reference in mA (0–85.7). Linear DS3502
        scale: wiper = round(ma / 85.7 * 127).

        Above 85.7 mA it is CLAMPED to 85.7 and written anyway, and the result
        says so: {"clamped": True, "requested_ma", "max_ma", "warning"}.

        Returns {"ok", "wiper", "expect_ma"} (+ the clamp fields if clamped).
        """
        return self._post("/api/hv/set-i", {"ma": abs(ma)})

    # ── HV readback (ADS1115) ─────────────────────────────────────────────────

    def read_ads_all(self) -> dict:
        """Read all four ADS1115 channels.

        Keys on success: emiss_v (V), emiss_i_ma (mA), focus_v (V), ref_mv (mV),
        codes ([int×4] raw counts), mv ([float×4] pin voltages). On failure
        (e.g. STM32 unreachable — this happens; it's a live UART link, not a
        guarantee) returns {"ok": False, "error": ...} instead — check "ok"
        before indexing into this, or use the read_emission_v()/etc.
        wrappers below which already do that for you.
        """
        return self._get("/api/stm32/ads1115")

    def read_emission_v(self) -> float | None:
        """Measured emission voltage (V, negative). Returns None on failure
        (e.g. STM32 unreachable) — always check for None before using."""
        r = self.read_ads_all()
        return float(r["emiss_v"]) if r.get("ok") else None

    def read_emission_i(self) -> float | None:
        """Measured emission BEAM current (mA) — the ADS1115 reading on the
        shared emission bus. This is NOT a per-filament heating current;
        see read_filament_current()/read_filament_currents() for that.
        Returns None on failure — always check for None before using."""
        r = self.read_ads_all()
        return float(r["emiss_i_ma"]) if r.get("ok") else None

    def read_focus_v(self) -> float | None:
        """Measured focus voltage (V, negative). Returns None on failure —
        always check for None before using."""
        r = self.read_ads_all()
        return float(r["focus_v"]) if r.get("ok") else None

    # ── HV enable / disable ───────────────────────────────────────────────────

    def enable_emission(self, on: bool) -> dict:
        """Enable or disable the emission HV output."""
        return self._post("/api/stm32/hv-enable", {"ch": "emission", "on": on})

    def enable_focus(self, on: bool) -> dict:
        """Enable or disable the focus HV output."""
        return self._post("/api/stm32/hv-enable", {"ch": "focus", "on": on})

    def hv_status(self) -> dict:
        """HV pin states: {emission_on, focus_on, ads1115_alert, amc3301_diag}.
        On failure returns {"ok": False, ...} instead — those keys will be
        missing, so check "ok" before reading them."""
        return self._get("/api/stm32/hv-status")
