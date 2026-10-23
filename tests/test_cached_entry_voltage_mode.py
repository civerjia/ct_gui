#!/usr/bin/env python3
"""0x3A cached entry: a VOLTAGE-mode board (mode 0) with a live measurement is
PRESENT and keeps its current (2026-10-23: 5 V on filament 0 read 1751 mA from
the firmware, None in the client, because present was `mode != 0`). STOP/SLEEP
boards carry the stale bit and stay absent; arrival bits decode in both.

    python3 tests/test_cached_entry_voltage_mode.py
"""
import _path  # noqa: F401
import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.server._reads import _cached_entry  # noqa: E402
from ct.client._power import _PowerMixin  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


e = _cached_entry(0, 0x10, 1751, 1200)
c = _PowerMixin._entry(e, 0, "cached")
check("voltage mode, live, settled -> present, current kept, arrival settled",
      e["present"] and c["current_mA"] == 1751.0 and c["arrival"] == "settled" and c["cc_mode"] == 0, (e, c))
e = _cached_entry(0, 0x80, 0, 0)
c = _PowerMixin._entry(e, 0, "cached")
check("mode 0 + stale (STOP/SLEEP: output off) -> not present, current None",
      not e["present"] and c["current_mA"] is None, (e, c))
e = _cached_entry(0, 0x40, 0, 0)
check("unavailable -> not present", not e["present"] and e["current_mA"] is None, e)
e = _cached_entry(0, 0x11, 1200, 1200)
check("current mode settled unchanged", e["present"] and e["current_mA"] == 1200 and e["arrival"] == "settled", e)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
