#!/usr/bin/env python3
"""Explicit, immutable authorization for the Stage 3 receipt-store apply.

This is deliberately separate from the disabled design packet.  Generating or
validating an authorization packet does not connect to PostgreSQL; execution is
admitted only by the runner after it rechecks this packet, the current code, and a
fresh restore-verified Stage 2 backup receipt.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from scripts.kg import stage3_processing_receipt_store_packet as design
from scripts.kg.stage2_artifacts import load_verified

KIND = "kg-stage3-processing-receipt-authorized-apply"
VERSION = "1.0"
CURRENT_PLAN_DIGEST = "73359d2df800b5d8f4e1a399bfc925141e4617e82f0509354470e6f4478eb2f9"
CURRENT_DESIGN_PACKET_DIGEST = "ae86c35948ca6e214e5699e799897811b2517d285e003c4126c7c6a44fea852c"
#: A proposal is a different contract, not a weaker authorization.  These markers let
#: every apply-facing gate refuse one categorically - by content, never by filename,
#: directory, or prose in the artifact.
PROPOSAL_KIND = "kg-stage3-processing-receipt-apply-proposal"
PROPOSAL_STATE = "proposed"
PROPOSAL_APPROVER_PLACEHOLDER = "PENDING MANAGER REVIEW (NOT AN AUTHORIZATION)"


def _normalised(value: Any) -> str:
    return " ".join(str(value or "").split()).upper()


def proposal_problems(packet: Any) -> list[str]:
    """Refuse a proposal, or a placeholder approver, in any apply-facing gate.

    An artifact that merely *says* it is not an authorization is not safe: the check
    must be on the artifact's own declared contract, so a proposal copied to an
    apply-style filename in an apply-style directory is still refused.
    """
    if not isinstance(packet, Mapping):
        return ["apply packet must be an object"]
    problems: list[str] = []
    if packet.get("kind") == PROPOSAL_KIND:
        problems.append("a proposal is not an authorization: proposal kind")
    if str(packet.get("state") or "") == PROPOSAL_STATE:
        problems.append("a proposal is not an authorization: state is proposed")
    approver = _normalised(packet.get("approver"))
    if approver == _normalised(PROPOSAL_APPROVER_PLACEHOLDER):
        problems.append("a placeholder approver is not an authorization")
    elif approver.startswith("PENDING MANAGER REVIEW"):
        problems.append("a pending-review approver is not an authorization")
    if packet.get("authorization_required") is True:
        problems.append("an artifact declaring authorization_required is not authorized")
    if packet.get("proposed") is True or packet.get("executable") is False:
        problems.append("an artifact declaring itself non-executable is not an authorization")
    return problems


def digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str).encode()).hexdigest()


def build(*, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
          backup_receipt_path: str, backup_receipt_digest: str, code_digest: str,
          approver: str, writer_role: str, batch_size: int) -> dict[str, Any]:
    """Build one human-authorization artifact; callers write it immutably."""
    if plan.get("digest") != CURRENT_PLAN_DIGEST:
        raise ValueError("the authorization must bind the current receipt dry plan")
    if design_packet.get("digest") != CURRENT_DESIGN_PACKET_DIGEST:
        raise ValueError("the authorization must bind the current disabled packet")
    if not str(approver).strip() or batch_size < 1:
        raise ValueError("an approver and a positive bounded batch size are required")
    body = {"kind": KIND, "version": VERSION, "state": "authorized", "enabled": True,
            "target": dict(plan["target"]), "plan_digest": plan["digest"],
            "design_packet_digest": design_packet["digest"], "backup_receipt_path": backup_receipt_path,
            "backup_receipt_digest": backup_receipt_digest, "code_digest": code_digest,
            "approver": approver, "writer_role": writer_role, "batch_size": batch_size,
            "contract": {"no_swept_at_rewrite": True, "receipt_history": "append_only",
                         "replay": "zero_write_exact_receipt_replay"}}
    return {**body, "digest": digest(body)}


def validate(packet: Any, *, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
             current_code_digest: str | None = None) -> list[str]:
    """Fail closed before a runner can use an authorized packet."""
    if not isinstance(packet, Mapping):
        return ["authorized apply packet must be an object"]
    body = {key: value for key, value in packet.items() if key != "digest"}
    problems: list[str] = []
    problems.extend(proposal_problems(packet))
    if packet.get("kind") != KIND or packet.get("version") != VERSION:
        problems.append("authorized apply packet kind or version is wrong")
    if packet.get("state") != "authorized" or packet.get("enabled") is not True:
        problems.append("authorized apply packet is not explicitly enabled")
    if packet.get("digest") != digest(body):
        problems.append("authorized apply packet digest does not match")
    if plan.get("digest") != CURRENT_PLAN_DIGEST or packet.get("plan_digest") != plan.get("digest"):
        problems.append("authorized apply packet does not bind the current dry plan")
    if (design_packet.get("digest") != CURRENT_DESIGN_PACKET_DIGEST or
            packet.get("design_packet_digest") != design_packet.get("digest")):
        problems.append("authorized apply packet does not bind the current disabled packet")
    if packet.get("target") != plan.get("target") or packet.get("target", {}).get("tier") != "development":
        problems.append("authorized apply packet target is not the exact development target")
    if not str(packet.get("approver") or "").strip() or not str(packet.get("writer_role") or "").strip():
        problems.append("authorized apply packet lacks approver or writer role")
    if not isinstance(packet.get("batch_size"), int) or packet["batch_size"] < 1:
        problems.append("authorized apply packet batch size is invalid")
    if current_code_digest is not None and packet.get("code_digest") != current_code_digest:
        problems.append("authorized apply packet code binding drift")
    return problems


def load(path: str | Path) -> dict[str, Any]:
    return load_verified(Path(path))
