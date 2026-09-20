"""Pure plan for evidence-based migration of qualified legacy outcomes."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from typing import Any, Iterable
from pathlib import Path

from scripts.entities.event_normalize_models import CandidateError, normalize_action_verb
from scripts.kg.registries.events import canonicalize_outcome
from scripts.kg.stage2_artifacts import write_immutable

PLAN_VERSION = "kg-stage3-qualified-outcome-migration/1.0"
QUALIFIED_BASE = "approved"
LEGACY_PROJECTION_SQL = """
SELECT e.id AS event_id, e.outcome AS legacy_outcome,
       NULL::text AS outcome_base, NULL::text AS outcome_qualifier,
       d.text_content AS document_text,
       x.id AS extraction_id, x.extractor, x.extractor_version,
       x.action_verb, x.text_offset_start, x.text_offset_end
FROM meeting_events e
JOIN supporting_documents d ON d.id = e.supporting_doc_id
LEFT JOIN meeting_event_extractions x ON x.meeting_event_id = e.id
WHERE e.outcome LIKE 'approved_%'
ORDER BY e.id, x.id
""".strip()
REPLAY_PROJECTION_SQL = LEGACY_PROJECTION_SQL.replace(
    "NULL::text AS outcome_base, NULL::text AS outcome_qualifier",
    "e.outcome_base, e.outcome_qualifier",
)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode()).hexdigest()


def _evidence(extraction: dict[str, Any], document_text: str) -> dict[str, Any]:
    result = {
        "extraction_id": extraction.get("id"),
        "extractor": extraction.get("extractor"),
        "extractor_version": extraction.get("extractor_version"),
        "action_verb": extraction.get("action_verb"),
        "text_offset_start": extraction.get("text_offset_start"),
        "text_offset_end": extraction.get("text_offset_end"),
        "valid": False,
        "reason": None,
    }
    try:
        start, end = int(result["text_offset_start"]), int(result["text_offset_end"])
    except (TypeError, ValueError):
        result["reason"] = "missing_or_invalid_offsets"
        return result
    if start < 0 or end <= start or end > len(document_text):
        result["reason"] = "offsets_out_of_bounds"
        return result
    action = str(result["action_verb"] or "")
    if document_text[start:end] != action:
        result["reason"] = "source_span_mismatch"
        return result
    try:
        event_type, outcome = normalize_action_verb(action)
    except CandidateError:
        result["reason"] = "unregistered_action_verb"
        return result
    if event_type != "approval" or outcome.base != QUALIFIED_BASE or not outcome.qualifier:
        result["reason"] = "not_qualified_approval_evidence"
        return result
    if outcome.qualifier == "subject_to":
        inline = re.fullmatch(
            r"approved\s+subject\s+to\s+(?:conditions|stipulations)", action, re.I
        )
        bounded_tail = document_text[end:min(len(document_text), end + 600)]
        following = re.match(
            r"\s*the\s+following\s+stipulations\s*:\s*"
            r"(?:\(?[a-z0-9]+\)?[.)])\s+\S",
            bounded_tail, re.I,
        )
        if not inline and not (
            re.fullmatch(r"approved\s+subject\s+to", action, re.I) and following
        ):
            result["reason"] = "subject_to_missing_governed_complement"
            return result
    context_start, context_end = max(0, start - 120), min(len(document_text), end + 120)
    result.update({"valid": True, "outcome_base": outcome.base,
                   "outcome_qualifier": outcome.qualifier,
                   "evidence_text_sha256": hashlib.sha256(action.encode()).hexdigest(),
                   "context_start": context_start, "context_end": context_end,
                   "context_sha256": hashlib.sha256(
                       document_text[context_start:context_end].encode()).hexdigest()})
    return result


def group_projection_rows(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group the ordered SQL projection without dropping independent evidence."""
    grouped: dict[int, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        event_id = int(row["event_id"])
        event = grouped.setdefault(event_id, {
            "event_id": event_id, "legacy_outcome": row.get("legacy_outcome"),
            "outcome_base": row.get("outcome_base"),
            "outcome_qualifier": row.get("outcome_qualifier"),
            "document_text": row.get("document_text"), "extractions": [],
        })
        stable = (event["legacy_outcome"], event["outcome_base"],
                  event["outcome_qualifier"], event["document_text"])
        observed = (row.get("legacy_outcome"), row.get("outcome_base"),
                    row.get("outcome_qualifier"), row.get("document_text"))
        if stable != observed:
            raise ValueError(f"incoherent repeated projection for event {event_id}")
        if row.get("extraction_id") is not None:
            event["extractions"].append({
                "id": row.get("extraction_id"), "extractor": row.get("extractor"),
                "extractor_version": row.get("extractor_version"),
                "action_verb": row.get("action_verb"),
                "text_offset_start": row.get("text_offset_start"),
                "text_offset_end": row.get("text_offset_end"),
            })
    return [grouped[key] for key in sorted(grouped)]


def classify_event(row: dict[str, Any]) -> dict[str, Any]:
    """Classify one legacy event exactly once without mutating it."""
    event_id = row.get("event_id")
    legacy = str(row.get("legacy_outcome") or "").strip()
    record: dict[str, Any] = {"event_id": event_id, "legacy_outcome": legacy}
    try:
        canonical = canonicalize_outcome(legacy)
    except Exception:
        return {**record, "disposition": "quarantine", "reason": "invalid_legacy_outcome",
                "evidence": []}
    if canonical.qualifier is None:
        return {**record, "disposition": "out_of_scope", "reason": "unqualified_outcome",
                "evidence": []}
    if canonical.base != QUALIFIED_BASE:
        return {**record, "disposition": "out_of_scope", "reason": "non_approval_qualifier",
                "evidence": []}

    current_base, current_qualifier = row.get("outcome_base"), row.get("outcome_qualifier")
    if (current_base is None) != (current_qualifier is None):
        return {**record, "disposition": "quarantine", "reason": "partial_existing_pair",
                "evidence": []}
    if current_base is not None:
        if (current_base, current_qualifier) == (canonical.base, canonical.qualifier):
            return {**record, "disposition": "replay", "reason": "canonical_pair_present",
                    "outcome_base": canonical.base, "outcome_qualifier": canonical.qualifier,
                    "evidence": []}
        return {**record, "disposition": "quarantine", "reason": "existing_pair_conflict",
                "evidence": []}

    document_text = str(row.get("document_text") or "")
    evidence = [_evidence(dict(item), document_text)
                for item in row.get("extractions", [])]
    valid = [item for item in evidence if item["valid"]]
    pairs = Counter((item["outcome_base"], item["outcome_qualifier"]) for item in valid)
    if not valid:
        return {**record, "disposition": "quarantine", "reason": "no_exact_qualified_evidence",
                "evidence": evidence}
    if len(pairs) != 1:
        return {**record, "disposition": "quarantine", "reason": "conflicting_qualified_evidence",
                "evidence": evidence}
    pair = next(iter(pairs))
    evidence_driven_collapse = (
        legacy == "approved_with_conditions"
        and pair[0] == "approved"
        and pair[1] in {"with_conditions", "with_stipulations", "subject_to"}
    )
    if pair != (canonical.base, canonical.qualifier) and not evidence_driven_collapse:
        return {**record, "disposition": "quarantine", "reason": "legacy_evidence_conflict",
                "evidence": evidence}
    precise_legacy = {
        "with_conditions": "approved_with_conditions",
        "with_stipulations": "approved_with_stipulations",
        "subject_to": "approved_subject_to",
        "as_amended": "approved_as_amended",
    }[pair[1]]
    return {**record, "disposition": "planned",
            "reason": "evidence_driven_legacy_collapse_remap" if evidence_driven_collapse
            and pair != (canonical.base, canonical.qualifier) else "exact_evidence_match",
            "normalized_legacy_outcome": precise_legacy,
            "outcome_base": pair[0], "outcome_qualifier": pair[1],
            "document_text_sha256": hashlib.sha256(document_text.encode()).hexdigest(),
            "evidence": evidence}


def build_plan(rows: Iterable[dict[str, Any]], *, target: str, schema_digest: str,
               code_digest: str = "") -> dict[str, Any]:
    if target != "poliscopic_dev":
        raise ValueError("qualified-outcome migration plans are development-only")
    source_rows = [dict(row) for row in rows]
    for source in source_rows:
        source["extractions"] = sorted(
            [dict(item) for item in source.get("extractions", [])],
            key=lambda item: int(item.get("id") or 0),
        )
    source_rows.sort(key=lambda value: int(value.get("event_id") or 0))
    records = [classify_event(row) for row in source_rows]
    ids = [record["event_id"] for record in records]
    if any(not isinstance(value, int) or value <= 0 for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("event ids must be unique positive integers")
    records.sort(key=lambda value: value["event_id"])
    accounting = dict(sorted(Counter(r["disposition"] for r in records).items()))
    body = {"version": PLAN_VERSION, "mode": "dry-run", "receipt_mode": "dry-run",
            "target": target,
            "schema_digest": schema_digest, "code_digest": code_digest,
            "records": records, "accounting": accounting,
            "source_population_digest": _digest(source_rows),
            "mutations_performed": 0}
    return {**body, "digest": _digest(body)}


def validate_plan(
    plan: dict[str, Any], source_rows: Iterable[dict[str, Any]] | None = None
) -> list[str]:
    problems = []
    body = {key: value for key, value in plan.items() if key != "digest"}
    if plan.get("version") != PLAN_VERSION or plan.get("mode") != "dry-run":
        problems.append("wrong plan version or mode")
    if plan.get("receipt_mode") != "dry-run":
        problems.append("records are not declared dry-run receipts")
    if plan.get("target") != "poliscopic_dev" or plan.get("mutations_performed") != 0:
        problems.append("plan is not a zero-mutation development dry run")
    if plan.get("digest") != _digest(body):
        problems.append("plan digest mismatch")
    records = plan.get("records") if isinstance(plan.get("records"), list) else []
    if list(records) != sorted(records, key=lambda value: value.get("event_id", 0)):
        problems.append("records are not in stable event order")
    if dict(sorted(Counter(r.get("disposition") for r in records).items())) != plan.get("accounting"):
        problems.append("accounting does not reconcile")
    for record in records:
        if record.get("disposition") == "planned":
            if not record.get("evidence") or any(not e.get("valid") for e in record["evidence"]):
                problems.append(f"planned event {record.get('event_id')} lacks exact evidence")
    if source_rows is not None:
        supplied = [dict(row) for row in source_rows]
        rebuilt = build_plan(
                supplied, target=str(plan.get("target")),
                schema_digest=str(plan.get("schema_digest")),
                code_digest=str(plan.get("code_digest") or ""),
        )
        if plan.get("source_population_digest") != rebuilt.get("source_population_digest"):
            problems.append("source population digest mismatch")
        elif rebuilt != plan:
            problems.append("plan does not rederive from bound source rows")
    return problems


def write_plan(
    path: str | Path, rows: Iterable[dict[str, Any]], *, target: str,
    schema_digest: str, code_digest: str,
) -> tuple[dict[str, Any], str]:
    """Build, independently validate, and atomically write one immutable plan."""
    source = [dict(row) for row in rows]
    plan = build_plan(source, target=target, schema_digest=schema_digest,
                      code_digest=code_digest)
    problems = validate_plan(plan, source)
    if problems:
        raise ValueError("invalid qualified-outcome plan: " + "; ".join(problems))
    return plan, write_immutable(path, plan)
