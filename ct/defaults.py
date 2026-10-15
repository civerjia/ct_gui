"""Rig operating defaults -- defined ONCE.

The GUI takes these from the backend (GET /api/defaults) instead of writing
the numbers into its own code. Override on the backend with the environment
variable; the GUI follows without an edit.
"""
from __future__ import annotations

import os


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


#: IDLE heating current (mA) the GUI starts with (Heating "idle A", Idle all).
IDLE_MA: int = _int_env("CT_IDLE_MA", 1200)

#: Values IDLE_MA had before. Per-filament GUI settings saved in a browser that
#: still hold one of these held the DEFAULT, not a choice, and are moved to the
#: current IDLE_MA when loaded. Append when IDLE_MA changes; never remove.
RETIRED_IDLE_MA: tuple[int, ...] = (1500,)


def as_dict() -> dict:
    """What GET /api/defaults serves."""
    return {"idle_ma": IDLE_MA, "retired_idle_ma": list(RETIRED_IDLE_MA)}
