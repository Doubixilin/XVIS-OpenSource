# Deployment

Use a private test directory first. This guide configures a single trusted owner, one Hub and one macOS execution Node. It does not enable a public multi-user execution service.

## 1. Python and Hub configuration

Install Python 3.12+, create a virtual environment and install the locked dependencies:

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
mkdir -p local_private
chmod 700 local_private
cp hub/config.local.yaml.example hub/config.local.yaml
chmod 600 hub/config.local.yaml
```

The default Hub uses port 18300 on loopback. Change the port if it is occupied. The startup code checks the listener and does not kill an existing service.

Generate separate credentials into a new private environment file (the exclusive creation fails if the file already exists):

```sh
.venv/bin/python - <<'PY'
import os, secrets
fd = os.open('local_private/hub.env', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, 'w') as f:
    f.write('XVIS_APP_TOKEN=' + secrets.token_urlsafe(32) + '\n')
    f.write('XVIS_NODE_TOKEN=' + secrets.token_urlsafe(32) + '\n')
PY
```

In a dedicated shell, load these values and start the Hub:

```sh
set -a
. local_private/hub.env
set +a
.venv/bin/python scripts/start_hub.py --config hub/config.local.yaml
```

`GET http://127.0.0.1:18300/health` is an unauthenticated health check. Keep task endpoints authenticated. Only the App credential belongs in the phone; the static Node uses its separate Node credential.

## 2. HTTPS deployment

For remote access, use a server you control with a valid HTTPS certificate. Set `root_path: /xvis` when exposing `https://example.com/xvis/`. The [Nginx fragment](../deploy/xvis/nginx-location.conf) belongs inside **your** existing HTTPS server block. Review it, adapt its port, run `nginx -t`, then reload your server. No script automatically modifies an existing site.

Keep request buffering behavior, WSS upgrade headers and disabled response buffering for foreground streaming. The fragment permits a 17 MiB transport envelope; the application separately enforces 16 MiB file, 6 MiB image and 64 KiB JSON limits.

The [optional Linux installer](../deploy/xvis/README.md) uses a systemd service, a single worker, `/etc/xvis/` configuration and `/var/lib/xvis/` persistent data. It requires root and explicit invocation; testing the source does not install it. Back up database and attachments before an upgrade.

## 3. DSH runtime and macOS Node

The adapter checks exact runtime versions: **Node.js 24.21.0** and **`@deepseek-ai/dsh` 0.2.1-alpha.1**. Do not substitute an arbitrary latest release: Host and event APIs can differ. Upstream source reference is `5badb15009ae1756c3afe0ae0cef1faafc290ccc`.

Obtain the runtime through the official [Node.js distribution](https://nodejs.org/download/release/) and [DeepSeek Harness project](https://github.com/deepseek-ai/deepseek-harness). When the pinned npm release is available, its isolated installation command is:

```sh
mkdir -p local_private/dsh-validation/runtime local_private/workspace
npm install --prefix local_private/dsh-validation/runtime --save-exact @deepseek-ai/dsh@0.2.1-alpha.1
cp node/config.local.yaml.example node/config.local.yaml
chmod 600 node/config.local.yaml
```

Replace every `/absolute/path/to/xvis` in the Node template with the actual checkout path. Set `node_bin` to the selected Node executable; it need not live inside the checkout. Set `hub_ws_url` to your own `wss://.../xvis/ws/node` endpoint and supply the matching static `XVIS_NODE_TOKEN` via the environment or a private config field. Node paths must be absolute; the sample is intentionally not runnable unchanged.

Configure your own model access using `DEEPSEEK_API_KEY` / the pinned DSH's documented configuration. Keep credentials out of the repository and use a fresh `dsh_home` independent of `~/.dsh`. The Node does not copy credentials from the App or a desktop DSH session.

Model access configuration does not imply arbitrary provider support. Headless uses its independent DSH configuration; the current Host adapter explicitly selects `deepseek-official / deepseek-flash`. Replacing a provider or execution engine requires separate compatibility work and validation.

```sh
.venv/bin/python scripts/start_node.py --config node/config.local.yaml
```

For interactive Host mode, set `execution_mode: host` on **only** `p-xvis-test` and set a distinct absolute `host_home` path. Other projects remain headless. First test with synthetic files; project registration alone is not an OS security sandbox.

Optional macOS login startup uses `scripts/install/install_node_macos.sh`. Prepare a separate mode-600 JSON environment file with the needed `XVIS_NODE_TOKEN` and model environment, then pass the absolute config and environment paths. The installer refuses to overwrite an existing LaunchAgent. Foreground startup is sufficient for initial setup.

## 4. iPhone and additional nodes

Build according to [ios/README.md](../ios/README.md), then enter your HTTPS endpoint and App token in Settings. The default workspace ID is `p-xvis-test` on static node `mac-a`. Keep those names for the initial template or explicitly select a registered workspace in the App.

For an additional node, `python scripts/pair_node.py --help` lists explicit runtime/workspace inputs. The CLI creates a short-lived link and, on macOS, a QR image. Confirm the device name/fingerprint in App Settings. The CLI writes a new mode-600 config but does not start the node. Never reuse one node's identity on another running machine.

APNs is optional and disabled by default. The current provider supports certificate/key authentication; `.p8` JWT authentication is not implemented. Match certificate environment and topic to the signed App. Ordinary task history remains usable without push delivery.

## 5. Data ownership and recovery scope

The Hub database and attachments are authoritative records on the server you operate, which may be a cloud server. Node journals, DSH homes and workspace files live on the execution device. App caches and the source Git repository are not full backups of these records.

The Linux installer creates an upgrade-time database backup and preserves service rollback information. This is not a complete recovery package for attachments, Node state, DSH sessions and workspaces. Operators still need to manage those backups consistently and protect credentials separately. General export/import, automated local or off-site archives and full clean-environment recovery drills remain [planned work](roadmap.md); they are not installed by this guide.
