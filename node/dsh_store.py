"""The v1 node's private, durable task journal and event outbox."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

TERMINAL = {"done", "failed", "cancelled", "timed_out", "interrupted"}


class Journal:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(path)
        os.chmod(path, 0o600)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS outbox(task_id TEXT NOT NULL, seq INTEGER NOT NULL,
          message TEXT NOT NULL, PRIMARY KEY(task_id,seq));
        """)

    def get(self, task_id: str) -> dict | None:
        row = self.db.execute("SELECT data FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> list[dict]:
        return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM tasks ORDER BY rowid")]

    def put(self, task: dict) -> None:
        with self.db:
            self.db.execute("INSERT INTO tasks VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET data=excluded.data",
                            (task["task_id"], json.dumps(task, ensure_ascii=False)))

    def update(self, task_id: str, **fields) -> dict:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        task.update(fields)
        self.put(task)
        return task

    def event(self, task_id: str, event: dict, ts: str) -> dict:
        task = self.get(task_id)
        seq = task.get("last_persisted_seq", 0) + 1
        message = {"type": "task_event", "task_id": task_id, "event_seq": seq, "event": event, "ts": ts}
        task["last_persisted_seq"] = seq
        with self.db:
            self.db.execute("UPDATE tasks SET data=? WHERE task_id=?", (json.dumps(task, ensure_ascii=False), task_id))
            self.db.execute("INSERT INTO outbox VALUES(?,?,?)", (task_id, seq, json.dumps(message, ensure_ascii=False)))
        return message

    def pending(self, task_id: str | None = None) -> list[dict]:
        query = "SELECT message FROM outbox"
        args = ()
        if task_id:
            query += " WHERE task_id=?"
            args = (task_id,)
        return [json.loads(row[0]) for row in self.db.execute(query + " ORDER BY task_id,seq", args)]

    def acknowledge(self, task_id: str, seq: int) -> None:
        task = self.get(task_id)
        if task is None or not isinstance(seq, int) or seq < 0 or seq > task.get("last_persisted_seq", 0):
            return
        with self.db:
            self.db.execute("DELETE FROM outbox WHERE task_id=? AND seq<=?", (task_id, seq))

    def close(self):
        self.db.close()
