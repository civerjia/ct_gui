#!/usr/bin/env python3
"""GUI dead/power-state display and readable batch errors, offline:
- the raw proxies (/api/cmd, /api/power-cmd) refuse to power a DEAD filament
  and refuse STOP->ACTIVE, naming the board; STOP/SLEEP and non-power
  commands pass; an accepted command is tracked by the ladder;
- board-snapshot rows carry fid / dead / dead_reason / power_state, with the
  firmware's state preferred and the backend's as fallback;
- a failed batch power command reads as a sentence naming each board and why.

    python3 tests/test_gui_dead_and_errors.py
"""

import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def board_of(fid):
    c0, ch, pos, _ = S.filament_to_board(fid)
    return c0, ch, pos


S.LAST_POWER_STATE.clear()
# pick two filaments on controller 1 from the live mapping
fids = [f for f in range(S.FILAMENT_COUNT) if board_of(f)[0] == 0 and board_of(f)[1] is not None][:3]
dead_f, live_f, other_f = fids
S.DEAD_FIDS.clear()
S.DEAD_FIDS[dead_f] = {"reason": "open circuit (2 mA capped)", "by": "test"}
_, dch, dpos = board_of(dead_f)
_, lch, lpos = board_of(live_f)

# --- raw proxy guard -----------------------------------------------------------
single = lambda ch, pos, **kw: {"target": "single", "channel": ch, "mux_port": pos, **kw}
ref, _, _ = S._raw_power_guard(1, "CH_SET_POWER_STATE", single(dch, dpos, state=4, arg=1500))
check("IDLE on a dead filament is refused", ref and ref.get("refused"), str(ref))
check("...and the error names the board and the reason",
      ref and f"CH{dch + 1}.{dpos + 1}" in ref["error"] and "open circuit" in ref["error"]
      and "nothing was sent" in ref["error"], ref and ref["error"])
ref, _, _ = S._raw_power_guard(1, "CH_SET_POWER_STATE", single(dch, dpos, state=1))
check("STOP on a dead filament is allowed", ref is None)
ref, _, _ = S._raw_power_guard(1, "CH_SET_TPS_ENABLE", single(dch, dpos, enable=True))
check("TPS enable on a dead filament is refused", ref is not None)
ref, _, _ = S._raw_power_guard(1, "CH_SET_TPS_ENABLE", single(dch, dpos, enable=False))
check("TPS disable on a dead filament is allowed", ref is None)
ref, _, _ = S._raw_power_guard(1, "CH_GET_INA219", single(dch, dpos))
check("a read is never guarded", ref is None)

mask = [0] * 8
mask[dch] |= 1 << dpos
mask[lch] |= 1 << lpos
ref, _, _ = S._raw_power_guard(1, "CH_SET_POWER_STATE", {"board_mask": mask, "state": 3})
check("a board_mask STANDBY with one dead board is refused whole", ref is not None)

ref, _, _ = S._raw_power_guard(1, "CH_SET_POWER_STATE", single(lch, lpos, state=5, arg=2600))
check("STOP->ACTIVE (state unknown) is refused", ref and "ACTIVE" in ref["error"], str(ref))
S.note_power_state([live_f], S.POWER_STATE_STANDBY)
ref, _, _ = S._raw_power_guard(1, "CH_SET_POWER_STATE", single(lch, lpos, state=5, arg=2600))
check("STANDBY->ACTIVE is refused", ref and "STANDBY" in ref["error"], str(ref))
S.note_power_state([live_f], S.POWER_STATE_IDLE, {live_f: 1500})
ref, fl, st = S._raw_power_guard(1, "CH_SET_POWER_STATE", single(lch, lpos, state=5, arg=2600))
check("IDLE->ACTIVE is allowed, with the fid and state to track",
      ref is None and fl == [live_f] and st == 5, f"{ref} {fl} {st}")

# --- board rows ----------------------------------------------------------------
rows = [{"channel": ch, "mux_port": m, "label": f"CH{ch + 1}.{m + 1}", "power_state": None}
        for ch in range(8) for m in range(8)]
for r in rows:
    if (r["channel"], r["mux_port"]) == (lch, lpos):
        r["power_state"] = S.POWER_STATE_SLEEP           # firmware says SLEEP
S.LAST_POWER_STATE.clear()
S.note_power_state([live_f], S.POWER_STATE_IDLE, {live_f: 1500})   # backend thinks IDLE
_, och, opos = board_of(other_f)
S.note_power_state([other_f], S.POWER_STATE_STANDBY)
S._annotate_board_rows(1, rows)
by = {(r["channel"], r["mux_port"]): r for r in rows}
d, lv, ot = by[(dch, dpos)], by[(lch, lpos)], by[(och, opos)]
check("dead row flagged with its reason", d["dead"] and "open circuit" in (d["dead_reason"] or ""))
check("row carries its fid", lv["fid"] == live_f)
check("firmware state wins over the backend's",
      lv["power_state_name"] == "SLEEP" and lv["power_state_src"] == "firmware", str(lv))
check("backend state used when the firmware gave none",
      ot["power_state_name"] == "STANDBY" and ot["power_state_src"] == "backend", str(ot))
check("no state anywhere -> None, not STOP", d["power_state"] is None)

# --- readable batch error ------------------------------------------------------
out = {"ok": False, "applied": 1, "failed": [live_f], "excluded": [], "ladder_blocked": [other_f]}
results = {"1": {"ok": False, "ladder_reasons": {str(other_f): "currently at STANDBY; ACTIVE may only be entered from IDLE(4)"},
                 "dead_skipped": [dead_f]},
           "2": {"ok": False, "error": "running — disarm first"}}
msg = S._prep_summary(S.POWER_STATE_ACTIVE, None, out, results)
print("   summary:", msg)
check("summary leads with the state and the count", msg.startswith("ACTIVE: 1 of 2 applied"), msg)
check("failed board named with a reason",
      f"Power 1 CH{lch + 1}.{lpos + 1} (filament {live_f})" in msg and "Power 1 is not connected" in msg, msg)
check("ladder refusal carries its reason", "refused — currently at STANDBY" in msg, msg)
check("controller-level error included", "Power 2: running — disarm first" in msg, msg)
check("dead ones listed as left off on purpose", "marked dead, left off on purpose" in msg, msg)

# --- /api/filament-prep end to end: a dead filament in an "all" batch -------
# The GUI's "Standby all" asks for all 96; the dead ones are skipped on
# purpose and must not turn the batch into a failure ("no board answers").
import json  # noqa: E402
import threading  # noqa: E402
import urllib.request  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402


class _Cl:
    connected = True


class _Link:
    client = _Cl()
    name = "Power 1"

    def request(self, *a, **k):
        return {}


def _fake_prep(link, c0, state, fids, currents=None, default_arg=0, channels=None):
    mine = [f for f in fids if board_of(f)[0] == c0]
    dead = [f for f in mine if f in S.DEAD_FIDS]
    live = [f for f in mine if f not in S.DEAD_FIDS]
    return {"controller": c0, "ok": True, "applied": len(live), "failed": [], "state": state,
            "landed": live, "touched": live, "dead_skipped": dead, "not_this_controller": [],
            "unslotted": [], "ladder_blocked": [], "ladder_reasons": {}}


S.prep_filaments = _fake_prep
S.decode_shv_status = lambda r: {"state": 0}
S.CONTROLLERS.clear()
S.CONTROLLERS[1] = _Link()
srv = ThreadingHTTPServer(("127.0.0.1", 0), S.CtHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
want = [f for f in range(S.FILAMENT_COUNT) if board_of(f)[0] == 0 and board_of(f)[1] is not None]
req = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/api/filament-prep",
                             data=json.dumps({"state": 3, "filaments": want}).encode(),
                             headers={"Content-Type": "application/json", "X-CT-Client": "test"})
r = json.loads(urllib.request.urlopen(req, timeout=10).read())
check("batch including a dead filament is ok", r.get("ok") is True, str(r.get("error") or r)[:300])
check("...the dead one is not 'excluded'", dead_f not in (r.get("excluded") or []), str(r.get("excluded")))
check("...and the summary says it was left off on purpose",
      "marked dead, left off on purpose" in (r.get("summary") or ""), r.get("summary"))
srv.shutdown()

S.DEAD_FIDS.clear()
S.LAST_POWER_STATE.clear()
print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
