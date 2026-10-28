#!/usr/bin/env python3
"""Fire a list of filaments in order, each as soon as it is hot: ct.scan_view().

    python3 examples/scan_view.py --host 192.168.8.118:8780                 # trigger="sim"
    python3 examples/scan_view.py --host 192.168.8.118:8780 --trigger ext   # your edges

WHAT YOU EDIT: FILAMENTS, ACTIVE_MV, IDLE_MA, NUM_PULSES and on_armed() below.
Everything else is the call itself, with every argument written out.

HOW ONE RUN GOES (3 filaments, preheat_ahead=1; time runs to the right):

    filament 0   VOLTAGE ..arrived..[settle_ms] HOT on_armed(0) |||||                 IDLE
    filament 1   VOLTAGE ..arrived..[settle_ms] HOT ..waits..   on_armed(1) |||||     IDLE
    filament 95                         VOLTAGE ..arrived..[settle_ms] HOT on_armed(95) ||||| IDLE
                                        ^ raised when filament 1's turn starts (1 ahead)
    ||||| = its num_pulses pulses.  A filament goes to IDLE as soon as the next one
    is released (at once, if the next one is not hot yet); the last one at the end.

  - Order is the list order, always. A filament that is hot early waits.
  - HOT = settle_ms has passed since its voltage FIRST arrived (within 150 mV
    of ACTIVE_MV), and its heating was not interrupted since (output fault,
    board lost, or more than max_voltage_drop_mv below target). Wobble is fine.
  - on_armed(filament) is called BY THE SYSTEM when it is that filament's turn
    and it is hot. Your command goes in it (start the detector, send the
    edges). It fires for every filament of the list, one call each, in order.
  - trigger="sim": after on_armed returns, the library sends num_pulses edges
    pulse_interval_ms apart. trigger="ext": YOUR hardware sends num_pulses
    edges, starting from on_armed; edges at any other time are dropped and
    counted (they can never fire another filament).
  - preheat_ahead="auto" raises each filament early enough to be hot by its
    turn, from how fast it heated in earlier runs (3 s + settle_ms when it has
    no history yet), never more than max_hot at once.

WHEN SOMETHING GOES WRONG
  - Dangerous (stops the whole run at once, everything to IDLE, r["stopped"]
    says why): SHV fault, a MOS stuck on, HV guard tripped, vacuum over limit,
    HV switched off, a controller lost > link_lost_timeout_s, on_armed still
    running after on_armed_timeout_s, no pulse at all for no_pulse_timeout_s,
    Ctrl-C.
  - Data lost (that filament's row says what, the run goes on): never arrived
    within heat_timeout_s, heating interrupted, on_armed raised, its edges not
    all in within shot_wait_timeout_s.

BEFORE: every filament of the list at IDLE (the rig keeps them there), and
emission + focus HV on -- either already, or pass --hv-on and this script turns
it on and back off at the end. AFTER: every filament at IDLE (a faulted one at
SLEEP).
"""
import argparse
import signal
import time

import _path  # noqa: F401  -- makes ct_simple_control importable from examples/
from ct_simple_control import CTClient

# ---- WHAT YOU EDIT ---------------------------------------------------------
FILAMENTS = [0, 1, 95]                    # fired in THIS order (user numbering)
ACTIVE_MV = {0: 7000, 1: 7050, 95: 6950}  # EVERY filament its own heating voltage, mV
IDLE_MA = 1200                            # each one back to this after its pulses, mA
NUM_PULSES = 20                           # pulses per filament
# ----------------------------------------------------------------------------

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="localhost", help="the backend, host or host:port")
ap.add_argument("--trigger", choices=("sim", "ext"), default="sim")
ap.add_argument("--hv-on", action="store_true",
                help="turn emission + focus on first (with the values below) and off at the end")
ap.add_argument("--emission-v", type=float, default=200)
ap.add_argument("--emission-ma", type=float, default=80, help="emission current limit")
ap.add_argument("--focus-v", type=float, default=350)
args = ap.parse_args()

# SIGTERM would end the process without the teardown; make it an exception.
def _bail(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")
for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
    signal.signal(sig, _bail)

ct = CTClient(args.host, client_id="scan_view_example")
t0 = time.monotonic()


def on_armed(filament):
    """The SYSTEM calls this once per filament, in list order, when that
    filament is hot and its turn has come. Put your command here.

    Must return within on_armed_timeout_s (below), or the run stops. With
    trigger="ext" its num_pulses edges must all arrive within
    shot_wait_timeout_s of this call -- send them from here, or start the
    thing that sends them and return."""
    print(f"  {time.monotonic() - t0:7.2f} s  on_armed({filament})", end="", flush=True)
    if args.trigger == "ext":
        print(f"  -> send {NUM_PULSES} edges now", flush=True)
        # your_trigger_box.send(NUM_PULSES)      # <- your command
    else:
        print("  -> the library sends the edges when this returns", flush=True)
        # detector.start_exposure()      # <- your command, if any


hv = ct.hv_status()
hv_was_on = bool(hv.get("emission_on") and hv.get("focus_on"))
if not hv_was_on and not args.hv_on:
    raise SystemExit("emission/focus HV is off: turn it on first, or run with --hv-on")

with ct.lease(ttl=120, note="scan_view example"):
    try:
        if not hv_was_on:
            for label, r in (("emission V", ct.set_emission_v(args.emission_v)),
                             ("emission limit", ct.set_emission_i(args.emission_ma)),
                             ("focus V", ct.set_focus_v(args.focus_v)),
                             ("emission on", ct.enable_emission(True)),
                             ("focus on", ct.enable_focus(True))):
                if not r.get("ok"):
                    raise SystemExit(f"{label} failed: {r}")

        print(f"scan_view {FILAMENTS}, trigger={args.trigger}")
        r = ct.scan_view(
            FILAMENTS,                  # fired in this order
            ACTIVE_MV,                  # {filament: mV}, every filament its own
            IDLE_MA,                    # back to this after its pulses, mA
            1000,                       # settle_ms: hot = this long after its FIRST arrival
            # -- heating ahead --
            preheat_ahead="auto",       # how many ahead heat: "auto" (learnt per filament) or a number
            preheat_margin_s=0.3,       # "auto": start each one this much earlier still, s
            max_hot=6,                  # at most this many filaments heating at once
            max_voltage_drop_mv=500,    # more than this below ACTIVE_MV (3 reads) = no longer hot
            # -- firing --
            on_armed=on_armed,          # the function itself, no (); required with "ext"
            trigger=args.trigger,       # "sim": the library sends the edges; "ext": your hardware
            num_pulses=NUM_PULSES,      # pulses per filament (= edges per filament with "ext")
            width_us=15000,             # pulse width, us
            pulse_interval_ms=(30 if args.trigger == "sim" else None),
                                        # "sim" only: spacing of its edges, ms (must be > width)
            trigger_timeout_ms=(100 if args.trigger == "sim" else 30),
                                        # firmware: longest wait for the NEXT edge of the
                                        # SAME filament, ms (must be > pulse_interval_ms)
            max_on_ms=66,               # firmware cap on one pulse, ms
            total_ms=None,              # firmware cap on the whole list, ms (None = computed)
            # -- timeouts --
            heat_timeout_s=6.0,         # first arrival within this after it is raised, s
            on_armed_timeout_s=None,    # on_armed must return within this, s (None = shot_wait)
            shot_wait_timeout_s=9.0,    # its pulses all done within this after on_armed, s
            no_pulse_timeout_s=None,    # stop if no pulse at all for this long, s
                                        # (None = heat_timeout_s + settle_ms + shot_wait_timeout_s)
            link_lost_timeout_s=5.0,    # stop if a controller is lost this long, s
            # -- checking and measuring --
            verify=True,                # read the schedule back before arming
            measure=True,               # STM32 measures every pulse: mA and mAms
            rate_hz=1000000,            # STM32 sample rate, Hz
            bg_gap_us=100.0,            # post-pulse background: wait this long after the pulse, us
            bg_window_us=50.0,          # ...then average over this long, us (None = 50/50)
        )
    finally:
        if not hv_was_on:
            for label, fn in (("emission off", lambda: ct.enable_emission(False)),
                              ("focus off", lambda: ct.enable_focus(False))):
                res = fn()
                print(f"{label}: {'ok' if res.get('ok') else 'FAILED -- CHECK HV: ' + str(res)}")

# ---- what came back ----------------------------------------------------------
if r.get("error"):                       # refused before anything was touched
    raise SystemExit(f"refused: {r['error']}")
print(f"\nok={r['ok']}  fired {r['fired']}/{len(FILAMENTS)}  in {r['elapsed_s']} s"
      f"  stopped={r.get('stopped')}  skipped={r['skipped']}  partial={r['partial']}"
      f"  dropped_edges={r['dropped_edges']}  suppressed_not_hot={r['suppressed_not_hot']}")
print(f"{'filament':>8} {'board':>10} {'mV':>5} {'fired':>6} {'heat_s':>7} {'waited_s':>8}"
      f" {'gap_s':>6} {'mAms':>8}  problem")
show = lambda x, k: "-" if x.get(k) is None else str(x[k])   # noqa: E731  (missing = "-")
for x in r["rows"]:
    problem = x.get("skipped") or x.get("on_armed_error") or ""
    if x.get("hv_stuck_on"):
        problem += " HV STUCK ON"
    print(f"{x['filament']:>8} {x['board']:>10} {x['mv']:>5} {x['fired']:>3}/{x['wanted']:<2}"
          f" {show(x, 'heat_s'):>7} {show(x, 'waited_s'):>8} {show(x, 'gap_s'):>6}"
          f" {show(x, 'mams'):>8}  {problem}")
# heat_s   = raised -> hot (arrival + settle_ms)
# waited_s = hot, but not its turn yet
# gap_s    = previous filament's last pulse -> this one's on_armed
