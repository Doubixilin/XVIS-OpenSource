"""Short-lived, phone-confirmed Node pairing. The Hub stores credential hashes only."""
from collections import deque
import hashlib
import hmac
import re
import time
import uuid

from fastapi import Body
from hub.gateway import v1_repo as repo

SCHEMA = """
CREATE TABLE IF NOT EXISTS v1_pairing_requests (
 request_id TEXT PRIMARY KEY, name TEXT NOT NULL, scan_hash TEXT NOT NULL,
 poll_hash TEXT NOT NULL, token_hash TEXT NOT NULL, expires_at REAL NOT NULL,
 node_id TEXT, last_poll_at REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS v1_node_credentials (
 node_id TEXT PRIMARY KEY REFERENCES v1_nodes(node_id), token_hash TEXT NOT NULL UNIQUE,
 created_at TEXT NOT NULL, revoked_at TEXT
);
"""


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def node_for_token(conn, token):
    row = conn.execute("SELECT node_id FROM v1_node_credentials WHERE token_hash=? AND revoked_at IS NULL", (digest(token),)).fetchone()
    return row[0] if row else None


def registered(app, node_id):
    return node_id in app.state.config["node_tokens"] or app.state.db.execute(
        "SELECT 1 FROM v1_node_credentials WHERE node_id=? AND revoked_at IS NULL", (node_id,)).fetchone() is not None


def preview(row):
    return {"request_id": row["request_id"], "name": row["name"], "fingerprint": row["token_hash"][:12],
            "expires_at": row["expires_at"], "node_id": row["node_id"], "status": "approved" if row["node_id"] else "pending"}


def require_request(conn, rid, body, key):
    if re.fullmatch(r"p-[0-9a-f]{32}", rid) is None or set(body) != {"secret"} or not isinstance(body["secret"], str) or not 32 <= len(body["secret"]) <= 128:
        raise repo.RepoError(422, "invalid_pairing", "配对链接无效")
    row = conn.execute("SELECT * FROM v1_pairing_requests WHERE request_id=?", (rid,)).fetchone()
    if not row or not hmac.compare_digest(row[key], digest(body["secret"])):
        raise repo.RepoError(404, "pairing_not_found", "配对请求不存在或密钥无效")
    if row["expires_at"] <= time.time():
        raise repo.RepoError(409, "pairing_expired", "配对已过期，请在设备重新生成")
    if row["node_id"] and conn.execute("SELECT revoked_at FROM v1_node_credentials WHERE node_id=?", (row["node_id"],)).fetchone()[0]:
        raise repo.RepoError(409, "pairing_revoked", "此设备授权已撤销，请重新配对")
    return row


def register_routes(app):
    admissions = deque()

    @app.post("/api/v1/pairing/requests")
    async def begin(body: dict = Body(...)):
        now = time.time()
        if set(body) != {"request_id", "name", "scan_hash", "poll_hash", "token_hash"} or not isinstance(body["request_id"], str) or re.fullmatch(r"p-[0-9a-f]{32}", body["request_id"]) is None:
            raise repo.RepoError(422, "invalid_pairing", "配对请求字段无效")
        if not isinstance(body["name"], str) or not body["name"].strip() or len(body["name"]) > 80 or any(ord(c) < 32 for c in body["name"]):
            raise repo.RepoError(422, "invalid_pairing", "设备名称无效")
        for key in ("scan_hash", "poll_hash", "token_hash"):
            if not isinstance(body[key], str) or re.fullmatch(r"[0-9a-f]{64}", body[key]) is None:
                raise repo.RepoError(422, "invalid_pairing", "配对密钥摘要无效")
        if len({body[k] for k in ("scan_hash", "poll_hash", "token_hash")}) != 3:
            raise repo.RepoError(422, "invalid_pairing", "配对密钥必须彼此独立")
        conn = app.state.db
        old = conn.execute("SELECT * FROM v1_pairing_requests WHERE request_id=?", (body["request_id"],)).fetchone()
        if old:
            if any(old[k] != body[k] for k in body) or old["expires_at"] <= now:
                raise repo.RepoError(409, "pairing_conflict", "配对编号已使用或过期")
            return {"request_id": old["request_id"], "expires_at": old["expires_at"]}
        while admissions and admissions[0] <= now - 60: admissions.popleft()
        if len(admissions) >= 10:
            raise repo.RepoError(429, "pairing_rate_limit", "配对请求过多，请稍后再试")
        if conn.execute("SELECT count(*) FROM v1_pairing_requests WHERE expires_at>?", (now,)).fetchone()[0] >= 20:
            raise repo.RepoError(429, "pairing_limit", "待配对设备过多，请稍后再试")
        if body["token_hash"] in {digest(t) for t in (app.state.config["app_token"], *app.state.config["node_tokens"].values())} or conn.execute("SELECT 1 FROM v1_node_credentials WHERE token_hash=?", (body["token_hash"],)).fetchone():
            raise repo.RepoError(409, "pairing_conflict", "请使用全新的设备凭证")
        admissions.append(now)
        with conn:
            conn.execute("DELETE FROM v1_pairing_requests WHERE expires_at<?", (now - 86400,))
            conn.execute("INSERT INTO v1_pairing_requests(request_id,name,scan_hash,poll_hash,token_hash,expires_at) VALUES(?,?,?,?,?,?)",
                         (*(body[k] for k in ("request_id", "name", "scan_hash", "poll_hash", "token_hash")), now + 300))
        return {"request_id": body["request_id"], "expires_at": now + 300}

    @app.post("/api/v1/pairing/{request_id}/status")
    async def status(request_id: str, body: dict = Body(...)):
        row = require_request(app.state.db, request_id, body, "poll_hash")
        now = time.time()
        if now - row["last_poll_at"] < 1:
            raise repo.RepoError(429, "pairing_rate_limit", "请稍后查询")
        with app.state.db:
            app.state.db.execute("UPDATE v1_pairing_requests SET last_poll_at=? WHERE request_id=?", (now, request_id))
        return preview(row)

    @app.post("/api/v1/pairing/{request_id}/inspect")
    async def inspect(request_id: str, body: dict = Body(...)):
        return preview(require_request(app.state.db, request_id, body, "scan_hash"))

    @app.post("/api/v1/pairing/{request_id}/approve")
    async def approve(request_id: str, body: dict = Body(...)):
        conn = app.state.db
        row = require_request(conn, request_id, body, "scan_hash")
        if not row["node_id"]:
            if conn.execute("SELECT 1 FROM v1_node_credentials WHERE token_hash=?", (row["token_hash"],)).fetchone():
                raise repo.RepoError(409, "pairing_conflict", "此凭证已用于另一设备，请重新生成配对")
            nid = "n-" + uuid.uuid4().hex
            with conn:
                conn.execute("INSERT INTO v1_nodes(node_id,name) VALUES(?,?)", (nid, row["name"]))
                conn.execute("INSERT INTO v1_node_credentials(node_id,token_hash,created_at) VALUES(?,?,?)", (nid, row["token_hash"], repo.now_iso()))
                conn.execute("UPDATE v1_pairing_requests SET node_id=? WHERE request_id=?", (nid, request_id))
        return preview(conn.execute("SELECT * FROM v1_pairing_requests WHERE request_id=?", (request_id,)).fetchone())

    @app.get("/api/v1/paired-nodes")
    async def paired():
        return {"nodes": [dict(row) for row in app.state.db.execute(
            "SELECT n.node_id,n.name,c.created_at,c.revoked_at FROM v1_node_credentials c JOIN v1_nodes n USING(node_id) ORDER BY c.created_at DESC")]}

    @app.post("/api/v1/paired-nodes/{node_id}/revoke")
    async def revoke(node_id: str):
        conn = app.state.db
        row = conn.execute("SELECT * FROM v1_node_credentials WHERE node_id=?", (node_id,)).fetchone()
        if not row: raise repo.RepoError(404, "not_found", "此设备没有扫码授权")
        with conn:
            conn.execute("UPDATE v1_node_credentials SET revoked_at=COALESCE(revoked_at,?) WHERE node_id=?", (repo.now_iso(), node_id))
            conn.execute("UPDATE v1_projects SET available=0 WHERE node_id=?", (node_id,))
        state = app.state.nodes.get(node_id)
        if state:
            app.state.nodes.unregister(state)
            await state.ws.close(code=4403)
        for task in list(conn.execute("SELECT * FROM v1_tasks WHERE node_id=? AND status IN ('queued','assigned','running','cancel_requested')", (node_id,))):
            repo.change_status(conn, dict(task), "cancelled" if task["status"] == "queued" else "interrupted",
                               error={"code": "NODE_REVOKED", "message": "设备授权已撤销；已开始的本机进程停止尚未确认"})
        return {"node_id": node_id, "revoked": True}
