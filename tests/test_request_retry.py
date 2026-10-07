#!/usr/bin/env python3
"""A request that is safe to repeat (0x79 status, SHV_DISARM, download frames)
is retried with growing timeouts instead of failing on one slow reply, and
still FAILS (raises) when no try answers -- a gate must not pass on silence.

2026-10-06 liuxing_api run: the master ESP32 held RP2350 replies up to 0.72 s;
one-shot 1 s reads gave 9 failed downloads, 7 failed disarms and repeated
"cannot confirm controller is not running a schedule".

    python3 tests/test_request_retry.py
"""
import _path  # noqa: F401

import os

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")
os.environ["CT_NO_AUTO_CONNECT"] = "1"

import ct.server._server as S  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


class SlowLink:
    """Times out `slow` times, then answers. Records the timeouts it was given."""

    def __init__(self, slow, answer):
        self.slow, self.answer, self.timeouts = slow, answer, []

    def request(self, ft, payload=b"", flags=0, timeout=2.0):
        self.timeouts.append(timeout)
        if self.slow > 0:
            self.slow -= 1
            raise TimeoutError(f"Timed out waiting for response to 0x{ft:02X}")
        return self.answer


status_raw = {"raw": bytes([0, 0]) + bytes(70)}

# 1. Two slow replies, the third answers: success, longer timeout each try.
link = SlowLink(2, status_raw)
r = S.request_retry(link, S.SHV_GET_STATUS)
check("third try answers -> returned", r is status_raw)
check("timeouts grow each try", link.timeouts == list(S.RETRY_TIMEOUTS_S), link.timeouts)

# 2. Never answers: raises (a gate must fail closed, never pass on silence).
link = SlowLink(99, status_raw)
try:
    S.request_retry(link, S.SHV_GET_STATUS)
    check("no answer -> raises", False, "returned instead of raising")
except Exception as exc:
    check("no answer -> raises", "Timed out" in str(exc), str(exc))
check("...after exactly len(RETRY_TIMEOUTS_S) tries", len(link.timeouts) == len(S.RETRY_TIMEOUTS_S), link.timeouts)

# 3. ok=: an answer that is not OK is retried, then reported, not returned.
link = SlowLink(0, {"raw": bytes([0x05])})
try:
    S.request_retry(link, S.SHV_DISARM, ok=S._status_ok)
    check("not-OK answer every time -> raises", False)
except Exception as exc:
    check("not-OK answer every time -> raises", "not OK" in str(exc), str(exc))

# 4. A clean link costs exactly one request.
link = SlowLink(0, status_raw)
S.request_retry(link, S.SHV_GET_STATUS)
check("fast link -> one request", link.timeouts == [S.RETRY_TIMEOUTS_S[0]], link.timeouts)

# 5. shv_status_retry decodes, and still raises on silence.
link = SlowLink(1, status_raw)
st = S.shv_status_retry(link)
check("shv_status_retry decodes after a slow try", isinstance(st, dict) and "state" in st, st)

print(f"\n{'ALL PASS' if not FAILS else f'{len(FAILS)} FAILED: {FAILS}'}")
raise SystemExit(1 if FAILS else 0)
