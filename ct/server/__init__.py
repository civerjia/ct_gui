"""backend.py -- the HTTP server that owns the controller links. Start it with
`python backend.py` (repository root) or `python -m ct.server`."""
from ct import rig_env as _rig_env

# state/rig_env.toml before _server reads its environment (see ct/rig_env.py)
RIG_ENV = _rig_env.load()

from ._server import main  # noqa: E402,F401
