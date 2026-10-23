#!/usr/bin/env python3
"""GUI Emission & Schedule card backend (2026-10-23): POST/GET /api/shot runs
ct.fire_single_pulse() in the backend with the GUI's PHYSICAL filament index
translated to the user order; GET /api/pulse-events-ma returns the library's
converted events (emission_ma etc.), converting only when there are events.
Offline: CTClient is replaced by a fake.

    python3 tests/test_gui_shot_endpoints.py
"""
import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._server as srv  # noqa: E402
import ct.client._client as cl  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


seen = {}


class FakeCT:
    def __init__(self, *a, **k):
        seen["init"] = k.get("client_id")

    def _user_index_of(self, fid):
        return {39: 4}.get(fid, fid)

    def lease(self, ttl=60, note=""):
        import contextlib
        seen["lease"] = note
        return contextlib.nullcontext()

    def fire_single_pulse(self, f, **kw):
        seen["fire"] = (f, kw)
        return {"ok": True, "fired": 3, "records": [{"filament": 39}] * 3, "measured": []}

    def pulse_events(self, since):
        seen["pe"] = since
        return {"ok": True, "events": seen.get("events", [])}

    def pulse_events_ma(self, since):
        seen["pema"] = since
        return {"ok": True, "events": [{"id": 1, "emission_ma": 1.5}]}


cl.CTClient = FakeCT
srv._safety_schedule_running = lambda: (False, None)

check("no filament -> refused", not srv.shot_start({"active_ma": 2000})["ok"])
check("neither current nor voltage -> refused", not srv.shot_start({"filament": 3})["ok"])
check("both current and voltage -> refused",
      not srv.shot_start({"filament": 3, "active_ma": 2000, "active_mv": 7000})["ok"])

r = srv.shot_start({"filament": 39, "active_mv": 7600, "idle_ma": 1200, "num_pulses": 3,
                    "voltage_settle_s": 2.0, "junk": 1})
check("voltage shot started", r["ok"], r)
for _ in range(50):
    st = srv.shot_status()
    if not st["running"]:
        break
    time.sleep(0.05)
f, kw = seen.get("fire", (None, {}))
check("physical 39 -> user 4, sim trigger, measured, only known keys passed",
      f == 4 and kw.get("trigger") == "sim" and kw.get("measure") is True and kw.get("active_mv") == 7600
      and "junk" not in kw and "active_ma" not in kw, seen.get("fire"))
check("under the lease, result stored with the user index",
      "filament 39" in seen.get("lease", "") and st["result"]["fired"] == 3
      and st["result"]["user_filament"] == 4 and st["params"]["filament"] == 39, st)

srv._safety_schedule_running = lambda: (True, "armed")
check("refused while a schedule is armed",
      not srv.shot_start({"filament": 3, "active_ma": 2000})["ok"])

srv._PULSE_MA_CT = None
r = srv.pulse_events_ma_get(10)
check("no events -> raw reply, no conversion (no rail / reference read)",
      r["ok"] and r["events"] == [] and "pema" not in seen, (r, seen))
seen["events"] = [{"id": 11}]
r = srv.pulse_events_ma_get(10)
check("events -> converted by the library", r["events"][0]["emission_ma"] == 1.5 and seen["pema"] == 10, r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
