#!/bin/bash
# 安装独立用户 LaunchAgent；拒绝覆盖现有同名配置。
set -eu
REPO="$(cd "$(dirname "$0")/../.." && pwd)"
PYTHON_BIN="${XVIS_PYTHON:-$REPO/.venv/bin/python}"
NODE_CONFIG="${1:-$REPO/node/config.local.yaml}"
NODE_ENV="${2:-$REPO/local_private/dsh-node/env.json}"
TARGET="$HOME/Library/LaunchAgents/com.xvis.dsh-node.plist"
if [ "$(uname)" != "Darwin" ]; then
    echo "此安装器仅适用于 macOS" >&2
    exit 1
fi
if [ -e "$TARGET" ]; then
    echo "已存在 $TARGET；请先按卸载步骤移除，安装器不会覆盖。" >&2
    exit 1
fi
if [ ! -f "$NODE_CONFIG" ] || [ ! -f "$NODE_ENV" ]; then
    echo "请先准备节点独立配置及环境文件，均设置权限 600。" >&2
    exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents" "$REPO/local_private/dsh-node/service-logs"
chmod 700 "$REPO/local_private/dsh-node/service-logs"
"$PYTHON_BIN" - "$TARGET" "$PYTHON_BIN" "$REPO" "$NODE_CONFIG" "$NODE_ENV" <<'PY'
import os,plistlib,sys
from pathlib import Path
target,python,repo,config,env=sys.argv[1:]
for value in (python,repo,config,env):
    if not Path(value).is_absolute(): raise SystemExit('所有安装路径必须为绝对路径')
for private in (config,env):
    if Path(private).stat().st_mode & 0o077: raise SystemExit('节点私有文件必须设置权限 600')
definition={
 'Label':'com.xvis.dsh-node',
 'ProgramArguments':[python,repo+'/scripts/install/run_node_macos.py','--config',config,'--env',env],
 'WorkingDirectory':repo,'RunAtLoad':True,'KeepAlive':True,'ThrottleInterval':30,
 'StandardOutPath':repo+'/local_private/dsh-node/service-logs/launchd.out.log',
 'StandardErrorPath':repo+'/local_private/dsh-node/service-logs/launchd.err.log',
 'Umask':63,
}
with open(target,'xb') as stream: plistlib.dump(definition,stream)
os.chmod(target,0o600)
PY
plutil -lint "$TARGET"
launchctl bootstrap "gui/$(id -u)" "$TARGET"
echo "已安装：$TARGET"
echo "停止：launchctl bootout gui/$(id -u)/com.xvis.dsh-node"
echo "卸载：先停止，再删除该 plist；保留 journal 以免丢失重复执行保护记录。"
