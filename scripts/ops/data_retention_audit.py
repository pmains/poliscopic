#!/usr/bin/env python3
"""Read-only inventory for the repository-local data directory.

The report deliberately distinguishes logical path bytes from unique allocated
bytes so hard-linked retention holds are not counted as duplicate disk use.
It never hashes, moves, compresses, or deletes files.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def classify(relative: Path) -> str:
    parts = relative.parts
    top = parts[0] if parts else ""
    suffix = relative.suffix.lower()
    name = relative.name.lower()

    if top.startswith("retention-hold-") or top == "holds":
        return "held_evidence"
    if top == "backups":
        return "database_backup_evidence"
    if top in {"doc_downloads", "agendas", "agenda-items", "supporting-materials"}:
        return "source_evidence"
    if top in {"kg-review-labels", "audit", "archive"}:
        return "human_or_audit_evidence"
    if top in {"kg-plans", "document-layout", "reports"}:
        return "pipeline_artifact"
    if top == "document-layout-benchmark" and len(parts) > 1 and parts[1] == "pdf-cache":
        return "reproducible_cache"
    if top in {"benchmark", "models"} or name.startswith("role_classifier"):
        return "model_or_training_artifact"
    if suffix == ".log" or name.endswith(".log.gz"):
        return "routine_log"
    if suffix in {".pid", ".lock", ".part", ".tmp"}:
        return "transient_marker"
    if top == "runs":
        return "workflow_run"
    return "unclassified"


def inventory(root: Path, *, largest: int = 25, now: float | None = None) -> dict[str, Any]:
    root = root.resolve()
    current = time.time() if now is None else now
    counts: Counter[str] = Counter()
    logical: Counter[str] = Counter()
    top_counts: Counter[str] = Counter()
    top_logical: Counter[str] = Counter()
    extensions: Counter[str] = Counter()
    age_counts: Counter[str] = Counter()
    largest_rows: list[dict[str, Any]] = []
    seen_inodes: set[tuple[int, int]] = set()
    unique_apparent_bytes = 0
    unique_allocated_bytes = 0
    logical_bytes = 0
    symlinks = 0
    errors: list[dict[str, str]] = []

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for filename in filenames:
            path = Path(directory) / filename
            try:
                stat = path.lstat()
            except OSError as exc:
                errors.append({"path": str(path), "error": str(exc)})
                continue
            relative = path.relative_to(root)
            if path.is_symlink():
                symlinks += 1
                continue
            if not path.is_file():
                continue
            category = classify(relative)
            top = relative.parts[0]
            size = stat.st_size
            allocated = getattr(stat, "st_blocks", 0) * 512
            logical_bytes += size
            counts[category] += 1
            logical[category] += size
            top_counts[top] += 1
            top_logical[top] += size
            extensions[relative.suffix.lower() or "[none]"] += 1
            inode = (stat.st_dev, stat.st_ino)
            if inode not in seen_inodes:
                seen_inodes.add(inode)
                unique_apparent_bytes += size
                unique_allocated_bytes += allocated
            age_days = max(0, int((current - stat.st_mtime) // 86400))
            if age_days > 90:
                age_counts[">90d"] += 1
            if age_days > 30:
                age_counts[">30d"] += 1
            if age_days > 7:
                age_counts[">7d"] += 1
            largest_rows.append({
                "path": str(relative),
                "size": size,
                "allocated_bytes": allocated,
                "link_count": stat.st_nlink,
                "category": category,
                "mtime": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
            })

    largest_rows.sort(key=lambda row: row["size"], reverse=True)
    by_category = {
        key: {"files": counts[key], "logical_bytes": logical[key]}
        for key in sorted(counts)
    }
    by_top_level = {
        key: {"files": top_counts[key], "logical_bytes": top_logical[key]}
        for key in sorted(top_counts, key=lambda value: top_logical[value], reverse=True)
    }
    return {
        "kind": "data-retention-read-only-inventory",
        "version": "1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "root": str(root),
        "mode": "read-only",
        "file_count": sum(counts.values()),
        "symlink_count": symlinks,
        "logical_path_bytes": logical_bytes,
        "unique_inode_apparent_bytes": unique_apparent_bytes,
        "unique_inode_allocated_bytes": unique_allocated_bytes,
        "hardlink_savings_apparent_bytes": logical_bytes - unique_apparent_bytes,
        "age_counts": dict(sorted(age_counts.items())),
        "by_category": by_category,
        "by_top_level": by_top_level,
        "extensions": dict(extensions.most_common()),
        "largest_files": largest_rows[:largest],
        "errors": errors,
        "limitations": [
            "No hashes or reference checks are performed.",
            "APFS clones may share blocks while retaining distinct inodes.",
            "A category is a review aid, not deletion authorization.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("data"))
    parser.add_argument("--largest", type=int, default=25)
    parser.add_argument("--output", type=Path, help="create a new JSON file; refuses overwrite")
    args = parser.parse_args()
    report = inventory(args.root, largest=max(args.largest, 0))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
        print(args.output)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
