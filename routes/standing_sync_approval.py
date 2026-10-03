#!/usr/bin/env python3
"""Human review surface for the standing daily-sync authorization PROPOSAL.

Boundary rules, mirroring the corrected Stage 3 approval pattern:

* **Not reachable from the main application.** The blueprint is only mounted by
  ``scripts/ops/standing_sync_approval_serve.py``; the main app never registers
  it, and the route 404s unless the opt-in flag is set in THIS process.
* **Approval records authorization only.** The POST writes one immutable,
  mode-0600 human-approval record and executes nothing: no sync, no database, no
  subprocess. The record lives outside ``data/release`` so the production
  interlock — which reads only ``data/release/<operation>-<id>/authorization.json``
  — can never honour it as an authorization.
* **The proposal cannot be executable.** A proposal that declares itself
  executable, or carries an approver or an authorization, is refused outright.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, abort, render_template, request, session

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = REPO_ROOT / "scripts"
for _candidate in (str(REPO_ROOT), str(_SCRIPTS)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.ops import standing_authorization_proposal as proposal_mod  # noqa: E402

ENABLE_FLAG = "POLISCOPIC_STANDING_SYNC_APPROVAL_UI"
ENABLED_VALUE = "1"
CSRF_SESSION_KEY = "standing_sync_approval_csrf"
RECORD_KIND = "standing-authorization-approval-record"

standing_sync_approval_bp = Blueprint(
    "standing_sync_approval", __name__, url_prefix="/ops/standing-sync-approval")


def enabled() -> bool:
    """Whether THIS process opted into the review surface."""
    return os.environ.get(ENABLE_FLAG, "").strip() == ENABLED_VALUE


def proposal_path() -> Path:
    override = os.environ.get("POLISCOPIC_STANDING_PROPOSAL")
    return (Path(override) if override
            else REPO_ROOT / "data" / "standing-proposals" /
            "OP-RECON-standing-daily-sync.proposal.json")


def record_dir() -> Path:
    override = os.environ.get("POLISCOPIC_STANDING_APPROVAL_DIR")
    return Path(override) if override else REPO_ROOT / "data" / "standing-approvals"


def csrf_token() -> str:
    """A random token bound to this session, minted on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _guard() -> None:
    """404 when the surface is off; 403 without this session's token."""
    if not enabled():
        abort(404)
    submitted = str(request.form.get("csrf_token")
                    or request.args.get("csrf_token") or "")
    expected = session.get(CSRF_SESSION_KEY)
    if not isinstance(expected, str) or not submitted:
        abort(403)
    if not secrets.compare_digest(submitted, expected):
        abort(403)


def _load_proposal() -> dict[str, Any]:
    """Load and verify the proposal, refusing anything authorization-shaped."""
    path = proposal_path()
    if not path.is_file():
        abort(409, f"The proposal artifact is not available ({path}).")
    try:
        proposal = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        abort(409, f"The proposal artifact could not be read: {exc}")
    if not isinstance(proposal, dict):
        abort(409, "The proposal artifact is not a JSON object.")
    verdict = proposal_mod.validate_request(
        proposal,
        # Validate against the envelope the proposal ITSELF declares, so the same
        # corrected boundary serves any proposal built by the contract (the daily
        # data sync and the newsletter editorial sync have different entry points,
        # scopes and modes).
        operation=str(proposal.get("operation") or ""),
        entry_point=str(proposal.get("entry_point") or ""),
        target=str(proposal.get("target") or ""),
        scope=list(proposal.get("scope") or []),
        approval={"present": True})
    # validate_request returns NOT_APPROVED-shaped results for missing approvals;
    # here we only care about structural and binding refusals.
    if verdict.get("code") == proposal_mod.MALFORMED:
        abort(409, f"The proposal artifact does not validate: {verdict['reason']}")
    if verdict.get("code") == proposal_mod.TAMPERED:
        abort(409, "The proposal digest does not match its contents.")
    if verdict.get("code") == proposal_mod.PROPOSAL_NOT_AUTHORIZATION:
        abort(409, f"The proposal is authorization-shaped and is refused: "
                   f"{verdict['reason']}")
    if verdict.get("code") == proposal_mod.GATE_MISSING:
        abort(409, f"The proposal is incomplete: {verdict['reason']}")
    # A proposal is only reviewable while every code binding still matches the
    # live file.  Checking only the stored digest proves that the artifact was not
    # edited; it does not prove that the implementation stayed unchanged between
    # proposal generation and human review.  Refuse that race before rendering a
    # page or accepting a POST.
    bound_hashes = dict(proposal.get("code_hashes") or {})
    try:
        current_hashes = proposal_mod.code_hashes(list(bound_hashes))
    except (OSError, ValueError) as exc:
        abort(409, f"The proposal's bound code could not be verified: {exc}")
    changed = sorted(path for path, expected in bound_hashes.items()
                     if current_hashes.get(path) != expected)
    if changed:
        abort(409, "The proposal is stale because bound code changed: "
              + ", ".join(changed))
    return proposal


def _review_context(proposal: Mapping[str, Any], errors=()) -> dict[str, Any]:
    return {
        "csrf_token": csrf_token(),
        "errors": list(errors),
        "digest": proposal["digest"],
        "summary": proposal["summary"],
        "operation": proposal["operation"],
        "entry_point": proposal["entry_point"],
        "target": proposal["target"],
        "execution_mode": proposal["execution_mode"],
        "validity_days": proposal["validity_days"],
        "max_uses": proposal["max_uses"],
        "not_before": proposal["not_before"],
        "not_after": proposal["not_after"],
        "scope": proposal["scope"],
        "gates": proposal["mandatory_gates"],
        "permitted_actions": proposal["permitted_actions"],
        "prohibited_actions": proposal["prohibited_actions"],
        "code_hashes": proposal["code_hashes"],
        "use_accounting": proposal["use_accounting"],
    }


def _record_path(digest: str) -> Path:
    return record_dir() / f"OP-RECON-standing-{digest[:16]}.approval.json"


def _record_digest(body: Mapping[str, Any]) -> str:
    canonical = json.dumps({k: v for k, v in body.items() if k != "record_digest"},
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _publish(path: Path, document: Mapping[str, Any]) -> None:
    """Write once, mode 0600, never replacing an existing record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        abort(409, "An approval record for this proposal already exists and cannot "
                   "be replaced.")
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())


@standing_sync_approval_bp.get("/")
def review():
    """The plain-English review page."""
    if not enabled():
        abort(404)
    proposal = _load_proposal()
    return render_template("standing_sync_approval.html", page="review",
                           **_review_context(proposal))


@standing_sync_approval_bp.post("/approve")
def approve():
    """Record the human's approval. Executes nothing."""
    _guard()
    proposal = _load_proposal()

    errors: list[str] = []
    reviewer = str(request.form.get("reviewer") or "").strip()
    confirmed = str(request.form.get("confirm_digest") or "").strip()
    acknowledged = request.form.get("acknowledge_recurring")

    if not reviewer:
        errors.append("Enter your name: an approval must name its author.")
    if not secrets.compare_digest(confirmed, proposal["digest"]):
        errors.append("Type the proposal digest exactly as shown. "
                      "Approval is bound to that exact digest.")
    if acknowledged != "yes":
        errors.append("You must acknowledge that this permits recurring production "
                      "upserts, and only while every mandatory gate passes.")
    if errors:
        return render_template("standing_sync_approval.html", page="review",
                               **_review_context(proposal, errors)), 400

    body: dict[str, Any] = {
        "kind": RECORD_KIND,
        "schema": "poliscopic.standing-authorization-approval/v1",
        "proposal_digest": proposal["digest"],
        "operation": proposal["operation"],
        "entry_point": proposal["entry_point"],
        "target": proposal["target"],
        "execution_mode": proposal["execution_mode"],
        "validity_days": proposal["validity_days"],
        "max_uses": proposal["max_uses"],
        "scope": proposal["scope"],
        "code_hashes": proposal["code_hashes"],
        "gate_ids": [gate["id"] for gate in proposal["mandatory_gates"]],
        "permitted_actions": proposal["permitted_actions"],
        "prohibited_actions": proposal["prohibited_actions"],
        "reviewer": reviewer,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "acknowledged_recurring_upserts_only_while_gates_pass": True,
        # Explicitly NOT an authorization and NOT an execution.
        "executed": False,
        "authorizes_operation": False,
        "effect": ("records the human's approval of the proposal; confers no "
                   "execution. A recurring authorization must still be recorded "
                   "through scripts/ops/operation_authorization.py against a real "
                   "plan, with the human's verbatim words."),
    }
    body["record_digest"] = _record_digest(body)
    path = _record_path(proposal["digest"])
    _publish(path, body)

    session.pop(CSRF_SESSION_KEY, None)  # one-shot: consumed on success
    return render_template("standing_sync_approval.html", page="confirmed",
                           record=body, record_path=str(path))
