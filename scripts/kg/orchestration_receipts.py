#!/usr/bin/env python3
"""Orchestration-level ontology-emission receipt enforcement (Brief 018 Step 4).

Every phase declared in :mod:`scripts.kg.producer_coverage` with
``requires_receipt=True`` must return exactly one validation receipt from its run
function, and the orchestrator must refuse the phase when it cannot trust that
receipt.

This module owns only the checks that are *specific to the orchestration
boundary* - receipt presence, count, shape, producer identity, dry/live
semantics, rejection permission, and agreement with the producer's own reported
accounting.  Value/row equation reconciliation, registry and model-version
binding, sealed state, quarantine/prohibited-value screening and dry-run
mutation checks are delegated to the canonical
:func:`scripts.kg.emission_receipts.reconcile_receipts`.  There is deliberately
**no second validator** here.

Enforcement is applied unconditionally by the orchestrator, so it cannot be
skipped by a caller: a phase without a trustworthy receipt fails the run gate.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from scripts.kg import registries as r
from scripts.kg import phase_envelope
from scripts.kg.emission_receipts import reconcile_receipts
from scripts.kg.producer_coverage import PRODUCER_COVERAGE
from scripts.kg.producer_versions import (
    declared_component_versions,
    declared_producer_version,
    version_declaration_problems,
)

__all__ = [
    "RECEIPT_KEY",
    "enforce_phase_receipt",
    "enforce_run_receipts",
    "receipt_accounting_disagreements",
]

#: Where a phase's run function reports its receipt.
RECEIPT_KEY = "validation_receipt"

#: Receipt fields that must be present for the payload to be well-formed.
_REQUIRED_RECEIPT_KEYS = ("producer", "producer_version", "model_version",
                          "registry_snapshot", "state", "values", "rows")

#: Producer-reported stat key -> location inside the canonical receipt.
_ACCOUNTING_KEYS: tuple[tuple[str, str, str], ...] = (
    ("rows_committed", "rows", "committed"),
    ("rows_rolled_back", "rows", "rolled_back"),
    ("rows_proposed", "rows", "proposed"),
    ("values_attempted", "values", "attempted"),
    ("values_rejected", "values", "rejected"),
)


def receipt_accounting_disagreements(raw_result: Mapping[str, Any] | None,
                                     receipt: Mapping[str, Any]) -> list[str]:
    """Reasons the producer's own reported accounting disagrees with its receipt.

    Only keys the producer actually reports are compared, so a producer that
    reports nothing here is not penalised - the receipt equations still apply.
    """
    if not isinstance(raw_result, Mapping):
        return []
    stats = raw_result.get("stats")
    if not isinstance(stats, Mapping):
        stats = raw_result
    problems: list[str] = []
    for stat_key, section, field in _ACCOUNTING_KEYS:
        if stat_key not in stats:
            continue
        reported = int(stats[stat_key] or 0)
        recorded = int((receipt.get(section) or {}).get(field) or 0)
        if reported != recorded:
            problems.append(
                f"receipt/accounting disagreement: {stat_key} reported {reported} "
                f"but receipt.{section}.{field} is {recorded}"
            )
    return problems


def enforce_phase_receipt(
    phase_name: str,
    *,
    raw_result: Mapping[str, Any] | None,
    dry_run: bool,
    producer_version: str | None = None,
    allowed_rejections: int = 0,
    expected_model_version: str | None = None,
    expected_registry_snapshot: str | None = None,
) -> dict[str, Any]:
    """Decide whether one phase's receipt may be trusted.

    Returns ``{"ok": bool, "reasons": [...], "checks": [...], "receipt": ...}``.
    Fail-closed: anything unexpected is a refusal, and a refusal always fails the
    orchestrator's gate.
    """
    coverage = PRODUCER_COVERAGE.get(phase_name)
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    def record(name: str, ok: bool, detail: str) -> None:
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            reasons.append(f"{phase_name}: {detail}")

    if coverage is None:
        record("receipt_coverage", False, "phase has no producer coverage entry")
        return {"ok": False, "reasons": reasons, "checks": checks, "receipt": None}

    if not coverage.requires_receipt:
        checks.append({"check": "receipt_required", "ok": True,
                       "detail": "exempt producer: emits no ontology-bearing values"})
        return {"ok": True, "reasons": [], "checks": checks, "receipt": None}

    if not isinstance(raw_result, Mapping):
        record("receipt_present", False, "phase returned no result payload")
        return {"ok": False, "reasons": reasons, "checks": checks, "receipt": None}

    payload = raw_result.get(RECEIPT_KEY)
    if payload is None:
        record("receipt_present", False, "missing validation receipt")
        return {"ok": False, "reasons": reasons, "checks": checks, "receipt": None}

    # Exactly one receipt is required: a partial or multiple payload is refused.
    if isinstance(payload, (list, tuple)):
        if len(payload) != 1:
            record("receipt_singular", False,
                   f"expected exactly one receipt, got {len(payload)}")
            return {"ok": False, "reasons": reasons, "checks": checks, "receipt": None}
        payload = payload[0]
        record("receipt_singular", True, "a single receipt was supplied in a sequence")
    if not isinstance(payload, Mapping):
        record("receipt_well_formed", False,
               f"receipt is {type(payload).__name__}, not an object")
        return {"ok": False, "reasons": reasons, "checks": checks, "receipt": None}

    if phase_envelope.is_envelope(payload):
        # A multi-domain phase exposes one envelope.  Each component receipt is
        # reconciled independently through the same canonical authority, and the
        # components' value/row domains are never summed.
        problems = phase_envelope.envelope_problems(
            payload,
            producer=phase_name,
            declared_version=(producer_version
                              or declared_producer_version(phase_name)),
            dry_run=dry_run,
            model_version=expected_model_version or r.MODEL_VERSION,
            registry_snapshot=expected_registry_snapshot or r.snapshot_sha256(),
            component_versions=declared_component_versions(phase_name),
            reconcile=reconcile_receipts,
            allowed_rejections=allowed_rejections,
        )
        record("phase_envelope", not problems,
               "phase envelope reconciles with every component" if not problems
               else "; ".join(problems))
        return {"ok": not problems, "reasons": reasons, "checks": checks,
                "receipt": payload}

    missing = [key for key in _REQUIRED_RECEIPT_KEYS if key not in payload]
    if missing:
        record("receipt_well_formed", False, f"receipt is missing fields {missing}")
        return {"ok": False, "reasons": reasons, "checks": checks, "receipt": dict(payload)}
    record("receipt_well_formed", True, "all required receipt fields present")

    # Producer identity must match the phase that returned it.
    claimed = str(payload.get("producer") or "")
    record("producer_identity", claimed == phase_name,
           f"receipt producer {claimed!r} does not match phase {phase_name!r}")

    # The version check is bound to the *declared* version, resolved from the
    # authoritative registry when the caller supplies none.  A producer that
    # declares no version fails closed: a non-empty but unchecked string proves
    # nothing.
    declared = producer_version or declared_producer_version(phase_name)
    if declared is None:
        record("producer_version", False,
               f"producer {phase_name!r} has no declared version in "
               "producer_versions.PRODUCER_VERSIONS")
    elif not str(payload.get("producer_version") or "").strip():
        record("producer_version", False, "receipt carries no producer version")
    elif str(payload.get("producer_version")) != declared:
        record("producer_version", False,
               f"receipt producer_version {payload.get('producer_version')!r} != "
               f"declared {declared!r}")
    else:
        record("producer_version", True,
               f"producer version matches declared {declared!r}")

    if not str(payload.get("model_version") or "").strip():
        record("model_version_present", False, "receipt carries no model version")
    else:
        record("model_version_present", True, "model version present")

    # Dry/live semantics must agree with the run actually being performed.
    if bool(payload.get("dry_run")) != bool(dry_run):
        record("dry_run_semantics", False,
               f"receipt dry_run={bool(payload.get('dry_run'))} but the run is "
               f"dry_run={bool(dry_run)}")
    else:
        record("dry_run_semantics", True, f"dry_run={bool(dry_run)} agrees")

    # Rejections are refused unless explicitly permitted.
    rejected = int((payload.get("values") or {}).get("rejected") or 0)
    if rejected > allowed_rejections:
        record("rejections_permitted", False,
               f"{rejected} rejected value(s) exceed the permitted "
               f"{allowed_rejections}")
    else:
        record("rejections_permitted", True, f"{rejected} rejected value(s) permitted")

    # Canonical reconciliation: equations, registry/model binding, sealed state,
    # failure, quarantine/prohibited values, dry-run mutation.
    canonical = reconcile_receipts(
        [payload],
        expected_producers=[phase_name],
        model_version=expected_model_version or r.MODEL_VERSION,
        registry_snapshot=expected_registry_snapshot or r.snapshot_sha256(),
    )
    record("canonical_reconciliation", not canonical,
           "canonical receipt reconciliation passed" if not canonical
           else "; ".join(canonical))

    disagreements = receipt_accounting_disagreements(raw_result, payload)
    record("receipt_accounting_agreement", not disagreements,
           "receipt agrees with the producer's reported accounting" if not disagreements
           else "; ".join(disagreements))

    return {"ok": not reasons, "reasons": reasons, "checks": checks,
            "receipt": dict(payload)}


def enforce_run_receipts(
    phase_entries: Sequence[Mapping[str, Any]],
    *,
    dry_run: bool,
    expected_phases: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Combined enforcement across the whole run.

    Requires every receipt-bearing phase to appear exactly once and to have
    passed, so a phase that silently vanishes cannot pass the orchestration gate.
    """
    required = (tuple(expected_phases) if expected_phases is not None else tuple(sorted(
        name for name, item in PRODUCER_COVERAGE.items() if item.requires_receipt)))
    ran = [str(entry.get("name")) for entry in phase_entries
           if entry.get("status") not in ("skipped",)]
    excused = {str(entry.get("name")) for entry in phase_entries
               if entry.get("status") == "skipped"}
    seen = ran
    reasons: list[str] = []
    checks: list[dict[str, Any]] = []

    missing = sorted(set(required) - set(seen) - excused)
    if missing:
        reasons.append(f"phases missing from the run: {missing}")
    duplicates = sorted({n for n in seen if seen.count(n) > 1})
    if duplicates:
        reasons.append(f"phases reported more than once: {duplicates}")
    uncovered = sorted(set(seen) - set(PRODUCER_COVERAGE))
    if uncovered:
        reasons.append(f"phases with no coverage entry: {uncovered}")
    checks.append({
        "check": "six_phase_coverage",
        "ok": not (missing or duplicates or uncovered),
        "detail": (f"all {len(required)} receipt-bearing phase(s) selected for this "
                   f"run present exactly once"
                   if not (missing or duplicates or uncovered)
                   else f"missing={missing} duplicates={duplicates} uncovered={uncovered}"),
    })

    declaration_problems = version_declaration_problems()
    checks.append({
        "check": "version_declarations",
        "ok": not declaration_problems,
        "detail": ("every receipt-bearing producer declares exactly one version"
                   if not declaration_problems
                   else "; ".join(declaration_problems)),
    })
    if declaration_problems:
        reasons.append(
            f"producer version declarations are incomplete: {declaration_problems}"
        )

    failed = sorted(entry.get("name") for entry in phase_entries
                    if entry.get("receipt_ok") is False)
    checks.append({
        "check": "phase_receipts_enforced",
        "ok": not failed,
        "detail": "every phase receipt was trusted" if not failed
        else f"phase receipts refused: {failed}",
    })
    if failed:
        reasons.append(f"phase receipts refused: {failed}")

    return {"ok": not reasons, "reasons": reasons, "checks": checks,
            "required_phases": list(required), "phases_seen": seen}
