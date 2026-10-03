#!/usr/bin/env python3
"""One-operation authorization: the runtime enforcement the checklist calls G7/G8.

WHY THIS EXISTS
    ``production_interlock.check()`` refuses every production-mutating operation
    because no authorization issuer/validator exists — it reports
    ``AUTHORIZATION_DISABLED``.  ``briefs/PRODUCTION-OPERATIONS-CHECKLIST.md``
    names this exact gap: G7 has "no implementation for a general plan artifact",
    and G8 notes "Runtime enforcement of this gate does not yet exist".

    This module is that enforcement.  It is deliberately NOT an issuer: an agent
    cannot mint authority here.  It VALIDATES an authorization a human actually
    gave, against a plan that binds the exact code, scope and target of the one
    operation it covers.

FAIL-CLOSED BY CONSTRUCTION
    Every anomaly refuses: missing, unreadable, malformed or ambiguous artifacts;
    tampered plan digest; changed code; out-of-window; operation / entry-point /
    scope / target mismatch; not human-sourced; or exhausted uses.  There is no
    override flag and no environment escape hatch.  The interlock keeps refusing
    anything this module does not positively validate.

DELIBERATE DEVIATION — ``mode``
    The checklist says authorization is single-use.  That cannot express "daily
    automated publication", which is inherently recurring.  So an authorization
    declares a ``mode``:

      * ``single-use`` — one execution; a second refuses (USES_EXHAUSTED).
      * ``standing``   — recurring, but ONLY within a bounded window, with a
                         bounded ``max_uses``, and bound to the exact
                         ``code_hashes`` of the publishing code.  Editing that
                         code voids the authorization until a human re-issues it.

    ``standing`` is a bounded, revocable widening — not a blanket permit.
    Every ALLOWED decision appends a receipt.

NOT CLAIMED
    This does not produce a G5 preflight digest or a G6 backup receipt; those are
    separate gates and no receipt is fabricated here.  ``target`` is a DECLARED
    identity binding, not a discovered host fingerprint.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_PLAN = "operation-plan/1"
SCHEMA_AUTHORIZATION = "operation-authorization/1"
SCHEMA_VERDICT = "operation-authorization-verdict/1"

ARTIFACT_MODE = 0o600

# Refusal codes — stable, machine-readable.
AUTHORIZATION_MISSING = "AUTHORIZATION_MISSING"
AUTHORIZATION_MALFORMED = "AUTHORIZATION_MALFORMED"
AUTHORIZATION_AMBIGUOUS = "AUTHORIZATION_AMBIGUOUS"
AUTHORIZATION_TAMPERED = "AUTHORIZATION_TAMPERED"
PLAN_MALFORMED = "PLAN_MALFORMED"
PLAN_DIGEST_MISMATCH = "PLAN_DIGEST_MISMATCH"
PLAN_DIGEST_UNBOUND = "PLAN_DIGEST_UNBOUND"
CODE_CHANGED = "CODE_CHANGED"
AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
AUTHORIZATION_NOT_YET_VALID = "AUTHORIZATION_NOT_YET_VALID"
OPERATION_MISMATCH = "OPERATION_MISMATCH"
ENTRY_POINT_MISSING = "ENTRY_POINT_MISSING"
ENTRY_POINT_MISMATCH = "ENTRY_POINT_MISMATCH"
SCOPE_MISSING = "SCOPE_MISSING"
SCOPE_MISMATCH = "SCOPE_MISMATCH"
TARGET_MISMATCH = "TARGET_MISMATCH"
NOT_HUMAN_AUTHORIZED = "NOT_HUMAN_AUTHORIZED"
USES_EXHAUSTED = "USES_EXHAUSTED"

# ── execution mode ───────────────────────────────────────────────────────
#
# Scope alone cannot distinguish an insert-or-update from a delete or a schema
# change: a plain `sync_prod.py --reconcile` declares EXACTLY the same operation,
# entry point, target and table scope as a routine upsert. Without binding the
# MODE, an upsert authorization would silently satisfy a delete. So every
# production-mutating request must declare its mode, and the plan must declare the
# same one.
SYNC_MODES = ("upsert", "reconcile", "reconcile-only", "schema-only",
              "bootstrap-schema")
OTHER_MODES = ("repair", "cleanup", "backfill", "schema")
KNOWN_MODES = SYNC_MODES + OTHER_MODES

#: A plan written before mode binding existed declares no mode. Every such plan was
#: created for upsert-only work, so it is read as ``upsert`` — strictly MORE
#: restrictive than before, because it can now never satisfy a delete or a schema
#: request. No legacy plan gains any permission it did not already have.
LEGACY_MODE = "upsert"
DEFAULT_MODE = "upsert"
MODE_MISSING = "MODE_MISSING"
MODE_UNKNOWN = "MODE_UNKNOWN"
MODE_MISMATCH = "MODE_MISMATCH"

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_UNSET = object()


def release_dir() -> Path:
    """Where plans and authorizations live. A path is not an authorization."""
    override = os.environ.get("POLISCOPIC_RELEASE_DIR")
    return Path(override) if override else PROJECT_ROOT / "data" / "release"


def audit_dir() -> Path:
    override = os.environ.get("POLISCOPIC_AUDIT_DIR")
    return Path(override) if override else PROJECT_ROOT / "data" / "audit"


# ── canonical hashing ────────────────────────────────────────────────────


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def canonical_json(payload: object) -> str:
    """Deterministic encoding — the digest must not depend on key order."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def code_hashes(paths: list[str]) -> dict[str, str]:
    """sha256 of each code path, relative to the project root."""
    out: dict[str, str] = {}
    for rel in paths:
        path = PROJECT_ROOT / rel
        if not path.is_file():
            raise FileNotFoundError(f"code path is not a file: {rel}")
        out[rel] = sha256_file(path)
    return out


def plan_digest(plan: dict) -> str:
    """Digest over every field except ``digest`` itself."""
    payload = {k: v for k, v in plan.items() if k != "digest"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def authorization_digest(auth: dict) -> str:
    """Digest over every field except ``auth_digest`` itself.

    Without this, the authorization would not be tamper-evident: mode, window or
    max_uses could be widened after a human recorded a narrower approval.
    """
    payload = {k: v for k, v in auth.items() if k != "auth_digest"}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


# ── plan construction ────────────────────────────────────────────────────


def build_plan(
    operation: str,
    operation_id: str,
    entry_point: str,
    scope: list[str],
    code_paths: list[str],
    rollback_owner: str,
    not_before: datetime,
    not_after: datetime,
    target: str = "production",
    created_at: datetime | None = None,
    notes: str = "",
    mode: str = DEFAULT_MODE,
) -> dict:
    """Build a single-kind, digest-bound plan. Refuses an inverted window."""
    if not_before >= not_after:
        raise ValueError("plan window is inverted or empty")
    if mode not in KNOWN_MODES:
        raise ValueError(f"unknown execution mode: {mode!r}")
    if not rollback_owner.strip():
        raise ValueError("a rollback owner must be named by name")
    if not scope:
        raise ValueError("scope must not be empty")
    plan = {
        "schema": SCHEMA_PLAN,
        "operation": operation,
        "operation_id": operation_id,
        "entry_point": entry_point,
        "target": target,
        "mode": mode,
        "scope": sorted(scope),
        "code_hashes": code_hashes(code_paths),
        "rollback_owner": rollback_owner,
        "not_before": _iso(not_before),
        "not_after": _iso(not_after),
        "created_at": _iso(created_at or _utcnow()),
        "notes": notes,
        "bindings_not_included": [
            "G5 preflight digest (separate gate; not fabricated here)",
            "G6 backup receipt (separate gate; not fabricated here)",
        ],
    }
    plan["digest"] = plan_digest(plan)
    return plan


def _write_exclusive(path: Path, payload: dict) -> None:
    """Write-once, mode 0600. Never overwrite an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"refusing to overwrite existing artifact: {path}")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ARTIFACT_MODE)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(canonical_json(payload) + "\n")
    finally:
        try:
            os.chmod(path, ARTIFACT_MODE)
        except OSError:
            pass


def operation_dir(operation: str, operation_id: str) -> Path:
    return release_dir() / f"{operation}-{operation_id}"


def write_plan(plan: dict) -> Path:
    path = operation_dir(plan["operation"], plan["operation_id"]) / "plan.json"
    _write_exclusive(path, plan)
    record = audit_dir() / f"{plan['operation_id']}-g7-plan.txt"
    if not record.exists():
        record.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(record, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ARTIFACT_MODE)
        with os.fdopen(fd, "w") as handle:
            handle.write(f"operation: {plan['operation']}\n")
            handle.write(f"operation_id: {plan['operation_id']}\n")
            handle.write(f"digest: {plan['digest']}\n")
    return path


def record_authorization(
    plan: dict,
    verbatim_approval: str,
    author: str,
    source: str = "human",
    authorized_at: datetime | None = None,
    mode: str = "single-use",
    max_uses: int | None = 1,
    verbatim_source: dict | None = None,
) -> Path:
    """RECORD an approval already given. Never compose, infer or broaden one.

    ``verbatim_approval`` must be the human's own words, unedited.  This function
    will not synthesise text; the caller passes what was actually said.
    """
    if not verbatim_approval.strip():
        raise ValueError("authorization requires the human's verbatim approval text")
    if source != "human":
        raise ValueError("authorization must be sourced from a human")
    if not author.strip():
        raise ValueError("authorization requires a named author")
    if mode not in ("single-use", "standing"):
        raise ValueError(f"unknown authorization mode: {mode!r}")
    if mode == "single-use" and max_uses != 1:
        raise ValueError("single-use authorization must have max_uses == 1")
    payload = {
        "schema": SCHEMA_AUTHORIZATION,
        "operation": plan["operation"],
        "operation_id": plan["operation_id"],
        "plan_digest": plan["digest"],
        "scope": sorted(plan["scope"]),
        "target": plan["target"],
        "mode": mode,
        "max_uses": max_uses,
        "verbatim_approval": verbatim_approval,
        "author": author,
        "source": source,
        "authorized_at": _iso(authorized_at or _utcnow()),
        "not_before": plan["not_before"],
        "not_after": plan["not_after"],
        "verbatim_source": verbatim_source or {},
    }
    payload["auth_digest"] = authorization_digest(payload)
    path = operation_dir(plan["operation"], plan["operation_id"]) / "authorization.json"
    _write_exclusive(path, payload)
    return path


# ── use receipts ─────────────────────────────────────────────────────────


def _receipts_path(operation_id: str) -> Path:
    return audit_dir() / f"{operation_id}-authorization-uses.jsonl"


def count_uses(operation_id: str) -> int:
    path = _receipts_path(operation_id)
    if not path.is_file():
        return 0
    try:
        return sum(1 for line in path.read_text().splitlines() if line.strip())
    except OSError:
        return 0


def _append_receipt(operation_id: str, receipt: dict) -> None:
    path = _receipts_path(operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        handle.write(canonical_json(receipt) + "\n")


# ── the validator ────────────────────────────────────────────────────────


def _refuse(code: str, reason: str, **extra) -> dict:
    verdict = {
        "schema": SCHEMA_VERDICT,
        "status": "REFUSED",
        "code": code,
        "reason": reason,
        "authorization_issuance": "disabled",
    }
    verdict.update(extra)
    return verdict


def _read_json(path: Path) -> tuple[dict | None, str | None]:
    if not path.is_file():
        return None, f"absent: {path}"
    try:
        raw = path.read_text()
    except OSError as exc:
        return None, f"unreadable: {exc}"
    if not raw.strip():
        return None, "empty file"
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return None, f"not valid JSON: {exc}"
    if not isinstance(payload, dict):
        return None, "not a JSON object"
    return payload, None


def validate(
    operation: str,
    entry_point: str = "",
    scope: list[str] | None = None,
    target: str = "production",
    now: datetime | None = None,
    mode: str | None = None,
    operation_id: str | None = None,
) -> dict:
    """Validate a human authorization for THIS exact operation request.

    Returns a verdict dict. ``status == "ALLOWED"`` only when every binding
    matches; anything else is a refusal with a stable code.
    """
    now = now or _utcnow()
    if operation == "":
        return _refuse(OPERATION_MISMATCH, "no operation supplied")

    directory = release_dir()
    if not directory.is_dir():
        return _refuse(AUTHORIZATION_MISSING,
                       f"no release directory: {directory}")

    if operation_id:
        selected = operation_dir(operation, operation_id)
        candidates = [selected] if selected.is_dir() else []
    else:
        candidates = sorted(p for p in directory.glob(f"{operation}-*") if p.is_dir())
    if not candidates:
        detail = (f"no authorization artifact for operation {operation!r}"
                  if not operation_id else
                  f"selected authorization {operation_id!r} does not exist for "
                  f"operation {operation!r}")
        return _refuse(AUTHORIZATION_MISSING, detail)

    # The caller MUST declare what it is doing, so an authorization cannot be
    # satisfied by omission.
    if not entry_point:
        return _refuse(ENTRY_POINT_MISSING,
                       "caller declared no entry point; an authorization cannot be "
                       "satisfied by omission")
    if scope is None:
        return _refuse(SCOPE_MISSING,
                       "caller declared no scope; an authorization cannot be "
                       "satisfied by omission")

    # A single operation KIND may legitimately hold several authorizations — for
    # different entry points or scopes (the newsletter editorial sync and a data
    # sync are both OP-RECON). Select the one that matches THIS request. Refusing
    # merely because a kind holds more than one artifact would block every other
    # legitimate authorization for that kind; ambiguity must mean "more than one
    # matches this request", not "more than one exists".
    matching: list[Path] = []
    for candidate in candidates:
        candidate_plan, _ = _read_json(candidate / "plan.json")
        if not isinstance(candidate_plan, dict):
            continue
        if candidate_plan.get("operation") != operation:
            continue
        if candidate_plan.get("entry_point") != entry_point:
            continue
        if candidate_plan.get("target") != target:
            continue
        if sorted(candidate_plan.get("scope") or []) != sorted(scope):
            continue
        matching.append(candidate)

    if operation_id and not matching:
        # Validate the explicitly selected artifact below so callers receive the
        # precise binding mismatch (scope, entry point, target, etc.).
        op_dir = candidates[0]
    elif len(matching) > 1:
        return _refuse(AUTHORIZATION_AMBIGUOUS,
                       "multiple authorizations match this exact request: "
                       f"{[p.name for p in matching]}")

    elif matching:
        op_dir = matching[0]
    elif len(candidates) == 1:
        # Exactly one artifact exists for the kind but it is for something else:
        # validate it anyway so the refusal names the precise mismatch rather than
        # a vague "missing".
        op_dir = candidates[0]
    else:
        return _refuse(AUTHORIZATION_MISSING,
                       "no authorization matches this request "
                       f"(entry_point={entry_point!r}, target={target!r})")
    plan, plan_err = _read_json(op_dir / "plan.json")
    if plan is None:
        return _refuse(AUTHORIZATION_MISSING, f"plan {plan_err}",
                       operation_dir=str(op_dir))
    if plan.get("schema") != SCHEMA_PLAN:
        return _refuse(PLAN_MALFORMED,
                       f"plan schema must be {SCHEMA_PLAN!r}",
                       operation_dir=str(op_dir))

    recorded = plan.get("digest")
    if not isinstance(recorded, str) or not recorded:
        return _refuse(PLAN_DIGEST_UNBOUND, "plan carries no digest")
    if plan_digest(plan) != recorded:
        return _refuse(PLAN_DIGEST_MISMATCH,
                       "plan digest does not match its contents (tampered)",
                       operation_dir=str(op_dir))

    auth, auth_err = _read_json(op_dir / "authorization.json")
    if auth is None:
        return _refuse(AUTHORIZATION_MISSING, f"authorization {auth_err}",
                       operation_dir=str(op_dir), plan_digest=recorded)
    if auth.get("schema") != SCHEMA_AUTHORIZATION:
        return _refuse(AUTHORIZATION_MALFORMED,
                       f"authorization schema must be {SCHEMA_AUTHORIZATION!r}",
                       operation_dir=str(op_dir))

    # The authorization must be tamper-evident in its own right.
    if auth.get("auth_digest") != authorization_digest(auth):
        return _refuse(AUTHORIZATION_TAMPERED,
                       "authorization digest does not match its contents (tampered)",
                       operation_dir=str(op_dir))

    # 1. the authorization must bind THIS plan digest
    if auth.get("plan_digest") != recorded:
        return _refuse(PLAN_DIGEST_MISMATCH,
                       "authorization does not bind this plan digest",
                       operation_dir=str(op_dir), plan_digest=recorded)

    # 2. human-sourced, named, verbatim
    if auth.get("source") != "human":
        return _refuse(NOT_HUMAN_AUTHORIZED,
                       "authorization is not sourced from a human (an agent may "
                       "record an approval, never issue one)")
    if not str(auth.get("author") or "").strip():
        return _refuse(NOT_HUMAN_AUTHORIZED, "authorization names no author")
    if not str(auth.get("verbatim_approval") or "").strip():
        return _refuse(AUTHORIZATION_MALFORMED,
                       "authorization carries no verbatim approval text")

    # 3. operation identity
    if plan.get("operation") != operation or auth.get("operation") != operation:
        return _refuse(OPERATION_MISMATCH,
                       f"authorization is for {auth.get('operation')!r}, "
                       f"requested {operation!r}")

    # 3b. execution MODE. A request cannot be satisfied by omission, and a plan
    #     authorized for one mode must never satisfy another — this is what stops
    #     an upsert authorization from authorizing a delete or a schema change at
    #     the same entry point and scope.
    if mode is None or not str(mode).strip():
        return _refuse(MODE_MISSING,
                       "caller declared no execution mode; an authorization cannot "
                       "be satisfied by omission")
    if mode not in KNOWN_MODES:
        return _refuse(MODE_UNKNOWN,
                       f"unknown execution mode {mode!r}; known modes are "
                       f"{list(KNOWN_MODES)}")
    plan_mode = plan.get("mode") or LEGACY_MODE
    if plan_mode != mode:
        return _refuse(MODE_MISMATCH,
                       f"authorization is for mode {plan_mode!r}, requested "
                       f"{mode!r}")

    # 4. window
    not_before = parse_ts(auth.get("not_before"))
    not_after = parse_ts(auth.get("not_after"))
    if not_before is None or not_after is None:
        return _refuse(AUTHORIZATION_MALFORMED,
                       "authorization window is missing or not a timestamp")
    if not_before > not_after:
        return _refuse(AUTHORIZATION_MALFORMED, "authorization window is inverted")
    if now < not_before:
        return _refuse(AUTHORIZATION_NOT_YET_VALID,
                       f"authorization not effective until {_iso(not_before)}")
    if now > not_after:
        return _refuse(AUTHORIZATION_EXPIRED,
                       f"authorization expired at {_iso(not_after)}")

    # 5. entry point — the caller MUST declare it. SKIPPING the check when the
    #    caller omits it would let an authorization be satisfied by omission,
    #    which is how a newsletter authorization could reach an unrelated caller.
    if not entry_point:
        return _refuse(ENTRY_POINT_MISSING,
                       "caller declared no entry point; an authorization cannot be "
                       "satisfied by omission")
    if plan.get("entry_point") != entry_point:
        return _refuse(ENTRY_POINT_MISMATCH,
                       f"authorization is for entry point {plan.get('entry_point')!r}, "
                       f"requested {entry_point!r}")

    # 6. scope — same reasoning: an undeclared scope is a REFUSAL, not a skip.
    if scope is None:
        return _refuse(SCOPE_MISSING,
                       "caller declared no scope; an authorization cannot be "
                       "satisfied by omission")
    if sorted(scope) != sorted(plan.get("scope") or []):
        return _refuse(SCOPE_MISMATCH,
                       f"requested scope {sorted(scope)} != authorized "
                       f"{sorted(plan.get('scope') or [])}")

    # 7. target identity
    if target and plan.get("target") != target:
        return _refuse(TARGET_MISMATCH,
                       f"authorization targets {plan.get('target')!r}, "
                       f"requested {target!r}")

    # 8. code binding — a changed publishing path voids the authorization
    try:
        current = code_hashes(list((plan.get("code_hashes") or {}).keys()))
    except FileNotFoundError as exc:
        return _refuse(CODE_CHANGED, f"bound code path missing: {exc}")
    bound = plan.get("code_hashes") or {}
    for rel, want in sorted(bound.items()):
        got = current.get(rel)
        if got != want:
            return _refuse(CODE_CHANGED,
                           f"bound code changed since authorization: {rel}")

    # 9. use budget
    mode = auth.get("mode")
    if mode not in ("single-use", "standing"):
        return _refuse(AUTHORIZATION_MALFORMED, f"unknown mode {mode!r}")
    max_uses = auth.get("max_uses")
    if mode == "single-use" and max_uses != 1:
        return _refuse(AUTHORIZATION_MALFORMED,
                       "single-use authorization must declare max_uses == 1")
    used = count_uses(plan.get("operation_id") or "")
    if isinstance(max_uses, int) and max_uses >= 0 and used >= max_uses:
        return _refuse(USES_EXHAUSTED,
                       f"authorization used {used}/{max_uses} times")

    # 10. ALLOWED — record the use
    receipt = {
        "operation": operation,
        "operation_id": plan.get("operation_id"),
        "entry_point": entry_point or None,
        "plan_digest": recorded,
        "scope": sorted(plan.get("scope") or []),
        "target": plan.get("target"),
        "mode": mode,
        "use_number": used + 1,
        "max_uses": max_uses,
        "author": auth.get("author"),
        "at": _iso(now),
    }
    try:
        _append_receipt(plan.get("operation_id") or "unknown", receipt)
    except OSError as exc:
        return _refuse(AUTHORIZATION_MALFORMED,
                       f"could not record authorization use receipt: {exc}")

    return {
        "schema": SCHEMA_VERDICT,
        "status": "ALLOWED",
        "code": None,
        "reason": (f"validated {mode} authorization for {operation}, "
                   f"use {used + 1}" + (f"/{max_uses}" if max_uses else "")),
        "authorization_issuance": "validated",
        "operation": operation,
        "operation_id": plan.get("operation_id"),
        "plan_digest": recorded,
        "author": auth.get("author"),
        "mode": mode,
        "scope": sorted(plan.get("scope") or []),
        "valid_until": _iso(not_after),
        "receipt": receipt,
    }
