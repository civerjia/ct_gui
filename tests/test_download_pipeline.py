#!/usr/bin/env python3
"""Pipelined schedule download (window > 1) against a fake RP2350 over TCP.

The RP2350 handles every frame it has buffered each main-loop pass; with the
filaments heating a pass is 100-200 ms, so one frame per round trip makes a
download crawl. window=N keeps N frames in flight. A frame not confirmed
re-sends everything from it on, serially, so a re-sent CLEAR can never wipe
entries written after it.

    python3 tests/test_download_pipeline.py
"""
import _path  # noqa: F401

import os
import socket
import threading
import time

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.protocol as P  # noqa: E402
import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeRp:
    """Answers frames in batches every `loop_s` (like a busy RP2350 main loop),
    keeps the table it is sent, and loses the reply to the `lose`-th frame."""

    def __init__(self, loop_s=0.1, lose=None):
        self.loop_s, self.lose = loop_s, lose
        self.table, self.seen, self.n = {}, [], 0
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        conn, _ = self.srv.accept()
        conn.setblocking(False)
        parser = P.FrameParser()
        while True:
            time.sleep(self.loop_s)
            try:
                data = conn.recv(65536)
            except BlockingIOError:
                continue
            if not data:
                return
            for f in parser.feed(data):
                self.n += 1
                self.seen.append(f.type)
                if f.type == S.SHV_CLEAR_TABLE:
                    self.table.clear()
                elif f.type == S.SHV_SET_ENTRIES:
                    st = f.payload[0] | f.payload[1] << 8
                    for k in range(f.payload[2]):
                        self.table[st + k] = bytes(f.payload[3 + 4 * k:7 + 4 * k])
                if self.n == self.lose:
                    continue
                resp = P.Frame(P.VERSION, f.type, P.FLAG_IS_RESPONSE, f.seq, bytes([0]))
                conn.sendall(resp.encode())


class Link:
    def __init__(self, client):
        self.client = client

    def hold_monitor(self, *a, **k):
        pass

    def set_poll_paused(self, *a, **k):
        pass


fils = [f for f in range(96) if f not in S.dead_fids()]
plan = {"config": {"interPulseMs": 3000, "maxOnMs": 165, "totalMs": 60000, "triggerEdge": 0},
        "emission": [{"filament": f, "numPulses": 1, "widthUs": 7000} for _ in range(5) for f in fils],
        "heating": []}
want = len(plan["emission"])


def run(window, lose=None, loop_s=0.1):
    rp = FakeRp(loop_s=loop_s, lose=lose)
    cl = P.TcpProtocolClient()
    cl.connect("127.0.0.1", rp.port)
    t = time.monotonic()
    r = S.download_to_controller(Link(cl), 0, plan, window=window)
    took = time.monotonic() - t
    cl.disconnect() if hasattr(cl, "disconnect") else None
    return r, took, rp


r1, t1, rp1 = run(1)
r4, t4, rp4 = run(4)
print(f"   busy RP2350 (100 ms loop), {r1['frames']} frames: serial {t1:.2f} s, window 4 {t4:.2f} s")
check("serial ok, full table", r1["ok"] and len(rp1.table) == want, (r1, len(rp1.table)))
check("window 4 ok, full table", r4["ok"] and len(rp4.table) == want, (r4, len(rp4.table)))
check("window 4 is much faster on a busy loop", t4 < t1 / 2.5, (t1, t4))
check("same frames in the same order", rp1.seen == rp4.seen)
check("window reported", r4["timing"].get("window") == 4 and r4["timing"].get("resent") == 0, r4["timing"])

# A reply lost mid-pipeline (the emit CLEAR is frame 4): everything from it on
# is re-sent in order, and the table still ends up complete.
rl, tl, rpl = run(4, lose=4)
check("lost reply -> still ok, table complete", rl["ok"] and len(rpl.table) == want, (rl, len(rpl.table)))
check("...re-sent from the lost frame on", rl["timing"]["resent"] == rl["frames"] - 3, rl["timing"])
last_clear = max(i for i, t in enumerate(rpl.seen) if t == S.SHV_CLEAR_TABLE)
check("...no CLEAR after the last entries write",
      all(t != S.SHV_SET_ENTRIES for t in rpl.seen[:last_clear]) or
      any(t == S.SHV_SET_ENTRIES for t in rpl.seen[last_clear:]), rpl.seen)
print(f"   lost reply with window 4: {tl:.2f} s")

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
