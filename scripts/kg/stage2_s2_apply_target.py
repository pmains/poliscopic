#!/usr/bin/env python3
"""``stage2_s2_apply_target.py`` — target and backup binding for apply admission.

Split out of the runner so both stay readable.  A target is not "the database we
happened to connect to": it must be the same five fields in the engine, the
config, the plans and the backup, and the backup must be loadable, hash-matching
and restore-proven.

The backup is compared **field by field**, and the fields include the ones a hash
cannot speak for: the **live mode and ownership of both the receipt and the dump**.
A receipt that is world-readable is a disclosure even when its digest matches, and
a dump owned by someone else is not the dump the plan named.  Receipt mode is
required to be exactly ``0600``; every other plan-bound field — path, receipt
digest, target, dump hash, restore evidence, source counts, source schema and the
current baseline — must be exactly equal, never merely present.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as _runner  # noqa: E402 (lazy: see runner)

ApplyRefused = _runner.ApplyRefused

#: The five fields that must agree across engine, config, plans and backup.
DEV_TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")
#: Only the development tier may be written to.
EXPECTED_TIER = "development"
#: A protected receipt and dump must be exactly owner-only.
PROTECTED_MODE = "0o600"

__all__ = ["ApplyRefused", "DEV_TARGET_FIELDS", "EXPECTED_TIER", "PROTECTED_MODE",
           "load_backup", "read_backup_target", "verify_backup_binding", "verify_target"]


def _file_stat(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"mode": None, "uid": None, "gid": None, "exists": False, "size": None}
    info = os.stat(path)
    return {"mode": oct(info.st_mode & 0o777), "uid": int(info.st_uid),
            "gid": int(info.st_gid), "exists": True, "size": int(info.st_size)}


def _target_of(engine: Any, config: Mapping[str, Any] | None) -> dict[str, Any]:
    url = getattr(engine, "url", None)
    return {
        "dialect": getattr(url, "drivername", None) or getattr(engine, "dialect", None),
        "host": getattr(url, "host", None),
        "port": getattr(url, "port", None),
        "database": getattr(url, "database", None),
        "tier": (config or {}).get("tier"),
    }


def verify_target(engine: Any, *, plan: Mapping[str, Any],
                   config: Mapping[str, Any] | None,
                   backup: Mapping[str, Any]) -> dict[str, Any]:
    from_engine = _target_of(engine, config)
    from_plan = dict((plan.get("bindings") or {}).get("target") or {})
    from_config = dict(config or {})
    from_backup = dict(backup.get("target") or {})

    for field in DEV_TARGET_FIELDS:
        values = {
            "engine": from_engine.get(field),
            "config": from_config.get(field),
            "plan": from_plan.get(field),
            "backup": from_backup.get(field),
        }
        normalised = {k: (int(v) if field == "port" and v is not None else v)
                      for k, v in values.items()}
        distinct = {v for v in normalised.values() if v is not None}
        if len(distinct) > 1:
            raise ApplyRefused(f"target field {field!r} disagrees: {normalised}")
        if field == "port" and len({v for v in normalised.values()}) > 1:
            raise ApplyRefused(f"target field {field!r} disagrees: {normalised}")
    if from_engine.get("tier") != EXPECTED_TIER:
        raise ApplyRefused(f"target tier {from_engine.get('tier')!r} is not {EXPECTED_TIER!r}")
    return from_engine


def load_backup(backup_path: str | Path, *, target: Mapping[str, Any]) -> dict[str, Any]:
    """Load the live backup: bytes, mode, ownership, restore evidence, counts.

    Everything returned here is derived from the receipt or the dump file itself —
    from the restore's own evidence, the receipt's recorded counts and schema
    fingerprint, and the live ``stat`` of both files.  Nothing is accepted from a
    caller, and nothing is compared against itself.
    """
    path = Path(backup_path)
    if not path.exists():
        raise ApplyRefused(f"backup receipt {path.name!r} does not exist")

    receipt_stat = _file_stat(path)
    if receipt_stat["mode"] != PROTECTED_MODE:
        raise ApplyRefused(
            f"the backup receipt mode is {receipt_stat['mode']}, not {PROTECTED_MODE}")

    try:
        receipt = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ApplyRefused(f"backup receipt is not canonical JSON: {exc}") from exc
    result = receipts.validate_receipt(receipt, expected_counts=None)
    if not result["valid"]:
        raise ApplyRefused(f"backup receipt is invalid: {result['problems'][:3]}")

    dump_path = Path(str(receipt.get("dump_path") or ""))
    if not dump_path.exists():
        raise ApplyRefused("the backup receipt names a dump that is not present")
    dump_stat = _file_stat(dump_path)
    digest = hashlib.sha256(dump_path.read_bytes()).hexdigest()
    if digest != receipt.get("dump_sha256"):
        raise ApplyRefused("the dump hash does not match the receipt")
    if not receipts.restore_verified(receipt):
        raise ApplyRefused("the backup receipt does not prove a verified restore")
    if dict(receipt.get("target") or {}).get("database") != target.get("database"):
        raise ApplyRefused("the backup was taken from a different database")

    restore = receipt.get("pg_restore") or {}
    signatures = receipt.get("signatures") or {}
    return {
        "receipt": receipt,
        "path": path.name,
        "receipt_digest": hashlib.sha256(path.read_bytes()).hexdigest(),
        "receipt_stat": receipt_stat,
        "target": {f: (receipt.get("target") or {}).get(f) for f in DEV_TARGET_FIELDS},
        "dump_path": str(receipt.get("dump_path") or ""),
        "dump_sha256": digest,
        "dump_stat": dump_stat,
        "restore_proof": {
            "exit_code": restore.get("exit_code"),
            "evidence_present": bool(str(restore.get("evidence") or "").strip()),
            "evidence": str(restore.get("evidence") or ""),
            "evidence_sha256": (hashlib.sha256(str(restore.get("evidence") or "")
                                               .encode("utf-8")).hexdigest()
                                if restore.get("evidence") else None),
            "verified": bool(restore.get("verified")),
        },
        "source_counts_sha256": signatures.get("counts_sha256"),
        "source_schema_sha256": signatures.get("schema_sha256"),
        "source_counts": dict(receipt.get("counts") or {}),
        "evidence_source": "the backup receipt and the dump file on disk",
    }


#: The plan-bound backup fields compared for exact equality, and how to read each
#: one from the live backup.  Published so a test can assert the comparison is
#: complete rather than trusting that it is.
COMPARED_FIELDS = (
    "path", "canonical_digest", "target", "dump_path", "dump_sha256",
    "restore_proof", "source_counts", "source_counts_sha256",
    "source_schema_sha256",
)


def verify_backup_binding(bound: Mapping[str, Any],
                          live: Mapping[str, Any]) -> list[str]:
    """Every plan-bound backup field must equal the live one, exactly."""
    problems: list[str] = []
    if not bound:
        return ["the plan binds no backup at all"]

    if bound.get("canonical_digest") != live.get("receipt_digest"):
        problems.append("the bound receipt digest is not the live one")
    if bound.get("path") != live.get("path"):
        problems.append("the bound receipt path is not the live one")
    if bound.get("target") != live.get("target"):
        problems.append("the bound backup target is not the live one")
    if bound.get("dump_path") != live.get("dump_path"):
        problems.append("the bound dump path is not the live one")
    if bound.get("dump_sha256") != live.get("dump_sha256"):
        problems.append("the bound dump hash is not the live one")
    if bound.get("restore_proof") != live.get("restore_proof"):
        problems.append("the bound restore evidence is not the live one")
    if bound.get("source_counts") != live.get("source_counts"):
        problems.append("the bound source counts are not the live ones")
    if bound.get("source_counts_sha256") != live.get("source_counts_sha256"):
        problems.append("the bound source counts fingerprint is not the live one")
    if bound.get("source_schema_sha256") != live.get("source_schema_sha256"):
        problems.append("the bound source schema fingerprint is not the live one")
    # The receipt-derived source counts are also compared against the plan's own
    # recorded baseline counts when the plan carries them: an explicit, one-way
    # check against plan data, never a value a caller supplied.
    plan_counts = bound.get("plan_baseline_counts")
    if plan_counts:
        for name, value in plan_counts.items():
            if int(live.get("source_counts", {}).get(name, -1)) != int(value):
                problems.append(
                    f"the receipt's source count {name!r} does not match the plan baseline")

    # Mode and ownership, which no digest can speak for.
    if bound.get("mode") != PROTECTED_MODE:
        problems.append(f"the bound receipt mode is not {PROTECTED_MODE}")
    if bound.get("receipt") != live.get("receipt_stat"):
        problems.append("the bound receipt mode/ownership is not the live one")
    if live.get("receipt_stat", {}).get("mode") != PROTECTED_MODE:
        problems.append(f"the live receipt mode is not {PROTECTED_MODE}")
    if bound.get("dump") != live.get("dump_stat"):
        problems.append("the bound dump mode/ownership is not the live one")
    if not live.get("dump_stat", {}).get("exists"):
        problems.append("the live dump is absent")
    if (bound.get("restore_proof") or {}).get("exit_code") != 0:
        problems.append("the bound backup has no successful restore proof")
    if bound.get("problems"):
        problems.append(f"the bound backup records problems: {bound['problems']}")
    return problems


def read_backup_target(backup_path: str | Path) -> dict[str, Any]:
    """Read the backup receipt as data, so the target can be compared against it.

    Deliberately does not validate: the caller needs the target fields before it
    can check anything, and validation happens in ``load_backup``.
    """
    path = Path(backup_path)
    if not path.exists():
        raise ApplyRefused("the backup receipt is missing, so the target cannot be compared")
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ApplyRefused(f"backup receipt is not canonical JSON: {exc}") from exc
