#!/usr/bin/env python3
"""wait_for_currents (the batch verify behind idle_all/active_all verify=True)
gives up on a filament that stops getting closer to its target, instead of
holding the whole batch until the timeout. Offline: fake cached reads; the
stall window is shortened so the test runs in a few seconds.

    python3 tests/test_batch_verify_stall.py
"""

import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct_simple_control import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def client(traj):
    """traj: {filament: fn(t) -> (current_mA or None, arrival)}"""
    ct = CTClient.__new__(CTClient)
    ct.STALL_S = 0.6
    ct.record = False          # no call log from a test
    t0 = time.monotonic()

    def read(fils):
        t = time.monotonic() - t0
        out = {}
        for f in fils:
            cur, arr = traj[f](t)
            out[f] = {"current_mA": cur, "arrival": arr, "present": cur is not None, "cc_mode": 1}
        return out

    ct.read_filament_current_cached = read
    ct._get = lambda *a, **k: {"ok": True, "struggling": {}}
    ct._is_dead = lambda f: False
    ct._fid_of = lambda f: f
    return ct


def ramp(rate, start=800, target=1200, settle_after=None):
    def fn(t):
        cur = min(target, start + rate * t)
        done = settle_after is not None and t >= settle_after
        return cur, ("settled" if done else "ramping")
    return fn


traj = {
    1: ramp(400, settle_after=1.2),        # normal: ~1 s
    2: ramp(400, settle_after=1.2),
    3: lambda t: (810.0, "ramping"),       # stuck below target (bad contact)
    4: lambda t: (None, None),             # no reading at all (lost board)
}
ct = client(traj)
t = time.monotonic()
r = ct.wait_for_currents({f: 1200 for f in traj}, timeout_s=20.0, poll_interval_s=0.05)
el = time.monotonic() - t
check("batch returns once the good ones arrive and the bad ones stall (not 20 s)", el < 3.0, f"{el:.1f}s")
check("good filaments arrived", r["results"][1]["ok"] and r["results"][2]["ok"])
check("stuck filament given up, marked stalled", r["results"][3].get("stalled") and not r["results"][3]["ok"],
      str(r["results"][3]))
check("no-reading filament given up, marked stalled", r["results"][4].get("stalled") and "no reading" in r["results"][4]["error"])
check("overall ok is False (two did not arrive)", r["ok"] is False and r["pending"] == [])

# slow but steadily closer: never given up, waits until it arrives
slow = {5: ramp(150, settle_after=2.8)}    # 150 mA/s >> 25 mA per stall window
r = client(slow).wait_for_currents({5: 1200}, timeout_s=20.0, poll_interval_s=0.05)
check("slow but progressing filament is waited for", r["results"][5]["ok"] and not r["results"][5].get("stalled"),
      str(r["results"][5]))

# capped is still reported immediately, as before
capped = {6: lambda t: (900.0, "capped")}
t = time.monotonic()
r = client(capped).wait_for_currents({6: 1200}, timeout_s=20.0, poll_interval_s=0.05)
check("capped still ends at once", r["results"][6].get("capped") and time.monotonic() - t < 0.5)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
