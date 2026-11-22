#!/usr/bin/env python3
"""Pack the fork's own plugins (plugins/*/) into the private plugin catalog.

Writes deploy/plugin-index/: index.json, plus one manifest copy and one source
tarball per plugin, flat (they are mounted from a ConfigMap, which has no
folders). The in-cluster `securo-plugin-index` serves that folder and
PLUGINS_INDEX_URL points at its index.json, so this replaces the public
catalog on this instance.

The tarball is built reproducibly (sorted, fixed times), so the same code
always gives the same checksum. Its "commit" is the sha1 of the tarball:
the runner keys an install by it, so changed code is offered as an update.

Run after changing a plugin, then commit deploy/plugin-index/:

    python3 scripts/build-plugin-index.py
"""
from __future__ import annotations

import gzip
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGINS = ROOT / "plugins"
OUT = ROOT / "deploy" / "plugin-index"
SKIP = {"__pycache__", ".pytest_cache", ".DS_Store"}


def _tarball(folder: Path) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for path in sorted(folder.rglob("*")):
            if any(part in SKIP for part in path.relative_to(folder).parts) or path.suffix == ".pyc":
                continue
            info = tar.gettarinfo(str(path), arcname=str(path.relative_to(folder)))
            info.mtime, info.uid, info.gid, info.uname, info.gname = 0, 0, 0, "", ""
            info.mode = 0o755 if path.is_dir() else 0o644
            if path.is_dir():
                tar.addfile(info)
            else:
                with path.open("rb") as handle:
                    tar.addfile(info, handle)
    return gzip.compress(raw.getvalue(), mtime=0)


def _text(value: object) -> dict[str, str]:
    return value if isinstance(value, dict) else {"en": str(value)}


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)
    entries = []
    for manifest_path in sorted(PLUGINS.glob("*/securo-plugin.json")):
        folder = manifest_path.parent
        manifest_bytes = manifest_path.read_bytes()
        manifest = json.loads(manifest_bytes)
        archive = _tarball(folder)
        commit = hashlib.sha1(archive).hexdigest()
        slug = folder.name
        (OUT / f"{slug}.manifest.json").write_bytes(manifest_bytes)
        (OUT / f"{slug}.tar.gz").write_bytes(archive)
        contributes = {kind: len(items) for kind, items in manifest.get("contributes", {}).items()}
        entries.append({
            "id": manifest["id"],
            "repository": manifest["repository"],
            "release": f"v{manifest['version']}",
            "commit": commit,
            "manifest": f"{slug}.manifest.json",
            "manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "archive": f"{slug}.tar.gz",
            "archive_sha256": hashlib.sha256(archive).hexdigest(),
            "name": _text(manifest["name"]),
            "description": _text(manifest["description"]),
            "version": manifest["version"],
            "license": manifest["license"],
            "min_securo_version": manifest["min_securo_version"],
            "contributes": contributes,
            "workspaces": manifest.get("workspaces", ["personal", "business"]),
        })
        print(f"{manifest['id']} {manifest['version']} commit {commit[:12]} ({len(archive)} bytes)")
    (OUT / "index.json").write_text(json.dumps({"plugins": entries}, indent=2) + "\n")
    files = sorted(path.name for path in OUT.iterdir())
    listing = "".join(f"      - {name}\n" for name in files)
    (OUT / "kustomization.yaml").write_text(
        "# Written by scripts/build-plugin-index.py; don't edit by hand.\n"
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "namespace: securo\n"
        "configMapGenerator:\n"
        "  - name: securo-plugin-index\n"
        "    files:\n" + listing
    )


if __name__ == "__main__":
    main()
