"""Offline-only admission gate for the separately authorized B3 result-derived apply.

There is deliberately no executable database write path in this module.  A later
review may enable a separately reviewed executor only after the processing-receipt
mutation/replay completes and a new B3-specific restore-verified backup is bound.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Mapping

from scripts.kg import stage3_b3_result_derived_packet as packet_contract

EXECUTION_ENABLED = False
AUTHORIZATION_TOKEN = "stage3-b3-result-derived-development-apply/v1"


class ApplyRefused(RuntimeError):
    pass


def code_digest(repo: Path = packet_contract.plan_contract.REPO) -> str:
    files = ("scripts/kg/stage3_b3_result_derived_apply.py",
             "scripts/kg/stage3_b3_result_derived_packet.py",
             "scripts/kg/stage3_b3_result_derived_plan.py")
    return hashlib.sha256(b"".join((repo / name).read_bytes() for name in files)).hexdigest()


def gate(*, packet: Mapping[str, Any], backup_binding: Mapping[str, Any] | None,
         current_target: Mapping[str, Any] | None, current_schema_sha256: str | None,
         authorization_token: str, processing_receipts_complete: bool) -> list[str]:
    """Pure refusal list; no connection, transaction, or write surface exists."""
    problems: list[str] = []
    plan_path = ((packet.get("bindings") or {}).get("plan") or {}).get("path")
    try:
        invalid_packet = not plan_path or bool(packet_contract.validate_packet(packet, plan_path=plan_path))
    except Exception:
        invalid_packet = True
    if invalid_packet:
        problems.append("packet is not an exact immutable plan-bound rebuild")
    if packet.get("enabled") is not False or packet.get("write_path") != "absent by design":
        problems.append("packet exposes a write path")
    if not processing_receipts_complete:
        problems.append("processing-receipt mutation/replay is not complete")
    if authorization_token != AUTHORIZATION_TOKEN:
        problems.append("explicit B3 authorization token mismatch")
    if not backup_binding or backup_binding.get("post_receipts_fresh") is not True:
        problems.append("post-receipts restore-verified B3 backup is absent")
    if not current_target or current_target != packet.get("target"):
        problems.append("current target differs from bound plan target")
    if not isinstance(current_schema_sha256, str) or len(current_schema_sha256) != 64:
        problems.append("current schema fingerprint is absent")
    if EXECUTION_ENABLED:
        problems.append("offline gate must not enable execution")
    return problems


def execute(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    raise ApplyRefused("B3 result-derived executor is not implemented or enabled")
