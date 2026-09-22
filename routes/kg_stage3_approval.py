"""Review and authorization-record workspace for the Stage 3 receipt continuation.

This is a **review boundary, not an execution surface**.

* The GET path only reads immutable repository artifacts.
* The POST may create exactly one separate, immutable, mode-0600 human-approval
  record.  It never mutates the proposal, never writes a receipt, and never runs a
  backfill.
* Nothing here calls the authorization builder, the preflight builder, the runner,
  or a database.  A later, separately bounded task consumes an approved record to
  generate the distinct authorized packet and a fresh preflight.
* Approving records authorization.  It does not execute anything.

Every binding is reloaded and re-validated server-side; form fields are treated as
claims to be checked, never as facts to be trusted.  The approval ledger lives in its
own directory under its own name so it cannot match any runner terminal, preflight, or
checkpoint glob.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from flask import Blueprint, abort, render_template, request, session

from scripts.kg import stage3_processing_receipt_apply as apply
from scripts.kg import stage3_processing_receipt_apply_proposal as proposal_mod
from scripts.kg import stage3_processing_receipt as receipt
from scripts.kg.stage2_artifacts import is_obsolete, load_verified

kg_stage3_approval_bp = Blueprint("kg_stage3_approval", __name__,
                                  url_prefix="/kg/stage3-approval")

REPO = Path(__file__).resolve().parent.parent
PROPOSAL_PATH = REPO / "data/kg-plans/kg-stage3-processing-receipt-proposal-20260921T195816Z.json"
PLAN_PATH = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260921T193158Z.json"
DESIGN_PATH = REPO / "data/kg-plans/kg-stage3-processing-receipt-store-packet-20260921T194940Z.json"
RECEIPT_SET_PATH = REPO / "data/kg-plans/kg-stage3-processing-receipt-set-20260921T192601Z.json"
BACKUP_PATH = REPO / "data/backups/kg-stage2-backup-receipt-20260921T184835Z.json"
SCHEDULE_PATH = REPO / "data/kg-plans/kg-stage3-receipt-batch-schedule-20260921T195036Z.json"
APPROVAL_DIR = REPO / "data/kg-approvals"

EXPECTED = {
    "proposal": "f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60",
    "plan": "73359d2df800b5d8f4e1a399bfc925141e4617e82f0509354470e6f4478eb2f9",
    "design_packet": "ae86c35948ca6e214e5699e799897811b2517d285e003c4126c7c6a44fea852c",
    "receipt_set": "95f8e5d04636624c679144e5b9e8f938abf1bfc816bb39f69cfc22bb05bd8a0c",
    "backup_receipt": "3eb11bbec9bdbff7c9e7ecd5a46a0f17709c318cdebbac4425fe03d3afbccd7e",
    "schedule": "117da8dd098d7483de49985e52545f28d1817e71c4293fb00714a2c2950616b2",
}
EXPECTED_TARGET = {"tier": "development", "database": "poliscopic_dev"}
CURRENT_CURSOR = 6100
REMAINING_WRITES = 58628
BATCH_COUNT = 120
BATCH_SIZE = 500
HELD_AFTER_CURSOR = 996
EXISTING_REPLAY = 6095
HELD_IN_CONSUMED_PREFIX = 5

APPROVAL_KIND = "kg-stage3-processing-receipt-human-approval"
APPROVAL_VERSION = "1.0"

#: This approval surface is unavailable unless a dedicated process enables it.  The
#: blueprint existing is never sufficient: the main application does not register it, and
#: a disabled process answers 404 rather than revealing a usable endpoint.
ENABLE_FLAG = "POLISCOPIC_STAGE3_APPROVAL_UI"
ENABLED_VALUE = "1"
CSRF_SESSION_KEY = "stage3_approval_csrf"
LOOPBACK_ADDRESSES = ("127.0.0.1", "::1")


def enabled() -> bool:
    """True only for a process that explicitly opted in."""
    return os.environ.get(ENABLE_FLAG, "").strip() == ENABLED_VALUE


def csrf_token() -> str:
    """A cryptographically random token bound to this session.

    The token is minted per session and compared in constant time on submission, so a
    request from another session, a missing token, or a guessed token is refused.  It is
    consumed after a successful submission, so replaying the same form is refused too.
    """
    token = session.get(CSRF_SESSION_KEY)
    if not isinstance(token, str) or not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _guard() -> None:
    """Refuse before any other work: disabled by default, and loopback only.

    The loopback check is defence in depth and never a substitute for the flag: a proxy
    can make a remote caller look local, so the endpoint must also be explicitly enabled
    by a dedicated launcher for the request to be considered at all.
    """
    if not enabled():
        abort(404)
    if request.remote_addr not in LOOPBACK_ADDRESSES:
        abort(403)

CHECKLIST = (
    ("development_target",
     "I confirm the target is the development database poliscopic_dev, and never production."),
    ("append_only_scope",
     "I confirm every write is an append-only processing receipt, and no existing receipt changes."),
    ("held_excluded",
     "I confirm held rows are never written, including the five held rows inside the consumed prefix."),
    ("no_production_operations",
     "I confirm this excludes production, scraping, syncing, deployment, restarts, and alerts."),
    ("verified_backup",
     "I confirm a verified successor backup is bound to this approval."),
    ("stop_on_failure",
     "I confirm the run stops on the first failure rather than continuing."),
)


def approval_wording() -> str:
    """The exact text a reviewer approves; bound to the proposal and the counts."""
    return (
        f"I have reviewed the Stage 3 processing-receipt continuation proposal "
        f"{EXPECTED['proposal']} and approve it for the development database only: "
        f"{REMAINING_WRITES:,} new append-only processing receipts across {BATCH_COUNT} batches "
        f"of at most {BATCH_SIZE} rows, starting strictly after the proven cursor "
        f"{CURRENT_CURSOR}. That consumed prefix already holds {EXISTING_REPLAY:,} receipts "
        f"classified as replay no-ops and {HELD_IN_CONSUMED_PREFIX} held rows; the run begins "
        f"after them and does not iterate or replay them. The {HELD_AFTER_CURSOR} held rows "
        f"after the cursor are never written, and no supporting_documents.swept_at is rewritten. "
        f"I understand that approving records authorization only and does not execute anything."
    )


def _load(path: Path, *, label: str, expected: str) -> dict[str, Any]:
    if not path.is_file():
        abort(409, f"The {label} artifact is not available on this server.")
    if is_obsolete(path):
        abort(409, f"The {label} artifact is obsolete and must not be approved.")
    try:
        document = load_verified(path)
    except Exception as exc:  # a tampered or truncated artifact is a refusal, not a crash
        abort(409, f"The {label} artifact failed verification: {exc}")
    if document.get("digest") != expected:
        abort(409, f"The {label} artifact no longer matches the reviewed binding.")
    return document


def _operation() -> dict[str, Any]:
    """Reload and re-validate every binding server-side.  No form value is trusted."""
    proposal = _load(PROPOSAL_PATH, label="proposal", expected=EXPECTED["proposal"])
    plan = _load(PLAN_PATH, label="plan", expected=EXPECTED["plan"])
    design = _load(DESIGN_PATH, label="design packet", expected=EXPECTED["design_packet"])
    receipt_set = _load(RECEIPT_SET_PATH, label="receipt set", expected=EXPECTED["receipt_set"])
    backup = _load(BACKUP_PATH, label="backup receipt", expected=EXPECTED["backup_receipt"])
    schedule = _load(SCHEDULE_PATH, label="schedule", expected=EXPECTED["schedule"])

    if not proposal_mod.is_proposal(proposal):
        abort(409, "The proposal artifact does not declare the non-executable proposal contract.")
    problems = proposal_mod.validate_proposal(proposal, plan=plan, design_packet=design)
    if problems:
        abort(409, f"The proposal artifact does not validate: {problems[0]}")
    if proposal.get("backup_receipt_digest") != EXPECTED["backup_receipt"]:
        abort(409, "The proposal is not bound to the verified successor backup receipt.")
    if proposal.get("batch_size") != BATCH_SIZE:
        abort(409, "The proposal batch size does not match the reviewed schedule.")
    target = dict(proposal.get("target") or {})
    if target.get("tier") != "development" or target.get("database") != EXPECTED_TARGET["database"]:
        abort(409, "This review is bound to the development target only.")
    code_digest = str(proposal.get("code_digest") or "")
    if not code_digest or apply.code_digest() != code_digest:
        abort(409, "The proposal's code binding is stale and must be regenerated before approval.")

    if schedule.get("plan_digest") != EXPECTED["plan"]:
        abort(409, "The schedule is not bound to the reviewed plan.")
    if schedule.get("cursor") != CURRENT_CURSOR:
        abort(409, "The schedule cursor does not match the proven continuation cursor.")
    totals = schedule.get("totals") or {}
    if int(totals.get("expected_writes", -1)) != REMAINING_WRITES:
        abort(409, "The schedule's expected write count does not match the reviewed counts.")
    if len(schedule.get("batches") or []) != BATCH_COUNT:
        abort(409, "The schedule's batch count does not match the reviewed counts.")
    outcomes = totals.get("expected_outcomes") or {}
    if int(outcomes.get("held", -1)) != HELD_AFTER_CURSOR:
        abort(409, "The schedule's held-row count does not match the reviewed counts.")
    if int(receipt_set.get("count", -1)) != EXISTING_REPLAY:
        abort(409, "The receipt set count does not match the reviewed replay count.")

    return {
        "proposal": proposal, "plan": plan, "design_packet": design, "receipt_set": receipt_set,
        "backup_receipt": backup, "schedule": schedule, "code_digest": code_digest,
        "target": target,
    }


def _record_path(proposal_digest: str) -> Path:
    return APPROVAL_DIR / f"kg-stage3-approval-record-{proposal_digest}.json"


def _record_digest(body: Mapping[str, Any]) -> str:
    return receipt.canonical_sha256({key: value for key, value in body.items()
                                     if key != "digest"})


def _publish(path: Path, document: Mapping[str, Any]) -> None:
    """Write once, exclusively: temp file, fsync, then an atomic exclusive publish.

    ``os.link`` is used rather than ``os.replace`` so the publish cannot overwrite an
    existing record even under a race: a duplicate submission raises FileExistsError
    instead of silently replacing an earlier human decision.
    """
    APPROVAL_DIR.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".approval-", suffix=".tmp", dir=APPROVAL_DIR)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.link(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _review_context() -> dict[str, Any]:
    operation = _operation()
    record_path = _record_path(EXPECTED["proposal"])
    return {
        "operation": operation,
        "wording": approval_wording(),
        "checklist": CHECKLIST,
        "counts": {
            "writes": REMAINING_WRITES, "batches": BATCH_COUNT, "batch_size": BATCH_SIZE,
            "cursor": CURRENT_CURSOR, "held_after_cursor": HELD_AFTER_CURSOR,
            "existing_replay": EXISTING_REPLAY,
            "held_in_consumed_prefix": HELD_IN_CONSUMED_PREFIX,
        },
        "expected": EXPECTED,
        "record_path": record_path,
        "csrf_token": csrf_token(),
        "existing_record": load_verified(record_path) if record_path.is_file() else None,
    }


@kg_stage3_approval_bp.get("/")
def review():
    _guard()
    return render_template("kg_stage3_approval.html", page="review", **_review_context())


@kg_stage3_approval_bp.post("/approve")
def approve():
    _guard()
    submitted_token = str(request.form.get("csrf_token") or "")
    expected_token = session.get(CSRF_SESSION_KEY)
    if not submitted_token or not isinstance(expected_token, str) or not expected_token \
            or not secrets.compare_digest(submitted_token, expected_token):
        abort(400, "The form token is missing, wrong, or belongs to another session.")
    context = _review_context()
    operation = context["operation"]
    errors: list[str] = []

    reviewer = " ".join(str(request.form.get("reviewer_name") or "").split())
    if not reviewer:
        errors.append("Your name is required. The approval record must name its reviewer.")
    if request.form.get("confirm_digest") != EXPECTED["proposal"]:
        errors.append("The confirmation does not match the exact bound proposal.")
    if str(request.form.get("approval_text") or "").strip() != approval_wording():
        errors.append("The approval wording does not match the exact reviewed wording.")
    missing = [label for key, label in CHECKLIST if request.form.get(f"ack_{key}") != "yes"]
    if missing:
        errors.append(f"{len(missing)} acknowledgement(s) are unchecked; all are required.")
    if errors:
        return render_template("kg_stage3_approval.html", page="review", errors=errors,
                               submitted=request.form, **context), 400

    body = {
        "kind": APPROVAL_KIND,
        "version": APPROVAL_VERSION,
        "reviewer_name": reviewer,
        "approval_text": approval_wording(),
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "proposal_digest": EXPECTED["proposal"],
        "plan_digest": EXPECTED["plan"],
        "design_packet_digest": EXPECTED["design_packet"],
        "receipt_set_digest": EXPECTED["receipt_set"],
        "backup_receipt_digest": EXPECTED["backup_receipt"],
        "schedule_digest": EXPECTED["schedule"],
        "code_digest": operation["code_digest"],
        "target": dict(operation["target"]),
        "scope": {
            "cursor": CURRENT_CURSOR, "batches": BATCH_COUNT, "batch_size": BATCH_SIZE,
            "remaining_writes": REMAINING_WRITES, "held_after_cursor": HELD_AFTER_CURSOR,
            "existing_receipts_replayed": EXISTING_REPLAY,
            "held_in_consumed_prefix": HELD_IN_CONSUMED_PREFIX,
            "stop_on_first_failure": True, "append_only": True,
            "swept_at_rewrite": False,
        },
        "acknowledged": [key for key, _label in CHECKLIST],
        "execution": {"executed": False, "executed_at": None,
                      "authorization_packet": None, "preflight": None},
        "review_boundary": {
            "records_authorization_only": True,
            "executes_nothing": True,
            "is_apply_terminal_receipt": False,
        },
    }
    body["digest"] = _record_digest(body)

    path = context["record_path"]
    if path.exists():
        abort(409, "An approval record for this proposal already exists and cannot be replaced.")
    try:
        _publish(path, body)
    except FileExistsError:
        abort(409, "An approval record for this proposal already exists and cannot be replaced.")

    stored = load_verified(path)
    session.pop(CSRF_SESSION_KEY, None)
    return render_template("kg_stage3_approval.html", page="confirmed", record=stored,
                           **context)
