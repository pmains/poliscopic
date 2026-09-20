#!/usr/bin/env python3
"""Design-only additive receipt-store packet for Stage 3 Q5 blocker 1 (revision 5).

**Nothing here writes.**  The packet declares the exact schema, the typed row
mapping, the append-only and conflict rules, the migration order, the
backup/rollback/replay contract, and a disabled runner interface whose execution
entry point raises unconditionally.

Revision 5 closes the remaining executable-schema findings:

1. PostgreSQL recomputes the receipt's canonical body-minus-digest using a declared,
   immutable pgcrypto-backed function; it also CHECK-binds kind/version/producer,
   flattened identity fields and extractor version to the generated identity columns;
2. append-only covers UPDATE, DELETE **and** TRUNCATE for the real writer, and the
   privilege statements are part of the atomic apply statement list;
3. the rollback contract is exact and reconstructed, arbitrary re-signed DROP
   statements are refused, and the rollback gate requires an apply receipt that owns
   this packet;
4. a backup must be canonically loaded and validated with the authoritative verifiers,
   match the exact target, be mode 0600 with a present matching dump, prove a restore,
   be fresh, and bind a verified baseline and schema signature;
5. ``validate_packet`` reconstructs the whole packet from its bound inputs and compares
   the exact top-level key set and every security-critical block;
6. ``plan_appends`` refuses more than one action per identity in a single call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg import stage3_processing_plan_validator as validator  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_backup as backup  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_rows as rows  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_schema as schema  # noqa: E402
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

#: The backup contract lives in its own module; re-exported for callers and tests.
backup_problems = backup.backup_problems

PACKET_VERSION = "kg-stage3-processing-receipt-store-packet/2.2"
PACKET_KIND = "kg-stage3-processing-receipt-store-packet"
PLAN_KIND = validator.PLAN_KIND
APPLY_RECEIPT_KIND = "kg-stage3-processing-receipt-store-apply-receipt"
BACKUP_RECEIPT_KIND = backup.BACKUP_RECEIPT_KIND
ENABLED = False
AUTHORIZATION_TOKEN = "stage3-receipt-store-development-apply"
MAX_BACKUP_AGE_SECONDS = backup.MAX_BACKUP_AGE_SECONDS
DISABLED_REASON = (
    "Design-only packet. No database object may be created or altered and no apply entry "
    "point exists. An executable apply requires explicit development schema authorization, "
    "a fresh restore-verified backup receipt, a bound writer role, and a single SERIALIZABLE "
    "transaction."
)
CODE_MODULES = (
    "scripts/kg/stage3_processing_receipt_store_packet.py",
    "scripts/kg/stage3_processing_receipt_store_schema.py",
    "scripts/kg/stage3_processing_receipt_store_backup.py",
    "scripts/kg/stage3_processing_receipt_store_rows.py",
    "scripts/kg/stage3_processing_plan_inputs.py",
    "scripts/kg/stage3_processing_plan_validator.py",
    "scripts/kg/stage3_processing_receipt.py",
    "scripts/kg/stage3_processing_identity.py",
    "scripts/kg/producer_versions.py",
    "scripts/kg/stage2_artifacts.py",
    "scripts/kg/stage1_backup_receipt.py",
    "scripts/kg/stage2_backup_verify.py",
)
PACKET_KEYS = (
    "kind", "version", "created_at", "mode", "enabled", "applied", "write_path",
    "disabled_reason", "target", "bindings", "schema_contract", "schema_operations",
    "migration_order", "data_operations", "row_mapping", "writer_supplied_columns",
    "store_assigned_columns", "generated_columns", "append_only", "current_state_sql",
    "backup_requirement", "transaction_contract", "runner_interface", "rollback",
    "accounting", "approval_boundary", "digest",
)
CRITICAL_BLOCKS = tuple(key for key in PACKET_KEYS if key != "digest")

#: The store rules live in their own module; one exception type covers both layers.
PacketRefused = rows.StoreRefused


class WritePathNotImplemented(PacketRefused):
    """Raised by every execution entry point: this packet has no write path."""


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in CODE_MODULES:
        path = repo / name
        if not path.is_file():
            raise PacketRefused(f"required code module is absent: {name}")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def packet_digest(packet: Mapping[str, Any]) -> str:
    return canonical_sha256({key: value for key, value in packet.items() if key != "digest"})


def load_plan(*, plan_path: str | Path, repo: Path = REPO) -> dict[str, Any]:
    """Load the authoritative plan and require that it validates as it stands."""
    path = Path(plan_path)
    if is_obsolete(path):
        raise PacketRefused(f"{path.name} is obsolete for use")
    plan = load_verified(path)
    if plan.get("kind") != PLAN_KIND:
        raise PacketRefused(f"{path.name} is not a processing dry plan")
    if dict(plan.get("target") or {}).get("tier") != "development":
        raise PacketRefused("the packet binds development artifacts only")
    problems = validator.validate_plan(plan, target=plan.get("target"),
                                       hashes=validator.code_hashes(repo))
    if problems:
        raise PacketRefused(f"the bound plan does not validate: {problems[:3]}")
    return plan


def migration_order() -> list[dict[str, Any]]:
    return [{"order": index + 1, "phase": schema.MIGRATION_PHASES[index],
             "statement": statement}
            for index, statement in enumerate(schema.DDL)]


def rollback_contract() -> dict[str, Any]:
    """The exact rollback: only the objects this packet creates, nothing else."""
    return {
        "statements": list(schema.ROLLBACK_DDL),
        "owned_objects": list(schema.OWNED_OBJECTS),
        "requires": ["an immutable apply receipt that owns this packet digest",
                     "the receipt's applied statements equal the packet schema operations",
                     "the receipt's created objects equal the packet owned objects",
                     "an authorized approver and a bound writer role in that receipt",
                     "every stored row explained by the receipt's append set"],
        "forbidden": ["rolling back objects the packet did not create",
                      "deleting rows to make a rollback possible",
                      "re-running the packet after a rollback without a new backup"],
        "statement_count": len(schema.ROLLBACK_DDL),
    }


def build_packet(*, plan_path: str | Path, created_at: str,
                 repo: Path = REPO) -> dict[str, Any]:
    plan = load_plan(plan_path=plan_path, repo=repo)
    contract = schema.schema_contract()
    body = {
        "kind": PACKET_KIND,
        "version": PACKET_VERSION,
        "created_at": created_at,
        "mode": "design-only",
        "enabled": False,
        "applied": False,
        "write_path": "absent by design",
        "disabled_reason": DISABLED_REASON,
        "target": dict(plan["target"]),
        "bindings": {
            "plan": {"path": str(plan_path), "digest": plan["digest"], "kind": plan["kind"]},
            "plan_digest": plan["digest"],
            "selection_snapshot": dict(plan["selection_binding"]),
            "evidence": {name: dict(item) for name, item in
                         sorted(plan["evidence_binding"].items())},
            "receipts": dict(plan["receipts_binding"]),
            "reference_schema": dict(plan["producer"]["code_hashes"]),
            "code_hashes": code_hashes(repo),
            "schema_declaration_sha256": contract["declaration_sha256"],
            "schema_ddl_sha256": contract["ddl_sha256"],
            "schema_rollback_sha256": contract["rollback_sha256"],
        },
        "schema_contract": contract,
        "schema_operations": list(schema.DDL),
        "migration_order": migration_order(),
        "data_operations": [],
        "row_mapping": [{"column": name, "declaration": declaration_sql,
                         "payload_source": source,
                         "store_assigned": name in rows.STORE_ASSIGNED_COLUMNS}
                        for name, declaration_sql, source in schema.COLUMNS],
        "writer_supplied_columns": list(rows.WRITER_SUPPLIED_COLUMNS),
        "store_assigned_columns": list(rows.STORE_ASSIGNED_COLUMNS),
        "generated_columns": list(schema.DERIVED_FROM_BODY),
        "append_only": dict(schema.APPEND_ONLY_RULES),
        "current_state_sql": schema.CURRENT_STATE_SQL,
        "backup_requirement": {**backup.BACKUP_REQUIREMENT,
                               "receipt_kind": BACKUP_RECEIPT_KIND},
        "transaction_contract": {
            "isolation": "SERIALIZABLE",
            "statements": "exactly the declared statement list, in order, once",
            "advisory_lock": "taken before any statement and held to commit",
            "placeholders": {"required": list(schema.STATEMENT_PLACEHOLDERS),
                             "binding": "the authorizing decision supplies one simple writer "
                                        "role; the apply validates and safely quotes it inside "
                                        "the transaction"},
            "pgcrypto": dict(schema.PGCRYPTO_REQUIREMENT),
            "privileges": "the privilege REVOKE statements are inside the atomic statement "
                          "list, never applied post-commit",
            "rollback": "any failure rolls the whole transaction back; PostgreSQL DDL is "
                        "transactional",
            "partial_application": "outside one transaction a partial object set is "
                                   "unexplained; the runner refuses and the only sanctioned "
                                   "recovery is a restore",
            "postconditions": ["table present with the declared generated columns",
                               "declaration signature equal",
                               "both append-only triggers present",
                               "zero rows stored by this packet"],
            "replay": "a second run detects the applied signature and returns a no-op "
                      "instead of re-executing",
        },
        "runner_interface": {
            "module": "scripts/kg/stage3_processing_receipt_store_packet.py",
            "enabled": False,
            "authorization_token_required": True,
            "requires": ["the exact packet path and digest",
                         "an equality match on the development target",
                         "a canonically loaded, fresh, restore-verified backup receipt",
                         "a verified pgcrypto capability and a safely quoted writer role",
                         "a single SERIALIZABLE transaction with an advisory lock"],
            "execution_entry_point": "execute_packet raises WritePathNotImplemented",
            "write_path": "absent by design",
        },
        "rollback": rollback_contract(),
        "accounting": {"schema_operations": len(schema.DDL), "data_operations": 0,
                       "rows_stored_by_this_packet": 0,
                       "identities_in_bound_plan": int(plan["bound"]["selected"]),
                       "statement_placeholders": len(schema.STATEMENT_PLACEHOLDERS)},
        "approval_boundary": {
            "authorized_here": "design-only declaration and offline gates",
            "authorization_required_for": [
                "creating the processing_receipts table, its constraints, generated columns, "
                "indexes, triggers and privilege statements in the development database",
                "binding the writer role used by the privilege statements",
                "any receipt INSERT or any swept_at update",
                "enabling or executing the apply runner",
                "re-downloading or re-extracting any source",
            ],
            "requires": ["explicit development schema authorization from Peter Mains",
                         "a fresh restore-verified backup receipt",
                         "an apply receipt and a verified rollback"],
            "mutations_proposed": 0,
        },
    }
    return {**body, "digest": packet_digest(body)}


def validate_packet(packet: Any, *, repo: Path = REPO) -> list[str]:
    """Reconstruct the packet from its bound inputs and compare it exactly."""
    if not isinstance(packet, Mapping):
        return ["packet must be an object"]
    problems: list[str] = []
    keys = set(packet)
    missing = sorted(set(PACKET_KEYS) - keys)
    unexpected = sorted(keys - set(PACKET_KEYS))
    if missing or unexpected:
        problems.append(f"top-level key set is not canonical; missing {missing}, "
                        f"unexpected {unexpected}")
    if packet.get("kind") != PACKET_KIND or packet.get("version") != PACKET_VERSION:
        problems.append("packet kind or version is not canonical")
    if packet.get("digest") != packet_digest(packet):
        problems.append("packet digest does not match the packet body")
    if packet.get("mode") != "design-only" or packet.get("enabled") is not False \
            or packet.get("applied") is not False or packet.get("write_path") != "absent by design":
        problems.append("a design-only packet must be disabled, unapplied, and declare no "
                        "write path")
    if list(packet.get("data_operations") or []) != []:
        problems.append("a design-only packet must declare zero data operations")
    if ENABLED is not False:
        problems.append("the packet module's ENABLED flag must be False")

    plan_binding = ((packet.get("bindings") or {}).get("plan") or {})
    plan_path = Path(str(plan_binding.get("path") or ""))
    if not plan_path.exists():
        problems.append("the bound plan artifact is absent")
    else:
        try:
            expected = build_packet(plan_path=plan_path,
                                    created_at=str(packet.get("created_at")), repo=repo)
        except PacketRefused as exc:
            problems.append(f"the packet cannot be reconstructed: {exc}")
        else:
            differing = sorted(key for key in CRITICAL_BLOCKS
                               if expected.get(key) != packet.get(key))
            if differing:
                problems.append("security-critical blocks differ from the canonical "
                                f"reconstruction: {differing[:6]}")
            if expected.get("digest") != packet.get("digest"):
                problems.append("the packet digest is not the canonical reconstruction's")

    rollback = packet.get("rollback") or {}
    statements = list(rollback.get("statements") or [])
    if statements != list(schema.ROLLBACK_DDL):
        problems.append("rollback statements are not the canonical rollback DDL")
    for name in rollback.get("owned_objects") or []:
        if name not in schema.OWNED_OBJECTS:
            problems.append(f"rollback claims an object it does not own: {name}")
    if sorted(rollback.get("owned_objects") or []) != sorted(schema.OWNED_OBJECTS):
        problems.append("rollback owned objects are not exactly the packet's objects")
    text = " ".join(statements).upper()
    for foreign in ("SUPPORTING_DOCUMENTS", "AGENDA_ITEMS", "ENTITIES", "MEETING_EVENTS",
                    "PUBLIC", "SCHEMA"):
        if foreign in text:
            problems.append(f"rollback must not touch {foreign}")
    return problems


def apply_gate(packet: Any, *, authorization: str | None = None,
               target: Mapping[str, Any] | None = None,
               packet_digest_value: str | None = None,
               backup_path: str | Path | None = None,
               writer_role: str | None = None,
               pgcrypto_available: bool = False,
               transactional: bool = False,
               applied_statements: Sequence[str] = (),
               now: datetime | None = None,
               repo: Path = REPO) -> dict[str, Any]:
    """The full pre-apply gate.  ``allowed`` can never be true in this turn."""
    refusals: list[str] = []
    packet_target = dict(packet.get("target") or {}) if isinstance(packet, Mapping) else None
    if not ENABLED:
        refusals.append("the receipt-store apply is DISABLED by design (ENABLED is False)")
    if authorization != AUTHORIZATION_TOKEN:
        refusals.append("the authorization token is missing or wrong")
    if not isinstance(packet, Mapping) or packet_digest_value != packet.get("digest"):
        refusals.append("the exact packet path and digest were not supplied")
    problems = validate_packet(packet, repo=repo)
    if problems:
        refusals.append(f"the packet does not validate: {problems[:2]}")
    if not isinstance(target, Mapping) or target.get("tier") != "development":
        refusals.append("the target is not the development database")
    elif dict(target) != (packet_target or {}):
        refusals.append("the target does not match the packet target")
    refusals.extend(backup_problems(backup_path, target=target or {}, now=now, repo=repo))
    if pgcrypto_available is not True:
        refusals.append("pgcrypto digest capability was not verified for the apply target")
    try:
        schema.quote_writer_role(writer_role)
    except ValueError as exc:
        refusals.append(str(exc))
    if transactional is not True:
        refusals.append("a single transaction is mandatory; partial DDL is unrecoverable")
    if applied_statements:
        refusals.extend(schema.partial_apply_problems(applied_statements))
    gate_passed = not refusals
    return {"allowed": gate_passed and ENABLED, "gate_passed": gate_passed,
            "refusals": refusals, "enabled": ENABLED, "data_operations": 0,
            "rows_written": 0,
            "statement_placeholders": list(schema.STATEMENT_PLACEHOLDERS),
            "writer_role_bound": bool(str(writer_role or "").strip()) and
                                not any("writer role" in item for item in refusals)}


def execute_packet(**_kwargs: Any) -> None:
    """There is no write path.  This entry point always refuses."""
    raise WritePathNotImplemented(
        "this packet has no executable write path: design-only, disabled, and unapplied")


def rollback_gate(packet: Any, *, authorization: str | None = None,
                  applied_receipt: Any = None,
                  unexplained_rows: int = 0,
                  repo: Path = REPO) -> dict[str, Any]:
    """Rollback needs authorization, a valid packet, and a receipt that owns it."""
    refusals: list[str] = []
    if not ENABLED:
        refusals.append("rollback is DISABLED by design (ENABLED is False)")
    if authorization != AUTHORIZATION_TOKEN:
        refusals.append("the authorization token is missing or wrong")
    problems = validate_packet(packet, repo=repo)
    if problems:
        refusals.append(f"the packet does not validate: {problems[:2]}")
    if not isinstance(applied_receipt, Mapping):
        refusals.append("an immutable apply receipt is required")
    else:
        if applied_receipt.get("kind") != APPLY_RECEIPT_KIND:
            refusals.append(f"the apply receipt kind must be {APPLY_RECEIPT_KIND!r}")
        if applied_receipt.get("packet_digest") != (packet or {}).get("digest"):
            refusals.append("the apply receipt does not own this packet digest")
        if list(applied_receipt.get("statements") or []) != \
                list((packet or {}).get("schema_operations") or []):
            refusals.append("the apply receipt does not record this packet's statements")
        if sorted(applied_receipt.get("objects_created") or []) != sorted(schema.OWNED_OBJECTS):
            refusals.append("the apply receipt does not own exactly the packet's objects")
        if not str(applied_receipt.get("approver") or "").strip():
            refusals.append("the apply receipt records no approver")
        if not str(applied_receipt.get("writer_role") or "").strip():
            refusals.append("the apply receipt records no bound writer role")
    if unexplained_rows:
        refusals.append(f"{unexplained_rows} stored rows are not explained by the receipt")
    return {"allowed": False, "refusals": refusals,
            "statements": list(schema.ROLLBACK_DDL),
            "owned_objects": list(schema.OWNED_OBJECTS)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    created = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    packet = build_packet(plan_path=args.plan, created_at=created)
    problems = validate_packet(packet)
    if problems:
        raise PacketRefused(f"the built packet refuses validation: {problems[:3]}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    path = args.out_dir / f"kg-stage3-processing-receipt-store-packet-{created}.json"
    digest = write_immutable(path, packet)
    print(json.dumps({"status": "success", "path": str(path), "digest": digest,
                      "enabled": ENABLED, "data_operations": 0,
                      "schema_operations": len(schema.DDL)}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
