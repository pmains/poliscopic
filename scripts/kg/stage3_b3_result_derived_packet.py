#!/usr/bin/env python3
"""Disabled apply/rollback packet for a strict B3 result-derived plan."""
from __future__ import annotations

import hashlib
import json
import argparse
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.kg import stage3_b3_result_derived_plan as plan_contract
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable

KIND = "kg-stage3-b3-result-derived-apply-packet"
VERSION = "kg-stage3-b3-result-derived-apply-packet/1.0"
ENABLED = False
AUTHORIZATION_TOKEN = "B3_RESULT_DERIVED_APPLY_NOT_AUTHORIZED"
EXPECTED_PLAN_DIGEST = "ae26e0e4244f02caa62524dadba5fa6e33d0bac085eedecd45a3d36a5104e641"


class WritePathNotImplemented(RuntimeError):
    pass


def sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def build_packet(*, plan_path: str | Path, created_at: str, repo: Path = plan_contract.REPO) -> dict[str, Any]:
    path = Path(plan_path)
    if is_obsolete(path):
        raise ValueError("result-derived plan is obsolete")
    plan = load_verified(path)
    if (plan.get("kind") != plan_contract.KIND or plan.get("enabled") is not False
            or plan.get("digest") != EXPECTED_PLAN_DIGEST):
        raise ValueError("wrong or enabled result-derived plan")
    body = {"kind": KIND, "version": VERSION, "created_at": created_at, "mode": "design-only", "enabled": False,
            "applied": False, "write_path": "absent by design", "authorization_token": AUTHORIZATION_TOKEN,
            "target": dict(plan.get("target") or {}),
            "bindings": {"plan": {"path": str(path), "digest": plan["digest"]}, "plan_code_hashes": plan["bindings"]["code_hashes"],
                         "packet_code_hashes": {"scripts/kg/stage3_b3_result_derived_packet.py": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}},
            "accounting": dict(plan["accounting"]),
            "admission_state": "awaiting_post_receipts_backup",
            "backup_binding": {"required": True, "bound": False,
                               "fresh_after": "Stage 3 processing-receipt mutation and replay completion",
                               "requirements": ["development target equality", "mode 0600 receipt and dump",
                                                "dump digest", "isolated successful restore", "current source counts",
                                                "current schema fingerprint"]},
            "apply_contract": {"disabled": True, "requires": ["exact_plan_digest", "explicit_human_authorization", "exact_development_target", "fresh_protected_backup", "isolated_restore_proof", "serializable_transaction", "reservation_and_collision_recheck"], "operations": "create only plan containers, then exact result/event span links; no inferred rows", "receipt": "immutable O_EXCL mode-0600 receipt with pre/postimages and owned source keys"},
            "rollback_contract": {"disabled": True, "authority": "exact verified receipt", "operation": "remove only receipt-owned links and containers", "refuse_if": ["dependent rows", "reservation drift", "source hash drift", "wrong target", "receipt mismatch"], "receipt": "immutable O_EXCL mode-0600 rollback receipt"},
            "replay_contract": {"before_apply": "refuse with zero writes", "after_apply": "exact receipt-owned state is no-op", "drift": "refuse with zero writes"}}
    return {**body, "digest": sha(body)}


def validate_packet(packet: Mapping[str, Any], *, plan_path: str | Path, repo: Path = plan_contract.REPO) -> list[str]:
    expected = build_packet(plan_path=plan_path, created_at=str(packet.get("created_at")), repo=repo)
    return [] if dict(packet) == expected else ["packet differs from exact plan-bound rebuild"]


def execute_packet(*args: Any, **kwargs: Any) -> None:
    del args, kwargs
    raise WritePathNotImplemented("B3 result-derived apply is disabled and has no executable write path")


def rollback_preflight(*, packet: Mapping[str, Any], receipt_verified: bool, dependent_rows: int, source_drift: bool) -> list[str]:
    output = []
    if packet.get("kind") != KIND: output.append("wrong packet")
    if not receipt_verified: output.append("receipt is not verified")
    if dependent_rows: output.append("receipt-owned containers have dependencies")
    if source_drift: output.append("source evidence drift")
    return output


def write_packet(*, packet: Mapping[str, Any], out_path: Path) -> str:
    if validate_packet(packet, plan_path=(packet.get("bindings") or {}).get("plan", {}).get("path")): raise ValueError("packet does not validate")
    return write_immutable(out_path, dict(packet))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args(argv)
    packet = build_packet(plan_path=args.plan, created_at=args.created_at)
    digest = write_packet(packet=packet, out_path=args.out)
    print(json.dumps({"status": "success", "path": str(args.out), "digest": digest,
                      "enabled": False, "admission_state": packet["admission_state"]}, sort_keys=True))
    return 0


if __name__ == "__main__": raise SystemExit(main())
