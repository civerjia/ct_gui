"""Every directory the software reads or writes, in one place.

All of them hang off ROOT -- the repository root (the directory holding
backend.py) -- not off whichever module happens to use them, so moving a module
inside the package can never move the logs or the operator's saved state.
Runtime directories are git-ignored (see .gitignore).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

WEB_DIR = ROOT / "web"                  # the browser UI served by backend.py
LOG_DIR = ROOT / "logs"                 # backend.log, client/*.jsonl call records
CLIENT_LOG_DIR = LOG_DIR / "client"
CALIB_DIR = ROOT / "calibration"        # emission-current calibration records
STATE_DIR = ROOT / "state"              # operator decisions that must outlive a restart
RECORD_DIR = ROOT / "recordings"        # ADC recordings
RUN_REPORT_DIR = ROOT / "run_reports"   # end-of-run reports
