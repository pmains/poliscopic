#!/usr/bin/env python3
"""Classify checkout changes into reviewable repository-hygiene buckets.

The audit is read-only. It never stages, moves, deletes, or rewrites files.
Its purpose is to make a large dirty checkout legible before coherent commits
are assembled.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True, slots=True)
class Change:
    status: str
    path: str
    category: str


def classify_path(path: str) -> str:
    """Classify one repository-relative path without inspecting its contents."""
    normalized = path.removeprefix("./")
    name = Path(normalized).name

    if normalized == ".env.example":
        return "runtime_configuration"
    if name == ".env" or name.startswith(".env.") or normalized.endswith(
        ("credentials", "credentials.json")
    ):
        return "local_secret"
    if normalized.startswith(("data/", "logs/", "tmp/", ".cache/")):
        return "generated_or_local_state"
    if normalized.startswith("tests/"):
        return "tests"
    if normalized.startswith("docs/briefs/"):
        return "durable_roadmap"
    if normalized == "briefs/PRODUCTION-OPERATIONS-CHECKLIST.md":
        return "operational_documentation"
    if normalized.startswith("briefs/"):
        return "research_evidence"
    if normalized in {
        ".gitignore",
        "README.md",
        "MANIFEST.in",
        "pyproject.toml",
        "requirements.txt",
        "uv.lock",
    } or normalized.startswith("scripts/README"):
        return "project_configuration"
    if normalized.startswith(("routes/", "templates/", "static/", "src/", "workflows/")):
        return "runtime_source"
    if normalized.startswith(("scripts/kg/", "scripts/entities/", "scripts/benchmark/")):
        return "research_source"
    if normalized.startswith("scripts/") or normalized in {"app.py", "wsgi.py"}:
        return "runtime_source"
    return "unclassified"


def parse_porcelain(payload: bytes) -> list[Change]:
    """Parse ``git status --porcelain=v1 -z`` output."""
    fields = payload.split(b"\0")
    changes: list[Change] = []
    index = 0
    while index < len(fields):
        field = fields[index]
        index += 1
        if not field:
            continue
        decoded = field.decode("utf-8", errors="surrogateescape")
        status = decoded[:2]
        path = decoded[3:]
        if "R" in status or "C" in status:
            # Under ``-z`` Git emits the destination first and the source
            # second. Classify the destination while consuming both fields.
            if index < len(fields) and fields[index]:
                index += 1
        changes.append(Change(status=status, path=path, category=classify_path(path)))
    return changes


def collect_changes(root: Path = REPO_ROOT) -> list[Change]:
    """Read the current Git worktree status."""
    result = subprocess.run(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return parse_porcelain(result.stdout)


def build_report(changes: list[Change]) -> dict[str, object]:
    """Build a deterministic machine-readable hygiene report."""
    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for change in sorted(changes, key=lambda item: (item.category, item.path)):
        grouped[change.category].append(asdict(change))
    counts = Counter(change.category for change in changes)
    return {
        "kind": "repository-hygiene-audit",
        "read_only": True,
        "total_changes": len(changes),
        "counts": dict(sorted(counts.items())),
        "attention": {
            "local_secrets_present": counts["local_secret"],
            "unclassified_paths": counts["unclassified"],
        },
        "groups": dict(sorted(grouped.items())),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--summary",
        action="store_true",
        help="print only counts and attention flags",
    )
    args = parser.parse_args()
    report = build_report(collect_changes())
    if args.summary:
        report = {
            key: report[key]
            for key in ("kind", "read_only", "total_changes", "counts", "attention")
        }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
