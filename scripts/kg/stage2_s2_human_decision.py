#!/usr/bin/env python3
"""``stage2_s2_human_decision.py`` — immutable records of human adjudication.

A proposal must arrive **undecided**: ``validate_proposal`` refuses any proposal
whose ``decision``/``decided_by``/``decided_at`` is not null.  The human decision
therefore cannot live on the proposal.  It lives here instead, as its own
write-once artifact that binds the proposal it decided.

A decision is only ever built from the authoritative engine-bound loader.  Every
fact it records — the document fingerprint, the unlinked-state fingerprint, the
candidate identity and fingerprint, the plan and aggregate lineage — is copied
from that loader's output, so a decision cannot be assembled from a caller's
claims about current database state.

Nothing here promotes or applies anything.  ``promoted`` and ``applied`` are
always false, and no function writes to a database.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import identity_keys  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "APPROVE",
    "DECISION_KINDS",
    "DECISION_KIND",
    "DECISION_VERSION",
    "DecisionRefused",
    "aggregate_chain",
    "bind_aggregate",
    "build_decision",
    "decision_id_for",
    "validate_decision",
]

DEFAULT_PLAN_DIR = REPO / "data" / "kg-plans"

DECISION_KIND = "kg-stage2-s2-human-decision"
DECISION_VERSION = "kg-stage2-s2-human-decision/1.0"
APPROVE = "approve"
DECISION_KINDS = ("approve", "reject", "alternate", "meeting_only")

#: A decision may not be promoted or applied by the act of recording it.
NOT_ACTIONS = ("promoted", "applied")


class DecisionRefused(RuntimeError):
    """The decision could not be recorded as stated; nothing is written."""


def decision_id_for(
    moment: datetime | str, document_id: int, prefix: str = "kg-s2-dec"
) -> str:
    """A coherent, sortable decision id: ``<prefix>-<stamp>-doc<id>``.

    *moment* may be a ``datetime`` or an ISO-8601 string, because a decision
    request supplies the human's timestamp as text.  A naive string is taken as
    UTC; an offset-aware one is converted.
    """
    if isinstance(moment, str):
        try:
            moment = datetime.fromisoformat(moment)
        except ValueError as exc:
            raise DecisionRefused(f"decided_at {moment!r} is not an ISO-8601 timestamp") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    stamp = moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{prefix}-{stamp}-doc{int(document_id)}"


def aggregate_chain(
    directory: str | Path | None = None, plan_digest: str | None = None
) -> list[dict[str, str]]:
    """The current aggregate and every aggregate it supersedes, newest first.

    An aggregate is a **derived index** over proposals, not the authority.  A
    decision binds the plan (the stable contract) and the proposal; the aggregate
    in force when it was adjudicated is recorded for traceability and verified
    against this chain, because the successor aggregate is the one that indexes
    the decision and therefore cannot be bound by it.

    This lives here rather than in the lineage module on purpose: the lineage
    module is part of the plan's ``code_hashes``, and adding to it would move the
    plan digest and invalidate every decision that binds the current plan.
    """
    from scripts.kg import stage2_s2_ai_lineage as lineage

    path, document, digest = lineage.current_aggregate(directory, plan_digest)
    chain = [{"path": path.name, "digest": digest}]
    seen = {digest}
    while True:
        reference = document.get("supersedes") or {}
        target = reference.get("digest")
        if not target or target in seen:
            break
        seen.add(target)
        record = {"path": str(reference.get("path") or ""), "digest": str(target)}
        chain.append(record)
        candidate = Path(directory or DEFAULT_PLAN_DIR) / record["path"]
        if not record["path"] or not candidate.exists():
            break
        try:
            document, _digest = artifacts.load_verified(candidate)
        except Exception:
            break
    return chain


def _lineage_of(loaded: Mapping[str, Any]) -> dict[str, Any]:
    lineage = loaded.get("lineage") or {}
    return {
        "plan": dict(lineage.get("plan") or {}),
        "aggregate": dict(lineage.get("aggregate") or {}),
        "proposal": dict(lineage.get("proposal") or {}),
    }


def build_decision(
    *,
    loaded: Mapping[str, Any],
    role: str,
    description: str,
    human_stated_item: str,
    adjudicator: str,
    decision_id: str,
    decided_at: str,
    decision: str = APPROVE,
) -> dict[str, Any]:
    """Build one decision from the loader's output plus the human's own words.

    *role* and *description* are the human's description of what the source
    document is; they are preserved verbatim as evidence.
    """
    if decision not in DECISION_KINDS:
        raise DecisionRefused(f"decision {decision!r} is not one of {DECISION_KINDS}")
    for field, value in (("adjudicator", adjudicator), ("decision_id", decision_id),
                         ("decided_at", decided_at)):
        if not str(value or "").strip():
            raise DecisionRefused(f"{field} is required on a human decision")
    for field, value in (("role", role), ("description", description),
                         ("human_stated_item", human_stated_item)):
        if not str(value or "").strip():
            raise DecisionRefused(f"{field} is required; the human's own words are the evidence")

    proposal = loaded.get("proposal") or {}
    document = loaded.get("document") or {}
    target = loaded.get("target")
    if decision == APPROVE and target is None:
        raise DecisionRefused("cannot approve a link without a bound candidate")

    document_id = int(document.get("document_id"))
    links = proposal.get("candidate_links") or []
    bound = links[0] if links else {}
    stored_number = str(bound.get("agenda_item_number") or "")

    payload: dict[str, Any] = {
        "kind": DECISION_KIND,
        "version": DECISION_VERSION,
        "decision_id": decision_id,
        "decided_at": decided_at,
        "adjudicator": adjudicator,
        "decision": decision,
        "assertion_class": "human",
        "identity_key": identity_keys.adjudication_identity(
            adjudicator=adjudicator, decision_id=decision_id, decided_at=decided_at).digest,
        "document_id": document_id,
        "document_fingerprint": document.get("document_fingerprint"),
        "unlinked_state_fingerprint": document.get("unlinked_state_fingerprint"),
        "source_supported": bool(document.get("source_supported")),
        "document_role": str(role).strip(),
        "document_description": str(description).strip(),
        "human_stated_item": str(human_stated_item).strip(),
        "candidate": None,
        "proposal": {
            "path": Path((_lineage_of(loaded)["proposal"].get("path") or "")).name,
            "digest": (_lineage_of(loaded)["proposal"].get("digest")),
            "decision_unit_id": proposal.get("decision_unit_id"),
            "model_recommendation": proposal.get("model_recommendation"),
            "confidence": proposal.get("confidence"),
        },
        "lineage": _lineage_of(loaded),
        "target_identity": dict(loaded.get("target_identity") or {}),
        "snapshot": dict(loaded.get("snapshot") or {}),
        "promoted": False,
        "applied": False,
    }
    if target is not None:
        payload["candidate"] = {
            "agenda_item_db_id": int(target["agenda_item_db_id"]),
            "meeting_db_id": int(target["meeting_db_id"]),
            "agenda_item_id": bound.get("agenda_item_id"),
            "agenda_item_number": stored_number,
            "agenda_item_fingerprint": target.get("agenda_item_fingerprint"),
        }
    # The human may name the item differently from the stored number.  That is
    # recorded, not reconciled: a mismatch is evidence about the candidate, and
    # silently rewriting it would hide a parsing defect behind a human decision.
    item_mismatch = bool(stored_number and payload["human_stated_item"]
                         and stored_number != payload["human_stated_item"])
    payload["item_number_mismatch"] = item_mismatch
    payload["item_number_note"] = (
        f"human names {payload['human_stated_item']!r}; candidate {target['agenda_item_db_id']} "
        f"is stored as {stored_number!r}" if item_mismatch else "")
    return payload


def validate_decision(
    record: Mapping[str, Any], *,
    current_document_fingerprint: str | None = None,
    current_unlinked_state_fingerprint: str | None = None,
    current_candidate_fingerprint: str | None = None,
    current_plan_digest: str | None = None,
    current_aggregate_digest: str | None = None,
    aggregate_chain: Sequence[str] | None = None,
    source_supported: bool | None = None,
) -> list[str]:
    """Validate ONE decision against freshly loaded current state."""
    problems: list[str] = []
    if record.get("kind") != DECISION_KIND:
        problems.append(f"kind must be {DECISION_KIND!r}")
    if record.get("decision") not in DECISION_KINDS:
        problems.append(f"decision {record.get('decision')!r} is not registered")
    for field in ("decision_id", "decided_at", "adjudicator", "document_id",
                  "document_role", "document_description", "human_stated_item"):
        if not str(record.get(field) or "").strip():
            problems.append(f"decision is missing {field!r}")
    for field in NOT_ACTIONS:
        if record.get(field) is not False:
            problems.append(f"a decision must record {field}=false")
    if record.get("source_supported") is not False:
        problems.append("a decision may not record a source-supported document")

    if current_document_fingerprint is not None and \
            record.get("document_fingerprint") != current_document_fingerprint:
        problems.append("document fingerprint has drifted since the decision")
    if current_unlinked_state_fingerprint is not None and \
            record.get("unlinked_state_fingerprint") != current_unlinked_state_fingerprint:
        problems.append("unlinked-state fingerprint has drifted since the decision")
    if source_supported is not None and bool(source_supported):
        problems.append("the document became source-supported after the decision")

    lineage = record.get("lineage") or {}
    if current_plan_digest is not None and \
            (lineage.get("plan") or {}).get("digest") != current_plan_digest:
        problems.append("decision does not bind the current plan digest")
    # The aggregate is a DERIVED INDEX, not the authority.  The decision binds
    # the plan and the proposal; the aggregate in force at adjudication is
    # recorded for traceability.  It cannot be required to equal the current
    # aggregate, because the current aggregate is the one that indexes this very
    # decision - its content depends on the decision, so binding it would be
    # circular.  It is verified by walking the supersession chain instead, so a
    # decision bound to an aggregate that is neither current nor an ancestor of
    # current is still refused.
    if current_aggregate_digest is not None:
        recorded_aggregate = (lineage.get("aggregate") or {}).get("digest")
        if recorded_aggregate != current_aggregate_digest and \
                recorded_aggregate not in set(aggregate_chain or ()):
            problems.append(
                "decision binds an aggregate that is neither current nor an ancestor "
                "of the current aggregate")

    if record.get("decision") == APPROVE:
        candidate = record.get("candidate") or {}
        if not candidate.get("agenda_item_db_id"):
            problems.append("an approval must name the candidate it approves")
        if not candidate.get("agenda_item_fingerprint"):
            problems.append("an approval must carry the candidate fingerprint")
        if current_candidate_fingerprint is not None and \
                candidate.get("agenda_item_fingerprint") != current_candidate_fingerprint:
            problems.append("candidate fingerprint has drifted since the decision")
        if not record.get("document_role"):
            problems.append("an approval must record the document role")
    return problems


def assert_decision_current(
    record: Mapping[str, Any],
    loaded: Mapping[str, Any],
    lineage_chain: Sequence[Mapping[str, Any]] | None = None,
) -> None:
    """Refuse if the decision no longer describes the loader's current state."""
    document = loaded.get("document") or {}
    target = loaded.get("target") or {}
    lineage = loaded.get("lineage") or {}
    chain = [entry["digest"] for entry in lineage_chain] if lineage_chain else None
    problems = validate_decision(
        record,
        current_document_fingerprint=document.get("document_fingerprint"),
        current_unlinked_state_fingerprint=document.get("unlinked_state_fingerprint"),
        current_candidate_fingerprint=(target or {}).get("agenda_item_fingerprint"),
        current_plan_digest=(lineage.get("plan") or {}).get("digest"),
        current_aggregate_digest=(lineage.get("aggregate") or {}).get("digest"),
        aggregate_chain=chain,
        source_supported=document.get("source_supported"),
    )
    if problems:
        raise DecisionRefused("; ".join(problems[:5]))


def bind_aggregate(
    aggregate: Mapping[str, Any],
    decisions: Sequence[Mapping[str, Any]],
    *,
    created_at: str,
    supersedes: Mapping[str, Any] | None = None,
    supersedes_reason: str = "",
) -> dict[str, Any]:
    """Rebind an aggregate so it carries the human decisions, and nothing else.

    The per-unit proposal membership is copied unchanged.  Only the decision
    index and the counts move; a rebind may not add, drop or retarget a proposal.
    """
    by_document: dict[int, dict[str, Any]] = {}
    for record in decisions:
        document_id = int(record["document_id"])
        if document_id in by_document:
            raise DecisionRefused(f"two decisions for document {document_id}")
        by_document[document_id] = {
            "decision_id": record["decision_id"],
            "decision": record["decision"],
            "adjudicator": record["adjudicator"],
            "decided_at": record["decided_at"],
            "document_role": record["document_role"],
            "digest": record.get("digest"),
            "path": record.get("path"),
        }

    bound_documents = {int(e["document_id"])
                       for entries in (aggregate.get("per_unit") or {}).values()
                       for e in entries}
    unknown = sorted(set(by_document) - bound_documents)
    if unknown:
        raise DecisionRefused(f"decisions name documents not bound by the aggregate: {unknown}")

    rebound = dict(aggregate)
    rebound["created_at"] = created_at
    rebound["counts"] = dict(aggregate.get("counts") or {})
    approved = sum(1 for r in decisions if r["decision"] == APPROVE)
    rebound["counts"].update({
        "decided": len(by_document),
        "approved": approved,
        "promoted": 0,
        "applied": 0,
    })
    rebound["decisions"] = {str(k): v for k, v in sorted(by_document.items())}
    rebound["supersedes"] = dict(supersedes) if supersedes else None
    rebound["supersedes_reason"] = supersedes_reason
    for key in ("digest",):
        rebound.pop(key, None)
    return rebound
