"""Authenticated immutable task-file delivery; independent of execution status."""
from __future__ import annotations
import asyncio
import hashlib
import os
import stat
import tempfile
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import Response
from pydantic import Field, field_validator
from hub.gateway import v1_repo as repo
from shared.file_contract import FILE_ID, FIELDS, MAX_BYTES, MAX_FILES, STORAGE_MAX, FileError, inspect, metadata
from shared.v1_schemas import StrictModel, checked_text


class FileDeclaration(StrictModel):
    file_id: str
    filename: str
    mime_type: str
    size_bytes: int
    sha256: str
    status: str
    error: str | None

    @field_validator("status")
    @classmethod
    def status_valid(cls, value):
        if value not in {"pending", "failed"}:
            raise ValueError("invalid_file_status")
        return value

    @field_validator("error")
    @classmethod
    def error_bounded(cls, value):
        return checked_text(value, 1024) if value is not None else None


class FileManifest(StrictModel):
    files: list[FileDeclaration] = Field(max_length=MAX_FILES)
    note: str

    @field_validator("note")
    @classmethod
    def note_bounded(cls, value):
        return checked_text(value, 1024)


class RetryRequest(StrictModel):
    pass


def checked_id(value: str) -> str:
    if FILE_ID.fullmatch(value) is None:
        raise repo.RepoError(422, "invalid_file_id", "文件编号无效")
    return value


def file_view(row) -> dict:
    return {key: row[key] for key in (*FIELDS, "status", "error")}


def list_files(conn, task_id: str) -> dict:
    manifest = conn.execute("SELECT * FROM v1_file_manifests WHERE task_id=?", (task_id,)).fetchone()
    files = [file_view(row) for row in conn.execute("SELECT * FROM v1_files WHERE task_id=? ORDER BY position", (task_id,))]
    note = manifest["note"] if manifest else ""
    states = [file["status"] for file in files]
    if manifest is None:
        state = "unknown"
    elif not files:
        state = "failed" if note else "none"
    elif all(s == "ready" for s in states):
        state = "partial" if note else "ready"
    elif "failed" in states:
        state = "partial" if "ready" in states else "failed" if all(s == "failed" for s in states) else "pending"
    else:
        state = "pending"
    return {"files": files, "transfer_state": state, "note": note,
            "retry_requested": bool(manifest and manifest["retry_requested"])}


def register_manifest(conn, task: dict, declaration: FileManifest) -> dict:
    if task["status"] not in {"running", "done"}:
        raise repo.RepoError(409, "invalid_state", "任务当前不能登记成果文件")
    values = [entry.model_dump() for entry in declaration.files]
    try:
        for value in values:
            metadata(value)
    except FileError as exc:
        raise repo.RepoError(422, "invalid_file", str(exc)) from None
    if len({v["file_id"] for v in values}) != len(values):
        raise repo.RepoError(422, "invalid_file", "文件编号重复")
    existing = conn.execute("SELECT * FROM v1_file_manifests WHERE task_id=?", (task["task_id"],)).fetchone()
    previous = list_files(conn, task["task_id"])["files"]
    if existing and [metadata(v) for v in values] != [metadata(v) for v in previous]:
        raise repo.RepoError(409, "file_conflict", "此任务的成果文件集合已固定")
    if any(conn.execute("SELECT 1 FROM v1_files WHERE file_id=? AND task_id<>?", (v["file_id"], task["task_id"])).fetchone() for v in values):
        raise repo.RepoError(409, "file_conflict", "文件编号已关联其他任务")
    with conn:
        conn.execute("INSERT INTO v1_file_manifests(task_id,note,retry_requested) VALUES(?,?,0) ON CONFLICT(task_id) DO UPDATE SET note=excluded.note,retry_requested=0",
                     (task["task_id"], declaration.note))
        for position, value in enumerate(values):
            if existing:
                conn.execute("UPDATE v1_files SET status=?,error=? WHERE file_id=? AND status<>'ready'",
                             (value["status"], value["error"], value["file_id"]))
            else:
                conn.execute("INSERT INTO v1_files(file_id,task_id,position,filename,mime_type,size_bytes,sha256,status,error) VALUES(?,?,?,?,?,?,?,?,?)",
                             (value["file_id"], task["task_id"], position, value["filename"], value["mime_type"], value["size_bytes"], value["sha256"], value["status"], value["error"]))
    return list_files(conn, task["task_id"])


def require_file(conn, task_id: str, file_id: str):
    checked_id(file_id)
    row = conn.execute("SELECT * FROM v1_files WHERE task_id=? AND file_id=?", (task_id, file_id)).fetchone()
    if row is None:
        raise repo.RepoError(404, "file_not_found", "成果文件未关联此任务")
    return row


class ArtifactStore:
    def __init__(self, db_path: str, max_bytes=STORAGE_MAX):
        self.directory = Path(db_path).resolve().parent / "files"
        if self.directory.is_symlink():
            raise ValueError("成果文件目录不能为符号链接")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.directory.chmod(0o700)
        self.max_bytes = max_bytes

    def read(self, value) -> bytes:
        try:
            descriptor = os.open(self.directory / checked_id(value["file_id"]), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as handle:
                if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                    raise OSError("not_regular")
                body = handle.read(MAX_BYTES + 1)
        except OSError:
            raise repo.RepoError(503, "file_unavailable", "成果文件暂时无法读取，请请求补传后重试") from None
        if len(body) != value["size_bytes"] or hashlib.sha256(body).hexdigest() != value["sha256"]:
            raise repo.RepoError(503, "file_unavailable", "成果文件完整性核对失败，请请求补传后重试")
        return body

    def put(self, conn, task_id: str, file_id: str, body: bytes, mime: str) -> dict:
        value = require_file(conn, task_id, file_id)
        if mime != value["mime_type"]:
            raise repo.RepoError(415, "unsupported_file", "文件类型与登记不一致")
        if len(body) != value["size_bytes"] or hashlib.sha256(body).hexdigest() != value["sha256"]:
            raise repo.RepoError(422, "file_integrity", "文件长度或摘要与登记不一致")
        try:
            inspect(body, mime)
        except FileError as exc:
            raise repo.RepoError(422, "invalid_file", str(exc)) from None
        try:
            self.read(value)
        except repo.RepoError:
            pass
        else:
            with conn:
                conn.execute("UPDATE v1_files SET status='ready',error=NULL WHERE file_id=?", (file_id,))
            return file_view(require_file(conn, task_id, file_id))
        target = self.directory / file_id
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise repo.RepoError(503, "file_unavailable", "成果文件存储位置不可用")
        used = sum(e.stat(follow_symlinks=False).st_size for e in os.scandir(self.directory) if e.is_file(follow_symlinks=False))
        replaced = target.stat().st_size if target.exists() else 0
        if used - replaced + len(body) > self.max_bytes:
            raise repo.RepoError(507, "file_storage_full", "成果文件存储已满，请在中枢检查空间")
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(prefix=".upload-", dir=self.directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(body); stream.flush(); os.fsync(stream.fileno())
            os.replace(temporary, target); temporary = None
            target.chmod(0o600)
            fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            with conn:
                conn.execute("UPDATE v1_files SET status='ready',error=NULL WHERE file_id=?", (file_id,))
        except OSError:
            raise repo.RepoError(507, "file_storage_error", "成果文件保存失败，请检查中枢空间后重试") from None
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        return file_view(require_file(conn, task_id, file_id))


async def send_requested(app, state):
    if "output_files_v1" not in state.capabilities:
        return
    for row in app.state.db.execute("SELECT m.task_id FROM v1_file_manifests m JOIN v1_tasks t ON t.task_id=m.task_id WHERE t.node_id=? AND m.retry_requested=1", (state.node_id,)):
        await state.send({"type": "task_files_retry", "task_id": row["task_id"]})


def register_routes(app):
    app.state.artifacts = ArtifactStore(app.state.config.get("db_path", "hub/data/dsh-hub.db"))
    app.state.file_lock = asyncio.Lock()

    @app.post("/api/v1/tasks/{task_id}/files/manifest")
    async def manifest(task_id: str, value: FileManifest):
        async with app.state.file_lock:
            return register_manifest(app.state.db, repo.require_task(app.state.db, task_id), value)

    @app.get("/api/v1/tasks/{task_id}/files")
    async def listed(task_id: str):
        repo.require_task(app.state.db, task_id)
        return list_files(app.state.db, task_id)

    @app.put("/api/v1/tasks/{task_id}/files/{file_id}")
    async def upload(task_id: str, file_id: str, request: Request):
        async with app.state.file_lock:
            return app.state.artifacts.put(app.state.db, task_id, file_id, await request.body(), request.headers.get("content-type", "").split(";", 1)[0].strip().lower())

    @app.get("/api/v1/tasks/{task_id}/files/{file_id}/content")
    async def content(task_id: str, file_id: str):
        value = require_file(app.state.db, task_id, file_id)
        if value["status"] != "ready":
            raise repo.RepoError(409, "file_not_ready", "成果文件尚未传回，请请求补传后重试")
        body = app.state.artifacts.read(value)
        return Response(body, media_type=value["mime_type"], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f"attachment; filename=\"{file_id}{Path(value['filename']).suffix.lower()}\"; filename*=UTF-8''{quote(value['filename'])}"})

    @app.post("/api/v1/tasks/{task_id}/files/retry")
    async def retry(task_id: str, value: RetryRequest):
        task = repo.require_task(app.state.db, task_id)
        listing = list_files(app.state.db, task_id)
        if task["status"] != "done" or listing["transfer_state"] in {"unknown", "none"}:
            raise repo.RepoError(409, "file_not_retryable", "此任务没有可补传的成果文件")
        # Ready records may still have missing/corrupt disk bytes. The same staged bytes repair them.
        if listing["transfer_state"] == "ready":
            damaged = False
            for item in listing["files"]:
                try:
                    app.state.artifacts.read(item)
                except repo.RepoError:
                    damaged = True
            if not damaged:
                return {"recorded": False, "node_online": bool(app.state.nodes.get(task["node_id"]))}
        with app.state.db:
            app.state.db.execute("UPDATE v1_file_manifests SET retry_requested=1 WHERE task_id=?", (task_id,))
        state = app.state.nodes.get(task["node_id"])
        online = bool(state and state.ready and "output_files_v1" in state.capabilities)
        if online:
            try:
                await state.send({"type": "task_files_retry", "task_id": task_id})
            except Exception:
                online = False
                app.state.nodes.unregister(state)
        return {"recorded": True, "node_online": online}
