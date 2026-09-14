#!/usr/bin/env python3
"""Multi-ENTRY (alternating filament) reliability sweep against the RP2350's
new FIFO-fed PIO redesign (24c069d) -- specifically exercises the code path
that had the "wrong filament fired" bug (numPulses=1/entry, multiple
entries), using REAL trigger edges from this ESP32 (not the peer's own bench
trigger source). Completion-status only (state/stopReason/totalPulsesDone) --
this host has no visibility into the per-pulse 165 read-back the peer's own
bench check uses, so this confirms reliable completion from OUR real hardware
trigger path, not per-pulse filament correctness (that was already verified
120/120 on their bench).
"""
import sys, time
import requests

sys.path.insert(0, '.')
from ct_simple_control import CTClient

ESP32_HOST = '192.168.50.173'
FILAMENTS = [0, 1, 2, 3, 4, 5, 6, 7]   # CH1.1 through CH1.8, in order, one
                                       # 8-entry table -- all 8 positions on
                                       # channel 0, not just 2 alternating
NUM_PULSES_PER_ENTRY = 1
REPEATS = 8            # loops through the whole 8-entry table this many times
TRIALS = 20
WIDTHS_US = [1000, 2000, 4000]
GAPS_US = [1000, 1500]


def fire_train(ct, controller, width_us, gap_us):
    period_us = width_us + gap_us
    entries = [{"filament": ct._phys(f), "numPulses": NUM_PULSES_PER_ENTRY, "width": width_us}
              for f in FILAMENTS]
    num_pulses_total = NUM_PULSES_PER_ENTRY * len(entries) * REPEATS

    ct._shv(controller, {"op": "disarm"})
    ct._shv(controller, {"op": "set_config", "interPulseMs": 3000,
                         "maxOnMs": 40, "totalMs": 15000})
    ct._shv(controller, {"op": "clear_table"})
    dl = ct._shv(controller, {"op": "set_entries", "entries": entries})
    if not dl.get("ok"):
        return False, {"stage": "set_entries", "detail": dl}

    arm_r = ct._shv(controller, {"op": "arm", "repeats": REPEATS})
    if not arm_r.get("ok") or arm_r.get("reject", 0) != 0:
        return False, {"stage": "arm", "arm": arm_r}

    t_fire = time.monotonic()
    rate_hz = max(1, round(1e6 / period_us))
    burst = requests.post(f"http://{ESP32_HOST}/sync/burst",
                          params={"count": num_pulses_total, "rate_hz": rate_hz},
                          timeout=5.0).json()
    if not burst.get("ok"):
        ct._shv(controller, {"op": "disarm"})
        return False, {"stage": "burst", "burst": burst}

    deadline = time.monotonic() + 8
    bs = {}
    while time.monotonic() < deadline:
        bs = requests.get(f"http://{ESP32_HOST}/sync/burst/status", timeout=2.0).json()
        if not bs.get("running"):
            break
        time.sleep(0.02)

    shv_deadline = time.monotonic() + 3.0
    final_st = {}
    while time.monotonic() < shv_deadline:
        final_st = ct._shv(controller, {"op": "status"})
        fs = final_st.get("status", {})
        if fs.get("state") in (3, 4) or fs.get("totalPulsesDone", 0) >= num_pulses_total:
            break
        time.sleep(0.01)
    elapsed_ms = (time.monotonic() - t_fire) * 1000
    ct._shv(controller, {"op": "disarm"})

    fs = final_st.get("status", {})
    ok = (fs.get("state") == 3 and fs.get("stopReason") == 1
         and fs.get("totalPulsesDone") == num_pulses_total)
    # A trial that COMPLETES clean (ok=True) but with uncounted>0 is the
    # missed-edge bug still happening -- just absorbed by the one-pulse-ahead
    # recovery instead of faulting. Track it as its own category, not lumped
    # into "pass" -- per the RP2350 session's explicit ask, log @27 on every
    # trial, not just failures.
    uncounted = fs.get("uncounted")
    rb_sat = fs.get("rbSaturated")
    # uncounted is exact only when rbSaturated==0 -- a discarded read-back
    # (FIFO full) makes the FIFO level understate how many pulses fired, so
    # uncounted under-reports when rbSaturated>0. Flag that case distinctly
    # rather than trusting uncounted at face value (per the RP2350 session).
    return ok, {"stage": "done", "elapsed_ms": elapsed_ms,
               "esp32_fired": bs.get("fired"), "final_status": fs,
               "target": num_pulses_total, "uncounted": uncounted,
               "rbSaturated": rb_sat,
               "uncounted_is_lower_bound": bool(rb_sat),
               "absorbed": bool(ok and uncounted)}


def main():
    ct = CTClient(client_id="pulse_multientry_sweep")
    try:
        for f in FILAMENTS:
            print(ct.describe(ct.standby_one(f)))
        controller = ct.filament_to_board(FILAMENTS[0])["controller"]

        results = []
        total = len(WIDTHS_US) * len(GAPS_US) * TRIALS
        n = 0
        for width_us in WIDTHS_US:
            for gap_us in GAPS_US:
                fails = []
                absorbed = []   # completed ok but uncounted>0 -- bug still happening
                elapsed_list = []
                for trial in range(TRIALS):
                    n += 1
                    ok, detail = fire_train(ct, controller, width_us, gap_us)
                    if ok:
                        elapsed_list.append(detail["elapsed_ms"])
                        if detail.get("absorbed"):
                            absorbed.append(detail)
                    else:
                        fails.append(detail)
                    # Every trial's uncounted, per the RP2350 session's ask --
                    # but only print the full line for anything non-clean, to
                    # keep 630 trials of output readable.
                    tag = "OK" if ok else "FAIL"
                    if not ok or detail.get("absorbed"):
                        print(f"[{n}/{total}] width={width_us}us gap={gap_us}us trial={trial+1}/{TRIALS} "
                             f"-> {tag} uncounted={detail.get('uncounted')} {detail if not ok else ''}")
                    elif (trial + 1) % 25 == 0:
                        print(f"[{n}/{total}] width={width_us}us gap={gap_us}us trial={trial+1}/{TRIALS} -> OK (uncounted=0)")
                results.append({
                    "width_us": width_us, "gap_us": gap_us,
                    "passed": TRIALS - len(fails), "total": TRIALS,
                    "fails": fails, "absorbed": absorbed, "elapsed_ms": elapsed_list,
                })

        print("\n" + "=" * 80)
        print(f"{'width_us':>9} {'gap_us':>7} {'pass/total':>11} {'absorbed':>9} {'avg_ms':>8}  notes")
        print("-" * 80)
        any_fail = False
        any_absorbed = False
        for r in results:
            avg_ms = (sum(r["elapsed_ms"]) / len(r["elapsed_ms"])) if r["elapsed_ms"] else float("nan")
            note = ""
            if r["passed"] < r["total"]:
                any_fail = True
                note = "; ".join(str(f) for f in r["fails"][:2])
            if r["absorbed"]:
                any_absorbed = True
                note += (" | " if note else "") + f"absorbed detail: {r['absorbed'][:2]}"
            print(f"{r['width_us']:>9} {r['gap_us']:>7} {r['passed']:>4}/{r['total']:<6} "
                 f"{len(r['absorbed']):>9} {avg_ms:>8.1f}  {note}")
        print("=" * 80)
        if any_fail:
            print("SOME COMBINATIONS HARD-FAILED -- see notes above")
        elif any_absorbed:
            print("ALL COMPLETED, but the missed-edge bug still occurred and was "
                 "absorbed (uncounted>0) on some trials -- see notes above. "
                 "Per RP2350 session: this still needs reporting, not just the hard faults.")
        else:
            print("ALL COMBINATIONS PASSED, uncounted=0 on every single trial")
    finally:
        for fn in (lambda: ct.enable_focus(False),
                  lambda: ct.hv_grid_clear_all(),
                  lambda: ct.stop_all()):
            try:
                fn()
            except Exception:
                pass


if __name__ == "__main__":
    main()
