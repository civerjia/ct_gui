#!/usr/bin/env python3
"""CTClient.heat_short_test: one filament heated at a time, focus watched for a
short to emission. Offline -- the hardware calls are replaced by a model of
what the rig did on 2026-10-06: a hot filament pulls focus to emission + ~2 V,
and focus comes back a few seconds after its heater goes off.

    python3 tests/test_heat_short.py
"""
import _path  # noqa: F401

import contextlib
import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Rig:
    """culprit: heated this long (s) -> focus collapses; recovers `recover` s
    after its heater goes off (None = never)."""

    def __init__(self, culprit=None, after=1.0, recover=0.5, hv_on=False):
        self.culprit, self.after, self.recover = culprit, after, recover
        self.hv = {"emission_on": hv_on, "focus_on": hv_on}
        self.heating = {}        # filament -> since
        self.off_at = None
        self.calls = []
        self.focus_set = 0.0

    def install(self, ct):
        rig = self
        ct._mosfet_targets = lambda f: (list(f) if f is not None else [0, 1, 2, 3, 4], [])
        ct.hv_status = lambda: {"ok": True, **rig.hv}

        @contextlib.contextmanager
        def energised(*fils, verify=True):
            try:
                yield ct
            finally:
                rig.calls.append(("stop", fils))
        ct.energised = energised
        ct.stop_all = lambda *a, **k: {"ok": True}
        ct.sleep_all = lambda *a, **k: {"ok": True}
        ct.standby_all = lambda *a, **k: {"ok": True}
        ct.set_emission_i = lambda *a: {"ok": True}

        def enable_emission(on):
            rig.hv["emission_on"] = on
            rig.calls.append(("emission", on))
            return {"ok": True}

        def enable_focus(on):
            rig.hv["focus_on"] = on
            rig.calls.append(("focus", on))
            return {"ok": True}
        ct.enable_emission = enable_emission
        ct.enable_focus = enable_focus
        ct._mosfet_rail = lambda v, timeout_s=3.0: (-abs(v), None)

        def set_focus_v(v):
            rig.focus_set = abs(v)
            return {"ok": True}
        ct.set_focus_v = set_focus_v

        def idle_one(f, current_ma, **k):
            rig.heating[f] = time.time()
            rig.calls.append(("idle", f))
            return {"ok": True}

        def sleep_one(f, **k):
            if f in rig.heating:
                del rig.heating[f]
                if f == rig.culprit:
                    rig.off_at = time.time()
            rig.calls.append(("sleep", f))
            return {"ok": True}
        ct.idle_one = idle_one
        ct.sleep_one = sleep_one
        ct.read_ads_all = rig.ads
        ct.describe = lambda r: str(r)

    def shorted(self):
        now = time.time()
        if self.culprit in self.heating and now - self.heating[self.culprit] >= self.after:
            return True
        if self.off_at is not None:
            return self.recover is None or now - self.off_at < self.recover
        return False

    def ads(self):
        if not self.hv["focus_on"]:
            return {"ok": True, "focus_v": 0.0, "emiss_v": -197.8, "emiss_i_ma": 11.6}
        if self.shorted():
            return {"ok": True, "focus_v": -196.0, "emiss_v": -197.8, "emiss_i_ma": 10.1}
        return {"ok": True, "focus_v": -self.focus_set + 2, "emiss_v": -197.8, "emiss_i_ma": 11.6}


def run(rig, **kw):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    rig.install(ct)
    return ct.heat_short_test(heat_s=1.5, sample_s=0.05, recover_s=2.0, **kw)


# 1. Clean rig: every filament ok, rails off at the end, filaments STOPped.
rig = Rig()
r = run(rig, filaments=[0, 1, 2])
check("clean -> ok", r["ok"] and r["counts"] == {"ok": 3, "short": 0, "unmeasured": 0}, r)
check("clean -> rails off at the end", rig.hv == {"emission_on": False, "focus_on": False}, rig.hv)
check("clean -> energised() teardown ran", ("stop", (0, 1, 2)) in rig.calls, rig.calls)
check("each heated filament goes to SLEEP after", [c for c in rig.calls if c[0] == "sleep"]
      == [("sleep", 0), ("sleep", 1), ("sleep", 2)], rig.calls)

# 2. Filament 3 shorts focus after 0.5 s hot; recovers 0.4 s after heater off.
rig = Rig(culprit=3, after=0.5, recover=0.4)
rows = []
r = run(rig, filaments=[2, 3, 4], progress=lambda f, row: rows.append(f))
res = r["results"]
check("culprit named", res[3]["verdict"] == "short" and res[2]["verdict"] == "ok"
      and res[4]["verdict"] == "ok", res)
check("short timing recorded", res[3]["short_after_s"] is not None and 0.3 <= res[3]["short_after_s"] <= 1.0, res[3])
check("emission current drop recorded", res[3]["di_ma"] == -1.5, res[3])
check("recovery recorded, test carried on", res[3]["recovered_s"] is not None and 4 in res, res[3])
check("not ok, problem names the filament", not r["ok"] and any("filament 3" in p for p in r["problems"]), r["problems"])
check("progress for every filament", rows == [2, 3, 4], rows)

# 3. Focus never comes back: stop there, the rest cannot be judged.
rig = Rig(culprit=3, after=0.3, recover=None)
r = run(rig, filaments=[3, 4])
check("no recovery -> stops", 4 not in r["results"] and any("did not recover" in p for p in r["problems"]), r)
check("no recovery -> rails still turned off", rig.hv == {"emission_on": False, "focus_on": False}, rig.hv)

# 4. stop_on_short.
rig = Rig(culprit=0, after=0.2, recover=0.2)
r = run(rig, filaments=[0, 1], stop_on_short=True)
check("stop_on_short -> stops after the first", list(r["results"]) == [0], r["results"])

# 5. Refuses with HV already on, touches nothing.
rig = Rig(hv_on=True)
r = run(rig, filaments=[0])
check("HV already on -> refused", not r["ok"] and "already ON" in r["problems"][-1] and not rig.calls, (r, rig.calls))

# 6. Abort between filaments.
rig = Rig()
n = {"i": 0}


def _abort():
    n["i"] += 1
    return n["i"] > 1


r = run(rig, filaments=[0, 1, 2], abort=_abort)
check("abort -> stops, says so", list(r["results"]) == [0] and "aborted" in r["problems"], r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
