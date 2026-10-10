"""Opt-in synthetic continuous-input and streaming checks against a local H1 fixture."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import time
import uuid

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_h1_chain import until
from scripts.verify_dsh_hub import APP_TOKEN


async def run(directory):
    endpoint = json.loads((directory / "endpoint.json").read_text())
    async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{endpoint['http_port']}",
                                headers={"Authorization": "Bearer " + APP_TOKEN}, trust_env=False, timeout=35) as api:
        response = await api.post("/api/v1/tasks", json={"client_request_id": str(uuid.uuid4()),
            "node_id": "mac-a", "project_id": "p-xvis-test", "prompt":
            "隔离验证。先调用 ask_user_question，问题 id=ready，选项为继续。等待后按照最新补充方向回答，不使用其他工具。"})
        assert response.status_code == 201, response.text
        tid = response.json()["task_id"]
        async def detail(): return (await api.get("/api/v1/tasks/" + tid)).json()
        async def waiting():
            value = await detail()
            assert value["status"] not in {"failed", "interrupted"}, value
            return value if value.get("control") and any(i["status"] == "pending" for i in value["interactions"]) else None
        task = await until(waiting)
        epoch = task["control"]["epoch"]
        async def command(action, text=None, item=None):
            current = await detail()
            body = {"command_id": str(uuid.uuid4()), "epoch": epoch, "action": action}
            if text: body["text"] = text
            if item: body.update(item_id=item, revision=current["control"]["revision"])
            url = f"/api/v1/tasks/{tid}/controls"
            first = await api.post(url, json=body)
            assert first.status_code == 200, first.text
            retry = await api.post(url, json=body)
            assert retry.status_code == 200 and retry.json()["command_id"] == body["command_id"]
            async def accepted():
                value = await detail()
                c = next((c for c in value["control"]["commands"] if c["command_id"] == body["command_id"]), None)
                if c and c["status"] != "pending":
                    assert c["status"] == "accepted", c
                    return c
            await until(accepted, 20)
            return body["command_id"]
        async def queue_item(cid):
            async def found():
                return next((i for i in (await detail())["control"]["items"] if i["request_id"] == cid), None)
            return await until(found, 10)
        removed = await command("queue", "不应该执行 REMOVED_89")
        removed_item = await queue_item(removed)
        await command("remove", item=removed_item["item_id"])
        queued = await command("queue", "下一轮只回复 OLD_89")
        queued_item = await queue_item(queued)
        await command("edit", text="下一轮只回复 QUEUE_NEW_89", item=queued_item["item_id"])
        promoted = await command("queue", "将此条用于当前轮：输出 STEER_89，然后写约400字中文海边散步短文。")
        promoted_item = await queue_item(promoted)
        await command("promote", item=promoted_item["item_id"])
        direct = await command("steer", "保留短文要求，不使用其他工具。")
        frames, first_text_at = [], None
        started = time.monotonic()
        async def stream():
            nonlocal first_text_at
            async with api.stream("GET", f"/api/v1/tasks/{tid}/stream") as response:
                assert response.status_code == 200 and response.headers["x-accel-buffering"] == "no"
                async for line in response.aiter_lines():
                    if not line: continue
                    page = json.loads(line)
                    frames.extend(page["events"])
                    if first_text_at is None and any(e["type"] == "answer_delta" for e in page["events"]):
                        first_text_at = time.monotonic() - started
        reader = asyncio.create_task(stream())
        item = task["interactions"][0]
        response = await api.post(f"/api/v1/tasks/{tid}/interactions/{item['interaction_id']}/respond", json={
            "revision": 1, "response_id": str(uuid.uuid4()), "value": {"answers": [{"id": q["id"], "selected": [q["options"][0]["label"]]} for q in item["request"]["questions"]]}})
        assert response.status_code == 200, response.text
        async def done():
            value = await detail()
            return value if value["status"] in {"done", "failed", "interrupted", "timed_out"} else None
        final = await until(done, 180)
        elapsed = time.monotonic() - started
        await asyncio.sleep(.5)
        reader.cancel(); await asyncio.gather(reader, return_exceptions=True)
        assert final["status"] == "done", final
        assert "STEER_89" in final["result"]["final_text"] and "QUEUE_NEW_89" in final["result"]["final_text"], final
        assert first_text_at is not None and first_text_at < elapsed
        assert not final["control"]["items"]
        consumed = {c["command_id"] for c in final["control"]["commands"] if c["consumed"]}
        assert {queued, promoted, direct} <= consumed and removed not in consumed
        stale = await api.post(f"/api/v1/tasks/{tid}/controls", json={"command_id": str(uuid.uuid4()), "epoch": epoch, "action": "steer", "text": "结束后不能唤醒"})
        assert stale.status_code == 409
        evidence = {"task_id": tid, "status": "passed", "first_text_seconds": first_text_at,
                    "completed_seconds": elapsed, "stream_events": len(frames), "command_count": len(final["control"]["commands"]),
                    "consumed_once": True, "terminal_rejected": True, "final": final}
        (directory / "continuous-results.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps({k: v for k, v in evidence.items() if k != "final"}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true")
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    if not args.real: parser.error("requires --real")
    asyncio.run(run(args.directory))
