#!/usr/bin/env python3
"""Every module whose names are star-imported keeps a LITERAL __all__ in step.

WHY THIS EXISTS. ct/client/_base.py and the ct/server/_*.py modules are
star-imported by their siblings, underscore names included, so each lists
EVERY global in __all__. That list used to be computed:

    __all__ = [_n for _n in list(globals()) if not _n.startswith("__")]

which is always right at run time and invisible to an editor: Pylance/pyright
read __all__ statically, could not evaluate it, and reported every
star-imported helper as "not defined" -- goto definition stopped working
across the package (2026-09-25). The lists are now literal. This test is the
price: add a name to one of these modules and forget __all__, and it fails.

    python3 tests/test_star_exports.py
"""

import _path  # noqa: F401  -- makes the ct package importable from tests/

import importlib
import os
import sys

os.environ.setdefault("CT_NO_AUTO_UPDATE", "1")

MODULES = ["ct.client._base"] + [
    f"ct.server.{m}" for m in ("_access", "_common", "_link", "_logfiles", "_mapping",
                               "_monitor", "_reads", "_recording", "_safety",
                               "_schedule", "_shared", "_wire")]


def main() -> int:
    problems = []
    for name in MODULES:
        mod = importlib.import_module(name)
        want = {n for n in vars(mod) if not n.startswith("__")}
        have = set(mod.__all__)
        missing, extra = sorted(want - have), sorted(have - want)
        if missing:
            problems.append(f"{name}: defined but not in __all__ (siblings cannot see them): {missing}")
        if extra:
            problems.append(f"{name}: in __all__ but not defined: {extra}")
    if problems:
        print("FAIL")
        for p in problems:
            print("  -", p)
        return 1
    print(f"PASS: {len(MODULES)} modules, every global listed in __all__")
    return 0


if __name__ == "__main__":
    sys.exit(main())
