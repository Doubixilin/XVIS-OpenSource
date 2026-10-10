"""Opt-in real Host stream reconnect and queued-input cancellation acceptance."""
import argparse
import asyncio
import json
from pathlib import Path
import socket
import sys
import uuid

import httpx
import uvicorn
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from scripts.verify_h1_chain import until
from scripts.verify_dsh_hub import config_data, APP_TOKEN, NODE_TOKEN
from hub.gateway.app import create_app
from node.dsh_agent import DSHNode
from node.dsh_config import load_config


async def run(directory):
    directory = directory.resolve(); directory.mkdir(parents=True, mode=0o700)
    workspace = directory / "workspace"; workspace.mkdir()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
    config = directory / "hub.yaml"
    hub_config = config_data(directory, port)
    hub_config["default_timeout_sec"] = 180
    config.write_text(yaml.safe_dump(hub_config)); config.chmod(0o600)
    app = create_app(str(config))
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    serving = asyncio.create_task(server.serve())
    async def ready(): return server.started
    await until(ready, 10)
    runtime = ROOT / "local_private/dsh-validation"
    path = directory / "node.yaml"
    path.write_text(yaml.safe_dump({"hub_ws_url": f"ws://127.0.0.1:{port}/ws/node", "node_token": NODE_TOKEN,
        "node_bin": str(runtime / "node-runtime/bin/node"), "dsh_cli": str(runtime / "runtime/node_modules/@deepseek-ai/dsh/lib/bin.js"),
        "dsh_home": str(directory / "headless"), "host_home": str(directory / "host"),
        "journal_path": str(directory / "node.db"), "log_dir": str(directory / "runs"),
        "projects": [{"project_id": "p-xvis-test", "name": "H2 resilience", "path": str(workspace), "execution_mode": "host"}]}))
    path.chmod(0o600)
    node = DSHNode(load_config(str(path))); running = asyncio.create_task(node.start())
    async def node_ready(): return node.ready
    results = []
    try:
        await until(node_ready, 15)
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", trust_env=False,
                                    headers={"Authorization": "Bearer " + APP_TOKEN}, timeout=15) as api:
            async def submit(prompt):
                response = await api.post("/api/v1/tasks", json={"client_request_id": str(uuid.uuid4()), "node_id": "mac-a", "project_id": "p-xvis-test", "prompt": prompt})
                assert response.status_code == 201, response.text
                return response.json()["task_id"]
            async def detail(tid): return (await api.get("/api/v1/tasks/" + tid)).json()
            async def done(tid):
                task = await detail(tid)
                return task if task["status"] in {"done", "cancelled", "interrupted", "failed", "timed_out"} else None
            tid = await submit("隔离流式验收。不调用工具，先写 RECONNECT_BEGIN_94，再写约3000字中文海边游记，分十二个有标题的段落，最后写 RECONNECT_END_94。开头结尾标记各一次。")
            async def text_started():
                return node.runner if node.runner and len(node.runner.live_text) > 30 else None
            runner = await until(text_started)
            session = runner.sid
            await runner.ws.close(); await node.ws.close()
            final = await until(lambda: done(tid), 180)
            assert final["status"] == "done", final
            text = final["result"]["final_text"]
            assert text.count("RECONNECT_BEGIN_94") == text.count("RECONNECT_END_94") == 1
            assert node.journal.get(tid)["dsh_session_id"] == session
            events = list(app.state.db.execute("SELECT type FROM v1_events WHERE task_id=?", (tid,)))
            assert sum(r[0] == "answer_reset" for r in events) >= 2
            results.append({"case": "stream_reconnect", "task_id": tid, "status": "passed", "same_session": True, "final_characters": len(text)})
            tid = await submit("隔离取消验收。先调用 ask_user_question，问题 id=hold，选项继续；等待回答，不调用其他工具。")
            async def pending():
                task = await detail(tid)
                return task if task.get("control") and any(q["status"] == "pending" for q in task.get("interactions", [])) else None
            task = await until(pending)
            body = {"command_id": str(uuid.uuid4()), "epoch": task["control"]["epoch"], "action": "queue", "text": "只回复 CANCELLED_QUEUE_MUST_NOT_RUN_94"}
            assert (await api.post(f"/api/v1/tasks/{tid}/controls", json=body)).status_code == 200
            async def queued(): return (await detail(tid))["control"]["items"]
            await until(queued)
            assert (await api.post(f"/api/v1/tasks/{tid}/cancel")).status_code == 200
            final = await until(lambda: done(tid))
            assert final["status"] == "cancelled" and not final["control"]["items"], final
            command = final["control"]["commands"][0]
            assert not command["consumed"] and command["status"] == "interrupted", command
            results.append({"case": "cancel_with_queue", "task_id": tid, "status": "passed", "queue_consumed": False})
            (directory / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(json.dumps(results, ensure_ascii=False))
    finally:
        await node.close(); running.cancel(); await asyncio.gather(running, return_exceptions=True)
        server.should_exit = True; await serving


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true"); parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not args.real: parser.error("requires --real")
    asyncio.run(run(args.directory))
