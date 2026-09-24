"""Self-update from GitHub, at startup only.

    from ct import update; update.check_and_update()

ct_simple_control and backend.py call this once, when they start -- BEFORE
anything talks to hardware. If this directory is a git clone whose upstream
branch has new commits, it fast-forwards to them and restarts the running
script with the same arguments, so the new code is what actually runs.

It never updates mid-session: a restart in the middle of a run would drop
whatever the script was controlling. Everything that is not a clean,
fast-forward update is a WARNING and the script carries on with the code it
has:

  - not a standalone clone (e.g. the development copy inside the firmware repo)
    or no upstream branch            -> silently skipped
  - git not installed / network down / fetch timed out -> warn, continue
  - local edits to tracked files     -> warn, continue (never overwritten)
  - local commits not on GitHub      -> warn, continue (diverged; never reset)
  - interactive session (python -i, IPython, Jupyter) -> pull, then ask for a
    manual restart: the interpreter's state cannot be carried over

Turn it off with the environment variable CT_NO_AUTO_UPDATE=1. The restarted
process gets CT_UPDATED=1 so it never checks (and restarts) a second time.

Every update, skip and failure is also appended to logs/update.log (time,
host, the script that started it) -- readable from another machine through
the backend: ct.read_log("update.log"). An up-to-date check writes nothing.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]   # the repository root (this file is ct/update.py)
FETCH_TIMEOUT_S = 5.0


def _git(*args: str, timeout: float = 10.0) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(REPO_DIR), *args], capture_output=True,
                          text=True, timeout=timeout)


UPDATE_LOG = REPO_DIR / "logs" / "update.log"


def _record(msg: str) -> None:
    """Append one line to logs/update.log. Never raises: a log that cannot be
    written must not stop the script it is describing."""
    try:
        UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
        script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else "(interactive)"
        with open(UPDATE_LOG, "a", encoding="utf-8") as fh:
            for i, line in enumerate(msg.splitlines() or [""]):
                prefix = (f"{time.strftime('%Y-%m-%d %H:%M:%S')} {socket.gethostname()} {script}: "
                          if i == 0 else "    ")
                fh.write(prefix + line.strip() + "\n")
    except OSError:
        pass


def _warn(msg: str) -> None:
    print(f"[ct_update] {msg}", file=sys.stderr)
    _record(msg)


def version() -> dict:
    """{"commit", "tree", "dirty"} of the code in this directory.

    Compare versions by "tree": the git hash of this directory's CONTENT. The
    public repo is a rewritten copy of a directory in the development repo
    (split out, author rewritten), so identical code has different COMMIT
    hashes in the two -- but the same tree hash. "commit" is for display."""
    out = {"commit": None, "tree": None, "dirty": False}
    try:
        head = _git("rev-parse", "--short", "HEAD")
        if head.returncode != 0:
            return out
        tree = _git("rev-parse", "--short", "HEAD:./")
        out["commit"] = head.stdout.strip()
        out["tree"] = tree.stdout.strip() if tree.returncode == 0 else None
        out["dirty"] = bool(_git("status", "--porcelain", "--untracked-files=no", "--", ".")
                            .stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return out


def _interactive() -> bool:
    return (hasattr(sys, "ps1") or "IPython" in sys.modules or "ipykernel" in sys.modules
            or not sys.argv or sys.argv[0] in ("", "-c"))


def check_and_update() -> None:
    if os.environ.get("CT_NO_AUTO_UPDATE") or os.environ.get("CT_UPDATED"):
        return
    try:
        top = _git("rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError):
        return                      # git not installed: nothing to update with
    if top.returncode != 0 or Path(top.stdout.strip()).resolve() != REPO_DIR:
        return                      # not a standalone clone of this directory
    upstream = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    if upstream.returncode != 0:
        return                      # no upstream branch configured
    try:
        fetched = _git("fetch", "--quiet", timeout=FETCH_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        _warn(f"update check skipped: fetch took longer than {FETCH_TIMEOUT_S:g} s")
        return
    if fetched.returncode != 0:
        lines = [ln for ln in fetched.stderr.strip().splitlines() if ln.strip()]
        why = next((ln for ln in lines if ln.startswith("fatal:")), lines[0] if lines else "fetch failed")
        _warn(f"update check skipped: could not reach GitHub ({why})")
        return
    behind = int(_git("rev-list", "--count", "HEAD..@{u}").stdout.strip() or 0)
    if behind == 0:
        return
    ahead = int(_git("rev-list", "--count", "@{u}..HEAD").stdout.strip() or 0)
    if ahead:
        _warn(f"{behind} new commit(s) on GitHub, but this copy has {ahead} local commit(s) "
              f"that are not there -- not updating. Resolve by hand (git pull --rebase).")
        return
    if _git("status", "--porcelain", "--untracked-files=no").stdout.strip():
        _warn(f"{behind} new commit(s) on GitHub, but tracked files here have local edits -- "
              f"not updating (nothing was overwritten). Commit or discard them, then git pull.")
        return
    old = version()["commit"]
    log = _git("log", "--oneline", "HEAD..@{u}").stdout.strip()
    pulled = _git("pull", "--ff-only", "--quiet", timeout=60.0)
    if pulled.returncode != 0:
        _warn(f"update failed, carrying on with {old}: {pulled.stderr.strip()}")
        return
    new = version()["commit"]
    _warn(f"updated {old} -> {new} ({behind} commit(s)):\n  " + log.replace("\n", "\n  "))
    if _interactive():
        _warn("interactive session: RESTART it to run the new code "
              "(what is loaded now is still the old version)")
        return
    _warn("restarting with the new version...")
    sys.stdout.flush()
    sys.stderr.flush()
    os.environ["CT_UPDATED"] = "1"
    os.execv(sys.executable, [sys.executable, *sys.argv])
