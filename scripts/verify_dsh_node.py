"""No-model node acceptance tests: real subprocesses, isolated homes/journals."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import websockets

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from node.dsh_agent import DSHNode
from node.dsh_config import load_config, project_path
from node.dsh_process import ProcessTracker, process_table
from node.dsh_runner import verdict, text_chunks, wire_summary

FAKE = r'''
import json,os,signal,subprocess,sys,time
from pathlib import Path
def emit(x):
    data=(json.dumps(x,ensure_ascii=False)+'\n').encode()
    for i in range(0,len(data),137):
        sys.stdout.buffer.write(data[i:i+137]);sys.stdout.buffer.flush()
prompt=sys.stdin.read()
# The real runner appends an output transport convention after the user's text.
# Only the fake model's scenario selector removes that final suffix.
prompt=prompt.rsplit('\n\n[结果图片回传约定]\n',1)[0]
counter=Path(os.environ['DSH_HOME'])/'starts'
counter.write_text(counter.read_text()+'x' if counter.exists() else 'x')
sid=sys.argv[sys.argv.index('--session-id')+1] if '--session-id' in sys.argv else 'session-fake'
emit({'type':'session','sessionId':sid,'cwd':os.getcwd()})
if prompt in ('cancel','timeout'):
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    child=subprocess.Popen([sys.executable,'-c','import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(300)'],start_new_session=True)
    Path('child.pid').write_text(str(child.pid))
    emit({'type':'tool_call','callId':'c1','tool':'bash','input':{'command':'sleep 300'}})
    time.sleep(300)
time.sleep(.15)
emit({'type':'thinking','text':'DO_NOT_FORWARD'})
emit({'type':'tool_result','callId':'c1','status':'completed','result':'exit 1 [sandbox: file access denied under workspace-write mode] Operation not permitted'})
emit({'type':'text','text':'你好'})
emit({'type':'status','phase':'turn_end','reason':{'kind':'completed'}})
if prompt=='badfinal':emit({'type':'final','text':{}})
elif prompt=='half':sys.stdout.write('{"type":"final","text":"unfinished');sys.stdout.flush()
else:emit({'type':'final','text':'文'*40000 if prompt=='large' else 'success'})
'''


class Socket:
    def __init__(self):
        self.messages = []
    async def send(self, raw):
        self.messages.append(json.loads(raw))
    async def close(self):
        pass


class NodeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="xvis-node-")
        self.base = Path(self.tmp.name).resolve()
        self.project = self.base / "project"
        self.project.mkdir()
        self.fake = self.base / "fake.py"
        self.fake.write_text(FAKE)
        self.cfg = {"node_bin": sys.executable, "dsh_cli": str(self.fake), "dsh_home": str(self.base / "home"),
            "journal_path": str(self.base / "journal.sqlite"), "log_dir": str(self.base / "runs"),
            "runtime_version": {"dsh": "0.2.1-alpha.1", "node": "v24.21.0"},
            "projects": {"p": {"path": str(self.project), "realpath": str(self.project)}},
            "default_timeout_sec": 15, "node_token": "a"*40}
        self.node = DSHNode(self.cfg)
        self.probe = patch("node.dsh_agent.runtime_version", return_value=self.cfg["runtime_version"])
        self.probe.start()
        self.socket = Socket()
        self.node.ws, self.node.ready = self.socket, True

    async def asyncTearDown(self):
        await self.node.close()
        self.probe.stop()
        self.tmp.cleanup()

    async def run_task(self, tid="task-one", prompt="success", **extra):
        await self.node.assign({"type": "task_assign", "task_id": tid, "project_id": "p", "prompt": prompt, **extra})
        if self.node.worker:
            await self.node.worker
        return self.node.journal.get(tid)

    async def test_stream_outbox_duplicate_and_reconnect(self):
        task = await self.run_task(prompt="large")
        self.assertEqual(task["status"], "done")
        self.assertEqual(self.node.occupied(), [])
        events = self.node.journal.pending("task-one")
        self.assertGreater(len(events), 5)
        self.assertEqual("".join(e["event"]["text"] for e in events if e["event"]["type"] == "final_chunk"), "文"*40000)
        self.assertTrue(task["final_truncated"])
        self.assertFalse(any(e["event"]["type"] == "thinking" for e in events))
        self.assertTrue(next(e["event"] for e in events if e["event"]["type"] == "tool_result")["permission_denied"])
        self.assertTrue(all(len(json.dumps(e, ensure_ascii=False).encode()) < 65536 for e in events))
        await self.node.assign({"task_id":"task-one","project_id":"p","prompt":"again"})
        self.assertEqual((self.base / "home" / "starts").read_text(), "x")
        await self.node.dispatch({"type":"event_ack","task_id":"task-one","acked_seq":2})
        self.assertEqual(self.node.journal.pending("task-one")[0]["event_seq"], 3)
        await self.node.close()
        self.node = DSHNode(self.cfg)
        self.node.ws = self.socket
        await self.node.recover()
        start = len(self.socket.messages)
        await self.node.dispatch({"type":"task_snapshot","tasks":[{"task_id":"task-one"}]})
        resend = [m for m in self.socket.messages[start:] if m["type"]=="task_event"]
        self.assertEqual(resend[0]["event_seq"], 3)
        self.assertEqual(self.socket.messages[-1]["running"], [])

    async def test_followup_triplet_missing_changed_and_path_replacement(self):
        original = await self.run_task()
        followup = {"followup_of":{"task_id":"task-one","dsh_session_id":"session-fake"}}
        self.assertTrue(self.node.followup_valid(followup, str(self.project)))
        for field, altered in (("runtime_version", None), ("runtime_version", {"node":"different"}),
                               ("dsh_home", "/other"), ("cwd", "/other"), ("session_cwd", None)):
            changed = dict(original)
            changed[field] = altered
            self.node.journal.put(changed)
            self.assertFalse(self.node.followup_valid(followup, str(self.project)), field)
        self.node.journal.put(original)
        continued = await self.run_task("task-two", **followup)
        self.assertEqual(continued["status"], "done")
        self.assertEqual(continued["dsh_session_id"], original["dsh_session_id"])
        self.assertEqual(self.node.journal.get("task-one")["status"], "done")
        self.project.rename(self.base / "renamed")
        self.project.symlink_to(self.base / "other")
        (self.base / "other").mkdir()
        self.assertIsNone(project_path(self.cfg, "p"))

    async def test_cancel_actual_detached_tool_and_busy(self):
        await self.node.assign({"task_id":"task-long","project_id":"p","prompt":"cancel"})
        child = self.project / "child.pid"
        for _ in range(100):
            if child.exists() and len(self.node.runner.tracker.records) > 1:
                break
            await asyncio.sleep(.05)
        self.assertTrue(child.exists(), "actual child must start before cancel")
        child_pid = int(child.read_text())
        tracked = list(self.node.runner.tracker.records.values())
        self.assertIn(child_pid, [p["pid"] for p in tracked])
        self.assertNotEqual(next(p for p in tracked if p["pid"]==child_pid)["pgid"], self.node.runner.proc.pid)
        await self.node.assign({"task_id":"task-busy","project_id":"p","prompt":"success"})
        self.assertIsNone(self.node.journal.get("task-busy"))
        await self.node.dispatch({"type":"task_cancel","task_id":"task-long"})
        await asyncio.wait_for(self.node.worker, 20)
        done = self.node.journal.get("task-long")
        self.assertEqual(done["status"], "cancelled")
        self.assertTrue(done["stop_confirmed"])
        self.assertFalse(any(p["pid"]==child_pid for p in process_table()))
        self.assertEqual(self.node.occupied(), [])

    async def test_timeout_malformed_and_final_schema(self):
        done = await self.run_task("task-timeout", "timeout", timeout_sec=.3)
        self.assertEqual(done["status"], "timed_out")
        for tid, mode in (("task-invalid","badfinal"),("task-half","half")):
            task = await self.run_task(tid, mode)
            self.assertEqual(task["status"], "failed")
        for survivors, code in ((-1,0),(1,0),(0,None)):
            self.assertEqual(verdict(None,None,code,True,False,survivors)[0], "interrupted")

    async def test_recovery_no_blind_restart_pid_reuse_and_ps_failure(self):
        self.node.journal.put({"task_id":"task-crash","status":"assigned","last_persisted_seq":0})
        await self.node.recover()
        self.assertEqual(self.node.journal.get("task-crash")["status"], "interrupted")
        self.assertEqual(self.node.occupied(), ["task-crash"])
        self.assertFalse((self.base / "home" / "starts").exists())
        old = {"pid":12345,"ppid":1,"pgid":12345,"birth":"old","command":"dsh"}
        tracker = ProcessTracker([old])
        with patch("node.dsh_process.process_table", return_value=[dict(old,birth="new")]), patch("os.killpg") as kill:
            self.assertTrue(tracker.signal(15))
            self.assertEqual(tracker.survivors(), -1)
            kill.assert_not_called()
        with patch("node.dsh_process.process_table", return_value=None):
            self.assertEqual(tracker.survivors(), -1)
            self.assertFalse(tracker.signal(15))

    async def test_observed_group_untracked_orphan_remains_unknown(self):
        old = {"pid":12345,"ppid":1,"pgid":12345,"birth":"old","command":"dsh"}
        orphan = {"pid":12346,"ppid":1,"pgid":12345,"birth":"child","command":"tool"}
        tracker = ProcessTracker([old])
        with patch("node.dsh_process.process_table", return_value=[orphan]), patch("os.killpg") as kill:
            self.assertEqual(tracker.survivors(), -1)
            self.assertTrue(tracker.signal(15))
            kill.assert_not_called()
        with patch("node.dsh_process.process_table", return_value=[old,orphan]):
            self.assertEqual(tracker.survivors(), 2)
        with patch("node.dsh_process.process_table", return_value=[]):
            self.assertEqual(tracker.survivors(), 0)
            self.assertEqual(ProcessTracker().survivors(), -1)
        for first_rows in (None, []):
            unobserved = ProcessTracker()
            with patch("node.dsh_process.process_table", return_value=first_rows):
                unobserved.observe(old["pid"])
            with patch("node.dsh_process.process_table", return_value=[dict(old, birth="reused")]), patch("os.killpg") as kill:
                unobserved.observe(old["pid"])
                self.assertEqual(unobserved.records, {})
                self.assertEqual(unobserved.survivors(), -1)
                unobserved.signal(15)
                kill.assert_not_called()

    async def test_ready_gate_and_unknown_snapshot(self):
        self.node.ready = False
        await self.node.assign({"task_id":"task-gated","project_id":"p","prompt":"success"})
        self.assertIsNone(self.node.journal.get("task-gated"))
        await self.node.dispatch({"type":"task_snapshot","tasks":[{"task_id":"unknown"}]})
        self.assertTrue(any(m.get("local_status")=="unknown" for m in self.socket.messages))
        await self.node.dispatch({"type":"ready"})
        self.assertTrue(self.node.ready)

    async def test_cancel_before_spawn_creates_no_process(self):
        await self.node.assign({"task_id":"task-pre-cancel","project_id":"p","prompt":"success"})
        self.node.runner.cancel()
        await self.node.worker
        task = self.node.journal.get("task-pre-cancel")
        self.assertEqual(task["status"], "cancelled")
        self.assertTrue(task["not_started"])
        self.assertIsNone(task["exit_code"])
        self.assertFalse((self.base / "home" / "starts").exists())
        self.assertEqual(self.node.occupied(), [])

    async def test_real_websocket_header_ready_and_event_ack(self):
        finished = asyncio.Future()
        async def hub(socket):
            self.assertEqual(socket.request.headers["Authorization"], "Bearer " + self.cfg["node_token"])
            self.assertEqual(socket.request.path, "/ws/node")
            hello = json.loads(await socket.recv())
            self.assertEqual(hello["type"], "hello")
            await socket.send(json.dumps({"type":"hello_ack","node_id":"mac-a","ready":False}))
            await socket.send(json.dumps({"type":"task_snapshot","tasks":[]}))
            self.assertEqual(json.loads(await socket.recv())["type"], "heartbeat")
            await socket.send(json.dumps({"type":"ready","node_id":"mac-a"}))
            await socket.send(json.dumps({"type":"task_assign","task_id":"task-ws","project_id":"p","prompt":"success"}))
            terminal = False
            async for raw in socket:
                message = json.loads(raw)
                if message["type"] == "task_event":
                    await socket.send(json.dumps({"type":"event_ack","task_id":"task-ws","acked_seq":message["event_seq"]}))
                if message["type"] == "task_done":
                    self.assertEqual(message["status"], "done")
                    terminal = True
                if terminal and message["type"]=="heartbeat" and message["running"]==[]:
                    finished.set_result(True)
                    return
        async with websockets.serve(hub, "127.0.0.1", 0) as server:
            self.cfg["hub_ws_url"] = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/ws/node"
            start_task = asyncio.create_task(self.node.start())
            try:
                await asyncio.wait_for(finished, 10)
                self.assertEqual(self.node.journal.get("task-ws")["status"], "done")
            finally:
                start_task.cancel()
                await asyncio.gather(start_task, return_exceptions=True)

    async def test_recover_observed_detached_groups_without_restart(self):
        home = self.base / "home"
        home.mkdir()
        proc = await asyncio.create_subprocess_exec(sys.executable, str(self.fake),
            cwd=str(self.project), env={**os.environ, "DSH_HOME":str(home)},
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True)
        proc.stdin.write(b"cancel")
        await proc.stdin.drain()
        proc.stdin.close()
        tracker = ProcessTracker()
        for _ in range(100):
            tracker.observe(proc.pid)
            if len(tracker.records)>1:
                break
            await asyncio.sleep(.05)
        self.assertGreater(len(tracker.records), 1)
        self.node.journal.put({"task_id":"task-recovery","status":"running","pid":proc.pid,
            "processes":list(tracker.records.values()), "last_persisted_seq":0})
        await self.node.recover()
        await proc.wait()
        recovered = self.node.journal.get("task-recovery")
        self.assertEqual(recovered["status"], "interrupted")
        self.assertTrue(recovered["stop_confirmed"])
        self.assertEqual(tracker.survivors(), 0)
        self.assertEqual(home.joinpath("starts").read_text(), "x")

    async def test_configuration_explicit_path_and_desktop_home_boundary(self):
        import yaml
        explicit = self.base / "named.yaml"
        raw = dict(self.cfg)
        raw["projects"] = [{"project_id":"p", "path":str(self.project)}]
        raw["hub_ws_url"] = "ws://127.0.0.1:18300/ws/node"
        explicit.write_text(yaml.safe_dump(raw))
        explicit.chmod(0o600)
        (self.base / "config.local.yaml").write_text("invalid: true")
        with patch("node.dsh_config.runtime_version", return_value=self.cfg["runtime_version"]):
            self.assertEqual(load_config(str(explicit))["hub_ws_url"], raw["hub_ws_url"])
            for desktop in (Path.home()/".dsh", Path.home()/".dsh"/"xvis-node"):
                raw["dsh_home"] = str(desktop)
                explicit.write_text(yaml.safe_dump(raw))
                with self.assertRaisesRegex(ValueError,"独立 DSH_HOME"):
                    load_config(str(explicit))

    async def test_escaped_json_text_respects_wire_limit(self):
        text = "\\\n\t" * 40000 + "中文"
        chunks = list(text_chunks(text))
        self.assertEqual("".join(chunks), text)
        self.assertGreater(len(chunks), 3)
        self.assertTrue(all(len(json.dumps(chunk, ensure_ascii=False).encode()) <= 48002 for chunk in chunks))
        self.assertLess(len(json.dumps(wire_summary(text), ensure_ascii=False).encode()), 65536)

    async def test_unknown_stop_remains_occupied_and_terminal_stable(self):
        await self.node.assign({"task_id":"task-uncertain","project_id":"p","prompt":"cancel"})
        for _ in range(100):
            if self.node.runner and len(self.node.runner.tracker.records)>1:
                break
            await asyncio.sleep(.05)
        runner = self.node.runner
        original_stop = runner.tracker.stop
        calls = 0
        async def uncertain_then_verified(pid, persist, grace=5):
            nonlocal calls
            calls += 1
            if calls == 1:
                return -1
            return await original_stop(pid, persist, grace=.2)
        runner.tracker.stop = uncertain_then_verified
        runner.cancel()
        for _ in range(100):
            if self.node.journal.get("task-uncertain")["status"]=="interrupted":
                break
            await asyncio.sleep(.02)
        self.assertEqual(self.node.journal.get("task-uncertain")["status"], "interrupted")
        self.assertEqual(self.node.occupied(), ["task-uncertain"])
        await asyncio.wait_for(self.node.worker, 15)
        self.assertEqual(self.node.journal.get("task-uncertain")["status"], "interrupted")
        self.assertEqual(self.node.occupied(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
