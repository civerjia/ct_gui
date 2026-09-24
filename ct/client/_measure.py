"""CTClient: STM32 per-pulse HV current measurement.

One part of the client class, split out of one 9900-line file by section:
    STM32 per-pulse HV CURRENT measurement

It is a mixin: CTClient in _client.py inherits it, and every name the methods
use comes from _base (star-imported, underscore names included -- see
_base.__all__) or, for CTClient itself, is bound in by _client.py.
"""
from ._base import *  # noqa: F401,F403


class _MeasureMixin:
    # ── STM32 per-pulse HV CURRENT measurement ──────────────────────────────
    # Everything above (fire_single_pulse, hv_grid_set, ...) controls WHEN/
    # WHICH HV switch fires. None of it tells you how much current actually
    # flowed -- that's a SEPARATE measurement subsystem: the STM32G431 sitting
    # on the MASTER controller's link runs a hardware pulse_detector that
    # measures every REAL rise/fall edge on the emission-current line directly
    # (no amplitude threshold, no guessed timing) once armed. There is exactly
    # ONE detector, on the master -- no `controller` parameter on any of these,
    # unlike every board/HV method above.
    #
    # Shared with the GUI: pulse_arm()/pulse_disarm() hit the SAME
    # /api/adc/pulse-arm|disarm endpoints as the GUI's "Stream" button and
    # "Record measurement" card, which backend.py reference-counts
    # server-side -- arming here while a GUI tab already has Stream or Record
    # running just JOINS that arm (doesn't reset it, and your requested
    # rate_hz is ignored if you're not the first arm-er); disarming here only
    # actually disarms the hardware once nothing else still wants it armed.
    # Safe to run this script alongside an open GUI tab; just don't assume
    # you got the rate_hz you asked for if something else armed it first.

    def ready_arm(self, rate_hz: int = 1000000, n_samples: int = 2000,
                  ttl_ms: int | None = None,
                  bg_gap_us: float | None = None,
                  bg_window_us: float | None = None) -> dict:
        """Arm the pulse-envelope RELAY plus the STM32 detector inside it.

        bg_gap_us / bg_window_us are ONE SYMMETRIC PAIR applied to BOTH sides of
        the envelope: settle for `gap`, then average `window`.

            <- gap 200 -><- win 50 ->| envelope |<- win 50 -><- gap 200 ->
                  (pre: SUBTRACTED)                 (post: reference)

        Pre and post share the numbers on purpose -- same front end, same
        disturbance beside each edge. The gap keeps that disturbance out of the
        average, and it matters most on the PRE side: `integral = Sx -
        (F-R)*mean_bg`, so a biased pre-background biases the charge in
        proportion to pulse width. (The pre side used to have no gap at all and
        a shorter window than the post side, i.e. the less accurate average was
        the one being subtracted.)

        Defaults _BG_GAP_US / _BG_WINDOW_US. Both in MICROSECONDS; at 1 MSPS
        that is samples one-for-one. The ESP32 refuses a window outside 4..128
        samples rather than letting the STM32 clamp it silently.

        Together they set MIN_INTER_PULSE_US: a pulse needs gap + window of
        quiet on each side, so 200 + 50 twice is 500 us. fire_single_pulse()
        enforces it.

        ⚠ THE PRE-SIDE GAP IS NOT CONFIRMABLE YET. It rides a new 20-byte
        PULSE_CFG tier; STM32 firmware without that tier ignores the trailing
        bytes silently, and every capability bit is already allocated (bit 7 is
        documented as the last free one), so nothing can report whether it was
        applied. ready_status()["bg_gap"] echoes what was REQUESTED. The window
        and the POST gap ride tiers that already exist and do take effect.

        This is the one you want when you intend to MEASURE fired pulses, and it
        is what fire_single_pulse(measure=True) uses. The STM32 times each pulse
        from the real edge on its PA4 pin; that pin is driven by the ESP32
        mirroring the RP2350's own pulse-envelope output. Arm only the detector
        (pulse_arm) and PA4 never moves, so a fire measures nothing at all --
        the detector reports zero events while everything else looks healthy.
        """
        body: dict = {"rate": int(rate_hz), "n_samples": int(n_samples)}
        # Omitted (not 0) when unspecified: the ESP32 uses its own default, and
        # ttl_ms=0 explicitly DISABLES the auto-disarm -- sending 0 to mean
        # "unspecified" would remove the recovery this exists for.
        if ttl_ms is not None:
            body["ttl_ms"] = int(ttl_ms)
        # The wire wants SAMPLES; callers here think in microseconds like every
        # other timing argument in this client, so convert at the boundary using
        # the rate actually being armed. Omitted (not 0) when unspecified -- 0 is
        # a real value on the wire meaning "do not measure that background at
        # all", so it must not double as "caller said nothing".
        for key, us in (("bg_gap", bg_gap_us), ("bg_window", bg_window_us)):
            if us is not None:
                body[key] = max(0, int(round(float(us) * rate_hz / 1_000_000)))
        return self._post("/api/adc/ready-arm", body)

    def recover(self, stop_heating: bool = False) -> dict:
        """Clear state left behind by an operation that did not finish.

        A killed script, a Ctrl-C, or a backend that exited before its cleanup
        leaves the pulse-envelope relay armed, the STM32 CS claimed, or a
        schedule armed — and every later run then fails with "already armed" or
        an arm reject that reads like a hardware fault. `try/finally` and
        energised() cannot help: they need the process to still be alive.

        Returns {"ok", "was_stuck": bool, "found": {...}, "cleared": [...]}.
        `found` is reported whether or not anything needed clearing, so a
        recurring leak is visible instead of being quietly fixed each time.

        Does NOT de-energise filaments unless stop_heating=True: heat is not
        what gets a later run stuck, and stopping it could interrupt somebody
        else's legitimate run. Energised filaments are listed either way.

        The ESP32 also reclaims an abandoned arm on its own after a timeout
        (60 s by default) — this is the immediate version of that, for when you
        do not want to wait. ready_status()["ttl_expiries"] counts how many
        arms the timeout has had to reclaim; non-zero means some caller's
        cleanup is not running.
        """
        return self._post("/api/recover", {"stop_heating": bool(stop_heating)},
                          timeout=30.0)

    def ready_status(self) -> dict:
        """Whether the pulse-envelope relay is armed, and how many edges it has
        relayed. Useful when an arm is refused as "already armed" -- there is no
        owner recorded, so this is all there is to go on."""
        return self._post("/api/adc/ready-status", {}, timeout=5.0)

    def ready_renew(self) -> dict:
        """Push the relay's auto-disarm deadline out by its TTL.

        Only needed for a run that outlasts the TTL (60 s by default) — an
        ordinary fire finishes well inside it. The timeout exists to reclaim an
        ABANDONED arm, so renewing is the exception, not a keepalive you are
        expected to run."""
        return self._post("/api/adc/ready-renew", {}, timeout=5.0)

    def ready_disarm(self) -> dict:
        """Stop relaying pulse envelopes and release the STM32 CS claim."""
        return self._post("/api/adc/ready-disarm", {})

    def pulse_arm(self, rate_hz: int = 1000000) -> dict:
        """Arm the STM32 per-pulse current detector. Nothing is measured
        until pulses actually fire -- this just gets the STM32's ADC
        streaming and its hardware edge-triggered detector armed and
        waiting. Reference-counted with the GUI's Stream/Record (see the
        section comment above) -- safe to call even if a GUI tab already
        has one of those running; check result["shared"]/["other_users"]
        if you need to know whether you got your own arm or joined one."""
        return self._post("/api/adc/pulse-arm", {"rate": int(rate_hz)}, timeout=10.0)

    def pulse_disarm(self) -> dict:
        """Release this script's claim on the shared detector arm. Only
        actually disarms the STM32 if the GUI isn't also using it right
        now (see the section comment above) -- a result with
        {"shared": True, "still_armed_for": [...]} means it's still armed
        for someone else, which is NOT an error, just information."""
        return self._post("/api/adc/pulse-disarm", {}, timeout=5.0)

    def pulse_cursor(self) -> int:
        """Where the pulse log is RIGHT NOW, as a `since` value for later.

        Read this before firing, then pass it to pulse_events() afterwards to
        get only your own events:

            cursor = ct.pulse_cursor()
            ...                                # fire
            r = ct.pulse_events(cursor)

        Implemented as pulse_events(0) and taking `last_id` from the reply,
        which every reply carries regardless of `since`. That costs one
        transfer of the ring (at most 128 events) and is correct for the whole
        id range, forever.

        The obvious alternative -- pass a huge `since` so nothing can be newer,
        get zero events and a truthful last_id -- has a CEILING and is the
        reason this method exists. `pulse_id` is a uint32, but the ESP32 parses
        the query parameter through Arduino's String::toInt(), which returns a
        SIGNED long: values above 2**31-1 saturate rather than wrap (verified
        on the wire -- since=2**32 returns count 0, where a wrap to 0 would have
        returned the whole ring). So no cursor above 2,147,483,647 can be
        expressed at all, and once pulse_id passes that the trick silently stops
        excluding old events instead of failing. At a scan a minute that is
        centuries away; at 1000 pulses/s it is 23 days, and the id only resets
        when the ESP32 reboots.

        Returns 0 if the read fails -- which means "from the beginning", the
        safe direction: you see extra events rather than silently missing yours.
        """
        r = self.pulse_events(0)
        return int(r.get("last_id") or 0) if r.get("ok") else 0

    def pulse_events(self, since: int = 0) -> dict:
        """Poll STM32-measured pulse events NEWER than `since`.

        WHAT `since` IS. The ESP32 keeps a rolling log of measured pulses, each
        with a monotonically increasing `id`, and that log KEEPS GROWING -- it
        is not cleared when you fire. `since` is a cursor into it: you get back
        only events whose id is greater than the number you pass. Without it
        every poll hands you the whole backlog, including pulses from a run an
        hour ago, with no way to tell which ones were yours.

        HOW TO USE IT. Read the cursor BEFORE firing, then poll with it after:

            since = ct.pulse_cursor()      # where the log is now
            ...                            # fire
            r = ct.pulse_events(since)     # only yours

        Then keep `r["last_id"]` and pass it as the next `since`.

        Do not reach for a huge `since` to read the cursor -- see
        pulse_cursor() for why that has a ceiling this does not.

        `since=0` (the default) means "everything still in the log", which is
        rarely what you want. fire_single_pulse(measure=True) does all of this
        for you; this is the manual form for when you fire some other way.

        Each event:
            "id"        monotonic event id (use as the next `since`)
            "t_us"      STM32 sample index at the pulse start (R). Free-running
                        since boot, so it WRAPS every 2**32 samples -- about
                        71.6 minutes at 1 MSPS. Differencing two of these
                        across a wrap gives nonsense: order by "id", and use
                        "recv_ms" for wall-clock.
            "on_us"     MEASURED pulse width, from the real envelope on the
                        STM32's PA4 pin -- not the commanded width. Compare it
                        against what you asked for; they should agree closely.
            "peak"      highest raw code inside the pulse. ABSOLUTE --
                        the background is NOT removed, so on a filament that
                        is already emitting, most of this number can be
                        standing DC rather than pulse. It is also a SINGLE
                        sample, so it carries the full noise of one
                        conversion: on this bench peak ran ~7 mA above
                        plateau on the same pulse, all of it circuit noise.
                        Do not report peak as "the pulse current"; use
                        plateau, minus bg. **None when not
                        measured** (empty envelope), on firmware with the
                        measure_flags capability. Older firmware reports the
                        field's initial 0 there, which converts to a confident
                        ~-32 mA -- so "empty_envelope" below, derived from
                        on_us, stays the authoritative test and this null is
                        the second layer.
            "plateau"   mean raw code over [rise + plateau_margin, fall),
                        margin = 0 here so it is the whole envelope --
                        INCLUDING the rise and fall ramps, despite the name.
                        ABSOLUTE: the background is NOT removed. The pulse's
                        own current is `plateau - bg`, and nothing in the
                        event carries that already-subtracted (see
                        pulse_events_ma()'s plateau_net_ma, which does).
                        Because margin is 0, plateau*duration and `integral`
                        cover the SAME span, so they agree to ~0.1% on this
                        bench -- that agreement checks the two paths use the
                        same background, it does not independently confirm
                        the plateau level. **None
                        when not measured** (empty range), on firmware with the
                        measure_flags capability. Older firmware reports "peak"
                        instead, with no flag.
                        Do NOT try to detect that by testing plateau == peak: a
                        genuinely flat pulse has floor(mean) == max, so the test
                        misfires on the CLEANEST data. With margin at 0 the
                        empty case needs duration_samples <= 0 -- a zero-length
                        envelope -- which a real pulse never produces.
            "bg"        PRE-pulse background: floor of the mean over
                        `background_n` samples ending `background_gap` samples
                        BEFORE the rise. Taken RETROSPECTIVELY out of the
                        STM32's sample history at the moment the rise is
                        detected -- nothing waits, because a pulse's arrival
                        cannot be predicted. Symmetric with post_bg: one
                        gap/window pair (ready_arm's bg_gap_us/bg_window_us,
                        default 200 us / 50 us) configures both sides.
                        This is a LEVEL, not a correction: it is the number
                        you subtract from peak/plateau, and it is what
                        `integral` already has subtracted.
                        Pulses closer together than MIN_INTER_PULSE_US leave
                        no clean history, so the pre-window would hold the
                        previous pulse's tail -- ready_arm() refuses that
                        spacing rather than quietly degrading bg, integral
                        and sigma.
            "post_bg"   mean AFTER the pulse: the STM32 waits ~50 us for the
                        signal to settle, then averages ~50 us. **None when it
                        was not measured** -- either the window is configured
                        to 0 samples, or the next pulse arrived before even one
                        sample could be taken. None, not 0: 0 is a perfectly
                        legal post-pulse current and the two must not look
                        alike. Guard with `is not None`.
            "bg_sigma4" 4x the background sigma (sigma = bg_sigma4/4)
            "integral"  background-subtracted sum over the pulse, in raw
                        counts: round(Sigma(x) - (F-R)*Sigma_bg/n), using the
                        EXACT background mean -- not the rounded "bg" above, so
                        it does not carry that field's up-to-1-code error.
                        SIGNED -- pure noise
                        sums to about zero and a pulse dimmer than its own
                        background is legitimately negative. Use
                        pulse_events_ma()'s "integral_mams" rather than scaling
                        it by hand -- the rate that conversion needs is
                        "rate_hz" below, not the one you asked for.
            "empty_envelope"  True when on_us == 0: a rise and a fall landed
                        on the SAME sample, which is a PA4 glitch, not a pulse
                        -- and the STM32 still commits a full, normal-looking
                        event for it. In that event peak is its INITIAL value
                        (0), integral is 0, and plateau is empty; none are
                        measurements. peak_ma/plateau_ma/integral_mams are
                        therefore omitted or None on such events (0 counts would
                        otherwise convert to a confident ~-32 mA, and the
                        integral to a legal 0.0 charge). bg and post_bg stay
                        valid -- bg is snapshotted at the rise and post_bg is
                        measured normally -- so those still convert.
                        Discard these events, or keep them explicitly as
                        glitches; do not average them in.
            "rate_hz"   the rate this pulse was ACTUALLY sampled at, reported
                        by the STM32 per event. Its timer runs at
                        170 MHz / an integer divider, so the achieved rate
                        rarely equals the requested one, and it can change
                        between pulses. **None on firmware too old to report
                        it.** on_us/integral and every sample-count parameter
                        are in samples of THIS rate -- so with it None, they
                        cannot be turned into time or charge at all.
            "recv_ms"   host receive time

        WHICH FIELDS HAVE THE BACKGROUND REMOVED -- read this before
        quoting any of them as "the pulse current":

            peak      ABSOLUTE   background INCLUDED   single sample (noisy)
            plateau   ABSOLUTE   background INCLUDED   mean over the envelope
            bg        ABSOLUTE   the background itself (pre-pulse level)
            post_bg   ABSOLUTE   the background itself (post-pulse level)
            integral  NET        background ALREADY REMOVED, exactly:
                                 Sigma(x) - (F-R)*mean_bg

        So peak and plateau are levels measured against the ADC's own zero,
        NOT against the filament's standing emission. Measured on this
        bench at 2.8 A heating: bg 5.78 mA, plateau 15.86 mA -- the pulse
        contributed 10.08 mA and the other 5.78 mA was already flowing
        before it. Quoting plateau_ma as the pulse current overstates it by
        the whole background, and the hotter the filament the worse that
        gets. Subtract: `plateau_ma - bg_ma` (pulse_events_ma() hands you
        that as plateau_net_ma). `integral`/`integral_mams` need no such
        subtraction -- doing it twice is the mirror-image mistake.

        peak/plateau/bg/post_bg are RAW ADC
        counts, not mA; convert with pulse_ma()/pulse_events_ma() below,
        never by hand (the correct conversion needs a LIVE reference
        reading, not a fixed constant -- see pulse_ma's docstring).
        pulse_events_ma() additionally adds, per event:
            "integral_mams"  charge in mA*ms, = slope * integral / rate_hz.
                        **None** when it could not be computed; see
                        "integral_mams_unavailable" for which reason.
            "integral_mams_unavailable"  present only when integral_mams is
                        None, naming which fact was missing:
                          "rate_unknown"  old firmware sent no sample rate, so
                              there is nothing to divide by (assuming 1 MSPS
                              would scale the answer by whatever the real rate
                              turned out to be).
                          "integral_clamped"  the STM32 reports the OLD
                              clamp-at-zero integral, which is biased upward on
                              weak pulses. Converting it would hand back a
                              number that is wrong by an amount nothing in the
                              data reveals. Flash both sides.
                          "integral_form_unknown"  the STM32 never answered
                              GET_INFO, so which of the two forms it sends is
                              unknown -- distinct from knowing it is old.
                          "empty_envelope"  on_us == 0, so integral is 0
                              because nothing was integrated -- not because the
                              charge was zero.
                          "saturated"  integral hit INT32_MAX/INT32_MIN.
            "integral_saturated"  True when integral hit 0xFFFFFFFF or
                        duration hit 0xFFFF. The firmware clamps WITHOUT
                        setting any flag, so a saturated reading cannot be
                        told from a real one by value -- hence None above.
            "integral_mams_sigma"  the scatter in that charge from background
                        noise alone: sigma*sqrt(N) converted the same way.
                        A charge smaller than its own sigma has NOT been
                        distinguished from noise -- compare the two before
                        believing a small value, and expect roughly a third of
                        pure-noise pulses to land outside +/-1 sigma.
                        **None when bg_sigma4 is 0** (flagged
                        "background_flat"): a live front end always has some
                        spread, so zero means the input is stuck or unpowered.
                        Reporting 0 would make the |charge| > sigma test pass
                        for anything -- the guard would silently stop guarding.
        Returns {"ok", "events": [...], "last_id": int}."""
        return self._get(f"/api/pulse-events?since={int(since)}", timeout=5.0)

    def get_ads1115_ref_mv(self) -> float | None:
        """Live ADS1115 "1.2V ref" channel reading (mV) — the external
        differential circuit's ACTUAL reference right now (nominally
        ~1.2V, but drifts board-to-board and with temperature — treating
        it as a fixed constant is exactly what overstated current by
        ~35% before this was fixed in the GUI). Needed by pulse_ma() for
        an accurate conversion. Returns None if the read failed (master
        not connected, etc.) — pulse_ma() falls back to a fixed ~1.2V
        then, same as before this existed."""
        r = self._get("/api/stm32/ads1115", timeout=3.0)
        return r.get("ref_mv") if r.get("ok") else None

    # R_sense/gain live ONLY here — pulse_events_ma() below calls this
    # rather than re-deriving the formula, so there is exactly one place
    # to correct if either constant changes (this exact formula was
    # independently duplicated 3+ times across this repo before and
    # drifted out of sync once already — see the JS side's tests.js).

    def pulse_ma(self, raw: float, ref_mv: float | None = None) -> float:
        """Convert one raw STM32 ADC count (a pulse_events() peak/plateau/
        bg field) to emission current in mA.

        The STM32's OWN internal 12-bit ADC (PA0/ADC1_IN1, via the AMC3301
        isolation amp) samples V = raw*3.3/4095; Ie = 2*(V - 0.5*ref_v) /
        R_sense / G_amc A -> mA, with R_sense/G_amc from the class
        constants above. This is NOT the formula an ESP32-ADC-scale
        constant (3.1 V / 6.8 ohm) would give you — those numbers belong
        to a different ADC entirely and overstated current by ~35% when
        they were still in use here.

        `ref_mv` is the external differential circuit's LIVE reference —
        pass a reading from get_ads1115_ref_mv() for an accurate result.
        Omit it and this fetches one live reading itself (one extra HTTP
        round trip per call — fetch it ONCE and reuse it across a batch
        instead, e.g. via pulse_events_ma(), rather than calling this
        directly in a loop)."""
        # A BENCH WITH NO ANALOG FRONT END READS ZERO, AND ZERO CONVERTS TO
        # ABOUT -32 mA. The lab test board's STM32 is a bare board: no DS3502,
        # no ADS1115, no AMC3301. There, `i2c_present` is 0x80 (probe valid,
        # all four absent), get_ads1115_ref_mv() returns None, adc_window reads
        # min=0 max=0 with ZERO variance, and every peak/plateau/bg/post_bg is
        # 0 -- so every current here is pulse_ma(0), and integral_mams is 0.0
        # with background_flat True. None of that is a fault to chase; the
        # parts are absent, not broken and not switched off. Emission-current
        # MAGNITUDES can only be measured on a populated board.
        if ref_mv is None:
            live = self.get_ads1115_ref_mv()
            ref_mv = live if live is not None else 1227.0   # last-known-good fallback
        v = raw * 3.3 / 4095 - 0.5 * (ref_mv / 1000.0)
        return 2 * v / self._PULSE_R_SENSE_OHM / self._PULSE_AMC3301_GAIN * 1000

    # Saturation markers the STM32 reports instead of a value it cannot hold.
    # integral is SIGNED, so both ends are markers, and both are REACHABLE: the
    # integral accumulates over the STM32's internal u32 sample count, which is
    # NOT bounded by duration_samples' u16 -- at full scale it hits INT32 in
    # about 520k samples (~0.5 s at 1 MSPS).
    # duration_samples is truncated to this on the way out, so the envelope was
    # AT LEAST this long. It saturates independently of the integral: a pulse
    # can have a perfectly good charge and an unusable width, so this must not
    # invalidate integral_mams (which never uses the duration).
    #
    # The pre-signed firmware used 0xFFFFFFFF as ITS integral marker, which
    # reads as -1 once parsed signed. That is deliberately NOT treated as
    # saturation: -1 is an ordinary integral now that the clamp is gone (pure
    # noise sums to about zero), and mixing the two firmware generations is
    # ruled out by flashing both sides together.
    def _check_background(self, e: dict) -> None:
        """Cross-check the PRE-pulse background against the POST-pulse one, and
        annotate the event. Uses only fields already on the wire.

        Why this is needed. The charge is
        `integral = Sx - (F-R)*mean_bg`, so an error in the background biases
        the charge by `duration * error` -- LINEARLY with pulse width, and
        silently: a contaminated background produces a perfectly plausible
        number. On a 1000-sample pulse, 1 LSB of background error is 1000
        LSB*samples of integral error.

        The two windows are NOT measured the same way, and that asymmetry is
        the point. The POST window waits `post_bg_gap_samples` (default 50) for
        the signal to settle after the fall. The PRE window has NO equivalent
        guard -- the firmware snapshots "the samples right before this edge",
        abutting the rising edge with zero margin. While the detector processed
        samples in coarse batches, edges were applied late and the window
        effectively sat well before the real edge; now that edges are
        timestamped by DMA position and "applied at the exact sample", the
        window tightly abuts the edge and picks up whatever leads it (switch
        pre-charge, gate coupling, a driver turning on before the envelope).

        On a clean measurement both windows sample the same quiet baseline and
        agree. A material disagreement says the PRE window was contaminated --
        and the pre window is the one the charge depends on.

        Sets `background_pre_post_delta` (counts, pre - post) always, and
        `background_suspect` True/False/None. None means UNVERIFIABLE, not
        clean: it needs `post_bg` (absent when post_bg_n_samples=0 or no sample
        was taken) and a non-zero sigma to have a scale to judge against.
        """
        # How much of the requested window actually backed the mean. None =
        # firmware predates the field (unknown, NOT complete); an integer short
        # of the request means the history ran out. background_n == 0 never
        # reaches here -- _add_charge() refuses that outright.
        bg_n = e.get("background_n")
        # ORDER MATTERS, and the STM32 side flagged it: when background_n is 0,
        # sigma4 is fixed at 0 too -- but that is "there was no background",
        # not "the input is flat". Decide on background_n FIRST, or a missing
        # background gets filed as a dead front end and sends someone to check
        # the analog path. _add_charge() refuses before reaching here; this
        # keeps the check correct when called on its own.
        if bg_n == 0:
            e["background_partial"] = True
            e["background_pre_post_delta"] = None
            e["background_suspect"] = None
            e["background_n_note"] = ("no background at all (background_n = 0) — "
                                      "not a flat input; the sample history "
                                      "could not supply the window")
            e["background_note"] = None
            return
        if bg_n is not None and bg_n < self._BG_WINDOW_US:
            e["background_partial"] = True
            e["background_n_note"] = (
                f"background averaged only {bg_n} of the requested "
                f"{self._BG_WINDOW_US:.0f} samples — history was short (fresh "
                f"reset, rate change, or ADC restart)")
        else:
            e["background_partial"] = False if bg_n is not None else None
        pre, post, sigma4 = e.get("bg"), e.get("post_bg"), e.get("bg_sigma4")
        if pre is None or post is None:
            e["background_pre_post_delta"] = None
            e["background_suspect"] = None
            e["background_note"] = ("no post-pulse background to compare against "
                                    "(post_bg_n_samples=0, or none was taken)")
            return
        delta = float(pre) - float(post)
        e["background_pre_post_delta"] = delta
        if not sigma4:
            # Reachable only with background_n > 0 (the 0 case returned above),
            # so a zero spread here really is a flat input.
            # Same reasoning as background_flat: zero spread is a dead input,
            # not a noise-free one, and it leaves no scale to judge delta by.
            e["background_suspect"] = None
            e["background_note"] = ("background has zero spread — no scale to "
                                    "judge the pre/post delta against")
            return
        sigma = float(sigma4) / 4.0
        e["background_suspect"] = abs(delta) > self._BG_DELTA_SIGMA * sigma
        e["background_note"] = (
            f"pre-pulse background is {delta:+.1f} counts off the post-pulse one "
            f"({abs(delta) / sigma:.1f} sigma). The PRE window has no settle "
            f"guard, so it is the suspect one — and the charge depends on it."
            if e["background_suspect"] else None)

    def _add_charge(self, e: dict, ref_mv: float,
                    integral_signed: bool | None = None,
                    background_windowing: bool | None = None) -> None:
        """Add integral_mams (charge, mA*ms) and its scatter to one event.

        integral is round(Sigma(sample - background)) over the pulse -- signed,
        with no per-sample clamp -- so the conversion's large offset cancels and
        charge is just slope * integral / rate.

        A NEGATIVE integral is a legal result, not an error: with the clamp gone
        pure noise sums to about zero, and a pulse dimmer than the background it
        was measured against lands below it. Do not treat negatives as faults or
        floor them at zero.

        integral_mams_sigma comes with it: sigma*sqrt(N) worth of charge, the
        scatter you would see from background noise alone over this pulse
        length. A charge smaller than its own sigma has not been distinguished
        from noise.
        """
        raw = e.get("integral")
        dur = e.get("on_us")
        # The rate comes from the EVENT, not from the rate we requested and not
        # from a constant. The STM32 reports what its timer actually achieved
        # (170 MHz / an integer divider, so rarely exactly the requested value),
        # and it can change between pulses. null => firmware too old to say.
        # Charge scales 1:1 with it, so an assumed rate is a wrong answer
        # wearing the right units: no rate, no mAs.
        rate = e.get("rate_hz")
        if raw is None:
            return
        # Zero-length envelope: integral is 0 because nothing was integrated,
        # not because the charge was zero. See the empty_envelope comment in
        # pulse_events_ma().
        if e.get("empty_envelope"):
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "empty_envelope"
            return
        # integral changed MEANING without changing shape: it used to be clamped
        # at zero per sample, which biases weak pulses upward (measured: ~57% of
        # a 976 us reading). Same offset, same width, no way to tell from the
        # value -- so the STM32's capability bit decides, relayed by the ESP32.
        # False => refuse rather than convert; None => it never answered, which
        # is UNKNOWN and must not decay into an assumption either way.
        if integral_signed is False:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "integral_clamped"
            return
        if integral_signed is None:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "integral_form_unknown"
            return
        if not rate:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "rate_unknown"
            return
        if raw >= self._INTEGRAL_SAT_HI or raw <= self._INTEGRAL_SAT_LO:
            # The STM32 clamps rather than wrapping, and reports no flag -- so a
            # saturated reading is indistinguishable from a real one by value
            # alone. None, not the clamped number.
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "saturated"
            e["integral_saturated"] = True
            return
        # NO BACKGROUND AT ALL. The STM32 reports background_n = 0 when its
        # sample history could not supply the window (fresh reset, rate change,
        # ADC restart). Then background_mean and sigma4 are both 0 and `integral`
        # degenerates to the RAW in-envelope sum with nothing subtracted -- a
        # large, entirely plausible number that is not a charge. The STM32
        # session asked for this to be flagged; refusing is the flag.
        # Whether the exact-window background (pre-gap honoured, background_n
        # reported) is running at all. fw_build >= 0x00030000, per the STM32
        # side -- gated on the build, not a capability bit, because the caps
        # byte is full and the PULSE_CFG tier design drops an unknown pre-gap
        # SILENTLY. None = the ESP32 never got GET_INFO, which is UNKNOWN and
        # must not decay into either answer.
        e["background_windowing"] = background_windowing
        bg_n = e.get("background_n")
        if bg_n == 0:
            e["integral_mams"] = None
            e["integral_mams_unavailable"] = "no_background"
            return
        e["integral_saturated"] = False
        self._check_background(e)
        slope = self.pulse_ma(1, ref_mv) - self.pulse_ma(0, ref_mv)   # mA per count
        # 9 decimals, not 6: a 1 ms pulse of a few mA is ~2e-4 mA*ms and its
        # sigma ~1e-5, which 6 decimals would flatten to one significant digit
        # and print as -0.0 for any small negative. Charge here spans several
        # orders of magnitude, so keep the resolution and normalise negative
        # zero away -- "-0.0 mA*ms" reads as a sign error rather than a number.
        def _q(v: float) -> float:
            r = round(v, 9)
            return 0.0 if r == 0 else r
        # rate is in Hz, so slope*integral/rate is mA*s; x1000 -> mA*ms, which
        # is the scale these pulses actually live at (a 1 ms pulse of a few mA
        # is ~0.2 mA*ms, vs 0.0002 mA*s -- four leading zeros of nothing).
        e["integral_mams"] = _q(slope * raw * 1000.0 / rate)
        # Random error, not bias: summing N samples of noise with sigma each
        # gives a spread of about sigma*sqrt(N). (The old clamped integral
        # needed a BIAS estimate instead -- it could only ever accumulate
        # upward. Signed accumulation centres on zero, so what is left is
        # scatter, and scatter is what tells you whether a small charge is
        # real.) Compare |integral_mams| against this.
        # sigma needs N, and a truncated duration is a LOWER BOUND on N, not N.
        # Using 65535 there would understate the scatter on exactly the longest
        # pulses -- the ones most likely to have accumulated a big integral. No
        # N, no sigma.
        sigma4 = e.get("bg_sigma4")
        # sigma4 == 0 means the background window had ZERO spread. A live 12-bit
        # front end always has some, so this says the input is stuck or
        # unpowered -- not that the measurement is noise-free. It must not
        # become a threshold: the documented test is |charge| > sigma, and with
        # sigma 0 that passes for ANY charge, turning the one guard against
        # over-reading a weak signal into a rubber stamp. Found on hardware with
        # the analog front end unpowered (every sample 0, sigma4 0).
        if sigma4 == 0:
            e["background_flat"] = True
            e["integral_mams_sigma"] = None
        elif dur is not None and dur >= self._DURATION_SATURATED:
            e["duration_saturated"] = True
            e["integral_mams_sigma"] = None
        elif sigma4 is not None and dur:
            e["duration_saturated"] = False
            e["integral_mams_sigma"] = _q(
                slope * (sigma4 / 4.0) * math.sqrt(dur) * 1000.0 / rate)

    def pulse_events_ma(self, since: int = 0) -> dict:
        """Like pulse_events(), but every event also gets peak_ma/
        plateau_ma/bg_ma/post_bg_ma fields, converted with ONE live ADS1115
        reference reading shared across the whole batch — cheaper and
        more internally consistent than calling pulse_ma() per-event
        (each of which would otherwise fetch its own live reading).
        Returns {"ok", "events": [...], "last_id", "ref_mv": <the reading
        actually used, or None if that read failed and the ~1.2V fallback
        was used instead>}.

        BACKGROUND. peak_ma/plateau_ma are ABSOLUTE — the standing emission
        is still in them (see pulse_events()' "WHICH FIELDS HAVE THE
        BACKGROUND REMOVED"). This adds the subtracted forms so nobody has
        to remember to do it:

            "peak_net_ma"     peak_ma - bg_ma
            "plateau_net_ma"  plateau_ma - bg_ma   <- the pulse's own current

        Both are ABSENT (not 0.0, not None) when there is no background to
        subtract, for the same reason the _ma fields are: a net current with
        no background behind it is not a measurement.
        integral_mams is already net of the BACKGROUND — do not subtract that
        from it again. The diode path is still in it, as it is in
        plateau_net_ma:

            "emission_ma"     plateau_net_ma - diode_ma    <- emission current
            "emission_mams"   integral_mams - diode_ma * on_us / 1000
                                                           <- emission charge
            "diode_ma"        the diode-path current that was removed

        All three are ABSENT when the rail could not be read (no diode_ma) or
        their input is missing — never an uncorrected number under the
        corrected name.

        print() the result: each event prints grouped (emission / net /
        absolute / counts / checks), so absolute and net cannot be confused
        at a glance.

        A field that was not measured stays None and gets NO _ma companion --
        post_bg_ma is simply absent on such an event, rather than carrying a
        converted stand-in. Check `"post_bg_ma" in event`, or guard on
        `event["post_bg"] is not None`."""
        r = self.pulse_events(since)
        if not r.get("ok"):
            return r
        ref_mv = self.get_ads1115_ref_mv()
        # One rail read for the whole batch, like the reference above. None if
        # it could not be read -- the events then carry no emission_ma at all,
        # rather than an uncorrected number wearing the right name.
        diode_ma = self.diode_path_ma().get("ma")
        # Resolve the fallback ONCE here and always pass a real number to
        # pulse_ma() below — passing None would make IT fetch its own live
        # reading per event, defeating the one-shared-reading point of
        # this method entirely.
        resolved_ref_mv = ref_mv if ref_mv is not None else 1200.0
        for e in r.get("events", []):
            # A zero-length envelope is a PA4 glitch, not a pulse: a rise and a
            # fall landing on the same sample still commit a full event. In it,
            # peak_adc is its INITIAL value (0), integral is 0 and plateau is
            # empty -- none of them measurements. Converting 0 counts yields
            # about -32 mA, a confident-looking current that was never measured,
            # and integral would report a legal 0.0 charge. bg and post_bg ARE
            # real (bg is snapshotted at the rise, post_bg measured normally),
            # so they still convert.
            empty = e.get("on_us") == 0
            e["empty_envelope"] = empty
            keys = ("bg", "post_bg") if empty else ("peak", "plateau", "bg", "post_bg")
            # The `is not None` guard is what keeps "not measured" (null) from
            # being converted into a plausible mA value.
            for key in keys:
                if e.get(key) is not None:
                    e[f"{key}_ma"] = round(self.pulse_ma(e[key], resolved_ref_mv), 3)
            # peak_ma/plateau_ma are ABSOLUTE levels -- the standing emission
            # is still in them. The pulse's OWN current is the difference, and
            # leaving every caller to remember that is how plateau_ma gets
            # quoted as "the pulse current" (at 2.8 A heating that overstates
            # it by ~57%). Subtract once, here. Raw counts first: bg and
            # plateau are integers, and taking the difference before the
            # conversion keeps its large offset from cancelling inexactly.
            # The background's noise in mA, next to bg_ma. bg_sigma4 is 4*sigma
            # in ADC counts, which cannot be compared with any current here.
            if e.get("bg_sigma4"):
                slope = (self.pulse_ma(1.0, resolved_ref_mv)
                         - self.pulse_ma(0.0, resolved_ref_mv))
                e["bg_sigma_ma"] = round(e["bg_sigma4"] / 4.0 * slope, 4)
            bg_raw = e.get("bg")
            for key in ("peak", "plateau"):
                if e.get(key) is not None and bg_raw is not None:
                    e[f"{key}_net_ma"] = round(
                        self.pulse_ma(e[key], resolved_ref_mv)
                        - self.pulse_ma(bg_raw, resolved_ref_mv), 3)
            # ...and the emission proper. plateau_net_ma is still not it: the
            # same MOSFET that gates the emission puts the sub-board's two
            # diodes and 100 kOhm across the rail for the duration of the
            # pulse, so every shot carries (|V| - 2.82)/100k on top -- ~1 mA at
            # 100 V, ~2 mA at 200 V, which is the WHOLE signal at the cold end
            # of a curve. Computed from the rail, see diode_path_ma().
            if diode_ma is not None and e.get("plateau_net_ma") is not None:
                e["diode_ma"] = diode_ma
                e["emission_ma"] = round(e["plateau_net_ma"] - diode_ma, 3)
                # else: NO field at all. A net current with no background to
                # subtract is not a measurement, and 0.0 would read as one.
            self._add_charge(e, resolved_ref_mv, r.get("integral_signed"),
                             r.get("background_windowing"))
            # The charge, likewise: integral_mams is net of the BACKGROUND only,
            # so the diode path is still in it -- ~2 mA*ms of a 1 ms pulse at
            # 200 V, the whole reading for a cold filament. The diode current
            # is a constant DC for as long as the switch is closed, so its
            # charge is diode_ma * width, taken over the envelope's own measured
            # width. integral_mams_sigma applies unchanged: the subtraction is
            # a formula, it adds no scatter. Absent, like emission_ma, whenever
            # any of the three inputs is.
            emis_q = self._emission_charge(e.get("integral_mams"), diode_ma,
                                           e.get("on_us"))
            if emis_q is not None:
                e["emission_mams"] = emis_q
        r["ref_mv"] = ref_mv
        return r

    # ---- structured display -------------------------------------------
    # A pulse event carries ~25 fields, half of them absolute levels and
    # half of them background-subtracted, plus six different "this number
    # is not a measurement" flags. Printed as a flat list of numbers the
    # important distinction -- which figures still contain the standing
    # emission -- is invisible, and the flags scroll past unread. These
    # render it grouped instead, so ABSOLUTE and NET cannot be mistaken
    # for each other and an unusable event says so on its own line.

    def measure_pulse_current(
        self,
        filament: int,
        num_pulses: int = 1,
        width_us: int = 1000,
        rate_hz: int = 1000000,
        inter_pulse_ms: int = 3000,
        max_on_ms: int = 40,
        total_ms: int = 15000,
        controller: int | None = None,
        trigger: str = "sim",
        timeout_s: float = 15.0,
        verify: bool = True,
        reuse: bool = False,
        bg_gap_us: float | None = None,
        bg_window_us: float | None = None,
    ) -> dict:
        """Fire and measure, returning the two halves separately.

        Identical work to fire_single_pulse(..., measure=True) -- which is
        now the recommended call, since firing and measuring belong to the
        same operation and keeping them in one function stops anyone firing
        HV they forgot to measure. This wrapper differs only in SHAPE: it
        nests the fire result under "fired" instead of merging it, which is
        handy when you want to log the two halves apart.

            {"ok":       both fired AND every pulse measured,
             "fired":    the full fire_single_pulse result dict,
             "measured": [one event per pulse, peak_ma/plateau_ma/bg_ma],
             "ref_mv":   the live reference reading actually used}

        See fire_single_pulse's "measure=True" section for the arming and
        correlation rules, why a partial measurement reports ok=False, and what
        bg_gap_us/bg_window_us do -- they are forwarded unchanged, so this
        wrapper can do everything the call it wraps can.
        """
        r = self.fire_single_pulse(
            filament, num_pulses=num_pulses, width_us=width_us,
            inter_pulse_ms=inter_pulse_ms, max_on_ms=max_on_ms,
            total_ms=total_ms, controller=controller, trigger=trigger,
            timeout_s=timeout_s, verify=verify, reuse=reuse,
            measure=True, rate_hz=rate_hz,
            bg_gap_us=bg_gap_us, bg_window_us=bg_window_us)
        fired = {k: v for k, v in r.items() if k not in ("measured", "ref_mv")}
        return {"ok": bool(r.get("ok")), "fired": fired,
                "measured": r.get("measured") or [], "ref_mv": r.get("ref_mv")}
