#!/usr/bin/env python3
"""/api/cmd CH_READ/WRITE_TPS_REGISTER (0x29/0x2A) reach the backend's builder.
2026-10-13: "unknown command CH_WRITE_TPS_REGISTER" -- the protocol library had
the frames, the backend's /api/cmd did not. Offline.

    python3 tests/test_tps_raw_register.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

from ct.server._wire import build_payload, _is_read_command  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


ft, fl, pl = build_payload("CH_READ_TPS_REGISTER", {"channel": 5, "mux_port": 1, "reg": 2, "width_bytes": 1})
check("read IOUT_LIMIT of CH6.2: 0x29 [5,1,2,1]", ft == 0x29 and pl == bytes([5, 1, 2, 1]), (ft, pl))
ft, fl, pl = build_payload("CH_WRITE_TPS_REGISTER",
                           {"channel": 5, "mux_port": 1, "reg": 2, "width_bytes": 1, "value": 0x7F})
check("write 0x7F: 0x2A [5,1,2,1,0x7F,0]", ft == 0x2A and pl == bytes([5, 1, 2, 1, 0x7F, 0]), (ft, pl))
check("read is a read (allowed under another's lease); write is not",
      _is_read_command("CH_READ_TPS_REGISTER") and not _is_read_command("CH_WRITE_TPS_REGISTER"))

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
