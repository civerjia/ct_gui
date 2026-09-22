#!/usr/bin/env python3
"""Emission current vs heating current, for one filament.

Pre-heats through the ladder, walks ACTIVE from --start-ma up to --max-ma, fires
pulses at each step and measures every one, then leaves the filament in
--end-state (default STOP -- never ACTIVE).

    python3 tests/test_emission_curve.py                       # 2500..2800 mA
    python3 tests/test_emission_curve.py --filaments 8 --step-ma 50
    python3 tests/test_emission_curve.py --no-save

The x-axis is the firmware's per-pulse snapshot of the heating current at the
INSTANT each pulse fired (`heat_mA`), not what ACTIVE was commanded. The CC loop
settles near, not at, its target, and a shot that lands during the ramp sits at
a current no host poll can recover afterwards -- so a curve plotted against the
commanded value quietly mixes "measured" with "asked for".

Results are written to the backend's calibration/ directory (JSON + flat CSV)
unless --no-save.

SAFETY. Filaments are energised, and the whole sweep sits at firing current --
the result reports `active_s` so that time is visible rather than assumed. The
ladder is walked in full, the sweep only ever steps UP, --max-ma is a hard
ceiling, and everything runs inside `ct.energised()`, which STOPs on a crash or
Ctrl-C. The signal handler below turns SIGTERM/SIGHUP into an exception so the
teardown runs under a harness timeout too. HV is disabled in a finally.
"""
import _path  # noqa: F401  — makes ct_simple_control importable from tests/

import argparse
import signal
import sys
import time

from ct_simple_control import CTClient, CTError


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="localhost",
                   help="backend.py's address (NOT the ESP32's IP)")
    p.add_argument("--filaments", default="8",
                   help="comma-separated USER_INDEX values, measured one after "
                        "another; each is returned to --end-state before the "
                        "next starts (default: 8)")
    p.add_argument("--start-ma", type=int, default=2500,
                   help="first firing point (default: 2500)")
    p.add_argument("--max-ma", type=int, default=2800,
                   help="hard ceiling; never exceeded (default: 2800)")
    p.add_argument("--step-ma", type=int, default=100)
    p.add_argument("--width-us", type=int, default=1000)
    p.add_argument("--pulses", type=int, default=3,
                   help="shots averaged at each heating current (default: 3)")
    p.add_argument("--idle-ma", type=int, default=1500,
                   help="the pre-heat rung of the ladder (default: 1500)")
    p.add_argument("--end-state", default="stop",
                   choices=("stop", "sleep", "standby", "idle"),
                   help="where to leave each filament (default: stop). ACTIVE "
                        "is not offered.")
    p.add_argument("--settle-s", type=float, default=1.0,
                   help="extra dwell after the CC loop reports settled")
    p.add_argument("--emission-v", type=float, default=200.0)
    p.add_argument("--emission-i", type=float, default=20.0)
    p.add_argument("--focus-v", type=float, default=350.0)
    p.add_argument("--save-as", default="emission_vs_heating",
                   help="name for the saved JSON/CSV (default: "
                        "emission_vs_heating)")
    p.add_argument("--no-save", action="store_true")
    p.add_argument("--lease-ttl", type=int, default=600)
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    filaments = [int(x) for x in args.filaments.replace(",", " ").split()]
    if not filaments:
        print("nothing to measure: --filaments was empty")
        return 2

    def bail(sig, _frame):
        raise KeyboardInterrupt(f"signal {sig}")
    for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(s, bail)

    ct = CTClient(args.host)
    results: dict[int, dict] = {}
    print(f"backend {args.host} · filaments {filaments} · "
          f"{args.start_ma}..{args.max_ma} mA step {args.step_ma} · "
          f"{args.pulses}x{args.width_us} us · end {args.end_state}")

    try:
        with ct.lease(ttl=args.lease_ttl, note="emission curve"):
            ct.set_emission_v(args.emission_v)
            ct.set_emission_i(args.emission_i)
            ct.set_focus_v(args.focus_v)
            ct.enable_emission(True)
            ct.enable_focus(True)
            time.sleep(1.0)
            print(f"HV: emission {ct.read_emission_v()} V, "
                  f"focus {ct.read_focus_v()} V, "
                  f"DC {ct.read_emission_i()} mA (cold)")
            try:
                for f in filaments:
                    print(f"\n-- filament {f} " + "-" * (58 - len(str(f))))

                    def show(p, _f=f):
                        print(f"   {p['commanded_ma']} mA cmd -> at pulse "
                              f"{p['heat_mA']} mA, net {p['net_ma']} mA "
                              f"({p['n_used']}/{len(p['pulses'])} used)"
                              + (f"   [{p['note']}]" if p.get("note") else ""))

                    r = ct.emission_vs_heating(
                        f, start_ma=args.start_ma, max_ma=args.max_ma,
                        step_ma=args.step_ma, width_us=args.width_us,
                        pulses_per_point=args.pulses, idle_ma=args.idle_ma,
                        end_state=args.end_state, settle_s=args.settle_s,
                        progress=show)
                    results[f] = r
                    print()
                    print(ct.format_emission_curve(r))
            finally:
                ct.enable_emission(False)
                ct.enable_focus(False)
    except KeyboardInterrupt as exc:
        print(f"\ninterrupted ({exc}) — filaments STOPped, HV off")
    except CTError as exc:
        print(f"\nclient error: {exc}")

    # Save whatever was measured, including after an interrupt: a partial sweep
    # is still data, and the alternative is losing the minutes of ACTIVE time
    # that produced it.
    if results and not args.no_save:
        saved = ct.save_emission_curves(args.save_as, results, {
            "start_ma": args.start_ma, "max_ma": args.max_ma,
            "step_ma": args.step_ma, "width_us": args.width_us,
            "pulses_per_point": args.pulses, "idle_ma": args.idle_ma,
            "end_state": args.end_state, "emission_v": args.emission_v,
            "focus_v": args.focus_v})
        print(f"\nsaved: {saved.get('json')}" if saved.get("ok")
              else f"\nSAVE FAILED: {saved.get('error')}")
        if saved.get("ok"):
            print(f"       {saved.get('csv')}")

    bad = [f for f, r in results.items() if not r.get("ok")]
    missing = [f for f in filaments if f not in results]
    print("\n" + "=" * 72)
    print(f"{len(results) - len(bad)}/{len(filaments)} filament(s) measured cleanly")
    for f in bad:
        for p in results[f].get("problems") or []:
            print(f"  filament {f}: {p}")
    for f in missing:
        print(f"  filament {f}: not measured (run ended early)")
    print("=" * 72)
    return 1 if (bad or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
