"""Process-local Host input control, with durable command identities and text projection."""
import asyncio
import json
import time

from shared import control_contract as contract

LIMIT = 256 * 1024


def bounded(text, limit=LIMIT):
    return text.encode()[:limit].decode("utf-8", "ignore")


class ContinuousHost:
    def init_continuous(self):
        self.commands = {}
        self.admitted_commands = set()
        self.inbox = {"next-turn": [], "next-step": []}
        self.queue_revision = 0
        self.control_lock = asyncio.Lock()
        self.control_busy = False
        self.turn_finished = False
        self.finished_texts = []
        self.current_text = ""
        self.live_text = ""
        self.stream_buffer = ""
        self.stream_attempt = None
        self.stream_index = 0
        self.stream_last_flush = 0

    def queue_state(self):
        return {"epoch": self.epoch, "revision": self.queue_revision, "items": [
            {"item_id": m["id"], "request_id": m["source"]["rpcId"],
             "text": "".join(p.get("text", "") for p in m["content"] if p["type"] == "text"),
             "delivery": "queue" if target == "next-turn" else "steer"}
            for target, messages in self.inbox.items() for m in messages
            if m.get("source", {}).get("rpcId") in self.commands]}

    async def publish_queue(self):
        await self._event({"type": "control_state", "control": contract.state(self.queue_state())})

    def settle_turn(self):
        if self.turn_finished and not self.control_busy and not any(self.inbox.values()):
            self.ended.set()

    async def control(self, message):
        if message.get("task_id") != self.task_id:
            return
        value = message.get("command")
        try:
            contract.command(value)
        except (ValueError, KeyError, TypeError, AttributeError):
            return
        async with self.control_lock:
            cid = value["command_id"]
            if value["epoch"] != self.epoch:
                return
            if cid in self.commands:
                old = self.commands[cid]
                if old["request"] == value:
                    await self.control_receipt(cid, old["status"], old["detail"])
                return
            self.commands[cid] = {"request": value, "status": "uncertain", "detail": "引擎接收状态尚未确认，请查看执行记录"}
            self.journal.update(self.task_id, host_commands=self.commands)
            self.control_busy = True
            try:
                if self.ended.is_set() or self.cancel_event.is_set() or not self.sid or not self.cid:
                    raise ValueError("当前任务已停止或暂未连接")
                action = value["action"]
                queue = self.queue_state()
                projected_size = sum(len(i["text"].encode()) for i in queue["items"])
                if "item_id" in value:
                    item = next((i for i in queue["items"] if i["item_id"] == value["item_id"]), None)
                    if not item or value["revision"] != self.queue_revision:
                        raise ValueError("队列已变化，此操作未执行")
                    if action == "edit":
                        projected_size -= len(item["text"].encode())
                if projected_size + len(value.get("text", "").encode()) > 32768:
                    raise ValueError("待执行补充内容合计不能超过 32 KiB")
                if action in {"steer", "queue"}:
                    if len(queue["items"]) >= 20:
                        raise ValueError("待执行补充条数已达上限")
                    await self.rpc("session/prompt", {"request": {"sessionId": self.sid, "requestId": cid,
                        "mode": action, "content": [{"type": "text", "text": value["text"]}]}})
                    # The HTTP receipt may race the previous turn/end on the follow stream.
                    # Do not close Host until its durable admission has reached our projection.
                    async with asyncio.timeout(10):
                        while cid not in self.admitted_commands:
                            await asyncio.sleep(.02)
                else:
                    mutation = {"kind": "steer" if action == "promote" else action}
                    if action == "edit":
                        mutation["content"] = [{"type": "text", "text": value["text"]}]
                    await self.rpc("session/updateQueue", {"request": {"sessionId": self.sid,
                        "itemId": value["item_id"], "action": mutation}})
                await self.control_receipt(cid, "accepted", "引擎已接收")
            except ValueError:
                await self.control_receipt(cid, "rejected", "任务或队列已变化，或待执行内容超限；请刷新后操作")
            except Exception:
                # Never retry a non-idempotent queue edit after a lost RPC response.
                await self.control_receipt(cid, "uncertain", "引擎接收状态尚未确认，请查看执行记录")
            finally:
                self.control_busy = False
                self.settle_turn()

    async def control_receipt(self, cid, status, detail):
        self.commands[cid].update(status=status, detail=detail)
        self.journal.update(self.task_id, host_commands=self.commands)
        await self._event({"type": "control_result", "epoch": self.epoch,
                           "command_id": cid, "status": status, "detail": detail})

    async def continuous_record(self, event):
        kind, data = event["type"], event["data"]
        if kind == "agent/inbox/spliced":
            items = self.inbox[data["target"]]
            start, count = data["start"], data.get("removedCount", 0)
            if start > len(items) or start + count > len(items):
                raise ValueError("Host queue history gap")
            items[start:start + count] = data["inserted"]
            self.admitted_commands.update(m.get("source", {}).get("rpcId") for m in data["inserted"])
            self.queue_revision = event["seq"]
            await self.publish_queue()
        elif kind == "user/message":
            cid = data.get("source", {}).get("rpcId")
            if cid in self.commands:
                await self._event({"type": "control_consumed", "epoch": self.epoch, "command_id": cid})
        elif kind == "turn/start":
            self.turn_finished = False
            self.current_text = ""
        elif kind == "turn/end":
            self.turn_finished = True
            if self.current_text:
                self.finished_texts.append(self.current_text)
            self.settle_turn()

    def composed_text(self, tail=""):
        return "\n\n---\n\n".join([*self.finished_texts, *([tail] if tail else [])])

    async def reset_answer(self, text):
        self.stream_buffer = ""
        self.live_text = bounded(text)
        await self._event({"type": "answer_reset"})
        data = self.live_text
        while data:
            piece = bounded(data, 8192)
            await self._event({"type": "answer_delta", "text": piece})
            data = data[len(piece):]

    async def flush_answer(self):
        while self.stream_buffer:
            piece = bounded(self.stream_buffer, 8192)
            self.stream_buffer = self.stream_buffer[len(piece):]
            await self._event({"type": "answer_delta", "text": piece})
        self.stream_last_flush = time.monotonic()

    async def stream_flusher(self):
        while True:
            await asyncio.sleep(.1)
            await self.flush_answer()

    async def assistant_frame(self, frame):
        if frame["type"] == "start":
            self.stream_attempt, self.stream_index = frame["attemptId"], 0
            await self.reset_answer(self.composed_text())
            if self.live_text:
                self.stream_buffer = "\n\n---\n\n"
                self.live_text += self.stream_buffer
        elif frame["type"] == "chunk":
            if frame["attemptId"] != self.stream_attempt or frame["index"] < self.stream_index:
                return
            if frame["index"] != self.stream_index:
                raise ValueError("Host assistant stream gap")
            self.stream_index += 1
            chunk = frame["chunk"]
            if chunk.get("type") == "text-delta":
                text = bounded(chunk["text"], max(0, LIMIT - len(self.live_text.encode())))
                self.live_text += text
                self.stream_buffer += text
                if time.monotonic() - self.stream_last_flush >= .1:
                    await self.flush_answer()
        elif frame["type"] == "end":
            await self.flush_answer()
            if frame["outcome"]["kind"] == "abandoned":
                await self.reset_answer(self.composed_text())

    async def assistant_baseline(self, baseline):
        attempt = baseline.get("activeAttempt")
        if attempt:
            self.stream_attempt, self.stream_index = attempt["attemptId"], attempt["nextIndex"]
            parts = []
            for item in attempt["stream"]:
                if item["type"] == "text-chunks":
                    parts.extend(item["texts"])
                elif item["type"] == "chunk" and item["chunk"]["type"] == "text-delta":
                    parts.append(item["chunk"]["text"])
            await self.reset_answer(self.composed_text("".join(parts)))
