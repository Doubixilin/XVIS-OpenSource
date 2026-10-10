"""独立 v1 表，不迁移或覆盖历史任务。"""
from __future__ import annotations
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS v1_nodes (
 node_id TEXT PRIMARY KEY, name TEXT NOT NULL, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS v1_projects (
 node_id TEXT NOT NULL, project_id TEXT NOT NULL, name TEXT NOT NULL,
 path TEXT NOT NULL, available INTEGER NOT NULL,
 PRIMARY KEY(node_id, project_id)
);
CREATE TABLE IF NOT EXISTS v1_tasks (
 task_id TEXT PRIMARY KEY, client_request_id TEXT NOT NULL UNIQUE,
 request_hash TEXT NOT NULL, status TEXT NOT NULL,
 node_id TEXT NOT NULL, project_id TEXT NOT NULL, prompt TEXT NOT NULL,
 timeout_sec INTEGER NOT NULL, follows_task_id TEXT,
 dsh_session_id TEXT, created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT,
 result_json TEXT, error_json TEXT, cancel_requested INTEGER NOT NULL DEFAULT 0,
 accepted INTEGER NOT NULL DEFAULT 0, acked_seq INTEGER NOT NULL DEFAULT 0,
 assistant_context_json TEXT, project_context_json TEXT, waiting_reason TEXT
);
CREATE TABLE IF NOT EXISTS v1_events (
 id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
 event_seq INTEGER, type TEXT NOT NULL, payload_json TEXT NOT NULL,
 created_at TEXT NOT NULL,
 UNIQUE(task_id, event_seq),
 FOREIGN KEY(task_id) REFERENCES v1_tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_v1_tasks_node_status ON v1_tasks(node_id,status,created_at);
CREATE INDEX IF NOT EXISTS idx_v1_events_task_id ON v1_events(task_id,id);
CREATE TABLE IF NOT EXISTS v1_images (
 image_id TEXT PRIMARY KEY, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, width INTEGER NOT NULL, height INTEGER NOT NULL,
 created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v1_task_images (
 task_id TEXT NOT NULL, image_id TEXT NOT NULL,
 direction TEXT NOT NULL CHECK(direction IN ('input','output')), position INTEGER NOT NULL,
 PRIMARY KEY(task_id,image_id), UNIQUE(task_id,direction,position),
 FOREIGN KEY(task_id) REFERENCES v1_tasks(task_id),
 FOREIGN KEY(image_id) REFERENCES v1_images(image_id)
);
CREATE TABLE IF NOT EXISTS v1_assistant_profile (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), revision INTEGER NOT NULL,
 profile_json TEXT NOT NULL
);
INSERT OR IGNORE INTO v1_assistant_profile(singleton,revision,profile_json)
 VALUES(1,0,'{"name":"XVIS","persona_enabled":false,"persona":"","memory_enabled":false,"memories":[]}');
CREATE TABLE IF NOT EXISTS v1_hub_metadata (
 key TEXT PRIMARY KEY, value TEXT NOT NULL
);
INSERT OR IGNORE INTO v1_hub_metadata(key,value)
 VALUES('server_id','s-' || lower(hex(randomblob(16))));
CREATE TABLE IF NOT EXISTS v1_inbox_results (
 result_id TEXT PRIMARY KEY, task_id TEXT NOT NULL UNIQUE,
 status TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 created_at TEXT NOT NULL, read_at TEXT,
 FOREIGN KEY(task_id) REFERENCES v1_tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_v1_inbox_unread ON v1_inbox_results(read_at);
CREATE TABLE IF NOT EXISTS v1_notification_devices (
 device_id TEXT PRIMARY KEY, token TEXT NOT NULL, environment TEXT NOT NULL,
 enabled INTEGER NOT NULL, updated_at TEXT NOT NULL,
 registration_nonce TEXT NOT NULL DEFAULT '',
 UNIQUE(environment,token)
);
CREATE TABLE IF NOT EXISTS v1_notification_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL,
 revision INTEGER NOT NULL, device_id TEXT NOT NULL, apns_id TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, next_attempt_at REAL NOT NULL DEFAULT 0,
 last_reason TEXT,
 UNIQUE(result_id,revision,device_id),
 FOREIGN KEY(result_id) REFERENCES v1_inbox_results(result_id)
);
CREATE INDEX IF NOT EXISTS idx_v1_notification_due ON v1_notification_outbox(state,next_attempt_at);
CREATE TABLE IF NOT EXISTS v1_file_manifests (
 task_id TEXT PRIMARY KEY, note TEXT NOT NULL, retry_requested INTEGER NOT NULL DEFAULT 0,
 FOREIGN KEY(task_id) REFERENCES v1_tasks(task_id)
);
CREATE TABLE IF NOT EXISTS v1_files (
 file_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, position INTEGER NOT NULL,
 filename TEXT NOT NULL, mime_type TEXT NOT NULL, size_bytes INTEGER NOT NULL,
 sha256 TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('pending','ready','failed')), error TEXT,
 UNIQUE(task_id,position), FOREIGN KEY(task_id) REFERENCES v1_file_manifests(task_id)
);
"""

def init_v1_db(path: str | Path) -> sqlite3.Connection:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(target))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    if "assistant_context_json" not in {row["name"] for row in conn.execute("PRAGMA table_info(v1_tasks)")}:
        conn.execute("ALTER TABLE v1_tasks ADD COLUMN assistant_context_json TEXT")
    for column in ("project_context_json", "waiting_reason"):
        if column not in {row["name"] for row in conn.execute("PRAGMA table_info(v1_tasks)")}:
            conn.execute(f"ALTER TABLE v1_tasks ADD COLUMN {column} TEXT")
    if "registration_nonce" not in {row["name"] for row in conn.execute("PRAGMA table_info(v1_notification_devices)")}:
        conn.execute("ALTER TABLE v1_notification_devices ADD COLUMN registration_nonce TEXT NOT NULL DEFAULT ''")
    conn.execute("UPDATE v1_notification_devices SET registration_nonce=lower(hex(randomblob(16))) WHERE registration_nonce=''")
    from hub.gateway.interactions import SCHEMA as INTERACTION_SCHEMA
    conn.executescript(INTERACTION_SCHEMA)
    from hub.gateway.continuous import SCHEMA as CONTINUOUS_SCHEMA
    conn.executescript(CONTINUOUS_SCHEMA)
    from hub.gateway.pairing import SCHEMA as PAIRING_SCHEMA
    conn.executescript(PAIRING_SCHEMA)
    for table in ("v1_tasks", "v1_projects"):
        if "execution_mode" not in {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN execution_mode TEXT NOT NULL DEFAULT 'headless'")
    conn.commit()
    return conn
