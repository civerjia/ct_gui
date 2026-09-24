"""The Python API: CTClient and everything a script imports with it.

    from ct.client import CTClient          # or, as before:
    from ct_simple_control import CTClient
"""
from ._client import *  # noqa: F401,F403
from ._client import CTClient, Result  # noqa: F401  (explicit for readers/linters)
