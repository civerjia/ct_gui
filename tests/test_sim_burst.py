#!/usr/bin/env python3
"""The simulated trigger train goes out at exactly the spacing asked for, or not
at all. Offline: the ESP32's HTTP is a fake.

2026-10-27: the backend sent the spacing as an integer rate_hz, so only periods
that divide 1 s were exact -- the default 400 ms went out as 2 Hz = 500 ms, and
anything above 1 s as 1 Hz. A refused train was also dropped into _SIM_STATE
while /api/sync/simulate said ok, so the caller only saw its shot time out.

    python3 tests/test_sim_burst.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.protocol as P  # noqa: E402
import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Esp:
    """new=True: firmware with period_us. Records every form it was sent."""

    def __init__(self, new=True, min_period_us=25):
        self.new, self.min_period_us, self.sent = new, min_period_us, []

    def post(self, url, fields, timeout):
        self.sent.append(dict(fields))
        count = int(fields.get("count", 0))
        if self.new and "period_us" in fields:
            p = int(fields["period_us"])
            if p < self.min_period_us:
                return 400, f"period_us {p} < minimum {self.min_period_us} (sync_out_width_us + gap)"
            return 200, f'{{"ok":true,"count":{count},"period_us":{p}}}'
        rate = int(fields.get("rate_hz", 0))
        if count == 0 or rate == 0:
            return 400, "need count>0 & rate_hz>0"
        return 200, f'{{"ok":true,"count":{count},"rate_hz":{rate}}}'


def burst(esp, period_us):
    saved = P._http_post_form
    P._http_post_form = esp.post
    try:
        return P.sync_post_burst("esp", 5, period_us)
    finally:
        P._http_post_form = saved


# 1. New firmware: the period goes out as given, any value.
for period in (400_000, 1_500_000, 3_000):
    e = Esp()
    r = burst(e, period)
    check(f"new fw: {period} us sent as period_us", r["ok"] and e.sent == [{"count": "5", "period_us": str(period)}],
          (r, e.sent))

# 2. Old firmware, a period that divides 1 s: retried as the EXACT rate.
e = Esp(new=False)
r = burst(e, 50_000)
check("old fw: 50 ms -> rate_hz 20", r["ok"] and e.sent[-1] == {"count": "5", "rate_hz": "20"}, (r, e.sent))

# 3. Old firmware, a period it cannot make: refused, nothing fired at another spacing.
for period in (400_000, 1_500_000, 3_000):
    e = Esp(new=False)
    r = burst(e, period)
    check(f"old fw: {period} us refused, no rate_hz sent", not r["ok"] and "whole rate in Hz" in r["error"]
          and not any("rate_hz" in f for f in e.sent), (r, e.sent))

# 4. Too short for the pulse width: the firmware's reason comes back.
e = Esp(min_period_us=25)
r = burst(e, 10)
check("period below the width: refused with the minimum", not r["ok"] and "minimum 25" in r["error"], r)

# 5. Backend: interval_ms -> period_us; a refusal is the caller's error, and the
#    sim state is released (not left 'running').
check("_sim_period_us: 400 ms -> 400000 us, 0 -> 1 ms",
      S._sim_period_us(400) == 400_000 and S._sim_period_us(0) == 1000 and S._sim_period_us(0.25) == 250)

calls = []
saved = S.sync_post_burst
S.sync_post_burst = lambda host, count, period_us, timeout=3.0: (
    calls.append(period_us), {"ok": False, "status": 400, "error": "period_us 10 < minimum 25"})[1]
try:
    r = S._start_scan_sim("esp", 5, 0.01)
finally:
    S.sync_post_burst = saved
check("backend start: refusal returned with the ESP32's reason", not r["ok"] and calls == [10]
      and "minimum 25" in (S._SIM_STATE.get("start_error") or ""), (r, S._SIM_STATE))
with S._SIM_LOCK:
    S._SIM_STATE["running"] = True
S._sim_finish()
check("_sim_finish releases the sim", S._SIM_STATE["running"] is False)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
