#!/usr/bin/env python3
"""CTClient(host) takes a port inside host, so a program that passes only a
host string (combined_backend's CT_BACKEND_HOST) can reach a backend that is
not on 8770 -- control2 runs it on 8780 because 8770 is the Spellman backend.

    python3 tests/test_backend_base.py
"""
import _path  # noqa: F401

from ct.client._client import backend_base

CASES = [
    (("192.168.8.218", 8770), "http://192.168.8.218:8770"),
    (("192.168.8.118:8780", 8770), "http://192.168.8.118:8780"),
    (("control2:8780", 8770), "http://control2:8780"),
    (("http://192.168.8.118:8780/", 8770), "http://192.168.8.118:8780"),
    (("localhost", 8780), "http://localhost:8780"),
    (("control2:8780", 9999), "http://control2:8780"),
    (("[::1]:8780", 8770), "http://[::1]:8780"),
]

fails = 0
for args, want in CASES:
    got = backend_base(*args)
    ok = got == want
    fails += not ok
    print(f"{'PASS' if ok else 'FAIL'} backend_base{args} = {got}" + ("" if ok else f" (want {want})"))
print("ALL PASS" if not fails else f"{fails} FAILED")
raise SystemExit(1 if fails else 0)
