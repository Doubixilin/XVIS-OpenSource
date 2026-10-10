"""Current v1 Mac node; the old multi-engine agent remains archived."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import re
import subprocess
import uuid
from pathlib import Path

import websockets

from node.dsh_config import load_config, project_path, runtime_version
from node.dsh_images import ImageError, collect_outputs, input_metadata, upload_outputs
from node.dsh_profile import ProfileError, assistant_context
from node.dsh_project_context import ProjectContextError, project_context
from node import dsh_artifacts
from node.dsh_process import ProcessTracker
from node.dsh_runner import DSHRunner, timestamp
from node.dsh_host import HostRunner
from node.dsh_store import Journal, TERMINAL

log = logging.getLogger("xvis-node")


class DSHNode:
    def __init__(self, cfg: dict):
        self.cfg, self.boot_id = cfg, str(uuid.uuid4())
        self.journal = Journal(Path(cfg["journal_path"]))
        self.ws, self.ready, self.runner, self.worker = None, False, None, None
        self.send_lock, self.assign_lock = asyncio.Lock(), asyncio.Lock()
        self.closed = asyncio.Event()
        self.node_id = None
        self.image_worker = None
        self.image_queue = set()
        self.file_worker = None
        self.file_queue = set()
        self.file_force_queue = set()

    def occupied(self) -> list[str]:
        return [t["task_id"] for t in self.journal.all()
                if t["status"] not in TERMINAL or not t.get("stop_confirmed", False)]

    def status_message(self, task: dict) -> dict:
        return {"type": "task_status", "task_id": task["task_id"], "local_status": task["status"],
                "last_persisted_seq": task.get("last_persisted_seq", 0),
                "dsh_session_id": task.get("dsh_session_id"), "cancel_requested": task.get("cancel_requested", False),
                "final_text": task.get("final_text", ""), "exit_code": task.get("exit_code"),
                "final_truncated": task.get("final_truncated", False),
                "not_started": task.get("not_started", False),
                "turn_end_reason": task.get("turn_end_reason"),
                "detail": "；".join(x for x in (task.get("detail", ""), task.get("images_note", ""), task.get("files_note", "")) if x),
                "note": "已确认停止" if task.get("stop_confirmed") else "进程状态待本机核验"}

    def done_message(self, task: dict) -> dict:
        msg = self.status_message(task)
        msg["type"], msg["status"] = "task_done", msg.pop("local_status")
        return msg

    async def send(self, message: dict):
        if self.ws is not None:
            try:
                async with self.send_lock:
                    if self.ws is not None:
                        await self.ws.send(json.dumps(message, ensure_ascii=False))
            except (websockets.ConnectionClosed, OSError):
                pass  # Journal/outbox are authoritative, not socket delivery.

    async def heartbeat(self):
        await self.send({"type": "heartbeat", "running": self.occupied(), "ts": timestamp()})

    async def recover(self):
        for task in self.journal.all():
            if task["status"] in TERMINAL and task.get("stop_confirmed"):
                if task["status"] == "done" and task.get("image_transfer_version") == 1 and "output_images" not in task:
                    final_path = Path(self.cfg["log_dir"]) / task["task_id"] / "final.txt"
                    try:
                        collect_outputs(self.cfg, self.journal, task["task_id"], final_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        self.journal.update(task["task_id"], images_note="结果图片准备未完成，请检查本机任务记录")
                    note = self.journal.get(task["task_id"]).get("images_note")
                    if note:
                        self.journal.event(task["task_id"], {"type": "error", "message": note}, timestamp())
                if task["status"] == "done" and task.get("file_transfer_version") == 1 and "output_files" not in task:
                    note = "完成时成果副本未固定，请检查本机任务记录"
                    self.journal.update(task["task_id"], output_files=[], file_collection_note=note,
                                        files_note=note, file_manifest_uploaded=False)
                continue
            stopped = False
            if task.get("processes") and task.get("pid"):
                tracker = ProcessTracker(task["processes"])
                count = await tracker.stop(task["pid"],
                    lambda records, tid=task["task_id"]: self.journal.update(tid, processes=records))
                stopped = count == 0
            self.journal.update(task["task_id"], status="interrupted", stop_confirmed=stopped,
                detail="节点重启，已受理任务未重跑" + ("，遗留进程已停止" if stopped else "，请在本机核验遗留进程"),
                finished_at=timestamp())

    def task_home(self, mode):
        return str(Path(self.cfg["host_home"] if mode == "host" else self.cfg["dsh_home"]).resolve())

    def followup_valid(self, message: dict, cwd: str) -> bool:
        followup = message.get("followup_of")
        if followup is None:
            return True
        if not isinstance(followup, dict):
            return False
        if message.get("execution_mode") == "host" and not self.cfg.get("host_home"):
            return False
        parent = self.journal.get(followup.get("task_id", ""))
        return bool(parent and parent["status"] == "done" and parent.get("stop_confirmed")
            and parent.get("dsh_session_id") == followup.get("dsh_session_id") and parent.get("dsh_session_id")
            and parent.get("execution_mode", "headless") == message.get("execution_mode", "headless")
            and parent.get("dsh_home") == self.task_home(message.get("execution_mode", "headless"))
            and parent.get("cwd") == cwd and parent.get("session_cwd") == cwd
            and parent.get("project_identity") == [Path(cwd).stat().st_dev, Path(cwd).stat().st_ino]
            and parent.get("runtime_version") == self.cfg["runtime_version"]
            and parent.get("project_context", {}) == message.get("project_context", {}))

    async def assign(self, message: dict):
        async with self.assign_lock:
            task_id = message.get("task_id", "")
            if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", task_id):
                return
            existing = self.journal.get(task_id)
            if existing:
                await self.send(self.status_message(existing))
                return
            reason = None
            cwd = project_path(self.cfg, message.get("project_id", ""))
            if not self.ready or self.occupied():
                reason = "busy"
            elif cwd is None:
                reason = "unknown_project"
            elif not self.followup_valid(message, cwd):
                reason = "invalid_followup"
            # Reject a package/runtime changed while this node was already online.
            if reason is None:
                try:
                    if runtime_version(self.cfg) != self.cfg["runtime_version"]:
                        reason = "invalid_followup" if message.get("followup_of") else "runtime_changed"
                except (OSError, ValueError, KeyError, subprocess.SubprocessError):
                    reason = "invalid_followup" if message.get("followup_of") else "runtime_changed"
            mode = message.get("execution_mode", "headless")
            if mode not in {"headless", "host"} or (mode == "host" and
                (not self.cfg.get("host_home") or self.cfg["projects"].get(message.get("project_id"), {}).get("execution_mode") != "host")):
                reason = "invalid_task"
            prompt = message.get("prompt")
            try:
                images = input_metadata(message.get("images", []))
            except ImageError:
                images, reason = [], reason or "invalid_task"
            project = {}
            try:
                project = project_context(message.get("project_context", {}), node_id=self.node_id, project_id=message.get("project_id"))
                if project and self.node_id is None:
                    raise ProjectContextError("节点身份尚未确认")
            except ProjectContextError:
                reason = "invalid_task"
            context = None
            file_version = message.get("output_files_version", 0)
            if type(file_version) is not int or file_version not in {0, 1}:
                reason = "invalid_task"
            if "assistant_context" in message:
                try:
                    context = assistant_context(message["assistant_context"])
                except ProfileError:
                    reason = "invalid_task"
            timeout = message.get("timeout_sec", self.cfg.get("default_timeout_sec", 1800))
            if reason is None and (not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > 16384
                or not isinstance(timeout, (int, float)) or not 0 < timeout <= 86400):
                reason = "invalid_task"
            if reason:
                await self.send({"type": "task_rejected", "task_id": task_id, "reason": reason})
                await self.heartbeat()
                return
            task = {"task_id": task_id, "project_id": message["project_id"], "prompt": prompt,
                "images": images, "project_context": project,
                "image_transfer_version": 1,
                "file_transfer_version": file_version,
                "timeout_sec": timeout, "followup_of": message.get("followup_of"), "status": "assigned",
                "cwd": cwd, "dsh_home": self.task_home(mode), "execution_mode": mode,
                "project_identity": [Path(cwd).stat().st_dev, Path(cwd).stat().st_ino],
                "runtime_version": self.cfg["runtime_version"], "cancel_requested": False,
                "last_persisted_seq": 0, "stop_confirmed": False, "accepted_at": timestamp()}
            if context is not None:
                task["assistant_context"] = context
            self.journal.put(task)
            await self.send({"type": "task_accepted", "task_id": task_id, "journal_seq": 1})
            self.runner = (HostRunner if mode == "host" else DSHRunner)(self.cfg, self.journal, task_id, self.send)
            self.worker = asyncio.create_task(self.run_task(self.runner))

    async def run_task(self, runner: DSHRunner):
        try:
            await runner.run()
        finally:
            task = self.journal.get(runner.task_id)
            if task["status"] in TERMINAL:
                await self.send(self.done_message(task))
                await self.send(self.status_message(task))
            self.runner = None
            await self.heartbeat()
            if task["status"] == "done":
                self.schedule_images(runner.task_id)
                self.schedule_files(runner.task_id)

    def schedule_files(self, task_id=None, *, force=False):
        candidates = [self.journal.get(task_id)] if task_id else self.journal.all()
        for task in candidates:
            if not task or task.get("status") != "done" or task.get("file_transfer_version") != 1 or "output_files" not in task:
                continue
            if force:
                self.file_force_queue.add(task["task_id"])
            if force or not task.get("file_manifest_uploaded") or any(f.get("status") != "uploaded" for f in task["output_files"]):
                self.file_queue.add(task["task_id"])
        if self.file_queue and (self.file_worker is None or self.file_worker.done()):
            self.file_worker = asyncio.create_task(self.flush_files())

    async def flush_files(self):
        while self.file_queue and not self.closed.is_set():
            task_id = sorted(self.file_queue)[0]
            self.file_queue.remove(task_id)
            if task_id in self.file_force_queue:
                self.file_force_queue.remove(task_id)
                records = self.journal.get(task_id)["output_files"]
                for value in records:
                    value.update(status="pending", error=None)
                self.journal.update(task_id, output_files=records, file_manifest_uploaded=False)
            try:
                note = await asyncio.wait_for(dsh_artifacts.upload_outputs(self.cfg, self.journal, task_id), 120)
            except (asyncio.TimeoutError, OSError, ValueError):
                note = "成果文件暂未传回，将在重新连接后补传"
                self.journal.update(task_id, files_note=note, file_manifest_uploaded=False)
            await self.send(self.status_message(self.journal.get(task_id)))

    def schedule_images(self, task_id=None):
        candidates = [self.journal.get(task_id)] if task_id else self.journal.all()
        for task in candidates:
            if task and task.get("status") == "done" and any(
                image.get("status") != "uploaded" for image in task.get("output_images", [])):
                self.image_queue.add(task["task_id"])
        if self.image_queue and (self.image_worker is None or self.image_worker.done()):
            self.image_worker = asyncio.create_task(self.flush_images())

    async def flush_images(self):
        while self.image_queue and not self.closed.is_set():
            task_id = sorted(self.image_queue)[0]
            self.image_queue.remove(task_id)
            before = self.journal.get(task_id).get("images_note", "")
            try:
                note = await asyncio.wait_for(upload_outputs(self.cfg, self.journal, task_id), 65)
            except (asyncio.TimeoutError, OSError, ValueError):
                note = "结果图片暂未传回，将在重新连接后补传"
                self.journal.update(task_id, images_note=note)
            if note and note != before:
                await self.send(self.journal.event(task_id, {"type": "error", "message": note}, timestamp()))
            await self.send(self.status_message(self.journal.get(task_id)))

    async def dispatch(self, message: dict):
        kind = message.get("type")
        if kind == "hello_ack":
            self.node_id = message.get("node_id") if isinstance(message.get("node_id"), str) else None
        elif kind == "ready":
            if isinstance(message.get("node_id"), str):
                self.node_id = message["node_id"]
            self.ready = True
            self.schedule_images()
            self.schedule_files()
        elif kind == "task_snapshot":
            self.ready = False
            for expected in message.get("tasks", []):
                task_id = expected.get("task_id")
                task = self.journal.get(task_id)
                if task is None:
                    await self.send({"type": "task_status", "task_id": task_id, "local_status": "unknown",
                        "last_persisted_seq": 0, "note": "本机 journal 无记录，不自动重跑"})
                    continue
                if expected.get("cancel_requested") and self.runner and self.runner.task_id == task_id:
                    self.runner.cancel()
                await self.send(self.status_message(self.journal.get(task_id)))
                for pending in self.journal.pending(task_id):
                    await self.send(pending)
                current = self.journal.get(task_id)
                if current["status"] in TERMINAL:
                    await self.send(self.done_message(current))
                await self.send(self.status_message(current))
            await self.heartbeat()
        elif kind == "event_ack":
            self.journal.acknowledge(message.get("task_id", ""), message.get("acked_seq", -1))
        elif kind == "task_assign":
            await self.assign(message)
        elif kind == "task_cancel":
            task_id = message.get("task_id", "")
            if self.runner and self.runner.task_id == task_id:
                self.runner.cancel()
            elif task := self.journal.get(task_id):
                await self.send(self.status_message(task))
        elif kind == "interaction_response":
            if isinstance(self.runner, HostRunner):
                await self.runner.respond(message)
        elif kind == "task_control":
            if isinstance(self.runner, HostRunner):
                await self.runner.control(message)
        elif kind == "task_files_retry":
            if isinstance(message.get("task_id"), str):
                self.schedule_files(message["task_id"], force=True)

    async def heartbeat_loop(self):
        while True:
            await asyncio.sleep(15)
            await self.heartbeat()

    def hello_tasks(self) -> list[dict]:
        # Hub always includes its active tasks in the snapshot. Fully acknowledged
        # terminal history need not inflate every hello indefinitely.
        return [t for t in self.journal.all() if t["status"] not in TERMINAL
                or not t.get("stop_confirmed") or self.journal.pending(t["task_id"])
                or any(image.get("status") != "uploaded" for image in t.get("output_images", []))
                or (t.get("file_transfer_version") == 1 and (not t.get("file_manifest_uploaded")
                    or any(f.get("status") != "uploaded" for f in t.get("output_files", []))))]

    async def start(self):
        await self.recover()
        delay = 1.0
        while not self.closed.is_set():
            try:
                async with websockets.connect(self.cfg["hub_ws_url"],
                    additional_headers={"Authorization": "Bearer " + self.cfg["node_token"]},
                    max_size=65536, ping_interval=20, close_timeout=5) as ws:
                    self.ws, self.ready, self.node_id = ws, False, None
                    await self.send({"type": "hello", "boot_id": self.boot_id, "protocol": 1,
                        "capabilities": ["output_files_v1", "project_context_v1", "interactions_v1", "continuous_v1"],
                        "projects": [{"project_id": pid, "path": p["realpath"], "name": p.get("name", pid),
                            "available": project_path(self.cfg, pid) is not None,
                            "execution_mode": p.get("execution_mode", "headless")} for pid, p in self.cfg["projects"].items()],
                        "tasks": [{"task_id": t["task_id"], "local_status": t["status"],
                            "dsh_session_id": t.get("dsh_session_id"), "cancel_requested": t.get("cancel_requested", False),
                            "last_persisted_seq": t.get("last_persisted_seq", 0)} for t in self.hello_tasks()]})
                    heartbeat = asyncio.create_task(self.heartbeat_loop())
                    try:
                        async for raw in ws:
                            message = json.loads(raw)
                            if isinstance(message, dict):
                                await self.dispatch(message)
                            if self.ready:
                                delay = 1.0
                    finally:
                        heartbeat.cancel()
                        await asyncio.gather(heartbeat, return_exceptions=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("连接中断 (%s)，将重新连接", type(exc).__name__)
            finally:
                self.ws, self.ready = None, False
            if not self.closed.is_set():
                await asyncio.sleep(min(30, delay * random.uniform(0.8, 1.2)))
                delay = min(30, delay * 2)

    async def close(self):
        self.closed.set()
        if self.runner:
            self.runner.cancel()
        if self.worker:
            await self.worker
        if self.image_worker:
            self.image_worker.cancel()
            await asyncio.gather(self.image_worker, return_exceptions=True)
        if self.file_worker:
            self.file_worker.cancel()
            await asyncio.gather(self.file_worker, return_exceptions=True)
        if self.ws:
            await self.ws.close()
        self.journal.close()


def main():
    parser = argparse.ArgumentParser(description="XVIS Mac DSH 节点")
    parser.add_argument("--config", default=str(Path(__file__).resolve().parent / "config.yaml"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    node = DSHNode(load_config(args.config))

    async def serve():
        import signal
        loop, main_task = asyncio.get_running_loop(), asyncio.current_task()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, main_task.cancel)
        try:
            await node.start()
        finally:
            await node.close()
    try:
        asyncio.run(serve())
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass


if __name__ == "__main__":
    main()
