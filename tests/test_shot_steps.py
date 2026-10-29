#!/usr/bin/env python3
"""The steps inside fire_single_pulse(), callable one by one (shot_*).

fire_single_pulse() is now shot_prepare -> [shot_measure_arm] -> shot_arm ->
shot_trigger -> shot_wait -> shot_records -> [shot_measured]. These checks
pin that the composed path sends exactly what it did before, and that a
caller can put the steps in its own order (heating between them) and still
get the same result and a full clean-up on failure. Offline: every hardware
call is recorded.

    python3 tests/test_shot_steps.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402
from ct.client._base import SHV_COMPLETE, SHV_FAULT, SHV_ARMED  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(arm_ok=True, end_state=SHV_COMPLETE, on_mismatch=False, master=None):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    ct._hv_ready_for_shot = lambda: {"ok": True, "trips": None}   # HV on, guard quiet
    calls = []
    ct._is_dead = lambda f: False
    ct.filament_to_board = lambda f: {"controller": 1, "slot": 2}
    ct.status = lambda: {"master": master, "controllers": {"1": {"connected": True},
                                                            "2": {"connected": True}}}

    def post(path, body=None, timeout=None):
        calls.append((path, tuple(body.get("controllers", [])) if body and "controllers" in body else None))
        if path == "/api/arm":
            if not arm_ok:
                return {"ok": False, "results": {str(c): {"ok": False, "reject": 7}
                                                 for c in body["controllers"]}}
            return {"ok": True, "results": {str(c): {"ok": True} for c in body["controllers"]}}
        return {"ok": True}
    ct._post = post
    ct.download = lambda plan: (calls.append(("download", None)), {"ok": True})[1]
    ct._verify_retrying_timeouts = lambda plan: (calls.append(("verify", None)),
                                                 {"ok": True, "results": {"1": {"crc": 7, "match": True}}})[1]
    polls = {"n": 0}

    def shv_status(c):
        calls.append(("status", c))
        polls["n"] += 1
        if polls["n"] == 1:
            return {"state": SHV_ARMED, "unsafeSlots": 0}      # the post-arm safety read
        return {"state": end_state, "stopReason": 3, "faultFilament": 16, "totalPulsesDone": 1}
    ct.shv_status = shv_status
    ct.shv_pulse_log = lambda c: (calls.append(("pulse_log", c)),
                                  [{"filament": 16, "on_mismatch": on_mismatch, "read165": 2}])[1]
    ct.ready_arm = lambda *a, **k: (calls.append(("ready_arm", None)), {"ok": True})[1]
    ct.ready_disarm = lambda: (calls.append(("ready_disarm", None)), {"ok": True})[1]
    ct.pulse_cursor = lambda: 41
    ct.read_emission_i_limit = lambda: 45.0
    ct._collect_pulse_events = lambda since, n, *a: (calls.append(("events", since)), ([{"id": 42}], 1228))[1]
    ct._poll_intervals = lambda a, b: iter(lambda: 0.0, None)
    return ct, calls


KW = dict(num_pulses=1, width_us=7000, inter_pulse_ms=100, max_on_ms=165, total_ms=6000)

# 1. fire_single_pulse goes through the steps, in the old order.
ct, calls = make()
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **KW)
seq = [c[0] for c in calls]
print("  ", seq)
check("composed fire ok", r["ok"] and r["fired"] == 1 and r["measured"] == [{"id": 42}], r)
check("composed order: detector, disarm, download, verify, arm, safety read, trigger, wait, log, events, detector off",
      seq == ["ready_arm", "/api/disarm", "download", "verify", "/api/arm", "status",
              "/api/sync/simulate", "status", "pulse_log", "events", "ready_disarm"], seq)

# 2. The same shot by hand, with heating between the steps.
ct, calls = make()
shot = ct.shot_prepare(16, **KW)
calls.append(("IDLE->ACTIVE", None))
shot = ct.shot_measure_arm(shot, 1000000)
shot = ct.shot_arm(shot)
shot = ct.shot_trigger(shot)
shot = ct.shot_wait(shot, 6.0)
calls.append(("ACTIVE->IDLE", None))
res = ct.shot_records(shot)
res = ct.shot_measured(shot, res)
seq = [c[0] for c in calls]
print("  ", seq)
check("by hand: ok, same result fields", res["ok"] and res["fired"] == 1 and res["measured"] == [{"id": 42}]
      and res["records"] and "status" in res, res)
check("by hand: heating where the caller put it",
      seq.index("IDLE->ACTIVE") < seq.index("/api/sync/simulate")
      and seq.index("status", seq.index("/api/sync/simulate")) < seq.index("ACTIVE->IDLE") < seq.index("pulse_log"), seq)
check("cursor taken at measure_arm", shot["since"] == 41 and ("events", 41) in calls)

# 3. Arm rejected: nothing fires, failure keeps the shot context, abort cleans all.
ct, calls = make(arm_ok=False)
shot = ct.shot_measure_arm(ct.shot_prepare(16, **KW))
shot = ct.shot_arm(shot)
check("arm rejected -> not ok, says why", not shot["ok"] and "arm rejected" in shot["error"], shot)
check("...keeps armed_set / measuring for clean-up", shot.get("armed_set") == [1] and shot.get("measuring"))
t = ct.shot_trigger(shot)
check("...later steps do nothing", t is shot and "/api/sync/simulate" not in [c[0] for c in calls])
n0 = len(calls)
ct.shot_abort(shot)
check("abort disarms the board and the detector",
      ("/api/disarm", (1,)) in calls[n0:] and ("ready_disarm", None) in calls[n0:], calls[n0:])

# 4. External trigger: shot_trigger sends nothing.
ct, calls = make()
shot = ct.shot_trigger(ct.shot_arm(ct.shot_prepare(16, trigger="ext", **KW)))
check("ext trigger -> no simulate", shot["ok"] and "/api/sync/simulate" not in [c[0] for c in calls])

# 5. Fault during the run: disarmed, not ok, context kept.
ct, calls = make(end_state=SHV_FAULT)
shot = ct.shot_wait(ct.shot_trigger(ct.shot_arm(ct.shot_prepare(16, **KW))), 2.0)
check("fault -> not ok, named", not shot["ok"] and "SHV fault" in shot["error"] and shot.get("armed_set") == [1], shot)
check("...board disarmed", calls[-1] == ("/api/disarm", (1,)), calls[-1])
check("...records of a failed shot is a no-op", ct.shot_records(shot) is shot)

# 6. Switch read-back mismatch still judged by shot_records.
ct, calls = make(on_mismatch=True)
res = ct.shot_records(ct.shot_wait(ct.shot_trigger(ct.shot_arm(ct.shot_prepare(16, **KW))), 2.0))
check("MOSFET not confirmed closed: plain error names pulse + site, not measured",
      not res["ok"] and "Filament 16 (P1 CH1.3): the MOSFET was not confirmed closed on "
      "pulse(s) 1 of 1" in res["error"] and "Not measured" in res["error"]
      and "0x" not in res["error"], res)
check("...the bytes are in detail, not in the error",
      "pulse 1: wrote 0x04, read back 0x02 (the neighbouring bit)" in res.get("detail", ""), res)

# 6b. ...and the measured current says which: MOSFET closed (read-back wrong) or not.
# Current flowed -> the shot is GOOD: ok True, the read-back problem as a warning.
ct, calls = make(on_mismatch=True)
ct._collect_pulse_events = lambda since, n, *a: ([{"id": 42, "emission_ma": 20.0}], 1228)
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **KW)
check("mismatch + current flowed -> ok True, warning, readback_unreliable, no error",
      r["ok"] and "only the MOSFET read-back is wrong" in r.get("warning", "") and r.get("readback_unreliable")
      and "error" not in r and r["mosfet_conducted"] is True and r["measured"], r)
# No current -> the MOSFET did not close: still a failure.
ct, calls = make(on_mismatch=True)
ct._collect_pulse_events = lambda since, n, *a: ([{"id": 42, "emission_ma": None}], 1228)
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **KW)
check("mismatch + no current -> ok False, 'did not fire'",
      not r["ok"] and "Pulse(s) 1 did not fire" in r["error"] and r["mosfet_conducted"] is False, r)
# Current flowed, but ALSO read non-zero after the pulse (may still be closed): stays a failure.
ct, calls = make(on_mismatch=True)
ct.shv_pulse_log = lambda c: [{"filament": 16, "on_mismatch": True, "hv_stuck_on": True, "read165": 2}]
ct._collect_pulse_events = lambda since, n, *a: ([{"id": 42, "emission_ma": 20.0}], 1228)
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **KW)
check("current flowed but stuck-on after the pulse -> ok False", not r["ok"] and "error" in r, r)
# Current flowed in the mismatched pulse, but a pulse went unmeasured: stays a failure.
ct, calls = make(on_mismatch=True)
ct.shv_pulse_log = lambda c: [{"filament": 16, "on_mismatch": True, "read165": 2},
                              {"filament": 16, "on_mismatch": False, "read165": 4}]
ct._collect_pulse_events = lambda since, n, *a: ([{"id": 42, "emission_ma": 20.0}], 1228)
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **{**KW, "num_pulses": 2})
check("1 of 2 pulses measured -> ok False (cannot pair, incomplete)", not r["ok"], r)
# Not measured -> unknown: still a failure.
ct, calls = make(on_mismatch=True)
r = ct.fire_single_pulse(16, measure=False, timeout_s=6.0, **KW)
check("mismatch, not measured -> ok False, 'Not measured'",
      not r["ok"] and "Not measured" in r["error"], r)

# 6c. 2026-10-12, filament 8 (P1 CH2.1): 10 pulses, only some read back wrong.
#     The verdict must be about THOSE pulses' own current (it used to look at
#     pulse 1 only), name them, and say whether the data are usable.
ct, calls = make(on_mismatch=True)
log = [{"filament": 16, "on_mismatch": i in (2, 6), "read165": {2: 8, 6: 0}.get(i, 4)}
       for i in range(10)]
ct.shv_pulse_log = lambda c: log
ems = [5.0] * 10
ems[6] = None                                  # pulse 7: no current at all
ct._collect_pulse_events = lambda since, n, *a: ([{"id": 50 + i, "emission_ma": ems[i],
                                                   "emission_mams": None if ems[i] is None else 110.0,
                                                   "integral_mams": 1.0 if ems[i] is None else 150.0}
                                                  for i in range(10)], 1228)
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **{**KW, "num_pulses": 10})
e = r.get("error") or ""
d = r.get("detail") or ""
check("10 pulses: names the 2 mismatched ones; bytes in detail",
      "pulse(s) 3, 7 of 10" in e and "pulse 3: wrote 0x04, read back 0x08 (the neighbouring bit)" in d
      and "pulse 7: wrote 0x04, read back 0x00 (no bit)" in d, (e, d))
check("...pulse 3 current flowed: read-back wrong, good data",
      "Pulse(s) 3 fired normally (5.00 mA / 110.0 mA*ms flowed)" in e and "data is good" in e, e)
check("...pulse 7 no current: did not fire, its measured mA*ms shown",
      "Pulse(s) 7 did not fire (no current: 1.0 mA*ms measured" in e, e)
check("...the 'not measured' placeholder is gone", "Not measured" not in e, e)
check("...mosfet_conducted False (one did not)", r.get("mosfet_conducted") is False, r)

# 6d. Events missing: cannot pair -> said, no verdict invented.
ct, calls = make(on_mismatch=True)
ct.shv_pulse_log = lambda c: log
ct._collect_pulse_events = lambda since, n, *a: ([{"id": 50, "emission_ma": 5.0}] * 9, 1228)
r = ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **{**KW, "num_pulses": 10})
check("unpaired: unknown, said so", "unknown whether those pulses fired" in (r.get("error") or "")
      and r.get("mosfet_conducted") is None, r)

# 6e. Arm refused: names the filament, its board, and what NotReady means.
ct, calls = make(arm_ok=False)
shot = ct.shot_arm(ct.shot_measure_arm(ct.shot_prepare(16, **KW)))
check("arm refused message: filament + board + I2C/pre-arm check, nothing fired",
      "arm rejected for filament 16 (" in shot["error"] and "pre-arm safety check" in shot["error"]
      and "Nothing was fired" in shot["error"], shot["error"])

# 7. Two controllers: the master is armed and triggered too.
ct, calls = make(master=2)
r = ct.fire_single_pulse(16, timeout_s=6.0, **KW)
check("two controllers: arm both, master last", ("/api/arm", (1, 2)) in calls, calls)

# 8. shot_wait_timeout_s (new name) and timeout_s (old name) both reach shot_wait.
for kw, want in (({"shot_wait_timeout_s": 7.5}, 7.5), ({"timeout_s": 8.5}, 8.5), ({}, 15.0)):
    ct, calls = make()
    seen = {}
    real = ct._shot_wait
    ct._shot_wait = lambda shot, t, real=real, seen=seen: (seen.setdefault("t", t), real(shot, t))[1]
    ct.fire_single_pulse(16, **KW, **kw)
    check(f"{kw or 'default'} -> shot_wait waits {want} s", seen.get("t") == want, seen)

# 9. SUPPLY LIMIT (2026-10-15, filament 78 P1 CH6.3): pulses at the emission
#    current limit are the supply clamping, not emission -> ok False, plain
#    message first; it used to come back ok=True.
def clamp_run(nets, heat=2597.0, limit=45.0, mismatch_at=None):
    ct, calls = make(on_mismatch=mismatch_at is not None)
    ct.read_emission_i_limit = lambda: limit
    ct.shv_pulse_log = lambda c: [{"filament": 16, "on_mismatch": i == mismatch_at,
                                   "read165": 0 if i == mismatch_at else 4,
                                   "heat_meas_mA": heat} for i in range(len(nets))]
    ct._collect_pulse_events = lambda since, n, *a: (
        [{"id": 50 + i, "plateau_net_ma": v, "emission_ma": None if v is None else v - 1}
         for i, v in enumerate(nets)], 1228)
    return ct.fire_single_pulse(16, measure=True, timeout_s=6.0, **{**KW, "num_pulses": len(nets)})


r = clamp_run([38.0] * 8 + [5.0, 5.0])
e = r.get("error") or ""
check("8 of 10 at 38 mA vs 45 mA limit -> ok False, supply_limited",
      not r["ok"] and r.get("supply_limited") == list(range(8)) and r.get("supply_limit_ma") == 45.0, r)
check("...conclusion first, heating current, what to do",
      e.startswith("Filament 16 (P1 CH1.3): 8 of 10 pulses read about 38 mA = the emission "
                   "supply's current limit (45 mA set), not emission")
      and "arcing or breaking down at this heating current (2597 mA)" in e
      and "Lower the heating current" in e, e)
r = clamp_run([38.0] * 9 + [None], mismatch_at=9)
e = r.get("error") or ""
check("clamp + a pulse that did not fire: both, clamp first",
      not r["ok"] and e.index("current limit") < e.index("Pulse(s) 10 did not fire"), e)
r = clamp_run([38.0, 5.0], mismatch_at=0)
check("clamped pulse with wrong read-back: NOT downgraded to a warning",
      not r["ok"] and "current limit" in (r.get("error") or "") and not r.get("readback_unreliable")
      and "warning" not in r, r)
r = clamp_run([28.0, 28.0], heat=0.0, limit=30.0)
check("cold clamp -> short/breakdown wording, not 'heating current'",
      not r["ok"] and "short or breakdown" in (r.get("error") or ""), r.get("error"))
r = clamp_run([20.0] * 3)
check("below 80% of the limit -> ok, no supply_limited", r["ok"] and "supply_limited" not in r, r)
r = clamp_run([44.0] * 3, limit=None)
check("limit unknown -> check skipped, ok", r["ok"] and "supply_limited" not in r, r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
