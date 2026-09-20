#!/usr/bin/env python3
"""``stage2_s2_execute.py`` — the one public write entry point for Stage 2 plans.

Everything a caller can say is *which reviewed plan to run*, *who authorized it*, and
*where to put the artifacts*.  A caller cannot supply operations, SQL, a table name, a
callback, a mapping that steers a write, or a connection.

The shape of a run:

1. **fail-closed target gate** — a SQLite fixture or development PostgreSQL; production
   is refused structurally by host and database name;
2. **one owned SERIALIZABLE transaction**, begun before the first check, retained
   through the write, its postconditions and the commit, and retried whole on a
   serialization failure (:mod:`stage2_s2_admission_tx`);
3. **canonical plan load** by exact path and digest, then bound-code, contract and
   drift checks;
4. **derivation only** — typed operations and reservation operations come from the plan;
5. **advisory locks in deterministic key order**, then occupancy and reservation
   checks, then the reservation inserts;
6. **typed mutations**, then **in-transaction postconditions**;
7. **commit only after every check holds**; the preimage and the terminal receipt are
   written afterwards as immutable artifacts.

A replay of an apply that already committed is an exact no-op: it detects its own
reservations and rows, writes nothing, and records ``no-op-already-applied``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import inspect as sa_inspect, text  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_reservation as reservation  # noqa: E402
from scripts.kg import stage2_s2_admission_binding as admission_binding  # noqa: E402
from scripts.kg import stage2_s2_admission_tx as tx  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as runner  # noqa: E402
from scripts.kg import stage2_s2_receipt as receipt_mod  # noqa: E402
from scripts.kg import stage2_s2_reservation_binding as reservation_binding  # noqa: E402
from scripts.kg import stage2_s2_write_body as write_body  # noqa: E402

__all__ = [
    "EXECUTE_PARAMETERS",
    "ExecuteRefused",
    "affected_scope_digest",
    "execute_plan",
    "replay",
]

DEFAULT_PLAN_DIR = REPO / "data" / "kg-plans"
DEFAULT_RECEIPT_DIR = REPO / "data" / "kg-plans"

#: Every parameter the public entry points accept.  No callback, no SQL, no table, no
#: connection, and no mapping that could steer a write.
EXECUTE_PARAMETERS = ("plan_path", "plan_digest", "role", "approver", "receipt_dir",
                      "preimage_dir", "plan_dir", "engine", "max_attempts",
                      "expected_scope_sha256")


class ExecuteRefused(RuntimeError):
    """The plan was not executed; nothing was written."""


def _utc_stamp() -> str:
    """The artifact-name stamp, in the form every other Stage 2 artifact uses."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _reserve_operations(plan: Mapping[str, Any], *, role: str) -> list[dict[str, Any]]:
    """The subset of the plan's reservation operations that actually reserve."""
    body = plan.get("reservation_operations") or {}
    return sorted((dict(o) for o in body.get("operations") or []
                   if o.get("outcome") == "reserve"), key=lambda o: str(o["key"]))


def _reservation_problems(connection: Any) -> list[str]:
    """The reservation table must be exactly the contract, or it proves nothing."""
    if connection.dialect.name != "postgresql":
        if not sa_inspect(connection).has_table(reservation.RESERVATION_TABLE):
            return [f"{reservation.RESERVATION_TABLE} does not exist"]
        return []
    return reservation.verify_reservation_contract(connection)


def _occupied_keys(connection: Any,
                   operations: Sequence[Mapping[str, Any]]) -> list[str]:
    """Which of the keys a plan would create a live agenda item already holds."""
    found: list[str] = []
    for entry in operations:
        row = connection.execute(text(
            "SELECT id FROM agenda_items WHERE meeting_db_id = :m AND "
            "agenda_item_number = :n"),
            {"m": int(entry["meeting_db_id"]),
             "n": str(entry["agenda_item_number"])}).scalar()
        if row is not None:
            found.append(str(entry["key"]))
    return sorted(found)


def affected_scope_digest(connection: Any, operations: Sequence[Any]) -> str:
    """A canonical digest of the rows and documents a plan will touch.

    Drift is then a comparison of two digests rather than a diff of two prose
    reports.  Reads only.
    """
    rows: list[dict[str, Any]] = []
    documents: set[int] = set()
    meetings: set[int] = set()
    for operation in operations:
        meetings.add(int(operation.meeting_db_id))
        documents.update(int(d) for d in operation.document_ids)
        row = connection.execute(text(
            "SELECT id, meeting_db_id, agenda_item_number, COALESCE(agenda_item_title,'') "
            "AS t, COALESCE(agenda_item_id,'') AS a, COALESCE(sort_order,0) AS s "
            "FROM agenda_items WHERE meeting_db_id = :m AND agenda_item_number = :n"),
            {"m": operation.meeting_db_id,
             "n": operation.agenda_item_number}).mappings().first()
        rows.append({"key": f"{operation.meeting_db_id}|{operation.agenda_item_number}",
                     "present": row is not None, "row": dict(row) if row else None})
    for document_id in sorted(documents):
        row = connection.execute(text(
            "SELECT id, agenda_item_db_id, agenda_item_id, agenda_item_number "
            "FROM supporting_documents WHERE id = :i"),
            {"i": document_id}).mappings().first()
        rows.append({"document": document_id, "row": dict(row) if row else None})
    return binding.canonical_sha256({"rows": rows, "meetings": sorted(meetings)})


def _load_and_check(plan_path: str, plan_digest: str, role: str,
                    plan_dir: str | Path | None, connection: Any,
                    expected_scope_sha256: str | None) -> tuple[dict[str, Any],
                                                                tuple[Any, ...]]:
    """Canonical load, then every fail-closed check the plan itself must satisfy."""
    plan = write_body.load_authorized_plan(plan_path, plan_digest, plan_dir)
    if plan.get("role") not in (None, role) and plan.get("kind", "").find(role) < 0:
        raise ExecuteRefused(f"the plan is not a {role!r} plan")
    admission_binding._verify_code_hashes(plan)          # bound code == disk code
    problems = _reservation_problems(connection)         # schema contract
    if problems:
        raise ExecuteRefused("the reservation contract is not satisfied: "
                             + "; ".join(problems[:4]))
    operations = write_body.operations_for(plan, role=role)
    problems = write_body.validate_operations(plan, operations, role=role)
    if problems:
        raise ExecuteRefused("the operations are not the plan's: "
                             + "; ".join(problems[:4]))
    problems = reservation_binding.validate_reservation_operations(
        plan.get("reservation_operations") or {}, plan, role=role)
    if problems:
        raise ExecuteRefused("the reservation operations are not the plan's: "
                             + "; ".join(problems[:4]))
    # ``expected_scope_sha256`` is THIS executor's affected-scope digest, computed by
    # the caller from a read taken moments earlier.  It is deliberately NOT compared
    # against the plan's bound ``current_state_sha256``: that value comes from the S2
    # current-state machinery over a different scope and a different algorithm, so
    # comparing them could only ever refuse.  The plan's own state binding is proved
    # by the admission path; what is proved HERE is that the exact rows and documents
    # the caller pinned are the ones this transaction sees.
    if expected_scope_sha256 is not None:
        live = affected_scope_digest(connection, operations)
        if live != expected_scope_sha256:
            raise ExecuteRefused(
                f"the affected scope has drifted: {live[:16]}... is not the expected "
                f"{str(expected_scope_sha256)[:16]}...")
    return plan, operations


def _unit(plan_path: str, plan_digest: str, role: str, approver: str,
          plan_dir: str | Path | None,
          expected_scope_sha256: str | None) -> Callable[[Any], dict[str, Any]]:
    """The whole apply as one unit, run inside the transaction the owner began."""

    def unit(connection: Any) -> dict[str, Any]:
        target = write_body.assert_writable_target(connection)
        plan, operations = _load_and_check(plan_path, plan_digest, role, plan_dir,
                                           connection, expected_scope_sha256)
        reserves = _reserve_operations(plan, role=role)

        # Locks first, in deterministic key order, so the occupancy read and the
        # reservation insert cannot interleave with another writer on the same key.
        for entry in reserves:
            reservation.lock_key(connection, int(entry["meeting_db_id"]),
                                 str(entry["agenda_item_number"]))

        prior = reservation.existing_reservations(connection, reserves)
        inserted_keys = [op.natural_key for op in operations
                         if op.kind == "insert_item"]
        live_rows = [key for key in inserted_keys
                     if connection.execute(text(
                         "SELECT id FROM agenda_items WHERE meeting_db_id = :m AND "
                         "agenda_item_number = :n"),
                         {"m": key[0], "n": key[1]}).scalar() is not None]

        # ── exact no-op replay, decided BEFORE occupancy ───────────────────
        # On a replay the keys ARE occupied and ARE reserved — by this very plan, in
        # an earlier committed run.  Reading occupancy first would call our own rows a
        # conflict, so the replay test runs first and is exact: every key reserved by
        # THIS digest, and every planned row already present.
        if len(reserves) and len(prior) == len(reserves) and \
                all(prior[e["key"]]["plan_digest"] == plan_digest for e in reserves) \
                and len(live_rows) == len(inserted_keys):
            return {"status": "replayed", "commit_status": "no-op-already-applied",
                    "writes": 0, "plan_digest": plan_digest, "plan_path": plan_path,
                    "role": role, "target": target,
                    "plan_replay_digest": plan.get("replay_digest"),
                    "reserved_already": len(prior), "rows_already": len(live_rows),
                    "operations": len(operations), "inserted": [],
                    "preimage": None, "reservations": 0}

        occupied = _occupied_keys(connection, reserves)
        if occupied:
            raise ExecuteRefused(
                "refusing: a live agenda item already holds "
                + ", ".join(occupied[:5]))

        if prior:
            raise ExecuteRefused(
                "refusing: keys already reserved by another plan: "
                + ", ".join(sorted(prior)[:5]))

        writes_expected = len([o for o in operations if o.kind == "insert_item"]) + \
            len([o for o in operations if o.kind == "renumber_item"])
        rows_before = int(connection.execute(
            text("SELECT COUNT(*) FROM agenda_items")).scalar())
        preimage = write_body.preimage_for(connection, operations)

        reserved = reservation.reserve_keys(
            connection, reserves, plan_digest=plan_digest, reserved_by=approver)
        if reserved["held"]:
            raise ExecuteRefused("refusing: a reservation was held: "
                                 + "; ".join(str(h)[:80] for h in reserved["held"][:3]))

        expected = write_body.postcondition_expectation(
            plan, operations, rows_before=rows_before)
        result = write_body._execute_operations(          # noqa: SLF001 - private by design
            connection, operations, plan_digest=plan_digest, expected=expected)

        rows_after = int(connection.execute(
            text("SELECT COUNT(*) FROM agenda_items")).scalar())
        if rows_after != expected["row_count"]:
            raise ExecuteRefused(
                f"the row count is {rows_after}, not the planned {expected['row_count']}")

        owned = []
        for entry in result["inserted"]:
            row = connection.execute(text(
                "SELECT id, meeting_db_id, agenda_item_number, "
                "COALESCE(agenda_item_title,'') AS title, "
                "COALESCE(agenda_item_id,'') AS agenda_item_id, "
                "COALESCE(sort_order,0) AS sort_order FROM agenda_items WHERE id = :i"),
                {"i": int(entry["id"])}).mappings().first()
            owned.append({"id": int(row["id"]),
                          "meeting_db_id": int(row["meeting_db_id"]),
                          "agenda_item_number": str(row["agenda_item_number"]),
                          "row_fingerprint": runner.item_row_fingerprint(row)})
        attachments = []
        for entry in preimage["attachments"]:
            row = connection.execute(text(
                "SELECT agenda_item_id, agenda_item_number, agenda_item_db_id "
                "FROM supporting_documents WHERE id = :i"),
                {"i": int(entry["id"])}).mappings().first()
            if row is not None and row["agenda_item_db_id"] is not None:
                attachments.append({"id": int(entry["id"]),
                                    "preimage": entry["before"],
                                    "postimage": {"agenda_item_id": row["agenda_item_id"],
                                                  "agenda_item_number": row["agenda_item_number"],
                                                  "agenda_item_db_id": row["agenda_item_db_id"]}})
        return {"status": "committed", "commit_status": "committed",
                "writes": writes_expected, "plan_digest": plan_digest,
                "plan_replay_digest": plan.get("replay_digest"),
                "plan_path": plan_path, "role": role, "target": target,
                "operations": len(operations),
                "reservations": len(reserved["inserted"]),
                "inserted": owned, "attachment_preimages": attachments,
                "preimage": preimage, "rows_before": rows_before,
                "rows_after": rows_after, "postconditions": result["postconditions"],
                "execution": result}

    return unit


def _early_target_refusal(engine: Any) -> None:
    """Refuse a production engine before a connection exists at all."""
    url = getattr(engine, "url", None)
    if url is None:
        return
    if tier_module._looks_production_host(getattr(url, "host", None)):
        raise ExecuteRefused("refusing to execute against a production host")
    if str(getattr(url, "database", None) or "").lower() in \
            tier_module.PRODUCTION_DATABASE_NAMES:
        raise ExecuteRefused("refusing to execute against a production database")


def execute_plan(engine: Any, *, plan_path: str, plan_digest: str, role: str,
                 approver: str, receipt_dir: str | Path | None = None,
                 preimage_dir: str | Path | None = None,
                 plan_dir: str | Path | None = None,
                 max_attempts: int = tx.MAX_SERIALIZATION_ATTEMPTS,
                 expected_scope_sha256: str | None = None,
                 write_artifacts: bool = True) -> dict[str, Any]:
    """Execute one authorized plan, or refuse.  The only public write entry point."""
    if not str(approver or "").strip():
        raise ExecuteRefused("an apply must name who authorized it")
    if not plan_digest:
        raise ExecuteRefused("no plan digest was authorized")
    _early_target_refusal(engine)
    folder = Path(plan_dir) if plan_dir is not None else DEFAULT_PLAN_DIR
    try:
        result = tx.run_unit(engine, _unit(plan_path, plan_digest, role, approver,
                                           folder, expected_scope_sha256),
                             max_attempts=max_attempts)
    except ExecuteRefused:
        raise
    except tx.TransactionRefused as exc:
        raise ExecuteRefused(str(exc)) from exc
    except write_body.WriteRefused as exc:
        raise ExecuteRefused(str(exc)) from exc
    except admission_binding.ApplyRefused as exc:
        raise ExecuteRefused(str(exc)) from exc
    except artifacts.ArtifactDigestMismatch as exc:
        raise ExecuteRefused(f"the authorized plan failed verification: {exc}") from exc
    except SQLAlchemyError as exc:
        raise ExecuteRefused(
            f"the apply failed and was rolled back: {exc}") from exc

    result["artifacts"] = {}
    if write_artifacts:
        out = Path(receipt_dir) if receipt_dir is not None else DEFAULT_RECEIPT_DIR
        if result["preimage"] is not None:
            pre = Path(preimage_dir) if preimage_dir is not None else out
            result["artifacts"]["preimage"] = _write_preimage(pre, result)
        result["artifacts"]["receipt"] = _write_receipt(out, result, approver)
    return result


def _write_preimage(out_dir: Path, result: Mapping[str, Any]) -> dict[str, Any]:
    stamp = _utc_stamp()
    path = out_dir / f"kg-stage2-s2-preimage-{stamp}-{result['plan_digest'][:16]}.json"
    payload = {"kind": "kg-stage2-s2-apply-preimage", "version": "1.0",
               "plan_path": result["plan_path"], "plan_digest": result["plan_digest"],
               "role": result["role"], "target": result["target"],
               "rows_before": result["rows_before"],
               "preimage": result["preimage"]}
    digest = artifacts.write_immutable(path, payload)
    return {"path": path.name, "digest": digest}


def _write_receipt(out_dir: Path, result: Mapping[str, Any],
                   approver: str) -> dict[str, Any]:
    stamp = _utc_stamp()
    path = out_dir / (f"kg-stage2-s2-apply-receipt-{stamp}-"
                      f"{result['plan_digest'][:16]}.json")
    payload = {
        "kind": receipt_mod.RECEIPT_KIND, "version": receipt_mod.RECEIPT_VERSION,
        "plan_role": result["role"], "plan_path": result["plan_path"],
        "plan_digest": result["plan_digest"],
        "plan_replay_digest": result.get("plan_replay_digest"),
        "target": {k: result["target"].get(k) for k in ("dialect", "host", "port",
                                                        "database")},
        "commit_status": result["commit_status"],
        "authorized_by": approver,
        "writes": result["writes"],
        "reservations": result.get("reservations", 0),
        "inserted_rows": result["inserted"],
        "attachment_preimages": result.get("attachment_preimages", []),
    }
    digest = artifacts.write_immutable(path, payload)
    return {"path": path.name, "digest": digest}


def replay(engine: Any, *, plan_path: str, plan_digest: str, role: str, approver: str,
           plan_dir: str | Path | None = None,
           max_attempts: int = tx.MAX_SERIALIZATION_ATTEMPTS) -> dict[str, Any]:
    """Re-run an apply and require it to be an exact no-op.  Writes nothing."""
    result = execute_plan(engine, plan_path=plan_path, plan_digest=plan_digest,
                          role=role, approver=approver, plan_dir=plan_dir,
                          max_attempts=max_attempts, write_artifacts=False)
    if result["status"] != "replayed" or int(result["writes"]) != 0:
        raise ExecuteRefused(
            f"the replay was not a no-op: status {result['status']!r}, "
            f"writes {result['writes']}")
    return result
