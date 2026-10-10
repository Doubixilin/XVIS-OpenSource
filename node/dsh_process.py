"""Observe actual descendants, including DSH tools in detached process groups.

Never infer an empty process group from a failed query.  Birth identity prevents
recovery from signalling an unrelated process which happens to reuse a PID.
"""
from __future__ import annotations

import asyncio
import os
import signal
import subprocess


def process_table() -> list[dict] | None:
    try:
        result = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid=,lstart=,comm="],
                                capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C"})
        if result.returncode or result.stderr.strip() or not result.stdout.strip():
            return None
        rows = []
        for line in result.stdout.splitlines():
            parts = line.strip().split(maxsplit=8)
            if len(parts) != 9:
                return None
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]),
                         "birth": " ".join(parts[3:8]), "command": parts[8]})
        return rows
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def same_process(a: dict, b: dict) -> bool:
    return a["pid"] == b["pid"] and a["birth"] == b["birth"] and a["pgid"] == b["pgid"]


class ProcessTracker:
    def __init__(self, records: list[dict] | None = None):
        self.records = {p["pid"]: p for p in records or []}
        self.verified = False
        self._bootstrap_allowed = not self.records

    def observe(self, root_pid: int, rows: list[dict] | None = None) -> bool:
        bootstrap = self._bootstrap_allowed
        self._bootstrap_allowed = False
        rows = process_table() if rows is None else rows
        self.verified = rows is not None
        if rows is None:
            return False
        by_pid = {p["pid"]: p for p in rows}
        owned = {pid for pid, old in self.records.items() if pid in by_pid and same_process(old, by_pid[pid])}
        if not self.records and bootstrap and root_pid in by_pid:
            owned.add(root_pid)
        while True:
            new = {p["pid"] for p in rows if p["ppid"] in owned}
            if new <= owned:
                break
            owned |= new
        for pid in owned:
            self.records[pid] = by_pid[pid]
        return True

    def survivors(self) -> int:
        rows = process_table()
        self.verified = rows is not None
        if rows is None:
            return -1
        if not self.records:
            return -1  # Spawned work with no saved identity is not proven empty.
        by_pid = {p["pid"]: p for p in rows}
        observed_groups = {old["pgid"] for old in self.records.values()}
        # Count every previously observed group, even after its known member has
        # exited. A remaining unobserved member can be an orphan or PGID reuse:
        # without a matching birth identity we cannot safely signal or call it empty.
        matched_groups = {old["pgid"] for pid, old in self.records.items()
                          if pid in by_pid and same_process(old, by_pid[pid])}
        present_groups = {p["pgid"] for p in rows if p["pgid"] in observed_groups}
        if present_groups - matched_groups:
            return -1
        return sum(p["pgid"] in observed_groups for p in rows)

    def signal(self, sig: int) -> bool:
        rows = process_table()
        if rows is None:
            self.verified = False
            return False
        by_pid = {p["pid"]: p for p in rows}
        groups = {old["pgid"] for pid, old in self.records.items()
                  if pid in by_pid and same_process(old, by_pid[pid])}
        for group in groups:
            if group == os.getpgrp():
                raise RuntimeError("refusing to signal the node's own process group")
            try:
                os.killpg(group, sig)
            except ProcessLookupError:
                pass
        return True

    async def stop(self, root_pid: int, persist, grace: float = 5.0) -> int:
        if not self.observe(root_pid):
            return -1
        persist(list(self.records.values()))
        if not self.signal(signal.SIGTERM):
            return -1
        deadline = asyncio.get_running_loop().time() + grace
        while asyncio.get_running_loop().time() < deadline:
            self.observe(root_pid)
            persist(list(self.records.values()))
            if self.survivors() == 0:
                return 0
            await asyncio.sleep(0.05)
        if not self.observe(root_pid):
            return -1
        persist(list(self.records.values()))
        if not self.signal(signal.SIGKILL):
            return -1
        for _ in range(100):
            count = self.survivors()
            if count == 0:
                return 0
            if count < 0:
                return -1
            await asyncio.sleep(0.05)
        return self.survivors()
