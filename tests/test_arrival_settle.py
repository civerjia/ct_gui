#!/usr/bin/env python3
"""Arrival settle time (RP2350 fw 903cd23+, CH_ARRIVAL_SETTLE 0x83): the
firmware reports ACTIVE/VOLTAGE arrival 0x30 "settling" until the heating has
held arrival for settle_ms, then "settled". fire_single_pulse(settle_ms=)
writes it, waits for "settled" as before (heat_timeout_s + settle), and sets
it back to 0. Offline: hardware calls recorded, time.sleep a no-op.

    python3 tests/test_arrival_settle.py
"""
import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402
from ct.server._reads import _cached_entry  # noqa: E402
from ct.server._wire import build_payload  # noqa: E402
from ct.server._access import is_read_post  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


time.sleep = lambda s: None

# 1. Decode + wire.
check("0x30 decodes as settling", _cached_entry(0, 0x31, 2500, 2500)["arrival"] == "settling")
check("0x10 still settled", _cached_entry(0, 0x11, 2500, 2500)["arrival"] == "settled")
ft, _, pl = build_payload("CH_ARRIVAL_SETTLE", {"settle_ms": 1500})
check("SET frame 0x83 + u16", ft == 0x83 and pl == bytes([0xDC, 0x05]), (ft, pl))
ft, _, pl = build_payload("CH_ARRIVAL_SETTLE", {})
check("GET frame 0x83 empty", ft == 0x83 and pl == b"")
check("GET is read-only for the lease",
      is_read_post("/api/cmd", {"command": "CH_ARRIVAL_SETTLE"}))
check("SET is not read-only",
      not is_read_post("/api/cmd", {"command": "CH_ARRIVAL_SETTLE", "settle_ms": 5}))


def make(seq, settle_ok=True):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    calls, seen = [], {"settle": []}

    def set_arrival_settle(ms, controller=None):
        seen["settle"].append(ms)
        return {"ok": True, "settle_ms": ms} if settle_ok else {
            "ok": False, "error": "this RP2350 firmware has no arrival settle time"}

    def fire_core(filament, **k):
        calls.append("setup")
        try:
            k["on_armed"]()
        except BaseException as exc:
            calls.append("disarm")
            return {"ok": False, "fired": 0, "error": f"on_armed raised ({exc})"}
        calls.append("trigger")
        k["after_fire"]()
        return {"ok": True, "fired": 1, "records": [{}]}

    ct.set_arrival_settle = set_arrival_settle
    ct.voltage_one = lambda f, mv, **k: calls.append("voltage") or {"ok": True}
    ct.idle_one = lambda f, ma, **k: calls.append("idle") or {"ok": True}
    ct._fire_core = fire_core
    seq = list(seq)

    def cached_read(f):
        calls.append("poll:" + seq[0]["arrival"])
        return {f: seq.pop(0) if len(seq) > 1 else seq[0]}
    ct.read_filament_current_cached = cached_read
    return ct, calls, seen


KW = dict(width_us=7000, pulse_interval_ms=100, max_on_ms=165, total_ms=6000)
S = lambda a: {"current_mA": 2500, "cc_mode": 0, "arrival": a}

# 2. VOLTAGE: settling is waited out, trigger only after settled, settle restored.
ct, calls, seen = make([S("ramping"), S("settling"), S("settling"), S("settled")])
r = ct.fire_single_pulse(3, active_mv=8000, idle_ma=1200, settle_ms=1500,
                         heat_timeout_s=5.0, **KW)
check("voltage + settle: fired", r.get("ok"), r)
polls = [c for c in calls if c.startswith("poll")]
check("trigger only after 'settled'", polls[-1] == "poll:settled"
      and calls.index("trigger") > calls.index("poll:settled"), calls)
check("settle written then set back to 0", seen["settle"] == [1500, 0], seen)
check("result carries settle_ms", r.get("settle_ms") == 1500, r)

# 3. Still settling at the deadline -> not fired, and the reason says so.
ct, calls, seen = make([S("settling")])
r = ct.fire_single_pulse(3, active_mv=8000, idle_ma=1200, settle_ms=1000,
                         heat_timeout_s=0.0, **KW)
check("settling at deadline: not fired", not r.get("ok") and "trigger" not in calls, calls)
check("settle restored after a failed shot", seen["settle"] == [1000, 0], seen)

# 4. Refusals: no heating; out of range; old firmware.
ct, calls, seen = make([S("settled")])
r = ct.fire_single_pulse(3, settle_ms=500, **KW)
check("settle_ms without heating refused", not r.get("ok") and "needs active_ma" in r["error"]
      and not calls and not seen["settle"], r)
r = ct.fire_single_pulse(3, active_mv=8000, idle_ma=1200, settle_ms=70000, **KW)
check("settle_ms out of range refused", not r.get("ok") and "out of range" in r["error"], r)
ct, calls, seen = make([S("settled")], settle_ok=False)
r = ct.fire_single_pulse(3, active_mv=8000, idle_ma=1200, settle_ms=500, **KW)
check("old firmware: not fired, says why", not r.get("ok") and "no arrival settle" in r["error"]
      and not calls, r)

# 5. Without settle_ms nothing is written (no extra link traffic, old fw fine).
ct, calls, seen = make([S("settled")])
r = ct.fire_single_pulse(3, active_mv=8000, idle_ma=1200, **KW)
check("no settle_ms: fired, settle untouched", r.get("ok") and seen["settle"] == [], (r, seen))

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
raise SystemExit(1 if FAILS else 0)
