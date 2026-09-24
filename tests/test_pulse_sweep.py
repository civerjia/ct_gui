#!/usr/bin/env python3
"""Voltage sweep using the STM32 pulse_detector's on-chip summary
(EVT_PULSE / GET /api/pulse-events), NOT a raw-waveform UART fetch.

Known-timing mode (pulse_detector.h's pulse_cfg_t / pulse_result_t):
since ready_relay's trigger is synced to the RP2350's real pulse and the
commanded width is known ahead of time, amplitude-threshold rise/fall
detection is only used to confirm a pulse happened -- plateau_adc and
post_bg_mean are computed on-chip over FIXED sample ranges measured from
rise_sample, not threshold-detected ranges:
  plateau  = mean over [margin, known_width - margin), i.e. the pulse
             minus a transition margin off each end.
  post_bg  = mean over [known_width + post_bg_gap, known_width +
             post_bg_gap + post_bg_n) -- AFTER the known pulse end, with a
             settle gap (the real fall can be slower than the commanded
             pulse edge; the test bench's own decay isn't representative
             of production hardware, so this gap is generous, not tuned).
The whole event commits once elapsed samples reach known_width +
post_bg_gap + post_bg_n, regardless of whether the amplitude threshold
ever reports a fall -- bounds commit latency to a KNOWN sample count, sized
here to fit comfortably inside a 1 ms minimum pulse-to-pulse spacing. So
post_bg ships in the SAME 20-byte EVT_PULSE event as everything else --
no separate UART round trip, no risk of reading across pulses in a train.

bg_before still comes from the SAME event's `background_mean` (the
rolling background captured at the rising edge) -- also on-chip, also
already in the event, nothing separate needed for it either.
"""
import _path  # noqa: F401  — makes ct_simple_control importable from tests/
import sys, time
import serial

sys.path.insert(0, '.')
from ct_simple_control import CTClient

SER_PORT = '/dev/cu.usbmodem214201'
FILAMENT = 7
RATE_HZ = 500_000
N_SAMPLES = 2048          # ready_relay's capture window (must cover known
                           #_width + post_bg_gap + post_bg_n with margin)
WIDTH_US = 2000
VOLTAGES = [10, 20, 30, 40, 50]
MAX_ATTEMPTS = 4
US_PER_SAMPLE = 1e6 / RATE_HZ

# Known-timing config, all in SAMPLES at RATE_HZ (see module docstring).
KNOWN_WIDTH_SAMPLES = round(WIDTH_US * RATE_HZ / 1e6)
PLATEAU_MARGIN_SAMPLES = round(KNOWN_WIDTH_SAMPLES * 0.15)
POST_BG_GAP_SAMPLES = round(200 * RATE_HZ / 1e6)     # 200us settle
POST_BG_N_SAMPLES = round(200 * RATE_HZ / 1e6)       # 200us average
# Detector threshold config -- unchanged from firmware defaults (already
# proven to detect rise/fall reliably across V=20-50 this session).
PD_BG_WINDOW, PD_MIN_DUR, PD_K_SIGMA, PD_GAP, PD_LOCAL, PD_RATIO = 20, 64, 3, 0, 0, 0


def emission_ma(raw, ref_v):
    # Same formula as web/power.js's emissionMa() / tests.js's
    # peakToMa() -- see those files' comments. raw is the STM32's own
    # 12-bit ADC code (VDDA=3.3V ref), not an ESP32 ADC_ATTEN_12 reading;
    # R_sense=10ohm, G=8.2 (AMC3301). ref_v is the external differential
    # circuit's reference, a LIVE measurement (ct.read_ads_all()['ref_mv']
    # /1000) -- NOT a hardcoded constant.
    return 2 * (raw * 3.3 / 4095 - 0.5 * ref_v) / 10 / 8.2 * 1000


def integral_ma_us(integral_counts):
    # `integral` in the event is Sigma(sample - bg_mean) over the pulse, in
    # RAW ADC COUNTS already differenced from background on-chip -- so only
    # the SLOPE of emission_ma() applies (the offset term cancels in a
    # difference, same as EMI_MA_PER_COUNT in power.js/tests.js). Multiplying
    # by US_PER_SAMPLE turns a sum-of-counts into a charge-like mA*us
    # quantity (average current x duration), the background-subtracted
    # pulse integral the user asked for.
    ma_per_count = 2 * (3.3 / 4095) / 10 / 8.2 * 1000
    return integral_counts * ma_per_count * US_PER_SAMPLE


def ser_cmd(ser, c, wait=0.5):
    ser.write((c + "\n").encode())
    time.sleep(wait)
    out = b""
    t0 = time.time()
    while time.time() - t0 < wait:
        chunk = ser.read(4096)
        if chunk:
            out += chunk
            t0 = time.time()
        else:
            time.sleep(0.02)
    return out.decode(errors='replace')


def configure_known_timing(ser):
    cmd = (f"stm32 pulse cfg {PD_BG_WINDOW} {PD_MIN_DUR} {PD_K_SIGMA} "
          f"{PD_GAP} {PD_LOCAL} {PD_RATIO} "
          f"{KNOWN_WIDTH_SAMPLES} {PLATEAU_MARGIN_SAMPLES} "
          f"{POST_BG_GAP_SAMPLES} {POST_BG_N_SAMPLES}")
    out = ser_cmd(ser, cmd, wait=0.5)
    ok = "OK" in out
    print(f"known-timing config {'OK' if ok else 'FAILED'}: "
          f"known_width={KNOWN_WIDTH_SAMPLES} margin={PLATEAU_MARGIN_SAMPLES} "
          f"post_bg_gap={POST_BG_GAP_SAMPLES} post_bg_n={POST_BG_N_SAMPLES} samples")
    if not ok:
        print(out)
    return ok


def fire_once(ct, ser, controller, plan):
    """Arm ready_relay (real trigger relay), fire one pulse, wait for the
    STM32's pulse_detector to emit its on-chip summary. Returns
    {'event': {...}} on success or {'error': ...}."""
    last_id = ct._get('/api/pulse-events').get('last_id', 0)

    ser_cmd(ser, "ready disarm", wait=0.3)
    ser_cmd(ser, f"ready arm {RATE_HZ} {N_SAMPLES} 80", wait=0.3)

    ct.shv_disarm(controller)
    dl = ct.download(plan)
    v = ct.verify_schedule(plan)
    if not (dl.get("ok") and v.get("ok")):
        return {"error": f"download/verify failed: dl={dl} v={v}"}
    ct.hv_grid_set(FILAMENT, on=True)
    arm_r = ct.shv_arm(controller, repeats=1)
    if not arm_r.get("ok"):
        ct.hv_grid_set(FILAMENT, on=False)
        return {"error": f"arm rejected {arm_r}"}
    ct._post("/api/sync/simulate", {"count": 1, "interval_ms": 3000,
                                    "controller": int(controller)}, timeout=10.0)
    deadline = time.monotonic() + 10
    state = None
    while time.monotonic() < deadline:
        st = ct.shv_status(controller)
        state = st.get("state")
        if state in (3, 4):
            break
        time.sleep(0.05)
    ct.hv_grid_set(FILAMENT, on=False)
    ct.shv_disarm(controller)

    if state != 3:
        return {"error": f"pulse did not complete (state={state})"}

    # The event ships as a single small UART frame the instant the
    # detector's known window closes -- should already be sitting in the
    # ring by the time shv_status reported COMPLETE, but poll briefly.
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        j = ct._get(f'/api/pulse-events?since={last_id}')
        events = j.get('events') or []
        if events:
            return {"event": events[-1]}
        time.sleep(0.05)
    return {"error": "no EVT_PULSE arrived (detector didn't see a rise)"}


def is_valid_event(bg_before, peak, plateau, width_us):
    if peak is None or plateau is None or width_us is None:
        return False
    if (plateau - bg_before) < 10:   # raw-count units; ~0.06 mA/count
        return False
    if not (0.3 * WIDTH_US <= width_us <= 3 * WIDTH_US):
        return False
    return True


def main():
    ct = CTClient(client_id="pulse_sweep")
    print(ct.describe(ct.standby_one(FILAMENT)))
    controller = ct.filament_to_board(FILAMENT)["controller"]

    ser = serial.Serial(SER_PORT, 115200, timeout=1)
    time.sleep(0.3)
    ser.reset_input_buffer()

    if not configure_known_timing(ser):
        print("aborting: known-timing config rejected")
        ser.close()
        return

    plan = {
        "config": {"interPulseMs": 3000, "maxOnMs": 40, "totalMs": 15000, "triggerEdge": 0},
        "emission": [{"filament": ct._phys(FILAMENT), "numPulses": 1, "widthUs": WIDTH_US}],
        "heating": [],
    }

    # Warm-up: whatever a PRIOR script/session left the wiper at, HV rails
    # with no active discharge path slew DOWN slowly (just leak off) -- a
    # short settle after a descending set_emission_v() badly undershoots.
    ct.set_emission_i(85.7)
    ct.set_emission_v(VOLTAGES[0])
    time.sleep(2.0)

    rows = []
    for volts in VOLTAGES:
        ct.set_emission_i(85.7)
        ct.set_emission_v(volts)
        time.sleep(0.8)
        v_actual = ct.read_emission_v()
        ads = ct.read_ads_all()
        ref_v = (ads.get('ref_mv') / 1000) if ads.get('ok') and ads.get('ref_mv') is not None else 1.227

        row = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = fire_once(ct, ser, controller, plan)
            if "error" in result:
                print(f"V={volts} attempt {attempt}/{MAX_ATTEMPTS}: {result['error']}")
                continue
            e = result["event"]
            bg_before = e["bg"]
            peak = e["peak"]
            plateau = e["plateau"] if e.get("plateau") else peak
            post_bg = e.get("post_bg")
            width_us = e["on_us"] * US_PER_SAMPLE
            if not is_valid_event(bg_before, peak, plateau, width_us):
                print(f"V={volts} attempt {attempt}/{MAX_ATTEMPTS}: bad event "
                      f"(bg={bg_before} peak={peak} plateau={plateau} width_us={width_us:.0f}) -- retrying")
                continue
            row = {
                "V": volts, "v_actual": v_actual, "ref_v": ref_v, "attempts": attempt,
                "width_us": width_us,
                "bg_before_mA": emission_ma(bg_before, ref_v),
                "bg_after_mA": emission_ma(post_bg, ref_v) if post_bg else None,
                "peak_mA": emission_ma(peak, ref_v),
                "plateau_mA": emission_ma(plateau, ref_v),
                "integral_mA_us": integral_ma_us(e["integral"]),
            }
            break
        if row is None:
            print(f"V={volts}: all {MAX_ATTEMPTS} attempts failed, skipping")
            continue

        rows.append(row)
        va_str = f"{v_actual:.1f}" if v_actual is not None else "n/a"
        ba_str = f"{row['bg_after_mA']:.2f}" if row['bg_after_mA'] is not None else "n/a"
        print(f"V_cmd={volts:4.0f} V_actual={va_str:>6} (attempt {row['attempts']}/{MAX_ATTEMPTS})  "
              f"bg_before={row['bg_before_mA']:6.2f}mA  bg_after={ba_str:>6}mA  "
              f"plateau={row['plateau_mA']:6.2f}mA  peak={row['peak_mA']:6.2f}mA  "
              f"width={row['width_us']:.0f}us (cmd {WIDTH_US}us)  "
              f"integral={row['integral_mA_us']:.0f}mA*us")

    ser_cmd(ser, "ready disarm", wait=0.3)
    ser.close()

    print("\n--- summary table ---")
    print(f"{'V_cmd':>6} {'V_actual':>9} {'bg_before':>10} {'bg_after':>10} {'plateau':>8} {'peak':>8} "
          f"{'width_us':>9} {'integral':>10} {'attempts':>9}")
    for r in rows:
        va = f"{r['v_actual']:.1f}" if r['v_actual'] is not None else "n/a"
        ba = f"{r['bg_after_mA']:.2f}" if r['bg_after_mA'] is not None else "n/a"
        print(f"{r['V']:6.0f} {va:>9} {r['bg_before_mA']:10.2f} {ba:>10} "
              f"{r['plateau_mA']:8.2f} {r['peak_mA']:8.2f} {r['width_us']:9.0f} "
              f"{r['integral_mA_us']:10.0f} {r['attempts']:9d}")


if __name__ == "__main__":
    main()
