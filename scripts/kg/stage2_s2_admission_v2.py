#!/usr/bin/env python3
"""``stage2_s2_admission_v2.py`` — typed receipt roles for Stage 2 admission.

V1 required every plan - an immutable historical dependency and the current apply plan -
to satisfy ONE live backup receipt.  That invariant is invalid: the two are different
typed roles.  This module separates them and fails closed on every way a caller can get
them wrong.

* ``dependency_evidence`` - a HISTORICAL plan is verified against the backup IT was
  originally bound to, plus its immutable apply receipt.  A newer live receipt is not
  evidence about a historical plan and is never substituted for it.
* ``current_protection`` - the CURRENT apply plan is admitted only against the fresh
  canonical receipt, which must be present, unexpired, untampered and of the exact
  target.

Everything here lives in modules v1 never bound, so the applied correction artifact and
every module it binds stay byte-identical.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

__all__ = ["AdmissionRefused", "CURRENT_PROTECTION", "DEPENDENCY_EVIDENCE",
           "MAX_RECEIPT_AGE_SECONDS", "admit_v2", "plan_target", "verify_current",
           "verify_dependency"]

#: The two typed roles.  A plan has exactly one.
DEPENDENCY_EVIDENCE = "dependency_evidence"
CURRENT_PROTECTION = "current_protection"

#: The freshness window the current protection receipt must satisfy.
MAX_RECEIPT_AGE_SECONDS = 86_400

TARGET_FIELDS = ("database", "host", "port", "tier")


class AdmissionRefused(RuntimeError):
    """The admission was refused; nothing was applied."""


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load(path: Path, *, what: str) -> dict[str, Any]:
    if not path.exists():
        raise AdmissionRefused(f"{what} {path.name!r} is missing")
    return json.loads(path.read_text())


def plan_target(plan: Mapping[str, Any]) -> dict[str, Any]:
    """A plan's target, wherever its contract stores it."""
    return dict(plan.get("target") or (plan.get("bindings") or {}).get("target") or {})


def _target_problems(role: str, label: str, receipt: Mapping[str, Any],
                     plan_target: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    got = dict(receipt.get("target") or {})
    for field in TARGET_FIELDS:
        if got.get(field) != plan_target.get(field):
            problems.append(f"{role}:{label}: receipt {field} is {got.get(field)!r}, "
                            f"plan says {plan_target.get(field)!r}")
    return problems


def _bound_receipt(plan: Mapping[str, Any], label: str) -> tuple[Path, Mapping[str, Any]]:
    bound = (plan.get("bindings") or {}).get("backup") or {}
    if not bound.get("path"):
        raise AdmissionRefused(f"{label} binds no backup receipt")
    path = REPO / "data" / "backups" / Path(str(bound["path"])).name
    return path, bound


def verify_dependency(plan: Mapping[str, Any], *, label: str,
                      apply_receipt: str | Path | None = None) -> dict[str, Any]:
    """A historical plan, verified against ITS OWN originally bound backup."""
    path, bound = _bound_receipt(plan, label)
    receipt = _load(path, what=f"{label} bound backup receipt")
    problems: list[str] = []
    digest = _sha(path)
    if digest != bound.get("canonical_digest"):
        problems.append(f"{label}: bound receipt {path.name} is TAMPERED "
                        f"(live {digest[:16]}..., bound {str(bound.get('canonical_digest'))[:16]}...)")
    if problems:
        raise AdmissionRefused("; ".join(problems[:4]))
    result = {"role": DEPENDENCY_EVIDENCE, "label": label, "receipt": path.name,
              "receipt_digest": digest, "stale_is_acceptable": True}
    if apply_receipt is not None:
        apply_path = Path(apply_receipt)
        record = _load(apply_path, what=f"{label} immutable apply receipt")
        if record.get("plan_digest") != plan.get("digest"):
            raise AdmissionRefused(
                f"{label}: the apply receipt names a different plan digest")
        result["apply_receipt"] = apply_path.name
        result["apply_receipt_digest"] = _sha(apply_path)
    return result


def verify_current(plan: Mapping[str, Any], *, label: str, receipt_path: str | Path,
                   max_age_seconds: int = MAX_RECEIPT_AGE_SECONDS) -> dict[str, Any]:
    """The current apply plan, verified against the fresh canonical receipt."""
    path = Path(receipt_path)
    receipt = _load(path, what=f"{label} current protection receipt")
    digest = _sha(path)
    bound = (plan.get("bindings") or {}).get("backup") or {}
    problems: list[str] = []
    if Path(str(bound.get("path") or "")).name != path.name:
        problems.append(f"{label}: the plan binds {bound.get('path')!r}, not the current "
                        f"protection receipt {path.name!r} (SWAPPED ROLES)")
    if bound.get("canonical_digest") != digest:
        problems.append(f"{label}: the current receipt does not match the bound digest "
                        f"(MISMATCHED)")
    created = receipt.get("created_at")
    if not created:
        problems.append(f"{label}: the current receipt is STALE (no created_at)")
    else:
        age = (datetime.now(timezone.utc)
               - datetime.fromisoformat(str(created).replace("Z", "+00:00"))).total_seconds()
        if age > max_age_seconds:
            problems.append(f"{label}: the current receipt is STALE ({int(age)}s old)")
    problems.extend(_target_problems(CURRENT_PROTECTION, label, receipt,
                                     plan_target(plan)))
    if not ((receipt.get("pg_restore") or {}).get("exit_code") == 0):
        problems.append(f"{label}: the current receipt proves no verified restore")
    if problems:
        raise AdmissionRefused("; ".join(problems[:4]))
    return {"role": CURRENT_PROTECTION, "label": label, "receipt": path.name,
            "receipt_digest": digest, "stale_is_acceptable": False}


def admit_v2(*, repair: Mapping[str, Any], correction: Mapping[str, Any],
             current_receipt: str | Path,
             correction_apply_receipt: str | Path) -> dict[str, Any]:
    """Admit the current repair plan with dependency evidence typed separately."""
    if Path(str(current_receipt)).name == Path(str(
            ((correction.get("bindings") or {}).get("backup") or {}).get("path"))).name:
        raise AdmissionRefused(
            "SWAPPED ROLES: the current protection receipt is the historical plan's")
    evidence = verify_dependency(correction, label="correction",
                                 apply_receipt=correction_apply_receipt)
    current = verify_current(repair, label="repair", receipt_path=current_receipt)
    return {"status": "admitted", "writes": 0,
            "dependency_evidence": evidence, "current_protection": current,
            "shared_live_receipt_equality": "removed"}
