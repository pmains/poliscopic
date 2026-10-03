#!/usr/bin/env python3
"""Write a successful daily-sync terminal and consume its deferred authority."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
for _candidate in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import operation_authorization as authorization  # noqa: E402
from daily_sync_gate import validate_pre_sync  # noqa: E402

SCHEMA = "daily-prod-upsert-terminal/2"


class Refused(RuntimeError):
    pass


def build_terminal(*, run_date: str, attempt_id: str, authorization_id: str,
                   preflight_path: Path, backup_receipt_path: Path,
                   public_urls: list[str], sync_dir: Path | None = None) -> dict[str, Any]:
    gate = validate_pre_sync(
        run_date=run_date, attempt_id=attempt_id,
        authorization_id=authorization_id, preflight_path=preflight_path,
        backup_receipt_path=backup_receipt_path, sync_dir=sync_dir)
    if gate.get("status") != "ALLOWED":
        raise Refused(f"daily gate no longer passes: {gate.get('code')}: "
                      f"{gate.get('reason')}")
    directory = authorization.operation_dir("OP-RECON", authorization_id)
    plan, plan_error = authorization._read_json(directory / "plan.json")
    auth, auth_error = authorization._read_json(directory / "authorization.json")
    if plan is None or auth is None:
        raise Refused(f"authorization artifacts unavailable: {plan_error or auth_error}")
    if (plan.get("digest") != authorization.plan_digest(plan) or
            auth.get("auth_digest") != authorization.authorization_digest(auth)):
        raise Refused("authorization artifacts are not digest-valid")
    body = {
        "schema": SCHEMA,
        "terminal": True,
        "status": "succeeded",
        # Reconciled means the terminal and authorization-use ledger agree; this
        # is NOT delete reconciliation.  The execution mode remains upsert-only.
        "reconciled": True,
        "operation": "OP-RECON",
        "operation_id": authorization_id,
        "attempt_id": attempt_id,
        "run_date": run_date,
        "mode": "upsert-only",
        "plan_digest": plan["digest"],
        "authorization_digest": auth["auth_digest"],
        "completed_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "gates": gate,
        "public_smoke_urls": public_urls,
        "integrity_postconditions": "sync_prod exit 0",
    }
    return {**body, "digest": hashlib.sha256(
        authorization.canonical_json(body).encode("utf-8")).hexdigest()}


def record_terminal(*, output: Path, terminal: dict[str, Any]) -> dict[str, str]:
    authorization._write_exclusive(output, terminal)
    try:
        use_path = authorization.consume_successful_terminal(
            str(terminal["operation_id"]), terminal)
    except Exception:
        # Preserve the immutable terminal: it is evidence that the sync itself
        # succeeded and makes an accounting repair deterministic and auditable.
        raise
    return {"terminal": str(output), "authorization_use": str(use_path)}


def ensure_terminal_consumed(output: Path) -> dict[str, str]:
    try:
        terminal = json.loads(output.read_text())
    except Exception as exc:
        raise Refused("existing terminal is unreadable") from exc
    body = {key: value for key, value in terminal.items() if key != "digest"}
    expected = hashlib.sha256(
        authorization.canonical_json(body).encode("utf-8")).hexdigest()
    if (terminal.get("schema") != SCHEMA or terminal.get("digest") != expected or
            terminal.get("status") != "succeeded"):
        raise Refused("existing terminal is not a valid successful v2 receipt")
    use_path = authorization.terminal_use_path(
        str(terminal.get("operation_id") or ""),
        str(terminal.get("attempt_id") or ""))
    if use_path.is_file():
        if json.loads(use_path.read_text()) != terminal:
            raise Refused("terminal and authorization-use receipt differ")
    else:
        use_path = authorization.consume_successful_terminal(
            str(terminal["operation_id"]), terminal)
    return {"terminal": str(output), "authorization_use": str(use_path)}


def check_terminal_accounted(output: Path) -> dict[str, str]:
    try:
        terminal = json.loads(output.read_text())
    except Exception as exc:
        raise Refused("existing terminal is unreadable") from exc
    body = {key: value for key, value in terminal.items() if key != "digest"}
    expected = hashlib.sha256(
        authorization.canonical_json(body).encode("utf-8")).hexdigest()
    if (terminal.get("schema") != SCHEMA or terminal.get("digest") != expected or
            terminal.get("status") != "succeeded"):
        raise Refused("existing terminal is not a valid successful v2 receipt")
    use_path = authorization.terminal_use_path(
        str(terminal.get("operation_id") or ""),
        str(terminal.get("attempt_id") or ""))
    if not use_path.is_file() or json.loads(use_path.read_text()) != terminal:
        raise Refused("successful terminal has no matching authorization-use receipt")
    return {"terminal": str(output), "authorization_use": str(use_path)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-date")
    parser.add_argument("--attempt-id")
    parser.add_argument("--authorization-id")
    parser.add_argument("--preflight", type=Path)
    parser.add_argument("--backup-receipt", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--public-url", action="append", default=[])
    parser.add_argument("--reconcile-existing", action="store_true")
    parser.add_argument("--check-existing", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.reconcile_existing and arguments.check_existing:
            raise Refused("choose either reconcile-existing or check-existing")
        if arguments.check_existing:
            result = check_terminal_accounted(arguments.output)
        elif arguments.reconcile_existing:
            result = ensure_terminal_consumed(arguments.output)
        else:
            required = (arguments.run_date, arguments.attempt_id,
                        arguments.authorization_id, arguments.preflight,
                        arguments.backup_receipt)
            if any(value in (None, "") for value in required):
                raise Refused("new terminal requires run date, attempt id, "
                              "authorization id, preflight, and backup receipt")
            terminal = build_terminal(
                run_date=arguments.run_date, attempt_id=arguments.attempt_id,
                authorization_id=arguments.authorization_id,
                preflight_path=arguments.preflight,
                backup_receipt_path=arguments.backup_receipt,
                public_urls=arguments.public_url)
            result = record_terminal(output=arguments.output, terminal=terminal)
    except (Refused, ValueError, FileExistsError, OSError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": "succeeded", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
