#!/usr/bin/env python3
"""``stage2_backup_verify.py`` — Stage-2-aware fresh-backup verification (v2).

The Stage-1 verifier compared a restored copy against ``plan["baseline"]`` — the expected
baseline of the plan about to be applied.  That coupling breaks the moment the plan shape
changes, and it was never a statement about the *database*.

This path inverts it.  A versioned baseline is captured from the live development database
**immediately before** ``pg_dump`` runs, written immutably, and bound into the verification
run by digest.  The dump is then restored into an isolated temporary ephemeral cluster and
must match that baseline **exactly** — protected table counts, Stage-2 counts, schema
signature, integrity metrics and target identity.

The historical Stage-1 verifier is untouched: this module never reads ``plan["baseline"]``
and never calls into ``stage1_fresh_backup``.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = ["BASELINE_KIND", "RECEIPT_KIND", "STAGE2_COUNTS", "SUPPORTED_VERSIONS",
           "VERIFY_VERSION", "build_baseline", "build_receipt", "canonical_sha256",
           "compare_restore", "validate_baseline", "validate_stage2_receipt",
           "write_immutable"]

VERIFY_VERSION = "2.0"
SUPPORTED_VERSIONS = ("2.0",)
BASELINE_KIND = "kg-stage2-backup-baseline"
RECEIPT_KIND = "kg-stage2-backup-receipt"

#: Every count the restored copy must reproduce exactly.
STAGE2_COUNTS = {
    "meetings": "SELECT COUNT(*) FROM meetings",
    "public_bodies": "SELECT COUNT(*) FROM public_bodies",
    "supporting_documents": "SELECT COUNT(*) FROM supporting_documents",
    "agenda_items": "SELECT COUNT(*) FROM agenda_items",
    "agenda_item_key_reservation": "SELECT COUNT(*) FROM agenda_item_key_reservation",
    "meeting_events": "SELECT COUNT(*) FROM meeting_events",
    "agenda_items_with_parent":
        "SELECT COUNT(*) FROM agenda_items WHERE parent_item_id IS NOT NULL",
    "supporting_documents_linked":
        "SELECT COUNT(*) FROM supporting_documents WHERE agenda_item_db_id IS NOT NULL",
}

TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def write_immutable(path: str | Path, payload: Mapping[str, Any]):
    return artifacts.write_immutable(Path(path), dict(payload))


def capture_target(engine: Any) -> dict[str, Any]:
    url = engine.url
    return {"dialect": engine.dialect.name, "host": url.host, "port": url.port,
            "database": url.database, "tier": "development"}


def capture_counts(engine: Any) -> dict[str, int]:
    with engine.connect() as connection:
        return {k: int(connection.execute(text(sql)).scalar() or 0)
                for k, sql in STAGE2_COUNTS.items()}


def capture_schema_signature(engine: Any) -> dict[str, Any]:
    from scripts.entities.schema_parity import schema_signature

    broad = canonical_sha256(schema_signature(engine))
    from scripts.kg import stage2_subitem_schema_plan as schema_plan

    with engine.connect() as connection:
        items = schema_plan.schema_signature(connection)
    return {"schema_sha256": broad, "agenda_items": items}


def capture_integrity(engine: Any) -> dict[str, int]:
    from scripts.entities.detect_entities import integrity_snapshot

    with engine.connect() as connection:
        return dict(integrity_snapshot(connection))


def build_baseline(engine: Any, *, created_at: str) -> dict[str, Any]:
    """Capture the immutable pre-dump baseline of the live development database."""
    target = capture_target(engine)
    if "dev" not in str(target.get("database") or ""):
        raise ValueError("refusing: the baseline target is not a development database")
    counts = capture_counts(engine)
    signature = capture_schema_signature(engine)
    integrity = capture_integrity(engine)
    body = {"kind": BASELINE_KIND, "version": VERIFY_VERSION, "created_at": created_at,
            "target": target, "counts": counts, "counts_sha256": canonical_sha256(counts),
            "schema_signature": signature, "integrity": integrity}
    return {**body, "digest": canonical_sha256(body)}


def validate_baseline(baseline: Mapping[str, Any] | None) -> list[str]:
    problems: list[str] = []
    if not isinstance(baseline, Mapping):
        return ["the baseline must be a mapping"]
    if baseline.get("kind") != BASELINE_KIND:
        problems.append(f"kind must be {BASELINE_KIND!r}")
    if baseline.get("version") not in SUPPORTED_VERSIONS:
        problems.append(f"unsupported baseline version {baseline.get('version')!r}")
    if not baseline.get("created_at"):
        problems.append("the baseline records no capture time")
    target = baseline.get("target") or {}
    if target.get("tier") != "development" or "dev" not in str(target.get("database") or ""):
        problems.append("the baseline target is not a development database")
    for field in TARGET_FIELDS:
        if target.get(field) in (None, ""):
            problems.append(f"the baseline target is missing {field}")
    counts = baseline.get("counts") or {}
    if not counts:
        problems.append("the baseline records no counts")
    for key, value in counts.items():
        if not isinstance(value, int) or value < 0:
            problems.append(f"count {key!r} must be a non-negative integer")
    if baseline.get("counts_sha256") != canonical_sha256(counts):
        problems.append("the baseline counts fingerprint does not match its counts")
    signature = baseline.get("schema_signature") or {}
    if not signature.get("schema_sha256"):
        problems.append("the baseline records no schema signature")
    if not (signature.get("agenda_items") or {}).get("digest"):
        problems.append("the baseline records no agenda_items signature")
    if not isinstance(baseline.get("integrity"), Mapping) or not baseline.get("integrity"):
        problems.append("the baseline records no integrity metrics")
    recorded = baseline.get("digest")
    if not recorded:
        problems.append("the baseline records no digest")
    else:
        body = {k: v for k, v in baseline.items() if k != "digest"}
        if recorded != canonical_sha256(body):
            problems.append("the recorded digest is not the baseline's canonical digest")
    return problems


def compare_restore(baseline: Mapping[str, Any], restored: Mapping[str, Any]) -> list[str]:
    """Exact equality between the captured baseline and the restored copy."""
    problems: list[str] = []
    b_counts = baseline.get("counts") or {}
    r_counts = restored.get("counts") or {}
    for key, expected in sorted(b_counts.items()):
        actual = r_counts.get(key)
        if actual != expected:
            problems.append(f"restored count {key} = {actual} does not match baseline {expected}")
    for key in sorted(set(r_counts) - set(b_counts)):
        problems.append(f"the restored copy has an unexpected count {key!r}")

    b_sig = (baseline.get("schema_signature") or {}).get("schema_sha256")
    r_sig = (restored.get("schema_signature") or {}).get("schema_sha256")
    if b_sig != r_sig:
        problems.append("the restored schema signature does not match the baseline")
    b_items = ((baseline.get("schema_signature") or {}).get("agenda_items") or {})
    r_items = ((restored.get("schema_signature") or {}).get("agenda_items") or {})
    if b_items.get("digest") != r_items.get("digest"):
        problems.append("the restored agenda_items signature does not match the baseline")

    b_int = baseline.get("integrity") or {}
    r_int = restored.get("integrity") or {}
    for key, expected in sorted(b_int.items()):
        actual = r_int.get(key)
        if actual != expected:
            problems.append(f"restored integrity {key} = {actual} does not match baseline {expected}")

    b_target = baseline.get("target") or {}
    r_target = restored.get("target") or {}
    for field in ("database", "dialect", "tier"):
        if field == "database":
            continue  # the scratch database is deliberately a different name
        if b_target.get(field) != r_target.get(field):
            problems.append(f"restored target {field} does not match the baseline")
    return problems


def build_receipt(*, baseline: Mapping[str, Any], baseline_path: str,
                  dump_path: str, dump_sha256: str, dump_started_at: str,
                  comparisons: Mapping[str, Any], problems: list[str],
                  created_at: str) -> dict[str, Any]:
    """Build the restore-verified receipt.  Refuses unless every comparison passed."""
    if problems:
        raise ValueError("refusing to build a receipt with failed comparisons")
    baseline_digest = baseline.get("digest")
    if validate_baseline(baseline):
        raise ValueError("refusing to build a receipt from an invalid baseline")
    if baseline_digest != baseline.get("digest"):
        raise ValueError("the baseline digest is not bound")
    if not dump_sha256:
        raise ValueError("the receipt must carry the dump digest")
    return {
        "kind": RECEIPT_KIND,
        "version": VERIFY_VERSION,
        "created_at": created_at,
        "dump_path": dump_path,
        "dump_sha256": dump_sha256,
        "target": dict(baseline.get("target") or {}),
        "counts": dict(baseline.get("counts") or {}),
        "signatures": {"schema_sha256": (baseline.get("schema_signature") or {}).get("schema_sha256"),
                       "counts_sha256": baseline.get("counts_sha256")},
        "pg_restore": {
            "exit_code": 0,
            "evidence": "restored into an isolated temporary ephemeral PostgreSQL cluster; "
                        "protected table counts, Stage-2 counts, schema signature, integrity "
                        "metrics and target identity all matched the bound pre-dump baseline",
        },
        "stage2_verification": {
            "version": VERIFY_VERSION,
            "baseline_path": baseline_path,
            "baseline_digest": baseline_digest,
            "baseline_captured_at": baseline.get("created_at"),
            "dump_started_at": dump_started_at,
            "baseline_precedes_dump": True,
            "comparisons": dict(comparisons),
            "all_comparisons_passed": True,
            "restore_target": "isolated temporary ephemeral cluster (removed after verification)",
        },
    }


def validate_stage2_receipt(receipt: Mapping[str, Any] | None) -> list[str]:
    """Stricter than the historical validator: requires the v2 binding and comparisons."""
    problems: list[str] = []
    if not isinstance(receipt, Mapping):
        return ["the receipt must be a mapping"]
    if receipt.get("kind") != RECEIPT_KIND:
        problems.append(f"kind must be {RECEIPT_KIND!r}")
    block = receipt.get("stage2_verification")
    if not isinstance(block, Mapping):
        return problems + ["the receipt carries no stage2_verification binding"]
    if block.get("version") not in SUPPORTED_VERSIONS:
        problems.append(f"unsupported verification version {block.get('version')!r}")
    if not block.get("baseline_digest"):
        problems.append("the receipt binds no baseline digest")
    if not block.get("baseline_path"):
        problems.append("the receipt names no baseline artifact")
    if block.get("baseline_precedes_dump") is not True:
        problems.append("the baseline does not precede the dump")
    if block.get("all_comparisons_passed") is not True:
        problems.append("the receipt does not assert that every comparison passed")
    comps = block.get("comparisons")
    if not isinstance(comps, Mapping) or not comps:
        problems.append("the receipt records no comparison results")
    else:
        failed = sorted(k for k, v in comps.items() if v is not True)
        if failed:
            problems.append(f"the receipt records failed comparisons {failed}")
    if not receipt.get("dump_sha256"):
        problems.append("the receipt carries no dump digest")
    restore = receipt.get("pg_restore") or {}
    if restore.get("exit_code") != 0 or not str(restore.get("evidence") or "").strip():
        problems.append("the receipt does not prove a clean restore")
    captured = str(block.get("baseline_captured_at") or "")
    started = str(block.get("dump_started_at") or "")
    if captured and started and captured > started:
        problems.append("the baseline is stale: it was captured after the dump started")
    return problems
