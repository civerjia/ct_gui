"""Makes `ct_simple_control` / `net_protocol` importable from this directory.

These scripts used to sit next to those modules, so a bare
`from ct_simple_control import CTClient` resolved via the script's own
directory. Moving them one level down broke that, and it broke it in the
confusing direction: the import fails only when the script is run from
somewhere other than tools/ct_gui, so it looks like a machine problem.

Import this first, before any local module:

    import _path  # noqa: F401
    from ct_simple_control import CTClient
"""
import os
import sys

_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PARENT not in sys.path:
    sys.path.insert(0, _PARENT)
