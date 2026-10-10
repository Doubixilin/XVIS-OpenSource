"""R1-01: temporary SQLite/loopback Hub/fake APNs only; no real credentials or push."""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx
from pydantic import ValidationError
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hub.gateway import inbox, notifications, v1_repo as repo
from hub.gateway.app import create_app
from scripts import verify_dsh_hub as harness
from shared.v1_db import SCHEMA, init_v1_db
from shared.v1_schemas import NotificationDevice, ResultRead


def device_id(number=1):
    return "d-" + f"{number:032x}"


def device(number=1, **fields):
    return NotificationDevice.model_validate({"token": f"{number:064x}", "environment": "sandbox", "enabled": True, **fields})


def task(conn, number=1, status="done"):
    value, _ = repo.create_task(conn, f"request-{number}", f"hash-{number}", "mac-a", "p-accept", f"私人指令-{number}\n其余正文", 30)
    repo.change_status(conn, value, "assigned")
    if status in repo.TERMINAL:
        repo.change_status(conn, repo.require_task(conn, value["task_id"]), status, result={"final_text": "私人回答"} if status == "done" else None)
    return repo.require_task(conn, value["task_id"])


class SchemaAndConfigTests(unittest.TestCase):
    def test_device_token_variable_even_lowercase_and_strict_body(self):
        for size in (32, 64, 512):
            self.assertEqual(len(device(token="a" * size).token), size)
        for fields in ({"token": "a" * 30}, {"token": "a" * 513}, {"token": "a" * 33},
                       {"token": "A" * 64}, {"token": "../" * 20}, {"enabled": 1},
                       {"environment": "dev"}, {"topic": "other-app"}):
            with self.subTest(fields=fields):
                with self.assertRaises(ValidationError):
                    device(**fields)
        for revision in (0, -1, True, "1"):
            with self.assertRaises(ValidationError):
                ResultRead.model_validate({"revision": revision})
        with self.assertRaises(ValidationError):
            ResultRead.model_validate({"revision": 1, "task_id": "not-allowed"})

    def test_optional_configuration_never_reads_credentials_or_imports_http2(self):
        with patch.object(notifications.ssl, "create_default_context", side_effect=AssertionError("must not read cert")):
            self.assertEqual(notifications.validate_config({}), {"enabled": False})
            self.assertIsNone(notifications.create_sender({"enabled": False}))
        for raw in ([], {"enabled": "true"}, {"enabled": True},
                    {"enabled": True, "environment": "dev", "topic": "x", "certfile": "/fake", "keyfile": "/fake"},
                    {"enabled": True, "environment": "sandbox", "topic": "x", "certfile": "relative", "keyfile": "/fake"}):
            with self.assertRaises(ValueError):
                notifications.validate_config({"apns": raw})

    def test_unloadable_sender_reports_unavailable_without_secret_logging(self):
        settings = notifications.validate_config({"apns": {"enabled": True, "environment": "sandbox", "topic": "test.xvis",
                                                           "certfile": "/nonexistent/private/cert.pem", "keyfile": "/nonexistent/private/key.pem"}})
        with patch.object(notifications, "APNsSender", side_effect=OSError("PRIVATE PEM OR TOKEN")):
            with self.assertLogs(notifications.LOGGER, level="WARNING") as logs:
                self.assertIsNone(notifications.create_sender(settings))
        self.assertNotIn("PRIVATE", " ".join(logs.output))
        self.assertNotIn("nonexistent", " ".join(logs.output))


class InboxPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hub.db"
        self.conn = init_v1_db(self.path)

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_server_identity_survives_restart_and_new_database_is_distinct(self):
        identity = inbox.server_id(self.conn)
        self.assertRegex(identity, r"^s-[0-9a-f]{32}$")
        self.conn.close()
        self.conn = init_v1_db(self.path)
        self.assertEqual(inbox.server_id(self.conn), identity)
        other = init_v1_db(self.path.with_name("other.db"))
        self.assertNotEqual(inbox.server_id(other), identity)
        other.close()

    def test_existing_registration_additive_nonce_migration_preserves_identity_and_device(self):
        path = self.path.with_name("pre-nonce.db")
        old = sqlite3.connect(path)
        old.executescript(SCHEMA.replace(" registration_nonce TEXT NOT NULL DEFAULT '',\n", ""))
        identity = old.execute("SELECT value FROM v1_hub_metadata WHERE key='server_id'").fetchone()[0]
        old.execute("INSERT INTO v1_notification_devices(device_id,token,environment,enabled,updated_at) VALUES(?,?,?,?,?)",
                    (device_id(), device().token, "sandbox", 1, repo.now_iso()))
        old.commit(); old.close()
        migrated = init_v1_db(path)
        row = migrated.execute("SELECT * FROM v1_notification_devices").fetchone()
        self.assertEqual((row["device_id"], row["token"], row["enabled"]), (device_id(), device().token, 1))
        self.assertRegex(row["registration_nonce"], r"^[0-9a-f]{32}$")
        self.assertEqual(inbox.server_id(migrated), identity)
        migrated.close()

    def test_all_terminal_statuses_only_enter_once_and_read_replay_stays_read(self):
        for number, status in enumerate(sorted(repo.TERMINAL), 1):
            value = task(self.conn, number, status)
            row = self.conn.execute("SELECT * FROM v1_inbox_results WHERE task_id=?", (value["task_id"],)).fetchone()
            self.assertEqual((row["status"], row["revision"]), (status, 1))
            read = inbox.mark_read(self.conn, row["result_id"], 1)["item"]
            self.assertTrue(repo.change_status(self.conn, value, status))
            self.assertEqual(inbox.mark_read(self.conn, row["result_id"], 1)["item"]["read_at"], read["read_at"])
        self.assertEqual(inbox.list_unread(self.conn)["unread_count"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_inbox_results").fetchone()[0], 5)

    def test_atomic_terminal_and_outbox_rollback_together(self):
        inbox.save_device(self.conn, device_id(), device(), None)
        value = task(self.conn, status="assigned")
        self.conn.execute("CREATE TRIGGER block_outbox BEFORE INSERT ON v1_notification_outbox BEGIN SELECT RAISE(ABORT,'test rollback'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            repo.change_status(self.conn, value, "done")
        self.assertEqual(repo.require_task(self.conn, value["task_id"])["status"], "assigned")
        self.assertEqual(inbox.list_unread(self.conn)["unread_count"], 0)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_events WHERE task_id=? AND payload_json=?", (value["task_id"], repo.dump({"status": "done"}))).fetchone()[0], 0)

    def test_old_database_does_not_backfill_but_old_active_task_enters_on_terminal(self):
        old_path = self.path.with_name("old.db")
        old = sqlite3.connect(old_path)
        old.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS v1_hub_metadata")[0])
        for number, status in ((1, "done"), (2, "assigned")):
            old.execute("INSERT INTO v1_tasks(task_id,client_request_id,request_hash,status,node_id,project_id,prompt,timeout_sec,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (f"old-{number}", f"r-{number}", f"h-{number}", status, "mac-a", "p", "原指令", 30, repo.now_iso()))
        old.commit(); old.close()
        migrated = init_v1_db(old_path)
        self.assertEqual(inbox.list_unread(migrated)["unread_count"], 0)
        self.assertEqual(repo.require_task(migrated, "old-1")["prompt"], "原指令")
        self.assertTrue(repo.change_status(migrated, repo.require_task(migrated, "old-1"), "done"))
        self.assertTrue(repo.change_status(migrated, repo.require_task(migrated, "old-2"), "failed"))
        self.assertEqual([item["task_id"] for item in inbox.list_unread(migrated)["items"]], ["old-2"])
        migrated.close()

    def test_read_compare_and_swap_cannot_ack_newer_revision(self):
        value = task(self.conn)
        first = inbox.list_unread(self.conn)["items"][0]
        inbox.mark_read(self.conn, first["result_id"], 1)
        # This is a future-compatible repository fixture, not a state-machine extension.
        with self.conn:
            inbox.record_terminal(self.conn, value["task_id"], "interrupted", repo.now_iso())
        with self.assertRaises(repo.RepoError) as conflict:
            inbox.mark_read(self.conn, first["result_id"], 1)
        self.assertEqual((conflict.exception.status, conflict.exception.code), (409, "result_revision_conflict"))
        newest = inbox.get_item(self.conn, first["result_id"])
        self.assertEqual((newest["revision"], newest["read_at"]), (2, None))
        self.assertIsNotNone(inbox.mark_read(self.conn, first["result_id"], 2)["item"]["read_at"])

    def test_stable_pagination_total_count_and_read_items_do_not_hide_older_unread(self):
        for number in range(1, 7): task(self.conn, number)
        first = inbox.list_unread(self.conn, limit=2)
        self.assertEqual(first["unread_count"], 6)
        self.assertEqual([item["title"] for item in first["items"]], ["私人指令-6", "私人指令-5"])
        inbox.mark_read(self.conn, first["items"][0]["result_id"], 1)
        second = inbox.list_unread(self.conn, first["next_cursor"], 2)
        third = inbox.list_unread(self.conn, second["next_cursor"], 2)
        self.assertEqual(second["unread_count"], 5)
        self.assertEqual(len({item["task_id"] for page in (first, second, third) for item in page["items"]}), 6)
        self.assertIsNone(third["next_cursor"])
        for cursor in ("wrong", "0", "-1", str(2 ** 64)):
            with self.assertRaises(repo.RepoError): inbox.list_unread(self.conn, cursor)

    def test_device_token_deduplication_disable_delete_and_environment(self):
        inbox.save_device(self.conn, device_id(1), device(), None)
        value = task(self.conn)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_notification_outbox").fetchone()[0], 1)
        inbox.save_device(self.conn, device_id(2), device(), None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_notification_devices").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT state FROM v1_notification_outbox").fetchone()[0], "skipped")
        with self.assertRaises(repo.RepoError):
            inbox.save_device(self.conn, device_id(2), device(environment="production"), "sandbox")
        self.assertEqual(self.conn.execute("SELECT environment FROM v1_notification_devices").fetchone()[0], "sandbox")
        task(self.conn, 2)
        inbox.save_device(self.conn, device_id(2), device(enabled=False), None)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_notification_outbox WHERE state='pending'").fetchone()[0], 0)
        inbox.remove_device(self.conn, device_id(2)); inbox.remove_device(self.conn, device_id(2))
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_notification_devices").fetchone()[0], 0)


class FakeSender:
    environment = "sandbox"
    def __init__(self, *results):
        self.results = list(results) or [notifications.SendResult(200)]
        self.sent = []
    async def send(self, token, payload, apns_id, collapse_id):
        self.sent.append((token, payload, apns_id, collapse_id))
        result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        if isinstance(result, Exception): raise result
        return result


class SenderTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "hub.db"
        self.conn = init_v1_db(self.path)
        inbox.save_device(self.conn, device_id(), device(), None)
        self.task = task(self.conn)
        self.result = inbox.list_unread(self.conn)["items"][0]
    def tearDown(self):
        self.conn.close(); self.temp.cleanup()
    def pending(self): return dict(self.conn.execute("SELECT * FROM v1_notification_outbox ORDER BY id LIMIT 1").fetchone())

    async def test_sender_accepted_is_not_delivery_and_payload_contains_only_identifiers_status(self):
        sender = FakeSender()
        self.assertEqual(await notifications.send_pending(self.conn, sender), 1)
        self.assertEqual(self.pending()["state"], "accepted")
        self.assertEqual(await notifications.send_pending(self.conn, sender), 0)
        payload = sender.sent[0][1]
        self.assertEqual(set(payload), {"aps", "xvis"})
        self.assertEqual(set(payload["xvis"]), {"server_id", "task_id", "result_id", "revision"})
        encoded = json.dumps(payload, ensure_ascii=False)
        for private in ("私人指令", "私人回答", device().token, harness.APP_TOKEN): self.assertNotIn(private, encoded)
        self.assertEqual(inbox.list_unread(self.conn)["unread_count"], 1)

    async def test_read_and_device_removal_stop_pending_sends(self):
        inbox.mark_read(self.conn, self.result["result_id"], 1)
        sender = FakeSender()
        self.assertEqual(await notifications.send_pending(self.conn, sender), 0)
        task(self.conn, 2)
        inbox.remove_device(self.conn, device_id())
        self.assertEqual(await notifications.send_pending(self.conn, sender), 0)
        self.assertEqual(sender.sent, [])

    async def test_network_retry_persists_across_restart_and_keeps_notification_identity(self):
        sender = FakeSender(RuntimeError("PRIVATE TOKEN"), notifications.SendResult(200))
        now = time.time()
        await notifications.send_pending(self.conn, sender, current_time=now)
        self.assertEqual((self.pending()["state"], self.pending()["attempts"], self.pending()["last_reason"]), ("pending", 1, "network_error"))
        self.conn.close(); self.conn = init_v1_db(self.path)
        self.assertEqual(await notifications.send_pending(self.conn, sender, current_time=now + 1), 0)
        self.assertEqual(await notifications.send_pending(self.conn, sender, current_time=now + 31), 1)
        self.assertEqual(sender.sent[0][2:], sender.sent[1][2:])
        self.assertEqual(repo.require_task(self.conn, self.task["task_id"])["status"], "done")

    async def test_server_errors_wait_fifteen_minutes_and_retry_budget_is_bounded(self):
        sender = FakeSender(notifications.SendResult(503, "ServiceUnavailable"))
        now = time.time()
        await notifications.send_pending(self.conn, sender, current_time=now)
        self.assertEqual(await notifications.send_pending(self.conn, sender, current_time=now + 899), 0)
        for number in range(1, 5): await notifications.send_pending(self.conn, sender, current_time=now + 901 * number)
        self.assertEqual((self.pending()["state"], self.pending()["attempts"]), ("failed", 5))
        self.assertEqual(len(sender.sent), 5)

    async def test_invalid_token_is_disabled_but_tasks_and_unread_results_stay_available(self):
        sender = FakeSender(notifications.SendResult(410, "Unregistered"))
        await notifications.send_pending(self.conn, sender)
        self.assertEqual(self.conn.execute("SELECT enabled FROM v1_notification_devices").fetchone()[0], 0)
        self.assertEqual(inbox.list_unread(self.conn)["unread_count"], 1)
        self.assertEqual(repo.require_task(self.conn, self.task["task_id"])["status"], "done")
        task(self.conn, 2)
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM v1_notification_outbox").fetchone()[0], 1)

    async def test_inflight_failure_does_not_disable_replacement_token(self):
        conn = self.conn
        class ReplacementSender(FakeSender):
            async def send(self, *args):
                inbox.save_device(conn, device_id(), device(2), None)
                return notifications.SendResult(410, "Unregistered")
        await notifications.send_pending(self.conn, ReplacementSender())
        self.assertEqual(tuple(self.conn.execute("SELECT token,enabled FROM v1_notification_devices").fetchone()), (device(2).token, 1))

    async def test_old_unregistered_response_cannot_disable_same_token_reregistration(self):
        conn = self.conn
        original = conn.execute("SELECT registration_nonce FROM v1_notification_devices").fetchone()[0]
        class SameTokenSender(FakeSender):
            async def send(self, *args):
                inbox.save_device(conn, device_id(), device(), None)
                return notifications.SendResult(410, "Unregistered")
        await notifications.send_pending(self.conn, SameTokenSender())
        saved = conn.execute("SELECT enabled,registration_nonce FROM v1_notification_devices").fetchone()
        self.assertEqual(saved["enabled"], 1)
        self.assertNotEqual(saved["registration_nonce"], original)

    async def test_old_unregistered_response_cannot_disable_deleted_then_same_token_registration(self):
        conn = self.conn
        class RecreatedSender(FakeSender):
            async def send(self, *args):
                inbox.remove_device(conn, device_id())
                inbox.save_device(conn, device_id(), device(), None)
                return notifications.SendResult(410, "Unregistered")
        await notifications.send_pending(self.conn, RecreatedSender())
        self.assertEqual(conn.execute("SELECT enabled FROM v1_notification_devices").fetchone()[0], 1)
        self.assertEqual(self.pending()["state"], "skipped")

    async def test_read_while_send_is_inflight_does_not_resurrect_pending_record(self):
        conn, result_id = self.conn, self.result["result_id"]
        class ReadingSender(FakeSender):
            async def send(self, *args):
                inbox.mark_read(conn, result_id, 1)
                return notifications.SendResult(None, "network_error")
        await notifications.send_pending(self.conn, ReadingSender())
        self.assertEqual(self.pending()["state"], "skipped")
        self.assertEqual(inbox.list_unread(self.conn)["unread_count"], 0)

    async def test_environment_mismatch_never_sends(self):
        sender = FakeSender(); sender.environment = "production"
        self.assertEqual(await notifications.send_pending(self.conn, sender), 0)
        self.assertEqual(self.pending()["state"], "skipped")
        self.assertEqual(sender.sent, [])

    async def test_actual_request_uses_http2_headers_without_content(self):
        requests = []
        async def handle(req):
            requests.append(req)
            return httpx.Response(200, extensions={"http_version": b"HTTP/2"})
        sender = notifications.APNsSender.__new__(notifications.APNsSender)
        sender.environment, sender.topic = "sandbox", "test.xvis"
        sender.client = httpx.AsyncClient(base_url="https://api.sandbox.push.apple.com", transport=httpx.MockTransport(handle))
        try:
            await notifications.send_pending(self.conn, sender)
        finally: await sender.aclose()
        request = requests[0]
        self.assertEqual(request.url.path, "/3/device/" + device().token)
        self.assertEqual(request.headers["apns-topic"], "test.xvis")
        self.assertEqual(request.headers["apns-push-type"], "alert")
        self.assertEqual(request.headers["apns-id"], self.pending()["apns_id"])
        self.assertNotIn("authorization", request.headers)
        self.assertEqual(set(json.loads(request.content)), {"aps", "xvis"})

    async def test_default_info_root_and_client_loggers_never_record_device_token(self):
        records = []
        class Capture(logging.Handler):
            def emit(self, record): records.append(record.getMessage())
        capture = Capture()
        loggers = [logging.getLogger(), logging.getLogger("httpx"), logging.getLogger("httpcore")]
        levels = [logger.level for logger in loggers]
        for logger in loggers: logger.addHandler(capture)
        loggers[0].setLevel(logging.INFO)
        loggers[1].setLevel(logging.INFO)
        loggers[2].setLevel(logging.DEBUG)
        actual_client = httpx.AsyncClient
        async def handle(req): return httpx.Response(200, extensions={"http_version": b"HTTP/2"})
        def fake_client(**kwargs):
            self.assertTrue(kwargs["http2"])
            self.assertFalse(kwargs["http1"])
            self.assertFalse(kwargs["trust_env"])
            # No TLS/APNs network: HTTP/2 response is a protocol fixture, not a handshake claim.
            return actual_client(base_url=kwargs["base_url"], transport=httpx.MockTransport(handle))
        sender = None
        try:
            with patch.object(notifications.ssl.SSLContext, "load_cert_chain", return_value=None), patch.object(httpx, "AsyncClient", side_effect=fake_client):
                sender = notifications.APNsSender({"environment": "sandbox", "topic": "test.xvis", "certfile": "/fake-cert.pem", "keyfile": "/fake-key.pem"})
            await notifications.send_pending(self.conn, sender)
            self.assertGreaterEqual(loggers[1].level, logging.WARNING)
            self.assertGreaterEqual(loggers[2].level, logging.WARNING)
            self.assertNotIn(device().token, " ".join(records))
            self.assertNotIn("/3/device/", " ".join(records))
        finally:
            if sender is not None: await sender.aclose()
            for logger, level in zip(loggers, levels):
                logger.removeHandler(capture); logger.setLevel(level)


class InboxHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.HubIntegrationTests.asyncSetUp
    asyncTearDown = harness.HubIntegrationTests.asyncTearDown
    open_node = harness.HubIntegrationTests.open_node
    recv = harness.HubIntegrationTests.recv
    send = harness.HubIntegrationTests.send
    create = harness.HubIntegrationTests.create
    start = harness.HubIntegrationTests.start
    done = harness.HubIntegrationTests.done

    async def test_real_terminal_replay_followup_and_read_round_are_independent(self):
        ws, _ = await self.open_node()
        saved = await self.client.put("/api/v1/notification-devices/" + device_id(), json=device().model_dump())
        self.assertFalse(saved.json()["push_available"])
        response, body = await self.create()
        tid = response.json()["task_id"]
        await self.recv(ws, "task_assign"); await self.start(ws, tid); await self.done(ws, tid)
        first = (await self.client.get("/api/v1/inbox")).json()["items"][0]
        self.assertEqual(first["task_id"], tid)
        read = await self.client.put(f'/api/v1/inbox/{first["result_id"]}/read', json={"revision": 1})
        self.assertEqual(read.status_code, 200)
        await self.done(ws, tid)
        retry = await self.client.post("/api/v1/tasks", json=body)
        self.assertEqual(retry.json()["task_id"], tid)
        self.assertEqual((await self.client.get("/api/v1/inbox")).json()["unread_count"], 0)
        follow = await self.client.post(f"/api/v1/tasks/{tid}/followup", json={"client_request_id": "follow-r1", "message": "继续"})
        child = follow.json()["task_id"]
        await self.send(ws, {"type": "heartbeat", "running": []})
        await self.recv(ws, "task_assign"); await self.start(ws, child); await self.done(ws, child)
        page = (await self.client.get("/api/v1/inbox")).json()
        self.assertEqual([item["task_id"] for item in page["items"]], [child])
        old = await self.client.get(f'/api/v1/inbox/{first["result_id"]}')
        self.assertIsNotNone(old.json()["item"]["read_at"])
        self.assertEqual(self.app.state.db.execute("SELECT COUNT(*) FROM v1_notification_outbox").fetchone()[0], 2)

    async def test_cancel_request_is_not_terminal_but_queued_cancel_is(self):
        with self.app.state.db:
            self.app.state.db.execute("INSERT INTO v1_projects(node_id,project_id,name,path,available) VALUES('mac-a','p-accept','test',?,1)", (str(self.directory),))
        queued, _ = await self.create()
        cancel = await self.client.post(f'/api/v1/tasks/{queued.json()["task_id"]}/cancel')
        self.assertEqual(cancel.json()["status"], "cancelled")
        self.assertEqual((await self.client.get("/api/v1/inbox")).json()["items"][0]["status"], "cancelled")
        active = task(self.app.state.db, number=99, status="assigned")
        await self.client.post(f'/api/v1/tasks/{active["task_id"]}/cancel')
        self.assertEqual((await self.client.get("/api/v1/inbox")).json()["unread_count"], 1)

    async def test_app_only_routes_strict_errors_and_no_store(self):
        value = task(self.app.state.db)
        result = inbox.list_unread(self.app.state.db)["items"][0]
        routes = (("GET", "/api/v1/inbox", None), ("GET", f'/api/v1/inbox/{result["result_id"]}', None),
                  ("PUT", f'/api/v1/inbox/{result["result_id"]}/read', {"revision": 1}),
                  ("GET", "/api/v1/notification-settings", None),
                  ("PUT", "/api/v1/notification-devices/" + device_id(), device().model_dump()),
                  ("DELETE", "/api/v1/notification-devices/" + device_id(), None))
        for method, path, body in routes:
            for token, status in (("wrong", 401), (harness.NODE_TOKEN, 403)):
                response = await self.client.request(method, path, json=body, headers={"Authorization": "Bearer " + token})
                self.assertEqual(response.status_code, status)
                self.assertEqual(response.headers["cache-control"], "no-store")
        for body, status in (({"revision": 2}, 409), ({"revision": 2 ** 64}, 409), ({"revision": True}, 422), ({"revision": 0}, 422)):
            response = await self.client.put(f'/api/v1/inbox/{result["result_id"]}/read', json=body)
            self.assertEqual(response.status_code, status)
        self.assertIsNone(inbox.get_item(self.app.state.db, result["result_id"])["read_at"])
        self.assertEqual((await self.client.get("/api/v1/inbox/r-" + "0" * 32)).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/inbox/invalid")).status_code, 422)
        self.assertEqual((await self.client.put("/api/v1/notification-devices/invalid", json=device().model_dump())).status_code, 422)

    async def test_lookup_by_task_reads_old_page_and_read_item_without_backfill(self):
        old_page = task(self.app.state.db, 1)
        for number in range(2, 34): task(self.app.state.db, number)
        page = (await self.client.get("/api/v1/inbox")).json()
        self.assertNotIn(old_page["task_id"], [item["task_id"] for item in page["items"]])
        lookup = await self.client.get("/api/v1/inbox/by-task/" + old_page["task_id"])
        self.assertEqual(lookup.status_code, 200)
        self.assertEqual(lookup.headers["cache-control"], "no-store")
        item = lookup.json()["item"]
        self.assertIsNone(item["read_at"])
        await self.client.put(f'/api/v1/inbox/{item["result_id"]}/read', json={"revision": item["revision"]})
        read = (await self.client.get("/api/v1/inbox/by-task/" + old_page["task_id"])).json()["item"]
        self.assertEqual(read["result_id"], item["result_id"])
        self.assertIsNotNone(read["read_at"])
        with self.app.state.db:
            self.app.state.db.execute("INSERT INTO v1_tasks(task_id,client_request_id,request_hash,status,node_id,project_id,prompt,timeout_sec,created_at) VALUES('old-terminal','legacy-r','legacy-h','done','mac-a','p-accept','old',30,?)", (repo.now_iso(),))
        self.assertEqual((await self.client.get("/api/v1/inbox/by-task/old-terminal")).status_code, 404)
        self.assertEqual((await self.client.get("/api/v1/inbox")).json()["unread_count"], 32)

    async def test_lookup_by_task_auth_missing_and_path_parameters_do_not_mutate(self):
        value = task(self.app.state.db)
        url = "/api/v1/inbox/by-task/" + value["task_id"]
        for token, status in (("wrong", 401), (harness.NODE_TOKEN, 403)):
            response = await self.client.get(url, headers={"Authorization": "Bearer " + token})
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.headers["cache-control"], "no-store")
        for suffix in ("not-found", "%27%20OR%201%3D1", "invalid%2Fsegment"):
            self.assertEqual((await self.client.get("/api/v1/inbox/by-task/" + suffix)).status_code, 404)
        self.assertEqual(inbox.list_unread(self.app.state.db)["unread_count"], 1)
        self.assertIsNone(inbox.get_item_for_task(self.app.state.db, value["task_id"])["read_at"])

    async def test_registration_and_settings_work_without_apns_and_never_echo_token(self):
        settings = (await self.client.get("/api/v1/notification-settings")).json()
        self.assertFalse(settings["push_available"])
        for _ in range(2):
            response = await self.client.put("/api/v1/notification-devices/" + device_id(), json=device().model_dump())
            self.assertEqual(response.status_code, 200)
            self.assertEqual(set(response.json()), {"server_id", "device_id", "enabled", "push_available"})
            self.assertNotIn(device().token, response.text)
        self.assertEqual(self.app.state.db.execute("SELECT COUNT(*) FROM v1_notification_devices").fetchone()[0], 1)
        for _ in range(2):
            self.assertFalse((await self.client.delete("/api/v1/notification-devices/" + device_id())).json()["enabled"])

    async def test_missing_pem_sender_does_not_break_http_or_environment_guard(self):
        self.app.state.config["apns"] = {"enabled": True, "environment": "sandbox"}
        # Rebuild the same offline fixture with an explicitly configured, nonexistent PEM.
        self.server.should_exit = True; await asyncio.wait_for(self.server_task, 5)
        data = harness.config_data(self.directory, self.port)
        data["apns"] = {"enabled": True, "environment": "sandbox", "topic": "test.xvis",
                        "certfile": str(self.directory / "missing.pem"), "keyfile": str(self.directory / "missing-key.pem")}
        self.config_path.write_text(yaml.safe_dump(data))
        self.app = create_app(str(self.config_path))
        import uvicorn
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error", lifespan="on"))
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started: break
            await asyncio.sleep(0.01)
        self.assertFalse((await self.client.get("/api/v1/notification-settings")).json()["push_available"])
        conflict = await self.client.put("/api/v1/notification-devices/" + device_id(), json=device(environment="production").model_dump())
        self.assertEqual(conflict.status_code, 422)
        self.assertEqual((await self.client.get("/api/v1/inbox")).status_code, 200)


if __name__ == "__main__":
    unittest.main(verbosity=2)
