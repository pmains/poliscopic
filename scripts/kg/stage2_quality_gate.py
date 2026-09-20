#!/usr/bin/env python3
"""``stage2_quality_gate.py`` — what would close Stage 2, and how it is proved.

This module defines the **post-apply quality gate** and the **immutable receipt**
that a Stage 2 closeout would have to produce.  It has no write path: it evaluates
observations and validates artifacts.

Every criterion carries three things, because a criterion without them is a
slogan:

* an **explicit denominator** — a rate is meaningless unless the population it is
  a rate *of* is named, so the eligible document denominator is computed here
  rather than left implicit;
* a **pass condition** that is a comparison, not an adjective;
* a **failure direction** — what it means when it does not pass.

The gate distinguishes **explained exceptions** from **failures**.  A meeting-level
document, a placeholder source key, an ambiguous item number and a duplicate
numbering collision are all *recorded* outcomes with reasons; they are not
defects.  An orphan, a cycle, a drift, or a missing denominator is a failure and
stops the closeout.
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

__all__ = [
    "EXCEPTION_CLASSES",
    "GATE_CRITERIA",
    "GATE_KIND",
    "GATE_VERSION",
    "RECEIPT_KIND",
    "RECEIPT_VERSION",
    "container_coverage",
    "eligible_documents",
    "evaluate_gate",
    "receipt_schema",
    "validate_gate",
    "validate_receipt",
]

GATE_KIND = "kg-stage2-quality-gate"
GATE_VERSION = "kg-stage2-quality-gate/1.0"
RECEIPT_KIND = "kg-stage2-closeout-receipt"
RECEIPT_VERSION = "kg-stage2-closeout-receipt/1.0"

#: The document classes that are NOT eligible for an agenda-item link, and why.
#: They are excluded from the denominator rather than counted as failures: a
#: meeting-scoped document has no item to name, and a placeholder key carries no
#: identity to resolve.
INELIGIBLE_FOR_ITEM_LINK = {
    "meeting_level_only": "the document is scoped to the meeting, not to an item",
    "unassigned_placeholder": "the source key is empty or the literal '0'; there is "
                              "no identity to resolve",
}

#: Outcomes that are explained exceptions rather than failures, with their reason.
EXCEPTION_CLASSES = {
    "meeting_level_only": "meeting-scoped document; correctly has no item link",
    "unassigned_placeholder": "placeholder/empty source key; no identity to resolve",
    "gap_missing_target": "the item the document names does not exist yet; the repair "
                          "plan materialises it",
    "held_ambiguous": "the item number matches several candidates; held",
    "collision_held": "two items claim one normalised number in a meeting; the "
                      "identity is ambiguous, so both are held",
    "ambiguous_numbering": "the identifier is not hierarchical (22C, sec-N, a bare "
                           "letter); no parent is inferred",
    "invalid_numbering": "no parseable number at all",
    "event_meeting_level": "the event is attached to a meeting, not to an item",
    "event_ineligible": "the event has no eligible agenda-item evidence",
    "s1_hold_phoenix_gp": "the phoenix-gp stream names no inferable public body",
    "s1_hold_skip_sentinel": "the __skip__ sentinel is not a body",
}

#: The criteria, in the order a closeout would evaluate them.  ``kind`` is
#: ``"exact"`` (a count or set equality) or ``"rate"`` (a threshold with an
#: explicit denominator).  A criterion marked ``proven_post_apply`` **cannot** be
#: passed before an apply happens: a replay that has nothing to replay proves
#: nothing, and parity that has never been compared proves nothing.  Marking them
#: is what stops a dry gate from reporting a vacuous pass as a real one.
GATE_CRITERIA = (
    {
        "id": "G1-container-coverage",
        "statement": "100% of eligible registry records map to exactly one canonical "
                     "container",
        "kind": "rate",
        "numerator": "containers_with_a_canonical_parent",
        "denominator": "containers_eligible_for_a_parent",
        "pass_condition": "numerator == denominator",
        "on_failure": "an unmapped eligible container is a blocking failure",
    },
    {
        "id": "G2-containment-integrity",
        "statement": "zero containment orphans and zero containment cycles",
        "kind": "exact",
        "fields": ("orphan_count", "cycle_count"),
        "pass_condition": "orphan_count == 0 and cycle_count == 0",
        "on_failure": "an orphan or a cycle is a blocking failure",
    },
    {
        "id": "G3-deterministic-item-links",
        "statement": "deterministic agenda-item/document links >= 99.5% of the "
                     "ELIGIBLE document population",
        "kind": "rate",
        "numerator": "deterministic_links",
        "denominator": "eligible_documents",
        "threshold": 0.995,
        "pass_condition": "deterministic_links / eligible_documents >= 0.995",
        "on_failure": "falling below the threshold on the eligible denominator is a "
                      "blocking failure; the denominator is never widened to pass",
    },
    {
        "id": "G4-explained-remainder",
        "statement": "every document not deterministically linked carries an "
                     "explained reason code",
        "kind": "exact",
        "fields": ("unexplained_remainder",),
        "pass_condition": "unexplained_remainder == 0",
        "on_failure": "an unexplained document is a blocking failure",
    },
    {
        "id": "G5-no-collateral-drift",
        "statement": "protected counts and integrity metrics are unchanged except "
                     "for the intended columns",
        "kind": "exact",
        "fields": ("drifted_protected_counts", "drifted_integrity_metrics"),
        "pass_condition": "both drift sets are empty",
        "on_failure": "any drift outside the intended change is a blocking failure",
    },
    {
        "id": "G6-schema-readiness",
        "statement": "the additive columns exist, are nullable, and their foreign "
                     "keys are validated",
        "kind": "exact",
        "fields": ("columns_present", "columns_validated"),
        "pass_condition": "every planned column is present and its FK is validated",
        "on_failure": "an absent or unvalidated column is a blocking failure",
    },
    {
        "id": "G7-replay-idempotence",
        "statement": "replaying an applied plan is a no-op with writes == 0",
        "kind": "exact",
        "fields": ("replay_writes",),
        "pass_condition": "replay_writes == 0",
        "on_failure": "a replay that writes is a blocking failure",
        "proven_post_apply": True,
    },
    {
        "id": "G8-parity",
        "statement": "development and production schemas agree for every touched "
                     "table",
        "kind": "exact",
        "fields": ("parity_differences",),
        "pass_condition": "parity_differences == []",
        "on_failure": "a schema divergence is a blocking failure",
        "proven_post_apply": True,
    },
)

#: Every field a closeout receipt must carry.  The schema is validated, not assumed.
RECEIPT_FIELDS = (
    "kind", "version", "created_at", "target", "plan_digests", "gate", "counts",
    "denominators", "exceptions", "approvals", "commit_status", "applied_at",
    "receipt_digest",
)

COMMIT_STATUSES = ("committed", "rolled-back")


def eligible_documents(counts: Mapping[str, Any]) -> int:
    """The explicit denominator for the deterministic-link rate.

    Eligible = every document that *could* carry an agenda-item link.  The
    meeting-scoped documents and the placeholder-keyed documents are removed,
    because neither class has an item to resolve to.  Removing them is a statement
    about the population, not a way to raise the rate: the same two classes are
    reported as explained exceptions with their own counts.
    """
    total = int(counts.get("total_documents", 0))
    excluded = sum(int(counts.get(name, 0)) for name in INELIGIBLE_FOR_ITEM_LINK)
    return total - excluded


def container_coverage(state: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical container coverage: parented containers over eligible containers.

    A *container* is a meeting or an agenda item.  A container is *eligible* for a
    canonical parent when its registry record names a body the plan could resolve;
    a document stream that names no public body is held, not counted as missing.
    """
    eligible = int(state.get("containers_eligible_for_a_parent", 0))
    parented = int(state.get("containers_with_a_canonical_parent", 0))
    return {"eligible": eligible, "parented": parented,
            "unparented": eligible - parented,
            "rate": (parented / eligible) if eligible else None,
            "met": eligible > 0 and parented == eligible}


def _criterion_result(criterion: Mapping[str, Any],
                      observed: Mapping[str, Any]) -> dict[str, Any]:
    """Evaluate one criterion against the observed state.  Never guesses."""
    cid = criterion["id"]
    result: dict[str, Any] = {"id": cid, "statement": criterion["statement"],
                              "pass_condition": criterion["pass_condition"],
                              "on_failure": criterion["on_failure"],
                              "observed": {}, "passed": False, "blocking": True,
                              "status": "failed",
                              "proven_post_apply": bool(criterion.get("proven_post_apply"))}
    if criterion.get("proven_post_apply") and not observed.get("applied"):
        # Nothing has been applied, so this cannot be proven yet.  It is NOT a pass:
        # reporting "0 writes" for a replay that never ran would close a gate on a
        # measurement nobody took.
        result["status"] = "pending-post-apply"
        result["observed"] = {field: None for field in criterion.get("fields", ())}
        result["failure"] = (f"{cid}: only provable after an apply; the dry gate "
                             f"records it as pending rather than passed")
        return result
    if criterion["kind"] == "rate":
        numerator = int(observed.get(criterion["numerator"], 0))
        denominator = int(observed.get(criterion["denominator"], 0))
        rate = (numerator / denominator) if denominator else None
        result["observed"] = {criterion["numerator"]: numerator,
                              criterion["denominator"]: denominator,
                              "rate": rate}
        if not denominator:
            result["failure"] = (
                f"{cid}: the denominator {criterion['denominator']!r} is zero or "
                f"absent, so no rate can be asserted")
            return result
        if "threshold" in criterion:
            result["passed"] = rate is not None and rate >= criterion["threshold"]
        else:
            result["passed"] = numerator == denominator
        result["status"] = "passed" if result["passed"] else "failed"
        if not result["passed"] and "threshold" in criterion:
            result["failure"] = (
                f"{cid}: {rate:.6f} is below the threshold {criterion['threshold']}")
        return result
    values = {field: observed.get(field) for field in criterion["fields"]}
    result["observed"] = values
    result["passed"] = _exact_pass(cid, values)
    result["status"] = "passed" if result["passed"] else "failed"
    if not result["passed"]:
        result["failure"] = f"{cid}: {values}"
    return result


def _exact_pass(cid: str, values: Mapping[str, Any]) -> bool:
    if cid == "G2-containment-integrity":
        return values.get("orphan_count") == 0 and values.get("cycle_count") == 0
    if cid == "G4-explained-remainder":
        return values.get("unexplained_remainder") == 0
    if cid == "G5-no-collateral-drift":
        return (not values.get("drifted_protected_counts")
                and not values.get("drifted_integrity_metrics"))
    if cid == "G6-schema-readiness":
        planned = values.get("columns_present")
        validated = values.get("columns_validated")
        return bool(planned) and set(planned) == set(validated or [])
    if cid == "G7-replay-idempotence":
        return values.get("replay_writes") == 0
    if cid == "G8-parity":
        return values.get("parity_differences") == []
    return False


def evaluate_gate(
    observed: Mapping[str, Any], *,
    approvals: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Evaluate every criterion.  A gate is closed only when all of them pass."""
    results = [_criterion_result(c, observed) for c in GATE_CRITERIA]
    failed = [r["id"] for r in results if not r["passed"]]
    return {
        "kind": GATE_KIND,
        "version": GATE_VERSION,
        "applied": bool(observed.get("applied")),
        "criteria": results,
        "failed": failed,
        "pending_post_apply": [r["id"] for r in results
                               if r["status"] == "pending-post-apply"],
        "blocking_failures": [r["id"] for r in results if not r["passed"] and r["blocking"]],
        "closed": not failed,
        "approvals": [dict(a) for a in approvals],
        "observed": dict(observed),
        "write_path": "absent by design",
    }


def validate_gate(gate: Mapping[str, Any]) -> list[str]:
    """Structural checks, plus a recomputation of every criterion from ``observed``."""
    problems: list[str] = []
    if gate.get("kind") != GATE_KIND:
        problems.append(f"kind must be {GATE_KIND!r}")
    if gate.get("version") != GATE_VERSION:
        problems.append(f"version must be {GATE_VERSION!r}")
    if gate.get("write_path") != "absent by design":
        problems.append("the gate must declare no write path")
    if gate.get("applied") and gate.get("pending_post_apply"):
        problems.append("an applied gate cannot still have post-apply-pending criteria")
    if not gate.get("applied") and not gate.get("pending_post_apply"):
        problems.append("a dry gate must record which criteria are only provable "
                        "after an apply")
    criteria = gate.get("criteria") or []
    ids = [c.get("id") for c in criteria]
    if ids != [c["id"] for c in GATE_CRITERIA]:
        problems.append("the criteria are not the full declared set, in order")
    observed = gate.get("observed") or {}
    if observed:
        recomputed = evaluate_gate(observed)
        for left, right in zip(criteria, recomputed["criteria"]):
            if left.get("passed") != right.get("passed"):
                problems.append(f"{left.get('id')}: the recorded result is not the "
                                f"recomputed one")
        if gate.get("failed") != recomputed["failed"]:
            problems.append("the recorded failures are not the recomputed ones")
        if gate.get("closed") != recomputed["closed"]:
            problems.append("the recorded closure is not the recomputed one")
    else:
        problems.append("the gate records no observed state to recompute from")
    return problems


def receipt_schema() -> dict[str, Any]:
    """The immutable closeout receipt schema.  Defined here, produced elsewhere."""
    return {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "required": list(RECEIPT_FIELDS),
        "commit_statuses": list(COMMIT_STATUSES),
        "immutability": "written once with O_CREAT|O_EXCL and mode 0600; never "
                        "overwritten; superseded by a recorded obsolete sidecar",
        "fields": {
            "target": "dialect, host, port, database, tier - the exact target",
            "plan_digests": "every applied plan's path, digest and replay digest",
            "gate": "the evaluated gate, with the explicit denominators",
            "counts": "before and after protected counts",
            "denominators": "the eligible document denominator and its exclusions",
            "exceptions": "every explained exception with its count and reason",
            "approvals": "who approved each apply, and when",
            "commit_status": "committed | rolled-back",
            "receipt_digest": "the receipt's own canonical digest",
        },
        "write_path": "absent by design",
    }


def build_receipt(*, created_at: str, target: Mapping[str, Any],
                  plan_digests: Sequence[Mapping[str, Any]],
                  gate: Mapping[str, Any], counts: Mapping[str, Any],
                  denominators: Mapping[str, Any],
                  exceptions: Mapping[str, Any],
                  approvals: Sequence[Mapping[str, Any]],
                  commit_status: str = "committed") -> dict[str, Any]:
    """Assemble a closeout receipt.  Refuses a receipt that proves nothing."""
    if commit_status not in COMMIT_STATUSES:
        raise ValueError(f"commit_status {commit_status!r} is not registered")
    if not plan_digests:
        raise ValueError("a closeout receipt must name the plans it applied")
    if not approvals:
        raise ValueError("a closeout receipt must name who approved the applies")
    if not gate.get("closed"):
        raise ValueError("a closeout receipt requires a closed gate")
    receipt = {
        "kind": RECEIPT_KIND, "version": RECEIPT_VERSION, "created_at": created_at,
        "target": {f: target.get(f) for f in
                   ("dialect", "host", "port", "database", "tier")},
        "plan_digests": [dict(p) for p in plan_digests],
        "gate": dict(gate),
        "counts": dict(counts),
        "denominators": dict(denominators),
        "exceptions": dict(exceptions),
        "approvals": [dict(a) for a in approvals],
        "commit_status": commit_status,
        "applied_at": created_at,
    }
    receipt["receipt_digest"] = artifacts.compute_digest(receipt)
    problems = validate_receipt(receipt)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return receipt


def validate_receipt(receipt: Mapping[str, Any]) -> list[str]:
    """A receipt must carry every field, a closed gate, and be self-consistent."""
    problems: list[str] = []
    if receipt.get("kind") != RECEIPT_KIND:
        problems.append(f"kind must be {RECEIPT_KIND!r}")
    if receipt.get("version") != RECEIPT_VERSION:
        problems.append(f"version must be {RECEIPT_VERSION!r}")
    for field in RECEIPT_FIELDS:
        if receipt.get(field) is None:
            problems.append(f"the receipt omits {field!r}")
    if receipt.get("commit_status") not in COMMIT_STATUSES:
        problems.append("the receipt carries an unregistered commit status")
    if not receipt.get("plan_digests"):
        problems.append("the receipt names no applied plans")
    if not receipt.get("approvals"):
        problems.append("the receipt names no approvals")
    for listed in receipt.get("plan_digests") or []:
        for field in ("path", "digest", "replay_digest"):
            if not listed.get(field):
                problems.append(f"a plan entry omits {field!r}")
    gate = receipt.get("gate") or {}
    problems.extend(validate_gate(gate))
    if not gate.get("closed"):
        problems.append("the receipt records an open gate")
    denominators = receipt.get("denominators") or {}
    if not denominators.get("eligible_documents"):
        problems.append("the receipt binds no eligible document denominator")
    recorded = receipt.get("receipt_digest")
    body = {k: v for k, v in receipt.items() if k != "receipt_digest"}
    if recorded != artifacts.compute_digest(body):
        problems.append("the recorded receipt digest is not the artifact's canonical digest")
    return problems
