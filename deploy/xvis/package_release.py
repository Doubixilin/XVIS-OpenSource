#!/usr/bin/env python3
"""Package only XVIS runtime sources; never include private configuration/data."""
import hashlib
import json
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "local_private" / "deploy"


def public_metadata(info):
    """Do not put the builder's local account identity in release archives."""
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    info.pax_headers = {}
    return info


def main():
    files = [ROOT / name for name in (
        "LICENSE", "THIRD_PARTY_NOTICES.md", "requirements-lock.txt", "docs/assets.md", "docs/dependencies.json", "hub/gateway/__init__.py", "hub/gateway/app.py", "hub/gateway/node_registry.py", "hub/gateway/v1_repo.py", "hub/gateway/images.py", "hub/gateway/assistant_profile.py", "hub/gateway/inbox.py", "hub/gateway/notifications.py",
        "hub/gateway/artifacts.py", "hub/gateway/project_context.py", "hub/gateway/interactions.py", "hub/gateway/continuous.py", "hub/gateway/pairing.py",
        "shared/file_contract.py", "shared/interaction_contract.py", "shared/control_contract.py", "shared/__init__.py", "shared/v1_db.py", "shared/v1_schemas.py", "scripts/start_hub.py")]
    for directory in ("deploy/xvis",):
        for path in (ROOT / directory).rglob("*"):
            if path.is_file() and not path.is_symlink() and "__pycache__" not in path.parts and ".local." not in path.name:
                if path.suffix in (".py", ".service", ".conf", ".json", ".txt", ".sh", ".md", ".example"):
                    files.append(path)
    manifest = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(set(files))}
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    OUT.mkdir(parents=True, exist_ok=True)
    target = OUT / f"xvis-{digest[:16]}.tar.gz"
    manifest_path = OUT / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    with tarfile.open(target, "w:gz") as archive:
        for path in sorted(set(files)):
            archive.add(path, arcname=str(path.relative_to(ROOT)), recursive=False, filter=public_metadata)
        archive.add(manifest_path, arcname="manifest.json", filter=public_metadata)
    checksum = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix + ".sha256").write_text(f"{checksum}  {target.name}\n")
    print(json.dumps({"release": digest[:16], "archive": str(target), "sha256": checksum,
                      "files": len(manifest)}, indent=2))


if __name__ == "__main__":
    main()
