#!/usr/bin/env python3
"""Read-only manifest of the working tree for release-control purposes.

The dirty checkout must never again be the deploy source, and its state must be
reproducible on demand.  This tool records, without mutating anything:

  * git HEAD and diff statistics
  * every modified / deleted / untracked path reported by git
  * SHA-256 for each path where a regular file exists (and a size otherwise)
  * the total counts, so a later reviewer can compare snapshots

It deliberately does NOT walk large binary trees it does not need: paths under
``data/`` and ``.git/`` get size only, and files above ``MAX_HASH_BYTES`` are
recorded as unhashed with their size.  That is a stated limitation, not a silent
one.

Read-only.  Writes exactly one JSON artifact under data/audit.

Usage:
    python3 scripts/ops/tree_manifest.py [--out PATH]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MAX_HASH_BYTES = 32 * 1024 * 1024          # hash files up to 32 MiB
SKIP_HASH_PREFIXES = ("data/", ".git/", ".venv/", "node_modules/")


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ("git",) + args, cwd=_REPO_ROOT, capture_output=True,
            text=True, timeout=120, check=False,
        ).stdout.strip()
    except Exception as exc:                                  # pragma: no cover
        return f"<error: {exc}>"


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry(rel: str, status: str) -> dict:
    p = _REPO_ROOT / rel
    rec: dict = {"path": rel, "status": status}
    if p.is_symlink():
        rec.update({"kind": "symlink", "target": os.readlink(p)})
        return rec
    if p.is_dir():
        rec["kind"] = "directory"
        return rec
    if not p.exists():
        rec["kind"] = "absent"
        return rec
    size = p.stat().st_size
    rec.update({"kind": "file", "bytes": size})
    rel_posix = rel.replace(os.sep, "/")
    if rel_posix.startswith(SKIP_HASH_PREFIXES):
        rec["sha256"] = None
        rec["hash_skipped"] = "large binary tree (data/.git/.venv)"
    elif size > MAX_HASH_BYTES:
        rec["sha256"] = None
        rec["hash_skipped"] = f"size {size} exceeds {MAX_HASH_BYTES}"
    else:
        rec["sha256"] = sha256(p)
    return rec


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path,
                    default=_REPO_ROOT / "data" / "audit")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    head = _git("rev-parse", "HEAD")
    branch = _git("rev-parse", "--abbrev-ref", "HEAD")
    porcelain = _git("status", "--porcelain")
    diff_stat = _git("diff", "--stat")

    paths: list[tuple[str, str]] = []
    for line in porcelain.splitlines():
        if len(line) < 4:
            continue
        code, rel = line[:2], line[3:].strip()
        # git reports renames as "old -> new"; keep both sides
        if " -> " in rel:
            old, _, new = rel.partition(" -> ")
            paths.append((old, "R-old:" + code))
            paths.append((new, "R-new:" + code))
        else:
            paths.append((rel.strip('"'), code))

    entries = [entry(rel, code) for rel, code in paths]
    counts: dict[str, int] = {}
    for _rel, code in paths:
        counts[code] = counts.get(code, 0) + 1

    manifest = {
        "kind": "worktree-manifest",
        "created_at": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "mode": "read-only",
        "repo": str(_REPO_ROOT),
        "git": {
            "head": head,
            "branch": branch,
            "diff_stat_tail": diff_stat.splitlines()[-1] if diff_stat else "",
            "tracked_modified_or_deleted": sum(
                n for c, n in counts.items() if c.strip() and c.strip()[0] in "MADR"),
            "untracked": counts.get("??", 0),
        },
        "status_counts": counts,
        "total_paths": len(entries),
        "limits": {
            "max_hash_bytes": MAX_HASH_BYTES,
            "hash_skipped_prefixes": list(SKIP_HASH_PREFIXES),
            "note": ("paths under data/ are sized but not hashed; hashing the "
                     "whole tree is neither necessary nor affordable"),
        },
        "paths": sorted(entries, key=lambda e: e["path"]),
    }
    manifest["manifest_digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in manifest.items()
                    if k != "manifest_digest"}, sort_keys=True, default=str).encode()
    ).hexdigest()

    out = args.out / f"tree-manifest-{manifest['created_at']}.json"
    out.write_text(json.dumps(manifest, indent=2))
    os.chmod(out, 0o600)

    g = manifest["git"]
    print("=" * 74)
    print("WORKTREE MANIFEST (read-only)")
    print("=" * 74)
    print(f"  HEAD            {g['head']}")
    print(f"  branch          {g['branch']}")
    print(f"  modified/deleted (tracked) {g['tracked_modified_or_deleted']}")
    print(f"  untracked                  {g['untracked']}")
    print(f"  total paths recorded       {manifest['total_paths']}")
    hashed = sum(1 for e in entries if e.get("sha256"))
    print(f"  hashed                     {hashed}")
    print(f"  manifest_digest {manifest['manifest_digest']}")
    print(f"  artifact        {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
