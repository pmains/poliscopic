#!/usr/bin/env python3
"""Stage 3 processing dry-plan schema authority and immutable-plan validator.

This module owns the dry-plan vocabulary — the plan kind and producer version, the
outcome names, the accounting equation, the record invariants, the bound code set,
the evidence components, and the exact top-level component set.  The builder
conforms to it; the validator re-derives every derivable value from the plan body
and refuses the plan when anything disagrees.

Naming is a contract, not a convenience.  A dry row that has no exact canonical
stored receipt **was not processed**: it is ``planned`` (``would_process``), never
``success`` and never ``marked_processed``.  ``marked_processed`` /
``processing_proven`` / ``current_proven`` are reserved for identities that carry a
canonical stored success receipt.

The validator is offline.  It refuses digest, code, target, component, population,
accounting, and record tampering, obsolete or drifted evidence inputs, unknown
top-level components, and any plan that claims work a dry run cannot have done.
It does not contact a database; live current-state reconciliation belongs to the
builder's run.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.kg import stage3_processing_plan_inputs as inputs
from scripts.kg import stage3_processing_receipt as receipt

REPO = Path(__file__).resolve().parents[2]

PRODUCER_VERSION = "kg-stage3-processing-backfill/1.0"
PLAN_KIND = "kg-stage3-processing-dry-plan"
BUILDER_MODULE = "scripts/kg/stage3_processing_backfill.py"
OUTCOMES = ("planned", "replay", "failure", "held")
#: Only a canonical stored receipt proves processing; nothing else may claim it.
PROCESSED_OUTCOMES = ("replay",)
PLANNED_OUTCOME = "planned"
HELD_REASONS = (
    "source_stale_pending_text_refresh",
    "not_eligible",
    "receipt_identity_conflict",
    "receipt_invalid_fail_closed",
    "receipt_status_unregistered",
)
EVIDENCE_COMPONENTS = inputs.EVIDENCE_COMPONENTS
REQUIRED_COMPONENTS = ("producer", "identity_authority", "evidence_binding",
                       "current_state_validation", "selection_binding", "receipts_binding")
PLAN_KEYS = (
    "kind", "version", "created_at", "mode", "applied", "write_path",
    "processing_performed", "writes_performed", "target", "producer",
    "identity_authority", "identity_fields", "extractor", "extractor_version",
    "policy", "bound", "evidence_binding", "current_state_validation",
    "selection_binding", "receipts_binding", "receipt_fold", "accounting",
    "records", "records_sha256", "approval_boundary", "digest",
)
BOUND_KEYS = ("order", "offset", "limit", "population", "selected")
RECORD_DERIVED_KEYS = (
    "population", "selected", "bound", "planned", "replay", "failure", "held",
    "reconciles", "marked_processed", "marked_processed_among_failure_or_held",
    "processing_proven", "current_proven", "would_process", "retry_eligible",
    "by_reason", "holds_by_reason", "by_acquisition_provenance",
    "acquisition_reconciles", "legacy_swept_at_present",
)
RECEIPT_DERIVED_KEYS = ("stored_receipts", "receipts_folded", "receipts_replayed",
                        "receipt_conflicts", "receipts_invalid", "receipts_superseded")
CODE_MODULES = (
    BUILDER_MODULE,
    "scripts/kg/stage3_processing_receipt.py",
    "scripts/kg/stage3_processing_plan_inputs.py",
    "scripts/kg/stage3_processing_plan_validator.py",
    "scripts/kg/stage3_processing_identity.py",
    "scripts/kg/stage3_source_eligibility.py",
    "scripts/kg/producer_versions.py",
    "scripts/kg/stage2_artifacts.py",
)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    return {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
            for name in CODE_MODULES}


def plan_digest(plan: Mapping[str, Any]) -> str:
    return canonical_sha256({k: v for k, v in plan.items() if k != "digest"})


def expected_selected(bound: Mapping[str, Any]) -> int:
    population = int(bound.get("population") or 0)
    offset = int(bound.get("offset") or 0)
    limit = bound.get("limit")
    available = max(0, population - offset)
    return available if limit is None else max(0, min(int(limit), available))


def hold_class(reason: Any) -> str:
    text = str(reason or "")
    return "not_eligible" if text.startswith("not_eligible") else text


def record_accounting(records: Sequence[Mapping[str, Any]],
                      *, bound: Mapping[str, Any]) -> dict[str, Any]:
    """The accounting values derivable from the records alone."""
    counts = Counter({name: 0 for name in OUTCOMES})
    reasons: Counter = Counter()
    acquisition: Counter = Counter()
    holds = {name: 0 for name in HELD_REASONS}
    for record in records:
        outcome = str(record.get("outcome"))
        counts[outcome] += 1
        reasons[f"{outcome}:{record.get('reason')}"] += 1
        acquisition[str(record.get("acquisition_provenance"))] += 1
        if outcome == "held":
            holds[hold_class(record.get("reason"))] = holds.get(hold_class(record.get("reason"))) + 1
    selected = len(records)
    marked = [record for record in records if record.get("marked_processed")]
    return {
        "population": int(bound.get("population") or 0),
        "selected": selected,
        "bound": dict(bound),
        "planned": counts["planned"],
        "replay": counts["replay"],
        "failure": counts["failure"],
        "held": counts["held"],
        "reconciles": selected == sum(counts.values()) == int(bound.get("selected") or 0),
        "marked_processed": len(marked),
        "marked_processed_among_failure_or_held": sum(
            1 for record in records
            if record.get("outcome") in ("failure", "held") and record.get("marked_processed")),
        "processing_proven": counts["replay"],
        "current_proven": counts["replay"],
        "would_process": counts["planned"],
        "retry_eligible": counts["failure"],
        "by_reason": dict(sorted(reasons.items())),
        "holds_by_reason": dict(sorted(holds.items())),
        "by_acquisition_provenance": dict(sorted(acquisition.items())),
        "acquisition_reconciles": selected == sum(acquisition.values()),
        "legacy_swept_at_present": sum(
            1 for record in records if record.get("legacy_swept_at_present")),
    }


def accounting(records: Sequence[Mapping[str, Any]], *, bound: Mapping[str, Any],
               folded: Mapping[str, Any]) -> dict[str, Any]:
    """Full accounting: record-derived values plus the stored-receipt fold."""
    body = record_accounting(records, bound=bound)
    body["stored_receipts"] = dict(folded.get("by_status") or {})
    body["receipts_folded"] = int(folded.get("valid_receipts", 0))
    body["receipts_replayed"] = int(folded.get("replayed", 0))
    body["receipt_conflicts"] = len(folded.get("conflicts") or ())
    body["receipts_invalid"] = len(folded.get("invalid") or ())
    body["receipts_superseded"] = int(folded.get("superseded", 0))
    return body


def invariant_violations(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Fail-closed invariants over a reconciliation.  Empty means acceptable."""
    problems: list[str] = []
    for record in records:
        outcome = record.get("outcome")
        where = f"source_id={record.get('source_id')} outcome={outcome}"
        if outcome not in OUTCOMES:
            problems.append(f"unregistered outcome: {where}")
            continue
        if outcome in ("failure", "held") and record.get("marked_processed"):
            problems.append(f"a failed or held document is marked processed: {where}")
        if outcome == "replay" and not record.get("marked_processed"):
            problems.append(f"a proof-bearing receipt is not shown as processed: {where}")
        if outcome == "planned":
            if not record.get("would_process"):
                problems.append(f"a planned document is not marked would_process: {where}")
            if record.get("marked_processed") or record.get("processing_proven"):
                problems.append(f"an unprocessed document claims processing: {where}")
        if outcome in PROCESSED_OUTCOMES and not record.get("processing_proven"):
            problems.append(f"processing is claimed without an exact receipt: {where}")
        if outcome not in PROCESSED_OUTCOMES and record.get("processing_proven"):
            problems.append(f"processing is claimed without an exact receipt: {where}")
        if outcome == "replay" and record.get("receipt_status") != "success":
            problems.append(f"a replay does not carry a stored success receipt: {where}")
        if outcome in ("failure", "held") and not str(record.get("reason") or "").strip():
            problems.append(f"a failure or hold must name its reason: {where}")
        if outcome == "failure" and not record.get("retry_eligible"):
            problems.append(f"a failure must remain retry-eligible: {where}")
        if outcome != "failure" and record.get("retry_eligible"):
            problems.append(f"only a failure is retry-eligible: {where}")
        if record.get("acquisition_provenance") not in receipt.ACQUISITION_CLASSES:
            problems.append(f"acquisition provenance is not registered: {where}")
        if not isinstance(record.get("legacy_swept_at_present"), bool):
            problems.append(f"legacy_swept_at_present must be a boolean: {where}")
        identity = record.get("processing_identity")
        if not isinstance(identity, Sequence) or isinstance(identity, (str, bytes)) \
                or len(identity) != len(receipt.IDENTITY_FIELDS):
            problems.append(f"processing identity is malformed: {where}")
            continue
        if str(identity[0]) not in receipt.SOURCE_KINDS:
            problems.append(f"processing identity source kind is unsupported: {where}")
        if not isinstance(identity[1], int) or isinstance(identity[1], bool) or identity[1] <= 0:
            problems.append(f"processing identity source id is invalid: {where}")
        if str(identity[4]) != receipt.EXTRACTOR or \
                str(identity[5]) != receipt.EXTRACTOR_VERSION:
            problems.append(f"processing identity extractor/version is not current: {where}")
    return problems


def validate_plan(plan: Any, *, target: Mapping[str, Any] | None = None,
                  hashes: Mapping[str, str] | None = None,
                  repo: Path = REPO) -> list[str]:
    """Refuse any immutable dry plan that is not exactly what it claims to be."""
    if not isinstance(plan, Mapping):
        return ["plan must be an object"]
    problems: list[str] = []

    unknown = sorted(set(plan) - set(PLAN_KEYS))
    if unknown:
        problems.append(f"unregistered top-level components: {unknown}")
    missing = [key for key in PLAN_KEYS if key not in plan]
    if missing:
        problems.append(f"missing plan components: {missing}")
    if plan.get("kind") != PLAN_KIND:
        problems.append("plan kind is not the canonical dry-plan kind")
    if plan.get("version") != PRODUCER_VERSION:
        problems.append("plan version is not the canonical producer version")
    if plan.get("mode") != "dry-run" or plan.get("applied") is not False:
        problems.append("a dry plan must be unapplied")
    if plan.get("write_path") != "absent by design":
        problems.append("a dry plan must declare no write path")
    if plan.get("writes_performed") != 0 or plan.get("processing_performed") is not False:
        problems.append("a dry plan must report zero writes and no processing")

    recorded_digest = plan.get("digest")
    if not receipt._hex64(recorded_digest) or recorded_digest != plan_digest(plan):
        problems.append("plan digest does not match the plan body")

    producer = plan.get("producer")
    expected_hashes = dict(hashes) if hashes is not None else code_hashes(repo)
    if not isinstance(producer, Mapping):
        problems.append("producer component is missing")
    else:
        if producer.get("module") != BUILDER_MODULE:
            problems.append("producer module is not the canonical builder")
        if producer.get("version") != PRODUCER_VERSION:
            problems.append("producer version is not the canonical producer version")
        if dict(producer.get("code_hashes") or {}) != expected_hashes:
            problems.append("producer code hashes do not match the code on disk")

    problems.extend(receipt.identity_authority_problems(plan.get("identity_authority"), repo))
    if list(plan.get("identity_fields") or []) != list(receipt.IDENTITY_FIELDS):
        problems.append("identity fields are not the canonical processing identity")
    if plan.get("extractor") != receipt.EXTRACTOR or \
            plan.get("extractor_version") != receipt.EXTRACTOR_VERSION:
        problems.append("plan extractor/version is not the current declared extractor")

    if not isinstance(plan.get("target"), Mapping) or not plan.get("target"):
        problems.append("plan target is missing")
    elif target is not None and dict(plan["target"]) != dict(target):
        problems.append("plan target differs from the expected target")

    problems.extend(inputs.evidence_problems(plan.get("evidence_binding"),
                                             plan.get("target"), repo))
    for component in REQUIRED_COMPONENTS:
        if not isinstance(plan.get(component), Mapping):
            problems.append(f"required component is missing: {component}")
    problems.extend(inputs.current_state_problems(plan, repo))
    selection_artifact, selection_entries, selection_problems = inputs.load_selection(plan, repo)
    problems.extend(selection_problems)
    folded, receipt_problems = inputs.resolve_receipt_binding(plan.get("receipts_binding"), repo)
    problems.extend(receipt_problems)
    problems.extend(inputs.receipt_fold_problems(plan, folded))

    bound = plan.get("bound")
    records = plan.get("records")
    if not isinstance(bound, Mapping) or set(bound) != set(BOUND_KEYS):
        problems.append("bound block is not canonical")
        bound = bound if isinstance(bound, Mapping) else {}
    if bound.get("order") != "source_id_asc":
        problems.append("selection order is not the canonical order")
    if not isinstance(records, Sequence) or isinstance(records, (str, bytes)):
        problems.append("records must be a sequence")
        records = []
    records = list(records)
    if bound.get("selected") != len(records):
        problems.append("selected count does not match the record count")
    if bound.get("selected") != expected_selected(bound):
        problems.append("selected count is not what the bound implies")
    identities = [receipt.identity_key(list(record.get("processing_identity") or []))
                  for record in records]
    if len(set(identities)) != len(identities):
        problems.append("a document was reconciled more than once")
    ids = [record.get("source_id") for record in records]
    if ids != sorted(ids):
        problems.append("records are not in canonical source_id order")
    problems.extend(invariant_violations(records))
    problems.extend(inputs.rederive_record_problems(records, selection_entries, folded))
    if plan.get("records_sha256") != canonical_sha256(records):
        problems.append("records digest does not match the records")

    recorded_accounting = plan.get("accounting")
    if not isinstance(recorded_accounting, Mapping):
        problems.append("accounting block is missing")
    else:
        recomputed = record_accounting(records, bound=bound)
        for key in RECORD_DERIVED_KEYS:
            if recorded_accounting.get(key) != recomputed.get(key):
                problems.append(f"accounting {key} does not match the records")
        for key in RECEIPT_DERIVED_KEYS:
            if key not in recorded_accounting:
                problems.append(f"accounting {key} is missing")
        if recorded_accounting.get("reconciles") is not True:
            problems.append("accounting does not reconcile")
        if recorded_accounting.get("marked_processed_among_failure_or_held"):
            problems.append("a failed or held document is marked processed")

    boundary = plan.get("approval_boundary")
    if not isinstance(boundary, Mapping):
        problems.append("approval boundary is missing")
    else:
        if boundary.get("mutations_proposed") != 0:
            problems.append("a dry plan may not propose mutations")
        if not isinstance(boundary.get("not_authorized"), Sequence) or \
                isinstance(boundary.get("not_authorized"), (str, bytes)) or \
                not boundary.get("not_authorized"):
            problems.append("approval boundary must name what is not authorized")
    return problems
