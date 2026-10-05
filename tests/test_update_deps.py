#!/usr/bin/env python3
"""An update that needs a package this machine does not have is not applied
until the package is there (2026-10-05: an update added `requests` to code the
other machine's Python could not import). Offline: GitHub, pip and the file
writes are stand-ins.

    python3 tests/test_update_deps.py
"""
import _path  # noqa: F401

import os
import subprocess

from ct import update as U

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  ({detail})" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


REQ = b"""# header
requests>=2.28    # the client
matplotlib>=3.6   # OPTIONAL -- only plots
numpy[extra]==1.26 ; python_version >= "3.9"
"""
specs = U.required_specs(REQ)
check("OPTIONAL lines and comments are skipped",
      [n for _s, n in specs] == ["requests", "numpy"], str(specs))
check("installed packages are not missing", U.missing_specs([("pip", "pip")]) == [])
check("an absent package is missing",
      U.missing_specs([("ct-no-such-pkg>=1", "ct-no-such-pkg")]) == ["ct-no-such-pkg>=1"])

os.environ["CT_NO_AUTO_INSTALL"] = "1"
why = U.ensure_requirements("ct-no-such-pkg>=1\n", "version abc1234")
check("CT_NO_AUTO_INSTALL: reported, nothing installed",
      why and "ct-no-such-pkg" in why and "CT_NO_AUTO_INSTALL" in why, str(why))
del os.environ["CT_NO_AUTO_INSTALL"]

ran = []
real_run = subprocess.run


def fake_pip(cmd, **kw):
    ran.append(cmd)
    return subprocess.CompletedProcess(cmd, 1, "", "ERROR: no network")


subprocess.run = fake_pip
why = U.ensure_requirements("ct-no-such-pkg>=1\n", "version abc1234")
subprocess.run = real_run
check("missing -> pip install with THIS interpreter",
      ran and ran[0][:4] == [U.sys.executable, "-m", "pip", "install"] and "ct-no-such-pkg>=1" in ran[0],
      str(ran))
check("pip failed -> the reason says how to install by hand",
      why and "installing failed" in why and "pip install ct-no-such-pkg" in why, str(why))


# The whole update: the new version needs a package that cannot be installed.
applied = []
os.environ.pop("CT_NO_AUTO_UPDATE", None)
saved = {k: getattr(U, k) for k in ("_is_dev_copy", "_latest_commit", "_download", "apply_update",
                                    "_manifest", "version", "_warn")}
warned = []
U._is_dev_copy = lambda: False
U._latest_commit = lambda: "f" * 40
U._manifest = lambda: {"commit": "a" * 40, "files": {}}
U.version = lambda: {"commit": "aaaaaaa", "dirty": False}
U._download = lambda sha: {"requirements.txt": b"ct-no-such-pkg>=1\n", "x.py": b"new"}
U.apply_update = lambda sha, files: applied.append(sha) or {"changed": ["x.py"], "removed": [],
                                                            "backed_up": [], "backup_dir": ""}
_real_warn = saved["_warn"]
U._warn = lambda m, level="info": (warned.append(m), _real_warn(m, level))
os.environ["CT_NO_AUTO_INSTALL"] = "1"
try:
    U.check_and_update()
finally:
    for k, v in saved.items():
        setattr(U, k, v)
    del os.environ["CT_NO_AUTO_INSTALL"]
    os.environ["CT_NO_AUTO_UPDATE"] = "1"
check("an update whose package is missing is NOT applied", applied == [], str(applied))
check("...and says so, naming the package and the version kept",
      any("NOT applied" in w and "ct-no-such-pkg" in w and "aaaaaaa" in w for w in warned), str(warned))
check("...and the reason is kept for the GUI and backend.log",
      U.LAST_UPDATE_PROBLEM and "ct-no-such-pkg" in U.LAST_UPDATE_PROBLEM, str(U.LAST_UPDATE_PROBLEM))
check("...as a warning in this run's messages",
      any(lvl == "warning" and "UPDATE FAILED" in m for lvl, m in U.RUN_MESSAGES), str(U.RUN_MESSAGES[-2:]))

print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
raise SystemExit(1 if FAILS else 0)
