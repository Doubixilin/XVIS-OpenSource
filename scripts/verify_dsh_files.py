"""R1-02 isolated file contract and authenticated loopback Hub checks."""
from __future__ import annotations
import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
import uuid
import zipfile
from unittest.mock import patch
import websockets

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from hub.gateway import artifacts, v1_repo as repo
from shared.file_contract import MAX_BYTES, FileError, describe, inspect, metadata
from shared.v1_db import init_v1_db
from scripts import verify_dsh_hub as harness

def file_id():
    return "f-" + uuid.uuid4().hex


def docx(extra=None, malformed=False):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", '<Types><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("_rels/.rels", "<Relationships/>")
        archive.writestr("word/document.xml", "<broken" if malformed else "<document><p>成果</p></document>")
        if extra:
            for name, value in extra.items():
                archive.writestr(name, value)
    return buffer.getvalue()


def entry(body=b"result", name="成果.txt"):
    return {**describe(body, file_id(), name), "status": "pending", "error": None}


class FileContractTests(unittest.TestCase):
    def test_supported_utf8_bom_pdf_and_docx_containers(self):
        for name, body in (("说明.txt", "完成\n".encode()), ("a.md", b"\xef\xbb\xbf# done"), ("a.csv", b"a,b\n1,2\n"),
                           ("a.pdf", b"%PDF-1.7\nobj\n%%EOF\n"), ("a.docx", docx())):
            with self.subTest(name=name):
                result = describe(body, file_id(), name)
                self.assertEqual(result["size_bytes"], len(body))
                self.assertEqual(result["sha256"], hashlib.sha256(body).hexdigest())

    def test_reject_empty_oversize_nul_encoding_mime_and_pdf_truncation(self):
        for body, mime in ((b"", "text/plain"), (b"a" * (MAX_BYTES + 1), "text/plain"), (b"a\0b", "text/plain"),
                           (b"\xff", "text/plain"), (b"x", "image/png"), (b"%PDF-1.7\nnoeof", "application/pdf"),
                           (b"%PDF-invalid\n%%EOF", "application/pdf")):
            with self.subTest(mime=mime, size=len(body)), self.assertRaises(FileError):
                inspect(body, mime)

    def test_reject_docx_corruption_macro_traversal_xml_and_budget(self):
        for body in (docx()[:-20], docx(malformed=True), docx({"../escape": "x"}), docx({"word/vbaProject.bin": "x"}),
                     docx({"huge": b"x" * (64 * 1024 * 1024 + 1)})):
            with self.assertRaises(FileError):
                inspect(body, "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_reject_docx_symlink_and_xml_entities(self):
        for value, name in ((b"<!DOCTYPE a [<!ENTITY x 'x'>]><a>&x;</a>", "word/document.xml"), (b"elsewhere", "linked")):
            buffer = io.BytesIO(docx())
            with zipfile.ZipFile(buffer, "a") as archive:
                info = zipfile.ZipInfo(name); info.external_attr = (stat.S_IFLNK | 0o777) << 16 if name == "linked" else 0
                archive.writestr(info, value)
            with self.assertRaises(FileError):
                inspect(buffer.getvalue(), "application/vnd.openxmlformats-officedocument.wordprocessingml.document")

    def test_metadata_rejects_name_path_control_type_hash_bool_and_ids(self):
        valid = entry()
        for change in ({"filename": "../x.txt"}, {"filename": "x\n.txt"}, {"filename": "中" * 61 + ".txt"},
                       {"filename": "run.exe"}, {"mime_type": "application/pdf"}, {"sha256": "A" * 64},
                       {"size_bytes": True}, {"file_id": "../x"}):
            with self.subTest(change=change), self.assertRaises(FileError):
                metadata({**valid, **change})

    def test_additive_migration_preserves_historical_tasks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "db.sqlite"
            conn = init_v1_db(path)
            task, _ = repo.create_task(conn, "old", "hash", "mac", "p", "old", 30)
            with conn:
                conn.execute("DROP TABLE v1_files"); conn.execute("DROP TABLE v1_file_manifests")
                conn.execute("CREATE TABLE legacy(value TEXT)"); conn.execute("INSERT INTO legacy VALUES('keep')")
            conn.close(); conn = init_v1_db(path)
            self.assertEqual(repo.require_task(conn, task["task_id"])["request_hash"], "hash")
            self.assertEqual(repo.task_view(conn, repo.require_task(conn, task["task_id"]))["file_transfer"]["state"], "unknown")
            self.assertEqual(conn.execute("SELECT value FROM legacy").fetchone()[0], "keep")
            conn.close()


class FileHTTPTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.HubIntegrationTests.asyncSetUp
    asyncTearDown = harness.HubIntegrationTests.asyncTearDown
    open_node = harness.HubIntegrationTests.open_node
    recv = harness.HubIntegrationTests.recv
    send = harness.HubIntegrationTests.send
    create = harness.HubIntegrationTests.create
    detail = harness.HubIntegrationTests.detail
    start = harness.HubIntegrationTests.start
    done = harness.HubIntegrationTests.done

    async def ready_task(self):
        ws, _ = await self.open_node()
        response, _ = await self.create(); tid = response.json()["task_id"]
        await self.recv(ws, "task_assign"); await self.start(ws, tid); await self.done(ws, tid)
        return ws, tid

    async def declare(self, tid, values, note="", token=harness.NODE_TOKEN):
        return await self.client.post(f"/api/v1/tasks/{tid}/files/manifest", json={"files": values, "note": note},
            headers={"Authorization": "Bearer " + token})

    async def upload(self, tid, value, body=b"result", mime=None, token=harness.NODE_TOKEN):
        return await self.client.put(f"/api/v1/tasks/{tid}/files/{value['file_id']}", content=body,
            headers={"Authorization": "Bearer " + token, "Content-Type": mime or value["mime_type"]})

    async def listing(self, tid, token=harness.APP_TOKEN):
        return await self.client.get(f"/api/v1/tasks/{tid}/files", headers={"Authorization": "Bearer " + token})

    async def test_fixed_manifest_raw_bytes_headers_and_idempotency(self):
        ws, tid = await self.ready_task(); value = entry()
        self.assertEqual((await self.listing(tid)).json()["transfer_state"], "unknown")
        self.assertEqual((await self.declare(tid, [value])).json()["transfer_state"], "pending")
        uploaded = await self.upload(tid, value)
        self.assertEqual(uploaded.status_code, 200); self.assertEqual(uploaded.json()["status"], "ready")
        self.assertEqual((await self.upload(tid, value)).json(), uploaded.json())
        self.assertEqual((await self.declare(tid, [{**value, "status": "failed", "error": "safe"}])).json()["transfer_state"], "ready")
        content = await self.client.get(f"/api/v1/tasks/{tid}/files/{value['file_id']}/content")
        self.assertEqual(content.content, b"result")
        self.assertEqual(content.headers["cache-control"], "no-store")
        self.assertEqual(content.headers["x-content-type-options"], "nosniff")
        self.assertIn("filename*=UTF-8''", content.headers["content-disposition"])
        detail = await self.detail(tid)
        self.assertEqual(detail["output_files"], [uploaded.json()]); self.assertEqual(detail["status"], "done")
        self.assertEqual(len(list(self.app.state.artifacts.directory.iterdir())), 1)
        self.assertEqual(self.app.state.db.execute("SELECT COUNT(*) FROM v1_inbox_results WHERE task_id=?", (tid,)).fetchone()[0], 1)

    async def test_role_boundaries_and_pending_not_downloadable(self):
        ws, tid = await self.ready_task(); value = entry(); await self.declare(tid, [value])
        for token, expected in (("wrong", 401), (harness.APP_TOKEN, 403), (harness.OTHER_TOKEN, 403)):
            self.assertEqual((await self.declare(tid, [value], token=token)).status_code, expected)
            self.assertEqual((await self.upload(tid, value, token=token)).status_code, expected)
        for token, expected in ((harness.APP_TOKEN, 200), (harness.NODE_TOKEN, 200), (harness.OTHER_TOKEN, 403), ("wrong", 401)):
            response = await self.listing(tid, token)
            self.assertEqual(response.status_code, expected); self.assertEqual(response.headers["cache-control"], "no-store")
        path = f"/api/v1/tasks/{tid}/files/{value['file_id']}/content"
        self.assertEqual((await self.client.get(path)).status_code, 409)
        self.assertEqual((await self.client.post(f"/api/v1/tasks/{tid}/files/retry", json={}, headers={"Authorization": "Bearer " + harness.NODE_TOKEN})).status_code, 403)
        self.assertEqual((await self.upload(tid, entry())).status_code, 404)

    async def test_immutable_metadata_order_and_cross_task_ids(self):
        ws, tid = await self.ready_task(); first, second = entry(), entry(name="b.txt")
        await self.declare(tid, [first, second])
        for values in ([second, first], [first], [{**first, "filename": "changed.txt"}, second]):
            self.assertEqual((await self.declare(tid, values)).status_code, 409)
        response, _ = await self.create(); other = response.json()["task_id"]
        await self.send(ws, {"type": "heartbeat", "running": []}); await self.recv(ws, "task_assign"); await self.start(ws, other)
        self.assertEqual((await self.declare(other, [first])).status_code, 409)
        self.assertEqual((await self.declare(other, [entry()] * 5)).status_code, 422)
        self.assertEqual((await self.declare(other, [{**entry(), "status": "ready"}])).status_code, 422)

    async def test_integrity_mime_size_and_atomic_failure(self):
        ws, tid = await self.ready_task(); value = entry(); await self.declare(tid, [value])
        self.assertEqual((await self.upload(tid, value, b"changed")).status_code, 422)
        self.assertEqual((await self.upload(tid, value, mime="application/pdf")).status_code, 415)
        self.assertEqual((await self.upload(tid, value, b"x" * (MAX_BYTES + 1))).status_code, 413)
        with patch("hub.gateway.artifacts.os.replace", side_effect=OSError("test failure")):
            self.assertEqual((await self.upload(tid, value)).status_code, 507)
        self.assertEqual((await self.listing(tid)).json()["files"][0]["status"], "pending")
        self.assertEqual(list(self.app.state.artifacts.directory.iterdir()), [])

    async def test_streaming_limit_quota_and_private_store_symlink(self):
        ws, tid = await self.ready_task(); value = entry(); await self.declare(tid, [value])
        async def chunks():
            for _ in range(257):
                yield b"x" * 65536
        path = f"/api/v1/tasks/{tid}/files/{value['file_id']}"
        response = await self.client.put(path, content=chunks(), headers={"Authorization": "Bearer " + harness.NODE_TOKEN, "Content-Type": "text/plain"})
        self.assertEqual(response.status_code, 413)
        self.app.state.artifacts.max_bytes = 2
        self.assertEqual((await self.upload(tid, value)).status_code, 507)
        self.app.state.artifacts.max_bytes = 100
        outside = self.directory / "outside"; outside.write_bytes(b"keep")
        (self.app.state.artifacts.directory / value["file_id"]).symlink_to(outside)
        self.assertEqual((await self.upload(tid, value)).status_code, 503)
        self.assertEqual(outside.read_bytes(), b"keep")

    async def test_empty_failed_partial_and_none_states(self):
        ws, tid = await self.ready_task()
        self.assertEqual((await self.declare(tid, [], "采集失败")).json()["transfer_state"], "failed")
        self.assertEqual((await self.declare(tid, [], "")).json()["transfer_state"], "none")
        response, _ = await self.create(); other = response.json()["task_id"]
        await self.send(ws, {"type": "heartbeat", "running": []}); await self.recv(ws, "task_assign"); await self.start(ws, other)
        first, second = entry(), {**entry(name="b.txt"), "status": "failed", "error": "未传回"}
        await self.declare(other, [first, second]); await self.upload(other, first)
        self.assertEqual((await self.listing(other)).json()["transfer_state"], "partial")

    async def test_collection_note_keeps_partial_when_accepted_files_are_ready(self):
        ws, tid = await self.ready_task(); value = entry()
        await self.declare(tid, [value], "另一个成果文件采集失败")
        await self.upload(tid, value)
        listing = (await self.listing(tid)).json()
        self.assertEqual(listing["transfer_state"], "partial")
        self.assertEqual(listing["files"][0]["status"], "ready")
        self.assertIn("失败", listing["note"])

    async def test_retry_persisted_offline_and_same_manifest_clears_request(self):
        ws, tid = await self.ready_task(); value = {**entry(), "status": "failed", "error": "未传回"}
        await self.declare(tid, [value]); await ws.close(); await asyncio.sleep(.03)
        path = f"/api/v1/tasks/{tid}/files/retry"
        for _ in range(2):
            self.assertEqual((await self.client.post(path, json={})).json(), {"recorded": True, "node_online": False})
        reopened = init_v1_db(self.directory / "hub.db")
        self.assertTrue(artifacts.list_files(reopened, tid)["retry_requested"]); reopened.close()
        await self.declare(tid, [value]); self.assertFalse((await self.listing(tid)).json()["retry_requested"])
        self.assertEqual((await self.detail(tid))["status"], "done")
        self.assertEqual(self.app.state.db.execute("SELECT COUNT(*) FROM v1_tasks").fetchone()[0], 1)

    async def test_corrupt_ready_file_explicit_retry_can_repair_same_bytes(self):
        ws, tid = await self.ready_task(); value = entry(); await self.declare(tid, [value]); await self.upload(tid, value)
        path = f"/api/v1/tasks/{tid}/files/{value['file_id']}/content"
        (self.app.state.artifacts.directory / value["file_id"]).write_bytes(b"damaged")
        self.assertEqual((await self.client.get(path)).status_code, 503)
        self.assertTrue((await self.client.post(f"/api/v1/tasks/{tid}/files/retry", json={})).json()["recorded"])
        await self.upload(tid, value); self.assertEqual((await self.client.get(path)).content, b"result")

    async def test_non_regular_stored_blob_is_rejected_without_blocking(self):
        ws, tid = await self.ready_task(); value = entry(); await self.declare(tid, [value]); await self.upload(tid, value)
        stored = self.app.state.artifacts.directory / value["file_id"]
        stored.unlink(); os.mkfifo(stored, 0o600)
        response = await asyncio.wait_for(self.client.get(f"/api/v1/tasks/{tid}/files/{value['file_id']}/content"), 1)
        self.assertEqual(response.status_code, 503)

    async def test_capabilities_enable_assignment_and_reconnect_retry_message(self):
        ws, tid = await self.ready_task(); await self.declare(tid, [entry()]); await ws.close(); await asyncio.sleep(.02)
        await self.client.post(f"/api/v1/tasks/{tid}/files/retry", json={})
        ws = await websockets.connect(f"ws://127.0.0.1:{self.port}/ws/node", additional_headers={"Authorization": "Bearer " + harness.NODE_TOKEN})
        self.connections.append(ws)
        await ws.send(json.dumps({"type": "hello", "protocol": 1, "boot_id": "files-test", "capabilities": ["output_files_v1", "project_context_v1"],
            "projects": [{"project_id": "p-accept", "path": str(self.directory), "available": True}], "tasks": []}))
        await self.recv(ws, "hello_ack"); await self.recv(ws, "task_snapshot")
        await self.send(ws, {"type": "heartbeat", "running": []}); await self.recv(ws, "ready")
        self.assertEqual((await self.recv(ws, "task_files_retry"))["task_id"], tid)
        response, _ = await self.create(); assigned = await self.recv(ws, "task_assign")
        self.assertEqual(assigned["output_files_version"], 1)
        self.assertIn("project_context_v1", self.app.state.nodes.get("mac-a").capabilities)

    async def test_queued_manifest_and_unknown_retry_are_rejected(self):
        await self.open_node(); response, _ = await self.create(); tid = response.json()["task_id"]
        self.assertEqual((await self.declare(tid, [entry()])).status_code, 409)
        self.assertEqual((await self.client.post(f"/api/v1/tasks/{tid}/files/retry", json={})).status_code, 409)
        self.assertEqual((await self.client.post(f"/api/v1/tasks/{tid}/files/manifest", content="{" ,headers={"Authorization": "Bearer " + harness.NODE_TOKEN})).status_code, 400)
        self.assertEqual((await self.declare(tid, [], "中" * 342)).status_code, 422)


if __name__ == "__main__":
    unittest.main(verbosity=2)
