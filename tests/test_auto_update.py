#!/usr/bin/env python3
"""The backend applies a GitHub update by itself once the rig is quiet
(2026-10-14) -- no GUI restart button. Quiet = no lease, no schedule, no
remote call / with-block, no test job, grid open, no filament/HV command for
AUTO_UPDATE_QUIET_S. Reads do not count. One try per version (no loop).
Offline: time, GitHub and the restart are fakes.

    python3 tests/test_auto_update.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._server as srv  # noqa: E402
from ct import update as ct_update  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self):
        self.t = 10000.0

    def monotonic(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeTime:
    def __init__(self, clock, real):
        self._c, self._r = clock, real

    def __getattr__(self, n):
        return getattr(self._c, n) if n in ("monotonic", "sleep") else getattr(self._r, n)


clock = Clock()
real_time = srv.time
srv.time = FakeTime(clock, real_time)
srv.CONTROLLERS.clear()
srv._SAFETY_TOUCH_FIL.clear()
srv._GRID_CLOSED.clear()
srv._SAFETY_TOUCH_HV = 0.0
lease = {"held": False}
srv._lease_snapshot = lambda: dict(lease)
srv._remote.activity = lambda: list(ACT)
ACT = []

# 1. _rig_busy: each thing that a restart would cut is named; reads are not.
check("quiet rig -> []", srv._rig_busy() == [], srv._rig_busy())
lease.update(held=True, owner="combined_backend")
check("lease held -> busy", any("lease" in w for w in srv._rig_busy()))
lease["held"] = False
ACT[:] = ["combined_backend: a call in progress"]
check("remote call in progress -> busy", any("call in progress" in w for w in srv._rig_busy()))
ACT[:] = []
srv._HEAT_SHORT["running"] = True
check("test job running -> busy", any("heat-short" in w for w in srv._rig_busy()))
srv._HEAT_SHORT["running"] = False
srv._GRID_CLOSED.add(12)
check("grid closed -> busy", any("grid" in w for w in srv._rig_busy()))
srv._GRID_CLOSED.clear()
srv._SAFETY_TOUCH_FIL[5] = clock.t - 60
check("filament command 60 s ago -> busy", any("command" in w for w in srv._rig_busy()))
srv._SAFETY_TOUCH_FIL[5] = clock.t - srv.AUTO_UPDATE_QUIET_S - 1
check("...older than the quiet window -> quiet", srv._rig_busy() == [], srv._rig_busy())

# 2. The loop: update available, rig busy for a while, then quiet -> restart once.
restarts = []
srv._auto_update_restart = lambda latest: restarts.append(latest)
ct_update.remote_check = lambda cur: {"ok": True, "available": True, "latest": "abc1234",
                                      "current": "old0000"}
os.environ.pop("CT_AUTOUPDATE_TRIED", None)
srv._STOPPING.clear()
srv._SAFETY_TOUCH_FIL[5] = clock.t - 10          # someone heating a moment ago
start = clock.t
busy_until = clock.t + 600


def busy_then_quiet():
    return ["filament/HV command"] if clock.t < busy_until else []
real_busy = srv._rig_busy
srv._rig_busy = busy_then_quiet
srv._update_watch_loop()
check("restarted once the rig went quiet, onto the new version", restarts == ["abc1234"], restarts)
check("...not before it was quiet", clock.t >= busy_until, clock.t - start)

# 3. Already restarted once for this version and still behind: no loop.
restarts.clear()
os.environ["CT_AUTOUPDATE_TRIED"] = "abc1234"
srv._rig_busy = lambda: []
calls = {"n": 0}


def check_twice(cur):
    calls["n"] += 1
    if calls["n"] >= 2:
        srv._STOPPING.set()
    return {"ok": True, "available": True, "latest": "abc1234", "current": "old0000"}
ct_update.remote_check = check_twice
srv._update_watch_loop()
check("same version tried already -> no second restart", restarts == [], restarts)

# 4. CT_AUTO_UPDATE=0: announce only.
srv._STOPPING.clear()
os.environ.pop("CT_AUTOUPDATE_TRIED", None)
srv.AUTO_UPDATE = False
calls["n"] = 0
srv._update_watch_loop()
check("auto-update off -> never restarts", restarts == [], restarts)
check("status says auto off", srv._UPDATE_STATUS.get("auto") is False, srv._UPDATE_STATUS)

srv._rig_busy = real_busy
srv.time = real_time
print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
