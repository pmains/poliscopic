#!/usr/bin/env python3
"""A genuinely non-executable proposal for a Stage 3 continuation.

A proposal records what a run *would* do and which bindings it would use, so a human can
review them.  It is deliberately **not** built by the authorization builder and carries a
different kind, a different state, and no approver field at all.

Why this module exists as a separate contract rather than a flag: an artifact that merely
says "not an authorization" in its filename or prose is not safe, because the authorized
apply contract will happily accept it and hand it to a runner.  A proposal therefore has
its own shape, its own validator, and is refused by content at every apply-facing gate
(``apply_packet.proposal_problems``).

A proposal can never become an authorization.  An authorization requires a separately
generated artifact carrying an approver that was actually supplied.
"""

from __future__ import annotations

from typing import Any, Mapping

KIND = "kg-stage3-processing-receipt-apply-proposal"
VERSION = "1.0"
STATE = "proposed"
WRITE_PATH = "absent by design"
APPROVAL_REQUIREMENT = (
    "an authorization requires a separately generated packet carrying an approver that was "
    "actually supplied; this proposal carries none and must not be executed")

PROPOSAL_KEYS = ("kind", "version", "state", "enabled", "applied", "executable", "proposed",
                 "write_path", "authorization_required", "approval_requirement", "target",
                 "plan_digest", "design_packet_digest", "backup_receipt_path",
                 "backup_receipt_digest", "code_digest", "writer_role", "batch_size",
                 "contract", "digest")


def digest(value: Mapping[str, Any]) -> str:
    """The canonical digest over a proposal body."""
    from scripts.kg import stage3_processing_receipt as receipt

    return receipt.canonical_sha256({key: item for key, item in value.items()
                                     if key != "digest"})


def build(*, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
          backup_receipt_path: str, backup_receipt_digest: str, code_digest: str,
          writer_role: str, batch_size: int) -> dict[str, Any]:
    """Build one human-review proposal.  There is no approver parameter by design."""
    if not str(writer_role).strip() or batch_size < 1:
        raise ValueError("a writer role and a positive bounded batch size are required")
    body = {
        "kind": KIND,
        "version": VERSION,
        "state": STATE,
        "enabled": False,
        "applied": False,
        "executable": False,
        "proposed": True,
        "write_path": WRITE_PATH,
        "authorization_required": True,
        "approval_requirement": APPROVAL_REQUIREMENT,
        "target": dict(plan["target"]),
        "plan_digest": plan["digest"],
        "design_packet_digest": design_packet["digest"],
        "backup_receipt_path": str(backup_receipt_path),
        "backup_receipt_digest": str(backup_receipt_digest),
        "code_digest": code_digest,
        "writer_role": str(writer_role),
        "batch_size": int(batch_size),
        "contract": {"no_swept_at_rewrite": True, "receipt_history": "append_only",
                     "replay": "zero_write_exact_receipt_replay",
                     "executable": False},
    }
    return {**body, "digest": digest(body)}


def validate_proposal(proposal: Any, *, plan: Mapping[str, Any],
                      design_packet: Mapping[str, Any],
                      current_code_digest: str | None = None) -> list[str]:
    """Reconstruct the proposal's shape and bindings; refuse anything that is not one."""
    if not isinstance(proposal, Mapping):
        return ["proposal must be an object"]
    problems: list[str] = []
    keys = set(proposal)
    missing = sorted(set(PROPOSAL_KEYS) - keys)
    extra = sorted(keys - set(PROPOSAL_KEYS))
    if missing or extra:
        problems.append(f"proposal key set is not canonical; missing {missing}, extra {extra}")
    if proposal.get("kind") != KIND or proposal.get("version") != VERSION:
        problems.append("proposal kind or version is not canonical")
    if proposal.get("state") != STATE:
        problems.append("a proposal must declare state proposed")
    for field, expected in (("enabled", False), ("applied", False), ("executable", False),
                            ("proposed", True), ("authorization_required", True)):
        if proposal.get(field) is not expected:
            problems.append(f"a proposal must declare {field} as {expected}")
    if proposal.get("write_path") != WRITE_PATH:
        problems.append("a proposal must declare an absent write path")
    if "approver" in proposal:
        problems.append("a proposal must carry no approver field at all")
    if proposal.get("digest") != digest(proposal):
        problems.append("proposal digest does not match the proposal body")
    if proposal.get("plan_digest") != plan.get("digest"):
        problems.append("proposal does not bind the supplied plan")
    if proposal.get("design_packet_digest") != design_packet.get("digest"):
        problems.append("proposal does not bind the supplied design packet")
    if proposal.get("target") != plan.get("target"):
        problems.append("proposal target is not the plan target")
    if current_code_digest is not None and proposal.get("code_digest") != current_code_digest:
        problems.append("proposal code binding drift")
    return problems


def is_proposal(value: Any) -> bool:
    """True when the artifact declares the proposal contract, whatever it is named."""
    return isinstance(value, Mapping) and value.get("kind") == KIND and \
        value.get("state") == STATE
