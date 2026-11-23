#!/usr/bin/env python3
"""Bake-out: heat the filaments ONE AT A TIME to a fixed voltage for a few
seconds, around the ring, round and round -- paced by the vacuum.

    python3 examples/bake_out.py --host 100.83.111.52:8780
    python3 examples/bake_out.py --host 100.83.111.52:8780 --mv 5000 --hold-s 5 --skip 26
    python3 examples/bake_out.py --host 100.83.111.52:8780 --cycles 3 --idle-ma 1000
    python3 examples/bake_out.py --host 100.83.111.52:8780 --only 12 40 --cycles 1   # just these two

Every filament rests at its CC IDLE (--idle-ma) and takes its turn at --mv for
--hold-s, then goes back to IDLE before the next one starts -- so at most ONE
filament is ever above IDLE, and the gas comes out one filament at a time.

THE VACUUM SETS THE PACE. Before each turn the pressure must be below
--pause-above (default half the backend's limit); if it is not, the script
waits until it is under --resume-below. During a turn, if the pressure
climbs past --cut-above (default 0.8 x the limit) that filament goes back to
IDLE at once. The backend's own interlock (a vacuum over the limit puts every
filament to SLEEP and latches) stays the last line, and should never be
reached. A latched HV guard / vacuum trip stops this script: clearing it is
a person's decision.

SAFE IF THIS SCRIPT DIES. Each turn is a TIMED voltage hold
(voltage_one(..., active_s=)): the BACKEND puts the filament back to IDLE when
the time runs out, whether or not this process is still there. Ctrl-C also
returns the current filament to IDLE.

NO HV. Emission and focus are not touched; this is heating only.

THE LOG (CSV, one row per turn) says, per filament: whether it arrived at
--mv, its current there, and the vacuum before and at its peak during the
turn -- the "rise" column shows which filaments still outgas. A summary per
cycle lists the gassiest five.

Filaments are taken in ring order (the backend's filament order). --skip
takes PHYSICAL filament numbers (FID), like the dead list; dead filaments are
always skipped.
"""
import argparse
import csv
import datetime
import sys
import time

import _path  # noqa: F401
from ct_simple_control import CTClient

POLL_S = 0.5            # vacuum poll during a turn, s
WAIT_LOG_EVERY_S = 30   # how often a vacuum wait says it is still waiting, s


def now() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def pressure(ct: CTClient):
    v = ct.vacuum() or {}
    return v.get("pressure_torr"), v.get("limit_torr")


def guard_trip(ct: CTClient):
    return (ct.hv_guard() or {}).get("tripped")


def wait_for_vacuum(ct: CTClient, resume_below: float, pause_above: float) -> None:
    """Return once the pressure is under pause_above; if it was not, wait
    until it is under resume_below (hysteresis). Raises on a latched trip."""
    p, _ = pressure(ct)
    if p is not None and p < pause_above:
        return
    t0, said = time.monotonic(), -WAIT_LOG_EVERY_S
    while True:
        trip = guard_trip(ct)
        if trip:
            raise SystemExit(f"[{now()}] HV guard / vacuum latch tripped ({trip.get('source')}, "
                             f"{trip.get('at')}): stopping. Clearing it is a person's decision.")
        p, _ = pressure(ct)
        if p is not None and p < resume_below:
            print(f"[{now()}] vacuum {p:.2e} Torr -- going on after {time.monotonic() - t0:.0f} s")
            return
        if time.monotonic() - said >= WAIT_LOG_EVERY_S:
            said = time.monotonic()
            print(f"[{now()}] waiting for the vacuum: {p if p is None else f'{p:.2e}'} Torr, "
                  f"resume below {resume_below:.2e}")
        time.sleep(2.0)


def one_turn(ct: CTClient, f: int, a) -> dict:
    """Heat filament f (this client's numbering) to a.mv for a.hold_s, then IDLE."""
    p0, _ = pressure(ct)
    row = {"filament": f, "vac_before": p0, "vac_peak": p0, "ok": False,
           "arrived_s": None, "mA": None, "cut_short": False, "error": ""}
    t0 = time.monotonic()
    r = ct.voltage_one(f, a.mv, active_s=a.hold_s, then_idle_ma=a.idle_ma,
                       timeout_s=max(3.0, a.hold_s))
    h = r.get("heating") or {}
    row["arrived_s"], row["mA"] = h.get("elapsed_s"), h.get("measured_ma")
    if not r.get("ok"):
        row["error"] = str(r.get("error") or h.get("error") or r)[:200]
    try:
        # hold, watching the vacuum; the backend ends the hold at active_s anyway
        while time.monotonic() - t0 < a.hold_s:
            p, _ = pressure(ct)
            if p is not None:
                row["vac_peak"] = max(row["vac_peak"] or 0.0, p)
                if p >= a.cut_above:
                    row["cut_short"] = True
                    break
            time.sleep(POLL_S)
    finally:
        back = ct.idle_one(f, a.idle_ma)          # explicit: ends the hold now
        if not back.get("ok"):
            row["error"] = (row["error"] + " | back to IDLE: " + str(back.get("error")))[:300]
    row["ok"] = bool(r.get("ok")) and bool(back.get("ok"))
    return row


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", required=True, help="backend, host or host:port")
    ap.add_argument("--mv", type=int, default=5000, help="bake voltage, mV (default 5000)")
    ap.add_argument("--hold-s", type=float, default=5.0, help="time at --mv per turn, s (default 5)")
    ap.add_argument("--gap-s", type=float, default=0.5, help="pause between turns, s (default 0.5)")
    ap.add_argument("--idle-ma", type=int, default=1000, help="IDLE current between turns, mA (default 1000)")
    ap.add_argument("--skip", type=int, nargs="*", default=[], help="physical filament numbers (FID) to leave out")
    ap.add_argument("--only", type=int, nargs="*", default=None,
                    help="bake ONLY these physical filaments (FID), e.g. the gassiest ones")
    ap.add_argument("--cycles", type=int, default=0, help="rounds of the ring; 0 = until Ctrl-C (default)")
    ap.add_argument("--pause-above", type=float, default=None, help="Torr; default 0.5 x the backend limit")
    ap.add_argument("--resume-below", type=float, default=None, help="Torr; default 0.4 x the backend limit")
    ap.add_argument("--cut-above", type=float, default=None, help="Torr; default 0.8 x the backend limit")
    ap.add_argument("--log", default=None, help="CSV file (default bake_<date>_<time>.csv here)")
    a = ap.parse_args()

    ct = CTClient(a.host, client_id="bake-out")
    p, limit = pressure(ct)
    if limit is None:
        print("no vacuum limit from the backend -- refusing to bake blind")
        return 2
    a.pause_above = a.pause_above or 0.5 * limit
    a.resume_below = a.resume_below or 0.4 * limit
    a.cut_above = a.cut_above or 0.8 * limit
    if not a.resume_below < a.pause_above < a.cut_above < limit:
        print(f"need resume-below < pause-above < cut-above < limit ({limit:.2e}); got "
              f"{a.resume_below:.2e} / {a.pause_above:.2e} / {a.cut_above:.2e}")
        return 2
    trip = guard_trip(ct)
    if trip:
        print(f"HV guard / vacuum latch is tripped ({trip.get('source')}, {trip.get('at')}): clear it first")
        return 2

    order = ct.filament_order           # this client's number -> physical (sparse)
    dead = set(ct.dead)
    skip = set(a.skip)
    fils = [i for i in range(ct.FILAMENT_COUNT) if order.get(i, i) not in dead | skip
            and (a.only is None or order.get(i, i) in set(a.only))]
    if not fils:
        print("no filament left to bake (all dead, skipped, or not in --only)")
        return 2
    log = a.log or datetime.datetime.now().strftime("bake_%Y%m%d_%H%M%S.csv")
    print(f"[{now()}] baking {len(fils)} filaments at {a.mv} mV for {a.hold_s} s each, IDLE {a.idle_ma} mA "
          f"between; vacuum {p} Torr, limit {limit:.1e}, pause above {a.pause_above:.1e}, "
          f"cut above {a.cut_above:.1e}. Log: {log}. Ctrl-C to stop.")

    cols = ["time", "cycle", "filament", "fid", "ok", "arrived_s", "mA", "vac_before", "vac_peak",
            "rise", "cut_short", "error"]
    cycle = 0
    with open(log, "w", newline="") as fh, ct.lease(ttl=120, note="bake-out, one filament at a time"):
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        try:
            while a.cycles == 0 or cycle < a.cycles:
                cycle += 1
                rows = []
                for f in fils:
                    wait_for_vacuum(ct, a.resume_below, a.pause_above)
                    row = one_turn(ct, f, a)
                    vb, vp = row["vac_before"], row["vac_peak"]
                    row.update(time=now(), cycle=cycle, fid=order.get(f, f),
                               rise=(vp - vb) if (vb is not None and vp is not None) else None)
                    w.writerow(row)
                    fh.flush()
                    rows.append(row)
                    if not row["ok"] or row["cut_short"]:
                        print(f"[{now()}] {f} (FID {row['fid']}): ok={row['ok']} cut_short={row['cut_short']} "
                              f"{row['error']}")
                    time.sleep(a.gap_s)
                bad = [r for r in rows if not r["ok"]]
                gassy = sorted((r for r in rows if r["rise"] is not None), key=lambda r: -r["rise"])[:5]
                peak = max((r["vac_peak"] for r in rows if r["vac_peak"] is not None), default=None)
                print(f"[{now()}] cycle {cycle}: {len(rows) - len(bad)}/{len(rows)} ok, peak "
                      f"{peak if peak is None else f'{peak:.2e}'} Torr; gassiest (FID: rise) "
                      + ", ".join(f"{r['fid']}: {r['rise']:.1e}" for r in gassy))
        except KeyboardInterrupt:
            print(f"\n[{now()}] stopped by Ctrl-C")
    print(f"[{now()}] done; every filament is back at IDLE {a.idle_ma} mA. Log: {log}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
