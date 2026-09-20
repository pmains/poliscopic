#!/usr/bin/env python3
"""``stage2_s2_receipt_rollback.py`` — receipt-owned rollback, development tier only.

Rollback is the most dangerous operation in the program, so it is the most
restricted.

* It runs against a SQLite fixture or the **development** tier, and a caller has no
  say in that: the target gate is the same one the write body uses, so production is
  refused structurally — by host and database name — rather than by a flag.
* It accepts **no plan mapping and no receipt object**.  It is given the
  authorized plan's *path and digest* and the receipt's *path*; it loads the plan
  canonically — verifying the stored digest against the file's bytes — **before**
  it loads the receipt, and then requires the receipt to be bound to that exact
  plan and to the exact target.  Anything a caller could fabricate is excluded by
  the interface itself.
* It **owns one explicit transaction**, begun **before the plan is loaded and
  before any preflight read**, and retained through the mutation.  The begin is
  SQLAlchemy's — never a raw ``BEGIN`` statement, and never a caller-supplied
  connection.
* It **preflights everything before mutating anything**: every owned id, every row
  fingerprint, and **both** attachment fields (``agenda_item_id`` *and*
  ``agenda_item_number``).  A rollback that discovered drift halfway through would
  leave a half-undone apply.

It never deletes by natural key: ``(meeting_db_id, agenda_item_number)`` is not an
ownership claim.  This module is not wired to any CLI.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_admission_tx as tx  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as runner  # noqa: E402
from scripts.kg import stage2_s2_receipt as receipt_mod  # noqa: E402
from scripts.kg import stage2_s2_write_body as write_body  # noqa: E402

__all__ = ["RollbackRefused", "preflight", "rollback"]

#: The dialects a rollback may touch.  Kept as a module value so a change to the
#: permitted target set is one reviewed edit, exactly like the write body's.
ALLOWED_DIALECTS = write_body.ALLOWED_DIALECTS

DEFAULT_PLAN_DIR = REPO / "data" / "kg-plans"

#: Both attachment fields must be restored, and both must preflight.
ATTACHMENT_FIELDS = ("agenda_item_id", "agenda_item_number")


class RollbackRefused(RuntimeError):
    """The rollback is not admissible; nothing was deleted or restored."""


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    """The same fingerprint the runner uses, so a receipt means one thing."""
    return runner.item_row_fingerprint({
        "id": int(row["id"]), "meeting_db_id": int(row["meeting_db_id"]),
        "agenda_item_number": str(row["agenda_item_number"]),
        "title": str(row.get("agenda_item_title") or ""),
        "agenda_item_id": row.get("agenda_item_id") or "",
        "sort_order": row.get("sort_order") or 0,
    })


def _check_dialect(connection: Any) -> str:
    """The same fail-closed target gate the write body uses.  Production is refused."""
    try:
        target = write_body.assert_writable_target(connection)
    except write_body.WriteRefused as exc:
        raise RollbackRefused(str(exc)) from exc
    return str(target["dialect"])


def load_authorized_plan(plan_path: str, plan_digest: str,
                         plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Load the authorized plan canonically, by exact path and digest.

    A mapping is not a plan.  The file is read, its stored digest is verified
    against its bytes, and the digest is then required to equal the authorized one,
    so the receipt can be bound to something that was actually reviewed.
    """
    if Path(plan_path).name != plan_path:
        raise RollbackRefused(f"the authorized plan path must be a plain name: {plan_path!r}")
    folder = Path(plan_dir) if plan_dir is not None else DEFAULT_PLAN_DIR
    path = folder / plan_path
    if not path.exists():
        raise RollbackRefused(f"the authorized plan {plan_path!r} does not exist")
    if artifacts.is_obsolete(path) is not None:
        raise RollbackRefused(f"the authorized plan {plan_path!r} is obsolete")
    document = artifacts.load_verified(path)
    recorded = artifacts.recorded_digest(document)
    if recorded != plan_digest:
        raise RollbackRefused(
            f"the authorized plan digest {str(plan_digest)[:16]}... is not the "
            f"artifact's {str(recorded)[:16]}...")
    return document


def _preflight(connection: Any, receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Read and verify every owned row and field WITHOUT mutating anything."""
    from sqlalchemy import text

    rows = receipt.get("inserted_rows") or []
    preimages = receipt.get("attachment_preimages") or []
    problems: list[str] = []
    owned: list[dict[str, Any]] = []
    for entry in rows:
        row = connection.execute(text(
            "SELECT id, meeting_db_id, agenda_item_number, agenda_item_title, "
            "COALESCE(agenda_item_id, '') AS agenda_item_id, "
            "COALESCE(sort_order, 0) AS sort_order FROM agenda_items WHERE id = :i"),
            {"i": int(entry["id"])}).mappings().first()
        if row is None:
            problems.append(f"inserted row {entry['id']} is absent")
            continue
        if _row_fingerprint(row) != entry.get("row_fingerprint"):
            problems.append(f"inserted row {entry['id']} has drifted")
            continue
        owned.append({"id": int(entry["id"])})

    restores: list[dict[str, Any]] = []
    for preimage in preimages:
        document_id = int(preimage["id"])
        row = connection.execute(text(
            "SELECT agenda_item_id, agenda_item_number FROM supporting_documents "
            "WHERE id = :i"), {"i": document_id}).mappings().first()
        if row is None:
            problems.append(f"document {document_id} is absent")
            continue
        expected = preimage.get("postimage") or {}
        for field in ATTACHMENT_FIELDS:
            if str(row[field] or "") != str(expected.get(field) or ""):
                problems.append(f"document {document_id} {field} has drifted since apply")
        restores.append({"id": document_id,
                         "agenda_item_id": preimage.get("agenda_item_id"),
                         "agenda_item_number": preimage.get("agenda_item_number")})

    if problems:
        raise RollbackRefused("; ".join(problems[:5]))
    return {"owned": owned, "restores": restores,
            "checked_fields": list(ATTACHMENT_FIELDS)}


def _unit(receipt_path: str | Path, *, plan_path: str, plan_digest: str,
          target: Mapping[str, Any], plan_dir: str | Path | None,
          mutate: bool) -> Callable[[Any], dict[str, Any]]:
    """The whole rollback as one unit: dialect, plan, receipt, preflight, mutation."""

    def unit(connection: Any) -> dict[str, Any]:
        _check_dialect(connection)
        # The authorized plan is loaded canonically FIRST, so the receipt is bound
        # to a reviewed artifact rather than to whatever a caller asserted.
        plan = load_authorized_plan(plan_path, plan_digest, plan_dir)
        receipt = receipt_mod.load_authorized_receipt(
            receipt_path, plan_path=plan_path, plan_digest=plan_digest,
            plan_replay_digest=plan.get("replay_digest"), target=target)

        checked = _preflight(connection, receipt)
        if not mutate:
            return {"status": "preflight-only", **checked,
                    "plan_digest": plan_digest, "plan_path": plan_path}

        from sqlalchemy import text
        for entry in checked["owned"]:
            connection.execute(text("DELETE FROM agenda_items WHERE id = :i"),
                               {"i": entry["id"]})
        for entry in checked["restores"]:
            connection.execute(text(
                "UPDATE supporting_documents SET agenda_item_id = :a, "
                "agenda_item_number = :n WHERE id = :i"),
                {"a": entry["agenda_item_id"], "n": entry["agenda_item_number"],
                 "i": entry["id"]})
        return {"status": "rolled-back", "plan_digest": plan_digest,
                "plan_path": plan_path,
                "deleted": sorted(e["id"] for e in checked["owned"]),
                "restored": sorted(e["id"] for e in checked["restores"]),
                "deleted_by_natural_key": False,
                "mutations": len(checked["owned"]) + len(checked["restores"]),
                "checked_fields": checked["checked_fields"]}

    return unit


def _run(engine: Any, unit: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """Run the unit in a transaction this module owns, mapping refusals.

    The dialect guard runs **before** anything is acquired: refusing a non-fixture
    database is the one check that must happen before a connection exists at all.
    ``max_attempts=1``: a rollback is destructive, so it is performed once and
    never silently replayed.  The transaction itself is begun by the owner
    **before the plan is loaded and before any preflight read**.
    """
    url = getattr(engine, "url", None)
    if url is not None:
        try:
            write_body.assert_writable_target(
                type("_T", (), {"dialect": getattr(engine, "dialect", None),
                                "engine": engine})())
        except write_body.WriteRefused as exc:
            raise RollbackRefused(str(exc)) from exc
    try:
        return tx.run_unit(engine, unit, max_attempts=1)
    except tx.TransactionRefused as exc:
        raise RollbackRefused(str(exc)) from exc
    except receipt_mod.ReceiptRefused as exc:
        raise RollbackRefused(str(exc)) from exc


def preflight(engine: Any, receipt_path: str | Path, *, plan_path: str,
              plan_digest: str, target: Mapping[str, Any],
              plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Read and verify every owned row and field WITHOUT mutating anything.

    Still runs inside a transaction this module owns, begun before the plan or the
    receipt is loaded.
    """
    return _run(engine, _unit(receipt_path, plan_path=plan_path,
                              plan_digest=plan_digest, target=target,
                              plan_dir=plan_dir, mutate=False))


def rollback(engine: Any, receipt_path: str | Path, *, plan_path: str,
             plan_digest: str, target: Mapping[str, Any],
             plan_dir: str | Path | None = None) -> dict[str, Any]:
    """Preflight everything, then mutate once, in one transaction this module owns."""
    return _run(engine, _unit(receipt_path, plan_path=plan_path,
                              plan_digest=plan_digest, target=target,
                              plan_dir=plan_dir, mutate=True))
