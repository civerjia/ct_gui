#!/usr/bin/env python3
"""Fire one filament on an EXTERNAL trigger, and know when to send it.

    python3 examples/external_trigger.py --host 192.168.8.165 -f 8
    python3 examples/external_trigger.py --host 192.168.8.165 -f 8 --pulses 3 --width-us 1000

What it does, in order:
  1. heats filament F up the ladder STOP -> SLEEP -> STANDBY -> IDLE -> ACTIVE
     (never skipping a step), each step checked;
  2. sets and enables the emission and focus HV;
  3. calls fire_single_pulse(trigger="ext", on_armed=...). The on_armed
     function below runs at the moment everything is armed -- THAT is when to
     send your external trigger. An edge sent earlier is lost;
  4. prints the result (per-pulse records, measured emission);
  5. always, even on error or Ctrl-C: HV off, filament F back to STOP.

There is no default filament on purpose: which board carries a load is a
property of your bench. Only filament F is touched; nothing else is stopped.
"""
import argparse
import signal
import time

import _path  # noqa: F401  -- makes ct_simple_control importable from examples/
from ct_simple_control import CTClient

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="localhost", help="backend.py's IP (its start-up banner)")
ap.add_argument("-f", "--filament", type=int, required=True)
ap.add_argument("--idle-ma", type=float, default=1200)
ap.add_argument("--active-ma", type=float, default=2000)
ap.add_argument("--emission-v", type=float, default=200)
ap.add_argument("--emission-ma", type=float, default=80, help="emission current limit")
ap.add_argument("--focus-v", type=float, default=350)
ap.add_argument("--pulses", type=int, default=1)
ap.add_argument("--width-us", type=int, default=1000)
ap.add_argument("--total-ms", type=int, default=30000,
                help="firmware bound, counted from ARMING: your edge(s) and the whole run")
args = ap.parse_args()

# SIGTERM would end the process without running `finally` (HV off, STOP).
# Turn it into an exception so the teardown below always runs.
def _bail(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")
for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
    signal.signal(sig, _bail)

ct = CTClient(args.host, client_id="external_trigger_example")
F = args.filament
armed_at = {}


def on_armed():
    """Called ONCE, when the system is ready for the trigger. Return quickly:
    print, set a threading.Event, notify another program -- don't block here."""
    armed_at["t"] = time.time()
    print(f"\n[{time.strftime('%H:%M:%S')}] READY -- send the external trigger now "
          f"({args.pulses} pulse(s), within {args.total_ms / 1000:.0f} s)\n", flush=True)
    # If YOUR code produces the edge, set an Event here instead and let the
    # thread that drives the trigger wait on it:
    #     ready = threading.Event();  ...  on_armed=ready.set
    #     (other thread)  if ready.wait(60): send_my_trigger()


def step(label, r):
    ok = r.get("ok") if isinstance(r, dict) else bool(r)
    print(f"{label:28s} {'ok' if ok else 'FAILED'}")
    if not ok:
        raise SystemExit(f"{label} failed:\n{r}")


with ct.lease(ttl=120, note=f"external trigger example, filament {F}"):
    try:
        step("STOP", ct.stop_one(F, verify=True))
        step("SLEEP", ct.sleep_one(F, verify=True))
        step("STANDBY", ct.standby_one(F, verify=True))
        step(f"IDLE {args.idle_ma:g} mA", ct.idle_one(F, current_ma=args.idle_ma, verify=True))
        step(f"ACTIVE {args.active_ma:g} mA", ct.active_one(F, current_ma=args.active_ma, verify=True))

        step(f"emission {args.emission_v:g} V", ct.set_emission_v(args.emission_v))
        step(f"emission limit {args.emission_ma:g} mA", ct.set_emission_i(args.emission_ma))
        step(f"focus {args.focus_v:g} V", ct.set_focus_v(args.focus_v))
        step("emission on", ct.enable_emission(True))
        step("focus on", ct.enable_focus(True))

        print("arming ... (on_armed will say when to trigger)", flush=True)
        r = ct.fire_single_pulse(
            filament=F,
            num_pulses=args.pulses,
            width_us=args.width_us,
            total_ms=args.total_ms,
            trigger="ext",
            timeout_s=args.total_ms / 1000 + 5,   # longer than total_ms
            measure=True,
            on_armed=on_armed,                    # the function itself, no ()
        )
        if armed_at:
            print(f"run ended {time.time() - armed_at['t']:.1f} s after READY")
        else:
            print("never armed -- the error below says why")
        print(r)
    finally:
        print("\nteardown:", flush=True)
        for label, fn in (("emission off", lambda: ct.enable_emission(False)),
                          ("focus off", lambda: ct.enable_focus(False)),
                          (f"STOP filament {F}", lambda: ct.stop_one(F, verify=True))):
            res = fn()
            print(f"  {label:20s} {'ok' if res.get('ok') else 'FAILED -- CHECK THE BENCH: ' + str(res.get('error'))}")
