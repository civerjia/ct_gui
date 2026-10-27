#!/usr/bin/env python3
"""ct.firmware_info() / ct.firmware_selftest(): RP2350 self-test over the
bridge, no USB (2026-10-14). GET_BOOT_INFO (0x64, fw 98086b2+) gives the boot
guard record and the git build; the rest is vcap / slew / pulse-record size.
Offline: the /api/cmd replies are fakes.

    python3 tests/test_firmware_selftest.py
"""
import _path  # noqa: F401

import os
import struct

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.client._client import CTClient  # noqa: E402
from ct.server._wire import build_payload  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def boot_raw(fails=0, prev=0x64, stage=0x64, pc=0, up_ms=120000, git=b"98086b2d"):
    return list(bytes([0, fails, prev, stage]) + struct.pack("<II", pc, up_ms) + git.ljust(12, b"\0"))


def make(boot=None, vcap=9500, slew=(1000, 4000, 4000), records=None, ctrls=(1, 2), rb=24):
    ct = CTClient("127.0.0.1", client_id="test", _local=True, keepalive=False)
    ct._connected_controllers = lambda: list(ctrls)
    boot = boot or {}

    def diag(c, command, body=None, timeout=10.0):
        b = boot.get(c, boot_raw())
        if b == "old":
            return None, "GET_BOOT_INFO: firmware status 0x0E not supported by this firmware"
        return b, None
    ct._diag_cmd = diag
    ct._voltage_cap_one = lambda c, mv: {"ok": True, "controller": c, "mv": vcap, "ceiling_mv": 15000}
    ct._get_slew_rates_one = lambda c=1: {"ok": True, "below_mV_per_s": slew[0],
                                          "above_mV_per_s": slew[1], "warm_mV_per_s": slew[2]}
    ct._shv = lambda c, body: {"ok": True, "records": (records or {}).get(c, []),
                               "record_bytes": rb if (records or {}).get(c) else None}
    return ct


check("GET_BOOT_INFO is a read, type 0x64, empty", build_payload("GET_BOOT_INFO", {}) == (0x64, 0, b""))

ct = make()
fi = ct.firmware_info(1)["controllers"]["1"]
check("firmware_info decodes git / stage / uptime", fi["git"] == "98086b2d" and fi["stage"] == 0x64
      and fi["stage_name"].startswith("stable") and fi["uptime_s"] == 120.0 and fi["fault_pc"] is None, fi)

r = make(records={1: [{"filament": 3}]}).firmware_selftest()
check("healthy rig: ok, no problems", r["ok"] and not r["problems"], r["problems"])
check("empty log is 'not judged' (ok None), not a failure",
      any(x["check"] == "pulse_log" and x["ok"] is None for x in r["checks"]), r["checks"])

r = make(boot={2: boot_raw(fails=2, prev=12, pc=0x10012345)}).firmware_selftest()
check("failed boots + hardfault on P2 reported, named",
      not r["ok"] and any("P2 boot" in p and "2 failed boot" in p and "0x10012345" in p
                          for p in r["problems"]), r["problems"])

r = make(boot={1: boot_raw(stage=11, up_ms=60000)}).firmware_selftest()
check("boot not stable after 60 s: failure, says how far it got",
      any("stage 0xB I2C done" in p for p in r["problems"]), r["problems"])
r = make(boot={1: boot_raw(stage=12, up_ms=17500)}).firmware_selftest()
check("just rebooted (17.5 s): 'still booting', not a failure",
      r["ok"] and any(x["check"] == "boot" and x["ok"] is None and "still booting" in x["detail"]
                      for x in r["checks"]), r)

r = make(boot={1: "old", 2: "old"}).firmware_selftest()
check("old firmware: boot check unsupported (ok None), not a failure",
      r["ok"] and all(x["ok"] is None for x in r["checks"] if x["check"] == "boot"), r)

ct = make()
ct._diag_cmd = lambda c, command, body=None, timeout=10.0: (None, "unknown command GET_BOOT_INFO")
r = ct.firmware_selftest(controller=1)
check("old backend: boot check unsupported, says the backend is old",
      r["ok"] and any(x["ok"] is None and "backend predates" in x["detail"] for x in r["checks"]), r)

r = make(boot={2: boot_raw(git=b"5051982a")}).firmware_selftest()
check("different builds on P1/P2 flagged", any("builds" in p for p in r["problems"]), r["problems"])
r = make(boot={1: boot_raw(git=b"98086b2d+"), 2: boot_raw(git=b"98086b2d+")}).firmware_selftest()
check("dirty build flagged", any("uncommitted" in p for p in r["problems"]), r["problems"])

r = make(vcap=13000, slew=(400, 1000, 2800)).firmware_selftest(controller=1)
check("wrong vcap and slew both reported", any("vcap" in p for p in r["problems"])
      and any("slew" in p for p in r["problems"]), r["problems"])

ct = make(records={1: [{"filament": 3, "heat_bus_unavailable": "firmware_no_voltage"}]})
ct._shv = lambda c, body: {"ok": True, "records": [{"heat_bus_unavailable": "firmware_no_voltage"}]}
r = ct.firmware_selftest(controller=1)
check("16-byte records (old backend, inferred) reported",
      any("16-byte" in p for p in r["problems"]), r["problems"])

r = make(records={1: [{"filament": 3}]}, rb=20).firmware_selftest(controller=1)
check("20-byte records (no reading time) reported",
      any("20-byte" in p and "time" in p for p in r["problems"]), r["problems"])

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
