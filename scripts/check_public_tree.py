"""Check publishable paths and obvious secret material without printing values."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
raw = subprocess.check_output(["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"], cwd=ROOT)
paths = sorted(set(filter(None, raw.decode().split("\0"))))
failures = []
forbidden_paths = re.compile(r"(^|/)(local_private|external|\.venv|xcuserdata)/|config(?:\.[^/]+)?\.local\.yaml$|\.(?:p8|p12|pem|key|mobileprovision|db|sqlite3?)$")
secret = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|sk-[A-Za-z0-9_-]{30,})\b")
home = re.compile(r"/(?:Users|home)/([^/\s`\"']+)")
for name in paths:
    path = ROOT / name
    if forbidden_paths.search(name) or path.is_symlink():
        failures.append(f"{name}: forbidden publishable path")
        continue
    data = path.read_bytes()
    if b"\0" in data:
        continue
    text = data.decode("utf-8", errors="replace")
    for line_number, line in enumerate(text.splitlines(), 1):
        if secret.search(line):
            failures.append(f"{name}:{line_number}: possible secret")
        if any(m.group(1) not in {"validator", "example", "<user>"} for m in home.finditer(line)):
            failures.append(f"{name}:{line_number}: personal home path")
    if path.suffix == ".md":
        for target in re.findall(r"(?<!!)\[[^\]]*\]\(([^\s)]+)\)", text):
            if "://" in target or target.startswith(("#", "mailto:")):
                continue
            if not (path.parent / target.split("#", 1)[0]).exists():
                failures.append(f"{name}: broken local link {target}")
if failures:
    print("\n".join(failures))
    sys.exit(1)
print(f"Public tree checks passed for {len(paths)} files (not a full content audit).")
