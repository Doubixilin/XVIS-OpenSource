"""SQLite v1 持久化，所有调用位于单个 Hub 事件循环。"""
from __future__ import annotations
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

TERMINAL = frozenset({"done", "failed", "cancelled", "timed_out", "interrupted"})
ACTIVE = frozenset({"assigned", "running", "cancel_requested"})

class RepoError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")

def dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

def bounded_text(text: str, max_bytes: int = 65536) -> str:
    return text.encode("utf-8")[:max_bytes].decode("utf-8", "ignore")

def get_task(conn, task_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM v1_tasks WHERE task_id=?", (task_id,)).fetchone()
    return dict(row) if row else None

def require_task(conn, task_id: str) -> dict:
    task = get_task(conn, task_id)
    if not task:
        raise RepoError(404, "not_found", "任务不存在")
    return task

def append_status(conn, task_id: str, status: str) -> None:
    conn.execute("INSERT INTO v1_events(task_id,type,payload_json,created_at) VALUES(?,?,?,?)",
                 (task_id, "task_status", dump({"status": status}), now_iso()))

def image_view(row) -> dict:
    return {key: row[key] for key in ("image_id", "mime_type", "size_bytes", "sha256", "width", "height")}

def task_images(conn, task_id: str, direction: str | None = None) -> list[dict]:
    query = "SELECT i.*,a.direction FROM v1_images i JOIN v1_task_images a ON a.image_id=i.image_id WHERE a.task_id=?"
    values = [task_id]
    if direction is not None:
        query += " AND a.direction=?"
        values.append(direction)
    query += " ORDER BY CASE a.direction WHEN 'input' THEN 0 ELSE 1 END,a.position"
    return [{**image_view(row), "direction": row["direction"]} for row in conn.execute(query, values)]

def task_context(task: dict) -> dict:
    encoded = task.get("assistant_context_json")
    return json.loads(encoded) if encoded else {}

def project_snapshot(task: dict) -> dict:
    encoded = task.get("project_context_json")
    return json.loads(encoded) if encoded else {}

def task_view(conn, task: dict, *, include_prompt: bool = False) -> dict:
    from hub.gateway.continuous import snapshot as control_snapshot
    from hub.gateway.artifacts import list_files
    from hub.gateway.interactions import for_task
    files = list_files(conn, task["task_id"])
    child = conn.execute("SELECT task_id FROM v1_tasks WHERE follows_task_id=? ORDER BY rowid DESC LIMIT 1",
                         (task["task_id"],)).fetchone()
    stripped_prompt = task["prompt"].strip()
    title = stripped_prompt.splitlines()[0][:120] if stripped_prompt else "项目任务"
    view = {"task_id": task["task_id"], "title": title, "execution_mode": task.get("execution_mode", "headless"),
            "interactions": for_task(conn, task["task_id"]), "control": control_snapshot(conn, task), "status": task["status"], "node_id": task["node_id"],
            "project_id": task["project_id"], "created_at": task["created_at"],
            "started_at": task["started_at"], "finished_at": task["finished_at"],
            "result": json.loads(task["result_json"]) if task["result_json"] and task["status"] == "done" else None,
            "error": json.loads(task["error_json"]) if task["error_json"] else None,
            "can_followup": task["status"] == "done" and bool(task["dsh_session_id"]),
            "followup_task_id": child[0] if child else None, "is_followup_of": task["follows_task_id"],
            "images": task_images(conn, task["task_id"]),
            "output_files": files["files"], "file_transfer": {"state": files["transfer_state"], "note": files["note"]},
            "assistant_name": task_context(task).get("name", "XVIS"),
            "project_context_revision": project_snapshot(task).get("revision"),
            "waiting_reason": task.get("waiting_reason") if task["status"] == "queued" else None}
    if include_prompt:
        view["prompt"] = task["prompt"]
        view["project_context"] = project_snapshot(task)
    return view

def request_fingerprint(operation: str, data: dict) -> str:
    data = dict(data)
    if not data.get("image_ids"):
        data.pop("image_ids", None)  # Original text-only request identifiers stay valid across this migration.
    return hashlib.sha256(dump({"operation": operation, **data}).encode()).hexdigest()

def existing_request(conn, request_id: str, fingerprint: str) -> dict | None:
    row = conn.execute("SELECT * FROM v1_tasks WHERE client_request_id=?", (request_id,)).fetchone()
    if row and row["request_hash"] != fingerprint:
        raise RepoError(409, "request_id_conflict", "此请求编号已用于不同内容，请检查原请求")
    return dict(row) if row else None

def create_task(conn, request_id: str, fingerprint: str, node_id: str, project_id: str,
                prompt: str, timeout_sec: int, follows_task_id: str | None = None,
                image_ids: list[str] | None = None, assistant_context: dict | None = None, project_context: dict | None = None,
                summary_sources: list[str] | None = None) -> tuple[dict, bool]:
    # 同一连接没有跨 await 的事务；唯一索引仍保护并发/重试。
    existing = existing_request(conn, request_id, fingerprint)
    if existing:
        return existing, False
    if conn.execute("SELECT COUNT(*) FROM v1_tasks WHERE status='queued'").fetchone()[0] >= 100:
        raise RepoError(503, "queue_full", "等待执行的任务已满，请稍后再试")
    tid = "t-" + secrets.token_hex(12)
    with conn:
        for image_id in image_ids or []:
            if not conn.execute("SELECT 1 FROM v1_images WHERE image_id=?", (image_id,)).fetchone():
                raise RepoError(422, "image_not_found", "所选图片尚未上传，请重试图片上传")
        conn.execute("INSERT INTO v1_tasks(task_id,client_request_id,request_hash,status,node_id,project_id,prompt,timeout_sec,follows_task_id,created_at,assistant_context_json,project_context_json) VALUES(?,?,?,'queued',?,?,?,?,?,?,?,?)",
                     (tid, request_id, fingerprint, node_id, project_id, prompt, timeout_sec, follows_task_id, now_iso(),
                      dump(assistant_context) if assistant_context is not None else None,
                      dump(project_context) if project_context is not None else None))
        if follows_task_id:
            mode = require_task(conn, follows_task_id).get("execution_mode", "headless")
        else:
            project_row = conn.execute("SELECT execution_mode FROM v1_projects WHERE node_id=? AND project_id=?", (node_id, project_id)).fetchone()
            mode = project_row[0] if project_row else "headless"
        conn.execute("UPDATE v1_tasks SET execution_mode=? WHERE task_id=?", (mode, tid))
        if summary_sources is not None:
            from hub.gateway.project_context import record_summary
            record_summary(conn, tid, node_id, project_id, summary_sources)
        for position, image_id in enumerate(image_ids or []):
            conn.execute("INSERT INTO v1_task_images(task_id,image_id,direction,position) VALUES(?,?,'input',?)", (tid, image_id, position))
        append_status(conn, tid, "queued")
    return require_task(conn, tid), True

def change_status(conn, task: dict, status: str, *, session_id: str | None = None,
                  result: dict | None = None, error: dict | None = None) -> bool:
    if task["status"] in TERMINAL:
        return task["status"] == status
    allowed = {"queued": {"assigned", "cancelled"},
               "assigned": {"running", "queued", "cancel_requested", *TERMINAL},
               "running": {"cancel_requested", *TERMINAL},
               "cancel_requested": set(TERMINAL)}
    if status not in allowed.get(task["status"], set()):
        return False
    current = now_iso()
    with conn:
        updated = conn.execute("UPDATE v1_tasks SET status=?,dsh_session_id=COALESCE(?,dsh_session_id),started_at=CASE WHEN ?='running' THEN COALESCE(started_at,?) ELSE started_at END,finished_at=CASE WHEN ? THEN ? ELSE finished_at END,result_json=?,error_json=? WHERE task_id=? AND status=?",
                    (status, session_id, status, current, status in TERMINAL, current,
                     dump(result) if result is not None else task["result_json"],
                     dump(error) if error is not None else task["error_json"], task["task_id"], task["status"]))
        if updated.rowcount:
            append_status(conn, task["task_id"], status)
            if status in TERMINAL or status == "cancel_requested":
                from hub.gateway.interactions import close_task
                close_task(conn, task["task_id"], "expired" if status == "timed_out" else
                           "cancelled" if status in {"cancelled", "cancel_requested"} else "interrupted")
            if status in TERMINAL:
                from hub.gateway.inbox import record_terminal
                record_terminal(conn, task["task_id"], status, current)
    return bool(updated.rowcount)

def active_tasks(conn, node_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT * FROM v1_tasks WHERE node_id=? AND status IN ('assigned','running','cancel_requested') ORDER BY rowid", (node_id,))]

def next_queued(conn, node_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM v1_tasks WHERE node_id=? AND status='queued' ORDER BY rowid LIMIT 1", (node_id,)).fetchone()
    return dict(row) if row else None

def record_event(conn, task: dict, seq: int, event: dict, ts: str | None) -> int:
    # 已确认且已按保留规则清理的事件不重新建行。
    row = conn.execute("SELECT type,payload_json FROM v1_events WHERE task_id=? AND event_seq=?", (task["task_id"], seq)).fetchone()
    payload = {k: v for k, v in event.items() if k != "type"}
    encoded = dump(payload)
    if row:
        if row["type"] != event["type"] or row["payload_json"] != encoded:
            raise RepoError(409, "event_conflict", "相同事件序号的内容不一致")
    elif seq > task["acked_seq"]:
        with conn:
            from hub.gateway.interactions import project_event
            project_event(conn, task, event)
            from hub.gateway.continuous import project_event as project_control
            project_control(conn, task, event)
            conn.execute("INSERT INTO v1_events(task_id,event_seq,type,payload_json,created_at) VALUES(?,?,?,?,?)", (task["task_id"], seq, event["type"], encoded, ts or now_iso()))
    acked = task["acked_seq"]
    while conn.execute("SELECT 1 FROM v1_events WHERE task_id=? AND event_seq=?", (task["task_id"], acked + 1)).fetchone():
        acked += 1
    with conn:
        conn.execute("UPDATE v1_tasks SET acked_seq=? WHERE task_id=?", (acked, task["task_id"]))
    return acked

def prune_events(conn) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="milliseconds")
    with conn:
        cur = conn.execute("DELETE FROM v1_events WHERE created_at<? AND task_id IN (SELECT task_id FROM v1_tasks WHERE status IN ('done','failed','cancelled','timed_out','interrupted')) AND (event_seq IS NULL OR event_seq<=(SELECT acked_seq FROM v1_tasks WHERE task_id=v1_events.task_id))", (cutoff,))
    return cur.rowcount
