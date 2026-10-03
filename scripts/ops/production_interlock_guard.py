"""Dependency-light caller for the repository's production interlock."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def require_production_interlock(operation: str, entry_point: str,
                                 scope: list[str] | None = None,
                                 target: str = "production",
                                 mode: str | None = None) -> dict:
    """Return an allowed verdict or terminate before production can be reached.

    ``scope``, ``target`` and ``mode`` are forwarded to the validator so an
    authorization cannot be widened silently: the caller must declare what it will
    touch AND which execution mode it will use, because a table scope alone cannot
    separate an upsert from a delete or a schema change. A mismatch, an omitted
    mode, or an unknown mode against the authorized plan is a refusal.
    """
    ops_dir = Path(__file__).resolve().parent
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    try:
        from production_interlock import check
        verdict = check(operation, entry_point=entry_point, scope=scope,
                        target=target, mode=mode)
    except Exception as exc:
        verdict = {
            "status": "REFUSED",
            "code": "INTERLOCK_UNAVAILABLE",
            "reason": f"production interlock unavailable ({exc})",
        }
    if verdict.get("status") != "ALLOWED":
        sys.stderr.write(json.dumps(verdict, sort_keys=True) + "\n")
        raise SystemExit(3)
    return verdict
