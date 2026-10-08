#!/usr/bin/env python3
"""No rail comes on with a grid switch closed. Before emission/focus ON the
backend reads every controller's switches for real, forces any closed one open
and confirms it, and refuses the rail when it cannot.

2026-10-07 10:16: after a power cycle P1's CH1.1, CH1.2 and CH5.1 read CLOSED
with the firmware's desired byte 0; emission came on, three unrelated
sub-boards conducted (+6 mA) and focus could not rise.

    python3 tests/test_hv_preflight.py
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


class _Cl:
    def __init__(self, up=True):
        self.connected = up


class Rp:
    """desired = firmware's byte, hw = the 595 outputs, stuck = welded bits."""

    def __init__(self, hw=None, desired=None, stuck=None, state=0, silent=False, up=True):
        self.hw = list(hw or [0] * 8)
        self.desired = list(desired or [0] * 8)
        self.stuck = list(stuck or [0] * 8)
        self.state, self.silent = state, silent
        self.client = _Cl(up)
        self.sent = []

    def hold_monitor(self, *a, **k):
        pass

    def fb(self):
        return [h | s for h, s in zip(self.hw, self.stuck)]

    def request(self, op, payload=b"", flags=0, timeout=2.0):
        self.sent.append(op)
        if self.silent:
            raise TimeoutError(f"Timed out waiting for response to 0x{op:02X}")
        if op == 0x79:
            raw = bytearray(70)
            raw[1] = self.state
            return {"raw": bytes(raw)}
        if op == 0x14:
            return {"raw": bytes([0]) + bytes(self.fb())}
        if op == 0x13:
            return {"raw": bytes([0]) + bytes(self.desired) + bytes(self.fb())}
        if op == 0x10:
            ch, bit, val = payload[0], payload[1], payload[2]
            v = (self.desired[ch] | (1 << bit)) if val else (self.desired[ch] & ~(1 << bit) & 0xFF)
            self.desired[ch] = self.hw[ch] = v
            return {"raw": bytes([0, ch, v, self.fb()[ch]])}
        if op == 0x15:
            for ch in range(8):
                if payload[0] & (1 << ch):
                    self.desired[ch] = self.hw[ch] = payload[1 + ch]
            return {"raw": bytes([0])}
        if op == 0x78:
            self.hw = [0] * 8
            self.desired = [0] * 8
            return {"raw": bytes([0])}
        raise AssertionError(hex(op))


SF.FORCE_OFF_DEADLINE_S = 1.0
saved = dict(S.CONTROLLERS)


def run(rigs):
    S.CONTROLLERS.clear()
    S.CONTROLLERS.update(rigs)
    try:
        return S.hv_preflight_switches("test")
    finally:
        S.CONTROLLERS.clear()
        S.CONTROLLERS.update(saved)


# 1. The 10:16 case: switches closed in hardware, desired 0 -> opened, rail allowed.
p1 = Rp(hw=[3, 0, 0, 0, 1, 0, 0, 0])
r = run({1: p1, 2: Rp()})
check("closed switches opened, rail allowed", r["ok"] and p1.fb() == [0] * 8, r)
check("...and reported which", r["opened"].get("1", "").startswith("CH1.1, CH1.2") and "CH5.1" in r["opened"]["1"], r)

# 2. All open: one status + two reads per controller, nothing written.
a, b = Rp(), Rp()
r = run({1: a, 2: b})
check("all open -> ok, nothing opened", r["ok"] and not r["opened"], r)
check("all open -> no write sent", not any(op in (0x10, 0x15, 0x78) for op in a.sent + b.sent), a.sent)

# 3. A welded switch -> rail refused, named.
# (CH6: a used channel -- CH7/CH8 carry no boards and are not checked.)
r = run({1: Rp(stuck=[0, 0, 0, 0, 0, 0x40, 0, 0])})
check("welded switch -> HV refused", not r["ok"] and "HV NOT turned on" in r["error"] and "CH6.7" in r["error"], r)

# 4. Unreadable controller -> refused (fail closed).
r = run({1: Rp(silent=True)})
check("unreadable -> HV refused", not r["ok"] and "unreadable" in r["error"], r)

# 5. Disconnected / running controllers are listed, not silently passed.
r = run({1: Rp(), 2: Rp(up=False)})
check("disconnected -> allowed but listed unchecked", r["ok"] and any("Power 2" in u for u in r["unchecked"]), r)
run_rp = Rp(hw=[1] + [0] * 7, desired=[1] + [0] * 7, state=2)
r = run({1: run_rp})
check("running schedule -> not touched, listed", r["ok"] and run_rp.fb()[0] == 1
      and any("running" in u for u in r["unchecked"]), r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
