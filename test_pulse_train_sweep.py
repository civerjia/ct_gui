#!/usr/bin/env python3
"""Reliability sweep for the confirmed-correct pulse-train firing sequence
(test_pulse_train.py, with hv_grid_set(on=True) removed -- see that file's
module docstring for why it was pre-asserting the pulse-drive bit early and
making pulse #1 look anomalously long on a scope; that's now fixed and
scope-confirmed correct).

Sweeps WIDTH_US x GAP_US, REPEATS trials each, firing NUM_PULSES per trial
via sync_io::startBurst() (real hardware-timed SyncOut edges) and using the
RP2350's OWN schedule status (state==Complete, totalPulsesDone==NUM_PULSES)
as the pass/fail ground truth -- not the STM32 pulse_detector, which has a
separate, unrelated SPI-capture overrun issue (2048 samples lost every run,
still unfixed) that would otherwise make every trial here look like a
failure for the wrong reason. This sweep is entirely about RP2350/ESP32
trigger-train reliability across width/gap combinations, not about the
on-chip current measurement.

No hv_grid_set() anywhere -- confirmed unnecessary (arm() succeeds and the
train fires correctly without it, filament stays in STANDBY with ISO
already on from standby_one()).
"""
import sys, time
import requests

sys.path.insert(0, '.')
from ct_simple_control import CTClient

ESP32_HOST = '192.168.50.173'
FILAMENT = 7
NUM_PULSES = 4
REPEATS = 15                     # root-causing the ~10% residual fault rate
                                  # seen at width=2000/4000 -- need enough
                                  # occurrences to catch the esp32_fired vs
                                  # rp2350_done diagnostic on a real failure
WIDTHS_US = [2000, 4000]         # the two widths that showed faults
GAPS_US = [1000, 1500]


def fire_train(ct, controller, width_us, gap_us, num_pulses):
    """One trial: arm num_pulses of width_us at gap_us spacing, wait for the
    RP2350's own completion status, disarm. Returns (ok, detail_dict)."""
    period_us = width_us + gap_us
    ct._shv(controller, {"op": "disarm"})
    ct._shv(controller, {"op": "set_config", "interPulseMs": 3000,
                         "maxOnMs": max(40, round(width_us / 1000) + 10), "totalMs": 15000})
    ct._shv(controller, {"op": "clear_table"})
    ct._shv(controller, {"op": "set_entries",
                         "entries": [{"filament": ct._phys(FILAMENT), "numPulses": 1,
                                     "width": width_us}]})
    arm_r = ct._shv(controller, {"op": "arm", "repeats": num_pulses})
    if not arm_r.get("ok") or arm_r.get("reject", 0) != 0:
        return False, {"stage": "arm", "arm": arm_r}

    t_fire = time.monotonic()
    rate_hz = max(1, round(1e6 / period_us))
    burst = requests.post(f"http://{ESP32_HOST}/sync/burst",
                          params={"count": num_pulses, "rate_hz": rate_hz},
                          timeout=5.0).json()
    if not burst.get("ok"):
        ct._shv(controller, {"op": "disarm"})
        return False, {"stage": "burst", "burst": burst}

    deadline = time.monotonic() + 5
    bs = {}
    while time.monotonic() < deadline:
        bs = requests.get(f"http://{ESP32_HOST}/sync/burst/status", timeout=2.0).json()
        if not bs.get("running"):
            break
        time.sleep(0.02)

    # Wait for the RP2350's OWN status, not just the ESP32 burst status --
    # disarming the instant the ESP32 side looks done is a real race that
    # can cut the last pulse short (confirmed once as totalPulsesDone=3/4).
    shv_deadline = time.monotonic() + 2.0
    final_st = {}
    while time.monotonic() < shv_deadline:
        final_st = ct._shv(controller, {"op": "status"})
        fs = final_st.get("status", {})
        if fs.get("state") in (3, 4) or fs.get("totalPulsesDone", 0) >= num_pulses:
            break
        time.sleep(0.01)
    elapsed_ms = (time.monotonic() - t_fire) * 1000

    ct._shv(controller, {"op": "disarm"})

    fs = final_st.get("status", {})
    ok = (fs.get("state") == 3 and fs.get("stopReason") == 1
         and fs.get("totalPulsesDone") == num_pulses)
    # esp32_fired = how many edges the ESP32's gptimer ISR actually emitted
    # (ground truth for "did ESP32 send them all"). rp2350_done = how many
    # the RP2350 registered before completing/faulting. esp32_fired <
    # num_pulses means ESP32 itself dropped an edge (gptimer/ISR-side bug);
    # esp32_fired == num_pulses but rp2350_done < num_pulses means ESP32
    # sent everything and RP2350 missed/dropped one (PIO/latch-SM side).
    return ok, {"stage": "done", "elapsed_ms": elapsed_ms, "burst": burst,
               "esp32_fired": bs.get("fired"), "final_status": fs}


def main():
    ct = CTClient(client_id="pulse_train_sweep")
    with ct.session():
        print(ct.describe(ct.standby_one(FILAMENT)))
        controller = ct.filament_to_board(FILAMENT)["controller"]

        results = []
        total = len(WIDTHS_US) * len(GAPS_US) * REPEATS
        n = 0
        for width_us in WIDTHS_US:
            for gap_us in GAPS_US:
                period_us = width_us + gap_us
                min_period = width_us + 10   # RP2350's documented floor
                margin_note = "" if period_us >= min_period else "  <-- BELOW DOCUMENTED MINIMUM"
                fails = []
                elapsed_list = []
                for trial in range(REPEATS):
                    n += 1
                    ok, detail = fire_train(ct, controller, width_us, gap_us, NUM_PULSES)
                    if ok:
                        elapsed_list.append(detail["elapsed_ms"])
                    else:
                        fails.append(detail)
                    fail_note = ""
                    if not ok and detail.get("stage") == "done":
                        esp32_fired = detail.get("esp32_fired")
                        rp2350_done = detail.get("final_status", {}).get("totalPulsesDone")
                        side = ("ESP32-side (gptimer/ISR dropped an edge)" if esp32_fired is not None
                               and esp32_fired < NUM_PULSES else
                               "RP2350-side (ESP32 sent them all, RP2350 missed one)")
                        fail_note = f"  [esp32_fired={esp32_fired} rp2350_done={rp2350_done} -> {side}]"
                    print(f"[{n}/{total}] width={width_us}us gap={gap_us}us trial={trial+1}/{REPEATS} "
                         f"-> {'OK' if ok else 'FAIL'}{margin_note}{fail_note}")
                results.append({
                    "width_us": width_us, "gap_us": gap_us, "period_us": period_us,
                    "passed": REPEATS - len(fails), "total": REPEATS,
                    "fails": fails, "elapsed_ms": elapsed_list,
                })
                time.sleep(0.1)   # brief settle between combos

        print("\n" + "=" * 78)
        print(f"{'width_us':>9} {'gap_us':>7} {'period_us':>10} {'pass/total':>11} {'avg_ms':>8}  notes")
        print("-" * 78)
        any_fail = False
        for r in results:
            avg_ms = (sum(r["elapsed_ms"]) / len(r["elapsed_ms"])) if r["elapsed_ms"] else float("nan")
            note = ""
            if r["passed"] < r["total"]:
                any_fail = True
                reasons = set()
                for f in r["fails"]:
                    if f["stage"] == "arm":
                        reasons.add(f"arm rejected ({f['arm'].get('reject')})")
                    elif f["stage"] == "burst":
                        reasons.add("burst failed to start")
                    else:
                        fs = f.get("final_status", {})
                        reasons.add(f"state={fs.get('state')} stop={fs.get('stopReason')} "
                                   f"done={fs.get('totalPulsesDone')}/{NUM_PULSES}")
                note = "; ".join(reasons)
            print(f"{r['width_us']:>9} {r['gap_us']:>7} {r['period_us']:>10} "
                 f"{r['passed']:>4}/{r['total']:<6} {avg_ms:>8.1f}  {note}")
        print("=" * 78)
        print("ALL COMBINATIONS PASSED" if not any_fail else "SOME COMBINATIONS FAILED -- see notes above")


if __name__ == "__main__":
    main()
