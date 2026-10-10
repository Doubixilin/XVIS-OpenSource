"""DSH 单引擎 v1 中枢。历史 chat/PWA/多引擎接口不挂载。"""
from __future__ import annotations
import asyncio
import hmac
import json
import logging
import os
import re
import socket
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
import time
from typing import Any

import yaml
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException

from hub.gateway import v1_repo as repo
from hub.gateway import assistant_profile
from hub.gateway import inbox, notifications
from hub.gateway import artifacts, project_context
from shared.file_contract import MAX_BYTES as FILE_MAX, TYPES as FILE_TYPES
from hub.gateway.images import IMAGE_MAX, STORAGE_MAX, ImageStore, checked_id
from hub.gateway.node_registry import NodeRegistry, NodeState
from shared.v1_db import init_v1_db
from shared.v1_schemas import AssistantProfile, TaskCreate, FollowupCreate, NotificationDevice, ResultRead

from hub.gateway import interactions, continuous, pairing

LOGGER = logging.getLogger("hub.gateway")
BODY_MAX = 65536
EVENT_MAX = 65536
SECRET_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}\Z")


def secret_value(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("访问凭证必须为字符串")
    match = SECRET_ENV.fullmatch(value)
    if match:
        value = os.environ.get(match.group(1), "")
    if not value.isascii() or any(char.isspace() for char in value) or len(value) < 32 or any(marker in value.lower() for marker in ("changeme", "example", "your-token", "placeholder", "改成", "你的", "模板")) or "${" in value:
        raise ValueError("访问凭证为空、过短或仍是模板值")
    return value


def load_config(path: str = "hub/config.yaml") -> dict:
    p = Path(path)
    # 只有默认入口允许本地覆盖；明确 --config 必须使用指定文件。
    if p == Path("hub/config.yaml") and p.with_name("config.local.yaml").exists():
        p = p.with_name("config.local.yaml")
    try:
        config = yaml.safe_load(p.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, yaml.YAMLError):
        # YAML 错误原文可能包含凭证所在行，不能进入启动日志。
        raise ValueError("中枢配置读取失败，请检查文件路径与 YAML 格式") from None
    if not isinstance(config, dict):
        raise ValueError("中枢配置必须为对象")
    config["app_token"] = secret_value(config.get("app_token"))
    tokens = config.get("node_tokens")
    if not isinstance(tokens, dict) or not tokens:
        raise ValueError("必须配置绑定节点身份的 node_tokens")
    config["node_tokens"] = {str(nid): secret_value(token) for nid, token in tokens.items()}
    if any(not nid or len(nid) > 128 for nid in config["node_tokens"]):
        raise ValueError("节点编号无效")
    all_tokens = [config["app_token"], *config["node_tokens"].values()]
    if len(set(all_tokens)) != len(all_tokens):
        raise ValueError("App 与各节点必须使用不同访问凭证")
    for key, default in (("default_timeout_sec", 1800), ("task_create_per_minute", 30), ("node_heartbeat_timeout_sec", 45), ("node_scan_interval_sec", 5)):
        if float(config.get(key, default)) <= 0:
            raise ValueError(f"配置 {key} 必须为正数")
    storage_max = config.get("image_storage_max_bytes", STORAGE_MAX)
    if type(storage_max) is not int or storage_max <= 0:
        raise ValueError("图片存储上限必须为正整数")
    config["apns"] = notifications.validate_config(config)
    return config


def auth_role(config: dict, authorization: str, conn=None) -> tuple[str | None, str | None]:
    if not authorization.startswith("Bearer "):
        return None, None
    supplied = authorization[7:]
    if not supplied.isascii():
        return None, None
    if hmac.compare_digest(supplied, config["app_token"]):
        return "app", None
    for nid, token in config["node_tokens"].items():
        if hmac.compare_digest(supplied, token):
            return "node", nid
    if conn is not None and len(supplied) <= 256:
        if nid := pairing.node_for_token(conn, supplied):
            return "node", nid
    return None, None


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


def validate_project(conn, node_id: str, project_id: str) -> None:
    row = conn.execute("SELECT available FROM v1_projects WHERE node_id=? AND project_id=?", (node_id, project_id)).fetchone()
    if not row:
        raise repo.RepoError(422, "invalid_field", "请选择 Mac 已登记的项目")
    if not row["available"]:
        raise repo.RepoError(422, "invalid_field", "此项目当前不可用，请在 Mac 检查目录")


def owned_task(app: FastAPI, state: NodeState, tid: Any) -> dict:
    if not isinstance(tid, str):
        raise repo.RepoError(422, "invalid_field", "任务编号无效")
    task = repo.require_task(app.state.db, tid)
    if task["node_id"] != state.node_id:
        raise repo.RepoError(403, "forbidden_role", "节点无权报告此任务")
    return task


def positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def terminal_data(message: dict) -> tuple[str, dict | None, dict | None]:
    status = message.get("status", message.get("local_status"))
    if status not in repo.TERMINAL:
        raise repo.RepoError(422, "invalid_field", "终态无效")
    if status == "done":
        reason = message.get("turn_end_reason")
        kind = reason.get("kind") if isinstance(reason, dict) else reason
        final = message.get("final_text")
        if not isinstance(final, str) or len(final.encode("utf-8")) > EVENT_MAX or kind != "completed" or type(message.get("exit_code")) is not int or message.get("exit_code") != 0:
            raise repo.RepoError(422, "invalid_field", "任务完成证据不完整或终答摘要过大")
        truncated = bool(message.get("final_truncated", False))
        result = {"final_text": final, "truncated": truncated}
        if truncated:
            result["full_text_ref"] = "events"
        return status, result, None
    not_started = status in {"cancelled", "timed_out"} and message.get("not_started") is True and message.get("exit_code") is None
    if status in {"cancelled", "timed_out"} and not not_started and (not isinstance(message.get("exit_code"), int) or isinstance(message.get("exit_code"), bool)):
        raise repo.RepoError(422, "invalid_field", "尚未确认任务进程退出")
    reason = message.get("turn_end_reason")
    detail = message.get("detail")
    code = status.upper()
    if isinstance(reason, dict) and isinstance(reason.get("error"), dict):
        err = reason["error"]
        detail = detail or err.get("message")
        code = str(err.get("code") or code)
    error = {"code": repo.bounded_text(code, 128), "message": repo.bounded_text(str(detail or {"failed": "任务执行失败", "timed_out": "任务执行超时", "interrupted": "任务停止状态尚未确认"}.get(status, "任务已取消")), 8192)} if status != "cancelled" else None
    return status, None, error


def validate_assignment_size(conn, prompt: str, assistant: dict, project: dict,
                             image_ids: list[str] | None = None, *, follows_task_id: str | None = None) -> None:
    # Account for JSON escaping and all optional extensions before admitting a task.
    images = []
    for image_id in image_ids or []:
        row = conn.execute("SELECT * FROM v1_images WHERE image_id=?", (image_id,)).fetchone()
        if row is not None:
            images.append(repo.image_view(row))
    payload = {"type": "task_assign", "task_id": "t-" + "0" * 24, "project_id": "x" * 128,
               "prompt": prompt, "timeout_sec": 86400, "images": images,
               "assistant_context": assistant, "project_context": project, "output_files_version": 1}
    if follows_task_id:
        parent = repo.require_task(conn, follows_task_id)
        payload["followup_of"] = {"task_id": parent["task_id"], "dsh_session_id": parent["dsh_session_id"]}
    if len(repo.dump(payload).encode("utf-8")) > BODY_MAX:
        raise repo.RepoError(422, "task_payload_too_large", "任务及背景合计超过传输限制，请缩短文字后重试")


async def schedule_node(app: FastAPI, node_id: str) -> None:
    async with app.state.dispatch_lock:
        state = app.state.nodes.get(node_id)
        if not state or not state.ready or not state.idle_confirmed or state.running or repo.active_tasks(app.state.db, node_id):
            return
        task = repo.next_queued(app.state.db, node_id)
        if not task:
            return
        if task.get("execution_mode") == "host" and "interactions_v1" not in state.capabilities:
            with app.state.db:
                app.state.db.execute("UPDATE v1_tasks SET waiting_reason=? WHERE task_id=?",
                    ("当前执行设备暂不支持中途交互，请更新节点", task["task_id"]))
            return
        snapshot = repo.project_snapshot(task)
        if project_context.has_content(snapshot) and "project_context_v1" not in state.capabilities:
            with app.state.db:
                app.state.db.execute("UPDATE v1_tasks SET waiting_reason=? WHERE task_id=?",
                    ("当前执行设备暂不支持项目背景，请更新节点", task["task_id"]))
            return
        msg = {"type": "task_assign", "task_id": task["task_id"], "project_id": task["project_id"], "prompt": task["prompt"], "timeout_sec": task["timeout_sec"]}
        if "interactions_v1" in state.capabilities:
            msg["execution_mode"] = task.get("execution_mode", "headless")
        msg["images"] = [{key: value for key, value in image.items() if key != "direction"}
                         for image in repo.task_images(app.state.db, task["task_id"], "input")]
        msg["assistant_context"] = repo.task_context(task)
        if "project_context_v1" in state.capabilities:
            msg["project_context"] = snapshot
        if "output_files_v1" in state.capabilities:
            msg["output_files_version"] = 1
        if task["follows_task_id"]:
            original = repo.require_task(app.state.db, task["follows_task_id"])
            msg["followup_of"] = {"task_id": original["task_id"], "dsh_session_id": original["dsh_session_id"]}
        if len(repo.dump(msg).encode("utf-8")) > BODY_MAX:
            with app.state.db:
                app.state.db.execute("UPDATE v1_tasks SET waiting_reason=? WHERE task_id=?",
                    ("任务及背景合计超过传输限制，请缩短资料后重新提交", task["task_id"]))
            return
        if not repo.change_status(app.state.db, task, "assigned"):
            return
        with app.state.db:
            app.state.db.execute("UPDATE v1_tasks SET waiting_reason=NULL WHERE task_id=?", (task["task_id"],))
        state.idle_confirmed = False
        try:
            await state.send(msg)
        except Exception:
            # 发出后是否抵达未知，保持 assigned，重连通过 journal 对账。
            app.state.nodes.unregister(state)
            LOGGER.warning("派发连接中断 task_id=%s node_id=%s", task["task_id"], node_id)


async def maybe_ready(app: FastAPI, state: NodeState) -> None:
    if not app.state.nodes.current(state) or state.ready:
        return
    for tid, report in state.reconciliation.items():
        task = repo.get_task(app.state.db, tid)
        if not task or not report or report.get("local_status") != task["status"] or task["acked_seq"] < report.get("last_persisted_seq", 0):
            return
    state.ready = True
    await state.send({"type": "ready", "node_id": state.node_id})
    await artifacts.send_requested(app, state)
    await interactions.send_pending(app, state)
    await continuous.send_pending(app, state)
    for task in repo.active_tasks(app.state.db, state.node_id):
        if task["cancel_requested"]:
            await state.send({"type": "task_cancel", "task_id": task["task_id"]})
    await schedule_node(app, state.node_id)


async def handle_node_message(app: FastAPI, state: NodeState, msg: dict) -> None:
    if not app.state.nodes.current(state):
        return
    app.state.nodes.touch(state)
    kind = msg.get("type")
    if kind == "heartbeat":
        running = msg.get("running")
        if not isinstance(running, list) or len(running) > 100 or not all(isinstance(tid, str) for tid in running):
            raise repo.RepoError(422, "invalid_field", "运行任务列表无效")
        for tid in running:
            task = owned_task(app, state, tid)
            if task["status"] == "queued":
                raise repo.RepoError(409, "invalid_state", "未派发任务不能报告为运行中")
        state.running = running
        state.idle_confirmed = not running
        with app.state.db:
            app.state.db.execute("UPDATE v1_nodes SET last_seen=? WHERE node_id=?", (state.last_seen_iso, state.node_id))
        await maybe_ready(app, state)
        await schedule_node(app, state.node_id)
        await interactions.send_pending(app, state)
        await continuous.send_pending(app, state)
        return
    if kind not in {"task_accepted", "task_rejected", "task_started", "task_event", "task_done", "task_status"}:
        raise repo.RepoError(422, "invalid_field", "节点消息类型无效")
    task = owned_task(app, state, msg.get("task_id"))
    tid = task["task_id"]
    if kind == "task_event":
        seq, event = msg.get("event_seq"), msg.get("event")
        if not positive_int(seq) or not isinstance(event, dict) or not isinstance(event.get("type"), str) or len(repo.dump(event).encode("utf-8")) > EVENT_MAX:
            raise repo.RepoError(422, "invalid_field", "事件编号、类型或大小无效")
        if event["type"] == "thinking":
            raise repo.RepoError(422, "invalid_field", "思考内容应保留在 Mac")
        if task["status"] == "queued":
            raise repo.RepoError(409, "invalid_state", "未派发任务不能提交执行事件")
        ts = msg.get("ts") if isinstance(msg.get("ts"), str) else None
        acked = repo.record_event(app.state.db, task, seq, event, ts)
        if event["type"] == "session" and isinstance(event.get("sessionId"), str):
            with app.state.db:
                app.state.db.execute("UPDATE v1_tasks SET dsh_session_id=COALESCE(dsh_session_id,?) WHERE task_id=?", (event["sessionId"], tid))
        await state.send({"type": "event_ack", "task_id": tid, "acked_seq": acked})
        await maybe_ready(app, state)
        return
    if kind == "task_accepted":
        if task["status"] not in repo.ACTIVE or not positive_int(msg.get("journal_seq")):
            raise repo.RepoError(409, "invalid_state", "任务受理状态无效")
        with app.state.db:
            app.state.db.execute("UPDATE v1_tasks SET accepted=1 WHERE task_id=?", (tid,))
        return
    if kind == "task_started":
        if task["status"] not in {"assigned", "running", "cancel_requested"}:
            raise repo.RepoError(409, "invalid_state", "任务不能回到运行状态")
        session = msg.get("dsh_session_id")
        if not isinstance(session, str) or not session:
            raise repo.RepoError(422, "invalid_field", "DSH 会话编号无效")
        if task["status"] == "assigned":
            repo.change_status(app.state.db, task, "running", session_id=session)
        else:
            with app.state.db:
                app.state.db.execute("UPDATE v1_tasks SET dsh_session_id=COALESCE(dsh_session_id,?) WHERE task_id=?", (session, tid))
        return
    if kind == "task_rejected":
        reason = msg.get("reason")
        if task["status"] not in {"assigned", "cancel_requested"} or task["accepted"]:
            raise repo.RepoError(409, "invalid_state", "已受理的任务不能按未执行退回")
        if reason == "busy":
            # 明确未受理的拒绝证明没有启动；已记录取消时无需继续占槽。
            repo.change_status(app.state.db, task, "cancelled" if task["status"] == "cancel_requested" else "queued")
            state.idle_confirmed = False
        elif reason in {"unknown_project", "invalid_followup", "runtime_changed", "invalid_task"}:
            repo.change_status(app.state.db, task, "failed", error={"code": str(reason), "message": {"unknown_project": "项目未登记或目录不可用", "invalid_followup": "续接环境与原会话不一致", "runtime_changed": "DSH 或 Node 版本已变化，请在 Mac 检查运行环境", "invalid_task": "任务内容或执行时限无效"}[reason]})
        else:
            raise repo.RepoError(422, "invalid_field", "任务拒绝原因无效")
        return
    local_status = msg.get("local_status") if kind == "task_status" else msg.get("status")
    if kind == "task_status":
        if tid not in state.reconciliation:
            # 执行期间的异常报告允许，但不创建新的待核对任务。
            if local_status not in repo.TERMINAL:
                raise repo.RepoError(409, "invalid_state", "任务不在本次对账列表")
        last_seq = msg.get("last_persisted_seq", 0)
        if not isinstance(last_seq, int) or isinstance(last_seq, bool) or last_seq < 0:
            raise repo.RepoError(422, "invalid_field", "本地事件序号无效")
        if tid in state.reconciliation:
            state.reconciliation[tid] = {"local_status": local_status, "last_persisted_seq": last_seq}
        if local_status == "running" and task["status"] == "assigned":
            repo.change_status(app.state.db, task, "running", session_id=msg.get("dsh_session_id"))
        elif local_status == "cancel_requested" and task["status"] in {"assigned", "running"}:
            with app.state.db:
                app.state.db.execute("UPDATE v1_tasks SET cancel_requested=1 WHERE task_id=?", (tid,))
            repo.change_status(app.state.db, task, "cancel_requested")
        if local_status not in repo.TERMINAL:
            await maybe_ready(app, state)
            return
    if local_status in repo.TERMINAL:
        if task["status"] in repo.TERMINAL:
            if task["status"] != local_status:
                raise repo.RepoError(409, "invalid_state", "已完成任务的终态不可修改")
        else:
            status, result, error = terminal_data(msg)
            if msg.get("not_started") is True and task["started_at"] is not None:
                raise repo.RepoError(409, "invalid_state", "任务已报告启动，不能再声明从未创建进程")
            if not repo.change_status(app.state.db, task, status, session_id=msg.get("dsh_session_id"), result=result, error=error):
                raise repo.RepoError(409, "invalid_state", "任务终态与当前状态不一致")
        await maybe_ready(app, state)
    else:
        raise repo.RepoError(422, "invalid_field", "任务终态无效")


async def scan_loop(app: FastAPI) -> None:
    last_prune = 0.0
    while True:
        await asyncio.sleep(float(app.state.config.get("node_scan_interval_sec", 5)))
        for state in app.state.nodes.expired(float(app.state.config.get("node_heartbeat_timeout_sec", 45))):
            if app.state.nodes.unregister(state):
                try:
                    await state.ws.close(code=4408)
                except Exception:
                    pass
                # 断网不改变持久任务状态。
        if time.monotonic() - last_prune > 3600:
            repo.prune_events(app.state.db)
            last_prune = time.monotonic()


def create_app(config_path: str = "hub/config.yaml") -> FastAPI:
    config = load_config(config_path)
    logging.basicConfig(level=getattr(logging, str(config.get("log_level", "INFO")).upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s %(message)s")

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        scanner = asyncio.create_task(scan_loop(app))
        sender_task = asyncio.create_task(notifications.sender_loop(app)) if app.state.apns_sender else None
        try:
            yield
        finally:
            scanner.cancel()
            try:
                await scanner
            except asyncio.CancelledError:
                pass
            if sender_task is not None:
                sender_task.cancel()
                try:
                    await sender_task
                except asyncio.CancelledError:
                    pass
            if app.state.apns_sender is not None:
                await app.state.apns_sender.aclose()
            app.state.db.close()

    app = FastAPI(title="XVIS DSH Hub", version="1", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.config = config
    app.state.nodes = NodeRegistry()
    app.state.dispatch_lock = asyncio.Lock()
    app.state.creation_times = deque()
    app.state.db = init_v1_db(config.get("db_path", "hub/data/dsh-hub.db"))
    project_context.initialize(app.state.db)
    app.state.images = ImageStore(config.get("db_path", "hub/data/dsh-hub.db"), config.get("image_storage_max_bytes", STORAGE_MAX))
    app.state.image_lock = asyncio.Lock()
    artifacts.register_routes(app)
    app.state.apns_sender = notifications.create_sender(config["apns"])
    with app.state.db:
        for nid in config["node_tokens"]:
            name = str((config.get("node_names") or {}).get(nid, nid))
            app.state.db.execute("INSERT INTO v1_nodes(node_id,name) VALUES(?,?) ON CONFLICT(node_id) DO UPDATE SET name=excluded.name", (nid, name))

    @app.middleware("http")
    async def access_and_size(request: Request, call_next):
        route_path = request.scope["path"]
        root_path = request.scope.get("root_path", "")
        if root_path and (route_path == root_path or route_path.startswith(root_path + "/")):
            route_path = route_path[len(root_path):] or "/"
        global_image = re.fullmatch(r"/api/v1/images/([^/]+)(/content)?", route_path)
        task_image = re.fullmatch(r"/api/v1/tasks/([^/]+)/images/([^/]+)(/content)?", route_path)
        task_file = re.fullmatch(r"/api/v1/tasks/([^/]+)/files(?:/(manifest|retry|f-[^/]+)(/content)?)?", route_path)
        is_file = task_file is not None
        is_image = global_image is not None or task_image is not None
        is_context = re.fullmatch(r"/api/v1/nodes/[^/]+/projects/[^/]+/context(?:/.*)?", route_path) is not None
        is_profile = route_path == "/api/v1/assistant-profile"
        is_notification = "/interactions" in route_path or route_path == "/api/v1/inbox" or route_path.startswith("/api/v1/inbox/") or route_path == "/api/v1/notification-settings" or route_path.startswith("/api/v1/notification-devices/")
        is_pairing = route_path.startswith("/api/v1/pairing/") or route_path.startswith("/api/v1/paired-nodes")
        public_pairing = request.method == "POST" and (route_path == "/api/v1/pairing/requests" or re.fullmatch(r"/api/v1/pairing/p-[0-9a-f]{32}/status", route_path) is not None)
        def reject(status: int, code: str, message: str):
            response = error_response(status, code, message)
            if is_image or is_file or is_profile or is_notification or is_context or is_pairing:
                response.headers["Cache-Control"] = "no-store"
            return response
        # 所有 HTTP 路径默认鉴权，不能因反代 root_path 绕过。
        if route_path != "/health":
            role, node_id = ("pairing", None) if public_pairing else auth_role(config, request.headers.get("authorization", ""), app.state.db)
            if role is None:
                return reject(401, "unauthorized", "访问凭证无效，请检查连接设置")
            request.state.role, request.state.node_id = role, node_id
            if task_file:
                node_write = request.method == "PUT" or (request.method == "POST" and task_file[2] == "manifest")
                if (node_write and role != "node") or (request.method == "POST" and task_file[2] == "retry" and role != "app"):
                    return reject(403, "forbidden_role", "此凭证无权执行此文件操作")
                try:
                    task = repo.require_task(app.state.db, task_file[1])
                    if role == "node" and task["node_id"] != node_id:
                        return reject(403, "forbidden_role", "节点无权访问此任务的成果文件")
                    if task_file[2] and task_file[2] not in {"manifest", "retry"}:
                        artifacts.checked_id(task_file[2])
                except repo.RepoError as exc:
                    return reject(exc.status, exc.code, exc.message)
            elif task_image:
                if request.method == "PUT" and role != "node":
                    return reject(403, "forbidden_role", "结果图片只能由所属 Mac 上传")
                if role == "node":
                    try:
                        task = repo.require_task(app.state.db, task_image[1])
                        if task["node_id"] != node_id:
                            return reject(403, "forbidden_role", "节点无权访问此任务的图片")
                    except repo.RepoError as exc:
                        return reject(exc.status, exc.code, exc.message)
            elif role != "app" and not public_pairing:
                return reject(403, "forbidden_role", "节点凭证不能访问此接口")
            if is_image:
                try:
                    checked_id(global_image[1] if global_image else task_image[2])
                except repo.RepoError as exc:
                    return reject(exc.status, exc.code, exc.message)
            image_upload = is_image and request.method == "PUT" and not route_path.endswith("/content")
            file_upload = is_file and request.method == "PUT" and not route_path.endswith("/content")
            body_max = 2048 if is_pairing else FILE_MAX if file_upload else IMAGE_MAX if image_upload else BODY_MAX
            if file_upload:
                mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in FILE_TYPES.values():
                    return reject(415, "unsupported_file", "成果文件类型不支持")
            if image_upload:
                mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if mime not in {"image/png", "image/jpeg"}:
                    return reject(415, "unsupported_image", "仅支持 JPEG 或 PNG 图片")
            try:
                declared_size = int(request.headers.get("content-length", "0"))
            except ValueError:
                return reject(400, "invalid_field", "请求长度无效")
            if declared_size < 0 or declared_size > body_max:
                return reject(413 if image_upload or file_upload else 400, "file_too_large" if file_upload else "image_too_large" if image_upload else "body_too_large", "文件超过 16 MiB" if file_upload else "图片超过 6 MiB" if image_upload else "请求内容过大")
            parts, size = [], 0
            async for chunk in request.stream():
                size += len(chunk)
                if size > body_max:
                    return reject(413 if image_upload or file_upload else 400, "file_too_large" if file_upload else "image_too_large" if image_upload else "body_too_large", "文件超过 16 MiB" if file_upload else "图片超过 6 MiB" if image_upload else "请求内容过大")
                parts.append(chunk)
            request._body = b"".join(parts)
            if request.method in {"POST", "PUT"} and not image_upload and not file_upload and request._body:
                try:
                    json.loads(request._body)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return reject(400, "invalid_json", "请求不是有效的 JSON")
        response = await call_next(request)
        if is_image or is_file or is_profile or is_notification or is_context or is_pairing:
            response.headers["Cache-Control"] = "no-store"
        if is_image or is_file:
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.exception_handler(repo.RepoError)
    async def repo_error(request: Request, exc: repo.RepoError):
        return error_response(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = exc.errors()
        if any(err.get("type") == "json_invalid" for err in errors):
            return error_response(400, "invalid_json", "请求不是有效的 JSON")
        if any("prompt_too_large" in str(err.get("ctx", {}).get("error", "")) for err in errors):
            return error_response(400, "prompt_too_large", "任务内容超过 16 KiB，请缩短后再试")
        if any("profile_too_large" in str(err.get("ctx", {}).get("error", "")) for err in errors):
            return error_response(422, "profile_too_large", "助理资料超过 16 KiB，请减少文字或记忆条目")
        if any("project_context_too_large" in str(err.get("ctx", {}).get("error", "")) for err in errors):
            return error_response(422, "project_context_too_large", "项目背景超过 12 KiB，请减少文字")
        return error_response(422, "invalid_field", "请求字段缺失或格式无效")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(exc.status_code, "not_found" if exc.status_code == 404 else "invalid_field", "接口不存在" if exc.status_code == 404 else "请求无法处理")

    def check_rate_limit() -> None:
        times = app.state.creation_times
        now = time.monotonic()
        while times and now - times[0] >= 60:
            times.popleft()
        if len(times) >= int(config.get("task_create_per_minute", 30)):
            raise repo.RepoError(429, "rate_limited", "提交过于频繁，请稍后再试")
        times.append(now)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/v1/assistant-profile")
    async def get_assistant_profile():
        return assistant_profile.read_profile(app.state.db)

    @app.put("/api/v1/assistant-profile")
    async def put_assistant_profile(req: AssistantProfile):
        return assistant_profile.write_profile(app.state.db, req)

    @app.get("/api/v1/inbox")
    async def result_inbox(cursor: str | None = None, limit: int = 30):
        return inbox.list_unread(app.state.db, cursor, limit)

    @app.get("/api/v1/inbox/by-task/{task_id}")
    async def inbox_task_item(task_id: str):
        return {"server_id": inbox.server_id(app.state.db), "item": inbox.get_item_for_task(app.state.db, task_id)}

    @app.get("/api/v1/inbox/{result_id}")
    async def inbox_item(result_id: str):
        return {"server_id": inbox.server_id(app.state.db), "item": inbox.get_item(app.state.db, result_id)}

    @app.put("/api/v1/inbox/{result_id}/read")
    async def read_result(result_id: str, req: ResultRead):
        return inbox.mark_read(app.state.db, result_id, req.revision)

    def notification_settings() -> dict:
        return {"server_id": inbox.server_id(app.state.db), "push_available": app.state.apns_sender is not None}

    @app.get("/api/v1/notification-settings")
    async def get_notification_settings():
        return notification_settings()

    @app.put("/api/v1/notification-devices/{device_id}")
    async def put_notification_device(device_id: str, req: NotificationDevice):
        inbox.save_device(app.state.db, device_id, req, config["apns"].get("environment"))
        return {**notification_settings(), "device_id": device_id, "enabled": req.enabled}

    @app.delete("/api/v1/notification-devices/{device_id}")
    async def delete_notification_device(device_id: str):
        inbox.remove_device(app.state.db, device_id)
        return {**notification_settings(), "device_id": device_id, "enabled": False}

    @app.get("/api/v1/nodes")
    async def nodes():
        items = []
        for row in app.state.db.execute("SELECT * FROM v1_nodes ORDER BY node_id"):
            if not pairing.registered(app, row["node_id"]):
                continue
            state = app.state.nodes.get(row["node_id"])
            items.append({"node_id": row["node_id"], "name": row["name"], "online": bool(state), "last_seen": state.last_seen_iso if state else row["last_seen"], "busy": bool(repo.active_tasks(app.state.db, row["node_id"])) or bool(state and state.running)})
        return {"nodes": items}

    @app.get("/api/v1/projects")
    async def projects():
        return {"projects": [{"project_id": row["project_id"], "name": row["name"], "node_id": row["node_id"], "available": bool(row["available"]), "execution_mode": row["execution_mode"]} for row in app.state.db.execute("SELECT * FROM v1_projects ORDER BY node_id,project_id") if pairing.registered(app, row["node_id"])]}

    @app.post("/api/v1/tasks")
    async def create_task(req: TaskCreate):
        data = req.model_dump()
        fingerprint = repo.request_fingerprint("create", data)
        existing = repo.existing_request(app.state.db, req.client_request_id, fingerprint)
        if existing:
            return JSONResponse(status_code=200, content={"task_id": existing["task_id"], "status": existing["status"]})
        if not pairing.registered(app, req.node_id):
            raise repo.RepoError(422, "invalid_field", "此 Mac 尚未登记")
        validate_project(app.state.db, req.node_id, req.project_id)
        assistant = assistant_profile.task_snapshot(app.state.db)
        project = project_context.task_snapshot(app.state.db, req.node_id, req.project_id)
        validate_assignment_size(app.state.db, req.prompt, assistant, project, req.image_ids)
        check_rate_limit()
        task, fresh = repo.create_task(app.state.db, req.client_request_id, fingerprint, req.node_id, req.project_id, req.prompt, int(config.get("default_timeout_sec", 1800)), image_ids=req.image_ids,
                                       assistant_context=assistant, project_context=project)
        await schedule_node(app, req.node_id)
        return JSONResponse(status_code=201 if fresh else 200, content={"task_id": task["task_id"], "status": task["status"]})

    async def submit_project_summary(node_id: str, project_id: str, req: project_context.ProjectSummaryCreate):
        prepared = project_context.prepare_summary(app.state.db, node_id, project_id, req)
        existing = prepared["existing"]
        if existing:
            return JSONResponse(status_code=200, content={"task_id": existing["task_id"], "status": existing["status"]})
        if not pairing.registered(app, node_id):
            raise repo.RepoError(422, "invalid_field", "此设备尚未登记")
        validate_project(app.state.db, node_id, project_id)
        assistant = assistant_profile.task_snapshot(app.state.db)
        validate_assignment_size(app.state.db, prepared["prompt"], assistant, prepared["project_context"])
        check_rate_limit()
        task, fresh = repo.create_task(app.state.db, req.client_request_id, prepared["fingerprint"], node_id,
            project_id, prepared["prompt"], int(config.get("default_timeout_sec", 1800)),
            assistant_context=assistant,
            project_context=prepared["project_context"], summary_sources=prepared["source_task_ids"])
        await schedule_node(app, node_id)
        return JSONResponse(status_code=201 if fresh else 200, content={"task_id": task["task_id"], "status": task["status"]})

    project_context.register_routes(app, submit_project_summary)
    interactions.register_routes(app)
    continuous.register_routes(app)
    pairing.register_routes(app)

    @app.get("/api/v1/tasks")
    async def tasks(cursor: str | None = None, limit: int = 20):
        limit = max(1, min(limit, 100))
        before = 9223372036854775807
        if cursor:
            try:
                before = int(cursor)
            except ValueError:
                raise repo.RepoError(422, "invalid_field", "分页游标无效")
        rows = list(app.state.db.execute("SELECT rowid,* FROM v1_tasks WHERE rowid<? ORDER BY rowid DESC LIMIT ?", (before, limit + 1)))
        page = rows[:limit]
        return {"tasks": [repo.task_view(app.state.db, dict(row)) for row in page], "next_cursor": str(page[-1]["rowid"]) if len(rows) > limit and page else None}

    @app.get("/api/v1/tasks/{task_id}")
    async def task_detail(task_id: str):
        return repo.task_view(app.state.db, repo.require_task(app.state.db, task_id), include_prompt=True)

    @app.get("/api/v1/tasks/{task_id}/events")
    async def task_events(task_id: str, after: int = 0, limit: int = 100):
        repo.require_task(app.state.db, task_id)
        if after < 0:
            raise repo.RepoError(422, "invalid_field", "事件游标无效")
        limit = max(1, min(limit, 100))
        rows = list(app.state.db.execute("SELECT * FROM v1_events WHERE task_id=? AND id>? ORDER BY id LIMIT ?", (task_id, after, limit + 1)))
        page = rows[:limit]
        return {"events": [{"id": row["id"], "task_id": task_id, "type": row["type"], "payload": json.loads(row["payload_json"]), "created_at": row["created_at"]} for row in page], "next_after": page[-1]["id"] if page else after, "has_more": len(rows) > limit}

    @app.post("/api/v1/tasks/{task_id}/cancel")
    async def cancel(task_id: str):
        task = repo.require_task(app.state.db, task_id)
        if task["status"] in repo.TERMINAL:
            return {"task_id": task_id, "status": task["status"], "cancel": "ignored_terminal"}
        if task["status"] == "queued":
            repo.change_status(app.state.db, task, "cancelled")
            return {"task_id": task_id, "status": "cancelled", "cancel": "recorded"}
        with app.state.db:
            app.state.db.execute("UPDATE v1_tasks SET cancel_requested=1 WHERE task_id=?", (task_id,))
        if task["status"] != "cancel_requested":
            repo.change_status(app.state.db, task, "cancel_requested")
        state = app.state.nodes.get(task["node_id"])
        delivered = False
        if state and state.ready:
            try:
                await state.send({"type": "task_cancel", "task_id": task_id})
                delivered = True
            except Exception:
                app.state.nodes.unregister(state)
        return {"task_id": task_id, "status": "cancel_requested", "cancel": "delivered" if delivered else "recorded"}

    @app.post("/api/v1/tasks/{task_id}/followup")
    async def followup(task_id: str, req: FollowupCreate):
        fingerprint = repo.request_fingerprint("followup", {"task_id": task_id, **req.model_dump()})
        existing = repo.existing_request(app.state.db, req.client_request_id, fingerprint)
        original = repo.require_task(app.state.db, task_id)
        if existing:
            return JSONResponse(status_code=200, content={"task_id": existing["task_id"], "status": existing["status"], "follows_task_id": task_id, "node_online": bool(app.state.nodes.get(existing["node_id"]))})
        if original["status"] != "done" or not original["dsh_session_id"]:
            raise repo.RepoError(409, "not_followable", "只能续接已完成且保存了会话的任务")
        validate_project(app.state.db, original["node_id"], original["project_id"])
        validate_assignment_size(app.state.db, req.message, repo.task_context(original),
            repo.project_snapshot(original), req.image_ids, follows_task_id=task_id)
        check_rate_limit()
        task, fresh = repo.create_task(app.state.db, req.client_request_id, fingerprint, original["node_id"], original["project_id"], req.message, original["timeout_sec"], task_id, req.image_ids,
                                       assistant_context=repo.task_context(original), project_context=repo.project_snapshot(original))
        await schedule_node(app, original["node_id"])
        return JSONResponse(status_code=201 if fresh else 200, content={"task_id": task["task_id"], "status": task["status"], "follows_task_id": task_id, "node_online": bool(app.state.nodes.get(original["node_id"]))})

    def image_metadata(image_id: str) -> dict:
        checked_id(image_id)
        row = app.state.db.execute("SELECT * FROM v1_images WHERE image_id=?", (image_id,)).fetchone()
        if row is None:
            raise repo.RepoError(404, "image_not_found", "图片不存在")
        return repo.image_view(row)

    def image_content(image_id: str) -> Response:
        metadata = image_metadata(image_id)
        body = app.state.images.read(image_id, metadata)
        suffix = "png" if metadata["mime_type"] == "image/png" else "jpg"
        return Response(body, media_type=metadata["mime_type"], headers={
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Disposition": f'inline; filename="{image_id}.{suffix}"'})

    @app.put("/api/v1/images/{image_id}")
    async def upload_input(image_id: str, request: Request):
        body = await request.body()
        mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        async with app.state.image_lock:
            return app.state.images.put(app.state.db, image_id, body, mime)

    @app.get("/api/v1/images/{image_id}/content")
    async def read_image(image_id: str):
        return image_content(image_id)

    @app.get("/api/v1/tasks/{task_id}/images/{image_id}/content")
    async def read_task_image(task_id: str, image_id: str):
        repo.require_task(app.state.db, task_id)
        checked_id(image_id)
        if not app.state.db.execute("SELECT 1 FROM v1_task_images WHERE task_id=? AND image_id=?", (task_id, image_id)).fetchone():
            raise repo.RepoError(404, "image_not_found", "图片未关联此任务")
        return image_content(image_id)

    @app.put("/api/v1/tasks/{task_id}/images/{image_id}")
    async def upload_output(task_id: str, image_id: str, request: Request):
        body = await request.body()
        mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        async with app.state.image_lock:
            return app.state.images.put(app.state.db, image_id, body, mime, repo.require_task(app.state.db, task_id))

    @app.websocket("/ws/node")
    async def node_ws(websocket: WebSocket):
        role, node_id = auth_role(config, websocket.headers.get("authorization", ""), app.state.db)
        if role != "node":
            status = 403 if role else 401
            if "websocket.http.response" in websocket.scope.get("extensions", {}):
                await websocket.send_denial_response(error_response(status, "forbidden_role" if role else "unauthorized", "节点访问凭证无效"))
            else:
                await websocket.accept()
                await websocket.close(code=4403 if role else 4401)
            return
        await websocket.accept()
        state = None
        try:
            raw = await asyncio.wait_for(websocket.receive_text(), timeout=15)
            if len(raw.encode("utf-8")) > BODY_MAX:
                raise repo.RepoError(400, "body_too_large", "节点上线消息过大")
            hello = json.loads(raw)
            if not isinstance(hello, dict) or hello.get("type") != "hello" or type(hello.get("protocol")) is not int or hello.get("protocol") != 1 or not isinstance(hello.get("boot_id"), str) or not hello["boot_id"]:
                raise repo.RepoError(422, "invalid_field", "首条消息必须为 v1 hello")
            if hello.get("node_id", node_id) != node_id:
                raise repo.RepoError(403, "forbidden_role", "节点身份与凭证不符")
            announced, journals = hello.get("projects"), hello.get("tasks")
            capabilities = hello.get("capabilities", [])
            if not isinstance(capabilities, list) or len(capabilities) > 20 or any(not isinstance(c, str) or not re.fullmatch(r"[a-z0-9_]{1,64}", c) for c in capabilities):
                raise repo.RepoError(422, "invalid_field", "节点能力声明无效")
            if not isinstance(announced, list) or not isinstance(journals, list) or len(announced) > 100 or len(journals) > 1000:
                raise repo.RepoError(422, "invalid_field", "上线项目或任务列表无效")
            validated = []
            for project in announced:
                if not isinstance(project, dict) or not isinstance(project.get("project_id"), str) or not project["project_id"] or not isinstance(project.get("path"), str) or not Path(project["path"]).is_absolute() or not isinstance(project.get("available"), bool):
                    raise repo.RepoError(422, "invalid_field", "登记项目必须包含编号、绝对路径与可用状态")
                mode = project.get("execution_mode", "headless")
                if mode not in {"headless", "host"} or (mode == "host" and ("interactions_v1" not in capabilities or project["project_id"] != "p-xvis-test")):
                    raise repo.RepoError(422, "invalid_field", "Host 仅能在支持中途交互的测试工作区启用")
                validated.append(project)
            reconcile_ids = {task["task_id"] for task in repo.active_tasks(app.state.db, node_id)}
            for journal in journals:
                if not isinstance(journal, dict) or not isinstance(journal.get("task_id"), str):
                    raise repo.RepoError(422, "invalid_field", "节点任务记录无效")
                task = repo.get_task(app.state.db, journal["task_id"])
                if not task or task["node_id"] != node_id:
                    raise repo.RepoError(403, "forbidden_role", "节点包含不属于自己的任务记录")
                reconcile_ids.add(task["task_id"])
            if not pairing.registered(app, node_id):
                raise repo.RepoError(403, "forbidden_role", "此设备授权已撤销")
            state, previous = app.state.nodes.register(node_id, websocket, hello["boot_id"])
            state.capabilities = set(capabilities)
            state.reconciliation = {tid: None for tid in sorted(reconcile_ids)}
            with app.state.db:
                app.state.db.execute("UPDATE v1_projects SET available=0 WHERE node_id=?", (node_id,))
                for project in validated:
                    app.state.db.execute("INSERT INTO v1_projects(node_id,project_id,name,path,available) VALUES(?,?,?,?,?) ON CONFLICT(node_id,project_id) DO UPDATE SET name=excluded.name,path=excluded.path,available=excluded.available", (node_id, project["project_id"], str(project.get("name") or project["project_id"]), project["path"], project["available"]))
                    app.state.db.execute("UPDATE v1_projects SET execution_mode=? WHERE node_id=? AND project_id=?",
                        (project.get("execution_mode", "headless"), node_id, project["project_id"]))
                app.state.db.execute("UPDATE v1_nodes SET last_seen=? WHERE node_id=?", (state.last_seen_iso, node_id))
            if previous:
                try:
                    await previous.ws.close(code=4409)
                except Exception:
                    pass
            await state.send({"type": "hello_ack", "node_id": node_id, "ready": False})
            snapshot = []
            for tid in state.reconciliation:
                task = repo.require_task(app.state.db, tid)
                snapshot.append({"task_id": tid, "expected_status": task["status"], "last_event_seq": task["acked_seq"], "cancel_requested": bool(task["cancel_requested"])})
            await state.send({"type": "task_snapshot", "tasks": snapshot})
            # 空 snapshot 仍由首个心跳确认本机空闲后再 ready。
            while app.state.nodes.current(state):
                raw = await websocket.receive_text()
                if len(raw.encode("utf-8")) > 131072:
                    raise repo.RepoError(400, "body_too_large", "节点消息过大")
                try:
                    msg = json.loads(raw)
                    if not isinstance(msg, dict):
                        raise repo.RepoError(422, "invalid_field", "节点消息必须为对象")
                    await handle_node_message(app, state, msg)
                except (repo.RepoError, json.JSONDecodeError) as exc:
                    code = exc.code if isinstance(exc, repo.RepoError) else "invalid_json"
                    message = exc.message if isinstance(exc, repo.RepoError) else "节点消息不是有效 JSON"
                    LOGGER.warning("节点消息拒绝 node_id=%s code=%s", node_id, code)
                    await state.send({"type": "protocol_error", "code": code, "message": message})
        except (WebSocketDisconnect, asyncio.TimeoutError):
            pass
        except (repo.RepoError, json.JSONDecodeError):
            await websocket.close(code=4400)
        except Exception:
            LOGGER.warning("节点连接异常 node_id=%s", node_id)
        finally:
            if state:
                app.state.nodes.unregister(state)

    return app


def main(config_path: str = "hub/config.yaml") -> None:
    import uvicorn
    config = load_config(config_path)
    host, port = str(config.get("host", "127.0.0.1")), int(config.get("port", 18300))
    # 真实 bind 预检，端口不可用时非零退出。
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        # 与 asyncio/Uvicorn 监听一致：允许重启复用 TIME_WAIT，不能复用正在监听的端口。
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise SystemExit(f"中枢监听端口不可用：{host}:{port}") from exc
    uvicorn.run(create_app(config_path), host=host, port=port, workers=1, root_path=str(config.get("root_path", "")), access_log=False, ws="websockets", ws_max_size=131072, log_level=str(config.get("log_level", "info")).lower())
