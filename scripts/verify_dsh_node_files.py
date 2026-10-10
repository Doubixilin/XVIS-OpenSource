"""No-model file staging/recovery: isolated fake DSH process and HTTP transport."""
from __future__ import annotations
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from node.dsh_agent import DSHNode
from node import dsh_artifacts as files
from shared.file_contract import FileError, describe, metadata

FAKE = r'''
import json,os,sys
from pathlib import Path
def emit(value):print(json.dumps(value,ensure_ascii=False),flush=True)
prompt=sys.stdin.read();home=Path(os.environ['DSH_HOME']);home.mkdir(exist_ok=True)
starts=home/'starts';starts.write_text(starts.read_text()+'x' if starts.exists() else 'x')
(home/'prompt').write_text(prompt)
emit({'type':'session','sessionId':'files-session-fake','cwd':os.getcwd()})
if prompt.startswith('output'):
    Path('result.txt').write_bytes('固定成果57381'.encode());final='做了什么：整理资料\n成果：[说明](result.txt)\n实际检查：文本内容\n剩余：无'
else:final='文字回答'
reason={'kind':'error','error':{'message':'fake error'}} if prompt.startswith('output-fail') else {'kind':'completed'}
emit({'type':'status','phase':'turn_end','reason':reason});emit({'type':'final','text':final})
sys.exit(1 if reason['kind']=='error' else 0)
'''


class Socket:
    def __init__(self):
        self.messages = []
    async def send(self, raw):
        self.messages.append(json.loads(raw))
    async def close(self):
        pass


class FileNodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="xvis-files-node-")
        self.base = Path(self.temp.name).resolve(); self.project = self.base / "project"; self.project.mkdir()
        fake = self.base / "fake.py"; fake.write_text(FAKE)
        self.cfg = {"node_bin": sys.executable, "dsh_cli": str(fake), "dsh_home": str(self.base / "home"),
            "journal_path": str(self.base / "journal.db"), "log_dir": str(self.base / "runs"),
            "runtime_version": {"dsh": "0.2.1-alpha.1", "node": "v24.21.0"},
            "projects": {"p": {"path": str(self.project), "realpath": str(self.project)}},
            "default_timeout_sec": 15, "node_token": "fixture-node-files-token-" + "a" * 40,
            "hub_ws_url": "ws://127.0.0.1:18998/xvis/ws/node"}
        self.node = DSHNode(self.cfg); self.socket = Socket(); self.node.ws, self.node.ready = self.socket, True
        self.runtime_patch = patch("node.dsh_agent.runtime_version", return_value=self.cfg["runtime_version"]); self.runtime_patch.start()
        self.requests, self.stored, self.manifests = [], {}, {}
        self.upload_status, self.manifest_status, self.bad_ack = 200, 200, False
        self.wait_upload = None
        original = httpx.AsyncClient
        def client(**kwargs):
            self.assertIs(kwargs["trust_env"], False); self.assertIs(kwargs["follow_redirects"], False)
            return original(transport=httpx.MockTransport(self.handler), **kwargs)
        self.http_patch = patch("node.dsh_artifacts.httpx.AsyncClient", side_effect=client); self.http_patch.start()

    async def asyncTearDown(self):
        await self.node.close(); self.http_patch.stop(); self.runtime_patch.stop(); self.temp.cleanup()

    async def handler(self, request):
        self.assertEqual(request.headers["authorization"], "Bearer " + self.cfg["node_token"])
        self.assertEqual(request.url.host, "127.0.0.1"); self.assertTrue(request.url.path.startswith("/xvis/api/v1/tasks/"))
        self.requests.append(request)
        if request.method == "POST":
            value = json.loads(await request.aread())
            self.assertEqual(set(value), {"files", "note"})
            for item in value["files"]:
                self.assertIn(item["status"], {"pending", "failed"}); metadata(item)
            self.manifests[request.url.path] = value
            entries = [{**v, "status": "ready" if v["file_id"] in self.stored else v["status"]} for v in value["files"]]
            if self.bad_ack and entries:
                entries[0]["sha256"] = "0" * 64
            return httpx.Response(self.manifest_status, json={"files": entries, "transfer_state": "ready" if entries else "none", "note": value["note"], "retry_requested": False})
        if self.wait_upload:
            await self.wait_upload.wait()
        ident = request.url.path.rsplit("/", 1)[1]
        body = await request.aread()
        declaration = next(v for manifest in self.manifests.values() for v in manifest["files"] if v["file_id"] == ident)
        if self.upload_status == 200:
            self.stored[ident] = body
        return httpx.Response(self.upload_status, json={**metadata(declaration), "status": "ready", "error": None},
            headers={"Location": "https://elsewhere.invalid/private"})

    async def run_task(self, task_id="files-test", prompt="output", **fields):
        await self.node.assign({"task_id": task_id, "project_id": "p", "prompt": prompt, "output_files_version": 1, **fields})
        if self.node.worker:
            await asyncio.wait_for(self.node.worker, 5)
        if self.node.file_worker:
            await asyncio.wait_for(self.node.file_worker, 5)
        return self.node.journal.get(task_id)

    def starts(self):
        path = self.base / "home" / "starts"
        return path.read_text() if path.exists() else ""

    async def test_success_real_subprocess_prompt_manifest_fixed_copy(self):
        task = await self.run_task()
        self.assertEqual(task["status"], "done"); self.assertEqual(self.starts(), "x")
        self.assertIn("[成果文件交付约定]", (self.base / "home" / "prompt").read_text())
        value = task["output_files"][0]; self.assertEqual(value["status"], "uploaded")
        self.assertEqual(self.stored[value["file_id"]], "固定成果57381".encode())
        self.assertEqual(value["sha256"], hashlib.sha256(self.stored[value["file_id"]]).hexdigest())
        staged = self.base / "runs" / task["task_id"] / "output-files" / value["file_id"]
        self.assertEqual(staged.stat().st_mode & 0o777, 0o600)
        self.assertEqual([r.method for r in self.requests], ["POST", "PUT", "POST"])
        self.assertEqual(next(m for m in self.socket.messages if m["type"] == "task_done")["status"], "done")

    async def test_old_assignment_has_no_new_http_behavior(self):
        await self.node.assign({"task_id": "old", "project_id": "p", "prompt": "output"})
        await self.node.worker
        task = self.node.journal.get("old")
        self.assertEqual(task["status"], "done"); self.assertNotIn("output_files", task)
        self.assertEqual(self.requests, []); self.assertNotIn("[成果文件交付约定]", (self.base / "home" / "prompt").read_text())

    async def test_failed_upload_done_unchanged_retry_uses_original_bytes(self):
        self.upload_status = 503; task = await self.run_task()
        self.assertEqual(task["status"], "done"); self.assertEqual(task["output_files"][0]["status"], "failed")
        self.assertIn("成果文件", task["files_note"])
        stable = metadata(task["output_files"][0]); (self.project / "result.txt").write_text("later edit")
        self.upload_status = 200; await self.node.dispatch({"type": "ready"}); await self.node.file_worker
        current = self.node.journal.get(task["task_id"])
        self.assertEqual(metadata(current["output_files"][0]), stable)
        self.assertEqual(self.stored[stable["file_id"]], "固定成果57381".encode()); self.assertEqual(self.starts(), "x")
        self.assertEqual(current["files_note"], "")

    async def test_restart_retry_journal_never_relaunches_execution(self):
        self.upload_status = 500; task = await self.run_task()
        stable = metadata(task["output_files"][0]); await self.node.close()
        (self.project / "result.txt").unlink()
        self.node = DSHNode(self.cfg); self.node.ws = self.socket; await self.node.recover()
        self.assertIn(task["task_id"], [t["task_id"] for t in self.node.hello_tasks()])
        self.upload_status = 200; await self.node.dispatch({"type": "ready"}); await self.node.file_worker
        self.assertEqual(self.starts(), "x"); self.assertEqual(self.stored[stable["file_id"]], "固定成果57381".encode())
        self.assertEqual(self.node.journal.get(task["task_id"])["status"], "done")

    async def test_corrupt_private_copy_is_not_uploaded(self):
        self.upload_status = 500; task = await self.run_task(); ident = task["output_files"][0]["file_id"]
        (self.base / "runs" / task["task_id"] / "output-files" / ident).write_bytes(b"wrong")
        self.upload_status = 200; prior = len([r for r in self.requests if r.method == "PUT"])
        await self.node.dispatch({"type": "task_files_retry", "task_id": task["task_id"]}); await self.node.file_worker
        self.assertEqual(len([r for r in self.requests if r.method == "PUT"]), prior)
        self.assertEqual(self.node.journal.get(task["task_id"])["output_files"][0]["status"], "failed")
        self.assertEqual(self.starts(), "x")

    async def test_manifest_failure_wrong_ack_redirect_no_upload_or_forward(self):
        for index, changes in enumerate(({"manifest_status": 302}, {"manifest_status": 200, "bad_ack": True})):
            for key, value in changes.items():
                setattr(self, key, value)
            task = await self.run_task(task_id=f"manifest-{index}")
            self.assertEqual(task["status"], "done"); self.assertFalse(task["file_manifest_uploaded"])
        self.assertFalse(any(r.method == "PUT" for r in self.requests))
        self.assertTrue(all(r.url.host == "127.0.0.1" for r in self.requests))

    async def test_failed_execution_does_not_export_files(self):
        task = await self.run_task(prompt="output-fail")
        self.assertEqual(task["status"], "failed"); self.assertNotIn("output_files", task); self.assertEqual(self.requests, [])

    async def test_invalid_file_version_rejected_before_model(self):
        for index, version in enumerate((True, 2, "1", None)):
            await self.node.assign({"task_id": f"invalid-{index}", "project_id": "p", "prompt": "x", "output_files_version": version})
            self.assertIsNone(self.node.journal.get(f"invalid-{index}"))
        self.assertEqual(self.starts(), "")

    async def test_no_output_manifest_distinguishes_none_from_unknown(self):
        task = await self.run_task(prompt="plain")
        self.assertEqual(task["output_files"], []); self.assertTrue(task["file_manifest_uploaded"])
        self.assertEqual(task["file_collection_note"], ""); self.assertEqual(len(self.requests), 2)

    async def test_recovery_missing_frozen_records_never_rereads_workspace(self):
        task = await self.run_task(); task.pop("output_files"); self.node.journal.put(task)
        (self.project / "result.txt").write_text("later edit")
        await self.node.recover(); current = self.node.journal.get(task["task_id"])
        self.assertEqual(current["output_files"], []); self.assertIn("未固定", current["file_collection_note"])
        self.assertEqual(self.starts(), "x")

    async def test_explicit_retry_uploaded_copy_and_inflight_retry_are_serial(self):
        task = await self.run_task(); ident = task["output_files"][0]["file_id"]
        self.wait_upload = asyncio.Event()
        await self.node.dispatch({"type": "task_files_retry", "task_id": task["task_id"]})
        for _ in range(50):
            if len([r for r in self.requests if r.method == "PUT"]) >= 2:
                break
            await asyncio.sleep(.01)
        await self.node.dispatch({"type": "task_files_retry", "task_id": task["task_id"]})
        self.wait_upload.set(); await self.node.file_worker
        self.assertEqual(len([r for r in self.requests if r.method == "PUT"]), 3)
        self.assertEqual(self.starts(), "x"); self.assertEqual(self.stored[ident], "固定成果57381".encode())

    async def test_markdown_parser_code_images_references_and_declaration_order(self):
        text = '[一](a.txt)\n![图](image.png)\n`[code](code.txt)`\n```md\n[example](example.txt)\n```\n' \
            '[二][r]\n[r]: <with space/b(c).md> "title"\n[文档](https://example.org/doc)\n~~~\n[unclosed](no.txt)'
        self.assertEqual(files.markdown_files(text), ["a.txt", "with space/b(c).md", "https://example.org/doc"])
        self.assertIsNone(files.local_file(str(self.project), "https://example.org/doc"))

    async def test_collection_only_declared_dedup_four_limit_and_unknown_format(self):
        await self.node.assign({"task_id": "collect", "project_id": "p", "prompt": "plain"}); await self.node.worker
        for index in range(6):
            (self.project / f"{index}.txt").write_text(str(index))
        files.collect_outputs(self.cfg, self.node.journal, "collect", "[a](0.txt)\n[a](0.txt)\n" + "\n".join(f"[x]({i}.txt)" for i in range(1, 6)))
        task = self.node.journal.get("collect")
        self.assertEqual([f["filename"] for f in task["output_files"]], [f"{i}.txt" for i in range(4)])
        self.assertIn("四件", task["files_note"])
        (self.project / "secret.env").write_text("not deliverable")
        files.collect_outputs(self.cfg, self.node.journal, "collect", "[other](secret.env)")
        self.assertEqual(self.node.journal.get("collect")["output_files"], task["output_files"])

    async def test_paths_identity_symlinks_regular_file_and_url_prefix(self):
        outside = self.base / "outside.txt"; outside.write_text("keep")
        (self.project / "link.txt").symlink_to(outside)
        (self.project / "real.txt").write_text("inside")
        (self.project / "dir-link").symlink_to(self.project, target_is_directory=True)
        for path in ("link.txt", "dir-link/real.txt", "../outside.txt", str(outside), "real.txt?token=x", "real.txt#x"):
            with self.subTest(path=path), self.assertRaises((FileError, OSError, ValueError)):
                files.local_file(str(self.project), path)
        with self.assertRaises(FileError):
            files.read_workspace(str(self.project), "real.txt", [0, 0])
        with self.assertRaises((FileError, OSError)):
            files.read_workspace(str(self.project), "dir-link/real.txt")
        self.assertEqual(files.file_url(self.cfg, "task", "manifest"), "http://127.0.0.1:18998/xvis/api/v1/tasks/task/files/manifest")
        for url in ("ws://public.example/ws/node", "wss://host/x/../ws/node", "wss://host/ws/node?token=x"):
            with self.assertRaises(ValueError):
                files.file_url({**self.cfg, "hub_ws_url": url}, "task", "manifest")


if __name__ == "__main__":
    unittest.main(verbosity=2)
