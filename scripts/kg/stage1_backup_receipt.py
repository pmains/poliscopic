#!/usr/bin/env python3
"""``stage1_backup_receipt.py`` — validate a protected-backup restore receipt.

The Stage 1 apply may only run against a development database whose full backup
has been **verified restorable**.  This module owns that proof's shape: it
validates a receipt binding the dump, its SHA-256, the ``pg_restore`` evidence,
the isolated scratch target, restored counts and schema signatures, and the
timestamps — and refuses anything incomplete, inconsistent, or not development.

This module creates no backups and performs no restore.  It carries no secrets:
credential-shaped fields are refused rather than accepted.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

__all__ = [
    "REQUIRED_FIELDS",
    "SECRET_FIELD_MARKERS",
    "counts_fingerprint",
    "restore_verified",
    "receipt_template",
    "require_receipt",
    "validate_receipt",
]

#: Every field a receipt must carry.
REQUIRED_FIELDS = (
    "dump_path",
    "dump_sha256",
    "created_at",
    "target",
    "scratch",
    "pg_restore",
    "counts",
    "signatures",
)

#: Field-name fragments that indicate credential material; refused outright.
SECRET_FIELD_MARKERS = ("password", "passwd", "secret", "token", "api_key", "private_key")

_SHA256 = re.compile(r"[0-9a-f]{64}\Z")

#: A scratch target must look isolated, never a live tier.
_SCRATCH_MARKERS = ("scratch", "restore_test", "verify", "tmp")


def counts_fingerprint(counts: Mapping[str, Any]) -> str:
    """Deterministic fingerprint over restored row counts."""
    encoded = ";".join(f"{key}={int(value)}" for key, value in sorted(counts.items()))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _check_secrets(receipt: Mapping[str, Any], problems: list[str]) -> None:
    for key in receipt:
        lowered = str(key).lower()
        if any(marker in lowered for marker in SECRET_FIELD_MARKERS):
            problems.append(f"receipt field {key!r} looks like credential material")


def _check_dump(receipt: Mapping[str, Any], problems: list[str]) -> None:
    path = receipt.get("dump_path")
    if not path or not str(path).strip():
        problems.append("dump_path is required")
    digest = receipt.get("dump_sha256")
    if not isinstance(digest, str) or not _SHA256.match(digest):
        problems.append("dump_sha256 must be a 64-character hex digest")


def _check_target(receipt: Mapping[str, Any], problems: list[str]) -> None:
    target = receipt.get("target") or {}
    if not isinstance(target, Mapping):
        problems.append("target must be a mapping")
        return
    if target.get("tier") != "development":
        problems.append(f"target tier must be development, got {target.get('tier')!r}")
    scrubbed = f"{target.get('host', '')}/{target.get('database', '')}".lower()
    if "prod" in scrubbed:
        problems.append("target looks like production; refusing")


def _check_scratch(receipt: Mapping[str, Any], problems: list[str]) -> None:
    scratch = receipt.get("scratch") or {}
    target = receipt.get("target") or {}
    if not isinstance(scratch, Mapping):
        problems.append("scratch must be a mapping")
        return
    if not any(marker in str(scratch.get("database", "")).lower() for marker in _SCRATCH_MARKERS):
        problems.append("scratch.database must name an isolated scratch database")
    if scratch.get("database") and scratch.get("database") == target.get("database"):
        problems.append("scratch must not be the live target")
    if scratch.get("host") and scratch.get("host") == target.get("host") and (
        scratch.get("database") == target.get("database")
    ):
        problems.append("scratch must be a distinct database")


def _check_pg_restore(receipt: Mapping[str, Any], problems: list[str]) -> None:
    evidence = receipt.get("pg_restore") or {}
    if not isinstance(evidence, Mapping):
        problems.append("pg_restore evidence must be a mapping")
        return
    if evidence.get("exit_code") != 0:
        problems.append(f"pg_restore exit_code must be 0, got {evidence.get('exit_code')!r}")
    if not str(evidence.get("evidence") or "").strip():
        problems.append("pg_restore evidence text is required")


def _check_counts(receipt: Mapping[str, Any], expected_counts: Mapping[str, Any] | None,
                  problems: list[str]) -> None:
    counts = receipt.get("counts")
    if not isinstance(counts, Mapping) or not counts:
        problems.append("restored counts are required")
        return
    for key, value in counts.items():
        if not isinstance(value, int) or value < 0:
            problems.append(f"count {key!r} must be a non-negative integer")
    if expected_counts:
        for key, expected in expected_counts.items():
            if key in counts and int(counts[key]) != int(expected):
                problems.append(
                    f"restored count {key} = {counts[key]} does not match source {expected}"
                )


def _check_signatures(receipt: Mapping[str, Any], problems: list[str]) -> None:
    signatures = receipt.get("signatures") or {}
    if not isinstance(signatures, Mapping):
        problems.append("signatures must be a mapping")
        return
    for field in ("schema_sha256", "counts_sha256"):
        value = signatures.get(field)
        if not isinstance(value, str) or not _SHA256.match(value):
            problems.append(f"signatures.{field} must be a 64-character hex digest")
    counts = receipt.get("counts")
    if isinstance(counts, Mapping) and counts:
        if counts_fingerprint(counts) != signatures.get("counts_sha256"):
            problems.append("counts_sha256 does not match the restored counts")


def _check_timestamps(receipt: Mapping[str, Any], now: datetime | None,
                      max_age: timedelta, problems: list[str]) -> None:
    created = receipt.get("created_at")
    if not created:
        problems.append("created_at is required")
        return
    try:
        parsed = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        problems.append(f"created_at {created!r} is not an ISO-8601 timestamp")
        return
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    moment = now or datetime.now(timezone.utc)
    age = moment - parsed
    if age < timedelta(0):
        problems.append("created_at is in the future")
    elif age > max_age:
        problems.append(f"receipt is older than {max_age}")


def validate_receipt(receipt: Mapping[str, Any] | None, *,
                     expected_counts: Mapping[str, Any] | None = None,
                     now: datetime | None = None,
                     max_age: timedelta = timedelta(hours=24)) -> dict[str, Any]:
    """Validate a backup/restore receipt.  Returns ``{valid, problems}``."""
    problems: list[str] = []
    if not isinstance(receipt, Mapping):
        return {"valid": False, "problems": ["receipt must be a mapping"]}

    for field in REQUIRED_FIELDS:
        if field not in receipt:
            problems.append(f"missing required field: {field}")

    _check_secrets(receipt, problems)
    _check_dump(receipt, problems)
    _check_target(receipt, problems)
    _check_scratch(receipt, problems)
    _check_pg_restore(receipt, problems)
    _check_counts(receipt, expected_counts, problems)
    _check_signatures(receipt, problems)
    _check_timestamps(receipt, now, max_age, problems)

    return {
        "valid": not problems,
        "problems": problems,
        "counts_fingerprint": (
            counts_fingerprint(receipt["counts"])
            if isinstance(receipt.get("counts"), Mapping) and receipt.get("counts")
            else None
        ),
    }


def require_receipt(receipt: Mapping[str, Any] | None, **kwargs: Any) -> dict[str, Any]:
    """Validate a receipt, raising when it is not usable for an apply."""
    result = validate_receipt(receipt, **kwargs)
    if not result["valid"]:
        raise ValueError(f"backup receipt refused: {'; '.join(result['problems'])}")
    return result


def restore_verified(receipt: Mapping[str, Any] | None) -> bool:
    """True only when the receipt proves the dump restored cleanly.

    A backup nobody has restored is not a backup.  The proof is the recorded
    pg_restore run: exit code 0 **and** non-empty evidence text.
    """
    if not isinstance(receipt, Mapping):
        return False
    evidence = receipt.get("pg_restore") or {}
    if not isinstance(evidence, Mapping):
        return False
    return evidence.get("exit_code") == 0 and bool(str(evidence.get("evidence") or "").strip())


def receipt_template() -> dict[str, Any]:
    """An empty receipt showing the required shape (no secrets)."""
    return {
        "dump_path": "<path to protected dump>",
        "dump_sha256": "<64 hex>",
        "created_at": "<ISO-8601>",
        "target": {"tier": "development", "host": "<host>", "database": "poliscopic_dev"},
        "scratch": {"host": "<host>", "database": "poliscopic_restore_scratch"},
        "pg_restore": {"exit_code": 0, "evidence": "<command and outcome>"},
        "counts": {"meetings": 0},
        "signatures": {"schema_sha256": "<64 hex>", "counts_sha256": "<64 hex>"},
    }
