#!/usr/bin/env python3
"""A grid switch that closes with no command is found within GRID_SWEEP_S and
forced open, whether or not a rail is coming on. Offline: fake controllers.

2026-10-27 11:37-11:53: the emission baseline stepped by whole diode-path
currents (+2, +6, +4 mA) with nothing commanding the grid, and the next HV
preflight found P1 CH4.1 closed with desired 0. Only the preflight was looking.

    python3 tests/test_grid_sweep.py
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
        return S.grid_sweep_once()
    finally:
        S.CONTROLLERS.clear()
        S.CONTROLLERS.update(saved)


# 1. The 11:49 case: P1 CH4.1 closed in hardware, desired 0 -> opened, recorded.
p1, p2 = Rp(hw=[0, 0, 0, 1, 0, 0, 0, 0]), Rp()
found = run({1: p1, 2: p2})
check("stray CH4.1 forced open", p1.fb() == [0] * 8, p1.fb())
check("...one record, names the switch", len(found) == 1 and found[0]["controller"] == 1
      and found[0]["switches"] == "CH4.1" and found[0]["opened"], found)
want = S.MAPPING.filament_for_board(0, 3, 0)
check("...and the filament, from P1's 0-based mapping", found and found[0]["filaments"] == [want]
      and want is not None, (found, want))
check("...and it is in the safety event log",
      any(e.get("kind") == "grid_stray_closed" for e in S._SAFETY_EVENTS), S._SAFETY_EVENTS[-1:])
check("P2 all open: nothing written", not any(op in (0x10, 0x15, 0x78) for op in p2.sent), p2.sent)

# 2. A switch the host commanded closed (desired 1) is left alone.
p = Rp(hw=[0, 0, 0, 1, 0, 0, 0, 0], desired=[0, 0, 0, 1, 0, 0, 0, 0])
found = run({1: p})
check("commanded closed -> not touched", not found and p.fb()[3] == 1
      and not any(op in (0x10, 0x15, 0x78) for op in p.sent), p.sent)

# 3. Armed / running: the PIO owns the chain -> skipped without reading the 165s.
for state in (1, 2):
    p = Rp(hw=[1] + [0] * 7, state=state)
    found = run({1: p})
    check(f"schedule state {state} -> skipped, no 165 read", not found and 0x14 not in p.sent
          and p.fb()[0] == 1, p.sent)

# 4. An unused channel (CH8, no boards) floats to 0xFF -> ignored.
p = Rp(hw=[0] * 7 + [0xFF])
found = run({1: p})
check("unused channel 0xFF -> ignored", not found and not any(op in (0x10, 0x15, 0x78) for op in p.sent),
      (found, p.sent))

# 5. Disconnected or silent controllers: no exception, nothing found.
found = run({1: Rp(up=False), 2: Rp(silent=True)})
check("disconnected / silent -> no record, no crash", found == [], found)

# 6. A switch that will not open is still recorded, with the error.
p = Rp(stuck=[0, 0, 0, 0, 0, 0x40, 0, 0])
found = run({1: p})
check("welded switch -> recorded, opened False, error", len(found) == 1 and not found[0]["opened"]
      and found[0].get("error"), found)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
