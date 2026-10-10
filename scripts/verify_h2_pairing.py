"""Offline pairing authority, expiry, replay, revocation and two-node isolation checks."""
import asyncio
import json
from pathlib import Path
import secrets
import sys
import time
import unittest
import uuid

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_dsh_hub import HubIntegrationTests, APP_TOKEN, NODE_TOKEN
from hub.gateway import pairing


class PairingTests(HubIntegrationTests):
    async def begin(self, name="隔离第二设备"):
        token, scan, poll = [secrets.token_urlsafe(32) for _ in range(3)]
        body = {"request_id": "p-" + uuid.uuid4().hex, "name": name,
                "token_hash": pairing.digest(token), "scan_hash": pairing.digest(scan), "poll_hash": pairing.digest(poll)}
        response = await self.client.post("/api/v1/pairing/requests", json=body, headers={"Authorization": ""})
        self.assertEqual(response.status_code, 200, response.text)
        return body, token, scan, poll, response.json()
    async def approve(self, body, scan):
        response = await self.client.post(f"/api/v1/pairing/{body['request_id']}/approve", json={"secret": scan})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["node_id"]
    async def test_only_existing_phone_can_approve_and_secrets_are_distinct(self):
        body, token, scan, poll, _ = await self.begin()
        rid = body["request_id"]
        for suffix in ("inspect", "approve"):
            self.assertEqual((await self.client.post(f"/api/v1/pairing/{rid}/{suffix}", json={"secret": scan}, headers={"Authorization": ""})).status_code, 401)
            self.assertEqual((await self.client.post(f"/api/v1/pairing/{rid}/{suffix}", json={"secret": scan}, headers={"Authorization": "Bearer " + NODE_TOKEN})).status_code, 403)
        wrong = await self.client.post(f"/api/v1/pairing/{rid}/status", json={"secret": scan}, headers={"Authorization": ""})
        self.assertEqual(wrong.status_code, 404)
        view = await self.client.post(f"/api/v1/pairing/{rid}/inspect", json={"secret": scan})
        self.assertEqual(view.json()["fingerprint"], pairing.digest(token)[:12])
        self.assertEqual(view.headers["cache-control"], "no-store")
        self.assertNotIn(token, json.dumps(dict(self.app.state.db.execute("SELECT * FROM v1_pairing_requests").fetchone())))
        self.assertIsNone(pairing.node_for_token(self.app.state.db, token))
    async def test_replay_does_not_extend_expiry_or_create_two_credentials(self):
        body, token, scan, poll, original = await self.begin()
        retry = await self.client.post("/api/v1/pairing/requests", json=body, headers={"Authorization": ""})
        self.assertEqual(retry.json(), original)
        conflict = await self.client.post("/api/v1/pairing/requests", json={**body, "name": "other"})
        self.assertEqual(conflict.status_code, 409)
        first = await self.approve(body, scan); second = await self.approve(body, scan)
        self.assertEqual(first, second)
        self.assertEqual(pairing.node_for_token(self.app.state.db, token), first)
        self.assertIsNone(pairing.node_for_token(self.app.state.db, scan))
        status = await self.client.post(f"/api/v1/pairing/{body['request_id']}/status", json={"secret": poll}, headers={"Authorization": ""})
        self.assertEqual(status.json()["node_id"], first)
        self.assertEqual(self.app.state.db.execute("SELECT count(*) FROM v1_node_credentials").fetchone()[0], 1)
    async def test_expired_requests_and_oversized_or_duplicate_keys_fail_closed(self):
        body, token, scan, poll, _ = await self.begin()
        self.app.state.db.execute("UPDATE v1_pairing_requests SET expires_at=?", (time.time() - 1,)); self.app.state.db.commit()
        self.assertEqual((await self.client.post(f"/api/v1/pairing/{body['request_id']}/approve", json={"secret": scan})).status_code, 409)
        for bad in ({**body, "request_id": "p-" + uuid.uuid4().hex, "token_hash": pairing.digest(APP_TOKEN)},
                    {**body, "request_id": "p-" + uuid.uuid4().hex, "poll_hash": body["scan_hash"]},
                    {**body, "request_id": "p-" + uuid.uuid4().hex, "name": "\nspoof"}):
            self.assertIn((await self.client.post("/api/v1/pairing/requests", json=bad)).status_code, (409, 422))
        self.assertEqual((await self.client.post("/api/v1/pairing/requests", content=b"x" * 2049, headers={"Authorization": ""})).status_code, 400)
    async def test_two_nodes_isolate_task_reporting_and_revocation_closes_socket(self):
        body, token, scan, poll, _ = await self.begin()
        nid = await self.approve(body, scan)
        original, _ = await self.open_node()
        second, _ = await self.open_node(node_id=nid, token=token)
        response, _ = await self.create(node_id=nid)
        tid = response.json()["task_id"]
        await self.recv(second, "task_assign"); await self.start(second, tid)
        await self.send(original, {"type": "task_event", "task_id": tid, "event_seq": 1, "event": {"type": "text", "text": "foreign"}})
        error = await self.recv(original, "protocol_error")
        self.assertEqual(error["code"], "forbidden_role")
        self.assertEqual((await self.client.get(f"/api/v1/tasks/{tid}", headers={"Authorization": "Bearer " + token})).status_code, 403)
        result = await self.client.post(f"/api/v1/paired-nodes/{nid}/revoke")
        self.assertEqual(result.status_code, 200)
        with self.assertRaises(websockets.ConnectionClosed): await second.recv()
        self.assertIsNone(pairing.node_for_token(self.app.state.db, token))
        self.assertEqual((await self.detail(tid))["status"], "interrupted")
        self.assertEqual((await self.client.post(f"/api/v1/paired-nodes/{nid}/revoke")).status_code, 200)
        self.assertEqual((await self.client.get("/api/v1/nodes", headers={"Authorization": "Bearer " + token})).status_code, 401)
        self.assertTrue(any(n["node_id"] == "mac-a" for n in (await self.client.get("/api/v1/nodes")).json()["nodes"]))
        with self.assertRaises(websockets.InvalidStatus):
            await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/node", additional_headers={"Authorization": "Bearer " + token})
    async def test_pairing_registration_is_bounded(self):
        for index in range(10): await self.begin(str(index))
        body = {"request_id": "p-" + uuid.uuid4().hex, "name": "excess", **{k: pairing.digest(k) for k in ("scan_hash", "poll_hash", "token_hash")}}
        response = await self.client.post("/api/v1/pairing/requests", json=body, headers={"Authorization": ""})
        self.assertEqual(response.status_code, 429)

    async def test_two_pending_requests_cannot_approve_the_same_credential(self):
        body, token, scan, poll, _ = await self.begin()
        second = {**body, "request_id": "p-" + uuid.uuid4().hex}
        self.assertEqual((await self.client.post("/api/v1/pairing/requests", json=second)).status_code, 200)
        await self.approve(body, scan)
        result = await self.client.post(f"/api/v1/pairing/{second['request_id']}/approve", json={"secret": scan})
        self.assertEqual(result.status_code, 409)
        self.assertEqual(self.app.state.db.execute("SELECT count(*) FROM v1_node_credentials").fetchone()[0], 1)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(PairingTests(name) for name in PairingTests.__dict__ if name.startswith("test_"))


if __name__ == "__main__": unittest.main()
