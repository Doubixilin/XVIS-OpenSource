"""Strict configuration for the single-engine Mac node."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlparse

import yaml


def runtime_version(cfg: dict) -> dict:
    package = Path(cfg["dsh_cli"]).parent.parent / "package.json"
    version = json.loads(package.read_text())["version"]
    node_version = subprocess.check_output([cfg["node_bin"], "--version"], text=True, timeout=10).strip()
    return {"dsh": version, "node": node_version}


def load_config(path: str) -> dict:
    original = Path(path).resolve()
    local = original.with_name("config.local.yaml")
    default = Path(__file__).resolve().parent / "config.yaml"
    target = local if original == default and local.exists() else original
    cfg = yaml.safe_load(target.read_text()) or {}
    token = os.environ.get("XVIS_NODE_TOKEN", cfg.get("node_token", ""))
    if not isinstance(token, str):
        raise ValueError("节点凭证必须为字符串")
    if len(token) < 32 or any(x in token.lower() for x in ("changeme", "replace", "your-token")):
        raise ValueError("请配置至少 32 字符的节点访问凭证")
    if (target == local or len(str(cfg.get("node_token", ""))) >= 32) and target.stat().st_mode & 0o077:
        raise ValueError("节点本地配置只能由当前用户读写，请设置权限 600")
    cfg["node_token"] = token
    url = urlparse(cfg.get("hub_ws_url", ""))
    if url.username or url.password or url.query or url.fragment:
        raise ValueError("节点连接地址不得含凭证、查询参数或片段")
    if url.scheme != "wss" and not (url.scheme == "ws" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
        raise ValueError("公网节点连接必须使用 WSS")
    for key in ("node_bin", "dsh_cli", "dsh_home", "journal_path", "log_dir"):
        value = cfg.get(key, "")
        if not Path(value).is_absolute():
            raise ValueError(f"{key} 必须使用绝对路径")
        cfg[key] = str(Path(value).resolve())
    if Path(cfg["dsh_home"]).is_relative_to((Path.home() / ".dsh").resolve()):
        raise ValueError("必须使用独立 DSH_HOME")
    for key in ("node_bin", "dsh_cli"):
        if not Path(cfg[key]).is_file():
            raise ValueError(f"{key} 文件不存在")
    current = runtime_version(cfg)
    if current != {"dsh": "0.2.1-alpha.1", "node": "v24.21.0"}:
        raise ValueError("首期仅使用验证过的 DSH 0.2.1-alpha.1 / Node v24.21.0")
    cfg["runtime_version"] = current
    projects = cfg.get("projects", [])
    if not isinstance(projects, list) or any(not isinstance(p, dict) or not isinstance(p.get("project_id"), str)
        or not p["project_id"] or not isinstance(p.get("path"), str) or not Path(p["path"]).is_absolute() for p in projects):
        raise ValueError("项目登记必须包含唯一 ID 和绝对路径")
    for p in projects:
        mode = p.get("execution_mode", "headless")
        if mode not in {"headless", "host"} or (mode == "host" and p["project_id"] != "p-xvis-test"):
            raise ValueError("Host 仅允许在 p-xvis-test 显式启用")
    if any(p.get("execution_mode") == "host" for p in projects):
        host = Path(cfg.get("host_home", ""))
        if not host.is_absolute():
            raise ValueError("Host 必须配置独立绝对路径 host_home")
        host = host.resolve()
        for other in (Path(cfg["dsh_home"]), (Path.home() / ".dsh").resolve()):
            if host.is_relative_to(other) or other.is_relative_to(host):
                raise ValueError("Host home 不得与 headless 或桌面目录重叠")
        cfg["host_home"] = str(host)
    cfg["projects"] = {p["project_id"]: {"path": p["path"], "realpath": str(Path(p["path"]).resolve()),
                        "execution_mode": p.get("execution_mode", "headless"),
                        "name": str(p.get("name") or Path(p["path"]).name)[:128],
                        "identity": [Path(p["path"]).stat().st_dev, Path(p["path"]).stat().st_ino] if Path(p["path"]).is_dir() else None}
                       for p in projects}
    if len(cfg["projects"]) != len(projects):
        raise ValueError("项目 ID 不得重复")
    if not cfg["projects"]:
        raise ValueError("至少登记一个绝对路径项目")
    cfg["default_timeout_sec"] = float(cfg.get("default_timeout_sec", 1800))
    return cfg


def project_path(cfg: dict, project_id: str) -> str | None:
    project = cfg["projects"].get(project_id)
    if not project:
        return None
    path = Path(project["path"])
    if not path.is_dir() or str(path.resolve()) != project["realpath"]:
        return None
    if "identity" in project and project["identity"] != [path.stat().st_dev, path.stat().st_ino]:
        return None
    return project["realpath"]
