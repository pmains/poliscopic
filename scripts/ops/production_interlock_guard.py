"""Dependency-light caller for the repository's production interlock."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def require_production_interlock(operation: str, entry_point: str) -> dict:
    """Return an allowed verdict or terminate before production can be reached."""
    ops_dir = Path(__file__).resolve().parent
    if str(ops_dir) not in sys.path:
        sys.path.insert(0, str(ops_dir))
    try:
        from production_interlock import check
        verdict = check(operation, entry_point=entry_point)
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
