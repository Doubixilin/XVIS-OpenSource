"""No-model image acceptance: authenticated HTTP mocks and real isolated subprocesses."""
from __future__ import annotations

import asyncio
import json
import struct
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from node.dsh_agent import DSHNode
from node.dsh_images import (ImageError, MAX_BYTES, collect_outputs, describe, download_inputs,
                             image_info, image_url, input_metadata, local_image, markdown_images,
                             read_workspace_image)


def png(red=255):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes((0, red, 0, 0)))) + chunk(b"IEND", b""))


FAKE = r'''
import json,os,sys
from pathlib import Path
def emit(value):print(json.dumps(value,ensure_ascii=False),flush=True)
prompt=sys.stdin.read()
home=Path(os.environ['DSH_HOME']);home.mkdir(exist_ok=True)
starts=home/'starts';starts.write_text(starts.read_text()+'x' if starts.exists() else 'x')
(home/'received-prompt').write_text(prompt)
sid=sys.argv[sys.argv.index('--session-id')+1] if '--session-id' in sys.argv else 'session-image-fake'
emit({'type':'session','sessionId':sid,'cwd':os.getcwd()})
if '[本轮用户上传的图片]' in prompt:
    assert '逐张调用官方 read_image' in prompt
    lines=prompt.split('[本轮用户上传的图片]',1)[1].splitlines()
    for line in lines:
        if line[:1].isdigit() and '. ' in line:
            path=Path(line.split('. ',1)[1]);assert path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
if prompt.startswith('output'):
    source=Path('source.png');Path('result.png').write_bytes(source.read_bytes())
    final='图片结果\n![结果](result.png)'
else:final='文字结果'
reason={'kind':'error','error':{'message':'fake error'}} if prompt.startswith('output-fail') else {'kind':'completed'}
emit({'type':'status','phase':'turn_end','reason':reason})
emit({'type':'final','text':final})
sys.exit(1 if reason['kind']=='error' else 0)
'''


class Socket:
    def __init__(self):self.messages=[]
    async def send(self, raw):self.messages.append(json.loads(raw))
    async def close(self):pass


class ImageNodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xvis-node-images-")
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.payload = png()
        (self.project / "source.png").write_bytes(self.payload)
        fake = self.base / "fake.py"
        fake.write_text(FAKE)
        self.cfg = {"node_bin": sys.executable, "dsh_cli": str(fake), "dsh_home": str(self.base / "home"),
            "journal_path": str(self.base / "journal.sqlite"), "log_dir": str(self.base / "runs"),
            "runtime_version": {"dsh": "0.2.1-alpha.1", "node": "v24.21.0"},
            "projects": {"p": {"path": str(self.project), "realpath": str(self.project)}},
            "default_timeout_sec": 15, "node_token": "test-node-token-" + "a" * 40,
            "hub_ws_url": "https-invalid-for-test"}
        self.cfg["hub_ws_url"] = "ws://127.0.0.1:18999/ws/node"
        self.node = DSHNode(self.cfg)
        self.socket = Socket()
        self.node.ws, self.node.ready = self.socket, True
        self.probe = patch("node.dsh_agent.runtime_version", return_value=self.cfg["runtime_version"])
        self.probe.start()
        self.http_calls = []
        self.upload_status, self.download_status = 200, 200
        self.download_data, self.upload_bad_ack = self.payload, False
        self.download_wait = None
        original_client = httpx.AsyncClient
        transport = httpx.MockTransport(self.http_handler)
        def client(**kwargs):
            self.assertIs(kwargs.get("trust_env"), False)
            self.assertIs(kwargs.get("follow_redirects"), False)
            return original_client(transport=transport, **kwargs)
        self.http_patch = patch("node.dsh_images.httpx.AsyncClient", side_effect=client)
        self.http_patch.start()

    async def asyncTearDown(self):
        await self.node.close()
        self.http_patch.stop()
        self.probe.stop()
        self.tmp.cleanup()

    async def http_handler(self, request):
        self.assertEqual(request.headers.get("Authorization"), "Bearer " + self.cfg["node_token"])
        self.assertEqual(request.url.host, "127.0.0.1")
        self.http_calls.append(request)
        if request.method == "GET":
            if self.download_wait is not None:
                await self.download_wait.wait()
            return httpx.Response(self.download_status, content=self.download_data,
                headers={"Content-Type": "image/png", "Location": "https://other.invalid/private"})
        body = await request.aread()
        image_id = request.url.path.rsplit("/", 1)[1]
        result = describe(body, image_id)
        if self.upload_bad_ack:
            result["sha256"] = "0" * 64
        return httpx.Response(self.upload_status, json=result)

    async def run_task(self, task_id="image-task", prompt="input", **extra):
        await self.node.assign({"task_id": task_id, "project_id": "p", "prompt": prompt, **extra})
        if self.node.worker:
            await asyncio.wait_for(self.node.worker, 15)
        if self.node.image_worker:
            await asyncio.wait_for(self.node.image_worker, 15)
        return self.node.journal.get(task_id)

    async def test_input_transport_integrity_private_files_and_original_prompt(self):
        image = describe(self.payload, "i-" + "1" * 32)
        task = await self.run_task(prompt="请查看图片", images=[image])
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["prompt"], "请查看图片")
        received = (self.base / "home" / "received-prompt").read_text()
        self.assertTrue(received.startswith(task["prompt"] + "\n\n"))
        self.assertIn("不要假装已经看图", received)
        target = self.base / "runs" / task["task_id"] / "input-images" / (image["image_id"] + ".png")
        self.assertEqual(target.read_bytes(), self.payload)
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.http_calls[0].url.path,
            f"/api/v1/tasks/{task['task_id']}/images/{image['image_id']}/content")

    async def test_text_only_task_has_output_contract_and_keeps_original_instruction(self):
        task = await self.run_task(prompt="请截图")
        received = (self.base / "home" / "received-prompt").read_text()
        self.assertEqual(task["prompt"], "请截图")
        self.assertTrue(received.startswith("请截图\n\n"))
        self.assertIn("[结果图片回传约定]", received)
        self.assertNotIn("逐张调用官方 read_image", received)
        self.assertEqual(self.http_calls, [])

    async def test_prefixed_deployment_download_and_upload_keep_same_origin(self):
        self.cfg["hub_ws_url"] = "ws://127.0.0.1:18999/xvis/ws/node"
        image = describe(self.payload, "i-" + "7" * 32)
        task = await self.run_task(prompt="output", images=[image])
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["output_images"][0]["status"], "uploaded")
        self.assertEqual([request.method for request in self.http_calls], ["GET", "PUT"])
        self.assertEqual(self.http_calls[0].url.path,
            f"/xvis/api/v1/tasks/{task['task_id']}/images/{image['image_id']}/content")
        self.assertEqual(self.http_calls[1].url.path,
            f"/xvis/api/v1/tasks/{task['task_id']}/images/{task['output_images'][0]['image_id']}")

    async def test_metadata_rejections_are_before_model_launch(self):
        good = describe(self.payload, "i-" + "2" * 32)
        for index, changes in enumerate(({"image_id": "../secret"}, {"size_bytes": MAX_BYTES + 1},
                                         {"width": True}, {"mime_type": "image/gif"})):
            await self.node.assign({"task_id": f"bad-{index}", "project_id": "p", "prompt": "x",
                "images": [{**good, **changes}]})
            self.assertIsNone(self.node.journal.get(f"bad-{index}"))
        with self.assertRaises(ImageError):input_metadata([good, good])
        with self.assertRaises(ImageError):input_metadata([good] * 5)
        self.assertFalse((self.base / "home" / "starts").exists())

    async def test_corrupt_download_keeps_model_unstarted(self):
        image = describe(self.payload, "i-" + "3" * 32)
        for index, data in enumerate((png(100), self.payload + b"extra")):
            self.download_data = data
            task = await self.run_task(f"corrupt-{index}", images=[image])
            self.assertEqual(task["status"], "failed")
            self.assertTrue(task["not_started"])
            self.assertTrue(task["stop_confirmed"])
            self.assertIn("图片", task["detail"])
        self.assertFalse((self.base / "home" / "starts").exists())

    async def test_redirect_not_followed_or_credential_forwarded(self):
        self.download_status = 302
        task = await self.run_task(images=[describe(self.payload, "i-" + "4" * 32)])
        self.assertEqual(task["status"], "failed")
        self.assertEqual(len(self.http_calls), 1)
        self.assertIn("HTTP 302", task["detail"])

    async def test_cancel_and_timeout_during_download_do_not_start_model(self):
        self.download_wait = asyncio.Event()
        image = describe(self.payload, "i-" + "5" * 32)
        await self.node.assign({"task_id": "cancel-images", "project_id": "p", "prompt": "x", "images": [image]})
        for _ in range(30):
            if self.http_calls:break
            await asyncio.sleep(.01)
        self.node.runner.cancel()
        await asyncio.wait_for(self.node.worker, 2)
        task = self.node.journal.get("cancel-images")
        self.assertEqual(task["status"], "cancelled")
        self.assertTrue(task["not_started"])
        task = await self.run_task("timeout-images", images=[image], timeout_sec=.05)
        self.assertEqual(task["status"], "timed_out")
        self.assertTrue(task["not_started"])
        self.assertFalse((self.base / "home" / "starts").exists())

    async def test_output_failure_text_survives_and_ready_retries_frozen_image(self):
        self.upload_status = 500
        task = await self.run_task(prompt="output")
        self.assertEqual(task["status"], "done")
        self.assertIn("![结果](result.png)", task["final_text"])
        record = task["output_images"][0]
        self.assertEqual(record["status"], "pending")
        self.assertIn("重新连接", self.node.status_message(task)["detail"])
        stable_id = record["image_id"]
        (self.project / "result.png").write_bytes(png(20))
        self.upload_status = 200
        await self.node.dispatch({"type": "ready"})
        await self.node.image_worker
        current = self.node.journal.get(task["task_id"])
        self.assertEqual(current["output_images"][0]["image_id"], stable_id)
        self.assertEqual(current["output_images"][0]["status"], "uploaded")
        self.assertEqual(current["images_note"], "")
        self.assertEqual(self.http_calls[-1].content, self.payload)
        self.assertEqual((self.base / "home" / "starts").read_text(), "x")

    async def test_restart_retry_is_idempotent_and_does_not_restart_model(self):
        self.upload_status = 503
        task = await self.run_task(prompt="output")
        stable_id = task["output_images"][0]["image_id"]
        await self.node.close()
        self.node = DSHNode(self.cfg)
        self.node.ws = self.socket
        await self.node.recover()
        self.assertIn(task["task_id"], [t["task_id"] for t in self.node.hello_tasks()])
        self.upload_status = 200
        await self.node.dispatch({"type": "ready"})
        await self.node.image_worker
        output = self.node.journal.get(task["task_id"])["output_images"][0]
        self.assertEqual(output["status"], "uploaded")
        self.assertEqual(output["image_id"], stable_id)
        self.assertEqual((self.base / "home" / "starts").read_text(), "x")

    async def test_recovery_completes_image_staging_after_model_done_without_rerun(self):
        self.upload_status = 503
        task = await self.run_task(prompt="output")
        task.pop("output_images")
        self.node.journal.put(task)
        await self.node.recover()
        staged = self.node.journal.get(task["task_id"])
        self.assertEqual(staged["status"], "done")
        self.assertEqual(len(staged["output_images"]), 1)
        self.assertEqual((self.base / "home" / "starts").read_text(), "x")

    async def test_wrong_upload_ack_remains_retryable(self):
        self.upload_bad_ack = True
        task = await self.run_task(prompt="output")
        self.assertEqual(task["status"], "done")
        self.assertEqual(task["output_images"][0]["status"], "pending")
        self.assertTrue(task["images_note"])

    async def test_failed_model_does_not_export_images(self):
        task = await self.run_task(prompt="output-fail")
        self.assertEqual(task["status"], "failed")
        self.assertFalse(task.get("output_images"))
        self.assertEqual(self.http_calls, [])

    async def test_markdown_declarations_paths_and_workspace_escape(self):
        folder = self.project / "with space"
        folder.mkdir()
        (folder / "a(b).png").write_bytes(self.payload)
        text = '![a](<with space/a(b).png> "title")\n![a](result.png)\n' \
            '```md\n![sample](source.png)\n```\n`![sample](source.png)`'
        self.assertEqual(markdown_images(text), ["with space/a(b).png", "result.png"])
        task = {"task_id": "collection", "status": "done", "cwd": str(self.project)}
        self.node.journal.put(task)
        outside = self.base / "outside.png"
        outside.write_bytes(self.payload)
        (self.project / "link.png").symlink_to(outside)
        (self.project / "inside-link.png").symlink_to(self.project / "source.png")
        text = '![ok](<with space/a(b).png>)\n![outside](../outside.png)\n![link](link.png)\n' \
            '![file](file:///private/x.png)\n![web](https://example.invalid/a.png)\n![inside](inside-link.png)'
        collect_outputs(self.cfg, self.node.journal, "collection", text)
        task = self.node.journal.get("collection")
        self.assertEqual(len(task["output_images"]), 1)
        self.assertEqual(task["output_images"][0]["relative_path"], "with space/a(b).png")
        self.assertTrue(task["images_note"])
        with self.assertRaises(ImageError):local_image(str(self.project), "../outside.png")
        with self.assertRaises(OSError):read_workspace_image(str(self.project), "link.png")
        nested = self.project / "alias"
        nested.symlink_to(self.base)
        with self.assertRaises(OSError):read_workspace_image(str(self.project), "alias/outside.png")

    async def test_output_count_duplicate_and_bad_container(self):
        declarations = []
        for i in range(6):
            (self.project / f"{i}.png").write_bytes(self.payload)
            declarations.append(f"![{i}]({i}.png)")
        self.node.journal.put({"task_id": "many", "status": "done", "cwd": str(self.project)})
        collect_outputs(self.cfg, self.node.journal, "many", "\n".join(declarations))
        task = self.node.journal.get("many")
        self.assertEqual(len(task["output_images"]), 4)
        self.assertIn("四张", task["images_note"])
        damaged = bytearray(self.payload)
        damaged[-5] ^= 1
        with self.assertRaises(ImageError):image_info(bytes(damaged))
        with self.assertRaises(ImageError):image_info(b"x" * (MAX_BYTES + 1))
        self.node.journal.put({"task_id": "duplicate", "status": "done", "cwd": str(self.project)})
        collect_outputs(self.cfg, self.node.journal, "duplicate", "![one](0.png) ![two](./0.png)")
        self.assertEqual(len(self.node.journal.get("duplicate")["output_images"]), 1)

    async def test_reference_images_keep_order_and_escaped_examples_are_ignored(self):
        text = '![First][photo]\n![inline](a(b).png)\n![second][]\n![shortcut]\n' \
            '\\![literal](secret.png)\n[photo]: <with space/a.png> "caption"\n' \
            '[second]: second.png\n[shortcut]: shortcut.png'
        self.assertEqual(markdown_images(text), ["with space/a.png", "a(b).png", "second.png", "shortcut.png"])

    async def test_url_origin_https_and_unsafe_config(self):
        image_id = "i-" + "6" * 32
        self.assertEqual(image_url({"hub_ws_url": "wss://hub.example:8443/ws/node"}, "task", image_id),
            f"https://hub.example:8443/api/v1/tasks/task/images/{image_id}")
        self.assertEqual(image_url({"hub_ws_url": "wss://example.com/xvis/ws/node"}, "task", image_id, True),
            f"https://example.com/xvis/api/v1/tasks/task/images/{image_id}/content")
        for value in ("ws://hub.example/ws/node", "wss://secret@hub.example/ws/node", "wss://@hub.example/xvis/ws/node",
                      "wss://hub.example/ws/node?token=secret", "wss://hub.example/other",
                      "wss://hub.example/xvis/ws/node#fragment", "wss://hub.example/xvis/../ws/node",
                      "wss://hub.example/xvis/%2f/ws/node", "wss://hub.example/xvis//ws/node"):
            with self.assertRaises(ImageError):image_url({"hub_ws_url": value}, "task", image_id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
