"""Explicit authorization state for the qualified-outcome apply."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from scripts.kg.stage3_qualified_outcome_schema_packet import DESIGN_KIND, validate_packet

APPLY_KIND = "kg-stage3-qualified-outcome-authorized-apply"
APPLY_VERSION = "1.0"


def _digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def build_apply_packet(*, design_packet: dict[str, Any], plan: dict[str, Any],
                       backup_receipt_digest: str, code_digest: str,
                       schema_digest: str) -> dict[str, Any]:
    problems = validate_packet(design_packet, plan)
    if problems:
        raise ValueError("invalid design packet: " + "; ".join(problems))
    body = {"kind": APPLY_KIND, "version": APPLY_VERSION, "state": "authorized",
            "enabled": True, "target": "poliscopic_dev",
            "design_packet_digest": design_packet["digest"],
            "plan_digest": plan["digest"],
            "backup_receipt_digest": backup_receipt_digest,
            "code_digest": code_digest, "schema_digest": schema_digest}
    return {**body, "digest": _digest(body)}


def validate_apply_packet(packet: dict[str, Any], *, design_packet: dict[str, Any],
                          plan: dict[str, Any]) -> list[str]:
    problems = []
    body = {key: value for key, value in packet.items() if key != "digest"}
    if packet.get("kind") != APPLY_KIND or packet.get("version") != APPLY_VERSION:
        problems.append("not an authorized apply packet")
    if packet.get("state") != "authorized" or packet.get("enabled") is not True:
        problems.append("apply packet is not enabled and authorized")
    if design_packet.get("kind") != DESIGN_KIND:
        problems.append("bound schema packet is not design-only authority")
    if packet.get("design_packet_digest") != design_packet.get("digest"):
        problems.append("design packet binding mismatch")
    if packet.get("plan_digest") != plan.get("digest"):
        problems.append("plan binding mismatch")
    if packet.get("target") != "poliscopic_dev":
        problems.append("apply target is not development")
    if packet.get("digest") != _digest(body):
        problems.append("apply packet digest mismatch")
    return problems
