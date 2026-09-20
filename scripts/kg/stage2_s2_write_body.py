#!/usr/bin/env python3
"""``stage2_s2_write_body.py`` — the internal, typed, development-capable write body.

This module is the **only** place in Stage 2 that knows how to write ``agenda_items``
or attach a ``supporting_documents`` row.  It used to be inert and SQLite-only; it is
now able to run against the **development** tier, and it still cannot be talked into a
write by a caller.

What it is:

* **typed.**  A write is a ``TypedOperation`` drawn from a closed set of kinds.  There
  is no string of SQL, no template, no table name and no column name that a caller can
  supply.  The set is closed because a union of four known shapes can be reviewed and
  an arbitrary callback cannot.
* **exact-plan-bound.**  Operations are derived from a plan by :func:`operations_for`,
  or by :func:`operations_for_authorized_plan`, which loads the plan canonically by
  path and digest first.  A caller cannot hand in an operation of their own: each one
  carries the plan digest it came from.
* **not caller-enablable.**  There is no switch, no flag, and no keyword that turns the
  write path on or off.  Enablement is a property of the **target**: the body runs
  against a SQLite fixture or a development PostgreSQL database, and refuses anything
  else.  Production is refused structurally, by host and database name, not by a
  policy a caller could relax.
* **private mutations.**  The functions that touch rows are private.  The only public
  write entry point in Stage 2 is :func:`scripts.kg.stage2_s2_execute.execute_plan`,
  which owns its transaction and derives everything from an authorized plan.
* **receipt- and postcondition-gated.**  The executor supplies the postcondition
  expectation derived from the plan, and the write is committed only after it holds.

The public admission path in :mod:`stage2_s2_apply_runner` remains check-only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.db import tier as tier_module  # noqa: E402

__all__ = [
    "ALLOWED_DIALECTS",
    "OPERATION_KINDS",
    "PUBLIC_PARAMETERS",
    "TypedOperation",
    "WriteRefused",
    "assert_writable_target",
    "operations_for",
    "operations_for_authorized_plan",
    "postcondition_expectation",
    "preimage_for",
    "validate_operations",
]

#: The only dialects this body will touch: a SQLite fixture, or development PostgreSQL.
ALLOWED_DIALECTS = ("sqlite", "postgresql")

#: The closed set of operation shapes.  A new kind is a reviewed change here.
OPERATION_KINDS = ("insert_item", "renumber_item", "attach_document", "set_parent_item")

#: The parameters every public entry point in this module may accept.  No callback, no
#: SQL, no table, no mapping that steers a write, and no caller-supplied connection.
PUBLIC_PARAMETERS = ("plan_path", "plan_digest", "role", "plan_dir")

DEFAULT_PLAN_DIR = REPO / "data" / "kg-plans"


class WriteRefused(RuntimeError):
    """The write was refused; nothing was written."""


def assert_writable_target(connection: Any) -> dict[str, Any]:
    """Refuse a target this body may not write.  No caller input decides.

    Fails closed: an unclassifiable dialect, a production host, or a production
    database name is a refusal.  There is deliberately no ``allow_production``
    parameter, because a policy a caller can relax is not a policy.
    """
    dialect = getattr(getattr(connection, "dialect", None), "name", None)
    if dialect not in ALLOWED_DIALECTS:
        raise WriteRefused(
            f"the write body touches only {ALLOWED_DIALECTS}; refusing {dialect!r}")
    info: dict[str, Any] = {"dialect": dialect, "host": None, "database": None,
                            "production_refused": True}
    engine = getattr(connection, "engine", None)
    url = getattr(engine, "url", None)
    if dialect == "postgresql":
        host = getattr(url, "host", None)
        database = getattr(url, "database", None)
        info["host"], info["database"] = host, database
        if tier_module._looks_production_host(host):
            raise WriteRefused(
                f"refusing the production host {host!r}: the write body is for the "
                f"development tier only")
        if str(database or "").lower() in tier_module.PRODUCTION_DATABASE_NAMES:
            raise WriteRefused(
                f"refusing the production database {database!r}: the write body is "
                f"for the development tier only")
    return info


@dataclass(frozen=True)
class TypedOperation:
    """One typed, fully-specified write.  No free-form SQL is representable."""

    kind: str
    plan_digest: str
    meeting_db_id: int
    agenda_item_number: str
    agenda_item_title: str = ""
    body: str | None = None
    item_type_category: str = "item"
    section_level: int = 0
    sort_order: int = 0
    existing_row_id: int | None = None
    document_ids: tuple[int, ...] = field(default_factory=tuple)
    #: The exact, complete column set for an insert, as ordered pairs.  Carried on the
    #: operation so the INSERT cannot name a column the plan did not derive.
    row_values: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.kind not in OPERATION_KINDS:
            raise WriteRefused(
                f"operation kind {self.kind!r} is not one of {OPERATION_KINDS}")
        if not self.plan_digest:
            raise WriteRefused("an operation must carry the plan digest it came from")
        if self.kind == "renumber_item" and self.existing_row_id is None:
            raise WriteRefused("renumber_item requires the existing row id")
        if self.kind == "attach_document" and not self.document_ids:
            raise WriteRefused("attach_document requires the documents to attach")

    @property
    def natural_key(self) -> tuple[int, str]:
        return (self.meeting_db_id, self.agenda_item_number)

    def as_receipt_entry(self) -> dict[str, Any]:
        return {"kind": self.kind, "plan_digest": self.plan_digest,
                "meeting_db_id": self.meeting_db_id,
                "agenda_item_number": self.agenda_item_number,
                "existing_row_id": self.existing_row_id,
                "document_ids": list(self.document_ids)}


def operations_for(plan: Mapping[str, Any], *, role: str) -> tuple[TypedOperation, ...]:
    """Derive the typed operations a plan implies.  A caller cannot invent one.

    The plan is the only source: a value that the plan does not carry cannot appear
    in an operation, so an operation can never name a row the plan did not name.
    """
    from scripts.kg import stage2_artifacts as artifacts

    digest = artifacts.recorded_digest(plan)
    if not digest:
        raise WriteRefused("the plan carries no digest to bind its operations to")
    operations: list[TypedOperation] = []
    if role == "repair":
        for row in sorted(plan.get("rows") or [],
                          key=lambda r: (int(r["meeting_db_id"]),
                                         str(r["agenda_item_number"]))):
            if row.get("action") != "materialise":
                continue
            proposed = row.get("proposed_row") or {}
            operations.append(TypedOperation(
                kind="insert_item", plan_digest=digest,
                meeting_db_id=int(proposed["meeting_db_id"]),
                agenda_item_number=str(proposed["agenda_item_number"]),
                agenda_item_title=str(proposed.get("agenda_item_title") or ""),
                body=proposed.get("body"),
                item_type_category=str(proposed.get("item_type_category") or "item"),
                section_level=int(proposed.get("section_level") or 0),
                sort_order=int(proposed.get("sort_order") or 0),
                document_ids=tuple(sorted(int(d)
                                          for d in row.get("resolves_documents") or ())),
                row_values=tuple(sorted(proposed.items()))))
        return tuple(operations)
    if role == "correction":
        for operation in plan.get("operations") or []:
            action = operation.get("action")
            proposed = operation.get("proposed_row") or {}
            if action == "new_item_row":
                operations.append(TypedOperation(
                    kind="insert_item", plan_digest=digest,
                    meeting_db_id=int(proposed["meeting_db_id"]),
                    agenda_item_number=str(proposed["agenda_item_number"]),
                    agenda_item_title=str(proposed.get("agenda_item_title") or ""),
                    body=proposed.get("body"),
                    item_type_category=str(proposed.get("item_type_category") or "item"),
                    section_level=int(proposed.get("section_level") or 0),
                    sort_order=int(proposed.get("sort_order") or 0),
                    document_ids=tuple(sorted(int(d)
                                              for d in operation.get("resolves_documents") or ())),
                    row_values=tuple(sorted(proposed.items()))))
            elif action == "renumber_existing_row":
                operations.append(TypedOperation(
                    kind="renumber_item", plan_digest=digest,
                    meeting_db_id=int(operation["meeting_db_id"]),
                    agenda_item_number=str(operation["to_label"]),
                    agenda_item_title=str(operation.get("proposed_title") or ""),
                    existing_row_id=int((operation.get("identity") or {})
                                        .get("existing_row_id")
                                        or operation.get("existing_row_id") or 0) or None,
                    document_ids=tuple(sorted(int(d)
                                              for d in operation.get("resolves_documents") or ()))))
        return tuple(operations)
    raise WriteRefused(f"role {role!r} has no defined operation set")


def load_authorized_plan(plan_path: str, plan_digest: str,
                         plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Load an authorized plan canonically, by exact path and digest.

    A mapping is not a plan.  The file is read, its stored digest is verified against
    its bytes, and the digest is then required to equal the authorized one.
    """
    if Path(plan_path).name != plan_path:
        raise WriteRefused(
            f"the authorized plan path must be a plain name: {plan_path!r}")
    folder = Path(plan_dir) if plan_dir is not None else DEFAULT_PLAN_DIR
    path = folder / plan_path
    if not path.exists():
        raise WriteRefused(f"the authorized plan {plan_path!r} does not exist")
    from scripts.kg import stage2_artifacts as artifacts

    if artifacts.is_obsolete(path) is not None:
        raise WriteRefused(f"the authorized plan {plan_path!r} is obsolete")
    document = artifacts.load_verified(path)
    recorded = artifacts.recorded_digest(document)
    if recorded != plan_digest:
        raise WriteRefused(
            f"the authorized plan digest {str(plan_digest)[:16]}... is not the "
            f"artifact's {str(recorded)[:16]}...")
    return document


def operations_for_authorized_plan(plan_path: str, plan_digest: str, *, role: str,
                                   plan_dir: str | Path | None = None,
                                   ) -> tuple[dict[str, Any], tuple[TypedOperation, ...]]:
    """Load the plan canonically, then derive.  The only public derivation entry."""
    plan = load_authorized_plan(plan_path, plan_digest, plan_dir)
    operations = operations_for(plan, role=role)
    problems = validate_operations(plan, operations, role=role)
    if problems:
        raise WriteRefused("; ".join(problems[:5]))
    return plan, operations


def validate_operations(plan: Mapping[str, Any],
                        operations: Sequence[TypedOperation], *, role: str) -> list[str]:
    """The operations must be exactly the ones the plan implies."""
    from scripts.kg import stage2_artifacts as artifacts

    problems: list[str] = []
    digest = artifacts.recorded_digest(plan)
    expected = operations_for(plan, role=role)
    if list(operations) != list(expected):
        problems.append("the operations are not exactly the ones the plan implies")
    for operation in operations:
        if operation.plan_digest != digest:
            problems.append(f"{operation.kind}: bound to another plan digest")
        if operation.kind not in OPERATION_KINDS:
            problems.append(f"{operation.kind!r} is not a registered operation kind")
    keys = [o.natural_key for o in operations if o.kind == "insert_item"]
    if len(keys) != len(set(keys)):
        problems.append("two inserts claim the same natural key")
    return problems


def postcondition_expectation(plan: Mapping[str, Any],
                              operations: Sequence[TypedOperation], *,
                              rows_before: int) -> dict[str, Any]:
    """The exact after-state the plan implies.  Derived, never asserted by a caller."""
    inserts = [o for o in operations if o.kind == "insert_item"]
    renumbers = [o for o in operations if o.kind == "renumber_item"]
    return {
        "row_count": int(rows_before) + len(inserts),
        "inserted": [{"meeting_db_id": o.meeting_db_id,
                      "agenda_item_number": o.agenda_item_number} for o in inserts],
        "renumbered": [{"id": int(o.existing_row_id),
                        "agenda_item_number": o.agenda_item_number} for o in renumbers],
        "attached": [{"document_ids": list(o.document_ids)}
                     for o in inserts if o.document_ids],
    }


def _insert(connection: Any, operation: TypedOperation) -> int:
    from sqlalchemy import text

    if operation.row_values:
        # The complete row the plan derived: the INSERT names exactly those
        # columns, so no column can appear that the plan did not evidence.
        columns = [name for name, _ in operation.row_values]
        sql = ("INSERT INTO agenda_items (" + ", ".join(columns) + ") VALUES ("
               + ", ".join(":" + name for name in columns) + ") RETURNING id")
        return int(connection.execute(
            text(sql), dict(operation.row_values)).scalar())

    row = connection.execute(text(
        "INSERT INTO agenda_items (meeting_db_id, agenda_item_number, "
        "agenda_item_title, agenda_item_text, item_type_category, section_level, "
        "sort_order) VALUES (:meeting_db_id, :number, :title, :title, :category, "
        ":level, :sort_order) RETURNING id"),
        {"meeting_db_id": operation.meeting_db_id,
         "number": operation.agenda_item_number,
         "title": operation.agenda_item_title,
         "category": operation.item_type_category,
         "level": operation.section_level,
         "sort_order": operation.sort_order}).scalar()
    return int(row)


def _renumber(connection: Any, operation: TypedOperation) -> int:
    from sqlalchemy import text

    result = connection.execute(text(
        "UPDATE agenda_items SET agenda_item_number = :number, "
        "agenda_item_title = :title WHERE id = :id"),
        {"number": operation.agenda_item_number,
         "title": operation.agenda_item_title,
         "id": int(operation.existing_row_id)})
    return int(result.rowcount)


def _attach(connection: Any, operation: TypedOperation, item_id: int) -> int:
    from sqlalchemy import bindparam, text

    if not operation.document_ids:
        return 0
    result = connection.execute(
        text("UPDATE supporting_documents SET agenda_item_db_id = :item "
             "WHERE id IN :ids").bindparams(bindparam("ids", expanding=True)),
        {"item": item_id, "ids": list(operation.document_ids)})
    return int(result.rowcount)


def preimage_for(connection: Any, operations: Sequence[TypedOperation]) -> dict[str, Any]:
    """The exact pre-state of everything the operations will touch.

    Captured before any mutation, so a rollback has something to restore and the
    receipt can prove what changed.  Reads only.
    """
    from sqlalchemy import text

    inserts, renumbers, documents = [], [], set()
    for operation in operations:
        if operation.kind == "insert_item":
            found = connection.execute(text(
                "SELECT id FROM agenda_items WHERE meeting_db_id = :m AND "
                "agenda_item_number = :n"),
                {"m": operation.meeting_db_id,
                 "n": operation.agenda_item_number}).scalar()
            inserts.append({"meeting_db_id": operation.meeting_db_id,
                            "agenda_item_number": operation.agenda_item_number,
                            "existed_before": found is not None})
        elif operation.kind == "renumber_item":
            row = connection.execute(text(
                "SELECT agenda_item_number, agenda_item_title FROM agenda_items "
                "WHERE id = :i"), {"i": int(operation.existing_row_id)}).mappings().first()
            renumbers.append({"id": int(operation.existing_row_id),
                              "before": dict(row) if row else None})
        documents.update(int(d) for d in operation.document_ids)
    attachments = []
    for document_id in sorted(documents):
        row = connection.execute(text(
            "SELECT id, agenda_item_id, agenda_item_number, agenda_item_db_id "
            "FROM supporting_documents WHERE id = :i"),
            {"i": document_id}).mappings().first()
        attachments.append({"id": document_id,
                            "before": dict(row) if row else None})
    return {"inserts": inserts, "renumbers": renumbers, "attachments": attachments}


def check_postconditions(connection: Any, expected: Mapping[str, Any]) -> dict[str, Any]:
    """Verify the exact after-state, inside the caller's transaction.  Reads only."""
    from sqlalchemy import text

    problems: list[str] = []
    if "row_count" in expected:
        total = int(connection.execute(
            text("SELECT COUNT(*) FROM agenda_items")).scalar())
        if total != int(expected["row_count"]):
            problems.append(f"agenda_items rows {total} != {expected['row_count']}")
    for key in expected.get("inserted") or []:
        found = connection.execute(text(
            "SELECT id FROM agenda_items WHERE meeting_db_id = :m AND "
            "agenda_item_number = :n"),
            {"m": int(key["meeting_db_id"]),
             "n": str(key["agenda_item_number"])}).scalar()
        if found is None:
            problems.append(f"missing inserted key {key}")
    for entry in expected.get("renumbered") or []:
        found = connection.execute(text(
            "SELECT agenda_item_number FROM agenda_items WHERE id = :i"),
            {"i": int(entry["id"])}).scalar()
        if found is None or str(found) != str(entry["agenda_item_number"]):
            problems.append(f"renumber {entry['id']} did not take")
    return {"problems": problems, "checked": True}


def _execute_operations(connection: Any, operations: Sequence[TypedOperation], *,
                        plan_digest: str,
                        expected: Mapping[str, Any]) -> dict[str, Any]:
    """Run typed operations on the transaction-owner's connection.  PRIVATE.

    The only caller is :func:`scripts.kg.stage2_s2_execute.execute_plan`, which has
    already gated the target, loaded the authorized plan and validated that these
    operations are exactly the ones that plan implies.  This function never commits.
    """
    operations = tuple(operations)
    if not operations:
        raise WriteRefused("no operations to run")
    if not plan_digest:
        raise WriteRefused("the authorization names no plan digest")
    for operation in operations:
        if operation.plan_digest != plan_digest:
            raise WriteRefused(
                "an operation is bound to a different plan than the authorization")

    inserted: list[dict[str, Any]] = []
    renumbered: list[dict[str, Any]] = []
    attached: list[dict[str, Any]] = []
    for operation in operations:
        if operation.kind == "insert_item":
            item_id = _insert(connection, operation)
            inserted.append({"id": item_id, **operation.as_receipt_entry()})
            linked = _attach(connection, operation, item_id)
            if linked:
                attached.append({"item_id": item_id,
                                 "documents": list(operation.document_ids),
                                 "rows": linked})
        elif operation.kind == "renumber_item":
            changed = _renumber(connection, operation)
            renumbered.append({"id": operation.existing_row_id, "rows": changed,
                               **operation.as_receipt_entry()})
        elif operation.kind == "attach_document":
            attached.append({"documents": list(operation.document_ids),
                             "rows": _attach(connection, operation, 0)})
        else:  # pragma: no cover - set_parent_item belongs to containment
            raise WriteRefused(
                "set_parent_item belongs to the containment apply, which this module "
                "does not implement")

    postconditions = check_postconditions(connection, expected)
    if postconditions["problems"]:
        raise WriteRefused("postconditions failed: "
                           + "; ".join(postconditions["problems"][:5]))
    return {"status": "executed", "plan_digest": plan_digest,
            "inserted": inserted, "renumbered": renumbered, "attached": attached,
            "operations": len(operations), "committed_by": "transaction owner",
            "postconditions": postconditions}
