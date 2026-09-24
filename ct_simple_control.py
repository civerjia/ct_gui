"""Compatibility name for the Python API.

    from ct_simple_control import CTClient      # keeps working

The code lives in ct/client/. This module IS that module (not a copy): every
name, private ones included, and any attribute a script patches, are shared.
"""
import sys

from ct.client import _client

sys.modules[__name__] = _client
