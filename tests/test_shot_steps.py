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
check("HV DID NOT TURN ON reported", not res["ok"] and "HV DID NOT TURN ON" in res["error"], res)

# 7. Two controllers: the master is armed and triggered too.
ct, calls = make(master=2)
r = ct.fire_single_pulse(16, timeout_s=6.0, **KW)
check("two controllers: arm both, master last", ("/api/arm", (1, 2)) in calls, calls)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
