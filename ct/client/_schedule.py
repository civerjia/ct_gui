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
import logging
import threading
import warnings
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # editors only: at run time _client.py binds CTClient into this module
    from ._client import CTClient


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

    @staticmethod
    def _name_heating_states(plan: dict) -> tuple[dict, str | None]:
        """Heating rows may name their state -- PowerState.ACTIVE, or "ACTIVE" /
        "active" -- instead of a bare 5. Converted to the number the wire
        carries; an unknown name is an error, never a guess."""
        rows = plan.get("heating")
        if not isinstance(rows, list):
            return plan, None
        out = []
        for n, row in enumerate(rows):
            st = row.get("state") if isinstance(row, dict) else None
            if isinstance(st, str):
                try:
                    st = PowerState[st.strip().upper()]
                except KeyError:
                    return plan, (f"heating[{n}]: unknown state {row['state']!r} -- use "
                                  f"{', '.join(p.name for p in PowerState)} (or PowerState.X)")
            out.append({**row, "state": int(st)} if st is not None else row)
        return {**plan, "heating": out}, None

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
        dead = self._dead_users()   # USER_INDEX view of the physical mask
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
            excluded |= set(self._dead_users())

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
        # Only controllers that TOOK PART: a one-filament shot arms only that
        # filament's controller, and the idle one's 0 edges is not "out of
        # step" (it read as a failure on every single-controller shot).
        # "Took part" = fired something in this report's log, or armed now. A
        # status left "complete" by an EARLIER run is not this run.
        fired_ctrl = {r.get("controller") for r in logs}
        took_part = {c: v for c, v in st_by.items()
                     if c in fired_ctrl or str(c) in {str(x) for x in fired_ctrl}
                     or v.get("state") in (1, 2)}
        if len(took_part) > 1:
            edges = {c: v.get("triggerEdges") for c, v in took_part.items()}
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
            problems.append(f"MOSFET read-back mismatch (OFF, may still be closed) "
                            f"on filament(s) {stuck}")
        mism = sorted({r["filament"] for r in logs if r.get("on_mismatch")})
        if mism:
            problems.append(f"MOSFET read-back mismatch (ON) on filament(s) {mism}")
        dropped_dead = []
        if expected:
            # Filaments the dead mask removed are EXPECTED to be missing -- the
            # plan was built before the filter ran. Reporting them as a
            # shortfall turns a guard doing its job into an alarm, which is
            # exactly the failure mode this report exists to avoid.
            dead = self._dead_users()
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
                 "bus_mV": r.get("heat_bus_mV"), "cmd_mV": r.get("heat_cmd_mV"),
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
                        # state: IDLE / ACTIVE (PowerState, or the name "IDLE"/"ACTIVE")
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
        plan, err = self._name_heating_states(plan)
        if err:
            return {"ok": False, "results": [], "error": err}
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

    def _verify_retrying_timeouts(self, plan: dict, attempts: int = 3) -> dict:
        """verify_schedule(), retried when EVERY failure is a transport
        timeout. The verify is a read -- repeating it changes nothing -- and a
        single timed-out read on a busy link used to throw away a whole
        emission point that had downloaded fine (2800 mA, 2026-09-25: C1 timed
        out on 0x74, C2 matched). A real MISMATCH (a row with match False) is
        never retried: that is what the verify exists to catch."""
        v: dict = {}
        for attempt in range(max(1, attempts)):
            v = self.verify_schedule(plan)
            if v.get("ok"):
                return v
            rows = [r for r in (v.get("results") or {}).values() if not r.get("ok")]
            timeouts_only = bool(rows) and all(
                r.get("match") is None and "time" in str(r.get("error", "")).lower()
                for r in rows)
            if not timeouts_only:
                return v
            time.sleep(0.2)
        return v

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
        plan, err = self._name_heating_states(plan)
        if err:
            return {"ok": False, "results": {}, "error": err}
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


    # ── pulse timing: two numbers that used to share one name ─────────────
    # inter_pulse_ms meant two different things (2026-10-14): with
    # trigger="sim" it was the SPACING of the simulated triggers (and the
    # firmware's timeout was quietly set to 2x+100); with an external trigger
    # it was only the firmware's TIMEOUT -- the longest wait for the next
    # trigger before the whole run faults (stopReason InterPulseTimeout) --
    # and the spacing was whatever the trigger source did. Its docstring even
    # called it a "minimum gap". Now:
    #   pulse_interval_ms   spacing between pulses (sim: what is fired; external:
    #                       optional, what the caller's source will do -- used
    #                       only for the measure check and the detector TTL)
    #   trigger_timeout_ms  longest gap allowed between triggers (firmware
    #                       interPulseMs). Default: sim 2 x interval + 100,
    #                       external 3000.
    # inter_pulse_ms still works and keeps its old meaning per mode; which one
    # it was taken as is logged once per process.
    _DEFAULT_PULSE_INTERVAL_MS = 500       # = CTClient.SHOT_PULSE_INTERVAL_MS
    _DEFAULT_TRIGGER_TIMEOUT_MS = 3000
    # Whole ms, as the field is. The 10 ms floor this used to be came from the
    # backend's old HTTP-per-pulse sim loop; the train is a hardware timer now
    # (us resolution), so the real limits are the pulse width and, measuring,
    # the backgrounds -- both checked where they apply. Spacings that do not
    # divide 1 s need ESP32 firmware with period_us (the backend refuses them
    # on older firmware rather than firing at a different spacing).
    _SIM_MIN_INTERVAL_MS = 1
    # Largest spacing whose default timeout (2 x + 100) still fits the u32 field.
    _MAX_PULSE_INTERVAL_MS = (4294967295 - 100) // 2

    def _pulse_timing(self, trigger: str, inter_pulse_ms=None, pulse_interval_ms=None,
                      trigger_timeout_ms=None) -> dict:
        """{"interval_ms": int | None, "timeout_ms": int, "note": str | None}
        or {"error": str}."""
        sim = str(trigger) == "sim"
        note = None
        if inter_pulse_ms is not None:
            if pulse_interval_ms is not None or trigger_timeout_ms is not None:
                return {"error": "give pulse_interval_ms / trigger_timeout_ms, or the old "
                                 "inter_pulse_ms -- not both"}
            if sim:
                pulse_interval_ms = inter_pulse_ms
                kind = "inter_pulse_ms:sim"
                note = (f"inter_pulse_ms={int(inter_pulse_ms)} taken as pulse_interval_ms "
                        f"(trigger='sim': the spacing of the simulated triggers). Use "
                        f"pulse_interval_ms / trigger_timeout_ms -- the old name means "
                        f"something else with an external trigger")
            else:
                trigger_timeout_ms = inter_pulse_ms
                kind = "inter_pulse_ms:ext"
                note = (f"inter_pulse_ms={int(inter_pulse_ms)} taken as trigger_timeout_ms "
                        f"(external trigger: the longest wait for the next trigger before "
                        f"the run faults -- NOT the spacing, which your trigger source sets). "
                        f"Use trigger_timeout_ms")
        if sim and pulse_interval_ms is None:
            pulse_interval_ms = self._DEFAULT_PULSE_INTERVAL_MS
        if sim and int(pulse_interval_ms) < self._SIM_MIN_INTERVAL_MS:
            return {"error": f"pulse_interval_ms={int(pulse_interval_ms)} is below the "
                             f"{self._SIM_MIN_INTERVAL_MS} ms the simulated trigger can "
                             f"space edges (range {self._SIM_MIN_INTERVAL_MS}.."
                             f"{self._MAX_PULSE_INTERVAL_MS} ms)"}
        if trigger_timeout_ms is None:
            trigger_timeout_ms = (int(pulse_interval_ms) * 2 + 100 if sim
                                  else self._DEFAULT_TRIGGER_TIMEOUT_MS)
        interval = None if pulse_interval_ms is None else int(pulse_interval_ms)
        timeout = int(trigger_timeout_ms)
        for name, v, hi in (("pulse_interval_ms", interval, self._MAX_PULSE_INTERVAL_MS),
                            ("trigger_timeout_ms", timeout, self._U32_MAX)):
            if v is not None and (v < 1 or v > hi):
                return {"error": f"{name}={v} out of range 1..{hi} ms"}
        if interval is not None and timeout <= interval:
            # Measured 2026-10: a timeout equal to the spacing makes every
            # trigger race its own deadline (faulted after one pulse).
            return {"error": f"trigger_timeout_ms={timeout} must be longer than "
                             f"pulse_interval_ms={interval} (it is the longest wait for "
                             f"the NEXT trigger; equal values fault after one pulse)"}
        if note:
            self._log_note_once(kind, note)
        return {"interval_ms": interval, "timeout_ms": timeout}

    def _interval_arg(self, new, old, default: int, old_name: str):
        """(spacing ms, error) for a function that took the spacing under an
        old name: pulse_interval_ms wins, the old name still works, both is
        an error."""
        if new is not None and old is not None:
            return None, f"give pulse_interval_ms or the old {old_name}, not both"
        v = new if new is not None else old if old is not None else default
        return int(v), None

    # The old-name note is logged ONCE per process and kind, not attached to
    # every result: on every shot it buried the real error (2026-10-15, #78).
    _NOTES_LOGGED: set = set()
    _NOTES_LOCK = threading.Lock()

    @classmethod
    def _log_note_once(cls, kind: str, note) -> None:
        if not note:
            return
        with cls._NOTES_LOCK:
            if kind in cls._NOTES_LOGGED:
                return
            cls._NOTES_LOGGED.add(kind)
        logging.getLogger("ct_gui").info(note)

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
                       inter_pulse_ms: int | None = None,  # old name of
                                                     # trigger_timeout_ms
                       max_on_ms: int = 40,          # arm-time safety gate
                                                      # (ms); uint16, 0-65535 --
                                                      # see fire_single_pulse's
                                                      # docstring for the full
                                                      # "checked at arm, not
                                                      # runtime" explanation
                       total_ms: int = 30000,  # FIRMWARE's own schedule
                                                # deadline (ms); uint32
                       trigger_edge: int = 0,  # 0=rising, 1=falling —
                                                # which SyncIn edge
                                                # fires the schedule
                       trigger_timeout_ms: int | None = None) -> dict:
        """Set the SHV schedule engine's timing config. All three timeouts
        are FIRMWARE-side (the RP2350 enforces them itself, independent of
        this Python process):

            trigger_timeout_ms (old name inter_pulse_ms; default 3000) — the
                LONGEST gap allowed between triggers, NOT a spacing: a
                RUNTIME watchdog while Armed/Running: if no
                new SyncIn trigger arrives within this many ms of the last
                one, the RP2350 faults the WHOLE schedule
                (stopReason=InterPulseTimeout, shv_status()["stopReason"])
                — it does not skip/ignore the late trigger, the run stops.
                Raise it for slow manual bench triggers; keep it tight on a
                real cadence so a genuinely stuck run is caught quickly.
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
        if inter_pulse_ms is not None and trigger_timeout_ms is not None:
            return {"ok": False, "error": "give trigger_timeout_ms or inter_pulse_ms, not both"}
        timeout = int(trigger_timeout_ms if trigger_timeout_ms is not None
                      else inter_pulse_ms if inter_pulse_ms is not None
                      else self._DEFAULT_TRIGGER_TIMEOUT_MS)
        err = (self._range_error("max_on_ms", int(max_on_ms), self._U16_MAX)
               or self._range_error("trigger_timeout_ms", timeout, self._U32_MAX)
               or self._range_error("total_ms", int(total_ms), self._U32_MAX))
        if err:
            return {"ok": False, "error": err}
        return self._shv(controller, {
            "op": "set_config",
            "interPulseMs": timeout,
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
        on_mismatch, hv_stuck_on, unverified, heat_meas_mA, heat_target_mA,
        heat_bus_mV, heat_cmd_mV} -- the filament's heating current and
        voltage at the instant that pulse fired (bus = the CC loop's last
        measured load voltage, up to ~2 s old when settled; cmd = the TPS
        setpoint at that instant), or None with a *_unavailable reason
        ("firmware_no_voltage" on RP2350 firmware without the 20-byte record).

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
        return PulseLog(out)

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
                      active_ma: int = 2900,  # paired with `expect` --
                                                        # the ACTIVE current the
                                                        # run-report expects
                      pulse_interval_ms: float | None = None) -> dict:
                                                        # = interval_ms, the
                                                        # library-wide name
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
        if pulse_interval_ms is not None:
            if interval_ms is not None:
                return {"ok": False, "error": "give pulse_interval_ms or interval_ms, not both"}
            interval_ms = pulse_interval_ms
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

    def _arm_failure(self, r: dict) -> tuple:
        """(firmware reject code or None, why) for a failed shv_arm. A refusal
        from the BACKEND (e.g. the controllers disagree on the trigger delay)
        or a transport error carries no firmware code -- its reason is in
        r["error"], which used to be dropped for "unknown reject code"."""
        code = r.get("reject")
        if code is not None:
            return code, self._SHV_REJECT_NAMES.get(code, f"unknown reject code {code}")
        return None, (r.get("error") or "no reason given by the backend")

    class _ScanStop(Exception):
        """scan_view: stop the view here; the message is the result's error."""

    class _ScanEstimate:
        """k="auto"'s numbers, kept across views (one per client session).

        arrive: raise -> "settled" (settle included) of filaments raised from
        IDLE; cycle: one slot's own work -- trigger sent to the next filament's
        turn, WITHOUT any wait for heating. Waiting is left out on purpose: put
        it in and one slow arrival shortens the lead, which makes the next one
        wait longer, which shortens it again.
        Robust to a stray value: arrive uses the 90th percentile of the last
        20, cycle the median of the last 20."""

        def __init__(self, arrive_s: float, cycle_s: float):
            import collections
            self.arrive = collections.deque(maxlen=20)
            self.cycle = collections.deque(maxlen=20)
            self.arrive0, self.cycle0 = float(arrive_s), float(cycle_s)

        @staticmethod
        def _q(vals, q):
            v = sorted(vals)
            return v[min(len(v) - 1, int(q * len(v)))]

        def arrive_s(self) -> float:
            return self._q(self.arrive, 0.9) if len(self.arrive) >= 3 else self.arrive0

        def cycle_s(self) -> float:
            return self._q(self.cycle, 0.5) if len(self.cycle) >= 3 else self.cycle0

    def _scan_retry(self, fn, until_s: float = 10.0, ok=lambda r: bool(r and r.get("ok"))):
        """An IDEMPOTENT call, repeated until it succeeds or until_s runs out (a
        bridge that reconnects takes ~6 s). The last result, or a dict with the
        exception. Never for the trigger -- see _scan_trigger."""
        deadline = time.monotonic() + until_s
        last: dict = {}
        while True:
            try:
                last = fn() or {}
                if ok(last):
                    return last
            except Exception as exc:              # link errors are what this is for
                last = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            if time.monotonic() >= deadline:
                return last
            time.sleep(0.2)

    def _scan_trigger(self, head: int, n: int, interval: int) -> str | None:
        """Send one filament's n simulated triggers EXACTLY once. None = sent,
        else why not. A request that failed is re-sent only when the head's
        count and the sim state both say the first one never started."""
        st = self._scan_retry(lambda: self.shv_status(head),
                              ok=lambda r: r.get("totalPulsesDone") is not None)
        base = st.get("totalPulsesDone")
        if base is None:
            return f"cannot read controller {head}'s trigger count"
        free_by = time.monotonic() + 3.0
        # The backend's previous burst thread outlives the RP2350's count of its
        # edges, and a start while it runs is refused (first rig scan,
        # 2026-10-26: "simulation already running" on the 3rd filament).
        while (self.simulate_sync_status() or {}).get("running") and time.monotonic() < free_by:
            time.sleep(0.02)
        why = None
        for _ in range(3):
            try:
                r = self._post("/api/sync/simulate", {"count": int(n), "interval_ms": float(interval),
                                                      "controller": int(head)}, timeout=10.0)
                if r.get("ok"):
                    return None
                why = r.get("error") or str(r)
            except Exception as exc:
                why = f"{type(exc).__name__}: {exc}"
            time.sleep(0.3)
            # Did it start after all? Then it must not go out twice.
            sim = self.simulate_sync_status() or {}
            now = (self.shv_status(head) or {}).get("totalPulsesDone")
            if sim.get("running") or (now is not None and int(now) > int(base)):
                return None
        return f"simulated trigger failed 3 times: {why}"

    def filament_perf(self, filaments=None) -> dict:
        """The backend's per-filament performance table (what scan_view
        measured): {filament: {n, fired, failed, last_error, last_mv,
        arrive_raw_med_s, arrive_raw_max_s, ohm_med, samples}}, YOUR numbering.
        arrive_raw = raise -> settled with the settle time taken out."""
        fids = [int(self._fid_of(f)) for f in filaments] if filaments is not None else []
        r = self._get("/api/filament-perf" + (f"?filaments={','.join(map(str, fids))}" if fids else ""))
        if not r.get("ok"):
            return r
        return {"ok": True, "filaments": {int(self._user_index_of(int(k))): v
                                          for k, v in (r.get("filaments") or {}).items()}}

    def filament_perf_clear(self, filaments="all") -> dict:
        """Drop the per-filament table's history: "all", or YOUR filament
        numbers. For samples a since-fixed bug measured wrong."""
        which = "all" if filaments == "all" else [int(self._fid_of(f)) for f in filaments]
        return self._post("/api/filament-perf", {"clear": which})

    def scan_view(self,
                  filaments,                   # ONE view, in firing order (your numbering)
                  active_mv,                   # mV for all, or {filament: mV}
                  idle_ma: float,              # back to this after its pulse(s)
                  settle_ms: int = 0,          # hold arrival this long first (firmware-timed)
                  k="auto",                    # "auto", or a fixed number of filaments ahead
                  k_max: int = 6,              # never more than this many hot at once
                  start_in_s: float = 0.0,     # first pulse no sooner than this (motor moving); None = 0
                  detector_center=None,        # filament the detector faces; None = the middle one
                  num_pulses: int = 1,         # pulses per filament
                  width_us: int = 1000,
                  pulse_interval_ms: int = 100,   # spacing inside one filament's burst
                  heat_timeout_s: float = 8.0,    # longest wait for ARRIVAL, per filament
                  lead_margin_s: float = 0.3,     # k="auto": raise this much earlier still
                  max_on_ms: int = 40) -> dict:
        """SOFTWARE SCAN PATCH (2026-10-26, until the new boards): fire ONE CT
        view -- its filaments in order -- in VOLTAGE mode, the host heating
        each just ahead of its turn and firing it only once the RP2350 reports
        its heating ARRIVED. Call it once per view, when the motor STARTS
        moving to that view, with start_in_s = how long the move takes.

          * ONE schedule for the view (every filament an emission entry, no
            heating rows): download, verify and arm at once, during the move;
          * HEATING JUST IN TIME: filament s is raised to a timed VOLTAGE when
            its turn is expected within (its arrival time + lead_margin_s).
            The turn is predicted from the view start (start_in_s), the slots
            before it and the measured time per slot; arrival from what this
            session has measured. k="auto" does that; k=N instead raises a
            filament N turns ahead (old behaviour). Never more than k_max hot.
          * for each filament in order: wait until it is "settled" (load
            voltage +-150 mV, held settle_ms -- CH_ARRIVAL_SETTLE), send its
            num_pulses simulated triggers exactly once, wait until they are
            counted, drop it to idle_ma. The backend allows these mid-run
            changes (in_run) for the running schedule's own filaments only;
          * a prediction that is wrong only costs time: nothing is fired before
            "settled", ever. Link hiccups (reads, VOLTAGE/IDLE commands) are
            retried for 10 s; the trigger is never sent twice;
          * anything that does not recover -- a filament that does not arrive
            within heat_timeout_s (+ settle), an SHV fault, a lost count --
            STOPS the view: disarm, every filament back to idle_ma, settle 0,
            and the reason in "error". Retry or skip is the caller's call.
            (If the caller itself dies, the backend drops the timed VOLTAGE.)

        detector_center: the filament the detector faces in this view (the
        collimator centre). None = the middle of `filaments` (35 -> the 18th).
        Only drawn: the GUI places the detector/collimator there and the beam
        follows the firing filament inside that window. Returned as
        "detector_center".

        Simulated trigger only. Every filament must be at a settled IDLE first.
        Needs RP2350 fw with the voltage loop (903cd23+ for settle_ms > 0).

            for v, fils in enumerate(views):
                motor.start_move(v)
                r = ct.scan_view(fils, active_mv=8000, idle_ma=1200, settle_ms=500,
                                 start_in_s=3.0, num_pulses=5, width_us=20000,
                                 pulse_interval_ms=100)
                print(r)          # rows + r["gantt"]: what actually happened

        Returns {"ok", "error"?, "rows": [{filament, site, mv, raise_s,
        settled_s, trigger_s, fired_s, heated_s, waited_s, arrival, heat_mV, heat_mA,
        records}], "elapsed_s", "start_s", "per_filament_s", "heated_mean_s",
        "waited_mean_s", "estimate": {arrive_s, cycle_s}, "gantt"} -- times in
        s from the call. settled_s = first seen "settled"; heated_s = raise ->
        pulse (time hot); waited_s = settled -> its trigger (hot and only
        waiting: the lead was too long); fired_s = its pulses counted.
        """
        t0 = time.monotonic()
        fils = [int(f) for f in filaments]
        if start_in_s is None:            # None = no motor move to wait for
            start_in_s = 0.0

        def fail(msg, **kw):
            return {"ok": False, "error": msg, "rows": [], **kw}

        if not fils:
            return fail("no filaments")
        if len(set(fils)) != len(fils):
            return fail(f"a filament is listed twice: {fils}")
        dead = [f for f in fils if self._is_dead(f)]
        if dead:
            return fail(f"dead filament(s) {dead} in the view")
        try:
            # Through the remote proxy a dict's int keys arrive as strings.
            mv = {f: int((active_mv[f] if f in active_mv else active_mv[str(f)])
                         if isinstance(active_mv, dict) else active_mv) for f in fils}
        except (KeyError, TypeError, ValueError) as exc:
            return fail(f"active_mv must be a number or give every filament one ({exc!r})")
        center = fils[len(fils) // 2] if detector_center is None else int(detector_center)
        auto = k == "auto"
        try:
            kk = None if auto else max(1, int(k))
        except (TypeError, ValueError):
            return fail(f"k must be 'auto' or a number, got {k!r}")
        k_max = max(1, int(k_max))
        n, interval, settle = int(num_pulses), int(pulse_interval_ms), int(settle_ms or 0)
        err = (self._range_error("num_pulses", n, self._U8_MAX)
               or self._range_error("width_us", int(width_us), self._U16_MAX)
               or self._range_error("max_on_ms", int(max_on_ms), self._U16_MAX))
        if not err and not 0 <= settle <= 60000:
            err = f"settle_ms={settle} out of range 0..60000"
        if not err and interval < self._SIM_MIN_INTERVAL_MS:
            err = f"pulse_interval_ms={interval}: must be >= {self._SIM_MIN_INTERVAL_MS} ms"
        if not err and interval * 1000 <= int(width_us):
            err = f"pulse_interval_ms={interval} is not longer than width_us={width_us}"
        if not err and not 0 <= float(start_in_s) <= 600:
            err = f"start_in_s={start_in_s} out of range 0..600"
        if err:
            return fail(err)
        sites = {f: self.filament_to_board(f) for f in fils}
        nosite = [f for f, s in sites.items() if not s]
        if nosite:
            return fail(f"filament(s) {nosite} have no board")
        ctrls = sorted({int(s["controller"]) for s in sites.values()})
        armed = set(ctrls)
        for c in ctrls:
            comp = self._envelope_companion(c)
            if comp:
                armed.add(comp)
        master = (self.status() or {}).get("master")
        head = int(master) if master is not None and int(master) in armed else ctrls[0]
        armed = sorted(armed)

        burst_s = n * interval / 1000.0
        # Each filament's own arrival history at about this voltage (backend
        # table); the session-wide estimate only for one with too little.
        try:
            hist = (self.filament_perf() or {}).get("filaments") or {}
        except Exception:
            hist = {}
        est = getattr(self, "_scan_est", None)
        if est is None:
            est = self._scan_est = self._ScanEstimate(arrive_s=2.5 + settle / 1000.0,
                                                      cycle_s=burst_s + 0.4)
        wait_s = float(heat_timeout_s) + settle / 1000.0
        # The firmware's trigger watchdog spans the start delay, the wait for
        # heating and our retries (10 s) -- it must never fire first.
        tmo_ms = int((wait_s + 15.0) * 1000) + n * interval
        total_ms = min(self._U32_MAX, int(float(start_in_s) * 1000) + len(fils) * tmo_ms + 10000)
        plan = {"config": {"interPulseMs": tmo_ms, "maxOnMs": int(max_on_ms),
                           "totalMs": total_ms, "triggerEdge": 0},
                "emission": [{"filament": f, "numPulses": n, "widthUs": int(width_us)}
                             for f in fils],
                "heating": []}
        rows = {f: {"filament": f, "site": self._mosfet_site(f).strip(" ()"), "mv": mv[f],
                    "raise_s": None, "settled_s": None, "fired_s": None, "arrival": None}
                for f in fils}
        raised: set = set()
        settle_set = False
        push_on = False
        out: dict = {"ok": False}
        since = lambda: round(time.monotonic() - t0, 3)  # noqa: E731
        start_at = t0 + float(start_in_s)

        def raise_(f):
            r = self._scan_retry(lambda: self._state_one(
                f, VOLTAGE, mv[f], "scan_view",
                extra={"active_s": wait_s + float(start_in_s) + len(fils) * 2.0 + 30.0,
                       "then_idle_ma": int(idle_ma), "in_run": True}))
            if not r.get("ok"):
                # This filament's own problem (board absent, not warm, dead...):
                # named, so scan_views leaves it out and fires the rest.
                out["failed_filament"] = f
                out["failed_why"] = f"VOLTAGE {mv[f]} mV refused: {r.get('error')}"
                raise self._ScanStop(f"filament {f}: {out['failed_why']}")
            raised.add(f)
            rows[f]["raise_s"] = since()

        _pools: dict = {}

        def pool_near(m):
            if m in _pools:
                return _pools[m]
            _pools[m] = sorted(x["arrive_raw_s"] for e in hist.values() for x in (e.get("samples") or [])
                          if x.get("arrive_raw_s") is not None and x.get("mv") is not None
                          and abs(int(x["mv"]) - int(m)) <= 300)
            return _pools[m]

        def arrive_for(f):
            near = sorted(x["arrive_raw_s"] for x in ((hist.get(f) or {}).get("samples") or [])
                          if x.get("arrive_raw_s") is not None and x.get("mv") is not None
                          and abs(int(x["mv"]) - mv[f]) <= 300)
            if len(near) >= 2:
                # 75th percentile: with a handful of samples a 90th IS the
                # maximum, and one long read (2026-10-26) pulled every raise early.
                return near[min(len(near) - 1, int(0.75 * len(near)))] + settle / 1000.0
            # No history of its own: the MEDIAN of every filament's arrivals at
            # about this voltage (user, 2026-10-26). The session's p90 was pulled
            # up by a few slow filaments and raised all the unknown ones early.
            if len(pool_near(mv[f])) >= 3:
                pool = pool_near(mv[f])
                return pool[len(pool) // 2] + settle / 1000.0
            return est.arrive_s()

        def due(j, turn_at):
            """Raise every filament whose turn is close enough. turn_at = when
            filament j (the next to fire) is expected to fire."""
            for s in range(j, len(fils)):
                f = fils[s]
                if f in raised or rows[f]["raise_s"] is not None:
                    continue
                if len(raised) >= k_max:
                    return
                if auto:
                    fire_at = max(turn_at, time.monotonic()) + (s - j) * est.cycle_s()
                    if fire_at - time.monotonic() > arrive_for(f) + float(lead_margin_s):
                        continue          # not yet; a faster-arriving one further on may be
                elif s - j >= kk:
                    return
                raise_(f)

        def watch_settled(extra=()):
            got = self._scan_retry(lambda: self.read_filament_current_cached(sorted(raised | set(extra))),
                                   until_s=2.0, ok=lambda r: bool(r)) if (raised or extra) else {}
            got = {int(g): v for g, v in (got or {}).items() if str(g).lstrip("-").isdigit()}
            for g in raised:
                if (got.get(g) or {}).get("arrival") == "settled" and rows[g]["settled_s"] is None:
                    rows[g]["settled_s"] = since()
                    est.arrive.append(rows[g]["settled_s"] - rows[g]["raise_s"])
            return got

        try:
            self._disarm_all(armed)
            dl = self._scan_retry(lambda: self.download(plan))
            if not dl.get("ok"):
                raise self._ScanStop(f"download failed: {dl.get('error', dl)}")
            v = self._verify_retrying_timeouts(plan)
            if not v.get("ok"):
                raise self._ScanStop(f"schedule verify mismatch after download: {v}")
            if settle:
                r = self._scan_retry(lambda: self.set_arrival_settle(settle))
                if not r.get("ok"):
                    raise self._ScanStop(f"settle_ms={settle}: {r.get('error')}")
                settle_set = True
            arm = self._post("/api/arm", {"repeats": 1, "controllers": armed})
            per = arm.get("results") or {}
            bad = {c: per.get(str(c), arm) for c in armed if not per.get(str(c), arm).get("ok")}
            if bad:
                raise self._ScanStop(f"arm failed: { {c: self._arm_failure(r)[1] for c, r in bad.items()} }")
            # 20 fps live view for the WHOLE view (the GUI), not only per burst.
            push_on = bool((self._post("/api/live-push", {
                "on": True, "view": {"center": int(self._fid_of(center)),
                                     "filaments": [int(self._fid_of(f)) for f in fils]}}) or {}).get("ok"))
            # The move: nothing fires before start_at; the first filaments are
            # raised so that they arrive about then.
            while time.monotonic() < start_at:
                due(0, start_at)
                watch_settled()
                time.sleep(0.05)
            out["start_s"] = since()
            done = 0
            for i, f in enumerate(fils):
                due(i, time.monotonic())
                if f not in raised:
                    raise_(f)             # k_max reached and it is its turn: it goes now
                deadline = t0 + rows[f]["raise_s"] + wait_s
                d: dict = {}
                while True:
                    got = watch_settled((f,))
                    d = got.get(f) or {}
                    arrival = d.get("arrival")
                    if arrival in ("settled", "capped") or d.get("cc_mode") in (2, 3) \
                            or time.monotonic() >= deadline:
                        break
                    due(i, time.monotonic())
                    time.sleep(0.05)
                rows[f]["arrival"] = arrival
                if arrival != "settled":
                    why = ("output faulted" if d.get("cc_mode") in (2, 3) else
                           "capped: the load is short of the voltage at the output's limit"
                           if arrival == "capped" else
                           f"not settled within {wait_s:g} s (arrival {arrival!r})")
                    out["failed_filament"], out["failed_why"] = f, why
                    raise self._ScanStop(f"filament {f} ({rows[f]['site']}) at {mv[f]} mV: "
                                         f"{why} -- view stopped, nothing fired on it")
                if rows[f]["settled_s"] is None:
                    rows[f]["settled_s"] = since()
                t_trig = time.monotonic()
                rows[f]["trigger_s"] = since()
                if push_on:
                    self._post("/api/live-push", {"firing": [int(self._fid_of(f))]})
                why = self._scan_trigger(head, n, interval)
                if why:
                    raise self._ScanStop(f"filament {f}: {why}")
                done += n
                fire_deadline = time.monotonic() + burst_s + 10.0
                while True:
                    # The others keep heating while this one fires: catch the
                    # moment each settles HERE too. Missed until the next turn,
                    # an arrival read 1-1.5 s long (first per-view rig run,
                    # 2026-10-26), the estimate grew, and filaments were raised
                    # ever earlier to wait ever longer.
                    watch_settled()
                    st = self._scan_retry(lambda: self.shv_status(head), until_s=2.0,
                                          ok=lambda r: r.get("state") is not None)
                    if st.get("state") == SHV_FAULT:
                        raise self._ScanStop(
                            f"SHV fault on controller {head} at filament {f}: reason "
                            f"{self._SHV_STOP_REASON_NAMES.get(st.get('stopReason'), st.get('stopReason'))}")
                    if int(st.get("totalPulsesDone") or 0) >= done:
                        break
                    if time.monotonic() >= fire_deadline:
                        raise self._ScanStop(f"filament {f}: controller {head} counted "
                                             f"{st.get('totalPulsesDone')} of {done} triggers")
                    time.sleep(0.05)
                rows[f]["fired_s"] = since()
                if push_on:
                    self._post("/api/live-push", {"firing": []})
                rows[f]["heated_s"] = round(rows[f]["fired_s"] - rows[f]["raise_s"], 3)
                # Settled -> its trigger: the time it was hot only waiting. Not
                # to fired_s, which also holds its own burst and the count check.
                rows[f]["waited_s"] = round(rows[f]["trigger_s"] - rows[f]["settled_s"], 3)
                r = self._scan_retry(lambda: self._state_one(f, IDLE, int(idle_ma), "scan_view",
                                                             extra={"in_run": True}))
                if r.get("ok"):
                    raised.discard(f)
                if i + 1 < len(fils):
                    est.cycle.append(time.monotonic() - t_trig)
            out["ok"] = True
        except self._ScanStop as exc:
            out["error"] = str(exc)
        finally:
            self._disarm_all(armed)
            if push_on:
                try:
                    self._post("/api/live-push", {"on": False})
                except Exception:
                    pass
            for f in sorted(raised):
                r = self._scan_retry(lambda f=f: self.idle_one(f, idle_ma))
                if not r.get("ok"):
                    out.setdefault("idle_errors", {})[f] = r.get("error")
            if settle_set:
                self._scan_retry(lambda: self.set_arrival_settle(0))
        # The heating at each pulse, from the filament's OWN controller's log
        # (the master also logs the entries it only framed).
        for c in ctrls:
            for rec in self.shv_pulse_log(c):
                f = rec.get("filament")
                if f in rows and int(sites[f]["controller"]) == c:
                    rows[f].setdefault("records", []).append(rec)
        fired = [rows[f] for f in fils if rows[f]["fired_s"] is not None]
        for r in fired:
            rec = (r.get("records") or [{}])[0]
            r["heat_mV"], r["heat_mA"] = rec.get("heat_bus_mV"), rec.get("heat_meas_mA")
        recs = [{"filament": int(self._fid_of(r["filament"])), "mv": r["mv"], "settle_ms": settle,
                 "arrive_s": (None if r["settled_s"] is None
                              else round(r["settled_s"] - r["raise_s"], 3)),
                 "heat_mV": r.get("heat_mV"), "heat_mA": r.get("heat_mA"),
                 "waited_s": r.get("waited_s")} for r in fired]
        if out.get("failed_filament") is not None:
            ff = out["failed_filament"]
            recs.append({"filament": int(self._fid_of(ff)), "mv": mv[ff], "settle_ms": settle,
                         "error": out.get("failed_why")})
        if recs:
            try:
                pr = self._post("/api/filament-perf", {"records": recs})
                if not pr.get("ok"):
                    out["perf_error"] = pr.get("error")
            except Exception as exc:
                out["perf_error"] = str(exc)
        mean = lambda key: (round(sum(r[key] for r in fired) / len(fired), 3)  # noqa: E731
                            if fired else None)
        out.update(rows=[rows[f] for f in fils], fired=len(fired), elapsed_s=since(),
                   heated_mean_s=mean("heated_s"), waited_mean_s=mean("waited_s"),
                   per_filament_s=(round((since() - out["start_s"]) / len(fired), 2)
                                   if fired and out.get("start_s") is not None else None),
                   settle_ms=settle, k=k, detector_center=center, estimate={"arrive_s": round(est.arrive_s(), 3),
                                                     "cycle_s": round(est.cycle_s(), 3)},
                   gantt=self._scan_gantt([rows[f] for f in fils], out.get("start_s")))
        return out

    def scan_plan(self, mode: str = "stationary", **geometry) -> dict:
        """The views of one CT rotation, ready for scan_views().

        Each view: {"view": n, "center": the filament the detector faces,
        "filaments": the ones it fires, in order}. Filaments are YOUR numbers,
        which are the ring positions (the backend's filament order maps them to
        boards). Dead filaments are already left out. `mode` and the geometry
        keywords are build_scan_schedule()'s (mode="precision" adds gantry
        angles: many more views).

            plan = ct.scan_plan()
            plan["views"][0]   # {'view': 0, 'center': 0, 'filaments': [79, 80, ... 17]}
        """
        sched = self.build_scan_schedule(mode=mode, **geometry)
        views: list = []
        key = None
        for r in sched["emission"]:
            k = (r["ringStep"], r.get("gantry"))
            if k != key:
                views.append({"view": len(views), "center": int(r["coll"]),
                              "filaments": []})
                if mode == "precision":
                    views[-1]["gantry"] = r.get("gantry")
                key = k
            views[-1]["filaments"].append(int(r["filament"]))
        return {"ok": True, "n_views": len(views), "dead_left_out": sched.get("excluded"),
                "truncated": sched.get("truncated"), "views": views}

    def scan_views(self,
                   views,                    # scan_plan()["views"] (or a slice of it)
                   active_mv,                # heating voltage (mV): one number or {filament: mV}
                   idle_ma: float,           # back to this after each filament's pulses
                   settle_ms: int = 500,     # hold the voltage this long before firing
                   num_pulses: int = 1,      # pulses per filament
                   width_us: int = 1000,
                   pulse_interval_ms: int = 100,
                   move_s: float = 3.0,      # how long the motor takes to reach a view; None = 0
                   move_to=None,             # move_to(view): STARTS the motor move; None = no motor
                   heat_timeout_s: float = 8.0,
                   k="auto") -> dict:
        """SOFTWARE SCAN PATCH (2026-10-26, until the new boards): fire many
        views in a row, VOLTAGE heating just in time. For each view:

          1. move_to(view) is called (if given) -- it should START the motor
             and return; the view's first pulse comes move_s later, and the
             first filaments are heated during the move so they are ready then;
          2. scan_view() fires the view (see it for the heating rules);
          3. a filament that does not reach its voltage (or is refused it: board
             absent, not warm) is left out of the rest
             of the run, and the view's filaments NOT YET FIRED are fired once
             more without it; any other failure STOPS the run (nothing is
             heated for nothing).

        Ctrl-C, or the script dying, stops it safely: the current view is
        disarmed and its filaments go back to idle_ma. Ends at idle_ma.

            plan = ct.scan_plan()
            r = ct.scan_views(plan["views"][:10], active_mv=5000, idle_ma=1200,
                              num_pulses=5, width_us=20000, pulse_interval_ms=100,
                              move_s=3.0, move_to=motor.start_move)
            print(r)           # one line per view

        Before the first view, filaments whose board gives no current reading
        are left out (with the reason). Returns {"ok", "error"?, "views_done",
        "views_total", "left_out", "left_out_why", "pulses", "elapsed_s", "per_view": [{view, center, ok, fired, n,
        pulses, s, hot_s, waited_s, error?}]}. Each pulse's heating V/I is in
        filament_perf() (and in scan_view()'s own result, view by view).
        """
        t0 = time.monotonic()
        left_out: set = set()
        why_out: dict = {}
        per: list = []
        pulses = 0
        out: dict = {"ok": True}
        # Up front: a filament whose board gives no current reading cannot be
        # heated -- left out now, not discovered mid-view.
        every = sorted({int(f) for v in views for f in v["filaments"]})
        try:
            now = self.read_filament_current_cached(every) or {}
        except Exception:
            now = {}
        for f in every:
            if now and (now.get(f) or {}).get("current_mA") is None:
                left_out.add(f)
                why_out[f] = "no current reading before the run (board absent?)"
        for v in views:
            done_here: set = set()
            for attempt in (1, 2):
                # A retry fires only what this view has NOT fired yet: the rest
                # already has its pulses, heating it again would be for nothing.
                fils = [f for f in v["filaments"] if f not in left_out and f not in done_here]
                if callable(move_to) and attempt == 1:
                    move_to(v)
                r = self.scan_view(fils, active_mv=active_mv, idle_ma=idle_ma,
                                   settle_ms=settle_ms, k=k,
                                   start_in_s=float(move_s or 0.0) if attempt == 1 else 0.0,
                                   detector_center=v.get("center"), num_pulses=num_pulses,
                                   width_us=width_us, pulse_interval_ms=pulse_interval_ms,
                                   heat_timeout_s=heat_timeout_s)
                n_p = sum(len(x.get("records") or []) for x in (r.get("rows") or []))
                done_here |= {x["filament"] for x in (r.get("rows") or [])
                              if x.get("fired_s") is not None and "filament" in x}
                pulses += n_p
                row = {"view": v.get("view"), "center": v.get("center"), "try": attempt,
                       "ok": bool(r.get("ok")), "fired": r.get("fired"), "n": len(fils),
                       "pulses": n_p, "s": r.get("elapsed_s"),
                       "hot_s": r.get("heated_mean_s"), "waited_s": r.get("waited_mean_s")}
                if not r.get("ok"):
                    row["error"] = r.get("error")
                per.append(row)
                if r.get("ok"):
                    break
                ff = r.get("failed_filament")
                if ff is None or attempt == 2:
                    out.update(ok=False, error=f"view {v.get('view')}: {r.get('error')} -- run stopped")
                    break
                left_out.add(int(ff))
                why_out[int(ff)] = f"view {v.get('view')}: {r.get('failed_why') or r.get('error')}"
            if not out["ok"]:
                break
        out.update(views_done=sum(1 for p in per if p["ok"]), views_total=len(views),
                   left_out=sorted(left_out),
                   left_out_why={f: why_out.get(f, "") for f in sorted(left_out)}, pulses=pulses,
                   elapsed_s=round(time.monotonic() - t0, 1), per_view=per)
        return out

    @staticmethod
    def _scan_gantt(rows, start_s, width: int = 64) -> str:
        """What actually happened, one line per filament on a shared time axis:
        '.' raised, still heating; '=' settled, waiting for its turn; '#' its
        pulses; '|' the view start (motor in position)."""
        ends = [r[k] for r in rows for k in ("fired_s", "settled_s", "raise_s") if r.get(k) is not None]
        if not ends:
            return ""
        t_end = max(ends) + 0.5
        col = lambda t: min(width - 1, int(t / t_end * width))  # noqa: E731
        lines = [f"0 s{' ' * (width - 6)}{t_end:.1f} s"]
        for r in rows:
            line = [" "] * width
            a, s, f = r.get("raise_s"), r.get("settled_s"), r.get("fired_s")
            if a is not None:
                for c in range(col(a), col(s if s is not None else (f or t_end))):
                    line[c] = "."
            if s is not None:
                for c in range(col(s), col(f if f is not None else t_end)):
                    line[c] = "="
            if f is not None:
                line[col(f)] = "#"
            if start_s is not None and line[col(start_s)] == " ":
                line[col(start_s)] = "|"
            lines.append(f"{r['filament']:>3} {''.join(line)}")
        return "\n".join(lines)

    def fire_single_pulse(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        inter_pulse_ms: int | None = None,   # OLD NAME, ambiguous -- use
                                              # pulse_interval_ms (sim spacing)
                                              # or trigger_timeout_ms; see
                                              # _pulse_timing
        max_on_ms: int = 40,
        total_ms: int = 15000,       # RP2350 FIRMWARE's own schedule timeout (ms)
                                      # — see docstring, "total_ms vs shot_wait_timeout_s"
        controller: int | None = None,   # None = auto-infer from `filament` via
                                          # the active-list mapping — see docstring,
                                          # "why controller exists at all"
        trigger: str = "sim",
        shot_wait_timeout_s: float = 15.0,   # shot_wait's timeout: how long THIS
                                      # script waits, from READY (armed), for
                                      # the trigger and the run to finish (s) --
                                      # see docstring, "total_ms vs shot_wait_timeout_s"
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
        sim_delay_ms=None,           # REMOVED 2026-10-26: passing it is an
                                      # error -- use settle_ms (the firmware
                                      # holds on_armed until the heating has
                                      # stayed arrived that long)
        active_ma: float | None = None,  # heat to ACTIVE at this current
                                          # only AFTER the arm, fire, and
                                          # drop to idle_ma at once -- see
                                          # "active_ma" in the docstring
        idle_ma: float | None = None,    # active_ma only: IDLE current to
                                          # return to right after the pulse
        heat_timeout_s: float = 5.0,     # longest wait for the heating to
                                          # ARRIVE (ACTIVE current, or the
                                          # VOLTAGE within +-150 mV); not
                                          # arrived = not fired
        settle_ms: int | None = None,    # active_ma/active_mv only: the
                                          # FIRMWARE holds "settled" (and so
                                          # on_armed + the trigger) until the
                                          # heating has stayed arrived this
                                          # long -- see "settle_ms" below
        active_mv: float | None = None,  # INSTEAD of active_ma: heat at this
                                          # VOLTAGE (no CC loop) -- see
                                          # "active_mv" in the docstring
        voltage_settle_s: float | None = None,  # REMOVED: ignored with a
                                          # warning -- active_mv waits for
                                          # arrival, up to heat_timeout_s
        timeout_s: float | None = None,  # old name of shot_wait_timeout_s, still accepted
        pulse_interval_ms: int | None = None,   # spacing between pulses (ms):
                                                 # what trigger="sim" fires.
                                                 # Default 3000. Range: sim
                                                 # 10..2147483597; must be >
                                                 # width_us/1000; (num_pulses-1)
                                                 # x it must fit total_ms;
                                                 # measure=True needs >= 0.5.
                                                 # External: optional, only
                                                 # what your source will do.
        trigger_timeout_ms: int | None = None,  # LONGEST wait for the next
                                                 # trigger before the run faults
                                                 # (ms). Range 1..4294967295,
                                                 # must be > pulse_interval_ms.
                                                 # Default: sim 2 x interval +
                                                 # 100, external 3000.
    ) -> dict:
        """Download a one-entry schedule, arm it, fire, and verify.

        active_ma: keep the filament at ACTIVE only for the shot. Without it
        the caller raises ACTIVE first and every setup step below (detector
        arm, schedule check/download, arm, safety read) runs while the
        filament sits at firing current -- 4-9 s per filament measured
        2026-10-07, against a 2-3 s heat-up. With it, the filament must be at
        IDLE; everything is prepared at IDLE, ACTIVE is raised after the arm
        (and verified, up to heat_timeout_s: a current that has not arrived
        by then means NOT fired, ok=False), then on_armed is called and the
        trigger follows at once,
        and the filament goes back to idle_ma as soon as the run is over --
        before the pulse log and the detector events are read. ACTIVE time
        is then the heat-up plus the pulses. The firmware's total_ms clock
        starts at ARM, so heat_timeout_s is added to it here. As a backstop
        the ACTIVE is timed in the backend (active_s), so a client that dies
        mid-shot still drops back to idle_ma.

        active_mv: the same, but the heat-up is VOLTAGE mode at active_mv mV
        instead of ACTIVE at a current. Closed loop on the LOAD voltage (RP2350
        fw feat/pulse-instant-heat): the firmware ramps at the slew, then reads
        the INA219 bus voltage and trims the output until the measured load
        voltage is within +-150 mV of active_mv -- ARRIVED, reported like the CC
        loop's convergence. After the arm the filament goes to a TIMED voltage
        (backend: IDLE-first guard, dead-man, back to idle_ma); on_armed and the
        trigger wait for that arrival exactly as they wait for an ACTIVE
        current, up to heat_timeout_s -- not arrived = not fired. The current
        it settles at is in every pulse record (heat_meas_mA with heat_bus_mV),
        which is the data for choosing each filament's voltage. Heating is host
        software in both modes; the firmware schedule fires and records.
        Give active_ma OR active_mv.

            ct.fire_single_pulse(f, active_mv=7600, idle_ma=1200, measure=True)

        settle_ms: with active_ma or active_mv -- how long the heating must STAY
        arrived before on_armed is called and the trigger goes out, timed by the
        RP2350 (CH_ARRIVAL_SETTLE, fw 903cd23+): the board reads arrival
        "settling" until it has held the ACTIVE current (CC converged) or the
        voltage (+-150 mV) for settle_ms continuously, then "settled"; dropping
        out restarts the clock. So an external trigger source can fire blindly
        on on_armed. 0..60000 ms; written to every connected controller for
        this shot and set back to 0 after it. heat_timeout_s is the wait for
        ARRIVAL; settle_ms is added on top of it. Older firmware: error, not
        fired.

            ct.fire_single_pulse(f, active_mv=8000, idle_ma=1200, settle_ms=1500,
                                 trigger="ext", on_armed=tell_the_source)

        sim_delay_ms: REMOVED (2026-10-26). It was a host-side sleep between
        arrival and the simulated trigger; settle_ms does it in the firmware for
        every trigger source. Passing it returns an error, nothing is done.

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

        `total_ms` vs `shot_wait_timeout_s` — two DIFFERENT timeouts, on two DIFFERENT
        machines, watching two DIFFERENT things:

            total_ms   -> lives on the RP2350. Downloaded as part of the
                          schedule config. The FIRMWARE's own deadline for
                          the whole armed schedule (from arm to the last
                          pulse) — if exceeded, the RP2350 itself declares
                          the schedule timed out/faulted, independent of
                          whether Python is even still watching.
            shot_wait_timeout_s -> lives in THIS Python process; counts from
                          READY (armed), so with an external trigger it
                          includes waiting for the edge. How long the local
                          while-loop below keeps polling shv_status() over
                          HTTP before giving up and returning
                          {"ok": False, "timeout": True, ...} on its own —
                          independent of what the RP2350 is doing. Python
                          could give up on a schedule that's still running
                          fine on the hardware, or keep polling a schedule
                          the RP2350 already abandoned.

        Rule of thumb: shot_wait_timeout_s should be a bit LARGER than total_ms/1000,
        so Python doesn't give up right before the firmware would have
        reported COMPLETE/FAULT on its own — e.g. total_ms=40000 (40 s)
        pairs with shot_wait_timeout_s=45.0, not shot_wait_timeout_s=15.0 (the default, sized
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
            pulse_interval_ms: spacing between pulses (ms). trigger="sim"
                            fires at this spacing (default 3000). External
                            trigger: your source sets the spacing; pass it
                            only so the measure check / detector TTL know it.
            trigger_timeout_ms: the LONGEST wait for the next trigger (ms) —
                            a RUNTIME watchdog: no trigger within this long
                            faults the WHOLE run (stopReason=
                            InterPulseTimeout), it does not skip a late one.
                            Default: sim 2 x pulse_interval_ms + 100,
                            external 3000. Must exceed pulse_interval_ms.
            inter_pulse_ms: OLD NAME, still accepted with its old meaning per
                            mode — sim: pulse_interval_ms; external:
                            trigger_timeout_ms. Which one is logged once
                            per process (ct_gui log), not put in the result.

        Limits (refused with ok=False and the reason, never silently changed):
            num_pulses          1 .. 255                  (one byte on the wire)
            width_us            1 .. 65535 us             (u16); also <= max_on_ms x 1000
            max_on_ms           0 .. 65535 ms             (u16); checked at arm
            pulse_interval_ms   sim: 1 .. 2147483597 ms   (exact; spacings that do
                                not divide 1 s need ESP32 fw with period_us);
                                > width_us/1000; with
                                measure=True >= 0.5 ms (backgrounds need
                                2 x (bg_gap 200 + window 50) us)
            trigger_timeout_ms  1 .. 4294967295 ms        (u32); > pulse_interval_ms
            total_ms            1 .. 4294967295 ms        (u32); sim: must exceed
                                (num_pulses - 1) x pulse_interval_ms + width
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
            shot_wait_timeout_s: seconds this script waits, from READY (armed), for
                            the trigger and the run to finish (old name:
                            timeout_s) — see above.
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
            # ok True can carry "warning" + "readback_unreliable": True -- the
            # grid MOSFET read-back (74HC165) was wrong on some pulses, but every
            # one of them measured current, so the MOSFET did close and the data
            # are good (measure=True only; unmeasured it stays ok False).

        EXTERNAL TRIGGER -- when to send it (on_armed)

        With trigger="ext" the pulse fires on YOUR edge, so you need to know
        when the system is ready for it. on_armed is called exactly once, at
        that moment: after the schedule is downloaded and verified, the STM32
        detector armed (measure=True), this filament's controller armed (and
        the master, last), and the check that the filament was not skipped as
        unsafe -- and, with active_ma, after the ACTIVE current has been
        measured at its target (so the pulse never lands mid-ramp). An edge
        sent BEFORE that is lost, or fires an unheated filament. After on_armed returns, this
        call waits for the run to finish.

        Same script, a person or another instrument sends the edge:

            import time

            def on_armed():
                print(f"[{time.strftime('%H:%M:%S')}] READY -- send the external trigger now")

            r = ct.fire_single_pulse(filament=8, width_us=1000, trigger="ext",
                                     total_ms=30000,   # edge + whole run within 30 s of ARMING
                                     shot_wait_timeout_s=35.0,   # this script waits a bit longer
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
                                     shot_wait_timeout_s=35.0, measure=True,
                                     on_armed=ready.set)   # already a no-argument function

        on_armed runs in THIS thread, between arming and waiting: return
        quickly (print, set an Event, notify another program). If it raises,
        the schedule is disarmed and nothing fires ({"ok": False, "error": ...}).
        Timing: total_ms counts from ARMING and bounds both the wait for your
        edge and the whole run (firmware TotalTimeout); inter_pulse_ms only
        applies between pulses once firing has started; shot_wait_timeout_s counts from
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
        if timeout_s is not None:        # the old name wins if a script passes it
            shot_wait_timeout_s = timeout_s
        timing = self._pulse_timing(trigger, inter_pulse_ms, pulse_interval_ms,
                                    trigger_timeout_ms)
        if timing.get("error"):
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "error": timing["error"]}
        interval, tmo = timing["interval_ms"], timing["timeout_ms"]

        # Both backgrounds need gap + window of quiet on each side of the pulse.
        # Fire tighter than that and one pulse's background is measured over its
        # neighbour's tail: no error, no flag, just a biased charge -- which is
        # exactly the failure this whole background pass exists to remove. So it
        # is refused here rather than measured badly.
        #
        # Only when measure=True: firing faster is legitimate when nobody is
        # integrating, and this client has no business dictating pulse spacing
        # for a run whose charge nobody reads.
        if measure and interval is not None and interval * 1000 < self.MIN_INTER_PULSE_US:
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "measured": [], "ref_mv": None,
                    "error": (f"pulse_interval_ms={interval} is "
                              f"{interval * 1000} us, below the "
                              f"{self.MIN_INTER_PULSE_US} us each pulse needs for "
                              f"its backgrounds (gap {self._BG_GAP_US:.0f} + window "
                              f"{self._BG_WINDOW_US:.0f} us on each side). Fire "
                              f"further apart, shorten bg_gap_us/bg_window_us, or "
                              f"pass measure=False if you do not need the charge.")}
        if active_ma is not None and active_mv is not None:
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "error": "give active_ma OR active_mv, not both"}
        if sim_delay_ms is not None:
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "error": "sim_delay_ms was removed (2026-10-26): use settle_ms -- the "
                             "RP2350 then holds on_armed and the trigger until the heating "
                             "has stayed arrived that long, for any trigger source"}
        settle = None
        if settle_ms is not None:
            if active_ma is None and active_mv is None:
                return {"ok": False, "fired": 0, "records": [], "status": {},
                        "error": "settle_ms needs active_ma or active_mv: it is a wait AFTER "
                                 "the heating arrives, and without them this call does not heat"}
            settle = int(settle_ms)
            if not 0 <= settle <= 60000:
                return {"ok": False, "fired": 0, "records": [], "status": {},
                        "error": f"settle_ms={settle_ms} out of range 0..60000 ms"}
            r = self.set_arrival_settle(settle)
            if not r.get("ok"):
                return {"ok": False, "fired": 0, "records": [], "status": {},
                        "error": f"settle_ms={settle}: could not set the firmware's arrival "
                                 f"settle time: {r.get('error')} -- not fired"}
            heat_timeout_s = float(heat_timeout_s) + settle / 1000.0
        try:
            res = self._fire_single_pulse_run(
                filament, num_pulses, width_us, interval, tmo, max_on_ms, total_ms,
                controller, trigger, shot_wait_timeout_s, verify, reuse, measure,
                rate_hz, bg_gap_us, bg_window_us, on_armed, active_ma, idle_ma,
                heat_timeout_s, active_mv, voltage_settle_s, settle is not None)
        finally:
            if settle:
                try:
                    self.set_arrival_settle(0)
                except Exception as exc:
                    logging.getLogger("ct_gui").warning(
                        "fire_single_pulse: arrival settle not set back to 0: %s", exc)
        if settle is not None and isinstance(res, dict):
            res["settle_ms"] = settle
        return res

    def _fire_single_pulse_run(self, filament, num_pulses, width_us, interval, tmo,
                               max_on_ms, total_ms, controller, trigger,
                               shot_wait_timeout_s, verify, reuse, measure, rate_hz,
                               bg_gap_us, bg_window_us, on_armed, active_ma, idle_ma,
                               heat_timeout_s, active_mv, voltage_settle_s,
                               fast_poll) -> dict:
        """fire_single_pulse's dispatch: heated by voltage, heated by current,
        or unheated. Arguments already validated by fire_single_pulse."""
        if active_mv is not None:
            if voltage_settle_s is not None:
                # The fixed settle sleep is gone: active_mv now waits for the
                # firmware's voltage arrival. Accepted, ignored, said.
                logging.getLogger("ct_gui").warning(
                    "fire_single_pulse: voltage_settle_s=%s is ignored -- active_mv now "
                    "waits for the voltage to ARRIVE (+-150 mV), up to heat_timeout_s=%s",
                    voltage_settle_s, heat_timeout_s)
                warnings.warn("voltage_settle_s is ignored: active_mv waits for the "
                              "voltage to arrive, up to heat_timeout_s", stacklevel=3)
            res = self._fire_heated(
                filament, None, idle_ma, float(heat_timeout_s),
                dict(num_pulses=num_pulses, width_us=width_us,
                     pulse_interval_ms=interval, trigger_timeout_ms=tmo,
                     max_on_ms=max_on_ms,
                     total_ms=total_ms, controller=controller, trigger=trigger,
                     timeout_s=shot_wait_timeout_s, verify=verify, reuse=reuse,
                     measure=measure, rate_hz=rate_hz, bg_gap_us=bg_gap_us,
                     bg_window_us=bg_window_us, on_armed=on_armed),
                active_mv=active_mv, fast_poll=fast_poll)
        elif active_ma is not None:
            res = self._fire_heated(
                filament, active_ma, idle_ma, heat_timeout_s,
                dict(num_pulses=num_pulses, width_us=width_us,
                     pulse_interval_ms=interval, trigger_timeout_ms=tmo,
                     max_on_ms=max_on_ms,
                     total_ms=total_ms, controller=controller, trigger=trigger,
                     timeout_s=shot_wait_timeout_s, verify=verify, reuse=reuse,
                     measure=measure, rate_hz=rate_hz, bg_gap_us=bg_gap_us,
                     bg_window_us=bg_window_us, on_armed=on_armed))
        else:
            res = self._fire_single(
                filament, num_pulses=num_pulses, width_us=width_us,
                pulse_interval_ms=interval, trigger_timeout_ms=tmo, max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=shot_wait_timeout_s, verify=verify, reuse=reuse, measure=measure,
                rate_hz=rate_hz, bg_gap_us=bg_gap_us, bg_window_us=bg_window_us,
                on_armed=on_armed)
        return res

    # active_one(verify=True)'s default tolerance: "within this of the target".
    _HEAT_BAND_MA = 150.0

    @classmethod
    def _heat_in_band(cls, h: dict, target_ma: float) -> bool:
        """A heating result that timed out but whose last REAL measurement is
        inside the band. Never for a fault/short verdict or a missing reading
        (a 0.0 filler must not count)."""
        if h.get("faulted") or h.get("cannot_start") or not h.get("measured_valid"):
            return False
        m = h.get("measured_ma")
        return m is not None and abs(float(m) - float(target_ma)) <= cls._HEAT_BAND_MA

    def _fire_heated(self, filament: int, active_ma: float | None, idle_ma,
                     heat_timeout_s: float, kw: dict, active_mv: float | None = None,
                     fast_poll: bool = False) -> dict:
        """fire_single_pulse(active_ma=... | active_mv=...): prepare at IDLE,
        heat only between the arm and the end of the run. See its docstring.
        With active_mv, heat_timeout_s is the voltage settle time."""
        if idle_ma is None:
            return {"ok": False, "fired": 0, "records": [], "status": {},
                    "error": f"{'active_mv' if active_mv is not None else 'active_ma'} "
                             f"needs idle_ma: the IDLE current to drop "
                             "back to as soon as the pulse is done"}
        user_on_armed = kw.pop("on_armed", None)
        heat = {"raised": False, "dropped": False, "active": None, "idle": None,
                "t_active": None, "t_idle": None}

        def raise_active():
            # The caller's on_armed means "send the trigger NOW", so it runs
            # only after the ACTIVE current has ARRIVED -- calling it before
            # active_one (as until 2026-10-11) let an external edge fire the
            # pulse mid-ramp, on a filament still heating up.
            heat["raised"] = True        # from here on, the finally drops it
            heat["t_active"] = time.monotonic()
            if active_mv is not None:
                raise_voltage()
                if callable(user_on_armed):
                    user_on_armed()
                return
            # Backstop in the BACKEND: held while the run is armed/running,
            # applied when it ends -- so a client that dies after this line
            # still returns the filament to idle_ma.
            r = self.active_one(filament, active_ma, verify=True,
                                timeout_s=heat_timeout_s,
                                active_s=float(heat_timeout_s) + float(kw["timeout_s"]) + 5.0,
                                then_idle_ma=idle_ma)
            heat["active"] = r
            if not r.get("ok"):
                # Refused (not at IDLE, dead, ...): ACTIVE never applied, so
                # there is nothing to drop -- an IDLE here could even heat a
                # filament that was below IDLE. An exception above (outcome
                # unknown) keeps "raised" and gets the drop.
                heat["raised"] = False
                raise RuntimeError(f"ACTIVE {active_ma} mA not accepted: "
                                   f"{r.get('error') or r}")
            # Accepted is not arrived: verify's wait gives up after
            # heat_timeout_s with r["ok"] still True. Do not fire a filament
            # that never reached firing current (the finally drops it).
            h = r.get("heating") or {}
            if not h.get("ok") and h.get("arrival") != "settling" and self._heat_in_band(h, active_ma):
                # Near miss, rescued: the loop still says "ramping" at the
                # deadline but the measured current is inside the band
                # (2026-10-11: 4 of 1265 shots refused at 2532-2575 mA of 2600).
                # wait_for_current lets "ramping" overrule an in-band sample
                # because a cold IDLE inrush passes THROUGH the band in the
                # first ~0.4 s -- not a concern at the end of heat_timeout_s.
                h = {**h, "ok": True, "accepted_in_band": True,
                     "note": f"loop still '{h.get('arrival')}' at {heat_timeout_s} s, "
                             f"but {h.get('measured_ma')} mA is within "
                             f"{self._HEAT_BAND_MA:.0f} mA of {active_ma} -- fired"}
                r = {**r, "heating": h}
                heat["active"] = r
            if not h.get("ok"):
                raise RuntimeError(
                    f"ACTIVE {active_ma} mA not reached within {heat_timeout_s} s "
                    f"(measured {h.get('measured_ma')} mA"
                    f"{': ' + str(h['error']) if h.get('error') else ''}) -- not fired")
            if callable(user_on_armed):
                user_on_armed()

        def raise_voltage():
            r = self.voltage_one(filament, active_mv,
                                 active_s=float(heat_timeout_s) + float(kw["timeout_s"]) + 5.0,
                                 then_idle_ma=idle_ma)
            heat["active"] = r
            if not r.get("ok"):
                heat["raised"] = False       # refused: nothing applied to drop
                raise RuntimeError(f"VOLTAGE {active_mv} mV not accepted: "
                                   f"{r.get('error') or r}")
            # ARRIVAL, as for ACTIVE: the firmware's voltage loop reports
            # "settled" (load voltage within +-150 mV of active_mv) or "capped"
            # (output at its limit, load still short) in the cached-current
            # read -- no I2C on the RP2350 side, so it can be polled.
            deadline = time.monotonic() + float(heat_timeout_s)
            # With settle_ms the firmware times the wait and on_armed should
            # follow "settled" at once: poll at a steady 50 ms then, not on a
            # back-off that can add 0.3 s.
            naps = iter(lambda: 0.05, None) if fast_poll else self._poll_intervals(0.1, 0.3)
            t0 = time.monotonic()
            d, arrival = {}, None
            while True:
                d = (self.read_filament_current_cached(filament) or {}).get(int(filament)) or {}
                arrival, mode = d.get("arrival"), d.get("cc_mode")
                if mode in (2, 3) or arrival in ("settled", "capped") or time.monotonic() >= deadline:
                    break
                time.sleep(next(naps))
            ma = d.get("current_mA")
            h = {"ok": arrival == "settled", "mode": "voltage", "commanded_mv": int(active_mv),
                 "arrival": arrival, "elapsed_s": round(time.monotonic() - t0, 3),
                 "measured_ma": ma, "measured_valid": ma is not None, "cc_mode": mode,
                 "measured_from": "cached"}
            if mode in (2, 3):
                h.update(ok=False, faulted=True,
                         error=f"output faulted at {active_mv} mV (cc_mode {mode}: "
                               f"{'open filament' if mode == 2 else 'OCP/short'})")
            elif arrival == "capped":
                h.update(error=f"{active_mv} mV not reached on the load: the output is at its "
                               f"limit (voltage cap or +1.5 V drop compensation) and the load "
                               f"is still more than 150 mV short ({ma} mA)")
            elif arrival == "settling":
                h.update(error=f"{active_mv} mV arrived but did not stay within +-150 mV for the "
                               f"settle time before the {heat_timeout_s} s deadline")
            elif arrival != "settled":
                h.update(error=f"{active_mv} mV not reached within {heat_timeout_s} s "
                               f"(arrival {arrival!r}, {ma} mA). Firmware without the "
                               f"voltage loop (before feat/pulse-instant-heat) never "
                               f"reports arrival in VOLTAGE mode")
            heat["active"] = {**r, "heating": h}
            if not h["ok"]:
                raise RuntimeError(f"VOLTAGE {active_mv} mV: {h['error']} -- not fired")

        def drop_idle():
            # Called when the run ends and again from the finally: a failed
            # drop gets one more try there.
            if heat["raised"] and not heat["dropped"]:
                try:
                    heat["idle"] = self.idle_one(filament, idle_ma)
                except Exception as exc:
                    heat["idle"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
                heat["dropped"] = bool(heat["idle"].get("ok"))
                heat["t_idle"] = time.monotonic()

        # The firmware's total clock runs from ARM; the heat-up is now inside it.
        kw["total_ms"] = int(kw["total_ms"]) + int(float(heat_timeout_s) * 1000) + 1000
        try:
            out = self._fire_single(filament, on_armed=raise_active,
                                    after_fire=drop_idle, **kw)
        finally:
            drop_idle()
        out["heating"] = (heat["active"] or {}).get("heating")
        if heat["t_active"] is not None and heat["t_idle"] is not None:
            out["active_s"] = round(heat["t_idle"] - heat["t_active"], 2)
        if heat["idle"] is not None and not heat["idle"].get("ok"):
            out["ok"] = False
            out["error"] = ((out["error"] + "; ") if out.get("error") else "") + (
                f"could not return filament {filament} to IDLE {idle_ma} mA after "
                f"the shot: {heat['idle'].get('error') or heat['idle']} -- the "
                f"backend's timed ACTIVE will still drop it")
        return out

    def _fire_single(self, filament: int, num_pulses, width_us, pulse_interval_ms,
                     trigger_timeout_ms, max_on_ms, total_ms, controller, trigger,
                     timeout_s, verify, reuse, measure, rate_hz, bg_gap_us, bg_window_us,
                     on_armed=None, after_fire=None) -> dict:
        if not measure:
            return self._fire_core(
                filament, num_pulses=num_pulses, width_us=width_us,
                pulse_interval_ms=pulse_interval_ms, trigger_timeout_ms=trigger_timeout_ms,
                max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=timeout_s, verify=verify, reuse=reuse,
                on_armed=on_armed, after_fire=after_fire)

        # Arm BEFORE firing -- a detector armed afterwards has already missed
        # the pulses. If it can't arm we fire nothing: silently firing HV that
        # nobody is measuring is the opposite of what measure=True asked for.
        # See shot_measure_arm() for why it is the RELAY that is armed.
        m = self.shot_measure_arm({"ok": True, "filament": filament,
                                   "pulse_interval_ms": pulse_interval_ms,
                                   "trigger_timeout_ms": int(trigger_timeout_ms),
                                   "num_pulses": int(num_pulses)},
                                  rate_hz, bg_gap_us=bg_gap_us,
                                  bg_window_us=bg_window_us, shot_wait_timeout_s=timeout_s)
        if not m.get("ok"):
            return m
        try:
            fired = self._fire_core(
                filament, num_pulses=num_pulses, width_us=width_us,
                pulse_interval_ms=pulse_interval_ms, trigger_timeout_ms=trigger_timeout_ms,
                max_on_ms=max_on_ms,
                total_ms=total_ms, controller=controller, trigger=trigger,
                timeout_s=timeout_s, verify=verify, reuse=reuse,
                on_armed=on_armed, after_fire=after_fire)
        except BaseException:
            self.ready_disarm()
            raise
        return self.shot_measured(m, fired)   # collects, then disarms the detector

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
        # ONE request, the boards disarmed in parallel by the backend: a slow
        # board no longer holds up the other (a fire used to pay for both).
        try:
            self._post("/api/disarm", {"controllers": [int(c) for c in controllers]})
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

    def shot_prepare(self, filament: int, num_pulses: int = 1, width_us: int = 1000,
                     inter_pulse_ms: int | None = None, max_on_ms: int = 40,
                     total_ms: int = 15000, controller: int | None = None,
                     trigger: str = "sim", verify: bool = True,
                     reuse: bool = False, pulse_interval_ms: int | None = None,
                     trigger_timeout_ms: int | None = None) -> dict:
        """Step 1 of a shot: check the arguments, pick the controller (and the
        master that frames its envelope), disarm both, and put the one-entry
        schedule on the RP2350s (or confirm by CRC that it is already there,
        reuse=True). Touches no heating and no HV. pulse_interval_ms /
        trigger_timeout_ms (and the old inter_pulse_ms) and their limits are as
        in fire_single_pulse(). Returns the SHOT dict that
        every later step takes and returns (ok False = stop; see "error").

        The steps, in the order fire_single_pulse() runs them:
            shot = ct.shot_prepare(f, ...)      # schedule on the board
            shot = ct.shot_measure_arm(shot)    # optional: STM32 detector
            shot = ct.shot_arm(shot)            # arm; checks the slot is safe
            shot = ct.shot_trigger(shot)        # "sim": fire now; "ext": no-op
            shot = ct.shot_wait(shot)           # until complete / fault / timeout
            res  = ct.shot_records(shot)        # pulse log, ON/OFF read-backs
            res  = ct.shot_measured(shot, res)  # optional: detector events
        Anything else (heating, reads) can go between them; ct.shot_abort(shot)
        disarms everything (put it in a finally). See examples/fire_steps.py."""
        if self._is_dead(filament):
            return self._dead_result(filament, {"fired": 0, "records": [], "status": {}})
        # pulse_interval_ms / trigger_timeout_ms: see _pulse_timing.
        timing = self._pulse_timing(trigger, inter_pulse_ms, pulse_interval_ms,
                                    trigger_timeout_ms)
        if timing.get("error"):
            return {"ok": False, "error": timing["error"], "fired": 0, "records": [],
                    "status": {}}
        interval, tmo = timing["interval_ms"], timing["timeout_ms"]

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
               or self._range_error("total_ms", int(total_ms), self._U32_MAX))
        if not err and interval is not None and interval * 1000 <= int(width_us):
            err = (f"pulse_interval_ms={interval} ({interval * 1000} us) is not longer than "
                   f"width_us={int(width_us)}: the next pulse would start before this one "
                   f"ends")
        if not err and trigger == "sim":
            train_ms = (int(num_pulses) - 1) * interval + -(-int(width_us) // 1000)
            if train_ms >= int(total_ms):
                err = (f"{int(num_pulses)} pulses {interval} ms apart take {train_ms} ms, "
                       f"not inside total_ms={int(total_ms)} (the firmware's limit for the "
                       f"whole run, counted from ARM) -- raise total_ms or fire fewer / "
                       f"closer pulses")
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
            # The firmware's interPulseMs is the TIMEOUT (trigger_timeout_ms):
            # by default 2 x the spacing + 100 for sim, see _pulse_timing.
            "config": {"interPulseMs": int(tmo),
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
                v = self._verify_retrying_timeouts(plan)
                if not v.get("ok"):
                    return {"ok": False, "error": f"schedule verify mismatch after download: {v}",
                            "fired": 0, "records": [], "status": {}}
                if reuse:
                    row = (v.get("results") or {}).get(str(controller)) or {}
                    if row.get("crc") is not None:
                        self._last_crc[controller] = row["crc"]

        return {"ok": True, "filament": int(filament), "controller": int(controller),
                "companion": companion, "armed_set": armed_set,
                "num_pulses": int(num_pulses),
                "pulse_interval_ms": interval, "trigger_timeout_ms": int(tmo),
                # Old key, old per-mode meaning, for code that reads it.
                "inter_pulse_ms": int(interval if trigger == "sim" else tmo),
                "trigger": trigger, "schedule": reuse_note,
                "fired": 0, "records": [], "status": {}}

    def shot_arm(self, shot: dict) -> dict:
        """Step 2: arm the shot's controller (and the master, for an envelope),
        then confirm the filament was not skipped as unsafe. Nothing fires until
        shot_trigger() (or the external edge). On failure everything is disarmed."""
        if not shot.get("ok"):
            return shot          # an earlier step failed: do nothing
        r = self._shot_arm(shot)
        return r if r.get("ok") else {**shot, **r}

    def _shot_arm(self, shot: dict) -> dict:
        if not shot.get("ok"):
            return shot
        filament, controller = shot["filament"], shot["controller"]
        companion, armed_set = shot["companion"], shot["armed_set"]
        reuse_note = shot.get("schedule")
        # Target and master in ONE request: the backend arms them master LAST
        # (it is the head of the trigger chain -- armed first, an edge in
        # between would advance it past entry 0 while the target was still
        # disarmed), checks the trigger delay once for both instead of once per
        # board, and puts back down whatever armed if the other fails.
        arm_all = self._post("/api/arm", {"repeats": 1, "controllers": armed_set})
        per = arm_all.get("results") or {}
        arm_r = per.get(str(controller)) or ({} if per else arm_all)
        if not arm_r.get("ok"):
            code, why = self._arm_failure(arm_r if per else arm_all)
            who = f"filament {filament}{self._mosfet_site(filament)}"
            return {"ok": False,
                    "error": (f"arm rejected for {who} (code {code}): {why}" if code is not None
                              else f"arm failed for {who}: {why}"),
                    "arm_reject": code, "arm_reject_name": why,
                    "fired": 0, "records": [], "status": {}, "schedule": reuse_note}
        if companion:
            c_arm = per.get(str(companion)) or {}
            if not c_arm.get("ok"):
                self._disarm_all(armed_set)
                code, why = self._arm_failure(c_arm)
                return {"ok": False,
                        "error": f"the master (controller {companion}) could not be "
                                 f"armed to frame this pulse: "
                                 f"{f'arm rejected (code {code}): ' if code is not None else ''}"
                                 f"{why}. Without it the STM32 gets no "
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

        return {**shot, "armed": True, "status": st_after}

    def shot_trigger(self, shot: dict) -> dict:
        """Step 3: start the pulses. trigger="sim": the ESP32 drives SyncIn now,
        through the head of the chain. trigger="ext": nothing to do -- the
        board waits for the external edge (shot_wait() then waits for it)."""
        if not shot.get("ok"):
            return shot          # an earlier step failed: do nothing
        r = self._shot_trigger(shot)
        return r if r.get("ok") else {**shot, **r}

    def _shot_trigger(self, shot: dict) -> dict:
        if not shot.get("ok"):
            return shot
        trigger = shot.get("trigger", "sim")
        num_pulses = shot["num_pulses"]
        interval = shot.get("pulse_interval_ms") or shot["inter_pulse_ms"]
        controller, companion = shot["controller"], shot["companion"]
        armed_set = shot["armed_set"]
        if trigger == "sim":
            # Through the HEAD of the chain. The master's ReadyIn ISR fires the
            # master and forwards the edge on SyncOut, exactly like an external
            # trigger; aimed at the target controller directly, the master
            # would never see it and never open the window.
            r = self._post("/api/sync/simulate", {
                "count": int(num_pulses),
                "interval_ms": float(max(int(interval), 10)),
                "controller": int(companion or controller),
            }, timeout=10.0)
            if not r.get("ok"):
                self._disarm_all(armed_set)
                return {"ok": False, "error": f"could not start SyncIn simulation: "
                                              f"{r.get('error', r)}",
                        "fired": 0, "records": [], "status": {}}

        return {**shot, "triggered": trigger == "sim"}

    def shot_wait(self, shot: dict, timeout_s: float = 15.0) -> dict:
        """Step 4: wait, up to timeout_s from now, for the trigger AND the run
        it starts to finish -- poll the controller until it completes (ok True,
        "complete": True), faults or times out (ok False, everything disarmed).
        Reads nothing else -- drop the heating right after it, before
        shot_records(), to keep ACTIVE short."""
        if not shot.get("ok"):
            return shot          # an earlier step failed: do nothing
        r = self._shot_wait(shot, timeout_s)
        return r if r.get("ok") else {**shot, **r}

    def _shot_wait(self, shot: dict, timeout_s: float = 15.0) -> dict:
        if not shot.get("ok"):
            return shot
        controller, armed_set = shot["controller"], shot["armed_set"]
        reuse_note = shot.get("schedule")
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
                return {**shot, "complete": True, "status": st}
            time.sleep(next(naps))

        self._disarm_all(armed_set)
        return {"ok": False, "timeout": True,
                "error": f"shot_wait timed out: the trigger and the run did not "
                         f"finish within {timeout_s} s of READY (state={state}) "
                         f"-- disarmed",
                "fired": 0, "records": [], "status": {}, "schedule": reuse_note}

    @staticmethod
    def _hex8(v) -> str:
        return f"0x{int(v) & 0xFF:02X}" if isinstance(v, int) else str(v)

    def _mosfet_bit(self, filament):
        """The grid-MOSFET byte a pulse on `filament` commands (one bit per
        board position), or None if the filament has no board."""
        try:
            site = self.filament_to_board(int(filament))
        except Exception:
            site = None
        if not site:
            return None
        pos = site.get("position")
        return 1 << int(pos if pos is not None else int(site["slot"]) % 8)

    def _mosfet_site(self, filament) -> str:
        try:
            site = self.filament_to_board(int(filament))
        except Exception:
            site = None
        if not site:
            return ""
        slot = site.get("slot")
        ch = site.get("channel", None if slot is None else int(slot) // 8)
        pos = site.get("position", None if slot is None else int(slot) % 8)
        if ch is None or pos is None:
            return f" (P{site.get('controller')})"
        return f" (P{site.get('controller')} CH{int(ch) + 1}.{int(pos) + 1})"

    def shot_records(self, shot: dict) -> dict:
        """Step 5: read the pulse log for a completed shot and judge it: fired
        count, MOSFET read-back mismatch (ON / OFF) from the switch read-backs,
        and (two controllers) whether the master framed every pulse. Returns
        the result in fire_single_pulse()'s shape."""
        if not shot.get("ok") or not shot.get("complete"):
            return shot
        filament, controller = shot["filament"], shot["controller"]
        companion, num_pulses = shot["companion"], shot["num_pulses"]
        reuse_note, st = shot.get("schedule"), shot.get("status") or {}
        logs = self.shv_pulse_log(controller)
        fired = [r for r in logs if r.get("filament") == filament]
        out = {"ok": bool(fired), "fired": len(fired),
               "records": fired, "status": st, "schedule": reuse_note}
        # MOSFET READ-BACK MISMATCH, OFF (flags bit 0x02): after the pulse the
        # 74HC165 read-back of the grid MOSFETs was non-zero. This is the only
        # pulse-log flag that is about the PULSE rather than about the
        # verification of it, and it is the one that matters -- a MOSFET that
        # stayed closed leaves the filament on the emission rail after the
        # pulse. Surfaced at the top level because it was previously
        # invisible: `flags` was a raw byte nobody decoded, so this condition
        # could occur and be reported as a perfectly successful shot. The
        # emission/focus rails themselves are a separate thing (hv_status).
        stuck = [(i, r) for i, r in enumerate(fired) if r.get("hv_stuck_on")]
        if stuck:
            out["hv_stuck_on"] = sorted({r.get("filament") for _, r in stuck})
            out["ok"] = False
            # Plain words first (what it means, what to do); the bytes go to
            # "detail" -- end users could not read the hex (2026-10-15, #78).
            out["error"] = (f"Filament {filament}{self._mosfet_site(filament)}: the MOSFET "
                            f"may still be closed after pulse(s) "
                            f"{self._pulse_list(i for i, _ in stuck)} -- check it before "
                            f"firing again.")
            self._add_detail(out, "read-back after the pulse (should be 0x00): " + "; ".join(
                f"pulse {i + 1}: {self._hex8(r.get('read165'))}" for i, r in stuck))
        # MOSFET READ-BACK MISMATCH, ON (flags bit 0x01): during the pulse
        # the 74HC165 read-back of the grid MOSFETs did not match the byte
        # commanded. Either the MOSFET did not close (no HV reached the
        # filament -- 2026-09-23: filament 50, read 0x00, ~0 mA, had been
        # reported ok) or the read-back itself is wrong (2026-10-07: P1 read
        # the NEIGHBOURING bit, 0x02 for 0x01, while current flowed).
        # shot_measured() tells which from the measured current. Not about
        # the emission/focus rails, which are on regardless.
        no_on = [(i, r) for i, r in enumerate(fired) if r.get("on_mismatch")]
        if no_on:
            out["on_mismatch"] = sorted({r.get("filament") for _, r in no_on})
            out["on_mismatch_pulses"] = [i for i, _ in no_on]      # index into records
            out["ok"] = False
            cmd = self._mosfet_bit(filament)

            def what(r):
                rd = r.get("read165")
                s = f"wrote {self._hex8(cmd)}, read back {self._hex8(rd)}"
                if isinstance(rd, int) and isinstance(cmd, int):
                    if rd == 0:
                        s += " (no bit)"
                    elif rd in ((cmd << 1) & 0xFF, cmd >> 1):
                        s += " (the neighbouring bit)"
                return s
            self._add_detail(out, "MOSFET read-back: " + "; ".join(
                f"pulse {i + 1}: {what(r)}" for i, r in no_on))
            out["error"] = ((out["error"] + " ") if out.get("error") else "") + (
                f"Filament {filament}{self._mosfet_site(filament)}: the MOSFET was not "
                f"confirmed closed on pulse(s) {self._pulse_list(i for i, _ in no_on)} "
                f"of {len(fired)}. " + self._ON_MISMATCH_UNMEASURED)
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

    def shot_measure_arm(self, shot: dict, rate_hz: int = 1000000,
                         bg_gap_us: float | None = None,
                         bg_window_us: float | None = None,
                         shot_wait_timeout_s: float = 15.0,
                         timeout_s: float | None = None) -> dict:
        """Optional, before shot_arm(): arm the STM32 detector (via the ESP32
        relay) so every pulse of this shot is measured, and remember where the
        event stream stands. Fails -> nothing has been armed; do not fire."""
        if not shot.get("ok"):
            return shot
        # The longest gap the run allows between pulses (trigger_timeout_ms);
        # older shot dicts only have inter_pulse_ms.
        gap_ms = int(shot.get("trigger_timeout_ms") or shot.get("inter_pulse_ms") or 0)
        rate_hz = int(rate_hz)
        if timeout_s is not None:      # old name of shot_wait_timeout_s
            shot_wait_timeout_s = timeout_s
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
        # gap is the longest one the run allows (trigger_timeout_ms); give it
        # room, and never go below the firmware default. Also covers the head
        # of the run, before the first pulse.
        arm_ttl_ms = max(self._READY_TTL_FLOOR_MS,
                         gap_ms * self._READY_TTL_GAP_FACTOR // 2,
                         int(shot_wait_timeout_s * 1000))
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
        # The cursor BEFORE firing, so only THIS shot's events are collected,
        # never a stale backlog (via pulse_cursor(): a huge `since` saturates
        # at 2**31-1 in the ESP32's query parsing).
        try:
            since = self.pulse_cursor()
        except BaseException:
            self.ready_disarm()
            raise
        # The emission current limit, so shot_measured() can tell a pulse the
        # SUPPLY clamped from real emission. None = unknown, check skipped.
        return {**shot, "measuring": True, "since": since, "rate_hz": rate_hz,
                "em_limit_ma": self.read_emission_i_limit()}

    def shot_measured(self, shot: dict, result: dict | None = None,
                      grace_s: float = 3.0, prefetched: tuple | None = None) -> dict:
        """Optional, last: collect the detector events this shot produced and
        disarm the detector. `result` is shot_records()'s return; the merged
        result is ok only if every fired pulse was measured."""
        base = dict(result if result is not None else shot)
        if not shot.get("measuring"):
            return base
        try:
            want = int(shot["num_pulses"])
            # prefetched = (events, ref_mv) already read while the train ran (a
            # long train overruns the ESP32's 128-event ring); collect the rest.
            got, ref0 = prefetched if prefetched else ([], None)
            since = got[-1]["id"] if got else int(shot["since"])
            more, ref_mv = self._collect_pulse_events(since, want - len(got), grace_s)
            measured, ref_mv = list(got) + more, ref_mv or ref0
        finally:
            self.ready_disarm()
        out = {**base, "measured": measured, "ref_mv": ref_mv,
               "ok": bool(base.get("ok")) and len(measured) >= want}
        if not out["ok"] and base.get("ok") and not out.get("error"):
            out["error"] = (f"fired {base.get('fired')} pulse(s) but the detector "
                            f"reported {len(measured)} of {want} — measurement "
                            f"incomplete, so the result is not trustworthy")
        # A MOSFET read-back mismatch (ON) is either a MOSFET that did not
        # close or a wrong read-back. The emission current of the pulse says
        # which: through the sub-board's diode path a closed MOSFET always
        # draws current (emission_ma is None when it drew none).
        if base.get("on_mismatch") and measured:
            out["error"] = (out.get("error") or "").replace(
                self._ON_MISMATCH_UNMEASURED, self._on_mismatch_verdict(base, measured))
            out["mosfet_conducted"] = self._on_mismatch_conducted(base, measured)
            # Current flowed in EVERY mismatched pulse: the MOSFETs closed and
            # the shot is good -- only the 165 read-back was wrong (signal
            # integrity). That must not fail the shot: a macro waiting on ok
            # would stop on good data. Only when the read-back was the SOLE
            # problem (nothing stuck on, every pulse fired and measured).
            if (out["mosfet_conducted"] is True and not base.get("hv_stuck_on")
                    and len(measured) >= want and int(base.get("fired") or 0) >= want):
                out["ok"] = True
                out["readback_unreliable"] = True
                out["warning"] = out.pop("error")
        # SUPPLY LIMIT. A pulse reading at the emission current limit is the
        # supply clamping, not emission: 2026-10-15, filament 78 (P1 CH6.3)
        # read ~38 mA against a 45 mA limit on 8 of 10 pulses at 2597 mA
        # heating (hot breakdown), and the shot came back ok=True. Checked
        # after the read-back verdict so it is never turned into a warning.
        lim = shot.get("em_limit_ma")
        hit = self._supply_limited(measured, lim)
        if hit:
            out["ok"] = False
            out["supply_limited"] = [i for i, _ in hit]          # index into measured
            out["supply_limit_ma"] = lim
            msg = self._supply_limit_message(base, shot, measured, hit, lim)
            if out.get("warning"):          # read-back verdict was the only problem
                out["error"] = msg + " " + out.pop("warning")
                out.pop("readback_unreliable", None)
            else:
                out["error"] = msg + ((" " + out["error"]) if out.get("error") else "")
        return out

    #: A pulse whose net current is at least this fraction of the emission
    #: current limit is taken as clamped by the supply.
    SUPPLY_LIMIT_FRAC = 0.8

    @classmethod
    def _supply_limited(cls, measured: list, lim) -> list:
        """[(event index, net mA)] of the pulses at the supply's current limit."""
        if not isinstance(lim, (int, float)) or lim <= 0:
            return []
        out = []
        for i, e in enumerate(measured or []):
            net = e.get("plateau_net_ma")
            if net is None:
                net = e.get("emission_ma")
            if isinstance(net, (int, float)) and net >= cls.SUPPLY_LIMIT_FRAC * lim:
                out.append((i, float(net)))
        return out

    def _supply_limit_message(self, base: dict, shot: dict, measured: list,
                              hit: list, lim: float) -> str:
        f = shot.get("filament")
        if f is None:
            f = next((r.get("filament") for r in base.get("records") or []), None)
        nets = [n for _, n in hit]
        recs = base.get("records") or []
        heat = []
        if len(recs) == len(measured):      # paired 1:1 by order, else unknown
            heat = [recs[i].get("heat_meas_mA") for i, _ in hit]
            heat = [h for h in heat if isinstance(h, (int, float))]
        mean_heat = sum(heat) / len(heat) if heat else None
        head = (f"Filament {f}{self._mosfet_site(f)}: {len(hit)} of {len(measured)} pulses "
                f"read about {sum(nets) / len(nets):.0f} mA = the emission supply's current "
                f"limit ({lim:.0f} mA set), not emission -- ")
        if mean_heat is not None and mean_heat >= self._HOT_HEAT_MA:
            return head + (f"the filament is arcing or breaking down at this heating "
                           f"current ({mean_heat:.0f} mA). Lower the heating current, "
                           f"or check this filament.")
        return head + ("a short or breakdown on this filament's path"
                       + (f" (heating {mean_heat:.0f} mA)" if mean_heat is not None else "")
                       + ". Check this filament.")

    #: Heating current above which a clamped pulse is reported as hot breakdown.
    _HOT_HEAT_MA = 500

    _ON_MISMATCH_UNMEASURED = ("Not measured, so it is unknown whether those pulses fired.")

    @staticmethod
    def _pulse_list(idx) -> str:
        """1-based pulse numbers from 0-based indices: "2", "3, 7"."""
        return ", ".join(str(i + 1) for i in idx)

    @staticmethod
    def _add_detail(out: dict, text: str) -> None:
        out["detail"] = ((out["detail"] + "; ") if out.get("detail") else "") + text

    @staticmethod
    def _on_mismatch_currents(base: dict, measured: list):
        """[(pulse index, emission mA or None)] for the mismatched pulses, or
        None when records and events cannot be paired 1:1 by order."""
        if len(measured) != int(base.get("fired") or 0):
            return None
        return [(i, measured[i].get("emission_ma")) for i in base.get("on_mismatch_pulses") or [0]
                if i < len(measured)]

    def _on_mismatch_conducted(self, base: dict, measured: list):
        cur = self._on_mismatch_currents(base, measured)
        return None if cur is None else all(em is not None for _, em in cur)

    def _on_mismatch_verdict(self, base: dict, measured: list) -> str:
        """The measured current of the MISMATCHED pulses themselves says which:
        a closed MOSFET always draws current through the sub-board's diode path
        (emission_ma is None when the path drew none)."""
        cur = self._on_mismatch_currents(base, measured)
        if cur is None:
            return (f"The detector measured {len(measured)} of {base.get('fired')} pulses, so "
                    f"it is unknown whether those pulses fired.")
        flowed = [(i, em) for i, em in cur if em is not None]
        dead = [i for i, em in cur if em is None]

        def mams(i, key):
            v = measured[i].get(key)
            return "n/a" if v is None else f"{v:.1f}"
        out = []
        if flowed:
            each = ", ".join(f"{em:.2f} mA / {mams(i, 'emission_mams')} mA*ms" for i, em in flowed)
            out.append(f"Pulse(s) {self._pulse_list(i for i, _ in flowed)} fired normally "
                       f"({each} flowed) -- only the MOSFET read-back is wrong (the 74HC165 or its "
                       f"cable); that data is good.")
        if dead:
            # The measured charge of a dead pulse is shown too: "no current" is
            # the verdict, the mA*ms is the evidence a reader checks it against.
            out.append(f"Pulse(s) {self._pulse_list(dead)} did not fire (no current: "
                       f"{', '.join(mams(i, 'integral_mams') for i in dead)} mA*ms measured; "
                       f"the MOSFET did not close).")
        return " ".join(out)

    def shot_abort(self, shot: dict) -> None:
        """Disarm everything the shot armed (both controllers, the detector).
        Safe to call at any point and more than once -- put it in a finally."""
        if shot.get("armed_set"):
            self._disarm_all(shot["armed_set"])
        if shot.get("measuring"):
            try:
                self.ready_disarm()
            except Exception:
                pass

    def _fire_core(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        pulse_interval_ms: int | None = None,
        trigger_timeout_ms: int | None = None,
        max_on_ms: int = 40,
        total_ms: int = 15000,       # RP2350 FIRMWARE's own schedule timeout (ms)
                                      # — see docstring, "total_ms vs shot_wait_timeout_s"
        controller: int | None = None,   # None = auto-infer from `filament` via
                                          # the active-list mapping — see docstring,
                                          # "why controller exists at all"
        trigger: str = "sim",
        timeout_s: float = 15.0,     # = fire_single_pulse's shot_wait_timeout_s (s)
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
        after_fire=None,       # callable() run the moment the run is over
                                # (complete, fault or timeout), before the
                                # pulse log is read -- drops ACTIVE early
    ) -> dict:
        """Fire one schedule entry and wait for it. The body of
        fire_single_pulse() -- see that method for the full contract;
        this exists only so the public method can wrap it with the
        detector arm/correlate step without duplicating any of it."""
        shot = self.shot_prepare(filament, num_pulses=num_pulses, width_us=width_us,
                                 pulse_interval_ms=pulse_interval_ms,
                                 trigger_timeout_ms=trigger_timeout_ms, max_on_ms=max_on_ms,
                                 total_ms=total_ms, controller=controller,
                                 trigger=trigger, verify=verify, reuse=reuse)
        if not shot.get("ok"):
            return shot
        shot = self.shot_arm(shot)
        if not shot.get("ok"):
            return shot
        armed_set = shot["armed_set"]
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

        shot = self.shot_trigger(shot)
        if not shot.get("ok"):
            return shot

        def _after():
            # The run is over: hand back to the caller (e.g. drop ACTIVE) before
            # the slower read-backs. Its failure must not lose the shot result.
            if callable(after_fire):
                try:
                    after_fire()
                except Exception:
                    pass      # its owner records its own outcome

        shot = self.shot_wait(shot, timeout_s)
        _after()
        if not shot.get("complete"):
            return shot
        return self.shot_records(shot)
