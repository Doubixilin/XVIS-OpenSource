"""共享助理资料：临时 SQLite、loopback Hub 与假节点，无模型或生产连接。"""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import uvicorn
from pydantic import ValidationError

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from hub.gateway import assistant_profile, v1_repo as repo
from hub.gateway.app import create_app
from scripts import verify_dsh_hub as harness
from shared.v1_db import SCHEMA, init_v1_db
from shared.v1_schemas import AssistantProfile


def profile(**fields) -> dict:
    return {"revision": 0, "name": "XVIS", "persona_enabled": False, "persona": "",
            "memory_enabled": False, "memories": [], **fields}


def memory(number: int = 1, text: str = "偏好简洁回答", enabled: bool = True) -> dict:
    return {"id": "m-" + f"{number:032x}", "text": text, "enabled": enabled}


class ProfileSchemaTests(unittest.TestCase):
    def test_exact_string_byte_limits_and_unicode_preservation(self):
        data = profile(name="😀" * 40, persona="😀" * 1024,
                       memories=[memory(text="😀" * 512)])
        self.assertEqual(AssistantProfile.model_validate(data).model_dump(), data)
        for fields in ({"name": "x" * 41}, {"persona": "😀" * 1024 + "x"},
                       {"memories": [memory(text="😀" * 512 + "x")]}):
            with self.subTest(fields=list(fields)):
                with self.assertRaises(ValidationError):
                    AssistantProfile.model_validate(profile(**fields))

    def test_empty_values_invalid_unicode_and_memory_ids(self):
        invalid = ({"name": ""}, {"name": "  "}, {"name": "\ud800"},
                   {"persona": "\ud800"}, {"memories": [memory(text=" ")]},
                   {"memories": [memory(text="")]}, {"memories": [memory(text="\ud800")]},
                   {"memories": [{**memory(), "id": "m-" + "A" * 32}]},
                   {"memories": [{**memory(), "id": "../memory"}]})
        for fields in invalid:
            with self.subTest(fields=repr(fields)):
                with self.assertRaises(ValidationError):
                    AssistantProfile.model_validate(profile(**fields))

    def test_strict_types_required_and_unknown_fields(self):
        for fields in ({"revision": -1}, {"revision": True}, {"revision": "0"},
                       {"persona_enabled": 1}, {"memory_enabled": "false"},
                       {"persona": None}, {"memories": None},
                       {"memories": [{**memory(), "enabled": "true"}]},
                       {"memories": [{**memory(), "permissions": "all"}]},
                       {"permissions": "all"}):
            with self.subTest(fields=repr(fields)):
                with self.assertRaises(ValidationError):
                    AssistantProfile.model_validate(profile(**fields))
        for field in profile():
            data = profile()
            del data[field]
            with self.subTest(missing=field):
                with self.assertRaises(ValidationError):
                    AssistantProfile.model_validate(data)

    def test_count_uniqueness_and_total_json_budget(self):
        valid = profile(memories=[memory(i, "x") for i in range(20)])
        self.assertEqual(len(AssistantProfile.model_validate(valid).memories), 20)
        for entries in ([memory(i, "x") for i in range(21)], [memory(), memory()],
                        [memory(i, "x" * 2048) for i in range(8)]):
            with self.subTest(count=len(entries)):
                with self.assertRaises(ValidationError):
                    AssistantProfile.model_validate(profile(memories=entries))
        # JSON escaping is part of the stored and transported aggregate budget.
        with self.assertRaises(ValidationError):
            AssistantProfile.model_validate(profile(persona="\n" * 4096,
                                                     memories=[memory(i, "\n" * 2048) for i in range(2)]))


class ProfilePersistenceTests(unittest.TestCase):
    def test_restart_preserves_profile_revision_and_disabled_text(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hub.db"
            conn = init_v1_db(path)
            self.assertEqual(assistant_profile.read_profile(conn), profile())
            data = profile(name="玉青", persona="仔细且直接", memories=[memory()])
            result = assistant_profile.write_profile(conn, AssistantProfile.model_validate(data))
            self.assertEqual(result, {**data, "revision": 1})
            self.assertEqual(assistant_profile.task_snapshot(conn), {"name": "玉青", "persona": "", "memories": []})
            conn.close()
            conn = init_v1_db(path)
            self.assertEqual(assistant_profile.read_profile(conn), result)
            enabled = {**result, "persona_enabled": True, "memory_enabled": True}
            self.assertEqual(assistant_profile.write_profile(conn, AssistantProfile.model_validate(enabled))["revision"], 2)
            self.assertEqual(assistant_profile.task_snapshot(conn), {"name": "玉青", "persona": "仔细且直接", "memories": ["偏好简洁回答"]})
            conn.close()

    def test_stale_revision_does_not_write_from_another_connection(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "hub.db"
            first, second = init_v1_db(path), init_v1_db(path)
            saved = assistant_profile.write_profile(first, AssistantProfile.model_validate(profile(name="先保存")))
            before = tuple(first.execute("SELECT * FROM v1_assistant_profile").fetchone())
            with self.assertRaises(repo.RepoError) as conflict:
                assistant_profile.write_profile(second, AssistantProfile.model_validate(profile(name="未保存稿")))
            self.assertEqual((conflict.exception.status, conflict.exception.code), (409, "profile_conflict"))
            self.assertEqual(tuple(first.execute("SELECT * FROM v1_assistant_profile").fetchone()), before)
            self.assertEqual(assistant_profile.read_profile(second), saved)
            first.close()
            second.close()

    def test_legacy_database_additive_migration_preserves_rows_and_fingerprint(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "old.db"
            legacy = sqlite3.connect(path)
            legacy.executescript(SCHEMA.replace(",\n assistant_context_json TEXT", "").split("CREATE TABLE IF NOT EXISTS v1_assistant_profile")[0])
            request = {"client_request_id": "old-request", "node_id": "mac-a", "project_id": "p", "prompt": "旧原文"}
            fingerprint = hashlib.sha256(repo.dump({"operation": "create", **request}).encode()).hexdigest()
            legacy.execute("INSERT INTO v1_tasks(task_id,client_request_id,request_hash,status,node_id,project_id,prompt,timeout_sec,dsh_session_id,created_at) VALUES(?,?,?,'done',?,?,?,30,'old-session','old-date')",
                           ("t-old", "old-request", fingerprint, "mac-a", "p", "旧原文"))
            legacy.execute("CREATE TABLE private_legacy(value TEXT)")
            legacy.execute("INSERT INTO private_legacy VALUES('keep')")
            legacy.commit()
            before = tuple(legacy.execute("SELECT * FROM v1_tasks").fetchone())
            legacy.close()
            conn = init_v1_db(path)
            row = conn.execute("SELECT * FROM v1_tasks").fetchone()
            self.assertEqual(tuple(row)[:len(before)], before)
            self.assertEqual(row["execution_mode"], "headless")
            self.assertIsNone(row["assistant_context_json"])
            self.assertEqual(conn.execute("SELECT value FROM private_legacy").fetchone()[0], "keep")
            self.assertEqual(assistant_profile.read_profile(conn), profile())
            self.assertEqual(repo.task_context(dict(row)), {})
            self.assertEqual(repo.task_view(conn, dict(row))["assistant_name"], "XVIS")
            task, fresh = repo.create_task(conn, "old-request", repo.request_fingerprint("create", {**request, "image_ids": []}), "mac-a", "p", "旧原文", 30,
                                           assistant_context={"name": "新资料", "persona": "", "memories": []})
            self.assertFalse(fresh)
            self.assertEqual(task["task_id"], "t-old")
            self.assertEqual(repo.task_context(task), {})
            conn.close()

    def test_task_snapshot_is_atomic_and_does_not_change_original_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            conn = init_v1_db(Path(temp) / "hub.db")
            context = {"name": "助理", "persona": "表达偏好", "memories": ["参考背景"]}
            task, fresh = repo.create_task(conn, "one", "hash", "mac-a", "p", "  原文\n ", 30, assistant_context=context)
            self.assertTrue(fresh)
            self.assertEqual(task["prompt"], "  原文\n ")
            self.assertEqual(repo.task_context(task), context)
            with self.assertRaises(repo.RepoError):
                repo.create_task(conn, "bad", "other", "mac-a", "p", "x", 30,
                                 image_ids=["i-" + "0" * 32], assistant_context=context)
            self.assertIsNone(conn.execute("SELECT task_id FROM v1_tasks WHERE client_request_id='bad'").fetchone())
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM v1_events").fetchone()[0], 1)
            conn.close()


class ProfileIntegrationTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = harness.HubIntegrationTests.asyncSetUp
    asyncTearDown = harness.HubIntegrationTests.asyncTearDown
    open_node = harness.HubIntegrationTests.open_node
    recv = harness.HubIntegrationTests.recv
    send = harness.HubIntegrationTests.send
    create = harness.HubIntegrationTests.create
    detail = harness.HubIntegrationTests.detail
    start = harness.HubIntegrationTests.start
    done = harness.HubIntegrationTests.done

    async def put(self, data):
        response = await self.client.put("/api/v1/assistant-profile", json=data)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    async def test_default_profile_auth_and_role_boundaries(self):
        response = await self.client.get("/api/v1/assistant-profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), profile())
        self.assertEqual(response.headers["cache-control"], "no-store")
        for method in ("get", "put"):
            for token, status in (("wrong", 401), (harness.NODE_TOKEN, 403), (harness.OTHER_TOKEN, 403)):
                kwargs = {"headers": {"Authorization": f"Bearer {token}"}}
                if method == "put":
                    kwargs["json"] = profile()
                result = await getattr(self.client, method)("/api/v1/assistant-profile", **kwargs)
                self.assertEqual(result.status_code, status)
        self.assertEqual((await self.client.get("/api/v1/assistant-profile")).json(), profile())

    async def test_validation_and_json_errors_do_not_write(self):
        for data in ({**profile(), "unfrozen_field": True}, {**profile(), "revision": "0"},
                     profile(persona="中" * 1366), profile(memories=[memory(), memory()]),
                     {"revision": 0}):
            response = await self.client.put("/api/v1/assistant-profile", json=data)
            self.assertEqual(response.status_code, 422, response.text)
        too_large = await self.client.put("/api/v1/assistant-profile", json=profile(memories=[memory(i, "x" * 2048) for i in range(8)]))
        self.assertEqual(too_large.status_code, 422)
        self.assertEqual(too_large.json()["error"]["code"], "profile_too_large")
        malformed = await self.client.put("/api/v1/assistant-profile", content="{", headers={"Content-Type": "application/json"})
        self.assertEqual(malformed.status_code, 400)
        huge = await self.client.put("/api/v1/assistant-profile", content=b" " * 65537, headers={"Content-Type": "application/json"})
        self.assertEqual(huge.status_code, 400)
        self.assertEqual(huge.json()["error"]["code"], "body_too_large")
        self.assertEqual((await self.client.get("/api/v1/assistant-profile")).json(), profile())

    async def test_parallel_revision_conflict_keeps_winning_edit(self):
        responses = await asyncio.gather(self.client.put("/api/v1/assistant-profile", json=profile(name="甲")),
                                         self.client.put("/api/v1/assistant-profile", json=profile(name="乙")))
        self.assertEqual(sorted(response.status_code for response in responses), [200, 409])
        winner = next(response.json() for response in responses if response.status_code == 200)
        loser = next(response.json() for response in responses if response.status_code == 409)
        self.assertEqual(loser["error"]["code"], "profile_conflict")
        self.assertEqual(winner["revision"], 1)
        self.assertEqual((await self.client.get("/api/v1/assistant-profile")).json(), winner)

    async def test_server_restart_keeps_full_profile(self):
        saved = await self.put(profile(name="本人的助理", persona="保留偏好", memories=[memory(enabled=False)]))
        self.server.should_exit = True
        await asyncio.wait_for(self.server_task, 5)
        self.app = create_app(str(self.config_path))
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port,
                                                   log_level="error", lifespan="on", ws="websockets"))
        self.server_task = asyncio.create_task(self.server.serve())
        for _ in range(100):
            if self.server.started:
                break
            await asyncio.sleep(0.01)
        self.assertTrue(self.server.started)
        self.assertEqual((await self.client.get("/api/v1/assistant-profile")).json(), saved)

    async def test_root_dispatch_uses_enabled_snapshot_and_task_view_name(self):
        ws, _ = await self.open_node()
        await self.put(profile(name="玉青", persona_enabled=True, persona="直接说明结论",
                               memory_enabled=True, memories=[memory(1, "启用背景"), memory(2, "禁用背景", False)]))
        response, _ = await self.create(prompt="独立原指令")
        self.assertEqual(response.status_code, 201)
        assignment = await self.recv(ws, "task_assign")
        expected = {"name": "玉青", "persona": "直接说明结论", "memories": ["启用背景"]}
        self.assertEqual(assignment["assistant_context"], expected)
        self.assertEqual(assignment["prompt"], "独立原指令")
        self.assertEqual(repo.task_context(repo.require_task(self.app.state.db, assignment["task_id"])), expected)
        detail = await self.detail(assignment["task_id"])
        self.assertEqual((detail["assistant_name"], detail["prompt"]), ("玉青", "独立原指令"))
        page = (await self.client.get("/api/v1/tasks")).json()
        self.assertEqual(page["tasks"][0]["assistant_name"], "玉青")

    async def test_disabled_contents_preserved_but_new_task_context_is_empty(self):
        ws, _ = await self.open_node()
        saved = await self.put(profile(name="私用助理", persona="暂不启用", memories=[memory()]))
        response, _ = await self.create()
        self.assertEqual(response.status_code, 201)
        assignment = await self.recv(ws, "task_assign")
        self.assertEqual(assignment["assistant_context"], {"name": "私用助理", "persona": "", "memories": []})
        self.assertEqual((await self.client.get("/api/v1/assistant-profile")).json(), saved)

    async def test_profile_changes_only_new_roots_and_followup_inherits_root(self):
        ws, _ = await self.open_node()
        original = await self.put(profile(name="原助理", persona_enabled=True, persona="原偏好",
                                          memory_enabled=True, memories=[memory()]))
        response, _ = await self.create()
        tid = response.json()["task_id"]
        first = await self.recv(ws, "task_assign")
        await self.start(ws, tid)
        await self.done(ws, tid)
        updated = await self.put(profile(revision=original["revision"], name="新助理", persona_enabled=True,
                                         persona="新偏好", memory_enabled=True, memories=[memory(2, "新记忆")]))
        await self.send(ws, {"type": "heartbeat", "running": []})
        followup = await self.client.post(f"/api/v1/tasks/{tid}/followup", json={"client_request_id": "follow-one", "message": "继续"})
        self.assertEqual(followup.status_code, 201)
        second = await self.recv(ws, "task_assign")
        self.assertEqual(second["assistant_context"], first["assistant_context"])
        self.assertEqual(second["prompt"], "继续")
        self.assertEqual(second["followup_of"]["task_id"], tid)
        child = second["task_id"]
        await self.start(ws, child)
        await self.done(ws, child)
        await self.send(ws, {"type": "heartbeat", "running": []})
        next_followup = await self.client.post(f"/api/v1/tasks/{child}/followup", json={"client_request_id": "follow-two", "message": "再继续"})
        self.assertEqual(next_followup.status_code, 201)
        third = await self.recv(ws, "task_assign")
        self.assertEqual(third["assistant_context"], first["assistant_context"])
        self.assertEqual((await self.detail(third["task_id"]))["assistant_name"], "原助理")
        new_root, _ = await self.create()
        self.assertEqual(new_root.status_code, 201)
        stored = repo.require_task(self.app.state.db, new_root.json()["task_id"])
        self.assertEqual(repo.task_context(stored), {"name": "新助理", "persona": "新偏好", "memories": ["新记忆"]})
        self.assertEqual((await self.client.get("/api/v1/assistant-profile")).json(), updated)

    async def test_existing_request_retry_returns_original_before_reading_profile(self):
        ws, _ = await self.open_node()
        saved = await self.put(profile(name="旧助理"))
        response, body = await self.create(request_id="retry", prompt="原文")
        assignment = await self.recv(ws, "task_assign")
        await self.put(profile(revision=saved["revision"], name="新助理"))
        with patch.object(assistant_profile, "task_snapshot", side_effect=AssertionError("retry must not read profile")):
            retried = await self.client.post("/api/v1/tasks", json=body)
        self.assertEqual(retried.status_code, 200)
        self.assertEqual(retried.json()["task_id"], response.json()["task_id"])
        self.assertEqual((await self.detail(assignment["task_id"]))["assistant_name"], "旧助理")
        conflict = await self.client.post("/api/v1/tasks", json={**body, "prompt": "不同原文"})
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "request_id_conflict")

    async def test_legacy_root_followup_does_not_use_current_profile(self):
        ws, _ = await self.open_node()
        old, _ = repo.create_task(self.app.state.db, "legacy", "hash", "mac-a", "p-accept", "旧原文", 30)
        repo.change_status(self.app.state.db, old, "assigned")
        repo.change_status(self.app.state.db, repo.require_task(self.app.state.db, old["task_id"]), "done", session_id="old-session")
        await self.put(profile(name="新助理", persona_enabled=True, persona="新偏好", memory_enabled=True, memories=[memory()]))
        self.assertEqual((await self.detail(old["task_id"]))["assistant_name"], "XVIS")
        response = await self.client.post(f"/api/v1/tasks/{old['task_id']}/followup", json={"client_request_id": "old-follow", "message": "续接原任务"})
        self.assertEqual(response.status_code, 201)
        assignment = await self.recv(ws, "task_assign")
        self.assertEqual(assignment["assistant_context"], {})
        self.assertEqual(assignment["followup_of"]["dsh_session_id"], "old-session")
        self.assertEqual((await self.detail(assignment["task_id"]))["assistant_name"], "XVIS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
