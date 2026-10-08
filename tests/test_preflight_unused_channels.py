#!/usr/bin/env python3
"""HV preflight ignores channels with no boards, using the REAL mapping.

2026-10-08: after a power-up P2 CH8 (no boards, 165 inputs floating) read
0xFF and every emission ON was refused. The first fix looked the mapping up
in the wrong module, fell back to "all eight channels", and kept refusing --
its test had replaced used_channel_mask, so it never saw that. This test
patches only the hardware reads.

    python3 tests/test_preflight_unused_channels.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402
import ct.server._safety as SF  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Cl:
    connected = True


class Link:
    client = Cl()

    def hold_monitor(self, *a, **k):
        pass

    def request(self, *a, **k):
        return {"status_code": 0, "raw": [0]}


check("the real mapping uses CH1-6 on both controllers",
      S.used_channel_mask(1) == 0x3F and S.used_channel_mask(2) == 0x3F,
      (hex(S.used_channel_mask(1)), hex(S.used_channel_mask(2))))

S.CONTROLLERS.clear()
S.CONTROLLERS[2] = Link()
S.shv_status_retry = lambda link: {"state": 0}
reads = {"v": ([0] * 8, [0] * 7 + [255])}
S._hv_bytes = SF._hv_bytes = lambda link: reads["v"]
SF.FORCE_OFF_DEADLINE_S = 0.3

r = S.hv_preflight_switches("test")
check("CH8 floating at 0xFF -> HV allowed", r.get("ok"), r)

reads["v"] = ([0] * 8, [0, 4, 0, 0, 0, 0, 0, 255])
r = S.hv_preflight_switches("test")
check("a closed switch on CH2 still refuses HV", not r.get("ok") and "CH2.3" in r.get("error", ""), r)

reads["v"] = ([0] * 8, [0] * 7 + [255])
f = SF.force_grid_off(Link(), 2, None, reason="test")
check("whole-controller force-off is judged on used channels only", f.get("ok"), f)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
