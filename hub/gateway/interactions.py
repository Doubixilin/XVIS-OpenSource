"""Durable pending requests and an idempotent answer outbox, without Host RPC."""
from __future__ import annotations

import json
import uuid

from fastapi import Body

from hub.gateway import v1_repo as repo
from shared import interaction_contract as contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS v1_interactions (
 interaction_id TEXT PRIMARY KEY, task_id TEXT NOT NULL,
 request_json TEXT NOT NULL, status TEXT NOT NULL, outcome TEXT,
 response_id TEXT, response_json TEXT, updated_at TEXT NOT NULL,
 FOREIGN KEY(task_id) REFERENCES v1_tasks(task_id)
);
CREATE INDEX IF NOT EXISTS idx_v1_interactions_task ON v1_interactions(task_id);
CREATE TABLE IF NOT EXISTS v1_interaction_notification_outbox (
 id INTEGER PRIMARY KEY AUTOINCREMENT, result_id TEXT NOT NULL REFERENCES v1_interactions(interaction_id),
 revision INTEGER NOT NULL, device_id TEXT NOT NULL, apns_id TEXT NOT NULL,
 state TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 created_at TEXT NOT NULL, next_attempt_at REAL NOT NULL DEFAULT 0, last_reason TEXT,
 UNIQUE(result_id,revision,device_id)
);
CREATE VIEW IF NOT EXISTS v1_interaction_notification_source AS
 SELECT interaction_id AS result_id, task_id, json_extract(request_json,'$.kind') AS status,
 json_extract(request_json,'$.revision') AS revision,
 CASE WHEN status='pending' AND julianday(json_extract(request_json,'$.expires_at'))>julianday('now')
 THEN NULL ELSE updated_at END AS read_at FROM v1_interactions;
"""


def close_task(conn, task_id, state):
    conn.execute("UPDATE v1_interactions SET status=?,updated_at=? WHERE task_id=? AND status IN ('pending','submitted')",
                 (state, repo.now_iso(), task_id))


def expire(conn):
    with conn:
        for row in conn.execute("SELECT * FROM v1_interactions WHERE status IN ('pending','submitted')"):
            if contract.expired(json.loads(row["request_json"])["expires_at"]):
                conn.execute("UPDATE v1_interactions SET status='expired',updated_at=? WHERE interaction_id=?",
                             (repo.now_iso(), row["interaction_id"]))


def view(row):
    data = json.loads(row["request_json"])
    return {**data, "task_id": row["task_id"], "status": row["status"], "outcome": row["outcome"],
            "response_id": row["response_id"], "response": json.loads(row["response_json"]) if row["response_json"] else None,
            "updated_at": row["updated_at"]}


def for_task(conn, task_id):
    expire(conn)
    return [view(row) for row in conn.execute("SELECT * FROM v1_interactions WHERE task_id=? ORDER BY rowid", (task_id,))]


def project_event(conn, task, event):
    """Called in the same transaction as the existing task-event outbox insert."""
    if event["type"] not in {"interaction_request", "interaction_update"}:
        return
    if task.get("execution_mode") != "host":
        raise repo.RepoError(409, "interaction_conflict", "此任务未启用中途交互")
    data = event.get("interaction")
    try:
        if event["type"] == "interaction_request":
            contract.request(data)
            if data["session_id"] != task["dsh_session_id"]:
                raise ValueError("交互会话不匹配")
            encoded = repo.dump(data)
            old = conn.execute("SELECT * FROM v1_interactions WHERE interaction_id=?", (data["interaction_id"],)).fetchone()
            if old:
                if old["task_id"] != task["task_id"] or old["request_json"] != encoded:
                    raise ValueError("交互编号内容冲突")
                return
            status = "pending"
            if task["status"] in repo.TERMINAL or task["cancel_requested"]:
                status = "cancelled" if task["cancel_requested"] else "interrupted"
            elif contract.expired(data["expires_at"]):
                status = "expired"
            conn.execute("INSERT INTO v1_interactions(interaction_id,task_id,request_json,status,updated_at) VALUES(?,?,?,?,?)",
                         (data["interaction_id"], task["task_id"], encoded, status, repo.now_iso()))
            if status == "pending":
                for device in conn.execute("SELECT device_id FROM v1_notification_devices WHERE enabled=1").fetchall():
                    conn.execute("INSERT OR IGNORE INTO v1_interaction_notification_outbox(result_id,revision,device_id,apns_id,created_at) VALUES(?,?,?,?,?)",
                                 (data["interaction_id"], data["revision"], device[0], str(uuid.uuid4()), repo.now_iso()))
        else:
            if not isinstance(data, dict):
                raise ValueError("交互确认无效")
            row = conn.execute("SELECT * FROM v1_interactions WHERE interaction_id=? AND task_id=?",
                               (data.get("interaction_id"), task["task_id"])).fetchone()
            if row is None:
                raise ValueError("交互尚未登记")
            original = json.loads(row["request_json"])
            if any(data.get(k) != original[k] for k in ("epoch", "session_id", "call_id", "revision")):
                raise ValueError("交互确认关联错误")
            state = data.get("status")
            if state not in contract.CLOSED:
                raise ValueError("交互确认状态无效")
            if row["status"] in contract.CLOSED:
                return
            outcome = data.get("outcome")
            if state == "confirmed":
                expected = json.loads(row["response_json"]) if row["response_json"] else None
                expected = "answered" if isinstance(expected, dict) else expected
                if row["status"] != "submitted" or data.get("response_id") != row["response_id"] or outcome != expected:
                    raise ValueError("交互未匹配已提交答复")
            conn.execute("UPDATE v1_interactions SET status=?,outcome=?,updated_at=? WHERE interaction_id=?",
                         (state, outcome, repo.now_iso(), row["interaction_id"]))
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise repo.RepoError(422, "invalid_interaction", str(exc)) from exc


async def send_pending(app, state):
    if not state.ready or "interactions_v1" not in state.capabilities:
        return
    expire(app.state.db)
    rows = app.state.db.execute("SELECT i.* FROM v1_interactions i JOIN v1_tasks t ON t.task_id=i.task_id WHERE t.node_id=? AND t.status='running' AND t.cancel_requested=0 AND i.status='submitted'", (state.node_id,)).fetchall()
    for row in rows:
        item = view(row)
        await state.send({"type": "interaction_response", **{key: item[key] for key in
            ("task_id", "interaction_id", "revision", "epoch", "session_id", "call_id", "response_id")}, "value": item["response"]})


def register_routes(app):
    @app.get("/api/v1/interactions")
    async def pending():
        expire(app.state.db)
        return {"items": [view(row) for row in app.state.db.execute(
            "SELECT * FROM v1_interactions WHERE status IN ('pending','submitted') ORDER BY rowid LIMIT 100")]}

    @app.post("/api/v1/tasks/{task_id}/interactions/{interaction_id}/respond")
    async def respond(task_id: str, interaction_id: str, body: dict = Body(...)):
        conn = app.state.db
        expire(conn)
        task = repo.require_task(conn, task_id)
        row = conn.execute("SELECT * FROM v1_interactions WHERE interaction_id=? AND task_id=?", (interaction_id, task_id)).fetchone()
        if row is None:
            raise repo.RepoError(404, "not_found", "此任务没有该待处理项")
        try:
            if set(body) != {"revision", "response_id", "value"} or type(body["revision"]) is not int or body["revision"] != 1:
                raise ValueError("交互版本或字段无效")
            uuid.UUID(body["response_id"])
            value = contract.answer(json.loads(row["request_json"]), body["value"])
        except (ValueError, TypeError, AttributeError, KeyError) as exc:
            raise repo.RepoError(422, "invalid_interaction", str(exc)) from exc
        encoded = repo.dump(value)
        if row["response_id"]:
            if row["response_id"] != body["response_id"] or row["response_json"] != encoded:
                raise repo.RepoError(409, "interaction_conflict", "此项已经提交过答复，请刷新查看")
        elif row["status"] != "pending" or task["status"] != "running" or task["cancel_requested"]:
            raise repo.RepoError(409, "interaction_closed", "此待处理项已结束，不能再批准或答复")
        else:
            with conn:
                conn.execute("UPDATE v1_interactions SET status='submitted',response_id=?,response_json=?,updated_at=? WHERE interaction_id=? AND status='pending'",
                             (body["response_id"], encoded, repo.now_iso(), interaction_id))
        state = app.state.nodes.get(task["node_id"])
        if state:
            try:
                await send_pending(app, state)
            except Exception:
                app.state.nodes.unregister(state)
        return view(conn.execute("SELECT * FROM v1_interactions WHERE interaction_id=?", (interaction_id,)).fetchone())
