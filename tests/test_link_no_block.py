#!/usr/bin/env python3
"""One lost reply must not stall every other request on the link.

Requests on a controller share one link lock. A request used to hold it for
its whole timeout, so a reply lost on the way back (2026-10-07) made the board
monitor, status reads and other clients queue for 1-3 s behind it. Now a
request holds the link only LINK_HOLD_S, then keeps waiting for its own reply
(matched by seq) without it.

Offline: a fake RP2350 over TCP that drops the reply to one frame type.

    python3 tests/test_link_no_block.py
"""
import _path  # noqa: F401

import socket
import threading
import time

import ct.protocol as P

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class FakeRp:
    """Answers every frame (after `delay`), except that it never answers the
    frame type in `drop`."""

    def __init__(self, drop=None, delay=0.02):
        self.drop, self.delay = drop, delay
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        conn, _ = self.srv.accept()
        parser = P.FrameParser()
        while True:
            data = conn.recv(4096)
            if not data:
                return
            for f in parser.feed(data):
                if f.type == self.drop:
                    continue
                time.sleep(self.delay)
                resp = P.Frame(P.VERSION, f.type, P.FLAG_IS_RESPONSE, f.seq,
                               bytes([0]) + f.payload + (0).to_bytes(4, "little"))
                conn.sendall(resp.encode())


rp = FakeRp(drop=0x79)
cl = P.TcpProtocolClient()
cl.connect("127.0.0.1", rp.port)

res = {}


def a():
    t = time.monotonic()
    try:
        cl.send_request(0x79, b"", timeout=2.0)
        res["a"] = ("ok", time.monotonic() - t)
    except TimeoutError:
        res["a"] = ("timeout", time.monotonic() - t)


def b():
    time.sleep(0.1)
    t = time.monotonic()
    r = cl.send_request(0x01, b"\x01\x02\x03\x04", timeout=1.0)
    res["b"] = ("ok" if r.get("raw") else "bad", time.monotonic() - t)


ta, tb = threading.Thread(target=a), threading.Thread(target=b)
ta.start(); tb.start(); ta.join(); tb.join()
print("   A (reply lost):", res["a"], "  B (behind it):", res["b"])
check("the request whose reply is lost still times out at its own timeout",
      res["a"][0] == "timeout" and 1.9 <= res["a"][1] <= 2.3, res["a"])
check("a request behind it is NOT held for that timeout",
      res["b"][0] == "ok" and res["b"][1] < P.LINK_HOLD_S + 0.3, res["b"])

# A normal reply is still collected inside the hold window (serial, unchanged).
t = time.monotonic()
r = cl.send_request(0x01, b"\x01\x02\x03\x04", timeout=1.0)
check("normal request unchanged", r.get("raw") and time.monotonic() - t < 0.2)

# Many concurrent requests each get THEIR reply (seq matching), none mixed up.
out = {}


def worker(i):
    r = cl.send_request(0x01, bytes([i, i, i, i]), timeout=2.0)
    out[i] = bytes(r["raw"][1:5])


ts = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
[t.start() for t in ts]
[t.join() for t in ts]
check("12 concurrent requests each get their own reply",
      all(out.get(i) == bytes([i] * 4) for i in range(12)), out)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
