"""The Python API: CTClient and everything a script imports with it.

    from ct.client import CTClient          # or, as before:
    from ct_simple_control import CTClient
"""
import inspect as _inspect

from . import _client
from ._client import *  # noqa: F401,F403
from ._client import CTClient, Result  # noqa: F401  (explicit for readers/linters)

# The public API, for `from ct.client import *` and for the API reference
# (scripts/make_api_docs.sh): without __all__, pdoc treats CTClient as a name
# imported from elsewhere and leaves it off this page. Public = no leading
# underscore, defined by the client itself (not a module or a stdlib name the
# client merely imports).
__all__ = sorted(
    _n for _n, _v in vars(_client).items()
    if not _n.startswith("_") and not _inspect.ismodule(_v)
    and (getattr(_v, "__module__", None) or "ct.client").startswith("ct.client")
)
