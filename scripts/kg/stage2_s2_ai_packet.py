#!/usr/bin/env python3
"""``stage2_s2_ai_packet.py`` — shared vocabulary and the packet-level API.

Extracted from ``stage2_s2_ai_proposal`` to keep both modules under the 500-line
limit.  This module owns the *vocabulary* (the assertion class, the choice set,
the required field list, the refusal type) and the packet-level review surface.
``stage2_s2_ai_proposal`` remains the single validation authority for a single
proposal and re-exports everything here, so callers keep one import.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg.registries.evidence import (  # noqa: E402
    ASSERTION_CLASSES,
    is_source_supported,
)

__all__ = [
    "ASSERTION_CLASSES",
    "CHOICES",
    "DEFAULT_THRESHOLD",
    "MODEL_RECOMMENDATIONS",
    "PACKET_KIND",
    "PACKET_VERSION",
    "PROPOSAL_ASSERTION_CLASS",
    "PROPOSAL_VERSION",
    "ProposalRefused",
    "REQUIRED_PROPOSAL_FIELDS",
    "UNDECIDED_FIELDS",
    "build_packet",
    "default_review_policy",
    "validate_packet",
]

PACKET_KIND = "kg-stage2-s2-ai-proposal"
PACKET_VERSION = "kg-stage2-s2-ai-proposal/1.0"

#: One canonical proposal describes exactly ONE supporting document.  Version
#: 2.0 adds ``decision_unit_id`` (which group the document was adjudicated in)
#: and ``model_recommendation`` (what the model suggested) while *separating*
#: that from ``decision`` (what a human decided, always null on arrival).
PROPOSAL_VERSION = "kg-stage2-s2-ai-proposal/2.0"

#: What a model may recommend.  A recommendation is not a decision.
MODEL_RECOMMENDATIONS = ("link", "abstain", "meeting_only")


class ProposalRefused(ValueError):
    """A proposal was refused; nothing should be recorded from it."""

#: The canonical slug a model proposal carries.
#:
#: A proposal is *model-inferred*, and the registry's own entry for that is
#: ``derived`` — "computed from canonical assertions", to be "labeled
#: inferred/derived and explain its inputs".  There is deliberately no local
#: vocabulary here: the value is one registered slug, and the guard below fails
#: closed at import if the registry ever stops defining it.
PROPOSAL_ASSERTION_CLASS = "derived"
if PROPOSAL_ASSERTION_CLASS not in ASSERTION_CLASSES:  # pragma: no cover - guard
    raise RuntimeError(
        f"registry does not define the proposal assertion class "
        f"{PROPOSAL_ASSERTION_CLASS!r}"
    )

#: The reviewer's options for a single proposal.
CHOICES = ("approve", "reject", "alternate", "meeting_only")

REQUIRED_PROPOSAL_FIELDS = (
    "document_id", "document_fingerprint", "unlinked_state_fingerprint",
    "decision_unit_id", "assertion_class", "provider", "model",
    "model_version", "prompt_version", "input_fingerprints", "confidence",
    "rationale", "evidence_spans", "candidate_links", "model_recommendation",
    "decision", "decided_by", "decided_at", "promoted",
)

#: Fields that must be present-and-null, or the proposal is not undecided.
UNDECIDED_FIELDS = ("decision", "decided_by", "decided_at")

#: Promotion is impossible without both a threshold and a reviewer.
DEFAULT_THRESHOLD = 0.9


def default_review_policy(threshold: float = DEFAULT_THRESHOLD) -> dict[str, Any]:
    """The gate a proposal must clear before it could ever become canonical."""
    return {
        "threshold": float(threshold),
        "requires_reviewer_decision": True,
        "may_overwrite_source_supported_link": False,
        "promotes_to_canonical": False,
        "assertion_class": PROPOSAL_ASSERTION_CLASS,
        "note": "a proposal is a model-inferred assertion stored as the canonical "
                "assertion class 'derived'; approval is a human act and promotion "
                "is a separate, separately-authorized step",
    }


def _proposal(document: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "document_id": int(document["document_id"]),
        "document_fingerprint": document["document_fingerprint"],
        "assertion_class": proposal.get("assertion_class") or PROPOSAL_ASSERTION_CLASS,
        "model": proposal.get("model"),
        "model_version": proposal.get("model_version"),
        "input_fingerprints": dict(proposal.get("input_fingerprints") or {}),
        "confidence": proposal.get("confidence"),
        "rationale": proposal.get("rationale") or "",
        "evidence_spans": [dict(span) for span in (proposal.get("evidence_spans") or [])],
        "candidate_links": [dict(c) for c in (proposal.get("candidate_links") or [])],
        # contract 2.0 fields, carried through so the packet path and the
        # single-proposal path are validated against the same field set
        "unlinked_state_fingerprint": proposal.get("unlinked_state_fingerprint"),
        "decision_unit_id": proposal.get("decision_unit_id"),
        "provider": proposal.get("provider"),
        "prompt_version": proposal.get("prompt_version"),
        "model_recommendation": proposal.get("model_recommendation"),
        "decision": None,
        "decided_by": None,
        "decided_at": None,
        "allowed_choices": list(CHOICES),
        "promoted": False,
    }


def build_packet(
    documents: Sequence[Mapping[str, Any]],
    proposals: Iterable[Mapping[str, Any]],
    *,
    packet_id: str,
    created_at: str,
    plan_digest: str,
    packet_digest: str,
    policy: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a review packet from candidates and the model's proposals."""
    by_id = {int(d["document_id"]): d for d in documents}
    reviewed: list[dict[str, Any]] = []
    for proposal in proposals:
        document = by_id.get(int(proposal["document_id"]))
        if document is None:
            continue                      # a proposal for an unknown document is dropped
        reviewed.append(_proposal(document, proposal))
    return {
        "kind": PACKET_KIND,
        "version": PACKET_VERSION,
        "packet_id": packet_id,
        "created_at": created_at,
        "source_plan_digest": plan_digest,
        "source_packet_digest": packet_digest,
        "review_policy": dict(policy or default_review_policy()),
        "counts": {"candidates": len(documents), "proposals": len(reviewed),
                   "undecided": sum(1 for r in reviewed if r["decision"] is None)},
        "proposals": reviewed,
        "decisions": [],
    }


def _span_problems(document_id: Any, spans: Sequence[Mapping[str, Any]]) -> list[str]:
    problems: list[str] = []
    for index, span in enumerate(spans):
        for field in ("text", "start", "end"):
            if field not in span:
                problems.append(f"proposal {document_id} span {index} has no {field}")
        start, end = span.get("start"), span.get("end")
        if isinstance(start, int) and isinstance(end, int) and end < start:
            problems.append(f"proposal {document_id} span {index} ends before it starts")
    return problems


def validate_packet(
    packet: Mapping[str, Any], source_supported: Mapping[int, Any] | None = None
) -> list[str]:
    """Refuse a packet that would let an inference masquerade as evidence."""
    problems: list[str] = []
    if packet.get("kind") != PACKET_KIND:
        problems.append(f"unexpected packet kind {packet.get('kind')!r}")
    policy = packet.get("review_policy") or {}
    if policy.get("promotes_to_canonical"):
        problems.append("review policy would promote proposals to canonical on its own")
    if policy.get("may_overwrite_source_supported_link"):
        problems.append("review policy would let a proposal overwrite a source link")
    if not policy.get("requires_reviewer_decision"):
        problems.append("review policy does not require a reviewer decision")

    source_supported = {int(k): v for k, v in (source_supported or {}).items()}
    for proposal in packet.get("proposals", []):
        document_id = proposal.get("document_id")
        for field in REQUIRED_PROPOSAL_FIELDS:
            if field not in proposal:
                problems.append(f"proposal {document_id} is missing {field!r}")
        asserted = proposal.get("assertion_class")
        if asserted not in ASSERTION_CLASSES:
            problems.append(
                f"proposal {document_id} asserts class {asserted!r}, which the "
                "assertion-class registry does not define")
        elif asserted != PROPOSAL_ASSERTION_CLASS:
            problems.append(
                f"proposal {document_id} asserts class {asserted!r}; a model "
                f"proposal may only be {PROPOSAL_ASSERTION_CLASS!r}")
        elif is_source_supported(asserted):
            problems.append(
                f"proposal {document_id} asserts a source-supported class")
        if not proposal.get("model") or not proposal.get("model_version"):
            problems.append(f"proposal {document_id} does not record its model and version")
        if not proposal.get("input_fingerprints"):
            problems.append(f"proposal {document_id} records no input fingerprints")
        confidence = proposal.get("confidence")
        if not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
            problems.append(f"proposal {document_id} has no usable confidence")
        if not proposal.get("rationale"):
            problems.append(f"proposal {document_id} has no rationale")
        problems += _span_problems(document_id, proposal.get("evidence_spans") or [])
        if not proposal.get("candidate_links"):
            problems.append(f"proposal {document_id} proposes no candidate link")
        if document_id in source_supported:
            problems.append(
                f"proposal {document_id} targets a document that already has a "
                "source-supported link")
        if proposal.get("promoted"):
            problems.append(f"proposal {document_id} arrives already promoted")
        if proposal.get("decision") is not None:
            problems.append(f"proposal {document_id} arrives with a decision")
    return problems
