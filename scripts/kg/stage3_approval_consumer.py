#!/usr/bin/env python3
"""Consume one pinned human approval record into an authorized packet.

This consumer is deliberately specific to a single approved lineage.  It is not a
general-purpose "find an approval and authorize something" tool: the record path, the
record digest, the reviewer, the proposal digest, the acknowledged keys, the review
boundary, the execution block, and the scope are all pinned here, and every one of them
must match exactly.  There is no CLI flag that can widen acceptance.

Why so rigid: an approval is a single human decision about a single set of artifacts.  A
consumer that accepts any record, or that accepts a record whose scope it merely reads
rather than re-derives, can be turned into a broadening tool by editing an artifact rather
than by asking the human again.

Two constraints shape the implementation.

* ``stage3_processing_receipt_apply_packet`` and the preflight module are members of
  ``CODE_FILES``.  Editing either would move ``code_digest()`` and invalidate the very
  approval being consumed, so this module never edits them.  It builds with the canonical
  builder and then *adds* the human-record binding, recomputing the digest through the
  builder's own ``digest()`` after removing the previous value (that function hashes the
  mapping as given and does not strip a digest key).
* Counts are re-derived from the artifacts, never read from the record alone.  A record
  cannot broaden its own scope, because the artifacts have the final say.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg import stage3_processing_receipt_apply as apply  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_packet as authorization  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_proposal as proposal_mod  # noqa: E402
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

APPROVAL_DIR = REPO / "data/kg-approvals"
PLANS_DIR = REPO / "data/kg-plans"

KIND = "kg-stage3-processing-receipt-human-approval"
VERSION = "1.0"
PINNED_RECORD = APPROVAL_DIR / (
    "kg-stage3-approval-record-f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60.json")
PINNED_RECORD_DIGEST = "95590e342db23701344b2c20811839dbd44377ecaf381470bb003a1c9820eead"
PINNED_REVIEWER = "Peter Mains"
PINNED_PROPOSAL_DIGEST = "f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60"
PINNED_ACKNOWLEDGED = ["development_target", "append_only_scope", "held_excluded",
                       "no_production_operations", "verified_backup", "stop_on_failure"]
PINNED_REVIEW_BOUNDARY = {"records_authorization_only": True, "executes_nothing": True,
                          "is_apply_terminal_receipt": False}
PINNED_EXECUTION = {"authorization_packet": None, "executed": False, "executed_at": None,
                    "preflight": None}
PINNED_SCOPE = {"cursor": 6100, "batches": 120, "batch_size": 500, "remaining_writes": 58628,
                "held_after_cursor": 996, "held_in_consumed_prefix": 5,
                "existing_receipts_replayed": 6095, "append_only": True,
                "stop_on_first_failure": True, "swept_at_rewrite": False}
ARTIFACT_PATHS = {
    "proposal_digest": PLANS_DIR / "kg-stage3-processing-receipt-proposal-20260921T195816Z.json",
    "plan_digest": PLANS_DIR / "kg-stage3-processing-dry-plan-20260921T193158Z.json",
    "design_packet_digest": PLANS_DIR / "kg-stage3-processing-receipt-store-packet-20260921T194940Z.json",
    "receipt_set_digest": PLANS_DIR / "kg-stage3-processing-receipt-set-20260921T192601Z.json",
    "backup_receipt_digest": REPO / "data/backups/kg-stage2-backup-receipt-20260921T184835Z.json",
    "schedule_digest": PLANS_DIR / "kg-stage3-receipt-batch-schedule-20260921T195036Z.json",
}
ARTIFACT_NAME = {"proposal_digest": "proposal", "plan_digest": "plan",
                 "design_packet_digest": "design packet",
                 "receipt_set_digest": "receipt set", "backup_receipt_digest": "backup receipt",
                 "schedule_digest": "schedule"}


class ConsumptionRefused(RuntimeError):
    """Raised when the approval cannot be consumed without changing what was approved."""


def load_record() -> tuple[Path, dict[str, Any]]:
    """The pinned record, or a refusal.  There is no discovery and no alternative."""
    if not PINNED_RECORD.is_file():
        raise ConsumptionRefused(f"the pinned approval record is absent: {PINNED_RECORD}")
    if is_obsolete(PINNED_RECORD):
        raise ConsumptionRefused("the pinned approval record is obsolete")
    record = load_verified(PINNED_RECORD)
    if record.get("digest") != PINNED_RECORD_DIGEST:
        raise ConsumptionRefused(
            f"the record digest is {record.get('digest')!r}, not the pinned "
            f"{PINNED_RECORD_DIGEST}")
    siblings = sorted(path.name for path in APPROVAL_DIR.glob("kg-stage3-approval-record-*.json"))
    if len(siblings) != 1:
        raise ConsumptionRefused(
            f"the approval ledger must hold exactly one record for this lineage; found {siblings}")
    return PINNED_RECORD, record


def _load_artifact(field: str, expected: str) -> dict[str, Any]:
    path = ARTIFACT_PATHS[field]
    label = ARTIFACT_NAME[field]
    if not path.is_file():
        raise ConsumptionRefused(f"the {label} artifact is absent: {path}")
    if is_obsolete(path):
        raise ConsumptionRefused(f"the {label} artifact is obsolete")
    document = load_verified(path)
    if document.get("digest") != expected:
        raise ConsumptionRefused(
            f"the {label} artifact is {document.get('digest')!r}, not the bound {expected!r}")
    return document


def load_artifacts(record: Mapping[str, Any]) -> dict[str, Any]:
    return {field[:-len("_digest")]: _load_artifact(field, str(record.get(field) or ""))
            for field in ARTIFACT_PATHS}


def selected_rows(plan: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    for value in plan.values():
        if isinstance(value, list) and value and isinstance(value[0], Mapping) \
                and "processing_identity" in value[0]:
            return value
    raise ConsumptionRefused("the plan exposes no selected rows")


def derive_scope(artifacts: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute every reviewed count from the artifacts themselves."""
    plan, schedule = artifacts["plan"], artifacts["schedule"]
    receipt_set, proposal = artifacts["receipt_set"], artifacts["proposal"]
    rows = selected_rows(plan)
    cursor = schedule.get("cursor")
    if not isinstance(cursor, int):
        raise ConsumptionRefused("the schedule declares no integer cursor")
    consumed = Counter(str(row.get("outcome")) for row in rows[:cursor])
    remaining = Counter(str(row.get("outcome")) for row in rows[cursor:])
    totals = schedule.get("totals") or {}
    outcomes = totals.get("expected_outcomes") or {}
    contract = proposal.get("contract") or {}
    return {
        "cursor": cursor,
        "batches": len(schedule.get("batches") or []),
        "batch_size": schedule.get("batch_size"),
        "remaining_writes": totals.get("expected_writes"),
        "held_after_cursor": outcomes.get("held"),
        "held_in_consumed_prefix": consumed.get("held", 0),
        "existing_receipts_replayed": receipt_set.get("count"),
        "append_only": contract.get("receipt_history") == "append_only",
        "stop_on_first_failure": bool(schedule.get("stop_on_first_failure")),
        "swept_at_rewrite": not bool(contract.get("no_swept_at_rewrite")),
        "_replay_in_consumed_prefix": consumed.get("replay", 0),
        "_planned_after_cursor": remaining.get("planned", 0),
    }


def validate_record(record: Mapping[str, Any], artifacts: Mapping[str, Any]) -> list[str]:
    """Every reason this record cannot authorize these artifacts."""
    problems: list[str] = []
    if record.get("kind") != KIND or record.get("version") != VERSION:
        problems.append("the record is not the approved kind and version")
    if record.get("reviewer_name") != PINNED_REVIEWER:
        problems.append("the record's reviewer is not the approved reviewer")
    approved_at = record.get("approved_at")
    if not isinstance(approved_at, str) or not approved_at.strip():
        problems.append("the record carries no approval timestamp")
    else:
        try:
            datetime.fromisoformat(approved_at)
        except ValueError:
            problems.append("the record's approval timestamp is not parseable")
    if dict(record.get("execution") or {}) != PINNED_EXECUTION:
        problems.append("the record's execution block does not show nothing executed")
    if dict(record.get("review_boundary") or {}) != PINNED_REVIEW_BOUNDARY:
        problems.append("the record's review-boundary flags are not the approved flags")
    if list(record.get("acknowledged") or []) != PINNED_ACKNOWLEDGED:
        problems.append("the record's acknowledgements are not the six reviewed keys")
    if record.get("proposal_digest") != PINNED_PROPOSAL_DIGEST:
        problems.append("the record does not bind the approved proposal")
    text = str(record.get("approval_text") or "")
    if not text.strip():
        problems.append("the record carries no approval text")
    elif PINNED_PROPOSAL_DIGEST not in text:
        problems.append("the approval text does not name the approved proposal")

    proposal = artifacts["proposal"]
    if not proposal_mod.is_proposal(proposal):
        problems.append("the proposal is not in the non-executable proposed state")
    proposal_problems = proposal_mod.validate_proposal(
        proposal, plan=artifacts["plan"], design_packet=artifacts["design_packet"])
    if proposal_problems:
        problems.append(f"the proposal does not validate: {proposal_problems[0]}")

    if record.get("code_digest") != apply.code_digest():
        problems.append("the recorded code binding has drifted from the current code")

    # Exact target equality across the record, the proposal, and the plan.
    record_target = dict(record.get("target") or {})
    if record_target != dict(proposal.get("target") or {}):
        problems.append("the record's target is not the reviewed proposal target")
    if record_target != dict(artifacts["plan"].get("target") or {}):
        problems.append("the record's target is not the reviewed plan target")
    if record_target.get("tier") != "development" \
            or record_target.get("database") != "poliscopic_dev":
        problems.append("the record does not authorize the exact development target")

    scope = dict(record.get("scope") or {})
    derived = derive_scope(artifacts)
    for key, expected in PINNED_SCOPE.items():
        if scope.get(key) != expected:
            problems.append(f"the record's scope value {key} is not the reviewed value")
        if derived.get(key) != expected:
            problems.append(f"the derived {key} from the artifacts is not the reviewed value")
    return problems


def existing_authorized_packets() -> list[Path]:
    return sorted(PLANS_DIR.glob("kg-stage3-processing-receipt-authorized-apply-*.json"))


def refuse_ambiguity() -> None:
    """Refuse another approval's packet, and refuse regenerating this one's."""
    for path in existing_authorized_packets():
        document = load_verified(path)
        bound = document.get("approval_record_digest")
        if bound == PINNED_RECORD_DIGEST:
            raise ConsumptionRefused(
                f"an authorized packet for this approval already exists: {path.name}")
        if bound is not None:
            raise ConsumptionRefused(
                f"an authorized packet for a different approval already exists: {path.name}")


def build_packet(record: Mapping[str, Any], *, artifacts: Mapping[str, Any]) -> dict[str, Any]:
    """The canonical packet, augmented with the human-record binding."""
    packet = authorization.build(
        plan=artifacts["plan"], design_packet=artifacts["design_packet"],
        backup_receipt_path=str(ARTIFACT_PATHS["backup_receipt_digest"]),
        backup_receipt_digest=str(record["backup_receipt_digest"]),
        code_digest=str(record["code_digest"]), approver=str(record["reviewer_name"]),
        writer_role="poliscopic", batch_size=int(PINNED_SCOPE["batch_size"]))
    text = str(record["approval_text"])
    packet.update({
        "approval_record_digest": PINNED_RECORD_DIGEST,
        "approval_record_path": str(PINNED_RECORD),
        "approval_record_kind": KIND,
        "approved_by": str(record["reviewer_name"]),
        "approved_at": str(record["approved_at"]),
        "approval_text": text,
        "approval_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "execution": dict(PINNED_EXECUTION),
    })
    packet.pop("digest", None)
    packet["digest"] = authorization.digest(packet)
    return packet


def consume(out_path: Path) -> dict[str, Any]:
    record_path, record = load_record()
    artifacts = load_artifacts(record)
    problems = validate_record(record, artifacts=artifacts)
    if problems:
        raise ConsumptionRefused("; ".join(problems))
    refuse_ambiguity()
    if out_path.exists():
        raise ConsumptionRefused(f"refusing to overwrite an existing packet: {out_path.name}")
    packet = build_packet(record, artifacts=artifacts)
    packet_problems = authorization.validate(
        packet, plan=artifacts["plan"], design_packet=artifacts["design_packet"],
        current_code_digest=apply.code_digest())
    if packet_problems:
        raise ConsumptionRefused(f"the built packet refuses validation: {packet_problems[:3]}")
    digest = write_immutable(out_path, packet)
    return {"record_path": record_path, "record_digest": PINNED_RECORD_DIGEST,
            "packet_path": out_path, "packet_digest": digest,
            "derived_scope": derive_scope(artifacts),
            "artifacts": {field: artifacts[field[:-len("_digest")]]["digest"]
                          for field in ARTIFACT_PATHS}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = consume(args.out)
    except Exception as exc:  # a refusal is a result, not a crash
        print(json.dumps({"outcome": "refused", "error": f"{type(exc).__name__}: {exc}"},
                         sort_keys=True))
        return 1
    print(json.dumps({"outcome": "authorized", "executed": False,
                      "approval_record_digest": result["record_digest"],
                      "packet_path": str(result["packet_path"]),
                      "packet_digest": result["packet_digest"],
                      "artifacts": result["artifacts"],
                      "derived_scope": result["derived_scope"]}, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
