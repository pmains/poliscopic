#!/usr/bin/env python3
"""``stage2_event_attachment.py`` — event → agenda-item attachment, analysis only.

An event says something happened ("Approved", "Discussed").  Attaching it to an
agenda item means claiming *which* item it happened to.  That claim needs
evidence, and there are exactly two kinds this module will accept:

* **source-supported**: the event's supporting document already carries a
  canonical agenda item — the document itself is item-specific;
* **exact identifier**: the event's ``case_number`` equals, in the same meeting,
  the ``case_number`` of exactly one canonical agenda item.

Co-occurrence is never evidence.  Two events appearing next to an item in one
document does not attach either of them, and document order is never consulted.

Everything else is accounted for, not dropped: an event whose document is
meeting-level is **meeting-level context**; a case number matching several items
is **ambiguous**; an event with no resolvable document or meeting is
**missing-evidence**; a procedural event (being called to order) is
**ineligible**, because it cannot belong to an item whatever else is true.

This module builds the baseline and a plan, and writes nothing.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402

__all__ = [
    "BASELINE_KIND",
    "CLASSES",
    "CLASS_AMBIGUOUS",
    "CLASS_DETERMINISTIC",
    "CLASS_INELIGIBLE",
    "CLASS_MEETING_LEVEL",
    "CLASS_MISSING",
    "PLAN_KIND",
    "PROCEDURAL_OUTCOMES",
    "audit",
    "build_plan",
    "classify_event",
    "plan_digest",
    "replay_digest",
    "validate_plan",
]

BASELINE_KIND = "kg-stage2-event-attachment-baseline"
PLAN_KIND = "kg-stage2-event-attachment-plan"
PLAN_VERSION = "kg-stage2-event-attachment-plan/1.0"

CLASS_DETERMINISTIC = "deterministic_link"
CLASS_MEETING_LEVEL = "meeting_level"
CLASS_AMBIGUOUS = "ambiguous"
CLASS_MISSING = "missing_evidence"
CLASS_INELIGIBLE = "ineligible"
CLASSES = (CLASS_DETERMINISTIC, CLASS_MEETING_LEVEL, CLASS_AMBIGUOUS,
           CLASS_MISSING, CLASS_INELIGIBLE)

#: Outcomes that cannot belong to an agenda item at all.
PROCEDURAL_OUTCOMES = frozenset({"called_to_order"})

#: Document classes that already mean "this document belongs to an item".
DOCUMENT_ITEM_CLASSES = frozenset({"attached_item_number", "attached_source_key",
                                   "attached_key_match"})


def classify_event(
    event: Mapping[str, Any],
    *,
    document: Mapping[str, Any] | None,
    document_class: str | None,
    document_item: int | None,
    case_item_ids: Sequence[int],
    meeting_db_id: int | None,
) -> dict[str, Any]:
    """One event's class, reason and evidence.  Pure.

    The order matters.  Ineligibility is decided first because a procedural event
    is ineligible however good the rest of its evidence is; missing evidence is
    decided before meeting-level because it is a stronger statement about what is
    absent.
    """
    outcome = str(event.get("outcome") or "")
    event_id = int(event["id"])
    case_number = str(event.get("case_number") or "").strip()

    if outcome in PROCEDURAL_OUTCOMES:
        return {
            "event_id": event_id, "class": CLASS_INELIGIBLE,
            "reason": f"procedural outcome {outcome!r} cannot belong to an agenda item",
            "item": None, "evidence": None,
        }

    if document is None or meeting_db_id is None:
        return {
            "event_id": event_id, "class": CLASS_MISSING,
            "reason": ("no resolvable supporting document" if document is None
                       else "no resolvable meeting"),
            "item": None, "evidence": None,
        }

    if document_item is not None and (document_class or "") in DOCUMENT_ITEM_CLASSES:
        return {
            "event_id": event_id, "class": CLASS_DETERMINISTIC,
            "reason": "the supporting document carries a canonical agenda item",
            "item": int(document_item),
            "evidence": {
                "kind": "source_supported_document",
                "supporting_doc_id": int(event["supporting_doc_id"]),
                "document_class": document_class,
                "document_item": int(document_item),
            },
        }

    if case_number:
        resolved = sorted({int(i) for i in case_item_ids})
        if len(resolved) == 1:
            return {
                "event_id": event_id, "class": CLASS_DETERMINISTIC,
                "reason": "case number matches exactly one canonical agenda item",
                "item": resolved[0],
                "evidence": {
                    "kind": "exact_case_number",
                    "case_number": case_number,
                    "item_count": 1,
                    "document_class": document_class,
                },
            }
        if len(resolved) > 1:
            return {
                "event_id": event_id, "class": CLASS_AMBIGUOUS,
                "reason": f"case number {case_number!r} matches {len(resolved)} items "
                          f"in this meeting",
                "item": None,
                "evidence": {"kind": "ambiguous_case_number",
                             "case_number": case_number, "item_count": len(resolved)},
            }

    if (document_class or "") in ("held_ambiguous", "gap_missing_target"):
        return {
            "event_id": event_id, "class": CLASS_AMBIGUOUS,
            "reason": f"the supporting document is held as {document_class!r} and may "
                      f"resolve to an item later",
            "item": None,
            "evidence": {"kind": "held_document", "document_class": document_class},
        }

    return {
        "event_id": event_id, "class": CLASS_MEETING_LEVEL,
        "reason": "the supporting document is meeting-level and nothing names an item "
                  + (f"(case number {case_number!r} matches no item in this meeting)"
                     if case_number else "(no case number)"),
        "item": None,
        "evidence": {"kind": "meeting_level_document",
                     "document_class": document_class,
                     "case_number": case_number or None},
    }


def audit(
    events: Sequence[Mapping[str, Any]],
    *,
    documents: Mapping[int, Mapping[str, Any]],
    document_classes: Mapping[int, str],
    document_items: Mapping[int, int],
    meeting_db_id_by_event: Mapping[int, int],
    case_items: Mapping[tuple[int, str], Sequence[int]],
    body_by_event: Mapping[int, str],
) -> dict[str, Any]:
    """The read-only baseline: every event classified exactly once."""
    rows: list[dict[str, Any]] = []
    for event in events:
        event_id = int(event["id"])
        doc_id = int(event["supporting_doc_id"])
        meeting = meeting_db_id_by_event.get(event_id)
        case_number = str(event.get("case_number") or "").strip()
        verdict = classify_event(
            event,
            document=documents.get(doc_id),
            document_class=document_classes.get(doc_id),
            document_item=document_items.get(doc_id),
            case_item_ids=case_items.get((meeting, case_number), ()) if case_number else (),
            meeting_db_id=meeting,
        )
        verdict["body"] = body_by_event.get(event_id)
        rows.append(verdict)

    counts = {name: 0 for name in CLASSES}
    for row in rows:
        counts[row["class"]] += 1
    by_body: dict[str, dict[str, int]] = {}
    for row in rows:
        bucket = by_body.setdefault(str(row["body"]), {name: 0 for name in CLASSES})
        bucket[row["class"]] += 1

    reasons: dict[str, int] = {}
    for row in rows:
        if row["class"] != CLASS_DETERMINISTIC:
            reasons[row["reason"]] = reasons.get(row["reason"], 0) + 1

    ids = sorted(row["event_id"] for row in rows)
    return {
        "kind": BASELINE_KIND,
        "version": "kg-stage2-event-attachment-baseline/1.0",
        "counts": counts,
        "total": len(rows),
        "population": {
            "count": len(ids),
            "event_ids_sha256": binding.canonical_sha256(ids),
            "mutually_exclusive": True,
            "reconciles": sum(counts.values()) == len(rows) == len(set(ids)),
        },
        "by_body": {k: dict(sorted(v.items())) for k, v in sorted(by_body.items())},
        "reasons": dict(sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0]))),
        "rows": rows,
    }


def plan_digest(plan: Mapping[str, Any]) -> str:
    return artifacts.compute_digest(plan)


def replay_digest(plan: Mapping[str, Any]) -> str:
    excluded = (artifacts.DIGEST_FIELD, "replay_digest", "created_at")
    return binding.canonical_sha256({k: v for k, v in plan.items() if k not in excluded})


def build_plan(
    baseline: Mapping[str, Any],
    *,
    decisions: Sequence[Mapping[str, Any]],
    created_at: str,
    target: Mapping[str, Any],
    plan_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Only high-confidence deterministic attachments become operations."""
    rows = [r for r in baseline["rows"] if r["class"] == CLASS_DETERMINISTIC]
    bindings = binding.build_bindings(
        holds=[{"id": r["event_id"], "meeting_db_id": 0, "item_number": ""}
               for r in baseline["rows"]],
        decisions=decisions, target=target, plan_dir=plan_dir)
    ids = sorted(int(r["event_id"]) for r in baseline["rows"])

    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "bindings": bindings,
        "operations": [{
            "action": "attach",
            "event_id": int(r["event_id"]),
            "agenda_item_db_id": int(r["item"]),
            "evidence": r["evidence"],
            "evidence_sha256": binding.canonical_sha256(r["evidence"]),
            "identity": {"strategy": "UPDATE meeting_events SET agenda_item_id "
                                     "WHERE id = <event_id> AND agenda_item_id IS NULL",
                         "natural_key": [int(r["event_id"])],
                         "rollback_owner": "apply receipt listed event ids only"},
        } for r in rows],
        "counts": dict(baseline["counts"]),
        "population": baseline["population"],
        "accounting": {
            "baseline_population_sha256": baseline["population"]["event_ids_sha256"],
            "classified_event_ids_sha256": binding.canonical_sha256(ids),
            "set_equality": ids == sorted(int(r["event_id"]) for r in baseline["rows"]),
            "operations": len(rows),
            "unattached": len(baseline["rows"]) - len(rows),
        },
        "policy": {
            "cooccurrence_is_not_evidence": True,
            "document_order_is_never_consulted": True,
            "only_high_confidence_deterministic_attachments": True,
        },
        "collision_policy": {
            "rule": "an event that already carries an agenda_item_id is never re-attached",
            "locking": "the check and the update share one transaction; the check takes "
                       "SELECT ... FOR UPDATE on the event rows first",
        },
        "replay": {
            "no_stability_claim": True,
            "statement": "applying changes the database; the artifact digest is historical",
            "post_apply_state": "every operation's event carries its agenda_item_id",
            "on_post_apply_run": "receipt-bound no-op with writes=0, or a refusal; never "
                                 "a re-attachment",
        },
        "rollback": {
            "ownership": "only event ids recorded in the apply receipt",
            "never": "never clear agenda_item_id for events the receipt does not own",
            "restore": "exact preimage values, never recomputed ones",
            "reversible": True,
        },
        "preconditions": {"read_only_snapshot": True, "no_database_write_by_this_plan": True},
        "write_path": "absent by design",
        "applied": False,
        "promoted": False,
    }
    plan["replay_digest"] = replay_digest(plan)
    plan[artifacts.DIGEST_FIELD] = plan_digest(plan)
    problems = validate_plan(plan, baseline=baseline)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return plan


def validate_plan(plan: Mapping[str, Any],
                  *, baseline: Mapping[str, Any] | None = None) -> list[str]:
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("mode") != "dry-run":
        problems.append("the plan must be dry-run only")
    if plan.get("applied") is not False or plan.get("promoted") is not False:
        problems.append("a plan must record applied=false and promoted=false")
    if plan.get("write_path") != "absent by design":
        problems.append("the plan must declare no write path")

    bindings = plan.get("bindings") or {}
    for key in ("lineage", "decisions", "target", "code_hashes"):
        if not bindings.get(key):
            problems.append(f"binding {key!r} is missing")

    counts = plan.get("counts") or {}
    if sorted(counts) != sorted(CLASSES):
        problems.append("the counts do not cover every class")
    if sum(counts.values()) != (plan.get("population") or {}).get("count"):
        problems.append("the class counts do not reconcile to the population")

    accounting = plan.get("accounting") or {}
    if accounting.get("set_equality") is not True:
        problems.append("the classified population does not equal the baseline population")
    if accounting.get("baseline_population_sha256") != accounting.get("classified_event_ids_sha256"):
        problems.append("the classified population digest does not match the baseline")
    if accounting.get("operations") != len(plan.get("operations") or []):
        problems.append("the operation count does not match the operations")
    if accounting.get("unattached", 0) + accounting.get("operations", 0) != \
            (plan.get("population") or {}).get("count"):
        problems.append("operations plus unattached does not reconcile")

    for op in plan.get("operations") or []:
        if not op.get("agenda_item_db_id"):
            problems.append(f"event {op.get('event_id')}: attach without an item")
        if not (op.get("evidence") or {}).get("kind"):
            problems.append(f"event {op.get('event_id')}: attach without evidence")
        if not op.get("evidence_sha256"):
            problems.append(f"event {op.get('event_id')}: attach without an evidence hash")

    if baseline is not None:
        want = sorted(int(r["event_id"]) for r in baseline["rows"]
                      if r["class"] == CLASS_DETERMINISTIC)
        got = sorted(int(o["event_id"]) for o in plan.get("operations") or [])
        if want != got:
            problems.append("the operations do not equal the deterministic population")

    rollback = plan.get("rollback") or {}
    if "never" not in rollback or "does not own" not in str(rollback.get("never", "")):
        problems.append("rollback does not restrict ownership to the receipt")
    if rollback.get("restore") != "exact preimage values, never recomputed ones":
        problems.append("rollback does not require exact preimages")
    if (plan.get("replay") or {}).get("no_stability_claim") is not True:
        problems.append("replay must not claim the artifact digest stays valid")
    if not plan.get("replay_digest"):
        problems.append("replay_digest is missing")
    if plan.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
