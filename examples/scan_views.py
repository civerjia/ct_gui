#!/usr/bin/env python3
"""Scan CT views: heat each filament in VOLTAGE mode just before its turn,
fire it, put it back to IDLE.

    python3 examples/scan_views.py --host 192.168.8.218                 # first 10 views
    python3 examples/scan_views.py --host 192.168.8.218 --views 96      # a whole rotation

Per view: the motor moves (here: --move-s of waiting -- put your motor in
move_to below); meanwhile the view's first filaments are heated, so each one
is at its voltage when its turn comes. A filament fires only once its voltage
has settled, then drops straight back to IDLE.

Before: every filament at IDLE (the rig keeps them there for the vacuum).
HV is not touched. Ctrl-C stops it safely; everything ends at IDLE.
"""
import argparse

import _path  # noqa: F401  -- makes ct_simple_control importable from examples/
from ct_simple_control import CTClient

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="localhost", help="the backend's IP")
ap.add_argument("--views", type=int, default=10, help="how many views (96 = a rotation)")
ap.add_argument("--mv", type=int, default=5000,
                help="heating voltage, mV, written for every filament below -- put each one's own there")
ap.add_argument("--move-s", type=float, default=3.0, help="motor move time per view, s")
args = ap.parse_args()

ct = CTClient(args.host)


def move_to(view):
    """Start the motor towards this view and return at once (do not wait for
    it). The view's first pulse comes --move-s later."""
    print(f"view {view['view']}: detector to filament {view['center']}")


plan = ct.scan_plan()        # every view of a rotation, dead filaments left out
print(f"{plan['n_views']} views; dead, left out: {plan['dead_left_out']}")

views = plan["views"][:args.views]
# EVERY filament its own voltage, mV. Here all the same; replace with yours.
mv = {f: args.mv for v in views for f in v["filaments"]}

with ct.lease(ttl=120, note="scan_views example"):
    r = ct.scan_views(views,
                      active_mv=mv,              # {filament: mV} for every filament
                      idle_ma=1200,              # back to this after its pulses, mA
                      move_to=move_to,           # your motor, before each view (None = none)
                      move_s=args.move_s,        # wait after the move, s
                      k=2,                       # pre-heat the next 2 filaments
                      on_error="stop",           # "stop" | "skip" a failed filament
                      num_pulses=5,              # --- fire_single_pulse's, per filament ---
                      width_us=20000,
                      pulse_interval_ms=100,
                      trigger="sim",             # "ext": add on_armed=your_function(filament)
                      measure=True,
                      heat_timeout_s=6.0,
                      settle_ms=500)
print(r)

# What each filament did (arrival time, V/I at its pulses, failures), kept by
# the backend across runs:
#     print(ct.filament_perf([79, 80, 81]))
