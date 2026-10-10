"""D1 专项：临时 SQLite/配置、loopback Hub 与假节点，无模型或生产连接。"""
from __future__ import annotations
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import time
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import httpx
import uvicorn
import websockets
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hub.gateway.app import create_app, load_config
from hub.gateway import v1_repo as repo
from shared.v1_db import init_v1_db

APP_TOKEN = "A" * 48
NODE_TOKEN = "B" * 48
OTHER_TOKEN = "C" * 48


def config_data(directory: Path, port: int = 18300) -> dict:
    return {"host": "127.0.0.1", "port": port, "db_path": str(directory / "hub.db"),
            "app_token": APP_TOKEN, "node_tokens": {"mac-a": NODE_TOKEN, "mac-b": OTHER_TOKEN},
            "node_names": {"mac-a": "测试 Mac"}, "log_level": "ERROR", "task_create_per_minute": 1000,
            "node_scan_interval_sec": 0.05, "node_heartbeat_timeout_sec": 5}


class ConfigAndRetentionTests(unittest.TestCase):
    def test_config_fail_closed_and_explicit_path(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            cfg = directory / "explicit.yaml"
            data = config_data(directory)
            cfg.write_text(yaml.safe_dump(data), encoding="utf-8")
            (directory / "config.local.yaml").write_text("app_token: wrong", encoding="utf-8")
            self.assertEqual(load_config(str(cfg))["app_token"], APP_TOKEN)
            for bad in ("", "changeme" * 10, "${MISSING_XVIS_HUB_TEST_ENV}", NODE_TOKEN):
                cfg.write_text(yaml.safe_dump({**data, "app_token": bad}), encoding="utf-8")
                with self.assertRaises(ValueError):
                    load_config(str(cfg))
            cfg.write_text(yaml.safe_dump({**data, "app_token": "${XVIS_HUB_TEST_ENV}"}), encoding="utf-8")
            with patch.dict(os.environ, {"XVIS_HUB_TEST_ENV": APP_TOKEN}):
                self.assertEqual(load_config(str(cfg))["app_token"], APP_TOKEN)

    def test_restart_after_tcp_active_close_time_wait(self):
        # 与 Linux 生产复现同构：服务端主动关闭使其监听端口进入 TIME_WAIT。
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.listen(1)
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
                client.settimeout(2)
                client.connect(("127.0.0.1", port))
                accepted, _ = listener.accept()
                accepted.shutdown(socket.SHUT_RDWR)
                accepted.close()
                self.assertEqual(client.recv(1), b"")
        finally:
            listener.close()
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as raw_probe:
            with self.assertRaises(OSError):
                raw_probe.bind(("127.0.0.1", port))
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            cfg = directory / "restart.yaml"
            cfg.write_text(yaml.safe_dump(config_data(directory, port)), encoding="utf-8")
            proc = subprocess.Popen([sys.executable, str(ROOT / "scripts/start_hub.py"), "--config", str(cfg)], cwd=ROOT,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with httpx.Client(trust_env=False, timeout=0.2) as client:
                    started = False
                    deadline = time.monotonic() + 5
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            break
                        try:
                            response = client.get(f"http://127.0.0.1:{port}/health")
                            if response.status_code == 200:
                                self.assertEqual(response.json(), {"status": "ok"})
                                started = True
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.02)
                self.assertTrue(started, "真实中枢未能在 TIME_WAIT 后立即监听")
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)

    def test_active_listener_still_refuses_start_with_nonzero_exit(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            with tempfile.TemporaryDirectory() as temp:
                directory = Path(temp)
                cfg = directory / "occupied.yaml"
                cfg.write_text(yaml.safe_dump(config_data(directory, listener.getsockname()[1])), encoding="utf-8")
                result = subprocess.run([sys.executable, str(ROOT / "scripts/start_hub.py"), "--config", str(cfg)], cwd=ROOT,
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                self.assertNotEqual(result.returncode, 0)

    def test_retention_preserves_active_and_unacked_events(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_v1_db(Path(temp) / "hub.db")
            old = "2000-01-01T00:00:00.000+00:00"
            for i, status in enumerate(("running", "done")):
                task, _ = repo.create_task(conn, str(i), str(i), "mac-a", "p", "x", 30)
                repo.change_status(conn, task, "assigned")
                repo.change_status(conn, repo.require_task(conn, task["task_id"]), status)
                repo.record_event(conn, repo.require_task(conn, task["task_id"]), 1, {"type": "text", "text": "acked"}, old)
                repo.record_event(conn, repo.require_task(conn, task["task_id"]), 3, {"type": "text", "text": "gap"}, old)
                with conn:
                    conn.execute("UPDATE v1_events SET created_at=? WHERE task_id=?", (old, task["task_id"]))
            repo.prune_events(conn)
            remaining = list(conn.execute("SELECT event_seq FROM v1_events WHERE event_seq IS NOT NULL ORDER BY task_id,event_seq"))
            self.assertEqual(sorted(row[0] for row in remaining), [1, 3, 3])
            conn.close()


class HubIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        self.config_path = self.directory / "explicit.yaml"
        self.config_path.write_text(yaml.safe_dump(config_data(self.directory, self.port)), encoding="utf-8")
        self.app = create_app(str(self.config_path))
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="on", ws="websockets", ws_max_size=131072))
        self.server_task = asyncio.create_task(self.server.serve())
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}", headers={"Authorization": f"Bearer {APP_TOKEN}"}, timeout=5, trust_env=False)
        self.connections = []
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.server.started)

    async def asyncTearDown(self):
        for ws in self.connections:
            await ws.close()
        await self.client.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 5)
        self.temp.cleanup()

    async def open_node(self, *, node_id="mac-a", token=NODE_TOKEN, tasks=None, finish=True):
        ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/node", additional_headers={"Authorization": f"Bearer {token}"}, max_size=131072)
        self.connections.append(ws)
        await ws.send(json.dumps({"type": "hello", "protocol": 1, "boot_id": str(uuid.uuid4()), "node_id": node_id,
                                  "projects": [{"project_id": "p-accept", "path": str(self.directory), "available": True}], "tasks": tasks or []}))
        ack = await self.recv(ws, "hello_ack")
        self.assertFalse(ack["ready"])
        snapshot = await self.recv(ws, "task_snapshot")
        if finish:
            self.assertEqual(snapshot["tasks"], [])
            await self.send(ws, {"type": "heartbeat", "running": []})
            await self.recv(ws, "ready")
        return ws, snapshot

    async def recv(self, ws, kind):
        message = json.loads(await asyncio.wait_for(ws.recv(), 2))
        self.assertEqual(message.get("type"), kind, message)
        return message

    async def send(self, ws, message):
        await ws.send(json.dumps(message, ensure_ascii=False))
        await asyncio.sleep(0.01)

    async def create(self, request_id=None, prompt="执行测试", **kwargs):
        body = {"client_request_id": request_id or str(uuid.uuid4()), "node_id": "mac-a", "project_id": "p-accept", "prompt": prompt, **kwargs}
        response = await self.client.post("/api/v1/tasks", json=body)
        return response, body

    async def detail(self, tid):
        response = await self.client.get(f"/api/v1/tasks/{tid}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    async def start(self, ws, tid):
        await self.send(ws, {"type": "task_accepted", "task_id": tid, "journal_seq": 1})
        await self.send(ws, {"type": "task_started", "task_id": tid, "dsh_session_id": "session-" + tid, "pid": 1234, "pgid": 1234})
        await self.send(ws, {"type": "heartbeat", "running": [tid]})

    async def done(self, ws, tid, **fields):
        await self.send(ws, {"type": "task_done", "task_id": tid, "status": "done", "dsh_session_id": "session-" + tid,
                             "final_text": "完成", "exit_code": 0, "turn_end_reason": {"kind": "completed"}, **fields})

    async def no_message(self, ws):
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(ws.recv(), 0.12)

    async def test_auth_health_legacy_and_body_limits(self):
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})
        for token, status in (("wrong", 401), (NODE_TOKEN, 403)):
            response = await self.client.get("/api/v1/nodes", headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(response.status_code, status)
            self.assertIn("error", response.json())
        self.assertEqual((await self.client.post("/api/chat", json={})).status_code, 404)
        with self.assertRaises(websockets.exceptions.InvalidStatus):
            await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/node", additional_headers={"Authorization": f"Bearer {APP_TOKEN}"})
        response = await self.client.post("/api/v1/tasks", content="{")
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "invalid_json"))
        response = await self.client.post("/api/v1/tasks", content="x" * 65537)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "body_too_large"))
        response, _ = await self.create(prompt="中" * 5462)
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "prompt_too_large"))
        response, _ = await self.create(project_id="unknown")
        self.assertEqual(response.status_code, 422)

    async def test_node_credentials_cannot_spoof_identity_or_use_query_tokens(self):
        for kwargs, expected in (({"additional_headers": {"Authorization": "Bearer invalid"}}, 401), ({}, 401)):
            with self.assertRaises(websockets.exceptions.InvalidStatus) as failure:
                await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/node?node_id=mac-a&token={NODE_TOKEN}", **kwargs)
            self.assertEqual(failure.exception.response.status_code, expected)
        ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/node", additional_headers={"Authorization": f"Bearer {NODE_TOKEN}"})
        self.connections.append(ws)
        await self.send(ws, {"type": "hello", "protocol": 1, "boot_id": "test", "node_id": "mac-b", "projects": [], "tasks": []})
        with self.assertRaises(websockets.exceptions.ConnectionClosed):
            await ws.recv()
        self.assertFalse((await self.client.get("/api/v1/nodes")).json()["nodes"][0]["online"])

    async def test_chunked_body_limit_and_invalid_terminal_shapes(self):
        async def oversized_chunks():
            yield b"x" * 32768
            yield b"x" * 32768
            yield b"x"
        response = await self.client.post("/api/v1/tasks", content=oversized_chunks())
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (400, "body_too_large"))
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        for changed in ({"exit_code": False}, {"final_text": {"text": "wrong"}}, {"final_text": "x" * 65537}):
            await self.done(ws, tid, **changed)
            self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "invalid_field")
            self.assertEqual((await self.detail(tid))["status"], "running")
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 1, "event": {"type": "text", "text": "x" * 65536}})
        self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "invalid_field")

    async def test_explicit_node_rejection_fails_without_blocking_next(self):
        ws, _ = await self.open_node()
        first, _ = await self.create()
        tid = first.json()["task_id"]
        await self.recv(ws, "task_assign")
        second, _ = await self.create()
        await self.send(ws, {"type": "task_rejected", "task_id": tid, "reason": "invalid_followup"})
        detail = await self.detail(tid)
        self.assertEqual(detail["status"], "failed")
        self.assertEqual(detail["error"]["code"], "invalid_followup")
        await self.no_message(ws)
        await self.send(ws, {"type": "heartbeat", "running": []})
        self.assertEqual((await self.recv(ws, "task_assign"))["task_id"], second.json()["task_id"])

    async def test_reverse_proxy_root_path_never_bypasses_auth(self):
        self.server.should_exit = True
        await self.server_task
        self.app = create_app(str(self.config_path))
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, root_path="/xvis", log_level="error", lifespan="on", ws="websockets"))
        self.server_task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        # 模拟 Nginx 剥去 /xvis/ 后转发，uvicorn 再将 root_path 带入 scope。
        health = await self.client.get("/health", headers={"Authorization": ""})
        self.assertEqual(health.json(), {"status": "ok"})
        ws, _ = await self.open_node()
        for token, expected in (("", 401), ("invalid", 401), (NODE_TOKEN, 403)):
            headers = {"Authorization": f"Bearer {token}" if token else ""}
            response = await self.client.get("/api/v1/nodes", headers=headers)
            self.assertEqual(response.status_code, expected)
            response = await self.client.post("/api/v1/tasks", headers=headers, json={"client_request_id": "root-path", "node_id": "mac-a", "project_id": "p-accept", "prompt": "测试"})
            self.assertEqual(response.status_code, expected)
        self.assertEqual(self.app.state.db.execute("SELECT COUNT(*) FROM v1_tasks").fetchone()[0], 0)
        valid, _ = await self.create()
        self.assertEqual(valid.status_code, 201)
        await self.recv(ws, "task_assign")

    async def test_cancel_before_fork_uses_not_started_proof(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.send(ws, {"type": "task_accepted", "task_id": tid, "journal_seq": 1})
        await self.client.post(f"/api/v1/tasks/{tid}/cancel", json={})
        await self.recv(ws, "task_cancel")
        await self.send(ws, {"type": "task_done", "task_id": tid, "status": "cancelled", "exit_code": None, "not_started": True})
        self.assertEqual((await self.detail(tid))["status"], "cancelled")
        await self.send(ws, {"type": "heartbeat", "running": []})
        await self.no_message(ws)

    async def test_cancel_before_busy_rejection_does_not_leave_slot_stuck(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.client.post(f"/api/v1/tasks/{tid}/cancel", json={})
        await self.recv(ws, "task_cancel")
        await self.send(ws, {"type": "task_rejected", "task_id": tid, "reason": "busy"})
        self.assertEqual((await self.detail(tid))["status"], "cancelled")
        self.assertEqual(repo.require_task(self.app.state.db, tid)["cancel_requested"], 1)
        next_task, _ = await self.create()
        await self.no_message(ws)
        await self.send(ws, {"type": "heartbeat", "running": []})
        self.assertEqual((await self.recv(ws, "task_assign"))["task_id"], next_task.json()["task_id"])

    async def test_five_concurrent_tasks_single_slot_and_idempotent_requests(self):
        ws, _ = await self.open_node()
        responses = await asyncio.gather(*(self.create(request_id=f"five-{i}") for i in range(5)))
        tids = [response.json()["task_id"] for response, _ in responses]
        self.assertEqual(len(set(tids)), 5)
        assigned = await self.recv(ws, "task_assign")
        await self.no_message(ws)
        original = next(body for response, body in responses if response.json()["task_id"] == assigned["task_id"])
        retries = await asyncio.gather(*(self.client.post("/api/v1/tasks", json=original) for _ in range(5)))
        self.assertEqual({resp.status_code for resp in retries}, {200})
        self.assertEqual({resp.json()["task_id"] for resp in retries}, {assigned["task_id"]})
        conflict = await self.client.post("/api/v1/tasks", json={**original, "prompt": "内容不同"})
        self.assertEqual(conflict.status_code, 409)
        for i in range(5):
            tid = assigned["task_id"]
            await self.start(ws, tid)
            await self.done(ws, tid)
            await self.no_message(ws)  # terminal 自身不释放槽位
            await self.send(ws, {"type": "heartbeat", "running": []})
            self.assertEqual((await self.detail(tid))["status"], "done")
            if i < 4:
                assigned = await self.recv(ws, "task_assign")
        self.assertEqual(self.app.state.db.execute("SELECT COUNT(*) FROM v1_tasks").fetchone()[0], 5)

    async def test_event_gaps_duplicates_ownership_and_terminal_gate(self):
        ws, _ = await self.open_node()
        other, _ = await self.open_node(node_id="mac-b", token=OTHER_TOKEN)
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        for seq, expected in ((1, 1), (3, 1), (2, 3), (2, 3)):
            await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": seq, "event": {"type": "text", "text": str(seq)}})
            self.assertEqual((await self.recv(ws, "event_ack"))["acked_seq"], expected)
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 2, "event": {"type": "text", "text": "冲突"}})
        self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "event_conflict")
        await self.send(other, {"type": "task_done", "task_id": tid, "status": "failed"})
        self.assertEqual((await self.recv(other, "protocol_error"))["code"], "forbidden_role")
        await self.done(ws, tid, turn_end_reason={"kind": "error"})
        self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "invalid_field")
        self.assertEqual((await self.detail(tid))["status"], "running")
        await self.done(ws, tid)
        await self.send(ws, {"type": "task_done", "task_id": tid, "status": "failed", "detail": "迟到"})
        self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "invalid_state")
        await self.send(ws, {"type": "task_started", "task_id": tid, "dsh_session_id": "wrong"})
        self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "invalid_state")
        self.assertEqual((await self.detail(tid))["status"], "done")
        all_events = (await self.client.get(f"/api/v1/tasks/{tid}/events")).json()["events"]
        self.assertEqual(len([event for event in all_events if event["type"] == "text"]), 3)

    async def test_cancel_intent_stop_confirmation_and_terminal_retry(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        cancellation = (await self.client.post(f"/api/v1/tasks/{tid}/cancel", json={})).json()
        self.assertEqual((cancellation["status"], cancellation["cancel"]), ("cancel_requested", "delivered"))
        await self.recv(ws, "task_cancel")
        await self.send(ws, {"type": "task_done", "task_id": tid, "status": "cancelled", "exit_code": None})
        self.assertEqual((await self.recv(ws, "protocol_error"))["code"], "invalid_field")
        self.assertEqual((await self.detail(tid))["status"], "cancel_requested")
        queued, _ = await self.create()
        next_tid = queued.json()["task_id"]
        await self.send(ws, {"type": "task_done", "task_id": tid, "status": "interrupted", "exit_code": None, "detail": "停止未确认"})
        await self.send(ws, {"type": "heartbeat", "running": [tid]})
        await self.no_message(ws)
        self.assertTrue((await self.client.get("/api/v1/nodes")).json()["nodes"][0]["busy"])
        retry = (await self.client.post(f"/api/v1/tasks/{tid}/cancel", json={})).json()
        self.assertEqual(retry["cancel"], "ignored_terminal")
        await self.send(ws, {"type": "heartbeat", "running": []})
        self.assertEqual((await self.recv(ws, "task_assign"))["task_id"], next_tid)

    async def test_queued_cancel_and_followup_new_task_offline_idempotency(self):
        ws, _ = await self.open_node()
        parent_prompt = "  检查当前项目\n保留这段原始说明。  "
        response, _ = await self.create(prompt=parent_prompt)
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        not_followable = await self.client.post(f"/api/v1/tasks/{tid}/followup", json={"client_request_id": "premature", "message": "续接"})
        self.assertEqual(not_followable.status_code, 409)
        await self.start(ws, tid)
        await self.done(ws, tid)
        await ws.close()
        await asyncio.sleep(0.03)
        followup_message = "  继续修复问题🙂\n本轮只处理已发现的问题。  "
        body = {"client_request_id": "followup-1", "message": followup_message}
        response = await self.client.post(f"/api/v1/tasks/{tid}/followup", json=body)
        self.assertEqual(response.status_code, 201)
        self.assertFalse(response.json()["node_online"])
        child = response.json()["task_id"]
        self.assertNotEqual(child, tid)
        self.assertEqual((await self.detail(tid))["status"], "done")
        again = await self.client.post(f"/api/v1/tasks/{tid}/followup", json=body)
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["task_id"], child)
        child_detail = await self.detail(child)
        self.assertEqual(child_detail["is_followup_of"], tid)
        self.assertEqual(child_detail["title"], "继续修复问题🙂")
        self.assertEqual(child_detail["prompt"], followup_message)
        parent_detail = await self.detail(tid)
        self.assertEqual(parent_detail["title"], "检查当前项目")
        self.assertEqual(parent_detail["prompt"], parent_prompt)
        cancelled = (await self.client.post(f"/api/v1/tasks/{child}/cancel", json={})).json()
        self.assertEqual(cancelled["status"], "cancelled")
        self.assertEqual((await self.detail(tid))["status"], "done")

    async def test_followup_assign_uses_original_session(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        await self.done(ws, tid)
        followup = await self.client.post(f"/api/v1/tasks/{tid}/followup", json={"client_request_id": "followup", "message": "继续"})
        await self.no_message(ws)
        await self.send(ws, {"type": "heartbeat", "running": []})
        msg = await self.recv(ws, "task_assign")
        self.assertEqual(msg["task_id"], followup.json()["task_id"])
        self.assertEqual(msg["followup_of"], {"task_id": tid, "dsh_session_id": "session-" + tid})

    async def test_reconnect_replays_without_assign_unknown_remains_blocked(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await ws.close()
        recovered, snapshot = await self.open_node(finish=False)
        self.assertEqual(snapshot["tasks"][0]["expected_status"], "assigned")
        await self.send(recovered, {"type": "task_status", "task_id": tid, "local_status": "unknown", "last_persisted_seq": 0})
        await self.send(recovered, {"type": "heartbeat", "running": []})
        await self.no_message(recovered)
        self.assertEqual((await self.detail(tid))["status"], "assigned")
        await self.send(recovered, {"type": "task_status", "task_id": tid, "local_status": "running", "last_persisted_seq": 2, "dsh_session_id": "persisted-session"})
        await self.no_message(recovered)
        for seq in (1, 2):
            await self.send(recovered, {"type": "task_event", "task_id": tid, "event_seq": seq, "event": {"type": "text", "text": str(seq)}})
            await self.recv(recovered, "event_ack")
        await self.recv(recovered, "ready")
        await self.no_message(recovered)
        self.assertEqual((await self.detail(tid))["status"], "running")

    async def test_old_connection_finally_does_not_unregister_new(self):
        old, _ = await self.open_node()
        new, _ = await self.open_node()
        await asyncio.sleep(0.03)
        self.assertTrue((await self.client.get("/api/v1/nodes")).json()["nodes"][0]["online"])
        response, _ = await self.create()
        self.assertEqual((await self.recv(new, "task_assign"))["task_id"], response.json()["task_id"])
        self.assertFalse(self.app.state.nodes.get("mac-a").ws.client is None)

    async def test_hub_restart_keeps_task_reconciles_pending_cancel(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        await ws.close()
        await asyncio.sleep(0.03)
        cancel = (await self.client.post(f"/api/v1/tasks/{tid}/cancel", json={})).json()
        self.assertEqual(cancel["cancel"], "recorded")
        self.server.should_exit = True
        await self.server_task
        self.app = create_app(str(self.config_path))
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="on", ws="websockets"))
        self.server_task = asyncio.create_task(self.server.serve())
        while not self.server.started:
            await asyncio.sleep(0.01)
        recovered, snapshot = await self.open_node(finish=False)
        self.assertTrue(snapshot["tasks"][0]["cancel_requested"])
        await self.send(recovered, {"type": "task_status", "task_id": tid, "local_status": "cancel_requested", "last_persisted_seq": 0})
        await self.recv(recovered, "ready")
        self.assertEqual((await self.recv(recovered, "task_cancel"))["task_id"], tid)
        await self.send(recovered, {"type": "heartbeat", "running": [tid]})
        await self.send(recovered, {"type": "task_done", "task_id": tid, "status": "cancelled", "exit_code": 0})
        self.assertEqual((await self.detail(tid))["status"], "cancelled")
        await self.send(recovered, {"type": "heartbeat", "running": []})
        await self.no_message(recovered)

    async def test_event_and_task_cursor_pagination_full_final(self):
        ws, _ = await self.open_node()
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        text = "汉" * 30000
        chunks = [text[:15000], text[15000:]]
        for seq, chunk in enumerate(chunks, 1):
            await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": seq, "event": {"type": "final_chunk", "text": chunk, "seq_end": seq == len(chunks)}})
            await self.recv(ws, "event_ack")
        await self.done(ws, tid, final_text=repo.bounded_text(text), final_truncated=True)
        self.assertTrue((await self.detail(tid))["result"]["truncated"])
        after, rebuilt = 0, ""
        while True:
            page = (await self.client.get(f"/api/v1/tasks/{tid}/events", params={"after": after, "limit": 1})).json()
            for event in page["events"]:
                if event["type"] == "final_chunk":
                    rebuilt += event["payload"]["text"]
            self.assertGreaterEqual(page["next_after"], after)
            after = page["next_after"]
            if not page["has_more"]:
                break
        self.assertEqual(rebuilt, text)
        await self.create()
        page1 = (await self.client.get("/api/v1/tasks", params={"limit": 1})).json()
        page2 = (await self.client.get("/api/v1/tasks", params={"limit": 1, "cursor": page1["next_cursor"]})).json()
        self.assertEqual(page2["tasks"][0]["task_id"], tid)
        self.assertNotIn("prompt", page2["tasks"][0])
        projects = (await self.client.get("/api/v1/projects")).json()
        self.assertNotIn("path", projects["projects"][0])

    async def test_task_title_is_unicode_summary_detail_preserves_original(self):
        ws, _ = await self.open_node()
        expected_title = "汉" * 119 + "🙂"
        prompt = "  \n  " + expected_title + "首行截断部分\n" + "只在详情展示的说明。" * 300 + "  \n"
        response, _ = await self.create(prompt=prompt)
        self.assertEqual(response.status_code, 201)
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign")
        listing_response = await self.client.get("/api/v1/tasks")
        listing = listing_response.json()["tasks"][0]
        self.assertEqual(listing["task_id"], tid)
        self.assertEqual(listing["title"], expected_title)
        self.assertEqual(len(listing["title"]), 120)
        self.assertNotIn("prompt", listing)
        self.assertNotIn("首行截断部分", listing_response.text)
        self.assertNotIn("只在详情展示的说明", listing_response.text)
        detail = await self.detail(tid)
        self.assertEqual(detail["title"], listing["title"])
        self.assertEqual(detail["prompt"], prompt)

    async def test_rate_queue_limit_and_busy_rejection_retries(self):
        ws, _ = await self.open_node()
        self.app.state.config["task_create_per_minute"] = 1
        first, body = await self.create()
        tid = first.json()["task_id"]
        await self.recv(ws, "task_assign")
        self.assertEqual((await self.create())[0].status_code, 429)
        self.assertEqual((await self.client.post("/api/v1/tasks", json=body)).status_code, 200)
        await self.send(ws, {"type": "task_rejected", "task_id": tid, "reason": "busy"})
        self.assertEqual((await self.detail(tid))["status"], "queued")
        await self.no_message(ws)
        await self.send(ws, {"type": "heartbeat", "running": []})
        self.assertEqual((await self.recv(ws, "task_assign"))["task_id"], tid)
        self.app.state.config["task_create_per_minute"] = 1000
        for i in range(100):
            response, _ = await self.create(request_id=f"queue-{i}")
            self.assertEqual(response.status_code, 201)
        response, _ = await self.create()
        self.assertEqual((response.status_code, response.json()["error"]["code"]), (503, "queue_full"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
