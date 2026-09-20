#!/usr/bin/env python3
"""Backup-receipt contract for the design-only receipt-store packet.

A future apply must hold a **canonically loaded** backup: the receipt is a
digest-bound artifact verified by the existing authoritative verifiers, mode 0600,
from the exact apply target, with a present dump whose digest matches, a proven
clean restore, a freshness window, and a verified baseline whose schema signature the
receipt carries.  Nothing here writes or connects to a database.
"""

from __future__ import annotations

import hashlib
import stat
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from scripts.kg import stage1_backup_receipt as backup_stage1
from scripts.kg import stage2_backup_verify as backup_verify
from scripts.kg.stage2_artifacts import is_obsolete, load_verified

REPO = Path(__file__).resolve().parents[2]

#: A backup older than this may not be used for an apply.
MAX_BACKUP_AGE_SECONDS = 86400
BACKUP_RECEIPT_KIND = backup_verify.RECEIPT_KIND
TARGET_FIELDS = backup_verify.TARGET_FIELDS
BACKUP_REQUIREMENT = {
    "receipt_kind": BACKUP_RECEIPT_KIND,
    "canonical_load": "stage2_artifacts.load_verified (the receipt must be digest-bound)",
    "verifier": {"module": "scripts/kg/stage2_backup_verify.py",
                 "version": backup_verify.VERIFY_VERSION,
                 "entry_point": "validate_stage2_receipt"},
    "restore_proof": {"module": "scripts/kg/stage1_backup_receipt.py",
                      "entry_point": "restore_verified"},
    "mode": "0o600",
    "target_fields": list(TARGET_FIELDS),
    "dump": "must exist and its sha256 must equal the receipt's dump_sha256",
    "freshness_seconds": MAX_BACKUP_AGE_SECONDS,
    "baseline_binding": "the bound baseline artifact must exist, be non-obsolete, match the "
                        "receipt's baseline digest, pass validate_baseline, and carry the "
                        "receipt's schema signature",
}


def file_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Hash retained dumps without materialising a multi-gigabyte file in RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def freshness_problem(created_at: Any, *, now: datetime | None,
                      max_age_seconds: int = MAX_BACKUP_AGE_SECONDS) -> str | None:
    """Refuse a stale, future-dated, or unparseable backup."""
    if not created_at:
        return "the backup receipt records no creation time"
    try:
        parsed = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
    except ValueError:
        return "the backup receipt creation time is unparseable"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - parsed).total_seconds()
    if age < 0:
        return "the backup receipt is dated in the future"
    if age > max_age_seconds:
        return f"the backup receipt is stale: {int(age)}s old exceeds {max_age_seconds}s"
    return None


def baseline_binding_problems(receipt_document: Mapping[str, Any], repo: Path = REPO) -> list[str]:
    """The receipt must bind a real, current, verified baseline."""
    block = receipt_document.get("stage2_verification") or {}
    baseline_path = Path(str(block.get("baseline_path") or ""))
    if not baseline_path.is_file():
        return ["the bound backup baseline is absent"]
    problems: list[str] = []
    if is_obsolete(baseline_path):
        problems.append("the bound backup baseline is obsolete")
    try:
        baseline = load_verified(baseline_path)
    except Exception as exc:  # noqa: BLE001 - a failed read is a refusal
        return problems + [f"the bound backup baseline failed verification: {exc}"]
    if block.get("baseline_digest") != baseline.get("digest"):
        problems.append("the receipt binds another baseline digest")
    problems.extend(backup_verify.validate_baseline(baseline))
    receipt_target = receipt_document.get("target") or {}
    baseline_target = baseline.get("target") or {}
    for field in TARGET_FIELDS:
        if baseline_target.get(field) != receipt_target.get(field):
            problems.append(f"the baseline target {field} differs from the backup receipt target")
    signatures = receipt_document.get("signatures") or {}
    baseline_schema = (baseline.get("schema_signature") or {}).get("schema_sha256")
    if signatures.get("schema_sha256") != baseline_schema:
        problems.append("the receipt schema signature differs from the bound baseline")
    return problems


def backup_problems(backup_path: str | Path | None, *, target: Mapping[str, Any],
                    now: datetime | None = None,
                    max_age_seconds: int = MAX_BACKUP_AGE_SECONDS,
                    repo: Path = REPO) -> list[str]:
    """Canonically load and fully validate the backup a future apply must hold."""
    if backup_path is None:
        return ["a backup receipt path is required"]
    path = Path(str(backup_path))
    if not path.is_file():
        return [f"the backup receipt is absent: {path.name}"]
    problems: list[str] = []
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        problems.append("the backup receipt mode is not 0o600")
    try:
        document = load_verified(path)
    except Exception as exc:  # noqa: BLE001 - a failed read is a refusal
        return problems + [f"the backup receipt failed canonical verification: {exc}"]
    if document.get("kind") != BACKUP_RECEIPT_KIND:
        problems.append(f"the backup receipt kind is not {BACKUP_RECEIPT_KIND}")
    problems.extend(backup_verify.validate_stage2_receipt(document))
    if not backup_stage1.restore_verified(document):
        problems.append("the backup receipt does not prove a clean restore")
    receipt_target = document.get("target") or {}
    for field in TARGET_FIELDS:
        if receipt_target.get(field) != target.get(field):
            problems.append(f"the backup target {field} differs from the apply target")
    dump_path = Path(str(document.get("dump_path") or ""))
    if not dump_path.is_file():
        problems.append("the backup receipt names a missing dump")
    elif file_sha256(dump_path) != document.get("dump_sha256"):
        problems.append("the dump digest does not match the receipt")
    freshness = freshness_problem(document.get("created_at"), now=now,
                                  max_age_seconds=max_age_seconds)
    if freshness:
        problems.append(freshness)
    problems.extend(baseline_binding_problems(document, repo))
    return problems
