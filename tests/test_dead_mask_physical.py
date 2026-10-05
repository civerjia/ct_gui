#!/usr/bin/env python3
"""The dead mask is PHYSICAL: set_dead() blocks the same hardware filaments
whether set_filament_order() ran before or after it. 2026-10-02 18:21: the
same set_dead([6, 26, 81, 90]) called after a 64-entry order swap blocked
FIDs 22, 26, 63, 89 -- freeing the broken boards 6 and 90. Offline: a fake
backend stores the mask as the real one does (FIDs).

    python3 tests/test_dead_mask_physical.py
"""

import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct_simple_control import CTClient  # noqa: E402

FAILS = []
# The order combined_backend sets (USER_INDEX -> FID), from the 2026-10-02 log.
ORDER = [16, 17, 18, 19, 20, 21, 22, 23, 8, 9, 10, 11, 12, 13, 14, 15, 0, 1, 2, 3, 4, 5, 6, 7,
         24, 25, 26, 27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 40, 41, 42, 43, 44, 45, 46, 47,
         69, 70, 71, 48, 49, 50, 51, 52, 53, 54, 55, 56, 57, 58, 59, 60, 72, 73, 74, 75, 76, 77, 78, 79,
         80, 81, 82, 83, 84, 85, 86, 87, 88, 89, 90, 91, 92, 93, 94, 95, 61, 62, 63, 64, 65, 66, 67, 68]
BROKEN = [6, 26, 81, 90]


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def make():
    backend = {"dead": {}}
    ct = CTClient.__new__(CTClient)
    ct.record = False
    ct.client_id = "test"
    ct._dead_cache, ct._dead_fetched_at, ct._dead_stale = set(), 0.0, False
    ct._order, ct._order_rev, ct._order_epoch = {}, {}, None

    def get(path, timeout=5.0):
        return {"ok": True, "dead": dict(backend["dead"])}

    def post(path, body, timeout=10.0):
        fids = body["fids"]
        if body["op"] == "set":
            backend["dead"] = {str(f): {"reason": body.get("reason")} for f in fids}
        elif body["op"] == "add":
            backend["dead"].update({str(f): {"reason": body.get("reason")} for f in fids})
        else:
            for f in fids:
                backend["dead"].pop(str(f), None)
        return {"ok": True}

    ct._get, ct._post = get, post
    return ct, backend


def set_order(ct, seq):
    ct._adopt_order({"ok": True, "order": seq, "epoch": "e"})


# Order set BEFORE set_dead
ct1, b1 = make()
set_order(ct1, ORDER)
ct1.set_dead(BROKEN, reason="broken boards")
# Order set AFTER set_dead
ct2, b2 = make()
ct2.set_dead(BROKEN, reason="broken boards")
set_order(ct2, ORDER)
# And today's exact sequence: set_dead, set order, set_dead again
ct3, b3 = make()
ct3.set_dead(BROKEN, reason="init 1")
set_order(ct3, ORDER)
ct3.set_dead(BROKEN, reason="init 2")

for name, b in (("order before", b1), ("order after", b2), ("dead, order, dead again", b3)):
    got = sorted(int(f) for f in b["dead"])
    check(f"{name}: the backend blocks exactly the broken boards", got == BROKEN, str(got))

check("ct.dead reads back the physical numbers", sorted(ct3.dead) == BROKEN, str(sorted(ct3.dead)))
for fid in BROKEN:
    user = ORDER.index(fid)
    check(f"physical {fid} (user index {user}) is refused under the swap", ct3._is_dead(user))
check("a good filament whose user index equals a broken FID is NOT refused",
      not ct3._is_dead(6) and not ct3._is_dead(90))
live = ct3._live(None)
check("batch target list excludes the broken FIDs and nothing else",
      sorted(set(range(96)) - set(int(x) for x in live)) == BROKEN)

ct3.remove_dead(90)
check("remove_dead takes the physical number too", sorted(ct3.dead) == [6, 26, 81])
check("out-of-range number refused, nothing changed",
      ct3.set_dead([200])["ok"] is False and sorted(ct3.dead) == [6, 26, 81])

check("to_physical / to_user cross the order both ways",
      ct3.to_physical([22, 82]) == [6, 90] and ct3.to_user([6, 90]) == [22, 82])

# An error from the backend names the PHYSICAL filament; the script must read
# its own number (2026-10-05 11:04: active_one(63) came back "filament 52 ...").
ct4, _ = make()
set_order(ct4, ORDER)
user = ORDER.index(52)
ct4._ensure_keepalive = lambda *a, **k: None
ct4._post = lambda path, body, timeout=10.0: {
    "ok": False, "ladder_blocked": True, "filament": body["filament"],
    "error": f"filament {body['filament']} may not go to ACTIVE: not warm yet"}
r = ct4._state_one(user, 5, 2600, "active")
check("single-filament error is in the script's numbering, with the physical id",
      r["error"].startswith(f"filament {user} (physical 52) may not go"), r["error"])
check("...and the result still names the script's filament", r["filament"] == user)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
