#!/usr/bin/env python3
"""Pure Stage 3 B3 evidence-span contract; no database or write path.

Offsets use Unicode code points and half-open intervals ``[start, end)``.  Every
span is bound to an exact retained-text version, one lineage parent, and either
one canonical agenda item or an explicit hold.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping, Sequence

PRODUCER_VERSION = "kg-stage3-b3-span-contract/1.0"
COORDINATE_SYSTEM = "unicode_codepoint_half_open"
SPAN_KINDS = ("stage1_meeting_event", "stage3_result_item")
LINK_BOUND = "bound"
LINK_UNLINKED = "unlinked"
LINK_AMBIGUOUS = "ambiguous"
HOLD_DISPOSITIONS = (
    "hold_missing_text",
    "hold_invalid_offsets",
    "hold_fingerprint_mismatch",
    "hold_missing_evidence_version",
    "hold_missing_lineage",
    "hold_unlinked_container",
    "hold_ambiguous_container",
    "hold_duplicate_identity",
)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_version_identity(version: Mapping[str, Any]) -> tuple[int, str, str, str]:
    """Exact retained text plus the extraction method/version that produced it."""
    return (
        int(version["supporting_doc_id"]),
        str(version["content_sha256"]),
        str(version["extraction_method"]),
        str(version["extractor_version"]),
    )


def span_identity(span: Mapping[str, Any]) -> tuple[int, str, int, int]:
    """Canonical span assertion identity."""
    return (
        int(span["supporting_doc_id"]),
        str(span["span_kind"]),
        int(span["text_offset_start"]),
        int(span["text_offset_end"]),
    )


def canonical_link(candidate_ids: Any) -> tuple[int | None, str]:
    """Return a link only when exactly one positive canonical id is supplied."""
    if candidate_ids is None:
        return None, LINK_UNLINKED
    if isinstance(candidate_ids, int) and not isinstance(candidate_ids, bool):
        return (candidate_ids, LINK_BOUND) if candidate_ids > 0 else (None, LINK_UNLINKED)
    if isinstance(candidate_ids, Sequence) and not isinstance(candidate_ids, (str, bytes)):
        ids = sorted({value for value in candidate_ids
                      if isinstance(value, int) and not isinstance(value, bool) and value > 0})
        if len(ids) == 1:
            return ids[0], LINK_BOUND
        return None, LINK_AMBIGUOUS if ids else LINK_UNLINKED
    return None, LINK_AMBIGUOUS


def build_span(*, supporting_doc_id: int, span_kind: str, start: int, end: int,
               text: str, extraction_method: str, extractor_version: str,
               lineage_parent_id: int, agenda_item_candidates: Any) -> dict[str, Any]:
    """Construct one assertion without deciding whether it is admissible."""
    item_id, link_status = canonical_link(agenda_item_candidates)
    version = {
        "supporting_doc_id": supporting_doc_id,
        "content_sha256": text_sha256(text),
        "extraction_method": extraction_method,
        "extractor_version": extractor_version,
    }
    valid_slice = (isinstance(start, int) and not isinstance(start, bool)
                   and isinstance(end, int) and not isinstance(end, bool)
                   and start >= 0 and end >= start)
    return {
        "supporting_doc_id": supporting_doc_id,
        "span_kind": span_kind,
        "coordinate_system": COORDINATE_SYSTEM,
        "text_offset_start": start,
        "text_offset_end": end,
        # Invalid source coordinates must survive construction so the classifier
        # can account for them as an explicit hold rather than crashing Q3.
        "span_sha256": text_sha256(text[start:end]) if valid_slice else None,
        "evidence_version": version,
        "evidence_version_identity": list(evidence_version_identity(version)),
        "lineage": {"source": span_kind, "parent_id": lineage_parent_id},
        "agenda_item_db_id": item_id,
        "link_status": link_status,
    }


def validate_span(span: Mapping[str, Any], current_text: str | None) -> list[str]:
    problems: list[str] = []
    text = current_text if isinstance(current_text, str) else ""
    doc_id = span.get("supporting_doc_id")
    if not isinstance(doc_id, int) or isinstance(doc_id, bool) or doc_id <= 0:
        problems.append("supporting_doc_id must be a positive integer")
    kind = span.get("span_kind")
    if kind not in SPAN_KINDS:
        problems.append("span_kind is not registered")
    if span.get("coordinate_system") != COORDINATE_SYSTEM:
        problems.append("coordinate_system must be unicode_codepoint_half_open")

    start, end = span.get("text_offset_start"), span.get("text_offset_end")
    valid_offsets = (
        isinstance(start, int) and not isinstance(start, bool) and start >= 0
        and isinstance(end, int) and not isinstance(end, bool) and end > start
    )
    if not valid_offsets:
        problems.append("offsets must be non-negative, increasing integers")
    elif end > len(text):
        problems.append("offsets exceed retained text")
    if not text:
        problems.append("retained text is required")

    lineage = span.get("lineage")
    if not isinstance(lineage, Mapping) or lineage.get("source") != kind:
        problems.append("lineage.source must equal span_kind")
    parent_id = lineage.get("parent_id") if isinstance(lineage, Mapping) else None
    if not isinstance(parent_id, int) or isinstance(parent_id, bool) or parent_id <= 0:
        problems.append("lineage.parent_id must be a positive integer")

    version = span.get("evidence_version")
    if not isinstance(version, Mapping):
        problems.append("evidence_version is required")
    else:
        try:
            identity = evidence_version_identity(version)
            if identity[0] != doc_id or not all(identity[1:]):
                problems.append("evidence_version is incomplete or belongs to another document")
            if identity[1] != text_sha256(text):
                problems.append("evidence content fingerprint drift")
            if list(identity) != span.get("evidence_version_identity"):
                problems.append("evidence_version_identity mismatch")
        except (KeyError, TypeError, ValueError):
            problems.append("evidence_version is incomplete")

    if valid_offsets and end <= len(text):
        if span.get("span_sha256") != text_sha256(text[start:end]):
            problems.append("span fingerprint mismatch")

    status, item_id = span.get("link_status"), span.get("agenda_item_db_id")
    if status == LINK_BOUND:
        if not isinstance(item_id, int) or isinstance(item_id, bool) or item_id <= 0:
            problems.append("bound spans require one positive agenda_item_db_id")
    elif status in (LINK_UNLINKED, LINK_AMBIGUOUS):
        if item_id is not None:
            problems.append("held spans may not carry an agenda_item_db_id")
    else:
        problems.append("link_status is not registered")
    return problems


def _disposition(problems: Sequence[str], link_status: Any) -> str:
    joined = " ".join(problems)
    if "retained text" in joined:
        return "hold_missing_text"
    if "fingerprint" in joined:
        return "hold_fingerprint_mismatch"
    if "evidence_version is required" in joined or "evidence_version is incomplete" in joined:
        return "hold_missing_evidence_version"
    if "lineage" in joined:
        return "hold_missing_lineage"
    if problems:
        return "hold_invalid_offsets"
    if link_status == LINK_AMBIGUOUS:
        return "hold_ambiguous_container"
    return "hold_unlinked_container"


def classify_spans(spans: Sequence[Mapping[str, Any]],
                   current_texts: Mapping[int, str | None]) -> dict[str, Any]:
    """Partition every proposal exactly once into accepted or one named hold."""
    accepted: list[dict[str, Any]] = []
    holds: list[dict[str, Any]] = []
    seen: set[tuple[int, str, int, int]] = set()
    for index, span in enumerate(spans):
        try:
            identity = span_identity(span)
        except (KeyError, TypeError, ValueError):
            holds.append({"index": index, "disposition": "hold_invalid_offsets"})
            continue
        if identity in seen:
            holds.append({"index": index, "identity": list(identity),
                          "disposition": "hold_duplicate_identity"})
            continue
        seen.add(identity)
        problems = validate_span(span, current_texts.get(identity[0]))
        if problems or span.get("link_status") != LINK_BOUND:
            holds.append({"index": index, "identity": list(identity),
                          "disposition": _disposition(problems, span.get("link_status")),
                          "problems": problems})
        else:
            accepted.append(dict(span))
    return {
        "proposed": len(spans),
        "accepted": accepted,
        "holds": holds,
        "accepted_count": len(accepted),
        "hold_count": len(holds),
        "reconciles": len(spans) == len(accepted) + len(holds),
    }


def accounting(classification: Mapping[str, Any]) -> dict[str, Any]:
    by_disposition = {name: 0 for name in HOLD_DISPOSITIONS}
    for hold in classification.get("holds") or []:
        name = str(hold["disposition"])
        by_disposition[name] = by_disposition.get(name, 0) + 1
    proposed = int(classification.get("proposed", 0))
    accepted = int(classification.get("accepted_count", 0))
    held = int(classification.get("hold_count", 0))
    return {
        "proposed": proposed,
        "would_insert": accepted,
        "held": held,
        "holds_by_disposition": by_disposition,
        "reconciles": proposed == accepted + held,
    }


def build_dry_plan(*, classification: Mapping[str, Any], created_at: str,
                   target: Mapping[str, Any], code_hashes: Mapping[str, str]) -> dict[str, Any]:
    operations = sorted(classification.get("accepted") or [], key=span_identity)
    plan = {
        "kind": "kg-stage3-b3-span-dry-plan",
        "version": PRODUCER_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "applied": False,
        "write_path": "absent by design",
        "target": dict(target),
        "producer": {"module": "scripts/kg/stage3_b3_span_contract.py",
                     "version": PRODUCER_VERSION, "code_hashes": dict(code_hashes)},
        "accounting": accounting(classification),
        "operations": operations,
        "operations_sha256": canonical_sha256(operations),
    }
    plan["digest"] = canonical_sha256(plan)
    return plan


def replay_check(*, plan: Mapping[str, Any],
                 existing: Mapping[tuple[int, str, int, int], str]) -> dict[str, Any]:
    missing: list[tuple[int, str, int, int]] = []
    conflicts: list[tuple[int, str, int, int]] = []
    for span in plan.get("operations") or []:
        identity = span_identity(span)
        if identity not in existing:
            missing.append(identity)
        elif existing[identity] != span.get("span_sha256"):
            conflicts.append(identity)
    return {
        "planned": len(plan.get("operations") or []),
        "would_write": len(missing),
        "missing": missing,
        "conflicts": conflicts,
        "is_noop": not missing and not conflicts,
    }
