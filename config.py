"""Local settings for the Windows runner."""

import os
import shutil
import sys
from pathlib import Path

# Replace the placeholder on the deployed machine before starting the runner.
TOKEN = "123456"
HOST = "0.0.0.0"
PORT = 18080
DATA_DIR = Path(__file__).resolve().parent / ".winrunner-data"
DEFAULT_TIMEOUT = 10.0
MAX_REQUEST_BYTES = 1024 * 1024
MAX_HTTP_CONNECTIONS = 64
PROJECT_DRAIN_TIMEOUT = 5.0  # Seconds to release HTTP readers / stop work before project deletion.

# Aliases map to (absolute executable path, fixed argument tuple). An absent
# optional executable is reported as a launch error, not an unknown alias.
INTERPRETERS = {
    "python": (sys.executable, ("-u",)),
    "python3": (sys.executable, ("-u",)),
    "node": (shutil.which("node.exe"), ()),
}
GIT = shutil.which("git.exe")
SYSTEM32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
CMD = SYSTEM32 / "cmd.exe"
POWERSHELL = SYSTEM32 / "WindowsPowerShell" / "v1.0" / "powershell.exe"
# Set False if local PowerShell policy does not permit loading script modules.
PS_SUPPRESS_PROGRESS = True
