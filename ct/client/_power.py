"""CTClient: power states, heating current, OCP, rig-wide settings.

One part of the client class, split out of one 9900-line file by section:
    power state
    power state — single filament
    Board heating status (single-board, direct I2C)
    OCP protection
    rig-wide settings
    Filament heating current (CC loop, per-filament)
    One shape, one failure convention

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # editors only: at run time _client.py binds CTClient into this module
    from ._client import CTClient


class _PowerMixin:
    # ── power state ───────────────────────────────────────────────────────────

    def _prep(self, state: int, filaments=None,
              currents: dict | None = None, arg: int = 0) -> dict:
        self._ensure_keepalive(state)
        # USER_INDEX values this call actually asked for that the dead mask
        # drops BEFORE anything is sent — _live()'s filtering is invisible
        # to the caller otherwise. A filament silently vanishing here (e.g.
        # because a filament_order swap happens to route a DEAD USER_INDEX
        # onto an otherwise-fine FID) looked exactly like
        # a backend bug until this was surfaced -- see the "why was 25
        # skipped" investigation this traced back to set_dead().
        # DE-ENERGISING STATES ARE NEVER FILTERED. The dead mask exists to stop
        # a faulty filament being powered, and it must never stop one being
        # turned OFF -- a filament marked dead while hot would otherwise have
        # no way to be stopped at all. _state_one() already honoured that
        # (it only refuses energising states); this batch path did not, so
        # stop_all() -- and with it session()'s teardown -- silently skipped
        # every dead filament. The backend has the same rule and would have
        # carried the STOP out; the client never sent it.
        if state not in self._ENERGISING_STATES:
            body: dict = {"state": state, "arg": arg}
            if filaments is not None:
                body["filaments"] = [int(self._fid_of(f)) for f in filaments]
            # filaments None stays None on the wire: "every populated board on
            # every CONNECTED controller". Expanding it into an explicit list
            # of all 96 made the backend report the other controller's half as
            # `excluded` on a one-controller bench, so a STOP that reached
            # everything that exists came back ok:False.
            return self._reindex_response(
                self._post("/api/filament-prep", body, timeout=20.0),
                keys=("applied", "failed", "excluded", "touched",
                      "not_this_controller", "unslotted", "dead_stopped"))
        requested = [int(f) for f in filaments] if filaments is not None else list(range(96))
        dead = self.dead   # bound once — property, see _live()
        dead_skipped = [f for f in requested if f in dead]
        live = self._live(filaments)
        if live is not None and len(live) == 0:
            return {"ok": True, "applied": 0, "failed": [], "skipped_dead": True,
                    "dead_skipped": dead_skipped}
        body: dict = {"state": state, "arg": arg}
        if live is not None:
            body["filaments"] = live
        if currents:
            # strip dead filaments (checked on USER_INDEX keys), then cross
            # the survivors' keys to FID for the wire
            alive = {int(k): v for k, v in currents.items() if int(k) not in dead}
            body["currents"] = {str(self._fid_of(k)): int(v) for k, v in alive.items()}
        # "excluded" (top level) + "touched"/"not_this_controller"/"unslotted"
        # (per-controller, inside "results") are filament-index lists too --
        # re-key them back to USER_INDEX the same as applied/failed, or a swap
        # would leak FIDs into what is supposed to be an all-USER_INDEX
        # response.
        r = self._reindex_response(
            self._post("/api/filament-prep", body, timeout=20.0),
            keys=("applied", "failed", "excluded", "touched", "not_this_controller",
                  "unslotted", "ladder_blocked", "dead_skipped"))
        # ladder_reasons is keyed by FID (as a string) -- re-key it too, or the
        # reasons name different filaments than the list beside them.
        for row in (r.get("results") or {}).values():
            if isinstance(row, dict) and isinstance(row.get("ladder_reasons"), dict):
                row["ladder_reasons"] = {str(self._user_index_of(int(k))): v
                                         for k, v in row["ladder_reasons"].items()}
        if dead_skipped:
            r["dead_skipped"] = dead_skipped
        return r

    def _verify_state(self, r: dict, state: int, timeout_s: float,
                      poll_interval_s: float = 0.5) -> dict:
        """verify=True for stop_all/sleep_all/standby_all: read back, from the
        hardware, that every commanded filament is in the state asked for.
        One bulk TPS status read per controller per poll (/api/tps-status) --
        never a per-board loop -- plus, for STANDBY, one bulk cached read.

        What counts as confirmed is the state's own definition:
            STOP     EN pin off (and the output enable not seen on)
            SLEEP    output enable READ BACK and off; the EN pin stays on
            STANDBY  EN on, output enable read back ON, and the CC loop in
                     VOLTAGE mode (cc_mode 0) -- IDLE/ACTIVE have EN and OE on
                     too, and differ only in regulating current (cc_mode 1)
        A current reading cannot confirm an OFF state: an output that is off has
        no measurement, and "no measurement" is not evidence of anything. A
        state whose deciding bit was not read (no OE read-back, no cached row)
        is reported unconfirmed, never as reached.

        Outcome under r["readback"]; the stragglers in r["not_reached"]."""
        commanded = self._commanded_filaments(r)
        # Dead filaments in a SLEEP batch were sent STOP instead (the backend's
        # DEAD_SLEEP_IS_STOP), so they are held to STOP's test, not SLEEP's.
        as_stop = sorted({int(f) for row in (r.get("results") or {}).values()
                          if isinstance(row, dict)
                          for f in (row.get("dead_stopped") or [])}) if state == SLEEP else []
        commanded = sorted(set(commanded) | set(as_stop))
        if not commanded:
            return r
        name = {STOP: "STOP", SLEEP: "SLEEP", STANDBY: "STANDBY"}[state]
        start = time.monotonic()
        pending = set(commanded)
        results: dict[int, dict] = {}
        polls = 0
        last: dict[int, dict] = {}
        modes: dict[int, object] = {}
        while True:
            st = self._get("/api/tps-status", timeout=10.0)
            polls += 1
            for _cid, row in (st.get("controllers") or {}).items():
                for fid, v in ((row or {}).get("filaments") or {}).items():
                    last[self._user_index_of(int(fid))] = v
            if state == STANDBY:
                for f, v in self.read_filament_current_cached(sorted(pending)).items():
                    modes[int(f)] = (v or {}).get("cc_mode")
            for f in sorted(pending):
                v = last.get(f)
                if v is None:
                    continue
                if state == STOP or f in as_stop:
                    done = (v.get("en") is False) and v.get("oe") is not True
                elif state == SLEEP:
                    done = v.get("oe") is False
                else:
                    done = (v.get("en") is True and v.get("oe") is True
                            and modes.get(f) == 0)
                if done:
                    results[f] = {"ok": True, "en": v.get("en"), "oe": v.get("oe")}
                    if f in as_stop:
                        results[f]["dead_stopped"] = True
                    if state == STANDBY:
                        results[f]["cc_mode"] = modes.get(f)
                    pending.discard(f)
            if not pending or time.monotonic() - start >= timeout_s:
                break
            time.sleep(poll_interval_s)
        for f in sorted(pending):
            v = last.get(f)
            if v is None:
                why = "its board was not read by the bulk TPS status"
            elif state == STOP or f in as_stop:
                why = f"EN pin still {'on' if v.get('en') else '?'}" + \
                      (", output enable ON" if v.get("oe") else "")
            elif v.get("oe") is None:
                why = (f"output enable not read back (firmware without the OE "
                       f"read, or the MODE read failed) — {name} cannot be confirmed")
            elif state == SLEEP:
                why = "output enable still ON"
            elif not v.get("en") or not v.get("oe"):
                why = f"output not on (EN {v.get('en')}, output enable {v.get('oe')})"
            elif modes.get(f) is None:
                why = "no cached CC-loop row — voltage mode cannot be confirmed"
            else:
                why = (f"CC loop in mode {modes.get(f)}, not voltage mode (0) — "
                       f"still regulating current, i.e. not at STANDBY")
            results[f] = {"ok": False, "en": (v or {}).get("en"),
                          "oe": (v or {}).get("oe"), "error": why}
            if state == STANDBY:
                results[f]["cc_mode"] = modes.get(f)
        h = Result({"ok": not pending, "state": name, "results": results,
                    "pending": sorted(pending),
                    "elapsed_s": round(time.monotonic() - start, 3), "polls": polls})
        out = {**r, "readback": h}
        if pending:
            out["not_reached"] = sorted(pending)
        return out

    def stop_all(self, filaments=None,
                 verify: bool = False,      # confirm every output is off -- see
                                             # _verify_state()
                 timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STOP a BATCH of filaments (all populated boards, or `filaments`).
        Dead filaments are INCLUDED: the dead mask only ever blocks energising.
        For exactly one filament, use stop_one().

        verify=True: read back, in bulk, that every commanded filament's TPS
        EN pin is off, and put the outcome under result["readback"] ({"ok",
        "results": {filament: {"ok", "en", "oe", "error"?}}, "pending"}); the
        ones still on are in `not_reached`, printed right under ok. The
        top-level "ok" is still "the command was accepted"."""
        r = self._prep(STOP, filaments)
        return self._verify_state(r, STOP, timeout_s) if verify else r

    def sleep_all(self, filaments=None,
                  verify: bool = False,     # confirm every output is off
                  timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """SLEEP a BATCH of filaments. For exactly one filament, use sleep_one().

        DEAD FILAMENTS ARE STOPPED, NOT SLEPT. SLEEP is not heating, but it
        turns the board's isolated 12 V rail and TPS EN pin on -- it powers the
        board. A dead filament is one that must not be used, so the backend
        sends it STOP instead (lower, and fully off -- skipping it would leave
        a dead filament at whatever it was, ACTIVE included) and names it in
        the per-controller `dead_stopped`. verify=True holds those to STOP's
        test (EN off).

        verify=True: as stop_all's, but SLEEP keeps the EN pin on -- it is
        "output enable off" -- so what is read back is the TPS output enable.
        Needs RP2350 firmware with the OE read (f08faa7); on older firmware
        every filament comes back unconfirmed, not off."""
        r = self._prep(SLEEP, filaments)
        return self._verify_state(r, SLEEP, timeout_s) if verify else r

    def standby_all(self, filaments=None,
                    verify: bool = False,    # confirm every output is on at
                                              # the 0.8 V voltage-mode floor
                    timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STANDBY a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use standby_one().

        verify=True: as stop_all's -- one bulk read-back per poll, outcome under
        result["readback"], stragglers in `not_reached`. STANDBY is confirmed
        by EN on, the output enable read back ON, and the CC loop in voltage
        mode (cc_mode 0): IDLE/ACTIVE also have EN and OE on, and differ only
        in regulating current. Needs RP2350 firmware with the OE read (f08faa7)."""
        r = self._prep(STANDBY, filaments)
        return self._verify_state(r, STANDBY, timeout_s) if verify else r

    def _commanded_filaments(self, r: dict) -> list[int]:
        """The filaments a batch command actually reached: every controller's
        `touched`, minus `failed`. None of the requested-but-dead, unslotted or
        other-controller ones -- those were never commanded, so there is
        nothing of theirs to wait for."""
        touched: set[int] = set()
        for row in (r.get("results") or {}).values():
            if isinstance(row, dict):
                touched.update(int(f) for f in (row.get("touched") or []))
        return sorted(touched - {int(f) for f in (r.get("failed") or [])})

    def _verify_batch(self, r: dict, currents: dict | None, default_ma: float,
                      tolerance_ma: float, timeout_s: float) -> dict:
        """verify=True for idle_all/active_all: wait for every commanded
        filament at once and merge the outcome under r["heating"], the same
        place idle_one/active_one put theirs. One bulk read per poll for the
        whole batch (wait_for_currents), never one loop per filament."""
        commanded = self._commanded_filaments(r)
        if not commanded:
            return r              # nothing was commanded: nothing to wait for
        per = {int(k): float(v) for k, v in (currents or {}).items()}
        targets = {f: per.get(f, float(default_ma)) for f in commanded}
        zero = sorted(f for f, ma in targets.items() if abs(ma) <= tolerance_ma)
        rest = {f: ma for f, ma in targets.items() if f not in zero}
        h = (self.wait_for_currents(rest, tolerance_ma=tolerance_ma, timeout_s=timeout_s)
             if rest else {"ok": True, "results": {}, "pending": []})
        if zero:
            # Commanded to ~0 mA -- usually default_ma left at 0. Not
            # confirmable by the bulk read, and NOT counted as arrived.
            h = {**h, "ok": False, "zero_target": zero,
                 "error": ((h.get("error") + "; ") if h.get("error") else "")
                          + f"{len(zero)} filament(s) were commanded to ~0 mA "
                            f"(no `currents` entry and default_ma "
                            f"{default_ma:g}) — nothing to verify"}
        not_reached = {int(f) for f, row in (h.get("results") or {}).items()
                       if not row.get("ok")}
        out = {**r, "heating": Result(h)}
        if not_reached or zero:
            out["not_reached"] = sorted(not_reached | set(zero))
        return out

    def idle_all(self,
                filaments=None,               # None = every populated board
                                               # (minus dead mask); or an
                                               # explicit list of indices
                currents: dict | None = None,   # {filament: mA} -- explicit
                                                 # PER-FILAMENT override
                default_ma: float = 0,        # mA for any filament NOT in
                                                  # `currents` above -- ⚠ NO
                                                  # firmware-side default: a
                                                  # filament with neither an
                                                  # entry here nor in
                                                  # `currents` idles at 0 mA
                verify: bool = False,          # wait until every commanded
                                                # filament has settled -- see
                                                # _verify_batch()
                tolerance_ma: float = 150.0,   # only used if verify=True
                timeout_s: float = 20.0) -> dict:  # only used if verify=True;
                                                    # IDLE from cold takes ~15 s
        """IDLE a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use idle_one() instead — it's clearer and
        avoids the "everyone else falls back to default_ma" footgun below.

        currents: {filament: mA} — per-filament idle current override.
        default_ma: current used for any filament NOT listed in `currents`
        (including every filament, if `currents` is omitted entirely).
        There is no firmware-side default — omitting both leaves every
        filament idling at 0 mA.

        verify=True: wait, in ONE bulk polling loop, until every filament that
        was actually commanded has settled at its current, and merge the
        outcome under result["heating"] ({"ok", "results": {filament:
        <idle_one's heating shape>}, "pending", ...}). `not_reached` lists the
        ones that did not -- including any commanded to ~0 mA, which cannot be
        confirmed. The top-level "ok" is still "the command was accepted", as
        for idle_one; check heating["ok"] for arrival.
        """
        r = self._prep(IDLE, filaments, currents, arg=int(default_ma))
        return (self._verify_batch(r, currents, default_ma, tolerance_ma, timeout_s)
                if verify else r)

    def active_all(self,
                   filaments=None,               # None = every populated
                                                  # board (minus dead mask)
                   currents: dict | None = None,   # {filament: mA} -- explicit
                                                    # PER-FILAMENT override
                   default_ma: float = 0,        # mA for any filament NOT
                                                     # in `currents` -- same
                                                     # "no default" footgun as
                                                     # idle_all, see above
                   verify: bool = False,          # wait until every commanded
                                                   # filament has settled
                   tolerance_ma: float = 150.0,   # only used if verify=True
                   timeout_s: float = 10.0) -> dict:  # only used if verify=True
        """ACTIVE a BATCH of filaments, excluding the dead mask.
        For exactly one filament, use active_one() instead.

        currents: {filament: mA} — per-filament active current override.
        default_ma: current used for any filament NOT listed in `currents`.

        verify=True: as idle_all's -- one bulk wait for the whole batch, the
        outcome under result["heating"], the stragglers in `not_reached`.
        """
        r = self._prep(ACTIVE, filaments, currents, arg=int(default_ma))
        return (self._verify_batch(r, currents, default_ma, tolerance_ma, timeout_s)
                if verify else r)

    def voltage_all(self,
                    filaments=None,               # None = every populated
                                                   # board (minus dead mask)
                    millivolts: dict | None = None,  # {filament: mV} --
                                                      # explicit PER-FILAMENT
                                                      # target-voltage override
                    default_mv: float = 800) -> dict:  # mV for any filament
                                                        # NOT in `millivolts` --
                                                        # defaults to 800 (the
                                                        # firmware's own
                                                        # STANDBY floor), NOT 0
                                                        # like idle_all/
                                                        # active_all's
                                                        # default_ma, because 0
                                                        # is below the
                                                        # firmware's clamp
        """Drive a BATCH of filaments to manual VOLTAGE mode (PowerState 6)
        at a fixed mV, excluding the dead mask. For exactly one filament,
        use voltage_one() instead.

        VOLTAGE is a fixed-voltage hold, NOT current-regulated (unlike
        IDLE/ACTIVE's closed CC loop) — mostly for bench/calibration use
        (e.g. probing an arbitrary point on the load curve) rather than
        normal heating control.

        millivolts: {filament: mV} — per-filament target-voltage override.
        default_mv: mV used for any filament NOT listed in `millivolts`
        (including every filament, if `millivolts` is omitted entirely).
        Firmware clamps every value to 0.8-15 V (800-15000 mV) regardless
        of what's requested here.
        """
        return self._prep(VOLTAGE, filaments, millivolts, arg=int(default_mv))

    # ── power state — single filament ─────────────────────────────────────────
    # These use the RP2350's own single-board CH_SET_POWER_STATE wire format
    # (one filament, one frame) — NOT the batch-with-one-item path the *_all
    # methods above take even when given a 1-element filaments= list. Prefer
    # these whenever you're operating on exactly one filament.
    #
    # None of these raise. A dead filament, an unmapped filament, a
    # disconnected controller, or a board that simply didn't ACK all come
    # back as {"ok": False, "error": "...", ...} — check "ok" yourself.

    def _idle_ceiling_refusal(self, filament, arg: int) -> dict | None:
        """The refusal an over-ceiling IDLE gets, or None if it is in range.

        Local so a script gets the same answer whether or not the backend is
        reachable, and so the number that is wrong is named rather than
        silently replaced -- which is exactly what the firmware does to it.
        """
        if int(arg) <= self._IDLE_CEILING_MA:
            return None
        return {"ok": False, "above_idle_ceiling": True,
                "filament": filament,
                "idle_ceiling_mA": self._IDLE_CEILING_MA,
                "error": f"IDLE {int(arg)} mA is above the "
                         f"{self._IDLE_CEILING_MA} mA ceiling — the RP2350 "
                         f"would clamp it to {self._IDLE_CEILING_MA} and report "
                         f"success, so a verify would wait for a current that "
                         f"never arrives. Ask for {self._IDLE_CEILING_MA} or "
                         f"less, or use active_one() if you need more"}

    def _ensure_keepalive(self, state: int) -> None:
        """Arm the background keepalive the first time this client energises
        anything, and leave it running for the life of the client.

        THE GENERAL FIX, rather than a keepalive bolted onto each long
        operation. Long holds are everywhere -- a voltage sweep, a 40 s
        wait_for_current (polls, and polls deliberately do not renew), a
        thermal settle, a schedule, or any user script that commands ACTIVE and
        then spends a minute on its own arithmetic. Patching them one at a time
        guarantees the one that gets missed is the one that trips.

        WHAT THIS MEANS FOR THE GUARANTEE, stated plainly: the watchdog
        protects against the CLIENT PROCESS DYING, which is the failure it was
        asked for -- the thread is a daemon, so it stops the instant the
        process does and the backend's timer starts running. It does NOT
        protect against a live client that has simply been abandoned (an
        interactive session someone walked away from). If that matters, pass
        keepalive=False and renew explicitly.
        """
        if state not in self._ENERGISING_STATES or not self._keepalive_enabled:
            return
        with self._keepalive_lock:
            if self._keepalive_stop is None:
                self._keepalive_stop = self._start_keepalive()

    def _state_one(self, filament: int, state: int, arg: int, op: str) -> dict:
        self._ensure_keepalive(state)
        if state == IDLE:
            refusal = self._idle_ceiling_refusal(int(filament), int(arg))
            if refusal:
                return refusal
        # Block energising, never de-energising: STOP/SLEEP on a dead filament
        # must go through, or marking one dead would leave it with no way to be
        # turned off -- the opposite of the point. Same rule as the backend's.
        if state in self._ENERGISING_STATES and self._is_dead(filament):
            return self._dead_result(filament)
        r = self._post("/api/filament-state",
                       {"filament": self._fid_of(filament), "state": state, "arg": int(arg)})
        # The board-didn't-ACK soft failure carries no "error" message on the
        # wire — fill one in so a printed/logged result is never just "None".
        if not r.get("ok") and not r.get("error"):
            r["error"] = "board did not ACK (absent, unseated, or faulted?)"
        r["filament"] = int(filament)   # always echo back YOUR (USER_INDEX) number, not the FID
        return r

    def wait_for_current(self, filament: int,
                         target_ma: float,             # the current you commanded
                                                        # (idle_one/active_one's
                                                        # current_ma) — what we're
                                                        # waiting to see measured
                         tolerance_ma: float = 150.0,   # how close counts as "there"
                                                         # (CC loop settles near, not
                                                         # exactly at, the target)
                         timeout_s: float = 5.0,        # give up and return
                                                         # ok=False after this long
                         poll_interval_s: float = 0.2) -> dict:  # how often to
                                                                  # re-check while waiting
        """Poll the REAL measured heating current until it settles within
        `tolerance_ma` of `target_ma`, or `timeout_s` elapses.

        A command like idle_one()/active_one() only confirms the RP2350
        accepted the command — it says nothing about whether the CC loop
        actually got the filament there (a board could be absent, faulted,
        thermally slow, or the target could simply be unreachable). This
        polls read_filament_currents() (no I2C, cheap) to give you the real
        answer. Never raises — check the returned "ok".

        Returns: {"ok": bool,          # reached target within tolerance
                  "filament": int, "target_ma": float, "measured_ma": float,
                  "measured_valid": bool,   # False = NO source could give a
                                            # live measurement; measured_ma is
                                            # 0.0 filler and "ok" is False
                  "measured_from": "cached" | "ina219" | "power_state",
                                            # which evidence answered. See the
                                            # note below on "power_state".
                                            # The CC cache stops being maintained
                                            # once the loop isn't regulating (i.e.
                                            # after stop/sleep), so a 0 mA target
                                            # is normally confirmed via ina219
                  "elapsed_s": float, "present": bool, "cc_mode": int,
                  "faulted": bool}   # True = gave up EARLY because the CC
                                     # loop reported this channel faulted,
                                     # with "error" naming it. Distinct from
                                     # ok=False after a full timeout, which
                                     # means it was still trying.
                  cc_mode: 0=voltage 1=current(regulating) 2/3=fault.
        A ~0 mA TARGET IS CONFIRMED BY POWER STATE, NOT BY CURRENT, and it has
        to be: stopping a board drops its rail, so the INA presence probe stops
        answering for it and no current reading exists any more. Waiting for a
        measured 0 would therefore never succeed. Such a result carries
        measured_from="power_state" and measured_valid=False -- it is a
        confirmation, explicitly not a reading, and describe() says so.

        Known limit, and why it is the acceptable direction: once the rail is
        down this cannot tell a real stopped board from a board that was never
        there (read_board_status reports STOP for both, and the only probe that
        distinguishes them is slow and leaves boards at SLEEP). So stop_one() on
        an absent filament reports success. The dangerous direction -- calling a
        still-heating board stopped -- cannot happen: the state comes from the
        firmware's own per-board read, and a board being driven reports
        IDLE/ACTIVE, not STOP. Verified on hardware.
        """
        start = time.monotonic()
        # Deliberately `start`, not 0: the first struggling check happens 2 s in,
        # not immediately. The bit LATCHES while the output stays down, so a
        # filament that was struggling in an earlier run still reads struggling
        # before this attempt has driven it at all. Checking at t=0 would refuse
        # a repaired load forever -- refuse, never drive, never clear, refuse.
        # The delay gives the firmware a revive pass to clear it.
        last_struggle_check = start
        # Deliberately `start - 1e9`, not `start`: the FIRST iteration should be
        # allowed to take the live read (that is the one that answers a stale
        # cache immediately); the rate limit is only about the ones after it.
        last_live_read = start - 1e9
        fault_streak = 0            # consecutive mode 2/3 reads; see below
        deadline = start + timeout_s
        data: dict = {}
        while True:
            data = self.read_filament_current_cached(filament).get(int(filament), {})
            # current_mA is None (key PRESENT, value None) when the RP2350's
            # cached reading isn't a live measurement yet -- so .get(...,0)
            # never fires and float(None) would raise. None also must not
            # count as a measured 0, or stop_one(verify=True) (target 0,
            # tolerance 50) would report success off a missing reading.
            raw = data.get("current_mA")
            valid = raw is not None
            source = "cached"
            if not valid:
                # The CC cache has no live measurement. That is NORMAL and
                # permanent for a stop/sleep/standby target: once the loop is no
                # longer regulating the port it stops maintaining a current, so
                # polling the cache alone can never confirm 0 mA and this call
                # would burn its full timeout and report "did NOT reach" for a
                # filament that stopped correctly (measured on hardware: 8.7 s to
                # a wrong answer). The live INA219 read CAN still see it. Fall
                # back to it -- it costs an I2C sweep, but only on the iteration
                # where the cache has nothing, and a stop confirms on the first
                # one. Skipped automatically mid-run: the backend refuses the
                # sweep while a schedule fires and answers cached (cached=True),
                # which we do not accept as a measurement.
                # A STOPPED board cannot be confirmed by CURRENT at all, and
                # that is structural, not a flake. Stopping removes the board's
                # rail, so the live INA presence probe reports present=False --
                # indistinguishable from a board that was never there, which is
                # exactly why the `present` guard below exists. Result: a
                # stop/sleep/standby verification could never succeed; measured
                # here, stop_one(verify=True) burned its full timeout and said
                # "did NOT reach 0.0 mA" for a filament that had stopped
                # correctly. (One earlier run passed only because the rail had
                # not collapsed yet -- timing luck, not confirmation.)
                #
                # For a ~0 mA target the honest instrument is the board's own
                # power state: if the CC loop is no longer driving it and the
                # board reports a non-heating state, it is not heating. That is
                # reported as measured_from="power_state" with
                # measured_valid=False, so it can never be mistaken for a
                # measured zero -- confirmation, but explicitly not a reading.
                if abs(float(target_ma)) <= tolerance_ma:
                    st = self.read_board_status(filament)
                    if st.get("ok") and st.get("state") in (STOP, SLEEP, STANDBY):
                        return {"ok": True, "filament": int(filament),
                                "target_ma": float(target_ma), "measured_ma": 0.0,
                                "measured_valid": False, "measured_from": "power_state",
                                # NOT "state_name": that key is read_board_status's
                                # shape and describe() matches it first, which made a
                                # standalone wait_for_current result render as a board
                                # status line instead of a heating one.
                                "power_state": st.get("state_name"),
                                "elapsed_s": time.monotonic() - start,
                                "present": bool(data.get("present", False)),
                                "cc_mode": data.get("cc_mode", 0),
                                "note": (f"not heating — board reports "
                                         f"{st.get('state_name')}; no current reading is "
                                         f"available once the rail is down, so this is "
                                         f"confirmed by power state, not measured")}
                # Rate-limited, NOT once per poll. This is a live INA219 I2C mux
                # sweep -- the most expensive read in this client -- and it fires
                # exactly when the cache has nothing, which for an absent board
                # is every single iteration for the whole timeout. Two costs, and
                # the second is the bad one: measured, polling a live sweep slows
                # a CC ramp by ~20%, so on a filament that is merely SLOW this
                # fallback was making it slower while waiting for it. For an
                # absent board it is pure waste -- the sweep reports present=False
                # and the guard below rejects the result every time.
                #
                # Once a second is enough to catch the case this exists for (a
                # cache that is genuinely stale while the board is fine).
                if time.monotonic() - last_live_read < max(poll_interval_s, 1.0):
                    live = {}
                else:
                    last_live_read = time.monotonic()
                    live = self.read_filament_vi_live([filament]).get(int(filament), {})
                # `present` is NOT optional here. The INA sweep reports a board it
                # could not find as 0 mA with present=False, so accepting any
                # non-None value re-opens the exact false-success this method
                # exists to close -- an absent board "confirmed" at 0 mA. That
                # regression was introduced by this very fallback and caught in
                # test; the guard is the only thing separating "measured 0" from
                # "nothing there to measure".
                if live and not live.get("cached") and live.get("present"):
                    lraw = live.get("current_mA")
                    if lraw is not None:
                        raw, valid, source = lraw, True, "ina219"
            measured = float(raw) if valid else 0.0
            # The CC loop's OWN verdict, when it has one. cc_mode 2/3 means the
            # firmware faulted this channel -- it is not going to arrive, and
            # continuing to poll just burns the timeout and then reports a
            # generic "did not reach target" that reads identically to a
            # thermally slow filament or an unreachable setpoint.
            #
            # This covers an OPEN filament and a genuine OCP trip. It does NOT
            # cover a SHORT, and that is not an oversight here but a property of
            # the firmware: mode 2/3 is only ever set behind a
            # `feedbackMv >= 2000` guard, and a shorted output cannot reach 2 V
            # -- that is what shorted means. A short therefore sits in mode 1
            # indefinitely while the guardian keeps reviving the collapsed
            # output. Measured on this bench by the RP2350 session: a shorted
            # board commanded Idle 1200 mA held mode 1, measMv 0, measMa 1-2 for
            # minutes. So never read "mode 1 and current not rising" as healthy
            # -- see the struggling[] check below, which is what catches it.
            #
            # There is also still no positive "arrived" signal, so ok= below
            # compares a POLLED sample against the target. That comparison
            # belongs in the loop, not here: a trip between two polls is
            # invisible to this host. Requested on the RP2350 side (they hold
            # tpsCcConverged_/tpsCcCapped_ internally, unreported).
            # ARRIVAL: the firmware's own answer, which is the whole point --
            # comparing polled samples here was always the wrong place for the
            # judgement. The loop sees every sample; this host sees one every
            # 50-100 ms across a shared link, and (measured) polling live INA
            # reads to watch a ramp slows the ramp by ~20%.
            #
            #   settled -> it arrived. Believe it over any comparison here.
            #   capped  -> pinned at the voltage cap, target NOT reached. This
            #              answers what a timeout could not: "cannot arrive at
            #              this cap", not "still on its way". Stop; waiting
            #              longer cannot help unless the load or cap changes.
            arrival = data.get("arrival")
            if arrival == "settled" and abs(float(target_ma)) > tolerance_ma:
                return {"ok": True, "filament": int(filament),
                        "target_ma": float(target_ma), "measured_ma": measured,
                        "measured_valid": valid, "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode"), "arrival": arrival,
                        "faulted": False}
            if arrival == "capped" and abs(float(target_ma)) > tolerance_ma:
                return {"ok": False, "filament": int(filament),
                        "target_ma": float(target_ma), "measured_ma": measured,
                        "measured_valid": valid, "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode"), "arrival": arrival,
                        "faulted": False, "capped": True,
                        "error": f"CC loop is CAPPED — pinned at the voltage cap "
                                 f"with {target_ma} mA unreached (holding "
                                 f"{measured} mA). Not a fault and not slow: "
                                 f"unreachable at this cap. Raise the cap or "
                                 f"change the load; waiting will not help."}
            # CANNOT START -- the only signal that catches a SHORT. A short
            # never sets the fault bits (they need feedbackMv >= 2000, which a
            # short cannot reach) and its arrival bits read "ramping" forever,
            # so on a shorted board every check above says "still on its way"
            # and this would burn the full timeout. Measured on CH2.8: arrival
            # "ramping", mode 1, 1 mA, struggling set ~6.5 s in.
            #
            # Checked on a slow cadence of its own: it is a TPS register read,
            # not something to poll at the loop rate, and the bit needs a few
            # seconds of failed revives to appear anyway.
            if (abs(float(target_ma)) > tolerance_ma
                    and time.monotonic() - last_struggle_check >= 2.0):
                last_struggle_check = time.monotonic()
                sr = self._get("/api/tps-struggling", timeout=5.0)
                rows = (sr.get("struggling") or {}) if sr.get("ok") else {}
                fid = int(self._fid_of(filament))
                for _cid, fids in rows.items():
                    # None = old firmware with no mask. Absent, not empty: it
                    # must not read as "nothing is struggling".
                    if fids and fid in fids:
                        return {"ok": False, "filament": int(filament),
                                "target_ma": float(target_ma),
                                "measured_ma": measured, "measured_valid": valid,
                                "measured_from": source,
                                "elapsed_s": time.monotonic() - start,
                                "present": bool(data.get("present", False)),
                                "cc_mode": data.get("cc_mode"),
                                "arrival": data.get("arrival"),
                                "faulted": False, "cannot_start": True,
                                "error": "the firmware cannot get this output "
                                         "started (TPS 'struggling': 3+ failed "
                                         "revives of a collapsed output). A SHORT "
                                         "looks exactly like this — it never sets "
                                         "the fault bits and its arrival stays "
                                         "'ramping', so nothing else here catches "
                                         "it. Check the load before retrying."}
            # A fault must be CONFIRMED before it ends the wait. A board that
            # trips OCP on the startup inrush and comes up on the next revive is
            # a NORMAL outcome, not a failure -- and it can flash FaultOcp while
            # that is happening. Returning on the first sighting would report a
            # hard fault for a board that recovered a moment later, which is the
            # same single-sample mistake as trusting one current reading.
            #
            # `struggling` is the firmware's own confirmed version of this (3
            # consecutive failed revives) and needs no debounce here; the mode
            # bits are instantaneous, so they do.
            cc_mode = data.get("cc_mode")
            fault_streak = fault_streak + 1 if cc_mode in (2, 3) else 0
            if fault_streak >= self._FAULT_CONFIRM_READS and abs(float(target_ma)) > tolerance_ma:
                return {"ok": False, "filament": int(filament),
                        "target_ma": float(target_ma), "measured_ma": measured,
                        "measured_valid": valid, "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        # `arrival` was missing from THIS return only, so a
                        # faulted result came back without a key the docstring
                        # promises and every other path supplies -- a caller
                        # reading r["arrival"] got None and could not tell
                        # "firmware reports no arrival" from "this return
                        # forgot to include it".
                        "cc_mode": cc_mode, "arrival": data.get("arrival"),
                        "faulted": True,
                        "error": f"CC loop reports this channel FAULTED (cc_mode "
                                 f"{cc_mode}: 2=open filament, 3=OCP/SCP) on "
                                 f"{fault_streak} consecutive reads — not a "
                                 f"transient startup trip, which recovers on the "
                                 f"next revive. Not ramping toward {target_ma} mA."}
            # The polled comparison is now only a FALLBACK, for firmware that
            # does not report arrival. When arrival IS reported it is
            # authoritative: "ramping" means the loop says it has not arrived,
            # and a host-side sample that happens to land inside the tolerance
            # band must not override that. Measured: commanding IDLE 1500 on a
            # cold filament, the inrush passes DOWN through 1569 mA within
            # 0.4 s, so the comparison declared success while the loop was
            # still ramping -- the exact false success this arrival bit exists
            # to remove.
            if arrival is not None:
                ok = False          # settled/capped already returned above
            else:
                ok = valid and abs(measured - target_ma) <= tolerance_ma
            if ok or time.monotonic() >= deadline:
                return {"ok": ok, "filament": int(filament), "target_ma": float(target_ma),
                        "measured_ma": measured, "measured_valid": valid,
                        "measured_from": source,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False)),
                        "cc_mode": data.get("cc_mode", 0),
                        "arrival": data.get("arrival"), "faulted": False}
            time.sleep(poll_interval_s)

    @staticmethod
    def _poll_intervals(first_s: float, cap_s: float, factor: float = 1.6):
        """Sleep durations for a wait loop: responsive at first, then backing
        off geometrically to `cap_s`.

        A fixed short sleep is not a poll rate. Measured on this bench, idle:
        SHV_GET_STATUS is 15 ms and the ESP32's /pulse_events is 65 ms, so a
        `sleep(0.05)` loop is not "20 Hz" -- it is back-to-back requests with a
        gap smaller than the round trip, i.e. as fast as the link will go. That
        matters because both of those loops run WHILE the thing they are
        watching is happening: the SHV poll shares the single RP2350 link with
        the schedule that is firing and with the 20 fps telemetry push, and
        /pulse_events is served by the ESP32's config_portal, which is
        single-threaded with the tcp_bridge relay carrying those very pulses
        (200-300 ms per request under load) -- so polling for pulse events
        slows the path the pulse events arrive on.

        Backing off keeps the first few checks fast (a fault or an immediate
        completion is still caught at once) while a wait that turns out to be
        long costs a request every `cap_s` instead of continuously.
        """
        delay = first_s
        while True:
            yield delay
            delay = min(cap_s, delay * factor)

    def wait_for_currents(self, targets: dict,        # {filament: target mA}
                          tolerance_ma: float = 150.0,
                          timeout_s: float = 10.0,
                          poll_interval_s: float = 0.2) -> dict:
        """Wait for MANY filaments to reach their targets, in ONE polling loop.

        Same rules and the same per-filament result shape as
        wait_for_current() -- firmware `arrival` is authoritative, faults are
        debounced over _FAULT_CONFIRM_READS reads, and `struggling` catches a
        short -- but one bulk read per tick covers the whole batch instead of
        one loop per filament.

        Use this whenever more than one filament is being brought up. The
        cached read is a BULK command: it returns every board's current whether
        you asked for one or ninety-six, so verifying a 35-filament heating
        step one filament at a time costs 35x the link traffic for exactly the
        same data. The struggling check is shared too -- one /api/tps-struggling
        every 2 s for the batch, not one per filament.

        Zero targets are REFUSED here. Confirming ~0 mA needs the live-INA219
        and power-state fallbacks (a stopped board drops its rail, so no
        current reading exists any more -- see wait_for_current()), and those
        are per-board reads that would put back exactly the traffic this
        exists to remove. Use stop_one(verify=True) for those.

        Returns {"ok": all arrived, "results": {filament: <wait_for_current
        shape>}, "pending": [...], "elapsed_s", "polls"}.
        """
        want = {int(f): float(ma) for f, ma in (targets or {}).items()}
        zero = sorted(f for f, ma in want.items() if abs(ma) <= tolerance_ma)
        if zero:
            return {"ok": False, "results": {}, "pending": sorted(want),
                    "elapsed_s": 0.0, "polls": 0,
                    "error": f"filament(s) {zero} have a ~0 mA target; a stop "
                             f"cannot be confirmed by the bulk cached read "
                             f"(the rail drops and the reading disappears). "
                             f"Use stop_one(verify=True) for those."}
        live = {f: ma for f, ma in want.items() if not self._is_dead(f)}
        results: dict[int, dict] = {f: self._dead_result(f) for f in want
                                    if self._is_dead(f)}
        if not live:
            return {"ok": False, "results": results, "pending": [],
                    "elapsed_s": 0.0, "polls": 0,
                    "error": "every requested filament is in the dead mask"}

        start = time.monotonic()
        deadline = start + timeout_s
        last_struggle_check = start
        streaks = {f: 0 for f in live}
        pending = set(live)
        polls = 0

        def finish(f, data, ok, **extra):
            raw = data.get("current_mA")
            return {"ok": ok, "filament": f, "target_ma": live[f],
                    "measured_ma": float(raw) if raw is not None else 0.0,
                    "measured_valid": raw is not None, "measured_from": "cached",
                    "elapsed_s": round(time.monotonic() - start, 3),
                    "present": bool(data.get("present", False)),
                    "cc_mode": data.get("cc_mode"),
                    "arrival": data.get("arrival"), "faulted": False, **extra}

        while pending:
            rows = self.read_filament_current_cached(sorted(pending))
            polls += 1
            # One struggling read for the whole batch, on its own slow cadence:
            # it is a TPS register read, and the bit needs a few seconds of
            # failed revives to appear at all.
            struggling: set = set()
            if time.monotonic() - last_struggle_check >= 2.0:
                last_struggle_check = time.monotonic()
                sr = self._get("/api/tps-struggling", timeout=5.0)
                if sr.get("ok"):
                    fid_to_user = {int(self._fid_of(f)): f for f in pending}
                    for _cid, fids in (sr.get("struggling") or {}).items():
                        for fid in (fids or []):      # None = no mask (old fw)
                            if fid in fid_to_user:
                                struggling.add(fid_to_user[fid])
            for f in sorted(pending):
                data = rows.get(f) or {}
                arrival = data.get("arrival")
                if arrival == "settled":
                    results[f] = finish(f, data, True); pending.discard(f); continue
                if arrival == "capped":
                    results[f] = finish(f, data, False, capped=True,
                        error=f"CC loop is CAPPED — pinned at the voltage cap "
                              f"with {live[f]} mA unreached. Unreachable at this "
                              f"cap; waiting will not help.")
                    pending.discard(f); continue
                if f in struggling:
                    results[f] = finish(f, data, False, cannot_start=True,
                        error="the firmware cannot get this output started (TPS "
                              "'struggling'). A SHORT looks exactly like this.")
                    pending.discard(f); continue
                cc_mode = data.get("cc_mode")
                streaks[f] = streaks[f] + 1 if cc_mode in (2, 3) else 0
                if streaks[f] >= self._FAULT_CONFIRM_READS:
                    results[f] = finish(f, data, False, faulted=True,
                        error=f"CC loop reports this channel FAULTED (cc_mode "
                              f"{cc_mode}) on {streaks[f]} consecutive reads.")
                    pending.discard(f); continue
                # Fallback for firmware with no arrival bits, same as the
                # single-filament version: compare the sample only when the
                # loop has given no verdict of its own.
                raw = data.get("current_mA")
                if arrival is None and raw is not None \
                        and abs(float(raw) - live[f]) <= tolerance_ma:
                    results[f] = finish(f, data, True); pending.discard(f)
            if not pending or time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_s)

        if pending:
            # ONE bulk read for every timed-out filament, not one each -- doing
            # it per filament here would reinstate exactly the N-fold traffic
            # this method exists to remove, on the timeout path where the link
            # is already the likeliest suspect.
            final = self.read_filament_current_cached(sorted(pending))
            for f in sorted(pending):
                results[f] = finish(f, final.get(f) or {}, False,
                                    error=f"did not reach {live[f]} mA within "
                                          f"{timeout_s} s")
        return {"ok": all(r.get("ok") for r in results.values()),
                "results": results, "pending": sorted(pending),
                "elapsed_s": round(time.monotonic() - start, 3), "polls": polls}

    def stop_one(self, filament: int,
                verify: bool = False,      # confirm current drops to ~0 mA
                                            # afterward (real feedback, see docstring)
                timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STOP a single filament. Returns {"ok": False, "dead": True, ...}
        if the filament is dead — does not raise.

        verify=True: poll the measured current down to ~0 mA afterward and
        merge that feedback under result["heating"] — confirms the filament
        actually stopped heating, not just that the command was accepted.
        """
        r = self._state_one(filament, STOP, 0, "stop_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, 0, tolerance_ma=50,
                                                       timeout_s=timeout_s)}
        return r

    def sleep_one(self, filament: int,
                 verify: bool = False,      # confirm current drops to ~0 mA
                                             # afterward (real feedback, see docstring)
                 timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """SLEEP a single filament. A dead filament is STOPped instead (SLEEP
        would power its rail -- see sleep_all); the result then carries
        "dead_stopped": True and the state actually sent. Does not raise.

        verify=True: same real-current feedback as stop_one(verify=True).
        """
        r = self._state_one(filament, SLEEP, 0, "sleep_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, 0, tolerance_ma=50,
                                                       timeout_s=timeout_s)}
        return r

    def standby_one(self, filament: int,
                    verify: bool = False,      # confirm the board reports STANDBY
                                                # and report what it actually draws
                    timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """STANDBY a single filament. Returns {"ok": False, "dead": True, ...}
        if the filament is dead — does not raise.

        verify=True confirms the board reports STANDBY and reports the current
        it is actually drawing, under result["standby"].

        It does NOT check the current against a target, because STANDBY has no
        current target: it holds the firmware's 0.8 V floor, and what flows is
        whatever the filament's resistance allows. Measured on a cold filament
        here: 2.1 A of inrush, decaying to ~885 mA steady by ~5 s at 0.78 V.
        This used to verify against a target of 0 mA and report "reached, 0.0 mA"
        while about 2 A was flowing — true only in the sense that it confirmed
        the STATE, and actively misleading about the current. Read
        result["standby"]["current_mA"], and wait ~5 s before calling a STANDBY
        current abnormal.
        """
        r = self._state_one(filament, STANDBY, 0, "standby_one")
        if verify and not r.get("dead"):
            st = self.read_board_status(filament)
            live = (self.read_filament_vi_live(filament).get(int(filament)) or {})
            in_standby = st.get("ok") and st.get("state") == STANDBY
            r = {**r, "standby": {
                "ok": bool(in_standby),
                "state": st.get("state"),
                "current_mA": live.get("current_mA"),
                "bus_mV": live.get("bus_mV"),
                "note": ("STANDBY has no current target — this is what it draws, "
                         "not a pass/fail. Inrush decays for ~5 s."),
            }}
        return r

    def idle_one(self, filament: int,
                current_ma: float,             # target IDLE (warm-pool) hold
                                                # current in mA -- typically
                                                # well below the ACTIVE firing
                                                # current, e.g. 1500 vs 2900
                verify: bool = False,          # poll real measured current
                                                # after commanding -- see below
                tolerance_ma: float = 150.0,   # only used if verify=True --
                                                # passed straight to
                                                # wait_for_current()
                timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """IDLE a single filament at `current_ma` mA. Returns
        {"ok": False, "dead": True, ...} if the filament is dead — does
        not raise.

        verify=True: after commanding, poll the REAL measured current until
        it settles within `tolerance_ma` of `current_ma` (or `timeout_s`
        elapses) and merge that feedback under result["heating"] — so you
        know the filament actually reached the current you asked for, not
        just that the command was accepted:
            {"ok": bool, "measured_ma": float, "elapsed_s": float,
             "present": bool, "cc_mode": int}
        """
        r = self._state_one(filament, IDLE, int(current_ma), "idle_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, current_ma,
                                                       tolerance_ma, timeout_s)}
        return r

    def active_one(self, filament: int,
                   current_ma: float,             # target ACTIVE (firing) current
                                                   # in mA -- the real operating
                                                   # current, e.g. 2900
                                                   # REQUIRES the filament to be
                                                   # at IDLE already -- see below
                   verify: bool = False,          # poll real measured current
                                                   # after commanding -- see below
                   tolerance_ma: float = 150.0,   # only used if verify=True --
                                                   # passed straight to
                                                   # wait_for_current()
                   timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """Promote a single filament to ACTIVE at `current_ma` mA.

        The filament MUST already be at IDLE. Going straight to ACTIVE is not
        allowed: full firing current into a cold filament damages it, and a
        filament that fails inside the vacuum cannot be repaired. Measured on a
        simulated load, ACTIVE from cold collapsed the output to 0 V for ~10 s
        before the firmware revived it — that is the mechanism, and it is not
        something to confirm on a real filament.

        Walk the ladder instead, and let IDLE SETTLE before promoting (its
        voltage keeps climbing for ~15 s from cold; ACTIVE from a settled IDLE
        takes ~3 s, from an unsettled one it starts far lower and takes longer):

            ct.sleep_one(f); ct.standby_one(f)
            ct.idle_one(f, 1500, verify=True, timeout_s=30)
            ...                                  # let it settle
            ct.active_one(f, 2900, verify=True)

        Returns {"ok": False, "ladder_blocked": True, "error": ...} if the
        filament is not at IDLE, or if this backend does not KNOW its state
        (after a reconnect — unknown refuses rather than allows). Returns
        {"ok": False, "dead": True, ...} if the filament is dead. Never raises.

        verify=True: same real-current feedback as idle_one(verify=True),
        merged under result["heating"].
        """
        # The backend enforces the ladder; this mirrors it so the reason is
        # clear without a round trip, and so a script gets the same answer
        # whether or not the backend is reachable. ACTIVE may only be entered
        # from IDLE: going straight to firing current damages the filament, and
        # in vacuum that damage is unrepairable.
        # Existence BEFORE the ladder. Otherwise a filament index that has no
        # board at all comes back "power state unknown to this backend — run the
        # ladder STOP→SLEEP→STANDBY→IDLE first", which is true but useless: the
        # ladder will never produce a state for an index that does not exist,
        # so the reader is sent to walk a ladder that cannot help. Every other
        # single-filament call already reports this as "has no board".
        if self.filament_to_board(filament) is None:
            return {"ok": False, "filament": int(filament),
                    "error": f"filament {int(filament)} has no board (unassigned "
                             f"in the active-list mapping, or out of range)"}
        r = self._state_one(filament, ACTIVE, int(current_ma), "active_one")
        if r.get("ladder_blocked"):
            return r
        if verify and not r.get("dead"):
            r = {**r, "heating": self.wait_for_current(filament, current_ma,
                                                       tolerance_ma, timeout_s)}
        return r

    def voltage_one(self, filament: int,
                    millivolts: float,             # target voltage in mV --
                                                    # firmware clamps to
                                                    # 0.8-15 V (800-15000);
                                                    # out-of-range is
                                                    # rejected HERE before
                                                    # any frame is sent
                    verify: bool = False,          # confirm the board
                                                    # actually entered
                                                    # voltage-regulation mode
                                                    # afterward -- see below
                    timeout_s: float = 5.0) -> dict:  # only used if verify=True
        """Drive a single filament to manual VOLTAGE mode (PowerState 6) at
        `millivolts` mV -- a fixed voltage hold, NOT current-regulated
        (unlike IDLE/ACTIVE, which hold a commanded mA via the closed CC
        loop). Mostly for bench/calibration use (e.g. probing an arbitrary
        point on the load curve) rather than normal heating control.

        Returns {"ok": False, "dead": True, ...} if the filament is dead —
        does not raise. Returns {"ok": False, "error": "..."} without
        touching hardware if `millivolts` is outside the firmware's
        0.8-15 V clamp.

        verify=True: there's no current target to poll here (unlike
        idle_one/active_one's wait_for_current) — instead this polls the
        same no-I2C cached read until the board reports cc_mode==0
        (voltage), or `timeout_s` elapses, and merges that under
        result["heating"]:
            {"ok": bool, "cc_mode": int, "elapsed_s": float, "present": bool}
        cc_mode: 0=voltage (expected here), 1=current, 2/3=fault.
        """
        mv = int(millivolts)
        if mv < 800 or mv > 15000:
            return {"ok": False, "filament": int(filament),
                    "error": f"millivolts={mv} out of range 800-15000 (firmware clamps to 0.8-15 V)"}
        r = self._state_one(filament, VOLTAGE, mv, "voltage_one")
        if verify and not r.get("dead"):
            r = {**r, "heating": self._wait_for_voltage_mode(filament, timeout_s)}
        return r

    def _wait_for_voltage_mode(self, filament: int, timeout_s: float,
                               poll_interval_s: float = 0.2) -> dict:
        """Poll read_filament_currents() until cc_mode reports 0 (voltage),
        or timeout_s elapses. Same no-I2C cached read as wait_for_current();
        used by voltage_one(verify=True), which has no mA target to wait on."""
        start = time.monotonic()
        deadline = start + timeout_s
        data: dict = {}
        while True:
            data = self.read_filament_current_cached(filament).get(int(filament), {})
            cc_mode = int(data.get("cc_mode", -1))
            ok = cc_mode == 0
            if ok or time.monotonic() >= deadline:
                return {"ok": ok, "filament": int(filament), "cc_mode": cc_mode,
                        "elapsed_s": time.monotonic() - start,
                        "present": bool(data.get("present", False))}
            time.sleep(poll_interval_s)

    # ── Board heating status (single-board, direct I2C) ──────────────────────
    # Distinct from read_filament_currents() above: that reads the CACHED
    # CC-loop current (no I2C, cheap, bulk). This reads the RP2350's own
    # last-commanded PowerState + fault kind for ONE board directly
    # (CH_GET_POWER_STATE, single-board I2C round-trip) — use it when you
    # need to know the actual state/fault, not just the measured current.

    def read_board_status(self, filament: int) -> dict:
        """Single-board heating status: last-commanded PowerState and fault
        kind for one filament, read directly from the RP2350 (not cached).

        Returns {"ok", "filament", "controller", "channel", "mux_port",
        "state": 1-6, "state_name": "STOP".."VOLTAGE",
        "fault": 0/1/2, "fault_name": "none"/"open"/"OCP/SCP"}.
        Never raises — check "ok".
        """
        r = self._get(f"/api/filament-status?filament={self._fid_of(filament)}")
        if r.get("ok"):
            r["state_name"] = _STATE_NAMES.get(r.get("state"))
            r["fault_name"] = _FAULT_NAMES.get(r.get("fault"))
        r["filament"] = int(filament)   # always echo back YOUR (USER_INDEX) number
        return r

    # ── OCP protection ─────────────────────────────────────────────────────────
    # Two DISTINCT, unrelated OCP mechanisms in this firmware:
    #   1. Per-board TPS55289 IOUT_LIMIT — the "real" steady-state OCP trip
    #      current for one board's HV supply. Settable per filament; no
    #      native batch opcode, so set_ocp_threshold_all() loops per-board.
    #   2. A GLOBAL per-controller two-stage floor (CH_STARTUP_OCP): a
    #      STARTUP threshold that tolerates the cold-inrush transient on
    #      turn-on, and a STEADY threshold applied ~2 s later. This is ONE
    #      pair of values for the whole controller (RP2350) — NOT per-board.
    # NOTE: the actual timing delay between STARTUP and STEADY (~2 s), and
    # the TPS55289's internal deglitch-bit settings, are compiled-in firmware
    # constants with no UART command to change them — only the two CURRENT
    # thresholds are settable, not the delay itself. If you need the delay
    # tunable, that requires a firmware change (a new UART opcode) first.

    def get_ocp_threshold_one(self, filament: int) -> dict:
        """Read back ONE board's currently configured TPS55289 steady-state
        OCP trip current (mA) — a real hardware register read, not just
        "whatever you last called set_ocp_threshold_one with" (catches a
        threshold set by another client, or one that predates this
        process). set_ocp_threshold_one/_all are SET-only in firmware
        (CH_SET_TPS_OCP_THRESHOLD 0x28 has no matching "get" opcode); this
        decodes the raw TPS55289 register instead (single-board I2C read
        — a fine one-off check, don't loop this to poll many boards).

        Returns {"ok", "filament", "controller", "channel", "mux_port",
        "enabled": bool, "threshold_ma": int}. `enabled` False means OCP
        protection is currently OFF for this board (threshold_ma reads 0
        in that case, not a real 0 mA trip point). Returns
        {"ok": False, "error": ...} if the filament has no board mapping.
        """
        r = self._get(f"/api/ocp-threshold?filament={self._fid_of(filament)}")
        r["filament"] = int(filament)   # always echo back YOUR (USER_INDEX) number
        return r

    def set_ocp_threshold_one(self, filament: int, threshold_ma: int) -> dict:
        """Set ONE board's TPS55289 steady-state OCP trip current (mA).

        No dead-mask guard here — OCP is a protection setting, not a
        heating/HV action, so it's not gated the same way; call this even
        on a filament you've marked dead if you specifically want to lower
        its trip point. Returns {"ok": False, "error": ...} if the filament
        has no board mapping.
        """
        # Range-check BEFORE the write, so an out-of-range value is reported as
        # what it is. The batch path can only say "not applied", which it then
        # explains as a mapping/connection problem -- blaming the bench for a
        # bad argument, and sending the reader to check a link that is fine.
        if not (0 <= int(threshold_ma) <= self._U16_MAX):
            return {"ok": False, "filament": int(filament),
                    "error": f"threshold_ma={threshold_ma} out of range "
                             f"0..{self._U16_MAX} (TPS55289 IOUT_LIMIT is a "
                             f"16-bit field); nothing was written"}
        r = self.set_ocp_threshold_all(filaments=[filament], threshold_ma=threshold_ma)
        # Confirm POSITIVELY that this filament was applied, rather than merely
        # checking it isn't in `failed`. A filament that was silently skipped --
        # unslotted, or on a controller that isn't connected -- appears in NEITHER
        # list, so the old absence-of-failure test returned ok:True for a
        # protection threshold that was never written.
        if int(filament) not in [int(x) for x in (r.get("applied") or [])]:
            reason = ("filament is dead-masked" if self._is_dead(filament)
                      else "no board mapping, or its controller is not connected")
            r = {**r, "ok": False,
                 "error": r.get("error") or
                          f"OCP threshold NOT written for filament {int(filament)} ({reason})"}
        return r

    def set_ocp_threshold_all(self,
                              filaments=None,      # None = every populated
                                                    # board. NOT dead-filtered --
                                                    # OCP is protection, see
                                                    # set_ocp_threshold_one
                              threshold_ma: int = 0) -> dict:  # per-board OCP
                                                                # trip current
        """Set the TPS55289 steady-state OCP trip current (mA) for a BATCH
        of filaments (all populated boards, or `filaments`). No native batch
        opcode exists for this — the backend loops one frame per board.

        Returns {"ok", "results": {controller: {...}}, "applied": [...], "failed": [...]}.
        """
        body: dict = {"threshold_ma": int(threshold_ma)}
        if filaments is not None:
            body["filaments"] = self._fids_of(filaments)
        return self._reindex_response(self._post("/api/ocp-threshold", body, timeout=30.0),
                                    keys=("applied", "failed", "excluded", "touched",
                                            "not_this_controller", "unslotted",
                                            "mismatched", "unstable"))

    # ── rig-wide settings ─────────────────────────────────────────────────────
    # These are properties of the RIG, not of one board: a shared schedule runs
    # on both controllers and has to behave the same on both halves. They used
    # to default to controller=1, so a script that "configured the rig"
    # configured half of it. controller=None (the default) now means every
    # connected controller; an explicit number still addresses one board.
    #
    # The configured values are hoisted to the top level ONLY when every board
    # agrees. When they differ the top level carries no value at all and ok is
    # False: one board's setting standing in for the rig's is exactly the
    # silent half-configuration this replaced. The per-board detail is always
    # under "controllers".

    def _connected_controllers(self) -> list[int]:
        try:
            st = self.status()
        except Exception:
            return []
        return sorted(int(c) for c, row in (st.get("controllers") or {}).items()
                      if row.get("connected"))

    def _rig_wide(self, fn, controller, same: tuple, union: tuple = (),
                  carry: tuple = ()) -> dict:
        if controller is not None:
            return fn(int(controller))
        ctrls = self._connected_controllers()
        if not ctrls:
            return {"ok": False, "error": "no controller connected", "controllers": {}}
        per = {str(c): dict(fn(c)) for c in ctrls}
        out: dict = {"controllers": per}
        bad = {c: (r.get("error") or "failed") for c, r in per.items() if not r.get("ok")}
        if bad:
            out.update(ok=False, error=f"failed on controller(s) {sorted(bad)}: {bad}")
            return out
        differ = {k: {c: r.get(k) for c, r in per.items()} for k in same
                  if len({repr(r.get(k)) for r in per.values()}) > 1}
        if differ:
            out.update(ok=False, error=f"the controllers disagree: {differ} — a "
                                       f"schedule running on both would behave "
                                       f"differently on each half")
            return out
        first = next(iter(per.values()))
        for k in same + carry:
            if k in first:
                out[k] = first[k]
        for k in union:
            out[k] = sorted({x for r in per.values() for x in (r.get(k) or [])})
        out["ok"] = True
        return out

    def get_fault_policy(self, controller: int | None = None) -> dict:
        """Fault policy on every connected controller (or one, if named).

        The two policies must match across boards; `faultedFilaments` is the
        UNION over boards (each board only knows its own). See
        _get_fault_policy_one for the single-board shape."""
        return self._rig_wide(self._get_fault_policy_one, controller,
                              same=("board", "mismatch"),
                              union=("faultedFilaments",))

    def set_fault_policy(self, controller: int | None = None,
                         board: int | None = None,
                         mismatch: int | None = None) -> dict:
        """Set the fault policy on every connected controller (or one)."""
        return self._rig_wide(
            lambda c: self._set_fault_policy_one(c, board=board, mismatch=mismatch),
            controller, same=("board", "mismatch"), union=("faultedFilaments",))

    def get_slew_rates(self, controller: int | None = None) -> dict:
        """CC-loop slew rates on every connected controller (or one)."""
        return self._rig_wide(self._get_slew_rates_one, controller,
                              same=("below_mV_per_s", "above_mV_per_s", "warm_mV_per_s"))

    def set_slew_rates(self, below_mV_per_s: int, above_mV_per_s: int,
                       warm_mV_per_s: int, controller: int | None = None) -> dict:
        """Set CC-loop slew rates on every connected controller (or one).
        Values below the 400 mV/s floor are raised to it, as before; see
        _set_slew_rates_one."""
        return self._rig_wide(
            lambda c: self._set_slew_rates_one(below_mV_per_s, above_mV_per_s,
                                               warm_mV_per_s, controller=c),
            controller, same=("below_mV_per_s", "above_mV_per_s", "warm_mV_per_s"),
            carry=("clamped", "floored_to_min"))

    def set_hv_shift_hz(self, hz: int, controller: int | None = None) -> dict:
        """Set the 165 read-back bit-bang clock on every connected controller
        (or one). `actualHz` is hoisted only if every board landed on the same
        frequency."""
        return self._rig_wide(lambda c: self._set_hv_shift_hz_one(hz, controller=c),
                              controller, same=("actualHz",))

    def get_ocp_startup(self, controller: int | None = None) -> dict:
        """OCP thresholds on every connected controller (or one)."""
        return self._rig_wide(self._get_ocp_startup_one, controller,
                              same=("startup_ma", "steady_ma"))

    def _get_ocp_startup_one(self, controller: int = 1) -> dict:
        """Read the global per-controller two-stage OCP floor.

        Returns {"ok", "controller", "startup_ma", "steady_ma"}.
        """
        return self._get(f"/api/ocp-startup?controller={int(controller)}")

    def set_ocp_startup(self,
                        startup_ma: int,               # trip current (mA) that
                                                        # tolerates the cold-
                                                        # inrush transient right
                                                        # at turn-on
                        steady_ma: int | None = None,   # trip current (mA)
                                                         # applied ~2s later, once
                                                         # inrush has settled;
                                                         # None = leave unchanged
                        controller: int | None = None) -> dict:  # None = every
                                                         # connected controller;
                                                         # N = one. A WHOLE-
                                                         # CONTROLLER setting,
                                                         # not per-board
        """Set the two-stage OCP floor on every connected controller (or one).
        Per controller, NOT per board — see the note above. `steady_ma` is
        optional; omit to leave the steady threshold unchanged and only update
        the startup one.

        Rig-wide by default, like get_ocp_startup: it defaulted to controller 1
        while the read covered both, so "set the rig's OCP" set half of it and
        the read then reported the disagreement.

        Returns {"ok", "startup_ma", "steady_ma", "controllers": {c: ...}} —
        the values now in effect, read back from each response and hoisted only
        if every board agrees. With `controller=N`: {"ok", "controller",
        "startup_ma", "steady_ma"}.
        """
        return self._rig_wide(
            lambda c: self._set_ocp_startup_one(startup_ma, steady_ma, controller=c),
            controller, same=("startup_ma", "steady_ma"))

    def _set_ocp_startup_one(self, startup_ma: int, steady_ma: int | None = None,
                             controller: int = 1) -> dict:
        body: dict = {"controller": int(controller), "startup_ma": int(startup_ma)}
        if steady_ma is not None:
            body["steady_ma"] = int(steady_ma)
        return self._post("/api/ocp-startup", body)

    # ── Filament heating current (CC loop, per-filament) ──────────────────────
    # Distinct from read_emission_i() above: that reads the shared emission
    # BEAM current off the ADS1115. These read each filament's own CATHODE
    # HEATING current from the CC loop (INA219, cached — no I2C, safe to poll
    # even mid-run) — use them to confirm idle_one()/active_one() actually
    # landed at the current you commanded.

    # ══ FILAMENT READS — WHICH ONE DO I WANT? ════════════════════════════════
    #
    #   I want to...                          | use
    #   --------------------------------------+-------------------------------
    #   check a filament reached its commanded | read_filament_current_cached()
    #   current, or watch currents WHILE a     |   (cheap, no I2C, run-safe)
    #   schedule is firing                     |
    #   --------------------------------------+-------------------------------
    #   know a filament's VOLTAGE, or get a    | read_filament_vi_live()
    #   matched V+I pair (e.g. to compute      |   (real I2C read; do NOT call
    #   resistance)                            |    while a schedule fires)
    #   --------------------------------------+-------------------------------
    #   ...just one filament, both values      | read_filament_vi_live(f)[f]
    #   --------------------------------------+-------------------------------
    #   ...just one filament, one number       | read_filament_current(f) /
    #                                          | read_filament_voltage(f)
    #
    # THE TRAP THIS TABLE EXISTS TO PREVENT: the cached read has NO voltage --
    # not "0 V", none at all, because the firmware command behind it carries no
    # voltage field. And the two single-value helpers read DIFFERENT sources, so
    # pairing them gives you V and I sampled by different commands at different
    # instants (measured 92 mA apart on a warming filament). For a pair, always
    # take BOTH from one read_filament_vi_live() entry.
    #
    # ── One shape, one failure convention ────────────────────────────────────
    # Every reader below takes `int | list | None` (None = all), returns
    # {filament: entry} with the SAME keys regardless of source, and encodes
    # "no reading" as None -- never as 0. A value this layer did not get from
    # hardware is absent, not zero; that rule is the one every bug in this file
    # has come down to.

    _ENTRY_KEYS = ("index", "present", "bus_mV", "current_mA", "target_mA",
                   "cc_mode", "source", "valid", "unavailable", "cached")

    @staticmethod
    def _want_filaments(filaments):
        """`int | list | None` -> `list[int] | None`. Accepting a bare int used
        to work on one reader and raise TypeError on its sibling."""
        if filaments is None:
            return None
        if isinstance(filaments, (int, float)) and not isinstance(filaments, bool):
            return [int(filaments)]
        return [int(f) for f in filaments]

    @staticmethod
    def _entry(raw: dict, user_index: int, source: str) -> dict:
        """Normalise one backend entry to the common shape.

        Fields the source cannot supply are None, not 0 -- the live read has no
        target_mA/cc_mode, the cached read has no bus_mV (its firmware command
        carries no voltage at all). `valid` says whether a usable measurement
        came back; a board that isn't present never reports numbers, because the
        INA sweep reports a board it cannot find as a tidy 0 mA."""
        present = bool(raw.get("present"))
        # Cached: current_mA is already None when stale/unavailable. Live: it is
        # 0 for an absent board, which is exactly the fabricated value this
        # normalisation exists to remove.
        mA = raw.get("current_mA")
        mV = raw.get("bus_mV")
        if not present:
            mA = mV = None
        valid = mA is not None or mV is not None
        return {"index": user_index, "present": present,
                "bus_mV": float(mV) if mV is not None else None,
                "current_mA": float(mA) if mA is not None else None,
                "target_mA": (float(raw["target_mA"])
                              if raw.get("target_mA") is not None else None),
                "cc_mode": raw.get("cc_mode"),
                # The CC loop's OWN verdict on whether it got there:
                # "ramping" | "settled" | "capped" | None (not trustworthy).
                # Only the 0x3A cached source carries it.
                "arrival": raw.get("arrival"),
                "cc_mode_raw": raw.get("cc_mode_raw"),   # undecoded byte, for diagnosis
                "source": source, "valid": valid,
                # Always present on BOTH sources, so a caller never has to know
                # which one answered to know which keys exist. `cached` is not
                # redundant with source=="cached": the LIVE read falls back to
                # cached data while a schedule is firing (the I2C sweep would
                # stall pulses), so source=="live" with cached=True means "you
                # asked for a live read and did not get one".
                "unavailable": bool(raw.get("unavailable", not present)),
                "cached": bool(raw.get("cached", source == "cached"))}

    def read_filament_current_cached(self, filaments=None) -> dict:
        """USE THIS TO: confirm filaments reached their commanded current, and
        to watch heating current while a schedule is firing.

        CURRENT ONLY, from the CC loop's cache. SAFE while a schedule fires.

        No voltage: the firmware command behind this returns currents and
        nothing else, so `bus_mV` is always None here — use read_filament_vi_live()
        if you need a voltage. No I2C either, which is the point: this is the
        read you can poll at speed, and the only one that is safe to call while
        a schedule is firing (the live read does a mux select and would stall
        pulses).

        Read ONE filament or MANY with the same call.

        filaments:
            None          -> every populated filament (bulk/paged sweep)
            5             -> just filament 5   (SINGLE-board command)
            [5]           -> same as 5         (SINGLE-board command)
            [0, 1, 2]     -> those three       (bulk sweep, filtered)

        Single and bulk are DIFFERENT firmware commands, not the same read
        filtered two ways: one filament goes out as a single small frame to
        only that filament's controller (0x3A FLAG_SINGLE), which is what
        makes a per-filament poll like wait_for_current() cheap. Asking for
        several always uses the paged bulk sweep — looping the single read
        over many boards would flood the shared bridge link. The returned
        shape is identical either way, so you never branch on which ran.

        Not auto-filtered by the dead mask — this is a read, and you may
        still want to see a dead filament's last-known current.

        Returns {filament_index: {"current_mA", "target_mA", "present",
        "cc_mode"}}. cc_mode: 0=voltage, 1=current (Idle/Active), 2/3=fault.
        current_mA is None when the board's cached reading isn't a live
        measurement — guard with `is not None`, don't treat it as 0 mA.
        Returns {} on failure (never raises).

        NO VOLTAGE HERE. The underlying firmware command returns currents and
        nothing else — there is no bus-voltage field in its response — so
        `bus_mV` comes back None rather than a made-up number. Use
        read_filament_voltages() for a voltage; the live INA219 read is the only
        source that has one.
        """
        want = self._want_filaments(filaments)

        if want is not None and len(want) == 1:
            r = self._get(f"/api/filament-currents?filament={self._fid_of(want[0])}")
        else:
            r = self._get("/api/filament-currents")
        # The backend replies keyed by FID (it has no
        # concept of the client-side swap) -- re-key to USER_INDEX so the
        # result always matches YOUR numbering, then filter on that. Also
        # fix up the "index" field INSIDE each entry (a raw copy of the key,
        # left as FID by the backend) so it agrees with the outer key.
        raw = {int(k): v for k, v in (r.get("filaments") or {}).items()}
        out = {}
        for k, v in raw.items():
            user_index = self._user_index_of(k)
            out[user_index] = self._entry(v, user_index, "cached") if isinstance(v, dict) else v
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def read_filament_current(self, filament: int) -> float | None:
        """USE THIS TO: read one filament's heating current as a plain number,
        when you don't need to know WHY a read came back empty.

        Measured heating current (mA) for ONE filament. Returns **None** (not
        0.0) if the filament isn't present/regulated by the CC loop, OR if the
        read itself failed — arithmetic on the result then raises loudly
        instead of silently continuing with a fabricated zero. This still
        can't distinguish those two cases; use
        read_filament_current_cached() directly if you need to tell them apart.

        Reads the CC-loop CACHE. If you also want the voltage, do NOT pair this
        with read_filament_voltage() -- that one reads live INA219, so the two
        come from different commands at different instants. Use
        read_filament_vi_live(filament) for a matched pair -- it takes both
        from one conversion."""
        raw = self.read_filament_current_cached(filament).get(int(filament), {}).get("current_mA")
        return float(raw) if raw is not None else None

    def read_filament_vi_live(self, filaments=None) -> dict:
        """USE THIS TO: measure board voltage, or get matched V+I pairs (e.g.
        to compute resistance). NOT for polling during a run -- it does real
        I2C and would stall pulses; use read_filament_current_cached() there.

        VOLTAGE AND CURRENT, live from the INA219. NOT safe mid-run.

        This is the only source of a board voltage — the cached read has none.
        It does a real I2C mux sweep, so the backend refuses it while a schedule
        is firing (it would stall pulses) and answers from the cache instead;
        those entries come back "cached": True with no usable voltage. For a
        current you can poll safely at any time, use read_filament_current_cached().

        Bulk read of every populated filament's measured board voltage
        (mV) AND current (mA) together. Unlike read_filament_currents()
        (CC-loop CACHED currents, no I2C, safe mid-run), this is a real
        INA219 I2C sweep — the backend SKIPS it automatically while a
        schedule is firing (I2C would stall pulses) and falls back to the
        no-I2C cached current for that window: during a run, entries come
        back with "bus_mV": 0 and "cached": True — voltage genuinely isn't
        available then; call again after the run completes for a real
        reading.

        filaments: optional list to filter the result to just these indices.

        Returns {filament_index: {"bus_mV", "current_mA", "present",
        "cached"}}. Returns {} on failure (never raises).

        DO NOT POLL THIS TO WATCH A RAMP. It is a LIVE INA219 read over the same
        I2C the CC loop uses to rewrite its setpoint, so sampling it slows the
        thing being sampled. Measured on one IDLE->ACTIVE transition: 3.61 s
        while polling this at 4 Hz, 3.28 s polling the zero-I2C cache, and
        3.0 s not polling at all -- a 20% observer effect that is easy to
        mistake for the loop being slow. Use read_filament_current_cached()
        (0x3A, costs the RP2350 no I2C), or command, sleep, and read once.
        """
        # ?live=1 is REQUIRED for a voltage: /api/telemetry defaults to the
        # no-I2C cached read, which carries no bus_mV at all (every entry comes
        # back bus_mV=0, cached=True). Without this flag every filament here
        # reads 0 mV and read_filament_voltage() returns None for all of them.
        want = self._want_filaments(filaments)
        r = self._get("/api/telemetry?live=1")
        raw = {int(row["index"]): row for row in (r.get("telemetry") or [])
              if isinstance(row, dict) and "index" in row}
        out = {}
        for k, v in raw.items():
            user_index = self._user_index_of(k)
            out[user_index] = self._entry(v, user_index, "live")
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def read_filament_voltage(self, filament: int) -> float | None:
        """USE THIS TO: read one filament's board voltage as a plain number.
        If you also want its current, use read_filament_vi_live(filament)
        instead — it takes both from one conversion.

        Measured board voltage (mV) for ONE filament. Returns None if
        the filament isn't present, or if voltage isn't available right
        now (mid-run — see read_filament_vi_live()) — distinct from 0.0,
        which is a real (if unusual) reading. Use read_filament_vi_live()
        directly if you need present/cached separated from a genuine 0 mV.

        If you also want the current, use read_filament_vi_live(filament) rather than pairing
        this with read_filament_current(): that one reads the CC-loop cache, so
        the two values would come from different commands at different
        instants."""
        data = self.read_filament_vi_live([filament]).get(int(filament), {})
        if not data.get("present") or data.get("cached"):
            return None
        return float(data.get("bus_mV", 0))

    def read_filament_currents(self, filaments=None) -> dict:
        """Deprecated alias for read_filament_current_cached().

        Same behaviour, clearer name. Cached CC-loop CURRENT ONLY (no voltage
        exists in that firmware response), no I2C, safe to poll mid-run."""
        return self.read_filament_current_cached(filaments)

    def read_filament_voltages(self, filaments=None) -> dict:
        """Deprecated alias for read_filament_vi_live().

        Same behaviour, clearer name. LIVE INA219 read of voltage AND current;
        does I2C, so it is NOT safe to poll while a schedule is firing."""
        return self.read_filament_vi_live(filaments)
