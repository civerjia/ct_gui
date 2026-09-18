import _path  # noqa: F401  — makes ct_simple_control importable from tests/
from ct_simple_control import CTClient, CTError
import time

test_filament_num = 12
idle_cuurrent_ma = 1200
active_current_ma = 2700
active_stable_wait_s = 2.0
dead_filaments = [6,26,73]
filament_list = [8,9,10,11,24,25,27]
filament_list = [25]
filament_list_mask = [1,2,3,4,5,6,7]
filament_list_mask = [6]
ct = CTClient("192.168.8.214")
with ct.lease(ttl=120, note="auto test"):
    ct.stop_all()
    ct.set_dead(dead_filaments)
    print(ct.dead)
    ct.set_filament_order({})
    # ct.set_filament_order({8: 1, 9: 2, 10: 3, 11: 4, 24: 5, 25: 6, 27: 7})

    print(ct.get_filament_order())
    
    ct.set_emission_v(200)        # backend loads LUT and writes DS3502
    ct.set_emission_i(30)
    ct.set_focus_v(350)
    ct.enable_emission(True)
    ct.enable_focus(True)
    time.sleep(1)
    print("emission_v:", ct.read_emission_v())   # V (negative)
    print("emission_i:", ct.read_emission_i())   # mA
    print("focus_v:", ct.read_focus_v())      # V (negative)

    # ct.sleep_all()
    # print(ct.standby_all(filaments=filament_list_mask))
    #ct.idle_all(filaments=filament_list_mask, default_ma=idle_cuurrent_ma)
    # time.sleep(active_stable_wait_s)


    # for test_filament_num in range(test_filament_num,test_filament_num+1):
    #     if test_filament_num in dead_filaments:
    #         continue
    #     ct.active_one(test_filament_num, current_ma=active_current_ma)
    #     time.sleep(active_stable_wait_s)

    #     dc_ma = ct.read_emission_i()              # steady-state DC current
    #     print(f"Before MOSFET on filament {test_filament_num} DC emission current: {dc_ma} mA")
    #     ct.hv_grid_set(test_filament_num, on=True, force=True)   # route HV to filament test_filament_num
    #     time.sleep(1.2)                           # let the switch settle
    #     dc_ma = ct.read_emission_i()              # steady-state DC current
    #     print(f"After MOSFET on filament {test_filament_num} DC emission current: {dc_ma} mA")
    #     ct.hv_grid_set(test_filament_num, on=False, force=True)  # switch it back off
    #     time.sleep(1.2)
    #     dc_ma = ct.read_emission_i()              # steady-state DC current
    #     print(f"After MOSFET off filament {test_filament_num} DC emission current: {dc_ma} mA")

    #     print("waiting for external trigger...")

 
    #     result = ct.fire_single_pulse(
    #         filament=test_filament_num,
    #         num_pulses=1,
    #         width_us=1000,
    #         inter_pulse_ms=1000,
    #         max_on_ms=40,
    #         total_ms=15000,      # RP2350 FIRMWARE's own schedule timeout (ms)
    #                                         # — see docstring, "total_ms vs timeout_s"
    #         controller=None,   # None = auto-infer from `filament` via
    #                                             # the active-list mapping — see docstring,
    #                                             # "why controller exists at all"
    #         trigger="sim",       # "sim" = ESP32 SyncIn; "ext" = external edge
    #         timeout_s=15.0,     # PYTHON CLIENT's polling timeout (seconds)
    #                                         # — see docstring, "total_ms vs timeout_s"
    #         verify=True, 
    #         reuse=False,   # skip re-download if unchanged since your last
    #                                 # call — see docstring, "reuse — skipping the
    #                                 # download when nothing changed"; OFF by
    #                                 # default because it has a real, documented
    #                                 # safety gap (see the docstring) — opt in only
    #                                 # when you understand it.
    #     )
    
    #     print(result)
    #     ct.idle_one(test_filament_num, current_ma=idle_cuurrent_ma)
    #     time.sleep(active_stable_wait_s)

    #print(ct.read_ads_all())
    #ct.enable_emission(True)
    #print(ct.read_emission_v())
    #result = ct.fire_single_pulse(filament=5, num_pulses=1, width_us=1000)
    #ct.enable_emission(False)
    #ct.stop_all()