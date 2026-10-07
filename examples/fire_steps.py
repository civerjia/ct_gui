#!/usr/bin/env python3
"""Fire one pulse per filament with the steps of fire_single_pulse() called
one by one, so the heating can go exactly where you want it.

    python3 examples/fire_steps.py --host 192.168.8.218 -f 16 55 70
    python3 examples/fire_steps.py --host 192.168.8.218 -f 16 --trigger ext

fire_single_pulse() is these steps, in this order. Each takes the SHOT dict
the previous one returned and returns it again; when a step fails, the shot
comes back with ok=False and "error", and every later step passes it through
untouched -- so one check at the end is enough. ct.shot_abort(shot) disarms
whatever was armed and is safe at any point (put it in a finally).

    shot = ct.shot_prepare(f, ...)        # schedule on the RP2350 (or CRC-confirmed)
    shot = ct.shot_measure_arm(shot)      # optional: STM32 detector + event cursor
    shot = ct.shot_arm(shot)              # arm; checks the slot was not skipped
    shot = ct.shot_trigger(shot)          # "sim": pulse now; "ext": waits for the edge
    shot = ct.shot_wait(shot)             # until complete / fault / timeout
    res  = ct.shot_records(shot)          # pulse log, ON/OFF switch read-backs
    res  = ct.shot_measured(shot, res)    # optional: detector events (then detector off)

This script's order -- ACTIVE only around the pulse:

    IDLE   prepare, measure_arm, arm         (nothing here needs firing current)
    ACTIVE active_one(verify) -> trigger -> wait
    IDLE   idle_one, then records + measured (reading results needs no heat)

Move the two heating lines to change it. Two rules hold whatever the order:
  * ACTIVE only from IDLE -- the filament must be at IDLE first (this script
    puts every filament there and lets it settle);
  * while a run is RUNNING the backend refuses active_one/idle_one, so with
    num_pulses > 1 heat BEFORE shot_trigger and drop it AFTER shot_wait.

The firmware's total_ms clock starts at shot_arm(): any heating you do
between arm and trigger counts against it.

On exit, even on error or Ctrl-C: disarm, detector off, every filament here
back to SLEEP. HV (emission/focus) is NOT touched -- set and enable it
yourself if the pulse should carry current.
"""
import argparse
import time

import _path  # noqa: F401  -- makes ct_simple_control importable from examples/
from ct_simple_control import CTClient

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="192.168.8.218")
ap.add_argument("-f", "--filaments", type=int, nargs="+", required=True)
ap.add_argument("--idle-ma", type=float, default=1300)
ap.add_argument("--active-ma", type=float, default=2700)
ap.add_argument("--width-us", type=int, default=7000)
ap.add_argument("--settle-s", type=float, default=15.0, help="IDLE settle before the first ACTIVE")
ap.add_argument("--trigger", choices=("sim", "ext"), default="sim")
ap.add_argument("--no-measure", action="store_true", help="skip the STM32 detector")
args = ap.parse_args()

ct = CTClient(args.host, client_id="fire_steps_example")


def fire_one(f: int) -> dict:
    t = {}
    shot = {}
    try:
        # --- at IDLE: everything that does not need firing current ----------
        t0 = time.monotonic()
        shot = ct.shot_prepare(f, num_pulses=1, width_us=args.width_us,
                               inter_pulse_ms=100, max_on_ms=165,
                               total_ms=6000 + 6000,   # +6 s: heating happens after arm
                               trigger=args.trigger, reuse=True)
        if not args.no_measure:
            shot = ct.shot_measure_arm(shot, rate_hz=1000000, bg_gap_us=100, bg_window_us=50)
        shot = ct.shot_arm(shot)
        t["setup_s"] = time.monotonic() - t0
        if not shot["ok"]:
            return shot

        # --- ACTIVE: heat, fire, wait ----------------------------------------
        t1 = time.monotonic()
        act = ct.active_one(f, args.active_ma, verify=True, timeout_s=5.0)
        if not act.get("ok"):
            return {**shot, "ok": False, "error": f"ACTIVE refused: {act.get('error')}"}
        if args.trigger == "ext":
            print(f"[{time.strftime('%H:%M:%S')}] filament {f} READY -- send the external trigger")
        shot = ct.shot_trigger(shot)
        shot = ct.shot_wait(shot, timeout_s=6.0)
        ct.idle_one(f, args.idle_ma)               # back to IDLE before any read-back
        t["active_s"] = time.monotonic() - t1

        # --- IDLE: read the results ------------------------------------------
        res = ct.shot_records(shot)
        res = ct.shot_measured(shot, res)
        res["heating"] = act.get("heating")
        res["timing"] = {k: round(v, 2) for k, v in t.items()}
        return res
    finally:
        ct.shot_abort(shot)                        # no-op when everything finished
        if t.get("active_s") is None:              # failed between ACTIVE and IDLE
            ct.idle_one(f, args.idle_ma)


with ct.lease(ttl=120, note="fire_steps example"):
    try:
        ct.stop_all(verify=True)
        ct.sleep_all(verify=True)
        ct.standby_all(verify=True)
        for f in args.filaments:
            ct.idle_one(f, args.idle_ma, verify=True, timeout_s=30)
        print(f"IDLE, settling {args.settle_s:.0f} s ...")
        time.sleep(args.settle_s)

        for f in args.filaments:
            r = fire_one(f)
            if not r.get("ok"):
                print(f"filament {f}: FAILED -- {r.get('error')}")
                continue
            ev = (r.get("measured") or [{}])[0]
            print(f"filament {f}: ok  fired={r['fired']}  "
                  f"setup {r['timing']['setup_s']} s at IDLE, ACTIVE {r['timing']['active_s']} s  "
                  f"heat {(r.get('heating') or {}).get('measured_ma')} mA  "
                  f"plateau {ev.get('plateau_ma')} mA  emission {ev.get('emission_ma')} mA")
    finally:
        ct.sleep_all()
