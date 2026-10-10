"""One loopback Host per task, pinned sessions, durable interaction projection.

Host credentials and RPC stay local. A Node restart never replays an approval.
Only durable journal records confirm answers; an RPC acknowledgement does not.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import websockets

from node.dsh_runner import DSHRunner, RotatingBinary, timestamp, wire_summary
from node.dsh_images import download_inputs, image_prompt, collect_outputs
from node.dsh_profile import assistant_prompt
from node.dsh_project_context import project_prompt
from node import dsh_artifacts
from shared import interaction_contract as contract
from node.dsh_continuous import ContinuousHost


def text_parts(message):
    return "".join(p.get("text", "") for p in message.get("content", []) if p.get("type") == "text")


class HostRunner(ContinuousHost, DSHRunner):
    def __init__(self, *args):
        super().__init__(*args)
        self.epoch = str(uuid.uuid4())
        self.sid = self.cid = self.base = self.token = None
        self.http = self.ws = None
        self.calls, self.completed_calls, self.audit_calls = {}, set(), {}
        self.waterfalls, self.items, self.live_events = {}, {}, set()
        self.last_seq = None
        self.ended = asyncio.Event()
        self.reply_lock = asyncio.Lock()
        self.expires_at = None
        self.monitors = []
        self.init_continuous()

    async def rpc(self, endpoint, args):
        response = await self.http.post("/api/" + endpoint, json={"type": "client-request",
            "rpcId": str(uuid.uuid4()), "method": endpoint, "payload": {"args": args}})
        response.raise_for_status()
        result = response.json()["result"]
        if result.get("ok") is not True:
            raise ValueError("Host RPC rejected")  # Never copy credential-bearing raw responses.
        return result.get("value")

    async def stream(self, stream_id, endpoint, args):
        await self.ws.send(json.dumps({"type": "open", "streamId": stream_id,
            "endpoint": endpoint, "payload": {"args": args}}))

    async def connect(self):
        response = await self.http.get("/", params={"token": self.token})
        if response.status_code not in {302, 303, 307}:
            raise ValueError("Host authentication failed")
        cookie = "; ".join(k + "=" + v for k, v in self.http.cookies.items())
        self.ws = await websockets.connect(self.base.replace("http", "ws") + "/api/remote.mux",
            additional_headers={"Cookie": cookie}, origin=self.base, max_size=16 * 1024 * 1024)
        self.cid = None
        self.live_events.clear()
        await self.stream("events", "$events", {})
        frame = json.loads(await asyncio.wait_for(self.ws.recv(), 10))
        if frame.get("value", {}).get("type") != "ready":
            raise ValueError("Host event stream not ready")
        self.cid = frame["value"]["clientId"]

    async def follow(self):
        await self.stream("follow", "session/follow", {"request": {
            "address": {"kind": "session", "sessionId": self.sid}, "maxMessages": 100, "assistantStream": True}})

    def save_items(self):
        self.journal.update(self.task_id, host_interactions=self.items)

    async def update_item(self, item, status, outcome=None):
        if item["status"] in contract.CLOSED:
            return
        if status != "confirmed" and contract.expired(item["expires_at"]):
            status = "expired"
        item.update(status=status, outcome=outcome)
        self.save_items()
        await self._event({"type": "interaction_update", "interaction": {
            **{k: item[k] for k in ("interaction_id", "revision", "epoch", "session_id", "call_id")},
            "status": status, "outcome": outcome, "response_id": item.get("response_id")}})

    async def correlate(self):
        for event_id, frame in list(self.waterfalls.items()):
            if any(i["event_id"] == event_id for i in self.items.values()):
                continue
            req = frame["request"]
            kind = "approval" if frame["event"] == "approval/request" else "question"
            if kind == "approval":
                call = self.calls.get(req.get("callId"))
                if call is None:
                    continue
                if call["name"] != req["toolName"]:
                    raise ValueError("Host approval call mismatch")
                detail = {"tool_name": call["name"], "reason": req.get("displayReason", {}).get("zh") or req["reason"],
                          "arguments": json.loads(call["arguments"])}
            else:
                candidates = []
                for c in self.calls.values():
                    if c["name"] != "ask_user_question" or c["callId"] in self.completed_calls:
                        continue
                    questions = json.loads(c["arguments"]).get("questions", [])
                    normalized = [{"id": q["id"], "question": q["question"], "options": q.get("options", []),
                                   "multiSelect": q.get("multi_select", False)} for q in questions]
                    incoming = [{"id": q["id"], "question": q["question"], "options": q.get("options", []),
                                 "multiSelect": q.get("multiSelect", False)} for q in req["questions"]]
                    if normalized == incoming:
                        candidates.append(c)
                if len(candidates) != 1:
                    continue
                call, detail = candidates[0], req
            data = {"interaction_id": "q-" + uuid.uuid4().hex, "revision": 1, "kind": kind,
                    "epoch": self.epoch, "session_id": self.sid, "call_id": call["callId"],
                    "expires_at": self.expires_at, "request": detail}
            contract.request(data)
            item = {**data, "event_id": event_id, "status": "pending"}
            self.items[data["interaction_id"]] = item
            self.save_items()
            await self._event({"type": "interaction_request", "interaction": data})

    async def respond(self, message):
        async with self.reply_lock:
            item = self.items.get(message.get("interaction_id"))
            if not item or message.get("task_id") != self.task_id:
                return
            if any(message.get(k) != item[k] for k in ("epoch", "session_id", "call_id", "revision")):
                return
            if item["status"] in contract.CLOSED:
                return
            if self.cancel_event.is_set() or contract.expired(item["expires_at"]) or self.ended.is_set():
                await self.update_item(item, "expired" if contract.expired(item["expires_at"]) else "cancelled")
                return
            try:
                uuid.UUID(message["response_id"])
                value = contract.answer(item, message["value"])
            except (ValueError, TypeError, KeyError, AttributeError):
                return
            if item.get("response_id") and (item["response_id"] != message["response_id"] or item["value"] != value):
                return
            item.update(status="submitted", response_id=message["response_id"], value=value)
            self.save_items()  # Durable before the potentially effective call.
            await self.deliver(item)

    async def deliver(self, item):
        if item["event_id"] not in self.live_events or not self.cid or self.cancel_event.is_set() or contract.expired(item["expires_at"]):
            return
        if item.get("sent_client_id") == self.cid:
            return
        try:
            await self.rpc("$events/result", {"clientId": self.cid, "eventId": item["event_id"],
                "outcome": {"kind": "result", "value": item["value"]}})
            item["sent_client_id"] = self.cid
            self.save_items()
        except (httpx.HTTPError, ValueError, KeyError):
            pass  # Reconciliation retries only within this live Host epoch.

    async def record(self, event):
        if event["seq"] <= self.last_seq:
            return
        self.last_seq = event["seq"]
        await self.continuous_record(event)
        kind, data = event["type"], event["data"]
        if kind == "tool/call":
            self.calls[data["callId"]] = data
            await self._ingest(json.dumps({"type": "tool_call", "callId": data["callId"],
                "tool": data["name"], "input": json.loads(data["arguments"])}).encode())
        elif kind == "approval/asked":
            self.audit_calls[data["id"]] = data["callId"]
        elif kind == "approval/decided":
            for item in self.items.values():
                if item["kind"] == "approval" and item["call_id"] == self.audit_calls.get(data["id"]):
                    if item.get("value") == data["outcome"] and item.get("response_id"):
                        await self.update_item(item, "confirmed", data["outcome"])
                    else:
                        await self.update_item(item, "cancelled")
        elif kind == "tool/result":
            message = data["message"]
            cid = message["toolCallId"]
            self.completed_calls.add(cid)
            text = text_parts(message)
            for item in self.items.values():
                if item["kind"] == "question" and item["call_id"] == cid:
                    try:
                        matched = not message.get("isError") and json.loads(text) == item.get("value") and item.get("response_id")
                    except ValueError:
                        matched = False
                    await self.update_item(item, "confirmed" if matched else "interrupted", "answered" if matched else None)
            await self._ingest(json.dumps({"type": "tool_result", "callId": cid,
                "tool": self.calls.get(cid, {}).get("name", ""), "result": text,
                "status": "error" if message.get("isError") else "success"}).encode())
        elif kind == "assistant/message":
            text = text_parts(data["message"])
            if text:
                self.current_text = text
                self.final = {"text": self.composed_text(text)}
                await self.reset_answer(self.final["text"])
                await self._ingest(json.dumps({"type": "text", "text": text}).encode())
        elif kind in {"turn/start", "turn/end"}:
            await self._ingest(json.dumps({"type": "status", "phase": kind.replace("/", "_"), **data}).encode())
        await self.correlate()

    async def frame(self, frame):
        if frame.get("type") == "error":
            raise ValueError("Host stream rejected")
        value = frame.get("value", {})
        kind = value.get("type")
        if kind == "waterfall" and value.get("agentId") == self.sid:
            if value.get("event") not in {"approval/request", "user-questions/request"}:
                raise ValueError("Unsupported Host interaction")
            self.waterfalls[value["eventId"]] = value
            self.live_events.add(value["eventId"])
            await self.correlate()
            for item in self.items.values():
                if item["status"] == "submitted" and item["event_id"] == value["eventId"]:
                    await self.deliver(item)
        elif frame.get("streamId") == "follow":
            if kind == "snapshot":
                task = self.journal.get(self.task_id)
                if value["header"]["id"] != self.sid or value["header"]["cwd"] != task["cwd"]:
                    raise ValueError("Host session scope mismatch")
                if self.last_seq is None:
                    self.last_seq = value["cursor"]  # Ignore previous rounds before prompt admission.
                    return
                records, more = value["records"], value["hasMore"]
                while more:
                    seqs = [r["event"]["seq"] for r in records if r.get("type") == "event"]
                    if not seqs or min(seqs) <= self.last_seq:
                        break
                    page = await self.rpc("session/page", {"request": {"address": {"kind": "session", "sessionId": self.sid},
                        "throughSeq": value["cursor"], "beforeSeq": min(seqs), "maxMessages": 100}})
                    if not page["records"]:
                        raise ValueError("Host history gap")
                    records = page["records"] + records
                    more = page["hasMore"]
                for record in records:
                    if record.get("type") == "event":
                        await self.record(record["event"])
                await self.assistant_baseline(value.get("assistantStream", {}))
            elif kind == "event":
                await self.record(value["event"])
            elif kind == "assistant-stream":
                await self.assistant_frame(value["frame"])

    async def conversation(self, prompt):
        await self.connect()
        task = self.journal.get(self.task_id)
        request = {"cwd": task["cwd"]}
        if task.get("followup_of"):
            request["sessionId"] = task["followup_of"]["dsh_session_id"]
        result = await self.rpc("session/create", {"request": request})
        self.sid = result["sessionId"]
        if request.get("sessionId", self.sid) != self.sid:
            raise ValueError("Host continuation mismatch")
        await self.rpc("session/selectModel", {"request": {"sessionId": self.sid,
            "provider": "deepseek-official", "model": "deepseek-flash"}})
        await self.follow()
        while self.last_seq is None:
            await self.frame(json.loads(await asyncio.wait_for(self.ws.recv(), 10)))
        await self._ingest(json.dumps({"type": "session", "sessionId": self.sid, "cwd": task["cwd"]}).encode())
        await self.publish_queue()
        self.monitors.append(asyncio.create_task(self.stream_flusher()))
        await self.rpc("session/prompt", {"request": {"sessionId": self.sid, "requestId": self.task_id,
            "mode": "queue", "content": [{"type": "text", "text": prompt}]}})
        while not self.ended.is_set():
            try:
                await self.frame(json.loads(await self.ws.recv()))
            except (websockets.ConnectionClosed, OSError):
                self.cid = None
                while self.proc.returncode is None:
                    await asyncio.sleep(1)
                    try:
                        await self.connect()
                        await self.follow()
                        break
                    except (websockets.WebSocketException, OSError, httpx.HTTPError):
                        continue
                else:
                    raise ValueError("Host process lost")

    async def execute(self, directory):
        task = self.journal.get(self.task_id)
        paths = await asyncio.wait_for(download_inputs(self.cfg, task), min(60, task["timeout_sec"])) if task.get("images") else []
        prompt = image_prompt(task["prompt"], paths)
        if task.get("file_transfer_version") == 1:
            prompt = dsh_artifacts.file_prompt(prompt)
        prompt = assistant_prompt(prompt, task.get("assistant_context"))
        prompt = project_prompt(prompt, task.get("project_context"), project_id=task["project_id"])
        env = dict(os.environ, DSH_HOME=task["dsh_home"], DSH_PERMISSION_MODE="workspace-write")
        env["PATH"] = str(Path(self.cfg["node_bin"]).parent) + os.pathsep + env.get("PATH", "")
        Path(task["dsh_home"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        self.proc = await asyncio.create_subprocess_exec(self.cfg["node_bin"], self.cfg["dsh_cli"],
            "web", "--host", "127.0.0.1", "--port", "0", "--no-open", cwd=task["cwd"], env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
        self.tracker.observe(self.proc.pid)
        self.journal.update(self.task_id, status="running", pid=self.proc.pid, pgid=self.proc.pid,
            processes=list(self.tracker.records.values()), started_at=timestamp(), host_epoch=self.epoch)
        with RotatingBinary(directory / "host-private.log") as log:
            async def drain():
                async for line in self.proc.stdout:
                    log.write(line)
                    if self.base is None:
                        match = re.search(rb"(http://127\.0\.0\.1:\d+)/\?token=([^\s\x1b]+)", line)
                        if match:
                            self.base, self.token = (s.decode() for s in match.groups())
            reader = asyncio.create_task(drain())
            observer = asyncio.create_task(self._observe())
            self.monitors = [reader, observer]
            try:
                async with asyncio.timeout(30):
                    while self.base is None:
                        if reader.done():
                            raise ValueError("Host failed to start")
                        await asyncio.sleep(.05)
                async with httpx.AsyncClient(base_url=self.base, trust_env=False, timeout=10) as http:
                    self.http = http
                    await self.conversation(prompt)
            finally:
                self.cid = None
                self._monitor_stop.set()
                observer.cancel()
                reader.cancel()
                await asyncio.gather(observer, reader, return_exceptions=True)

    async def run(self):
        task = self.journal.get(self.task_id)
        if self.cancel_event.is_set() or task.get("cancel_requested"):
            self.journal.update(self.task_id, status="cancelled", stop_confirmed=True,
                not_started=True, exit_code=None, detail="任务在启动前已取消", finished_at=timestamp())
            return
        directory = Path(self.cfg["log_dir"]) / self.task_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        final_path = directory / "final.txt"
        final_path.touch(mode=0o600, exist_ok=True)
        self.journal.update(self.task_id, final_path=str(final_path))
        self.expires_at = (datetime.now(timezone.utc) + timedelta(seconds=task["timeout_sec"])).isoformat()
        work = asyncio.create_task(self.execute(directory))
        cancel = asyncio.create_task(self.cancel_event.wait())
        state, detail = "interrupted", "Host 执行中断，未重跑；请检查本机记录"
        try:
            done, _ = await asyncio.wait({work, cancel}, timeout=task["timeout_sec"], return_when=asyncio.FIRST_COMPLETED)
            if cancel in done or work not in done:
                state = "cancelled" if cancel in done else "timed_out"
                detail = "任务已取消" if state == "cancelled" else "任务超时，待处理项已失效"
                if self.sid and self.http:
                    with contextlib.suppress(Exception):
                        await self.rpc("session/cancel", {"request": {"sessionId": self.sid}})
                work.cancel()
            else:
                await work
                if self.reason and self.reason.get("kind") == "completed" and self.final is not None:
                    state, detail = "done", ""
                else:
                    state, detail = "failed", "Host 未返回完整的最终回答"
        except Exception as exc:
            self.journal.update(self.task_id, host_error_type=type(exc).__name__)
        finally:
            cancel.cancel()
            if not work.done() and not work.cancelling():
                work.cancel()
            await asyncio.gather(work, cancel, return_exceptions=True)
            for monitor in self.monitors:
                monitor.cancel()
            await asyncio.gather(*self.monitors, return_exceptions=True)
            if self.ws:
                with contextlib.suppress(Exception):
                    await self.ws.close()
            # The supervisor owns shutdown, outside the cancellable engine coroutine.
            # Cancelling while Host is naturally closing must not cancel process cleanup.
            if self.proc is not None:
                for attempt in range(3):
                    remaining = await self.tracker.stop(self.proc.pid, self._persist_processes)
                    if remaining == 0:
                        break
                    await asyncio.sleep(.25)
                self.journal.update(self.task_id, host_stop_remaining=remaining)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self.proc.wait(), 5)
            stopped = self.proc is None or (self.proc.returncode is not None and self.tracker.survivors() == 0)
            if not stopped:
                state, detail = "interrupted", "Host 停止尚未确认，请在本机核验进程"
            for item in self.items.values():
                await self.update_item(item, "expired" if state == "timed_out" else "cancelled" if state == "cancelled" else "interrupted")
            if state == "done":
                await self._ingest(json.dumps({"type": "final", **self.final}).encode())
                if task.get("file_transfer_version") == 1:
                    dsh_artifacts.collect_outputs(self.cfg, self.journal, self.task_id, self.final["text"])
                collect_outputs(self.cfg, self.journal, self.task_id, self.final["text"])
            self.journal.update(self.task_id, status=state, detail=detail, stop_confirmed=stopped,
                exit_code=self.proc.returncode if self.proc else None, not_started=self.proc is None,
                turn_end_reason=self.reason, final_text=wire_summary(self.final["text"]) if state == "done" else "",
                final_truncated=bool(state == "done" and wire_summary(self.final["text"]) != self.final["text"]), finished_at=timestamp())
