#!/usr/bin/env python3
"""Fire one filament heated in VOLTAGE mode, and read back what current each
voltage gave -- the data for choosing a filament's voltage.

    python3 examples/fire_voltage_mode.py --host 192.168.8.218 -f 27 --mv 7600
    python3 examples/fire_voltage_mode.py --host 192.168.8.218 -f 27 --mv 7000 7600 8200

fire_single_pulse(active_mv=...) is fire_single_pulse(active_ma=...) with the
heat-up done at a fixed output voltage instead of an ACTIVE current: no CC loop
to converge. Per shot:

    IDLE     prepare, detector arm, arm      (nothing here needs heat)
    VOLTAGE  timed voltage_one(active_mv) -> wait for ARRIVAL (+-150 mV) ->
             one cached check that the output is up -> trigger -> wait
    IDLE     back to idle_ma, then records + detector events

The backend treats the timed VOLTAGE like a timed ACTIVE: only from a settled
IDLE, back to IDLE on its own clock if this script dies, watched by the
dead-man, and refused above the rig's voltage cap (11 V).

Every pulse record carries what the firmware saw AT THE INSTANT IT FIRED:
heat_meas_mA (current), heat_bus_mV (measured load voltage, cached) and
heat_cmd_mV (the TPS setpoint). The voltage needs RP2350 fw 530b925 or later;
on older firmware it is None with heat_bus_unavailable="firmware_no_voltage".
The script ends with a V -> I table from those records.

HV (emission/focus) is NOT turned on here: with it off the shot still heats and
records the current and voltage, which is all the table needs. If emission is
already on the script stops, unless you pass --hv-ok (you set the HV yourself
and want the emission measured too).

On exit, even on error or Ctrl-C: the filament goes back to SLEEP.
"""
import argparse
import time

import _path  # noqa: F401  -- makes ct_simple_control importable from examples/
from ct_simple_control import CTClient

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="192.168.8.218")
ap.add_argument("-f", "--filament", type=int, required=True)
ap.add_argument("--mv", type=int, nargs="+", required=True,
                help="heating voltage(s) in mV, one shot each (800 .. the 11000 mV cap)")
ap.add_argument("--idle-ma", type=float, default=1200)
ap.add_argument("--settle-s", type=float, default=6.0, help="longest wait for the voltage to ARRIVE (+-150 mV) before firing")
ap.add_argument("--idle-settle-s", type=float, default=15.0, help="IDLE settle before the first shot")
ap.add_argument("--width-us", type=int, default=7000)
ap.add_argument("--hv-ok", action="store_true", help="run even though emission HV is on")
args = ap.parse_args()

F = args.filament
ct = CTClient(args.host, client_id="fire_voltage_mode_example")

hv = ct.hv_status()
if not hv.get("ok"):
    raise SystemExit(f"cannot read the HV state: {hv.get('error')}")
if (hv.get("emission_on") or hv.get("focus_on")) and not args.hv_ok:
    raise SystemExit("emission/focus HV is ON -- turn it off first, or pass --hv-ok "
                     "if you set it on purpose")

table = []          # (commanded mV, measured mV, cmd mV, measured mA, emission mA)
with ct.lease(ttl=120, note="fire_voltage_mode example"):
    try:
        # The ladder, one step at a time: VOLTAGE (like ACTIVE) is only
        # accepted from a filament that has settled at IDLE.
        ct.stop_one(F, verify=True)
        ct.sleep_one(F, verify=True)
        ct.standby_one(F, verify=True)
        r = ct.idle_one(F, args.idle_ma, verify=True, timeout_s=30)
        if not r.get("ok"):
            raise SystemExit(f"filament {F} did not reach IDLE {args.idle_ma} mA: {r.get('error')}")
        print(f"IDLE {args.idle_ma:.0f} mA, settling {args.idle_settle_s:.0f} s ...")
        time.sleep(args.idle_settle_s)

        for mv in args.mv:
            r = ct.fire_single_pulse(F, num_pulses=1, width_us=args.width_us,
                                     pulse_interval_ms=100, max_on_ms=165, total_ms=6000,
                                     shot_wait_timeout_s=6.0, measure=True,
                                     active_mv=mv, idle_ma=args.idle_ma,
                                     heat_timeout_s=args.settle_s)
            print(f"\n--- {mv} mV ---")
            print(r)                              # the pulse table is in the Result's printout
            ev = r.get("measured") or []
            for n, rec in enumerate(r.get("records") or []):
                e = ev[n] if n < len(ev) and isinstance(ev[n], dict) else {}
                table.append((mv, rec.get("heat_bus_mV"), rec.get("heat_cmd_mV"),
                              rec.get("heat_meas_mA"), e.get("emission_ma")))
            # back at IDLE now; let it re-settle before the next voltage
            if mv != args.mv[-1]:
                time.sleep(args.idle_settle_s)
    finally:
        ct.sleep_one(F)

print(f"\nfilament {F}: heating at the instant each pulse fired")
print(f"  {'asked mV':>8}  {'meas mV':>8}  {'cmd mV':>8}  {'meas mA':>8}  {'emission mA':>11}")
for mv, bus, cmd, ma, em in table:
    cell = lambda v, fmt="{:.0f}": "-" if v is None else fmt.format(v)  # noqa: E731
    print(f"  {mv:>8}  {cell(bus):>8}  {cell(cmd):>8}  {cell(ma):>8}  {cell(em, '{:.3f}'):>11}")
