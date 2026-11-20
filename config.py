"""Local settings for the Windows runner."""

import os
import shutil
import sys
from pathlib import Path

HOST = os.environ.get("RUNNER_HOST", "127.0.0.1")
PORT = int(os.environ.get("RUNNER_PORT", "8080"))
DATA_DIR = Path(os.environ.get("RUNNER_DATA_DIR", Path(__file__).parent / ".winrunner-data"))
DEFAULT_TIMEOUT = 10.0
MAX_REQUEST_BYTES = 1024 * 1024

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
