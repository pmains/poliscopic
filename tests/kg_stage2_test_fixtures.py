"""Test-only adapters for exercising Stage 2 mechanics after code drift.

Historical plan artifacts are immutable evidence.  They are expected to become
stale whenever any byte of their bound code changes, so mechanism tests must not
pretend those artifacts remain current forever.  These helpers make an in-memory
or temporary-file copy with only the recorded code hashes rebound to the current
checkout.  The source artifact is never changed and the new digest is computed
from the copied body.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any, Mapping

from scripts.kg import stage2_artifacts as artifacts
from scripts.kg import stage2_s2_plan_binding as binding


def code_current_copy(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Return a test-mechanics copy bound to the checkout's current code."""
    current = copy.deepcopy(dict(plan))
    recorded = tuple((current.get("bindings") or {}).get("code_hashes") or {})
    current["bindings"]["code_hashes"] = binding.code_hashes(recorded)
    current[artifacts.DIGEST_FIELD] = artifacts.compute_digest(current)
    return current


def write_code_current_copy(plan: Mapping[str, Any], directory: Path,
                            name: str) -> tuple[Path, dict[str, Any]]:
    """Write one rebound copy into a disposable test directory."""
    current = code_current_copy(plan)
    path = directory / name
    artifacts.write_immutable(path, current)
    return path, artifacts.load_verified(path)
