"""Launchd entrypoint: read only this node's private JSON environment file."""
import argparse
import json
import os
import sys
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("--config", required=True)
parser.add_argument("--env", required=True)
args = parser.parse_args()
env_file = Path(args.env)
if env_file.stat().st_mode & 0o077:
    raise SystemExit("节点环境文件必须设置权限 600")
values = json.loads(env_file.read_text())
allowed = {"XVIS_NODE_TOKEN", "DEEPSEEK_API_KEY", "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY"}
if not isinstance(values, dict) or any(k not in allowed or not isinstance(v, str) for k, v in values.items()):
    raise SystemExit("节点环境文件含不支持的字段")
os.environ.update(values)
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from node.dsh_agent import main
sys.argv = [sys.argv[0], "--config", args.config]
main()
