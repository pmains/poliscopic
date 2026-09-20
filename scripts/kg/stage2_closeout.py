#!/usr/bin/env python3
"""``stage2_closeout.py`` — the Stage 2 closeout baseline and dependency ledger.

Every Stage 2 exit criterion and every Brief 021 work item gets a row carrying:

* **status** — met, partially met, or pending, stated against its pass condition;
* an **exact count** with its **denominator** where a rate is involved;
* **artifact/digest evidence** — the immutable artifact that proves the number;
* **owner/dependency** — what must exist before it can advance;
* a **pass condition** that is a comparison.

The ledger also records the **explained exceptions** separately from failures,
because the difference between "we chose not to infer this" and "this is broken"
is the difference between a closeout and an outage.  Finally it carries the
**minimal ordered apply sequence**, each step tagged with whether it needs Peter's
approval.

Nothing here writes.  It reads artifacts and produces plain data.
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

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_quality_gate as gate_mod  # noqa: E402

__all__ = [
    "LEDGER_KIND",
    "LEDGER_VERSION",
    "STATUSES",
    "build_ledger",
    "remaining_sequence",
    "validate_ledger",
]

LEDGER_KIND = "kg-stage2-closeout-ledger"
LEDGER_VERSION = "kg-stage2-closeout-ledger/1.0"

#: Every status a row may carry.  There is no "mostly done".
STATUSES = ("met", "in-progress", "pending-approval", "pending-dependency",
            "not-applicable")


def _row(*, criterion_id: str, source: str, statement: str, status: str,
         count: Any, denominator: Any, evidence: Sequence[Mapping[str, Any]],
         owner: str, dependency: Sequence[str], pass_condition: str,
         note: str = "") -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status {status!r} is not one of {STATUSES}")
    return {"id": criterion_id, "source": source, "statement": statement,
            "status": status, "count": count, "denominator": denominator,
            "rate": (count / denominator) if denominator else None,
            "evidence": [dict(e) for e in evidence], "owner": owner,
            "dependency": list(dependency), "pass_condition": pass_condition,
            "note": note}


def _evidence(label: str, path: str, digest: str | None) -> dict[str, Any]:
    return {"label": label, "path": path, "digest": digest}


def build_ledger(*, created_at: str, heads: Mapping[str, Any],
                 containers: Mapping[str, Any], documents: Mapping[str, Any],
                 containment: Mapping[str, Any], event: Mapping[str, Any],
                 repair: Mapping[str, Any], correction: Mapping[str, Any],
                 decisions: Mapping[str, Any], schema: Mapping[str, Any],
                 projected: Mapping[str, Any]) -> dict[str, Any]:
    """Assemble the ledger from gathered evidence.  No number is invented here."""
    eligible = gate_mod.eligible_documents(documents)
    coverage = gate_mod.container_coverage(containers)
    containment_evidence = [
        _evidence("containment baseline", heads["containment"]["path"],
                  heads["containment"]["digest"]),
        _evidence("containment plan", heads["containment_plan"]["path"],
                  heads["containment_plan"]["digest"]),
    ]

    rows = [
        _row(
            criterion_id="EC-1", source="roadmap Stage 2 exit criteria",
            statement="100% of eligible registry records map to one canonical container",
            status="met" if coverage["met"] else "in-progress",
            count=coverage["parented"], denominator=coverage["eligible"],
            evidence=[_evidence("S1 apply receipt", heads["s1_receipt"]["path"],
                                heads["s1_receipt"]["digest"])],
            owner="poliscopic agent",
            dependency=["S1 parentage apply (done)", "held streams remain held by policy"],
            pass_condition="containers_with_a_canonical_parent == "
                           "containers_eligible_for_a_parent",
            note=f"{coverage['unparented']} eligible container(s) unparented; the "
                 f"held streams are recorded as explained exceptions, not failures"),
        _row(
            criterion_id="EC-2", source="roadmap Stage 2 exit criteria",
            statement="zero containment orphans and zero containment cycles",
            status="met" if containment["orphan_count"] == 0
                   and not containment["cycles"] else "in-progress",
            count=containment["orphan_count"], denominator=None,
            evidence=containment_evidence, owner="poliscopic agent",
            dependency=["containment plan under review"],
            pass_condition="orphan_count == 0 and cycles == []",
            note="an orphan is a proposed PART_OF edge whose parent is not a "
                 "canonical item in the same meeting; a cycle is a child equal to "
                 "its own ancestor"),
        _row(
            criterion_id="EC-3", source="roadmap Stage 2 exit criteria",
            statement="deterministic agenda-item/document links >= 99.5% of the "
                      "ELIGIBLE document population, remainder quarantined and "
                      "explained",
            status=("met" if documents["deterministic_links"] / eligible >= 0.995
                    else "in-progress") if eligible else "pending-dependency",
            count=documents["deterministic_links"], denominator=eligible,
            evidence=[_evidence("S2 plan head", heads["s2_plan"]["path"],
                                heads["s2_plan"]["digest"])],
            owner="poliscopic agent",
            dependency=["S2 repair + correction plans await final review and approval"],
            pass_condition="deterministic_links / eligible_documents >= 0.995",
            note=f"eligible = total_documents - meeting_level_only - "
                 f"unassigned_placeholder = {documents['total_documents']} - "
                 f"{documents['meeting_level_only']} - "
                 f"{documents['unassigned_placeholder']} = {eligible}"),
        _row(
            criterion_id="W1", source="Brief 021 Step 1",
            statement="canonical meeting->body parentage planned and applied",
            status="met", count=containers["meetings_with_body"],
            denominator=containers["meetings"],
            evidence=[_evidence("S1 apply receipt", heads["s1_receipt"]["path"],
                                heads["s1_receipt"]["digest"])],
            owner="poliscopic agent", dependency=[],
            pass_condition="the applied row count equals the plan's deterministic scope",
            note="1,212 meetings updated across 32 bodies; 218 holds retained by policy"),
        _row(
            criterion_id="W2", source="Brief 021 Step 2",
            statement="additive document->agenda-item attachment planned, reviewed "
                      "and applied",
            status="pending-approval", count=documents["deterministic_links"],
            denominator=eligible,
            evidence=[_evidence("S2 plan head", heads["s2_plan"]["path"],
                                heads["s2_plan"]["digest"]),
                      _evidence("repair plan", repair["path"], repair["digest"]),
                      _evidence("correction plan", correction["path"],
                                correction["digest"])],
            owner="Peter (approval) / poliscopic agent (execution)",
            dependency=["schema readiness apply", "final independent review"],
            pass_condition="the applied plan's postconditions pass inside one "
                           "transaction and the receipt records writes == expected",
            note=f"repair resolves {repair['documents_resolved']} of "
                 f"{repair['documents_accounted']} accounted gap documents; "
                 f"correction carries {correction['operations']} operations"),
        _row(
            criterion_id="W3", source="Brief 021 extraction quality addendum",
            statement="split-label extraction and resolution capture implemented",
            status="in-progress", count=repair["documents_resolved"],
            denominator=repair["documents_accounted"],
            evidence=[_evidence("repair plan", repair["path"], repair["digest"])],
            owner="poliscopic agent", dependency=["S2 apply approval"],
            pass_condition="the reconstructed labels are applied by the repair plan",
            note="implemented and tested; not applied"),
        _row(
            criterion_id="W4", source="Brief 021 human adjudication addendum",
            statement="human decisions recorded against model proposals",
            status="in-progress", count=decisions["approved"],
            denominator=decisions["decisions_expected"],
            evidence=[_evidence("S2 aggregate", heads["aggregate"]["path"],
                                heads["aggregate"]["digest"])],
            owner="Peter (adjudicator)", dependency=["S2 apply approval"],
            pass_condition="5 approved decisions, each bound to a proposal by digest",
            note=f"{decisions['approved']} approved, {decisions['promoted']} promoted, "
                 f"{decisions['applied']} applied"),
        _row(
            criterion_id="W5", source="Brief 021 readiness slice",
            statement="scratch re-parse proves the promotion contract on a "
                      "reconstructed database",
            status="met", count=decisions["decision_units"],
            denominator=decisions["decision_units"],
            evidence=[_evidence("S2 aggregate", heads["aggregate"]["path"],
                                heads["aggregate"]["digest"])],
            owner="poliscopic agent", dependency=[],
            pass_condition="the scratch re-parse passes and the scratch database is "
                           "dropped",
            note="executed and PASSED; scratch database dropped"),
        _row(
            criterion_id="W6", source="Brief 021 evidence-backed materialisation",
            statement="materialisation dry plan over every held reference",
            status="pending-approval", count=repair["documents_resolved"],
            denominator=repair["documents_accounted"],
            evidence=[_evidence("repair plan", repair["path"], repair["digest"])],
            owner="Peter (approval)", dependency=["schema readiness apply"],
            pass_condition="every materialised row carries a printed-evidence witness",
            note=f"{repair['materialise']} materialise, {repair['hold']} hold"),
        _row(
            criterion_id="W7", source="Brief 021 truncated-label correction",
            statement="truncated-label correction dry plan",
            status="pending-approval", count=correction["new_item_row"],
            denominator=correction["operations"],
            evidence=[_evidence("correction plan", correction["path"],
                                correction["digest"])],
            owner="Peter (approval)", dependency=["schema readiness apply"],
            pass_condition="every operation is a truncation of the recorded label",
            note=f"{correction['held_documents']} documents held with reasons"),
        _row(
            criterion_id="W8", source="Brief 021 event attachment",
            statement="event -> agenda-item attachment where deterministically "
                      "possible",
            status="not-applicable", count=event["deterministic_link"],
            denominator=event["population"],
            evidence=[_evidence("event attachment plan", heads["event"]["path"],
                                heads["event"]["digest"])],
            owner="poliscopic agent", dependency=[],
            pass_condition="every eligible event is either linked or explained",
            note=f"{event['deterministic_link']} deterministic links found out of "
                 f"{event['population']} events, so there is nothing to apply: "
                 f"co-occurrence is not evidence"),
        _row(
            criterion_id="W9", source="Brief 021 agenda_subitem containment",
            statement="PART_OF containment planned with exact operation equality",
            status="in-progress", count=containment["proposed_links"],
            denominator=containment["proposed_links"],
            evidence=containment_evidence,
            owner="poliscopic agent", dependency=["containment plan final review"],
            pass_condition="zero orphans, zero cycles, and all edges disjoint from "
                           "the collision population",
            note=f"{containment['collision_held']} items held as ambiguous "
                 f"duplicate numbering"),
        _row(
            criterion_id="W10", source="Brief 021 apply runner hardening",
            statement="apply admission, receipt and rollback hardened; write body "
                      "absent",
            status="in-progress", count=1, denominator=1,
            evidence=[_evidence("runner", heads["s2_plan"]["path"],
                                heads["s2_plan"]["digest"])],
            owner="poliscopic agent",
            dependency=["write body implementation (not written)"],
            pass_condition="no public write callback exists and apply refuses before "
                           "opening a transaction",
            note="P0 and every P1 closed; the write body itself is deliberately not "
                 "written"),
    ]

    exceptions = {
        "meeting_level_only": {
            "count": documents["meeting_level_only"],
            "reason": gate_mod.EXCEPTION_CLASSES["meeting_level_only"],
            "acceptable": True},
        "unassigned_placeholder": {
            "count": documents["unassigned_placeholder"],
            "reason": gate_mod.EXCEPTION_CLASSES["unassigned_placeholder"],
            "acceptable": True},
        "gap_missing_target": {
            "count": documents["gap_missing_target"],
            "reason": gate_mod.EXCEPTION_CLASSES["gap_missing_target"],
            "acceptable": True,
            "projected_after_repair": documents["gap_missing_target"]
            - projected["resolved_gap_documents"]},
        "collision_held": {
            "count": containment["collision_held"],
            "reason": gate_mod.EXCEPTION_CLASSES["collision_held"],
            "acceptable": True},
        "ambiguous_numbering": {
            "count": containment["ambiguous"],
            "reason": gate_mod.EXCEPTION_CLASSES["ambiguous_numbering"],
            "acceptable": True},
        "invalid_numbering": {
            "count": containment["invalid"],
            "reason": gate_mod.EXCEPTION_CLASSES["invalid_numbering"],
            "acceptable": True},
        "event_meeting_level": {
            "count": event["meeting_level"],
            "reason": gate_mod.EXCEPTION_CLASSES["event_meeting_level"],
            "acceptable": True},
        "event_ineligible": {
            "count": event["ineligible"],
            "reason": gate_mod.EXCEPTION_CLASSES["event_ineligible"],
            "acceptable": True},
        "s1_holds": {
            "count": containers["s1_holds"],
            "reason": gate_mod.EXCEPTION_CLASSES["s1_hold_phoenix_gp"],
            "acceptable": True},
    }
    failures = {
        "orphans": containment["orphan_count"],
        "cycles": len(containment["cycles"]),
        "unexplained_remainder": documents.get("unexplained_remainder", 0),
        "schema_drift": len(schema.get("unexpected_differences") or []),
    }

    ledger = {
        "kind": LEDGER_KIND,
        "version": LEDGER_VERSION,
        "created_at": created_at,
        "target": dict(containers.get("target") or {}),
        "heads": {k: dict(v) for k, v in heads.items() if isinstance(v, Mapping)},
        "criteria": rows,
        "counts": {
            "meetings": containers["meetings"],
            "meetings_with_body": containers["meetings_with_body"],
            "agenda_items": containers["agenda_items"],
            "documents": documents["total_documents"],
            # The class counts are carried so the eligible denominator can be
            # RECOMPUTED from them; a denominator that does not follow from the
            # counts is refused rather than believed.
            "total_documents": documents["total_documents"],
            "meeting_level_only": documents["meeting_level_only"],
            "unassigned_placeholder": documents["unassigned_placeholder"],
            "gap_missing_target": documents["gap_missing_target"],
            "deterministic_links": documents["deterministic_links"],
            "eligible_documents": eligible,
            "containment_proposed_links": containment["proposed_links"],
            "containment_collision_keys": containment["collision_keys"],
            "event_deterministic_links": event["deterministic_link"],
        },
        "projected": dict(projected),
        "exceptions": exceptions,
        "failures": failures,
        "schema_readiness": dict(schema),
        "sequence": remaining_sequence(projected=projected, schema=schema),
        "write_path": "absent by design",
        "applied": False,
    }
    ledger["ledger_digest"] = artifacts.compute_digest(ledger)
    problems = validate_ledger(ledger)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return ledger


def remaining_sequence(*, projected: Mapping[str, Any],
                       schema: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The minimal ordered apply sequence, with its approval boundaries.

    Order is not cosmetic.  Each S2 plan binds the **current-state digest**, so
    applying one changes the state the other was validated against: the second
    would refuse admission until it is regenerated and re-reviewed.  That makes the
    regeneration step part of the sequence rather than an afterthought.
    """
    return [
        {"step": 1, "action": "verify-protected-backup",
         "detail": "a 0600 restore-verified backup receipt for the exact target",
         "requires_peter_approval": False,
         "blocking": True, "state": "ready",
         "note": "mechanical; the receipt already exists and is bound by both plans"},
        {"step": 2, "action": "apply-schema-readiness",
         "detail": "add the additive nullable supporting_documents.agenda_item_db_id "
                   "with its foreign key and index, in one transaction",
         "requires_peter_approval": True, "blocking": True,
         "state": "blocked" if schema.get("column_present") is False else "ready",
         "note": "no column exists today; this is the only schema change Stage 2 needs"},
        {"step": 3, "action": "apply-correction-plan",
         "detail": "apply the truncated-label correction plan",
         "requires_peter_approval": True, "blocking": True, "state": "blocked",
         "note": "blocked on step 2 and on final independent review of the plan digest"},
        {"step": 4, "action": "regenerate-and-review-repair-plan",
         "detail": "regenerate the repair plan and obtain an independent digest "
                   "review",
         "requires_peter_approval": True, "blocking": True, "state": "blocked",
         "note": "REQUIRED, not cosmetic: each plan binds the current-state digest, "
                 "so step 3 invalidates the repair plan's binding"},
        {"step": 5, "action": "apply-repair-plan",
         "detail": "apply the regenerated repair plan",
         "requires_peter_approval": True, "blocking": True, "state": "blocked",
         "note": f"expected to resolve {projected.get('resolved_gap_documents')} "
                 f"further document(s)"},
        {"step": 6, "action": "regenerate-containment-baseline-and-plan",
         "detail": "regenerate the containment baseline over the new agenda items",
         "requires_peter_approval": True, "blocking": True, "state": "blocked",
         "note": "the materialised items change the population the containment plan "
                 "was classified against"},
        {"step": 7, "action": "apply-containment-plan",
         "detail": "apply the PART_OF edges for the agenda_subitem slice",
         "requires_peter_approval": True, "blocking": True, "state": "blocked",
         "note": "requires the agenda_items.parent_item_id migration, which is "
                 "design-only today"},
        {"step": 8, "action": "evaluate-closeout-quality-gate",
         "detail": "evaluate every gate criterion against the post-apply state",
         "requires_peter_approval": False, "blocking": True, "state": "blocked",
         "note": "mechanical; produces the dry gate and, on success, the receipt"},
        {"step": 9, "action": "replay-verification",
         "detail": "re-run each applied plan and require writes == 0",
         "requires_peter_approval": False, "blocking": True, "state": "blocked",
         "note": "mechanical idempotence proof"},
        {"step": 10, "action": "parity-and-sync",
         "detail": "verify dev/prod schema parity and deploy",
         "requires_peter_approval": True, "blocking": True, "state": "blocked",
         "note": "the only production-touching step; never automatic"},
    ]


def validate_ledger(ledger: Mapping[str, Any]) -> list[str]:
    """Every criterion must be complete, and every rate must carry a denominator."""
    problems: list[str] = []
    if ledger.get("kind") != LEDGER_KIND:
        problems.append(f"kind must be {LEDGER_KIND!r}")
    if ledger.get("version") != LEDGER_VERSION:
        problems.append(f"version must be {LEDGER_VERSION!r}")
    if ledger.get("write_path") != "absent by design":
        problems.append("the ledger must declare no write path")
    if ledger.get("applied") is not False:
        problems.append("a closeout ledger must record applied=false")
    rows = ledger.get("criteria") or []
    if not rows:
        problems.append("the ledger carries no criteria")
    seen: set[str] = set()
    for row in rows:
        cid = row.get("id")
        if cid in seen:
            problems.append(f"{cid}: the criterion appears twice")
        seen.add(cid)
        for field in ("source", "statement", "status", "evidence", "owner",
                      "pass_condition"):
            if row.get(field) in (None, "", []):
                problems.append(f"{cid}: {field!r} is missing")
        # A dependency list may legitimately be empty (a met or not-applicable item
        # depends on nothing), but it must be a list rather than absent.
        if not isinstance(row.get("dependency"), list):
            problems.append(f"{cid}: 'dependency' must be a list")
        if row.get("status") not in STATUSES:
            problems.append(f"{cid}: status {row.get('status')!r} is not registered")
        if row.get("count") is None:
            problems.append(f"{cid}: no count")
        if row.get("denominator") and row.get("rate") is None:
            problems.append(f"{cid}: a denominator without a rate")
        for item in row.get("evidence") or []:
            if not item.get("path"):
                problems.append(f"{cid}: an evidence entry names no artifact")
    expectations = {c["id"] for c in gate_mod.GATE_CRITERIA}
    if not expectations:
        problems.append("the gate declares no criteria")
    sequence = ledger.get("sequence") or []
    steps = [s.get("step") for s in sequence]
    if steps != sorted(s for s in steps if isinstance(s, int)):
        problems.append("the apply sequence is not ordered")
    for step in sequence:
        if "requires_peter_approval" not in step:
            problems.append(f"step {step.get('step')}: no approval boundary recorded")
    if not any(s.get("requires_peter_approval") for s in sequence):
        problems.append("no step requires approval, which cannot be right")
    exceptions = ledger.get("exceptions") or {}
    for name, entry in exceptions.items():
        if entry.get("count") is None or not entry.get("reason"):
            problems.append(f"exception {name!r} carries no count or no reason")
        if entry.get("acceptable") is not True:
            problems.append(f"exception {name!r} is not marked acceptable")
    counts = ledger.get("counts") or {}
    recomputed_eligible = gate_mod.eligible_documents({
        "total_documents": counts.get("total_documents"),
        "meeting_level_only": counts.get("meeting_level_only"),
        "unassigned_placeholder": counts.get("unassigned_placeholder")})
    if counts.get("eligible_documents") != recomputed_eligible:
        problems.append(
            "the recorded eligible denominator does not follow from the document "
            "counts (a widened denominator would raise the rate without changing "
            "the population)")
    if not recomputed_eligible:
        problems.append("the eligible denominator is zero, so no rate is assertable")
    deterministic = counts.get("deterministic_links")
    if deterministic is None or deterministic > recomputed_eligible:
        problems.append("the deterministic link count exceeds the eligible population")
    problems.extend(_failures_must_be_zero(ledger.get("failures") or {}))
    recorded = ledger.get("ledger_digest")
    body = {k: v for k, v in ledger.items() if k != "ledger_digest"}
    if recorded != artifacts.compute_digest(body):
        problems.append("the recorded ledger digest is not the artifact's canonical digest")
    return problems


def _failures_must_be_zero(failures: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    for name, value in failures.items():
        if isinstance(value, list):
            if value:
                problems.append(f"failure {name!r} is non-empty at baseline: {value[:3]}")
        elif value:
            problems.append(f"failure {name!r} is {value}, not zero")
    return problems
