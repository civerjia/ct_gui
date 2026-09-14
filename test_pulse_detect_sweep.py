#!/usr/bin/env python3
"""Sweep WIDTH_US x GAP_US, firing a real 4-pulse train each time (same
mechanism as test_pulse_train.py -- confirmed correct: no hv_grid_set,
stable HV, RP2350 waits for its own completion status), and counting how
many of the 4 real pulses the STM32's on-chip pulse_detector actually
reports via EVT_PULSE.

This exists to characterize a specific, unresolved finding: test_pulse_train.py
consistently detects only the FIRST pulse in a train and none of the rest,
even though V_actual is confirmed stable at the real commanded voltage and
RP2350 confirms all 4 pulses fired cleanly. Leading hypothesis: pulse_detector.c
line ~522 does `bg_push(samp)` using the sample AT THE COMMIT BOUNDARY (right
at the end of the post_bg window) -- if that sample isn't fully settled back to
baseline, it corrupts the rolling background estimate used to detect the NEXT
pulse's rise, and every pulse after the first stops registering.

If that hypothesis is right, detected-count should improve with LARGER GAP_US
(more time between pulses for later, legitimate bg_push() calls in
PD_STATE_IDLE to dilute/recover from the one bad sample) and should be roughly
independent of WIDTH_US. If detected-count is flat (always 1) regardless of
gap, that points to a different, more structural bug (e.g. a one-shot state
that never re-arms at all, not a slowly-recovering corrupted estimate).
"""
import sys, time
import requests

sys.path.insert(0, '.')
from ct_simple_control import CTClient
import serial

from test_pulse_train import ser_cmd, configure_known_timing, emission_ma

SER_PORT = '/dev/cu.usbmodem214201'
ESP32_HOST = '192.168.50.173'
FILAMENT = 7
RATE_HZ = 500_000
VOLTS = 40
NUM_PULSES = 4
US_PER_SAMPLE = 1e6 / RATE_HZ
TRIALS = 2   # per (width, gap) combo

WIDTHS_US = [1000, 2000]
GAPS_US = [200, 500, 1000, 2000, 4000, 8000]


def fire_and_measure(ct, ser, controller, width_us, gap_us, ref_v):
    import test_pulse_train as tpt
    period_us = width_us + gap_us
    post_bg_gap_samples = round(150 * RATE_HZ / 1e6)
    post_bg_n_samples = round(150 * RATE_HZ / 1e6)
    known_width_samples = round(width_us * RATE_HZ / 1e6)
    margin_samples = max(1, round(known_width_samples * 0.15))
    # Patch the module-level POST_BG_* used inside configure_known_timing's
    # command string (it reads them as globals of test_pulse_train).
    tpt.POST_BG_GAP_SAMPLES = post_bg_gap_samples
    tpt.POST_BG_N_SAMPLES = post_bg_n_samples
    if not configure_known_timing(ser, known_width_samples, margin_samples):
        return None, "known-timing config rejected"

    train_span_us = (NUM_PULSES - 1) * period_us + width_us + \
                   (post_bg_gap_samples + post_bg_n_samples) * US_PER_SAMPLE
    n_samples = min(4096, round(train_span_us / US_PER_SAMPLE) + 500)

    ct._shv(controller, {"op": "disarm"})
    ct._shv(controller, {"op": "set_config", "interPulseMs": 3000,
                         "maxOnMs": 40, "totalMs": 15000})
    ct._shv(controller, {"op": "clear_table"})
    ct._shv(controller, {"op": "set_entries",
                         "entries": [{"filament": ct._phys(FILAMENT), "numPulses": 1,
                                     "width": width_us}]})

    last_id = ct._get('/api/pulse-events').get('last_id', 0)
    ser_cmd(ser, "ready disarm", wait=0.3)
    ser_cmd(ser, f"ready arm {RATE_HZ} {n_samples} 80", wait=0.3)

    arm_r = ct._shv(controller, {"op": "arm", "repeats": NUM_PULSES})
    if not arm_r.get("ok") or arm_r.get("reject", 0) != 0:
        return None, f"arm rejected: {arm_r}"

    rate_hz = max(1, round(1e6 / period_us))
    burst = requests.post(f"http://{ESP32_HOST}/sync/burst",
                          params={"count": NUM_PULSES, "rate_hz": rate_hz},
                          timeout=5.0).json()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        bs = requests.get(f"http://{ESP32_HOST}/sync/burst/status", timeout=2.0).json()
        if not bs.get("running"):
            break
        time.sleep(0.02)

    shv_deadline = time.monotonic() + 2.0
    final_st = {}
    while time.monotonic() < shv_deadline:
        final_st = ct._shv(controller, {"op": "status"})
        fs = final_st.get("status", {})
        if fs.get("state") in (3, 4) or fs.get("totalPulsesDone", 0) >= NUM_PULSES:
            break
        time.sleep(0.01)
    ct._shv(controller, {"op": "disarm"})
    fs = final_st.get("status", {})
    rp2350_ok = (fs.get("state") == 3 and fs.get("totalPulsesDone") == NUM_PULSES)

    time.sleep(0.3)
    j = ct._get(f'/api/pulse-events?since={last_id}')
    events = j.get('events') or []
    ser_cmd(ser, "ready disarm", wait=0.3)

    return {"rp2350_ok": rp2350_ok, "n_events": len(events), "events": events}, None


def main():
    ct = CTClient(client_id="pulse_detect_sweep")
    ser = None
    try:
        print(ct.describe(ct.standby_one(FILAMENT)))
        controller = ct.filament_to_board(FILAMENT)["controller"]

        ser = serial.Serial(SER_PORT, 115200, timeout=1)
        time.sleep(0.3)
        ser.reset_input_buffer()

        hv_st = ct.hv_status()
        v_actual = ct.read_emission_v() if hv_st.get("ok") else None
        if not (hv_st.get("ok") and hv_st.get("emission_on") and v_actual is not None
               and abs(abs(v_actual) - VOLTS) <= 2.0):
            print(f"emission not already at target (status={hv_st}, V_actual={v_actual}) "
                 f"-- configuring from scratch")
            ct.set_emission_i(85.7)
            ct.set_emission_v(VOLTS)
            ct.enable_emission(True)
            time.sleep(2.0)
            v_actual = ct.read_emission_v()
        print(f"V_actual={v_actual}")

        ads = ct.read_ads_all()
        ref_v = (ads.get('ref_mv') / 1000) if ads.get('ok') and ads.get('ref_mv') is not None else 1.227

        results = []
        for width_us in WIDTHS_US:
            for gap_us in GAPS_US:
                counts = []
                for trial in range(TRIALS):
                    r, err = fire_and_measure(ct, ser, controller, width_us, gap_us, ref_v)
                    if err:
                        print(f"width={width_us} gap={gap_us} trial={trial+1}/{TRIALS} -> ERROR: {err}")
                        counts.append(-1)
                        continue
                    counts.append(r["n_events"])
                    print(f"width={width_us} gap={gap_us} trial={trial+1}/{TRIALS} "
                         f"-> rp2350_ok={r['rp2350_ok']} detected={r['n_events']}/{NUM_PULSES}")
                    time.sleep(0.2)
                results.append((width_us, gap_us, counts))

        print("\n" + "=" * 60)
        print(f"{'width_us':>9} {'gap_us':>7}  detected/{NUM_PULSES} per trial")
        print("-" * 60)
        for width_us, gap_us, counts in results:
            print(f"{width_us:>9} {gap_us:>7}  {counts}")
        print("=" * 60)
    finally:
        if ser is not None:
            try:
                ser_cmd(ser, "ready disarm", wait=0.3)
                ser.close()
            except Exception:
                pass
        for fn in (lambda: ct.enable_focus(False),
                  lambda: ct.hv_grid_clear_all(),
                  lambda: ct.stop_all()):
            try:
                fn()
            except Exception:
                pass


if __name__ == "__main__":
    main()
