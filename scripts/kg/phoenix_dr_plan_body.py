#!/usr/bin/env python3
"""Plan-body assembly for the ``phoenix-dr`` repair plan.

Turns the adjudicated dispositions into one inert, digest-bound repair plan:
proposed operations, expected postconditions, pre/post integrity expectations,
the transaction contract, the backup contract and restore instructions.

Nothing here writes to the database, and nothing here predicts a surrogate key:
the meetings update resolves the new body by its unique ``body_code`` inside the
same transaction, so the plan never depends on a guessed id.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from sqlalchemy import Engine  # noqa: E402

from scripts.kg.phoenix_dr_adjudication import (  # noqa: E402
    BODY_CODE,
    JURISDICTION_SLUG,
    PROPOSED_BODY_NAME,
    PROPOSED_BODY_SLUG,
    PROPOSED_BODY_TYPE,
    REGISTRY_EVIDENCE,
    PlanError,
    assert_development_target,
    canonical_json,
    collect_dispositions,
    fetch_rows,
    resolve_jurisdiction,
)


def plan_digest(plan: Mapping[str, Any]) -> str:
    """SHA-256 over the plan body, excluding the digest field itself."""
    body = {k: v for k, v in plan.items() if k != "digest"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def validate_plan(plan: Mapping[str, Any]) -> None:
    """Fail closed on any internally inconsistent plan."""
    rec = plan["reconciliation"]
    accounted = rec["included"] + rec["excluded"] + rec.get("excluded_out_of_scope", 0)
    if accounted != rec["candidates_examined"]:
        raise PlanError("reconciliation does not account for every candidate")
    if plan["operations_by_table"]["supporting_documents"]["update"] != 0:
        raise PlanError("a supporting_documents update was emitted")
    if plan_digest(plan) != plan["digest"]["value"]:
        raise PlanError("plan digest does not match its content")
    update_ops = [o for o in plan["operations"] if o["op"] == "UPDATE"]
    if len(update_ops) != 1:
        raise PlanError("expected exactly one meetings UPDATE operation")
    if update_ops[0]["rows"] != rec["included"]:
        raise PlanError("meetings UPDATE row count does not match included meetings")


def content_hash_disposition() -> dict[str, Any]:
    """Report the analysed-content-hash decision, with no invented value.

    Repository semantics do not define ``supporting_documents.content_hash`` as
    the analysed-text hash, and explicitly refuse to persist one derived from
    current text, so no operation is emitted.
    """
    return {
        "operation_included": False,
        "emitted_operations": 0,
        "invented_value": False,
        "canonical_rule_exists": False,
        "reasoning": (
            "No canonical repository rule equates supporting_documents.content_hash with the "
            "analysed content. scripts/entities/event_normalize_models.py and "
            "event_normalize_snapshot.py both state that populating a stored content hash from "
            "current source text 'would fabricate a provenance claim the database cannot "
            "support'. event_normalize_query.analyzed_text_hash is documented as deliberately "
            "NOT supporting_documents.content_hash. sweep_docs_batch.py computes "
            "sha256(stored text) but uses it only as an in-memory evidence-identity value and "
            "never persists it. No writer anywhere populates the column. The normalizer does "
            "not read the column either: build_candidate_from_row derives the evidence content "
            "hash at runtime, so a NULL column does not block the gate."
        ),
        "alternatives": [
            {
                "id": "A",
                "name": "leave_null",
                "description": "Emit no operation. The column stays NULL; the gate is unaffected.",
                "recommended": True,
            },
            {
                "id": "B",
                "name": "runtime_only",
                "description": (
                    "Keep the analysed-text hash purely at runtime, as today, and record the NULL "
                    "column as lineage debt in the audit (already measured by "
                    "scripts/kg/audit_sections.py)."
                ),
            },
            {
                "id": "C",
                "name": "separate_semantics_decision",
                "description": (
                    "If the column is later meant to hold the analysed-text version, that needs "
                    "its own brief defining the semantics, a writer and a backfill rule. It is "
                    "out of scope here and must not be improvised."
                ),
            },
        ],
    }


def deferred_populations(conn) -> dict[str, Any]:
    """Populations deliberately excluded from this repair."""
    unparented = int(
        fetch_rows(conn, "SELECT COUNT(*) AS n FROM meetings WHERE public_body_id IS NULL")[0]["n"]
    )
    non_dr = int(fetch_rows(
        conn,
        "SELECT COUNT(*) AS n FROM meetings WHERE public_body_id IS NULL AND COALESCE(body,'') <> :b",
        b=BODY_CODE,
    )[0]["n"])
    phoenix_docs = int(fetch_rows(
        conn, "SELECT COUNT(*) AS n FROM supporting_documents WHERE body LIKE 'phoenix-%'"
    )[0]["n"])
    phoenix_docs_j1 = int(fetch_rows(
        conn,
        "SELECT COUNT(*) AS n FROM supporting_documents WHERE body LIKE 'phoenix-%' "
        "AND jurisdiction_id = 1",
    )[0]["n"])
    return {
        "deferred_jurisdiction_defect": {
            "description": (
                "supporting_documents.jurisdiction_id = 1 (Maricopa County) on phoenix-* rows. "
                "Excluded from this repair by instruction; no rows updated."
            ),
            "phoenix_star_documents": phoenix_docs,
            "phoenix_star_documents_jurisdiction_id_1": phoenix_docs_j1,
        },
        "deferred_unparented_meetings": {
            "description": (
                "Non-phoenix-dr unparented meetings. Recorded as subsequent read-only audit work "
                "only; not repaired here."
            ),
            "unparented_total": unparented,
            "unparented_non_phoenix_dr": non_dr,
        },
    }


def _before_state(conn) -> dict[str, int]:
    return {
        "public_bodies_total": int(fetch_rows(conn, "SELECT COUNT(*) AS n FROM public_bodies")[0]["n"]),
        "meetings_total": int(fetch_rows(conn, "SELECT COUNT(*) AS n FROM meetings")[0]["n"]),
        "meetings_null_public_body": int(fetch_rows(
            conn, "SELECT COUNT(*) AS n FROM meetings WHERE public_body_id IS NULL"
        )[0]["n"]),
        "meetings_null_jurisdiction": int(fetch_rows(
            conn, "SELECT COUNT(*) AS n FROM meetings WHERE jurisdiction_id IS NULL"
        )[0]["n"]),
        "supporting_documents_total": int(fetch_rows(
            conn, "SELECT COUNT(*) AS n FROM supporting_documents"
        )[0]["n"]),
    }


def build_plan(
    engine: Engine,
    *,
    integrity_provider: Callable[[Engine], dict] | None = None,
    scope_meeting_ids: Sequence[int] | None = None,
) -> dict[str, Any]:
    """Build the complete, inert repair plan.  Performs no writes."""
    target = assert_development_target(engine)
    with engine.connect() as conn:
        if engine.dialect.name == "postgresql":
            conn.execute(__import__("sqlalchemy").text("SET TRANSACTION READ ONLY"))
        jurisdiction_id, jurisdiction_name = resolve_jurisdiction(conn)
        dispositions = collect_dispositions(conn)
        existing = fetch_rows(
            conn,
            "SELECT id, name, slug, body_code FROM public_bodies "
            "WHERE body_code = :b OR slug = :b",
            b=BODY_CODE,
        )
        if existing:
            raise PlanError(
                f"a public_bodies row already exists for {BODY_CODE!r}: {existing}; "
                "refusing to plan a duplicate registration"
            )
        before = _before_state(conn)
        deferred = deferred_populations(conn)

    included = [d for d in dispositions if d.included]
    excluded = [d for d in dispositions if not d.included]
    # An adjudicated scope keeps a plan to exactly the rows that were classified;
    # same-body meetings the adjudication never saw are reported, never silently
    # parented.
    out_of_scope: list[Any] = []
    if scope_meeting_ids is not None:
        wanted = {int(m) for m in scope_meeting_ids}
        out_of_scope = [d for d in included if d.meeting_id not in wanted]
        included = [d for d in included if d.meeting_id in wanted]
    if not included:
        raise PlanError("no meeting passed the evidence test; nothing to plan")

    ids = [d.meeting_id for d in included]
    expected_pb_null = before["meetings_null_public_body"] - len(included)
    expected_j_null = before["meetings_null_jurisdiction"] - len(included)

    plan: dict[str, Any] = {
        "plan_id": f"kg-step2-{BODY_CODE}-body-registration",
        "plan_version": "1.0",
        "step": 2,
        "brief": "docs/briefs/019-stage-1-event-normalize-gate-remediation.md",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "generator": "scripts/kg/phoenix_dr_repair_plan.py",
        "target": target,
        "safety": {
            "read_only_generation": True,
            "applied": False,
            "mutations_performed": 0,
            "production_touched": False,
            "requires_protected_independent_full_database_backup": True,
            "requires_single_transaction": True,
            "partial_apply_permitted": False,
        },
        "authorization": {
            "approved_step_1_dispositions": [
                "register canonical phoenix-dr body under City of Phoenix",
                "scope to all 13 source-supported phoenix-dr meetings",
                "keep supporting_documents.jurisdiction_id defect out of scope",
                "no invented content hash",
                "remaining unparented meetings deferred",
            ],
            "apply_requires_separate_explicit_approval": True,
        },
        "registry_evidence": list(REGISTRY_EVIDENCE),
        "jurisdiction": {"id": jurisdiction_id, "name": jurisdiction_name, "slug": JURISDICTION_SLUG},
        "reconciliation": {
            "candidates_examined": len(dispositions),
            "included": len(included),
            "excluded": len(excluded),
            "excluded_out_of_scope": len(out_of_scope),
            "included_meeting_ids": ids,
            "excluded_meeting_ids": [d.meeting_id for d in excluded],
            "out_of_scope_meeting_ids": [d.meeting_id for d in out_of_scope],
            "scoped": scope_meeting_ids is not None,
            "accounts_for_every_candidate": (
                len(included) + len(excluded) + len(out_of_scope) == len(dispositions)
            ),
        },
        "adjudication": [_disposition_dict(d) for d in dispositions],
        "operations": [
            {
                "group": "register_public_body",
                "table": "public_bodies",
                "op": "INSERT",
                "rows": 1,
                "values": {
                    "jurisdiction_id": jurisdiction_id,
                    "name": PROPOSED_BODY_NAME,
                    "slug": PROPOSED_BODY_SLUG,
                    "body_code": BODY_CODE,
                    "body_type": PROPOSED_BODY_TYPE,
                    "description": None,
                },
                "identity": f"body_code = {BODY_CODE!r}",
                "id_assignment": (
                    "database-assigned; the id is deliberately not predicted. The meetings update "
                    "resolves the body by body_code inside the same transaction."
                ),
                "precondition": (
                    f"no public_bodies row where body_code = {BODY_CODE!r} or slug = {BODY_CODE!r}"
                ),
                "convention_derived_values": ["name", "slug", "body_type"],
                "requires_confirmation": (
                    "name/slug/body_type follow the existing 'Phoenix <Body>' convention and the "
                    "code registry; confirm before apply."
                ),
            },
            {
                "group": "parent_meetings",
                "table": "meetings",
                "op": "UPDATE",
                "rows": len(ids),
                "set": {
                    "public_body_id": f"(SELECT id FROM public_bodies WHERE body_code = {BODY_CODE!r})",
                    "jurisdiction_id": jurisdiction_id,
                },
                "where": {"id": ids},
                "fingerprints_before": {str(d.meeting_id): d.meeting_fingerprint for d in included},
                "document_fingerprints_before": {
                    str(d.meeting_id): d.document_fingerprint for d in included
                },
                "hard_where_clause": (
                    "The apply step must additionally require public_body_id IS NULL on every "
                    "target row, so a concurrently parented row is never overwritten."
                ),
            },
        ],
        "operations_by_table": {
            "public_bodies": {"insert": 1, "update": 0, "delete": 0},
            "meetings": {"insert": 0, "update": len(ids), "delete": 0},
            "supporting_documents": {"insert": 0, "update": 0, "delete": 0},
        },
        "content_hash_disposition": content_hash_disposition(),
        "before_state": before,
        "expected_postconditions": [
            {
                "id": "body_exactly_one",
                "sql": f"SELECT COUNT(*) FROM public_bodies WHERE body_code = '{BODY_CODE}'",
                "expected": 1,
            },
            {
                "id": "body_jurisdiction",
                "sql": f"SELECT jurisdiction_id FROM public_bodies WHERE body_code = '{BODY_CODE}'",
                "expected": jurisdiction_id,
            },
            {
                "id": "meetings_parented_exactly",
                "sql": (
                    f"SELECT COUNT(*) FROM meetings m JOIN public_bodies pb ON pb.id = m.public_body_id "
                    f"WHERE pb.body_code = '{BODY_CODE}'"
                ),
                "expected": len(ids),
            },
            {
                "id": "no_target_still_unparented",
                "sql": f"SELECT COUNT(*) FROM meetings WHERE body = '{BODY_CODE}' AND public_body_id IS NULL",
                "expected": 0,
            },
            {
                "id": "meetings_null_public_body",
                "sql": "SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL",
                "expected": expected_pb_null,
            },
            {
                "id": "meetings_null_jurisdiction",
                "sql": "SELECT COUNT(*) FROM meetings WHERE jurisdiction_id IS NULL",
                "expected": expected_j_null,
            },
            {
                "id": "public_bodies_total",
                "sql": "SELECT COUNT(*) FROM public_bodies",
                "expected": before["public_bodies_total"] + 1,
            },
            {
                "id": "supporting_documents_unchanged",
                "sql": "SELECT COUNT(*) FROM supporting_documents",
                "expected": before["supporting_documents_total"],
            },
            {
                "id": "no_other_meeting_touched",
                "sql": "SELECT COUNT(*) FROM meetings",
                "expected": before["meetings_total"],
            },
        ],
        "expected_post_state": {
            "public_bodies_total": before["public_bodies_total"] + 1,
            "meetings_null_public_body": expected_pb_null,
            "meetings_null_jurisdiction": expected_j_null,
            "supporting_documents_total": before["supporting_documents_total"],
            "meetings_total": before["meetings_total"],
        },
        "integrity": {
            "pre": integrity_provider(engine) if integrity_provider else None,
            "expectation": (
                "integrity metrics must be identical after repair: this plan adds no relationship, "
                "mention, participant or extraction rows and removes none."
            ),
        },
        "transaction_contract": [
            "Open one transaction. No partial apply is permitted.",
            f"Re-check the precondition inside the transaction: no public_bodies row for {BODY_CODE!r}.",
            "INSERT the public_bodies row; obtain its id from the same transaction.",
            f"UPDATE meetings SET public_body_id=<id>, jurisdiction_id={jurisdiction_id} "
            f"WHERE id IN ({len(ids)} fingerprinted ids) AND public_body_id IS NULL.",
            f"Require rowcount == {len(ids)}; otherwise raise and roll back.",
            "Run every expected_postcondition inside the transaction; any mismatch raises.",
            "Recompute the integrity metrics; a change raises.",
            "Commit only if all checks pass; otherwise roll back and report.",
        ],
        "backup_contract": {
            "required_before_apply": True,
            "description": (
                "A protected, independent, full-database backup of poliscopic_dev must exist and be "
                "verified restorable before any apply. Store it outside the database host; keep it "
                "immutable for the duration of the change."
            ),
            "verification": [
                "Record the backup artefact path and SHA-256.",
                "Restore the backup into a scratch database and compare row counts.",
                "Confirm the scratch restore matches the pre-repair counts below.",
            ],
            "pre_repair_counts_to_match": before,
        },
        "restore_instructions": [
            "STOP the writers (application processes) before any restore.",
            "PRIMARY: roll back the single transaction. The repair is all-or-nothing, so a failed "
            "apply leaves no partial state.",
            "IF the transaction already committed: restore the verified full backup with "
            "pg_restore --clean --if-exists into poliscopic_dev, or psql for a plain-SQL dump.",
            "MINIMAL INVERSE (only when no other change occurred after the repair): "
            f"UPDATE meetings SET public_body_id = NULL, jurisdiction_id = NULL WHERE id IN "
            f"({', '.join(str(i) for i in ids)}); then DELETE FROM public_bodies WHERE "
            f"body_code = '{BODY_CODE}';",
            "Verify the restore against the pre-repair counts recorded above.",
            "Re-run the Step 1 read-only adjudication to confirm the original state.",
        ],
        "excluded_deferred_populations": deferred,
        "unresolved": [
            "Confirm the convention-derived name/slug/body_type values for the new row.",
            "Decide whether the deferred jurisdiction_id defect becomes its own brief.",
            "Decide whether the content_hash column gains defined semantics in a later brief.",
        ],
    }

    plan["digest"] = {
        "algorithm": "sha256",
        "scope": "canonical JSON of the plan body excluding this field",
        "value": plan_digest(plan),
    }
    validate_plan(plan)
    return plan


def _disposition_dict(d) -> dict[str, Any]:
    """Serialise a Disposition dataclass without importing asdict at module load."""
    from dataclasses import asdict

    return asdict(d)
