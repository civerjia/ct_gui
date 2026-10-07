#!/usr/bin/env python3
"""An HV grid OFF that does not come back is never taken as done: the backend
forces the switch open, escalates to SHV_DISARM, and only answers ok once the
165 read-back AND the firmware's desired byte show it open. Offline: a fake
controller that models the RP2350's HV_SET_BIT / MULTI / DISARM / read-backs.

2026-10-06 12:58 the GUI focus leak scan closed P2 CH1.3, CH1.4, CH3.3 and
CH4.7, every OFF timed out on the slow link, the scan moved on, and the four
switches stayed closed for three hours -- HV on four filaments nobody asked for.

    python3 tests/test_hv_force_off.py
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


class FakeRp:
    """desired = the firmware's host-write state, hw = what the 595s drive.
    old_fw: SHV_DISARM clears the hardware but not `desired` (the bug fixed in
    the firmware the same day), so a later HV_SET_BIT RMW re-closes stale bits."""

    def __init__(self, old_fw=False):
        self.desired = [0] * 8
        self.hw = [0] * 8
        self.old_fw = old_fw
        self.drop = {}          # opcode -> how many frames of it to lose
        self.stuck = [0] * 8    # bits that stay closed whatever is written (welded)
        self.pio_owns = False   # HV_SET_* refused while the PIO owns the pins
        self.sent = []

    def hold_monitor(self, *_a, **_k):
        pass

    def _fb(self):
        return [h | s for h, s in zip(self.hw, self.stuck)]

    def request(self, op, payload=b"", flags=0, timeout=2.0):
        self.sent.append(op)
        if self.drop.get(op, 0) > 0:
            self.drop[op] -= 1
            raise TimeoutError(f"Timed out waiting for response to 0x{op:02X}")
        if op == 0x10:                                    # HV_SET_BIT ch,bit,val,mode
            if self.pio_owns:
                return {"raw": bytes([0x05])}
            ch, bit, val = payload[0], payload[1], payload[2]
            v = self.desired[ch]
            v = (v | (1 << bit)) if val else (v & ~(1 << bit) & 0xFF)
            self.desired[ch] = self.hw[ch] = v
            return {"raw": bytes([0, ch, self.desired[ch], self._fb()[ch]])}
        if op == 0x15:                                    # HV_SET_MULTI mask, values[8], mode
            if self.pio_owns:
                return {"raw": bytes([0x05])}
            for ch in range(8):
                if payload[0] & (1 << ch):
                    self.desired[ch] = self.hw[ch] = payload[1 + ch]
            return {"raw": bytes([0, payload[0], payload[0], 0]) + bytes(self.desired) + bytes(self._fb())}
        if op == 0x78:                                    # SHV_DISARM -> /SRCLR clear-all
            self.pio_owns = False
            self.hw = [0] * 8
            if not self.old_fw:
                self.desired = [0] * 8
            return {"raw": bytes([0])}
        if op == 0x14:
            return {"raw": bytes([0]) + bytes(self._fb())}
        if op == 0x13:
            return {"raw": bytes([0]) + bytes(self.desired) + bytes(self._fb())}
        raise AssertionError(f"unexpected opcode 0x{op:02X}")


SF.FORCE_OFF_DEADLINE_S = 1.5
body_off = {"command": "HV_SET_BIT", "channel": 0, "bit": 2, "value": 0, "force": True}
body_on = dict(body_off, value=1)

# 1. The 12:58 case: switch closed, its OFF frame lost.
rp = FakeRp()
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
rp.drop[0x10] = 1
r = S.hv_raw_write(rp, 2, "HV_SET_BIT", body_off, S.build_payload, 2.0)
check("lost OFF -> not ok (the caller is told)", r["ok"] is False, r)
check("lost OFF -> forced open and confirmed", r["forced_off"]["ok"] and rp.hw[0] == 0 and rp.desired[0] == 0, r)
check("lost OFF -> error names the switch", "CH1.3" in r["error"], r.get("error"))

# 2. An ON that timed out may land later -> also forced open.
rp = FakeRp()
rp.drop[0x10] = 1
r = S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
check("timed-out ON -> switch forced open", r["forced_off"]["ok"] and rp.hw == [0] * 8, r)

# 3. Link drops several frames in a row: keeps going, escalates to SHV_DISARM.
rp = FakeRp()
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
rp.drop[0x10] = 3
r = S.hv_raw_write(rp, 2, "HV_SET_BIT", body_off, S.build_payload, 2.0)
check("repeated losses -> still confirmed open", r["forced_off"]["ok"] and rp.hw[0] == 0, r)
check("repeated losses -> SHV_DISARM used", r["forced_off"]["disarmed"] and 0x78 in rp.sent, r)

# 4. PIO owns the pins (HV_SET_* refused): the disarm releases them.
rp = FakeRp()
rp.hw[3] = rp.desired[3] = 0x40
rp.pio_owns = True
r = S.force_grid_off(rp, 2, {3: 0x40})
check("PIO owns pins -> disarm, then confirmed open", r["ok"] and rp.hw[3] == 0, r)

# 5. Old firmware: disarm leaves `desired` stale; the zero-write after it clears it.
rp = FakeRp(old_fw=True)
rp.hw = [0x0C, 0, 0x04, 0x40, 0, 0, 0, 0]
rp.desired = list(rp.hw)
rp.drop[0x15] = 1
r = S.force_grid_off(rp, 2, None)
check("old fw stale desired -> all cleared", r["ok"] and rp.desired == [0] * 8 and rp.hw == [0] * 8, r)

# 6. A welded switch: never claims success, says which, says what to do.
rp = FakeRp()
rp.stuck[3] = 0x40
r = S.force_grid_off(rp, 2, None)
check("welded switch -> not ok", r["ok"] is False, r)
check("welded switch -> named, and told to cut HV",
      "CH4.7" in r.get("error", "") and "emission" in r.get("error", ""), r.get("error"))
check("welded switch -> reported still on", r.get("still_on") == {"3": 0x40}, r)

# 7. A clean OFF stays one frame, no extra round trips.
rp = FakeRp()
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
rp.sent.clear()
r = S.hv_raw_write(rp, 2, "HV_SET_BIT", body_off, S.build_payload, 2.0)
check("clean OFF -> ok, one frame", r["ok"] and rp.sent == [0x10], (r, rp.sent))

# 8. OFF answered OK but its own read-back still shows it closed -> forced.
rp = FakeRp()
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
rp.stuck[0] = 0x04
r = S.hv_raw_write(rp, 2, "HV_SET_BIT", body_off, S.build_payload, 2.0)
check("OK reply but read-back closed -> not ok", r["ok"] is False and "forced_off" in r, r)

# 9. Masks of raw writes.
check("masks: HV_SET_BIT", S.hv_write_off_masks("HV_SET_BIT", body_off) == {0: 0x04})
check("masks: MULTI", S.hv_write_off_masks("HV_SET_MULTI_CHANNEL", {"channel_mask": 0b101}) == {0: 0xFF, 2: 0xFF})
check("masks: not an HV write", S.hv_write_off_masks("CH_SET_POWER_STATE", {}) is None)

# 10. The dead-man watchdog sees switches closed by raw writes ...
import ct.server._access as A  # noqa: E402
SF._GRID_CLOSED.clear()
rp = FakeRp()
fid = S.MAPPING.filament_for_board(2, 0, 2)
key = int(fid) if fid is not None else -(1 + 2 * 64 + 0 * 8 + 2)
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
check("raw ON -> watchdog tracks the switch", key in SF._GRID_CLOSED, (key, SF._GRID_CLOSED))
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_off, S.build_payload, 2.0)
check("raw OFF -> watchdog drops it", key not in SF._GRID_CLOSED, SF._GRID_CLOSED)
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_on, S.build_payload, 2.0)
rp.drop[0x10] = 1
S.hv_raw_write(rp, 2, "HV_SET_BIT", body_off, S.build_payload, 2.0)
check("lost OFF, forced open -> watchdog drops it", key not in SF._GRID_CLOSED, SF._GRID_CLOSED)

# ... and when it fires, it opens them with the confirmed force-off, which
# also clears old firmware's stale desired byte.
class _Cl:
    connected = True
rp = FakeRp(old_fw=True)
rp.client = _Cl()
rp.hw = [0x0C, 0, 0x04, 0x40, 0, 0, 0, 0]
rp.desired = list(rp.hw)
saved = dict(S.CONTROLLERS)
S.CONTROLLERS.clear()
S.CONTROLLERS[2] = rp
try:
    SF._GRID_CLOSED.add(key)
    SF._safety_open_grid()
finally:
    S.CONTROLLERS.clear()
    S.CONTROLLERS.update(saved)
check("watchdog fires -> all open, desired cleared, tracking cleared",
      rp.hw == [0] * 8 and rp.desired == [0] * 8 and not SF._GRID_CLOSED, (rp.hw, rp.desired, SF._GRID_CLOSED))

# 11. The lease never stands between anyone and an open switch.
de = A.is_deenergising_post
check("lease: HV_SET_BIT 0 passes", de("/api/cmd", {"command": "HV_SET_BIT", "value": 0}))
check("lease: HV_SET_BIT 1 gated", not de("/api/cmd", {"command": "HV_SET_BIT", "value": 1}))
check("lease: MULTI all zero passes", de("/api/power-cmd", {"command": "HV_SET_MULTI_CHANNEL",
                                                            "channel_mask": 255, "values": [0] * 8}))
check("lease: MULTI with a 1 gated", not de("/api/cmd", {"command": "HV_SET_MULTI_CHANNEL",
                                                        "channel_mask": 1, "values": [4] + [0] * 7}))
check("lease: CHANNEL_BYTE 0 passes", de("/api/cmd", {"command": "HV_SET_CHANNEL_BYTE", "channel": 0, "value": 0}))
check("lease: hv-all-off passes", de("/api/hv-all-off", {}))

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
