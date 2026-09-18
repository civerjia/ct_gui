#!/usr/bin/env python3
"""End-to-end tour of the ct_simple_control API, in runnable sections.

    python3 walkthrough.py --list              # what the sections are
    python3 walkthrough.py                     # READ-ONLY sections only (safe)
    python3 walkthrough.py --only 8            # just the schedule builder
    python3 walkthrough.py --energise -f 27    # + the heating/measurement ones
    python3 walkthrough.py --energise --fire -f 27   # + firing pulses

Nothing energises a filament unless you pass --energise, and nothing fires a
pulse unless you pass --fire. Sections are labelled with what they do, and a
section is skipped (loudly) rather than silently downgraded when its permission
is missing.

`-f/--filament` is the filament to exercise. There is no default on purpose:
which board carries a load is a property of your bench, not of this script, and
picking one for you is how a script ends up heating something unexpected. Find
one with section 5.

WHY THE SIGNAL HANDLER AT THE BOTTOM: `energised()` guarantees a STOP on
exception and on Ctrl-C, but a plain SIGTERM terminates the process without
running any `finally` -- observed leaving a filament at 880 mA when a harness
timed a script out. Copy that handler into any bench script that might be
killed. SIGKILL cannot be caught by anything; only a backend-side watchdog
could cover that, and there isn't one yet.
"""
import argparse
import signal
import sys
import time

import _path  # noqa: F401  — makes ct_simple_control importable from examples/
from ct_simple_control import CTClient, CTError

SECTIONS = []


def section(number, title, needs=""):
    """needs: "" read-only | "energise" | "fire" """
    def deco(fn):
        SECTIONS.append((number, title, needs, fn))
        return fn
    return deco


def head(text):
    print(f"\n{'─' * 72}\n{text}\n{'─' * 72}")


# ─────────────────────────────────────────────────────────────────────────────
@section(1, "Connect, status, and the error convention")
def s1(ct, args):
    # Almost nothing in this client raises. Methods return {"ok": False,
    # "error": ...} so a bench script never dies halfway through a run with a
    # filament still powered. acquire_lease() is the one exception.
    st = ct.status()
    for cid, c in sorted(st["controllers"].items()):
        mark = "connected" if c["connected"] else "NOT connected"
        print(f"  controller {cid}: {mark}  host={c['host']}  "
              f"rp2350_rtt={(c['rp2350'] or {}).get('rtt_ms')}")
    print(f"  master (STM32 routing): {st['master']}")
    print(f"  write lease: {st['lock']}")

    bogus = ct.idle_one(999, 1500)          # out of range on purpose
    print(f"\n  a bad call returns rather than raises: ok={bogus.get('ok')}")
    print(f"    error: {str(bogus.get('error'))[:70]}")
    print(f"  describe() renders any result dict: {ct.describe(bogus)[:70]}")


# ─────────────────────────────────────────────────────────────────────────────
@section(2, "Three index spaces — the thing to get right")
def s2(ct, args):
    # SITE  (controller, channel, position)  where the board physically is
    # FID   0-95   what the firmware and backend call a filament
    # USER_INDEX   0-95   what YOU call it; every method here takes this
    # A filament_order swap maps USER_INDEX -> FID. Everything you pass in and
    # everything you get back is USER_INDEX; the crossing happens on the wire.
    print(f"  filament_order active (swap set)? {ct.get_filament_order() != ct.identity_order()}")
    for f in (args.filament, 0):
        if f is None:
            continue
        site = ct.filament_to_board(f)
        if site:
            print(f"  USER_INDEX {f:>2} -> controller {site['controller']} "
                  f"CH{site['channel'] + 1}.{site['position'] + 1} (slot {site['slot']})")
            back = ct.board_to_filament(site["controller"], site["channel"], site["position"])
            print(f"     and back again: {back}   round-trip {'ok' if back == f else 'MISMATCH'}")


# ─────────────────────────────────────────────────────────────────────────────
@section(3, "Dead mask — filaments that must never be energised")
def s3(ct, args):
    # "dead" means the FILAMENT is unusable, not that the board is absent. The
    # mask lives in the backend and is persisted to disk, so it survives both
    # your script and a backend restart.
    print(f"  dead: {sorted(ct.dead)}")
    for fid, why in sorted((ct.dead_details().get("dead") or {}).items()):
        print(f"    {fid}: {why.get('reason') or '(no reason recorded)'}")
    print("\n  every batch call filters these out silently but reports it:")
    print(f"    {ct.describe(ct.stop_all(sorted(ct.dead) or None))[:70]}")
    print("  to edit:  ct.add_dead(12, reason='open filament')  /  ct.remove_dead(12)")


# ─────────────────────────────────────────────────────────────────────────────
@section(4, "Configuration: slew rates, OCP, fault policy, trigger delay")
def s4(ct, args):
    # ── Slew rates ──────────────────────────────────────────────────────────
    # How fast the CC loop is allowed to ramp the output voltage. Three bands:
    # below/above are the COLD-start rates, warm is once the filament is hot.
    bands = ("below_mV_per_s", "above_mV_per_s", "warm_mV_per_s")
    slew = ct.get_slew_rates(1)
    print("  slew rates (mV/s):")
    print(f"    now       {{{', '.join(f'{k}: {slew.get(k)}' for k in bands)}}}")
    print(f"    defaults  {{{', '.join(f'{k}: {CTClient.SLEW_DEFAULTS[k]}' for k in bands)}}}")
    print(f"    ceilings  {{{', '.join(f'{k}: {CTClient.SLEW_CEILINGS[k]}' for k in bands)}}}")
    # THE TRAP: the ceilings are NOT the defaults. Cold slew sits far below its
    # ceiling deliberately -- fast cold ramping is exactly what trips OCP on the
    # cold-inrush. "Reset to defaults" by writing the ceilings once made cold
    # starts 5x/10x faster and tripped OCP. To restore, write SLEW_DEFAULTS:
    #     ct.set_slew_rates(**CTClient.SLEW_DEFAULTS, controller=1)

    # ── OCP ─────────────────────────────────────────────────────────────────
    # Two unrelated mechanisms. (1) per-board TPS55289 IOUT_LIMIT, the real
    # steady-state trip. (2) a GLOBAL two-stage floor per controller: a STARTUP
    # threshold tolerating cold inrush, then a STEADY one ~2 s later.
    print(f"\n  OCP startup/steady (global, per controller): {ct.get_ocp_startup(1)}")
    if args.filament is not None:
        ocp = ct.get_ocp_threshold_one(args.filament)
        print(f"  OCP trip for F{args.filament} (real register read): {ocp}")
        if not ocp.get("ok"):
            # Expected when the board is at STOP: the rail is down, so the
            # TPS55289 cannot be read. Bring it up (section 6) to see a value.
            print("    (board is powered down — a register read needs its rail up)")

    # ── Fault policy ────────────────────────────────────────────────────────
    # board=0 stop the whole run on a board fault (default), board=1 continue.
    print(f"\n  fault policy: {ct.get_fault_policy(1)}")

    # ── Trigger delay ───────────────────────────────────────────────────────
    # Delay from the SyncIn trigger edge to the HV pulse, in microseconds.
    print(f"  trigger delay: {ct.get_trigger_delay(1)}")


# ─────────────────────────────────────────────────────────────────────────────
@section(5, "Telemetry: cached vs live, board faults, thermal history")
def s5(ct, args):
    # CACHED (0x3A): current only, no I2C, carries the CC loop's own `arrival`
    # verdict. THE ONE SAFE TO POLL, including while a schedule fires.
    cached = ct.read_filament_current_cached()
    live_ones = {f: e for f, e in cached.items() if e.get("present")}
    print(f"  cached read: {len(live_ones)} present board(s), no I2C, safe mid-run")
    if not live_ones:
        # Not a fault: STOP drops each board's rail, so the presence probe stops
        # answering for it. Presence here means "powered and answering", not
        # "physically plugged in".
        print("    (all boards are at STOP — the rail is down, so nothing reports"
              " present. Run section 6 to bring one up.)")

    # LIVE (INA219): voltage AND current, but a real I2C mux sweep. The backend
    # refuses it while a schedule fires. Do NOT poll it to watch a ramp --
    # measured, it slows the ramp it is watching by ~20%.
    vi = ct.read_filament_vi_live()
    loaded = [(f, e) for f, e in sorted(vi.items())
              if e.get("present") and (e.get("current_mA") or 0) > 50]
    print(f"  live read  : boards actually drawing >50 mA: "
          f"{[f for f, _ in loaded] or 'none (all idle/empty)'}")
    for f, e in loaded[:4]:
        print(f"      F{f}: {e['bus_mV']:.0f} mV  {e['current_mA']:.0f} mA")

    # Fault flags, with their validity twins honoured: unreadable is None, NOT
    # "no fault". The GUI reads the raw bit and silently passes unreadable boards.
    # Only boards that are PRESENT: an absent board reports every flag as
    # unreadable, and listing 60 of those buries the one that matters.
    faults = {f: v for f, v in ct.read_board_faults().items()
              if v["present"] and (v["tps_fault"] or v["tps_fault"] is None)}
    print(f"  board faults on present boards "
          f"(True=faulted, None=UNREADABLE): {faults or 'all clear'}")

    # How long each filament has been off. A filament missing here is UNKNOWN,
    # not cold -- a backend restart clears this.
    hist = ct.thermal_history()
    print(f"  thermal history: {len(hist)} filament(s) with a known state")
    for f, h in sorted(hist.items())[:3]:
        print(f"      F{f}: state={h['state']} energising={h['energising']} "
              f"cold_for_s={h['cold_for_s']}")


# ─────────────────────────────────────────────────────────────────────────────
@section(6, "Heating ladder + BATCH verify", needs="energise")
def s6(ct, args):
    targets = {args.filament: 1500}
    # The ladder is STOP -> SLEEP -> STANDBY -> IDLE -> (settle) -> ACTIVE.
    # Going straight to ACTIVE is refused by the backend: it damages the
    # filament, and in vacuum that is unrepairable.
    with ct.energised(*targets):
        print("  ladder to IDLE…")
        ct.sleep_all(list(targets))
        ct.standby_all(list(targets))
        ct.idle_all(list(targets), currents=targets)

        # wait_for_currents, NOT wait_for_current per filament. The cached read
        # is a BULK command -- it returns every board whether you ask for one or
        # ninety-six -- so verifying N filaments one at a time pays for the same
        # data N times. Measured on 14 filaments: 6.5 s / 40 calls batched vs
        # 85.9 s / 409 sequential, identical verdicts.
        t0 = time.monotonic()
        r = ct.wait_for_currents(targets, timeout_s=10.0)
        print(f"  wait_for_currents: ok={r['ok']} in {time.monotonic() - t0:.1f}s, "
              f"{r['polls']} polls for {len(targets)} filament(s)")
        for f, row in sorted(r["results"].items()):
            # `arrival` is the CC LOOP'S OWN verdict and is authoritative.
            # A host-side sample landing inside tolerance does NOT override a
            # "ramping": a cold-start inrush passes down through the target
            # within 0.4 s and would read as a false success.
            print(f"    F{f}: ok={row['ok']} arrival={row['arrival']} "
                  f"I={row['measured_ma']:.0f} mA  {(row.get('error') or '')[:48]}")
        # A ~0 mA target is refused by the batch version -- a stop drops the
        # rail, so the reading ceases to exist. Use stop_one(verify=True).
    print(f"  after the with-block: {ct.read_board_status(args.filament).get('state_name')}")


# ─────────────────────────────────────────────────────────────────────────────
@section(7, "Test flows: resistance screen and impedance sweep", needs="energise")
def s7(ct, args):
    f = args.filament
    # TEST 1 -- quick short/open screen at the 0.8 V STANDBY floor.
    # This R is NOT a cold resistance: 0.8 V into a real filament is ~0.7 W, so
    # it is heating throughout the settle. Fine for a screen, where the
    # thresholds are orders of magnitude from the drift.
    r1 = ct.measure_filament_resistance([f], cool_s=8, progress=lambda m: print("    ·", m))
    print(f"\n  thermal precondition: met={r1['thermal']['met']} ({r1['thermal']['note']})")
    for k, row in sorted(r1["results"].items()):
        R = row["R_ohm"]
        print(f"    F{k}: {row['verdict']:>6}  "
              f"R={'—' if R is None else f'{R:.3f} Ω'}  I={row['current_mA']} mA")

    if not args.slow:
        print("\n  (skipping the impedance sweep — pass --slow; it takes minutes)")
        return

    # TEST 6 -- fit the whole V-I curve to R(I) = a·I² + R₀.
    # R₀ equals the ROOM-TEMPERATURE resistance only if the sweep starts at
    # ambient AND holds equilibrium at every step. Neither shows up in the fit;
    # both just move R₀. Measured on one load: 0.192 Ω vs 0.481 Ω, 2.5x, across
    # runs that all reported verdict "ok".
    r6 = ct.sweep_filament_impedance([f], start_mv=800, end_mv=3000, step_mv=200,
                                     dwell_s=6.0, cool_s=60, save=True,
                                     progress=lambda m: print("    ·", m))
    row = r6["results"][f]
    h = row["hysteresis"]
    print(f"\n    verdict={row['verdict']}  R0={row['R0_ohm']}  a={row['a']}")
    print(f"    collinearity={row['collinearity']}  rms_resid={row['rms_residual_frac']}")
    if h:
        # THE RETURN POINT is the only output that TESTS the cold premise rather
        # than assuming it: back to the opening voltage, re-measure, compare.
        # Raise dwell_s until this drift falls inside tolerance, then trust R₀.
        print(f"    return point: {h['r_open_ohm']:.4f} -> {h['r_return_ohm']:.4f} Ω "
              f"({h['drift_frac'] * 100:+.1f}%)")
    print(f"    r0_is_cold={row['r0_is_cold']}  {row.get('cold_note') or ''}")
    print(f"    saved: {(r6.get('saved') or {}).get('csv')}")

    # The fit on its own, for curves you already have:
    demo = [{"i": i, "v": (0.30 + 0.02 * i * i) * i} for i in (0.5, 1.0, 1.5, 2.0, 2.5, 3.0)]
    print(f"\n  fit_cold_resistance on a synthetic curve: {ct.fit_cold_resistance(demo)}")
    # save_calibration() writes any record to the backend's calibration/ dir:
    #   ct.save_calibration("my_measurement", {"curves": {...}, "params": {...}})


# ─────────────────────────────────────────────────────────────────────────────
@section(8, "Schedule: geometry -> emission -> heating -> validate -> gantt")
def s8(ct, args):
    # Pure computation -- no hardware is touched by any of this, which is why
    # this section is read-only. Nothing is downloaded until you call download().
    sch = ct.build_scan_schedule(mode="stationary", collimator_center=0)
    print(f"  build_scan_schedule: {len(sch['emission'])} rows, "
          f"{sch['triggers']} triggers, truncated={sch['truncated']}, "
          f"excluded(dead)={sch.get('excluded')}")
    # `truncated` matters: precision mode generates roughly
    # N*(2*steps+1)*coverage rows, so hitting the firmware's 8192 cap is the
    # NORMAL case there. A scan that quietly ends at ring step 21 of 96 reads
    # exactly like one that ran fine.

    plan = ct.build_scan_plan(sch["emission"], active_count=35, rotation_ms=360000)
    print(f"  build_scan_plan    : {len(plan['heating'])} heating deltas, "
          f"{len(plan['currents'])} filaments with currents")
    print(f"  config             : {plan['config']}")

    # Read the lead/peak numbers from validate_plan, not from the plan dict --
    # the plan's own _lead_triggers/_peak_active are underscore-prefixed because
    # they are internal bookkeeping, and reaching into them is how a caller ends
    # up depending on a shape that is free to change.
    v = ct.validate_plan(plan, rotation_ms=360000, t_settle_ms=2000)
    print(f"  validate_plan      : ok={v['ok']}  pre-heat lead={v['lead_ms']:.0f} ms "
          f"({v['lead_triggers']} triggers), needs {v['t_settle_ms']} ms; "
          f"peak concurrency={v['peak_active']}")

    # Small slice, so the gantt is readable. is_active_at / heating_windows
    # answer "was F7 hot when trigger 42 fired?" without re-deriving the plan.
    small = ct.build_scan_plan(sch["emission"][:40], active_count=3, rotation_ms=60000)
    print("\n" + ct.gantt(small, width=60))
    win = ct.heating_windows(small)
    some = sorted(win)[:3]
    print(f"  heating_windows (first few): {{{', '.join(f'{f}: {win[f]}' for f in some)}}}")
    if some:
        print(f"  is_active_at(F{some[0]}, trigger 5) = "
              f"{ct.is_active_at(small, some[0], 5)}")
    print("\n  to run it:  ct.download(plan); ct.verify_schedule(plan); ct.shv_arm(1)")
    print("  afterwards: ct.scan_report(1, since=cursor, plan=plan)")


# ─────────────────────────────────────────────────────────────────────────────
@section(9, "Fire a pulse and measure it", needs="fire")
def s9(ct, args):
    f = args.filament
    # Arming requires the scheduled filament to be in a POWERED state -- arm
    # rejects an absent/stopped one (IsoOff). HV stays off here: firing and DC
    # routing are alternatives, never steps.
    print(f"  HV grid (should be all off): "
          f"{list((ct.hv_grid_status(1).get('filaments') or {}).items())[:2]}")
    with ct.energised(f):
        ct.sleep_all([f])
        ct.standby_all([f])
        cursor = ct.pulse_cursor()      # take it BEFORE firing, so a stale
                                         # backlog can never be counted as yours
        r = ct.fire_single_pulse(f, num_pulses=3, width_us=1000,
                                 inter_pulse_ms=800, trigger="sim",
                                 measure=args.measure, timeout_s=25.0)
        print(f"  fired={r.get('fired')} ok={r.get('ok')} "
              f"{(r.get('error') or '')[:60]}")
        if r.get("hv_stuck_on"):
            print(f"  !! HV DID NOT TURN OFF on {r['hv_stuck_on']}")
        for rec in (r.get("records") or [])[:3]:
            print(f"    pulse: {rec}")
        if args.measure:
            # Charge comes from the event's OWN sample_rate_hz -- a hardcoded
            # rate would silently halve the charge at 500 kHz.
            ev = ct.pulse_events_ma(cursor)
            for e in (ev.get("events") or [])[:3]:
                # Print the CAVEATS next to the number, never the number alone.
                # A charge of 0.0 with no context reads as a real measurement of
                # zero; `background_flat` is what distinguishes that from "there
                # is no live input at all" (this lab board has no AMC3301
                # fitted, so every code is 0 and sigma is 0 with it).
                # `integral_mams_sigma` is None for the same reason -- and it
                # matters, because the documented test is |charge| > sigma,
                # which a sigma of 0.0 would turn into a rubber stamp.
                print(f"    measured: {e.get('on_us')} us  "
                      f"charge={e.get('integral_mams')} mA·ms  "
                      f"sigma={e.get('integral_mams_sigma')}  "
                      f"background_flat={e.get('background_flat')}  "
                      f"unavailable={e.get('integral_mams_unavailable')}")
                if e.get("background_flat"):
                    print("       ^ background_flat: no live input on this "
                          "channel — the 0.0 is an absence, not a measurement")
            print(f"    ADS1115 reference: {ct.get_ads1115_ref_mv()} mV")
            print(f"    raw 2048 -> {ct.pulse_ma(2048):.1f} mA")


# ─────────────────────────────────────────────────────────────────────────────
@section(10, "Recovery — clearing state a killed script left behind")
def s10(ct, args):
    # The pulse relay is a single global resource with no owner recorded. Its
    # resting state is ARMED (persistent mode), and relayed pulses renew its
    # abandonment TTL, so an active run is never reclaimed out from under itself.
    st = ct.ready_status()
    print(f"  relay: armed={st.get('armed')} persistent={st.get('persistent')} "
          f"rate={st.get('rate_hz')} pulses={st.get('pulses_relayed')}")
    # NON-ZERO means an arm was reclaimed by timeout at some point, i.e. some
    # caller's cleanup did not run. Surfaced rather than silently fixed.
    print(f"  ttl_expiries={st.get('ttl_expiries')} "
          f"(non-zero = somebody's cleanup is not running)")
    print("  ct.ready_renew()  extends it for a legitimately long run")
    print("  ct.recover()      clears a half-finished operation;")
    print("                    recover(stop_heating=True) also de-energises")
    print(f"\n  recover() dry look: {ct.recover()}")


# ─────────────────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="localhost", help="backend.py's host")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("-f", "--filament", type=int, default=None,
                    help="filament to exercise (USER_INDEX). Required by the "
                         "--energise/--fire sections; find one with section 5.")
    ap.add_argument("--only", help="comma-separated section numbers")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--energise", action="store_true",
                    help="allow sections that POWER a filament")
    ap.add_argument("--fire", action="store_true",
                    help="allow sections that FIRE pulses (implies --energise)")
    ap.add_argument("--measure", action="store_true",
                    help="section 9: also measure the fired pulses")
    ap.add_argument("--slow", action="store_true",
                    help="section 7: include the impedance sweep (minutes)")
    args = ap.parse_args()
    if args.fire:
        args.energise = True

    if args.list:
        for n, title, needs, _ in SECTIONS:
            tag = {"": "read-only", "energise": "ENERGISES", "fire": "FIRES"}[needs]
            print(f"  {n:>2}. [{tag:>9}] {title}")
        return 0

    # See the module docstring: energised() cannot survive a SIGTERM without
    # this, and a harness timeout is a SIGTERM.
    def _bail(signum, _frame):
        raise KeyboardInterrupt(f"signal {signum}")
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _bail)

    ct = CTClient(host=args.host, port=args.port, client_id="walkthrough")
    if not any(c["connected"] for c in ct.status()["controllers"].values()):
        print("No controller is connected. Connect one first:\n"
              "    ct.connect(1, '192.168.50.53')", file=sys.stderr)
        return 1

    wanted = {int(x) for x in args.only.split(",")} if args.only else None
    ran = skipped = 0
    for n, title, needs, fn in SECTIONS:
        if wanted is not None and n not in wanted:
            continue
        head(f"{n}. {title}")
        if needs == "energise" and not args.energise:
            print("  SKIPPED — powers a filament; pass --energise"); skipped += 1; continue
        if needs == "fire" and not args.fire:
            print("  SKIPPED — fires pulses; pass --fire"); skipped += 1; continue
        if needs and args.filament is None:
            print("  SKIPPED — needs -f/--filament"); skipped += 1; continue
        try:
            fn(ct, args)
            ran += 1
        except CTError as exc:                 # the lease is the only raiser
            print(f"  FAILED: {type(exc).__name__}: {exc}")
        except Exception as exc:               # keep going; report honestly
            print(f"  FAILED: {type(exc).__name__}: {exc}")
    head(f"{ran} section(s) ran, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
