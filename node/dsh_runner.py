"""Headless DSH runner: byte-stream parsing and confirmed task termination."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from node.dsh_process import ProcessTracker
from node.dsh_images import ImageError, collect_outputs, download_inputs, image_prompt
from node import dsh_artifacts
from node.dsh_profile import assistant_prompt
from node.dsh_project_context import project_prompt
from node.dsh_store import Journal


def timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def bounded(text: str, limit: int = 8192) -> str:
    return text.encode("utf-8")[:limit].decode("utf-8", "ignore")


def text_chunks(text: str, budget: int = 48000):
    """Bound the JSON-encoded text, including escaped newlines/backslashes."""
    position = 0
    while position < len(text):
        end = min(len(text), position + budget)
        while True:
            size = len(json.dumps(text[position:end], ensure_ascii=False).encode()) - 2
            if size <= budget:
                break
            end = position + max(1, int((end - position) * budget / size))
        yield text[position:end]
        position = end


def wire_summary(text: str) -> str:
    return next(text_chunks(text), "")


class ByteLines:
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, chunk: bytes) -> list[bytes]:
        self.buffer.extend(chunk)
        lines = []
        while True:
            pos = self.buffer.find(b"\n")
            if pos < 0:
                return lines
            lines.append(bytes(self.buffer[:pos]))
            del self.buffer[:pos + 1]


class RotatingBinary:
    """Bound raw diagnostic logs; final.txt retains the entire final separately."""
    def __init__(self, path: Path, limit: int = 32 * 1024 * 1024):
        self.path, self.limit = path, limit
        self.stream = open(path, "wb")
        os.chmod(path, 0o600)

    def write(self, chunk: bytes):
        if self.stream.tell() + len(chunk) > self.limit:
            self.stream.close()
            for index in range(3, 0, -1):
                source = self.path.with_name(self.path.name + "." + str(index))
                target = self.path.with_name(self.path.name + "." + str(index + 1))
                if source.exists():
                    source.replace(target)
            self.path.replace(self.path.with_name(self.path.name + ".1"))
            self.stream = open(self.path, "wb")
            os.chmod(self.path, 0o600)
        self.stream.write(chunk)

    def close(self):
        self.stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def verdict(final: dict | None, reason, exit_code: int | None, cancel: bool,
            timeout: bool, survivors: int, malformed: bool = False) -> tuple[str, str]:
    stopped = exit_code is not None and survivors == 0
    if cancel or timeout:
        if not stopped:
            return "interrupted", "停止尚未确认，请在 Mac 检查本任务进程"
        return ("timed_out", "任务超时，已确认停止") if timeout else ("cancelled", "已确认任务停止")
    if not stopped:
        return "interrupted", "执行进程停止尚未确认，请在 Mac 检查"
    if malformed or final is None or not isinstance(final.get("text"), str):
        return "failed", "DSH 未返回完整且合法的最终回答"
    kind = reason.get("kind") if isinstance(reason, dict) else reason
    if kind == "completed" and exit_code == 0:
        return "done", ""
    if isinstance(reason, dict) and kind == "error":
        error = reason.get("error") or {}
        return "failed", bounded(str(error.get("message") or error.get("code") or "DSH 执行失败"))
    return "failed", "DSH 未正常完成任务"


class DSHRunner:
    def __init__(self, cfg: dict, journal: Journal, task_id: str, emit):
        self.cfg, self.journal, self.task_id, self.emit = cfg, journal, task_id, emit
        self.cancel_event = asyncio.Event()
        self.proc = None
        self.tracker = ProcessTracker()
        self.final = None
        self.reason = None
        self.malformed = False
        self.unknown_count = 0
        self._monitor_stop = asyncio.Event()

    def cancel(self):
        if self.proc is not None and self.proc.returncode is not None:
            return
        task = self.journal.get(self.task_id)
        fields = {"cancel_requested": True}
        if task["status"] not in {"done", "failed", "cancelled", "timed_out", "interrupted"}:
            fields["status"] = "cancel_requested"
        self.journal.update(self.task_id, **fields)
        self.cancel_event.set()

    def _persist_processes(self, records):
        current = self.journal.get(self.task_id)
        if current.get("processes") != records:
            self.journal.update(self.task_id, processes=records)

    async def _observe(self):
        while not self._monitor_stop.is_set():
            self.tracker.observe(self.proc.pid)
            self._persist_processes(list(self.tracker.records.values()))
            await asyncio.sleep(0.05)

    async def _event(self, event: dict):
        await self.emit(self.journal.event(self.task_id, event, timestamp()))

    async def _ingest(self, line: bytes):
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                raise ValueError("event must be an object")
        except (ValueError, UnicodeDecodeError):
            self.malformed = True
            await self._event({"type": "error", "message": "DSH 输出含无法解析的数据"})
            return
        kind = event.get("type")
        if kind == "session":
            task = self.journal.get(self.task_id)
            sid = event.get("sessionId")
            if not isinstance(sid, str) or not sid or event.get("cwd") != task["cwd"] or (
                task.get("followup_of") and sid != task["followup_of"]["dsh_session_id"]):
                self.malformed = True
                await self._event({"type": "error", "message": "DSH 会话目录与已登记项目不一致"})
                self.journal.update(self.task_id, abort_requested=True)
                self.cancel_event.set()
                return
            self.journal.update(self.task_id, dsh_session_id=sid, session_cwd=event["cwd"])
            await self.emit({"type": "task_started", "task_id": self.task_id, "dsh_session_id": sid,
                             "pid": self.proc.pid, "pgid": self.proc.pid})
            await self._event({"type": "session", "sessionId": sid})
        elif kind == "thinking":
            return
        elif kind == "status":
            if event.get("phase") == "turn_end":
                self.reason = event.get("reason")
            # Do not relay arbitrary metadata or hidden model content.
            projected = {key: event[key] for key in ("type", "phase", "turn", "step", "reason", "usage", "truncated") if key in event}
            if len(json.dumps(projected, ensure_ascii=False).encode()) > 60000:
                projected = {"type": "status", "phase": event.get("phase"), "truncated": True}
            await self._event(projected)
        elif kind == "final":
            self.final = event
            if not isinstance(event.get("text"), str):
                self.malformed = True
                await self._event({"type": "error", "message": "DSH 最终回答缺少合法文本"})
                return
            text = event["text"]
            Path(self.journal.get(self.task_id)["final_path"]).write_text(text, encoding="utf-8")
            if len(json.dumps(text, ensure_ascii=False).encode()) <= 48000:
                await self._event({"type": "final", "text": text})
            else:
                position = 0
                for chunk in text_chunks(text):
                    position += len(chunk)
                    await self._event({"type": "final_chunk", "text": chunk, "seq_end": position == len(text)})
        elif kind == "text":
            text = event.get("text", "")
            if not isinstance(text, str):
                self.malformed = True
                return
            summary = wire_summary(text)
            await self._event({"type": "text", "text": summary,
                               "truncated": bool(event.get("truncated")) or summary != text})
        elif kind in {"tool_call", "tool_result"}:
            content = event.get("input") if kind == "tool_call" else event.get("result")
            full_summary = json.dumps(content, ensure_ascii=False)
            summary = bounded(full_summary, 2048)
            projected = {"type": kind, "callId": bounded(str(event.get("callId", "")), 256),
                         "tool": bounded(str(event.get("tool", "")), 256), "summary": summary,
                         "truncated": bool(event.get("truncated")) or len(json.dumps(content, ensure_ascii=False).encode()) > 2048}
            if kind == "tool_result":
                projected["status"] = event.get("status")
                projected["permission_denied"] = any(x in full_summary for x in
                    ("[sandbox: file access denied", "requires approval, but no approval channel is available"))
            await self._event(projected)
        elif kind == "error":
            await self._event({"type": "error", "message": bounded(str(event.get("message", "DSH 执行失败")))})
        else:
            self.unknown_count += 1
            if isinstance(kind, str) and len(json.dumps(event, ensure_ascii=False).encode()) <= 48000:
                await self._event(event)
            else:
                await self._event({"type": "unknown", "original_type": bounded(str(kind), 256),
                                   "count": self.unknown_count, "truncated": True})

    async def _stdout(self, log):
        assembler = ByteLines()
        while chunk := await self.proc.stdout.read(65536):
            log.write(chunk)
            for line in assembler.feed(chunk):
                if line.strip():
                    await self._ingest(line)
        if assembler.buffer.strip():
            self.malformed = True
            await self._event({"type": "error", "message": "DSH 输出在半行处中断", "truncated": True})

    async def _stderr(self, log):
        while chunk := await self.proc.stderr.read(65536):
            log.write(chunk)

    async def _deadline(self, wait_task):
        cancel_wait = asyncio.create_task(self.cancel_event.wait())
        try:
            done, _ = await asyncio.wait({wait_task, cancel_wait}, timeout=self.journal.get(self.task_id)["timeout_sec"],
                                         return_when=asyncio.FIRST_COMPLETED)
            if wait_task in done:
                return
            if cancel_wait not in done:
                self.journal.update(self.task_id, timed_out=True)
            count = await self.tracker.stop(self.proc.pid, self._persist_processes)
            if count != 0:
                detail = "停止尚未确认，请在 Mac 检查本任务进程"
                current = self.journal.update(self.task_id, status="interrupted", stop_confirmed=False, detail=detail)
                await self.emit({"type": "task_done", "task_id": self.task_id, "status": "interrupted",
                    "detail": detail, "dsh_session_id": current.get("dsh_session_id"),
                    "last_persisted_seq": current.get("last_persisted_seq", 0)})
                # Keep supervision and the execution slot while process ownership
                # remains unknown. A later successful check may clean and free it,
                # but the already reported interrupted terminal never rolls back.
                while self.proc.returncode is None or self.tracker.survivors() != 0:
                    await asyncio.sleep(5)
                    count = await self.tracker.stop(self.proc.pid, self._persist_processes)
                    if count == 0:
                        break
        finally:
            cancel_wait.cancel()

    async def run(self):
        task = self.journal.get(self.task_id)
        directory = Path(self.cfg["log_dir"]) / self.task_id
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        final_path = directory / "final.txt"
        final_path.touch(mode=0o600, exist_ok=True)
        self.journal.update(self.task_id, final_path=str(final_path))
        if task.get("cancel_requested"):
            self.journal.update(self.task_id, status="cancelled", exit_code=None, stop_confirmed=True,
                                not_started=True, detail="任务在启动前已取消")
            return
        execution_prompt = image_prompt(task["prompt"], [])
        if task.get("images"):
            downloading = asyncio.create_task(download_inputs(self.cfg, task))
            cancelled = asyncio.create_task(self.cancel_event.wait())
            try:
                done, _ = await asyncio.wait({downloading, cancelled},
                    timeout=min(task["timeout_sec"], 60), return_when=asyncio.FIRST_COMPLETED)
                if cancelled in done or downloading not in done:
                    downloading.cancel()
                    await asyncio.gather(downloading, return_exceptions=True)
                    timed_out = cancelled not in done
                    self.journal.update(self.task_id, status="timed_out" if timed_out else "cancelled",
                        timed_out=timed_out, stop_confirmed=True, not_started=True, exit_code=None,
                        detail="图片准备超时，模型尚未启动" if timed_out else "任务已取消，模型尚未启动", finished_at=timestamp())
                    return
                execution_prompt = image_prompt(task["prompt"], downloading.result())
            except (ImageError, OSError, ValueError) as exc:
                message = str(exc) if isinstance(exc, ImageError) else "输入图片下载或校验失败"
                self.journal.update(self.task_id, status="failed", stop_confirmed=True,
                    not_started=True, exit_code=None, detail=message, finished_at=timestamp())
                await self._event({"type": "error", "message": message})
                return
            except Exception:
                message = "输入图片下载失败，请检查中枢连接"
                self.journal.update(self.task_id, status="failed", stop_confirmed=True,
                    not_started=True, exit_code=None, detail=message, finished_at=timestamp())
                await self._event({"type": "error", "message": message})
                return
            finally:
                downloading.cancel()
                cancelled.cancel()
                await asyncio.gather(downloading, cancelled, return_exceptions=True)
        if self.cancel_event.is_set():
            self.journal.update(self.task_id, status="cancelled", stop_confirmed=True, not_started=True,
                exit_code=None, detail="任务在启动前已取消", finished_at=timestamp())
            return
        if task.get("file_transfer_version") == 1:
            execution_prompt = dsh_artifacts.file_prompt(execution_prompt)
        execution_prompt = assistant_prompt(execution_prompt, task.get("assistant_context"))
        execution_prompt = project_prompt(execution_prompt, task.get("project_context"), project_id=task["project_id"])
        args = [self.cfg["node_bin"], self.cfg["dsh_cli"], "--profile", "headless", "--json"]
        if task.get("followup_of"):
            args += ["--session-id", task["followup_of"]["dsh_session_id"]]
        env = dict(os.environ)
        env.update(DSH_HOME=task["dsh_home"], DSH_PERMISSION_MODE="workspace-write")
        env["PATH"] = str(Path(self.cfg["node_bin"]).parent) + os.pathsep + env.get("PATH", "")
        Path(task["dsh_home"]).mkdir(mode=0o700, parents=True, exist_ok=True)
        monitors = []
        try:
            self.proc = await asyncio.create_subprocess_exec(*args, cwd=task["cwd"], env=env,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True)
            self.tracker.observe(self.proc.pid)
            local_status = "cancel_requested" if self.journal.get(self.task_id).get("cancel_requested") else "running"
            self.journal.update(self.task_id, status=local_status, pid=self.proc.pid, pgid=self.proc.pid,
                                processes=list(self.tracker.records.values()), started_at=timestamp())
            with RotatingBinary(directory / "stdout.ndjson") as stdout, RotatingBinary(directory / "stderr.log") as stderr:
                monitors = [asyncio.create_task(self._observe()), asyncio.create_task(self._stdout(stdout)),
                            asyncio.create_task(self._stderr(stderr))]
                wait_task = asyncio.create_task(self.proc.wait())
                deadline = asyncio.create_task(self._deadline(wait_task))
                try:
                    self.proc.stdin.write(execution_prompt.encode())
                    await self.proc.stdin.drain()
                    self.proc.stdin.close()
                    await wait_task
                    await deadline
                    # A tool can own the pipes after the DSH process exits.
                    if self.tracker.survivors() != 0:
                        await self.tracker.stop(self.proc.pid, self._persist_processes)
                    await asyncio.wait_for(asyncio.gather(*monitors[1:]), 10)
                finally:
                    deadline.cancel()
                    self._monitor_stop.set()
                    await monitors[0]
            current = self.journal.get(self.task_id)
            survivors = self.tracker.survivors()
            status, detail = verdict(self.final, self.reason, self.proc.returncode,
                                     current.get("cancel_requested", False), current.get("timed_out", False),
                                     survivors, self.malformed)
            if current["status"] == "interrupted":
                status, detail = "interrupted", current.get("detail", detail)
            if status == "done" and task.get("file_transfer_version") == 1:
                # Freeze bytes before the durable successful terminal record. Recovery never rereads later workspace files.
                dsh_artifacts.collect_outputs(self.cfg, self.journal, self.task_id, self.final["text"])
            self.journal.update(self.task_id, status=status, detail=detail, exit_code=self.proc.returncode,
                turn_end_reason=self.reason, final_text=wire_summary(self.final["text"]) if status == "done" else "",
                final_truncated=bool(status == "done" and wire_summary(self.final["text"]) != self.final["text"]),
                stop_confirmed=survivors == 0, finished_at=timestamp())
            if status == "done":
                collect_outputs(self.cfg, self.journal, self.task_id, self.final["text"])
                note = self.journal.get(self.task_id).get("images_note")
                if note:
                    await self._event({"type": "error", "message": note})
        except BaseException as exc:
            if self.proc is not None:
                await self.tracker.stop(self.proc.pid, self._persist_processes)
            for monitor in monitors:
                monitor.cancel()
            await asyncio.gather(*monitors, return_exceptions=True)
            stopped = self.proc is None or self.tracker.survivors() == 0
            self.journal.update(self.task_id, status="failed" if self.proc is None else "interrupted",
                                detail="DSH 启动失败" if self.proc is None else "节点执行中断，请检查本机记录",
                                stop_confirmed=stopped, finished_at=timestamp())
            if isinstance(exc, asyncio.CancelledError):
                raise
