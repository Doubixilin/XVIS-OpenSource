"""Offline H2 correctness checks: cursor replay, control identity, expiry and notifications."""
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_dsh_hub import HubIntegrationTests, NODE_TOKEN
from scripts.verify_h1_interactions import request
from scripts.verify_dsh_notifications import device, device_id, FakeSender
from hub.gateway import continuous, interactions, notifications, inbox, v1_repo as repo
from shared.v1_db import init_v1_db
from node.dsh_host import HostRunner
from node.dsh_store import Journal


class ControlTests(HubIntegrationTests):
    async def setup_control(self):
        ws, _ = await self.open_node()
        self.app.state.nodes.get("mac-a").capabilities.update({"continuous_v1", "interactions_v1"})
        self.app.state.db.execute("UPDATE v1_projects SET execution_mode='host'")
        response, _ = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign"); await self.start(ws, tid)
        state = {"epoch": str(uuid.uuid4()), "revision": 0, "items": []}
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 1,
                             "event": {"type": "control_state", "control": state}})
        await self.recv(ws, "event_ack")
        return ws, tid, state

    async def test_command_durable_identity_conflict_ownership_and_cancel(self):
        ws, tid, state = await self.setup_control()
        body = {"command_id": str(uuid.uuid4()), "epoch": state["epoch"], "action": "steer", "text": "only synthetic"}
        url = f"/api/v1/tasks/{tid}/controls"
        self.assertEqual((await self.client.post(url, json=body)).status_code, 200)
        forwarded = await self.recv(ws, "task_control")
        self.assertEqual(forwarded["command"], body)
        reopened = init_v1_db(self.app.state.config["db_path"])
        self.assertEqual(reopened.execute("SELECT count(*) FROM v1_controls").fetchone()[0], 1); reopened.close()
        self.assertEqual((await self.client.post(url, json=body)).status_code, 200)
        self.assertEqual((await self.client.post(url, json={**body, "text": "changed"})).status_code, 409)
        self.assertEqual((await self.client.post(url, json=body, headers={"Authorization": "Bearer " + NODE_TOKEN})).status_code, 403)
        stale = {**body, "command_id": str(uuid.uuid4()), "epoch": str(uuid.uuid4())}
        self.assertEqual((await self.client.post(url, json=stale)).status_code, 409)
        await self.client.post(f"/api/v1/tasks/{tid}/cancel")
        self.assertEqual((await self.client.post(url, json={**body, "command_id": str(uuid.uuid4())})).status_code, 409)

    async def test_stream_cursor_replay_auth_and_node_cannot_read(self):
        ws, tid, state = await self.setup_control()
        for seq, event in [(2, {"type": "answer_reset"}), (3, {"type": "answer_delta", "text": "中文正文"})]:
            await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": seq, "event": event})
            await self.recv(ws, "event_ack")
        expected = (await self.client.get(f"/api/v1/tasks/{tid}/events")).json()
        url = f"/api/v1/tasks/{tid}/stream"
        async with self.client.stream("GET", url) as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            async for line in response.aiter_lines():
                if line:
                    self.assertEqual(json.loads(line), expected); break
        self.assertEqual((await self.client.get(url, headers={"Authorization": "Bearer " + NODE_TOKEN})).status_code, 403)
        self.assertEqual((await self.client.get(url, headers={"Authorization": "invalid"})).status_code, 401)
        self.assertEqual((await self.client.get(url + "?after=-1")).status_code, 422)


class PendingNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = init_v1_db(Path(self.temp.name) / "hub.db")
        inbox.save_device(self.conn, device_id(), device(), None)
        task, _ = repo.create_task(self.conn, str(uuid.uuid4()), "hash", "mac-a", "p-xvis-test", "private prompt", 30)
        self.conn.execute("UPDATE v1_tasks SET execution_mode='host',dsh_session_id='session-test',status='running'")
        self.conn.commit()
        self.task = repo.require_task(self.conn, task["task_id"])
        self.request = request()
    def tearDown(self): self.conn.close(); self.temp.cleanup()
    def add(self):
        with self.conn: interactions.project_event(self.conn, self.task, {"type": "interaction_request", "interaction": self.request})
    async def test_pending_push_identity_privacy_dedup_and_acceptance(self):
        self.add(); self.add()
        sender = FakeSender()
        self.assertEqual(await notifications.send_pending(self.conn, sender, interaction=True), 1)
        payload = sender.sent[0][1]
        self.assertEqual(payload["xvis"]["interaction_id"], self.request["interaction_id"])
        self.assertNotIn("result_id", payload["xvis"])
        self.assertNotIn("private prompt", json.dumps(payload))
        self.assertNotIn("选择格式", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(await notifications.send_pending(self.conn, sender, interaction=True), 0)
    async def test_answer_cancel_expiry_and_unregister_suppress(self):
        for state in ("submitted", "cancelled", "expired", "interrupted"):
            self.request = request(); self.add()
            self.conn.execute("UPDATE v1_interactions SET status=?", (state,)); self.conn.commit()
            self.assertEqual(await notifications.send_pending(self.conn, FakeSender(), interaction=True), 0)
        self.request = request(); self.add()
        inbox.remove_device(self.conn, device_id())
        self.assertEqual(await notifications.send_pending(self.conn, FakeSender(), interaction=True), 0)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM v1_interaction_notification_outbox WHERE state='pending'").fetchone()[0], 0)
    async def test_retry_checks_expiry_again_and_same_registration_isolation(self):
        self.add()
        sender = FakeSender(notifications.SendResult(503, "ServiceUnavailable"))
        await notifications.send_pending(self.conn, sender, interaction=True)
        row = self.conn.execute("SELECT * FROM v1_interaction_notification_outbox").fetchone()
        self.assertGreater(row["next_attempt_at"], 0)
        data = {**self.request, "expires_at": "2000-01-01T00:00:00Z"}
        self.conn.execute("UPDATE v1_interactions SET request_json=?", (json.dumps(data),)); self.conn.commit()
        self.assertEqual(await notifications.send_pending(self.conn, sender, interaction=True, current_time=row["next_attempt_at"]), 0)
    async def test_replacing_or_disabling_recipient_never_revives_old_push(self):
        self.add()
        inbox.save_device(self.conn, device_id(2), device(), None)
        self.assertEqual(await notifications.send_pending(self.conn, FakeSender(), interaction=True), 0)
        inbox.save_device(self.conn, device_id(2), device(enabled=False), None)
        inbox.save_device(self.conn, device_id(2), device(enabled=True), None)
        self.assertEqual(await notifications.send_pending(self.conn, FakeSender(), interaction=True), 0)


class NodeContinuousTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_filters_reasoning_and_recovers_visible_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "node.db")
            journal.put({"task_id": "t-test", "status": "running", "last_persisted_seq": 0})
            events = []
            async def emit(value): events.append(value["event"])
            runner = HostRunner({}, journal, "t-test", emit)
            await runner.assistant_frame({"type": "start", "attemptId": "attempt"})
            for index, chunk in enumerate(({"type": "reasoning-delta", "text": "secret reasoning"}, {"type": "text-delta", "text": "可见正文"})):
                await runner.assistant_frame({"type": "chunk", "attemptId": "attempt", "index": index, "chunk": chunk})
            await runner.flush_answer()
            self.assertNotIn("secret", json.dumps(events))
            self.assertEqual(events[-1]["text"], "可见正文")
            await runner.assistant_baseline({"activeAttempt": {"attemptId": "attempt", "nextIndex": 3,
                "stream": [{"type": "reasoning-chunks", "texts": ["secret"]}, {"type": "text-chunks", "texts": ["恢复", "正文"]}]}})
            self.assertEqual(runner.live_text, "恢复正文")
            journal.close()
    async def test_queue_mutation_lost_response_is_not_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "node.db")
            journal.put({"task_id": "t-test", "status": "running", "last_persisted_seq": 0})
            async def emit(value): pass
            runner = HostRunner({}, journal, "t-test", emit)
            runner.sid = "session"; runner.cid = "client"
            rid, mid = str(uuid.uuid4()), str(uuid.uuid4())
            runner.commands[rid] = {"request": {}, "status": "accepted", "detail": ""}
            runner.inbox["next-turn"] = [{"id": mid, "source": {"rpcId": rid}, "content": [{"type": "text", "text": "queued"}]}]
            count = 0
            async def rpc(*args):
                nonlocal count
                count += 1
                raise OSError("lost response")
            runner.rpc = rpc
            message = {"task_id": "t-test", "command": {"command_id": str(uuid.uuid4()), "epoch": runner.epoch,
                "action": "remove", "item_id": mid, "revision": 0}}
            await runner.control(message); await runner.control(message)
            self.assertEqual(count, 1)
            self.assertEqual(runner.commands[message["command"]["command_id"]]["status"], "uncertain")
            journal.close()


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(cls(name) for cls in (ControlTests, PendingNotificationTests, NodeContinuousTests)
                              for name in cls.__dict__ if name.startswith("test_"))


if __name__ == "__main__": unittest.main()
