"""Run offline and loopback checks; never invokes a real model or remote Hub."""
from pathlib import Path
import subprocess
import sys

if sys.version_info < (3, 12):
    raise SystemExit("Python 3.12+ required; create the environment with python3.12 -m venv .venv")

ROOT = Path(__file__).resolve().parents[1]
for command in (
    [sys.executable, "scripts/check_public_tree.py"],
    [sys.executable, "scripts/verify_dsh_headless.py"],
    [sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", "verify_*.py"],
):
    subprocess.run(command, cwd=ROOT, check=True)
