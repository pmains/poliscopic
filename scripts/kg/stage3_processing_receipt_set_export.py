#!/usr/bin/env python3
"""Export the live Stage 3 processing receipts as a canonical receipt set.

The exported bodies are the stored canonical ``receipt_body`` values read straight
out of ``processing_receipts``.  They are never reconstructed from plan rows: a
receipt set is proof that processing happened, so an inferred substitute would be a
forgery, not evidence.

The run is read only — the engine is guarded, every statement is audited, and the
artifact records ``applied: false`` with an absent write path.  The set is
deterministic: rows are read in a fixed identity-plus-digest order, and
``identity_sha256`` fingerprints the ordered identities so two exports of the same
stored state agree regardless of physical row order.  The export fails closed on an
invalid body, a duplicate identity, or an empty population.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target, guard_engine, statement_audit)
from scripts.kg import stage3_processing_plan_inputs as plan_inputs  # noqa: E402
from scripts.kg import stage3_processing_receipt as receipt  # noqa: E402
from scripts.kg.stage2_artifacts import write_immutable  # noqa: E402

TABLE = "public.processing_receipts"
RECEIPT_SET_VERSION = "kg-stage3-processing-receipt-set/1.0"
#: The generated identity columns plus the receipt digest: the store's own unique
#: index, so this order is total and stable for any physical row order.
ORDER_BY = ("source_kind", "source_id", "content_sha256", "extraction_method",
            "extractor", "extractor_version", "receipt_digest")


class ExportRefused(RuntimeError):
    """Raised when the stored state cannot be exported as trustworthy proof."""


def stored_bodies(engine: Any) -> list[Mapping[str, Any]]:
    """Every stored canonical receipt body, in the fixed identity-plus-digest order."""
    statement = f"SELECT receipt_body FROM {TABLE} ORDER BY " + ", ".join(ORDER_BY)
    with engine.connect() as connection:
        rows = connection.execute(text(statement)).scalars().all()
    return [dict(row) for row in rows if row is not None]


def build_receipt_set(bodies: Sequence[Mapping[str, Any]], *, target: Mapping[str, Any],
                      created_at: str) -> dict[str, Any]:
    """The canonical receipt-set artifact for exactly these stored bodies."""
    receipts: list[Mapping[str, Any]] = []
    problems: list[str] = []
    first_seen: dict[str, int] = {}
    for index, body in enumerate(bodies):
        invalid = receipt.validate_receipt(body)
        if invalid:
            problems.append(f"stored receipt at position {index} is invalid: {invalid[:3]}")
            continue
        identity = [str(part) for part in body["processing_identity"]]
        key = receipt.identity_key(identity)
        if key in first_seen:
            problems.append(
                f"duplicate receipt identity at positions {first_seen[key]} and {index}")
            continue
        first_seen[key] = index
        receipts.append(body)
    if problems:
        raise ExportRefused("; ".join(problems))
    if not receipts:
        raise ExportRefused("no canonical receipts are stored; refusing an empty export")
    return {
        "kind": plan_inputs.RECEIPT_SET_KIND,
        "version": RECEIPT_SET_VERSION,
        "created_at": created_at,
        "mode": "read-only",
        "applied": False,
        "write_path": "absent by design",
        "target": dict(target),
        "count": len(receipts),
        "identity_sha256": receipt.canonical_sha256(
            [[str(part) for part in item["processing_identity"]] for item in receipts]),
        "receipts": receipts,
    }


def export(engine: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Guard the engine, read the stored state, and build the artifact."""
    target = assert_read_only_target(engine)
    statements = guard_engine(engine)
    bodies = stored_bodies(engine)
    audit = statement_audit(statements)
    if not audit.get("select_only"):
        raise ExportRefused(f"read-only audit failed: {audit}")
    artifact = build_receipt_set(bodies, target=target,
                                 created_at=datetime.now(timezone.utc).isoformat())
    return artifact, audit


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        artifact, audit = export(get_engine())
    except Exception as exc:  # a refusal is a result, not a crash
        print(json.dumps({"outcome": "refused", "error": f"{type(exc).__name__}: {exc}"},
                         sort_keys=True))
        return 1
    digest = write_immutable(args.out, artifact)
    print(json.dumps({"outcome": "exported", "out": str(args.out), "digest": digest,
                      "count": artifact["count"],
                      "identity_sha256": artifact["identity_sha256"],
                      "read_only_audit": audit}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
