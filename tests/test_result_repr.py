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
    # Nothing disappears. "ok" is in the header rather than the body.
    for k in data:
        if k != "ok" and f"{k}:" not in txt and f"{k}=" not in txt:
            problems.append(f"{name}: key {k!r} is not in the output")
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
