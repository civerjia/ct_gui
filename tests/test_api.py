#!/usr/bin/env python3
"""End-to-end API test: mapping -> HV -> heating ladder -> fire -> measure.

This walks the whole normal operating path in the order a real run uses it,
and CHECKS each step instead of printing it. Every step prints PASS or FAIL
with the evidence, and the script exits non-zero if anything failed -- so it
can be run from a harness, not just read.

    python3 tests/test_api.py                       # defaults
    python3 tests/test_api.py --filaments 0,1,2     # several
    python3 tests/test_api.py --skip-hv             # ladder only, no HV
    python3 tests/test_api.py --help

WHAT IT CHECKS, and why each one is worth checking:

  order round-trip   set_filament_order() then get_filament_order(). The
                     mapping decides which physical filament every later call
                     reaches; a mapping that silently did not take makes every
                     result below correct-looking and about the wrong board.
  dead round-trip    set_dead() then ct.dead.
  HV readback        set_emission_v/set_focus_v, then read back. A set that
                     was accepted but not applied is the failure this catches
                     -- observed on this bench reading -101 V after 200 V was
                     commanded.
  trigger delay      set + get + `applies` (a stored-but-not-honoured delay
                     still returns ok=True; "applies" is the real answer).
  heating ladder     STOP->SLEEP->STANDBY->IDLE->ACTIVE with verify=True at
                     both current-carrying steps. verify is what turns
                     "the command was accepted" into "the filament got there";
                     ACTIVE is refused outright unless IDLE actually settled.
  fire + measure     one event per pulse, measured width matches the request,
                     the background is real and symmetric, and the net
                     emission current stands above its own noise.

SAFETY. Filaments are energised. The ladder is walked in full (going straight
to ACTIVE damages a filament, and in vacuum that is unrepairable), ACTIVE is
held only as long as the fire needs, and everything runs inside
`ct.energised()` so a crash or Ctrl-C still STOPs. `energised()` does not
survive SIGTERM on its own -- the signal handler below converts SIGTERM/SIGHUP
into an exception so the teardown runs. HV is disabled in a finally.
"""
import _path  # noqa: F401  — makes ct_simple_control importable from tests/

import argparse
import signal
import sys
import time

from ct_simple_control import CTClient, CTError


# ── the bench's USER_INDEX -> FID mapping ────────────────────────────────────
# order[i] = the FID that YOUR index i refers to. Written out rather than
# computed: this is bench wiring, and a mapping you cannot read off the page
# is one nobody will notice has drifted.
LIUXING_ORDER = (
    [8, 9, 10, 11, 24, 25, 26, 27]      # user 0-7
    + [0, 1, 2, 3]                       # user 8-11
    + list(range(12, 24))                # user 12-23
    + [4, 5, 6, 7]                       # user 24-27
    + list(range(28, 96))                # user 28-95, identity
)
assert sorted(LIUXING_ORDER) == list(range(96)), "LIUXING_ORDER is not a permutation"


class Report:
    """PASS/FAIL accumulator. `check` returns the verdict so a caller can stop."""

    def __init__(self):
        self.rows: list[tuple[str, bool, str]] = []

    def check(self, name: str, ok, detail: str = "") -> bool:
        ok = bool(ok)
        self.rows.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"   {detail}" if detail else ""))
        return ok

    def note(self, name: str, detail: str = "") -> None:
        """A measurement, not a verdict — printed, never counted as pass/fail.
        Some numbers here (the DC emission current, say) have no right answer
        to assert against; reporting them as a PASS would invent one."""
        print(f"  [ -- ] {name}" + (f"   {detail}" if detail else ""))

    def failed(self) -> list[str]:
        return [n for n, ok, _ in self.rows if not ok]

    def summary(self) -> int:
        bad = self.failed()
        print("\n" + "=" * 72)
        print(f"{len(self.rows) - len(bad)}/{len(self.rows)} checks passed")
        for n in bad:
            print(f"  FAILED: {n}")
        print("=" * 72)
        return 1 if bad else 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="localhost",
                   help="backend.py's address (NOT the ESP32's IP). Default: "
                        "localhost, i.e. backend on this machine.")
    p.add_argument("--filaments", default="8",
                   help="comma-separated USER_INDEX values to test (default: 8 "
                        "-- under the liuxing order that is FID 0, i.e. CH1.1. "
                        "USER_INDEX, not FID: --filaments 0 addresses FID 8 on "
                        "CH2, which is a different board entirely)")
    p.add_argument("--scan-presence", action="store_true",
                   help="run the full presence scan first and refuse to heat a "
                        "filament with no board. SLOW (seconds per controller, "
                        "and it leaves touched boards at SLEEP), so it is opt-in "
                        "-- without it an absent board shows up only as an IDLE "
                        "that never leaves 0 mA (its `present` flag still reads "
                        "True, so it is no help here).")
    p.add_argument("--dead", default="6,26,73",
                   help="comma-separated USER_INDEX values to mark dead, or "
                        "'' for none (default: 6,26,73)")
    p.add_argument("--order", choices=("liuxing", "identity"), default="liuxing",
                   help="filament mapping to install (default: liuxing)")
    p.add_argument("--idle-ma", type=int, default=1200)
    p.add_argument("--active-ma", type=int, default=2800)
    p.add_argument("--idle-timeout", type=float, default=40.0,
                   help="seconds to let IDLE settle from cold (default: 40; it "
                        "keeps climbing for ~15 s, and ACTIVE is refused until "
                        "the CC loop reports 'settled')")
    p.add_argument("--active-timeout", type=float, default=30.0)
    p.add_argument("--emission-v", type=float, default=200.0)
    p.add_argument("--emission-i", type=float, default=20.0,
                   help="emission current REFERENCE (a limit), not a current to "
                        "expect measured -- see the note in the HV section")
    p.add_argument("--focus-v", type=float, default=350.0)
    p.add_argument("--hv-tolerance-v", type=float, default=15.0)
    p.add_argument("--trigger-delay-us", type=int, default=3000)
    p.add_argument("--pulses", type=int, default=1)
    p.add_argument("--width-us", type=int, default=5000)
    p.add_argument("--inter-pulse-ms", type=int, default=1000)
    p.add_argument("--bg-gap-us", type=float, default=200.0,
                   help="settle gap on BOTH sides of the envelope (default 200)")
    p.add_argument("--bg-window-us", type=float, default=50.0,
                   help="background window on BOTH sides (default 50)")
    p.add_argument("--width-tolerance-pct", type=float, default=5.0)
    p.add_argument("--skip-hv", action="store_true",
                   help="do not set or enable HV -- exercises mapping + ladder "
                        "+ the detector only. Firing with HV off measures the "
                        "background and nothing else, which is a valid check "
                        "of the detector path.")
    p.add_argument("--lease-ttl", type=int, default=180)
    return p.parse_args(argv)


def as_indices(s: str) -> list[int]:
    return [int(x) for x in s.replace(",", " ").split()] if s.strip() else []


# ── the sections ─────────────────────────────────────────────────────────────

def check_mapping(ct, rep, args) -> None:
    print("\n-- mapping and dead list " + "-" * 47)
    want = LIUXING_ORDER if args.order == "liuxing" else list(range(96))
    ct.set_filament_order(want if args.order == "liuxing" else None)
    got = ct.get_filament_order()
    if not rep.check("filament order round-trips", got == want,
                     "" if got == want else
                     f"first mismatch at index "
                     f"{next(i for i in range(96) if got[i] != want[i])}"):
        # Every later result is about whichever board the WRONG mapping points
        # at, and would look perfectly normal. Nothing below is meaningful.
        raise SystemExit(rep.summary())
    rep.note("order", f"{args.order}: user 0 -> FID {got[0]}, user 8 -> FID {got[8]}")

    dead = as_indices(args.dead)
    ct.set_dead(dead)
    rep.check("dead list round-trips", set(ct.dead) == set(dead),
              f"set {sorted(dead)} -> read {sorted(ct.dead)}")

    wanted = as_indices(args.filaments)
    for f in wanted:
        if f in dead:
            rep.check(f"filament {f} is not in the dead list", False,
                      "refusing to energise a filament marked dead")
            raise SystemExit(rep.summary())

    if args.scan_presence:
        present = set(ct.present_filaments())
        # present_filaments() answers in USER_INDEX, like every other read --
        # comparing it against `wanted` (also USER_INDEX) needs no translation.
        rep.check("every requested filament has a board",
                  all(f in present for f in wanted),
                  f"missing {[f for f in wanted if f not in present]}"
                  if not all(f in present for f in wanted)
                  else f"{len(present)} boards present")
        if not all(f in present for f in wanted):
            raise SystemExit(rep.summary())


def check_hv(ct, rep, args) -> None:
    print("\n-- HV " + "-" * 66)
    if args.skip_hv:
        rep.note("HV", "skipped (--skip-hv): not set, not enabled")
        return

    ct.set_emission_v(args.emission_v)
    # set_emission_i sets a REFERENCE (a limit). read_emission_i() is a live
    # MEASUREMENT of what is actually flowing. Asserting the second equals the
    # first is the mistake this comment exists to prevent -- they are different
    # quantities, and on a cold filament the measurement is near zero by right.
    si = ct.set_emission_i(args.emission_i)
    rep.check("emission current reference accepted", si.get("ok"),
              f"wiper {si.get('wiper')}, expect {si.get('expect_ma')} mA")
    ct.set_focus_v(args.focus_v)
    ct.enable_emission(True)
    ct.enable_focus(True)
    time.sleep(1.0)

    ev, fv = ct.read_emission_v(), ct.read_focus_v()
    tol = args.hv_tolerance_v
    # Both rails are negative; compare magnitudes so the sign convention
    # cannot turn a real miss into a pass.
    rep.check("emission voltage reached the setpoint",
              ev is not None and abs(abs(ev) - args.emission_v) <= tol,
              f"set -{args.emission_v:.0f} V, read {ev} V (tolerance {tol:.0f} V)")
    rep.check("focus voltage reached the setpoint",
              fv is not None and abs(abs(fv) - args.focus_v) <= tol,
              f"set -{args.focus_v:.0f} V, read {fv} V (tolerance {tol:.0f} V)")

    hv = ct.hv_status()
    rep.check("emission rail reads ON", hv.get("ok") and hv.get("emission_on"),
              f"emission_on={hv.get('emission_on')} focus_on={hv.get('focus_on')}")
    rep.note("DC emission current now",
             f"{ct.read_emission_i()} mA (cold filament: this is background, "
             f"not the {args.emission_i:.0f} mA reference)")


def check_trigger_delay(ct, rep, args) -> None:
    print("\n-- trigger delay " + "-" * 55)
    ct.set_trigger_delay(args.trigger_delay_us)
    g = ct.get_trigger_delay()
    rep.check("trigger delay round-trips",
              g.get("ok") and g.get("delayUs") == args.trigger_delay_us,
              f"set {args.trigger_delay_us} us -> read {g.get('delayUs')} us")
    # A delay can be STORED and still not be honoured by the live fire path;
    # ok=True only says the write landed. "applies" is the answer that matters.
    rep.check("trigger delay applies to the fire path", g.get("applies"),
              f"applies={g.get('applies')}")


def heat(ct, rep, args, f: int) -> bool:
    """Walk the ladder to ACTIVE. True only if the filament really got there."""
    ct.sleep_all([f])
    ct.standby_all([f])

    r = ct.idle_one(f, args.idle_ma, verify=True, timeout_s=args.idle_timeout)
    h = r.get("heating") or {}
    if not rep.check(f"filament {f} reached IDLE {args.idle_ma} mA", h.get("ok"),
                     f"{h.get('measured_ma')} mA, arrival={h.get('arrival')}, "
                     f"present={h.get('present')}, {h.get('elapsed_s', 0):.1f} s"):
        # A filament with no board fails HERE, and `present` does NOT say so:
        # measured on this bench, an absent USER_INDEX 0 reported
        # present=True, 0.0 mA, arrival=ramping. The tell is the current --
        # commanded, never climbing. Use --scan-presence to have the absence
        # named up front rather than inferred from a stuck ramp.
        return False

    r = ct.active_one(f, args.active_ma, verify=True, timeout_s=args.active_timeout)
    if r.get("ladder_blocked"):
        # Not a bug in this script: the guard refuses ACTIVE unless the CC loop
        # says IDLE actually settled. Report its own words.
        return rep.check(f"filament {f} promoted to ACTIVE", False,
                         ct.describe(r)[:120])
    h = r.get("heating") or {}
    return rep.check(f"filament {f} reached ACTIVE {args.active_ma} mA", h.get("ok"),
                     f"{h.get('measured_ma')} mA, arrival={h.get('arrival')}, "
                     f"present={h.get('present')}, {h.get('elapsed_s', 0):.1f} s")


def check_pulse(ct, rep, args, f: int) -> None:
    r = ct.fire_single_pulse(
        filament=f,
        num_pulses=args.pulses,
        width_us=args.width_us,
        inter_pulse_ms=args.inter_pulse_ms,
        max_on_ms=40,
        total_ms=10000,
        controller=None,          # inferred from `filament` via the mapping
        trigger="sim",
        timeout_s=15.0,
        verify=True,
        reuse=False,
        measure=True,
        bg_gap_us=args.bg_gap_us,
        bg_window_us=args.bg_window_us,
    )
    # ok is already strict: it is True only if the fire succeeded AND every
    # fired pulse produced a measured event.
    if not rep.check(f"filament {f} fired and measured {args.pulses} pulse(s)",
                     r.get("ok"), r.get("error") or f"fired {r.get('fired')}"):
        return
    events = r.get("measured") or []
    if not rep.check(f"filament {f}: one event per pulse",
                     len(events) == args.pulses,
                     f"{len(events)} event(s) for {args.pulses} pulse(s)"):
        return

    print()
    print(ct.format_pulse_events(events))
    print()

    for e in events:
        tag = f"filament {f} event #{e.get('id')}"
        if e.get("empty_envelope"):
            rep.check(f"{tag}: real envelope", False,
                      "rise and fall on the same sample — a PA4 glitch")
            continue

        on_us = e.get("on_us")
        tol = args.width_us * args.width_tolerance_pct / 100.0
        rep.check(f"{tag}: measured width matches the request",
                  on_us is not None and abs(on_us - args.width_us) <= tol,
                  f"asked {args.width_us} us, measured {on_us} us "
                  f"(tolerance {tol:.0f} us)")

        # A background of 0 samples is not a quiet background -- `integral`
        # then has NOTHING subtracted and is not a charge at all.
        bg_n = e.get("background_n")
        want_n = int(round(args.bg_window_us * (e.get("rate_hz") or 1_000_000) / 1e6))
        rep.check(f"{tag}: background window is the one requested",
                  bg_n == want_n,
                  f"{bg_n} samples averaged, asked for {want_n} "
                  f"({args.bg_window_us:.0f} us)")
        rep.check(f"{tag}: pre/post backgrounds agree",
                  e.get("background_suspect") is False,
                  e.get("background_note")
                  or f"delta {e.get('background_pre_post_delta')} counts")

        # plateau/peak are ABSOLUTE -- background included. The pulse's own
        # current is the net form, and it has to stand above the background's
        # own noise to mean anything.
        net = e.get("plateau_net_ma")
        sigma4 = e.get("bg_sigma4")
        sigma_ma = (sigma4 / 4.0) * (ct.pulse_ma(1.0, r.get("ref_mv") or 1200.0)
                                     - ct.pulse_ma(0.0, r.get("ref_mv") or 1200.0)) \
            if sigma4 else None
        if args.skip_hv:
            # With HV off there is no emission to find; the check is that the
            # detector measured a background at all, which the two above did.
            rep.note(f"{tag}: net current", f"{net} mA (HV off — expected ~0)")
        elif sigma_ma is None:
            rep.check(f"{tag}: background has a measurable spread", False,
                      "bg_sigma4 = 0 — the input is stuck or unpowered")
        else:
            rep.check(f"{tag}: net emission current stands above the noise",
                      net is not None and net > 3 * sigma_ma,
                      f"net {net} mA vs 3 sigma {3 * sigma_ma:.3f} mA")

        charge = e.get("integral_mams")
        csig = e.get("integral_mams_sigma")
        if charge is None:
            rep.check(f"{tag}: charge available", False,
                      f"not available: {e.get('integral_mams_unavailable')}")
        elif args.skip_hv:
            rep.note(f"{tag}: charge", f"{charge} mA*ms (HV off)")
        else:
            rep.check(f"{tag}: charge is distinguishable from noise",
                      csig is not None and abs(charge) > csig,
                      f"{charge} mA*ms vs sigma {csig}")
            # integral and plateau*duration cover the SAME span (plateau_margin
            # is 0), so they must agree. They are not independent measurements
            # -- disagreement means the two paths used different backgrounds.
            if on_us and net is not None:
                mean_ma = charge / (on_us / 1000.0)
                rep.check(f"{tag}: charge agrees with plateau - bg",
                          abs(mean_ma - net) <= 0.05 * max(abs(net), 1e-9),
                          f"charge/width {mean_ma:.3f} mA vs net {net:.3f} mA")


def main(argv=None) -> int:
    args = parse_args(argv)
    filaments = as_indices(args.filaments)
    if not filaments:
        print("nothing to test: --filaments was empty")
        return 2

    # energised() unwinds on an exception, not on a bare signal -- without this
    # a SIGTERM (a harness timeout, say) kills the process with a filament at
    # full current and HV live.
    def bail(sig, _frame):
        raise KeyboardInterrupt(f"signal {sig}")
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, bail)

    rep = Report()
    ct = CTClient(args.host)
    print(f"backend {args.host} · filaments (USER_INDEX) {filaments} · "
          f"{args.pulses}x{args.width_us} us"
          + (" · HV SKIPPED" if args.skip_hv else ""))

    try:
        with ct.lease(ttl=args.lease_ttl, note="test_api"):
            ct.stop_all()
            check_mapping(ct, rep, args)
            try:
                check_hv(ct, rep, args)
                check_trigger_delay(ct, rep, args)
                for f in filaments:
                    print(f"\n-- filament {f} " + "-" * (58 - len(str(f))))
                    with ct.energised(f):
                        if heat(ct, rep, args, f):
                            check_pulse(ct, rep, args, f)
                        # Back down to IDLE either way: energised() STOPs on the
                        # way out, and dropping ACTIVE first is the gentler path.
                        ct.idle_one(f, args.idle_ma)
            finally:
                if not args.skip_hv:
                    ct.enable_emission(False)
                    ct.enable_focus(False)
                ct.stop_all(filaments)
    except KeyboardInterrupt as exc:
        print(f"\ninterrupted ({exc}) — filaments STOPped, HV off")
        rep.check("run completed", False, str(exc))
    except CTError as exc:
        print(f"\nclient error: {exc}")
        rep.check("run completed", False, str(exc))
    return rep.summary()


if __name__ == "__main__":
    sys.exit(main())
