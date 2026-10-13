#!/usr/bin/env python3
"""Output-voltage cap (0x3B CH_VOLTAGE_CAP): wire format, the backend keeping
it after an RP2350 reset (13 V default), and the client reading it back.
2026-10-13: the user lowered the rig's cap from 13 V to 10 V. Offline.

    python3 tests/test_voltage_cap.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._server as srv  # noqa: E402
from ct.server._wire import build_payload  # noqa: E402
from ct.server._access import is_read_post as _is_read_post  # noqa: E402
from ct.client._client import CTClient  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


check("GET is an empty 0x3B", build_payload("CH_VOLTAGE_CAP", {}) == (0x3B, 0, b""))
check("SET 10000 is 0x3B + LE16", build_payload("CH_VOLTAGE_CAP", {"millivolts": 10000})
      == (0x3B, 0, (10000).to_bytes(2, "little")))
check("GET counts as a read (no lease needed), SET does not",
      _is_read_post("/api/cmd", {"command": "CH_VOLTAGE_CAP"})
      and not _is_read_post("/api/cmd", {"command": "CH_VOLTAGE_CAP", "millivolts": 10000}))


class Fake:
    def __init__(self, cap):
        self.cap, self.sent = cap, []

    def send_request(self, ft, payload, flags=0, timeout=2.0):
        self.sent.append(bytes(payload))
        if payload:
            self.cap = int.from_bytes(payload, "little")
        return {"raw": [0, self.cap & 0xFF, self.cap >> 8, 0xE0, 0x2E]}   # ceiling 12000


class Link:
    def __init__(self, cap):
        self.client = Fake(cap)


srv.VCAP_WANT["mv"] = 10000
l = Link(13000)                          # an RP2350 that just reset
r = srv._vcap_check_one(l)
check("keeper: a reset controller (13000) is written back to 10000",
      r == {"before": 13000, "after": 10000} and l.client.cap == 10000, r)
l = Link(10000)
r = srv._vcap_check_one(l)
check("keeper: already right -> only read, nothing written",
      r["before"] == r["after"] == 10000 and l.client.sent == [b""], (r, l.client.sent))
srv._vcap_note_set({"raw": [0, 0x28, 0x23, 0, 0]})          # 9000 set through /api/cmd
check("a SET through /api/cmd becomes the kept value", srv.VCAP_WANT["mv"] == 9000)
srv.VCAP_WANT["mv"] = 10000

ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
ct._connected_controllers = lambda: [1, 2]
sent = []


def post(path, body, timeout=None):
    sent.append(body)
    mv = body.get("millivolts", 10000)
    mv = min(mv, 12000)
    return {"ok": True, "response": {"raw": [0, mv & 0xFF, mv >> 8, 0xE0, 0x2E]}}
ct._post = post
r = ct.set_voltage_cap(10000)
check("client: set on both controllers, value in force read back",
      r["ok"] and r["mv"] == 10000 and r["ceiling_mv"] == 12000
      and [b["controller"] for b in sent] == [1, 2], (r, sent))
r = ct.set_voltage_cap(15000, controller=1)
check("client: above the ceiling -> clamped, said", r["mv"] == 12000 and r.get("clamped"), r)
r = ct.get_voltage_cap()
check("client: get", r["ok"] and r["mv"] == 10000, r)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
