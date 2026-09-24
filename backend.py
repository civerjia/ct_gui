"""Start the backend:  python backend.py

The server lives in ct/server/_server.py. This file stays at the top level so
the command everyone already uses keeps working; importing it (`import
backend`) hands back that module itself.
"""
import sys

from ct.server import _server

if __name__ == "__main__":
    _server.main()
else:
    sys.modules[__name__] = _server
