# Optional Linux Hub installation

This installer is for an operator-controlled Linux server with systemd, Python 3.12+ and a separately managed HTTPS reverse proxy. It does not obtain certificates or modify Nginx configuration.

From a verified checkout, create the runtime-source package:

```sh
.venv/bin/python deploy/xvis/package_release.py
```

The command prints the archive path and hash under ignored `local_private/deploy/`. Inspect the archive; transfer it and its `.sha256` sidecar to your server. Invoke `install_server.sh` explicitly as root with the archive, checksum and absolute Python executable. An optional fourth argument supplies a verified wheelhouse containing `wheels.sha256`.

The installer creates an `xvis` service account, `/opt/xvis/releases/`, `/etc/xvis/` and `/var/lib/xvis/`. It preserves existing credentials/configuration, backs up an existing database and service, and switches the service with rollback on error. Review permissions, backup storage and the script before invocation on your server.

```text
install_server.sh release.tar.gz release.tar.gz.sha256 /absolute/path/to/python3
```

The Hub listens on loopback port 18300; change it consistently in the private Hub config and reverse proxy if needed. Include `nginx-location.conf` inside your own HTTPS server block and validate with `nginx -t` before reloading. Keep `/xvis/` as the external prefix unless you deliberately update the Hub `root_path` and clients.

Read [deployment](../../docs/deployment.md), [privacy](../../docs/privacy.md) and [security](../../SECURITY.md). This source package does not bundle Python, DSH, model credentials or third-party wheels. CI and local loopback tests do not prove your Linux deployment or real APNs delivery.
