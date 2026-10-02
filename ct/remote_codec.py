"""JSON encoding for remote CTClient calls (ct/client/_remote.py <-> ct/server/_remote.py).

Plain JSON loses things the API's results depend on: dict keys that are
ints (`r["results"][1]`), tuples, sets (`ct.dead` is a frozenset), and the
Result / PulseLog types whose repr is the readable printout. Every value is
encoded so it decodes back to the same Python value and type.

This format is shared by every client and every backend version, so it only
ever GROWS: a tag is never renamed or given a new meaning, and an unknown
tag decodes to a readable placeholder rather than failing.
"""
from __future__ import annotations

import base64
import enum
from pathlib import PurePath
from typing import Any, Callable

TAG = "__ct__"


def encode(o: Any, self_obj: Any = None, register_cm: Callable[[Any], int] | None = None) -> Any:
    """Python value -> JSON-safe value. `self_obj` encodes as a reference to
    the remote client itself; `register_cm` turns a context manager into a
    handle the other side can enter and exit."""
    def enc(v: Any) -> Any:
        if self_obj is not None and v is self_obj:
            return {TAG: "self"}
        if v is None or isinstance(v, (bool, str)):
            return v
        if isinstance(v, enum.Enum):
            v = v.value
        if isinstance(v, (int, float)):
            return v
        cls = type(v).__name__
        if hasattr(v, "item") and hasattr(v, "dtype") and getattr(v, "shape", None) == ():
            return v.item()                       # numpy scalar
        if hasattr(v, "tolist") and hasattr(v, "dtype"):
            return {TAG: "ndarray", "v": v.tolist(), "dtype": str(v.dtype)}
        if isinstance(v, dict):
            out = {TAG: "dict", "k": [enc(k) for k in v], "v": [enc(x) for x in v.values()]}
            if cls == "Result":
                out[TAG] = "Result"
            return out
        if isinstance(v, (list, tuple, set, frozenset)):
            items = [enc(x) for x in v]
            if isinstance(v, tuple):
                return {TAG: "tuple", "v": items}
            if isinstance(v, frozenset):
                return {TAG: "frozenset", "v": items}
            if isinstance(v, set):
                return {TAG: "set", "v": items}
            if cls == "PulseLog":
                return {TAG: "PulseLog", "v": items}
            return items
        if isinstance(v, (bytes, bytearray)):
            return {TAG: "bytes", "v": base64.b64encode(bytes(v)).decode("ascii")}
        if isinstance(v, PurePath):
            return str(v)
        if register_cm is not None and hasattr(v, "__enter__") and hasattr(v, "__exit__"):
            return {TAG: "cm", "id": register_cm(v)}
        return {TAG: "repr", "type": cls, "v": repr(v)}
    return enc(o)


def decode(o: Any, self_obj: Any = None, make_cm: Callable[[int], Any] | None = None) -> Any:
    """Inverse of encode(). `self_obj` stands in for a {"__ct__": "self"}
    reference; `make_cm(id)` builds the local handle for a context manager."""
    from ct.client._base import PulseLog, Result   # imported late: no cycle at import

    def dec(v: Any) -> Any:
        if isinstance(v, list):
            return [dec(x) for x in v]
        if not isinstance(v, dict):
            return v
        tag = v.get(TAG)
        if tag is None:                     # a plain dict that was never tagged
            return {k: dec(x) for k, x in v.items()}
        if tag == "self":
            return self_obj
        if tag in ("dict", "Result"):
            d = {_hashable(dec(k)): dec(x) for k, x in zip(v.get("k", []), v.get("v", []))}
            return Result(d) if tag == "Result" else d
        if tag == "tuple":
            return tuple(dec(x) for x in v.get("v", []))
        if tag == "set":
            return {_hashable(dec(x)) for x in v.get("v", [])}
        if tag == "frozenset":
            return frozenset(_hashable(dec(x)) for x in v.get("v", []))
        if tag == "PulseLog":
            return PulseLog(dec(x) for x in v.get("v", []))
        if tag == "bytes":
            return base64.b64decode(v.get("v", ""))
        if tag == "ndarray":
            try:
                import numpy as np
                return np.array(v.get("v"), dtype=v.get("dtype"))
            except Exception:
                return v.get("v")
        if tag == "cm" and make_cm is not None:
            return make_cm(int(v["id"]))
        if tag == "repr":
            return f"<{v.get('type')}: {v.get('v')}>"
        return f"<unknown remote value {tag!r}>"
    return dec(o)


def _hashable(k: Any) -> Any:
    return tuple(_hashable(x) for x in k) if isinstance(k, list) else k
