#!/usr/bin/env python3
"""``stage1_apply_runner.py`` — manifest-only Stage 1 apply runner (development only).

**This runner must not be executed without explicit human adjudication and a
separate apply authorization.**  It is the single, deliberate authority that turns
the reviewed Stage 1 packet into database changes.

Contract
--------
* **Development only.**  The target is asserted before anything else.
* **Canonically bound.**  The supplied packet is not trusted on its own consistency:
  its operations, target ids, body values, component paths/hashes/digests,
  374+18 identities, meeting scope, quarantine reason, human fields, code
  fingerprint and expected counts are compared against the expectation
  reconstructed from the reviewed sources.  A forged manifest that has been
  correctly re-hashed is therefore still refused.
* **Live registry.**  The quarantine reason is validated against the authoritative
  registry, never a packet-supplied list.
* **One transaction.**  Schema state, collisions, fingerprints and baseline are
  re-checked *inside* the transaction, the mutations run there, and the aggregate
  postconditions and integrity snapshot are evaluated on that same connection — so
  uncommitted changes are visible and no second connection is used.
* **Exact rowcounts.**  Exactly 3 inserts, 55 meeting updates and 18 quarantine
  updates, verified before commit.
* **Immutable receipt.**  A terminal receipt is recorded with exclusive creation.
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

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.db import quarantine_schema  # noqa: E402
from scripts.entities.event_normalize_artifacts import write_exclusive  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage1_packet_components as components  # noqa: E402
from scripts.kg import stage1_runner_checks as checks  # noqa: E402
from scripts.kg.phoenix_dr_adjudication import assert_development_target  # noqa: E402

__all__ = [
    "PACKET_VERSION",
    "apply_packet",
    "body_insert_sql",
    "configure_engine",
    "load_packet",
    "parity_expectations",
    "run",
    "verify_packet",
    "write_terminal_receipt",
]

PACKET_VERSION = "kg-stage1-execution-packet/2.0"

EXPECTED_INSERTS = 3
EXPECTED_MEETING_UPDATES = 55
EXPECTED_QUARANTINE_UPDATES = 18

_MEETING_UPDATE_SQL = (
    "UPDATE meetings SET "
    "public_body_id = (SELECT id FROM public_bodies WHERE body_code = :body_code), "
    "jurisdiction_id = :jurisdiction_id "
    "WHERE id IN :meeting_ids AND public_body_id IS NULL"
)

_QUARANTINE_UPDATE_SQL = (
    "UPDATE meeting_event_extractions SET "
    "quarantine_reason = :quarantine_reason, quarantined_at = :quarantined_at, "
    "quarantined_by = :quarantined_by, decision_id = :decision_id, "
    "model_version = :model_version "
    "WHERE id = :id AND quarantine_reason IS NULL"
)

#: The parameterized columns of a body insert (everything except audit fields).
_BODY_INSERT_VALUE_COLUMNS = (
    "body_code", "name", "slug", "body_type", "jurisdiction_id", "description",
)


def body_insert_sql() -> str:
    """Compose the body INSERT from reviewed constants.

    Every value is a bind parameter; the *only* non-parameterized fragment is the
    reviewed audit-timestamp expression, taken from
    :mod:`scripts.kg.stage1_packet_components` rather than from any caller-supplied
    packet — so a packet can never inject SQL here, only be refused.
    """
    columns = list(_BODY_INSERT_VALUE_COLUMNS) + list(components.AUDIT_TIMESTAMP_COLUMNS)
    placeholders = [f":{name}" for name in _BODY_INSERT_VALUE_COLUMNS]
    placeholders += [components.AUDIT_TIMESTAMP_EXPRESSION] * len(
        components.AUDIT_TIMESTAMP_COLUMNS)
    return (
        f"INSERT INTO public_bodies ({', '.join(columns)}) "
        f"VALUES ({', '.join(placeholders)})"
    )


def configure_engine(engine, *, apply: bool):
    """Assert the development target always; guard writes only for read-only runs.

    Order matters: the target is asserted on *every* path before any database work,
    and the mutating-statement guard is installed only when this process will not
    write.  The apply path must never carry that guard.
    """
    from scripts.entities.event_normalize_preflight import guard_engine

    info = assert_development_target(engine)
    if not apply:
        guard_engine(engine)
    return info


def load_packet(path: str) -> tuple[dict[str, Any], list[str]]:
    """Load a packet artifact and recompute its digest."""
    file_path = Path(path)
    if not file_path.is_file():
        return {}, [f"missing packet {path}"]
    document = json.loads(file_path.read_text(encoding="utf-8"))
    declared = document.get("packet_digest")
    body = {key: value for key, value in document.items() if key != "packet_digest"}
    recomputed = hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    problems: list[str] = []
    if document.get("applied") is not False:
        problems.append("packet does not declare itself unapplied")
    if declared != recomputed:
        problems.append(f"packet digest {recomputed} != declared {declared}")
    if document.get("packet_version") != PACKET_VERSION:
        problems.append(
            f"packet version {document.get('packet_version')!r} != {PACKET_VERSION!r}"
        )
    return document, problems


def verify_packet(packet: Mapping[str, Any], expected_digest: str | None) -> list[str]:
    """Self-consistency, exact digest and the authorization boundary."""
    problems: list[str] = []
    if not expected_digest:
        problems.append("an exact --packet-digest is required")
    elif packet.get("packet_digest") != expected_digest:
        problems.append(
            f"packet digest {packet.get('packet_digest')} != expected {expected_digest}"
        )
    counts = packet.get("counts") or {}
    for key, expected in (("meeting_updates", EXPECTED_MEETING_UPDATES),
                          ("public_body_inserts", EXPECTED_INSERTS),
                          ("quarantine_updates", EXPECTED_QUARANTINE_UPDATES)):
        if counts.get(key) != expected:
            problems.append(f"{key} must be {expected}")
    if (packet.get("verification") or {}).get("ready_for_human_adjudication") is not True:
        problems.append("packet is not marked ready for human adjudication")

    values = (packet.get("quarantine") or {}).get("values") or {}
    for field in components.HUMAN_REQUIRED_FIELDS:
        if values.get(field) in (None, ""):
            problems.append(f"human field {field} is not supplied")
    # NOTE: the reason is validated against the live registry in
    # stage1_runner_checks.canonical_problems, never against packet-supplied lists.
    authorization = (packet.get("adjudication") or {}).get("authorization") or {}
    if authorization.get("apply_authorized") is not True:
        problems.append(
            "the adjudication does not authorize a migration/data apply"
        )
    return problems


def parity_expectations(dialect: str = "postgresql") -> dict[str, Any]:
    """Prove this runner is the sole authority for the quarantine schema."""
    from scripts.entities.schema_parity import REQUIRED_COLUMNS

    reviewed = list(quarantine_schema.statements_for_review(dialect)["up"])
    required = set(REQUIRED_COLUMNS.get(quarantine_schema.TABLE, set()))
    quarantine_columns = set(quarantine_schema.COLUMN_DDL)
    return {
        "sole_authority": "scripts/kg/stage1_apply_runner.py",
        "dialect": dialect,
        "statements": reviewed,
        "additive_conflict": sorted(required & quarantine_columns),
        "additive": not (required & quarantine_columns),
        "parity_contract_columns": sorted(required),
        "quarantine_columns": sorted(quarantine_columns),
    }


def _expected_receipt_counts(packet: Mapping[str, Any],
                             supplied: Mapping[str, Any] | None) -> dict[str, Any]:
    """Bind the backup receipt's counts to the packet's recorded baseline."""
    if supplied is not None:
        return dict(supplied)
    return dict((packet.get("baseline") or {}).get("counts") or {})


def apply_packet(engine, packet: Mapping[str, Any], *, receipt: Mapping[str, Any] | None,
                 receipt_expected_counts: Mapping[str, Any] | None = None,
                 now: str | None = None,
                 require_canonical_binding: bool = True,
                 require_transactional_ddl: bool = True,
                 integrity_provider=None) -> dict[str, Any]:
    """Apply the packet in one transaction.  Rolls back entirely on any failure."""
    stamp = now or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    terminal: dict[str, Any] = {
        "runner": "scripts/kg/stage1_apply_runner.py",
        "packet_version": PACKET_VERSION,
        "packet_digest": packet.get("packet_digest"),
        "started_at": stamp,
        "status": "refused",
        "problems": [],
        "applied": False,
        "mutations_performed": 0,
    }

    # Every path asserts the development target before touching the database.
    try:
        assert_development_target(engine)
    except Exception as error:  # noqa: BLE001 - reported as a refusal reason
        terminal["problems"] = [f"target refused: {error}"]
        return terminal

    if require_canonical_binding:
        canonical = checks.canonical_problems(packet)
        if canonical:
            terminal["problems"] = [f"canonical binding: {problem}" for problem in canonical]
            return terminal

    verification = receipts.validate_receipt(
        receipt, expected_counts=_expected_receipt_counts(packet, receipt_expected_counts)
    )
    if not verification["valid"]:
        terminal["problems"] = [f"backup receipt: {p}" for p in verification["problems"]]
        return terminal

    dialect = engine.dialect.name
    if require_transactional_ddl:
        refusals = checks.atomicity_refusal(dialect)
        if refusals:
            terminal["problems"] = refusals
            return terminal

    values = (packet["quarantine"] or {}).get("values") or {}
    operations = packet.get("operations") or []
    inserts = [op for op in operations if op["op"] == "INSERT"]
    updates = [op for op in operations
               if op["op"] == "UPDATE" and op["table"] == "meetings"]
    quarantine_ids = list((packet.get("quarantine") or {}).get("target_ids") or ())

    try:
        with engine.begin() as connection:
            checks.require(checks.preflight_rechecks(connection, packet), "locked rechecks")

            executed = quarantine_schema.upgrade(connection, dialect)

            insert_rows = 0
            for operation in inserts:
                connection.execute(text(body_insert_sql()), dict(operation["values"]))
                insert_rows += 1

            meeting_rows = 0
            for operation in updates:
                result = connection.execute(
                    text(_MEETING_UPDATE_SQL).bindparams(
                        bindparam("meeting_ids", expanding=True)),
                    {"body_code": operation["component"],
                     "jurisdiction_id": 4,
                     "meeting_ids": list(operation["target_ids"])},
                )
                meeting_rows += int(result.rowcount or 0)

            quarantine_rows = 0
            for row_id in quarantine_ids:
                result = connection.execute(
                    text(_QUARANTINE_UPDATE_SQL),
                    {"id": row_id,
                     "quarantine_reason": values["quarantine_reason"],
                     "quarantined_at": values["quarantined_at"],
                     "quarantined_by": values["quarantined_by"],
                     "decision_id": values["decision_id"],
                     "model_version": values["model_version"]},
                )
                quarantine_rows += int(result.rowcount or 0)

            for label, observed, expected in (
                ("body inserts", insert_rows, EXPECTED_INSERTS),
                ("meeting updates", meeting_rows, EXPECTED_MEETING_UPDATES),
                ("quarantine updates", quarantine_rows, EXPECTED_QUARANTINE_UPDATES),
            ):
                if observed != expected:
                    raise RuntimeError(f"{label} {observed} != {expected}")

            outcomes = checks.postconditions(connection, packet, quarantine_rows,
                                             integrity_provider=integrity_provider)
            if not outcomes["satisfied"]:
                raise RuntimeError(f"postconditions failed: {outcomes['mismatches']}")
    except checks.RefusalError as error:
        terminal["status"] = "refused"
        terminal["problems"] = [str(error)]
        return terminal
    except Exception as error:  # noqa: BLE001 - any failure rolls the whole run back
        terminal["status"] = "rolled_back"
        terminal["problems"] = [str(error)]
        return terminal

    terminal.update({
        "status": "applied",
        "applied": True,
        "mutations_performed": insert_rows + meeting_rows + quarantine_rows,
        "ddl_statements": list(executed),
        "rowcounts": {
            "public_bodies_inserts": insert_rows,
            "meeting_updates": meeting_rows,
            "quarantine_updates": quarantine_rows,
        },
        "postconditions": outcomes,
        "verification_connection": "the apply transaction connection",
    })
    return terminal


def write_terminal_receipt(terminal: Mapping[str, Any]) -> str:
    """Write the terminal receipt immutably."""
    path = components.DATA_DIR / f"kg-stage1-apply-receipt-{terminal['started_at']}.json"
    write_exclusive(path, json.dumps(terminal, indent=2, sort_keys=True, default=str))
    return str(path)


def run(engine, packet_path: str, *, expected_digest: str | None, apply: bool = False,
        receipt: Mapping[str, Any] | None = None,
        receipt_expected_counts: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Entry point.  Without ``apply=True`` this refuses and changes nothing."""
    if engine is not None:
        try:
            assert_development_target(engine)
        except Exception as error:  # noqa: BLE001 - reported as a refusal reason
            return {"status": "refused", "applied": False,
                    "problems": [f"target refused: {error}"]}
    packet, problems = load_packet(packet_path)
    if problems:
        return {"status": "refused", "applied": False, "problems": problems}
    problems = verify_packet(packet, expected_digest)
    if problems:
        return {"status": "refused", "applied": False, "problems": problems}
    if not apply:
        return {
            "status": "refused",
            "applied": False,
            "problems": ["--apply is required; this runner never applies implicitly"],
        }
    return apply_packet(engine, packet, receipt=receipt,
                        receipt_expected_counts=receipt_expected_counts)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--packet", required=True)
    parser.add_argument("--packet-digest", required=True)
    parser.add_argument("--apply", action="store_true",
                        help="perform the apply; without it nothing is written")
    parser.add_argument("--receipt", default=None, help="path to the backup receipt JSON")
    arguments = parser.parse_args(argv)

    receipt = None
    if arguments.receipt:
        receipt = json.loads(Path(arguments.receipt).read_text(encoding="utf-8"))

    from scripts.db.core import get_engine

    engine = get_engine()
    configure_engine(engine, apply=arguments.apply)

    terminal = run(engine, arguments.packet, expected_digest=arguments.packet_digest,
                   apply=arguments.apply, receipt=receipt)
    print(json.dumps({key: value for key, value in terminal.items()
                      if key != "postconditions"}, indent=2, default=str))
    if terminal.get("status") == "applied":
        print(f"receipt={write_terminal_receipt(terminal)}")
    return 0 if terminal.get("status") == "applied" else 1


if __name__ == "__main__":
    raise SystemExit(main())
