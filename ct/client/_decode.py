"""CTClient: human-readable result decoding.

One part of the client class, split out of one 9900-line file by section:
    Human-readable result decoding

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403


class _DecodeMixin:
    # ── Human-readable result decoding ────────────────────────────────────────
    # Every method above returns a plain dict -- convenient for scripting, but
    # not something you'd want to eyeball in a log. describe() turns any of
    # those dicts into one short English sentence, for a print()/log line
    # instead of dumping raw JSON. Best-effort: it recognizes a result SHAPE
    # (which keys are present), not which method produced it -- so it works
    # on a dict you've stashed/reloaded too -- and falls back to a short
    # generic ok/error summary for anything it doesn't recognize. Never
    # raises: an unrecognized dict still gets the generic summary, and a
    # non-dict input is just str()'d.

    # ShvArm's rejection reasons (RP2350 SimpleHvReject, simple_hv_schedule.h).
    # Mirrored here because the arm result carried a bare integer: "arm rejected
    # (code 7)" gives the caller nothing to act on, and the two codes that
    # actually occur on this bench mean opposite things -- IsoOff is "you forgot
    # to power the filament", NotReady is "the controller itself is not set up".
    _SHV_REJECT_NAMES = {
        0: "none",
        1: "IndexOutOfWindow — channel >= 8 or bit > 7",
        2: "WidthTooLarge — pulseWidthUs exceeds maxOnMs",
        3: "EmptyTable — no schedule entries were loaded",
        4: "TpsDisabled — the board's TPS55289 is not enabled",
        5: "TpsFault — the board's TPS55289 reports a fault",
        6: "IsoOff — the filament's board is not powered (isolated 12 V rail "
           "off). Put it at SLEEP or above before arming",
        7: "NotReady — the controller/channel is not initialised. Usually the "
           "active list was never pushed, or the RP2350 reset since it was",
        8: "StateConflict — already armed or running; disarm first",
    }

    _SHV_STATE_NAMES = {0: "idle", 1: "armed", 2: "running",
                        3: "complete", 4: "fault"}
    _SHV_STOP_REASON_NAMES = {0: "none", 1: "complete", 2: "read-back mismatch",
                              3: "inter-pulse timeout", 4: "total timeout",
                              5: "fault", 6: "disarmed"}

    def describe(self, result: dict) -> str:
        """One short English sentence describing any result dict this
        client returns -- e.g. `print(ct.describe(ct.active_one(5, 2900)))`
        instead of printing the raw dict. See the section comment above for
        what it recognizes. Never raises."""
        if not isinstance(result, dict):
            return str(result)
        try:
            return self._describe(result)
        except Exception as exc:   # a formatting bug here should never break a log line
            return f"(describe() failed: {exc}) {result}"

    def _describe_skips(self, r: dict) -> str:
        """The clause naming filaments that were DROPPED rather than commanded.

        Without this, describe() reported "Applied to 2 filament(s)." for a call
        that was asked for three -- the dead_skipped/excluded keys were sitting
        right there in the dict and never surfaced. A summary line that silently
        omits what it skipped is the same defect the keys were added to fix, one
        layer up. Returns "" when nothing was dropped, so callers can append it
        unconditionally."""
        parts = []
        dead = r.get("dead_skipped") or []
        if dead:
            parts.append(f"{len(dead)} dead-skipped: {dead}")
        exc = r.get("excluded") or []
        if exc:
            parts.append(f"{len(exc)} excluded (no board / controller offline): {exc}")
        uns = r.get("unslotted") or []
        if uns:
            parts.append(f"{len(uns)} unslotted: {uns}")
        # Not a skip -- these WERE commanded, but the 74HC165 read-back disagrees
        # with what was asked for, so the bit did not land. Surfaced here because
        # it forces ok:False and would otherwise be invisible in the summary line.
        mis = r.get("mismatched") or []
        if mis:
            parts.append(f"{len(mis)} read-back MISMATCH (did not land): {mis}")
        # Distinct from mismatched: two reads disagreed with each OTHER, so the
        # bit's state is unknown rather than known-bad. Named differently on
        # purpose -- "unknown" and "failed" are different things to act on.
        unst = r.get("unstable") or []
        if unst:
            parts.append(f"{len(unst)} UNCONFIRMED (165 read unstable, state unknown): {unst}")
        return (", " + ", ".join(parts)) if parts else ""

    def _describe_heating(self, h: dict) -> str:
        """One clause describing a verify=True sub-result -- either
        wait_for_current()'s shape ("measured_ma") or
        _wait_for_voltage_mode()'s ("cc_mode") -- with no trailing period,
        for embedding inline. Empty string if `h` isn't one of those."""
        if not isinstance(h, dict):
            return ""
        if "measured_ma" in h:
            # Never say "reached N mA" for something that was not measured. A
            # stop is confirmed from the board's POWER STATE (no current reading
            # exists once the rail is down -- see wait_for_current), and
            # reporting that as a measured zero would be claiming evidence we
            # do not have, in the summary line people actually read.
            if h.get("measured_from") == "power_state":
                return (f"not heating — board reports {h.get('power_state')} "
                        f"(confirmed by power state, not measured) "
                        f"in {h.get('elapsed_s', 0):.1f}s")
            verb = "reached" if h.get("ok") else "did NOT reach"
            src = f" [{h['measured_from']}]" if h.get("measured_from") else ""
            return (f"{verb} {h.get('measured_ma')} mA{src} "
                    f"(target {h.get('target_ma')} mA) in {h.get('elapsed_s', 0):.1f}s")
        if "cc_mode" in h:
            verb = "entered voltage-regulation mode" if h.get("ok") else "did NOT enter voltage mode"
            return f"{verb} (cc_mode={h.get('cc_mode')}) in {h.get('elapsed_s', 0):.1f}s"
        return ""

    def _describe(self, r: dict) -> str:
        ok = r.get("ok")

        # dead-filament shortcut (stop_one/idle_one/hv_grid_set/fire_single_pulse/...)
        if r.get("dead"):
            return f"Filament {r.get('filament')} is marked dead — no command sent."

        # Fire+measure results, in EITHER shape. fire_single_pulse(measure=True)
        # merges the fire in, so "fired" is a pulse COUNT and "error" is at the
        # top level; measure_pulse_current() nests the whole fire result under
        # "fired" instead. Both are checked BEFORE fire_single_pulse's own shape
        # below, since all three carry a "fired" key. Reading an int "fired" as
        # a dict is how this branch used to report "unknown error" while the
        # actual reason was sitting right there in r["error"].
        if "measured" in r and "fired" in r:
            nested = r["fired"] if isinstance(r.get("fired"), dict) else None
            fire_ok = nested.get("ok") if nested is not None else (r.get("fired") or 0) > 0
            why = (nested or r).get("error") or r.get("error")
            label = "measure_pulse_current" if nested is not None else "fire_single_pulse(measure=True)"
            if not fire_ok:
                return f"{label}: fire failed — {why or 'unknown error'}"
            n = len(r.get("measured") or [])
            if not ok:
                return (f"{label}: fired but only {n} pulse(s) measured "
                        f"(detector gap?){' — ' + why if why else ''} "
                        f"— ref {r.get('ref_mv')} mV")
            mas = [e.get("peak_ma") for e in r["measured"] if e.get("peak_ma") is not None]
            peaks = ", ".join(f"{m:.2f}" for m in mas) if mas else "?"
            return f"Measured {n} pulse(s), peak mA: {peaks} (ref {r.get('ref_mv')} mV)"

        # fire_single_pulse(): {"ok","fired","records","status",...}
        if "fired" in r and "records" in r and "status" in r:
            if r.get("timeout"):
                return f"fire_single_pulse timed out: {r.get('error')}"
            if not ok:
                return f"fire_single_pulse failed: {r.get('error', 'unknown error')}"
            st = r.get("status") or {}
            state = self._SHV_STATE_NAMES.get(st.get("state"), st.get("state"))
            reason = self._SHV_STOP_REASON_NAMES.get(st.get("stopReason"), st.get("stopReason"))
            return (f"Fired {r.get('fired')} pulse(s), schedule {state} "
                    f"(stop reason: {reason}), {st.get('totalPulsesDone', '?')} "
                    f"total pulses done, elapsed {st.get('elapsedMs', '?')} ms.")

        # read_board_status(): has state_name/fault_name
        if "state_name" in r or "fault_name" in r:
            if not ok:
                return (f"read_board_status failed for filament {r.get('filament')}: "
                        f"{r.get('error', 'unknown error')}")
            loc = f"ctrl {r.get('controller')} ch{r.get('channel')}.{r.get('mux_port')}"
            return (f"Filament {r.get('filament')} ({loc}): state={r.get('state_name')}, "
                    f"fault={r.get('fault_name')}.")

        # shv_status(), or its "status" sub-dict on its own
        if "stopReason" in r or ("state" in r and "entryCount" in r):
            state = self._SHV_STATE_NAMES.get(r.get("state"), r.get("state"))
            reason = self._SHV_STOP_REASON_NAMES.get(r.get("stopReason"), r.get("stopReason"))
            fil = r.get("filamentIndex")
            fil_txt = f", live filament {fil}" if fil not in (None, 0xFF, 255) else ""
            return (f"SHV engine: {state} (stop reason: {reason}){fil_txt}, "
                    f"{r.get('totalPulsesDone', '?')}/{r.get('totalPulsesTarget', '?')} "
                    f"pulses done, entry {r.get('entryIndex', '?')}/{r.get('entryCount', '?')}, "
                    f"elapsed {r.get('elapsedMs', '?')} ms.")

        # enable_emission()/enable_focus(): {"ok","ch","on"}
        if "ch" in r and "on" in r and "controller" not in r:
            if not ok:
                return f"HV enable failed for {r.get('ch')}: {r.get('error', 'unknown error')}"
            return f"{str(r.get('ch')).capitalize()} HV commanded {'ON' if r.get('on') else 'OFF'}."

        # hv_status(): {"emission_on","focus_on",...}
        if "emission_on" in r or "focus_on" in r:
            if not ok:
                return f"hv_status read failed: {r.get('error', 'unknown error')}"
            tag = lambda v: "unknown" if v is None else ("ON" if v else "OFF")
            return (f"Emission HV: {tag(r.get('emission_on'))}; Focus HV: {tag(r.get('focus_on'))}; "
                    f"ADS alert: {bool(r.get('ads1115_alert'))}; AMC diag: {bool(r.get('amc3301_diag'))}.")

        # get_fault_policy()/set_fault_policy(): {"board","mismatch","mismatchCount","faultedFilaments"}
        if "mismatchCount" in r and "faultedFilaments" in r:
            pol = lambda v: "continue" if v else "stop"
            fils = r.get("faultedFilaments") or []
            fils_txt = f", faulted: {fils}" if fils else ""
            return (f"Fault policy: board={pol(r.get('board'))}, mismatch={pol(r.get('mismatch'))}; "
                    f"{r.get('mismatchCount', 0)} read-back mismatch(es) this run{fils_txt}.")

        # get_ocp_threshold_one(): {"enabled","threshold_ma",...}
        if "threshold_ma" in r and "enabled" in r:
            if not ok:
                return (f"OCP threshold read failed for filament {r.get('filament')}: "
                        f"{r.get('error', 'unknown error')}")
            state = f"{r.get('threshold_ma')} mA" if r.get("enabled") else "OFF (protection disabled)"
            return f"Filament {r.get('filament')} OCP threshold: {state}."

        # get_trigger_delay()/set_trigger_delay(): {"delayUs","applies"}
        if "delayUs" in r:
            applies = "applies" if r.get("applies") else "does NOT apply (live fire path can't honour it)"
            return f"Trigger delay: {r.get('delayUs')} µs, {applies}."

        # acquire_lease()/release_lease()/renew_lease(): {"lock": {...}}
        if "lock" in r:
            lock = r.get("lock") or {}
            if not ok:
                return (f"Lease unavailable — held by '{lock.get('owner')}' "
                        f"({lock.get('expires_in_s', '?')} s remaining).")
            return (f"Lease held by '{lock.get('owner')}'"
                    + (f", {lock.get('expires_in_s')} s remaining." if lock else " (released)."))

        # wait_for_current()/_wait_for_voltage_mode() called standalone (not nested
        # under "heating"), or a *_one() result with an embedded "heating" sub-result
        direct = self._describe_heating(r)
        if direct:
            prefix = f"Filament {r.get('filament')}: " if "filament" in r else ""
            return prefix + direct + "."

        # stop_all/sleep_all/standby_all/idle_all/active_all/voltage_all():
        # {"ok","applied": int, "failed": [...]}
        if "applied" in r and "failed" in r and isinstance(r.get("applied"), int):
            if r.get("skipped_dead"):
                dead = r.get("dead_skipped") or []
                which = f" ({dead})" if dead else ""
                return f"No live filaments in the requested set (all dead-masked{which}) — nothing sent."
            failed = r.get("failed") or []
            fail_txt = f", {len(failed)} failed: {failed}" if failed else ""
            return f"Applied to {r.get('applied')} filament(s){fail_txt}{self._describe_skips(r)}."

        # hv_grid_set()/hv_grid_set_all()/set_ocp_threshold_all(): "applied" is a
        # list of filaments here, not a count
        if "applied" in r and "failed" in r:
            if r.get("skipped_dead"):
                dead = r.get("dead_skipped") or []
                which = f" ({dead})" if dead else ""
                return f"Nothing sent — every requested filament is dead-masked{which}."
            applied = r.get("applied")
            n = len(applied) if isinstance(applied, list) else applied
            failed = r.get("failed") or []
            fail_txt = f", {len(failed)} failed: {failed}" if failed else ""
            return f"Applied to {n} filament(s){fail_txt}{self._describe_skips(r)}."

        # stop_one/sleep_one/standby_one/idle_one/active_one/voltage_one() via
        # _state_one(): {"ok","filament","state": 1-6,"arg", ...}
        if "filament" in r and isinstance(r.get("state"), int) and 1 <= r["state"] <= 6:
            state_name = _STATE_NAMES.get(r["state"], r["state"])
            base = f"Filament {r.get('filament')} commanded to {state_name}"
            arg = r.get("arg")
            if arg:
                unit = "mV" if r["state"] == VOLTAGE else "mA"
                base += f" ({arg} {unit})"
            if not ok:
                base += f" — FAILED: {r.get('error', 'board did not ACK')}"
            heating_txt = self._describe_heating(r.get("heating")) if "heating" in r else ""
            if heating_txt:
                base += f"; verify: {heating_txt}"
            return base + "."

        # bulk per-filament table with no top-level "ok" (read_filament_currents())
        if "ok" not in r and r and all(isinstance(v, dict) for v in r.values()):
            present = sum(1 for v in r.values() if v.get("present"))
            return f"{len(r)} filament(s) in result, {present} reported present."

        # generic fallback
        if ok is True:
            extra = {k: v for k, v in r.items() if k != "ok"}
            return f"OK ({extra})." if extra else "OK."
        if ok is False:
            # "no_device" is not a failure to retry -- the chip the command
            # needs is not on the bus. Saying "Failed: ..." for it sends the
            # reader looking for a fault in something that is simply absent.
            if r.get("reason") == "no_device":
                return f"Chip not present: {r.get('error', 'the required chip is absent')}"
            return f"Failed: {r.get('error', 'unknown error')}"
        return str(r)
