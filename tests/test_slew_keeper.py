#!/usr/bin/env python3
"""The backend keeps the rig's voltage-ramp slew (default 80 % of the
ceilings) on every controller. Offline: the controller is a fake that answers
CH_SLEW_RATE GET/SET like the firmware (SET answers with the values in force).

    python3 tests/test_slew_keeper.py
"""

import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeClient:
    connected = True

    def __init__(self, vals):
        self.vals = list(vals)
        self.writes = 0

    def send_request(self, ft, payload, flags=0, timeout=2.0):
        if payload:
            self.vals = [payload[i] | (payload[i + 1] << 8) for i in (0, 2, 4)]
            self.writes += 1
        raw = [0]
        for v in self.vals:
            raw += [v & 0xFF, v >> 8]
        return {"raw": raw}


class FakeLink:
    name = "Power 1"

    def __init__(self, vals):
        self.client = FakeClient(vals)


check("default target is 80 % of the ceilings", S.SLEW_WANT ==
      {"below_mV_per_s": 1600, "above_mV_per_s": 4000, "warm_mV_per_s": 4000}, str(S.SLEW_WANT))

link = FakeLink([400, 1000, 2800])                    # RP2350 just reset: firmware defaults
r = S._slew_check_one(link)
check("reset controller is written back to the rig's slew",
      link.client.vals == [1600, 4000, 4000] and r["after"] == S.SLEW_WANT, str(r))

r = S._slew_check_one(link)
check("already right: nothing written", link.client.writes == 1 and r["before"] == r["after"])

S._slew_note_set({"raw": [0, 0x20, 0x03, 0xD0, 0x07, 0xD0, 0x07]})   # someone SET 800/2000/2000
check("a SET through the API becomes the rig's slew",
      S.SLEW_WANT == {"below_mV_per_s": 800, "above_mV_per_s": 2000, "warm_mV_per_s": 2000})
S._slew_check_one(link)
check("...and is what gets kept from then on", link.client.vals == [800, 2000, 2000])

os.environ["CT_SLEW_PCT"] = "50"
check("CT_SLEW_PCT changes the default", S._slew_default_want()["above_mV_per_s"] == 2500)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
