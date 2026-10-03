#!/usr/bin/env python3
"""Issue the standing daily-sync authorization — ONLY through the canonical machinery.

This is a *recorder*, not an issuer of consent. It re-validates the human's
approval, builds the standing plan with the canonical builder, and calls
``operation_authorization.record_authorization`` — the same function the reviewed
CLI uses. It never composes approval text, never edits a generated field, and
never computes a digest by hand.

FAIL-CLOSED PRECONDITIONS. Every one must hold before a single byte is written.
The most important is ``MODE_UNBOUND``:

    An upsert-only standing authorization is only safe if the production
    interlock can TELL an upsert from a ``--reconcile`` / ``--schema-only``
    request. Those modes are explicitly prohibited by the approved proposal, but
    they declare the SAME operation, entry point, target and scope as a routine
    upsert. The interlock request carries no mode, so an authorization that
    matches the routine scope would ALSO match a delete or schema request —
    removing a protection that exists today. This recorder therefore refuses to
    issue while the interlock request cannot express mode.

Nothing here executes a sync.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO_ROOT), str(REPO_ROOT / "scripts"), str(REPO_ROOT / "scripts" / "ops")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.sync_runtime import select_sync_tables  # noqa: E402
from scripts.ops import standing_authorization_proposal as proposal_mod  # noqa: E402
from scripts.ops.operation_authorization import (  # noqa: E402
    build_plan,
    code_hashes,
    plan_digest,
    record_authorization,
    write_plan,
)

PROPOSAL_PATH = (REPO_ROOT / "data" / "standing-proposals" /
                 "OP-RECON-standing-daily-sync.proposal.json")
OPERATION_ID = "OP-RECON-standing-daily-sync"

#: The AUTHORIZATION mode (how many times the recorded approval may be used).
#: Distinct from the EXECUTION mode (what the run does) — see proposal_mod.SYNC_MODE.
AUTHORIZATION_MODE = "standing"


def approval_path_for(proposal_digest: str) -> Path:
    """The approval-record path the loopback review page writes for a digest.

    Derived from the digest so a regenerated proposal never silently inherits the
    previous proposal's approval: a new digest means a new record path, and a
    missing record is a refusal.
    """
    return (REPO_ROOT / "data" / "standing-approvals" /
            f"OP-RECON-standing-{(proposal_digest or '')[:16]}.approval.json")

# refusal codes
PROPOSAL_MISSING = "PROPOSAL_MISSING"
PROPOSAL_TAMPERED = "PROPOSAL_TAMPERED"
PROPOSAL_EXECUTABLE = "PROPOSAL_IS_NOT_AN_AUTHORIZATION"
APPROVAL_MISSING = "APPROVAL_RECORD_MISSING"
APPROVAL_TAMPERED = "APPROVAL_RECORD_TAMPERED"
APPROVAL_NOT_BOUND = "APPROVAL_DOES_NOT_BIND_PROPOSAL"
APPROVAL_EXECUTED = "APPROVAL_ALREADY_RECORDED_AS_EXECUTION"
APPROVAL_NO_REVIEWER = "APPROVAL_NAMES_NO_REVIEWER"
APPROVAL_NOT_ACKNOWLEDGED = "APPROVAL_ACKNOWLEDGEMENT_MISSING"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
GATE_MISMATCH = "GATE_SET_MISMATCH"
LIMIT_MISMATCH = "LIMIT_MISMATCH"
CODE_DRIFT = "CODE_HASH_DRIFT"
WINDOW_INVALID = "WINDOW_INVALID"
WINDOW_EXPIRED = "WINDOW_EXPIRED"
MODE_UNBOUND = "MODE_UNBOUND"
ALREADY_ISSUED = "ALREADY_ISSUED"
PLAN_BINDING_MISMATCH = "PLAN_BINDING_MISMATCH"

#: The canonical plan builder serializes timestamps to whole seconds
#: (``operation_authorization._iso`` -> "%Y-%m-%dT%H:%M:%SZ"), while the proposal
#: recorded microsecond precision. The windows are the same approved instants to
#: within that serializer's precision, so they are compared as instants with a
#: tolerance no larger than one second — not compared as strings, which would
#: report a spurious mismatch.
TIMESTAMP_TOLERANCE = timedelta(seconds=1)


def same_instant(first: object, second: object,
                 tolerance: timedelta = TIMESTAMP_TOLERANCE) -> bool:
    """Whether two serialized timestamps denote the same instant (within tolerance)."""
    left = proposal_mod._parse(first)
    right = proposal_mod._parse(second)
    if left is None or right is None:
        return False
    return abs(left - right) <= tolerance


def _refuse(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    verdict: dict[str, Any] = {"status": "REFUSED", "code": code, "reason": reason}
    verdict.update(extra)
    return verdict


def record_digest(record: Mapping[str, Any]) -> str:
    """Recompute the approval record digest exactly as the review route wrote it."""
    body = {key: value for key, value in record.items() if key != "record_digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def mode_enforcement_available() -> tuple[bool, str]:
    """Whether the interlock request can express the execution MODE."""
    from production_interlock import check

    if "mode" not in inspect.signature(check).parameters:
        return False, (
            "the production interlock request carries no mode, so an upsert-only "
            "authorization cannot be distinguished from a --reconcile, "
            "--reconcile-only, --schema-only or --bootstrap-schema request at the "
            "same entry point and scope. Issuing would make modes the approved "
            "proposal PROHIBITS satisfy this authorization.")
    return True, ""


def revalidate(
    *,
    proposal_path: Path = PROPOSAL_PATH,
    approval_path: Path | None = None,
    now: datetime | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any]]:
    """Independently revalidate the proposal and the approval record.

    Returns (proposal, record, verdict). A non-null verdict is a refusal.
    ``approval_path`` defaults to the record path for THIS proposal's digest, so a
    regenerated proposal cannot be satisfied by an earlier proposal's approval.
    """
    now = now or datetime.now(timezone.utc)

    if not proposal_path.is_file():
        return None, None, _refuse(PROPOSAL_MISSING, f"no proposal at {proposal_path}")
    proposal = json.loads(proposal_path.read_text())
    if proposal.get("digest") != proposal_mod.proposal_digest(proposal):
        return None, None, _refuse(PROPOSAL_TAMPERED,
                                   "proposal digest does not match its contents")
    if approval_path is None:
        approval_path = approval_path_for(str(proposal.get("digest") or ""))
    if proposal.get("executable") is not False:
        return None, None, _refuse(PROPOSAL_EXECUTABLE,
                                   "proposal declares itself executable")
    for field in ("approver", "authorization"):
        if proposal.get(field) is not None:
            return None, None, _refuse(PROPOSAL_EXECUTABLE,
                                       f"proposal carries a non-null {field!r}")

    if not approval_path.is_file():
        return None, None, _refuse(APPROVAL_MISSING, f"no approval record at {approval_path}")
    record = json.loads(approval_path.read_text())
    if record.get("record_digest") != record_digest(record):
        return None, None, _refuse(APPROVAL_TAMPERED,
                                   "approval record digest does not match its contents")
    if record.get("proposal_digest") != proposal.get("digest"):
        return None, None, _refuse(APPROVAL_NOT_BOUND,
                                   "approval record does not bind the proposal digest")
    if record.get("executed") is not False or record.get("authorizes_operation") is not False:
        return None, None, _refuse(APPROVAL_EXECUTED,
                                   "approval record is not a pure approval record")
    if not str(record.get("reviewer") or "").strip():
        return None, None, _refuse(APPROVAL_NO_REVIEWER, "approval record names no reviewer")
    if record.get("acknowledged_recurring_upserts_only_while_gates_pass") is not True:
        return None, None, _refuse(APPROVAL_NOT_ACKNOWLEDGED,
                                   "approval record lacks the recurring-upserts "
                                   "acknowledgement")

    # The record must describe exactly what the proposal approved.
    if sorted(record.get("scope") or []) != sorted(proposal.get("scope") or []):
        return None, None, _refuse(SCOPE_MISMATCH, "approval scope differs from the proposal")
    if list(record.get("gate_ids") or []) != [g["id"] for g in proposal["mandatory_gates"]]:
        return None, None, _refuse(GATE_MISMATCH, "approval gate set differs from the proposal")
    if record.get("max_uses") != proposal.get("max_uses"):
        return None, None, _refuse(LIMIT_MISMATCH, "approval max_uses differs from the proposal")
    if record.get("validity_days") != proposal.get("validity_days"):
        return None, None, _refuse(LIMIT_MISMATCH, "approval validity differs from the proposal")
    if record.get("code_hashes") != proposal.get("code_hashes"):
        return None, None, _refuse(CODE_DRIFT, "approval code hashes differ from the proposal")

    # Nothing may have changed since the human approved.
    current = code_hashes(list(proposal["code_hashes"]))
    changed = sorted(k for k, v in proposal["code_hashes"].items() if current.get(k) != v)
    if changed:
        return None, None, _refuse(CODE_DRIFT,
                                   f"bound code changed since approval: {changed}",
                                   changed=changed)
    if proposal.get("operation") != proposal_mod.OPERATION:
        return None, None, _refuse(SCOPE_MISMATCH, "proposal operation is not OP-RECON")
    if proposal.get("entry_point") != proposal_mod.ENTRY_POINT:
        return None, None, _refuse(SCOPE_MISMATCH, "proposal entry point changed")
    # The approved scope must still be the runtime authority's routine scope.
    if sorted(proposal["scope"]) != sorted(select_sync_tables(None)):
        return None, None, _refuse(SCOPE_MISMATCH,
                                   "proposal scope no longer equals the runtime "
                                   "declaration authority's routine scope")

    not_before = proposal_mod._parse(proposal.get("not_before"))
    not_after = proposal_mod._parse(proposal.get("not_after"))
    if not_before is None or not_after is None or not_after <= not_before:
        return None, None, _refuse(WINDOW_INVALID, "proposal window is missing or inverted")
    if now >= not_after:
        return None, None, _refuse(WINDOW_EXPIRED, "proposal window has expired")

    return proposal, record, {}


def build_standing_plan(proposal: Mapping[str, Any]) -> dict[str, Any]:
    """Build the 24-table standing plan with the canonical builder."""
    not_before = proposal_mod._parse(proposal["not_before"])
    not_after = proposal_mod._parse(proposal["not_after"])
    return build_plan(
        operation=proposal["operation"],
        operation_id=OPERATION_ID,
        entry_point=proposal["entry_point"],
        scope=list(proposal["scope"]),
        code_paths=list(proposal["code_hashes"]),
        rollback_owner=proposal["rollback_owner"],
        not_before=not_before,
        not_after=not_after,
        target=proposal["target"],
        mode=proposal.get("mode") or proposal_mod.SYNC_MODE,
        notes=("Standing routine upsert-only daily data sync, issued from approved "
               f"proposal {proposal['digest']}"),
    )


def verify_plan_binding(plan: Mapping[str, Any], proposal: Mapping[str, Any]) -> dict[str, Any]:
    """The plan must bind the same entry point, target, scope and code hashes."""
    for field in ("operation", "entry_point", "target"):
        if plan.get(field) != proposal.get(field):
            return _refuse(PLAN_BINDING_MISMATCH,
                           f"plan {field}={plan.get(field)!r} != proposal "
                           f"{proposal.get(field)!r}")
    if sorted(plan.get("scope") or []) != sorted(proposal.get("scope") or []):
        return _refuse(PLAN_BINDING_MISMATCH, "plan scope differs from the proposal")
    if plan.get("code_hashes") != proposal.get("code_hashes"):
        return _refuse(PLAN_BINDING_MISMATCH, "plan code hashes differ from the proposal")
    if not same_instant(plan.get("not_before"), proposal.get("not_before")):
        return _refuse(PLAN_BINDING_MISMATCH,
                       "plan not_before differs from the approved window")
    if not same_instant(plan.get("not_after"), proposal.get("not_after")):
        return _refuse(PLAN_BINDING_MISMATCH,
                       "plan not_after differs from the approved window")
    if plan.get("mode") != (proposal.get("mode") or proposal_mod.SYNC_MODE):
        return _refuse(PLAN_BINDING_MISMATCH,
                       f"plan mode {plan.get('mode')!r} differs from the approved "
                       f"execution mode "
                       f"{(proposal.get('mode') or proposal_mod.SYNC_MODE)!r}")
    if plan.get("digest") != plan_digest(plan):
        return _refuse(PLAN_BINDING_MISMATCH, "plan digest does not match its contents")
    return {}


def issue(
    *,
    proposal_path: Path = PROPOSAL_PATH,
    approval_path: Path | None = None,
    verbatim_file: Path | None = None,
    now: datetime | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Revalidate, build the plan, and (only if every precondition holds) record."""
    proposal, record, refusal = revalidate(
        proposal_path=proposal_path, approval_path=approval_path, now=now)
    if refusal:
        return refusal

    available, detail = mode_enforcement_available()
    if not available:
        return _refuse(MODE_UNBOUND, detail)

    plan = build_standing_plan(proposal)
    refusal = verify_plan_binding(plan, proposal)
    if refusal:
        return refusal

    from scripts.ops.operation_authorization import operation_dir

    directory = operation_dir(plan["operation"], plan["operation_id"])
    if (directory / "authorization.json").is_file():
        return _refuse(ALREADY_ISSUED, f"an authorization already exists at {directory}")

    if dry_run:
        return {"status": "WOULD_ISSUE", "code": None, "plan_digest": plan["digest"],
                "operation_id": plan["operation_id"], "directory": str(directory)}

    if verbatim_file is None or not verbatim_file.is_file():
        return _refuse(APPROVAL_MISSING,
                       "the human's verbatim approval text is required")
    verbatim = verbatim_file.read_text().strip()
    if not verbatim:
        return _refuse(APPROVAL_MISSING, "the verbatim approval text is empty")

    write_plan(plan)
    # Every element the reviewer needs to trace this authorization back, pinned in
    # the authorization's own evidence field — nothing inferred.
    verbatim_source = {
        "proposal_digest": proposal["digest"],
        "approval_record_path": str(approval_path),
        "approval_record_digest": record["record_digest"],
        "reviewer": record["reviewer"],
        "approved_at": record["approved_at"],
        "verbatim_followup": verbatim,
        "review_surface": ("loopback review page, session-bound CSRF, recorded "
                           "authorization only"),
        "issued_by": "scripts/ops/issue_standing_authorization.py",
        "recorded_via": "scripts/ops/operation_authorization.record_authorization",
    }
    path = record_authorization(
        plan,
        verbatim_approval=verbatim,
        author=record["reviewer"],
        source="human",
        authorized_at=proposal_mod._parse(record["approved_at"]),
        mode=AUTHORIZATION_MODE,
        max_uses=proposal["max_uses"],
        verbatim_source=verbatim_source,
        use_accounting="successful-terminal",
    )
    from scripts.ops.operation_authorization import count_uses

    return {"status": "ISSUED", "code": None, "authorization": str(path),
            "plan_digest": plan["digest"], "operation_id": plan["operation_id"],
            "max_uses": proposal["max_uses"],
            "uses_consumed": count_uses(plan["operation_id"])}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Issue the standing daily-sync authorization (fail-closed).")
    parser.add_argument("--proposal", type=Path, default=PROPOSAL_PATH)
    parser.add_argument("--approval", type=Path, default=None)
    parser.add_argument("--verbatim-file", type=Path)
    parser.add_argument("--apply", action="store_true",
                        help="actually record the authorization (default: check only)")
    arguments = parser.parse_args()

    result = issue(proposal_path=arguments.proposal, approval_path=arguments.approval,
                   verbatim_file=arguments.verbatim_file, dry_run=not arguments.apply)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in ("ISSUED", "WOULD_ISSUE") else 3


if __name__ == "__main__":
    raise SystemExit(main())
