"""Self-update from GitHub, at startup only -- without git.

    from ct import update; update.check_and_update()

ct_simple_control and backend.py call this once, when they start -- BEFORE
anything talks to hardware. It asks GitHub which commit is current; if this
copy is not that commit, it downloads that commit's files and writes every one
that differs, then restarts the running script with the same arguments, so the
new code is what actually runs.

WHY NOT git pull. It used to fast-forward a git clone, which made the update
only as healthy as the local .git: a clone inside OneDrive whose .git the sync
had damaged, or a shell without git on PATH, silently never updated again
(.218, 2026-09-25). GitHub is the truth; the local copy is made to match it,
whatever state it is in. Only the Python standard library is used.

WHAT IS WRITTEN, AND WHAT NEVER IS.
  - Only files that exist on GitHub are written. calibration/, state/, logs/,
    recordings, run reports and your own *_local.py scripts are never touched.
  - A file whose content differs is overwritten; if it had been EDITED here
    (it matches neither the new version nor the one last installed), the old
    content is first copied to logs/update_backup/<time>/ -- nothing is lost
    without a trace.
  - A file that GitHub no longer has, and that the last update installed, is
    MOVED into that backup (a stale module could otherwise shadow a new one).
  - .ct_version (commit + per-file hashes of what was installed) records the
    installed version.

SKIPPED -- each says why, nothing is silent:
  - CT_NO_AUTO_UPDATE=1                 (by request)
  - the development copy: this directory sits inside a larger git repository
    (the firmware repo's tools/ct_gui) -- it is the SOURCE of what goes to
    GitHub and must never be overwritten from it
  - GitHub unreachable / timed out      -> warn, carry on with the code here
  - an interactive session (python -i, IPython, Jupyter) -> update the files,
    then ask for a manual restart: the interpreter's state cannot be carried

It never updates mid-session. The restarted process gets CT_UPDATED=1 so it
does not check (and restart) a second time.

Every update, skip and failure is appended to logs/update.log (time, host, the
script that started it) -- readable from another machine through the backend:
ct.read_log("update.log"). An up-to-date check prints one line
("[ct_update] up to date (<commit>)") and writes nothing to the log.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parents[1]   # the repository root (this file is ct/update.py)
GITHUB_REPO = os.environ.get("CT_UPDATE_REPO", "civerjia/ct_gui")
GITHUB_BRANCH = os.environ.get("CT_UPDATE_BRANCH", "main")
FETCH_TIMEOUT_S = 5.0
DOWNLOAD_TIMEOUT_S = 60.0
MANIFEST = REPO_DIR / ".ct_version"
UPDATE_LOG = REPO_DIR / "logs" / "update.log"
BACKUP_DIR = REPO_DIR / "logs" / "update_backup"


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


# ---- what is installed here -------------------------------------------------

def _sha(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def _manifest() -> dict:
    try:
        m = json.loads(MANIFEST.read_text(encoding="utf-8"))
        return m if isinstance(m, dict) and isinstance(m.get("files"), dict) else {}
    except (OSError, ValueError):
        return {}


def _is_dev_copy() -> bool:
    """This directory is inside a larger git repository and is not a repo of
    its own: the development copy, the source of what goes to GitHub."""
    if (REPO_DIR / ".git").exists():
        return False
    return any((p / ".git").exists() for p in REPO_DIR.parents)


def _git_files() -> list[str] | None:
    """The tracked files under this directory, via git -- the dev copy only."""
    try:
        r = subprocess.run(["git", "-C", str(REPO_DIR), "ls-files", "-z", "--", "."],
                           capture_output=True, timeout=10.0)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    return [f for f in r.stdout.decode("utf-8", "replace").split("\0") if f]


def version() -> dict:
    """{"commit", "tree", "dirty"} of the code in this directory.

    "tree" is a hash of the CONTENT of the published files (path + bytes), so
    two copies with the same code have the same tree however they got it --
    installed from a GitHub download, or the development copy the public repo
    is split out of (whose commit hashes differ). Compare versions by "tree";
    "commit" is for display. "dirty": a file differs from what was installed."""
    m = _manifest()
    files = list(m.get("files") or {}) if m else (_git_files() or [])
    h = hashlib.sha1()
    dirty = False
    for rel in sorted(files):
        try:
            fs = _sha((REPO_DIR / rel).read_bytes())
        except OSError:
            fs = "missing"
        h.update(f"{rel}\0{fs}\n".encode())
        if m and m["files"].get(rel) != fs:
            dirty = True
    commit = (m.get("commit") or "")[:7] or None
    if commit is None and _is_dev_copy():
        try:
            r = subprocess.run(["git", "-C", str(REPO_DIR), "rev-parse", "--short", "HEAD"],
                               capture_output=True, text=True, timeout=5.0)
            commit = r.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            pass
    return {"commit": commit, "tree": h.hexdigest()[:10] if files else None, "dirty": dirty}


# ---- restart ----------------------------------------------------------------

_CHILD_ENV = "CT_RESTART_CHILD"
_RESTART_CODE = 75   # a child exiting with this asks its parent to start it again


def restart_in_place() -> None:
    """Run this program again, in the SAME terminal window, and never return.

    POSIX: os.execv replaces the process -- same PID, same window.
    Windows: os.execv is only emulated there -- it starts a NEW process and
    the old one exits, so cmd/PowerShell sees the program as finished, prints
    its prompt, and Ctrl-C / closing the window no longer reliably reach the
    new one; started any other way it can end up with no window at all. So on
    Windows the old process starts the new one as its child in the same
    console and WAITS for it, exiting with its code: the window keeps showing
    one running program. The caller must already have let go of every socket.
    """
    sys.stdout.flush()
    sys.stderr.flush()
    argv = [sys.executable, *sys.argv]
    # CT_RESTART_AS_CHILD=1 takes the Windows path anywhere (to test it).
    if os.name == "nt" or os.environ.get("CT_RESTART_AS_CHILD"):
        if os.environ.get(_CHILD_ENV):
            # Already the child of the process that owns the window: ask IT to
            # start the next one, rather than nesting a grandchild -- every
            # restart would otherwise leave one more process waiting.
            os._exit(_RESTART_CODE)
        env = dict(os.environ, **{_CHILD_ENV: "1"})
        while True:
            try:
                rc = subprocess.call(argv, env=env)
            except KeyboardInterrupt:
                rc = 130
            if rc != _RESTART_CODE:
                os._exit(rc)
    os.execv(sys.executable, argv)


def _interactive() -> bool:
    return (hasattr(sys, "ps1") or "IPython" in sys.modules or "ipykernel" in sys.modules
            or not sys.argv or sys.argv[0] in ("", "-c"))


# ---- GitHub -----------------------------------------------------------------

def _http(url: str, timeout: float) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "ct_gui-update",
                                               "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _latest_commit() -> str:
    data = json.loads(_http(f"https://api.github.com/repos/{GITHUB_REPO}/commits/{GITHUB_BRANCH}",
                            FETCH_TIMEOUT_S))
    return str(data["sha"])


def _download(sha: str) -> dict[str, bytes]:
    """{relative path: bytes} of every file in that commit."""
    raw = _http(f"https://codeload.github.com/{GITHUB_REPO}/zip/{sha}", DOWNLOAD_TIMEOUT_S)
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for name in z.namelist():
            if name.endswith("/") or "/" not in name:
                continue
            rel = name.split("/", 1)[1]          # strip "<repo>-<sha>/"
            if rel:
                files[rel] = z.read(name)
    return files


def _write(path: Path, data: bytes) -> None:
    """Write, retrying briefly: a sync client (OneDrive) can hold a file open."""
    path.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(10):
        try:
            tmp = path.with_name(path.name + ".ct_update_tmp")
            tmp.write_bytes(data)
            os.replace(tmp, path)
            return
        except OSError:
            if attempt == 9:
                raise
            time.sleep(0.3)


def apply_update(sha: str, files: dict[str, bytes]) -> dict:
    """Make this directory match `files` (one commit). Returns what changed."""
    old = _manifest()
    old_files = old.get("files") or {}
    stamp = time.strftime("%Y%m%d_%H%M%S")
    changed, backed_up, removed = [], [], []

    def backup(rel: str) -> None:
        src = REPO_DIR / rel
        dst = BACKUP_DIR / stamp / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        backed_up.append(rel)

    for rel, data in sorted(files.items()):
        target = REPO_DIR / rel
        new_sha = _sha(data)
        try:
            cur = target.read_bytes()
        except OSError:
            cur = None
        if cur is not None and _sha(cur) == new_sha:
            continue
        # Edited here = matches neither the new file nor what was installed.
        if cur is not None and old_files.get(rel) != _sha(cur):
            backup(rel)
        _write(target, data)
        changed.append(rel)
    for rel in sorted(set(old_files) - set(files)):
        target = REPO_DIR / rel
        if target.exists():
            backup(rel)
            target.unlink()
            removed.append(rel)
    MANIFEST.write_text(json.dumps({"commit": sha, "installed": time.strftime("%Y-%m-%d %H:%M:%S"),
                                    "files": {rel: _sha(d) for rel, d in files.items()}},
                                   indent=1), encoding="utf-8")
    return {"changed": changed, "backed_up": backed_up, "removed": removed,
            "backup_dir": str(BACKUP_DIR / stamp) if backed_up else None}


def check_and_update() -> None:
    if os.environ.get("CT_UPDATED"):
        return                                   # the process an update just restarted
    if os.environ.get("CT_NO_AUTO_UPDATE"):
        print("[ct_update] update check off (CT_NO_AUTO_UPDATE)", file=sys.stderr)
        return
    if _is_dev_copy():
        return        # the development copy: the source of GitHub, never overwritten from it
    try:
        sha = _latest_commit()
    except (OSError, ValueError, KeyError, urllib.error.URLError) as exc:
        _warn(f"update check skipped: could not reach GitHub ({type(exc).__name__}: {exc}) -- "
              f"carrying on with the code here")
        return
    m = _manifest()
    ver = version()
    if m.get("commit") == sha and not ver["dirty"]:
        print(f"[ct_update] up to date ({sha[:7]})", file=sys.stderr)   # not logged: every start
        return
    try:
        files = _download(sha)
    except (OSError, ValueError, zipfile.BadZipFile, urllib.error.URLError) as exc:
        _warn(f"update check found {sha[:7]} but the download failed ({type(exc).__name__}: {exc}) -- "
              f"carrying on with the code here")
        return
    try:
        res = apply_update(sha, files)
    except OSError as exc:
        _warn(f"update to {sha[:7]} failed while writing files ({exc}) -- some files may be "
              f"new and some old; run it again (restart) to finish")
        return
    was = (m.get("commit") or "")[:7] or "an untracked copy"
    if not res["changed"] and not res["removed"]:
        # Content already matched; only the record was missing or stale.
        print(f"[ct_update] up to date ({sha[:7]}); recorded", file=sys.stderr)
        return
    lines = [f"updated {was} -> {sha[:7]}: {len(res['changed'])} file(s) written, "
             f"{len(res['removed'])} removed"]
    if res["backed_up"]:
        lines.append(f"local edits kept in {res['backup_dir']}: {', '.join(res['backed_up'][:8])}"
                     + (" ..." if len(res["backed_up"]) > 8 else ""))
    _warn("\n".join(lines))
    if _interactive():
        _warn("interactive session: RESTART it to run the new code "
              "(what is loaded now is still the old version)")
        return
    _warn("restarting with the new version...")
    os.environ["CT_UPDATED"] = "1"
    restart_in_place()
