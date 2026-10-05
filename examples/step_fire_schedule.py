#!/usr/bin/env python3
"""Download a SHORT schedule, then fire it one filament per trigger, with the
firmware heating each filament to ACTIVE just before its turn.

    python3 examples/step_fire_schedule.py --host 192.168.8.218 -f 93 12 40
    python3 examples/step_fire_schedule.py --host 192.168.8.218 -f 93 12 40 --trigger sim --gap-s 10
    python3 examples/step_fire_schedule.py --host 192.168.8.218 -f 93 12 40 \\
        --emission-v 200 --emission-ma 20 --focus-v 350          # also set + enable HV

What it does, in order:
  1. checks the filaments: none dead, none repeated;
  2. builds ONE schedule: an emission table (one entry per filament, in the
     order given) and a heating table (below);
  3. all filaments to IDLE (verified), then the FIRST one to ACTIVE (verified);
  4. downloads, reads the table back (count + CRC), arms the STM32 detector,
     arms both controllers;
  5. trigger="ext": prints READY -- each external edge fires the NEXT entry.
     trigger="sim": the backend sends the edges itself, --gap-s apart;
  6. waits for the run to end, then prints one line per pulse: the heating
     current AT THE MOMENT IT FIRED (firmware pulse log) and the measured
     emission current (STM32 detector);
  7. always, even on error or Ctrl-C: disarm, detector off, HV off (only if
     this script turned it on), every filament here back to STOP.

THE HEATING TABLE -- why it looks like this
  The firmware applies a heating row when its `triggerIndex` <= the number of
  pulses fired so far, and ONLY while the run is RUNNING (after the first
  edge). So:
    * filament k (k >= 1) goes ACTIVE at triggerIndex = pulses before it, i.e.
      right after the PREVIOUS filament's pulse: the gap between two edges is
      its heating time;
    * every filament goes back to IDLE right after its own pulse(s);
    * the FIRST filament cannot be heated by the table (nothing runs before
      the first edge), so this script brings it to ACTIVE before arming;
    * the LAST filament's IDLE row is never reached (the run is complete after
      its pulse, and heating only runs while running), so it is idled here.
  While the run is RUNNING the backend refuses idle_one/active_one ("running
  -- disarm first"): during a run, the heating table is the ONLY way to change
  a filament's current.

How long a gap is enough is NOT known yet: IDLE -> 2600 mA through the table
has never been timed. Read heat_meas_mA in the printout -- if it is still
below the target at the pulse, the gap was too short.

Nothing outside the filaments named with -f is touched.
"""
import argparse
import math
import signal
import time

import _path  # noqa: F401  -- makes ct_simple_control importable from examples/
from ct_simple_control import ACTIVE, IDLE, CTClient, PowerState

ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
ap.add_argument("--host", default="localhost", help="backend.py's IP")
ap.add_argument("-f", "--filaments", type=int, nargs="+", required=True,
                help="filaments in firing order (your numbering)")
ap.add_argument("--idle-ma", type=float, default=1350)
ap.add_argument("--active-ma", type=float, default=2600)
ap.add_argument("--pulses", type=int, default=1, help="pulses per filament")
ap.add_argument("--width-us", type=int, default=5000)
ap.add_argument("--trigger", choices=("ext", "sim"), default="ext")
ap.add_argument("--gap-s", type=float, default=10.0,
                help="seconds between edges: the sim interval, and what the "
                     "firmware timeouts are sized for with ext")
ap.add_argument("--emission-v", type=float, help="set + enable emission HV (V)")
ap.add_argument("--emission-ma", type=float, help="emission current limit (mA)")
ap.add_argument("--focus-v", type=float, help="set + enable focus HV (V)")
args = ap.parse_args()

# SIGTERM would end the process without running `finally` (HV off, STOP).
def _bail(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")
for sig in (signal.SIGTERM, getattr(signal, "SIGHUP", signal.SIGTERM)):
    signal.signal(sig, _bail)

fils = list(args.filaments)
N, P = len(fils), int(args.pulses)
ct = CTClient(args.host, client_id="step_fire_schedule_example")


def step(label, r):
    ok = r.get("ok") if isinstance(r, dict) else bool(r)
    print(f"{label:34s} {'ok' if ok else 'FAILED'}", flush=True)
    if not ok:
        raise SystemExit(f"{label} failed:\n{r}")
    return r


# ── 1. sanity ────────────────────────────────────────────────────────────────
if len(set(fils)) != N:
    raise SystemExit("a filament is listed twice -- this example heats each one once")
dead = sorted(set(fils) & set(ct.to_user(ct.dead)))
if dead:
    # download() would DROP them, shifting every later entry against the
    # heating table built below. Refuse instead.
    raise SystemExit(f"marked dead, cannot be fired: {dead}")


# ── 2. the schedule ──────────────────────────────────────────────────────────
def step_heating(order, pulses, active_ma, idle_ma):
    """ACTIVE right after the previous filament's pulses, IDLE right after its
    own. The first filament's ACTIVE is not in the table (pre-heated before
    arm), the last one's IDLE is in it but never reached (see the docstring)."""
    rows, fired = [], 0
    for k, f in enumerate(order):
        if k > 0:
            rows.append({"filament": f, "triggerIndex": fired,
                         "state": ACTIVE, "milliamps": int(active_ma)})
        fired += pulses
        rows.append({"filament": f, "triggerIndex": fired,
                     "state": IDLE, "milliamps": int(idle_ma)})
    # Stable sort: at the same index the previous filament's IDLE stays ahead
    # of the next one's ACTIVE, so two filaments are never ACTIVE together.
    return sorted(rows, key=lambda r: r["triggerIndex"])


gap_ms = int(args.gap_s * 1000)
total_ms = gap_ms * N * P + 60_000                       # arm -> last pulse, with slack
plan = {
    "config": {
        "interPulseMs": max(30_000, 3 * gap_ms),         # watchdog between two edges
        "maxOnMs": max(40, math.ceil(args.width_us / 1000) + 1),   # arm rejects wider
        "totalMs": total_ms,
        "triggerEdge": 0,
    },
    "emission": [{"filament": f, "numPulses": P, "widthUs": int(args.width_us)} for f in fils],
    "heating": step_heating(fils, P, args.active_ma, args.idle_ma),
}
print("emission order:", fils)
for h in plan["heating"]:
    print(f"  after pulse {h['triggerIndex']:3d}: filament {h['filament']:3d} -> "
          f"{PowerState(h['state']).name:6s} {h['milliamps']} mA")

hv_turned_on = False
armed = []
with ct.lease(ttl=120, note=f"step-fire schedule {fils}"), ct.energised(*fils):
    try:
        # ── 3. heat ──────────────────────────────────────────────────────────
        step(f"IDLE {args.idle_ma:g} mA x{N}",
             ct.idle_all(fils, default_ma=args.idle_ma, verify=True))
        step(f"ACTIVE {args.active_ma:g} mA filament {fils[0]}",
             ct.active_one(fils[0], current_ma=args.active_ma, verify=True))

        # HV: only touched when asked, and never re-set while it is live.
        if args.emission_v is not None or args.focus_v is not None:
            hv = ct.hv_status()
            if not hv.get("ok", True) or hv.get("emission_on") or hv.get("focus_on"):
                raise SystemExit(f"HV is already on (or unreadable): {hv} -- refusing to "
                                 f"write setpoints into live HV. Turn it off first.")
            if args.emission_v is not None:
                step(f"emission {args.emission_v:g} V", ct.set_emission_v(args.emission_v))
                if args.emission_ma is not None:
                    step(f"emission limit {args.emission_ma:g} mA",
                         ct.set_emission_i(args.emission_ma))
            if args.focus_v is not None:
                step(f"focus {args.focus_v:g} V", ct.set_focus_v(args.focus_v))
            hv_turned_on = True
            if args.emission_v is not None:
                step("emission on", ct.enable_emission(True))
            if args.focus_v is not None:
                step("focus on", ct.enable_focus(True))

        # ── 4. download, check, arm ──────────────────────────────────────────
        d = step("download", ct.download(plan))
        if d.get("dead_skipped"):
            raise SystemExit(f"download dropped dead filaments {d['dead_skipped']} -- "
                             f"the heating table no longer lines up")
        step("verify (count + CRC)", ct.verify_schedule(plan))
        cursor = ct.pulse_cursor()
        step("detector armed", ct.ready_arm(rate_hz=1_000_000, ttl_ms=total_ms + 30_000))
        a = step("schedule armed", ct.arm_all())
        armed = list(a.get("order") or [1, 2])

        # ── 5. trigger ───────────────────────────────────────────────────────
        if args.trigger == "sim":
            step(f"sim: {N * P} edges, {args.gap_s:g} s apart",
                 ct.simulate_sync(count=N * P, interval_ms=gap_ms))
        else:
            print(f"\n[{time.strftime('%H:%M:%S')}] READY -- send {N * P} edges, about "
                  f"{args.gap_s:g} s apart. Edge k fires filament "
                  f"{' -> '.join(map(str, fils))} in that order.\n", flush=True)

        # ── 6. wait, then report ─────────────────────────────────────────────
        deadline = time.time() + total_ms / 1000 + 10
        last_done = -1
        while time.time() < deadline:
            sts = {c: ct.shv_status(c) for c in armed}
            done = max((s.get("totalPulsesDone") or 0) for s in sts.values())
            if done != last_done:
                nxt = fils[done // P] if done < N * P else None
                print(f"[{time.strftime('%H:%M:%S')}] {done}/{N * P} fired"
                      + (f", next: filament {nxt}" if nxt is not None else ""), flush=True)
                last_done = done
            if all(s.get("state") not in (1, 2) for s in sts.values() if s):
                break
            time.sleep(0.5)
        for c in armed:
            s = ct.shv_status(c)
            print(f"controller {c}: {s.get('state_name', s.get('state'))}"
                  + (f", stop reason {s.get('stopReason')}" if s.get("stopReason") else ""))

        # The last filament's IDLE row is never applied (see the docstring).
        step(f"IDLE filament {fils[-1]}", ct.idle_one(fils[-1], current_ma=args.idle_ma))

        log = sorted((r for c in armed for r in ct.shv_pulse_log(c)),
                     key=lambda r: (r.get("seq", 0), r.get("tOnUs", 0)))
        ev = ct.pulse_events_ma(since=cursor).get("events") or []
        print(f"\n{len(log)} pulse(s) fired, {len(ev)} measured")
        print(" #  filament  heating at the pulse        emission")
        for i, r in enumerate(log):
            meas, tgt = r.get("heat_meas_mA"), r.get("heat_target_mA")
            e = ev[i] if i < len(ev) else {}
            em = e.get("emission_ma")
            print(f"{i:2d}  {r.get('filament'):8}  "
                  f"{'—' if meas is None else meas:>5} / {'—' if tgt is None else tgt:<5} mA"
                  f"{'   (below target: gap too short?)' if meas is not None and tgt and meas < tgt - 150 else '':35s}"
                  f"  {'—' if em is None else f'{em:.2f} mA'}")
        if len(ev) != len(log):
            print("WARNING: measured events and fired pulses do not line up one to one -- "
                  "pair them by time, not by position")
    finally:
        # ── 7. teardown (energised() STOPs every filament on the way out) ────
        print("\nteardown:", flush=True)
        steps = [(f"disarm controller {c}", lambda c=c: ct.shv_disarm(c)) for c in (armed or [1, 2])]
        steps.append(("detector off", ct.ready_disarm))
        if hv_turned_on:
            steps += [("emission off", lambda: ct.enable_emission(False)),
                      ("focus off", lambda: ct.enable_focus(False))]
        for label, fn in steps:
            try:
                res = fn()
                ok = res.get("ok", True) if isinstance(res, dict) else bool(res)
                print(f"  {label:22s} {'ok' if ok else 'FAILED -- CHECK THE BENCH: ' + str(res.get('error'))}")
            except Exception as exc:
                print(f"  {label:22s} FAILED -- CHECK THE BENCH: {exc}")
        print(f"  filaments {fils} -> STOP (energised)")
