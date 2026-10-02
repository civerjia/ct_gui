#!/usr/bin/env python3
"""The backend gives a controller back the rig's trigger delay when an RP2350
reset zeroed it, and refuses to arm in every other disagreement. Offline: the
controllers are fakes holding a delay value.

    python3 tests/test_trigger_delay_restore.py
"""

import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._schedule as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeClient:
    connected = True


class FakeLink:
    def __init__(self, us, readable=True):
        self.client = FakeClient()
        self.us = us
        self.readable = readable


def fake_one(link, delay_us=None):
    if not link.readable:
        return {"ok": False, "error": "no valid trigger-delay reply"}
    if delay_us is not None:
        link.us = int(delay_us)
    return {"ok": True, "delayUs": link.us, "applies": True}


S._trigger_delay_one = fake_one


def rig(*links):
    S.CONTROLLERS.clear()
    for i, l in enumerate(links, 1):
        S.CONTROLLERS[i] = l
    return links


# A SET is remembered.
S.TRIGGER_DELAY_WANT["us"] = None
c1, c2 = rig(FakeLink(0), FakeLink(0))
check("set writes all", S.trigger_delay_all(3000)["ok"] and c1.us == c2.us == 3000)
check("set is remembered", S.TRIGGER_DELAY_WANT["us"] == 3000)

# One board reset -> restored, arm allowed.
c1.us = 0
check("one zeroed board: arm allowed", S.trigger_delay_mismatch() is None)
check("one zeroed board: value restored", c1.us == 3000 and c2.us == 3000, (c1.us, c2.us))

# Whole rig power-cycled (both zero) -> restored.
c1.us, c2.us = 0, 0
check("both zeroed: arm allowed", S.trigger_delay_mismatch() is None)
check("both zeroed: restored", c1.us == c2.us == 3000)

# Someone set a different non-zero value on one board: NOT overwritten, refused.
c1.us, c2.us = 1000, 3000
why = S.trigger_delay_mismatch()
check("different non-zero value: refused", why and "disagree" in why, str(why))
check("different non-zero value: left alone", c1.us == 1000 and c2.us == 3000)

# A read failure is not a reset signature: refused, nothing written.
c1.us, c2.us = 0, 3000
c2.readable = False
why = S.trigger_delay_mismatch()
check("read failure: refused", why and "could not be read" in why, str(why))
check("read failure: nothing written", c1.us == 0)
c2.readable = True

# Backend restarted (nothing remembered): first consistent non-zero read fills it.
S.TRIGGER_DELAY_WANT["us"] = None
c1.us, c2.us = 3000, 3000
check("consistent read: arm allowed", S.trigger_delay_mismatch() is None)
check("consistent read remembered", S.TRIGGER_DELAY_WANT["us"] == 3000)
c2.us = 0
check("then a reset is restored", S.trigger_delay_mismatch() is None and c2.us == 3000)

# Nothing remembered and a board at 0: no basis to restore -> refused as before.
S.TRIGGER_DELAY_WANT["us"] = None
c1.us, c2.us = 0, 3000
why = S.trigger_delay_mismatch()
check("nothing remembered: refused, not guessed", why and "disagree" in why and c1.us == 0, str(why))

# Deliberate SET to 0 is honoured (not 'restored' back).
S.trigger_delay_all(0)
check("set to 0 remembered as 0", S.TRIGGER_DELAY_WANT["us"] == 0)
check("set to 0: arm allowed, stays 0", S.trigger_delay_mismatch() is None and c1.us == c2.us == 0)

# Single controller: a reset is restored too.
(solo,) = rig(FakeLink(3000))
S.trigger_delay_all(3000)
solo.us = 0
check("single controller: allowed and restored", S.trigger_delay_mismatch() is None and solo.us == 3000)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
