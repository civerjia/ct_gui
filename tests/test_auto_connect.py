#!/usr/bin/env python3
"""Automatic discovery/connection of the controllers (auto_connect). Offline:
the LAN scan and the bridge links are fakes.

    python3 tests/test_auto_connect.py
"""

import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []
A, B, C = "10.0.0.1", "10.0.0.2", "10.0.0.3"   # B has the STM32
LAN = {}            # host -> {"has_stm32", "busy", "up"}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeClient:
    def __init__(self):
        self.connected = False


class FakeLink:
    def __init__(self, name):
        self.name = name
        self.client = FakeClient()
        self.host = None

    def connect(self, host):
        self.host = host
        self.client.connected = bool(LAN.get(host, {}).get("up", True))

    def disconnect(self):
        self.client.connected = False
        self.host = None

    def status(self):
        return {"connected": self.client.connected, "host": self.host}


S.scan_bridges = lambda: sorted(
    [{"host": h, "name": "CTPower-" + h[-1], "has_stm32": v["has_stm32"], "busy": v["busy"]}
     for h, v in LAN.items()], key=lambda r: (not r["has_stm32"], r["host"]))
S.invalidate_currents_cache = lambda c: None


def fresh(**lan):
    LAN.clear()
    LAN.update(lan)
    S.CONTROLLERS.clear()
    S.CONTROLLERS[1] = FakeLink("Power 1")
    S.CONTROLLERS[2] = FakeLink("Power 2")
    S._AUTO_MANUAL.clear()
    S._AUTO_OWNED.clear()
    S.MASTER = 2       # deliberately wrong, to see it corrected


def hosts():
    return S.CONTROLLERS[1].host, S.CONTROLLERS[2].host


ok = {"has_stm32": False, "busy": False}
stm = {"has_stm32": True, "busy": False}

fresh(**{A: ok, B: stm})
S.auto_connect()
check("both empty: STM32 board -> Power 1, other -> Power 2, master 1",
      hosts() == (B, A) and S.MASTER == 1, str(hosts()))

fresh(**{A: ok, B: stm, C: {"has_stm32": False, "busy": True}})
S.auto_connect()
check("a bridge held by another client is never taken", C not in hosts() and hosts() == (B, A), str(hosts()))

fresh(**{A: ok, B: stm})
S.CONTROLLERS[1].connect(A)
S._AUTO_MANUAL.add(1)                         # the user connected A by hand
S.auto_connect()
check("user's own connection kept; the free slot is filled, no swap", hosts() == (A, B), str(hosts()))

fresh(**{A: ok, B: stm})
S._AUTO_MANUAL.add(2)                         # the user disconnected Power 2
S.auto_connect()
check("background: a slot the user disconnected stays empty", hosts() == (B, None), str(hosts()))
S.auto_connect(explicit=True)
check("explicit Scan fills it again", hosts() == (B, A), str(hosts()))

fresh(**{A: {"has_stm32": False, "busy": False, "up": False}, B: stm})
r = S.auto_connect()
check("failed connection leaves the slot free and is reported",
      hosts() == (B, None) and r["failed"], str((hosts(), r["failed"])))

fresh(**{A: ok})
S.auto_connect()                              # only A visible yet -> Power 1
LAN[B] = stm
S.auto_connect()                              # B appears -> Power 2 -> swapped
check("STM32 board found later, both auto-connected: swapped into Power 1",
      hosts() == (B, A) and S.MASTER == 1, str(hosts()))

fresh()
r = S.auto_connect()
check("nothing on the LAN: ok, nothing connected", r["ok"] and hosts() == (None, None))

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
