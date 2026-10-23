#!/usr/bin/env python3
"""Per-pulse heating VOLTAGE (2026-10-14) and its TIME (2026-10-23): the RP2350 pulse record grew 16 ->
20 bytes (heat_bus_mV = the CC loop's last measured load voltage, heat_cmd_mV =
the TPS setpoint at that instant). The backend decodes either size from the
reply itself -- both firmwares are on the bench during a roll-out -- and the
client shows the voltage next to the current. Offline: a fake link.

    python3 tests/test_pulse_heat_voltage.py
"""
import _path  # noqa: F401

import os
import struct

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

import ct.server._server as srv  # noqa: E402
from ct.client._base import Result  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def rec(fil, seq, meas, tgt, bus=None, cmd=None, dt=None, flags=0):
    b = struct.pack("<BBHIHHHH", fil, flags, seq, 1000 * seq, 7000, 0x08, meas, tgt)
    if bus is not None:
        b += struct.pack("<HH", bus, cmd)
    if dt is not None:
        b += struct.pack("<hh", *dt)
    return b


class Link:
    def __init__(self, raw):
        self.raw = raw

    def request(self, typ, payload):
        return {"raw": list(self.raw)}


def reply(recs):
    return bytes([0]) + struct.pack("<HH", len(recs), 0) + bytes([len(recs)]) + b"".join(recs)


# 1. Old firmware, 16-byte records: current as before, voltage None + why.
r = srv.shv_op(Link(reply([rec(71, 0, 1970, 2000), rec(71, 1, 1975, 2000)])),
               {"op": "pulse_log", "start": 0})
x = r["records"][1]
check("16-byte: two records, current decoded", r["ok"] and len(r["records"]) == 2
      and x["heat_meas_mA"] == 1975 and x["heat_target_mA"] == 2000, r)
check("16-byte: voltage None, reason firmware_no_voltage",
      x["heat_bus_mV"] is None and x["heat_bus_unavailable"] == "firmware_no_voltage"
      and x["heat_cmd_mV"] is None, x)

# 2. New firmware, 20-byte records: voltage decoded, sentinels -> None + reason.
r = srv.shv_op(Link(reply([rec(59, 0, 2780, 2800, 7650, 7712),
                           rec(59, 1, 2790, 2800, 0xFFFE, 7720),
                           rec(59, 2, 0xFFFD, 0xFFFC, 0xFFFC, 0xFFFC)])),
               {"op": "pulse_log", "start": 0})
a, b, c = r["records"]
check("20-byte: three records, all fields", len(r["records"]) == 3
      and a["heat_meas_mA"] == 2780 and a["heat_bus_mV"] == 7650 and a["heat_cmd_mV"] == 7712, a)
check("20-byte: stale bus -> None 'stale', cmd still there",
      b["heat_bus_mV"] is None and b["heat_bus_unavailable"] == "stale" and b["heat_cmd_mV"] == 7720, b)
check("20-byte: output off -> None, never 0 V",
      c["heat_bus_mV"] is None and c["heat_cmd_mV"] is None
      and c["heat_cmd_unavailable"] == "output_off", c)
check("20-byte: read165 / seq still right (offsets unchanged)",
      a["read165"] == 8 and b["seq"] == 1 and c["durationUs"] == 7000, a)

# 3. The printed pulse table shows the voltage columns -- and not for old fw.
res = Result({"ok": True, "records": r["records"]})
txt = repr(res)
check("printout: heat mV / cmd mV columns with the values",
      "heat mV" in txt and "7650" in txt and "7712" in txt and "n/a (stale)" in txt, txt)
old = srv.shv_op(Link(reply([rec(71, 0, 1970, 2000)])), {"op": "pulse_log", "start": 0})
txt = repr(Result({"ok": True, "records": old["records"]}))
check("printout: old firmware -> no voltage columns", "heat mV" not in txt and "1970" in txt, txt)

# 4. 24-byte records (2026-10-23): WHEN the reading was taken, relative to the
#    trigger, in 0.1 ms; -32768 = not dated; flags bit3 = trigger stamp lost.
r = srv.shv_op(Link(reply([rec(16, 0, 2532, 2555, 7860, 7878, dt=(-23, -23)),
                           rec(16, 1, 2530, 2555, 7861, 7878, dt=(41, 41), flags=0x08),
                           rec(16, 2, 2529, 0xFFFC, 0xFFFE, 7878, dt=(-4980, -32768))])),
               {"op": "pulse_log", "start": 0})
a, b, c = r["records"]
check("24-byte: dt decoded in ms, signed", r["record_bytes"] == 24 and a["heat_meas_dt_ms"] == -2.3
      and a["heat_bus_dt_ms"] == -2.3 and b["heat_meas_dt_ms"] == 4.1, (a, b))
check("24-byte: offsets of the 20-byte fields unchanged",
      a["heat_meas_mA"] == 2532 and a["heat_bus_mV"] == 7860 and a["heat_cmd_mV"] == 7878
      and a["read165"] == 8, a)
check("24-byte: flags bit3 -> trigger_time_approx, not a verify flag",
      b["trigger_time_approx"] and not a["trigger_time_approx"] and not b["on_mismatch"]
      and not b["unverified"], b)
check("24-byte: a sentinel value carries no dt (never a time for a non-reading)",
      c["heat_meas_dt_ms"] == -498.0 and c["heat_bus_mV"] is None and c["heat_bus_dt_ms"] is None, c)
check("20-byte: dt None, reason firmware_no_timing", r is not None and
      srv.shv_op(Link(reply([rec(59, 0, 2780, 2800, 7650, 7712)])), {"op": "pulse_log", "start": 0})
      ["records"][0]["heat_dt_unavailable"] == "firmware_no_timing")
txt = repr(Result({"ok": True, "records": r["records"]}))
check("printout: 'read at ms' column with signed values and ~ for approx",
      "read at ms" in txt and "-2.3" in txt and "+4.1~" in txt, txt)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
