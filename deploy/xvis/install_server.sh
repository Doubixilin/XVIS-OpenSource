#!/usr/bin/env bash
# Install the reviewed release; route insertion is a separate explicit step.
set -euo pipefail
umask 077
if [[ $(id -u) != 0 || $# -lt 3 || $# -gt 4 ]]; then
    echo 'Usage (root): install_server.sh release.tar.gz release.sha256 python-path [verified-wheelhouse]' >&2
    exit 2
fi
archive=$(realpath "$1")
checksum=$(realpath "$2")
runtime=$(realpath "$3")
"$runtime" -c 'import sys; assert sys.version_info >= (3,12), "Python 3.12+ required"'
(cd "$(dirname "$archive")" && sha256sum -c "$checksum")
release=$(basename "$archive" .tar.gz)
[[ $release =~ ^xvis-[a-f0-9]{16}$ ]] || exit 2
target=/opt/xvis/releases/$release
[[ ! -e $target ]] || { echo 'Release already exists; refusing replacement' >&2; exit 1; }
if ! id xvis >/dev/null 2>&1; then
    useradd --system --home-dir /var/lib/xvis --shell /sbin/nologin xvis
fi
install -d -m 750 -o root -g xvis /opt/xvis /opt/xvis/releases /etc/xvis
install -d -m 700 -o xvis -g xvis /var/lib/xvis
install -d -m 750 -o root -g xvis "$target"
"$runtime" - "$archive" "$target" <<'PY'
import hashlib,json,pathlib,sys,tarfile
with tarfile.open(sys.argv[1]) as archive:
    archive.extractall(sys.argv[2],filter='data')
root=pathlib.Path(sys.argv[2])
for relative,want in json.loads((root/'manifest.json').read_text()).items():
    p=(root/relative).resolve()
    assert root.resolve() in p.parents, 'Invalid release path'
    assert hashlib.sha256(p.read_bytes()).hexdigest()==want, 'Release manifest mismatch'
PY
"$runtime" -m venv "$target/.venv"
if [[ $# == 4 ]]; then
    wheelhouse=$(realpath "$4")
    (cd "$wheelhouse" && sha256sum -c wheels.sha256)
    PIP_CONFIG_FILE=/dev/null "$target/.venv/bin/pip" --isolated install --no-index --find-links "$wheelhouse" -r "$target/requirements-lock.txt"
else
    PIP_CONFIG_FILE=/dev/null "$target/.venv/bin/pip" --isolated install --index-url https://pypi.org/simple -r "$target/requirements-lock.txt"
fi
chown -R root:xvis "$target"
chmod -R g+rX "$target"
if [[ ! -e /etc/xvis/hub.env ]]; then
    "$runtime" - <<'PY' > /etc/xvis/hub.env
import secrets
print('XVIS_APP_TOKEN='+secrets.token_urlsafe(32))
print('XVIS_NODE_TOKEN='+secrets.token_urlsafe(32))
PY
    chmod 600 /etc/xvis/hub.env
fi
if [[ ! -e /etc/xvis/hub.yaml ]]; then
    install -m 640 -o root -g xvis "$target/deploy/xvis/hub.yaml.example" /etc/xvis/hub.yaml
fi
backup=/opt/xvis/backups/$(date -u +%Y%m%dT%H%M%SZ)
install -d -m 700 "$backup"
readlink /opt/xvis/current > "$backup/previous-release" || true
if [[ -e /var/lib/xvis/hub.db ]]; then
    "$runtime" - "$backup/hub.db" <<'PY'
import sqlite3,sys
with sqlite3.connect('/var/lib/xvis/hub.db') as source, sqlite3.connect(sys.argv[1]) as destination:
    source.backup(destination)
PY
fi
if [[ -e /etc/systemd/system/xvis-hub.service ]]; then
    cp -p /etc/systemd/system/xvis-hub.service "$backup/"
fi
was_active=$(systemctl is-active xvis-hub || true)
was_enabled=$(systemctl is-enabled xvis-hub 2>/dev/null || true)
rollback() {
    trap - ERR
    set +e
    systemctl stop xvis-hub
    previous=$(cat "$backup/previous-release")
    if [[ -n $previous ]]; then
        ln -s "$previous" /opt/xvis/current.rollback
        mv -Tf /opt/xvis/current.rollback /opt/xvis/current
    else
        rm -f /opt/xvis/current
    fi
    if [[ -e $backup/xvis-hub.service ]]; then
        cp -p "$backup/xvis-hub.service" /etc/systemd/system/xvis-hub.service
    else
        systemctl disable xvis-hub
        rm -f /etc/systemd/system/xvis-hub.service
    fi
    systemctl daemon-reload
    if [[ $was_enabled == enabled ]]; then systemctl enable xvis-hub; else systemctl disable xvis-hub; fi
    if [[ $was_active == active ]]; then systemctl start xvis-hub; fi
    echo "XVIS activation failed; previous service state restored. Backup: $backup" >&2
}
trap 'rollback; exit 1' ERR
ln -s "$target" /opt/xvis/current.next
mv -Tf /opt/xvis/current.next /opt/xvis/current
install -m 644 "$target/deploy/xvis/xvis-hub.service" /etc/systemd/system/xvis-hub.service
systemctl daemon-reload
systemctl enable xvis-hub
systemctl restart xvis-hub
for attempt in {1..20}; do
    if curl -fsS http://127.0.0.1:18300/health >/dev/null; then
        echo "Installed $release; backup: $backup; Nginx route still requires insertion and nginx -t"
        trap - ERR
        exit 0
    fi
    sleep 1
done
rollback
exit 1
