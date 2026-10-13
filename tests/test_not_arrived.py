#!/usr/bin/env python3
"""wait_for_current: when a current does not arrive, say what the board DID.

2026-10-13, filament 77 (P1 CH6.2) at ACTIVE 2700, measured on the rig: the
current climbed to ~2.6 A at 7.5 V, then the board's output fell to 0 V / 5 mA
and was restarted, over and over. The firmware reported CAPPED (its voltage
SETPOINT was at the cap) and the client said "raise the cap" -- the opposite of
the truth. Offline: the CC-loop cache and the board monitor are fakes.

    python3 tests/test_not_arrived.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make(seq, board_mv, fresh=True):
    """seq: [(current_mA, arrival)] the cache returns poll by poll (the last
    repeats). board_mv: what the board monitor shows for the output."""
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    it = {"i": 0}

    def cached(f):
        ma, arr = seq[min(it["i"], len(seq) - 1)]
        it["i"] += 1
        return {f: {"current_mA": ma, "arrival": arr, "cc_mode": 1, "present": True}}
    ct.read_filament_current_cached = cached
    ct.filament_to_board = lambda f: {"controller": 1, "slot": 41, "channel": 5, "position": 1}
    ct._fid_of = lambda f: f

    def get(path, timeout=None, **k):
        if path.startswith("/api/board-snapshot"):
            return {"ok": True, "fresh": fresh,
                    "boards": [{"fid": 77, "label": "CH6.2", "bus_mV": board_mv}]}
        return {"ok": True, "struggling": {}}       # /api/tps-struggling
    ct._get = get
    return ct


# 1. The 77 case: climbed to 2609, output to 0 V, firmware says capped.
ct = make([(1193, "ramping"), (2071, "ramping"), (2609, "ramping"), (5, "capped")], 0)
h = ct.wait_for_current(77, 2700, 150, 2.0, poll_interval_s=0.01)
check("output off: not ok, output_off, not 'capped'",
      not h["ok"] and h.get("output_off") and h.get("capped") is False, h)
check("...says SWITCHED OFF, the peak, the voltage, and NOT the cap",
      "SWITCHED OFF" in h["error"] and "2609 mA" in h["error"] and "output 0 mV" in h["error"]
      and "NOT the voltage cap" in h["error"] and "P1 CH6.2" in h["error"], h["error"])

# 2. Same, but the monitor is stale (schedule armed): the current alone tells it.
ct = make([(1193, "ramping"), (2609, "ramping"), (5, "capped")], 0, fresh=False)
h = ct.wait_for_current(77, 2700, 150, 2.0, poll_interval_s=0.01)
check("output off, voltage unreadable: still diagnosed from the current",
      h.get("output_off") and "not readable" in h["error"] and "2609 mA" in h["error"], h)

# 3. Genuinely at the voltage cap (output high, current short of target): keep CAPPED.
ct = make([(1193, "ramping"), (2300, "ramping"), (2410, "capped")], 13000)
h = ct.wait_for_current(77, 2700, 150, 2.0, poll_interval_s=0.01)
check("real cap: capped kept, output shown",
      h.get("capped") is True and not h.get("output_off") and "CAPPED" in h["error"]
      and "output 13000 mV" in h["error"], h)

# 4. Output up, almost no current from the start: OPEN.
ct = make([(5, "ramping")], 9000)
h = ct.wait_for_current(77, 2700, 150, 0.1, poll_interval_s=0.01)
check("open: output up, ~0 mA -> OPEN", "OPEN" in (h.get("error") or ""), h)

# 5. Just slow (still ramping at the deadline): a plain, informative timeout.
ct = make([(1500, "ramping"), (2200, "ramping")], 6000)
h = ct.wait_for_current(77, 2700, 150, 0.1, poll_interval_s=0.01)
check("slow: says not reached, current, best, output, loop state",
      not h["ok"] and "not reached" in h["error"] and "2200 mA" in h["error"]
      and "output 6000 mV" in h["error"] and "ramping" in h["error"] and not h.get("output_off"), h)

# 6. Arrived: untouched (no error, no board read needed).
ct = make([(2690, "settled")], 7600)
h = ct.wait_for_current(77, 2700, 150, 1.0, poll_interval_s=0.01)
check("arrived: ok, no error", h["ok"] and "error" not in h, h)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
