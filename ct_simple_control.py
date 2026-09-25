"""Compatibility name for the Python API.

    from ct_simple_control import CTClient      # keeps working

The code lives in ct/client/. This module IS that module (not a copy): every
name, private ones included, and any attribute a script patches, are shared.
"""
import sys

from ct.client import _client
# The names, STATICALLY, for editors: Pylance never runs the sys.modules swap
# below, so without these `from ct_simple_control import CTClient` was an
# "unknown import symbol" and goto definition found nothing on the whole API.
from ct.client._client import *  # noqa: F401,F403,E402
from ct.client._client import CTClient, CTLeaseError, PulseLog, Result  # noqa: F401,E402

sys.modules[__name__] = _client
