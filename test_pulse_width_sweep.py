#!/usr/bin/env python3
"""Pulse-width sweep at a fixed voltage, validating that known-timing mode
(plateau/post_bg/width via pulse_detector's on-chip fixed-window
accumulation) holds up across different commanded widths, not just the
2000us used in the voltage sweep.
"""
import sys, time

sys.path.insert(0, '.')
from ct_simple_control import CTClient
import serial

SER_PORT = '/dev/cu.usbmodem214201'
FILAMENT = 7
RATE_HZ = 500_000
VOLTS = 40                 # fixed, mid-range reliable point from the voltage sweep
WIDTHS_US = [200, 500, 1000, 2000, 4000]
MAX_ATTEMPTS = 4
US_PER_SAMPLE = 1e6 / RATE_HZ
POST_BG_GAP_SAMPLES = round(200 * RATE_HZ / 1e6)   # 200us settle, fixed across widths
POST_BG_N_SAMPLES = round(200 * RATE_HZ / 1e6)     # 200us average
PD_BG_WINDOW, PD_MIN_DUR, PD_K_SIGMA, PD_GAP, PD_LOCAL, PD_RATIO = 20, 64, 3, 0, 0, 0


def emission_ma(raw, ref_v):
    return 2 * (raw * 3.3 / 4095 - 0.5 * ref_v) / 10 / 8.2 * 1000


def integral_ma_us(integral_counts):
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


def configure_known_timing(ser, known_width_samples, margin_samples):
    cmd = (f"stm32 pulse cfg {PD_BG_WINDOW} {PD_MIN_DUR} {PD_K_SIGMA} "
          f"{PD_GAP} {PD_LOCAL} {PD_RATIO} "
          f"{known_width_samples} {margin_samples} "
          f"{POST_BG_GAP_SAMPLES} {POST_BG_N_SAMPLES}")
    out = ser_cmd(ser, cmd, wait=0.5)
    return "OK" in out


def fire_once(ct, ser, controller, plan, n_samples):
    last_id = ct._get('/api/pulse-events').get('last_id', 0)
    ser_cmd(ser, "ready disarm", wait=0.3)
    ser_cmd(ser, f"ready arm {RATE_HZ} {n_samples} 80", wait=0.3)

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

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        j = ct._get(f'/api/pulse-events?since={last_id}')
        events = j.get('events') or []
        if events:
            return {"event": events[-1]}
        time.sleep(0.05)
    return {"error": "no EVT_PULSE arrived (detector didn't see a rise)"}


def is_valid_event(bg_before, peak, plateau, width_us, width_us_cmd):
    if peak is None or plateau is None or width_us is None:
        return False
    if (plateau - bg_before) < 10:
        return False
    if not (0.3 * width_us_cmd <= width_us <= 3 * width_us_cmd):
        return False
    return True


def main():
    ct = CTClient(client_id="pulse_width_sweep")
    print(ct.describe(ct.standby_one(FILAMENT)))
    controller = ct.filament_to_board(FILAMENT)["controller"]

    ser = serial.Serial(SER_PORT, 115200, timeout=1)
    time.sleep(0.3)
    ser.reset_input_buffer()

    ct.set_emission_i(85.7)
    ct.set_emission_v(VOLTS)
    time.sleep(2.0)
    v_actual = ct.read_emission_v()
    print(f"V_actual={v_actual}")

    rows = []
    for width_us in WIDTHS_US:
        known_width_samples = round(width_us * RATE_HZ / 1e6)
        margin_samples = max(1, round(known_width_samples * 0.15))
        n_samples = min(4000, known_width_samples + POST_BG_GAP_SAMPLES + POST_BG_N_SAMPLES + 500)

        if not configure_known_timing(ser, known_width_samples, margin_samples):
            print(f"width={width_us}us: pulse cfg FAILED, skipping")
            continue

        plan = {
            "config": {"interPulseMs": 3000, "maxOnMs": 40, "totalMs": 15000, "triggerEdge": 0},
            "emission": [{"filament": ct._phys(FILAMENT), "numPulses": 1, "widthUs": width_us}],
            "heating": [],
        }

        ads = ct.read_ads_all()
        ref_v = (ads.get('ref_mv') / 1000) if ads.get('ok') and ads.get('ref_mv') is not None else 1.227

        row = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            result = fire_once(ct, ser, controller, plan, n_samples)
            if "error" in result:
                print(f"width={width_us}us attempt {attempt}/{MAX_ATTEMPTS}: {result['error']}")
                continue
            e = result["event"]
            bg_before = e["bg"]
            peak = e["peak"]
            plateau = e["plateau"] if e.get("plateau") else peak
            post_bg = e.get("post_bg")
            meas_width_us = e["on_us"] * US_PER_SAMPLE
            if not is_valid_event(bg_before, peak, plateau, meas_width_us, width_us):
                print(f"width={width_us}us attempt {attempt}/{MAX_ATTEMPTS}: bad event "
                      f"(bg={bg_before} peak={peak} plateau={plateau} "
                      f"meas_width={meas_width_us:.0f}us) -- retrying")
                continue
            row = {
                "width_cmd_us": width_us, "attempts": attempt,
                "meas_width_us": meas_width_us,
                "bg_before_mA": emission_ma(bg_before, ref_v),
                "bg_after_mA": emission_ma(post_bg, ref_v) if post_bg else None,
                "peak_mA": emission_ma(peak, ref_v),
                "plateau_mA": emission_ma(plateau, ref_v),
                "integral_mA_us": integral_ma_us(e["integral"]),
            }
            break
        if row is None:
            print(f"width={width_us}us: all {MAX_ATTEMPTS} attempts failed, skipping")
            continue

        rows.append(row)
        ba_str = f"{row['bg_after_mA']:.2f}" if row['bg_after_mA'] is not None else "n/a"
        print(f"width_cmd={width_us:5d}us  meas_width={row['meas_width_us']:6.0f}us  "
              f"(attempt {row['attempts']}/{MAX_ATTEMPTS})  "
              f"bg_before={row['bg_before_mA']:6.2f}mA  bg_after={ba_str:>6}mA  "
              f"plateau={row['plateau_mA']:6.2f}mA  peak={row['peak_mA']:6.2f}mA  "
              f"integral={row['integral_mA_us']:.0f}mA*us")

    ser_cmd(ser, "ready disarm", wait=0.3)
    ser.close()

    print("\n--- summary table ---")
    print(f"{'width_cmd':>10} {'meas_width':>11} {'bg_before':>10} {'bg_after':>10} "
          f"{'plateau':>8} {'peak':>8} {'integral':>10} {'attempts':>9}")
    for r in rows:
        ba = f"{r['bg_after_mA']:.2f}" if r['bg_after_mA'] is not None else "n/a"
        print(f"{r['width_cmd_us']:10d} {r['meas_width_us']:11.0f} {r['bg_before_mA']:10.2f} {ba:>10} "
              f"{r['plateau_mA']:8.2f} {r['peak_mA']:8.2f} {r['integral_mA_us']:10.0f} {r['attempts']:9d}")


if __name__ == "__main__":
    main()
