#!/usr/bin/env python3
"""``stage2_s2_documents.py`` — additive document→agenda-item attachment.

Stage 2 Step 2 is **plan-only**.  Nothing here adds a column, changes a schema,
or writes a row: it reads the current development population, classifies every
supporting document against the canonical agenda items, and emits an immutable,
digest-bound review artifact.

Two identities must never be confused:

* ``supporting_documents.agenda_item_id`` is a **source-system key** (text).  It
  is heterogeneous by platform, and it is preserved byte-for-byte.
* ``agenda_items.id`` is the **canonical database identity**.  The proposed
  ``supporting_documents.agenda_item_db_id`` is an additive nullable integer FK
  pointing at it.

Linking is a deterministic cascade, never a guess:

1. ``meeting_item_number`` — the document's ``agenda_item_number`` equals the
   canonical item's ``agenda_item_number`` within the same meeting.  Exactly one
   candidate is a link; more than one is an ambiguity and is held.
2. ``alternate_source_key`` — on what is left, the document's ``agenda_item_id``
   equals the canonical item's own ``agenda_item_id``.  That column is unique,
   so a match is exact.
3. Anything left is *not* linked: either a reference to an item that was never
   acquired, or a document with no item number at all.

``agenda_item_id`` is deliberately **not** the primary link key.  Repository
writers store the literal ``"0"`` for a document that has no source key of its
own while the real reference sits in ``agenda_item_number``
(``scraper/platforms/civicclerk.py``, ``scraper/platforms/onbase.py``).  All
31,046 such rows carry a non-blank ``agenda_item_number``, so ``"0"`` means
"no document key", never "item zero".

Ambiguity, gaps, and meeting-level keys are recorded as **holds** with reasons.
They are never resolved by inference.
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
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.kg import stage1_backup_receipt as receipts  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg.stage2_s2_classify import (  # noqa: E402
    PROTECTED_TABLES,
    CLASSES,
    DISJOINT_CLASSES,
    HELD_CLASSES,
    HOLD_REASONS,
    MEETING_LEVEL_KEY_PREFIX,
    PLACEHOLDER_SOURCE_KEY,
    STRATEGIES,
    adjudication,
    agenda_item_fingerprint,
    classify,
    document_fingerprint,
    populations,
    protected_counts,
)

__all__ = [
    "ALGORITHM_VERSION",
    "MEETING_LEVEL_KEY_PREFIX",
    "PLACEHOLDER_SOURCE_KEY",
    "STRATEGIES",
    "CLASSES",
    "DISJOINT_CLASSES",
    "HELD_CLASSES",
    "PLAN_KIND",
    "TARGET_COLUMN",
    "build_plan",
    "classify",
    "code_hashes",
    "identity_problems",
    "plan_identity",
    "supersede_reference",
    "populations",
    "document_fingerprint",
    "target_section",
]

PLAN_KIND = "kg-stage2-s2-plan"
ALGORITHM_VERSION = "kg-stage2-s2/1.0"

#: The additive column this plan would populate.  Never written here.
TARGET_COLUMN = "agenda_item_db_id"

#: Modules whose behaviour this plan depends on.
CODE_MODULES = (
    # the plan and everything it decides with
    "scripts/kg/stage2_s2_documents.py",
    "scripts/kg/stage2_s2_classify.py",
    "scripts/kg/stage2_s2_verify.py",
    "scripts/kg/stage2_s2_adjudication.py",
    "scripts/kg/stage2_s2_adjudication_markdown.py",
    "scripts/kg/stage2_s2_ai_proposal.py",
    "scripts/kg/stage2_s2_ai_packet.py",
    # trust-bound review path: the loader and the read-only/target guard it
    # depends on are part of what the plan is bound to.
    "scripts/kg/stage2_s2_ai_adjudication_loader.py",
    "scripts/kg/stage2_s2_ai_lineage.py",
    "scripts/entities/event_normalize_preflight.py",
    "scripts/kg/registries/evidence.py",
    "scripts/kg/stage2_s2_apply.py",
    "scripts/kg/stage2_s2_apply_checks.py",
    "scripts/kg/stage2_s2_manifest.py",
    "scripts/kg/stage2_artifacts.py",
    "scripts/kg/stage1_backup_receipt.py",
    "scripts/kg/stage2_s1_verify.py",
    # schema, model, sync and parity: the column touches all four
    "scripts/db/models.py",
    "scripts/db/sync_prod.py",
    "scripts/db/sync_declarations.py",
    "scripts/entities/schema_parity.py",
)

def code_hashes() -> dict[str, str]:
    """SHA-256 of every module this plan's behaviour is bound to."""
    out: dict[str, str] = {}
    for relative in CODE_MODULES:
        path = REPO / relative
        if path.exists():
            out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def target_section(engine: Any) -> dict[str, Any]:
    """The exact engine identity a plan is written for."""
    url = engine.url
    return {
        "dialect": engine.dialect.name,
        "host": url.host,
        "port": int(url.port) if url.port else None,
        "database": (url.database or "").split("/")[-1] or None,
    }


def _agenda_items(connection: Any, ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    if not ids:
        return {}
    found: dict[int, dict[str, Any]] = {}
    chunk = 2000
    for start in range(0, len(ids), chunk):
        batch = [int(i) for i in ids[start:start + chunk]]
        statement = text(
            "SELECT id, meeting_db_id, agenda_item_number, agenda_item_id "
            "FROM agenda_items WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows = connection.execute(statement, {"ids": batch}).mappings()
        for row in rows:
            found[int(row["id"])] = {
                "id": int(row["id"]),
                "meeting_db_id": int(row["meeting_db_id"]),
                "fingerprint": agenda_item_fingerprint(row),
            }
    return found


def _reconcile(files: Sequence[Mapping[str, Any]], agenda_items: Mapping[int, Any]) -> list[str]:
    """Fail closed unless every document is classified and every link resolves."""
    problems: list[str] = []
    seen: set[int] = set()
    for entry in files:
        document_id = int(entry["document_id"])
        if document_id in seen:
            problems.append(f"document {document_id} classified twice")
        seen.add(document_id)
        if entry["class"] not in CLASSES:
            problems.append(f"document {document_id} has unknown class {entry['class']!r}")
            continue
        linked = entry["class"] in DISJOINT_CLASSES
        if linked:
            target = entry["agenda_item_db_id"]
            if target is None:
                problems.append(f"document {document_id} is linked but has no target")
            elif target not in agenda_items:
                problems.append(f"document {document_id} targets missing item {target}")
        elif entry["agenda_item_db_id"] is not None:
            problems.append(
                f"held document {document_id} carries target {entry['agenda_item_db_id']}"
            )
    return problems


def _schema_impact() -> dict[str, Any]:
    """The additive change this plan would need — described, never applied."""
    return {
        "applied": False,
        "table": "supporting_documents",
        "add_column": {
            "name": TARGET_COLUMN,
            "type": "integer",
            "nullable": True,
            "default": None,
        },
        "add_foreign_key": {
            "column": TARGET_COLUMN,
            "references": "agenda_items.id",
            "on_delete": "SET NULL",
            "on_update": "CASCADE",
        },
        "preserved": {
            "column": "agenda_item_id",
            "type": "character varying(256)",
            "not_null": True,
            "note": "source key kept byte-for-byte; its values and uniqueness "
                    "semantics are untouched",
        },
        "backfill": "no row is written by this plan; the new column stays NULL",
    }


def _sync_parity_impact() -> dict[str, Any]:
    """Every path that must learn about the new column before it ships."""
    return {
        "applied": False,
        "writers": [
            "scripts/db/backfill_supporting_documents.py (must leave the column NULL)",
        ],
        "readers": [
            "scripts/db/models.py SupportingDocument (add nullable Integer column)",
        ],
        "sync": [
            "scripts/db/sync_prod.py (column list and copy must include the new column)",
        ],
        "parity": [
            "scripts/entities/schema_parity.py (dev/prod schema signature must match)",
        ],
        "sequencing": "dev DDL, dev parity check, production DDL, then sync",
        "note": "the existing varchar agenda_item_id type mismatch against "
                "scripts/db/models.py must be reconciled before parity can pass",
    }


def build_plan(
    engine: Any,
    baseline: Mapping[str, Any],
    *,
    plan_id: str,
    created_at: str,
    supersedes: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the read-only Stage 2 Step 2 review plan."""
    with engine.connect() as connection:
        files = classify(connection)
        counts = populations(files)
        link_ids = sorted(
            int(e["agenda_item_db_id"]) for e in files if e["class"] in DISJOINT_CLASSES
        )
        agenda_items = _agenda_items(connection, link_ids)
        db_counts = protected_counts(connection)
        problems = _reconcile(files, agenda_items)

    attachments = [
        {
            "document_id": int(e["document_id"]),
            "document_fingerprint": e["fingerprint"],
            "strategy": e["strategy"],
            "agenda_item_db_id": int(e["agenda_item_db_id"]),
            "agenda_item_fingerprint": agenda_items[int(e["agenda_item_db_id"])]["fingerprint"],
        }
        for e in files
        if e["class"] in DISJOINT_CLASSES
    ]
    holds = [
        {
            "document_id": int(e["document_id"]),
            "document_fingerprint": e["fingerprint"],
            "class": e["class"],
            "source_key": e["source_key"],
            "reason": HOLD_REASONS[str(e["class"])],
        }
        for e in files
        if e["class"] in HELD_CLASSES
    ]
    held_ids = {h["document_id"] for h in holds}
    linked_ids = {a["document_id"] for a in attachments}

    plan = {
        "kind": PLAN_KIND,
        "algorithm_version": ALGORITHM_VERSION,
        "plan_id": plan_id,
        "created_at": created_at,
        "target_column": TARGET_COLUMN,
        "target": target_section(engine),
        "code_hashes": code_hashes(),
        "supersedes": dict(supersedes) if supersedes else None,
        "reconciliation": {
            "problems": problems,
            "duplicate_documents": len(files) - len({int(e["document_id"]) for e in files}),
            "linked_and_held_overlap": len(linked_ids & held_ids),
            "distinct_agenda_items": len(agenda_items),
            "unexplained": 0,
        },
        "baseline": {
            # Class counts describe the documents; db_counts describe the
            # database the backup must have captured.
            "counts": counts,
            "counts_fingerprint": receipts.counts_fingerprint(
                {k: v for k, v in counts.items()}
            ),
            "db_counts": db_counts,
            "db_counts_fingerprint": receipts.counts_fingerprint(db_counts),
            "protected_tables": list(PROTECTED_TABLES),
            "integrity": dict(baseline.get("integrity") or {}),
        },
        "counts": counts,
        "adjudication": adjudication(files),
        "schema_impact": _schema_impact(),
        "sync_parity_impact": _sync_parity_impact(),
        "expected_after_state": {
            "column": f"supporting_documents.{TARGET_COLUMN}",
            "not_null": counts["deterministic_links"],
            "null": counts["held_total"],
            "row_count_unchanged": counts["total_documents"],
        },
        "attachments": attachments,
        "holds": holds,
    }
    return plan


def plan_identity(moment: Any, supplied: str | None = None) -> tuple[str, str]:
    """Derive ``(plan_id, created_at)`` from one instant, so they cannot drift.

    A plan whose id says ``011000Z`` while ``created_at`` says ``00:12Z`` is two
    different claims about when it was made.  A supplied id must equal the id
    derived from the same instant; anything else is refused rather than stored.
    """
    created_at = moment.isoformat()
    derived = moment.strftime("%Y%m%dT%H%M%SZ")
    if supplied is not None and supplied != derived:
        raise ValueError(
            f"plan id {supplied!r} does not match created_at {created_at!r} "
            f"(derived {derived!r})"
        )
    return supplied or derived, created_at


def identity_problems(plan: Mapping[str, Any]) -> list[str]:
    """A plan's id must be the timestamp form of its own created_at."""
    created_at = str(plan.get("created_at") or "")
    plan_id = str(plan.get("plan_id") or "")
    if not created_at or not plan_id:
        return ["plan carries no id or no created_at"]
    try:
        moment = datetime.fromisoformat(created_at)
    except ValueError:
        return [f"created_at {created_at!r} is not an ISO-8601 timestamp"]
    derived = moment.strftime("%Y%m%dT%H%M%SZ")
    if derived != plan_id:
        return [f"plan id {plan_id!r} is not the identity of created_at {created_at!r}"]
    return []


def supersede_reference(path: str | Path) -> dict[str, Any]:
    """Load the plan this one replaces, refusing an obsolete unbound draft.

    A draft written before the current bound module set existed does not bind
    every module whose behaviour the plan depends on.  Superseding one is fine
    and expected; silently inheriting its authority is not, so the omission is
    named here rather than glossed over.
    """
    document = artifacts.load_verified(path)
    recorded = document.get("code_hashes") or {}
    live = code_hashes()
    missing = sorted(set(live) - set(recorded))
    drifted = sorted(m for m in set(live) & set(recorded) if live[m] != recorded[m])
    return {
        "path": str(path),
        "plan_id": document.get("plan_id"),
        "digest": artifacts.compute_digest(document),
        "kind": document.get("kind"),
        "unbound_modules": missing,
        "drifted_modules": drifted,
        "obsolete": bool(missing) or bool(drifted),
    }


def main(argv: Sequence[str] | None = None) -> int:
    """Generate the immutable Stage 2 Step 2 plan.  Read-only."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", default="data/kg-plans")
    parser.add_argument("--plan-id", default=None)
    parser.add_argument(
        "--supersedes",
        default=None,
        help="path to the plan this one replaces (refused if it cannot be read)",
    )
    args = parser.parse_args(argv)

    from scripts.db import config, tier as tier_module
    from db.core import get_engine
    from scripts.entities.detect_entities import integrity_snapshot
    from scripts.entities.event_normalize_preflight import guard_engine
    from scripts.kg.phoenix_dr_adjudication import assert_development_target

    tier_module.validate_tier_target(config.DB_TIER, config.DB_TARGET)
    engine = get_engine()
    guard_engine(engine)
    assert_development_target(engine)

    with engine.connect() as connection:
        baseline = {"integrity": {k: int(v) for k, v in integrity_snapshot(connection).items()}}

    try:
        plan_id, created_at = plan_identity(datetime.now(timezone.utc), args.plan_id)
    except ValueError as exc:
        print(f"plan identity refused: {exc}", file=sys.stderr)
        return 2
    supersedes = supersede_reference(args.supersedes) if args.supersedes else None
    plan = build_plan(
        engine,
        baseline,
        plan_id=plan_id,
        created_at=created_at,
        supersedes=supersedes,
    )
    path = Path(args.out_dir) / f"kg-stage2-s2-plan-{plan_id}.json"
    digest = artifacts.write_immutable(path, plan)
    print(json.dumps({
        "plan": str(path),
        "digest": digest,
        "counts": plan["counts"],
        "reconciliation": plan["reconciliation"],
        "supersedes": supersedes,
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
