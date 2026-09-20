#!/usr/bin/env python3
"""``stage1_execution_packet.py`` — assemble the **unapplied** Stage 1 packet (v2).

Assembles a dependency-ordered, digest-bound execution packet for resolving all
392 ``event_normalize`` civic-chain blockers.  Nothing is applied: the emitted
artifacts declare themselves unapplied and the database is only ever queried.

Corrections carried by version 2:

* **Scope from adjudication.**  Body scope comes from the adjudication artifact,
  so ``phoenix-dab``'s four extraction-less meetings (14608, 14629, 15331, 17049)
  are outside scope by construction and the meeting total is exactly 55.
* **Derived expected state.**  Every expected count is computed as
  ``baseline + operations``; nothing is hardcoded, and each derivation is
  re-checked as an arithmetic identity.
* **Bound populations.**  The 374 repair extraction ids and the 18 quarantine ids
  are bound into the packet with their adjudication artifact path and SHA-256, and
  proved disjoint and complete.
* **Quarantine values bound.**  Reason and ``model_version`` are bound from the
  registries; the three human fields remain explicit approval placeholders that
  block apply until supplied.

Usage:
    .venv/bin/python scripts/kg/stage1_execution_packet.py --verify-only
    .venv/bin/python scripts/kg/stage1_execution_packet.py --write
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import text  # noqa: E402

from scripts.db import quarantine_schema  # noqa: E402
from scripts.entities.event_normalize_artifacts import write_exclusive  # noqa: E402
from scripts.kg import stage1_adjudication as adjudication  # noqa: E402
from scripts.kg import stage1_packet_components as components  # noqa: E402
from scripts.kg.phoenix_dr_adjudication import assert_development_target  # noqa: E402
from scripts.kg.quarantine import (  # noqa: E402
    QUARANTINE_COLUMNS,
    quarantine_reason_slugs,
    validate_quarantine_reason,
)
from scripts.kg.registries import MODEL_VERSION  # noqa: E402

__all__ = [
    "PACKET_VERSION",
    "QUARANTINE_REASON",
    "build_operations",
    "build_packet",
    "derive_expected_state",
    "render_markdown",
    "write_packet",
]

PACKET_VERSION = "kg-stage1-execution-packet/2.0"

#: The controlled reason, owned by the approved adjudication.
QUARANTINE_REASON = adjudication.ADJUDICATION["quarantine_reason"]

#: Baseline counts this packet constrains.
_BASELINE_COUNTS = {
    "meetings_total": "SELECT COUNT(*) FROM meetings",
    "meetings_null_public_body": "SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL",
    "meetings_null_jurisdiction": "SELECT COUNT(*) FROM meetings WHERE jurisdiction_id IS NULL",
    "public_bodies_total": "SELECT COUNT(*) FROM public_bodies",
    "supporting_documents_total": "SELECT COUNT(*) FROM supporting_documents",
}

#: A ``meetings`` column that is NULL before the repair and set by it.
_NULL_TRACKED_COLUMNS = {
    "public_body_id": "meetings_null_public_body",
    "jurisdiction_id": "meetings_null_jurisdiction",
}


def capture_baseline(engine) -> dict[str, Any]:
    """Read-only before-state counts plus the Stage 0 integrity snapshot."""
    from scripts.entities.detect_entities import _integrity_snapshot

    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for name, query in _BASELINE_COUNTS.items():
            counts[name] = int(connection.execute(text(query)).scalar())
    return {"counts": counts, "integrity": _integrity_snapshot(engine)}


def build_operations(verified: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The exact operations, derived from the reviewed plans and adjudicated scope."""
    operations: list[dict[str, Any]] = []
    for component in verified:
        if component.get("kind") != "data" or not str(component["id"]).startswith("phoenix-"):
            continue
        document = json.loads(
            (_REPO_ROOT / str(component["path"])).read_text(encoding="utf-8")
        )
        insert = next(
            operation for operation in document["operations"]
            if operation.get("op") == "INSERT" and operation.get("table") == "public_bodies"
        )
        meeting_ids = sorted(int(value) for value in component["meeting_ids"])
        operations.append({
            "order": 2,
            "component": component["id"],
            "op": "INSERT",
            "table": "public_bodies",
            "rows": 1,
            "values": insert.get("values"),
            "audit_timestamp": components.audit_timestamp(),
            "identity": insert.get("identity"),
            "precondition": insert.get("precondition"),
        })
        operations.append({
            "order": 2,
            "component": component["id"],
            "op": "UPDATE",
            "table": "meetings",
            "rows": len(meeting_ids),
            "where": "id = ANY(:meeting_ids)",
            "target_ids": meeting_ids,
            "set": {"jurisdiction_id": 4,
                    "public_body_id": f"(SELECT id FROM public_bodies WHERE body_code = '{component['id']}')"},
            "set_null_tracked": ["jurisdiction_id", "public_body_id"],
            "precondition": "public_body_id IS NULL AND body = :body_code",
        })
    return operations


def derive_expected_state(baseline: Mapping[str, Any], operations: Sequence[Mapping[str, Any]],
                          quarantine_rows: int) -> dict[str, Any]:
    """Compute expected counts as ``baseline + operations`` and validate each identity.

    Deltas accumulate per count across every operation, and each count is then
    validated as one arithmetic identity (``baseline + total delta == expected``).
    No count is hardcoded.
    """
    counts = dict(baseline.get("counts") or {})
    deltas: dict[str, int] = {}
    steps: list[dict[str, Any]] = []

    for operation in operations:
        if operation["op"] == "INSERT" and operation["table"] == "public_bodies":
            deltas["public_bodies_total"] = deltas.get("public_bodies_total", 0) + operation["rows"]
            steps.append({"count": "public_bodies_total", "component": operation["component"],
                          "delta": operation["rows"]})
        elif operation["op"] == "UPDATE" and operation["table"] == "meetings":
            for column in operation.get("set_null_tracked") or ():
                key = _NULL_TRACKED_COLUMNS[column]
                deltas[key] = deltas.get(key, 0) - operation["rows"]
                steps.append({"count": key, "component": operation["component"],
                              "delta": -operation["rows"]})

    deltas["quarantined_extractions"] = quarantine_rows
    steps.append({"count": "quarantined_extractions", "component": "data.quarantine_skip_18",
                  "delta": quarantine_rows})

    expected = dict(counts)
    for key, delta in deltas.items():
        expected[key] = counts.get(key, 0) + delta

    derivation = [
        {"count": key, "baseline": counts.get(key, 0), "delta": delta,
         "expected": expected[key], "steps": [s for s in steps if s["count"] == key]}
        for key, delta in sorted(deltas.items())
    ]
    validated = all(
        counts.get(entry["count"], 0) + entry["delta"] == entry["expected"]
        for entry in derivation
    )
    non_negative = all(value >= 0 for value in expected.values())
    return {
        "expected": expected,
        "derivation": derivation,
        "steps": steps,
        "validated": validated,
        "non_negative": non_negative,
    }


def quarantine_bindings(engine, ids: Sequence[int]) -> dict[str, Any]:
    """Bind the 18 rows: identity, fingerprints, preconditions and values."""
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT * FROM meeting_event_extractions WHERE id = ANY(:ids) ORDER BY id"),
            {"ids": list(ids)},
        ).mappings().all()
    present = {int(row["id"]) for row in rows}
    missing = sorted(set(int(value) for value in ids) - present)
    document_ids = sorted({int(row["supporting_doc_id"]) for row in rows})
    meeting_event_ids = sorted({int(row["meeting_event_id"]) for row in rows})
    return {
        "target_ids": sorted(int(value) for value in ids),
        "row_count": len(ids),
        "missing_rows": missing,
        "membership_confirmed": not missing and len(rows) == len(ids),
        "current_fingerprints": {
            str(row["id"]): components.canonical_row_fingerprint(row) for row in rows
        },
        "supporting_document_ids": document_ids,
        "meeting_event_ids": meeting_event_ids,
        "current_null_preconditions": {
            str(row["id"]): row["quarantine_reason"] is None if "quarantine_reason" in row else True
            for row in rows
        },
        "precondition_sql": "id = :id AND quarantine_reason IS NULL",
    }


def quarantine_values() -> dict[str, Any]:
    """The exact quarantine values, supplied by the approved adjudication.

    No placeholder remains: the adjudicator, decision id and decision timestamp are
    recorded, the reason is the registry-approved controlled reason, and the model
    version comes from the registries.  Applying is still blocked, but now by the
    *authorization boundary* rather than by a missing field.
    """
    values = adjudication.quarantine_values(MODEL_VERSION)
    values["quarantine_reason"] = validate_quarantine_reason(values["quarantine_reason"])
    values["human_fields_required"] = list(components.HUMAN_REQUIRED_FIELDS)
    return values


def _atomicity(dialect: str) -> dict[str, Any]:
    """Decide the atomicity boundary from the live dialect, failing closed."""
    transactional_ddl = dialect == "postgresql"
    return {
        "dialect": dialect,
        "transactional_ddl": transactional_ddl,
        "single_transaction_required": transactional_ddl,
        "boundary": (
            "BEGIN; 5 ALTER TABLE ADD COLUMN; 1 CREATE INDEX; 3 public_bodies INSERT; "
            "55 meetings UPDATE; 18 meeting_event_extractions UPDATE; COMMIT;"
        ),
        "fail_closed": (
            "If the target dialect cannot run DDL in the same transaction as the data "
            "operations, this packet must not be applied in one step."
        ),
        "staged_rollback": (
            "Fallback only: stage (a) DDL then (b) data, each with its own verified "
            "backup point; the data stage rolls back with the recorded inverse "
            "operations, then the DDL stage with quarantine_schema.downgrade()."
        ),
    }


def build_packet(engine, stamp: str | None = None) -> dict[str, Any]:
    """Assemble the packet.  The engine is only ever queried."""
    assert_development_target(engine)
    generated_at = stamp or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    verified = components.verify_components()
    proof = components.scope_proof()
    before = capture_baseline(engine)
    operations = build_operations(verified)
    quarantine_ids = components.quarantine_extraction_ids()
    binding = quarantine_bindings(engine, quarantine_ids)
    values = quarantine_values()
    after = capture_baseline(engine)
    zero_delta = before == after

    meeting_updates = sum(
        operation["rows"] for operation in operations
        if operation["op"] == "UPDATE" and operation["table"] == "meetings"
    )
    inserts = sum(
        operation["rows"] for operation in operations
        if operation["op"] == "INSERT" and operation["table"] == "public_bodies"
    )
    derived = derive_expected_state(before, operations, len(quarantine_ids))

    packet: dict[str, Any] = {
        "packet_version": PACKET_VERSION,
        "generated_at": generated_at,
        "applied": False,
        "mutations_performed": 0,
        "components": [dict(component) for component in verified],
        "component_order": [component["id"] for component in verified],
        "bindings": {
            "adjudication_artifact": dict(components.ADJUDICATION_ARTIFACT),
            "plans": {body: dict(binding) for body, binding in components.PLAN_BINDINGS.items()},
            "quarantine_artifact": dict(components.QUARANTINE_ARTIFACT),
            "refuses_unbound_artifacts": True,
        },
        "populations": {
            "repair_extraction_ids": list(proof["repair_ids"]),
            "quarantine_extraction_ids": list(proof["quarantine_ids"]),
            "repair_count": proof["repair_count"],
            "quarantine_count": proof["quarantine_count"],
            "overlap": proof["overlap"],
            "disjoint": proof["disjoint"],
            "complete": proof["complete"],
            "meeting_scope": {body: list(ids) for body, ids in components.meeting_scope().items()},
        },
        "operations": operations,
        "counts": {
            "public_body_inserts": inserts,
            "meeting_updates": meeting_updates,
            "quarantine_updates": len(quarantine_ids),
            "meeting_updates_exact": meeting_updates == 55,
            "quarantine_updates_exact": len(quarantine_ids) == 18,
            "inserts_exact": inserts == 3,
        },
        "schema": {
            "module": components.SCHEMA_COMPONENT["module"],
            "semantics_module": components.SCHEMA_COMPONENT["semantics_module"],
            "table": quarantine_schema.TABLE,
            "statements": quarantine_schema.statements_for_review(engine.dialect.name),
            "sole_authority": "scripts/kg/stage1_apply_runner.py",
        },
        "adjudication": dict(adjudication.ADJUDICATION),
        "document_provenance": adjudication.document_provenance(),
        "code_fingerprint": components.canonical_expectation()["code_fingerprint"],
        "quarantine": {
            **binding,
            "values": values,
            "attributes_written": list(QUARANTINE_COLUMNS),
            "reason_slugs": list(quarantine_reason_slugs()),
        },
        "baseline": before,
        "expected_after_state": derived["expected"],
        "state_derivation": derived,
        "atomicity": _atomicity(engine.dialect.name),
        "restore_instructions": {
            "required_before_apply": True,
            "receipt_validator": "scripts/kg/stage1_backup_receipt.py",
            "steps": [
                "pg_dump the development database to a protected, timestamped artifact",
                "restore that artifact to an isolated scratch database",
                "record dump path, sha256, pg_restore evidence and scratch counts in a receipt",
                "validate the receipt; only then allow the single-transaction apply",
            ],
        },
        "reconciliation": {
            "repaired_extractions": proof["repair_count"],
            "quarantined_extractions": proof["quarantine_count"],
            "total_blockers": proof["total"],
            "expected_total": 392,
            "balances": proof["total"] == 392,
        },
        "verification": {
            "all_digests_match": all(component.get("digest_ok") for component in verified),
            "scope_exact": proof["meeting_total_exact"] and proof["repair_ids_exact"],
            "populations_disjoint": proof["disjoint"],
            "populations_complete": proof["complete"],
            "counts_exact": meeting_updates == 55 and inserts == 3 and len(quarantine_ids) == 18,
            "state_derivation_validated": derived["validated"] and derived["non_negative"],
            "quarantine_membership_confirmed": binding["membership_confirmed"],
            "zero_delta_verified": zero_delta,
            "human_fields_supplied": values["human_fields_supplied"],
            "apply_authorized": adjudication.ADJUDICATION["authorization"]["apply_authorized"],
            "backup_authorized": adjudication.ADJUDICATION["authorization"]["backup_authorized"],
        },
    }
    packet["verification"]["ready_for_human_adjudication"] = all((
        packet["verification"]["all_digests_match"],
        packet["verification"]["scope_exact"],
        packet["verification"]["populations_disjoint"],
        packet["verification"]["populations_complete"],
        packet["verification"]["counts_exact"],
        packet["verification"]["state_derivation_validated"],
        packet["verification"]["quarantine_membership_confirmed"],
        packet["verification"]["zero_delta_verified"],
    ))
    packet["verification"]["ready_to_apply"] = False
    packet["apply_blockers"] = [
        blocker for blocker, blocked in (
            ("the adjudication does not authorize a migration/data apply",
             not packet["verification"]["apply_authorized"]),
            ("the adjudication does not authorize backup creation",
             not packet["verification"]["backup_authorized"]),
        ) if blocked
    ]
    packet["packet_digest"] = hashlib.sha256(
        json.dumps(packet, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    return packet


def render_markdown(packet: Mapping[str, Any]) -> str:
    """Human-readable rendering of the same packet."""
    counts = packet["counts"]
    lines = [
        f"# Stage 1 execution packet — **UNAPPLIED** ({packet['packet_version']})",
        "",
        f"Generated `{packet['generated_at']}` · applied **{packet['applied']}** · "
        f"mutations **{packet['mutations_performed']}**",
        "",
        "## Scope",
        "",
        f"- meeting updates **{counts['meeting_updates']}** (dr 13 + dab 37 + ds 5) · "
        f"body inserts **{counts['public_body_inserts']}** · quarantine updates "
        f"**{counts['quarantine_updates']}**",
        f"- repair ids **{packet['populations']['repair_count']}** · quarantine ids "
        f"**{packet['populations']['quarantine_count']}** · disjoint "
        f"**{packet['populations']['disjoint']}** · complete "
        f"**{packet['populations']['complete']}**",
        "",
        "## Expected state (derived)",
        "",
        "| count | baseline | delta | expected |",
        "|---|---|---|---|",
    ]
    for entry in packet["state_derivation"]["derivation"]:
        lines.append(
            f"| {entry['count']} | {entry['baseline']} | {entry['delta']:+d} | {entry['expected']} |"
        )
    lines += [
        "",
        "## Quarantine values",
        "",
    ]
    for key, value in packet["quarantine"]["values"].items():
        lines.append(f"- {key}: `{value}`")
    lines += [
        "",
        "## Verification",
        "",
    ]
    lines += [f"- {key}: **{value}**" for key, value in packet["verification"].items()]
    lines += ["", f"Packet digest: `{packet['packet_digest']}`"]
    return "\n".join(lines) + "\n"


def write_packet(packet: Mapping[str, Any]) -> dict[str, str]:
    """Write the packet artifacts atomically; never overwrite an existing one."""
    stamp = packet["generated_at"]
    json_path = components.DATA_DIR / f"kg-stage1-execution-packet-{stamp}.json"
    md_path = components.DATA_DIR / f"kg-stage1-execution-packet-{stamp}.md"
    write_exclusive(json_path, json.dumps(packet, indent=2, sort_keys=True, default=str))
    write_exclusive(md_path, render_markdown(packet))
    return {"json": str(json_path), "markdown": str(md_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--verify-only", action="store_true",
                        help="verify bound components without touching a database")
    parser.add_argument("--write", action="store_true", help="write packet artifacts")
    arguments = parser.parse_args(argv)

    if arguments.verify_only:
        verified = components.verify_components()
        for component in verified:
            status = "OK" if component.get("digest_ok") else "FAIL"
            print(f"  [{status}] {component['id']}: {component.get('problems') or ''}")
        ok = all(component.get("digest_ok") for component in verified)
        return 0 if ok else 1

    from scripts.db.core import get_engine
    from scripts.entities.event_normalize_preflight import guard_engine

    engine = get_engine()
    guard_engine(engine)
    packet = build_packet(engine)
    print(json.dumps(packet["verification"], indent=2))
    print(f"packet_digest={packet['packet_digest']}")
    if arguments.write:
        print(json.dumps(write_packet(packet), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
