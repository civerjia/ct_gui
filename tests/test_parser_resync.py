#!/usr/bin/env python3
"""A frame that loses bytes in transit must not silence the link for a minute.

The RP2350's UART frame parser (uart_stream_parser.cpp) has no inter-byte
timeout: after a truncated frame it reads the NEXT frames as payload (up to
512 bytes) before the CRC fails. On a quiet link that is tens of seconds with
the RP2350 alive but answering nothing (2026-10-07 08:42, Power 1, ~60 s).
TcpProtocolClient now writes zero bytes after two timeouts in a row, which
finishes the half-read frame and puts the parser back to hunting.

Offline: a fake RP2350 over TCP that runs a line-for-line port of that parser.

    python3 tests/test_parser_resync.py
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


MAX_PAYLOAD = 512   # kUartMaxPayloadLength


class FirmwareParser:
    """uart_stream_parser.cpp, state for state (no timeout, length <= 512)."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state, self.hdr, self.payload, self.crc = "sof0", bytearray(), bytearray(), bytearray()
        self.length = 0

    def feed(self, b):
        """-> a complete, CRC-good (type, seq, payload) or None."""
        if self.state == "sof0":
            if b == P.SOF0:
                self.state = "sof1"
        elif self.state == "sof1":
            if b == P.SOF1:
                self.state, self.hdr = "hdr", bytearray()
            elif b != P.SOF0:
                self.state = "sof0"
        elif self.state == "hdr":
            self.hdr.append(b)
            if len(self.hdr) == 6:
                self.length = self.hdr[4] | (self.hdr[5] << 8)
                if self.length > MAX_PAYLOAD:
                    self.reset()
                elif self.length == 0:
                    self.state, self.crc = "crc", bytearray()
                else:
                    self.state, self.payload = "payload", bytearray()
        elif self.state == "payload":
            self.payload.append(b)
            if len(self.payload) == self.length:
                self.state, self.crc = "crc", bytearray()
        elif self.state == "crc":
            self.crc.append(b)
            if len(self.crc) == 2:
                good = P.crc16_ccitt_false(bytes(self.hdr) + bytes(self.payload)) == \
                    int.from_bytes(self.crc, "little")
                out = (self.hdr[1], self.hdr[3], bytes(self.payload)) if good else None
                self.reset()
                return out
        return None


class FakeRp:
    """Answers PING. truncate_next: the next request arrives missing its tail
    and claiming a long payload -- the in-transit damage that desyncs it."""

    def __init__(self):
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.parser = FirmwareParser()
        self.damage_next = False
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        conn, _ = self.srv.accept()
        self.conn = conn
        while True:
            data = conn.recv(4096)
            if not data:
                return
            if self.damage_next and len(data) >= 8 and data[0] == P.SOF0:
                # Length field hit (0x01FF = 511) and the rest of the frame lost:
                # the parser now waits for 511 payload bytes.
                data = bytes(data[:6]) + bytes([0xFF, 0x01])
                self.damage_next = False
            for b in data:
                got = self.parser.feed(b)
                if got:
                    ftype, seq, payload = got
                    resp = P.Frame(P.VERSION, ftype, P.FLAG_IS_RESPONSE, seq,
                                   bytes([0]) + payload + (0).to_bytes(4, "little"))
                    conn.sendall(resp.encode())


rp = FakeRp()
cl = P.TcpProtocolClient()
cl.connect("127.0.0.1", rp.port)

r = cl.send_request(0x01, b"\x01\x02\x03\x04", timeout=1.0)
check("clean link answers", r.get("raw") is not None, r)

rp.damage_next = True
t0 = time.monotonic()
outcomes = []
for i in range(4):
    try:
        cl.send_request(0x01, b"\x01\x02\x03\x04", timeout=0.5)
        outcomes.append("ok")
    except TimeoutError:
        outcomes.append("timeout")
took = time.monotonic() - t0
print("   after damage:", outcomes, f"{took:.1f} s", "flushes", cl.resync_flushes)
check("a damaged frame costs timeouts at first", outcomes[0] == "timeout", outcomes)
check("the link answers again after the resync flush", "ok" in outcomes, outcomes)
check("...within the third try (two timeouts, then flush)", outcomes.index("ok") <= 2, outcomes)
check("exactly one flush sent", cl.resync_flushes == 1, cl.resync_flushes)
check("the timeout run is reset after success", cl.consecutive_timeouts == 0, cl.consecutive_timeouts)
check("flushes are reported in the link timing", cl.timing_summary()["resync_flushes"] == 1,
      cl.timing_summary())

# Without the flush (the old behaviour) the same damage keeps it silent.
rp2 = FakeRp()
cl2 = P.TcpProtocolClient()
cl2.connect("127.0.0.1", rp2.port)
P_AFTER = P.RESYNC_AFTER_TIMEOUTS
P.RESYNC_AFTER_TIMEOUTS = 10 ** 6       # disable
rp2.damage_next = True
quiet = []
for i in range(4):
    try:
        cl2.send_request(0x01, b"\x01\x02\x03\x04", timeout=0.3)
        quiet.append("ok")
    except TimeoutError:
        quiet.append("timeout")
P.RESYNC_AFTER_TIMEOUTS = P_AFTER
check("control: without the flush the link stays silent", quiet == ["timeout"] * 4, quiet)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
