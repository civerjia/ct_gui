#!/usr/bin/env python3
"""pulse_interval_ms / trigger_timeout_ms (2026-10-14): inter_pulse_ms meant
the SPACING with trigger="sim" and only the firmware TIMEOUT with an external
trigger, and its docstring called it a "minimum gap". Now two names; the old
one keeps its per-mode meaning; which one is logged once per process (it used
to ride on every result, burying the real error -- 2026-10-15). Limits are refused,
never silently changed (the sim used to raise <10 ms to 10). Offline: the
plan sent to the board is captured.

    python3 tests/test_pulse_timing.py
"""
import _path  # noqa: F401

import logging
import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []
NOTES = []


class _Grab(logging.Handler):
    def emit(self, rec):
        NOTES.append(rec.getMessage())


logging.getLogger("ct_gui").addHandler(_Grab())
logging.getLogger("ct_gui").setLevel(logging.INFO)


def fresh_notes():
    CTClient._NOTES_LOGGED.clear()
    NOTES.clear()


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make():
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    seen = {}
    ct._is_dead = lambda f: False
    ct.filament_to_board = lambda f: {"controller": 1, "slot": 2}
    ct.status = lambda: {"master": None, "controllers": {"1": {"connected": True}}}
    ct._post = lambda path, body=None, timeout=None: (seen.setdefault("posts", []).append((path, body)),
                                                       {"ok": True})[1]

    def download(plan):
        seen["plan"] = plan
        return {"ok": True}
    ct.download = download
    ct._verify_retrying_timeouts = lambda plan: {"ok": True, "results": {"1": {"crc": 7}}}
    return ct, seen


BASE = dict(num_pulses=3, width_us=1000, max_on_ms=40, total_ms=20000)

# 1. sim: the old name is the spacing; timeout defaults to 2x+100; note says so.
ct, seen = make()
fresh_notes()
s = ct.shot_prepare(16, inter_pulse_ms=300, trigger="sim", **BASE)
check("sim old name -> interval 300, firmware timeout 700",
      s["ok"] and s["pulse_interval_ms"] == 300 and s["trigger_timeout_ms"] == 700
      and seen["plan"]["config"]["interPulseMs"] == 700, (s, seen.get("plan", {}).get("config")))
check("sim old name -> logged note names pulse_interval_ms, not in the shot",
      len(NOTES) == 1 and "pulse_interval_ms" in NOTES[0] and "note" not in s, (NOTES, s.get("note")))
ct.shot_prepare(16, inter_pulse_ms=250, trigger="sim", **BASE)
check("...logged once per process, not per call", len(NOTES) == 1, NOTES)

# 2. external: the old name is ONLY the timeout; no spacing implied.
ct, seen = make()
fresh_notes()
s = ct.shot_prepare(16, inter_pulse_ms=5000, trigger="ext", **BASE)
check("external old name -> timeout 5000, no interval",
      s["ok"] and s["trigger_timeout_ms"] == 5000 and s["pulse_interval_ms"] is None
      and seen["plan"]["config"]["interPulseMs"] == 5000, s)
check("external old name -> logged note says NOT the spacing",
      len(NOTES) == 1 and "NOT the spacing" in NOTES[0], NOTES)

# 3. New names, explicit.
ct, seen = make()
fresh_notes()
s = ct.shot_prepare(16, pulse_interval_ms=200, trigger_timeout_ms=1000, trigger="sim", **BASE)
check("new names: as given, no note", s["ok"] and s["pulse_interval_ms"] == 200
      and seen["plan"]["config"]["interPulseMs"] == 1000 and not NOTES, (s, NOTES))
ct, seen = make()
s = ct.shot_prepare(16, trigger="ext", **BASE)
check("external defaults: timeout 3000", s["ok"] and seen["plan"]["config"]["interPulseMs"] == 3000, s)


# 4. Refusals -- with the reason, nothing sent.
def refused(desc, want, **kw):
    ct, seen = make()
    s = ct.shot_prepare(16, **{**BASE, **kw})
    check(f"refused: {desc}", not s["ok"] and want in s["error"] and "plan" not in seen, s.get("error"))


refused("both names", "not both", inter_pulse_ms=100, pulse_interval_ms=100)
refused("sim < 10 ms (was silently raised to 10)", "below the 10 ms", pulse_interval_ms=5, trigger="sim")
refused("timeout not longer than interval", "must be longer", pulse_interval_ms=500,
        trigger_timeout_ms=500, trigger="sim")
refused("interval not longer than the pulse", "not longer than width_us", pulse_interval_ms=10,
        width_us=12000, max_on_ms=40, trigger="sim")
refused("train does not fit total_ms", "not inside total_ms", pulse_interval_ms=10000,
        trigger="sim", num_pulses=3, total_ms=15000)
refused("timeout above u32", "out of range", trigger_timeout_ms=2 ** 32, trigger="ext")

# 5. fire_single_pulse: measure check uses the SPACING; note reaches the result.
ct, seen = make()
r = ct.fire_single_pulse(16, pulse_interval_ms=0.4, trigger="ext", measure=True, **{**BASE, "num_pulses": 1})
check("measure: external spacing 0.4 ms refused (backgrounds need 0.5 ms)",
      not r["ok"] and "pulse_interval_ms" in r["error"], r.get("error"))
ct, seen = make()
ct._fire_single = lambda *a, **k: (seen.update(fs=k), {"ok": True, "fired": 3})[1]
fresh_notes()
r = ct.fire_single_pulse(16, inter_pulse_ms=5000, trigger="ext", measure=True, **BASE)
check("measure + external old name: NOT refused as a 5 s spacing problem, passed as timeout",
      r["ok"] and seen["fs"]["trigger_timeout_ms"] == 5000 and seen["fs"]["pulse_interval_ms"] is None, seen.get("fs"))
check("...the note is logged, the result does not carry it",
      "note" not in r and any("trigger_timeout_ms" in n for n in NOTES), (r, NOTES))

# 6. Downstream: sim trigger spacing and detector TTL use the right number.
ct, seen = make()
s = ct.shot_prepare(16, pulse_interval_ms=250, trigger="sim", **BASE)
s = {**s, "armed_set": [1]}
ct._disarm_all = lambda *a: None
ct._shot_trigger(s)
sim = [b for p, b in seen["posts"] if p == "/api/sync/simulate"]
check("sim trigger fires at pulse_interval_ms", sim and sim[0]["interval_ms"] == 250.0, sim)
ttl = {}
ct.ready_arm = lambda rate, ttl_ms=None, **k: (ttl.update(v=ttl_ms), {"ok": False, "error": "x"})[1]
ct.shot_measure_arm({"ok": True, "pulse_interval_ms": None, "trigger_timeout_ms": 60000,
                     "num_pulses": 1}, shot_wait_timeout_s=5)
check("detector TTL covers the trigger timeout (external, 60 s)", ttl["v"] >= 60000, ttl)

# 7. shv_set_config: new name, old name, both.
ct, seen = make()
ct._shv = lambda c, body: (seen.update(cfg=body), {"ok": True})[1]
ct.shv_set_config(1, trigger_timeout_ms=4000)
check("shv_set_config(trigger_timeout_ms) -> interPulseMs", seen["cfg"]["interPulseMs"] == 4000)
ct.shv_set_config(1, 2500)
check("shv_set_config positional old name still works", seen["cfg"]["interPulseMs"] == 2500)
check("shv_set_config both -> refused", not ct.shv_set_config(1, 2500, trigger_timeout_ms=1)["ok"])

# 8. One name, two defaults across the library (2026-10-14).
import inspect  # noqa: E402
check("defaults: SHOT 400, TRAIN 100", CTClient.SHOT_PULSE_INTERVAL_MS == 400
      and CTClient.TRAIN_PULSE_INTERVAL_MS == 100)
ct, seen = make()
s = ct.shot_prepare(16, trigger="sim", **BASE)
check("fire/shot default spacing is SHOT (400), timeout 900", s["pulse_interval_ms"] == 400
      and s["trigger_timeout_ms"] == 900, s)
for fn in ("mosfet_test", "mosfet_sweep", "emission_vs_heating", "measure_emission_pedestal",
           "emission_ramp", "emission_calibration", "simulate_sync", "fire_single_pulse",
           "shot_prepare", "measure_pulse_current"):
    params = inspect.signature(getattr(CTClient, fn)).parameters
    check(f"{fn} takes pulse_interval_ms", "pulse_interval_ms" in params)
check("emission_ramp: 30 pulses by default (30 x 100 ms = 3 s)",
      inspect.signature(CTClient.emission_ramp).parameters["num_pulses"].default == 30)
ct, seen = make()
r = ct.mosfet_test([1], inter_pulse_ms=400, pulse_interval_ms=400)
check("old + new name together refused", not r["ok"] and "not both" in r["error"], r)
got = {}
ct, seen = make()
ct._post = lambda path, body=None, timeout=None: (got.update(body or {}), {"ok": True})[1]
ct.simulate_sync(count=3, pulse_interval_ms=250)
check("simulate_sync(pulse_interval_ms) -> interval_ms on the wire", got.get("interval_ms") == 250.0, got)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
