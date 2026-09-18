#!/usr/bin/env python3
"""Multi-pulse train with a real 1ms GAP (off-time) between pulses,
validating that consecutive pulses' on-chip summaries (bg_before/plateau/
post_bg) stay correctly isolated per-pulse -- no bleed from one pulse's
post_bg window into the next pulse's rise, and vice versa. This is the
scenario the known-timing post_bg design was specifically built for (a
separate later UART round trip can't fit inside a 1ms gap; the on-chip
fixed-window accumulation ships everything in one small event per pulse).

GAP vs PERIOD, and why this matters: "pulse spacing is at least 1ms" means
the GAP (pulse end -> next pulse start), not the trigger PERIOD (pulse
start -> next pulse start). An earlier version of this script set the
SyncOut trigger rate directly from a 1ms PERIOD while ALSO using a 1ms
pulse width -- i.e. GAP_US = 0, the exact degenerate case the RP2350's PIO
latch SM can't recover from (per the RP2350 session: minimum trigger
period is empirically pulse_width + ~10us; period==width leaves the SM no
time to return to its trigger-wait state, so pulses run together / edges
get missed). PERIOD_US = WIDTH_US + GAP_US here, computed explicitly, is
what actually drives sync_io::startBurst()'s rate_hz.

ready_relay's capture window must span the WHOLE train (all N pulses'
rises + the last one's post_bg tail), since the CS-relay only fires once
per arm() -- the STM32 stays streaming continuously and the
pulse_detector re-triggers per pulse within that one capture.

SAFETY -- real incident this script caused, now fixed: this script never
called ct.enable_emission(False) (the STM32-side emission HV master
enable, /api/stm32/hv-enable -- a COMPLETELY SEPARATE control from the
RP2350's ISO/grid-routing switch that hv_grid_set toggles, and separate
again from the RP2350 PIO's per-channel 595/MOSFET pulse-drive bit). Every
earlier run of this script left that STM32-side emission enable ON for the
rest of the process's life, independent of anything the RP2350 side
reported (disarm/clearAll, hv_grid_set(off) with feedback=False, the PIO's
595 readback = 0 -- all genuinely correct and clean, and all irrelevant to
this specific enable line). This is almost certainly what showed up as a
real, physically-confirmed multi-second-plus HV-on condition (visible on
an LED and on a scope) that no RP2350-side fix ever touched. Wrapping the
whole body in ct.session() guarantees enable_emission(False) (and
enable_focus/hv_grid_clear_all/stop_all) run on the way out, even on an
exception -- see ct_simple_control.py's session() docstring.
"""
import _path  # noqa: F401  — makes ct_simple_control importable from tests/
import sys, time
import requests

sys.path.insert(0, '.')
from ct_simple_control import CTClient
import serial

import os as _os

SER_PORT = '/dev/cu.usbmodem214201'
ESP32_HOST = '192.168.50.173'  # this controller's STA IP -- `status` over serial
# FILAMENT=7 is ch1.8 (the ~10kOhm bench resistor); all other channels on
# this controller are ~100kOhm -- override via env to test a different one
# without editing this file (e.g. FILAMENT=0 KOHM=100 for ch1.1).
FILAMENT = int(_os.environ.get("FILAMENT", "7"))
R_CH_KOHM = float(_os.environ.get("KOHM", "10"))
RATE_HZ = int(_os.environ.get("RATE_HZ", "1000000"))   # 1 MHz confirmed
                                # stable (clean edge_rises/edge_falls/commits
                                # every run). 1.5 MHz and 2 MHz (the ADC's own
                                # ~2.2 Msps hardware ceiling) both crashed the
                                # STM32 mid-run (SAFETY_TRIP_BOOT) -- likely
                                # the detector-continuous path's 128-sample
                                # DMA half-buffer (hs_adc.c's
                                # HSADC_DETECTOR_HALF) can't keep up with the
                                # ISR rate at higher sample rates; needs
                                # firmware work (bigger batch or leaner
                                # per-sample cost) before going past 1 MHz.
VOLTS = float(_os.environ.get("VOLTS", "40"))
WIDTH_US = 1000                 # 1ms pulse width, per direct instruction
GAP_US = 1000                   # 1ms GAP (off-time) between pulses -- the
                                # real production constraint ("interval
                                # between pulses is at least 1ms")
PERIOD_US = WIDTH_US + GAP_US   # what actually drives the SyncOut trigger
                                # rate; RP2350's real minimum is width+~10us,
                                # so this has ample margin
NUM_PULSES = 4
US_PER_SAMPLE = 1e6 / RATE_HZ
POST_BG_GAP_SAMPLES = round(150 * RATE_HZ / 1e6)   # 150us settle -- tighter
POST_BG_N_SAMPLES = round(150 * RATE_HZ / 1e6)     # than the other tests since
                                                    # the whole gap is only ~700us
# k_sigma=3 on CONTINUOUS Gaussian background noise statistically guarantees
# frequent false triggers (a ~3-sigma one-tailed crossing has ~0.135%
# probability per sample -- at 500 kHz that's ~675/s BY THE MATH ALONE, no
# implementation bug needed). Confirmed directly: a real run measured
# rises=117720 over ~166s (~709/s, matching the prediction), and that ISR
# load caused a genuine ADC/DMA overrun that killed continuous streaming.
# Since known-PERIOD mode never uses IDLE's amplitude path for a real,
# trigger-anchored train (pulse_detector_mark_trigger() schedules the whole
# chain directly, see pulse_detector.h), raising k_sigma to the firmware's
# max (10, clamped in pulse_detector_configure) has zero downside for real
# pulses (peak was ~400+ sigma above background) and just silences noise.
PD_BG_WINDOW, PD_MIN_DUR, PD_K_SIGMA, PD_GAP, PD_LOCAL, PD_RATIO = 20, 32, 10, 0, 0, 0


def emission_ma(raw, ref_v):
    return 2 * (raw * 3.3 / 4095 - 0.5 * ref_v) / 10 / 8.2 * 1000


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


def configure_edge_triggered(ser, margin_samples):
    # EDGE-TRIGGERED mode: ready_relay now LIVE-mirrors every real GPIO39
    # edge onto GPIO34/PA4, so the STM32 measures each real pulse off real,
    # live rise/fall edges (pulse_detector_edge_rise/fall()) -- no amplitude
    # threshold, no guessed period, no known width. margin_samples skips
    # this many samples right after the real rise before averaging the
    # plateau (settle margin for inrush/ringing).
    # disable_amp_fallback=1: MUST be set here -- ready_relay is about to be
    # armed, and PD_STATE_IDLE's amplitude/variance check would otherwise
    # race pulse_detector_edge_rise() for the same real pulse (confirmed: a
    # real pulse's amplitude is way above any reasonable k_sigma threshold,
    # so both paths fire on it, and outcomes vary run-to-run depending on
    # which one wins -- edge_rises came up short of edge_falls, post_bg
    # sometimes never got measured).
    cmd = (f"stm32 pulse cfg {PD_BG_WINDOW} {PD_MIN_DUR} {PD_K_SIGMA} "
          f"{PD_GAP} {PD_LOCAL} {PD_RATIO} "
          f"{margin_samples} {POST_BG_GAP_SAMPLES} {POST_BG_N_SAMPLES} 1")
    out = ser_cmd(ser, cmd, wait=0.5)
    print(out.strip())
    return "OK" in out


def main():
    ct = CTClient(client_id="pulse_train")
    # NOT using ct.session() -- its teardown unconditionally disables
    # emission on exit, which is exactly the toggle that corrupts V_actual
    # (see the comment further down). Manually replicating session()'s
    # teardown MINUS that one call: still guarantees focus disabled, HV grid
    # cleared, and every filament stopped on the way out (including on an
    # exception), just leaves emission enable alone so repeated runs of this
    # script keep HV stable instead of re-toggling it every time.
    try:
        print(ct.describe(ct.standby_one(FILAMENT)))
        controller = ct.filament_to_board(FILAMENT)["controller"]

        # STM32 is optional -- if it's not plugged in (e.g. testing the
        # RP2350/HV side alone against a scope), skip everything that needs
        # it rather than crashing before ever firing a pulse.
        ser = None
        try:
            ser = serial.Serial(SER_PORT, 115200, timeout=1)
            time.sleep(0.3)
            ser.reset_input_buffer()
        except serial.SerialException as exc:
            print(f"STM32 serial not available ({exc}) -- skipping known-timing "
                  f"config and ready_relay; firing the RP2350/HV train only.")

        if ser is not None:
            known_width_samples = round(WIDTH_US * RATE_HZ / 1e6)
            margin_samples = max(1, round(known_width_samples * 0.15))
            # EDGE-TRIGGERED mode: ready_relay now live-mirrors every real
            # GPIO39 edge onto PA4, so the STM32 measures each real pulse
            # off real rise/fall edges directly -- no assumed width/period/
            # delay to configure at all.
            if not configure_edge_triggered(ser, margin_samples):
                print("aborting: pulse detector config rejected")
                ser.close()
                return

        # Don't toggle enable_emission unless it's actually off -- confirmed
        # (via a direct hv_status()/read_emission_v() check with NOTHING
        # toggled) that emission genuinely holds a rock-stable -40V when
        # simply left alone. Toggling it off then on -- which every earlier
        # version of this script did on every single run, since disable/
        # enable both happened inside main() -- instead sent it to a
        # spike-then-decay-to-~-2.4V state that never recovered in 16s of
        # polling. Root cause not fully pinned (safety_emission_set()'s
        # disable/enable is a hardware discharge/release gate on the STM32,
        # separate from the DS3502 wiper that actually sets the level -- why
        # re-enabling doesn't restore the wiper's commanded voltage is still
        # open), but the practical fix doesn't need the root cause: leave it
        # alone when it's already correctly configured, same as how the GUI
        # is normally used.
        hv_st = ct.hv_status()
        v_actual = ct.read_emission_v() if hv_st.get("ok") else None
        if not (hv_st.get("ok") and hv_st.get("emission_on") and v_actual is not None
               and abs(abs(v_actual) - VOLTS) <= 2.0):
            print(f"emission not already at target (status={hv_st}, V_actual={v_actual}) "
                 f"-- configuring from scratch")
            ct.set_emission_i(85.7)
            ct.set_emission_v(VOLTS)
            ct.enable_emission(True)
            # Poll for real convergence instead of a fixed sleep -- observed
            # settle time is inconsistent (sometimes ~1s, sometimes it never
            # recovers within 16s after a toggle right after firing pulses).
            # Never blindly proceed to fire on an unverified voltage.
            v_deadline = time.monotonic() + 15.0
            while time.monotonic() < v_deadline:
                v_actual = ct.read_emission_v()
                if v_actual is not None and abs(abs(v_actual) - VOLTS) <= 2.0:
                    break
                time.sleep(0.5)
            else:
                print(f"*** V_actual did NOT converge to {VOLTS}V within 15s "
                     f"(last read: {v_actual}) -- aborting, do not fire on bad HV ***")
                return
        print(f"V_actual={v_actual}")

        # Capture window must span the WHOLE train: (NUM_PULSES-1) trigger
        # periods + the last pulse's known_width + post_bg tail, plus margin.
        train_span_us = (NUM_PULSES - 1) * PERIOD_US + WIDTH_US + \
                       (POST_BG_GAP_SAMPLES + POST_BG_N_SAMPLES) * US_PER_SAMPLE
        n_samples = min(4096, round(train_span_us / US_PER_SAMPLE) + 500)
        print(f"train_span={train_span_us:.0f}us -> n_samples={n_samples}")

        ads = ct.read_ads_all()
        ref_v = (ads.get('ref_mv') / 1000) if ads.get('ok') and ads.get('ref_mv') is not None else 1.227

        # Precise multi-pulse trigger source: sync_io::startBurst()
        # (ESP32-side, sync_io.h/.cpp), a HARDWARE esp_timer that emits
        # `count` SyncOut edges at `rate_hz` and stops itself. Each edge
        # triggers exactly ONE pulse via the normal "scan schedule"
        # semantics (numPulses=1/entry, repeats=NUM_PULSES loops that one
        # entry once per edge).
        ct._shv(controller, {"op": "disarm"})   # clean state before reconfiguring
        # set_config's interPulseMs is the schedule's OWN watchdog: how long
        # it waits for the next trigger before declaring "inter-pulse
        # timeout" -- a completely different thing from the trigger PERIOD
        # (which is entirely up to whatever generates the SyncIn edges, here
        # sync_io::startBurst() via `rate_hz` below). Set generously here so
        # this watchdog itself can't be the thing that faults the run.
        plan_cfg = ct._shv(controller, {"op": "set_config", "interPulseMs": 3000,
                                        "maxOnMs": 40, "totalMs": 15000})
        print("set_config:", plan_cfg)
        ct._shv(controller, {"op": "clear_table"})
        dl = ct._shv(controller, {"op": "set_entries",
                                  "entries": [{"filament": ct._phys(FILAMENT), "numPulses": 1,
                                              "width": WIDTH_US}]})
        print("set_entries:", dl)

        last_id = ct._get('/api/pulse-events').get('last_id', 0)
        if ser is not None:
            print("ready disarm:", ser_cmd(ser, "ready disarm", wait=0.3).strip())
            print("ready arm:", ser_cmd(ser, f"ready arm {RATE_HZ} {n_samples}", wait=0.3).strip())

        # NOT calling hv_grid_set(on=True) here -- confirmed unnecessary
        # (arm() succeeds fine without it, filament 7 already in STANDBY
        # with ISO on from standby_one() above) AND actively harmful: it's
        # a BIT-BANG write (hvSetChannelByte) to the exact same physical
        # 595 output bit (channel 0, bit 7) the PIO addresses for this same
        # filament's pulse. Calling it here pre-asserts that bit HIGH well
        # before the real PIO-timed trigger, and nothing clears it until
        # the PIO's own hardware OFF sequence at the END of the real pulse
        # -- so the scope sees ONE continuous high span from this call all
        # the way to the true pulse's end, which reads as "pulse #1 is
        # anomalously long" even though the PIO's own per-pulse timing was
        # correct the whole time. This is the user's own diagnosis,
        # confirmed by testing arm() with this call removed entirely.
        arm_r = ct._shv(controller, {"op": "arm", "repeats": NUM_PULSES})
        print("arm:", arm_r)
        t_fire = time.monotonic()
        burst = requests.post(f"http://{ESP32_HOST}/sync/burst",
                              params={"count": NUM_PULSES, "rate_hz": round(1e6 / PERIOD_US)},
                              timeout=5.0).json()
        print(f"burst started (+{(time.monotonic()-t_fire)*1000:.1f}ms):", burst)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            bs = requests.get(f"http://{ESP32_HOST}/sync/burst/status", timeout=2.0).json()
            if not bs.get("running"):
                break
            time.sleep(0.05)
        print(f"burst status (+{(time.monotonic()-t_fire)*1000:.1f}ms):", bs)
        # /sync/burst/status "running=False" only means the ESP32 has SENT all
        # NUM_PULSES trigger edges -- it says nothing about whether the
        # RP2350 has finished PROCESSING the last one (firing the pulse,
        # verifying the 165 readback, advancing its own state machine).
        # disarm()-ing the instant the ESP32 side looks done is a real race:
        # confirmed once as `stopReason=Disarmed, totalPulsesDone=3/4` --
        # this script's own disarm cut pulse #4 off mid-flight. Wait for the
        # RP2350's OWN status to report done before touching it.
        shv_deadline = time.monotonic() + 2.0
        final_st = None
        while time.monotonic() < shv_deadline:
            final_st = ct._shv(controller, {"op": "status"})
            fs = final_st.get("status", {})
            if fs.get("state") in (3, 4) or fs.get("totalPulsesDone", 0) >= NUM_PULSES:
                break
            time.sleep(0.02)
        print(f"RP2350 schedule done (+{(time.monotonic()-t_fire)*1000:.1f}ms):", final_st)
        # disarm() -> clearAll() already zeroes this bit along with every
        # other channel's output (the /SRCLR broadcast clear). This
        # hv_grid_set(off) is a redundant, explicit belt-and-suspenders
        # check -- not undoing an on=True call (there isn't one anymore,
        # see above) -- kept only because its return value confirms the
        # real hardware feedback bit reads back as off.
        ct._shv(controller, {"op": "disarm"})
        off_r = ct.hv_grid_set(FILAMENT, on=False)
        print(f"HV grid off (+{(time.monotonic()-t_fire)*1000:.1f}ms since first SyncOut edge): {off_r}")
        if off_r.get("failed"):
            print(f"*** GRID OFF FAILED for {off_r['failed']} -- HV may still be live ***")
        # Deliberately NOT calling enable_emission(False) here -- confirmed
        # toggling it off/on is what was corrupting V_actual (see the
        # comment above the emission setup earlier in this function).
        # ct.session()'s teardown still disables it once, safely, when the
        # whole script actually exits -- that's the right place for it, not
        # after every single train.
        hv_st = ct.hv_status()
        print(f"hv_status: {hv_st}")
        gs = ct.hv_grid_status(controller)
        print(f"hv_grid_status after off: {gs.get('filaments', {}).get(str(FILAMENT))}")

        st = ct._post('/api/shv', {"controller": controller, "op": "status"})
        print(f"shv status (+{(time.monotonic()-t_fire)*1000:.1f}ms, HV already off):", st)
        if ser is not None:
            print("ready status:", ser_cmd(ser, "ready status", wait=0.3).strip())

        # Collect every EVT_PULSE that arrived during this one train.
        time.sleep(0.3)
        j = ct._get(f'/api/pulse-events?since={last_id}')
        events = j.get('events') or []
        if ser is not None:
            ser_cmd(ser, "ready disarm", wait=0.3)
            ser.close()

        # Ground-truth check, per direct instruction: CH1.8 (FILAMENT=7) is
        # wired to a bench resistor ~10 kOhm; other channels are ~100k
        # (override via FILAMENT/KOHM env vars). Expected pulse current is
        # just Ohm's law -- I = V/R. Compare against the REAL, existing
        # emission_ma() calibration (count -> mA), not a backwards-derived
        # slope: delta_mA = plateau_mA - bg_mA is the actual measured pulse
        # current (bg_mA subtracted out as the channel's own zero-offset),
        # directly comparable to expected_ma.
        R_OHM = R_CH_KOHM * 1000.0
        expected_ma = (abs(v_actual) / R_OHM * 1000.0) if v_actual else None

        print(f"\ngot {len(events)} events (expected {NUM_PULSES})")
        if expected_ma is not None:
            print(f"expected I = V/R = {abs(v_actual):.1f}V / {R_CH_KOHM:.0f}kOhm "
                  f"= {expected_ma:.3f} mA  (ground truth, filament {FILAMENT})")
        print(f"{'#':>3} {'t_us':>8} {'dt_prev_us':>11} {'bg_raw':>6} {'plateau_raw':>11} "
              f"{'bg_mA':>7} {'plateau_mA':>11} {'delta_mA':>9} {'peak_mA':>8} "
              f"{'post_bg_mA':>11} {'on_us':>6}")
        prev_t = None
        for i, e in enumerate(events):
            t_us = e['t_us'] * US_PER_SAMPLE
            dt = (t_us - prev_t) if prev_t is not None else None
            prev_t = t_us
            bg_raw = e.get('bg')
            plateau_raw = e.get('plateau')
            bg_ma = emission_ma(e['bg'], ref_v)
            plateau_ma = emission_ma(e['plateau'], ref_v) if e.get('plateau') else None
            peak_ma = emission_ma(e['peak'], ref_v)
            post_bg_ma = emission_ma(e['post_bg'], ref_v) if e.get('post_bg') else None
            delta_ma = (plateau_ma - bg_ma) if plateau_ma is not None else None
            dt_str = f"{dt:.0f}" if dt is not None else "  --"
            pl_str = f"{plateau_ma:.2f}" if plateau_ma is not None else "n/a"
            pb_str = f"{post_bg_ma:.2f}" if post_bg_ma is not None else "n/a"
            dm_str = f"{delta_ma:.3f}" if delta_ma is not None else "n/a"
            print(f"{i:3d} {t_us:8.0f} {dt_str:>11} {bg_raw:6d} {plateau_raw:11d} "
                  f"{bg_ma:7.2f} {pl_str:>11} {dm_str:>9} "
                  f"{peak_ma:8.2f} {pb_str:>11} {e['on_us']:6d}")
    finally:
        # session()'s teardown minus enable_emission(False) -- see the
        # comment at the top of main() for why that one call is deliberately
        # skipped here. Each step independent so one failing doesn't block
        # the rest, matching session()'s own pattern.
        for fn in (lambda: ct.enable_focus(False),
                  lambda: ct.hv_grid_clear_all(),
                  lambda: ct.stop_all()):
            try:
                fn()
            except Exception:
                pass


if __name__ == "__main__":
    main()
