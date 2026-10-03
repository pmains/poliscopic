#!/usr/bin/env python3
"""Fail-closed production interlock — the single production-safety authority.

PURPOSE
    Every production-writing entry point must refuse BEFORE any network, SSH,
    database, rsync, restart, or subprocess that can reach production, unless a
    future, separately implemented one-operation authorization validator succeeds.

    No such issuer/validator exists today, and issuance is DISABLED. Therefore
    **every production-mutating operation is refused, unconditionally**. Read-only
    modes are allowed only because they are classified read-only here and are
    tested as such.

WHAT THIS IS NOT
    The repository cannot authorize production. A code commit, a deployment, an
    environment variable, a historical receipt, or the mere absence of a hold file
    is NEVER authorization. There is deliberately no `--force`, no override flag,
    and no environment escape hatch.

DEPENDENCY-LIGHT: standard library only. No network, no database, no credentials.

CLI
    production_interlock.py check --operation OP-RECON [--entry-point sync.sh]
    production_interlock.py classify --operation OP-STATUS
    production_interlock.py hold-status
    production_interlock.py kinds

    Exit codes: 0 allowed (read-only only), 3 refused (production interlock),
    4 fail-closed usage/unknown input.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "production-interlock/1"

# ── operation classification ─────────────────────────────────────────────
# The six kinds from briefs/PRODUCTION-OPERATIONS-CHECKLIST.md §1, plus the
# explicit read-only kinds used by status/preflight code paths.
PRODUCTION_MUTATING = {
    "OP-CODE": "code deployment",
    "OP-SCHEMA": "schema migration",
    "OP-REPAIR": "bounded production data repair",
    "OP-RECON": "ordinary dev-to-production reconciliation",
    "OP-RESTORE": "rollback / restore",
}
READ_ONLY = {
    "OP-DEV": "development scrape/ingestion (no production access)",
    "OP-STATUS": "read-only production status report",
    "OP-PREFLIGHT": "read-only production identity and integrity preflight",
}
ALL_KINDS = {**PRODUCTION_MUTATING, **READ_ONLY}

# Refusal codes — stable machine-readable reasons.
AUTHORIZATION_DISABLED = "AUTHORIZATION_DISABLED"
HOLD_MISSING = "HOLD_MISSING"
HOLD_UNREADABLE = "HOLD_UNREADABLE"
HOLD_MALFORMED = "HOLD_MALFORMED"
HOLD_AMBIGUOUS = "HOLD_AMBIGUOUS"
HOLD_STALE = "HOLD_STALE"
UNKNOWN_OPERATION = "UNKNOWN_OPERATION"
BYPASS_ATTEMPT = "BYPASS_ATTEMPT"

# Execution-mode refusals. Scope alone cannot separate an insert-or-update from a
# delete or a schema change at the same entry point and scope, so a
# production-mutating request must declare its mode.
MODE_MISSING = "MODE_MISSING"
MODE_UNKNOWN = "MODE_UNKNOWN"
MODE_MISMATCH = "MODE_MISMATCH"

# Environment variables that would look like an escape hatch. Their PRESENCE is
# treated as an attempted bypass, never as authorization.
FORBIDDEN_ENV = (
    "POLISCOPIC_PRODUCTION_AUTHORIZED",
    "POLISCOPIC_FORCE_PROD",
    "POLISCOPIC_INTERLOCK_OFF",
    "POLISCOPIC_SKIP_INTERLOCK",
    "POLISCOPIC_ALLOW_PRODUCTION",
)

# The interlock lives OUTSIDE Git (data/ is gitignored; the default path is also
# overridable by path only — a path is not an authorization).
DEFAULT_INTERLOCK_DIR = Path(__file__).resolve().parents[2] / "data" / "interlock"
HOLD_FILENAME = "hold.json"
HOLD_SCHEMA = "production-hold/1"
OP_ID_FILENAME = "operation-id"


def interlock_dir() -> Path:
    """Resolve the external interlock directory (never tracked by Git)."""
    override = os.environ.get("POLISCOPIC_INTERLOCK_DIR")
    return Path(override) if override else DEFAULT_INTERLOCK_DIR


# ── classification ───────────────────────────────────────────────────────


def classify(operation: str) -> dict:
    """Classify an operation id. Unknown kinds are NOT defaulted to safe."""
    if operation in PRODUCTION_MUTATING:
        return {
            "operation": operation,
            "description": PRODUCTION_MUTATING[operation],
            "mutates_production": True,
            "known": True,
        }
    if operation in READ_ONLY:
        return {
            "operation": operation,
            "description": READ_ONLY[operation],
            "mutates_production": False,
            "known": True,
        }
    return {
        "operation": operation,
        "description": None,
        "mutates_production": None,
        "known": False,
    }


# ── hold state ───────────────────────────────────────────────────────────


def _parse_ts(value: str) -> datetime | None:
    try:
        text = value.replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, AttributeError, TypeError):
        return None


def read_hold(now: datetime | None = None) -> dict:
    """Read and validate the external hold. Fail closed on every anomaly."""
    now = now or datetime.now(timezone.utc)
    directory = interlock_dir()
    path = directory / HOLD_FILENAME

    if not directory.is_dir():
        return {"state": "missing", "code": HOLD_MISSING,
                "detail": f"interlock directory absent: {directory}", "path": str(path)}
    if not path.exists():
        return {"state": "missing", "code": HOLD_MISSING,
                "detail": f"hold file absent: {path}", "path": str(path)}

    try:
        raw = path.read_text()
    except OSError as exc:
        return {"state": "unreadable", "code": HOLD_UNREADABLE,
                "detail": f"hold unreadable: {exc}", "path": str(path)}
    if not raw.strip():
        return {"state": "malformed", "code": HOLD_MALFORMED,
                "detail": "hold file is empty", "path": str(path)}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        return {"state": "malformed", "code": HOLD_MALFORMED,
                "detail": f"hold is not valid JSON: {exc}", "path": str(path)}
    if not isinstance(payload, dict):
        return {"state": "ambiguous", "code": HOLD_AMBIGUOUS,
                "detail": "hold is not a JSON object", "path": str(path)}

    # Ambiguity: duplicate/competing hold files next to the canonical one.
    try:
        siblings = sorted(p.name for p in directory.glob("hold*.json"))
    except OSError:
        siblings = []
    if len(siblings) > 1:
        return {"state": "ambiguous", "code": HOLD_AMBIGUOUS,
                "detail": f"multiple hold files present: {siblings}", "path": str(path)}

    if payload.get("schema") != HOLD_SCHEMA:
        return {"state": "malformed", "code": HOLD_MALFORMED,
                "detail": f"hold schema must be {HOLD_SCHEMA!r}", "path": str(path)}
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return {"state": "malformed", "code": HOLD_MALFORMED,
                "detail": "hold has no readable reason", "path": str(path)}

    not_before = _parse_ts(payload.get("not_before")) if payload.get("not_before") else None
    not_after = _parse_ts(payload.get("not_after")) if payload.get("not_after") else None
    if payload.get("not_before") and not_before is None:
        return {"state": "malformed", "code": HOLD_MALFORMED,
                "detail": "hold not_before is not a timestamp", "path": str(path)}
    if payload.get("not_after") and not_after is None:
        return {"state": "malformed", "code": HOLD_MALFORMED,
                "detail": "hold not_after is not a timestamp", "path": str(path)}
    if not_before and not_after and not_before > not_after:
        return {"state": "ambiguous", "code": HOLD_AMBIGUOUS,
                "detail": "hold window is inverted", "path": str(path)}

    if not_before and now < not_before:
        return {"state": "stale", "code": HOLD_STALE,
                "detail": f"hold not yet effective until {not_before.isoformat()}",
                "path": str(path), "reason": reason}
    if not_after and now > not_after:
        return {"state": "stale", "code": HOLD_STALE,
                "detail": f"hold expired at {not_after.isoformat()}",
                "path": str(path), "reason": reason}

    return {"state": "present", "code": None, "detail": "hold is present and valid",
            "path": str(path), "reason": reason}


def operation_id() -> str | None:
    """Read the per-operation id if one exists (evidence only, never authority)."""
    path = interlock_dir() / OP_ID_FILENAME
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return text or None


# ── the decision ─────────────────────────────────────────────────────────


def check(operation: str, entry_point: str = "", scope: list[str] | None = None,
          target: str = "production", now: datetime | None = None,
          mode: str | None = None,
          authorization_id: str | None = None) -> dict:
    """Return the interlock decision for one operation request.

    Fails closed. Order of evaluation matters: an attempted bypass is reported
    before anything else, then classification, then the hold, then — for
    production-mutating operations only — the one-operation authorization
    validator in ``operation_authorization``.

    A valid hold is a containment condition, never a permission, and the absence
    of a hold is never authorization either. If the validator cannot be loaded,
    or does not positively ALLOW, the operation is refused.
    """
    now = now or datetime.now(timezone.utc)
    verdict = {
        "schema": SCHEMA,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "operation": operation,
        "authorization_id": authorization_id,
        "entry_point": entry_point or None,
        "interlock_path": str(interlock_dir()),
        "authorization_issuance": "disabled",
    }

    attempted = sorted(v for v in FORBIDDEN_ENV if os.environ.get(v))
    if attempted:
        verdict.update({
            "status": "REFUSED", "code": BYPASS_ATTEMPT,
            "reason": ("environment bypass attempt detected; no environment variable "
                       "can authorize production"),
            "bypass_attempt": attempted,
        })
        return verdict

    info = classify(operation)
    verdict["classification"] = info
    if not info["known"]:
        verdict.update({
            "status": "REFUSED", "code": UNKNOWN_OPERATION,
            "reason": (f"unknown operation kind {operation!r}; refusing rather than "
                       "assuming a safe default"),
        })
        return verdict

    hold = read_hold(now=now)
    verdict["hold"] = {"state": hold["state"], "code": hold["code"],
                       "detail": hold["detail"], "path": hold.get("path")}
    if hold.get("reason"):
        verdict["hold"]["reason"] = hold["reason"]

    if not info["mutates_production"]:
        verdict.update({
            "status": "ALLOWED", "code": None,
            "reason": (f"{operation} is classified read-only; the interlock does not "
                       "gate read-only modes"),
            "mutates_production": False,
        })
        return verdict

    # Production-mutating: consult the one-operation authorization validator.
    # Fail closed — an unloadable validator is a refusal, never a pass.
    hold_note = hold["detail"]
    try:
        from operation_authorization import (
            KNOWN_MODES,
            validate as _validate_authorization,
        )
    except Exception as exc:  # pragma: no cover - defensive
        verdict.update({
            "status": "REFUSED", "code": AUTHORIZATION_DISABLED,
            "reason": ("could not load the one-operation authorization validator; "
                       "refusing rather than assuming a safe default"),
            "mutates_production": True,
            "hold_note": hold_note,
            "validator_error": str(exc),
        })
        return verdict

    # The execution MODE must be declared by the caller, and must be a real mode.
    # This runs before the validator and long before any credential, connection or
    # query — an omitted or invented mode can never fall through to a default.
    if mode is None or not str(mode).strip():
        verdict.update({
            "status": "REFUSED", "code": MODE_MISSING,
            "reason": ("caller declared no execution mode; scope alone cannot "
                       "separate an upsert from a delete or a schema change at the "
                       "same entry point and scope"),
            "mutates_production": True,
            "hold_note": hold_note,
        })
        return verdict
    if mode not in KNOWN_MODES:
        verdict.update({
            "status": "REFUSED", "code": MODE_UNKNOWN,
            "reason": (f"unknown execution mode {mode!r}; known modes are "
                       f"{list(KNOWN_MODES)}"),
            "mutates_production": True,
            "hold_note": hold_note,
        })
        return verdict

    authorization = _validate_authorization(
        operation, entry_point=entry_point, scope=scope, target=target, now=now,
        mode=mode, operation_id=authorization_id,
    )
    if authorization.get("status") != "ALLOWED":
        verdict.update({
            "status": "REFUSED",
            "code": authorization.get("code") or AUTHORIZATION_DISABLED,
            "reason": authorization.get("reason"),
            "mutates_production": True,
            "hold_note": hold_note,
            "authorization": authorization,
            "detail": ("a valid hold is a containment condition, not a permission; "
                       "absence of a hold is never authorization either"),
        })
        return verdict

    verdict.update({
        "status": "ALLOWED",
        "code": None,
        "reason": authorization.get("reason"),
        "mutates_production": True,
        "hold_note": hold_note,
        "authorization_issuance": "validated",
        "authorization": authorization,
    })
    return verdict


# ── CLI ──────────────────────────────────────────────────────────────────


def _emit(verdict: dict, as_json: bool = True) -> None:
    if as_json:
        print(json.dumps(verdict, separators=(",", ":"), sort_keys=True))
    else:
        print(f"{verdict['status']}: {verdict.get('reason', '')}")


def _exit_code(verdict: dict) -> int:
    if verdict["status"] == "ALLOWED":
        return 0
    if verdict["code"] in (UNKNOWN_OPERATION,):
        return 4
    return 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed production interlock")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="refuse or allow one operation request")
    p_check.add_argument("--operation", required=True)
    p_check.add_argument("--entry-point", default="")
    p_check.add_argument("--json", action="store_true", default=True)
    p_check.add_argument("--human", action="store_true")
    p_check.add_argument("--scope", default="",
                         help="comma-separated scope tables the caller will touch")
    p_check.add_argument("--target", default="production")
    p_check.add_argument("--mode", default=None,
                         help="execution mode of the request being checked; "
                              "REQUIRED for production-mutating operations, because "
                              "a table scope alone cannot separate an upsert from a "
                              "delete or a schema change")
    p_check.add_argument("--authorization-id", default=None,
                         help="select one exact authorization artifact; managed "
                              "production jobs should always provide this")

    p_class = sub.add_parser("classify", help="classify an operation kind")
    p_class.add_argument("--operation", required=True)

    sub.add_parser("hold-status", help="report external hold state")
    sub.add_parser("kinds", help="list known operation kinds")

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        # Usage errors are fail-closed.
        return 4 if exc.code not in (0, None) else 0

    if args.command == "check":
        scope = [s.strip() for s in args.scope.split(",") if s.strip()] or None
        verdict = check(args.operation, entry_point=args.entry_point,
                        scope=scope, target=args.target, mode=args.mode,
                        authorization_id=args.authorization_id)
        _emit(verdict, as_json=not args.human)
        return _exit_code(verdict)

    if args.command == "classify":
        info = classify(args.operation)
        print(json.dumps(info, separators=(",", ":"), sort_keys=True))
        return 0 if info["known"] else 4

    if args.command == "hold-status":
        hold = read_hold()
        print(json.dumps(hold, separators=(",", ":"), sort_keys=True))
        return 0

    if args.command == "kinds":
        print(json.dumps({"production_mutating": sorted(PRODUCTION_MUTATING),
                          "read_only": sorted(READ_ONLY)},
                         separators=(",", ":"), sort_keys=True))
        return 0

    return 4


if __name__ == "__main__":
    sys.exit(main())
