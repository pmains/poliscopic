#!/usr/bin/env python3
"""``event_normalize_artifacts.py`` — atomic, immutable gate attempt artifacts.

Check-then-write is not safe: two attempts can both observe "free" paths and then
both write.  Every artifact here is therefore created with ``O_CREAT | O_EXCL``,
so creation is atomic and an existing attempt can never be overwritten.

Every terminal path of a gate attempt — blocker, digest mismatch, drift, child
exit, parse error, snapshot/postflight exception, timeout, or success — ends by
writing exactly one result artifact, labelled with its status and whether it is
partial.  Nothing here contacts a database.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

__all__ = [
    "ARTIFACT_STATUSES",
    "ArtifactCollision",
    "finish",
    "reserve_attempt",
    "write_exclusive",
]

#: Every terminal status an attempt may report.
ARTIFACT_STATUSES = (
    "plan",                 # planning/verification only; never spawned
    "child_contract",       # the child could not be given a safe contract
    "target_mismatch",      # engine and supplied target disagreed
    "blocked",              # launch blockers present
    "digest_mismatch",      # --plan-digest did not match
    "target_drift",         # target changed before spawn
    "fingerprint_drift",    # code fingerprint changed before spawn
    "snapshot_error",       # preflight snapshot raised
    "spawn_error",          # the child could not be started
    "timeout",              # the child exceeded its budget
    "child_exit",           # nonzero child exit
    "parse_error",          # envelope missing/multiple/malformed
    "postflight_error",     # postflight snapshot or fingerprint raised
    "checks_failed",        # evaluation produced failures
    "success",              # every check passed
)

#: Statuses that represent a completed, non-partial attempt.
COMPLETE_STATUSES = ("success", "checks_failed")


class ArtifactCollision(RuntimeError):
    """An attempt artifact already exists; refusing to overwrite it."""


def write_exclusive(path: str | Path, text: str) -> None:
    """Create ``path`` atomically with its content, failing if it exists."""
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise ArtifactCollision(f"artifact already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)


def reserve_attempt(stem: str) -> str:
    """Atomically reserve an attempt id, so a concurrent attempt cannot share it."""
    path = f"{stem}.lock"
    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise ArtifactCollision(f"attempt already reserved: {path}") from exc
    os.close(descriptor)
    return path


def finish(
    outcome: dict[str, Any],
    paths: Mapping[str, str],
    status: str,
    *,
    detail: str | None = None,
) -> dict[str, Any]:
    """Label and persist a terminal outcome, then return it.

    The result artifact is written for *every* terminal path, so a refusal is
    always as inspectable as a success.  Partial attempts are labelled.
    """
    if status not in ARTIFACT_STATUSES:
        raise ValueError(f"unknown artifact status {status!r}")
    outcome["status"] = status
    outcome["partial"] = status not in COMPLETE_STATUSES
    if detail is not None:
        outcome["detail"] = detail
    if status not in COMPLETE_STATUSES:
        outcome.setdefault("passed", False)
    outcome.setdefault("spawned", False)
    try:
        write_exclusive(
            paths["result"],
            json.dumps(outcome, indent=2, sort_keys=True, default=str) + "\n",
        )
    except ArtifactCollision:
        # A result already exists: never overwrite, but keep the attempt labelled.
        outcome["result_artifact"] = "existing"
    return outcome


def write_evidence(paths: Mapping[str, str], name: str, payload: Any) -> None:
    """Write one immutable evidence artifact atomically."""
    write_exclusive(paths[name], json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n")


def statuses() -> Sequence[str]:
    """All recognised terminal statuses, for documentation and tests."""
    return ARTIFACT_STATUSES
