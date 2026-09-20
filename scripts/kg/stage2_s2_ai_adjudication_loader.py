#!/usr/bin/env python3
"""``stage2_s2_ai_adjudication_loader.py`` — the authoritative review entry point.

``stage2_s2_ai_proposal.validate_current`` is a *pure* function: it compares a
proposal against whatever current-state context it is handed.  That makes it
testable, but it also means a caller could hand it anything — a claimed
``source_supported=False``, a candidate list that omits the real meeting, a
fingerprint it invented.  A reviewer must never be able to do that.

This module is what a human review surface calls.  It takes a proposal path and
an engine, and **nothing else**:

* the engine is bound to the verified development target before anything is read
  — classified, compared field-by-field against the configured target *and* the
  current plan's recorded target, and refused on any mismatch;
* every authoritative read (document row, complete same-meeting candidate set,
  target candidate row, link column and link state) happens inside **one
  coherent snapshot**, so the picture cannot shift between statements;
* the document, unlinked-state and candidate fingerprints are computed here,
  from the rows that same snapshot returned.

There is no parameter through which a caller can supply or override
``source_supported``, candidate membership, or any current fingerprint.

Fail-closed, in order: development tier; engine classified development/local and
matched against config and the plan; a repeatable-read read-only snapshot opened
(failure to open it is a refusal, not a fallback); digest-clean artifact; the
document must exist; for a link, the candidate must be in the meeting's set and
its row must exist and be in the **same meeting as the document**; the target
meeting must equal the document meeting.  Any problem raises
:class:`ProposalRefused` and returns nothing.
"""

from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from scripts.db import config, tier as tier_module  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
    guard_engine,
)
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_proposal as ai  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402
from scripts.kg import stage2_s2_classify as classify_mod  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402

__all__ = [
    "LINK_COLUMN",
    "TARGET_FIELDS",
    "TARGET_FIELDS",
    "current_plan_target",
    "load_for_adjudication",
]

LINK_COLUMN = documents.TARGET_COLUMN

#: The identity fields the engine, the configuration and the plan must agree on.
TARGET_FIELDS = ("dialect", "host", "port", "database")

#: PostgreSQL gets a genuine repeatable-read, read-only transaction because it
#: is the only dialect that can serve one.  Other dialects (the isolated SQLite
#: fixture) get one connection with one transaction, which is the equivalent
#: coherent snapshot there.
_SNAPSHOT_DIALECTS = ("postgresql",)

_DOCUMENT_SQL = text(
    "SELECT id, meeting_db_id, agenda_item_id, agenda_item_number, document_url, updated_at "
    "FROM supporting_documents WHERE id = :document_id"
)
_CANDIDATE_SQL = text(
    "SELECT id AS agenda_item_db_id, meeting_db_id, agenda_item_number, agenda_item_id "
    "FROM agenda_items WHERE meeting_db_id = :meeting_db_id ORDER BY sort_order NULLS LAST, id"
)
_TARGET_SQL = text(
    "SELECT id AS agenda_item_db_id, meeting_db_id, agenda_item_number, agenda_item_id "
    "FROM agenda_items WHERE id = :agenda_item_db_id"
)


def current_plan_target(directory: str | Path | None = None) -> dict[str, Any]:
    """The target recorded by the verified current plan head.

    The head is chosen by supersession lineage, never by modification time: a
    file can be touched without being current, and an mtime rule would then bind
    the engine to a plan that was superseded an hour ago.
    """
    try:
        _path, plan, _digest = lineage.current_plan(directory)
    except lineage.LineageRefused as exc:
        raise ai.ProposalRefused(f"no current Stage 2 plan to bind against: {exc}") from exc
    target = plan.get("target") or {}
    return {field: target.get(field) for field in TARGET_FIELDS}


def _require_development(engine: Any) -> None:
    """Development tier, and an engine that refuses every mutating statement."""
    tier_module.validate_tier_target(config.DB_TIER, config.DB_TARGET)
    if config.DB_TIER != tier_module.DEVELOPMENT:
        raise ai.ProposalRefused(
            f"adjudication is development-only, got tier {config.DB_TIER!r}")
    guard_engine(engine)


def _same_field(left: Any, right: Any) -> bool:
    """Ports compare numerically; everything else case-insensitively."""
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, int) or isinstance(right, int):
        try:
            return int(left) == int(right)
        except (TypeError, ValueError):
            return False
    return str(left).strip().lower() == str(right).strip().lower()


def bind_target(engine: Any, plan_target: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Bind the engine to the verified development target, or refuse.

    Three identities must agree: the engine, the configured target, and the
    current plan's recorded target.  The engine is classified first, so a
    production-like or unclassifiable target is refused before any connection.
    """
    record = assert_read_only_target(engine)
    plan_target = current_plan_target() if plan_target is None else plan_target

    for field in TARGET_FIELDS:
        configured = getattr(config.DB_TARGET, field, None)
        if not _same_field(record.get(field), configured):
            raise ai.ProposalRefused(
                f"engine {field}={record.get(field)!r} does not match configured "
                f"target {field}={configured!r}")
        if not _same_field(record.get(field), plan_target.get(field)):
            raise ai.ProposalRefused(
                f"engine {field}={record.get(field)!r} does not match plan target "
                f"{field}={plan_target.get(field)!r}")
    return record


@contextmanager
def _snapshot(engine: Any) -> Iterator[Any]:
    """One coherent snapshot for every authoritative read.

    PostgreSQL gets ``REPEATABLE READ`` plus ``SET TRANSACTION READ ONLY``.  If
    that cannot be established the loader refuses — a review that silently fell
    back to read-committed could see the document, the candidate set and the
    target row from three different moments.
    """
    dialect = engine.dialect.name
    connection = None
    try:
        if dialect in _SNAPSHOT_DIALECTS:
            connection = engine.connect().execution_options(
                isolation_level="REPEATABLE READ")
            connection.execute(text("SET TRANSACTION READ ONLY"))
        else:
            connection = engine.connect()
            connection.begin()
    except Exception as exc:
        if connection is not None:
            connection.close()
        raise ai.ProposalRefused(
            f"could not open a repeatable-read read-only snapshot: {exc}") from exc
    try:
        yield connection
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()


def _link_state(connection: Any, dialect: str, document_id: int) -> tuple[bool, Any]:
    """Whether the additive link column exists, and the document's link value."""
    if dialect == "postgresql":
        present = bool(connection.execute(
            text("SELECT COUNT(*) FROM information_schema.columns "
                 "WHERE table_name = 'supporting_documents' AND column_name = :c"),
            {"c": LINK_COLUMN},
        ).scalar())
    else:
        try:
            connection.execute(text(f"SELECT {LINK_COLUMN} FROM supporting_documents LIMIT 0"))
            present = True
        except Exception:
            present = False
    if not present:
        return False, None
    return True, connection.execute(
        text(f"SELECT {LINK_COLUMN} FROM supporting_documents WHERE id = :i"),
        {"i": document_id}).scalar()


def load_for_adjudication(proposal_path: str | Path, engine: Any) -> dict[str, Any]:
    """Load a proposal and return it only if it is current against the database.

    The human-review entry point.  It accepts no current-state context: a caller
    cannot claim the document is unlinked, cannot supply a candidate set, and
    cannot supply a fingerprint.
    """
    _require_development(engine)
    target_record = bind_target(engine)
    proposal = artifacts.load_verified(proposal_path)

    # Lineage first: a proposal that is not a member of the single current
    # verified plan-and-aggregate chain is not reviewable, whatever it says.
    try:
        lineage_record = lineage.verify_lineage(proposal_path, proposal)
    except lineage.LineageRefused as exc:
        raise ai.ProposalRefused(f"lineage refused: {exc}") from exc

    document_id = proposal.get("document_id")
    if not isinstance(document_id, int):
        raise ai.ProposalRefused("proposal does not name exactly one document")

    dialect = engine.dialect.name
    with _snapshot(engine) as connection:
        row = connection.execute(_DOCUMENT_SQL, {"document_id": document_id}).mappings().first()
        if row is None:
            raise ai.ProposalRefused(f"supporting document {document_id} does not exist")
        meeting_db_id = int(row["meeting_db_id"])

        candidates = list(connection.execute(
            _CANDIDATE_SQL, {"meeting_db_id": meeting_db_id}).mappings())
        candidate_ids = sorted({int(c["agenda_item_db_id"]) for c in candidates})

        recommendation = proposal.get("model_recommendation")
        links = proposal.get("candidate_links") or []
        target_row = None
        if recommendation == "link":
            if not links:
                raise ai.ProposalRefused("a link recommendation carries no candidate")
            wanted = int(links[0]["agenda_item_db_id"])
            if wanted not in candidate_ids:
                raise ai.ProposalRefused(
                    f"candidate {wanted} is not in meeting {meeting_db_id}'s agenda-item set")
            target_row = connection.execute(
                _TARGET_SQL, {"agenda_item_db_id": wanted}).mappings().first()
            if target_row is None:
                raise ai.ProposalRefused(f"candidate agenda item {wanted} does not exist")
            if int(target_row["meeting_db_id"]) != meeting_db_id:
                raise ai.ProposalRefused(
                    f"candidate {wanted} belongs to meeting "
                    f"{int(target_row['meeting_db_id'])}, not the document's meeting "
                    f"{meeting_db_id}")

        has_link_column, current_link = _link_state(connection, dialect, document_id)
        source_supported = current_link is not None

        # Fingerprints are computed from the rows this same snapshot returned.
        document_fp = classify_mod.document_fingerprint(row)
        unlinked_fp = ai.unlinked_state_fingerprint(document_id, document_fp)
        candidate_fp = ai.candidate_fingerprint(target_row) if target_row is not None else None

    problems = ai.validate_current(
        proposal,
        current_document_fingerprint=document_fp,
        current_unlinked_state_fingerprint=unlinked_fp,
        candidate_ids=candidate_ids,
        source_supported=source_supported,
        current_candidate_fingerprint=candidate_fp,
    )
    if problems:
        raise ai.ProposalRefused("; ".join(problems[:5]))

    return {
        "proposal": proposal,
        "target_identity": target_record,
        "lineage": lineage_record,
        "as_of": datetime.now(timezone.utc).isoformat(),
        "snapshot": {
            "isolation": ("REPEATABLE READ + READ ONLY"
                          if dialect in _SNAPSHOT_DIALECTS else "single transaction"),
            "dialect": dialect,
        },
        "document": {"document_id": document_id, "meeting_db_id": meeting_db_id,
                     "document_fingerprint": document_fp,
                     "unlinked_state_fingerprint": unlinked_fp,
                     "link_column_present": has_link_column,
                     "source_supported": source_supported},
        "candidate_set": {"count": len(candidate_ids), "ids": candidate_ids},
        "target": ({"agenda_item_db_id": int(target_row["agenda_item_db_id"]),
                    "meeting_db_id": int(target_row["meeting_db_id"]),
                    "agenda_item_fingerprint": candidate_fp} if target_row is not None else None),
    }
