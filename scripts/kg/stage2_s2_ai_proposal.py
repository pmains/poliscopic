#!/usr/bin/env python3
"""``stage2_s2_ai_proposal.py`` — inferred assertions, never source truth.

Some documents cannot be resolved by any deterministic rule and need a human
decision (see the adjudication packet).  This module defines the format for a
*secondary* pass in which a model may propose a link, so proposals arrive in a
shape that can be reviewed rather than applied.

The format is deliberately constrained.  A proposal:

* is an **inferred assertion**, never a source-supported one.  "Inferred" is
  the *semantics*; the stored ``assertion_class`` is the canonical registered
  slug ``derived``, taken from the assertion-class registry rather than invented
  here;
* **cannot target a document that already has a source-supported link**, so a
  model can never overwrite what the sources settled;
* **cannot become canonical on its own.**  Promotion requires a threshold and a
  reviewer decision, both of which are recorded; without them the proposal stays
  a proposal and the document stays unlinked;
* carries the model identity and version, the input fingerprints it saw, a
  confidence, a rationale, and evidence spans with offsets into the input text.

Nothing here contacts a model or a database.  It validates and records.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

#: The assertion-class registry is the single source of truth for the slug a
#: proposal carries.  Nothing here defines its own vocabulary.
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg.registries.evidence import is_source_supported  # noqa: E402

__all__ = [
    "ASSERTION_CLASSES",
    "CHOICES",
    "MODEL_RECOMMENDATIONS",
    "PROPOSAL_VERSION",
    "ProposalRefused",
    "candidate_fingerprint",
    "expand_group",
    "REQUIRED_CURRENT_STATE",
    "load_proposal_for_adjudication",
    "single_proposal",
    "unlinked_state_fingerprint",
    "validate_current",
    "validate_proposal",
    "PROPOSAL_ASSERTION_CLASS",
    "PACKET_KIND",
    "PACKET_VERSION",
    "REQUIRED_PROPOSAL_FIELDS",
    "build_packet",
    "default_review_policy",
    "validate_packet",
]

from scripts.kg.stage2_s2_ai_packet import (  # noqa: E402,F401 - re-exported
    ASSERTION_CLASSES,
    CHOICES,
    DEFAULT_THRESHOLD,
    MODEL_RECOMMENDATIONS,
    PACKET_KIND,
    PACKET_VERSION,
    PROPOSAL_ASSERTION_CLASS,
    PROPOSAL_VERSION,
    ProposalRefused,
    REQUIRED_PROPOSAL_FIELDS,
    UNDECIDED_FIELDS,
    build_packet,
    default_review_policy,
    validate_packet,
)

def unlinked_state_fingerprint(document_id: int, document_fingerprint: str) -> str:
    """The exact fingerprint of a document that is still unlinked.

    A proposal is only meaningful against the state it was made about, so the
    link-free state is pinned explicitly rather than assumed.  When the additive
    column exists this reads ``agenda_item_db_id IS NULL``; today the column does
    not exist, which is itself the unlinked state.
    """
    body = json.dumps({"document_id": int(document_id),
                       "document_fingerprint": document_fingerprint,
                       "agenda_item_db_id": None},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def candidate_fingerprint(candidate: Mapping[str, Any]) -> str:
    """Exact identity of the canonical agenda item a link would point at."""
    body = json.dumps({
        "agenda_item_db_id": int(candidate["agenda_item_db_id"]),
        # the meeting is part of the identity: a candidate that moved meetings
        # is a different candidate, and the loader asserts it equals the
        # document's meeting.
        "meeting_db_id": (int(candidate["meeting_db_id"])
                          if candidate.get("meeting_db_id") is not None else None),
        "agenda_item_id": candidate.get("agenda_item_id"),
        "agenda_item_number": candidate.get("agenda_item_number"),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def single_proposal(
    result: Mapping[str, Any], document: Mapping[str, Any], *,
    unit_id: str, model: str, model_version: str, provider: str,
    prompt_version: str, input_fingerprints: Mapping[str, str],
    candidates: Sequence[Mapping[str, Any]] = (), lead_document_id: int | None = None,
    evidence_spans: Sequence[Mapping[str, Any]] = (),
    candidate_resolver: Any = None,
) -> dict[str, Any]:
    """Build ONE canonical proposal for ONE document from a grouped result.

    The group's recommendation is inherited; the identity is per-document.  A
    link recommendation must name a candidate, and that candidate's identity and
    fingerprint are recorded so the recommendation can be re-checked later.
    """
    document_id = int(document["document_id"])
    fingerprint = document["fingerprint"]
    by_id = {int(c["agenda_item_db_id"]): c for c in candidates}
    recommendation = str(result.get("model_recommendation") or "").strip()
    if recommendation not in MODEL_RECOMMENDATIONS:
        raise ProposalRefused(f"unknown model recommendation {recommendation!r}")

    links: list[dict[str, Any]] = []
    if recommendation == "link":
        target = result.get("agenda_item_db_id")
        if target is None:
            raise ProposalRefused("a link recommendation must name a candidate")
        record = by_id.get(int(target))
        if record is None and candidate_resolver is not None:
            record = candidate_resolver(int(target))
        if record is None:
            raise ProposalRefused(f"candidate {target} is not in this meeting's candidate set")
        links.append({
            "agenda_item_db_id": int(target),
            "agenda_item_id": record.get("agenda_item_id"),
            "agenda_item_number": record.get("agenda_item_number"),
            "agenda_item_fingerprint": candidate_fingerprint(record),
            "confidence": result.get("confidence"),
        })

    return {
        "kind": PACKET_KIND,
        "version": PROPOSAL_VERSION,
        "decision_unit_id": unit_id,
        "document_id": document_id,
        "document_fingerprint": fingerprint,
        "unlinked_state_fingerprint": unlinked_state_fingerprint(document_id, fingerprint),
        "assertion_class": PROPOSAL_ASSERTION_CLASS,
        "provider": provider,
        "model": model,
        "model_version": model_version,
        "prompt_version": prompt_version,
        "input_fingerprints": dict(input_fingerprints),
        "model_recommendation": recommendation,
        "candidate_links": links,
        "confidence": result.get("confidence"),
        "rationale": result.get("rationale") or "",
        "evidence_spans": [dict(span) for span in evidence_spans],
        "decision": None,
        "decided_by": None,
        "decided_at": None,
        "promoted": False,
        "lead_document_id": lead_document_id,
    }


def expand_group(
    result: Mapping[str, Any], documents: Sequence[Mapping[str, Any]], **kwargs: Any
) -> list[dict[str, Any]]:
    """Expand one grouped result into one proposal per bound document.

    Fails closed: a group that does not produce exactly one proposal for every
    bound document, and only those documents, is refused rather than partially
    recorded.  A partial expansion is how a document silently goes unadjudicated.
    """
    bound = [int(d["document_id"]) for d in documents]
    if not bound:
        raise ProposalRefused("group has no bound documents")
    if len(set(bound)) != len(bound):
        raise ProposalRefused("group binds the same document more than once")
    produced = [single_proposal(result, d, candidates=kwargs.get("candidates", ()),
                                **{k: v for k, v in kwargs.items() if k != "candidates"})
                for d in documents]
    seen = [p["document_id"] for p in produced]
    if sorted(seen) != sorted(bound):
        raise ProposalRefused(
            f"expansion produced {sorted(seen)} but the group binds {sorted(bound)}")
    return produced


def validate_proposal(
    proposal: Mapping[str, Any], *,
    current_document_fingerprint: str | None = None,
    current_unlinked_state_fingerprint: str | None = None,
    current_candidate_fingerprint: str | None = None,
    candidate_ids: Sequence[int] | None = None,
    source_supported: bool = False,
) -> list[str]:
    """Validate ONE canonical proposal.  The single validation authority.

    One-document identity and the undecided state are non-negotiable: a proposal
    that carries several documents, or that arrives already decided or promoted,
    is refused rather than repaired.
    """
    problems: list[str] = []
    for field in REQUIRED_PROPOSAL_FIELDS:
        if field not in proposal:
            problems.append(f"proposal is missing {field!r}")

    document_id = proposal.get("document_id")
    if not isinstance(document_id, int):
        problems.append("document_id must be a single integer, not an array or null")
    if "document_ids" in proposal:
        problems.append("proposal carries a document_ids array; one proposal is one document")
    if not proposal.get("document_fingerprint"):
        problems.append("proposal carries no document fingerprint")
    if not proposal.get("unlinked_state_fingerprint"):
        problems.append("proposal carries no unlinked-state fingerprint")
    if not proposal.get("decision_unit_id"):
        problems.append("proposal carries no decision_unit_id")

    if proposal.get("assertion_class") not in ASSERTION_CLASSES:
        problems.append("assertion_class is not registered")
    elif proposal.get("assertion_class") != PROPOSAL_ASSERTION_CLASS:
        problems.append(f"assertion_class must be {PROPOSAL_ASSERTION_CLASS!r}")
    if is_source_supported(str(proposal.get("assertion_class"))):
        problems.append("a proposal may never claim a source-supported class")

    if not proposal.get("provider") or not proposal.get("model"):
        problems.append("proposal does not record its provider and model")
    if not proposal.get("model_version") or not proposal.get("prompt_version"):
        problems.append("proposal does not record its model and prompt versions")
    if not proposal.get("input_fingerprints"):
        problems.append("proposal records no input fingerprints")

    # undecided and unpromoted, always
    for field in UNDECIDED_FIELDS:
        if proposal.get(field, "missing") is not None:
            problems.append(f"{field} must be null on a proposal, got {proposal.get(field)!r}")
    if proposal.get("promoted") is not False:
        problems.append("a proposal must not arrive promoted")

    recommendation = proposal.get("model_recommendation")
    if recommendation not in MODEL_RECOMMENDATIONS:
        problems.append(f"model_recommendation {recommendation!r} is not one of {MODEL_RECOMMENDATIONS}")
    links = proposal.get("candidate_links") or []
    if recommendation == "link":
        if len(links) != 1:
            problems.append("a link recommendation must carry exactly one candidate link")
        else:
            link = links[0]
            if not link.get("agenda_item_fingerprint"):
                problems.append("candidate link carries no agenda-item fingerprint")
            if not link.get("agenda_item_id") and not link.get("agenda_item_number"):
                problems.append("candidate link carries no agenda-item identity")
            if candidate_ids is not None and int(link.get("agenda_item_db_id", -1)) not in set(candidate_ids):
                problems.append("candidate link is not in this meeting's candidate set")
    elif links:
        problems.append(f"a {recommendation} recommendation must not carry a candidate link")

    confidence = proposal.get("confidence")
    if recommendation == "abstain" and confidence is None:
        pass                       # an abstention may legitimately carry no confidence
    elif not isinstance(confidence, (int, float)) or not 0.0 <= float(confidence) <= 1.0:
        problems.append("proposal has no usable confidence")
    if not proposal.get("rationale"):
        problems.append("proposal has no rationale")
    for index, span in enumerate(proposal.get("evidence_spans") or []):
        for field in ("text", "start", "end"):
            if field not in span:
                problems.append(f"evidence span {index} has no {field}")
        if isinstance(span.get("start"), int) and isinstance(span.get("end"), int):
            if span["end"] < span["start"]:
                problems.append(f"evidence span {index} ends before it starts")

    if source_supported:
        problems.append("proposal targets a document that already has a source-supported link")
    if current_document_fingerprint is not None and \
            proposal.get("document_fingerprint") != current_document_fingerprint:
        problems.append("document fingerprint has drifted since the proposal was made")
    if current_unlinked_state_fingerprint is not None and \
            proposal.get("unlinked_state_fingerprint") != current_unlinked_state_fingerprint:
        problems.append("unlinked-state fingerprint has drifted since the proposal was made")
    if current_candidate_fingerprint is not None and links and \
            links[0].get("agenda_item_fingerprint") != current_candidate_fingerprint:
        problems.append("candidate fingerprint has drifted since the proposal was made")
    return problems

#: Current-state inputs an adjudication entry point MUST supply.  A proposal is
#: only reviewable against the state it was made about, so a loader that cannot
#: produce these must refuse rather than validate against nothing.
REQUIRED_CURRENT_STATE = (
    "current_document_fingerprint",
    "current_unlinked_state_fingerprint",
    "candidate_ids",
    "source_supported",
)


def validate_current(proposal: Mapping[str, Any], **current: Any) -> list[str]:
    """The adjudication entry point: every current-state input is mandatory.

    ``validate_proposal`` may be called with partial context because it also
    serves construction-time checks.  This wrapper is what a reviewer or an
    adjudication entry point should call, and it refuses outright if any
    current-state input is missing - including the *recomputed* unlinked-state
    fingerprint, which is compared rather than merely required to be non-empty.
    """
    missing = [name for name in REQUIRED_CURRENT_STATE if current.get(name) is None]
    if proposal.get("model_recommendation") == "link" and \
            current.get("current_candidate_fingerprint") is None:
        missing.append("current_candidate_fingerprint")
    if missing:
        return [f"current-state input {name!r} was not supplied" for name in missing]

    problems = validate_proposal(proposal, **current)
    expected = unlinked_state_fingerprint(
        int(proposal["document_id"]), current["current_document_fingerprint"])
    if proposal.get("unlinked_state_fingerprint") != expected:
        problems.append(
            "unlinked-state fingerprint is not the current unlinked state of this document")
    return problems


def load_proposal_for_adjudication(path: str | Path, **current: Any) -> dict[str, Any]:
    """Load a proposal and refuse to hand it to a reviewer unless it is current.

    Fails closed on: a missing current-state input, any fingerprint drift, a
    candidate outside the meeting's candidate set, and a document that has since
    gained a source-supported link.
    """
    proposal = artifacts.load_verified(path)
    problems = validate_current(proposal, **current)
    if problems:
        raise ProposalRefused("; ".join(problems[:5]))
    return proposal
