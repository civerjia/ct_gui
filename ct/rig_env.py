"""Per-rig settings that live on the rig, not in the start script.

state/rig_env.toml holds the CT_* environment variables of THIS machine (board
constants such as the pulse shunt, port, load counts). It is read at start-up,
before the server module reads its environment, so a value changed there takes
effect on the next restart -- and a restart can be requested remotely
(POST /api/restart). Nothing has to be edited or double-clicked at the rig.

Why a file: on Windows a remote restart re-runs the backend with the
environment the window was first started with, so a variable added to the .bat
needed someone at the machine to close and reopen the window (2026-11-23,
control2). state/ is never written by the auto-update (ct/update.py), so the
file survives updates.

Format:

    [env]
    CT_PULSE_R_SENSE_OHM = "2.55"   # v3 HV board: 2x 5.1 ohm in parallel
    CT_PULSE_ADC_VREF_V = "3.3"

Values from the file win over the process environment (the file is the one
place that is edited remotely); every value applied, and every one that
replaces a different inherited value, is printed. Only CT_* names are taken.
A file that cannot be read or parsed stops the start-up with the reason: a rig
silently running on default board constants is worse than one that does not
start.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    tomllib = None

#: The settings file, relative to the repository root. source: this module.
RIG_ENV_PATH = Path(__file__).resolve().parents[1] / "state" / "rig_env.toml"

#: Only environment names with this prefix are taken from the file.
#: source: every ct_gui setting is a CT_* variable.
ENV_PREFIX = "CT_"

#: What the last load() applied (name -> value), for the log and /api/version.
APPLIED: dict[str, str] = {}


def load(path: Path = RIG_ENV_PATH) -> dict[str, str]:
    """Apply [env] of `path` to os.environ; returns what was applied.
    No file = nothing to do. Unreadable or malformed = SystemExit."""
    if not path.exists():
        return {}
    if tomllib is None:
        raise SystemExit(f"[rig_env] {path} needs Python 3.11+ (tomllib) to be read")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SystemExit(f"[rig_env] cannot read {path}: {exc}") from exc
    env = data.get("env", {})
    if not isinstance(env, dict):
        raise SystemExit(f"[rig_env] {path}: [env] must be a table, got {type(env).__name__}")
    applied: dict[str, str] = {}
    for name, value in env.items():
        if not name.startswith(ENV_PREFIX):
            raise SystemExit(f"[rig_env] {path}: {name!r} is not a {ENV_PREFIX}* setting")
        if isinstance(value, bool) or not isinstance(value, (str, int, float)):
            raise SystemExit(f"[rig_env] {path}: {name} must be a string or number, "
                             f"got {type(value).__name__}")
        text = str(value)
        old = os.environ.get(name)
        if old is not None and old != text:
            print(f"[rig_env] {name}={text} (replaces {old} from the environment)", file=sys.stderr)
        else:
            print(f"[rig_env] {name}={text}", file=sys.stderr)
        os.environ[name] = text
        applied[name] = text
    APPLIED.clear()
    APPLIED.update(applied)
    return applied
