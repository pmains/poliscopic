#!/usr/bin/env python3
"""``stage2_s2_apply_checks.py`` — the fail-closed checks the S2 apply runs.

Every check here is deliberately complete rather than sampled.  Three rules
shape the module:

* **Nothing is truncated.**  A large ``IN`` list is split into chunks and every
  chunk is read; a check that silently inspected only the first slice would
  report success for rows it never looked at.
* **Nothing is read twice for the same decision.**  Identity, source-key and
  agenda-item reads happen *inside* the apply transaction, after a lock, so a
  value cannot change between the check and the write.
* **Absence is failure.**  A row the plan names that cannot be read, an
  unreadable backup receipt, a missing count key, or an unprotected file all
  refuse the apply rather than narrowing its scope.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.entities.detect_entities import integrity_snapshot  # noqa: E402
from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_s1_verify as s1_verify  # noqa: E402
from scripts.kg import stage2_s2_classify as classify_mod  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402

__all__ = [
    "CHUNK",
    "PROTECTED_TABLES",
    "chunks",
    "LOCKED_TABLES",
    "lock_scope",
    "protected_counts",
    "read_integrity",
    "require_protected_receipt",
    "row_counts",
    "verify_agenda_items",
    "verify_attachment_membership",
    "verify_document_identities",
    "verify_held_unlinked",
    "verify_protected_state",
    "verify_source_keys",
]

#: Rows per statement.  Large enough to be cheap, small enough to stay inside a
#: driver's parameter limit — and, critically, every chunk is issued.
CHUNK = 2000

#: One declaration, owned by the classifier and reused here.
PROTECTED_TABLES = classify_mod.PROTECTED_TABLES
protected_counts = classify_mod.protected_counts


class CheckRefused(RuntimeError):
    """A check refused the apply; nothing was written."""


def chunks(items: Sequence[Any], size: int | None = None) -> Iterator[Sequence[Any]]:
    """Every item, exactly once, in order.  Never a truncating prefix.

    ``size`` defaults to the module constant *at call time*, so a test (or an
    operator) can lower it and actually change the chunking.  Binding the
    constant as a default argument would freeze it at import and make the knob
    a lie.
    """
    step = CHUNK if size is None else size
    for start in range(0, len(items), step):
        yield items[start:start + step]


def _rows(connection: Any, statement: str, ids: Sequence[int]) -> Iterable[Any]:
    """Run one ``IN (:ids)`` statement per chunk, yielding every row."""
    query = text(statement).bindparams(bindparam("ids", expanding=True))
    for chunk in chunks(list(ids)):
        yield from connection.execute(query, {"ids": list(chunk)}).mappings()


def row_counts(connection: Any) -> dict[str, int]:
    """Documents total / linked / unlinked."""
    column = documents.TARGET_COLUMN
    total = int(connection.execute(text("SELECT COUNT(*) FROM supporting_documents")).scalar())
    linked = int(connection.execute(
        text(f"SELECT COUNT(*) FROM supporting_documents WHERE {column} IS NOT NULL")).scalar())
    return {"total": total, "linked": linked, "unlinked": total - linked}


def read_integrity(connection: Any, dialect: str) -> dict[str, int]:
    """The integrity snapshot, required on PostgreSQL.

    PostgreSQL always has the gate tables, so a failure there is a real error
    and propagates.  A partial mechanics fixture may not, and there is nothing
    to compare — the empty map makes that explicit rather than silently
    comparing nothing.
    """
    try:
        return {k: int(v) for k, v in integrity_snapshot(connection).items()}
    except Exception:
        if dialect == "postgresql":
            raise
        return {}


def require_protected_receipt(
    receipt_path: str | Path, plan: Mapping[str, Any], engine: Any
) -> dict[str, Any]:
    """Require a protected, target-bound backup receipt covering every count.

    The receipt is the evidence that this database was captured before the
    change.  It must exist, must not be readable by group or other, must name
    every protected table the plan baselines with an equal value, must agree
    with the plan's baseline fingerprint, and must describe *this* server.
    """
    path = Path(receipt_path)
    if not path.exists():
        raise CheckRefused(f"backup receipt not found: {path}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CheckRefused(f"backup receipt is not protected (mode {mode:o}): {path}")
    receipt = json.loads(path.read_text(encoding="utf-8"))

    baseline = plan.get("baseline") or {}
    expected = {k: int(v) for k, v in (baseline.get("db_counts") or {}).items()}
    if not expected:
        raise CheckRefused("plan carries no baseline database counts to bind the backup to")

    restored = receipt.get("counts")
    if not isinstance(restored, Mapping):
        raise CheckRefused("backup receipt carries no restored counts")
    missing = sorted(k for k in expected if k not in restored)
    if missing:
        raise CheckRefused(f"backup receipt is missing required count keys: {missing}")

    try:
        result = receipts.require_receipt(receipt, expected_counts=expected)
    except ValueError as exc:
        raise CheckRefused(f"backup receipt refused: {exc}") from exc

    fingerprint = result.get("counts_fingerprint")
    planned = baseline.get("db_counts_fingerprint")
    if not planned:
        raise CheckRefused("plan carries no baseline counts fingerprint")
    if not fingerprint:
        raise CheckRefused("backup receipt produced no counts fingerprint to compare")
    if fingerprint != planned:
        raise CheckRefused(
            f"backup receipt counts fingerprint {fingerprint} does not match plan baseline {planned}"
        )

    # Four fields, not "some development target": the receipt must describe the
    # same dialect, host, port and database as the plan and the live engine.
    binding = s1_verify.verify_target_binding(plan, engine, receipt)
    if binding:
        raise CheckRefused("backup receipt target refused: " + "; ".join(binding[:5]))
    return {"receipt": receipt, "validated": result, "path": str(path)}


#: The tables an apply reads and therefore must lock, in lock order.
LOCKED_TABLES = ("supporting_documents", "agenda_items")


def lock_scope(
    connection: Any, dialect: str, document_ids: Sequence[int], item_ids: Sequence[int] = ()
) -> None:
    """Lock every row the apply will read, before it reads anything.

    Both halves matter.  The documents are what get written; the agenda items
    are what they are checked *against*, so an item edited between the
    fingerprint check and the write would silently authorise a link to a
    different item.  PostgreSQL takes ``FOR UPDATE`` on each row; SQLite
    serialises writers at the database level and has no ``FOR UPDATE``, so the
    same statements are issued without it there.

    Every chunk is issued for both tables, in a fixed order, so the lock set is
    complete rather than a leading slice.
    """
    for table, ids in (("supporting_documents", document_ids), ("agenda_items", item_ids)):
        statement = f"SELECT id FROM {table} WHERE id IN :ids"
        if dialect == "postgresql":
            statement += " FOR UPDATE"
        query = text(statement).bindparams(bindparam("ids", expanding=True))
        for chunk in chunks(list(ids)):
            connection.execute(query, {"ids": list(chunk)})


def read_document_identities(connection: Any, document_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Source key and fingerprint for every named document, chunked."""
    found: dict[int, dict[str, Any]] = {}
    for row in _rows(
        connection,
        "SELECT id, meeting_db_id, agenda_item_id, agenda_item_number, "
        "document_url, updated_at FROM supporting_documents WHERE id IN :ids",
        document_ids,
    ):
        found[int(row["id"])] = {
            "source_key": row["agenda_item_id"],
            "fingerprint": documents.document_fingerprint(row),
        }
    return found


def read_agenda_items(connection: Any, item_ids: Sequence[int]) -> dict[int, str]:
    """Fingerprint for every referenced canonical agenda item, chunked."""
    found: dict[int, str] = {}
    for row in _rows(
        connection,
        "SELECT id, meeting_db_id, agenda_item_number, agenda_item_id "
        "FROM agenda_items WHERE id IN :ids",
        item_ids,
    ):
        found[int(row["id"])] = documents.agenda_item_fingerprint(row)
    return found


def read_source_keys(connection: Any, document_ids: Sequence[int]) -> dict[int, str]:
    """The source key of every named document, chunked."""
    found: dict[int, str] = {}
    for row in _rows(
        connection, "SELECT id, agenda_item_id FROM supporting_documents WHERE id IN :ids",
        document_ids,
    ):
        found[int(row["id"])] = row["agenda_item_id"]
    return found


def verify_document_identities(
    plan: Mapping[str, Any], observed: Mapping[int, Mapping[str, Any]]
) -> list[str]:
    """Every planned document — attachments *and* holds — must match exactly."""
    problems: list[str] = []
    expected = {int(a["document_id"]): a["document_fingerprint"] for a in plan.get("attachments", [])}
    expected.update({int(h["document_id"]): h["document_fingerprint"] for h in plan.get("holds", [])})
    if len(expected) != len(plan.get("attachments", [])) + len(plan.get("holds", [])):
        problems.append("the plan names the same document more than once")
    for document_id, fingerprint in expected.items():
        row = observed.get(document_id)
        if row is None:
            problems.append(f"document {document_id} is missing")
        elif row["fingerprint"] != fingerprint:
            problems.append(f"document {document_id} drifted since the plan was written")
    unexpected = set(observed) - set(expected)
    if unexpected:
        problems.append(f"{len(unexpected)} documents were read that the plan does not name")
    return problems


def verify_agenda_items(plan: Mapping[str, Any], observed: Mapping[int, str]) -> list[str]:
    """Every referenced target item must exist unchanged."""
    problems: list[str] = []
    expected = {int(a["agenda_item_db_id"]): a["agenda_item_fingerprint"]
                for a in plan.get("attachments", [])}
    for item_id, fingerprint in expected.items():
        actual = observed.get(item_id)
        if actual is None:
            problems.append(f"agenda item {item_id} is missing")
        elif actual != fingerprint:
            problems.append(f"agenda item {item_id} drifted since the plan was written")
    return problems


def verify_source_keys(before: Mapping[int, str], after: Mapping[int, str]) -> list[str]:
    """A preserved source key is the whole point; any drift is fatal."""
    problems: list[str] = []
    if set(before) != set(after):
        problems.append("the set of supporting_documents rows changed")
    for document_id, value in before.items():
        if document_id in after and after[document_id] != value:
            problems.append(f"source key for document {document_id} changed")
    return problems


def verify_held_unlinked(connection: Any, held_ids: Sequence[int]) -> list[str]:
    """Every held row must still be NULL — checked in full, not by prefix."""
    column = documents.TARGET_COLUMN
    linked: list[int] = []
    for row in _rows(
        connection,
        f"SELECT id FROM supporting_documents WHERE id IN :ids AND {column} IS NOT NULL",
        held_ids,
    ):
        linked.append(int(row["id"]))
    return [f"held document {i} was linked" for i in linked]


def verify_attachment_membership(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """The linked set must be exactly the plan's attachments, at the right target."""
    problems: list[str] = []
    column = documents.TARGET_COLUMN
    attachments = plan.get("attachments", [])
    expected = {int(a["document_id"]): int(a["agenda_item_db_id"]) for a in attachments}
    observed: dict[int, int] = {}
    for row in _rows(
        connection,
        f"SELECT id, {column} AS target FROM supporting_documents "
        f"WHERE id IN :ids AND {column} IS NOT NULL",
        sorted(expected),
    ):
        observed[int(row["id"])] = int(row["target"])
    for document_id, target in expected.items():
        if document_id not in observed:
            problems.append(f"planned document {document_id} was not linked")
        elif observed[document_id] != target:
            problems.append(
                f"document {document_id} points at {observed[document_id]}, planned {target}"
            )
    linked = row_counts(connection)["linked"]
    if linked != len(attachments):
        problems.append(f"{linked} rows are linked but the plan links {len(attachments)}")
    return problems


def verify_protected_state(
    before_counts: Mapping[str, int], after_counts: Mapping[str, int],
    before_integrity: Mapping[str, int], after_integrity: Mapping[str, int],
) -> list[str]:
    """Protected counts and integrity must be unchanged by a link-only apply."""
    problems: list[str] = []
    for table, value in before_counts.items():
        if after_counts.get(table) != value:
            problems.append(f"protected count {table} changed: {value} -> {after_counts.get(table)}")
    for metric, value in before_integrity.items():
        if after_integrity.get(metric) != value:
            problems.append(f"integrity {metric} changed: {value} -> {after_integrity.get(metric)}")
    return problems
