"""CTClient: emission vs heating, pedestal, temperature/work function.

One part of the client class, split out of one 9900-line file by section:
    emission vs heating current
    the emission pedestal
    temperature from resistance, work function from emission

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # editors only: at run time _client.py binds CTClient into this module
    from ._client import CTClient


class _EmissionMixin:
    # ── emission vs heating current ───────────────────────────────────────────
    # How much a filament emits depends on how hot it is, so the useful curve is
    # net emission current against HEATING current. The x-axis has to be what
    # the filament was ACTUALLY drawing when the pulse fired, not what it was
    # commanded: the CC loop settles near, not at, its target, and a pulse that
    # lands during the ramp sits at a current no host poll can recover
    # afterwards. The RP2350 snapshots it per pulse (heat_meas_mA in the pulse
    # log), which is the one number a host could never supply -- see
    # scan_report()'s "heating_at_pulse". This uses it as the x-axis and keeps
    # the commanded value only as a label.

    # ACTIVE below this is refused by the backend (it is the idle operating
    # current -- promoting to ACTIVE must not LOWER the current). Mirrored here
    # so a sweep whose whole range is under the floor says so before it heats
    # anything, rather than failing on the first point.
    _ACTIVE_FLOOR_MA = 1500

    # The IDLE ceiling, and the reason it needs mirroring MORE than the floor
    # does. The two bounds live in different places and behave differently:
    # the backend REFUSES an ACTIVE below the floor, but the RP2350 firmware
    # CLAMPS an over-ceiling IDLE silently (tps55289_board_constants.h
    # kIdleMaxMilliamps), so idle_one(f, 2500) used to come back ok, run at
    # 2000, and leave verify=True waiting for a current that was never going
    # to arrive -- with nothing at any layer saying it had been clamped.
    #
    # Not a dividing line: 1500 is also the firmware's kIdleCurrentMaDefault,
    # so 1500-2000 mA is legal for IDLE and for ACTIVE both.
    _IDLE_CEILING_MA = 2000

    #: end_state values emission_vs_heating() will leave a filament in. ACTIVE
    #: is deliberately absent: the whole point of an end state is that the
    #: filament is no longer at firing current when the call returns.
    _END_STATES = {"stop": STOP, "sleep": SLEEP, "standby": STANDBY, "idle": IDLE}

    def emission_vs_heating(
        self,
        filament: int,                     # USER_INDEX, as everywhere
        start_ma: int = 2500,              # first ACTIVE point -- where firing starts
        max_ma: int = 2800,                # last ACTIVE point; never exceeded
        step_ma: int = 100,                # spacing between points
        width_us: int = 1000,              # pulse width
        pulses_per_point: int = 3,         # shots averaged at each heating current
                                           # (3, not 2: the scatter of 2 shots
                                           # is not an estimate -- several
                                           # points came back sd = 0.000, which
                                           # then disables the significance cut
                                           # that uses it)
        idle_ma: int = 1500,               # the IDLE rung of the ladder (pre-heat)
        end_state: str = "stop",           # where to leave it: stop/sleep/standby/idle
        inter_pulse_ms: int = 400,
        settle_s: float = 0.3,             # extra dwell after the CC loop says settled
        idle_timeout_s: float = 40.0,
        active_timeout_s: float = 30.0,
        bg_gap_us: float | None = None,
        bg_window_us: float | None = None,
        controller: int | None = None,
        progress=None,                     # callable(point_dict) after each point
        save_as: str | None = None,        # write the curve to the backend's disk
        pedestal_ma=None,                  # None = compute from the rail (the
                                            # grid's own diode path);
                                            # "measure" = fire and measure it;
                                            # a float = use that; 0.0 = subtract
                                            # nothing
    ) -> dict:
        """PRECISE MEASUREMENT of one filament's emission against its heating current.

        One of a pair, and they are not interchangeable:

            emission_vs_heating()   THIS. Steps setpoints, measures V and I
                                    with the shots so every point has a
                                    resistance and therefore a temperature,
                                    and feeds fit_richardson(). Tens of
                                    seconds at firing current.
            emission_ramp()         quick verification -- one ramp, no
                                    settling, seconds. No temperature, and the
                                    curve is dynamic.

        Use this when the number has to stand up; use the other one to check
        that a filament is alive.

        Pre-heats through the ladder, then walks ACTIVE from `start_ma` up to
        `max_ma` in `step_ma` steps, firing `pulses_per_point` pulses at each
        step and measuring every one. Leaves the filament in `end_state`
        (default STOP) whatever happens -- including on an exception or Ctrl-C.

            r = ct.emission_vs_heating(8)                      # 2500..2800 mA
            for p in r["points"]:
                print(p["heat_mA"], "mA ->", p["net_ma"], "mA emission")

        WHY THE X-AXIS IS NOT `commanded_ma`. Each point reports three heating
        numbers and they are not interchangeable:

            commanded_ma   what ACTIVE was told to hold. A label, not a
                           measurement.
            settled_ma     what the CC loop reported after settling, from the
                           host's own verify. One poll, before the shots.
            heat_mA        the mean of the firmware's per-pulse snapshots --
                           the filament's current at the INSTANT each pulse
                           fired. This is the x-axis. On this bench a point
                           commanded 2600 mA fired at 2574 and 2566 mA.

        `heat_mA` is `None` when the firmware supplied no snapshot (see
        `heat_unavailable` for which reason), and such a point is kept with
        `usable: False` rather than falling back to the commanded value -- a
        curve whose x-axis silently mixes "measured" and "asked for" is worse
        than one with a gap in it.

        PULSES THAT FIRED COLD ARE DROPPED, NOT AVERAGED. A shot whose snapshot
        is more than 20% below the point's target landed before the filament
        got there; its emission is not comparable with the rest. Those pulses
        are recorded individually with `cold: True` and excluded from the
        point's mean, and the point says how many it lost.

        RECORDING. `save_as="emission_curve"` writes the result to the
        backend's own disk through save_calibration() -- a JSON with everything
        including the per-shot detail, and a flat CSV of the points, one row per
        heating current. The paths come back under `saved`. A save that fails
        does NOT fail the measurement (the numbers are already in the returned
        dict); it is reported under `saved` instead.

        HV MUST ALREADY BE ON. This does not touch the HV rails -- set them
        with set_emission_v()/set_focus_v()/enable_emission() first. It checks
        before heating anything and refuses if emission is off, because the
        alternative is a complete, plausible-looking curve of zeros.

        SAFETY. The ladder is walked in full (STOP->SLEEP->STANDBY->IDLE->
        ACTIVE): going straight to firing current damages a filament, and in
        vacuum that is unrepairable. ACTIVE is held only as long as the shots
        need, the sweep steps UPWARD so the filament is never taken above the
        point it has already reached, and `max_ma` is a hard ceiling -- a
        `start_ma`/`step_ma` combination that would overshoot it stops at the
        last point at or below instead. `end_state` cannot be ACTIVE.
        `active_s` in the result reports how long it actually spent at firing
        current.

        Returns
            {"ok":        every point produced at least one usable pulse,
             "filament":  int,
             "points":    [ ... one per heating current, see below ... ],
             "end_state": the state actually left behind,
             "active_s":  seconds spent at ACTIVE,
             "problems":  [str], empty when ok,
             "ref_mv":    the live reference used for the mA conversion}

        each point:
            {"commanded_ma", "settled_ma", "heat_mA", "heat_target_mA",
             "heat_unavailable":  reason string, or None,
             "net_ma":      mean net emission current (plateau - bg), or None,
             "net_ma_sd":   spread across the point's pulses (0.0 for one),
             "charge_mams": mean charge, or None -- net of the background
                            only, the diode path still in it,
             "emission_mams": mean charge with the diode path's
                            (pedestal_ma * on_us) removed, or None,
             "n_used", "n_fired", "n_cold",
             "usable":      bool,
             "pulses":      [ per-shot {heat_mA, net_ma, emission_ma,
                              charge_mams, emission_mams, on_us,
                              bg_ma, sigma_ma, cold} ]}
        """
        fil = int(filament)
        problems: list[str] = []

        # ---- refuse bad requests BEFORE heating anything ---------------------
        if end_state not in self._END_STATES:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"end_state {end_state!r} is not one of "
                f"{sorted(self._END_STATES)} — ACTIVE is deliberately not "
                f"offered: the filament must not be left at firing current"]}
        if self._is_dead(fil):
            return {**self._dead_result(fil), "points": [], "problems": [
                f"filament {fil} is marked dead"]}
        if self.filament_to_board(fil) is None:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"filament {fil} has no board (unassigned in the active-list "
                f"mapping, or out of range)"]}
        if step_ma <= 0:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"step_ma must be positive, got {step_ma}"]}
        if start_ma > max_ma:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"start_ma {start_ma} is above max_ma {max_ma} — there is no "
                f"point to measure"]}
        if start_ma < self._ACTIVE_FLOOR_MA:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"start_ma {start_ma} mA is below the {self._ACTIVE_FLOOR_MA} mA "
                f"ACTIVE floor — ACTIVE may not lower the current below idle"]}
        if idle_ma >= start_ma:
            problems.append(
                f"idle_ma {idle_ma} is not below start_ma {start_ma}; the "
                f"pre-heat rung should be cooler than the first firing point")

        # A curve of zeros is what an off rail produces, and it looks exactly
        # like a filament that does not emit. Check once, up front.
        hv = self.hv_status()
        if not hv.get("ok"):
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"could not read HV status: {hv.get('error')} — refusing to "
                f"sweep without knowing whether the emission rail is on"]}
        if not hv.get("emission_on"):
            return {"ok": False, "filament": fil, "points": [], "problems": [
                "the emission rail is OFF — every pulse would measure "
                "background only. Set it with set_emission_v()/"
                "enable_emission(True) before calling this"]}

        # Inclusive of max_ma, and never past it: a step that would overshoot
        # simply is not taken. The ceiling is the caller's stated limit for the
        # filament, not a rounding target.
        currents = list(range(int(start_ma), int(max_ma) + 1, int(step_ma)))

        # A fired pulse's net current carries an ohmic leakage pedestal that is
        # not emission (~2 mA at -200 V on this bench). Measured by default,
        # because leaving it in is a 20-130% error on the numbers this function
        # exists to produce, and it hides under a curve that still looks clean.
        pedestal_ma, ped = self._resolve_pedestal(pedestal_ma, fil, idle_ma,
                                                  controller)
        if pedestal_ma is None:
            return {"ok": False, "filament": fil, "points": [],
                    "end_state": end_state, "active_s": 0.0, "ref_mv": None,
                    "pedestal": ped, "problems": [
                        f"could not determine what to subtract "
                        f"({ped.get('source')}): "
                        + (ped.get("error") or
                           "; ".join(ped.get("warnings") or ["unknown"]))
                        + ". Pass pedestal_ma=0.0 to sweep without the "
                          "correction, knowing every point is then high by the "
                          "grid's own diode current"]}
        problems.extend(ped.get("warnings") or [])

        points: list[dict] = []
        ref_mv = None
        active_s = 0.0
        try:
            with self.energised(fil):
                self.sleep_all([fil])
                self.standby_all([fil])
                r = self.idle_one(fil, idle_ma, verify=True,
                                  timeout_s=idle_timeout_s)
                h = r.get("heating") or {}
                if not h.get("ok"):
                    problems.append(
                        f"pre-heat to IDLE {idle_ma} mA did not complete "
                        f"({h.get('measured_ma')} mA, arrival="
                        f"{h.get('arrival')}) — not promoting to ACTIVE")
                    return {"ok": False, "filament": fil, "points": [],
                            "problems": problems, "end_state": end_state,
                            "active_s": 0.0, "ref_mv": None}

                t_active0 = time.monotonic()
                for ma in currents:
                    pt = self._emission_point(
                        fil, ma, width_us=width_us,
                        pulses=pulses_per_point,
                        inter_pulse_ms=inter_pulse_ms,
                        settle_s=settle_s, timeout_s=active_timeout_s,
                        bg_gap_us=bg_gap_us, bg_window_us=bg_window_us,
                        controller=controller, pedestal_ma=float(pedestal_ma))
                    ref_mv = pt.pop("_ref_mv", ref_mv)
                    points.append(pt)
                    if callable(progress):
                        progress(pt)
                    if pt.get("_fatal"):
                        problems.append(pt["_fatal"])
                        break
                active_s = time.monotonic() - t_active0
        finally:
            # energised() has already STOPped on the way out. Re-command the
            # requested end state after it, so "leave it at IDLE" means IDLE and
            # not "STOP, then IDLE would have been nice" -- and so the STOP
            # still happens on the paths where this one fails.
            if end_state != "stop":
                self._state_one(fil, self._END_STATES[end_state],
                                idle_ma if end_state == "idle" else 0,
                                "emission_vs_heating end_state")

        for p in points:
            p.pop("_fatal", None)
        unusable = [p["commanded_ma"] for p in points if not p["usable"]]
        if unusable:
            problems.append(f"no usable pulse at {unusable} mA")
        if not points:
            problems.append("no points were measured")
        out = {"ok": not problems, "filament": fil, "points": points,
               "end_state": end_state, "active_s": round(active_s, 1),
               "problems": problems, "ref_mv": ref_mv,
               "pedestal_ma": float(pedestal_ma), "pedestal": ped}
        if save_as:
            out["saved"] = self.save_emission_curves(save_as, {fil: out}, {
                "start_ma": start_ma, "max_ma": max_ma, "step_ma": step_ma,
                "width_us": width_us, "pulses_per_point": pulses_per_point,
                "idle_ma": idle_ma, "end_state": end_state,
                "emission_v": self.read_emission_v(),
                "focus_v": self.read_focus_v(), "ref_mv": ref_mv})
        return out

    def save_emission_curves(self, name: str, results: dict,
                             params: dict | None = None) -> dict:
        """Write one or more emission_vs_heating() results to the backend's
        disk, as one JSON plus one flat CSV covering every filament.

        `results` is {filament: <an emission_vs_heating() result>}. Built to
        take a whole sweep's worth at once rather than one file per filament:
        the interesting comparison is between filaments, and that is a lot
        easier from one table.

        The CSV carries the POINTS only, one row per heating current; the
        per-shot detail would not fit a flat table and lives in the JSON's
        "pulses" section instead. Returns save_calibration()'s
        {"ok", "json", "csv", "filaments"}."""
        curves, shots, meta = {}, {}, {}
        for fil, r in results.items():
            # `pulses` is a LIST -- it cannot go in a CSV cell, so it is lifted
            # out here rather than stringified into one. The point rows keep
            # the counts (n_used/n_fired/n_cold), which is what a flat table
            # can actually carry.
            curves[str(int(fil))] = [{k: v for k, v in p.items() if k != "pulses"}
                                     for p in (r.get("points") or [])]
            shots[str(int(fil))] = [{"commanded_ma": p.get("commanded_ma"),
                                     **s} for p in (r.get("points") or [])
                                    for s in (p.get("pulses") or [])]
            # The pedestal block travels WITH the curve, sigma included. It
            # is part of the reduction, not a side note: without its sigma the
            # significance cut lands somewhere else and the saved run replays
            # to different numbers than the live one did. Measured: 12 points
            # and phi 2.87 eV offline against 10 points and 3.65 eV live.
            meta[str(int(fil))] = {"ok": r.get("ok"), "problems": r.get("problems"),
                                   "active_s": r.get("active_s"),
                                   "end_state": r.get("end_state"),
                                   "ref_mv": r.get("ref_mv"),
                                   "pedestal_ma": r.get("pedestal_ma"),
                                   "pedestal": r.get("pedestal")}
        return self.save_calibration(name, {"params": params or {},
                                            "curves": curves,
                                            "pulses": shots,
                                            "per_filament": meta})

    #: Flag a point whose total resistance moved more than this across its own
    #: shots. 2% is about 25 K at these temperatures -- small against the 300 K
    #: the curve spans, large enough to be worth seeing.
    _R_DRIFT_WARN = 0.02

    def _vi_pair(self, fil: int):
        """One matched INA219 V+I, as (mV, mA, ohm). None if it was not a live
        matched pair -- a fresh voltage over a stale current, or a cached entry
        with no voltage at all, would be a resistance that looks measured and
        is not."""
        vi = (self.read_filament_vi_live([fil]) or {}).get(fil) or {}
        if vi.get("cached") or not vi.get("bus_mV") or not vi.get("current_mA"):
            return None
        return (float(vi["bus_mV"]), float(vi["current_mA"]),
                float(vi["bus_mV"]) / float(vi["current_mA"]))

    def emission_ramp(
        self,
        filament: int,
        from_ma: int = 1500,           # where the ramp starts (already held)
        to_ma: int = 2800,             # where it is commanded to go
        num_pulses: int = 24,          # shots fired ACROSS the ramp
        inter_pulse_ms: int = 120,     # spacing -> the whole train is
                                       # num_pulses * this, in ms
        width_us: int = 1000,
        idle_ma: int = 1400,
        end_state: str = "stop",
        pedestal_ma=None,                  # see emission_vs_heating()
        bg_gap_us: float | None = None,
        bg_window_us: float | None = None,
        controller: int | None = None,
        save_as: str | None = None,
    ) -> dict:
        """QUICK VERIFICATION. The whole curve in one ramp, seconds not a minute.

        One of a pair, and they are not interchangeable:

            emission_vs_heating()   the PRECISE measurement. Steps setpoints,
                                    measures V and I with the shots, gives a
                                    temperature, feeds fit_richardson().
                                    Tens of seconds at firing current.
            emission_ramp()         THIS. Quick verification -- is this
                                    filament emitting, and roughly how much.
                                    No temperature, and the curve it returns
                                    is dynamic (see below). Seconds.

        Reach for this to check a filament, compare filaments, or confirm
        nothing has broken. Reach for the other one when the number has to
        stand up.

        Nothing waits. The schedule is armed first, the ACTIVE command is
        issued in the gap between arming and triggering, and the train then
        fires straight through the CC loop's ramp. Each shot lands at whatever
        heating current the filament happened to be passing through, and the
        firmware's per-pulse snapshot says which -- so the x-axis comes out of
        the log rather than out of a setpoint that was waited for.

            r = ct.emission_ramp(8, from_ma=1500, to_ma=2800)
            print(r)                               # ~3 s at ACTIVE

        WHY THIS IS AS VALID AS THE STEPPED SWEEP. Richardson is an
        instantaneous relation: a shot's emission and the current it fired at
        belong together whether or not anything had settled. Waiting was never
        what made the stepped version right -- measuring the pair TOGETHER was.
        And waiting does not even reach equilibrium: at a fixed 2400 mA the CC
        loop reports settled in 2.6 s while the filament keeps warming for
        tens of seconds (R_total +12.7% and emission +17% between 8 s and 33 s).
        Every "settled" point was a point on a transient too -- just a slower,
        more expensive one.

        WHAT IS LOST, and it is not nothing. No filament VOLTAGE, so no
        resistance and no temperature. A live INA219 read is I2C and the
        backend refuses it while a schedule is firing (it would stall pulses),
        and the cached CC read carries current only. So this gives emission
        against heating CURRENT -- the curve itself -- but not the Richardson
        reduction, which needs R. Points come back with `r_total_ohm: None`
        rather than a resistance borrowed from elsewhere, and fit_richardson()
        will decline them. Use emission_vs_heating() when temperature is the
        point; use this when the curve is.

        COVERAGE IS NOT CONTROLLED. Where the shots land depends on how fast
        the CC loop ramps, which is a property of the slew configuration and
        the filament, not of this call. The result reports what was actually
        covered (`span_ma`, `gap_max_ma`) instead of pretending to a grid: a
        ramp that finished early bunches every shot at the top, and that shows
        up as a large gap rather than as a curve with an invented middle.

        Returns the same shape emission_vs_heating() does -- one dict per
        distinct shot rather than per setpoint -- plus:
            {"ramp": {"from_ma", "to_ma", "commanded_at_s", "train_ms"},
             "span_ma":   lowest to highest heating current actually hit,
             "gap_max_ma": the largest hole between consecutive shots}
        """
        fil = int(filament)
        problems: list[str] = []
        # `problems` makes the run INVALID; `notes` describes what a ramp
        # inherently is. Thermal lag and shots bunching at the destination are
        # properties of measuring on a transient, not faults -- filing them as
        # problems made a perfectly good quick check report ok=False, which
        # trains a reader to ignore the field on the one function whose whole
        # job is to be run often.
        notes: list[str] = []
        if end_state not in self._END_STATES:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"end_state {end_state!r} is not one of {sorted(self._END_STATES)}"]}
        if self._is_dead(fil):
            return {**self._dead_result(fil), "points": [], "problems": [
                f"filament {fil} is marked dead"]}
        if to_ma <= from_ma:
            return {"ok": False, "filament": fil, "points": [], "problems": [
                f"to_ma {to_ma} is not above from_ma {from_ma} — a ramp needs "
                f"somewhere to go"]}
        hv = self.hv_status()
        if not hv.get("ok") or not hv.get("emission_on"):
            return {"ok": False, "filament": fil, "points": [], "problems": [
                "the emission rail is OFF — every pulse would measure "
                "background only"]}

        pedestal_ma, ped = self._resolve_pedestal(pedestal_ma, fil, idle_ma,
                                                  controller)
        if pedestal_ma is None:
            return {"ok": False, "filament": fil, "points": [],
                    "pedestal": ped, "problems": [
                        f"could not determine what to subtract "
                        f"({ped.get('source')}): "
                        + (ped.get("error") or
                           "; ".join(ped.get("warnings") or ["unknown"]))]}
        problems.extend(ped.get("warnings") or [])

        ramp = {"from_ma": int(from_ma), "to_ma": int(to_ma),
                "commanded_at_s": None,
                "train_ms": int(num_pulses) * int(inter_pulse_ms)}
        fired = {}
        try:
            with self.energised(fil):
                self.sleep_all([fil])
                self.standby_all([fil])
                r = self.idle_one(fil, from_ma, verify=True, timeout_s=40.0)
                h = r.get("heating") or {}
                if not h.get("ok"):
                    problems.append(
                        f"could not reach the ramp's starting current "
                        f"{from_ma} mA ({h.get('measured_ma')} mA, arrival="
                        f"{h.get('arrival')})")
                    return {"ok": False, "filament": fil, "points": [],
                            "problems": problems, "ramp": ramp,
                            "pedestal_ma": pedestal_ma, "pedestal": ped}

                t0 = time.monotonic()

                def start_ramp():
                    # Runs ARMED, one instant before the trigger. verify=False
                    # deliberately: waiting here would defeat the whole point,
                    # and the ramp is verified after the fact by the per-pulse
                    # snapshots, which are better evidence than a poll anyway.
                    ramp["commanded_at_s"] = round(time.monotonic() - t0, 3)
                    ramp["command"] = self.active_one(fil, to_ma, verify=False)

                fired = self.fire_single_pulse(
                    fil, num_pulses=num_pulses, width_us=width_us,
                    inter_pulse_ms=inter_pulse_ms, max_on_ms=40,
                    total_ms=max(10000, ramp["train_ms"] + 5000),
                    controller=controller, trigger="sim",
                    timeout_s=15.0 + ramp["train_ms"] / 1000.0,
                    verify=True, reuse=False, measure=True,
                    bg_gap_us=bg_gap_us, bg_window_us=bg_window_us,
                    on_armed=start_ramp)
                ramp["active_s"] = round(time.monotonic() - t0, 2)
        finally:
            if end_state != "stop":
                self._state_one(fil, self._END_STATES[end_state],
                                idle_ma if end_state == "idle" else 0,
                                "emission_ramp end_state")

        cmd = (ramp.get("command") or {})
        if not cmd.get("ok"):
            problems.append(
                f"the ACTIVE command itself did not land ("
                f"{self.describe(cmd)[:110]}) — the filament never ramped, so "
                f"every shot is at the starting current")
        events = fired.get("measured") or []
        if not events:
            problems.append(fired.get("error") or "the detector measured no pulse")
            return {"ok": False, "filament": fil, "points": [],
                    "problems": problems, "ramp": ramp,
                    "pedestal_ma": pedestal_ma, "pedestal": ped}

        log = [rec for rec in (self.shv_pulse_log(self._firing_controller(fil, controller)) or [])
               if rec.get("filament") == fil]
        if len(log) != len(events):
            # Without a 1:1 pairing every emission reading would go to the
            # wrong current, which on a RAMP is the whole measurement -- unlike
            # the stepped sweep, there is no setpoint to fall back on.
            problems.append(
                f"{len(log)} firmware snapshot(s) for {len(events)} measured "
                f"pulse(s) — they cannot be paired, and on a ramp there is no "
                f"setpoint to fall back on, so no point has a heating current")
            return {"ok": False, "filament": fil, "points": [],
                    "problems": problems, "ramp": ramp,
                    "pedestal_ma": pedestal_ma, "pedestal": ped}

        slope = (self.pulse_ma(1.0, fired.get("ref_mv") or 1200.0)
                 - self.pulse_ma(0.0, fired.get("ref_mv") or 1200.0))
        points = []
        for i, (e, rec) in enumerate(zip(events, log)):
            heat = rec.get("heat_meas_mA")
            net = e.get("plateau_net_ma")
            sigma4 = e.get("bg_sigma4")
            emis = None if net is None else round(net - pedestal_ma, 3)
            points.append({
                "shot": i, "seq": rec.get("seq"),
                "commanded_ma": None,          # there was no setpoint per shot
                "settled_ma": None,
                "heat_mA": heat,
                "heat_unavailable": rec.get("heat_meas_unavailable"),
                "heat_target_mA": rec.get("heat_target_mA"),
                # No live V during a run, so no resistance and no temperature.
                # None, not a value carried over from a neighbouring shot.
                "bus_mV": None, "vi_current_mA": None, "r_total_ohm": None,
                "r_drift_frac": None,
                "pedestal_ma": round(float(pedestal_ma), 3),
                "net_ma": net, "emission_ma": emis, "net_ma_sd": None,
                "charge_mams": e.get("integral_mams"),
                "emission_mams": self._emission_charge(
                    e.get("integral_mams"), pedestal_ma, e.get("on_us")),
                "on_us": e.get("on_us"),
                "bg_ma": e.get("bg_ma"),
                "sigma_ma": round((sigma4 / 4.0) * slope, 4) if sigma4 else None,
                "n_used": 1 if (emis is not None and not e.get("empty_envelope")) else 0,
                "n_fired": 1, "n_cold": 0,
                "usable": bool(emis is not None and heat is not None
                               and not e.get("empty_envelope")),
                "pulses": [], "note": None,
            })
        # THERMAL LAG. On a ramp the current arrives before the temperature
        # does, so emission at a given heating current is not a function of
        # that current alone -- it depends on how long the filament has been
        # there. This is measurable from the run itself and costs nothing:
        # wherever two shots fired at the SAME current at different times,
        # compare them. Measured on this bench, 20 shots over a 1500->2800 mA
        # ramp: the train outlasted the ramp, so 13 shots piled up at
        # 2757-2780 mA and emission went 10.03 -> 13.54 mA (+35%) across them
        # at a constant current.
        #
        # Reported rather than corrected. There is no correction: the honest
        # statement is that a ramp measures a DYNAMIC curve, and how far it
        # sits below the steady one depends on the slew rate.
        lag = None
        by_i = sorted((p for p in points if p["heat_mA"] is not None
                       and p["emission_ma"] is not None),
                      key=lambda p: p["shot"])
        for a in by_i:
            for b in by_i:
                if b["shot"] - a["shot"] < 3 or not a["heat_mA"]:
                    continue
                if abs(b["heat_mA"] - a["heat_mA"]) > 0.01 * a["heat_mA"]:
                    continue
                if a["emission_ma"] <= 0.2:      # noise, not a ratio
                    continue
                frac = (b["emission_ma"] - a["emission_ma"]) / a["emission_ma"]
                if lag is None or abs(frac) > abs(lag["change_frac"]):
                    lag = {"heat_mA": a["heat_mA"], "shots": [a["shot"], b["shot"]],
                           "emission_ma": [a["emission_ma"], b["emission_ma"]],
                           "change_frac": round(frac, 4),
                           "apart_ms": (b["shot"] - a["shot"]) * int(inter_pulse_ms)}
        if lag and abs(lag["change_frac"]) > 0.10:
            notes.append(
                f"thermal lag: two shots {lag['apart_ms']} ms apart at the same "
                f"{lag['heat_mA']} mA read {lag['emission_ma'][0]} and "
                f"{lag['emission_ma'][1]} mA ({lag['change_frac'] * 100:+.0f}%). "
                f"The current arrives before the temperature does, so this is a "
                f"DYNAMIC curve — at a given heating current it sits below the "
                f"settled one, by an amount that depends on the slew rate")

        points.sort(key=lambda p: (p["heat_mA"] is None, p["heat_mA"] or 0))
        heats = [p["heat_mA"] for p in points if p["heat_mA"] is not None]
        span = [min(heats), max(heats)] if heats else None
        gaps = [b - a for a, b in zip(heats, heats[1:])] if len(heats) > 1 else []
        gap_max = max(gaps) if gaps else None
        # The train and the ramp have to be the same LENGTH. Firing faster does
        # not add resolution once the ramp is over -- every extra shot lands at
        # the destination. Measured: 20 shots at 400 ms spanned 8 s against a
        # ~2.8 s ramp, so 7 covered it and 13 piled up at the top with a 222 mA
        # hole in the middle. The knob for resolution is the SLEW RATE, not the
        # pulse rate.
        if heats:
            top = max(heats)
            at_top = sum(1 for h in heats if h >= top - 0.01 * top)
            if at_top > max(3, len(heats) // 3):
                notes.append(
                    f"{at_top} of {len(heats)} shots landed within 1% of the "
                    f"top current — the {ramp['train_ms']} ms train outlasted "
                    f"the ramp, so they are a dwell series at the destination, "
                    f"not coverage. Shorten the train (fewer pulses, or a wider "
                    f"spacing with fewer of them) to match the ramp, and slow "
                    f"the slew rate if you want more points across it")
        if span and span[1] - span[0] < 0.5 * (to_ma - from_ma):
            notes.append(
                f"the shots only cover {span[0]}–{span[1]} mA of the "
                f"{from_ma}–{to_ma} mA that was asked for — the train ended "
                f"before the ramp did, or the ramp finished before the train "
                f"started. Adjust num_pulses × inter_pulse_ms against the slew "
                f"rate rather than reading the missing range as flat")
        out = {"ok": not problems, "filament": fil, "points": points,
               "end_state": end_state, "active_s": ramp.get("active_s"),
               "problems": problems, "notes": notes, "thermal_lag": lag,
               "ref_mv": fired.get("ref_mv"),
               "pedestal_ma": float(pedestal_ma), "pedestal": ped,
               "ramp": ramp, "span_ma": span, "gap_max_ma": gap_max,
               "temperature_available": False}
        if save_as:
            out["saved"] = self.save_emission_curves(save_as, {fil: out}, {
                "mode": "ramp", "from_ma": from_ma, "to_ma": to_ma,
                "num_pulses": num_pulses, "inter_pulse_ms": inter_pulse_ms,
                "width_us": width_us, "end_state": end_state,
                "emission_v": self.read_emission_v(),
                "focus_v": self.read_focus_v(), "ref_mv": fired.get("ref_mv")})
        return out

    def _firing_controller(self, fil: int, controller: int | None) -> int:
        """The controller that FIRED this filament -- where its pulse log is.

        The pulse log (and with it the per-pulse heating snapshot, which is the
        x-axis of every emission curve) lives on whichever controller fired the
        pulse, not on the master. This used to read `controller or 1`, i.e.
        always the master: fine while only the master's pulses could be
        measured, and wrong the moment the second controller's envelope is
        wired in -- its snapshots would be looked for on the wrong board,
        found missing, and every point dropped as unpairable.
        """
        if controller is not None:
            return int(controller)
        board = self.filament_to_board(fil)
        return int(board["controller"]) if board else 1

    def _emission_point(self, fil: int, ma: int, *, width_us: int, pulses: int,
                        inter_pulse_ms: int, settle_s: float, timeout_s: float,
                        bg_gap_us, bg_window_us, controller,
                        pedestal_ma: float = 0.0) -> dict:
        """One heating current: promote, settle, fire, pair, average."""
        pt = {"commanded_ma": int(ma), "settled_ma": None, "heat_mA": None,
              "heat_target_mA": None, "heat_unavailable": None,
              "bus_mV": None, "vi_current_mA": None, "r_total_ohm": None,
              "r_drift_frac": None,
              "net_ma": None, "net_ma_sd": None, "charge_mams": None,
              "emission_mams": None,
              "pedestal_ma": round(float(pedestal_ma), 3), "emission_ma": None,
              "n_used": 0, "n_fired": 0, "n_cold": 0, "usable": False,
              "pulses": [], "note": None}

        r = self.active_one(fil, ma, verify=True, timeout_s=timeout_s)
        if r.get("ladder_blocked") or r.get("dead"):
            # Fatal for the whole sweep: the ladder will not let the next,
            # HIGHER point through either.
            pt["note"] = self.describe(r)[:160]
            pt["_fatal"] = f"{ma} mA: {pt['note']}"
            return pt
        h = r.get("heating") or {}
        pt["settled_ma"] = h.get("measured_ma")
        if not h.get("ok"):
            # Not fatal: a filament that cannot hold 2800 mA may still have
            # held 2500, and those points are real. Record and move on.
            pt["note"] = (f"did not reach {ma} mA "
                          f"({h.get('measured_ma')} mA, arrival={h.get('arrival')})")
            return pt
        if settle_s > 0:
            time.sleep(settle_s)

        # Matched V+I, a real INA219 read, which the backend refuses mid-run
        # (it would stall pulses) -- so it has to go either side of the firing,
        # never during it. R_total = V/I is the filament PLUS its leads.
        #
        # TAKEN TWICE, BRACKETING THE SHOTS, because the filament is still
        # heating. The CC loop's "settled" is about CURRENT, not temperature:
        # it holds the current within ~2.6 s while the filament keeps warming
        # for tens of seconds. Measured at 2400 mA, from the moment the loop
        # reported settled: R_total 2.754 -> 3.105 ohm over 8 to 33 s (+12.7%)
        # and emission 1.24 -> 1.45 mA, still climbing at 35 s.
        #
        # That does NOT mean the sweep has to wait -- Richardson is an
        # instantaneous relation, so a (T, I) pair measured together is
        # self-consistent wherever it sits on the transient. What it means is
        # that R has to be measured WITH the shots rather than before them.
        # The bracket gives the mean, and r_drift_frac says how far it moved
        # while they were taken, so a point whose temperature ran away during
        # its own measurement is visible instead of averaged into the curve.
        vi_before = self._vi_pair(fil)

        since = self.pulse_cursor()
        fr = self.fire_single_pulse(
            fil, num_pulses=pulses, width_us=width_us,
            inter_pulse_ms=inter_pulse_ms, max_on_ms=40,
            total_ms=max(10000, pulses * (inter_pulse_ms + 1000)),
            controller=controller, trigger="sim",
            timeout_s=15.0 + pulses * inter_pulse_ms / 1000.0,
            verify=True, reuse=False, measure=True,
            bg_gap_us=bg_gap_us, bg_window_us=bg_window_us)
        vi_after = self._vi_pair(fil)
        pair = [v for v in (vi_before, vi_after) if v]
        if pair:
            pt["bus_mV"] = round(sum(v[0] for v in pair) / len(pair), 1)
            pt["vi_current_mA"] = round(sum(v[1] for v in pair) / len(pair), 1)
            pt["r_total_ohm"] = round(sum(v[2] for v in pair) / len(pair), 5)
            if len(pair) == 2 and vi_before[2]:
                pt["r_drift_frac"] = round((vi_after[2] - vi_before[2]) / vi_before[2], 5)
                if abs(pt["r_drift_frac"]) > self._R_DRIFT_WARN:
                    pt["note"] = ((pt["note"] + "; ") if pt.get("note") else "") + (
                        f"resistance moved {pt['r_drift_frac'] * 100:+.1f}% across "
                        f"this point's own shots — it is still heating, so its "
                        f"temperature is a mean over a moving target")

        events = fr.get("measured") or []
        pt["_ref_mv"] = fr.get("ref_mv")
        pt["n_fired"] = int(fr.get("fired") or 0)
        if not events:
            pt["note"] = fr.get("error") or "the detector measured no pulse"
            return pt

        # The RP2350's heating snapshots and the STM32's measurements are two
        # INDEPENDENT records of the same shots. Pair them by index only when
        # the counts agree; when they do not, the pairing is a guess and a
        # wrong pairing puts a real emission reading at the wrong heating
        # current -- the one error this whole function exists to avoid. Keep
        # the emission numbers, drop the per-pulse x, and say so.
        log = [rec for rec in (self.shv_pulse_log(self._firing_controller(fil, controller)) or [])
               if rec.get("filament") == fil]
        paired = len(log) == len(events)
        if not paired:
            pt["note"] = (f"{len(log)} firmware snapshot(s) for {len(events)} "
                          f"measured pulse(s) — cannot pair them, so this "
                          f"point has no measured heating current")

        slope = (self.pulse_ma(1.0, fr.get("ref_mv") or 1200.0)
                 - self.pulse_ma(0.0, fr.get("ref_mv") or 1200.0))
        nets, charges, emis_charges, heats = [], [], [], []
        for i, e in enumerate(events):
            rec = log[i] if paired else {}
            heat = rec.get("heat_meas_mA")
            target = rec.get("heat_target_mA")
            if target:
                pt["heat_target_mA"] = target
            if pt["heat_unavailable"] is None:
                pt["heat_unavailable"] = rec.get("heat_meas_unavailable")
            # 20% short of the setpoint = the shot landed during the ramp.
            cold = bool(heat is not None and target and heat < 0.8 * target)
            sigma4 = e.get("bg_sigma4")
            shot = {"heat_mA": heat,
                    "net_ma": e.get("plateau_net_ma"),
                    "charge_mams": e.get("integral_mams"),
                    "on_us": e.get("on_us"),
                    "bg_ma": e.get("bg_ma"),
                    "sigma_ma": round((sigma4 / 4.0) * slope, 4) if sigma4 else None,
                    "cold": cold,
                    "empty_envelope": bool(e.get("empty_envelope"))}
            if shot["net_ma"] is not None:
                shot["emission_ma"] = round(shot["net_ma"] - pedestal_ma, 3)
            emis_q = self._emission_charge(shot["charge_mams"], pedestal_ma,
                                           shot["on_us"])
            if emis_q is not None:
                shot["emission_mams"] = emis_q
            pt["pulses"].append(shot)
            if cold:
                pt["n_cold"] += 1
                continue
            if shot["empty_envelope"] or shot["net_ma"] is None:
                continue
            nets.append(shot["net_ma"])
            if shot["charge_mams"] is not None:
                charges.append(shot["charge_mams"])
            if shot.get("emission_mams") is not None:
                emis_charges.append(shot["emission_mams"])
            if heat is not None:
                heats.append(heat)

        pt["n_used"] = len(nets)
        if nets:
            pt["net_ma"] = round(sum(nets) / len(nets), 3)
            # The emission proper. Can legitimately be NEGATIVE at the cold end
            # (the pedestal is measured with its own noise, so a point with no
            # emission scatters either side of zero) -- NOT clamped at 0, for
            # the same reason `integral` is signed: flooring it would bias the
            # bottom of the curve upward and bend the Arrhenius slope.
            pt["emission_ma"] = round(pt["net_ma"] - pedestal_ma, 3)
            pt["net_ma_sd"] = round(
                math.sqrt(sum((x - sum(nets) / len(nets)) ** 2
                              for x in nets) / len(nets)), 3)
            pt["usable"] = True
        if charges:
            pt["charge_mams"] = round(sum(charges) / len(charges), 4)
        if emis_charges:
            pt["emission_mams"] = round(sum(emis_charges) / len(emis_charges), 4)
        if heats:
            pt["heat_mA"] = round(sum(heats) / len(heats), 1)
        elif pt["usable"] and pt["heat_unavailable"] is None and paired:
            pt["heat_unavailable"] = "no snapshot in any paired record"
        if pt["n_cold"]:
            pt["note"] = ((pt["note"] + "; ") if pt["note"] else "") + (
                f"{pt['n_cold']} of {len(pt['pulses'])} pulse(s) fired below "
                f"80% of the setpoint and were excluded")
        return pt

    # ── the emission pedestal ─────────────────────────────────────────────────
    # A fired pulse's net current is NOT all emission: heating-supply noise
    # contributes a floor that has to come off before the numbers mean
    # anything. Measured on this bench at -200 V with the filament too cool to
    # emit, it is ~2.0 mA, and it sat under every point of the first sweeps --
    # between 1500 and 2100 mA of heating, net stayed at 2.0 mA while the
    # filament crossed several hundred K. Left in, it dominates the
    # low-temperature end and bends the Richardson slope while the fit still
    # looks tidy, which is what makes it dangerous. Subtracted, the same points
    # span 0.17 to 8.7 mA.
    #
    # It scales with the emission rail (0.49 mA at -50 V against 1.98 mA at
    # -200 V), so it has to be measured at the voltage the curve will use.
    # It does not change with pulse width (2.13 mA at 500 us against 2.01 mA at
    # 10 ms) or with temperature (STANDBY 2.08 mA against IDLE 1400 mA
    # 2.02 mA) -- both of which this checks, because both must hold for one
    # subtracted number to be correct across a whole sweep.
    #
    # The DC background is a separate, deliberate part of the design and is not
    # this: it cancels out of `net` on its own.

    def _resolve_pedestal(self, pedestal_ma, fil: int, idle_ma: int,
                          controller) -> tuple[float | None, dict]:
        """Decide what to subtract from every pulse, and say where it came from.

            None        compute it from the live rail (the default, and free)
            "measure"   fire at a cold filament and measure it, the old way
            a float     use this, no questions

        Returns (value, info). `value` None means it could not be resolved --
        the caller refuses rather than subtracting 0, because subtracting 0 is
        indistinguishable from a correct subtraction in the output and leaves
        every point ~1-2 mA high.
        """
        if isinstance(pedestal_ma, (int, float)):
            return float(pedestal_ma), {"source": "given",
                                        "pedestal_ma": float(pedestal_ma)}
        if pedestal_ma == "measure":
            ped = self.measure_emission_pedestal(fil, heat_ma=idle_ma,
                                                 controller=controller)
            return ped.get("pedestal_ma"), {"source": "measured", **dict(ped)}
        d = self.diode_path_ma()
        return d.get("ma"), {"source": "diode_formula", **dict(d)}

    @staticmethod
    def _emission_charge(charge_mams, diode_ma, on_us):
        """charge_mams with the diode path's charge removed: diode_ma is a
        constant DC for as long as the switch is closed, so its charge is
        diode_ma * on_us / 1000 (mA*ms). None if any input is -- never the
        uncorrected charge under the corrected name."""
        if charge_mams is None or diode_ma is None or not on_us:
            return None
        return round(charge_mams - diode_ma * on_us / 1000.0, 6)

    def diode_path_ma(self, emission_v: float | None = None,
                      r_ohm: float | None = None, vf_v=None) -> dict:
        """The current the HV grid's own diode path passes at this rail voltage.

        Every fired pulse carries this on top of the emission, because the same
        MOSFET that gates the emission also puts the sub-board's two diodes and
        its 100 kOhm resistor across the rail for the duration of the pulse:

            I = (|V| - Vf1 - Vf2) / R      0.972 mA at 100 V

        It is NOT emission and has to come off before a pulse current means
        anything. It was originally measured per run (see
        measure_emission_pedestal), which cost four extra firings and a minute;
        it is a deterministic function of the rail, so it is computed now and
        the measurement kept as the cross-check. The two agreed to within 3% at
        50, 100, 150 and 200 V on this bench.

        `emission_v` None reads the rail live -- the voltage ACTUALLY there,
        not the one that was commanded, because a rail sitting low would
        under-subtract by exactly its error.

        Returns {"ok", "ma", "emission_v", "r_ohm", "vf_total_v", "error"}.
        `ma` is None when the rail could not be read or is below the diode
        drop -- never 0.0, which would silently mean "nothing to subtract".
        """
        v = emission_v
        if v is None:
            v = self.read_emission_v()
        vf = sum(vf_v if vf_v is not None else self._MOSFET_VF_V)
        r = float(r_ohm or self._MOSFET_R_OHM)
        out = {"ok": False, "ma": None, "emission_v": v,
               "r_ohm": r, "vf_total_v": vf, "error": None}
        if v is None:
            out["error"] = ("could not read the emission rail, so the diode "
                            "current cannot be computed — a pulse current "
                            "reported without it is high by up to ~2 mA")
            return out
        if abs(v) <= vf:
            out["error"] = (f"rail {v} V is at or below the {vf} V of diode "
                            f"drop, so no current flows through this path")
            out["ma"] = 0.0          # a real zero here, not a missing value
            out["ok"] = True
            return out
        out["ma"] = round((abs(float(v)) - vf) / r * 1000.0, 4)
        out["ok"] = True
        return out

    def measure_emission_pedestal(self, filament: int,
                                  heat_ma: int = 1400,      # too cool to emit
                                  widths_us=(1000, 10000),  # the 1/width test
                                  pulses: int = 3,
                                  inter_pulse_ms: int = 800,
                                  controller: int | None = None) -> dict:
        """Measure the non-emission part of a fired pulse's net current.

        Fires at `heat_ma` -- cool enough that thermionic emission is
        negligible -- and reports what is left. Also fires at STANDBY, the
        coolest state the hardware offers, as the check that `heat_ma` really
        is cool enough: if the two disagree, the "pedestal" already contains
        emission and subtracting it would remove real signal.

        HV must already be on, at the SAME emission voltage the curve will be
        measured at -- the pedestal scales with the rail (-50 V gave 0.49 mA
        where -200 V gave 1.98 mA). The voltage in force is recorded in the
        result so a later mismatch is visible.

        Returns
            {"ok", "pedestal_ma", "sd_ma", "emission_v",
             "width_independent": bool,   # steady current, not edge charge
             "temperature_independent": bool,  # matches STANDBY
             "by_width": {width_us: mean_ma}, "standby_ma",
             "warnings": [str]}

        `pedestal_ma` is None when the measurement did not hold together; it is
        never a plausible-looking number with the checks quietly failed.
        """
        out = {"ok": False, "pedestal_ma": None, "sd_ma": None,
               "emission_v": None, "width_independent": None,
               "temperature_independent": None, "by_width": {},
               "standby_ma": None, "warnings": []}
        hv = self.hv_status()
        if not hv.get("ok") or not hv.get("emission_on"):
            out["warnings"].append(
                "the emission rail is off — the pedestal scales with it, so a "
                "value measured with it off is not the one the curve needs "
                "subtracted")
            return out
        out["emission_v"] = self.read_emission_v()

        def shots(width):
            fr = self.fire_single_pulse(
                filament, num_pulses=pulses, width_us=width,
                inter_pulse_ms=inter_pulse_ms, max_on_ms=40,
                total_ms=max(12000, pulses * (inter_pulse_ms + 1500)),
                controller=controller, trigger="sim",
                timeout_s=20.0 + pulses * inter_pulse_ms / 1000.0,
                verify=True, reuse=False, measure=True)
            vals = [e["plateau_net_ma"] for e in (fr.get("measured") or [])
                    if e.get("plateau_net_ma") is not None
                    and not e.get("empty_envelope")]
            return vals

        with self.energised(filament):
            self.sleep_all([filament])
            self.standby_all([filament])
            standby_vals = shots(widths_us[0])
            if standby_vals:
                out["standby_ma"] = round(sum(standby_vals) / len(standby_vals), 3)

            r = self.idle_one(filament, heat_ma, verify=True, timeout_s=40.0)
            if not (r.get("heating") or {}).get("ok"):
                out["warnings"].append(
                    f"could not hold {heat_ma} mA to measure the pedestal at "
                    f"({self.describe(r)[:100]})")
                return out
            all_vals = []
            for w in widths_us:
                vals = shots(w)
                if vals:
                    out["by_width"][int(w)] = round(sum(vals) / len(vals), 3)
                    all_vals.extend(vals)

        if not all_vals:
            out["warnings"].append("no pulse was measured")
            return out
        mean = sum(all_vals) / len(all_vals)
        out["pedestal_ma"] = round(mean, 3)
        out["sd_ma"] = round(math.sqrt(sum((x - mean) ** 2 for x in all_vals)
                                       / len(all_vals)), 4)

        # Edge charge would fall as 1/width: 20x the width, 1/20th the mean.
        # A steady current does not move. 10% over the tested span is the line.
        by_w = out["by_width"]
        if len(by_w) >= 2:
            lo, hi = min(by_w.values()), max(by_w.values())
            out["width_independent"] = (hi - lo) <= 0.10 * max(hi, 1e-9)
            if not out["width_independent"]:
                out["warnings"].append(
                    f"the pedestal changes with pulse width ({by_w}) — it is not "
                    f"a steady current, so ONE number cannot be subtracted from "
                    f"every width. Measure it at the width the curve uses")
        if out["standby_ma"] is not None and mean > 0:
            # If heat_ma is already emitting, it reads HIGHER than STANDBY.
            out["temperature_independent"] = abs(out["standby_ma"] - mean) <= 0.15 * mean
            if not out["temperature_independent"]:
                out["warnings"].append(
                    f"pedestal at {heat_ma} mA ({mean:.3f} mA) differs from "
                    f"STANDBY ({out['standby_ma']} mA) — the filament is already "
                    f"emitting at {heat_ma} mA, so this number is emission plus "
                    f"pedestal. Measure lower")
        out["ok"] = not out["warnings"]
        return out

    # ── temperature from resistance, work function from emission ──────────────
    # Two independent physical relations over the same sweep:
    #
    #   resistance thermometry   tungsten's resistivity is a known function of
    #                            temperature, so R_filament/R_cold gives T
    #   Richardson-Dushman       I = A_eff T^2 exp(-phi/kT), so ln(I/T^2)
    #                            against 1/T is a straight line whose slope is
    #                            -phi/k
    #
    # Neither is usable alone here. Thermometry needs R_filament, and what is
    # measured is R_filament + R_lead -- the leads are outside the INA219's
    # sense point and differ per filament. Richardson needs T. Putting them
    # together makes R_lead the one free parameter: the value that makes the
    # Richardson plot straightest is the lead resistance, and the temperatures
    # fall out of it. That is the whole idea, and it is only as good as the
    # data's conditioning -- see the warnings fit_richardson() emits.

    # Tungsten resistivity, uOhm*cm, 300-3600 K. Desai/Chu/James/Ho,
    # J. Phys. Chem. Ref. Data 13, 1069 (1984), the standard reference fit.
    # Spot-checked against the tabulated values it summarises: 5.47 at 300 K
    # (table 5.44-5.6), 24.5 at 1000 (24.9), 57.4 at 2000 (56.7), 94.1 at
    # 3000 (92.0) -- a few percent at the top end, which matters less than it
    # looks because T enters through a RATIO of two values from this same fit.
    _W_RHO_POLY = (-0.9680, 1.9274e-2, 7.8260e-6, -1.8517e-9, 2.0790e-13)
    _W_RHO_T_MIN, _W_RHO_T_MAX = 300.0, 3600.0
    #: Linear thermal expansion of tungsten, 1/K. R = rho*L/A and both L and A
    #: grow, so the net effect on resistance is a 1/(1+alpha*dT) factor -- about
    #: 1% at 2500 K. Small, but free to include.
    _W_EXPANSION_PER_K = 4.5e-6
    #: Tungsten melts at 3695 K; a fit that lands near it is reporting that the
    #: inputs are wrong, not that the filament is about to melt.
    _W_MELT_K = 3695.0
    _BOLTZMANN_EV_PER_K = 8.617333262e-5

    @classmethod
    def tungsten_resistivity(cls, t_k: float) -> float:
        """Tungsten resistivity (uOhm*cm) at `t_k` kelvin, 300-3600 K."""
        t = float(t_k)
        c = cls._W_RHO_POLY
        return c[0] + t * (c[1] + t * (c[2] + t * (c[3] + t * c[4])))

    @classmethod
    def tungsten_temperature(cls, r_ratio: float, t_ref_k: float = 293.0
                             ) -> float | None:
        """Invert the resistance ratio R(T)/R(t_ref_k) to a temperature (K).

        Returns None when the ratio falls outside what 300-3600 K can produce
        -- a ratio below 1 means the "hot" resistance came out under the cold
        one, which is an input error (usually too large an R_lead), and
        returning some clamped edge temperature for it would hide exactly the
        thing the caller needs to see.
        """
        ratio = float(r_ratio)
        if not (ratio > 0) or not math.isfinite(ratio):
            return None

        def model(t):
            # Thermal expansion lengthens the filament and thickens it; the net
            # is a 1/(1+alpha*dT) factor on resistance.
            exp = 1.0 + cls._W_EXPANSION_PER_K * (t - t_ref_k)
            return (cls.tungsten_resistivity(t) /
                    cls.tungsten_resistivity(t_ref_k) / exp)

        lo, hi = cls._W_RHO_T_MIN, cls._W_RHO_T_MAX
        if ratio < model(lo) or ratio > model(hi):
            return None
        for _ in range(80):          # bisection; the model is monotonic here
            mid = 0.5 * (lo + hi)
            if model(mid) < ratio:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    #: Nominal work function, eV. Pure tungsten. Used as an INPUT rather than
    #: fitted, because fitting it and the lead resistance together is what
    #: leaves both undetermined -- see solve_lead_resistance().
    WORK_FUNCTION_EV = 4.5

    @classmethod
    def emission_temperature(cls, emission_ma: float,
                             a_eff_ma_per_k2: float,
                             work_function_ev: float = WORK_FUNCTION_EV
                             ) -> float | None:
        """Invert Richardson-Dushman for T: solve I = A_eff*T^2*exp(-phi/kT).

        Returns None when no temperature in 300-3600 K produces this current --
        a non-positive current, or one outside what the given A_eff and phi can
        make. None, not a clamped edge value: an emission reading that the
        model cannot account for is a fact about the inputs, and a temperature
        returned for it would be read as a measurement.
        """
        i = float(emission_ma)
        if not (i > 0) or not (a_eff_ma_per_k2 > 0):
            return None

        def model(t):
            return a_eff_ma_per_k2 * t * t * math.exp(
                -work_function_ev / (cls._BOLTZMANN_EV_PER_K * t))

        lo, hi = cls._W_RHO_T_MIN, cls._W_RHO_T_MAX
        if i < model(lo) or i > model(hi):
            return None
        for _ in range(80):      # monotonic in T over this range
            mid = 0.5 * (lo + hi)
            if model(mid) < i:
                lo = mid
            else:
                hi = mid
        return 0.5 * (lo + hi)

    @classmethod
    def filament_temperature(cls, *,
                             r_total_ohm: float | None = None,
                             r_lead_ohm: float = 0.0,
                             r_cold_ohm: float = 0.257,
                             t_ref_k: float = 293.0,
                             emission_ma: float | None = None,
                             a_eff_ma_per_k2: float | None = None,
                             work_function_ev: float = WORK_FUNCTION_EV) -> dict:
        """Filament temperature, by whichever of the two routes you can feed.

            ct.filament_temperature(r_total_ohm=3.30, r_lead_ohm=0.20)
            ct.filament_temperature(emission_ma=2.79, a_eff_ma_per_k2=120.)
            ct.filament_temperature(r_total_ohm=3.30, r_lead_ohm=0.20,
                                    emission_ma=2.79, a_eff_ma_per_k2=120.)

        RESISTANCE route. `R_filament = r_total_ohm - r_lead_ohm`, then
        `R_filament / r_cold_ohm` inverted through tungsten's resistivity.
        Needs the lead resistance, which the INA219 cannot separate out (it
        sits upstream of the leads), and is SENSITIVE to it -- about -59 K per
        +0.1 ohm on this bench.

        EMISSION route. Richardson-Dushman inverted at a GIVEN work function.
        Needs `a_eff_ma_per_k2`, which folds in the emitting area, so it is a
        per-filament constant that has to come from a fit. Completely
        independent of the lead resistance, which is what makes the pair
        useful: where the two disagree, the lead resistance is wrong. That is
        what solve_lead_resistance() exploits.

        Returns every field always, None where an input was missing -- never a
        silent fallback from one route to the other:

            {"t_resistance_K", "t_emission_K", "delta_K",   # emission - resistance
             "r_filament_ohm", "r_ratio", "inputs": {...}, "notes": [str]}
        """
        out = {"t_resistance_K": None, "t_emission_K": None, "delta_K": None,
               "r_filament_ohm": None, "r_ratio": None, "notes": [],
               "inputs": {"r_total_ohm": r_total_ohm, "r_lead_ohm": r_lead_ohm,
                          "r_cold_ohm": r_cold_ohm, "t_ref_k": t_ref_k,
                          "emission_ma": emission_ma,
                          "a_eff_ma_per_k2": a_eff_ma_per_k2,
                          "work_function_ev": work_function_ev}}
        if r_total_ohm is not None:
            r_fil = float(r_total_ohm) - float(r_lead_ohm)
            out["r_filament_ohm"] = round(r_fil, 5)
            if r_fil <= 0:
                out["notes"].append(
                    f"lead resistance {r_lead_ohm} ohm is at or above the "
                    f"measured total {r_total_ohm} ohm — nothing is left for "
                    f"the filament")
            else:
                ratio = r_fil / float(r_cold_ohm)
                out["r_ratio"] = round(ratio, 4)
                t = cls.tungsten_temperature(ratio, t_ref_k)
                out["t_resistance_K"] = None if t is None else round(t, 1)
                if t is None:
                    out["notes"].append(
                        f"resistance ratio {ratio:.3f} is outside what "
                        f"300-3600 K tungsten produces" + (
                            " — below 1, so the hot resistance came out under "
                            "the cold one (r_lead_ohm too large)"
                            if ratio < 1 else ""))
        if emission_ma is not None and a_eff_ma_per_k2:
            t = cls.emission_temperature(emission_ma, a_eff_ma_per_k2,
                                         work_function_ev)
            out["t_emission_K"] = None if t is None else round(t, 1)
            if t is None:
                out["notes"].append(
                    f"{emission_ma} mA is outside what phi={work_function_ev} eV "
                    f"and A_eff={a_eff_ma_per_k2} produce over 300-3600 K")
        elif emission_ma is not None:
            out["notes"].append(
                "emission route needs a_eff_ma_per_k2 — it folds in the "
                "emitting area, so it is per filament and comes from a fit "
                "(fit_richardson()'s richardson_a_eff_ma_per_k2)")
        if out["t_resistance_K"] is not None and out["t_emission_K"] is not None:
            out["delta_K"] = round(out["t_emission_K"] - out["t_resistance_K"], 1)
        return out

    def solve_lead_resistance(self, result: dict,
                              work_function_ev: float = WORK_FUNCTION_EV,
                              r_cold_ohm: float = 0.257,
                              r_lead_min: float = -0.20,
                              r_lead_max: float = 0.60,
                              min_snr: float = 5.0,
                              t_ref_k: float = 293.0) -> dict:
        """The lead resistance implied by a KNOWN work function.

        Fitting phi and R_lead together leaves both undetermined -- the
        Richardson line stays straight across a wide band of R_lead because
        shifting it rescales every temperature and the slope absorbs that
        (measured: r^2 0.99860 -> 0.99846 across 0 to 0.40 ohm while phi went
        4.115 -> 3.205 eV). Fixing phi removes that freedom: the slope is then
        -phi/k, R_lead is the only thing that can change the slope, and it has
        a unique solution.

        So this is the same fit run backwards. It brackets and bisects for the
        R_lead whose fitted slope equals -work_function_ev/k.

            s = ct.solve_lead_resistance(curve)            # phi = 4.5 eV
            print(s["r_lead_ohm"], s["temperatures_K"])

        `r_lead_min` is allowed to be NEGATIVE on purpose. A negative solution
        is not a lead resistance -- it is the measurement telling you that
        `r_cold_ohm` or the assumed phi is wrong, and clamping it at 0 would
        hide that behind a plausible-looking answer.

        Returns
            {"ok", "r_lead_ohm", "work_function_ev", "physical": bool,
             "bracketed": bool, "r_squared", "richardson_a_eff_ma_per_k2",
             "temperatures_K": [...], "points": [...], "warnings": [str]}
        """
        target = float(work_function_ev)

        def phi_at(rl):
            f = self.fit_richardson(result, r_cold_ohm=r_cold_ohm,
                                    r_lead_ohm=rl, min_snr=min_snr,
                                    t_ref_k=t_ref_k)
            return f["work_function_eV"] if f.get("ok") else None

        lo, hi = float(r_lead_min), float(r_lead_max)
        p_lo, p_hi = phi_at(lo), phi_at(hi)
        # Walk the ends inward until both are computable -- an R_lead that
        # drives a point below the cold resistance has no fit at all.
        step = (hi - lo) / 40.0
        while p_lo is None and lo < hi:
            lo += step
            p_lo = phi_at(lo)
        while p_hi is None and hi > lo:
            hi -= step
            p_hi = phi_at(hi)
        warnings: list[str] = []
        if p_lo is None or p_hi is None:
            return {"ok": False, "r_lead_ohm": None,
                    "work_function_ev": target, "physical": False,
                    "bracketed": False, "warnings": [
                        f"no lead resistance in [{r_lead_min}, {r_lead_max}] ohm "
                        f"gives a fit at all — check r_cold_ohm and the "
                        f"measured voltages"], "points": [], "temperatures_K": []}
        # phi falls monotonically with R_lead, so the target must sit between
        # the two ends for a solution to exist.
        if not (min(p_lo, p_hi) <= target <= max(p_lo, p_hi)):
            return {"ok": False, "r_lead_ohm": None,
                    "work_function_ev": target, "physical": False,
                    "bracketed": False, "warnings": [
                        f"phi = {target} eV is not reachable: over R_lead "
                        f"{lo:.3f} to {hi:.3f} ohm the fitted work function only "
                        f"spans {min(p_lo, p_hi):.3f} to {max(p_lo, p_hi):.3f} eV. "
                        f"Either the cold resistance ({r_cold_ohm} ohm) is wrong, "
                        f"or this cathode is not a {target} eV emitter"],
                    "points": [], "temperatures_K": [],
                    "phi_range_eV": [round(min(p_lo, p_hi), 4),
                                     round(max(p_lo, p_hi), 4)]}
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            p_mid = phi_at(mid)
            if p_mid is None:
                break
            if (p_mid - target) * (p_lo - target) > 0:
                lo, p_lo = mid, p_mid
            else:
                hi, p_hi = mid, p_mid
        r_lead = 0.5 * (lo + hi)
        f = self.fit_richardson(result, r_cold_ohm=r_cold_ohm, r_lead_ohm=r_lead,
                                min_snr=min_snr, t_ref_k=t_ref_k)
        if not f.get("ok"):
            return {"ok": False, "r_lead_ohm": round(r_lead, 5),
                    "work_function_ev": target, "physical": r_lead >= 0,
                    "bracketed": True, "points": [], "temperatures_K": [],
                    "warnings": f.get("warnings") or ["fit failed at the solution"]}
        physical = r_lead >= 0
        if not physical:
            warnings.append(
                f"the solution is NEGATIVE ({r_lead:.4f} ohm), which no wiring "
                f"can be. At phi = {target} eV the data wants LESS resistance "
                f"than the INA219 measures, so one of the two assumptions is "
                f"off: r_cold_ohm = {r_cold_ohm} ohm, or the work function "
                f"itself. It is not a lead resistance")
        warnings.extend(w for w in (f.get("warnings") or [])
                        if "lead resistance" not in w and "scan edge" not in w)
        temps = [p["T_K"] for p in f["points"]]
        return {"ok": True, "r_lead_ohm": round(r_lead, 5),
                "work_function_ev": target, "physical": physical,
                "bracketed": True,
                "r_squared": f["r_squared"],
                "richardson_a_eff_ma_per_k2": f["richardson_a_eff_ma_per_k2"],
                "r_cold_ohm": r_cold_ohm,
                "temperatures_K": [round(t, 1) for t in temps],
                "n_points": f["n_points"], "dropped": f.get("dropped"),
                "points": f["points"], "warnings": warnings}

    def fit_richardson(self, result: dict,
                       r_cold_ohm: float = 0.257,    # filament at t_ref_k
                       r_lead_ohm: float | None = None,  # None = fit it
                       r_lead_min: float = 0.0,
                       r_lead_max: float = 0.40,
                       r_lead_step: float = 0.0005,
                       t_ref_k: float = 293.0,
                       min_snr: float = 5.0) -> dict:
        """Fit Richardson-Dushman to an emission_vs_heating() result, solving
        for the lead resistance and the per-point temperature together.

            r = ct.emission_vs_heating(8, start_ma=2200, step_ma=50)
            f = ct.fit_richardson(r)
            print(f)

        HOW. Each point gives a measured R_total = V_bus/I_heat, which is the
        filament in series with its leads. For a trial R_lead:

            R_fil = R_total - R_lead  ->  R_fil/r_cold_ohm  ->  T (tungsten)
            ln(I_emission / T^2)  vs  1/T   ->  straight line, slope -phi/k

        R_lead is scanned over [r_lead_min, r_lead_max] and the value giving
        the best least-squares line wins. `r_lead_ohm` pins it instead of
        fitting, for when it is known independently.

        WHAT IT WILL NOT DO. This does not report a temperature it cannot
        justify. The fit is checked and the result carries `trustworthy` plus a
        `warnings` list; a high r^2 on four points is not evidence on its own,
        because ln(I/T^2) vs 1/T is nearly straight for a WIDE range of R_lead
        over a narrow temperature span. What decides it:

          * `r_lead_plateau_ohm` -- the width of the R_lead band whose fit is
            within 1% of the best. Wide means R_lead is NOT determined by this
            data, whatever the argmax says. This is the number to read first.
          * an optimum at a scan EDGE means unconstrained, not "0.40".
          * `work_function_eV` outside 1.5-6.0 eV means the model does not fit
            the data, however straight the line looks. Pure tungsten is
            ~4.55 eV, thoriated ~2.6 eV, oxide ~1-2 eV.
          * temperatures near tungsten's 3695 K melting point, or points where
            R_fil came out below the cold resistance (ratio < 1), which means
            R_lead was over-subtracted.

        Returns
            {"ok", "trustworthy", "warnings": [str],
             "r_lead_ohm", "r_lead_fitted": bool, "r_lead_plateau_ohm",
             "r_lead_at_edge": bool,
             "work_function_eV", "richardson_a_eff", "r_squared", "n_points",
             "points": [{heat_mA, r_total_ohm, r_fil_ohm, r_ratio, T_K,
                         net_ma, ln_i_over_t2, inv_T, residual}],
             "scan": [{r_lead_ohm, r_squared, work_function_eV}]}
        """
        warnings: list[str] = []
        # `emission_ma` is net MINUS the pedestal; `net_ma` still has it in.
        # Fitting the uncorrected current is what bends the Arrhenius slope
        # while leaving the line looking straight, so a curve that carries no
        # correction says so rather than quietly using the wrong column.
        field = "emission_ma"
        if not any("emission_ma" in p for p in (result.get("points") or [])):
            field = "net_ma"
            warnings.append(
                "this curve carries no pedestal correction, so the fit is "
                "running on raw net current — the non-emission floor (~2 mA on "
                "this bench) dominates the cold end and flattens the slope. "
                "Re-measure with emission_vs_heating()'s default pedestal "
                "handling")
        # Points at or below zero emission carry no information for a log fit
        # and are dropped, not clamped -- they are the cold end scattering
        # around zero, which is the correct behaviour of a subtracted pedestal.
        #
        # So are points that are positive but not SIGNIFICANTLY positive. A log
        # fit treats 0.015 mA and 0.085 mA as a factor of 5.7 apart when both
        # are the same zero seen through noise, and a handful of those at the
        # cold end drags the slope through them: on this bench, keeping every
        # positive point took r^2 from 0.998 to 0.891 and moved the work
        # function from 4.12 to 2.41 eV.
        #
        # min_snr is 5, not 3, from the same bench: at 3 sigma two points
        # survived whose residuals were 3-5x every other point's, and r^2 went
        # 0.9986 -> 0.9795. At 5 and at 8 the surviving set is identical, so 5
        # is inside a plateau rather than tuned to a number. Dropped points are
        # reported under `dropped` -- silently trimming a fit is how a fit stops
        # meaning anything.
        ped_sd = ((result.get("pedestal") or {}).get("sd_ma")
                  if isinstance(result.get("pedestal"), dict) else None)
        pts, weak = [], []
        for p in (result.get("points") or []):
            if not (p.get("usable") and p.get("r_total_ohm")
                    and p.get(field) is not None):
                continue
            # The emission is a difference of two measured things, so its
            # uncertainty is both of theirs.
            sd = math.sqrt((p.get("net_ma_sd") or 0.0) ** 2
                           + (ped_sd or 0.0) ** 2) or None
            if p[field] <= 0 or (sd and p[field] < min_snr * sd):
                weak.append({"commanded_ma": p.get("commanded_ma"),
                             field: p[field], "sigma_ma": sd})
                continue
            pts.append(p)
        if weak:
            warnings.append(
                f"{len(weak)} point(s) dropped as indistinguishable from the "
                f"pedestal at {min_snr:g} sigma (up to "
                f"{max(w[field] for w in weak):.3f} mA) — they are the cold end "
                f"of the sweep, where the filament is not yet emitting "
                f"measurably. This is expected, not a fault; see `dropped`")
        if len(pts) < 3:
            return {"ok": False, "trustworthy": False, "n_points": len(pts),
                    "warnings": warnings + [
                        f"only {len(pts)} point(s) carry both a resistance and a "
                        f"positive emission current — a two-parameter line needs "
                        f"at least 3, and realistically 6+ over as wide a "
                        f"temperature span as the filament tolerates"],
                    "points": [], "scan": []}

        def line(r_lead):
            """Least-squares ln(I/T^2) vs 1/T at this lead resistance.
            Returns (r2, slope, intercept, rows) or None if any point is
            unphysical there."""
            xs, ys, rows = [], [], []
            for p in pts:
                r_fil = p["r_total_ohm"] - r_lead
                if r_fil <= 0:
                    return None
                ratio = r_fil / r_cold_ohm
                t_k = self.tungsten_temperature(ratio, t_ref_k)
                if t_k is None:
                    return None
                x = 1.0 / t_k
                y = math.log(p[field] / (t_k * t_k))
                xs.append(x)
                ys.append(y)
                rows.append({"heat_mA": p.get("heat_mA"),
                             "commanded_ma": p.get("commanded_ma"),
                             "r_total_ohm": p["r_total_ohm"],
                             "r_fil_ohm": round(r_fil, 5),
                             "r_ratio": round(ratio, 4),
                             "T_K": round(t_k, 1),
                             "net_ma": p.get("net_ma"),
                             "emission_ma": p[field],
                             "inv_T": x, "ln_i_over_t2": y})
            n = len(xs)
            mx, my = sum(xs) / n, sum(ys) / n
            sxx = sum((x - mx) ** 2 for x in xs)
            sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
            syy = sum((y - my) ** 2 for y in ys)
            if sxx <= 0 or syy <= 0:
                return None
            slope = sxy / sxx
            intercept = my - slope * mx
            r2 = (sxy * sxy) / (sxx * syy)
            for row, x, y in zip(rows, xs, ys):
                row["residual"] = round(y - (intercept + slope * x), 5)
            return r2, slope, intercept, rows

        scan: list[dict] = []
        if r_lead_ohm is None:
            steps = int(round((r_lead_max - r_lead_min) / r_lead_step))
            for i in range(steps + 1):
                rl = r_lead_min + i * r_lead_step
                got = line(rl)
                if got is None:
                    continue
                r2, slope, _icept, _rows = got
                scan.append({"r_lead_ohm": round(rl, 5), "r_squared": r2,
                             "work_function_eV": -slope * self._BOLTZMANN_EV_PER_K})
            if not scan:
                return {"ok": False, "trustworthy": False, "n_points": len(pts),
                        "warnings": [
                            f"no lead resistance in [{r_lead_min}, {r_lead_max}] "
                            f"ohm makes the data physical — every trial left a "
                            f"filament resistance below the {r_cold_ohm} ohm cold "
                            f"value or outside the 300-3600 K tungsten range. "
                            f"Check r_cold_ohm and the measured voltages."],
                        "points": [], "scan": []}
            best = max(scan, key=lambda s: s["r_squared"])
            r_lead = best["r_lead_ohm"]
            fitted = True
        else:
            r_lead, fitted = float(r_lead_ohm), False

        got = line(r_lead)
        if got is None:
            return {"ok": False, "trustworthy": False, "n_points": len(pts),
                    "warnings": [f"R_lead {r_lead} ohm leaves at least one point "
                                 f"unphysical (filament resistance <= 0, or a "
                                 f"ratio outside the tungsten table)"],
                    "points": [], "scan": scan}
        r2, slope, intercept, rows = got
        phi = -slope * self._BOLTZMANN_EV_PER_K
        a_eff = math.exp(intercept)      # mA/K^2, geometry folded in

        # ---- is this worth believing? ---------------------------------------
        plateau = None
        at_edge = False
        if fitted and scan:
            # The band of R_lead whose fit is within 1% of the best. ln(I/T^2)
            # vs 1/T stays nearly straight across a wide range of R_lead when
            # the temperature span is narrow, so a high r^2 says almost nothing
            # on its own -- the WIDTH of this band is what says whether the
            # data actually pins R_lead down.
            good = [s["r_lead_ohm"] for s in scan
                    if s["r_squared"] >= 0.99 * max(x["r_squared"] for x in scan)]
            plateau = round(max(good) - min(good), 4)
            at_edge = (min(good) <= r_lead_min + r_lead_step
                       or max(good) >= r_lead_max - r_lead_step)
            if at_edge:
                warnings.append(
                    f"the best-fit lead resistance runs into the scan edge "
                    f"[{r_lead_min}, {r_lead_max}] ohm — the data does not "
                    f"bracket it, so {r_lead} ohm is where the scan stopped, "
                    f"not where the data points")
            if plateau > 0.05:
                warnings.append(
                    f"lead resistance is NOT determined by this data: every "
                    f"value across a {plateau:.3f} ohm band fits within 1% of "
                    f"the best. Widen the heating range (more points, lower "
                    f"start_ma) before quoting {r_lead} ohm")
        if not 1.5 <= phi <= 6.0:
            warnings.append(
                f"work function {phi:.2f} eV is outside the 1.5-6.0 eV range "
                f"any real cathode occupies (tungsten 4.55, thoriated ~2.6, "
                f"oxide ~1-2) — the straight line is fitting something that is "
                f"not Richardson emission")
        temps = [row["T_K"] for row in rows]
        if max(temps) > self._W_MELT_K * 0.95:
            warnings.append(
                f"peak temperature {max(temps):.0f} K is within 5% of "
                f"tungsten's {self._W_MELT_K:.0f} K melting point — that is an "
                f"input error, not a measurement")
        span = max(temps) - min(temps)
        if span < 200:
            warnings.append(
                f"the points span only {span:.0f} K; an Arrhenius slope over "
                f"so short a lever arm is dominated by noise. Extend the sweep "
                f"downward (lower start_ma) rather than adding points at the top")
        if r2 < 0.98:
            warnings.append(f"r^2 {r2:.4f} — the points are not on a line; "
                            f"the emission may be space-charge limited rather "
                            f"than temperature limited (check that emission "
                            f"still varies strongly with heating current)")

        # HOW MUCH THE ANSWER DEPENDS ON R_lead. The Richardson line stays
        # straight across a wide range of lead resistance -- shifting R_lead
        # rescales every temperature in nearly the same way, and an Arrhenius
        # slope absorbs that -- so r^2 cannot choose between them, while phi and
        # T move a lot. Measured here: r^2 0.99807 -> 0.99766 across 0 to 0.30
        # ohm while phi went 4.12 -> 3.41 eV. That makes these derivatives, not
        # the fitted R_lead, the useful output: they are what turns an R_lead
        # measured on the I-V side into a temperature and a work function.
        sens = {}
        for delta in (0.05,):
            lo_fit, hi_fit = line(max(0.0, r_lead - delta)), line(r_lead + delta)
            if lo_fit and hi_fit:
                d_phi = ((-hi_fit[1] * self._BOLTZMANN_EV_PER_K)
                         - (-lo_fit[1] * self._BOLTZMANN_EV_PER_K))
                lo_t = sum(r["T_K"] for r in lo_fit[3]) / len(lo_fit[3])
                hi_t = sum(r["T_K"] for r in hi_fit[3]) / len(hi_fit[3])
                # NOT `span` -- that name already holds the temperature
                # span used for the warnings above, and reusing it here
                # silently reported every fit as spanning 0 K.
                probe = (r_lead + delta) - max(0.0, r_lead - delta)
                sens = {"d_work_function_eV_per_ohm": round(d_phi / probe, 3),
                        "d_mean_T_K_per_ohm": round((hi_t - lo_t) / probe, 1),
                        "probe_delta_ohm": delta}

        return {"ok": True,
                "trustworthy": not warnings,
                "warnings": warnings,
                "dropped": weak,
                "sensitivity_to_r_lead": sens,
                "emission_field": field,
                "r_lead_ohm": round(r_lead, 5),
                "r_lead_fitted": fitted,
                "r_lead_plateau_ohm": plateau,
                "r_lead_at_edge": at_edge,
                "r_cold_ohm": r_cold_ohm,
                "work_function_eV": round(phi, 4),
                "richardson_a_eff_ma_per_k2": a_eff,
                "r_squared": round(r2, 6),
                "temperature_span_K": round(span, 1),
                "n_points": len(rows),
                "points": rows,
                "scan": scan}
