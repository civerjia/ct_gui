#!/usr/bin/env python3
"""FID -> USER_INDEX must load the filament order first (2026-10-15).

The order is loaded lazily, through filament_order, which _fid_of uses.
_user_index_of read the reverse map directly, so in a fresh client whose
FIRST translation was inbound -- the dead mask, as mosfet_test does before any
_fid_of -- every FID came back unchanged. The rig-wide cold MOSFET test then
skipped the good #22 / #62 (their USER numbers equal dead FIDs 6 / 50) and
sent the dead #6 / #50 to the backend, which refused them. Offline: a fake
backend with the 0-7 <-> 16-23, 48-59 <-> 60-71 order.

    python3 tests/test_order_lazy_reverse.py
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


ORDER = list(range(96))
ORDER[0:8], ORDER[16:24] = list(range(16, 24)), list(range(0, 8))
ORDER[48:60], ORDER[60:72] = list(range(60, 72)), list(range(48, 60))
DEAD = [6, 9, 26, 50, 73]                  # PHYSICAL, as the backend keeps it


def fresh():
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    calls = []

    def get(path, timeout=5.0):
        calls.append(path)
        if path.startswith("/api/filament-order"):
            return {"ok": True, "order": ORDER, "epoch": 1, "token": "t"}
        if path.startswith("/api/dead-fids"):
            return {"ok": True, "dead": {str(f): {} for f in DEAD}}
        return {"ok": False}
    ct._get = get
    return ct, calls


ct, calls = fresh()
users = sorted(ct._dead_users())           # FIRST translation is inbound
check("dead mask in user numbers on a fresh client: 22, 9, 26, 62, 73",
      users == sorted([22, 9, 26, 62, 73]), users)
check("...which loaded the order to do it", any(c.startswith("/api/filament-order") for c in calls), calls)
check("user 6 (FID 22, a good filament) is NOT dead", not ct._is_dead(6))
check("user 22 (FID 6, dead) IS dead", ct._is_dead(22))
check("user 50 (FID 62, good) is NOT dead; user 62 (FID 50) IS", not ct._is_dead(50) and ct._is_dead(62))

ct, _ = fresh()
check("_user_index_of on a fresh client translates", ct._user_index_of(6) == 22 and ct._user_index_of(62) == 50,
      (ct._user_index_of(6), ct._user_index_of(62)))
check("round trip user -> FID -> user", all(ct._user_index_of(ct._fid_of(u)) == u for u in range(96)))

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
