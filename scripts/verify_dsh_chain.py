"""Real Hub + Mac node integration; --real adds two paid DSH turns."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

import httpx
import uvicorn
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hub.gateway.app import create_app
from node.dsh_agent import DSHNode
from node.dsh_config import load_config
from scripts.verify_dsh_hub import config_data, APP_TOKEN, NODE_TOKEN
from scripts.verify_dsh_node import FAKE


class ChainTests(unittest.IsolatedAsyncioTestCase):
    real = False

    async def asyncSetUp(self):
        self.temp = None
        if self.real:
            self.directory = ROOT / "local_private" / "chain-real" / uuid.uuid4().hex[:12]
            self.directory.mkdir(parents=True, mode=0o700)
        else:
            self.temp = tempfile.TemporaryDirectory(prefix="xvis-chain-")
            self.directory = Path(self.temp.name).resolve()
        self.project = self.directory / "project"
        self.project.mkdir()
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            self.port = sock.getsockname()[1]
        data = config_data(self.directory, self.port)
        data.update(root_path="/xvis", node_heartbeat_timeout_sec=45)
        self.config = self.directory / "hub.yaml"
        self.config.write_text(yaml.safe_dump(data))
        await self.start_hub()
        self.client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{self.port}",
            headers={"Authorization": "Bearer " + APP_TOKEN}, trust_env=False, timeout=5)
        cfg = {"hub_ws_url": f"ws://127.0.0.1:{self.port}/ws/node", "node_token": NODE_TOKEN,
            "dsh_home": str(self.directory / "home"), "journal_path": str(self.directory / "journal.sqlite"),
            "log_dir": str(self.directory / "runs"), "default_timeout_sec": 120,
            "projects": [{"project_id": "p", "path": str(self.project), "name": "联调项目"}]}
        if self.real:
            cfg.update(node_bin=str(ROOT / "local_private/dsh-validation/node-runtime/bin/node"),
                dsh_cli=str(ROOT / "local_private/dsh-validation/runtime/node_modules/@deepseek-ai/dsh/lib/bin.js"))
            node_config = self.directory / "node.yaml"
            node_config.write_text(yaml.safe_dump(cfg))
            node_config.chmod(0o600)
            cfg = load_config(str(node_config))
            self.probe = None
        else:
            fake = self.directory / "fake.py"
            fake.write_text(FAKE.replace("time.sleep(.15)", "time.sleep(3 if prompt=='slow' else .15)"))
            cfg.update(node_bin=sys.executable, dsh_cli=str(fake),
                runtime_version={"dsh": "0.2.1-alpha.1", "node": "v24.21.0"},
                projects={"p": {"path": str(self.project), "realpath": str(self.project)}})
            self.probe = patch("node.dsh_agent.runtime_version", return_value=cfg["runtime_version"])
            self.probe.start()
        self.node = DSHNode(cfg)
        self.node_task = asyncio.create_task(self.node.start())
        await self.wait(lambda: self.node.ready)

    async def start_hub(self):
        self.app = create_app(str(self.config))
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
            root_path="/xvis", ws="websockets", log_level="error", timeout_graceful_shutdown=2))
        self.server_task = asyncio.create_task(self.server.serve())
        await self.wait(lambda: self.server.started)

    async def asyncTearDown(self):
        await self.node.close()
        self.node_task.cancel()
        await asyncio.gather(self.node_task, return_exceptions=True)
        await self.client.aclose()
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 6)
        if self.probe:
            self.probe.stop()
        if self.temp:
            self.temp.cleanup()
        else:
            print("REAL_CHAIN_EVIDENCE", self.directory)

    async def wait(self, predicate, seconds=20):
        async def poll():
            while not predicate():
                await asyncio.sleep(.02)
        await asyncio.wait_for(poll(), seconds)

    async def create(self, prompt, request_id=None):
        body = {"client_request_id": request_id or str(uuid.uuid4()), "node_id": "mac-a", "project_id": "p", "prompt": prompt}
        response = await self.client.post("/api/v1/tasks", json=body)
        self.assertIn(response.status_code, (200, 201), response.text)
        return response.json()["task_id"], body

    async def terminal(self, tid, expected="done", timeout=25):
        async def poll():
            while True:
                response = await self.client.get(f"/api/v1/tasks/{tid}")
                self.assertEqual(response.status_code, 200, response.text)
                task = response.json()
                if task["status"] in {"done", "failed", "cancelled", "timed_out", "interrupted"}:
                    return task
                await asyncio.sleep(.05)
        task = await asyncio.wait_for(poll(), timeout)
        self.assertEqual(task["status"], expected, task)
        await self.wait(lambda: not self.node.occupied())
        return task

    async def test_request_retry_followup_and_event_ack(self):
        tid, body = await self.create("success")
        retry = await self.client.post("/api/v1/tasks", json=body)
        self.assertEqual(retry.json()["task_id"], tid)
        parent = await self.terminal(tid)
        continuation = await self.client.post(f"/api/v1/tasks/{tid}/followup",
            json={"client_request_id": str(uuid.uuid4()), "message": "continue"})
        self.assertEqual(continuation.status_code, 201, continuation.text)
        followup = await self.terminal(continuation.json()["task_id"])
        self.assertEqual(followup["is_followup_of"], tid)
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{tid}")).json()["status"], parent["status"])
        await self.wait(lambda: not self.node.journal.pending(tid))
        self.assertEqual((self.directory / "home/starts").read_text(), "xx")

    async def test_running_queue_and_cancel_detached_child(self):
        tid, _ = await self.create("cancel")
        await self.wait(lambda: (self.project / "child.pid").exists() and len(self.node.runner.tracker.records) > 1)
        second, _ = await self.create("success")
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{second}")).json()["status"], "queued")
        response = await self.client.post(f"/api/v1/tasks/{tid}/cancel", json={})
        self.assertEqual(response.status_code, 200)
        await self.terminal(tid, "cancelled")
        await self.terminal(second)

    async def test_hub_restart_during_execution_keeps_single_start(self):
        tid, _ = await self.create("slow")
        await self.wait(lambda: self.node.journal.get(tid).get("dsh_session_id"))
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 6)
        await self.start_hub()
        await self.terminal(tid)
        await self.wait(lambda: self.node.ready and not self.node.journal.pending(tid))
        self.assertEqual((self.directory / "home/starts").read_text(), "x")
        second, _ = await self.create("success")
        await self.terminal(second)
        self.assertEqual((self.directory / "home/starts").read_text(), "xx")


class RealChainTests(ChainTests):
    real = True

    async def test_real_create_and_continue(self):
        self.assertTrue(os.environ.get("DEEPSEEK_API_KEY"), "Current authorized model environment required")
        tid, _ = await self.create("在当前目录创建 chain-proof.txt，只写 XVIS-FIRST。完成后回答 FIRST-OK。不要调用其他工具或访问其他目录。")
        parent = await self.terminal(tid, timeout=120)
        self.assertEqual((self.project / "chain-proof.txt").read_text().strip(), "XVIS-FIRST")
        response = await self.client.post(f"/api/v1/tasks/{tid}/followup",
            json={"client_request_id": str(uuid.uuid4()), "message": "继续同一会话。读取当前目录 chain-proof.txt，然后将内容替换为 XVIS-SECOND。完成后回答 SECOND-OK。不要访问其他目录。"})
        self.assertEqual(response.status_code, 201, response.text)
        follow_id = response.json()["task_id"]
        followup = await self.terminal(follow_id, timeout=120)
        self.assertEqual((self.project / "chain-proof.txt").read_text().strip(), "XVIS-SECOND")
        self.assertEqual(self.node.journal.get(tid)["dsh_session_id"], self.node.journal.get(follow_id)["dsh_session_id"])
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{tid}")).json()["status"], "done")
        await self.wait(lambda: not self.node.journal.pending(follow_id))
        (self.directory / "result.json").write_text(json.dumps({"first": parent, "followup": followup}, ensure_ascii=False, indent=2))


def load_tests(loader, tests, pattern):
    """Discovery is offline; real model checks require the explicit --real CLI."""
    return loader.loadTestsFromTestCase(ChainTests)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Run two real model turns in an isolated project")
    args = parser.parse_args()
    suite = unittest.TestSuite()
    if args.real:
        suite.addTest(RealChainTests("test_real_create_and_continue"))
    else:
        suite = unittest.defaultTestLoader.loadTestsFromTestCase(ChainTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    sys.exit(not result.wasSuccessful())
