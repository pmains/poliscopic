#!/usr/bin/env python3
"""``stage2_s2_promotion_contract.py`` — the reviewed promotion/apply contract.

Five decisions are approved.  None is promoted.  This module defines what
promotion *would* require, so that the act is a reviewed operation rather than an
improvised UPDATE.

Three rules carry the weight:

1. **Nothing is promoted implicitly.**  A promotion binds all five decisions and,
   for each, the proposal, document and target fingerprints it was decided
   against.  A missing or drifted fingerprint refuses the whole promotion.
2. **A wrong candidate number is never silently corrected.**  Candidate 278749 is
   stored as ``2026`` — the spurious resolution-number item — while the human
   decided item ``2.C``.  Promotion of that decision is refused until a canonical
   item numbered ``2.C`` actually exists for the meeting.  The rebinding is
   defined here and is *conditional on parser repair*, never on human assertion.
3. **Nothing here writes.**  ``promoted`` and ``applied`` stay false; no database
   is touched; there is no code path that performs a link.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_s2_human_decision as human  # noqa: E402

__all__ = [
    "CONTRACT_KIND",
    "CONTRACT_VERSION",
    "PromotionRefused",
    "assert_promotable",
    "build_contract",
    "rebind_candidate",
    "validate_contract",
]

CONTRACT_KIND = "kg-stage2-s2-promotion-contract"
CONTRACT_VERSION = "kg-stage2-s2-promotion-contract/1.0"

#: A promotion moves a link into canonical state.  These are the preconditions.
REQUIRED_DECISION_FIELDS = (
    "decision_id", "decided_at", "adjudicator", "document_id",
    "document_fingerprint", "unlinked_state_fingerprint",
)


class PromotionRefused(RuntimeError):
    """The promotion is not admissible; nothing is promoted or applied."""


def rebuild_reason(record: Mapping[str, Any], canonical_numbers: Sequence[str]) -> str:
    """Why the decision's candidate may not be promoted, or '' when it may.

    A decision whose human-named item disagrees with the candidate's stored
    number is only promotable once the canonical item the human named exists.
    The candidate's own number is never rewritten to match the human.
    """
    if not record.get("item_number_mismatch"):
        return ""
    stated = str(record.get("human_stated_item") or "").strip()
    candidate = record.get("candidate") or {}
    stored = str(candidate.get("agenda_item_number") or "").strip()
    numbers = {str(n).strip() for n in canonical_numbers}
    if stated and stated in numbers:
        return ""
    return (f"candidate {candidate.get('agenda_item_db_id')} is stored as {stored!r} but the "
            f"human decided {stated!r}; canonical {stated!r} does not exist for meeting "
            f"{candidate.get('meeting_db_id')}, so promotion is refused until the parser "
            f"materialises it")


def build_contract(
    decisions: Sequence[Mapping[str, Any]],
    *,
    canonical_items: Sequence[Mapping[str, Any]],
    created_at: str,
    target: Mapping[str, Any],
    applied_link_column: str = "supporting_documents.agenda_item_db_id",
) -> dict[str, Any]:
    """Build the promotion contract over the approved decisions.

    *canonical_items* are the meeting's canonical agenda items as they exist
    NOW; they decide whether each candidate's number is trustworthy.
    """
    by_meeting: dict[int, set[str]] = {}
    for item in canonical_items:
        meeting = int(item["meeting_db_id"])
        by_meeting.setdefault(meeting, set()).add(str(item["agenda_item_number"]).strip())

    entries = []
    for record in decisions:
        candidate = record.get("candidate") or {}
        meeting = int(candidate.get("meeting_db_id") or 0)
        mismatch = bool(record.get("item_number_mismatch"))
        reason = rebuild_reason(record, sorted(by_meeting.get(meeting, set())))
        entries.append({
            "decision_id": record.get("decision_id"),
            "document_id": record.get("document_id"),
            "decision": record.get("decision"),
            "adjudicator": record.get("adjudicator"),
            "decided_at": record.get("decided_at"),
            "document_role": record.get("document_role"),
            "document_fingerprint": record.get("document_fingerprint"),
            "unlinked_state_fingerprint": record.get("unlinked_state_fingerprint"),
            "proposal_path": (record.get("proposal") or {}).get("path"),
            "proposal_digest": (record.get("proposal") or {}).get("digest"),
            "candidate": {
                "agenda_item_db_id": candidate.get("agenda_item_db_id"),
                "meeting_db_id": candidate.get("meeting_db_id"),
                "agenda_item_number": candidate.get("agenda_item_number"),
                "agenda_item_fingerprint": candidate.get("agenda_item_fingerprint"),
            },
            "human_stated_item": record.get("human_stated_item"),
            # The disagreement itself is permanent evidence.
            "item_number_mismatch": mismatch,
            # Blocking while the canonical item is absent...
            "rebind_required": bool(mismatch and reason),
            # ...and the mapping to apply once the parser materialises it.
            "rebind_available": bool(mismatch and not reason),
            "rebind_reason": reason,
        })

    blocked = [e for e in entries if e["rebind_reason"]]
    contract = {
        "kind": CONTRACT_KIND,
        "version": CONTRACT_VERSION,
        "created_at": created_at,
        "policy": {
            "promotion_is_reviewed": True,
            "bind_every_fingerprint": True,
            "never_rewrite_a_candidate_number": True,
            "rebind_only_after_parser_repair": True,
            "no_link_is_written_by_this_contract": True,
        },
        "target": dict(target),
        "applied_link_column": applied_link_column,
        "decisions": entries,
        "counts": {
            "decisions": len(entries),
            "approved": sum(1 for e in entries if e["decision"] == human.APPROVE),
            "rebind_required": sum(1 for e in entries if e["rebind_required"]),
            "rebind_available": sum(1 for e in entries if e["rebind_available"]),
            "promotable": sum(1 for e in entries if not e["rebind_reason"]),
            "blocked": len(blocked),
            "promoted": 0,
            "applied": 0,
        },
        "blocked": [{"document_id": e["document_id"], "reason": e["rebind_reason"]}
                    for e in blocked],
        "promoted": False,
        "applied": False,
    }
    problems = validate_contract(contract)
    if problems:
        raise PromotionRefused("; ".join(problems[:5]))
    return contract


def validate_contract(contract: Mapping[str, Any]) -> list[str]:
    """Every structural requirement of a promotion contract."""
    problems: list[str] = []
    if contract.get("kind") != CONTRACT_KIND:
        problems.append(f"kind must be {CONTRACT_KIND!r}")
    if contract.get("promoted") is not False or contract.get("applied") is not False:
        problems.append("a contract must record promoted=false and applied=false")

    entries = contract.get("decisions") or []
    if not entries:
        problems.append("a promotion contract covers no decisions")
    for entry in entries:
        document_id = entry.get("document_id")
        for field in REQUIRED_DECISION_FIELDS:
            if not str(entry.get(field) or "").strip():
                problems.append(f"decision for document {document_id} is missing {field!r}")
        if not entry.get("proposal_digest"):
            problems.append(f"decision for document {document_id} binds no proposal digest")
        candidate = entry.get("candidate") or {}
        if not candidate.get("agenda_item_db_id"):
            problems.append(f"decision for document {document_id} binds no candidate")
        if not candidate.get("agenda_item_fingerprint"):
            problems.append(
                f"decision for document {document_id} binds no candidate fingerprint")
        if entry.get("decision") != human.APPROVE:
            continue
        if entry.get("rebind_required") and not entry.get("rebind_reason"):
            problems.append(
                f"document {document_id} requires a rebind but records no reason")
    return problems


def assert_promotable(contract: Mapping[str, Any]) -> None:
    """Refuse the promotion if any decision is blocked or any binding is absent."""
    problems = validate_contract(contract)
    if problems:
        raise PromotionRefused("; ".join(problems[:5]))
    blocked = contract.get("blocked") or []
    if blocked:
        first = blocked[0]
        raise PromotionRefused(
            f"{len(blocked)} decision(s) are not promotable; "
            f"document {first['document_id']}: {first['reason']}")
    counts = contract.get("counts") or {}
    if counts.get("promotable") != counts.get("decisions"):
        raise PromotionRefused("the contract's promotable count does not cover every decision")


def rebind_candidate(
    record: Mapping[str, Any],
    *,
    canonical_item: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Define the 2026 -> 2.C rebinding, conditionally on parser repair.

    Returns the canonical target the decision should promote to.  It refuses when
    the canonical item is absent or when its number still disagrees with the
    human's stated item: the correction follows from the parser materialising the
    item, never from the human asserting it.
    """
    if not record.get("item_number_mismatch"):
        raise PromotionRefused("this decision does not require a rebind")
    stated = str(record.get("human_stated_item") or "").strip()
    if canonical_item is None:
        raise PromotionRefused(
            f"no canonical item {stated!r} exists yet; the candidate is still stored as "
            f"{(record.get('candidate') or {}).get('agenda_item_number')!r}")
    number = str(canonical_item.get("agenda_item_number") or "").strip()
    if number != stated:
        raise PromotionRefused(
            f"the supplied canonical item is numbered {number!r}, not {stated!r}")
    candidate = record.get("candidate") or {}
    if int(canonical_item.get("meeting_db_id", -1)) != int(candidate.get("meeting_db_id", -2)):
        raise PromotionRefused("the canonical item belongs to a different meeting")
    return {
        "document_id": record.get("document_id"),
        "decision_id": record.get("decision_id"),
        "from": {"agenda_item_db_id": candidate.get("agenda_item_db_id"),
                 "agenda_item_number": candidate.get("agenda_item_number"),
                 "agenda_item_fingerprint": candidate.get("agenda_item_fingerprint")},
        "to": {"agenda_item_db_id": canonical_item.get("agenda_item_db_id") or
                                 canonical_item.get("id"),
               "agenda_item_number": number,
               "agenda_item_fingerprint": canonical_item.get("agenda_item_fingerprint")},
        "requires": "the corrected block parser must have materialised "
                    f"{stated!r} for meeting {candidate.get('meeting_db_id')}",
        "human_assertion_alone_is_insufficient": True,
    }
