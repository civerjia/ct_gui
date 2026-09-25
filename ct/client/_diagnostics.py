"""CTClient: MOSFET/switch tests, self-test, test flows.

One part of the client class, split out of one 9900-line file by section:
    HV grid MOSFET test (via a real SHV pulse)
    Board self-test & I2C diagnostics
    HV switch toggle test
    Test & measurement flows

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403
from typing import TYPE_CHECKING

if TYPE_CHECKING:   # editors only: at run time _client.py binds CTClient into this module
    from ._client import CTClient


class _DiagnosticsMixin:
    # ── HV grid MOSFET test (via a real SHV pulse) ───────────────────────────
    # hv_switch_test() drives the switch and reads the 74HC165 sense back: it
    # proves the CONTROL path reached the gate. It cannot prove the MOSFET
    # actually conducts -- a dead device still reads back the bit that was
    # written to it.
    #
    # This fires a real HV pulse instead and measures the current that flows.
    # Each sub-board has two diodes in series with a 100 kOhm resistor across
    # the switch, so with the MOSFET on and the filament COLD the only path is
    #
    #     I = (|V_emission| - Vf1 - Vf2) / R     e.g. (100 - 2.82) / 100k
    #                                                 = 0.972 mA at 100 V
    #
    # Seeing that current is the proof. It is also already confirmed on this
    # bench: what the emission work earlier called a non-emission "pedestal"
    # matches this path to within 3% at 50, 100, 150 and 200 V (measured
    # 0.488 / 1.004 / 1.533 / 1.979 mA against 0.472 / 0.972 / 1.472 / 1.972).

    #: Sub-board diode drops, volts. Two in series with _MOSFET_R_OHM.
    _MOSFET_VF_V = (0.82, 2.0)
    _MOSFET_R_OHM = 100_000.0
    #: A reading this far from expected is still a pass. Wide on purpose: a
    #: diode's Vf moves with temperature and with the current through it, and
    #: at ~1 mA neither drop is the datasheet number. The test is asking "does
    #: it conduct at all", not "is the resistor 1% tolerance".
    _MOSFET_TOL_FRAC = 0.35
    #: Below this fraction of expected, the switch is not conducting.
    _MOSFET_DEAD_FRAC = 0.30

    @classmethod
    def mosfet_expected_ma(cls, emission_v: float,
                           r_ohm: float | None = None,
                           vf_v=None) -> float:
        """The current the diode path should pass at this rail voltage."""
        vf = sum(vf_v if vf_v is not None else cls._MOSFET_VF_V)
        return (abs(float(emission_v)) - vf) / float(r_ohm or cls._MOSFET_R_OHM) * 1000.0

    def _hv_off_problem(self) -> str | None:
        """Turn the emission rail off. None if that worked, else what to report.

        Never raises -- it runs in finally blocks, where raising would replace
        whatever exception is already on its way out. A failure is RETURNED so
        the caller puts it in front of the reader: a rail that may still be live
        is the one thing a teardown must not keep quiet about. (If it goes
        unreported anyway, nothing turns the rail off by itself: the backend's
        dead-man watchdog only opens the grid MOSFETs, never the rails.)
        """
        try:
            r = self.enable_emission(False)
        except Exception as exc:          # defensive: the client should not raise
            return f"turning the emission rail OFF raised {type(exc).__name__}: {exc} — it may still be LIVE"
        if not r.get("ok"):
            return (f"turning the emission rail OFF failed "
                    f"({r.get('error') or 'no reason given'}) — it may still be LIVE")
        return None

    def _mosfet_targets(self, filaments) -> tuple[list[int], list[str]]:
        """Live filaments on CONNECTED controllers, and why any were dropped.
        An unconnected controller's filaments can only fail -- slowly, ~5 s each
        -- and would read as inconclusive for a reason that is not theirs."""
        wanted = self._live_user_indices() if filaments is None else \
            [int(f) for f in filaments]
        wanted = [f for f in wanted if not self._is_dead(f)]
        conn = set(self._connected_controllers())
        rows = ((self.get_mapping() or {}).get("mapping") or {}).get("filaments") or []
        ctrl_of = {int(r["filament"]): int(r["controller"]) + 1 for r in rows
                   if r.get("controller") in (0, 1)}
        keep, skipped = [], []
        for f in wanted:
            c = ctrl_of.get(self._fid_of(f))
            (keep if c in conn else skipped).append(f)
        notes = ([f"{len(skipped)} filament(s) not tested: their controller is not "
                  f"connected ({skipped[:6]}{'...' if len(skipped) > 6 else ''})"]
                 if skipped else [])
        return keep, notes

    def _mosfet_rail(self, volts: float, timeout_s: float = 3.0) -> tuple[float | None, str | None]:
        """Set the emission rail and wait until it READS BACK within 10%.
        (read value, None) on success, (last read, problem) otherwise."""
        self.set_emission_v(abs(volts))
        deadline = time.time() + timeout_s
        v = None
        while True:
            time.sleep(0.3)
            v = self.read_emission_v()
            if v is not None and abs(abs(v) - abs(volts)) <= 0.10 * abs(volts):
                return v, None
            if time.time() >= deadline:
                return v, (f"the emission rail did not reach -{abs(volts):g} V "
                           f"(read {'nothing' if v is None else f'{v:.1f} V'}) -- check "
                           f"the current limit and the HV LUT; nothing measured on it")

    def _mosfet_fire(self, f: int, *, pulses: int, width_us: int,
                     inter_pulse_ms: int, controller) -> dict:
        """Fire `pulses` on one cold filament: {"nets": [signed net mA per
        shot], "on_mismatch": bool, "error": str | None}."""
        fr = self.fire_single_pulse(
            f, num_pulses=pulses, width_us=width_us,
            inter_pulse_ms=inter_pulse_ms, max_on_ms=40,
            total_ms=max(10000, pulses * (inter_pulse_ms + 1000)),
            controller=controller, trigger="sim",
            timeout_s=15.0 + pulses * inter_pulse_ms / 1000.0,
            verify=True, reuse=False, measure=True)
        nets = [e["plateau_net_ma"] for e in (fr.get("measured") or [])
                if e.get("plateau_net_ma") is not None and not e.get("empty_envelope")]
        return {"nets": nets, "on_mismatch": bool(fr.get("on_mismatch")),
                "error": fr.get("error")}

    def mosfet_test(self, filaments=None,
                    emission_v: float = 100.0,
                    limit_ma: float = 30.0,
                    width_us: int = 1000,
                    pulses: int = 3,
                    inter_pulse_ms: int = 400,
                    r_ohm: float | None = None,
                    vf_v=None,
                    tolerance_frac: float | None = None,
                    controller: int | None = None,
                    progress=None) -> dict:
        """Prove each HV grid MOSFET actually CONDUCTS, by firing through it.

            r = ct.mosfet_test([0, 1, 2], emission_v=100)
            print(r)

        Fires a real pulse per filament with every filament COLD, and measures
        the current. Cold is what makes it a MOSFET test rather than an
        emission test: with no thermionic current the only path is the
        sub-board's two diodes in series with its 100 kOhm resistor, so a
        reading of (|V| - Vf1 - Vf2)/R means the device conducted and anything
        near zero means it did not.

        WHY NOT STOP. The filaments are driven to SLEEP, not STOP, and that is
        not a preference. At STOP the board's isolated 12 V rail is off, so
        ShvArm SKIPS the filament as unsafe -- the envelope still fires for the
        counted trigger and the detector still records a shot, so a STOPped
        filament would read as ~0 mA and be reported as a DEAD MOSFET when in
        fact nothing was ever tried. SLEEP enables the rail without any heating
        current, which is exactly what this test wants.

        THE CURRENT LIMIT. limit_ma is the emission supply's limit, and 30 mA is
    the working value: at 5 mA the rail never came up (set 80 V, read back
    -16 V, 2026-09-25) and every verdict was measured on the wrong rail. The
    rail is now read back before anything fires and the test refuses to run on
    one more than 10% away from what was set.

    ONE VOLTAGE CANNOT SEPARATE A HEALTHY PATH FROM AN OFFSET: a front-end
    offset of -0.4 mA calls a good MOSFET dead at a low rail. mosfet_sweep()
    fits several voltages and judges the slope; prefer it.

    This complements hv_switch_test() rather than replacing it: that one
        reads the 74HC165 sense back and proves the CONTROL path reached the
        gate, which a dead MOSFET also passes. Run both and a disagreement is
        informative -- switch bit set, no current, is a failed device.

        Returns
            {"ok":            every tested filament passed,
             "emission_v":    the rail actually read back,
             "expected_ma":   (|V| - Vf) / R,
             "results":       {user_index: {"measured_ma", "expected_ma",
                                            "ratio", "verdict", "shots",
                                            "note"}},
             "counts":        {"pass", "dead", "inconclusive"},
             "problems":      [str]}

        Verdicts, three of them for the same reason hv_switch_test has three: a
        reading that is neither the expected current nor zero is not a pass and
        not a failure, and collapsing it either way loses the one thing worth
        knowing.

            pass          within `tolerance_frac` of expected
            dead          below _MOSFET_DEAD_FRAC of expected -- not conducting
            inconclusive  in between, or no pulse was measured at all
        """
        tol = self._MOSFET_TOL_FRAC if tolerance_frac is None else float(tolerance_frac)
        wanted, problems = self._mosfet_targets(filaments)
        if not wanted:
            return {"ok": False, "results": {}, "counts": {},
                    "problems": problems + ["no live filament on a connected controller"]}

        # THE RAIL FIRST, BEFORE ANYTHING IS ENERGISED -- and READ BACK: a rail
        # that did not come up must not be measured (5 mA limit: -16 V of 80).
        self.set_emission_i(limit_ma)
        self.set_emission_v(abs(emission_v))
        self.enable_emission(True)
        v_read, rail_problem = self._mosfet_rail(emission_v)
        hv = self.hv_status()
        if rail_problem or not hv.get("emission_on"):
            off = self._hv_off_problem()
            return {"ok": False, "emission_v": v_read, "results": {}, "counts": {},
                    "problems": problems + [rail_problem or "the emission rail did not come on "
                                            "-- nothing to measure, and no filament was touched"]
                                + ([off] if off else [])}
        v_eff = abs(v_read)
        expected = self.mosfet_expected_ma(v_eff, r_ohm, vf_v)
        if expected <= 0:
            off = self._hv_off_problem()
            return {"ok": False, "results": {}, "counts": {},
                    "problems": problems + [f"rail {v_eff} V is at or below the "
                                 f"{sum(vf_v or self._MOSFET_VF_V)} V of diode "
                                 f"drop -- no current can flow through the path "
                                 f"this test measures"] + ([off] if off else [])}

        results: dict[int, dict] = {}
        hv_off = None
        try:
            with self.energised(*wanted):
                try:
                    # COLD, and on the iso rail. sleep_all() does both.
                    self.stop_all(wanted)
                    sl = self.sleep_all(wanted)
                    if not sl.get("ok"):
                        problems.append(f"could not put every filament to SLEEP "
                                        f"({self.describe(sl)[:100]}) -- a filament "
                                        f"still at STOP reads as a dead MOSFET")
                    for f in wanted:
                        m = self._mosfet_fire(f, pulses=pulses, width_us=width_us,
                                              inter_pulse_ms=inter_pulse_ms,
                                              controller=controller)
                        nets = m["nets"]
                        row = {"measured_ma": None, "expected_ma": round(expected, 4),
                               "ratio": None, "verdict": "inconclusive",
                               "shots": len(nets), "note": None}
                        if not nets:
                            row["note"] = (m["error"] or "no pulse was measured -- the "
                                           "switch was never actually exercised")
                        elif m["on_mismatch"]:
                            # The switch never closed: ~0 mA says nothing about the
                            # MOSFET. The switch fault itself is the finding.
                            row["measured_ma"] = round(sum(nets) / len(nets), 4)
                            row["note"] = m["error"]
                        else:
                            mean = sum(nets) / len(nets)
                            row["measured_ma"] = round(mean, 4)
                            row["ratio"] = round(mean / expected, 3)
                            if abs(mean - expected) <= tol * expected:
                                row["verdict"] = "pass"
                            elif mean < self._MOSFET_DEAD_FRAC * expected:
                                row["verdict"] = "dead"
                                row["note"] = (f"{mean:.3f} mA against {expected:.3f} mA "
                                               f"expected -- the MOSFET is not conducting")
                            else:
                                row["note"] = (f"{mean:.3f} mA against {expected:.3f} mA "
                                               f"expected ({mean / expected:.2f}x) -- "
                                               f"neither the diode path nor zero")
                        results[int(f)] = row
                        if callable(progress):
                            progress(int(f), row)
                finally:
                    # The rail comes down FIRST, before energised() STOPs the
                    # filaments: that teardown can be slow, and a rail left on
                    # while it runs is exposure with nothing being measured
                    # (it once stayed up 4 minutes past the last shot).
                    hv_off = self._hv_off_problem()
        finally:
            if hv_off is None:
                hv_off = self._hv_off_problem()   # idempotent; covers a failure before the inner try

        if hv_off:
            problems.append(hv_off)
        counts = {"pass": 0, "dead": 0, "inconclusive": 0}
        for row in results.values():
            counts[row["verdict"]] += 1
        for f, row in sorted(results.items()):
            if row["verdict"] != "pass":
                problems.append(f"filament {f}: {row['verdict']} -- {row['note']}")
        return {"ok": not problems and bool(results),
                "emission_v": v_read, "expected_ma": round(expected, 4),
                "tolerance_frac": tol, "results": results, "counts": counts,
                "problems": problems}

    def mosfet_sweep(self, filaments=None,
                     v_start: float = 50.0,
                     v_step: float = 10.0,
                     n: int = 4,
                     limit_ma: float = 30.0,
                     width_us: int = 1000,
                     pulses: int = 3,
                     inter_pulse_ms: int = 400,
                     r_ohm: float | None = None,
                     tolerance_frac: float | None = None,
                     controller: int | None = None,
                     progress=None) -> dict:
        """The MOSFET test at n >= 3 emission voltages, judged on the SLOPE.

            r = ct.mosfet_sweep()                      # -50/-60/-70/-80 V, 30 mA limit
            r = ct.mosfet_sweep([0, 1, 2], v_start=40, v_step=10, n=5)
            print(r)

        Every filament COLD (SLEEP), as in mosfet_test(): the only current path
        is the sub-board's two diodes and its resistor through the grid MOSFET,
        I = (V - Vf) / R. Each filament is fired at every voltage and its I-V
        points are fitted to a line. The slope gives R_eq; a constant
        front-end offset lands in the intercept and cannot fake a verdict --
        which is exactly what a single voltage cannot do (-0.4 mA offsets called
        good MOSFETs dead at a low rail). The intercept is reported as vf_v but
        is only a diode drop when there is no offset: judge on r_kohm.

        The rail is read back at every step; the sweep stops at the first step
        that is more than 10% off (the limit is usually why). HV off first on
        teardown, then the filaments.

        Returns {"ok", "volts": set points, "rail_v": read-backs,
        "results": {user_index: {"points": [[V, mA], ...], "r_kohm", "vf_v",
        "r2", "verdict", "note"}}, "counts": {"pass", "dead", "odd",
        "unmeasured"}, "problems"}.

        Verdicts (nominal r_ohm, default 100 kOhm; tolerance default 35%):
            pass         |R_eq - R| <= tol*R and r2 >= 0.9
            dead         slope <= 0, R_eq > 5 R, or the top-voltage current
                         < 30% of expected -- not conducting
            odd          conducts, but R_eq is off or the fit is poor
            unmeasured   fewer than 3 usable points
        """
        n = max(3, int(n))
        volts = [abs(float(v_start)) + i * abs(float(v_step)) for i in range(n)]
        r_nom = float(r_ohm or self._MOSFET_R_OHM) / 1000.0          # kOhm
        tol = self._MOSFET_TOL_FRAC if tolerance_frac is None else float(tolerance_frac)
        wanted, problems = self._mosfet_targets(filaments)
        if not wanted:
            return {"ok": False, "results": {}, "counts": {},
                    "problems": problems + ["no live filament on a connected controller"]}
        if self.hv_status().get("emission_on"):
            return {"ok": False, "results": {}, "counts": {},
                    "problems": problems + ["the emission rail is already ON -- turn it off "
                                            "first; this test sets its own voltages"]}
        points: dict[int, list] = {f: [] for f in wanted}
        rail: list = []
        hv_off = None
        self.set_emission_i(limit_ma)
        try:
            with self.energised(*wanted):
                try:
                    self.stop_all(wanted)
                    sl = self.sleep_all(wanted)
                    if not sl.get("ok"):
                        problems.append(f"could not put every filament to SLEEP "
                                        f"({self.describe(sl)[:100]})")
                    for i, v in enumerate(volts):
                        if i == 0:
                            self.set_emission_v(v)
                            self.enable_emission(True)
                        v_read, bad = self._mosfet_rail(v)
                        rail.append(v_read)
                        if bad:
                            problems.append(bad)
                            break
                        for f in wanted:
                            m = self._mosfet_fire(f, pulses=pulses, width_us=width_us,
                                                  inter_pulse_ms=inter_pulse_ms,
                                                  controller=controller)
                            if m["nets"] and not m["on_mismatch"]:
                                points[f].append([round(abs(v_read), 2),
                                                  round(sum(m["nets"]) / len(m["nets"]), 4)])
                            if callable(progress):
                                progress(v, int(f), m)
                finally:
                    hv_off = self._hv_off_problem()     # rail FIRST, then the filaments
        finally:
            if hv_off is None:
                hv_off = self._hv_off_problem()
        if hv_off:
            problems.append(hv_off)

        results: dict[int, dict] = {}
        counts = {"pass": 0, "dead": 0, "odd": 0, "unmeasured": 0}
        vf_nom = sum(self._MOSFET_VF_V)
        for f in wanted:
            p = points[f]
            row = {"points": p, "r_kohm": None, "vf_v": None, "r2": None,
                   "verdict": "unmeasured", "note": f"{len(p)} of {n} points"}
            if len(p) >= 3:
                xs = [q[0] for q in p]
                ys = [q[1] for q in p]
                k = len(p)
                mx, my = sum(xs) / k, sum(ys) / k
                sxx = sum((x - mx) ** 2 for x in xs)
                sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
                syy = sum((y - my) ** 2 for y in ys)
                a = sxy / sxx if sxx else 0.0
                b = my - a * mx
                r2 = (sxy * sxy / (sxx * syy)) if sxx and syy else 0.0
                row["r2"] = round(r2, 3)
                i_top, i_exp = ys[-1], (xs[-1] - vf_nom) / r_nom
                if a > 0:
                    row["r_kohm"] = round(1.0 / a, 1)
                    row["vf_v"] = round(-b / a, 2)
                if a <= 0 or 1.0 / a > 5 * r_nom or i_top < self._MOSFET_DEAD_FRAC * i_exp:
                    row["verdict"], row["note"] = "dead", "not conducting"
                elif abs(1.0 / a - r_nom) <= tol * r_nom and r2 >= 0.9:
                    row["verdict"], row["note"] = "pass", None
                else:
                    row["verdict"] = "odd"
                    row["note"] = f"R_eq {1.0 / a:.0f} kOhm (nominal {r_nom:.0f}), r2 {r2:.2f}"
            counts[row["verdict"]] += 1
            results[int(f)] = row
            if row["verdict"] != "pass":
                problems.append(f"filament {f}: {row['verdict']} -- {row['note']}")
        return {"ok": not problems and bool(results), "volts": volts, "rail_v": rail,
                "limit_ma": limit_ma, "results": results, "counts": counts,
                "problems": problems}

    # ── Board self-test & I2C diagnostics ─────────────────────────────────────
    # The GUI's I2C panel, as API calls. These ask "is the hardware wired up and
    # answering", not "is the filament good" -- for the latter see the test
    # flows below.
    #
    # All four return {controller: {...}} keyed by 1/2, covering only CONNECTED
    # controllers. A controller missing from the result was not reached, which
    # is NOT the same as a controller that answered with nothing; each one also
    # carries its own `*_error` key when that controller's own call failed, so
    # one dead controller never silently shrinks the other's result.

    def chip_health(self) -> dict:
        """I2C presence scan (CH_GET_PRESENT): which chips answer, per board.

        Read-only and safe at any time, including while a schedule fires.
        Returns {"ok", "controllers": {"1": {...}}}; a controller whose scan
        raised carries "present_error" instead of counts.

        This is NOT present_filaments(): that one sleeps every board to power
        the presence-sense rail and returns global filament indices; this reads
        the chips and returns the per-controller health the GUI's I2C panel
        shows. They also disagree by design -- a board can answer I2C here and
        still be unusable as a filament.
        """
        return self._post("/api/present", {}, timeout=30.0)

    def diagnosis(self) -> dict:
        """Deep per-board, per-chip I2C classification (CH_GET_DIAGNOSIS).

        Each chip lands in one of: operational / register-only / address-only /
        missing -- so a chip that ACKs its address but will not talk registers
        is distinguishable from one that is simply absent. That distinction is
        the whole point: both look like "not working" from every other read in
        this client.

        THREE THINGS IN A DUMP THAT LOOK LIKE FAULTS AND ARE NOT. Each of
        these was read as a fault on this bench before being run down:

          hv_io_state never better than `addr`, on EVERY channel
              The HV-current expander (0x23) is NOT FITTED -- these are 3-chip
              boards. The firmware's own `scan` says so outright:
              "hv=not fitted (3-chip board; 0x23 removed)". Nothing to
              diagnose, and it will never improve.

          tps_state / ina_state = `addr` on a populated, healthy channel
              Those chips sit behind the isolated 12 V rail. With the filaments
              at STOP the rail is off, so they ACK their address and nothing
              else. Energise before reading anything into it.

          tps_state / ina_state = `op`
              Impossible by construction: their operational-register writes
              have side effects (voltage change, measurement reset), so the
              firmware leaves both `op` masks at 0 permanently. `reg` is their
              ceiling.

        A board-wide `addr` CAN mean the control cable is unplugged -- that
        cost a working RP2350 a near-replacement here -- but check the three
        above first.

        WHAT THIS CANNOT TELL YOU: whether the bus wedged or the devices went
        quiet. Both arrive here as `missing`, and they send you to opposite
        ends of the hardware. The RP2350's own counters separate them, over its
        USB serial (`i2cstat`; no Python wrapper yet):

            timeouts/recoveries/busClears/sdaStuck climbing
                -> the BUS wedged, a slave was holding SDA
            nacks climbing with timeouts flat
                -> the bus is fine, the DEVICES are not answering (no ISO
                   power, absent board, dead chip)

        Measured here: a channel whose entire board set read `missing` had
        timeouts=0, recoveries=0, sdaStuck=0 and only NACKs -- nothing had ever
        been stuck, and time spent on the I2C driver for it was time wasted.
        `i2cstat clear` zeroes the counters, which is what makes a
        single-operation before/after measurement possible.

        Read-only, ~300 ms per controller. Returns {"ok", "controllers": {...}},
        each carrying "channel_mask" (the host's poll set) and
        "fw_channel_mask" (the firmware's own, kept only for diagnostics -- it
        is inert), or "error".
        """
        return self._post("/api/diagnosis", {}, timeout=30.0)

    def read_tca9554(self) -> dict:
        """Full TCA9554 expander register dump per channel (CH_READ_TCA9554).

        Config/input/output/polarity registers with the per-read ACK flag.

        THE VALUES ARE ONLY MEANINGFUL WHERE `ok` SAYS SO. `ok` is a 4-bit mask
        of which reads ACKed -- bit0 config(0x03), bit1 input(0x00), bit2
        output(0x01), bit3 polarity(0x02) -- and a register that did not answer
        comes back as 0, which is a perfectly legal register value. A chip
        reading `config=0 input=0 output=0 polarity=0 ok=0` has told you
        NOTHING; the same four zeros with `ok=15` is a real, all-zero chip.
        Gate on `ok` before reading any value; `ok=15` is the only fully
        trusted row.

        The partial values carry information of their own. `ok=1` means the
        FIRST read of the burst landed and the rest did not -- a different
        fault from `ok=0`. On this bench that exact pattern (first transaction
        answered, everything after it silent until the bus went idle) was the
        entire signature of a bad channel, and it is invisible unless the mask
        is read. It survived swapping that channel's I2C implementation
        outright, which is what ruled the controller out and sent the hunt to
        the channel's own hardware.

        Read-only. Returns {"ok", "controllers": {...}} with "tca9554_error" on
        a controller that failed.
        """
        return self._post("/api/tca9554-read", {}, timeout=30.0)

    def self_test(self) -> dict:
        """TCA9554 toggle self-test (CH_TCA9554_SELF_TEST) + a chip-health scan.

        THIS ONE DRIVES PINS. The backend refuses it on any controller that is
        running a schedule and says so in that controller's "selftest_error"
        rather than disarming for you -- stopping someone else's run to satisfy
        a diagnostic is not a decision this call gets to make. Disarm first.

        Non-destructive on an idle controller: it toggles the expander outputs
        and reads them back. On firmware without the 0x60 handler it falls back
        to deriving chip liveness from the 0x61 register read's ACK flags, so a
        missing handler degrades to a weaker answer rather than to a wrong one.

        Returns {"ok", "controllers": {...}}.
        """
        return self._post("/api/selftest", {}, timeout=60.0)

    # ── HV switch toggle test ─────────────────────────────────────────────────

    _HV_SETTLE_MS = 12          # host-side settle before reading the 165 back
    _HV_RETRY_ATTEMPTS = 4      # transient-only retries
    _HV_RETRY_DELAY_S = 0.2
    _HV_POLLPAUSE_EVERY = 8     # re-arm the auto-expiring pause every N switches

    def _hv_cmd_transient(self, r: dict) -> bool:
        """True if this failure is worth retrying.

        A transport timeout (no status frame came back at all) or a momentarily
        busy device mailbox is transient. VERIFY_FAIL is NOT: it means the
        switch did not actuate, which is the very thing the test is looking
        for. Retrying it would turn a dead switch into a passing one.
        """
        if r.get("ok"):
            return False
        st = ((r.get("response") or {}).get("status"))
        if not st:
            return True              # no status frame => transport error
        return st == "BUSY"

    def _hv_cmd_retry(self, command: str, body: dict, controller: int) -> dict:
        saved, self.max_retries = self.max_retries, 0
        try:
            return self._hv_cmd_retry_(command, body, controller)
        finally:
            self.max_retries = saved

    def _hv_cmd_retry_(self, command: str, body: dict, controller: int) -> dict:
        for attempt in range(self._HV_RETRY_ATTEMPTS):
            # ONE layer of retry, not two. _post() already retries transients
            # with exponential backoff (3 attempts, 0.5 s then 1.0 s), and this
            # loop adds 4 more at 200 ms -- they MULTIPLY. Measured on a
            # 48-switch run: the first five operations took 11, 13, 20, 23 and
            # 8 seconds, about 76 s before the link settled and the remaining
            # 43 switches ran at 0.2-1 s each. From the GUI, which has no
            # progress at this granularity, that opening stretch is
            # indistinguishable from a hang.
            #
            # A per-bit switch read has no business waiting 20 s: nothing about
            # the answer improves, and the test's own INCONCLUSIVE verdict
            # already means "re-run this one". So the outer loop owns the
            # policy and the inner client is told not to retry at all.
            #
            # /api/power-cmd, NOT /api/cmd: the HV switch opcodes (notably
            # HV_REFRESH_FEEDBACK, which is what forces a FRESH 165 read) are
            # only routed by the power-cmd handler. /api/cmd answers
            # "unknown command" for it, and that reads as a transport error --
            # i.e. every switch would come back INCONCLUSIVE rather than the
            # test failing loudly. Measured exactly that: 8/8 inconclusive,
            # restored_off 0/8, on a bench whose switches are fine.
            r = self._post("/api/power-cmd", {"controller": int(controller),
                                              "command": command, **body}, timeout=10.0)
            if not self._hv_cmd_transient(r):
                return r
            if attempt < self._HV_RETRY_ATTEMPTS - 1:
                time.sleep(self._HV_RETRY_DELAY_S)
        return r

    def _hv_force_read_bit(self, controller: int, channel: int, bit: int,
                           value: bool) -> int | None:
        """Drive one HV switch and read the 165 sense line back. 0/1, or None on
        a transport error (which is INCONCLUSIVE, not a failure).

        Why the host settle: the firmware's own HV_SET_BIT verify reads the 165
        the instant after latching the 595, racing the switch's physical
        settling, so a marginal switch verifies differently run to run. The
        settle cannot go in the firmware (it lives in the core-1 verify chain on
        a tight stack), so it lives here: force-write the bit (no firmware
        verify, no fault/clear), wait, force a FRESH 165 read, then fetch.
        """
        w = self._hv_cmd_retry("HV_SET_BIT",
                               {"channel": channel, "bit": bit,
                                "value": bool(value), "force": True}, controller)
        if not w.get("ok"):
            return None
        time.sleep(self._HV_SETTLE_MS / 1000.0)
        rf = self._hv_cmd_retry("HV_REFRESH_FEEDBACK", {"channel": channel}, controller)
        if not rf.get("ok"):
            return None
        g = self._hv_cmd_retry("HV_GET_ALL_BYTES", {}, controller)
        fb = ((g.get("response") or {}).get("decoded") or {}).get("feedback") if g.get("ok") else None
        if not fb:
            return None
        return (int(fb[channel]) >> bit) & 1

    def hv_switch_test(self, controller: int = 1,
                       channel_mask=None,          # None = all 8; or 0xFF-style
                                                    # int, or a list of channels
                       progress=None) -> dict:
        """Exercise every HV grid switch on the selected channels: force it ON,
        read the 165 sense back TWICE, then restore it OFF.

        Tests the SWITCH, not emission. **Run with the HV voltage at 0** -- this
        actuates the grid switches, and the read-back says whether the switch
        moved, which has nothing to do with whether HV is present. For the
        expander CHIP rather than the switch, use self_test().

        channel_mask: None for all 8, a bitmask (bit N = channel N, so 0b11 is
        CH1+CH2), or an explicit list like [0, 1]. Channels are 0-based here and
        labelled CH1..CH8 in the result, matching the GUI.

        ## Three outcomes, not two

        A switch that reads back inconsistently is NOT a pass and NOT a
        failure -- it is a marginal switch, and collapsing it either way loses
        the one thing worth knowing about it:

            pass          actuated on both reads AND released when restored
            dead          both reads returned 0 -- consistently did not actuate
            stuck_on      actuated, but did NOT release when driven back OFF --
                          the grid is left connected, which is worse than a
                          switch that never closes
            inconclusive  the two reads disagreed, a read never arrived, or the
                          release could not be confirmed

        Retries follow the same rule: a transport timeout or a busy mailbox is
        retried, a VERIFY_FAIL never is. Retrying the failure the test exists to
        find would turn a dead switch into a passing one.

        Returns {"ok", "pass", "results": {"CH1.1": "pass"|"dead"|
        "inconclusive", ...}, "dead": [...], "inconclusive": [...], "counts",
        "channels", "restored_off"}. Never raises.
        """
        say = progress or (lambda _msg: None)
        if channel_mask is None:
            channels = list(range(8))
        elif isinstance(channel_mask, int):
            channels = [c for c in range(8) if channel_mask & (1 << c)]
        else:
            channels = sorted({int(c) for c in channel_mask})
        bad = [c for c in channels if not (0 <= c < 8)]
        if bad:
            return {"ok": False, "error": f"channel(s) {bad} outside 0..7",
                    "results": {}}
        if not channels:
            return {"ok": False, "error": "channel_mask selected no channels",
                    "results": {}}

        # A disconnected controller makes every read fail, which is reported
        # honestly per switch (inconclusive) but came back ok=True overall --
        # "the test ran" for a test that could not reach the hardware. Check
        # first and say so.
        st = (self.status().get("controllers") or {}).get(str(int(controller)), {})
        if not st.get("connected"):
            return {"ok": False, "results": {},
                    "error": f"controller {controller} is not connected — no "
                             f"switch could be reached, so there is no result "
                             f"to report (not even a failing one)"}
        keys = [(c, b) for c in channels for b in range(8)]
        results: dict[str, str] = {}
        restored = 0
        # The pause stops the backend's 1 Hz PING from interleaving with these
        # round trips on the single shared bridge socket. It AUTO-EXPIRES
        # (POLL_PAUSE_MAX_S), which is why it is re-armed below rather than set
        # once -- and why a crash here cannot wedge the poller.
        self._post("/api/poll-pause", {"controller": int(controller),
                                       "paused": True}, timeout=5.0)
        try:
            for n, (c, b) in enumerate(keys):
                if n % self._HV_POLLPAUSE_EVERY == 0:
                    self._post("/api/poll-pause", {"controller": int(controller),
                                                   "paused": True}, timeout=5.0)
                label = f"CH{c + 1}.{b + 1}"
                say(f"{label} ({n + 1}/{len(keys)})…")
                s1 = self._hv_force_read_bit(controller, c, b, True)
                s2 = self._hv_force_read_bit(controller, c, b, True)
                # Restore OFF, and CHECK IT LANDED. This used to count a
                # restore as successful whenever the READ succeeded, ignoring
                # what it read -- so a switch that actuates ON reliably but will
                # not release read back 1, counted as restored, and scored a
                # clean pass. That is the more dangerous failure of the two: the
                # grid is left connected. The GUI showed it as a tick beside the
                # tile's own desired/feedback mismatch marker.
                off = self._hv_force_read_bit(controller, c, b, False)
                if off == 0:
                    restored += 1
                if s1 is None or s2 is None:
                    results[label] = "inconclusive"
                elif s1 == 1 and s2 == 1:
                    # It actuated. Whether it RELEASED is a separate question,
                    # and a switch that will not release is not a pass.
                    if off == 0:
                        results[label] = "pass"
                    elif off == 1:
                        results[label] = "stuck_on"
                    else:
                        results[label] = "inconclusive"
                elif s1 == 0 and s2 == 0:
                    results[label] = "dead"
                else:
                    results[label] = "inconclusive"
        finally:
            # Always resume, including on exception: leaving the PING paused
            # would make the controller look unreachable afterwards.
            self._post("/api/poll-pause", {"controller": int(controller),
                                           "paused": False}, timeout=5.0)

        counts: dict[str, int] = {}
        for v in results.values():
            counts[v] = counts.get(v, 0) + 1
        dead = sorted(k for k, v in results.items() if v == "dead")
        inconc = sorted(k for k, v in results.items() if v == "inconclusive")
        stuck = sorted(k for k, v in results.items() if v == "stuck_on")
        return {"ok": True, "pass": not dead and not inconc and not stuck,
                "results": results, "stuck_on": stuck,
                "dead": dead, "inconclusive": inconc, "counts": counts,
                "channels": [c + 1 for c in channels],
                # Switches whose restore-to-OFF was confirmed. Short of the
                # total means one may still be ON -- worth seeing, not hiding.
                "restored_off": f"{restored}/{len(keys)}"}

    # ── Test & measurement flows ──────────────────────────────────────────────
    # Ports of the GUI's "Calibration & Test" tab, so a flow can be run from a
    # script instead of a browser tab that has to stay open. Same thresholds,
    # same order of operations, same numbers -- see each method for where it
    # deliberately differs.
    #
    # Only the two flows that need nothing but the INA219 are here. The other
    # four (emission short scan, focus leak scan, emission current, emission
    # calibration) all need the DS3502s and the ADS1115 to set and read an HV
    # operating point, which the lab board does not have fitted -- they can be
    # written, but not verified, on this bench, so they are not yet written.

    def read_board_faults(self, filaments=None) -> dict:
        """Per-filament TPS55289 fault / HV-overcurrent flags, keyed by
        USER_INDEX. Returns {} on failure (never raises).

        Each flag is True, False, or **None**. None means the firmware marked
        that field's read as invalid (its `*_valid` twin is clear -- a missing
        or faulty chip, or a board built without it), so the flag is UNKNOWN,
        not "no fault". The GUI's test 1 reads the raw `tps_fault` bit without
        consulting its validity twin, which silently turns every unreadable
        board into a passing one; this is that same data with the hole left
        visible.

        Returns {user_index: {"tps_fault", "hv_overcurrent", "present",
        "controller", "channel", "position"}}.
        """
        want = self._want_filaments(filaments)
        mapping = self.get_mapping().get("mapping") or {}
        site_to_user: dict[tuple, int] = {}
        for row in mapping.get("filaments") or []:
            fid = row.get("filament")
            if fid is None or row.get("slot") is None or row.get("controller") is None:
                continue
            site_to_user[(row["controller"] + 1, row["channel"], row["position"])] = \
                self._user_index_of(fid)
        out: dict[int, dict] = {}
        for cid in sorted({site[0] for site in site_to_user}):
            r = self._get(f"/api/board-snapshot?controller={cid}", timeout=20.0)
            if not r.get("ok"):
                continue
            for b in r.get("boards") or []:
                user_index = site_to_user.get((cid, b.get("channel"), b.get("mux_port")))
                if user_index is None:
                    continue
                out[user_index] = {
                    "index": user_index, "controller": cid,
                    "channel": b.get("channel"), "position": b.get("mux_port"),
                    "present": bool(b.get("present")),
                    "tps_fault": (bool(b.get("tps_fault"))
                                  if b.get("tps_fault_valid") else None),
                    "hv_overcurrent": (bool(b.get("hv_overcurrent"))
                                       if b.get("hv_overcurrent_valid") else None),
                }
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def _thermal_history_raw(self) -> dict:
        """The raw /api/thermal-history response, so callers can tell an EMPTY
        history (nothing commanded yet) apart from an ABSENT one (a backend too
        old to have the endpoint). Both leave thermal_history() returning {},
        and only one of them is fixed by waiting."""
        return self._get("/api/thermal-history", timeout=10.0)

    def thermal_history(self, filaments=None) -> dict:
        """How long each filament has been de-energised, keyed by USER_INDEX.

        A filament that was just run is still hot, and hot tungsten reads a
        substantially higher resistance than cold tungsten — so "resistance"
        without a thermal precondition is not a repeatable number. This is the
        precondition, read from the backend (which outlives any one script and
        therefore remembers what the previous script left hot).

        Returns {user_index: {"state", "energising", "since_command_s",
        "cold_for_s"}}. `cold_for_s` is None while the filament is still
        energised. A filament MISSING from the result is unknown, not cold —
        the backend only knows what it commanded, so a fresh backend or a
        controller reconnect erases the history. Do not substitute 0 or
        infinity for a missing entry; treat it as "must cool it yourself".
        """
        want = self._want_filaments(filaments)
        r = self._thermal_history_raw()
        out = {}
        for fid_s, row in (r.get("filaments") or {}).items():
            user_index = self._user_index_of(int(fid_s))
            out[user_index] = {**row, "index": user_index}
        if want is not None:
            keep = set(want)
            out = {k: v for k, v in out.items() if k in keep}
        return out

    def cool_down(self, filaments=None, *, cool_s: float,
                  poll_s: float = 2.0, progress=None) -> dict:
        """De-energise `filaments` and wait until every one of them has been off
        for at least `cool_s` seconds. Returns once the precondition holds.

        Credits time already served: a filament the backend says has been at
        STOP for 300 s does not get another wait. Only filaments that are
        actually energised (or whose history is unknown) are commanded to STOP,
        precisely so that a filament already cooling does not have its clock
        reset by a redundant STOP.

        There is no default for `cool_s` anywhere in this client and there
        should not be: the right value is the thermal time constant of the real
        filament assembly in its vacuum envelope, which is a property of the
        production rig and cannot be inferred from the bench's dummy loads.
        Measure it once on the real hardware (sweep_filament_impedance()'s
        `hysteresis` output is the instrument for that) and pass that.

        Returns {"ok", "waited_s", "already_cold_s", "stopped": [...],
        "unknown": [...]}.
        """
        say = progress or (lambda _msg: None)
        # A negative cool_s is almost always a computed value that came out
        # wrong (a subtraction of timestamps, say). Treating it as "no wait"
        # silently grants the precondition this call exists to establish.
        if float(cool_s) < 0:
            return {"ok": False, "waited_s": 0.0, "already_cold_s": None,
                    "stopped": [], "unknown": [],
                    "error": f"cool_s={cool_s} is negative; nothing was cooled. "
                             f"A negative wait is not zero wait — it is a number "
                             f"that was computed wrong."}
        targets = self._live_user_indices(filaments)
        if not targets:
            return {"ok": True, "waited_s": 0.0, "already_cold_s": None,
                    "stopped": [], "unknown": []}
        hist = self.thermal_history(targets)
        unknown = [f for f in targets if f not in hist]
        hot = [f for f in targets if (hist.get(f) or {}).get("energising")]
        to_stop = sorted(set(hot) | set(unknown))
        if to_stop:
            say(f"STOP on {len(to_stop)} filament(s) before cooling…")
            self.stop_all(to_stop)
        hist = self.thermal_history(targets)
        # The weakest link sets the wait: one filament that was just running
        # makes the whole batch's measurement warm, not just its own row.
        served = [float((hist.get(f) or {}).get("cold_for_s") or 0.0)
                  for f in targets]
        already = min(served) if served else 0.0
        wait = max(0.0, float(cool_s) - already)
        if wait > 0:
            say(f"cooling {wait:.0f} s (already off {already:.0f} s)…")
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                time.sleep(min(poll_s, max(0.0, deadline - time.monotonic())))
        return {"ok": True, "waited_s": wait, "already_cold_s": already,
                "stopped": to_stop, "unknown": unknown}

    def _thermal_precondition(self, targets, cool_s, say) -> dict:
        """Shared preamble for the measurement flows: optionally establish, and
        always REPORT, the cold-start precondition. Never silently asserts the
        filament was cold."""
        if cool_s is not None:
            cooled = self.cool_down(targets, cool_s=cool_s, progress=say)
        else:
            cooled = None
        raw = self._thermal_history_raw()
        hist = self.thermal_history(targets)
        known = [float(hist[f]["cold_for_s"]) for f in targets
                 if f in hist and hist[f].get("cold_for_s") is not None]
        unknown = [f for f in targets if f not in hist
                   or hist[f].get("cold_for_s") is None]
        coldest = min(known) if known else None
        if not raw.get("ok"):
            # No endpoint (older backend) or the call failed. Report it as what
            # it is -- the precondition is UNVERIFIABLE, which is not the same
            # as unmet, and must not read as met either.
            return {"required_s": cool_s, "coldest_off_s": None,
                    "unknown_history": list(targets), "met": None,
                    # Truncated: a 404's body is a full HTML error page, and an
                    # unabridged one buries every other line of the report.
                    "note": f"thermal history unavailable: "
                            f"{str(raw.get('error') or 'backend did not answer')[:120]}",
                    "cool_down": cooled}
        if cool_s is None:
            note = ("no cool_s requested — R is whatever temperature these "
                    "filaments happen to be at")
            met = None
        elif unknown:
            note = (f"{len(unknown)} filament(s) have no usable cooling history; "
                    f"cannot confirm the cold start")
            met = None
        else:
            met = coldest is not None and coldest >= float(cool_s)
            note = (f"off for at least {coldest:.0f} s" if met else
                    f"only {coldest:.0f} s off, wanted {float(cool_s):.0f} s")
        return {"required_s": cool_s, "coldest_off_s": coldest,
                "unknown_history": unknown, "met": met, "note": note,
                "cool_down": cooled}

    def measure_filament_resistance(self, filaments=None, *,
                                    settle_s: float | None = None,
                                    short_ohm: float | None = None,
                                    open_ma: float | None = None,
                                    cool_s: float | None = None,
                                    progress=None) -> dict:
        """TEST 1 -- filament resistance at the 0.8 V STANDBY floor.

        Drives every live filament (or just `filaments`) to STANDBY, holds for
        `settle_s`, then takes ONE live INA219 V+I pair per board and reports
        R = V/I with a short/open verdict. Cheap and quick -- this is the
        go/no-go screen you run before anything else; sweep_filament_impedance()
        is the careful version.

        STANDBY is genuinely energised (the firmware's 0.8 V floor, ~0.9 A into
        a real filament), so the whole flow runs inside energised() and every
        touched filament is STOPped on the way out -- including on exception or
        Ctrl-C.

        A per-filament STANDBY failure does NOT abort the run: those filaments
        are reported as `standby_fail` and the rest are still measured. Only a
        STANDBY that applied to nothing at all aborts.

        ## This R is NOT a cold resistance

        It is R at the filament's temperature after `settle_s` at the 0.8 V
        floor, and 0.8 V into a real filament is ~0.7 W, so the filament is
        heating for the whole settle. Run this twice back to back and the
        second run reads higher. That is fine for what this test is -- a
        short/open screen, where the thresholds are orders of magnitude away
        from the drift -- but the number must not be recorded as a filament's
        cold resistance, and two runs' numbers are only comparable if both
        started from the same temperature. For a cold resistance, use
        sweep_filament_impedance(), which extrapolates to zero power.

        `cool_s`: de-energise and wait this long before measuring, so runs
        start from a comparable temperature; None (default) skips the wait. The
        thermal state is REPORTED either way, under "thermal" -- there is no
        invented default here, because the right value is a property of the
        real filament assembly. See cool_down().

        settle_s/short_ohm/open_ma default to the GUI's own box values
        (_T1_SETTLE_S / _T1_SHORT_OHM / _T1_OPEN_MA).
        progress: optional callable(str) for a live line; None = silent.

        Returns {"ok", "pass", "results": {user_index: {...}}, "counts",
        "flagged", "thresholds", "thermal"}. Per filament: "R_ohm" (None when
        no current flowed -- an absent measurement, not a fabricated 0 or
        infinity), "bus_mV", "current_mA", "tps_fault", "verdict"
        ("ok"/"short"/"open"/"standby_fail"), "reason".
        """
        settle_s = self._T1_SETTLE_S if settle_s is None else float(settle_s)
        short_ohm = self._T1_SHORT_OHM if short_ohm is None else float(short_ohm)
        open_ma = self._T1_OPEN_MA if open_ma is None else float(open_ma)
        say = progress or (lambda _msg: None)
        thresholds = {"settle_s": settle_s, "short_ohm": short_ohm, "open_ma": open_ma}

        targets = self._live_user_indices(filaments)
        if not targets:
            return {"ok": False, "error": "no live filaments to measure",
                    "results": {}, "thresholds": thresholds}

        thermal = self._thermal_precondition(targets, cool_s, say)
        with self.energised(*targets):
            say(f"STANDBY on {len(targets)} filament(s)…")
            prep = self.standby_all(targets)
            failed = {int(f) for f in (prep.get("failed") or [])}
            applied = int(prep.get("applied") or 0)
            if applied == 0:
                return {"ok": False,
                        "error": prep.get("error") or "STANDBY applied to nothing",
                        "prep": prep, "results": {}, "thresholds": thresholds,
                        "thermal": thermal}
            say(f"settling {settle_s:.1f} s at STANDBY…")
            time.sleep(settle_s)
            say("reading INA219 V/I…")
            vi = self.read_filament_vi_live(targets)
            faults = self.read_board_faults(targets)

        results: dict[int, dict] = {}
        for f in targets:
            entry = vi.get(f) or {}
            fault = (faults.get(f) or {}).get("tps_fault")
            row = {"index": f, "bus_mV": entry.get("bus_mV"),
                   "current_mA": entry.get("current_mA"),
                   "present": bool(entry.get("present")),
                   "tps_fault": fault, "R_ohm": None,
                   "verdict": None, "reason": None}
            if f in failed:
                row["verdict"], row["reason"] = "standby_fail", "STANDBY was refused"
                results[f] = row
                continue
            if not row["present"]:
                row["verdict"], row["reason"] = "absent", "board not present"
                results[f] = row
                continue
            mA, mV = row["current_mA"], row["bus_mV"]
            if mA is None or mV is None:
                # The live read fell back to cache (a schedule is firing) or the
                # board answered nothing. No pair, no resistance -- do not
                # divide a real voltage by a missing current.
                row["verdict"], row["reason"] = "no_reading", "no live V/I pair"
                results[f] = row
                continue
            # R is left None rather than infinity when no current flows: the
            # verdict already says "open", and an infinity here would not
            # survive a round trip through JSON.
            if mA > 0:
                row["R_ohm"] = (mV / 1000.0) / (mA / 1000.0)
            if fault:
                row["verdict"], row["reason"] = "short", "TPS55289 fault flag set"
            elif row["R_ohm"] is not None and row["R_ohm"] < short_ohm:
                row["verdict"] = "short"
                row["reason"] = f"R {row['R_ohm']:.3f} Ω < {short_ohm} Ω"
            elif mA < open_ma:
                row["verdict"] = "open"
                row["reason"] = f"{mA:.0f} mA < {open_ma} mA at {mV:.0f} mV"
            else:
                row["verdict"] = "ok"
            if fault is None and row["verdict"] == "ok":
                # Passed on R alone; the fault bit could not be read, so say so
                # rather than letting the pass imply it was checked.
                row["reason"] = "TPS fault flag unreadable — verdict from R only"
            results[f] = row

        counts: dict[str, int] = {}
        for row in results.values():
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        flagged = [f"F{row['index']}: {row['verdict'].upper()} ({row['reason']})"
                   for row in results.values()
                   if row["verdict"] not in ("ok", "absent")]
        return {"ok": True, "pass": not flagged, "results": results,
                "counts": counts, "flagged": flagged, "thresholds": thresholds,
                "thermal": thermal,
                "dead_skipped": prep.get("dead_skipped") or []}

    @staticmethod
    def fit_cold_resistance(curve) -> dict:
        """Least-squares fit of a filament V-I curve to V = a·I³ + R₀·I, i.e.
        R(I) = a·I² + R₀ -- so R₀ is the cold (zero-current) resistance and `a`
        is the self-heating coefficient.

        curve: [{"v": volts, "i": amps}, ...]; points with a missing v or i are
        skipped.

        ALWAYS returns a dict -- never a bare number and never None:

            {"R0_ohm": float|None, "a": float|None, "points": int,
             "collinearity": float|None, "r0_pinned": bool,
             "rms_residual_frac": float|None, "reason": str|None}

        `R0_ohm` is None whenever the fit could not resolve it, and `reason`
        says which of the four ways it failed. Nothing here ever reports an
        unresolved R₀ as 0 Ω, because downstream that number is compared
        against a short threshold and a fabricated zero reads as a dead short.

        ## Where this deliberately differs from the GUI's fitR0()

        The maths is identical (verified bit-for-bit against the original JS
        across eight curves, including its R₀ < 0 branch). Two of its outputs
        are not carried over, because both are unresolved fits wearing a
        number:

        1. **Singular normal equations** (every point at the same current --
           what an empty board pinned at the voltage floor produces). fitR0()
           returns `{R0: 0, a: 0}`. Here: R₀ None, reason "same current".
        2. **Ill-conditioned fit.** Over a narrow current range I and I³ are
           nearly the same shape, so the split between R₀ and `a` is not
           identifiable and R₀ lands anywhere -- usually negative, which then
           trips fitR0()'s R₀ < 0 branch and pins it to exactly 0. Measured on
           this bench: a load holding 0.99-1.11 A across an 0.88-1.46 V sweep,
           R = 0.89-1.31 Ω throughout, was fitted as R₀ = 0 Ω and would have
           been reported SHORT. `collinearity` (= Σi⁴² / Σi⁶·Σi², in [0,1],
           1 = indistinguishable) was 0.9916 there against 0.85 for sweeps that
           fit properly, so _T6_MAX_COLLINEARITY sits between them.

        A pinned R₀ = 0 from the surviving R₀ < 0 branch is kept, since it is
        the GUI's documented behaviour, but it is flagged `r0_pinned` so the
        caller can refuse to call it a short.
        """
        pts = [p for p in (curve or [])
               if p.get("i") is not None and p.get("v") is not None]
        out = {"R0_ohm": None, "a": None, "points": len(pts),
               "collinearity": None, "r0_pinned": False,
               "rms_residual_frac": None, "reason": None}
        if len(pts) < CTClient._T6_MIN_FIT_POINTS:
            out["reason"] = (f"only {len(pts)} usable point(s), need "
                             f"{CTClient._T6_MIN_FIT_POINTS}")
            return out
        sI6 = sI4 = sI2 = sI3V = sIV = 0.0
        for p in pts:
            i = float(p["i"]); v = float(p["v"])
            i2 = i * i; i3 = i2 * i
            sI6 += i3 * i3; sI4 += i2 * i2; sI2 += i2
            sI3V += i3 * v; sIV += i * v
        det = sI6 * sI2 - sI4 * sI4
        if not det or not (sI6 * sI2):
            out["collinearity"] = 1.0
            out["reason"] = "every point at the same current — R₀ not resolvable"
            return out
        # Cauchy-Schwarz bounds this at 1; it reaches 1 exactly when I and I³
        # are proportional over the sampled currents, i.e. when the sweep never
        # moved the current.
        out["collinearity"] = sI4 * sI4 / (sI6 * sI2)
        if out["collinearity"] >= CTClient._T6_MAX_COLLINEARITY:
            currents = [float(p["i"]) for p in pts]
            span = (max(currents) - min(currents)) / max(currents) * 100
            out["reason"] = (f"current moved only {span:.0f}% over the sweep "
                             f"(collinearity {out['collinearity']:.4f} ≥ "
                             f"{CTClient._T6_MAX_COLLINEARITY}) — R₀ and the I² "
                             f"term are not separable")
            return out
        a = (sI3V * sI2 - sI4 * sIV) / det
        r0 = (sI6 * sIV - sI4 * sI3V) / det
        if r0 < 0:
            r0 = 0.0
            a = (sI3V / sI6) if sI6 else 0.0
            out["r0_pinned"] = True
            out["reason"] = "fitted R₀ was negative — pinned to 0, not measured"
        out["R0_ohm"], out["a"] = r0, a
        # How well the model actually describes this load, as an RMS residual
        # relative to V. Reported, NOT gated on: a threshold would need a
        # population of real filaments to calibrate, and there isn't one on this
        # bench. It exists because R₀ alone looks equally authoritative whether
        # the curve is a tungsten filament or something the a·I²+R₀ form does
        # not fit at all -- the bench's current-limited dummy load fits to 22%
        # and still yields a tidy-looking R₀.
        ss = 0.0
        for p in pts:
            i = float(p["i"]); v = float(p["v"])
            ss += ((a * i * i * i + r0 * i) - v) ** 2 / (v * v) if v else 0.0
        out["rms_residual_frac"] = (ss / len(pts)) ** 0.5
        return out

    def sweep_filament_impedance(self, filaments=None, *,
                                 start_mv: int | None = None,
                                 end_mv: int | None = None,
                                 step_mv: int | None = None,
                                 dwell_s: float | None = None,
                                 short_ohm: float | None = None,
                                 cool_s: float | None = None,
                                 hysteresis_tol: float | None = None,
                                 save: bool = True,
                                 progress=None) -> dict:
        """TEST 6 -- per-filament impedance sweep, fitted to a cold resistance.

        For each live filament in turn: hold VOLTAGE mode at start_mv, dwell,
        read the INA219, step up by step_mv, repeat to end_mv, STOP, then fit
        the collected V-I curve with fit_cold_resistance(). One filament is
        energised at a time, and it is STOPped before the next one starts.

        SLOW -- one filament's sweep is roughly
        `dwell_s * (1 + (end_mv - start_mv) // step_mv)` seconds, so all 96 at
        the defaults is well over an hour. Pass `filaments` to sweep a subset.

        Unlike measure_filament_resistance(), which divides one V by one I at a
        single operating point, this fits a whole curve, so it separates the
        cold resistance R₀ from the self-heating term -- the number you want
        when comparing filaments to each other.

        ## R₀ is a cold resistance only if the filament was actually cold

        R₀ is the fit's extrapolation to zero dissipated power, so it equals the
        room-temperature resistance only when the sweep both STARTS at ambient
        and stays in thermal equilibrium throughout. A filament that ran
        recently is still hot and reads high; one swept faster than it can shed
        heat climbs during the sweep. Neither shows up in the fit -- both just
        move R₀, and the GUI's version reports the result as a cold resistance
        either way.

        Two independent guards, because they fail differently:

        - `cool_s` (default None = no wait): de-energise and wait this long
          before sweeping, crediting time already served. Establishes the start
          condition. Reported under "thermal".
        - The **return point** (always taken): after the last step the sweep
          goes back to `start_mv`, re-measures, and compares R with the opening
          point. This TESTS equilibrium instead of assuming it, and needs no
          knowledge of the filament's thermal constant. Drift beyond
          `hysteresis_tol` (default _T6_HYSTERESIS_TOL) marks R₀ not-cold.

        Per filament, `r0_is_cold` is True / False / None (unverified), with
        `cold_note` saying why, and the run-level `r0_not_cold` lists every
        filament whose R₀ fitted but is not a cold resistance. A not-cold R₀ is
        still returned -- it is a real measurement at an unknown temperature --
        but it is never silently labelled as cold.

        start_mv is clamped up to the firmware's 0.8 V floor: below it the
        regulator does not start at all, so a lower request would silently
        collect points that are all the same voltage.

        save=True writes the curves and fits to the backend's calibration
        directory as `impedance_sweep_<timestamp>.json` + `.csv`.
        progress: optional callable(str); None = silent.

        Returns {"ok", "results": {user_index: {"R0_ohm", "a", "verdict",
        "curve": [{"v","i","mv_set"}], "points"}}, "params", "counts",
        "flagged", "saved"}. A filament whose curve could not be fitted gets
        "R0_ohm": None and verdict "no_fit" -- never a placeholder number.
        """
        start_mv = self._T6_START_MV if start_mv is None else int(start_mv)
        end_mv = self._T6_END_MV if end_mv is None else int(end_mv)
        step_mv = self._T6_STEP_MV if step_mv is None else int(step_mv)
        dwell_s = self._T6_DWELL_S if dwell_s is None else float(dwell_s)
        short_ohm = self._T6_SHORT_OHM if short_ohm is None else float(short_ohm)
        hysteresis_tol = (self._T6_HYSTERESIS_TOL if hysteresis_tol is None
                          else float(hysteresis_tol))
        say = progress or (lambda _msg: None)

        start_mv = max(self._T6_START_MV, start_mv)
        if step_mv <= 0:
            return {"ok": False, "error": f"step_mv={step_mv} must be positive",
                    "results": {}}
        if end_mv < start_mv:
            return {"ok": False,
                    "error": f"end_mv={end_mv} is below start_mv={start_mv}",
                    "results": {}}
        steps = list(range(start_mv, end_mv + 1, step_mv))
        params = {"start_mv": start_mv, "end_mv": end_mv, "step_mv": step_mv,
                  "dwell_s": dwell_s, "short_ohm": short_ohm,
                  "cool_s": cool_s, "hysteresis_tol": hysteresis_tol,
                  "steps_per_filament": len(steps)}

        targets = self._live_user_indices(filaments)
        if not targets:
            return {"ok": False, "error": "no live filaments to sweep",
                    "results": {}, "params": params}

        thermal = self._thermal_precondition(targets, cool_s, say)
        results: dict[int, dict] = {}
        # One energised() around the whole run, not one per filament: if the
        # loop dies partway through, the filament being swept AND any earlier
        # one whose STOP did not land both still get stopped.
        with self.energised(*targets):
            for n, f in enumerate(targets, 1):
                curve = []
                for mv in steps:
                    say(f"F{f} ({n}/{len(targets)}) @ {mv} mV…")
                    r = self.voltage_one(f, mv)
                    if not r.get("ok"):
                        curve.append({"mv_set": mv, "v": None, "i": None,
                                      "error": r.get("error") or "set failed"})
                        continue
                    time.sleep(dwell_s)
                    entry = self.read_filament_vi_live([f]).get(f) or {}
                    mA, mV = entry.get("current_mA"), entry.get("bus_mV")
                    if entry.get("present") and mA is not None and mV is not None and mA > 0:
                        curve.append({"mv_set": mv, "v": mV / 1000.0, "i": mA / 1000.0})
                    else:
                        # Kept in the curve with v/i None so the record shows the
                        # step was attempted; fit_cold_resistance() skips it.
                        curve.append({"mv_set": mv, "v": None, "i": None})
                # Return to the FIRST voltage and re-measure. If the filament is
                # at the same temperature as when the sweep opened, this reads
                # the same R; if the sweep heated it faster than it could shed
                # the heat, it reads higher, and by how much. This is the only
                # thing here that TESTS the fit's premise rather than assuming
                # it -- and unlike a cool-down time, it needs no prior knowledge
                # of the filament's thermal constant, so it works on any rig.
                hysteresis = None
                first = next((p for p in curve if p.get("i")), None)
                if first is not None:
                    say(f"F{f}: return to {steps[0]} mV for the hysteresis check…")
                    rr = self.voltage_one(f, steps[0])
                    if rr.get("ok"):
                        time.sleep(dwell_s)
                        e = self.read_filament_vi_live([f]).get(f) or {}
                        mA, mV = e.get("current_mA"), e.get("bus_mV")
                        if e.get("present") and mA and mV and mA > 0:
                            r_open = first["v"] / first["i"]
                            r_back = (mV / 1000.0) / (mA / 1000.0)
                            hysteresis = {
                                "mv_set": steps[0], "v": mV / 1000.0,
                                "i": mA / 1000.0,
                                "r_open_ohm": r_open, "r_return_ohm": r_back,
                                # Positive = came back hotter than it started.
                                "drift_frac": (r_back - r_open) / r_open if r_open else None,
                            }
                self.stop_one(f)
                fit = self.fit_cold_resistance(curve)
                row = {"index": f, "curve": curve, "points": fit["points"],
                       "hysteresis": hysteresis,
                       "R0_ohm": fit["R0_ohm"], "a": fit["a"],
                       "collinearity": fit["collinearity"],
                       "r0_pinned": fit["r0_pinned"],
                       "rms_residual_frac": fit["rms_residual_frac"],
                       "verdict": "no_fit", "reason": fit["reason"]}
                if fit["R0_ohm"] is None:
                    pass                    # reason already explains which way
                elif fit["r0_pinned"]:
                    # R₀ = 0 here means "the fit wanted a negative one", not a
                    # measured 0 Ω. Calling that SHORT is the false alarm this
                    # whole path exists to avoid.
                    row["verdict"] = "no_fit"
                elif fit["R0_ohm"] < short_ohm:
                    row["verdict"] = "short"
                    row["reason"] = f"R₀ {fit['R0_ohm']:.4f} Ω < {short_ohm} Ω"
                else:
                    row["verdict"], row["reason"] = "ok", None
                # Whether the R0 that came out is a COLD resistance is a
                # separate question from whether the fit converged, and is
                # tracked separately so neither can stand in for the other.
                drift = (hysteresis or {}).get("drift_frac")
                if row["R0_ohm"] is None:
                    row["r0_is_cold"] = None
                elif drift is None:
                    row["r0_is_cold"] = None
                    row["cold_note"] = "no return point — cold start unverified"
                elif abs(drift) > hysteresis_tol:
                    row["r0_is_cold"] = False
                    row["cold_note"] = (
                        f"R at {steps[0]} mV drifted {drift * 100:+.1f}% over the "
                        f"sweep (tol ±{hysteresis_tol * 100:.0f}%) — the filament "
                        f"did not stay at one temperature, so R₀ is not a cold "
                        f"resistance")
                elif thermal["met"] is False:
                    row["r0_is_cold"] = False
                    row["cold_note"] = f"warm start: {thermal['note']}"
                elif thermal["met"] is None:
                    row["r0_is_cold"] = None
                    row["cold_note"] = f"cold start unconfirmed: {thermal['note']}"
                else:
                    row["r0_is_cold"] = True
                    row["cold_note"] = None
                results[f] = row
                say(f"F{f}: " + (f"R₀ = {row['R0_ohm']:.4f} Ω"
                                 if row["R0_ohm"] is not None else "no fit"))

        counts: dict[str, int] = {}
        for row in results.values():
            counts[row["verdict"]] = counts.get(row["verdict"], 0) + 1
        flagged = [f"F{row['index']}: {row['verdict'].upper()} ({row['reason']})"
                   for row in results.values() if row["verdict"] != "ok"]
        not_cold = [row["index"] for row in results.values()
                    if row.get("R0_ohm") is not None and row.get("r0_is_cold") is not True]
        out = {"ok": True, "pass": not flagged, "results": results,
               "params": params, "counts": counts, "flagged": flagged,
               "thermal": thermal, "r0_not_cold": sorted(not_cold),
               "saved": None}
        if save:
            out["saved"] = self.save_calibration("impedance_sweep", {
                "params": params,
                "curves": {str(k): v["curve"] for k, v in results.items()},
                "r0": {str(k): v["R0_ohm"] for k, v in results.items()},
                "a": {str(k): v["a"] for k, v in results.items()},
                # Saved alongside R0 on purpose: a stored cold resistance with
                # no record of whether the filament was cold is not a
                # calibration, it is a number.
                "r0_is_cold": {str(k): v.get("r0_is_cold") for k, v in results.items()},
                "hysteresis": {str(k): v.get("hysteresis") for k, v in results.items()},
                "thermal": thermal,
            })
        return out

    def save_calibration(self, name: str, data: dict) -> dict:
        """Write a calibration/measurement record to the backend's host disk as
        `<name>_<timestamp>.json` plus a flat `.csv` of `data["curves"]`.

        The file lands next to backend.py (its `calibration/` directory), NOT
        next to the calling script -- the backend is what owns the disk here.
        `name` is sanitised by the backend to [A-Za-z0-9._-].

        Returns {"ok", "json": path, "csv": path, "filaments"}; never raises.
        """
        return self._post("/api/calibration/save",
                          {"name": str(name), "data": data}, timeout=20.0)
