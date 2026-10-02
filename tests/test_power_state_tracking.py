#!/usr/bin/env python3
"""The backend's view of each filament's power state, offline:
- the ACTIVE guard lets a filament that left ACTIVE < WARM_AFTER_ACTIVE_S ago
  back up while its CC loop still reports 'ramping' (it is hot), and nothing
  else that is unsettled;
- after a backend restart the state is taken from the firmware's board cache
  instead of being "unknown", without overwriting a newer command.

    python3 tests/test_power_state_tracking.py
"""

import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []
IDLE, ACTIVE, STANDBY = S.POWER_STATE_IDLE, S.POWER_STATE_ACTIVE, S.POWER_STATE_STANDBY


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def clear():
    S.LAST_POWER_STATE.clear()
    S.LAST_ACTIVE_LEFT.clear()


# --- the warm rule -------------------------------------------------------------
clear()
S.note_power_state([1], STANDBY)
S.note_power_state([1], IDLE, {1: 1200})
why = S.ladder_blocks_active(1, "ramping", True)
check("cold: IDLE still ramping, never ACTIVE -> refused", why and "ramping" in why, str(why))

S.note_power_state([1], ACTIVE, {1: 2600})
S.note_power_state([1], IDLE, {1: 1200})              # back down from ACTIVE
check("leaving ACTIVE is recorded", 1 in S.LAST_ACTIVE_LEFT)
check("hot: left ACTIVE just now, ramping -> allowed", S.ladder_blocks_active(1, "ramping", True) is None)
S.note_power_state([1], IDLE, {1: 1200})              # a re-sent IDLE (what the script did)
check("re-sent IDLE does not reset the warm clock", S.ladder_blocks_active(1, "ramping", True) is None)
why = S.ladder_blocks_active(1, "capped", True)
check("hot but CAPPED -> still refused", why and "capped" in why, str(why))

S.LAST_ACTIVE_LEFT[1] = time.monotonic() - (S.WARM_AFTER_ACTIVE_S + 5)
why = S.ladder_blocks_active(1, "ramping", True)
check("left ACTIVE longer ago than the window -> refused again", why and "ramping" in why, str(why))
check("settled is allowed as before", S.ladder_blocks_active(1, "settled", True) is None)

clear()
S.note_power_state([2], ACTIVE, {2: 2600})
S.note_power_state([2], STANDBY)                      # down past IDLE
why = S.ladder_blocks_active(2, "ramping", True)
check("hot but at STANDBY (not IDLE) -> refused", why and "STANDBY" in why, str(why))

# --- state taken from the firmware after a restart ------------------------------
clear()
check("restart: unknown before the first cache read",
      "unknown" in (S.ladder_blocks_active(16, "settled", True) or ""))
board = S.MAPPING.board(16)                           # (controller, channel, position)
c0, ch, pos = board
t0 = time.monotonic()
cache = {(ch, pos): {"known": True, "power_state": IDLE}}
got = S._adopt_firmware_power_states(c0, cache, t0)
check("firmware IDLE adopted for an unknown filament", S.LAST_POWER_STATE.get(16, (None,))[0] == IDLE, str(got))
check("then ACTIVE is allowed once settled", S.ladder_blocks_active(16, "settled", True) is None)

S.note_power_state([16], STANDBY)                     # a command lands AFTER the read began
S._adopt_firmware_power_states(c0, {(ch, pos): {"known": True, "power_state": IDLE}}, t0)
check("a newer command is not overwritten by an older read", S.LAST_POWER_STATE[16][0] == STANDBY)

clear()
S.note_power_state([16], ACTIVE, {16: 2600})
time.sleep(0.01)
S._adopt_firmware_power_states(c0, {(ch, pos): {"known": True, "power_state": IDLE}}, time.monotonic())
check("firmware ACTIVE -> IDLE (e.g. a heating plan) counts as leaving ACTIVE",
      S.LAST_POWER_STATE[16][0] == IDLE and 16 in S.LAST_ACTIVE_LEFT)

clear()
S._adopt_firmware_power_states(c0, {(ch, pos): {"known": False, "power_state": IDLE}}, time.monotonic())
check("a channel that is not ready is not adopted", 16 not in S.LAST_POWER_STATE)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {', '.join(FAILS)}")
raise SystemExit(1 if FAILS else 0)
