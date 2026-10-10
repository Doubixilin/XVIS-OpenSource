"""Pair a new, isolated DSH Node using a phone-confirmed short-lived QR/link.

The output config must not exist. Runtime and project paths are explicit. This
creates local credentials only; it neither starts a Node nor changes desktop DSH.
"""
import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import ssl
import sys
import time
from urllib.parse import urlencode, urlsplit, urlunsplit
import uuid

import httpx
import yaml


def digest(value): return hashlib.sha256(value.encode()).hexdigest()


def hub_url(value):
    url = urlsplit(value)
    if url.username or url.password or url.query or url.fragment or not url.hostname or (url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"127.0.0.1", "localhost", "::1"})):
        raise ValueError("中枢必须使用 HTTPS；仅本机验收可使用 loopback HTTP")
    return value.rstrip("/") + "/"


async def pair(args):
    base = hub_url(args.hub)
    output = args.config.expanduser().absolute()
    if output.exists(): raise ValueError("配置已存在，请指定全新文件，既有节点不会被覆盖")
    workspace = args.workspace.expanduser().resolve(strict=True)
    if not workspace.is_dir(): raise ValueError("工作区必须是已经存在的目录")
    node_bin = args.node_bin.expanduser().resolve(strict=True)
    dsh_cli = args.dsh_cli.expanduser().resolve(strict=True)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    node_token, scan, poll = [secrets.token_urlsafe(32) for _ in range(3)]
    rid = "p-" + uuid.uuid4().hex
    payload = {"request_id": rid, "name": args.name, "scan_hash": digest(scan), "poll_hash": digest(poll), "token_hash": digest(node_token)}
    verify = ssl.create_default_context(cafile=str(args.ca_file)) if args.ca_file else True
    async with httpx.AsyncClient(base_url=base, verify=verify, trust_env=False, follow_redirects=False, timeout=15) as api:
        response = await api.post("api/v1/pairing/requests", json=payload)
        response.raise_for_status()
        deadline = response.json()["expires_at"]
        link = "xvis-pair://device?" + urlencode({"server": base, "id": rid, "secret": scan})
        link_path = output.with_suffix(".pairing.txt")
        qr_path = output.with_suffix(".pairing.png")
        created_paths = []
        render = None
        try:
            for path in (link_path, qr_path):
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                created_paths.append(path)
                with os.fdopen(fd, "w") as file:
                    if path == link_path: file.write(link)
            print(f"设备：{args.name}；核对指纹：{digest(node_token)[:12]}；五分钟内在 XVIS 设置 → 设备连接中确认。", flush=True)
            print(f"配对链接文件：{link_path}", flush=True)
            if sys.platform == "darwin":
                render = await asyncio.create_subprocess_exec("/usr/bin/swift", str(Path(__file__).with_name("render_pairing_qr.swift")), str(qr_path), stdin=asyncio.subprocess.PIPE,
                                                             stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                await render.communicate(link.encode())
                if render.returncode == 0:
                    print(f"二维码：{qr_path}", flush=True)
            while time.time() < deadline:
                await asyncio.sleep(2)
                response = await api.post(f"api/v1/pairing/{rid}/status", json={"secret": poll})
                if response.status_code == 429: continue
                response.raise_for_status()
                status = response.json()
                if status["status"] != "approved": continue
                nid = status["node_id"]
                root = output.parent / ("xvis-node-" + nid)
                root.mkdir(mode=0o700, exist_ok=False)
                url = urlsplit(base)
                config = {"hub_ws_url": urlunsplit(("wss" if url.scheme == "https" else "ws", url.netloc, url.path + "ws/node", "", "")),
                          "node_token": node_token, "node_bin": str(node_bin), "dsh_cli": str(dsh_cli),
                          "dsh_home": str(root / "dsh-home"), "host_home": str(root / "host-home"),
                          "journal_path": str(root / "node.sqlite"), "log_dir": str(root / "runs"),
                          "projects": [{"project_id": args.project_id, "name": args.project_name or workspace.name, "path": str(workspace)}]}
                fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "w") as file:
                    file.write(yaml.safe_dump(config, allow_unicode=True)); file.flush(); os.fsync(file.fileno())
                print(json.dumps({"paired": True, "node_id": nid, "config": str(output), "started": False}, ensure_ascii=False), flush=True)
                return
            raise ValueError("配对已过期，未写入节点配置")
        finally:
            if render and render.returncode is None:
                render.terminate()
                await render.wait()
            for path in created_paths: path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hub", required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--node-bin", type=Path, required=True)
    parser.add_argument("--dsh-cli", type=Path, required=True)
    parser.add_argument("--project-id", default="p-xvis-test")
    parser.add_argument("--project-name")
    parser.add_argument("--ca-file", type=Path, help="explicit CA for isolated TLS acceptance; verification stays enabled")
    args = parser.parse_args()
    try: asyncio.run(pair(args))
    except (ValueError, OSError, httpx.HTTPError) as exc:
        # HTTP exception strings can contain URLs; keep credentials out of logs.
        print("配对未完成：" + (str(exc) if isinstance(exc, ValueError) else type(exc).__name__), file=sys.stderr)
        raise SystemExit(1)
