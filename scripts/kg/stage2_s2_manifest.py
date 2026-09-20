#!/usr/bin/env python3
"""``stage2_s2_manifest.py`` — a derived index of the plan series.

``superseded_by`` needs to know which plan, if any, names a given plan as its
predecessor.  Scanning the directory answers that correctly but reads and
digest-verifies every plan file on every apply — and S2 plans are tens of
megabytes each, so the cost grows with the series.

This module keeps a small index beside the plans.  Two properties matter:

* **It is a cache, not evidence.**  Every fact in it is re-derivable from the
  plan files themselves, so it is written atomically by replacement.  An index
  that could not be rebuilt would be a second source of truth, which is exactly
  what it must not become.
* **Stale is never trusted.**  A plan file newer than the index invalidates it
  and the caller falls back to a full scan, which then refreshes the index.  An
  index that cannot be read is treated the same way.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "MANIFEST_NAME",
    "build_manifest",
    "find_successors",
    "load_manifest",
    "manifest_is_fresh",
    "plans_digest",
    "plan_files",
    "save_manifest",
    "scan_successors",
]

MANIFEST_NAME = "kg-stage2-s2-manifest.json"
MANIFEST_VERSION = "kg-stage2-s2-manifest/1.0"


def plan_files(out_dir: str | Path) -> list[Path]:
    """Every plan artifact, oldest first."""
    return sorted(Path(out_dir).glob("kg-stage2-s2-plan-*.json"),
                  key=lambda p: (p.stat().st_mtime, p.name))


def _newest_mtime(files: list[Path]) -> float:
    return max((p.stat().st_mtime for p in files), default=0.0)


def build_manifest(out_dir: str | Path, files: list[Path] | None = None) -> dict[str, Any]:
    """Derive the index from the plan files themselves."""
    files = plan_files(out_dir) if files is None else files
    plans: dict[str, Any] = {}
    for path in files:
        try:
            document = artifacts.load_verified(path)
        except Exception:
            continue
        supersedes = document.get("supersedes") or {}
        plans[str(document.get("plan_id"))] = {
            "path": path.name,
            "digest": artifacts.recorded_digest(document),
            "supersedes_plan_id": supersedes.get("plan_id"),
        }
    return {"version": MANIFEST_VERSION, "plans": plans,
            "plans_digest": plans_digest(plans),
            "newest_mtime": _newest_mtime(files),
            "generated_at": datetime.now(timezone.utc).isoformat()}


def plans_digest(plans: Mapping[str, Any]) -> str:
    """Integrity over the indexed facts, so casual corruption is detected.

    A doctored index must not be able to *hide* a successor: that would let a
    superseded plan be applied.  It could only ever produce a false refusal,
    which is the fail-closed direction, but detecting it is still cheaper than
    reasoning about it.
    """
    body = json.dumps(dict(plans), sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def manifest_is_fresh(manifest: Mapping[str, Any] | None, out_dir: str | Path) -> bool:
    """Fresh means: readable, right version, and no plan file is newer."""
    if not manifest or manifest.get("version") != MANIFEST_VERSION:
        return False
    if manifest.get("plans_digest") != plans_digest(manifest.get("plans") or {}):
        return False                  # tampered or corrupt: fall back to a scan
    try:
        return _newest_mtime(plan_files(out_dir)) <= float(manifest.get("newest_mtime", -1))
    except OSError:
        return False


def load_manifest(out_dir: str | Path) -> dict[str, Any] | None:
    """Read the index, or ``None`` if it is missing, unreadable or corrupt."""
    path = Path(out_dir) / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def save_manifest(out_dir: str | Path, manifest: Mapping[str, Any]) -> Path:
    """Replace the index atomically.  A cache, so replacement is correct."""
    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / MANIFEST_NAME
    temporary = directory / f".{MANIFEST_NAME}.tmp"
    payload = json.dumps(dict(manifest), indent=2, sort_keys=True) + "\n"
    descriptor = os.open(str(temporary), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()
    return target


def scan_successors(out_dir: str | Path, plan_id: str) -> list[str]:
    """The authoritative answer: read every plan and look for the reference."""
    successors: list[str] = []
    for path in sorted(Path(out_dir).glob("kg-stage2-s2-plan-*.json")):
        try:
            document = artifacts.load_verified(path)
        except Exception:
            continue
        if (document.get("supersedes") or {}).get("plan_id") == plan_id:
            successors.append(str(document.get("plan_id")))
    return successors


def find_successors(out_dir: str | Path, plan_id: str) -> list[str]:
    """Answer from the index when it is fresh, else scan and refresh it.

    The scan is always authoritative; the index only ever shortens the work.
    """
    manifest = load_manifest(out_dir)
    if manifest_is_fresh(manifest, out_dir):
        plans = manifest.get("plans") or {}
        return sorted(pid for pid, entry in plans.items()
                      if entry.get("supersedes_plan_id") == plan_id)
    successors = scan_successors(out_dir, plan_id)
    try:
        save_manifest(out_dir, build_manifest(out_dir))
    except OSError:
        pass                      # an unwritable index must not block an apply
    return successors
