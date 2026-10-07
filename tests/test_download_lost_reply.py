#!/usr/bin/env python3
"""A download frame whose reply is LOST costs about 1 s, not 3+.

2026-10-07: requests reached the RP2350 intact and its loop never stalled
past 220 ms, yet some replies never came back; the download waited 3 s on
each before retrying, so uploads took 4-10 s. The first try is now 1 s.

    python3 tests/test_download_lost_reply.py
"""
import _path  # noqa: F401

import os
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class Client:
    """Answers every frame at once, except that the reply to the first frame of
    type `lose` is lost (the caller waits out its whole timeout)."""

    def __init__(self, lose=None, lose_times=1):
        self.lose, self.lose_times = lose, lose_times
        self.calls = []
        self.connected = True

    def send_request(self, ft, payload=b"", flags=0, timeout=1.0):
        self.calls.append((ft, timeout))
        if ft == self.lose and self.lose_times > 0:
            self.lose_times -= 1
            time.sleep(timeout)              # nothing comes back
            raise TimeoutError(f"Timed out waiting for response to 0x{ft:02X}")
        return {"raw": bytes([0])}


class Link:
    def __init__(self, client):
        self.client = client

    def hold_monitor(self, *a, **k):
        pass

    def set_poll_paused(self, *a, **k):
        pass


plan = {"config": {"interPulseMs": 100, "maxOnMs": 165, "totalMs": 6000, "triggerEdge": 0},
        "emission": [{"filament": 0, "numPulses": 1, "widthUs": 7000}], "heating": []}

# Clean link: one try per frame, all with the short first timeout.
cl = Client()
t0 = time.monotonic()
r = S.download_to_controller(Link(cl), 0, plan)
check("clean download ok", r.get("ok"), r)
check("every frame's first try uses the short timeout",
      all(t == S.DOWNLOAD_FRAME_TIMEOUTS_S[0] for _, t in cl.calls), cl.calls)

# One lost reply (the active list): ~1 s extra, then OK on the retry.
cl = Client(lose=S.SHV_SET_ACTIVE_LIST)
t0 = time.monotonic()
r = S.download_to_controller(Link(cl), 0, plan)
took = time.monotonic() - t0
print(f"   one lost reply: {took:.2f} s")
check("lost reply -> retried and ok", r.get("ok"), r)
check("lost reply costs ~1 s (was 3 s)", 0.9 <= took < 2.0, took)

# The frame lost on every try still fails after 1 + 2 + 4 s, named.
cl = Client(lose=S.SHV_SET_ACTIVE_LIST, lose_times=99)
t0 = time.monotonic()
r = S.download_to_controller(Link(cl), 0, plan)
took = time.monotonic() - t0
check("always lost -> fails, naming the frame", not r.get("ok") and "active_list" in str(r), r)
check("...after the full retry ladder", abs(took - sum(S.DOWNLOAD_FRAME_TIMEOUTS_S)) < 1.0, took)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
