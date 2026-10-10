#!/usr/bin/env python3
"""H1-01: opt-in real DSH Host interaction probe, outside the production Node.

Requires the existing httpx/websockets environment and DEEPSEEK_API_KEY.
Each run owns a fresh DSH_HOME, workspace, loopback port and process group.
Only the exact synthetic printf command below may receive allowed-once.
Evidence contains synthetic tool calls/results, not credentials or reasoning.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import time
import uuid

import httpx
import websockets

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "local_private/dsh-validation"
VERSION = "0.2.1-alpha.1"
CASES = ("question", "question_reconnect", "approval_allow", "approval_reconnect", "approval_reject", "approval_cancel", "stream_control")
SAFE_EVENTS = {"tool/call", "tool/result", "approval/asked", "approval/decided",
               "turn/start", "turn/end", "assistant/message", "user/message", "agent/inbox/spliced"}


def require(ok, message):
    if not ok:
        raise AssertionError(message)


def text_parts(message):
    return "".join(p.get("text", "") for p in message.get("content", []) if p.get("type") == "text")


class Client:
    def __init__(self, base, token):
        self.base, self.token = base, token
        self.http = httpx.AsyncClient(base_url=base, trust_env=False, timeout=30)
        self.ws = self.reader = None
        self.frames = []
        self.events = {}
        self.sid = None
        self.error = None

    async def rpc(self, endpoint, args, *, allow_error=False):
        r = await self.http.post("/api/" + endpoint, json={
            "type": "client-request", "rpcId": str(uuid.uuid4()),
            "method": endpoint, "payload": {"args": args},
        })
        r.raise_for_status()
        result = r.json()["result"]
        if allow_error:
            return result
        require(result["ok"], str(result))
        return result.get("value")

    async def connect(self):
        auth = await self.http.get("/", params={"token": self.token})
        require(auth.status_code in (302, 303, 307), "launch-token exchange failed")
        cookie = "; ".join(k + "=" + v for k, v in self.http.cookies.items())
        self.ws = await websockets.connect(
            self.base.replace("http", "ws") + "/api/remote.mux",
            additional_headers={"Cookie": cookie}, origin=self.base,
        )
        await self.open_stream("events", "$events", {})
        first = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
        require(first.get("value", {}).get("type") == "ready", "missing event generation")
        self.cid = first["value"]["clientId"]
        self.reader = asyncio.create_task(self.read())
        if self.sid:
            await self.follow()

    async def open_stream(self, stream, endpoint, args):
        await self.ws.send(json.dumps({"type": "open", "streamId": stream,
                                      "endpoint": endpoint, "payload": {"args": args}}))

    async def follow(self):
        await self.open_stream("follow", "session/follow", {"request": {
            "address": {"kind": "session", "sessionId": self.sid}, "assistantStream": True,
        }})

    async def read(self):
        try:
            async for raw in self.ws:
                f = json.loads(raw)
                if f["type"] == "error":
                    self.error = f["error"]
                v = f.get("value", {})
                self.frames.append(v)
                if f["streamId"] == "follow":
                    records = v.get("records", []) if v.get("type") == "snapshot" else [v]
                    for record in records:
                        if record.get("type") == "event":
                            event = record["event"]
                            self.events[event["seq"]] = event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = type(exc).__name__

    async def until(self, predicate, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            require(self.error is None, f"stream failure: {self.error}")
            result = predicate()
            if result:
                return result
            await asyncio.sleep(.05)
        raise TimeoutError("probe predicate timed out")

    async def create(self, workspace, prompt):
        result = await self.rpc("session/create", {"request": {"cwd": str(workspace)}})
        self.sid = result["sessionId"]
        await self.rpc("session/selectModel", {"request": {
            "sessionId": self.sid, "provider": "deepseek-official", "model": "deepseek-flash",
        }})
        await self.follow()
        self.request_id = str(uuid.uuid4())
        await self.rpc("session/prompt", {"request": {
            "sessionId": self.sid, "requestId": self.request_id, "mode": "queue",
            "content": [{"type": "text", "text": prompt}],
        }})

    def pending(self, event):
        return next((f for f in self.frames if f.get("type") == "waterfall"
                     and f.get("agentId") == self.sid and f.get("event") == event), None)

    async def respond(self, frame, value, **overrides):
        args = {"clientId": self.cid, "eventId": frame["eventId"],
                "outcome": {"kind": "result", "value": value}, **overrides}
        return await self.rpc("$events/result", args, allow_error=True)

    def of_type(self, kind):
        return [e for _, e in sorted(self.events.items()) if e["type"] == kind]

    async def ended(self):
        return await self.until(lambda: self.of_type("turn/end"))

    async def disconnect(self):
        if self.ws:
            await self.ws.close()
        if self.reader:
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.reader
        self.ws = self.reader = None

    async def close(self):
        if self.sid and not self.of_type("turn/end"):
            with contextlib.suppress(Exception):
                await self.rpc("session/cancel", {"request": {"sessionId": self.sid}})
        await self.disconnect()
        await self.http.aclose()

    def evidence(self):
        events = []
        for _, original in sorted(self.events.items()):
            if original["type"] not in SAFE_EVENTS:
                continue
            if original["type"] == "user/message" and original["data"].get("source", {}).get("kind") != "user":
                continue
            e = json.loads(json.dumps(original))
            if e["type"] in ("assistant/message", "tool/result"):
                message = e["data"]["message"]
                e["data"] = {"turn": e["data"].get("turn"), "step": e["data"].get("step"),
                             "text": text_parts(message), "callId": message.get("toolCallId"),
                             "isError": message.get("isError", False)}
            events.append(e)
        return {"session_id": self.sid, "events": events,
                "requests": [f for f in self.frames if f.get("type") in ("waterfall", "cancel")],
                "text_delta_count": sum(f.get("type") == "assistant-stream" and
                    f.get("frame", {}).get("chunk", {}).get("type") == "text-delta" for f in self.frames)}


async def question(client, workspace, reconnect=False):
    await client.create(workspace,
        "这是隔离协议验收。请先调用 ask_user_question，问题 id 为 format，询问测试输出格式，"
        "选项为 Markdown、CSV。必须等待工具返回用户答案，最终只回复 HOST_QA_加所选选项。"
        "不要使用其他工具，不要自行替用户回答。")
    frame = await client.until(lambda: client.pending("user-questions/request"))
    await asyncio.sleep(.5)
    require(not client.of_type("tool/result") and not client.of_type("turn/end"), "question did not wait")
    details = {"wait_observed": True}
    answer = {"answers": [{"id": q["id"], "selected": ["CSV"]} for q in frame["request"]["questions"]]}
    if reconnect:
        old_cid = client.cid
        await client.disconnect()
        await asyncio.sleep(.5)
        client.frames.clear()
        await client.connect()
        replay = await client.until(lambda: client.pending("user-questions/request"))
        require(replay["eventId"] == frame["eventId"] and client.cid != old_cid, "pending request not restored")
        stale = await client.respond(frame, answer, clientId=old_cid)
        require(not stale["ok"], "old client generation accepted")
        details.update(replayed_same_event=True, old_generation_result=stale)
    require((await client.respond(frame, answer))["ok"], "question answer failed")
    await client.ended()
    require(client.of_type("turn/end")[-1]["data"]["reason"]["kind"] == "completed", "question turn did not complete")
    require(len(client.of_type("turn/start")) == 1, "answer started another turn")
    calls = client.of_type("tool/call")
    require(len(calls) == 1 and calls[0]["data"]["name"] == "ask_user_question", "unexpected tool calls")
    results = client.of_type("tool/result")
    require(len(results) == 1 and results[0]["data"]["message"]["toolCallId"] == calls[0]["data"]["callId"], "tool identity mismatch")
    require("CSV" in text_parts(results[0]["data"]["message"]), "answer missing from original tool result")
    require(any(text_parts(e["data"]["message"]) == "HOST_QA_CSV" for e in client.of_type("assistant/message")), "model did not consume answer")
    return details


async def stream_control(client, workspace):
    """Hold at a real question boundary so every queue mutation is observable."""
    await client.create(workspace,
        "隔离协议验证。先调用 ask_user_question，问题 id=ready，选项只有继续。等待答复后，"
        "按最新补充方向回答，不使用其他工具。")
    frame = await client.until(lambda: client.pending("user-questions/request"))
    async def prompt(text, mode="queue"):
        rid = str(uuid.uuid4())
        req = {"sessionId": client.sid, "requestId": rid, "mode": mode,
               "content": [{"type": "text", "text": text}]}
        await client.rpc("session/prompt", {"request": req})
        await client.rpc("session/prompt", {"request": req})  # Same identity must not append twice.
        def found():
            return next((m for e in client.of_type("agent/inbox/spliced")
                for m in e["data"].get("inserted", []) if m.get("source", {}).get("rpcId") == rid), None)
        return rid, await client.until(found)
    removed_id, removed = await prompt("此条应被移除，不得执行 REMOVE_73。")
    queued_id, queued = await prompt("下一轮仅回复 QUEUE_OLD。")
    steered_id, steered = await prompt("此条将提前执行。")
    async def update(item, action):
        return await client.rpc("session/updateQueue", {"request": {
            "sessionId": client.sid, "itemId": item["id"], "action": action}}, allow_error=True)
    require((await update(removed, {"kind": "remove"}))["ok"], "queue removal failed")
    require((await update(queued, {"kind": "edit", "content": [{"type": "text", "text": "下一轮仅回复 QUEUE_NEW_73。"}]}))["ok"], "queue edit failed")
    require((await update(steered, {"kind": "edit", "content": [{"type": "text", "text": "当前轮输出 STEER_73，然后用中文写约400字的海边散步短文。"}]}))["ok"], "steer edit failed")
    require((await update(steered, {"kind": "steer"}))["ok"], "queue promotion failed")
    direct_id, _ = await prompt("保留上一条当前轮短文要求；不要调用其他工具。", "steer")
    answer = {"answers": [{"id": q["id"], "selected": [q["options"][0]]} for q in frame["request"]["questions"]]}
    require((await client.respond(frame, answer))["ok"], "held question did not resume")
    await client.until(lambda: len(client.of_type("turn/end")) >= 2, 180)
    used = [e["data"].get("source", {}).get("rpcId") for e in client.of_type("user/message")]
    require(removed_id not in used, "removed prompt was consumed")
    for rid in (queued_id, steered_id, direct_id):
        require(used.count(rid) == 1, "prompt was not consumed exactly once")
    texts = [text_parts(e["data"]["message"]) for e in client.of_type("assistant/message")]
    require("QUEUE_NEW_73" in texts[-1] and "QUEUE_OLD" not in texts[-1], "queued replacement not honored")
    deltas = [f["frame"] for f in client.frames if f.get("type") == "assistant-stream"
              and f["frame"].get("chunk", {}).get("type") == "text-delta"]
    require(len(deltas) > 2, "no real text deltas")
    stale = await update(queued, {"kind": "remove"})
    require(not stale["ok"], "consumed queue item was still mutable")
    return {"consumed_once": True, "removed_not_consumed": True, "edited_queue_consumed": True,
            "text_delta_count": len(deltas), "delta_sample": deltas[0], "stale_mutation_rejected": True,
            "turns": len(client.of_type("turn/end"))}


async def approval(client, workspace, marker, action):
    command = "printf 'HOST_APPROVED\\n' >> " + shlex.quote(str(marker))
    await client.create(workspace,
        "这是隔离沙箱审批验收，仅调用一次 bash，command 必须逐字为：" + command +
        "\nsandbox_permissions 必须为 danger-full-access，justification 为“仅验证一次批准，在隔离验收目录追加一行标记”。"
        "不要先试普通权限。必须等待工具结果；若拒绝或取消，解释后结束，不得重试或绕过。"
        "不要读写其他文件，不使用其他工具。")
    frame = await client.until(lambda: client.pending("approval/request"))
    call = await client.until(lambda: next((e for e in client.of_type("tool/call")
        if e["data"]["callId"] == frame["request"].get("callId")), None))
    args = json.loads(call["data"]["arguments"])
    require(frame["request"]["toolName"] == call["data"]["name"] == "bash", "unexpected approval tool")
    require(args["command"] == command and args.get("sandbox_permissions") == "danger-full-access", "approval differs from exact probe command")
    require(not args.get("run_in_background"), "background command not permitted")
    require(not marker.exists(), "operation ran before approval")
    await asyncio.sleep(.5)
    require(not marker.exists() and not client.of_type("tool/result"), "approval did not wait")
    details = {"no_effect_before_answer": True, "command_verified": True}
    if action == "reconnect":
        old_cid = client.cid
        await client.disconnect()
        await asyncio.sleep(.5)
        client.frames.clear()
        await client.connect()
        replay = await client.until(lambda: client.pending("approval/request"))
        require(replay["eventId"] == frame["eventId"] and replay["request"]["callId"] == frame["request"]["callId"], "approval identity changed on reconnect")
        stale = await client.respond(frame, "allowed-once", clientId=old_cid)
        require(not stale["ok"] and not marker.exists(), "stale approval generation changed operation")
        details.update(replayed_same_event=True, old_generation_result=stale)
        action = "allow"
    if action == "cancel":
        await client.rpc("session/cancel", {"request": {"sessionId": client.sid}})
        await client.ended()
        details["late_answer_result"] = await client.respond(frame, "allowed-once")
    else:
        if action == "allow":
            details["unknown_event_result"] = await client.respond(frame, "allowed-once", eventId=str(uuid.uuid4()))
            await asyncio.sleep(.2)
            require(not marker.exists(), "wrong event approved real operation")
        require((await client.respond(frame, "allowed-once" if action == "allow" else "rejected"))["ok"], "approval response failed")
        await client.ended()
        details["duplicate_result"] = await client.respond(frame, "allowed-once")
    await asyncio.sleep(.3)
    require(len(client.of_type("turn/start")) == 1 and len(client.of_type("tool/call")) == 1, "operation retried or new turn started")
    audits = client.of_type("approval/decided")
    asked = client.of_type("approval/asked")
    expected = {"allow": "allowed-once", "reject": "rejected", "cancel": "cancelled"}[action]
    require(len(audits) == 1 and audits[0]["data"]["outcome"] == expected, "wrong approval audit")
    require(len(asked) == 1 and asked[0]["data"]["id"] == audits[0]["data"]["id"], "approval audit identity mismatch")
    require(asked[0]["data"]["callId"] == call["data"]["callId"], "approval audit refers to another tool")
    results = client.of_type("tool/result")
    require(len(results) == 1 and results[0]["data"]["message"]["toolCallId"] == call["data"]["callId"], "approval resumed another tool")
    require(bool(results[0]["data"]["message"].get("isError")) == (action != "allow"), "unexpected tool result status")
    require(client.of_type("turn/end")[-1]["data"]["reason"]["kind"] == ("aborted" if action == "cancel" else "completed"), "unexpected final turn state")
    if action == "allow":
        require(marker.read_bytes() == b"HOST_APPROVED\n", "allowed operation did not execute exactly once")
    else:
        require(not marker.exists(), "denied/cancelled operation executed")
    details.update(outcome=expected, marker_exists=marker.exists())
    return details


async def run(args):
    require(os.environ.get("DEEPSEEK_API_KEY"), "DEEPSEEK_API_KEY is required; never logged")
    dsh = Path(args.dsh).resolve()
    require(json.loads((dsh.parent.parent / "package.json").read_text())["version"] == VERSION, "wrong DSH version")
    node = str(Path(args.node).resolve())
    version_check = await asyncio.create_subprocess_exec(node, "--version", stdout=asyncio.subprocess.PIPE)
    version_bytes, _ = await asyncio.wait_for(version_check.communicate(), 10)
    node_version = version_bytes.decode().strip()
    require(version_check.returncode == 0 and node_version == "v24.21.0", "wrong Node version")
    directory = Path(args.output).resolve() / (time.strftime("run-%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6])
    directory.mkdir(parents=True, mode=0o700)
    workspace, home = directory / "workspace", directory / "home"
    workspace.mkdir(); home.mkdir(mode=0o700)
    env = {**os.environ, "DSH_HOME": str(home), "DSH_PERMISSION_MODE": "workspace-write"}
    summary = {"dsh_version": VERSION, "node_version": node_version,
               "scope": "real model + loopback Host; no Hub or iPhone", "cases": []}
    proc = None
    port = None
    log_path = directory / "host.log"
    try:
        with log_path.open("w") as log:
            log_path.chmod(0o600)
            proc = await asyncio.create_subprocess_exec(node, str(dsh), "web",
                "--host", "127.0.0.1", "--port", "0", "--no-open", cwd=workspace, env=env,
                stdout=log, stderr=log, start_new_session=True)
            for _ in range(300):
                match = re.search(r"(http://127\.0\.0\.1:(\d+))/\?token=(\S+)", log_path.read_text())
                if match:
                    base, port, token = match.groups(); port = int(port)
                    break
                require(proc.returncode is None, "Host exited before ready; inspect private host.log")
                await asyncio.sleep(.1)
            else:
                raise TimeoutError("Host startup timeout")
            print("Host started on loopback; credentials withheld", flush=True)
            for name in (args.case or CASES):
                client = Client(base, token)
                result = {"name": name}
                try:
                    await client.connect()
                    details = await (stream_control(client, workspace) if name == "stream_control" else question(client, workspace, name.endswith("reconnect")) if name.startswith("question")
                        else approval(client, workspace, directory / (name + ".txt"), name.split("_")[1]))
                    result.update(status="passed", details=details)
                except Exception as exc:
                    result.update(status="failed", error=type(exc).__name__ + ": " + str(exc))
                finally:
                    await client.close()
                    result.update(client.evidence())
                    summary["cases"].append(result)
                    (directory / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
                print(name, result["status"], result.get("error", ""), flush=True)
    finally:
        if proc and proc.returncode is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), 10)
            except asyncio.TimeoutError:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
        if port:
            with socket.socket() as sock:
                summary["listener_closed"] = sock.connect_ex(("127.0.0.1", port)) != 0
        summary["host_exit_code"] = proc.returncode if proc else None
        (directory / "results.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print("Evidence:", directory / "results.json", flush=True)
    return 0 if len(summary["cases"]) == len(args.case or CASES) and all(c["status"] == "passed" for c in summary["cases"]) and summary.get("listener_closed") else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="explicitly allow real model requests and isolated Host startup")
    parser.add_argument("--node", default=str(RUNTIME / "node-runtime/bin/node"))
    parser.add_argument("--dsh", default=str(RUNTIME / "runtime/node_modules/@deepseek-ai/dsh/lib/bin.js"))
    parser.add_argument("--output", default=str(ROOT / "local_private/host-probe-20261010"))
    parser.add_argument("--case", choices=CASES, action="append", help="run only this case; repeatable")
    args = parser.parse_args()
    if not args.real:
        parser.error("this is a real-model probe; pass --real explicitly")
    raise SystemExit(asyncio.run(run(args)))
