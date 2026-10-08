#!/usr/bin/env python3
"""Upsert reviewed image assets under ``static/uploads`` to production.

This is deliberately not a general deploy tool.  It accepts regular PNG/JPEG/WebP
files already inside the repository's ``static/uploads`` directory, refuses
symlinks and every other path/type, invokes the production interlock for its exact
scope and mode, and transfers without ``--delete`` or a service restart.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
UPLOADS = (ROOT / "static" / "uploads").resolve()
ENTRY_POINT = "scripts/ops/sync_static_uploads.py"
SCOPE = ["static/uploads:image-upsert"]
ALLOWED_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp"}

for candidate in (str(ROOT), str(ROOT / "scripts")):
    if candidate not in sys.path:
        sys.path.insert(0, candidate)

from ops.production_interlock_guard import require_production_interlock  # noqa: E402


class Refused(RuntimeError):
    pass


def validate_asset(value: str) -> Path:
    path = (ROOT / value).resolve() if not Path(value).is_absolute() else Path(value).resolve()
    try:
        path.relative_to(UPLOADS)
    except ValueError as exc:
        raise Refused(f"asset is outside static/uploads: {value}") from exc
    if path == UPLOADS or not path.is_file() or path.is_symlink():
        raise Refused(f"asset must be a regular, non-symlink file: {value}")
    if path.suffix.lower() not in ALLOWED_SUFFIXES:
        raise Refused(f"asset type is not allowed: {path.suffix}")
    return path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sync_asset(path: Path) -> dict[str, object]:
    host = os.environ.get("POLISCOPIC_DEPLOY_HOST", "").strip()
    if not host:
        raise Refused("POLISCOPIC_DEPLOY_HOST is required")
    destination = f"{host}:/opt/poliscopic/static/uploads/{path.name}"
    command = ["rsync", "-avz", "--checksum", "--no-owner", "--no-group",
               str(path), destination]
    # There is intentionally no --delete, shell, wildcard, directory source, or
    # restart.  One validated file maps to one fixed remote filename.
    subprocess.run(command, cwd=ROOT, check=True)

    url = f"https://poliscopic.com/static/uploads/{path.name}"
    with urllib.request.urlopen(url, timeout=30) as response:
        remote = response.read()
        status = response.status
    local_digest = sha256(path)
    remote_digest = hashlib.sha256(remote).hexdigest()
    if status != 200 or remote_digest != local_digest:
        raise Refused(
            f"post-transfer verification failed: status={status}, "
            f"local_sha256={local_digest}, remote_sha256={remote_digest}")
    return {"path": str(path.relative_to(ROOT)), "url": url, "bytes": path.stat().st_size,
            "sha256": local_digest, "http_status": status}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assets", nargs="+")
    args = parser.parse_args(argv)

    try:
        assets = [validate_asset(value) for value in args.assets]
        require_production_interlock("OP-CODE", ENTRY_POINT, scope=SCOPE,
                                     mode="upsert")
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        for asset in assets:
            print(sync_asset(asset))
    except (Refused, subprocess.CalledProcessError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
