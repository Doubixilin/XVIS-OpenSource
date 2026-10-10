"""Durable Host commands and a bounded authenticated foreground event stream."""
import asyncio
import json
import time

from fastapi import Body, Request
from fastapi.responses import StreamingResponse

from hub.gateway import v1_repo as repo
from shared import control_contract as contract

SCHEMA = """
CREATE TABLE IF NOT EXISTS v1_control_state (
 task_id TEXT PRIMARY KEY REFERENCES v1_tasks(task_id), state_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS v1_controls (
 command_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES v1_tasks(task_id),
 request_json TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL DEFAULT '',
 consumed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
);
"""


def command_view(row):
    return {**json.loads(row["request_json"]), "task_id": row["task_id"],
            "status": row["status"], "detail": row["detail"], "consumed": bool(row["consumed"])}


def snapshot(conn, task):
    row = conn.execute("SELECT state_json FROM v1_control_state WHERE task_id=?", (task["task_id"],)).fetchone()
    if row is None:
        return None
    if task["status"] in repo.TERMINAL:
        with conn:
            conn.execute("UPDATE v1_controls SET status='interrupted',detail='任务已结束，未确认执行' WHERE task_id=? AND status='pending'", (task["task_id"],))
            conn.execute("UPDATE v1_controls SET status='interrupted',detail='任务已停止，此补充未用于执行' WHERE task_id=? AND status='accepted' AND consumed=0 AND json_extract(request_json,'$.action') IN ('queue','steer')", (task["task_id"],))
    data = json.loads(row[0])
    if task["status"] in repo.TERMINAL: data["items"] = []
    return {**data, "commands": [command_view(r) for r in conn.execute(
        "SELECT * FROM v1_controls WHERE task_id=? ORDER BY rowid", (task["task_id"],))]}


def project_event(conn, task, event):
    kind = event["type"]
    if kind not in {"control_state", "control_result", "control_consumed"}:
        return
    try:
        if task["execution_mode"] != "host":
            raise ValueError("此任务不支持运行中补充")
        if kind == "control_state":
            data = contract.state(event["control"])
            old = conn.execute("SELECT state_json FROM v1_control_state WHERE task_id=?", (task["task_id"],)).fetchone()
            if old:
                previous = json.loads(old[0])
                if data["epoch"] != previous["epoch"] or data["revision"] < previous["revision"]:
                    raise ValueError("队列版本已失效")
            conn.execute("INSERT INTO v1_control_state VALUES(?,?) ON CONFLICT(task_id) DO UPDATE SET state_json=excluded.state_json",
                         (task["task_id"], repo.dump(data)))
        else:
            row = conn.execute("SELECT * FROM v1_controls WHERE command_id=? AND task_id=?", (event["command_id"], task["task_id"])).fetchone()
            if not row or json.loads(row["request_json"])["epoch"] != event["epoch"]:
                raise ValueError("补充记录不匹配")
            if kind == "control_consumed":
                conn.execute("UPDATE v1_controls SET consumed=1 WHERE command_id=?", (row["command_id"],))
            elif event["status"] in {"accepted", "rejected", "uncertain"}:
                conn.execute("UPDATE v1_controls SET status=?,detail=? WHERE command_id=? AND status='pending'",
                             (event["status"], str(event.get("detail", ""))[:256], row["command_id"]))
            else:
                raise ValueError("补充确认无效")
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise repo.RepoError(422, "invalid_control", "运行中补充事件无效") from exc


async def send_pending(app, state):
    if not state.ready or "continuous_v1" not in state.capabilities:
        return
    rows = app.state.db.execute("""SELECT c.* FROM v1_controls c JOIN v1_tasks t ON t.task_id=c.task_id
      WHERE t.node_id=? AND t.status='running' AND t.cancel_requested=0 AND c.status='pending' ORDER BY c.rowid""", (state.node_id,)).fetchall()
    for row in rows:
        await state.send({"type": "task_control", "task_id": row["task_id"], "command": json.loads(row["request_json"])})


def register_routes(app):
    @app.post("/api/v1/tasks/{task_id}/controls")
    async def submit(task_id: str, body: dict = Body(...)):
        try:
            contract.command(body)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            raise repo.RepoError(422, "invalid_control", "补充指令字段无效") from exc
        conn = app.state.db
        task = repo.require_task(conn, task_id)
        encoded = repo.dump(body)
        old = conn.execute("SELECT * FROM v1_controls WHERE command_id=?", (body["command_id"],)).fetchone()
        if old:
            if old["task_id"] != task_id or old["request_json"] != encoded:
                raise repo.RepoError(409, "control_conflict", "同一提交标识不能更换内容")
            return command_view(old)
        current = snapshot(conn, task)
        node = app.state.nodes.get(task["node_id"])
        if not current or not node or not node.ready or "continuous_v1" not in node.capabilities or task["status"] != "running" or task["cancel_requested"] or body["epoch"] != current["epoch"]:
            raise repo.RepoError(409, "control_closed", "当前任务暂不接受补充，请刷新状态")
        if len(current["commands"]) >= 60 or sum(c["action"] in {"queue", "steer"} for c in current["commands"]) >= 20 and body["action"] in {"queue", "steer"}:
            raise repo.RepoError(409, "control_limit", "本任务补充次数已达上限，请完成后续接")
        if "item_id" in body and (body["revision"] != current["revision"] or body["item_id"] not in {i["item_id"] for i in current["items"]}):
            raise repo.RepoError(409, "control_stale", "队列已变化，请刷新后操作")
        with conn:
            conn.execute("INSERT INTO v1_controls(command_id,task_id,request_json,status,created_at) VALUES(?,?,?,'pending',?)",
                         (body["command_id"], task_id, encoded, repo.now_iso()))
        try:
            await send_pending(app, node)
        except Exception:
            pass  # Durable retry; no new identity is generated.
        return command_view(conn.execute("SELECT * FROM v1_controls WHERE command_id=?", (body["command_id"],)).fetchone())

    @app.get("/api/v1/tasks/{task_id}/stream")
    async def stream(task_id: str, request: Request, after: int = 0):
        repo.require_task(app.state.db, task_id)
        if after < 0:
            raise repo.RepoError(422, "invalid_cursor", "事件游标无效")
        async def chunks():
            cursor, deadline = after, time.monotonic() + 25
            while time.monotonic() < deadline and not await request.is_disconnected():
                rows = app.state.db.execute("SELECT * FROM v1_events WHERE task_id=? AND id>? ORDER BY id LIMIT 101", (task_id, cursor)).fetchall()
                page = rows[:100]
                if page:
                    cursor = page[-1]["id"]
                    yield repo.dump({"events": [{"id": r["id"], "task_id": task_id, "type": r["type"], "payload": json.loads(r["payload_json"]), "created_at": r["created_at"]} for r in page], "next_after": cursor, "has_more": len(rows) > 100}) + "\n"
                else:
                    yield "\n"
                await asyncio.sleep(.25)
        return StreamingResponse(chunks(), media_type="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
