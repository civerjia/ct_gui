#!/usr/bin/env python3
"""Per-channel I2C clock (RP2350 0x65/0x66): the backend keeps what was SET and
re-applies it after an RP2350 reset (the firmware does not save it). Offline:
the link is a fake RP2350.

    python3 tests/test_i2c_hz.py
"""
import _path  # noqa: F401

import os
import tempfile
from pathlib import Path

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeRp:
    """Firmware model: 8 channels at 400 kHz; 0x65 GET, 0x66 SET (0 = default)."""
    def __init__(self, supported=True):
        self.hz = [400000] * 8
        self.supported = supported
        self.sets = []

    def send_request(self, ft, payload, flags=0, timeout=2.0):
        if not self.supported:
            return {"status": "UNSUPPORTED", "raw": [0x0E]}
        ch = payload[0]
        if ft == 0x66:
            hz = int.from_bytes(payload[1:5], "little") or 400000
            if not 20000 <= hz <= 400000:
                return {"status": "BAD_ARGUMENT", "raw": [0x04]}
            self.hz[ch] = hz
            self.sets.append((ch, hz))
        return {"status": "OK", "raw": [0, ch] + list(self.hz[ch].to_bytes(4, "little"))
                + list((400000).to_bytes(4, "little"))}

    def reset(self):
        self.hz = [400000] * 8


class Link:
    def __init__(self, rp):
        self.client, self.name = rp, "Power 2"


S.I2C_HZ_STATE_PATH = Path(tempfile.mkdtemp()) / "i2c_hz.json"
S.I2C_HZ_WANT.clear()
rp = FakeRp()
link = Link(rp)

ft, fl, pl = S.build_payload("CH_SET_I2C_HZ", {"channel": 2, "hz": 100000})
check("encode: 0x66 [ch, hz u32 LE]", ft == 0x66 and pl == bytes([2]) + (100000).to_bytes(4, "little"), pl)
resp = rp.send_request(ft, pl, fl)
S._i2c_hz_note_set(2, {"channel": 2, "hz": 100000}, resp)
check("SET is kept and saved", S.I2C_HZ_WANT == {"2": {"2": 100000}}
      and S._i2c_hz_load() == {"2": {"2": 100000}}, S.I2C_HZ_WANT)

rp.sets.clear()
check("nothing to do while it matches", S._i2c_hz_check_one(2, link) == [] and not rp.sets)
rp.reset()                                   # an RP2350 reset: back at 400 kHz
done = S._i2c_hz_check_one(2, link)
check("after a reset: re-applied", done == [(2, 400000, 100000)] and rp.hz[2] == 100000, done)
check("other controller untouched", S._i2c_hz_check_one(1, Link(FakeRp())) == [])

ft, fl, pl = S.build_payload("CH_SET_I2C_HZ", {"channel": 2, "hz": 0})
S._i2c_hz_note_set(2, {"channel": 2, "hz": 0}, rp.send_request(ft, pl, fl))
check("SET 0 (default) forgets it", S.I2C_HZ_WANT == {"2": {}} and rp.hz[2] == 400000, S.I2C_HZ_WANT)

S._i2c_hz_note_set(2, {}, {"status": "BAD_ARGUMENT", "raw": [0x04]})
check("a refused SET changes nothing", S.I2C_HZ_WANT == {"2": {}})

S.I2C_HZ_WANT["2"] = {"2": 100000}
old = Link(FakeRp(supported=False))
check("old firmware: skipped, no exception", S._i2c_hz_check_one(2, old) == []
      and old in S._I2C_HZ_UNSUPPORTED)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
