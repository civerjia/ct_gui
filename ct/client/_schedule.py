"""CTClient: schedule download, scan plans, SHV control, pulses.

One part of the client class, split out of one 9900-line file by section:
    SHV schedule — download (the real transfer path)
    Building a full scan plan
    SHV schedule — low-level
    SyncIn simulate (ESP32-generated trigger pulses)
    SHV schedule — single-filament pulse (high-level)

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403


class _ScheduleMixin:
    # ── SHV schedule — download (the real transfer path) ─────────────────────
    # This is the SAME reliable, pipelined download the GUI itself uses to get
    # a schedule onto the RP2350 — not a hand-assembled sequence of individual
    # SHV ops. It handles per-frame retries, currents caching, and CRC-checked
    # verification. ALWAYS use download()/verify_schedule() to load a schedule;
    # the individual shv_clear/shv_set_entry/etc. calls further below are raw
    # building blocks for advanced/custom sequences only — see the warning
    # on that section before reaching for them.

    def _adopt_loaded_schedule(self) -> None:
        """Seed the reuse pre-filter from the BACKEND's record of what is
        already in each controller's table.

        The backend keeps running between script runs, so it knows a table a
        previous process downloaded. Without this, a fresh process reuses
        nothing -- the second run of a script re-downloads a table the hardware
        already holds, which is the cost this whole fast path exists to remove.

        It only seeds the CHEAP pre-filter. The safety check is unchanged: the
        live CRC is still re-read and compared before any download is skipped,
        so an out-of-date hint costs one extra verify, never a wrong schedule.
        Fetched once per run; a failure just leaves the pre-filter empty, which
        degrades to the old always-download behaviour.
        """
        if self._loaded_fetched:
            return
        self._loaded_fetched = True
        r = self._get("/api/loaded-schedule", timeout=5.0)
        if not r.get("ok"):
            return
        for cid_s, row in (r.get("loaded") or {}).items():
            try:
                cid = int(cid_s)
            except (TypeError, ValueError):
                continue
            plan, crc = row.get("plan"), row.get("crc")
            # The backend stores the WIRE plan (FID space); the comparison in
            # fire_single_pulse is against a wire plan too, so no crossing here.
            # A row without a CRC is not usable as a pre-filter seed: the CRC is
            # what the reuse check compares, and seeding a plan with no CRC
            # would make `controller in self._last_crc` fail anyway.
            if isinstance(plan, dict) and crc is not None:
                self._last_plan[cid] = plan
                self._last_crc[cid] = int(crc)

    def _plan_to_fids(self, plan: dict) -> tuple[dict, list]:
        """Translate a schedule plan's filament indices USER_INDEX -> FID and
        drop dead-masked entries. Returns (translated_plan, dead_skipped).

        This is the ONE place a plan crosses the USER_INDEX/FID boundary. It
        used to be nowhere: download()/verify_schedule() put plan indices on the
        wire raw while every other filament-taking method went through _fid_of(),
        so with a swap active `active_one(5)` heated FID 8 while a plan
        naming 5 scheduled FID 5 -- the schedule fired a different, unheated
        filament than the one just pre-heated. fire_single_pulse compensated by
        pre-translating its own plan; that compensation is now REMOVED (it would
        translate twice here, and a symmetric swap would map straight back to the
        original). Build plans in USER_INDEX; this crosses them to FID.

        Dead entries are dropped rather than sent, matching _live()/_prep(), and
        reported so the drop is never silent."""
        dead_skipped: list[int] = []
        dead = self.dead   # bound once — property, see _live()
        out = dict(plan)
        for key in ("emission", "heating"):
            rows = plan.get(key)
            if not isinstance(rows, list):
                continue
            kept = []
            for row in rows:
                if not isinstance(row, dict) or "filament" not in row:
                    kept.append(row)
                    continue
                f = int(row["filament"])
                if f in dead:
                    if f not in dead_skipped:
                        dead_skipped.append(f)
                    continue
                kept.append({**row, "filament": self._fid_of(f)})
            out[key] = kept
        cur = plan.get("currents")
        if isinstance(cur, dict):
            kept_cur = {}
            for k, v in cur.items():
                f = int(k)
                if f in dead:
                    if f not in dead_skipped:
                        dead_skipped.append(f)
                    continue
                kept_cur[self._fid_of(f)] = v
            out["currents"] = kept_cur
        return out, sorted(dead_skipped)

    # ── Building a full scan plan ────────────────────────────────────────
    # download() takes a plan; it does not build one. The GUI's builder lives
    # in JavaScript (buildSchedule / planHeating / buildPlan in web/app.js),
    # so until now a script had to hand-assemble the dict and re-derive the
    # heating window from reading that JS. build_scan_plan() is that algorithm
    # in Python, so the two produce the same plan for the same inputs.

    # Ring geometry, from the GUI's ct/constants.js. A scan walks a collimator
    # window around a ring of filaments; these say how big the ring and the
    # window are.
    N_FILAMENTS = 96
    COLLIMATOR_COVERAGE = 35      # filaments under the collimator at once
    MAX_SCHEDULE_ROWS = 8192      # firmware schedule cap

    def build_scan_schedule(self, *,
                            mode: str = "stationary",   # or "precision"
                            collimator_center: int = 0,
                            collimator_dir: int = +1,   # +1 CCW ring step, -1 CW
                            filament_dir: int = +1,     # gantry sweep direction
                            gantry_max_deg: float = 10.0,
                            gantry_steps: int = 5,
                            pulses: int = 1,            # burst length per filament
                            width_us: int = 1000,
                            ring_order=None,            # ring position -> filament
                            skip=(),                    # filaments that fire nothing
                            skip_dead: bool = True,
                            max_rows: int | None = None) -> dict:
        """Generate the emission table for a full scan from the ring geometry.

        This is the GUI's buildSchedule/stepScan, which had no API equivalent --
        build_scan_plan() takes an emission list, it does not produce one. Feed
        the "emission" from here straight into build_scan_plan().

        The scan walks a collimator window (COLLIMATOR_COVERAGE filaments wide)
        around the ring. Each step fires the filament at the current position in
        that window; when the window is exhausted it either steps the collimator
        round by one (mode="stationary") or advances the gantry to its next
        angle and only steps the collimator when the gantry reverses at an end
        (mode="precision"). The scan ends when the collimator has been all the
        way round -- N_FILAMENTS ring steps.

        `ring_order` maps a RING POSITION to a filament. It is NOT the client's
        filament_order (USER_INDEX -> FID); that one is applied later, on the
        wire. Conflating them would silently reorder the scan geometry.

        `skip_dead` leaves out filaments in the backend dead mask, which is why
        this is an instance method rather than a static one.

        Returns {"emission", "rows", "truncated", "ring_steps", "triggers"}.
        Precision mode produces a LOT of rows -- roughly
        N * (2*gantry_steps+1) * COVERAGE -- so `truncated` is not an edge case
        there, and it is reported rather than left for you to notice the scan
        ends early.
        """
        if mode not in ("stationary", "precision"):
            raise ValueError("mode must be 'stationary' or 'precision'")
        n = self.N_FILAMENTS
        coverage = self.COLLIMATOR_COVERAGE
        half = (coverage - 1) // 2
        cap = self.MAX_SCHEDULE_ROWS if max_rows is None else int(max_rows)

        steps = max(0, int(gantry_steps))
        n_ang = 2 * steps + 1
        angles = [-gantry_max_deg + 2 * gantry_max_deg * (k / (n_ang - 1))
                  if n_ang > 1 else 0.0 for k in range(n_ang)]

        excluded = {int(f) for f in skip}
        if skip_dead:
            excluded |= set(self.dead)

        window_pos = 0
        coll = int(collimator_center) % n
        g_idx = 0 if filament_dir > 0 else n_ang - 1
        sweep = 1 if filament_dir > 0 else -1
        ring_step = 0
        gantry = angles[g_idx] if mode == "precision" else 0.0

        rows, trig, truncated = [], 0, False
        while ring_step < n:
            if len(rows) >= cap:
                truncated = True
                break
            pos = (coll - half + window_pos) % n
            fil = pos if ring_order is None else int(ring_order[pos])
            if fil not in excluded:
                burst = max(1, int(pulses))
                rows.append({"seq": len(rows), "trigger": trig, "burstLen": burst,
                             "filament": int(fil), "widthUs": int(width_us),
                             "coll": coll, "gantry": gantry,
                             "windowPos": window_pos, "ringStep": ring_step})
                trig += burst
            # stepScan
            window_pos += 1
            if window_pos >= coverage:
                window_pos = 0
                if mode == "stationary":
                    coll = (coll + collimator_dir) % n
                    ring_step += 1
                else:
                    nxt = g_idx + sweep
                    if nxt < 0 or nxt >= n_ang:
                        sweep = -sweep
                        coll = (coll + collimator_dir) % n
                        ring_step += 1
                    else:
                        g_idx = nxt
                    gantry = angles[g_idx]
        return {"emission": rows, "rows": len(rows), "truncated": truncated,
                "ring_steps": ring_step, "triggers": trig,
                "excluded": sorted(excluded)}

    @staticmethod
    def _peak_for_lead(runs, length: int, lead: int, hold: int) -> int:
        """Peak filaments ACTIVE at once for a given pre-heat lead.

        Cyclic difference sweep, monotonic non-decreasing in `lead` -- which is
        what lets the caller binary-search it.
        """
        diff = [0] * (length + 1)
        for first_start, last_end in runs:
            promote = (first_start - lead) % length
            demote = (last_end + hold) % length
            span = (demote - promote) % length or length
            if promote + span <= length:
                diff[promote] += 1
                diff[promote + span] -= 1
            else:
                diff[promote] += 1
                diff[length] -= 1
                diff[0] += 1
                diff[promote + span - length] -= 1
        cur = peak = 0
        for t in range(length):
            cur += diff[t]
            peak = max(peak, cur)
        return peak

    def build_scan_plan(self,
                        emission,              # [{"filament", "trigger",
                                               #   "burstLen"?, "widthUs"?}]
                                               # in trigger order
                        active_count: int = 3,  # peak filaments ACTIVE at once
                        idle_ma: int = 1500,
                        active_ma: int = 2950,
                        rotation_ms: int = 0,   # whole-scan wall time; only
                                                # shapes the config timeouts
                        hold_ms: int = 0,       # stay ACTIVE this long past a
                                                # filament's last pulse
                        width_us: int = 1000,   # default per-entry width
                        repeats: int = 1,
                        no_heat=()) -> dict:    # fire but never heat these
        """Build a full scan plan — the same shape the GUI downloads.

        Returns {"config", "emission", "heating", "currents"} ready for
        download(). USER_INDEX throughout; download() crosses to FID.

        The heating window is the part worth not rewriting by hand. Each
        filament is promoted to ACTIVE some triggers BEFORE its first pulse and
        demoted after its last, and the lead is chosen by binary search as the
        LARGEST one whose peak concurrent-ACTIVE count still fits
        `active_count` -- pre-heat as early as the power budget allows, not a
        fixed number of steps. A filament's window is the arc complementary to
        its largest dark gap, so a filament that fires in two bursts is held
        ACTIVE across the short gap and dropped across the long one.

        `rotation_ms` only feeds the config timeouts (interPulseMs, totalMs);
        the actual pacing comes from the trigger source. Leave it 0 and the
        firmware minimums apply.

        Does NOT pre-heat anything. The deltas run during the schedule; the
        filaments still have to be brought up before arm or arm skips them --
        see fire_single_pulse's note.
        """
        rows = [dict(e) for e in emission]
        if not rows:
            raise ValueError("build_scan_plan: emission is empty")
        for r in rows:
            r.setdefault("burstLen", 1)
            r.setdefault("widthUs", width_us)
        length = max(int(r["trigger"]) + int(r["burstLen"]) for r in rows)
        if active_count < 1:
            raise ValueError("build_scan_plan: active_count must be >= 1")

        pulse_ms = (rotation_ms / length) if (rotation_ms and length) else 0
        hold_bursts = max(1, -(-hold_ms // pulse_ms)) if pulse_ms else 1
        hold_bursts = int(hold_bursts)

        # Each filament's run = the arc complementary to its largest dark gap.
        skip = {int(f) for f in no_heat}
        bursts: dict = {}
        for r in rows:
            f = int(r["filament"])
            if f in skip:
                continue
            bursts.setdefault(f, []).append(
                (int(r["trigger"]), int(r["trigger"]) + int(r["burstLen"])))
        runs, run_fil = [], []
        for f, bs in bursts.items():
            bs.sort()
            gap_at, max_gap = 0, -1
            for i, (_s, e) in enumerate(bs):
                nxt = bs[(i + 1) % len(bs)][0]
                gap = (nxt - e) % length
                if gap > max_gap:
                    max_gap, gap_at = gap, i
            runs.append((bs[(gap_at + 1) % len(bs)][0], bs[gap_at][1]))
            run_fil.append(f)

        lo, hi, lead = 0, length, 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self._peak_for_lead(runs, length, mid, hold_bursts) <= active_count:
                lead, lo = mid, mid + 1
            else:
                hi = mid - 1

        heating = []
        for (first_start, last_end), f in zip(runs, run_fil):
            heating.append({"filament": f, "triggerIndex": (first_start - lead) % length,
                            "state": ACTIVE, "milliamps": int(active_ma)})
            heating.append({"filament": f, "triggerIndex": (last_end + hold_bursts) % length,
                            "state": IDLE, "milliamps": int(idle_ma)})
        heating.sort(key=lambda d: d["triggerIndex"])

        max_width = max(int(r["widthUs"]) for r in rows)
        return {
            "config": {
                # maxOnMs MUST exceed the widest pulse or arm rejects
                # WidthTooLarge; the other two scale with the scan so a slow
                # run does not trip InterPulseTimeout / TotalTimeout mid-scan.
                "interPulseMs": max(3000, int(-(-pulse_ms * 4 // 1)) if pulse_ms else 0),
                "maxOnMs": max(40, -(-max_width // 1000) + 1),
                "totalMs": max(60000, int(-(-rotation_ms * repeats * 2 // 1)) if rotation_ms else 0),
                "triggerEdge": 0,
            },
            "emission": [{"filament": int(r["filament"]), "numPulses": int(r["burstLen"]),
                          "widthUs": int(r["widthUs"])} for r in rows],
            "heating": heating,
            "currents": {int(f): {"idle_mA": int(idle_ma), "active_mA": int(active_ma)}
                         for f in bursts},
            "_lead_triggers": lead,      # diagnostics, ignored by download()
            "_hold_triggers": hold_bursts,
            "_peak_active": self._peak_for_lead(runs, length, lead, hold_bursts),
        }

    def scan_report(self, controller: int | None = None, since: int | None = None,
                    plan: dict | None = None) -> dict:
        """Assemble a post-run report from everything the hardware recorded.

        Three independent sources, which is the point -- each can be complete
        while another is not, and the disagreements are the findings:

          RP2350 pulse log   what FIRED: filament, trigger seq, measured width,
                             and the per-pulse 165 verification
          RP2350 status      the run's counters: done/rbIrqs/edges, uncounted,
                             underfed, unsafeSlots, and the ring health
          STM32 events       what was MEASURED per pulse: envelope width and
                             emission current/charge

        `since` is the pulse-event cursor taken BEFORE the run (pulse_cursor());
        without it the STM32 half covers whatever is still in the ring, which
        may include an earlier run. `plan` is optional and only used to say
        which filaments were expected.

        `heating_at_pulse` is the firmware's snapshot of the filament's heating
        current AT THE INSTANT each pulse fired, which is the number that makes
        an emission reading interpretable -- a shot on a filament that had not
        reached current is not comparable with one on a hot filament. The host
        could never supply it: the ACTIVE window is a few triggers wide, one
        poll round is ~250 ms, and sampling hard enough to align perturbs the
        ramp being sampled. `fired_cold` picks out the pulses that landed below
        their setpoint.
        """
        # EVERY CONNECTED CONTROLLER by default. A two-controller run has two
        # pulse logs and two statuses; reading one reported half the run and
        # called the other half missing. controller=N still narrows it to one.
        ctrls = ([int(controller)] if controller is not None
                 else (self._connected_controllers() or [1]))
        st_by = {c: (self.shv_status(c) or {}) for c in ctrls}
        logs = []
        for c in ctrls:
            for rec in (self.shv_pulse_log(c) or []):
                logs.append({**rec, "controller": c})
        # One timeline. tOnUs is relative to each board's first trigger, and
        # both boards take their first trigger from the same edge (~1-2 us
        # apart via SyncOut), so the two logs interleave on it.
        logs.sort(key=lambda r: (r.get("tOnUs") or 0, r.get("controller")))
        ev = self.pulse_events_ma(since if since is not None else 0)
        events = ev.get("events") or []

        fired = {}
        for r in logs:
            fired[r.get("filament")] = fired.get(r.get("filament"), 0) + 1
        expected = None
        if plan:
            expected = {}
            for e in (plan.get("emission") or []):
                f = int(e["filament"])
                expected[f] = expected.get(f, 0) + int(e.get("numPulses", 1))

        # Anything that makes the run untrustworthy, named rather than left for
        # the reader to notice in a table of counters.
        problems = []
        for c, st in st_by.items():
            tag = f"controller {c}: " if len(st_by) > 1 else ""
            done, irq = st.get("totalPulsesDone"), st.get("rbIrqs")
            if done is not None and irq is not None and done != irq:
                problems.append(tag + f"totalPulsesDone {done} != rbIrqs {irq} — the host's "
                                f"bookkeeping disagrees with what the hardware fired")
            if st.get("unsafeSlots"):
                u = st["unsafeSlots"]
                slots = [i for i in range(64) if (u >> i) & 1]
                problems.append(tag + f"arm SKIPPED power slots {slots} as unsafe — those "
                                f"filaments did not fire even though the run looks normal")
            if st.get("uncounted"):
                problems.append(tag + f"uncounted={st['uncounted']} — pulses fired that the "
                                f"edge counter missed"
                                + ("" if not st.get("rbSaturated") else
                                   " (rbSaturated>0, so this is a LOWER BOUND)"))
            if st.get("underfed"):
                problems.append(tag + f"underfed={st['underfed']} — triggers arrived with "
                                f"nothing staged")
            if st.get("off_mismatches"):
                problems.append(tag + f"off_mismatches={st['off_mismatches']} — THE HV DID "
                                f"NOT TURN OFF on that many pulses")
            if st.get("rbDropped"):
                problems.append(tag + f"rbDropped={st['rbDropped']} — read-back ring "
                                f"overran, verification data was lost")

        # THE CROSS-CHECK ONLY TWO BOARDS CAN MAKE. Both count every trigger
        # (another controller's entry still advances the index), so their edge
        # counts must agree. A difference means one board missed or gained a
        # trigger -- e.g. one arrived while only one of them was armed -- and
        # from that point the master's envelopes frame the WRONG pulse on the
        # other board. Every measurement after it is suspect, and it would
        # otherwise look like a dead MOSFET.
        if len(st_by) > 1:
            edges = {c: v.get("triggerEdges") for c, v in st_by.items()}
            if None not in edges.values() and len(set(edges.values())) > 1:
                problems.append(
                    f"the controllers counted different numbers of triggers "
                    f"{edges} — they are out of step, so the master's envelope "
                    f"does not line up with the other board's pulses after the "
                    f"divergence")
        # Rebuilt here, not reused: the loop above rebinds `st` per board, so
        # after it `st` is just the LAST board's status.
        st = (st_by[ctrls[0]] if len(st_by) == 1
              else {str(c): v for c, v in st_by.items()})
        stuck = sorted({r["filament"] for r in logs if r.get("hv_stuck_on")})
        if stuck:
            problems.append(f"HV did not turn off on filament(s) {stuck}")
        mism = sorted({r["filament"] for r in logs if r.get("on_mismatch")})
        if mism:
            problems.append(f"read-back did not match the commanded byte on "
                            f"filament(s) {mism}")
        dropped_dead = []
        if expected:
            # Filaments the dead mask removed are EXPECTED to be missing -- the
            # plan was built before the filter ran. Reporting them as a
            # shortfall turns a guard doing its job into an alarm, which is
            # exactly the failure mode this report exists to avoid.
            dead = self.dead
            dropped_dead = sorted(f for f in expected if f in dead)
            short = {f: (n, fired.get(f, 0)) for f, n in expected.items()
                     if f not in dead and fired.get(f, 0) != n}
            if short:
                problems.append(f"fired count differs from the plan for "
                                f"{ {f: f'{g}/{w}' for f, (w, g) in short.items()} }")
        if len(events) != len(logs):
            problems.append(f"{len(logs)} pulses fired but the STM32 measured "
                            f"{len(events)} — measurement is incomplete, so the "
                            f"per-pulse currents do not cover every pulse")

        # HEATING AT PULSE TIME -- the firmware's snapshot, now that it records
        # one. The finding this exists for is a pulse that landed on a filament
        # that had not reached current: commanded 1500 mA, drawing 1 mA. That is
        # a REAL reading from a powered board with an open filament, not a
        # sentinel and not an error, and it is the most important row in a
        # report when it appears.
        cold = []
        heat_unknown = []
        for r in logs:
            m, t = r.get("heat_meas_mA"), r.get("heat_target_mA")
            if m is None:
                heat_unknown.append((r.get("filament"),
                                     r.get("heat_meas_unavailable")))
                continue
            if t is None or t <= 0:
                continue
            if m < t * 0.8:      # 20% short of the setpoint it was told to hold
                cold.append({"filament": r.get("filament"), "seq": r.get("seq"),
                             "meas_mA": m, "target_mA": t,
                             "pct": round(100.0 * m / t, 1)})
        if cold:
            worst = min(cold, key=lambda c: c["pct"])
            problems.append(
                f"{len(cold)} pulse(s) fired on a filament BELOW its heating "
                f"setpoint — worst: filament {worst['filament']} at "
                f"{worst['meas_mA']} mA against {worst['target_mA']} mA "
                f"({worst['pct']}%). The shot happened before the filament was "
                f"hot, so its emission reading is not comparable with the rest.")
        if heat_unknown:
            reasons = sorted({w for _f, w in heat_unknown if w})
            problems.append(
                f"{len(heat_unknown)} pulse(s) have no heating snapshot "
                f"({', '.join(reasons) or 'unknown'}) — those rows cannot be "
                f"compared against the others")

        unverified = sorted({r["filament"] for r in logs if r.get("unverified")})
        widths = [e.get("on_us") for e in events if e.get("on_us") is not None]
        charges = [e.get("integral_mams") for e in events
                   if e.get("integral_mams") is not None]
        return {
            "ok": not problems,
            "problems": problems,
            "fired_pulses": len(logs),
            "fired_by_filament": dict(sorted(fired.items())),
            "expected_by_filament": expected,
            # Named, not silently subtracted: they were in the plan and did not
            # fire, and the reader should see WHY rather than wonder.
            "dropped_dead": dropped_dead,
            "measured_pulses": len(events),
            "unverified_filaments": unverified,   # fired, but no read-back evidence
            "width_us": ({"min": min(widths), "max": max(widths),
                          "n": len(widths)} if widths else None),
            "charge_mams": ({"min": min(charges), "max": max(charges),
                             "n": len(charges)} if charges else None),
            # The firmware's snapshot at fire time. `cold` is the finding:
            # pulses that landed before the filament reached its setpoint.
            "heating_at_pulse": [
                {"filament": r.get("filament"), "seq": r.get("seq"),
                 "meas_mA": r.get("heat_meas_mA"), "target_mA": r.get("heat_target_mA"),
                 "unavailable": r.get("heat_meas_unavailable")}
                for r in logs],
            "fired_cold": cold,
            "status": st,
            "pulses": logs,
            "events": events,
        }

    @staticmethod
    def is_active_at(plan: dict, filament: int, trigger: int) -> bool:
        """Was `filament` ACTIVE at trigger `trigger`, per this plan?

        Answers it from the plan's own deltas rather than from a live read, so
        it works before the run and cannot be perturbed by asking. The window
        wraps: a filament promoted near the end of the timeline is ACTIVE
        through the wrap into the start.
        """
        iv = CTClient.heating_windows(plan).get(int(filament))
        if not iv:
            return False
        length = iv["length"]
        span = (iv["demote"] - iv["promote"]) % length or length
        return (int(trigger) - iv["promote"]) % length < span

    @staticmethod
    def heating_windows(plan: dict) -> dict:
        """Per-filament ACTIVE window, as {filament: {promote, demote, length}}.

        This is the data a Gantt chart draws: when each filament comes up and
        goes back down, on the trigger timeline. Derived from the plan's heating
        deltas, so it describes what WILL happen rather than what a poll caught.
        """
        length = 0
        for e in (plan.get("emission") or []):
            length = max(length, int(e.get("numPulses", 1)))
        # The timeline length is the total trigger count, which for an emission
        # list in trigger order is the sum of the burst lengths.
        length = sum(int(e.get("numPulses", 1)) for e in (plan.get("emission") or [])) or 1
        out: dict = {}
        for d in (plan.get("heating") or []):
            f = int(d["filament"])
            slot = out.setdefault(f, {"promote": None, "demote": None, "length": length})
            if int(d["state"]) == ACTIVE:
                slot["promote"] = int(d["triggerIndex"])
            elif int(d["state"]) == IDLE:
                slot["demote"] = int(d["triggerIndex"])
        return {f: v for f, v in out.items()
                if v["promote"] is not None and v["demote"] is not None}

    def gantt(self, plan: dict, width: int = 72) -> str:
        """Render the schedule as text — the terminal form of the GUI's Gantt.

        One row per filament: `#` where it fires, `=` where it is held ACTIVE,
        and blank where it is off. The point is to see the OVERLAP: how many
        filaments are hot at once, and whether each one is up before its own
        pulse. A count of concurrently-ACTIVE filaments runs underneath.
        """
        emission = plan.get("emission") or []
        if not emission:
            return "(empty plan)"
        length = sum(int(e.get("numPulses", 1)) for e in emission)
        windows = self.heating_windows(plan)
        fires: dict = {}
        t = 0
        for e in emission:
            n = int(e.get("numPulses", 1))
            fires.setdefault(int(e["filament"]), set()).update(range(t, t + n))
            t += n
        scale = max(1, -(-length // width))     # triggers per column
        cols = -(-length // scale)
        lines = [f"trigger 0..{length - 1}"
                 + (f"  ({scale} per column)" if scale > 1 else "")]
        concurrent = [0] * cols
        for f in sorted(set(list(fires) + list(windows))):
            row = []
            for c in range(cols):
                span = range(c * scale, min(length, (c + 1) * scale))
                if any(tt in fires.get(f, ()) for tt in span):
                    row.append("#")
                elif any(self.is_active_at(plan, f, tt) for tt in span):
                    row.append("=")
                    concurrent[c] += 1
                else:
                    row.append(" ")
            # '#' columns are ACTIVE too -- count them, but draw the pulse.
            for c in range(cols):
                span = range(c * scale, min(length, (c + 1) * scale))
                if row[c] == "#" and any(self.is_active_at(plan, f, tt) for tt in span):
                    concurrent[c] += 1
            lines.append(f"  fil {f:>3} |{''.join(row)}|")
        peak = max(concurrent) if concurrent else 0
        lines.append(f"  ACTIVE   |{''.join(str(min(9, c)) if c else '.' for c in concurrent)}|"
                     f"  peak {peak}")
        lines.append("  legend: # pulse   = held ACTIVE   digits = concurrent ACTIVE")
        return "\n".join(lines)

    def validate_plan(self, plan: dict, rotation_ms: int,
                      t_settle_ms: float = 0.0) -> dict:
        """Is every filament ACTIVE long enough before it fires?

        The only check that matters on a scan plan: the pre-heat lead has to be
        at least the filament's settling time, or pulses land on a filament that
        has not reached operating current. `rotation_ms` converts the lead from
        triggers into milliseconds -- the plan itself is in triggers and knows
        nothing about wall time.

        Returns {"ok", "lead_triggers", "lead_ms", "t_settle_ms", "peak_active",
        "problems"}. ok is False when the lead is short, which is a REAL
        finding: build_scan_plan picks the largest lead the concurrency budget
        allows, so a short one means the budget cannot buy enough pre-heat and
        the answer is a higher active_count or a slower rotation, not a retry.
        """
        # Same vacuous-truth trap: no emission rows means lead_ms 0 against a
        # t_settle_ms that also defaults to 0, so ok comes back True for a plan
        # that cannot fire anything.
        if not (plan or {}).get("emission"):
            return {"ok": False, "problems": ["plan has no emission rows"],
                    "error": "plan has no emission rows — nothing to validate"}
        length = sum(int(e.get("numPulses", 1)) for e in (plan.get("emission") or [])) or 1
        lead = plan.get("_lead_triggers")
        windows = self.heating_windows(plan)
        if lead is None:
            # Not built here -- recover the lead from the first filament's own
            # window rather than refusing to answer.
            leads = []
            t = 0
            for e in (plan.get("emission") or []):
                f = int(e["filament"])
                if f in windows:
                    leads.append((t - windows[f]["promote"]) % length)
                t += int(e.get("numPulses", 1))
            lead = min(leads) if leads else 0
        pulse_ms = rotation_ms / length if (rotation_ms and length) else 0
        lead_ms = lead * pulse_ms
        peak = max((sum(1 for f in windows if self.is_active_at(plan, f, t))
                    for t in range(length)), default=0)
        problems = []
        if t_settle_ms and lead_ms + 1e-6 < t_settle_ms:
            problems.append(
                f"pre-heat lead is {lead_ms:.0f} ms ({lead} triggers) but the "
                f"filament needs {t_settle_ms:.0f} ms to settle — pulses will "
                f"land on filaments that have not reached operating current. "
                f"Raise active_count (buys a longer lead) or slow the rotation.")
        if not rotation_ms and t_settle_ms:
            problems.append("rotation_ms is 0, so the lead cannot be converted "
                            "to milliseconds and the settle check did not run")
        return {"ok": not problems, "lead_triggers": lead, "lead_ms": lead_ms,
                "t_settle_ms": t_settle_ms, "peak_active": peak,
                "problems": problems}

    def download(self, plan: dict,       # {"config", "emission", "heating"?,
                                          # "currents"?} -- see the shape below
                timeout: float = 30.0) -> dict:  # generous default; a full
                                                  # multi-filament schedule with
                                                  # currents can take a while
        """Download a schedule plan to every connected controller.

        plan: {
            "config": {"interPulseMs", "maxOnMs", "totalMs", "triggerEdge"},
            "emission": [{"filament", "numPulses", "widthUs"}, ...],
            "heating": [{"filament", "triggerIndex", "state", "milliamps"}, ...],  # optional
            "currents": {filament: {"idle_mA", "active_mA"}},  # optional
        }

        The full emission list is sent to every connected controller — each
        RP2350 only fires the entries for filaments its own active-list map
        actually owns, so this is safe even when only one controller is
        involved. Downloads to both connected controllers if both are up.

        Plan filament indices are USER_INDEX (your numbering) -- they are crossed
        through filament_order and dead-filtered on the way out by _plan_to_fids().
        Any dead-masked entry is dropped and reported back as "dead_skipped";
        if that would leave nothing to fire, the download is refused outright
        rather than writing an empty emission table.

        Returns {"ok", "results": [...]} — one result dict per controller, plus
        "dead_skipped": [...] whenever the dead mask removed something.
        """
        # "Nothing to check" is not "checked". An empty emission table makes
        # every count compare 0 against 0 and a download of nothing report success.
        # Refuse, so an empty plan fails here rather than at arm time.
        if not (plan or {}).get("emission"):
            return {"ok": False, "results": [],
                    "error": "plan has no emission rows — nothing would be "
                             "downloaded, and reporting that as a successful "
                             "download hides the empty plan until arm rejects it."}
        wire_plan, dead_skipped = self._plan_to_fids(plan)
        if isinstance(plan.get("emission"), list) and plan["emission"] and not wire_plan["emission"]:
            return {"ok": False, "results": [], "dead_skipped": dead_skipped,
                    "error": "every emission entry is dead-masked — nothing to download"}
        r = self._post("/api/download", {"plan": wire_plan}, timeout=timeout)
        if dead_skipped:
            r = {**r, "dead_skipped": dead_skipped}
        # Keep fire_single_pulse(reuse=True)'s per-controller cache honest even
        # when download() is called directly (bypassing fire_single_pulse): a
        # successful write updates what we believe is on that controller now;
        # a failed one CLEARS both cache entries rather than leaving a stale
        # belief about a possibly-partial write. _last_crc is deliberately
        # NOT set here (only fire_single_pulse's own verify_schedule() call
        # populates it) — a direct download() with no matching verify leaves
        # reuse's crc check unable to confirm anything, which correctly
        # forces a full download on the next fire_single_pulse(reuse=True)
        # instead of trusting a crc we never actually observed.
        for row in (r.get("results") or []):
            cid = row.get("controller")
            if cid is None:
                continue
            if row.get("ok"):
                # The WIRE plan (FID space), not the USER_INDEX one we were
                # handed. The hardware holds FIDs, the backend records FIDs, and
                # the reuse check compares against what the hardware holds -- so
                # caching the caller's own numbering here would mismatch the
                # moment a filament_order swap is active, and would make two
                # scripts with different orders but the SAME physical schedule
                # each think the other's table was stale.
                self._last_plan[cid + 1] = wire_plan
            else:
                self._last_plan.pop(cid + 1, None)
            self._last_crc.pop(cid + 1, None)
        return r

    def verify_schedule(self, plan: dict) -> dict:
        """Read the emission/heat table counts + CRC back from every
        connected controller and confirm they match `plan`. Call this after
        download() and before arming, to catch a corrupted/partial transfer
        before firing anything.

        Takes the SAME USER_INDEX plan you gave download() -- it is crossed
        identically here (_plan_to_fids), so the CRC compared against the hardware
        is computed over the bytes that were actually written. Passing a plan
        that download() dead-filtered is fine: this filters it the same way.

        Returns {"ok", "results": {controller: {"match": bool, ...}}}.
        """
        # "Nothing to check" is not "checked". An empty emission table makes
        # every count compare 0 against 0 and every test pass vacuously -- and
        # this runs immediately before arming, so a caller who built an empty
        # plan by mistake gets told it verified. Refuse instead.
        if not (plan or {}).get("emission"):
            return {"ok": False, "results": {},
                    "error": "plan has no emission rows — there is nothing to "
                             "verify, which is not the same as verified. Build "
                             "one with build_scan_schedule()/build_scan_plan()."}
        wire_plan, dead_skipped = self._plan_to_fids(plan)
        r = self._post("/api/verify-schedule", {"plan": wire_plan}, timeout=10.0)
        # SAY that entries were dropped. Without this a dead filament in the
        # plan makes the counts differ from what the CALLER built -- they built
        # 32 heating entries, 30 were verified -- and the only visible symptom
        # is match=False, which reads as a transfer failure. The dead mask is
        # backend-held and shared, so the entry may have been marked by someone
        # else entirely; nothing in the caller's own code would hint at it.
        if dead_skipped:
            r = {**r, "dead_skipped": sorted(dead_skipped),
                 "note": (f"{len(dead_skipped)} filament(s) were dropped from the plan "
                          f"as dead before it was sent: {sorted(dead_skipped)}. The "
                          f"counts below are for what was ACTUALLY downloaded, which "
                          f"is smaller than what you built -- that is the dead mask "
                          f"working, not a transfer problem. ct.dead_details() says "
                          f"who marked them and why.")}
        return r

    # ── SHV schedule — low-level ──────────────────────────────────────────────
    # RAW single-op building blocks, useful for advanced/custom sequences (e.g.
    # tweaking just the timing config without a full re-download). For loading
    # an actual schedule, use download() above instead — it is the reliable,
    # retry-capable, CRC-verifiable path; calling clear/set_entries/set_config
    # by hand here bypasses all of that and is easy to get subtly wrong on a
    # slow or lossy link.
    #
    # WIRE-FORMAT LIMITS: these fields are fixed-width integers on the wire to
    # the RP2350 — passing a value outside its range doesn't get clamped or
    # rounded anywhere (backend.py, firmware); the backend's own int->bytes
    # encoding just raises OverflowError, which surfaces as a not-very-useful
    # {"ok": False, "error": "int too big to convert"}. The methods below
    # check first and return a clear, specific error instead.
    #   maxOnMs, widthUs        -> uint16: 0-65535 (ms / µs respectively)
    #   interPulseMs, totalMs   -> uint32: 0-4294967295 (ms) — effectively
    #                              unbounded for any real schedule
    #   numPulses                -> uint8: 0-255 — NOT validated here; the
    #                              backend silently truncates via `& 0xFF`
    #                              (e.g. 300 becomes 44) rather than
    #                              rejecting it, so keep this one <= 255
    #                              yourself.


    def _range_error(self, name: str, value: int, max_value: int) -> str | None:
        """None if `value` fits [0, max_value]; else a ready-to-return error string."""
        if not (0 <= value <= max_value):
            return f"{name}={value} out of range (0-{max_value})"
        return None

    def _shv(self, controller: int, body: dict, timeout: float | None = None) -> dict:
        return self._post("/api/shv", {"controller": controller, **body}, timeout)

    def shv_clear(self, controller: int = 1) -> dict:
        return self._shv(controller, {"op": "clear_table"})

    def shv_push_active_list(self, controller: int = 1) -> dict:
        return self._shv(controller, {"op": "push_active_list"})

    def shv_set_entry(self, controller: int, filament: int,
                      num_pulses: int = 1,   # pulses in this entry's burst;
                                              # uint8 on the wire (0-255),
                                              # REJECTED here if out of range
                                              # (the backend encodes it &0xFF,
                                              # so 300 would silently become 44)
                      width_us: int = 1000) -> dict:  # pulse width (µs);
                                                       # uint16, 0-65535,
                                                       # rejected here if out
                                                       # of range
        err = (self._range_error("num_pulses", int(num_pulses), self._U8_MAX)
               or self._range_error("width_us", int(width_us), self._U16_MAX))
        if err:
            return {"ok": False, "error": err}
        return self._shv(controller, {
            "op": "set_entries",
            "entries": [{"filament": self._fid_of(filament),
                         "numPulses": int(num_pulses),
                         "width": int(width_us)}],
        })

    def shv_set_config(self, controller: int,
                       inter_pulse_ms: int = 3000,  # min gap between SyncIn
                                                     # edges (ms); uint32, huge
                                                     # legal range
                       max_on_ms: int = 40,          # arm-time safety gate
                                                      # (ms); uint16, 0-65535 --
                                                      # see fire_single_pulse's
                                                      # docstring for the full
                                                      # "checked at arm, not
                                                      # runtime" explanation
                       total_ms: int = 30000,  # FIRMWARE's own schedule
                                                # deadline (ms); uint32
                       trigger_edge: int = 0) -> dict:  # 0=rising, 1=falling —
                                                         # which SyncIn edge
                                                         # fires the schedule
        """Set the SHV schedule engine's timing config. All three timeouts
        are FIRMWARE-side (the RP2350 enforces them itself, independent of
        this Python process):

            inter_pulse_ms — a RUNTIME watchdog while Armed/Running: if no
                new SyncIn trigger arrives within this many ms of the last
                one, the RP2350 faults the WHOLE schedule
                (stopReason=InterPulseTimeout, shv_status()["stopReason"])
                — it does not skip/ignore the late trigger, the run stops.
                Default 30000 ms (30 s) is sized for slow manual bench
                triggers; tighten it once you're firing on a real cadence
                so a genuinely stuck run is caught quickly instead of
                sitting "Armed" for 30 s.
            max_on_ms — checked ONCE, at arm() time, against every entry's
                width_us already in the table: if ANY entry exceeds it,
                arm() itself is REJECTED (WidthTooLarge) and nothing fires
                at all — it is NOT a runtime cutoff that would truncate an
                in-flight pulse. Fix the offending entry or raise
                max_on_ms and re-arm.
            total_ms — the RP2350's own deadline for the WHOLE armed
                schedule, arm to last pulse; exceeding it faults the run
                (stopReason=TotalTimeout) independent of whether any Python
                process is even watching. This is a SEPARATE timeout from
                fire_single_pulse's `timeout_s` (that one lives in THIS
                process and only bounds how long it polls over HTTP before
                giving up) — see fire_single_pulse's docstring, "total_ms
                vs timeout_s", for the full two-clocks explanation and why
                timeout_s should be set a bit larger than total_ms/1000.
            trigger_edge — which SyncIn edge the schedule fires on. Match
                this to whatever actually drives SyncIn (the ESP32's own
                Sync I/O "Ext edge" setting, or an external source) — a
                mismatch means every real trigger is invisible to the
                engine and inter_pulse_ms's watchdog above will eventually
                fault the run with nothing having fired.

        max_on_ms is a firmware uint16 field — legal range 0-65535 ms
        (~65.5 s). Out-of-range values are rejected HERE with a clear
        {"ok": False, "error": ...}, before ever reaching the network."""
        err = (self._range_error("max_on_ms", int(max_on_ms), self._U16_MAX)
               or self._range_error("inter_pulse_ms", int(inter_pulse_ms), self._U32_MAX)
               or self._range_error("total_ms", int(total_ms), self._U32_MAX))
        if err:
            return {"ok": False, "error": err}
        return self._shv(controller, {
            "op": "set_config",
            "interPulseMs": int(inter_pulse_ms),
            "maxOnMs": int(max_on_ms),
            "totalMs": int(total_ms),
            "triggerEdge": 1 if int(trigger_edge) else 0,
        })

    def shv_arm(self,
               controller: int = 1,   # which RP2350's schedule engine to arm
               repeats: int = 1) -> dict:  # how many times to loop the whole
                                            # downloaded schedule before
                                            # auto-completing
        """Arm the SHV schedule. Returns immediately; firing waits for a
        SyncIn edge. Does NOT raise on rejection — check result["ok"]; a
        rejection includes result["reject"] (a numeric firmware reject
        code — e.g. IsoOff if the target filament's board isn't
        present/enabled).

        `repeats` — how many times the RP2350 auto-loops the WHOLE
        downloaded table before completing. The loop is seamless on the
        firmware side: it does NOT need a fresh external "start the next
        repeat" signal — SyncIn triggers keep driving individual PULSES as
        usual, and the engine itself re-stages entry 0 once the table's
        last entry finishes. `repeats=0` is treated the same as 1 (fires
        the table exactly once). `shv_status()["totalPulsesDone"]` counts
        across ALL repeats, not per-loop, so divide by the table's
        per-loop pulse count if you need to know which repeat you're in."""
        return self._shv(controller, {"op": "arm", "repeats": int(repeats)})

    def shv_disarm(self, controller: int = 1) -> dict:
        return self._shv(controller, {"op": "disarm"})

    def arm_all(self, repeats: int = 1) -> dict:
        """Arm the downloaded schedule on EVERY connected controller — the
        call for a two-controller run (shv_arm arms one board).

        The master is armed LAST, and the master forwards the trigger to the
        other board only while it is itself armed, so a trigger arriving
        mid-arm is dropped by both boards rather than counted by one (which
        would leave them an entry apart for the whole run). All-or-nothing:
        on the first failure the master is not armed and every board that
        did arm is disarmed again. Refused while the boards' trigger delays
        differ (`trigger_delay_mismatch`).

        Returns {"ok", "order": [controller, ...], "results":
        {controller: {"ok", "reject", "disarmed"?, "skipped"?}}}."""
        return self._post("/api/arm", {"repeats": max(1, int(repeats))})

    def shv_status(self, controller: int = 1) -> dict:
        """SHV status: {state, filamentIndex, totalPulsesDone, elapsedMs, …}.
        Returns {} on failure (never raises)."""
        st = self._shv(controller, {"op": "status"}).get("status") or {}
        # filamentIndex/faultFilament come back as FID (0xFF/255 = none)
        # -- re-key to USER_INDEX so they match YOUR numbering.
        for key in ("filamentIndex", "faultFilament"):
            if key in st and st[key] not in (None, 0xFF, 255):
                st[key] = self._user_index_of(st[key])
        return st

    def shv_pulse_log(self,
                      controller: int = 1,   # which RP2350 to read the log from
                      start: int = 0) -> list[dict]:  # log index to start from
                                                       # (paginate through a
                                                       # long run's history)
        """Fired pulse records, ALL of them, paging until the log is exhausted.

        Each record: {filament, seq, tOnUs, durationUs, flags, read165,
        on_mismatch, hv_stuck_on, unverified, heat_meas_mA, heat_target_mA} --
        the last two being the filament's heating current at the instant that
        pulse fired, or None with a *_unavailable reason.

        PAGES, and the page size is not something to assume. The firmware sizes
        a page to fit the link (~242 deliverable bytes), so it shrank from 32 to
        14 records when the record grew from 12 to 16 bytes -- and an oversized
        frame is DROPPED SILENTLY, so a caller that assumed the old size would
        have seen a run simply stop reporting past ~19 pulses. This loops on
        what each page actually returned, against the total the firmware states.

        Returns [] on failure (never raises).
        """
        out: list[dict] = []
        idx = int(start)
        total = None
        for _ in range(512):     # bound: 4096-record log / smallest sane page
            r = self._shv(controller, {"op": "pulse_log", "start": idx})
            page = r.get("records") or []
            if total is None:
                total = r.get("total")
            if not page:
                break            # empty page = nothing further, whatever total says
            out.extend(page)
            idx += len(page)
            if total is not None and len(out) >= int(total):
                break
        for rec in out:
            if "filament" in rec:
                rec["filament"] = self._user_index_of(rec["filament"])
        return out

    # ── SyncIn simulate (ESP32-generated trigger pulses) ──────────────────────
    # fire_single_pulse(trigger="sim") uses this internally for a single burst.
    # These standalone methods let you start/stop/monitor a simulated SyncIn
    # train directly — e.g. to drive a longer/independent test sequence, or to
    # generate the SyncIn edges for a hand-built low-level SHV sequence.

    def simulate_sync(self,
                      count: int = 1,                # how many SyncIn edges
                                                       # to generate
                      interval_ms: float | None = None,  # fixed gap between
                                                          # pulses -- pick
                                                          # EITHER this OR
                                                          # duration_s, not both
                      duration_s: float | None = None,   # spread `count` pulses
                                                          # evenly across this
                                                          # many seconds instead
                      controller: int | None = None,  # which RP2350 fires the
                                                       # edge; None = the current
                                                       # MASTER, the head of the
                                                       # chain (it forwards to
                                                       # the other board)
                      expect=None,                    # optional [filament, ...]
                                                       # to seed the run-report's
                                                       # expected-filament
                                                       # tracking, like a real scan
                      active_ma: int = 2900) -> dict:  # paired with `expect` --
                                                        # the ACTIVE current the
                                                        # run-report expects
        """Start the ESP32 generating `count` SyncIn pulses.

        Fires from the head of the chain: the current master unless
        `controller` says otherwise. The master forwards the edge to the other
        power unit (only while it is armed itself). Leave `controller` unset:
        it defaulted to 1, which is the master only until the master moves --
        after that the default fired the other board directly, the master never
        saw the trigger, and its envelope never framed the pulse (the G3
        failure). The backend resolves None to its current MASTER.

        Pass EITHER `interval_ms` (fixed gap between pulses) OR `duration_s`
        (spread `count` pulses evenly across this many seconds) — not both.
        If neither is given, pulses fire back-to-back with no delay.

        `expect` (optional list of filament indices) and `active_ma` seed
        the run-report's expected-filament tracking, same as a real scan.

        Does not raise — if a simulation is already running, returns
        {"ok": False, "error": "..."}; call simulate_sync_stop() first.

        Returns {"ok", "count", "interval_ms", "controller"}.
        """
        body: dict = {"count": int(count), "active_mA": int(active_ma)}
        if controller is not None:
            body["controller"] = int(controller)
        if interval_ms is not None:
            body["interval_ms"] = float(interval_ms)
        elif duration_s is not None:
            body["duration_s"] = float(duration_s)
        if expect is not None:
            body["expect"] = list(expect)
        return self._post("/api/sync/simulate", body, timeout=10.0)

    def simulate_sync_stop(self) -> dict:
        """Stop an in-progress simulated SyncIn train early. Safe to call
        even if nothing is running (no-op)."""
        return self._post("/api/sync/simulate-stop", {})

    def simulate_sync_status(self) -> dict:
        """Read simulate-sync progress.

        Returns {"ok", "running": bool, "fired": int, "count": int,
        "stop": bool, "controller": int|None}.
        """
        return self._get("/api/sync/simulate-status")

    # ── SHV schedule — single-filament pulse (high-level) ────────────────────

    def fire_single_pulse(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,       # RP2350 FIRMWARE's own schedule timeout (ms)
                                      # — see docstring, "total_ms vs timeout_s"
        controller: int | None = None,   # None = auto-infer from `filament` via
                                          # the active-list mapping — see docstring,
                                          # "why controller exists at all"
        trigger: str = "sim",
        timeout_s: float = 15.0,     # PYTHON CLIENT's polling timeout (seconds)
                                      # — see docstring, "total_ms vs timeout_s"
        verify: bool = True,
        reuse: bool = False,   # skip re-download if unchanged since your last
                                # call — see docstring, "reuse — skipping the
                                # download when nothing changed"; OFF by
                                # default because it has a real, documented
                                # safety gap (see the docstring) — opt in only
                                # when you understand it.
        measure: bool = False,   # ALSO measure the HV current of each
                                  # pulse on the STM32 detector -- see
                                  # "measure" in the docstring
        rate_hz: int = 1000000,  # detector ADC sample rate; only used
                                  # when measure=True
        bg_gap_us: float | None = None,  # measure=True only: wait this long
                                               # after the pulse ends before
                                               # sampling the post-pulse level
        bg_window_us: float | None = None,    # measure=True only: then average
                                               # over this long. None = firmware
                                               # default (50 us / 50 us)
        on_armed=None,               # callable() run after arming,
                                      # immediately before the trigger
                                      # -- see emission_ramp()
    ) -> dict:
        """Download a one-entry schedule, arm it, fire, and verify.

        IT DOES NOT HEAT THE FILAMENT. The schedule it builds carries an EMPTY
        heating table and this method calls nothing in the power-state ladder,
        so the filament stays in whatever state you left it in. What runs end
        to end here is the SCHEDULE path (download -> verify -> arm -> trigger
        -> read back, plus the detector when measure=True), not the whole
        operation.

        Bring the filament up yourself first. At MINIMUM its isolated rail must
        be on, or arm silently SKIPS it -- sleep_one() is enough for that, and
        the result then carries skipped_unsafe. For a real emission measurement
        it has to be at operating current:

            with ct.energised(f):
                ct.sleep_one(f); ct.standby_one(f)
                ct.idle_one(f, 1500, verify=True, timeout_s=30)
                ct.active_one(f, 2950, verify=True)
                r = ct.fire_single_pulse(f, width_us=1000, measure=True)


        Internally: disarm -> download() (the real reliable transfer path,
        same one the GUI uses) -> verify_schedule() -> arm -> trigger -> poll.
        Does NOT raise at any step — every failure mode (dead filament,
        download failure, verify mismatch, arm rejection, SHV fault, poll
        timeout) comes back as {"ok": False, "error": "...", ...}; check
        "ok" yourself. Best-effort disarms the schedule before returning on
        any failure path, so a failed fire doesn't leave it armed.

        `total_ms` vs `timeout_s` — two DIFFERENT timeouts, on two DIFFERENT
        machines, watching two DIFFERENT things:

            total_ms   -> lives on the RP2350. Downloaded as part of the
                          schedule config. The FIRMWARE's own deadline for
                          the whole armed schedule (from arm to the last
                          pulse) — if exceeded, the RP2350 itself declares
                          the schedule timed out/faulted, independent of
                          whether Python is even still watching.
            timeout_s  -> lives in THIS Python process. How long the local
                          while-loop below keeps polling shv_status() over
                          HTTP before giving up and returning
                          {"ok": False, "timeout": True, ...} on its own —
                          independent of what the RP2350 is doing. Python
                          could give up on a schedule that's still running
                          fine on the hardware, or keep polling a schedule
                          the RP2350 already abandoned.

        Rule of thumb: timeout_s should be a bit LARGER than total_ms/1000,
        so Python doesn't give up right before the firmware would have
        reported COMPLETE/FAULT on its own — e.g. total_ms=40000 (40 s)
        pairs with timeout_s=45.0, not timeout_s=15.0 (the default, sized
        for total_ms's own 15000 ms default).

        Why `controller` exists at all, and why it's separate from
        `filament`: every OTHER single-filament method in this client
        (active_one, idle_one, hv_grid_set, ...) takes ONLY a filament index
        — the backend resolves which controller/board that filament lives
        on via the active-list mapping (see filament_to_board()) and routes
        the command there for you. fire_single_pulse can't be fully
        filament-scoped the same way, because `download()`/`shv_arm()`/
        `shv_disarm()`/`shv_status()`/`shv_pulse_log()`/`simulate_sync()`
        are commands to a CONTROLLER'S SCHEDULE ENGINE (one whole RP2350's
        armed/running state), not to one board — a controller's schedule
        can hold entries for many filaments at once, and arming/triggering
        operates on the ENGINE, not a single filament within it. For this
        single-filament convenience wrapper, "which engine to arm" is
        almost always just "whichever controller owns this filament" — so
        `controller=None` (the default) resolves it automatically via
        filament_to_board(). Pass an explicit `controller` only if you need
        to override that (e.g. deliberately targeting a different chained
        controller for some reason). Passing the wrong explicit controller
        here is a real footgun: the schedule downloads fine either way
        (download() always reaches every connected controller), but arming
        the WRONG controller's engine means the RIGHT controller — the one
        that actually owns the board — never gets triggered, and the pulse
        silently never fires.

        `reuse` — skipping the download when nothing changed: downloading
        the schedule (active-list, mask, config, emission table) costs
        several UART round-trips (~6 frames) every single call, even when
        you're firing the SAME filament with the SAME pulse settings
        repeatedly (measured ~65x slower than a reused call). `reuse=True`
        skips straight to a cheap verify_schedule() check (~2 frames, no
        data transfer) instead of a full re-download WHEN both of these
        hold:
            1. This exact plan (filament/num_pulses/width_us/timing config)
               is the last one THIS CLIENT successfully downloaded to this
               controller (tracked in-memory per CTClient instance) — a
               cheap LOCAL pre-filter, no network call needed to fail this.
            2. A FRESH verify_schedule() call's returned table CRC (the
               firmware-computed checksum from ShvGetTableInfo) still
               matches the CRC we recorded right after OUR OWN last
               successful write. This is REAL content verification, not
               just an entry-count check — verify_schedule()'s own "match"
               field only compares counts (no CRC in that logic), which
               would miss a different actor overwriting the table with a
               same-sized-but-different schedule; comparing the CRC
               ourselves closes that gap without needing to know the
               RP2350's CRC algorithm.
        If EITHER check fails, it falls back to a full download()+verify()
        automatically — reuse never trades correctness for speed, only
        skips work when it's confident enough to, and self-heals the next
        time it's called.

        Still recommended: hold the lease for the whole sequence (e.g.
        `with ct.lease():` around repeated fire_single_pulse(reuse=True)
        calls) so writes from OTHER clients are blocked outright at the
        backend level — the CRC check is a strong second line of defense,
        not a replacement for that.

        Args:
            filament:       0–95 global filament index.
            num_pulses:     pulses in the burst.
            width_us:       pulse width (µs).
            inter_pulse_ms: minimum gap between SyncIn edges (ms) — a
                            RUNTIME watchdog: no trigger within this long
                            faults the WHOLE run (stopReason=
                            InterPulseTimeout), it does not skip a late one.
            max_on_ms:      firmware safety guard, checked ONCE at arm()
                            against width_us — NOT a runtime cutoff. If
                            width_us exceeds it, arm() itself is rejected
                            (WidthTooLarge) and nothing fires; it never
                            truncates an in-flight pulse.
            total_ms:       FIRMWARE schedule timeout (ms) — see above.
            controller:     1, 2, or None (default) to auto-infer from
                            `filament` via the active-list mapping — see
                            "why controller exists at all" above.
            trigger:        "sim" — ESP32 generates SyncIn pulse(s);
                            "ext" — caller supplies the external SyncIn edge,
                            and must not send it before on_armed is called --
                            see "EXTERNAL TRIGGER" below.
                            Either way this fires on the RISING edge —
                            triggerEdge is hardcoded 0 here (unlike
                            shv_set_config's `trigger_edge`, which this
                            convenience wrapper doesn't expose). Use
                            download()+shv_set_config(trigger_edge=1)+
                            shv_arm() directly if you need falling-edge.
            timeout_s:      CLIENT polling timeout (seconds) — see above.
            verify:         confirm the downloaded table's entry count/CRC
                            match before arming (recommended; costs one
                            extra round-trip). Ignored (always effectively
                            True) when `reuse` actually skips the download,
                            since that path already runs verify_schedule()
                            as its own gate.
            reuse:          skip re-download when unchanged — see above.
                            Default False; opt in only under a held lease.
            on_armed:       a function taking NO arguments, called once at the
                            moment the system is READY for the trigger -- see
                            "EXTERNAL TRIGGER" below.

        Returns:
            {"ok": bool, "fired": int, "records": [...], "status": {...},
             "error": str}   # "error" present only when "ok" is False

        EXTERNAL TRIGGER -- when to send it (on_armed)

        With trigger="ext" the pulse fires on YOUR edge, so you need to know
        when the system is ready for it. on_armed is called exactly once, at
        that moment: after the schedule is downloaded and verified, the STM32
        detector armed (measure=True), this filament's controller armed (and
        the master, last), and the check that the filament was not skipped as
        unsafe. An edge sent BEFORE that is lost. After on_armed returns, this
        call waits for the run to finish.

        Same script, a person or another instrument sends the edge:

            import time

            def on_armed():
                print(f"[{time.strftime('%H:%M:%S')}] READY -- send the external trigger now")

            r = ct.fire_single_pulse(filament=8, width_us=1000, trigger="ext",
                                     total_ms=30000,   # edge + whole run within 30 s of ARMING
                                     timeout_s=35.0,   # this script waits a bit longer
                                     measure=True, on_armed=on_armed)   # the function, no ()
            print(r)

        Your own code sends the edge, from another thread:

            import threading

            ready = threading.Event()

            def trigger_source():
                if ready.wait(timeout=60):          # blocks until armed
                    send_my_trigger()               # your code that makes the edge

            threading.Thread(target=trigger_source, daemon=True).start()
            r = ct.fire_single_pulse(filament=8, trigger="ext", total_ms=30000,
                                     timeout_s=35.0, measure=True,
                                     on_armed=ready.set)   # already a no-argument function

        on_armed runs in THIS thread, between arming and waiting: return
        quickly (print, set an Event, notify another program). If it raises,
        the schedule is disarmed and nothing fires ({"ok": False, "error": ...}).
        Timing: total_ms counts from ARMING and bounds both the wait for your
        edge and the whole run (firmware TotalTimeout); inter_pulse_ms only
        applies between pulses once firing has started; timeout_s counts from
        when on_armed returns and should be longer than total_ms.
        examples/external_trigger.py is a complete script.


        measure=True -- ALSO measure the current of every pulse
        ------------------------------------------------------
        Firing and measuring are two different subsystems: the RP2350
        decides WHEN HV fires, and a pulse detector on the STM32 measures
        how much current actually flowed. This flag runs both as one
        operation, which is almost always what you want -- a fired pulse
        you did not measure tells you very little.

            r = ct.fire_single_pulse(5, num_pulses=3, width_us=1000,
                                     measure=True)
            if r["ok"]:
                for e in r["measured"]:
                    print(e["peak_ma"], "mA peak,", e["plateau_ma"], "mA plateau")

        It arms the detector, notes where the event stream is, fires,
        collects exactly the events this fire produced, and releases the
        detector again -- including if the fire raises or times out.

        POST-PULSE BACKGROUND. Each event's post_bg is measured by waiting
        bg_gap_us after the envelope ends (for the analog front end to
        settle) and then averaging over bg_window_us. Both default to the
        firmware's 50 us / 50 us; pass your own when that doesn't fit this
        board. If post_bg comes back looking like the tail of the pulse rather
        than a settled level, the gap is too short. Setting bg_window_us=0
        disables the measurement, and post_bg then reports None -- NOT 0, which
        would be a legal post-pulse current.

            r = ct.fire_single_pulse(5, num_pulses=3, width_us=1000,
                                     measure=True,
                                     bg_gap_us=400,        # let it settle longer
                                     bg_window_us=100)

        With measure=True the result gains:
            "measured": [ ... ]   one event per pulse, each with peak_ma /
                                  plateau_ma / bg_ma (see pulse_events_ma)
            "ref_mv":    float    the live reference reading actually used
        and "ok" becomes stricter: it is True only if the fire succeeded AND
        every fired pulse produced a measured event. A fire that "worked"
        while the detector saw nothing -- link down, detector not really
        armed, events dropped -- reports ok=False rather than letting a
        silent measurement gap look like success.

        Requires the STM32 link to be up; there is exactly ONE detector and
        it lives on the master controller. If it cannot be armed, nothing is
        fired at all and the error says so, so measure=True never leaves you
        guessing whether HV went out.
        """
        # Both backgrounds need gap + window of quiet on each side of the pulse.
        # Fire tighter than that and one pulse's background is measured over its
        # neighbour's tail: no error, no flag, just a biased charge -- which is
        # exactly the failure this whole background pass exists to remove. So it
        # is refused here rather than measured badly.
        #
        # Only when measure=True: firing faster is legitimate when nobody is
        # integrating, and this client has no business dictating pulse spacing
        # for a run whose charge nobody reads.
        if measure and int(inter_pulse_ms) * 1000 < self.MIN_INTER_PULSE_US:
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "measured": [], "ref_mv": None,
                    "error": (f"inter_pulse_ms={inter_pulse_ms} is "
                              f"{int(inter_pulse_ms) * 1000} us, below the "
                              f"{self.MIN_INTER_PULSE_US} us each pulse needs for "
                              f"its backgrounds (gap {self._BG_GAP_US:.0f} + window "
                              f"{self._BG_WINDOW_US:.0f} us on each side). Fire "
                              f"further apart, shorten bg_gap_us/bg_window_us, or "
                              f"pass measure=False if you do not need the charge.")}
        if not measure:
            return self._fire_core(
                filament, num_pulses=num_pulses, width_us=width_us,
                inter_pulse_ms=inter_pulse_ms, max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=timeout_s, verify=verify, reuse=reuse,
                on_armed=on_armed)

        # Arm BEFORE firing -- a detector armed afterwards has already missed
        # the pulses. If it can't arm we fire nothing: silently firing HV that
        # nobody is measuring is the opposite of what measure=True asked for.
        #
        # This arms the RELAY, not just the detector. The STM32 times each pulse
        # from the real envelope on its PA4 pin, and PA4 only moves while the
        # ESP32 is mirroring the RP2350's pulse signal onto it. pulse_arm() alone
        # arms the detector and leaves the relay off, so the detector sits there
        # sampling and never sees a pulse start: measured on hardware, a 3-pulse
        # fire came back "0 of 3 measured" while the RP2350 fired correctly and
        # the STM32 took 7.2M samples. With the relay armed the same fire gives
        # 3 events whose measured widths (1009/1002/1000 us) match the commanded
        # 1000 us. See pulse_arm()'s note for the detector-only form.
        # Size the relay's abandonment TTL from THIS run rather than taking the
        # firmware default. The firmware renews the TTL on every relayed edge,
        # so an active run cannot be reclaimed -- but the renewal is driven by
        # PULSES, so a gap wider than the TTL still looks abandoned. The binding
        # gap is inter_pulse_ms; give it room, and never go below the firmware
        # default. Also covers the head of the run, before the first pulse.
        arm_ttl_ms = max(self._READY_TTL_FLOOR_MS,
                         int(inter_pulse_ms) * self._READY_TTL_GAP_FACTOR,
                         int(timeout_s * 1000))
        arm = self.ready_arm(rate_hz, ttl_ms=arm_ttl_ms,
                             bg_gap_us=bg_gap_us,
                             bg_window_us=bg_window_us)
        if not arm.get("ok"):
            hint = ""
            if "already armed" in str(arm.get("error", "")):
                st = self.ready_status()
                # Armed at the SAME rate is no longer an error (the firmware's
                # resting state is armed), so reaching here means the rate
                # differs -- say which, because "already armed" on its own sends
                # people looking for a stale arm that isn't the problem.
                armed_rate = st.get("rate_hz")
                if armed_rate and int(armed_rate) != int(rate_hz):
                    hint = (f" — the relay is armed at {armed_rate} Hz and this "
                            f"fire asked for {rate_hz} Hz. Measuring at a rate "
                            f"you did not ask for would be worse than failing, "
                            f"so it refuses. ct.ready_disarm() first, or fire at "
                            f"{armed_rate} Hz.")
                else:
                    hint = (" — the relay is already armed. The usual cause is a "
                            "previous run that was KILLED between arming and its "
                            "cleanup (the disarm is in a finally, so an exception "
                            "is fine; SIGKILL is not). If nothing else is using "
                            "it, clear it with ct.ready_disarm(). Not stolen "
                            "automatically: the relay is a single global resource "
                            "with no owner recorded, so another client could be "
                            f"mid-run. Current: {st}")
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "measured": [], "ref_mv": None,
                    "error": f"detector arm failed, nothing fired: "
                             f"{arm.get('error')}{hint}"}
        try:
            # Take the cursor BEFORE firing so we collect only what THIS fire
            # produces and never a stale backlog. Via pulse_cursor() rather
            # than a huge `since`: that shortcut saturates at 2**31-1 in the
            # ESP32's query parsing, and past that id it would stop excluding
            # old events silently. See pulse_cursor().
            since = self.pulse_cursor()
            fired = self._fire_core(
                filament, num_pulses=num_pulses, width_us=width_us,
                inter_pulse_ms=inter_pulse_ms, max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=timeout_s, verify=verify, reuse=reuse,
                on_armed=on_armed)
            measured, ref_mv = self._collect_pulse_events(since, int(num_pulses))
            out = {**fired, "measured": measured, "ref_mv": ref_mv,
                   "ok": bool(fired.get("ok")) and len(measured) >= int(num_pulses)}
            if not out["ok"] and fired.get("ok") and not out.get("error"):
                out["error"] = (f"fired {fired.get('fired')} pulse(s) but the detector "
                                f"reported {len(measured)} of {num_pulses} — measurement "
                                f"incomplete, so the result is not trustworthy")
            return out
        finally:
            self.ready_disarm()

    def _collect_pulse_events(self, since: int, want: int,
                              grace_s: float = 3.0) -> tuple[list, float | None]:
        """Collect `want` detector events newer than `since`. The detector is
        real-time, so these normally arrive immediately after the fire returns;
        the grace period is for a delayed/dropped event, not expected lag.
        Returns whatever it got -- the CALLER decides that a short count is a
        failure, so this can't quietly paper over one."""
        measured: list = []
        ref_mv = None
        deadline = time.monotonic() + grace_s
        cursor = since
        # Backed off rather than hammered: this endpoint is served by the
        # ESP32's config_portal, which shares loop() with the tcp_bridge relay
        # carrying these very pulses (measured 65 ms per request idle,
        # 200-300 ms under load). A tight loop here steals loop() time from the
        # path the events arrive on, so polling harder makes them arrive later.
        naps = self._poll_intervals(0.05, 0.3)
        while len(measured) < want and time.monotonic() < deadline:
            ev = self.pulse_events_ma(cursor)
            if ev.get("ok"):
                ref_mv = ev.get("ref_mv", ref_mv)
                if ev.get("events"):
                    measured.extend(ev["events"])
                    cursor = ev["events"][-1]["id"]
                    naps = self._poll_intervals(0.05, 0.3)   # events flowing: re-arm fast
            if len(measured) < want:
                time.sleep(next(naps))
        return measured, ref_mv

    def _envelope_companion(self, controller: int) -> int | None:
        """The controller that must ALSO run a plan so the STM32 sees its pulse.

        That is the master -- the only board whose RP2350 envelope reaches the
        STM32 -- whenever the pulse is fired somewhere else. None when the
        pulse is on the master already, or when the master is not connected
        (nothing can frame it then; the fire still goes ahead, unmeasured).
        """
        try:
            st = self.status()
        except Exception:
            return None
        master = st.get("master")
        if master is None or int(master) == int(controller):
            return None
        row = (st.get("controllers") or {}).get(str(master)) or {}
        return int(master) if row.get("connected") else None

    def _disarm_all(self, controllers) -> None:
        for c in controllers:
            try:
                self.shv_disarm(c)
            except Exception:
                pass    # best effort: a disarm that fails leaves an armed engine
                        # waiting for a trigger, which the next arm resets

    def _companion_check(self, companion: int, want: int) -> dict:
        """Confirm the master actually ran the plan -- i.e. that a window was
        opened for every pulse. A master that missed a trigger produced no
        envelope for it, and the STM32 would have nothing to frame; reporting
        that as an ordinary unmeasured pulse would send someone looking for a
        fault on the wrong board."""
        st = self.shv_status(companion) or {}
        done = st.get("totalPulsesDone")
        out = {"envelope_triggers": done}
        if done is not None and int(done) != int(want):
            out["envelope_mismatch"] = (
                f"the master (controller {companion}) counted {done} trigger(s) "
                f"for {want} pulse(s) -- the STM32 had no window for the rest, "
                f"so a missing measurement there is not evidence about the pulse")
        return out

    def _fire_core(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,       # RP2350 FIRMWARE's own schedule timeout (ms)
                                      # — see docstring, "total_ms vs timeout_s"
        controller: int | None = None,   # None = auto-infer from `filament` via
                                          # the active-list mapping — see docstring,
                                          # "why controller exists at all"
        trigger: str = "sim",
        timeout_s: float = 15.0,     # PYTHON CLIENT's polling timeout (seconds)
                                      # — see docstring, "total_ms vs timeout_s"
        verify: bool = True,
        on_armed=None,         # callable() run after arming, immediately before
                                # the trigger — the only place a caller can start
                                # something CONCURRENT with the firing
        reuse: bool = False,   # skip re-download if unchanged since your last
                                # call — see docstring, "reuse — skipping the
                                # download when nothing changed"; OFF by
                                # default because it has a real, documented
                                # safety gap (see the docstring) — opt in only
                                # when you understand it.
    ) -> dict:
        """Fire one schedule entry and wait for it. The body of
        fire_single_pulse() -- see that method for the full contract;
        this exists only so the public method can wrap it with the
        detector arm/correlate step without duplicating any of it."""
        if self._is_dead(filament):
            return self._dead_result(filament, {"fired": 0, "records": [], "status": {}})

        if controller is None:
            board = self.filament_to_board(filament)
            if board is None:
                return {"ok": False,
                        "error": f"filament {filament} has no board (unassigned or "
                                f"overflowed past the usable slots) — can't infer "
                                f"controller; pass one explicitly if you meant this",
                        "fired": 0, "records": [], "status": {}}
            controller = board["controller"]

        # max_on_ms/width_us are uint16 fields (0-65535), inter_pulse_ms/
        # total_ms are uint32 — see the wire-format note above shv_set_config.
        err = (self._range_error("num_pulses", int(num_pulses), self._U8_MAX)
               or self._range_error("max_on_ms", int(max_on_ms), self._U16_MAX)
               or self._range_error("width_us", int(width_us), self._U16_MAX)
               or self._range_error("inter_pulse_ms", int(inter_pulse_ms), self._U32_MAX)
               or self._range_error("total_ms", int(total_ms), self._U32_MAX))
        if err:
            return {"ok": False, "error": err, "fired": 0, "records": [], "status": {}}

        # TWO CONTROLLERS. Only the master has the STM32, and the only envelope
        # it sees is its OWN RP2350's ReadyOut. A pulse on another controller is
        # framed by the master running the SAME one-entry plan: that entry is
        # "another controller's filament" to the master, so its PIO runs the
        # full pulse cycle with mask 0x00 -- nothing latches, no HV, but ReadyOut
        # rises and falls for the entry's width (docs/two_controller_operation.md
        # §3). download() already puts the plan on every connected controller;
        # what used to be missing is that only the filament's own controller was
        # ARMED and TRIGGERED, so for a controller-2 filament the master never
        # ran it and the STM32 got no window. That, not a controller-2 fault, is
        # why controller-2 filaments produced no events.
        companion = self._envelope_companion(controller)
        armed_set = [controller] + ([companion] if companion else [])
        self._disarm_all(armed_set)   # cheap (1 frame each); resets engine state
                                      # to Idle WITHOUT touching the schedule
                                      # table -- safe even when reuse is about
                                      # to skip the download.

        plan = {
            # With trigger="sim" THIS CLIENT drives both the trigger and the
            # watchdog that fires if a trigger is late. Setting them to the same
            # value makes the simulated edge race its own deadline: measured on
            # hardware, inter_pulse_ms=300 lost that race on every attempt (2, 3
            # and 4 pulses all faulted with stopReason=inter-pulse timeout after
            # exactly one pulse) while 200 and 500 passed every time. The spacing
            # is the physically meaningful number, so keep the sim at what the
            # caller asked for and give the WATCHDOG room instead. An external
            # trigger is the caller's to time, so it keeps the value as given.
            "config": {"interPulseMs": (int(inter_pulse_ms) * 2 + 100
                                        if trigger == "sim" else int(inter_pulse_ms)),
                       "maxOnMs": int(max_on_ms),
                       "totalMs": int(total_ms), "triggerEdge": 0},
            # USER_INDEX here -- download()/verify_schedule() cross to FID
            # via _plan_to_fids(). This used to call _fid_of() itself, back
            # when download() sent plans raw; doing both would translate twice
            # (a symmetric swap maps straight back to the original). Keep the
            # plan in USER_INDEX; _last_plan holds the FID form and the reuse
            # so shv_pulse_log()'s re-keying still lines up with the
            # "fired"/"records" filter below.
            "emission": [{"filament": int(filament), "numPulses": int(num_pulses),
                         "widthUs": int(width_us)}],
            "heating": [],
        }

        skip_download = False
        # Why this call did or did not re-download, reported in the result. The
        # fast path is only useful if a caller can SEE it working: wall-clock
        # time cannot distinguish "reused the table" from "the download happened
        # to be cheap", and a silent fast path is one nobody can tell has
        # regressed. Values: "downloaded:first-seen" | "downloaded:plan-changed"
        # | "downloaded:crc-mismatch" | "reused:crc-confirmed" | "reuse-not-requested".
        reuse_note = "reuse-not-requested"
        if reuse:
            self._adopt_loaded_schedule()
        # Compare in FID space -- see the note in download(). _plan_to_fids also
        # drops dead filaments, so what is compared is exactly what would be
        # written, not what was asked for.
        reuse_wire, _reuse_dead = self._plan_to_fids(plan) if reuse else ({}, [])
        if reuse and self._last_plan.get(controller) != reuse_wire:
            reuse_note = ("downloaded:first-seen" if controller not in self._last_plan
                          else "downloaded:plan-changed")
        elif reuse and controller not in self._last_crc:
            reuse_note = "downloaded:no-crc-baseline"
        if reuse and self._last_plan.get(controller) == reuse_wire and controller in self._last_crc:
            # Cheap local pre-filter passed (plan unchanged from what WE last
            # wrote) — now confirm against the ACTUAL hardware CRC, not just
            # entry count, so a different actor's same-size schedule can't
            # slip past undetected. See fire_single_pulse's docstring.
            v = self.verify_schedule(plan)
            row = (v.get("results") or {}).get(str(controller)) or {}
            if (row.get("match") and row.get("crc") is not None
                    and row.get("crc") == self._last_crc.get(controller)):
                skip_download = True   # content confirmed byte-identical — go straight to arm
                reuse_note = "reused:crc-confirmed"
            else:
                reuse_note = "downloaded:crc-mismatch"

        if not skip_download:
            dl = self.download(plan)   # updates self._last_plan[controller]; clears any stale crc
            if not dl.get("ok"):
                return {"ok": False, "error": f"download failed: {dl.get('error', dl)}",
                        "fired": 0, "records": [], "status": {}}

            # Always verify when reuse is requested — even if verify=False —
            # since reuse's whole safety mechanism depends on having a fresh
            # CRC baseline to compare against on the NEXT call.
            if verify or reuse:
                v = self.verify_schedule(plan)
                if not v.get("ok"):
                    return {"ok": False, "error": f"schedule verify mismatch after download: {v}",
                            "fired": 0, "records": [], "status": {}}
                if reuse:
                    row = (v.get("results") or {}).get(str(controller)) or {}
                    if row.get("crc") is not None:
                        self._last_crc[controller] = row["crc"]

        arm_r = self.shv_arm(controller, repeats=1)
        if not arm_r.get("ok"):
            code = arm_r.get("reject")
            why = self._SHV_REJECT_NAMES.get(code, "unknown reject code")
            return {"ok": False,
                    "error": f"arm rejected (code {code}): {why}",
                    "arm_reject": code, "arm_reject_name": why,
                    "fired": 0, "records": [], "status": {}, "schedule": reuse_note}
        if companion:
            # The master LAST. It is the head of the trigger chain: once armed,
            # the next edge fires its (empty) pulse and is forwarded on SyncOut
            # to this controller. Arming it first would let an edge in between
            # advance the master past entry 0 while the target was still
            # disarmed -- the two would then disagree about which entry every
            # later trigger belongs to.
            c_arm = self.shv_arm(companion, repeats=1)
            if not c_arm.get("ok"):
                self._disarm_all(armed_set)
                code = c_arm.get("reject")
                why = self._SHV_REJECT_NAMES.get(code, "unknown reject code")
                return {"ok": False,
                        "error": f"the master (controller {companion}) could not be "
                                 f"armed to frame this pulse: arm rejected (code "
                                 f"{code}): {why}. Without it the STM32 gets no "
                                 f"envelope for controller {controller}'s pulse",
                        "arm_reject": code, "arm_reject_name": why,
                        "fired": 0, "records": [], "status": {}, "schedule": reuse_note}

        # A SUCCESSFUL arm can still have silently dropped this filament. Under
        # the CONTINUE fault policy, arm skips a filament that fails its safety
        # gate (IsoOff -- the board's isolated 12 V rail is off), returns
        # reject 0, and runs the rest. The envelope still fires for the counted
        # trigger so the pulse index stays aligned, so the detector records a
        # pulse and every other field looks like a normal shot -- while no HV
        # ever reached the filament.
        #
        # Measured: filament 5 at STOP, arm reject 0, unsafeSlots 0x20 (slot 5),
        # and a measured pulse event. That result was indistinguishable from a
        # real one without this check. SLEEP (iso on) gives unsafeSlots 0.
        st_after = self.shv_status(controller)
        unsafe = st_after.get("unsafeSlots")
        site = self.filament_to_board(filament)
        slot = (site or {}).get("slot")
        if unsafe and slot is not None and (unsafe >> int(slot)) & 1:
            self._disarm_all(armed_set)
            return {"ok": False, "fired": 0, "records": [], "status": st_after,
                    "schedule": reuse_note, "skipped_unsafe": True,
                    "error": f"arm accepted the schedule but SKIPPED filament "
                             f"{filament} (power slot {slot}) as unsafe — its "
                             f"board's isolated 12 V rail is off, so no HV would "
                             f"reach it. The run would still fire the envelope "
                             f"for the counted trigger, so this would otherwise "
                             f"look like a successful shot. Bring the rail up "
                             f"(sleep_one({filament}) is enough — it enables iso "
                             f"without heating current) and fire again."}

        # Everything above is setup -- download, detector arm, schedule arm,
        # safety checks -- and none of it is time-critical. The trigger below
        # is. `on_armed` runs in the gap between the two, which is the only
        # place a caller can start something that must be CONCURRENT with the
        # firing: the RP2350 is armed and waiting, so whatever this does
        # happens while nothing is yet in flight, and the trigger follows
        # immediately after. Used to ramp the heating current across a pulse
        # train -- see emission_ramp(). Its return value is ignored; an
        # exception from it aborts the fire and disarms, rather than leaving a
        # schedule armed on a filament whose state the caller was mid-way
        # through changing.
        if callable(on_armed):
            try:
                on_armed()
            except BaseException as exc:
                self._disarm_all(armed_set)
                self.ready_disarm()
                return {"ok": False, "fired": 0, "records": [], "status": {},
                        "error": f"on_armed raised before the trigger "
                                 f"({type(exc).__name__}: {exc}) — disarmed "
                                 f"without firing"}

        if trigger == "sim":
            # Through the HEAD of the chain. The master's ReadyIn ISR fires the
            # master and forwards the edge on SyncOut, exactly like an external
            # trigger; aimed at the target controller directly, the master
            # would never see it and never open the window.
            r = self._post("/api/sync/simulate", {
                "count": int(num_pulses),
                "interval_ms": float(max(inter_pulse_ms, 10)),
                "controller": int(companion or controller),
            }, timeout=10.0)
            if not r.get("ok"):
                self._disarm_all(armed_set)
                return {"ok": False, "error": f"could not start SyncIn simulation: "
                                              f"{r.get('error', r)}",
                        "fired": 0, "records": [], "status": {}}

        deadline = time.monotonic() + timeout_s
        state = SHV_IDLE
        # This loop runs WHILE the schedule is firing, on the same single
        # RP2350 link that is carrying the run and the 20 fps telemetry push.
        # A flat sleep(0.05) was ~15 requests/s of pure contention (the status
        # round trip is 15 ms, so the sleep was smaller than the request).
        # Fast at first so an immediate fault or a 1-pulse completion is still
        # seen at once, then backing off to 2 requests/s.
        naps = self._poll_intervals(0.05, 0.5)
        while time.monotonic() < deadline:
            st = self.shv_status(controller)
            state = st.get("state", SHV_IDLE)
            if state == SHV_FAULT:
                self._disarm_all(armed_set)
                return {"ok": False,
                        "error": f"SHV fault on controller {controller}: "
                                f"filament {st.get('faultFilament')}, reason "
                                f"{self._SHV_STOP_REASON_NAMES.get(st.get('stopReason'), st.get('stopReason'))}"
                                f" ({st.get('stopReason')})",
                        "fired": 0, "records": [], "status": st, "schedule": reuse_note}
            if state == SHV_COMPLETE:
                logs = self.shv_pulse_log(controller)
                fired = [r for r in logs if r.get("filament") == filament]
                out = {"ok": bool(fired), "fired": len(fired),
                       "records": fired, "status": st, "schedule": reuse_note}
                # HV DID NOT TURN OFF (flags bit 0x02): the OFF read-back came
                # back non-zero. This is the only pulse-log flag that is about
                # the PULSE rather than about the verification of it, and it is
                # the one that matters -- a switch that stayed closed leaves HV
                # on the filament after the pulse. Surfaced at the top level
                # because it was previously invisible: `flags` was a raw byte
                # nobody decoded, so this condition could occur and be reported
                # as a perfectly successful shot.
                stuck = [r.get("filament") for r in fired if r.get("hv_stuck_on")]
                if stuck:
                    out["hv_stuck_on"] = sorted(set(stuck))
                    out["ok"] = False
                    out["error"] = (f"HV DID NOT TURN OFF after the pulse on "
                                    f"filament(s) {sorted(set(stuck))} — the OFF "
                                    f"read-back was non-zero, so the grid switch "
                                    f"may still be closed. Check before firing "
                                    f"again.")
                # HV DID NOT TURN ON (flags bit 0x01): the ON read-back did not
                # match the commanded byte, so the switch the pulse was meant
                # to close was not closed. The trigger was counted and the
                # envelope opened, so everything else -- fired=1, a measured
                # event, a heating current -- looks like a normal shot, and the
                # measured current is of a pulse that never reached the
                # filament. Measured 2026-09-23: filament 50, read165=0,
                # ≈0 mA, reported ok=True.
                no_on = [r for r in fired if r.get("on_mismatch")]
                if no_on:
                    out["on_mismatch"] = sorted({r.get("filament") for r in no_on})
                    out["ok"] = False
                    rb = ", ".join(f"filament {r.get('filament')} read back "
                                   f"{r.get('read165')}" for r in no_on)
                    out["error"] = ((out["error"] + "; ") if out.get("error") else "") + (
                        f"HV DID NOT TURN ON: the ON read-back did not match the "
                        f"commanded switch ({rb}) — the grid switch was not closed, "
                        f"so no HV reached the filament and any measured current "
                        f"is not this filament's")
                # Unverified (0x04) is NOT a failure: the pulse fired, the
                # firmware just has no read-back evidence about it. Reported so
                # a caller can tell "verified good" from "no evidence", which
                # the ok flag alone cannot.
                unver = [r.get("filament") for r in fired if r.get("unverified")]
                if unver:
                    out["unverified"] = sorted(set(unver))
                if companion:
                    out["envelope_from"] = companion
                    out.update(self._companion_check(companion, int(num_pulses)))
                    self._disarm_all([companion])
                return out
            time.sleep(next(naps))

        self._disarm_all(armed_set)
        return {"ok": False, "timeout": True,
                "error": f"timed out after {timeout_s} s (state={state})",
                "fired": 0, "records": [], "status": {}, "schedule": reuse_note}
