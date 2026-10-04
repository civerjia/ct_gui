#!/usr/bin/env python3
"""The periodic GitHub update check, offline (GitHub mocked):
- remote_check says "available" only when GitHub's head is not what runs;
- the development copy never reports an update;
- the watch loop publishes the answer in /api/version's "update", and a
  failed check keeps the last answer instead of hiding a known update.

    python3 tests/test_update_watch.py
"""

import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

from ct import update as U  # noqa: E402
import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


HEAD = "abcdef1234567890abcdef1234567890abcdef12"
U._latest_commit = lambda: HEAD
U._is_dev_copy = lambda: False
r = U.remote_check("abcdef1")
check("same commit -> no update", r["ok"] and not r["available"], str(r))
r = U.remote_check("1234567")
check("older commit -> update available, names both",
      r["available"] and r["latest"] == "abcdef1" and r["current"] == "1234567", str(r))
r = U.remote_check(None)
check("unknown running version -> offered", r["available"], str(r))
U._is_dev_copy = lambda: True
r = U.remote_check("1234567")
check("dev copy never reports an update", not r["available"] and r.get("dev_copy"), str(r))
U._is_dev_copy = lambda: False

# --- the loop: run single iterations by making sleep raise after the check ---
class Stop(Exception):
    pass


calls = {"n": 0}


def fake_sleep(s):
    calls["n"] += 1
    if calls["n"] > 1:          # first sleep = the start-up delay; the next ends the run
        raise Stop


def run_once(check_fn):
    calls["n"] = 0
    U.remote_check = check_fn
    S.time.sleep, real = fake_sleep, S.time.sleep
    try:
        S._update_watch_loop()
    except Stop:
        pass
    finally:
        S.time.sleep = real


S._BACKEND_VERSION["commit"] = "1234567"
run_once(lambda cur: {"ok": True, "latest": "abcdef1", "current": cur, "available": True})
st = dict(S._UPDATE_STATUS)
check("loop publishes an available update", st.get("available") and st.get("latest") == "abcdef1", str(st))


def boom(cur):
    raise OSError("GitHub unreachable")


run_once(boom)
st = dict(S._UPDATE_STATUS)
check("a failed check keeps the known update and adds the error",
      st.get("available") and "unreachable" in (st.get("error") or ""), str(st))

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
