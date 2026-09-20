#!/usr/bin/env python3
"""``stage2_s2_apply.py`` — fail-closed additive apply (never run by this task).

Adds ``supporting_documents.agenda_item_db_id`` and populates it for the
deterministic links only.  **Implemented and tested, not executed**: no database
or schema is mutated by any task in this workspace series.

Order of decisions, all refusals before any write:

1. **Exact plan.**  Digest-clean, caller digest equal, bound code still matching.
   A plan that is *itself* obsolete is refused — either because a newer plan on
   disk supersedes it, or because it marks itself obsolete.  A current plan is
   allowed to supersede an obsolete prior artifact; the prior's status is
   evidence, not a reason to reject the successor.
2. **Exact target.**  Dialect, host, port, database equal across plan, live
   engine, and backup receipt; development only.
3. **Protected backup.**  A ``0600`` receipt covering every protected table with
   equal values and a matching baseline fingerprint.
4. **Locked recheck inside the transaction.**  Every one of the plan's documents
   and every referenced agenda item is locked and re-read *after* the
   transaction opens, so nothing can change between check and write.
5. **Additive schema.**  The column must be absent.
6. **Postconditions inside the transaction.**  Exact attachment membership,
   every held row still NULL, zero source-key changes, unchanged protected
   counts and integrity.  Anything else rolls the whole transaction back.
"""

from __future__ import annotations

import argparse
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

from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_apply_checks as checks  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402
from scripts.kg import stage2_s2_manifest as manifest_mod  # noqa: E402
from scripts.kg import stage2_s2_verify as verify  # noqa: E402

__all__ = [
    "APPLY_DIALECTS",
    "COLUMN",
    "FK_NAME",
    "REFUSED_EXIT_CODE",
    "ApplyRefused",
    "add_column_ddl",
    "apply_plan",
    "require_absent_column",
    "prior_attempts",
    "record_disposition",
    "require_plan",
    "superseded_by",
    "terminal_receipt",
    "write_receipt",
]

APPLY_DIALECTS = ("postgresql",)
COLUMN = documents.TARGET_COLUMN
FK_NAME = "supporting_documents_agenda_item_db_id_fkey"
RECEIPT_KIND = "kg-stage2-s2-receipt"
REFUSED_EXIT_CODE = 2


class ApplyRefused(RuntimeError):
    """The apply was refused; nothing was written."""


CLI_REFUSALS = (ApplyRefused, tier_module.TierError, checks.CheckRefused)


def add_column_ddl(dialect: str = "postgresql") -> list[str]:
    """The complete additive change.  SQLite cannot express ``ADD CONSTRAINT``."""
    statements = [
        f"ALTER TABLE supporting_documents ADD COLUMN {COLUMN} integer NULL",
        f"CREATE INDEX ix_supporting_documents_{COLUMN} "
        f"ON supporting_documents ({COLUMN})",
    ]
    if dialect == "postgresql":
        statements.append(
            f"ALTER TABLE supporting_documents ADD CONSTRAINT {FK_NAME} "
            f"FOREIGN KEY ({COLUMN}) REFERENCES agenda_items(id) "
            f"ON DELETE SET NULL ON UPDATE CASCADE"
        )
    return statements


def _column_present(connection: Any, dialect: str) -> bool:
    """PostgreSQL uses the catalogue; a failed statement would abort its txn."""
    if dialect == "postgresql":
        return bool(connection.execute(
            text("SELECT COUNT(*) FROM information_schema.columns "
                 "WHERE table_name = 'supporting_documents' AND column_name = :c"),
            {"c": COLUMN},
        ).scalar())
    try:
        connection.execute(text(f"SELECT {COLUMN} FROM supporting_documents LIMIT 0"))
        return True
    except Exception:
        return False


def require_absent_column(connection: Any, dialect: str) -> None:
    """Refuse unless the column is absent, so the step can only widen."""
    if _column_present(connection, dialect):
        raise ApplyRefused(f"supporting_documents.{COLUMN} already exists; refusing to re-add")


def superseded_by(out_dir: str | Path, plan_id: str) -> list[str]:
    """Plan ids on disk that name this plan as the artifact they replace.

    Answered from the derived index when it is fresh, otherwise by a full scan
    that then refreshes the index.  The scan stays authoritative, so the index
    can only shorten the work — never change the answer.
    """
    return manifest_mod.find_successors(out_dir, plan_id)


def require_plan(
    plan_path: str | Path, supplied_digest: str, out_dir: str | Path
) -> dict[str, Any]:
    """Load a digest-clean plan and refuse one that is itself obsolete."""
    plan = artifacts.load_verified(plan_path)
    actual = artifacts.compute_digest(plan)
    if actual != supplied_digest:
        raise ApplyRefused(f"supplied digest {supplied_digest} does not match plan digest {actual}")

    version_problems = verify.verify_code_hashes(plan)
    if version_problems:
        raise ApplyRefused("bound code refused: " + "; ".join(version_problems[:5]))
    identity_problems = verify.verify_identity(plan)
    if identity_problems:
        raise ApplyRefused("plan identity refused: " + "; ".join(identity_problems[:5]))

    # The plan's own obsolescence, never its predecessor's.
    if plan.get("obsolete") is True:
        raise ApplyRefused(f"plan {plan.get('plan_id')} marks itself obsolete")
    successors = superseded_by(out_dir, str(plan.get("plan_id")))
    if successors:
        raise ApplyRefused(
            f"plan {plan.get('plan_id')} has been superseded by {sorted(successors)}"
        )
    return plan


def terminal_receipt(out_dir: str | Path, plan_id: str) -> Path | None:
    """The one artifact that means "this plan has been applied"."""
    path = Path(out_dir) / f"kg-stage2-s2-receipt-{plan_id}.json"
    return path if path.exists() else None


def prior_attempts(out_dir: str | Path, plan_id: str) -> list[dict[str, Any]]:
    """Preimage and failure artifacts left by earlier attempts at this plan."""
    found: list[dict[str, Any]] = []
    for path in sorted(Path(out_dir).glob(f"kg-stage2-s2-receipt-{plan_id}-*.json")):
        try:
            found.append({"name": path.name,
                          "digest": artifacts.recorded_digest(
                              artifacts.load_verified(path))})
        except Exception:
            found.append({"name": path.name, "digest": None})
    return found


def record_disposition(
    out_dir: str | Path, plan_id: str, attempts: Sequence[Mapping[str, Any]], reason: str
) -> tuple[Path, str]:
    """Record an explicit decision about earlier attempts, so a retry is deliberate.

    A retry is never implicit: it names the artifacts it is superseding and why.
    """
    payload = {
        "kind": RECEIPT_KIND, "stage": "disposition", "plan_id": plan_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "superseded_attempts": [dict(a) for a in attempts],
        "reason": reason,
    }
    return write_receipt(out_dir, plan_id, payload, suffix="-disposition", unique=True)


def write_receipt(out_dir: str | Path, plan_id: str, payload: Mapping[str, Any],
                  suffix: str = "", unique: bool = False) -> tuple[Path, str]:
    """Create an immutable receipt.

    Never overwrites: an existing name raises.  With ``unique=True`` the next
    free numbered name is used instead, so a reviewed retry can leave a fresh
    artifact beside the earlier ones rather than deleting them.
    """
    directory = Path(out_dir)
    stem = f"kg-stage2-s2-receipt-{plan_id}{suffix}"
    candidate = directory / f"{stem}.json"
    if unique:
        counter = 2
        while candidate.exists():
            candidate = directory / f"{stem}-{counter}.json"
            counter += 1
    digest = artifacts.write_immutable(candidate, payload)
    return candidate, digest


def _scope(plan: Mapping[str, Any]) -> tuple[list[int], list[int], list[int]]:
    attachments = plan.get("attachments") or []
    holds = plan.get("holds") or []
    document_ids = [int(a["document_id"]) for a in attachments]
    held_ids = [int(h["document_id"]) for h in holds]
    item_ids = sorted({int(a["agenda_item_db_id"]) for a in attachments})
    return document_ids, held_ids, item_ids


def apply_plan(
    engine: Any,
    plan: Mapping[str, Any],
    *,
    supplied_digest: str,
    backup_receipt: str | Path,
    out_dir: str | Path,
    allow_unsupported_dialect: bool = False,
    disposition: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Add the column and populate the deterministic links, atomically."""
    dialect = engine.dialect.name
    if dialect not in APPLY_DIALECTS and not allow_unsupported_dialect:
        raise ApplyRefused(f"unsupported dialect for apply: {dialect!r}")
    if artifacts.compute_digest(plan) != supplied_digest:
        raise ApplyRefused("plan digest changed after loading")
    problems = verify.verify_target_binding(plan, engine)
    if problems:
        raise ApplyRefused("target binding refused: " + "; ".join(problems[:5]))

    plan_id = str(plan.get("plan_id"))
    if terminal_receipt(out_dir, plan_id):
        raise ApplyRefused(f"plan {plan_id} already has a terminal receipt")

    # An earlier attempt leaves a preimage and a failure record.  A retry past
    # them must be an explicit decision, not an accident of re-running.
    attempts = prior_attempts(out_dir, plan_id)
    if attempts:
        covered = {a["digest"] for a in (disposition or {}).get("superseded_attempts", [])}
        outstanding = [a for a in attempts if a["digest"] not in covered]
        if outstanding:
            raise ApplyRefused(
                "prior attempt(s) require an explicit disposition: "
                + ", ".join(sorted(a["name"] for a in outstanding))
            )

    # Mandatory: a protected receipt bound to this plan's baseline and target.
    backup = checks.require_protected_receipt(backup_receipt, plan, engine)

    document_ids, held_ids, item_ids = _scope(plan)
    if not document_ids:
        raise ApplyRefused("plan links nothing")
    all_ids = document_ids + held_ids

    preimage_path, _ = write_receipt(
        out_dir, plan_id,
        {"kind": RECEIPT_KIND, "stage": "preimage", "plan_id": plan_id,
         "plan_digest": supplied_digest, "created_at": datetime.now(timezone.utc).isoformat(),
         "backup_receipt": {"path": backup["path"],
                            "counts_fingerprint": backup["validated"].get("counts_fingerprint")},
         "planned": {"documents": len(all_ids), "linked": len(document_ids),
                     "held": len(held_ids), "target_items": len(item_ids)}},
        suffix="-preimage", unique=True,
    )

    expected_counts = {"expected_linked": len(document_ids),
                       "expected_unlinked": len(held_ids),
                       "expected_total": len(all_ids)}

    try:
        with engine.begin() as connection:
            require_absent_column(connection, dialect)

            # Lock, then read.  Everything below is inside this transaction, so
            # no value can change between the check and the write.
            checks.lock_scope(connection, dialect, all_ids, item_ids)

            identities = checks.read_document_identities(connection, all_ids)
            failures = checks.verify_document_identities(plan, identities)
            failures += checks.verify_agenda_items(
                plan, checks.read_agenda_items(connection, item_ids))
            before_keys = checks.read_source_keys(connection, all_ids)
            before_protected = checks.protected_counts(connection)
            before_integrity = checks.read_integrity(connection, dialect)
            if failures:
                raise ApplyRefused("preconditions failed: " + "; ".join(failures[:5]))

            for statement in add_column_ddl(dialect):
                connection.execute(text(statement))

            groups: dict[int, list[int]] = {}
            for entry in plan["attachments"]:
                groups.setdefault(int(entry["agenda_item_db_id"]), []).append(
                    int(entry["document_id"]))
            linked = 0
            for agenda_item_id, members in groups.items():
                result = connection.execute(
                    text(f"UPDATE supporting_documents SET {COLUMN} = :target WHERE id IN :ids")
                    .bindparams(bindparam("ids", expanding=True)),
                    {"target": agenda_item_id, "ids": members},
                )
                linked += result.rowcount

            counts = checks.row_counts(connection)
            failures = checks.verify_attachment_membership(connection, plan)
            failures += checks.verify_held_unlinked(connection, held_ids)
            failures += checks.verify_source_keys(
                before_keys, checks.read_source_keys(connection, all_ids))
            failures += checks.verify_protected_state(
                before_protected, checks.protected_counts(connection), before_integrity,
                checks.read_integrity(connection, dialect))
            for key, value in expected_counts.items():
                want = value
                got = {"expected_linked": counts["linked"], "expected_unlinked": counts["unlinked"],
                       "expected_total": counts["total"]}[key]
                if got != want:
                    failures.append(f"{key}: {got} != {want}")
            if failures:
                raise ApplyRefused("postconditions failed: " + "; ".join(failures[:5]))
    except Exception as exc:
        try:
            write_receipt(out_dir, plan_id,
                          {"kind": RECEIPT_KIND, "stage": "failure", "plan_id": plan_id,
                           "plan_digest": supplied_digest, "error": str(exc),
                           "created_at": datetime.now(timezone.utc).isoformat()},
                          suffix="-failure", unique=True)
        except artifacts.ArtifactCollision:
            pass
        raise

    receipt = {
        "kind": RECEIPT_KIND, "stage": "terminal", "plan_id": plan_id,
        "plan_digest": supplied_digest,
        "applied_at": datetime.now(timezone.utc).isoformat(),
        "engine_target": documents.target_section(engine),
        "backup_receipt": {"path": backup["path"],
                           "counts_fingerprint": backup["validated"].get("counts_fingerprint")},
        "operations": {"linked": linked, "target_items": len(groups)},
        "postconditions": {"linked": counts["linked"], "unlinked": counts["unlinked"],
                           "total": counts["total"], "passed": True},
        "preimage_artifact": str(preimage_path),
    }
    path, digest = write_receipt(out_dir, plan_id, receipt)
    receipt["receipt_path"] = str(path)
    receipt[artifacts.DIGEST_FIELD] = digest
    return receipt


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--backup-receipt", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)

    try:
        from scripts.db import config
        from db.core import get_engine

        tier_module.validate_tier_target(config.DB_TIER, config.DB_TARGET)
        if config.DB_TIER != tier_module.DEVELOPMENT:
            raise ApplyRefused(f"apply is development-only, got tier {config.DB_TIER!r}")
        plan = require_plan(args.plan, args.digest, args.out_dir)
        receipt = apply_plan(get_engine(), plan, supplied_digest=args.digest,
                             backup_receipt=args.backup_receipt, out_dir=args.out_dir)
    except CLI_REFUSALS as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return REFUSED_EXIT_CODE
    print(json.dumps(receipt, indent=2, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
