"""Opt-in H1-02 real Node/Hub/Host acceptance in an isolated local workspace.

--serve keeps the same fixture available for simulator interaction. No production
configuration or desktop DSH home is used. Logs and credentials remain private.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shlex
import socket
import sys
import uuid

import httpx
import uvicorn
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.verify_dsh_hub import config_data, APP_TOKEN, NODE_TOKEN
from hub.gateway.app import create_app
from node.dsh_agent import DSHNode
from node.dsh_config import load_config


async def until(fn, seconds=120):
    async with asyncio.timeout(seconds):
        while True:
            value = await fn()
            if value:
                return value
            await asyncio.sleep(.15)


async def run(args):
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    workspace = directory / "workspace"
    workspace.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", args.port))
        port = sock.getsockname()[1]
    config = directory / "hub.yaml"
    config.write_text(yaml.safe_dump({**config_data(directory, port), "default_timeout_sec": 900, "node_heartbeat_timeout_sec": 45}))
    config.chmod(0o600)
    app = create_app(str(config))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", ws_max_size=131072))
    server_task = asyncio.create_task(server.serve())
    async def ready(): return server.started
    await until(ready, 10)
    runtime = ROOT / "local_private/dsh-validation"
    node_config = directory / "node.yaml"
    node_config.write_text(yaml.safe_dump({"hub_ws_url": f"ws://127.0.0.1:{port}/ws/node", "node_token": NODE_TOKEN,
        "node_bin": str(runtime / "node-runtime/bin/node"),
        "dsh_cli": str(runtime / "runtime/node_modules/@deepseek-ai/dsh/lib/bin.js"),
        "dsh_home": str(directory / "headless-home"), "host_home": str(directory / "host-home"),
        "journal_path": str(directory / "node.sqlite"), "log_dir": str(directory / "runs"),
        "projects": [{"project_id": "p-xvis-test", "name": "中途交互验收", "path": str(workspace), "execution_mode": "host"}]}))
    node_config.chmod(0o600)
    node = DSHNode(load_config(str(node_config)))
    node_task = asyncio.create_task(node.start())
    tls_server = tls_task = None
    if args.cert:
        tls_server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=args.tls_port, log_level="error",
            lifespan="off", ssl_certfile=args.cert, ssl_keyfile=args.key))
        tls_task = asyncio.create_task(tls_server.serve())
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
        headers={"Authorization": "Bearer " + APP_TOKEN}, timeout=10) as client:
        async def node_ready(): return node.ready
        await until(node_ready, 15)
        (directory / "endpoint.json").write_text(json.dumps({"http_port": port, "tls_port": args.tls_port if args.cert else None}))
        print(json.dumps({"ready": True, "directory": str(directory), "port": port}), flush=True)
        try:
            if args.serve:
                while not server.should_exit and not (tls_server and tls_server.should_exit):
                    await asyncio.sleep(.2)
                return
            results = []
            async def detail(tid):
                return (await client.get("/api/v1/tasks/" + tid)).json()
            async def completed(tid):
                task = await detail(tid)
                return task if task["status"] in {"done", "failed", "cancelled", "timed_out", "interrupted"} else None
            async def pending(tid):
                task = await detail(tid)
                if task["status"] in {"failed", "interrupted"}:
                    raise AssertionError(task)
                return next((i for i in task.get("interactions", []) if i["status"] == "pending"), None)
            async def submit(prompt, parent=None):
                endpoint = f"/api/v1/tasks/{parent}/followup" if parent else "/api/v1/tasks"
                body = {"client_request_id": str(uuid.uuid4())}
                body.update({"message": prompt} if parent else {"node_id": "mac-a", "project_id": "p-xvis-test", "prompt": prompt})
                response = await client.post(endpoint, json=body)
                assert response.status_code == 201, response.text
                return response.json()["task_id"]
            async def respond(tid, item, value):
                body = {"revision": 1, "response_id": str(uuid.uuid4()), "value": value}
                url = f"/api/v1/tasks/{tid}/interactions/{item['interaction_id']}/respond"
                response = await client.post(url, json=body)
                assert response.status_code == 200, response.text
                duplicate = await client.post(url, json=body)
                assert duplicate.status_code == 200, duplicate.text
            for case in args.cases.split(","):
                marker = directory / (case + ".txt")
                command = "printf 'H1_APPROVED\\n' >> " + shlex.quote(str(marker))
                prompt = ("隔离验收。先调用 ask_user_question，id=format，问题为选择输出格式，选项 Markdown、CSV。等待答案后只回复 H1_QA_加所选选项。只调用这一个工具。" if case == "question" else
                    "隔离审批验收。只调用一次 bash，command 逐字为：" + command +
                    "\nsandbox_permissions 必须是 danger-full-access；justification 为仅在隔离验收目录追加一行标记。不要先试普通权限。等待结果；拒绝或取消后结束，不重试不绕过。不读写其他文件。")
                app.state.config["default_timeout_sec"] = 8 if case == "timeout" else 900
                tid = await submit(prompt)
                item = await until(lambda: pending(tid))
                if case != "question":
                    assert item["request"]["arguments"]["command"] == command
                    assert not marker.exists()
                if case == "reconnect":
                    await node.ws.close()
                    await node.runner.ws.close()
                    await asyncio.sleep(2)
                if case == "crash":
                    node.runner.proc.kill()
                elif case == "timeout":
                    pass
                elif case == "cancel":
                    response = await client.post(f"/api/v1/tasks/{tid}/cancel")
                    assert response.status_code == 200
                else:
                    value = {"answers": [{"id": q["id"], "selected": ["CSV"]} for q in item["request"]["questions"]]} if case == "question" else "rejected" if case == "reject" else "allowed-once"
                    await respond(tid, item, value)
                task = await until(lambda: completed(tid))
                expected_task = {"cancel": "cancelled", "crash": "interrupted", "timeout": "timed_out"}.get(case, "done")
                expected_item = {"cancel": "cancelled", "crash": "interrupted", "timeout": "expired"}.get(case, "confirmed")
                assert task["status"] == expected_task, task
                assert task["interactions"][0]["status"] == expected_item, task
                if case in {"cancel", "crash", "timeout"}:
                    late = await client.post(f"/api/v1/tasks/{tid}/interactions/{item['interaction_id']}/respond",
                        json={"revision": 1, "response_id": str(uuid.uuid4()), "value": "allowed-once"})
                    assert late.status_code == 409
                if case == "question":
                    assert task["result"]["final_text"] == "H1_QA_CSV", task
                    following = await submit("不调用工具，只回答刚才我选择的格式。", tid)
                    follow = await until(lambda: completed(following))
                    assert follow["status"] == "done" and "CSV" in follow["result"]["final_text"], follow
                    assert node.journal.get(following)["dsh_session_id"] == node.journal.get(tid)["dsh_session_id"]
                elif case in {"allow", "reconnect"}:
                    assert marker.read_text() == "H1_APPROVED\n"
                else:
                    assert not marker.exists()
                results.append({"case": case, "task_id": tid, "status": "passed", "task": task})
                (directory / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
                print(json.dumps({"case": case, "status": "passed"}), flush=True)
        finally:
            await node.close()
            node_task.cancel()
            await asyncio.gather(node_task, return_exceptions=True)
            server.should_exit = True
            if tls_server: tls_server.should_exit = True
            await server_task
            if tls_task: await tls_task


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--real", action="store_true", help="Explicitly allow isolated real-model execution")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--cases", default="question,allow,reject,cancel,reconnect,crash,timeout")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--cert")
    parser.add_argument("--key")
    parser.add_argument("--tls-port", type=int, default=18443)
    args = parser.parse_args()
    if not args.real:
        parser.error("需要 --real 明确启用隔离真实模型验收")
    if set(args.cases.split(",")) - {"question", "allow", "reject", "cancel", "reconnect", "crash", "timeout"}:
        parser.error("未知验收场景")
    if bool(args.cert) != bool(args.key):
        parser.error("TLS 证书和私钥必须一起指定")
    asyncio.run(run(args))
