"""持久结果收件箱；终态钩子与发送记录共用调用方的任务事务。"""
from __future__ import annotations
import re
import secrets
import uuid

from hub.gateway import v1_repo as repo
from shared.v1_schemas import DEVICE_ID, NotificationDevice

RESULT_ID = re.compile(r"r-[0-9a-f]{32}\Z")


def server_id(conn) -> str:
    return conn.execute("SELECT value FROM v1_hub_metadata WHERE key='server_id'").fetchone()[0]


def checked_result_id(value: str) -> str:
    if RESULT_ID.fullmatch(value) is None:
        raise repo.RepoError(422, "invalid_field", "结果编号无效")
    return value


def checked_device_id(value: str) -> str:
    if DEVICE_ID.fullmatch(value) is None:
        raise repo.RepoError(422, "invalid_field", "通知设备编号无效")
    return value


def item_view(row) -> dict:
    prompt = row["prompt"].strip()
    return {"result_id": row["result_id"], "task_id": row["task_id"],
            "node_id": row["node_id"], "project_id": row["project_id"],
            "title": prompt.splitlines()[0][:120] if prompt else "项目任务",
            "status": row["status"], "revision": row["revision"],
            "created_at": row["created_at"], "read_at": row["read_at"]}


ITEM_QUERY = """SELECT r.rowid AS result_order,r.*,t.node_id,t.project_id,t.prompt
 FROM v1_inbox_results r JOIN v1_tasks t ON t.task_id=r.task_id"""


def get_item(conn, result_id: str) -> dict:
    checked_result_id(result_id)
    row = conn.execute(ITEM_QUERY + " WHERE r.result_id=?", (result_id,)).fetchone()
    if row is None:
        raise repo.RepoError(404, "not_found", "结果不存在")
    return item_view(row)


def get_item_for_task(conn, task_id: str) -> dict:
    row = conn.execute(ITEM_QUERY + " WHERE r.task_id=?", (task_id,)).fetchone()
    if row is None:
        raise repo.RepoError(404, "not_found", "此任务没有结果收件箱记录")
    return item_view(row)


def list_unread(conn, cursor: str | None = None, limit: int = 30) -> dict:
    before = 9223372036854775807
    if cursor is not None:
        try:
            before = int(cursor)
        except ValueError:
            raise repo.RepoError(422, "invalid_field", "结果分页游标无效") from None
        if before <= 0 or before > 9223372036854775807:
            raise repo.RepoError(422, "invalid_field", "结果分页游标无效")
    limit = max(1, min(limit, 100))
    rows = list(conn.execute(ITEM_QUERY + " WHERE r.read_at IS NULL AND r.rowid<? ORDER BY r.rowid DESC LIMIT ?",
                             (before, limit + 1)))
    page = rows[:limit]
    return {"server_id": server_id(conn),
            "unread_count": conn.execute("SELECT COUNT(*) FROM v1_inbox_results WHERE read_at IS NULL").fetchone()[0],
            "items": [item_view(row) for row in page],
            "next_cursor": str(page[-1]["result_order"]) if len(rows) > limit and page else None}


def mark_read(conn, result_id: str, revision: int) -> dict:
    checked_result_id(result_id)
    if revision > 9223372036854775807:
        get_item(conn, result_id)
        raise repo.RepoError(409, "result_revision_conflict", "结果已有更新，请重新读取后确认")
    with conn:
        # Compare the displayed revision in the write itself, never acknowledge a newer result.
        updated = conn.execute("UPDATE v1_inbox_results SET read_at=COALESCE(read_at,?) WHERE result_id=? AND revision=?",
                               (repo.now_iso(), result_id, revision))
        if not updated.rowcount:
            if not conn.execute("SELECT 1 FROM v1_inbox_results WHERE result_id=?", (result_id,)).fetchone():
                raise repo.RepoError(404, "not_found", "结果不存在")
            raise repo.RepoError(409, "result_revision_conflict", "结果已有更新，请重新读取后确认")
        conn.execute("UPDATE v1_notification_outbox SET state='skipped',last_reason='read' WHERE result_id=? AND revision=? AND state='pending'",
                     (result_id, revision))
    return {"server_id": server_id(conn), "item": get_item(conn, result_id)}


def record_terminal(conn, task_id: str, status: str, created_at: str) -> None:
    """Caller owns the transaction. No historical backfill and no network work here."""
    row = conn.execute("SELECT * FROM v1_inbox_results WHERE task_id=?", (task_id,)).fetchone()
    if row is not None and row["status"] == status:
        return
    if row is None:
        result_id, revision = "r-" + secrets.token_hex(16), 1
        conn.execute("INSERT INTO v1_inbox_results(result_id,task_id,status,revision,created_at) VALUES(?,?,?,?,?)",
                     (result_id, task_id, status, revision, created_at))
    else:
        # Reserved for a future explicitly compatible terminal-state change, currently unreachable.
        result_id, revision = row["result_id"], row["revision"] + 1
        conn.execute("UPDATE v1_inbox_results SET status=?,revision=?,created_at=?,read_at=NULL WHERE result_id=?",
                     (status, revision, created_at, result_id))
        conn.execute("UPDATE v1_notification_outbox SET state='skipped',last_reason='superseded' WHERE result_id=? AND state='pending'",
                     (result_id,))
    for device in conn.execute("SELECT device_id FROM v1_notification_devices WHERE enabled=1"):
        conn.execute("INSERT OR IGNORE INTO v1_notification_outbox(result_id,revision,device_id,apns_id,created_at) VALUES(?,?,?,?,?)",
                     (result_id, revision, device["device_id"], str(uuid.uuid4()), created_at))


def save_device(conn, device_id: str, req: NotificationDevice, configured_environment: str | None) -> None:
    checked_device_id(device_id)
    if configured_environment is not None and req.environment != configured_environment:
        raise repo.RepoError(422, "notification_environment_conflict", "通知环境与此中枢配置不一致")
    with conn:
        duplicates = [row[0] for row in conn.execute(
            "SELECT device_id FROM v1_notification_devices WHERE environment=? AND token=? AND device_id<>?",
            (req.environment, req.token, device_id))]
        for old_id in duplicates:
            conn.execute("UPDATE v1_notification_outbox SET state='skipped',last_reason='replaced' WHERE device_id=? AND state='pending'", (old_id,))
            conn.execute("UPDATE v1_interaction_notification_outbox SET state='skipped',last_reason='replaced' WHERE device_id=? AND state='pending'", (old_id,))
            conn.execute("DELETE FROM v1_notification_devices WHERE device_id=?", (old_id,))
        conn.execute("INSERT INTO v1_notification_devices(device_id,token,environment,enabled,updated_at,registration_nonce) VALUES(?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET token=excluded.token,environment=excluded.environment,enabled=excluded.enabled,updated_at=excluded.updated_at,registration_nonce=excluded.registration_nonce",
                     (device_id, req.token, req.environment, req.enabled, repo.now_iso(), secrets.token_hex(16)))
        if not req.enabled:
            conn.execute("UPDATE v1_notification_outbox SET state='skipped',last_reason='disabled' WHERE device_id=? AND state='pending'", (device_id,))
            conn.execute("UPDATE v1_interaction_notification_outbox SET state='skipped',last_reason='disabled' WHERE device_id=? AND state='pending'", (device_id,))


def remove_device(conn, device_id: str) -> None:
    checked_device_id(device_id)
    with conn:
        conn.execute("DELETE FROM v1_notification_devices WHERE device_id=?", (device_id,))
        conn.execute("UPDATE v1_notification_outbox SET state='skipped',last_reason='unregistered' WHERE device_id=? AND state='pending'", (device_id,))
        conn.execute("UPDATE v1_interaction_notification_outbox SET state='skipped',last_reason='unregistered' WHERE device_id=? AND state='pending'", (device_id,))
