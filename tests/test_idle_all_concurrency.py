#!/usr/bin/env python3
"""Regression test: every filament to IDLE at once must settle, and quickly.

WHY THIS EXISTS. The RP2350's power service (closed-loop current + voltage
ramp) must drive its eight I2C channels CONCURRENTLY -- an architecture
decision, locked in RP2350bFilamentController/architecture.md §4.5 #11. On
2026-09-23 a serial implementation was found: with 83 boards at IDLE the CC
loop spent every tick's time slice and the ramp made ZERO steps -- every
filament frozen at ~700 mA of 1500, the GUI's "idle all" did nothing. Tests
with four filaments passed, which is why it went unnoticed. This one loads
the whole rig, because that is the only load that shows it.

Run it after ANY change to the RP2350 power service, before calling the change
done:

    python3 tests/test_idle_all_concurrency.py            # every live filament
    python3 tests/test_idle_all_concurrency.py --idle-ma 1000 --max-s 10

MEASURED, 93 filaments at IDLE 1000 mA from SLEEP:
    concurrent (RP2350 1e08f92)   93/93 settled, median 1.4 s, max 4.4 s, 6.2 s wall
    serial     (RP2350 856a5fa)   90 settled,    median 28 s,  max 39 s,  41 s wall
    serial     (before 856a5fa)   83 of 83 stuck at ~700 mA of 1500 -- never settled
The default bounds (max 10 s, median 4 s) sit 2-3x above the concurrent
figures and far below the serial ones.

What it does, no HV at any point: SLEEP then STANDBY every live filament
(verified; the power-up order never skips a step), IDLE
them all at --idle-ma in ONE batch with verify, then STOP (verified) -- also on
error or Ctrl-C. Exit 1 on failure.

FAIL when: any commanded filament does not settle within --timeout-s; the
slowest takes longer than --max-s; the median longer than --median-s; or the
batch reports a command that did not land.
"""

import _path  # noqa: F401  — makes ct_simple_control importable from tests/

import argparse
import statistics
import sys
import time

from ct_simple_control import CTClient


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--idle-ma", type=int, default=1000,
                    help="IDLE current for every filament (mA, <= 2000)")
    ap.add_argument("--timeout-s", type=float, default=60.0,
                    help="how long verify waits before a filament counts as not settled")
    ap.add_argument("--max-s", type=float, default=10.0,
                    help="FAIL if the slowest filament takes longer than this")
    ap.add_argument("--median-s", type=float, default=4.0,
                    help="FAIL if the median filament takes longer than this")
    args = ap.parse_args(argv)

    ct = CTClient(args.host, client_id="test-idle-all-concurrency")
    problems: list[str] = []
    with ct.lease(note="idle-all concurrency regression test, no HV"):
        try:
            r = ct.sleep_all(verify=True)
            if not r.get("readback", {}).get("ok"):
                problems.append(f"SLEEP did not read back for {r.get('not_reached')}")
            # The power-up order is STOP -> SLEEP -> STANDBY -> IDLE, never
            # skipping a step (cold-filament inrush / OCP).
            r = ct.standby_all(verify=True)
            if not r.get("readback", {}).get("ok"):
                problems.append(f"STANDBY did not read back for {r.get('not_reached')}")
            t0 = time.monotonic()
            r = ct.idle_all(default_ma=args.idle_ma, verify=True, timeout_s=args.timeout_s)
            wall = time.monotonic() - t0
            h = r.get("heating") or {}
            res = h.get("results") or {}
            settled = sorted(v["elapsed_s"] for v in res.values() if v.get("ok"))
            print(f"IDLE {args.idle_ma} mA: {len(settled)}/{len(res)} settled "
                  f"in {wall:.1f} s wall")
            if settled:
                med = statistics.median(settled)
                print(f"  time to settle: min {settled[0]:.1f} s   median {med:.1f} s   "
                      f"max {settled[-1]:.1f} s")
                if settled[-1] > args.max_s:
                    problems.append(f"slowest filament took {settled[-1]:.1f} s "
                                    f"(limit {args.max_s} s)")
                if med > args.median_s:
                    problems.append(f"median {med:.1f} s (limit {args.median_s} s)")
            if r.get("not_reached"):
                problems.append(f"did not settle: {r['not_reached']}")
                for f in r["not_reached"]:
                    v = res.get(f) or res.get(str(f)) or {}
                    print(f"    {f}: {v.get('measured_ma')} mA {v.get('arrival')} "
                          f"{v.get('error', '')}")
            if r.get("failed"):
                problems.append(f"command did not land for {r['failed']}")
            if not res:
                problems.append("no filament was commanded")
        finally:
            r = ct.stop_all(verify=True)
            if not r.get("readback", {}).get("ok"):
                problems.append(f"STOP did not read back for {r.get('not_reached')} "
                                f"— CHECK THE BENCH")
            print("teardown: STOP", "ok" if r.get("readback", {}).get("ok") else "NOT confirmed")

    print()
    if problems:
        print("FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
