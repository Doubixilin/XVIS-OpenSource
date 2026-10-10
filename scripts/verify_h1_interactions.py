"""Offline safety/recovery contract checks for H1-02; no production/model use."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_dsh_hub import HubIntegrationTests, NODE_TOKEN
from hub.gateway import interactions, v1_repo as repo
from shared import interaction_contract as contract
from shared.v1_db import init_v1_db
from node.dsh_host import HostRunner
from node.dsh_store import Journal


def request(kind="question", **fields):
    return {"interaction_id": "q-" + uuid.uuid4().hex, "revision": 1, "kind": kind,
        "session_id": "session-test", "epoch": str(uuid.uuid4()), "call_id": "call-test",
        "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        "request": {"questions": [{"id": "format", "question": "选择格式", "options": [{"label": "CSV"}, {"label": "MD"}]}]} if kind == "question" else
            {"tool_name": "bash", "reason": "测试", "arguments": {"command": "printf test"}}, **fields}


ANSWER = {"answers": [{"id": "format", "selected": ["CSV"]}]}


class InteractionTests(HubIntegrationTests):
    async def setup_request(self, kind="question", expired=False):
        ws, _ = await self.open_node()
        state = self.app.state.nodes.get("mac-a")
        state.capabilities.add("interactions_v1")
        self.app.state.db.execute("UPDATE v1_projects SET execution_mode='host'")
        response, _ = await self.create()
        tid = response.json()["task_id"]
        assign = await self.recv(ws, "task_assign")
        self.assertEqual(assign["execution_mode"], "host")
        await self.start(ws, tid)
        data = request(kind, session_id="session-" + tid)
        if expired: data["expires_at"] = "2000-01-01T00:00:00+00:00"
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 1,
            "event": {"type": "interaction_request", "interaction": data}})
        await self.recv(ws, "event_ack")
        return ws, tid, data, f"/api/v1/tasks/{tid}/interactions/{data['interaction_id']}/respond"

    def body(self, value=ANSWER):
        return {"revision": 1, "response_id": str(uuid.uuid4()), "value": value}

    async def test_durable_idempotence_conflict_and_engine_confirmation(self):
        ws, tid, data, url = await self.setup_request()
        body = self.body()
        first = await self.client.post(url, json=body)
        self.assertEqual(first.json()["status"], "submitted")
        sent = await self.recv(ws, "interaction_response")
        self.assertEqual(sent["call_id"], data["call_id"])
        self.assertEqual((await self.client.post(url, json=body)).status_code, 200)
        await self.recv(ws, "interaction_response")
        self.assertEqual((await self.client.post(url, json=self.body())).status_code, 409)
        # App timeout/reopen sees the original durable answer, before engine confirmation.
        second = init_v1_db(self.app.state.config["db_path"])
        self.assertEqual(interactions.for_task(second, tid)[0]["response_id"], body["response_id"])
        second.close()
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 2,
            "event": {"type": "interaction_update", "interaction": {**data, "status": "confirmed",
                "response_id": body["response_id"], "outcome": "answered"}}})
        await self.recv(ws, "event_ack")
        self.assertEqual((await self.detail(tid))["interactions"][0]["status"], "confirmed")
        self.assertEqual((await self.client.get("/api/v1/interactions")).json()["items"], [])

    async def test_cancel_closes_pending_and_late_replay_does_not_revive(self):
        ws, tid, data, url = await self.setup_request("approval")
        await self.client.post(f"/api/v1/tasks/{tid}/cancel")
        await self.recv(ws, "task_cancel")
        self.assertEqual((await self.client.post(url, json=self.body("allowed-once"))).status_code, 409)
        # Fresh transport sequence, same request identity: remains closed.
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 2,
            "event": {"type": "interaction_request", "interaction": data}})
        await self.recv(ws, "event_ack")
        self.assertEqual((await self.detail(tid))["interactions"][0]["status"], "cancelled")

    async def test_expiry_auth_cross_task_and_invalid_answers(self):
        ws, tid, data, url = await self.setup_request()
        self.assertEqual((await self.client.post(url, json=self.body(), headers={"Authorization": "Bearer " + NODE_TOKEN})).status_code, 403)
        self.assertEqual((await self.client.get("/api/v1/interactions", headers={"Authorization": "bad"})).status_code, 401)
        other, _ = await self.create()
        wrong = url.replace(tid, other.json()["task_id"])
        self.assertEqual((await self.client.post(wrong, json=self.body())).status_code, 404)
        for value in ("allowed-once", {"answers": []}, {"answers": [{"id": "format", "selected": ["bad"]}]},
                      {"answers": [{"id": "format", "selected": ["CSV", "MD"]}]},
                      {"answers": [{"id": "format", "selected": [], "custom": "x" * 2049}]}):
            self.assertEqual((await self.client.post(url, json=self.body(value))).status_code, 422)
        self.assertEqual((await self.client.post(url, json={**self.body(), "revision": 2})).status_code, 422)
        data["expires_at"] = "2000-01-01T00:00:00+00:00"
        with self.app.state.db:
            self.app.state.db.execute("UPDATE v1_interactions SET request_json=?", (repo.dump(data),))
        self.assertEqual((await self.client.post(url, json=self.body())).status_code, 409)
        self.assertEqual((await self.detail(tid))["interactions"][0]["status"], "expired")

    async def test_unmatched_confirmation_rolls_back_event_and_ack(self):
        ws, tid, data, _ = await self.setup_request("approval")
        await self.send(ws, {"type": "task_event", "task_id": tid, "event_seq": 2,
            "event": {"type": "interaction_update", "interaction": {**data, "status": "confirmed",
                "response_id": str(uuid.uuid4()), "outcome": "allowed-once"}}})
        await self.recv(ws, "protocol_error")
        task = repo.require_task(self.app.state.db, tid)
        self.assertEqual(task["acked_seq"], 1)
        self.assertEqual((await self.detail(tid))["interactions"][0]["status"], "pending")

    async def test_hub_offline_answer_delivers_after_node_reconciliation(self):
        ws, tid, data, url = await self.setup_request("approval")
        await ws.close()
        await asyncio.sleep(.1)
        body = self.body("rejected")
        self.assertEqual((await self.client.post(url, json=body)).json()["status"], "submitted")
        ws, snapshot = await self.open_node(finish=False, tasks=[{"task_id": tid}])
        self.app.state.nodes.get("mac-a").capabilities.add("interactions_v1")
        await self.send(ws, {"type": "task_status", "task_id": tid, "local_status": "running", "last_persisted_seq": 1})
        await self.recv(ws, "ready")
        replay = await self.recv(ws, "interaction_response")
        self.assertEqual(replay["response_id"], body["response_id"])
        self.assertEqual(replay["value"], "rejected")

    async def test_terminal_invalidation_and_pinned_followup_mode(self):
        ws, tid, data, _ = await self.setup_request()
        await self.send(ws, {"type": "task_done", "task_id": tid, "status": "interrupted", "detail": "Node restarted"})
        self.assertEqual((await self.detail(tid))["interactions"][0]["status"], "interrupted")
        conn = self.app.state.db
        with conn:
            conn.execute("UPDATE v1_tasks SET status='done' WHERE task_id=?", (tid,))
            conn.execute("UPDATE v1_projects SET execution_mode='headless'")
        child, _ = repo.create_task(conn, "followup-request", "hash", "mac-a", "p-accept", "continue", 30, follows_task_id=tid)
        self.assertEqual(child["execution_mode"], "host")


class NodeContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_before_start_never_starts_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "node.db")
            journal.put({"task_id": "t-test", "status": "accepted", "cancel_requested": True})
            async def emit(value): pass
            runner = HostRunner({}, journal, "t-test", emit)
            await runner.run()
            task = journal.get("t-test")
            self.assertEqual(task["status"], "cancelled")
            self.assertTrue(task["not_started"] and task["stop_confirmed"])
            self.assertIsNone(runner.proc)
            journal.close()

    async def test_rpc_success_is_not_confirmation_and_wrong_epoch_cannot_respond(self):
        with tempfile.TemporaryDirectory() as tmp:
            journal = Journal(Path(tmp) / "node.db")
            journal.put({"task_id": "t-test", "status": "running", "last_persisted_seq": 0})
            emitted, calls = [], []
            async def emit(value): emitted.append(value)
            runner = HostRunner({}, journal, "t-test", emit)
            data = request("approval")
            item = {**data, "status": "pending", "event_id": "event-local"}
            runner.items[item["interaction_id"]] = item
            runner.live_events.add("event-local"); runner.cid = "client-1"
            async def rpc(endpoint, args): calls.append(args)
            runner.rpc = rpc
            response = {**data, "task_id": "t-test", "response_id": str(uuid.uuid4()), "value": "allowed-once"}
            await runner.respond({**response, "epoch": "wrong"})
            self.assertEqual(calls, [])
            await runner.respond(response)
            await runner.respond(response)
            self.assertEqual(len(calls), 1)
            self.assertEqual(item["status"], "submitted")
            self.assertFalse(emitted)
            runner.last_seq = 0
            await runner.record({"seq": 1, "type": "approval/asked", "data": {"id": "audit", "callId": data["call_id"]}})
            await runner.record({"seq": 2, "type": "approval/decided", "data": {"id": "audit", "outcome": "allowed-once"}})
            self.assertEqual(item["status"], "confirmed")
            self.assertEqual(emitted[-1]["event"]["interaction"]["outcome"], "allowed-once")
            journal.close()

    async def test_multiselect_custom_and_question_binding(self):
        item = request()
        item["request"]["questions"][0]["multiSelect"] = True
        value = {"answers": [{"id": "format", "selected": ["CSV", "MD"], "custom": "按日期排列"}]}
        self.assertEqual(contract.answer(item, value), value)
        for bad in ({"answers": value["answers"] * 2}, {"answers": [{"id": "wrong", "selected": ["CSV"]}]}):
            with self.assertRaises(ValueError): contract.answer(item, bad)
        item["request"]["questions"] *= 5
        with self.assertRaises(ValueError): contract.request(item)


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    for cls in (InteractionTests, NodeContractTests):
        for name in cls.__dict__:
            if name.startswith("test_"): suite.addTest(cls(name))
    return suite


if __name__ == "__main__": unittest.main()
