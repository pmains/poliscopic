#!/usr/bin/env python3
"""Standing daily production-sync AUTHORIZATION PROPOSAL (non-executable).

This module builds and validates a *proposal* for a bounded, recurring
development→production data sync. It deliberately cannot authorize anything:

* the artifact it writes declares ``executable: false``, ``approver: null`` and
  ``authorization: null``, and lives OUTSIDE ``data/release`` so the interlock,
  which reads only ``data/release/<operation>-<id>/authorization.json``, can
  never mistake it for an authorization;
* there is no function here that records an approval. A human approval must be
  recorded later by the reviewed mechanism (``operation_authorization``) against
  a real plan, with the human's verbatim words;
* :func:`validate_request` refuses before any credential, network or database
  activity is possible — the module imports no engine and opens no connection.

The recurring envelope is intentionally narrow: upsert-only, no reconcile
delete, no schema/bootstrap work, no deployment, no restart, no scheduler or
alert mutation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS = REPO_ROOT / "scripts"
for _candidate in (str(REPO_ROOT), str(_SCRIPTS)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

SCHEMA_PROPOSAL = "poliscopic.standing-authorization-proposal/v1"
PROPOSAL_KIND = "standing-authorization-proposal"

OPERATION = "OP-RECON"
ENTRY_POINT = "scripts/db/sync_prod.py"
TARGET = "production"
EXECUTION_MODE = "routine-upsert-only-daily-data-sync"
#: The execution mode this authority confers. Bound into the plan and enforced by
#: the interlock, so it can never satisfy a reconcile/delete or schema request.
SYNC_MODE = "upsert"
VALIDITY_DAYS = 365
MAX_USES = 400

#: Modes a routine daily sync must never use. A delete or a schema change is a
#: separate one-operation authorization with its own approval.
PROHIBITED_MODES = (
    "reconcile",
    "reconcile-only",
    "schema-only",
    "bootstrap-schema",
)
PROHIBITED_ACTIONS = (
    "reconcile deletes (delete propagation)",
    "schema or bootstrap DDL",
    "code deployment or release",
    "service restart or reload",
    "scheduler mutation (create/edit/enable/disable any job)",
    "alert destination or alert policy mutation",
    "production-side repair other than values carried by an otherwise "
    "authorized upsert from a development state whose daily gate passed",
    "any write outside the declared scope",
)
PERMITTED_ACTIONS = (
    "insert-or-update (upsert) of rows from development into production for the "
    "declared scope, and only while every mandatory gate passes",
)

#: Mandatory per-run gates. Every one must be present and satisfied.
GATES: tuple[tuple[str, str], ...] = (
    ("same_day_scrape_receipt",
     "Same-day immutable scrape terminal receipt, not merely launcher exit."),
    ("metrics_valid",
     "Numerical pre/post metrics valid (metrics_status=ok); no '?' or "
     "'unavailable' required fields."),
    ("extraction_complete",
     "Extraction stage complete under its contract."),
    ("entity_gate",
     "Entity receipt enforcement and verification gate pass; no unresolved "
     "provenance."),
    ("completion_checker",
     "Daily completion checker returns COMPLETE for the exact run lineage."),
    ("fresh_preflight",
     "Fresh read-only dev/production preflight bound to the execution request."),
    ("backup_receipt",
     "Fresh protected-production backup receipt, restore-verified under the "
     "applicable backup contract."),
    ("binding_match",
     "Exact code hashes, entry point, target, scope, and operation match the "
     "standing authorization."),
    ("no_unexpected_deletes",
     "No unexpected deletes. Any delete/reconcile request must be a separate "
     "one-operation authorization."),
    ("integrity_postconditions",
     "Parent/reference tables precede dependents; scoped integrity "
     "postconditions pass."),
    ("terminal_receipt",
     "One terminal receipt per attempt; stop on first failure; no automatic "
     "guard weakening or scope expansion."),
)
GATE_IDS = tuple(identifier for identifier, _ in GATES)

# ── refusal codes (stable; a caller matches on these) ────────────────────
MALFORMED = "PROPOSAL_MALFORMED"
TAMPERED = "PROPOSAL_TAMPERED"
PROPOSAL_NOT_AUTHORIZATION = "PROPOSAL_IS_NOT_AN_AUTHORIZATION"
NOT_APPROVED = "PROPOSAL_NOT_APPROVED"
AMBIGUOUS_AUTHORIZATION = "AUTHORIZATION_AMBIGUOUS"
OPERATION_MISMATCH = "OPERATION_MISMATCH"
ENTRY_POINT_MISMATCH = "ENTRY_POINT_MISMATCH"
TARGET_MISMATCH = "TARGET_MISMATCH"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
CODE_MISMATCH = "CODE_HASH_MISMATCH"
WINDOW_NOT_YET_VALID = "WINDOW_NOT_YET_VALID"
WINDOW_EXPIRED = "WINDOW_EXPIRED"
USES_EXHAUSTED = "USES_EXHAUSTED"
GATE_MISSING = "GATE_MISSING"


def routine_scope() -> list[str]:
    """The routine daily sync table set.

    Derived from the SINGLE runtime declaration authority (the same
    ``select_sync_tables``/``ALL_SYNC_TABLES`` the sync loop and the interlock
    scope declaration both use). Never inferred from whichever tables happened to
    change on a given day.
    """
    from db.sync_runtime import select_sync_tables

    return select_sync_tables(None)


def code_hashes(paths: Sequence[str]) -> dict[str, str]:
    """sha256 of each bound file, so a stale binding is detectable."""
    digests: dict[str, str] = {}
    for relative in sorted(paths):
        path = REPO_ROOT / relative
        with path.open("rb") as stream:
            digests[relative] = hashlib.sha256(stream.read()).hexdigest()
    return digests


def proposal_digest(proposal: Mapping[str, Any]) -> str:
    """Digest the proposal content, excluding the digest field itself."""
    body = {key: value for key, value in proposal.items() if key != "digest"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_proposal(
    *,
    code_paths: Sequence[str],
    rollback_owner: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build the non-executable standing-authorization proposal."""
    now = now or datetime.now(timezone.utc)
    scope = routine_scope()
    proposal: dict[str, Any] = {
        "schema": SCHEMA_PROPOSAL,
        "kind": PROPOSAL_KIND,
        # ── by construction: not an authorization ──
        "executable": False,
        "approver": None,
        "authorization": None,
        "requires_human_approval": True,
        "recorded_by": "human approval via scripts/ops/operation_authorization.py",
        # ── envelope ──
        "operation": OPERATION,
        "entry_point": ENTRY_POINT,
        "target": TARGET,
        "execution_mode": EXECUTION_MODE,
        "mode": SYNC_MODE,
        "validity_days": VALIDITY_DAYS,
        "max_uses": MAX_USES,
        "not_before": now.isoformat(),
        "not_after": (now + timedelta(days=VALIDITY_DAYS)).isoformat(),
        "scope": scope,
        "scope_source": ("db.sync_runtime.select_sync_tables(None) — the single "
                         "runtime declaration authority"),
        "code_hashes": code_hashes(code_paths),
        "rollback_owner": rollback_owner,
        "permitted_actions": list(PERMITTED_ACTIONS),
        "prohibited_actions": list(PROHIBITED_ACTIONS),
        "prohibited_modes": list(PROHIBITED_MODES),
        "mandatory_gates": [{"id": identifier, "requirement": text}
                            for identifier, text in GATES],
        "use_accounting": {
            "consumed_only_after": ("a successful, reconciled terminal receipt"),
            "failures_and_refusals": ("do not consume a use and do not reset the "
                                      "count"),
        },
        "summary": (
            f"Permit routine upsert-only development→production data sync ({OPERATION} "
            f"via {ENTRY_POINT}) to {TARGET} for up to {VALIDITY_DAYS} days and "
            f"{MAX_USES} successful runs, over exactly the {len(scope)} declared "
            "tables, and only while every mandatory gate passes. The authorized "
            f"execution mode is '{SYNC_MODE}' — this authority cannot satisfy a "
            "reconcile, reconcile-only, schema-only or bootstrap-schema request. "
            "Deletes, schema changes, deployment, restarts, scheduler changes and "
            "alert changes are NOT permitted by this proposal."
        ),
    }
    proposal["digest"] = proposal_digest(proposal)
    return proposal


def consumes_use(receipt: Mapping[str, Any] | None) -> bool:
    """Whether a terminal receipt consumes one use.

    Only a successful, reconciled terminal receipt consumes a use. A failure, a
    refusal, or a non-terminal record must not consume or reset the count.
    """
    if not isinstance(receipt, Mapping):
        return False
    return (receipt.get("terminal") is True
            and receipt.get("status") == "succeeded"
            and receipt.get("reconciled") is True)


def _refuse(code: str, reason: str, **extra: Any) -> dict[str, Any]:
    verdict = {"status": "REFUSED", "code": code, "reason": reason}
    verdict.update(extra)
    return verdict


def validate_request(
    proposal: Mapping[str, Any] | None,
    *,
    operation: str = "",
    entry_point: str = "",
    target: str = "",
    scope: Sequence[str] | None = None,
    code_hashes_current: Mapping[str, str] | None = None,
    approval: Mapping[str, Any] | None = None,
    approvals_matching: int = 1,
    uses_consumed: int = 0,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a standing proposal against ONE concrete sync request.

    Pure and side-effect free: no credential resolution, no network, no database.
    Returns ``status == "ALLOWED"`` only when every binding matches; otherwise a
    refusal with a stable code.
    """
    now = now or datetime.now(timezone.utc)

    if not isinstance(proposal, Mapping):
        return _refuse(MALFORMED, "no proposal artifact supplied")
    if proposal.get("schema") != SCHEMA_PROPOSAL:
        return _refuse(MALFORMED, f"proposal schema must be {SCHEMA_PROPOSAL!r}")
    if proposal.get("kind") != PROPOSAL_KIND:
        return _refuse(MALFORMED, f"proposal kind must be {PROPOSAL_KIND!r}")

    recorded = proposal.get("digest")
    if not isinstance(recorded, str) or not recorded:
        return _refuse(MALFORMED, "proposal carries no digest")
    if proposal_digest(proposal) != recorded:
        return _refuse(TAMPERED, "proposal digest does not match its contents")

    # A proposal must never be executable, nor carry an approver: if it does, it
    # has been turned into an authorization-shaped artifact and is refused rather
    # than honoured.
    if proposal.get("executable") is not False:
        return _refuse(PROPOSAL_NOT_AUTHORIZATION,
                       "proposal declares itself executable; a proposal may never "
                       "be executable")
    for field in ("approver", "authorization"):
        if proposal.get(field) is not None:
            return _refuse(PROPOSAL_NOT_AUTHORIZATION,
                           f"proposal carries a non-null {field!r}; a proposal may "
                           "not carry an approver or an authorization")

    # Every mandatory gate must be described, so a gate cannot be dropped by
    # editing the proposal. The REQUIRED set is declared by the proposal itself
    # (``required_gate_ids``) so the same boundary can serve proposals with
    # different gate sets — the daily data sync has 11, the newsletter editorial
    # sync has 9. When a proposal does not declare one, the strict daily-sync set
    # is the default, so an older artifact cannot quietly lose gates.
    gates = proposal.get("mandatory_gates")
    if not isinstance(gates, list) or not gates:
        return _refuse(GATE_MISSING, "the proposal declares no mandatory gates")
    malformed = [gate for gate in gates
                 if not isinstance(gate, Mapping) or not str(gate.get("id") or "").strip()
                 or not str(gate.get("requirement") or "").strip()]
    if malformed:
        return _refuse(GATE_MISSING,
                       f"{len(malformed)} mandatory gate(s) are malformed: {malformed}")
    required = proposal.get("required_gate_ids")
    if required is None:
        required = list(GATE_IDS)
    if not isinstance(required, list) or not required:
        return _refuse(GATE_MISSING, "the proposal declares no required gate ids")
    declared = {str(gate["id"]) for gate in gates}
    missing = [identifier for identifier in required if identifier not in declared]
    if missing:
        return _refuse(GATE_MISSING, f"mandatory gates absent: {missing}")

    if approval is None:
        return _refuse(NOT_APPROVED,
                       "no human approval is recorded for this proposal; the "
                       "proposal alone grants nothing")
    if approvals_matching > 1:
        return _refuse(AMBIGUOUS_AUTHORIZATION,
                       f"{approvals_matching} approvals match this request")
    if approvals_matching == 0:
        return _refuse(NOT_APPROVED, "no approval matches this request")

    # Binding identity — an authorization cannot be satisfied by omission.
    if not operation:
        return _refuse(OPERATION_MISMATCH, "request declares no operation")
    if not entry_point:
        return _refuse(ENTRY_POINT_MISMATCH, "request declares no entry point")
    if not target:
        return _refuse(TARGET_MISMATCH, "request declares no target")
    if scope is None:
        return _refuse(SCOPE_MISMATCH, "request declares no scope")

    if operation != proposal.get("operation"):
        return _refuse(OPERATION_MISMATCH,
                       f"proposal is for {proposal.get('operation')!r}, "
                       f"requested {operation!r}")
    if entry_point != proposal.get("entry_point"):
        return _refuse(ENTRY_POINT_MISMATCH,
                       f"proposal is for {proposal.get('entry_point')!r}, "
                       f"requested {entry_point!r}")
    if target != proposal.get("target"):
        return _refuse(TARGET_MISMATCH,
                       f"proposal is for {proposal.get('target')!r}, "
                       f"requested {target!r}")

    proposed_scope = sorted(proposal.get("scope") or [])
    requested_scope = sorted(scope)
    if requested_scope != proposed_scope:
        extra_tables = sorted(set(requested_scope) - set(proposed_scope))
        detail = (f"requested scope is broader than the proposal by {extra_tables}"
                  if extra_tables else "requested scope does not match the proposal")
        return _refuse(SCOPE_MISMATCH, detail,
                       proposed_scope=proposed_scope,
                       requested_scope=requested_scope)

    if code_hashes_current is not None:
        proposed_hashes = proposal.get("code_hashes") or {}
        current = {key: value for key, value in code_hashes_current.items()
                   if key in proposed_hashes}
        changed = sorted(key for key, value in current.items()
                         if proposed_hashes.get(key) != value)
        unknown = sorted(set(code_hashes_current) - set(proposed_hashes))
        if changed or unknown or len(current) != len(proposed_hashes):
            return _refuse(CODE_MISMATCH,
                           f"bound code changed or unbound code present "
                           f"(changed={changed}, unbound={unknown})")

    not_before = _parse(proposal.get("not_before"))
    not_after = _parse(proposal.get("not_after"))
    if not_before is None or not_after is None or not_after <= not_before:
        return _refuse(MALFORMED, "proposal window is missing or inverted")
    if now < not_before:
        return _refuse(WINDOW_NOT_YET_VALID, "proposal window has not begun")
    if now >= not_after:
        return _refuse(WINDOW_EXPIRED, "proposal window has expired")

    maximum = proposal.get("max_uses")
    if not isinstance(maximum, int) or maximum <= 0:
        return _refuse(MALFORMED, "proposal declares no positive max_uses")
    if uses_consumed >= maximum:
        return _refuse(USES_EXHAUSTED,
                       f"authorization used {uses_consumed}/{maximum} times")

    return {
        "status": "ALLOWED",
        "code": None,
        "reason": None,
        "operation": operation,
        "entry_point": entry_point,
        "target": target,
        "scope": requested_scope,
        "use": uses_consumed + 1,
        "max_uses": maximum,
        "gates_required": list(GATE_IDS),
    }


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def write_proposal(proposal: Mapping[str, Any], path: Path) -> Path:
    """Write the proposal once, mode 0600, never overwriting an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(proposal, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    return path


DEFAULT_CODE_PATHS = (
    "scripts/db/sync_prod.py",
    "scripts/db/sync_runtime.py",
    "scripts/db/sync_declarations.py",
    "scripts/ops/production_interlock.py",
    "scripts/ops/operation_authorization.py",
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=("Build the NON-EXECUTABLE standing daily-sync authorization "
                     "proposal. This command authorizes nothing."))
    parser.add_argument("--output", type=Path)
    parser.add_argument("--rollback-owner", default="Pete Mains")
    arguments = parser.parse_args()

    proposal = build_proposal(code_paths=DEFAULT_CODE_PATHS,
                              rollback_owner=arguments.rollback_owner)
    output = arguments.output or (REPO_ROOT / "data" / "standing-proposals" /
                                  "OP-RECON-standing-daily-sync.proposal.json")
    if output.exists():
        print(json.dumps({"refused": "proposal already exists; refusing to overwrite",
                          "path": str(output)}, indent=2))
        return 3
    write_proposal(proposal, output)
    print(json.dumps({
        "kind": proposal["kind"],
        "executable": proposal["executable"],
        "operation": proposal["operation"],
        "entry_point": proposal["entry_point"],
        "target": proposal["target"],
        "validity_days": proposal["validity_days"],
        "max_uses": proposal["max_uses"],
        "scope_tables": len(proposal["scope"]),
        "gates": len(proposal["mandatory_gates"]),
        "digest": proposal["digest"],
        "path": str(output),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
