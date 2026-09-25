#!/usr/bin/env python3
"""Check that Result prints every result structure this client produces.

    python3 tests/test_result_repr.py              # recorded + synthetic
    python3 tests/test_result_repr.py --live       # ...plus live backend calls

Needs no hardware. `--live` needs backend.py running, but not a controller.

WHY THIS EXISTS. The readable-repr work was declared finished and the very next
question -- "what does fire_single_pulse print like?" -- showed it folding away
`measured`, which is the entire point of firing a pulse. A formatter is only as
good as the structures it has actually been shown, and this client returns
several dozen shapes: scalars, nested dicts, lists of records, 96-element
arrays, empty dicts, dicts with no "ok" key at all.

THE INVARIANT IS "NOTHING DISAPPEARS". Every top-level key of the input must
appear in the output. That is what makes the formatter safe to rely on: a
reader cannot know in advance which field turns out to matter, so a formatter
that decides for them is one that will eventually hide the fault they were
looking for.

The second check is that it stays READABLE -- a bounded line length -- because
"print everything" is trivially satisfied by dumping the dict, which is what
this replaced.
"""
import _path  # noqa: F401  — makes ct_simple_control importable from tests/

import argparse
import glob
import json
import os
import sys

from ct_simple_control import CTClient, Result


#: A line longer than this is a wall again. File paths and URLs are exempt --
#: they are single unbreakable tokens, and a wrapped path cannot be copied,
#: which is worse than a long line.
MAX_LINE = 96


def unbreakable(line: str) -> bool:
    """A line that is long only because it holds one unsplittable token."""
    return max((len(w) for w in line.split()), default=0) > MAX_LINE - 20


def check(name: str, data: dict) -> list[str]:
    """Render one structure and report what is wrong with the result."""
    problems = []
    try:
        txt = repr(Result(data))
    except Exception as exc:                      # a formatter must never raise
        return [f"{name}: repr() raised {type(exc).__name__}: {exc}"]
    # Nothing disappears from full(). "ok" is in the header rather than the
    # body. The PRINT may hide firmware-level detail, but only behind the
    # pulse table and only when it says so; full() must still carry every key.
    full = Result(data).full()
    for k in data:
        if k != "ok" and f"{k}:" not in full and f"{k}=" not in full:
            problems.append(f"{name}: key {k!r} is not in full()")
    hidden = [k for k in data if k != "ok" and f"{k}:" not in txt and f"{k}=" not in txt]
    if hidden and (not set(hidden) <= set(Result._PULSE_DETAIL)
                   or "firmware detail hidden" not in txt):
        problems.append(f"{name}: print hides {hidden} without saying so")
    for n, line in enumerate(txt.splitlines()):
        if len(line) > MAX_LINE and not unbreakable(line):
            problems.append(f"{name}: line {n} is {len(line)} chars and is "
                            f"breakable — {line[:60]}…")
    return problems


# ── the structures ───────────────────────────────────────────────────────────

SYNTHETIC = {
    "empty": {},
    "no-ok-key": {"delayUs": 3000, "applies": True},
    "bare-error": {"ok": False, "error": "controller 1 not connected"},
    "long-error": {"ok": False, "error": "IDLE 2500 mA is above the 2000 mA "
                   "ceiling — the RP2350 would clamp it to 2000 and report "
                   "success, so a verify would wait for a current that never "
                   "arrives."},
    "path": {"ok": True, "json": "/Users/someone/Documents/PlatformIO/Projects/"
             "ESP32CtPowerController/tools/ct_gui/calibration/x_20260922.json"},
    "fire-with-faults": {
        "ok": True, "fired": 3, "schedule": "reused",
        "records": [{"filament": 8, "seq": 0, "hv_stuck_on": True,
                     "unverified": False, "heat_meas_mA": 2774}],
        "status": {"state": 0, "done": 3, "unsafeSlots": 32, "uncounted": 1}},
    "deep-nesting": {"ok": True, "a": {"b": {"c": {"d": [1, 2, {"e": "deep"}]}}}},
    "all-none": {"ok": True, "x": None, "y": None, "z": None},
    "big-int-list": {"ok": True, "present": list(range(96))},
    "unicode": {"ok": False, "error": '灯丝 8 未达到设定值 — "IDLE" 被静默钳位'},
    "wide-record": {"ok": True, "points": [
        {f"field_{i}": i * 1.5 for i in range(20)}]},
    "list-of-lists": {"ok": True, "grid": [[1, 2], [3, 4]]},
    # A real two-controller shot, field for field: filament 81, 2026-09-23 --
    # a C1 filament, so no envelope_from; the ~53 mA artefact with a suspect
    # background, which is exactly the case where a reader must find
    # background_suspect without hunting for it.
    "fire-measured": {
        "ok": True, "fired": 1,
        "records": [{"filament": 81, "flags": 0, "seq": 0, "tOnUs": 0,
                     "durationUs": 1000, "read165": 32, "on_mismatch": False,
                     "hv_stuck_on": False, "unverified": False,
                     "heat_meas_mA": 780, "heat_meas_unavailable": None,
                     "heat_target_mA": 1700, "heat_target_unavailable": None}],
        "status": {"state": 3, "stopReason": 1, "entryIndex": 1,
                   "filamentIndex": 255, "totalPulsesTarget": 1,
                   "totalPulsesDone": 1, "triggerEdges": 1, "capabilityFlags": 15},
        "schedule": "downloaded:plan-changed",
        "measured": [{"id": 91, "t_us": 1561250884, "on_us": 1000, "peak": 2400,
                      "bg": 1038, "bg_sigma4": 29, "plateau": 2308,
                      "post_bg": 619, "integral": 1269751, "background_n": 50,
                      "background_gap": 100, "rate_hz": 1000000,
                      "recv_ms": 5865898, "empty_envelope": False,
                      "peak_ma": 68.496, "plateau_ma": 64.649, "bg_ma": 11.538,
                      "post_bg_ma": -5.985, "peak_net_ma": 56.958,
                      "plateau_net_ma": 53.111, "diode_ma": 1.9818,
                      "emission_ma": 51.129, "background_windowing": True,
                      "integral_saturated": False, "background_partial": False,
                      "background_pre_post_delta": 419,
                      "background_suspect": True,
                      "background_note": "pre-pulse background is +419.0 "
                          "counts off the post-pulse one (57.8 sigma). The PRE "
                          "window has no settle guard, so it is the suspect "
                          "one — and the charge depends on it.",
                      "integral_mams": 53.1003, "duration_saturated": False,
                      "integral_mams_sigma": 0.00958774,
                      "emission_mams": 51.1185}],
        "ref_mv": 1228.3},
    # mosfet_test, 2026-09-23 (59 and 68 dead). Its table printer was removed:
    # this is now the only rendering.
    "mosfet-test": {
        "ok": False, "emission_v": -101, "expected_ma": 0.9818,
        "tolerance_frac": 0.35,
        "results": {
            50: {"measured_ma": 1.0177, "expected_ma": 0.9818, "ratio": 1.037,
                 "verdict": "pass", "shots": 3, "note": None},
            59: {"measured_ma": -0.0417, "expected_ma": 0.9818, "ratio": -0.042,
                 "verdict": "dead", "shots": 3,
                 "note": "-0.042 mA against 0.982 mA expected — the MOSFET is "
                         "not conducting"}},
        "counts": {"pass": 1, "dead": 1, "inconclusive": 0},
        "problems": ["filament 59: dead — -0.042 mA against 0.982 mA expected "
                     "— the MOSFET is not conducting"]},
    # fit_richardson's shape, values from the 2026-09-22 bench fit.
    "richardson-fit": {
        "ok": True, "trustworthy": False, "n_points": 10, "r_squared": 0.99854,
        "work_function_eV": 3.646, "r_lead_ohm": 0.2, "r_lead_fitted": False,
        "r_cold_ohm": 0.257, "richardson_a_eff_ma_per_k2": 1.2e-3,
        "temperature_span_K": 297,
        "sensitivity_to_r_lead": {"d_work_function_eV_per_ohm": -2.28,
                                  "d_mean_T_K_per_ohm": -590},
        "points": [{"heat_mA": 2470.0, "commanded_ma": 2500, "r_total_ohm": 2.9512,
                    "r_fil_ohm": 2.7512, "r_ratio": 10.705, "T_K": 2038.2,
                    "net_ma": 3.374, "emission_ma": 1.401,
                    "inv_T": 0.00049063, "ln_i_over_t2": -14.8021,
                    "residual": 0.0132}],
        "dropped": [{"heat_mA": 2270.0, "emission_ma": 0.015}],
        "warnings": ["r_lead_ohm was given, not fitted — the emission curve "
                     "cannot determine it; take it from the I-V side"]},
}

#: Read-only and fast. Deliberately NOT every zero-argument method: several of
#: those (present_filaments, self_test, chip_health) are multi-second hardware
#: scans, and a formatter test that takes minutes and touches boards is one
#: nobody runs.
LIVE_METHODS = ["status", "safety", "hv_status", "get_mapping", "dead_details",
                "filament_order_status", "get_trigger_delay", "thermal_history",
                "diode_path_ma"]


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--live", action="store_true",
                    help="also call read-only methods against a running backend")
    ap.add_argument("--host", default="localhost")
    ap.add_argument("--recorded", default="calibration/*.json",
                    help="glob of saved run records to render (default: the "
                         "calibration directory)")
    ap.add_argument("--show", metavar="NAME",
                    help="print one structure's rendering instead of checking")
    args = ap.parse_args(argv)

    cases: list[tuple[str, dict]] = list(SYNTHETIC.items())

    for path in sorted(glob.glob(args.recorded)):
        try:
            d = json.loads(open(path).read())
        except Exception:
            continue
        if isinstance(d, dict):
            cases.append((f"recorded:{os.path.basename(path)}", d))

    if args.live:
        ct = CTClient(args.host, keepalive=False, timeout=3.0)
        for name in LIVE_METHODS:
            if not hasattr(ct, name):
                continue
            try:
                r = getattr(ct, name)()
            except Exception as exc:
                print(f"  live:{name} raised {type(exc).__name__}: {exc}")
                continue
            if isinstance(r, dict):
                cases.append((f"live:{name}", dict(r)))

    if args.show:
        for name, d in cases:
            if args.show in name:
                print(f"=== {name} ===")
                print(Result(d))
                print()
        return 0

    problems = []
    print(f"{'structure':<40} {'keys':>5} {'lines':>6} {'longest':>8}")
    for name, d in cases:
        txt = repr(Result(d))
        lines = txt.splitlines()
        found = check(name, d)
        problems += found
        print(f"  {name:<38} {len(d):>5} {len(lines):>6} "
              f"{max(len(x) for x in lines):>8}"
              + ("" if not found else "   <-- " + found[0].split(': ', 1)[1][:50]))
    print("\n" + "=" * 72)
    print(f"{len(cases) - len({p.split(':')[0] for p in problems})}"
          f"/{len(cases)} structures render cleanly")
    for p in problems:
        print(f"  {p}")
    print("=" * 72)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
