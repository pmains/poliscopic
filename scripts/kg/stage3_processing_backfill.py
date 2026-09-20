#!/usr/bin/env python3
"""Bounded DRY backfill mechanics over retained supporting-document text.

This module decides what a version-aware ``sweep_docs`` backfill *would* do for a
bounded, deterministically selected set of retained documents.  It never writes:
there is no apply entry point, ``write_path`` is absent by design, and the dry plan
is marked ``applied: false`` with ``writes_performed: 0``.

Every selected document reconciles into exactly one outcome:

* ``planned`` - eligible, no canonical receipt for its exact identity.  It was
  **not** processed and is not marked processed; a real run would process it;
* ``replay`` - the exact current identity already carries a canonical stored
  success receipt.  This is the only state in which processing is proven;
* ``failure`` - a canonical receipt for the exact identity reported failure.  The
  document stays unprocessed, is never marked processed, and stays retry-eligible;
* ``held`` - not eligible, stale, conflicting, or invalid: never processed.

An authoritative plan is emitted only after the current, non-obsolete
source-eligibility and processing-identity artifacts are bound *and* re-verified
against the live population in this same read-only run.  Acquisition provenance is
reported per document and never consulted as proof.  No source is re-downloaded
and no database row is written.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target, guard_engine, statement_audit)
from scripts.kg import stage3_processing_identity as identity  # noqa: E402
from scripts.kg import stage3_processing_plan_inputs as plan_inputs  # noqa: E402
from scripts.kg import stage3_processing_plan_validator as validator  # noqa: E402
from scripts.kg import stage3_processing_receipt as receipt  # noqa: E402
from scripts.kg import stage3_source_eligibility as eligibility  # noqa: E402
from scripts.kg.stage2_artifacts import (  # noqa: E402
    is_obsolete, load_verified, write_immutable)

PRODUCER_VERSION = validator.PRODUCER_VERSION
PLAN_KIND = validator.PLAN_KIND
OUTCOMES = validator.OUTCOMES
PLANNED = validator.PLANNED_OUTCOME
BUILDER_MODULE = validator.BUILDER_MODULE


def select(rows: Sequence[Mapping[str, Any]], *, limit: int | None = None,
           offset: int = 0) -> dict[str, Any]:
    """Deterministically bound the selection; refuse an ambiguous population."""
    ordered = sorted(rows, key=lambda row: int(row["id"]))
    ids = [int(row["id"]) for row in ordered]
    if len(set(ids)) != len(ids):
        raise ValueError("selection contains duplicate source ids; refusing to proceed")
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("offset and limit must be non-negative")
    window = ordered[offset:] if limit is None else ordered[offset:offset + limit]
    return {
        "rows": window,
        "bound": {"order": "source_id_asc", "offset": offset, "limit": limit,
                  "population": len(ordered), "selected": len(window)},
    }


def classification_state(row: Mapping[str, Any], folded: Mapping[str, Any]) -> dict[str, Any] | None:
    """The canonical receipt state governing ``row``'s current identity, or None."""
    key = receipt.identity_key(list(receipt.processing_identity(row)))
    if key in set(folded.get("invalid_identities") or ()):
        return {"status": receipt.STATE_INVALID, "reason": "receipt_invalid_fail_closed"}
    return (folded.get("current") or {}).get(key)


def classify_document(row: Mapping[str, Any], folded: Mapping[str, Any]) -> tuple[str, str, Any]:
    """Exactly one outcome for one document; fail closed on any doubt."""
    disposition, _reason = eligibility.classify_document(row)
    if disposition == "eligible_stale":
        return "held", "source_stale_pending_text_refresh", None
    if disposition != "eligible_current":
        return "held", f"not_eligible:{disposition}", None
    state = classification_state(row, folded)
    if state is None:
        return PLANNED, "no_exact_identity_receipt_would_process", None
    status = state.get("status")
    if status == "success":
        return "replay", "exact_identity_success_receipt_present", state
    if status == "failed":
        return "failure", str(state.get("reason") or "prior_processing_failed"), state
    if status == receipt.STATE_CONFLICT:
        return "held", "receipt_identity_conflict", state
    if status == receipt.STATE_INVALID:
        return "held", "receipt_invalid_fail_closed", state
    return "held", "receipt_status_unregistered", state


def build_record(row: Mapping[str, Any], *, outcome: str, reason: str,
                 state: Mapping[str, Any] | None) -> dict[str, Any]:
    """One reconciliation record.  Processing flags are derived, never passed in."""
    return {
        "source_kind": receipt.SOURCE_KINDS[0],
        "source_id": int(row["id"]),
        "processing_identity": list(receipt.processing_identity(row)),
        "outcome": outcome,
        "reason": reason,
        "receipt_status": (state or {}).get("status"),
        "receipt_digest": (state or {}).get("digest"),
        # Only a canonical stored success receipt proves (and records) processing.
        "marked_processed": outcome in validator.PROCESSED_OUTCOMES,
        "processing_proven": outcome in validator.PROCESSED_OUTCOMES,
        "would_process": outcome == PLANNED,
        "retry_eligible": outcome == "failure",
        "acquisition_provenance": receipt.acquisition_class(row),
        "legacy_swept_at_present": row.get("swept_at") is not None,
    }


def reconcile(rows: Sequence[Mapping[str, Any]], folded: Mapping[str, Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for row in rows:
        outcome, reason, state = classify_document(row, folded)
        records.append(build_record(row, outcome=outcome, reason=reason, state=state))
    identities = [receipt.identity_key(record["processing_identity"]) for record in records]
    if len(records) != len(set(identities)):
        raise ValueError("a selected document was reconciled more than once")
    return records


def build_dry_plan(*, rows: Sequence[Mapping[str, Any]], receipts: Sequence[Mapping[str, Any]]
                   | None, created_at: str, target: Mapping[str, Any],
                   hashes: Mapping[str, str], evidence: Mapping[str, Any] | None,
                   current_state_validation: Mapping[str, Any] | None,
                   selection_binding: Mapping[str, Any] | None = None,
                   limit: int | None = None, offset: int = 0,
                   receipts_binding: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build the authoritative dry plan; refuse to build without verified evidence."""
    if not isinstance(evidence, Mapping) or set(evidence) != set(validator.EVIDENCE_COMPONENTS):
        raise ValueError("an authoritative plan requires both bound evidence components")
    if not isinstance(current_state_validation, Mapping) or \
            current_state_validation.get("verified") is not True:
        raise ValueError("an authoritative plan requires verified current-state evidence")
    if not isinstance(selection_binding, Mapping) or \
            not selection_binding.get("identity_sha256"):
        raise ValueError("an authoritative plan requires a bound selection snapshot")

    folded = receipt.fold_receipts(receipts)
    bounded = select(rows, limit=limit, offset=offset)
    records = reconcile(bounded["rows"], folded)
    problems = validator.invariant_violations(records)
    if problems:
        raise ValueError(f"reconciliation violates a processing invariant: {problems[:3]}")
    if selection_binding["identity_sha256"] != plan_inputs.selection_identity_sha256(
            [record["processing_identity"] for record in records]):
        raise ValueError("the selection binding does not match the reconciled records")
    counts = validator.accounting(records, bound=bounded["bound"], folded=folded)
    if not counts["reconciles"]:
        raise ValueError("processing accounting does not reconcile")
    if counts["marked_processed_among_failure_or_held"]:
        raise ValueError("a failed or held document was marked processed")

    bound_documents = (current_state_validation.get("processing_identity") or {}).get("accounting")
    if isinstance(bound_documents, Mapping) and isinstance(bound_documents.get("documents"), int) \
            and bound_documents["documents"] != bounded["bound"]["population"]:
        raise ValueError("live population differs from the bound processing-identity artifact")

    body = {
        "kind": PLAN_KIND,
        "version": PRODUCER_VERSION,
        "created_at": created_at,
        "mode": "dry-run",
        "applied": False,
        "write_path": "absent by design",
        "processing_performed": False,
        "writes_performed": 0,
        "target": dict(target),
        "producer": {"module": BUILDER_MODULE, "version": PRODUCER_VERSION,
                     "code_hashes": dict(hashes)},
        "identity_authority": receipt.identity_authority_binding(),
        "identity_fields": list(receipt.IDENTITY_FIELDS),
        "extractor": receipt.EXTRACTOR,
        "extractor_version": receipt.EXTRACTOR_VERSION,
        "policy": {
            "planned": ("eligible exact identity with no canonical receipt: not processed, "
                        "not marked processed; a real run would process it"),
            "replay": ("exact identity already carries a canonical stored success receipt: "
                       "the only state in which processing is proven"),
            "failure": ("exact identity receipt reported failure; unprocessed, never marked "
                        "processed, retry-eligible"),
            "held": "not eligible, stale, conflicting, or invalid; never processed",
            "provenance_rule": ("recorded acquisition evidence proves how bytes arrived and is "
                                "never accepted as processing proof"),
            "replay_rule": ("receipts are keyed by processing identity; identical rows are "
                            "replays and a repeated arrival cannot write twice"),
        },
        "bound": dict(bounded["bound"]),
        "evidence_binding": {name: dict(binding) for name, binding in evidence.items()},
        "current_state_validation": dict(current_state_validation),
        "selection_binding": dict(selection_binding),
        "receipts_binding": dict(receipts_binding or {"source": "none", "count": 0}),
        "receipt_fold": {"valid_receipts": folded["valid_receipts"],
                         "identities": folded["identities"],
                         "conflicts": len(folded["conflicts"]),
                         "invalid": len(folded["invalid"]),
                         "replayed": folded["replayed"],
                         "superseded": folded["superseded"]},
        "accounting": counts,
        "records": records,
        "records_sha256": validator.canonical_sha256(records),
        "approval_boundary": {
            "authorized_here": "dry-run reconciliation over retained text only",
            "not_authorized": [
                "creating the receipt store or any additive schema",
                "writing any receipt, receipt row, or swept_at update",
                "re-downloading or re-extracting any source",
                "enabling or executing an apply runner",
            ],
            "requires": ("explicit development schema/data authorization plus a receipt store "
                         "design packet reviewed under the repository rules"),
            "mutations_proposed": 0,
        },
    }
    plan = {**body, "digest": validator.plan_digest(body)}
    problems = validator.validate_plan(plan, target=target, hashes=hashes)
    if problems:
        raise ValueError(f"the built plan refuses validation: {problems[:3]}")
    return plan


def replay_plan(plan: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Prove the plan is a pure function of its inputs, and therefore replay-safe."""
    rebuilt = build_dry_plan(created_at=str(plan["created_at"]), **kwargs)
    identical = validator.plan_digest(plan) == validator.plan_digest(rebuilt)
    return {
        "identical": identical,
        "planned_digest": plan.get("digest"),
        "rebuilt_digest": rebuilt.get("digest"),
        "writes_performed": 0,
        "processing_performed": False,
        "replay_safe": identical,
    }


def load_receipts(path: str | Path | None) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    """Bind an optional receipt-set artifact; no artifact binds the explicit empty set."""
    if path is None:
        return [], {"source": "none", "count": 0}
    return plan_inputs.load_receipt_set(path)


def load_evidence_artifacts(*, eligibility_path: str | Path,
                            processing_identity_path: str | Path,
                            target: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """Load the two authoritative artifacts; refuse obsolete, foreign, or drifted ones."""
    artifacts: dict[str, dict[str, Any]] = {}
    for component, path in (("source_eligibility", eligibility_path),
                            ("processing_identity", processing_identity_path)):
        candidate = Path(path)
        if is_obsolete(candidate):
            raise RuntimeError(f"{component} artifact is obsolete: {candidate.name}")
        artifact = load_verified(candidate)
        expected_kind = validator.EVIDENCE_COMPONENTS[component]
        if artifact.get("kind") != expected_kind:
            raise RuntimeError(f"{component} artifact has kind {artifact.get('kind')!r}")
        if dict(artifact.get("target") or {}) != dict(target):
            raise RuntimeError(f"{component} artifact binds another target")
        artifacts[component] = artifact
    if dict(artifacts["processing_identity"].get("eligibility_binding") or {}).get("digest") \
            != artifacts["source_eligibility"].get("digest"):
        raise RuntimeError("the processing-identity artifact does not bind this eligibility artifact")
    return artifacts


def bind_evidence(engine: Any, *, eligibility_path: str | Path,
                  processing_identity_path: str | Path, target: Mapping[str, Any]
                  ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    """Bind the artifacts, re-verify them against live state, and return that state.

    The returned document rows are the exact snapshot the validation ran against,
    so the plan built from them cannot disagree with the state it cites.
    """
    artifacts = load_evidence_artifacts(eligibility_path=eligibility_path,
                                        processing_identity_path=processing_identity_path,
                                        target=target)
    with engine.connect() as connection:
        documents = [dict(row) for row in connection.execute(text(identity.DOC_SQL)).mappings()]
        agenda_items = [dict(row) for row in connection.execute(text(eligibility.ITEM_SQL)).mappings()]
    schema = eligibility.schema_binding(engine)

    problems = eligibility.validate_current(
        artifacts["source_eligibility"], rows={"documents": documents, "agenda_items": agenda_items},
        target=target, schema=schema)
    problems += identity.validate_current(
        artifacts["processing_identity"], rows=documents, target=target,
        eligibility_binding=artifacts["processing_identity"]["eligibility_binding"])
    if problems:
        raise RuntimeError(f"live current-state validation refused the run: {problems[:3]}")

    paths = {"source_eligibility": eligibility_path,
             "processing_identity": processing_identity_path}
    evidence = {component: {"path": str(paths[component]), "digest": artifact["digest"],
                            "kind": artifact.get("kind"), "target": artifact.get("target")}
                for component, artifact in sorted(artifacts.items())}
    current_state = {
        "verified": True,
        "problems": [],
        "source_eligibility": {"artifact_digest": artifacts["source_eligibility"]["digest"],
                               "population_sha256": artifacts["source_eligibility"]["population_sha256"],
                               "accounting": artifacts["source_eligibility"]["accounting"]},
        "processing_identity": {"artifact_digest": artifacts["processing_identity"]["digest"],
                                "population_sha256": artifacts["processing_identity"]["population_sha256"],
                                "accounting": artifacts["processing_identity"]["accounting"]},
        "live_population": {"documents": len(documents), "agenda_items": len(agenda_items)},
    }
    return evidence, current_state, documents


def run(engine: Any, *, eligibility_path: str | Path, processing_identity_path: str | Path,
        out_dir: Path, limit: int | None = None, offset: int = 0,
        receipts_path: str | Path | None = None, stamp: str | None = None) -> dict[str, Any]:
    target = assert_read_only_target(engine)
    statements = guard_engine(engine)
    evidence, current_state, rows = bind_evidence(
        engine, eligibility_path=eligibility_path,
        processing_identity_path=processing_identity_path, target=target)
    audit = statement_audit(statements)
    if not audit["select_only"]:
        raise RuntimeError(f"read-only audit failed: {audit}")
    receipts, receipts_binding = load_receipts(receipts_path)
    hashes = validator.code_hashes()
    created = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bounded = select(rows, limit=limit, offset=offset)
    out_dir.mkdir(parents=True, exist_ok=True)
    snapshot = plan_inputs.build_selection_snapshot(
        created_at=created, target=target, bound=bounded["bound"],
        entries=[plan_inputs.classification_entry(row) for row in bounded["rows"]],
        evidence=evidence, code_hashes=hashes)
    snapshot_path = out_dir / f"kg-stage3-processing-selection-{created}.json"
    snapshot_digest = write_immutable(snapshot_path, snapshot)
    selection_binding = {
        "path": str(snapshot_path), "digest": snapshot_digest,
        "kind": plan_inputs.SELECTION_KIND, "target": dict(target),
        "identity_sha256": snapshot["identity_sha256"],
        "selected": bounded["bound"]["selected"],
        "population": bounded["bound"]["population"],
        "order": bounded["bound"]["order"],
    }
    plan = build_dry_plan(rows=rows, receipts=receipts, created_at=created, target=target,
                          hashes=hashes, evidence=evidence, current_state_validation=current_state,
                          selection_binding=selection_binding, limit=limit, offset=offset,
                          receipts_binding=receipts_binding)
    replay = replay_plan(plan, rows=rows, receipts=receipts, target=target, hashes=hashes,
                         evidence=evidence, current_state_validation=current_state,
                         selection_binding=selection_binding, limit=limit, offset=offset,
                         receipts_binding=receipts_binding)
    if not replay["identical"]:
        raise RuntimeError("dry plan is not replay-stable")
    path = out_dir / f"kg-stage3-processing-dry-plan-{created}.json"
    digest = write_immutable(path, plan)
    return {"status": "success", "path": str(path), "digest": digest,
            "selection_snapshot": {"path": str(snapshot_path), "digest": snapshot_digest,
                                   "identity_sha256": snapshot["identity_sha256"]},
            "receipts_binding": receipts_binding,
            "accounting": plan["accounting"], "bound": plan["bound"],
            "evidence_binding": plan["evidence_binding"], "replay": replay,
            "read_only_audit": audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eligibility", type=Path, required=True)
    parser.add_argument("--processing-identity", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--receipts", type=Path, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(run(get_engine(), eligibility_path=args.eligibility,
                         processing_identity_path=args.processing_identity,
                         out_dir=args.out_dir, limit=args.limit, offset=args.offset,
                         receipts_path=args.receipts), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
