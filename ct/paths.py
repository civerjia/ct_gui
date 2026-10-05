"""Every directory the software reads or writes, in one place.

All of them hang off ROOT -- the repository root (the directory holding
backend.py) -- not off whichever module happens to use them, so moving a module
inside the package can never move the logs or the operator's saved state.
Runtime directories are git-ignored (see .gitignore).
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WEB_DIR = ROOT / "web"                  # the browser UI served by backend.py
LOG_DIR = ROOT / "logs"                 # backend.log, client/*.jsonl call records
CLIENT_LOG_DIR = LOG_DIR / "client"
CALIB_DIR = ROOT / "calibration"        # emission-current calibration records
# operator decisions that must outlive a restart (dead filaments, filament
# order). CT_STATE_DIR moves it -- the tests point it at a scratch directory so
# they never read or overwrite the bench's real decisions.
STATE_DIR = Path(os.environ["CT_STATE_DIR"]) if os.environ.get("CT_STATE_DIR") else ROOT / "state"
RECORD_DIR = ROOT / "recordings"        # ADC recordings
RUN_REPORT_DIR = ROOT / "run_reports"   # end-of-run reports
