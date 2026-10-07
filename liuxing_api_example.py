"""liuxing_api.py with the new shot methods: ACTIVE only around the pulse.

Same settings and output as liuxing_api.py. What changes is WHEN a filament is
at ACTIVE. The old loop raised ACTIVE first, then did the whole setup at
firing current (detector arm, schedule check/download, arm, status read) and
read the results before going back to IDLE: 5-9 s at ACTIVE for a 7 ms pulse.
Here every setup step runs at IDLE, ACTIVE comes right before the trigger,
and IDLE comes back as soon as the pulse is done: ACTIVE = the heat-up
(~2-3 s) + the pulse.

Two ways to do it -- pick one with MODE:

  MODE = "single"   fire_single_pulse(..., active_ma=, idle_ma=)
                    one call does it all, in the order above.

  MODE = "steps"    the same steps called one by one (shot_prepare, shot_arm,
                    shot_trigger, shot_wait, shot_records, ...), so you can
                    put the heating -- or anything else -- where you want.

Both print the same per-pulse line as liuxing_api.py, plus how long the
filament spent at ACTIVE.

Where the code is:
  ct/client/_schedule.py   the shot_* methods (shot_prepare, shot_measure_arm,
                           shot_arm, shot_trigger, shot_wait, shot_records,
                           shot_measured, shot_abort) and fire_single_pulse,
                           which is just those steps in order -- each has a
                           docstring (help(ct.shot_prepare) lists them all).
  examples/fire_steps.py   the same steps as MODE = "steps", as a stand-alone
                           script with command-line options (-f 16 55 70); it
                           does not touch HV.
"""
import time

from ct_simple_control import CTClient, CTError  # noqa: F401

MODE = "single"          # "single" or "steps"

if_idle_all = True
iteration_num = 2
idle_cuurrent_ma = 1300
active_current_ma = 2700
set_width_us = 7000
trigger_delay_us = 3000
active_stable_wait_s = 1.0
trigger_type = "sim"     # "sim" = ESP32 SyncIn; "ext" = external edge
heat_timeout_s = 5.0     # longest wait for the ACTIVE current to arrive

dead_filaments = [6, 9, 26, 50, 73]
filament_list_mask = [16, 55, 70, 72, 78]


def on_armed():
    print(f"[{time.strftime('%H:%M:%S')}] READY -- send the external trigger now")


def print_ads(ct):
    ads = ct.read_ads_all()
    if ads.get("ok"):
        print(f"ADS1115: emiss_v={ads.get('emiss_v')} V, emiss_i_ma={ads.get('emiss_i_ma')} mA, "
              f"focus_v={ads.get('focus_v')} V, ref_mv={ads.get('ref_mv')} mV")
    else:
        print(f"ADS1115 read failed: {ads.get('error', 'unknown error')}")


def print_result(f, result):
    """The same per-pulse line liuxing_api.py prints, plus the ACTIVE time."""
    if not result.get("ok"):
        print(f"fire FAILED (filament {f}, fired={result.get('fired')}): "
              f"{result.get('error') or 'no reason given'}")
        return
    events = result.get("measured") or []
    records = result.get("records") or []
    if not events:
        print(f"fire ok but NO measured event came back (filament {f})")
        return
    e = events[0]
    paired = bool(records) and len(records) == len(events)
    rec = records[0] if paired else {}
    print(f"filament {f} pulse:"
          f" active_s={result.get('active_s')}"
          f" on_us={e.get('on_us')}"
          f" heat_meas_mA={rec.get('heat_meas_mA')}"
          f" heat_target_mA={rec.get('heat_target_mA')}"
          f" plateau_ma={e.get('plateau_ma')}"
          f" bg_ma={e.get('bg_ma')}"
          f" post_bg_ma={e.get('post_bg_ma')}"
          f" emission_ma={e.get('emission_ma')}"
          f" emission_mams={e.get('emission_mams')}")
    if records and not paired:
        print(f"    NOTE: {len(records)} pulse-log record(s) vs {len(events)} measured "
              f"event(s) -- lengths differ, so no heating snapshot is claimed")
    for src, why in ((e, "emission_unavailable"), (e, "integral_mams_unavailable"),
                     (e, "background_note"), (rec, "heat_meas_unavailable"),
                     (rec, "heat_target_unavailable")):
        if src.get(why):
            print(f"    {why}: {src[why]}")
    if e.get("empty_envelope"):
        print("    empty_envelope: True (a rise and a fall on the same sample -- "
              "no pulse was measured)")


# --- MODE = "single": one call -----------------------------------------------
def fire_single(ct, f):
    # The filament must be at IDLE. fire_single_pulse prepares everything at
    # IDLE, raises ACTIVE (verified, up to heat_timeout_s) right after the arm,
    # triggers at once and drops back to idle_ma as soon as the run is over.
    # total_ms is extended by heat_timeout_s for you (its clock starts at arm).
    return ct.fire_single_pulse(
        filament=f,
        num_pulses=1,
        width_us=set_width_us,
        inter_pulse_ms=100,
        max_on_ms=165,
        total_ms=6000,
        controller=None,
        trigger=trigger_type,
        timeout_s=6.0,
        verify=True,
        reuse=True,
        measure=True,
        rate_hz=1000000,
        bg_gap_us=100,
        bg_window_us=50,
        on_armed=on_armed if trigger_type == "ext" else None,
        active_ma=active_current_ma,     # <- new
        idle_ma=idle_cuurrent_ma,        # <- new
        heat_timeout_s=heat_timeout_s,   # <- new
    )


# --- MODE = "steps": the same, step by step ------------------------------------
def fire_steps(ct, f):
    shot = {}
    t_active = t_idle = None
    try:
        # 1. At IDLE: schedule on the board, detector armed, schedule armed.
        shot = ct.shot_prepare(f, num_pulses=1, width_us=set_width_us,
                               inter_pulse_ms=100, max_on_ms=165,
                               total_ms=6000 + int(heat_timeout_s * 1000) + 1000,  # heat-up is after arm
                               trigger=trigger_type, verify=True, reuse=True)
        shot = ct.shot_measure_arm(shot, rate_hz=1000000, bg_gap_us=100, bg_window_us=50)
        shot = ct.shot_arm(shot)
        if not shot["ok"]:
            return shot                          # nothing heated, nothing fired

        # 2. ACTIVE only from here ...
        t_active = time.monotonic()
        act = ct.active_one(f, active_current_ma, verify=True, timeout_s=heat_timeout_s)
        if not act.get("ok"):
            return {**shot, "ok": False, "fired": 0,
                    "error": f"ACTIVE {active_current_ma} mA refused: {act.get('error')}"}
        if trigger_type == "ext":
            on_armed()
        shot = ct.shot_trigger(shot)
        shot = ct.shot_wait(shot, timeout_s=6.0)
        # 3. ... to here: back to IDLE before reading anything.
        ct.idle_one(f, idle_cuurrent_ma)
        t_idle = time.monotonic()

        # 4. At IDLE: pulse log + switch read-backs, then the detector events.
        res = ct.shot_records(shot)
        res = ct.shot_measured(shot, res)
        res["heating"] = act.get("heating")
        res["active_s"] = round(t_idle - t_active, 2)
        return res
    finally:
        ct.shot_abort(shot)                      # disarms whatever is still armed
        if t_active is not None and t_idle is None:
            ct.idle_one(f, idle_cuurrent_ma)     # failed while ACTIVE: drop it


ct = CTClient("192.168.8.218")
ct.safety_config(enabled=True, active_timeout_s=6.0, active_fallback=None, hv_timeout_s=1.0)
ct.set_dead(dead_filaments)
print("dead filaments:", ct.dead)
print(ct.get_filament_order())
fire = fire_single if MODE == "single" else fire_steps
print("MODE:", MODE)

with ct.lease(ttl=120, note="auto test"):
    ct.stop_all(verify=True)
    ct.sleep_all(verify=True)
    ct.standby_all(verify=True)
    if if_idle_all:
        ct.idle_all(filaments=None, default_ma=idle_cuurrent_ma, verify=True)

    ct.set_emission_v(200)
    ct.set_emission_i(55)
    # ct.set_focus_v(350)
    ct.enable_emission(True)
    ct.enable_focus(True)
    time.sleep(active_stable_wait_s)
    ct.set_trigger_delay(trigger_delay_us)

    for i in range(iteration_num):
        for f in filament_list_mask:
            ct.idle_one(f, current_ma=idle_cuurrent_ma, verify=True)   # must be at IDLE
            print_ads(ct)                                              # read at IDLE now
            result = fire(ct, f)
            print_result(f, result)
            ct.idle_one(filament=f, current_ma=idle_cuurrent_ma, verify=True)
            print_ads(ct)
