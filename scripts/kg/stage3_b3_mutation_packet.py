#!/usr/bin/env python3
"""Q4 disabled mutation-packet design bound to the exact Q3 zero-operation plan."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg import stage3_b3_schema_contract as span_schema  # noqa: E402
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

PACKET_VERSION = "kg-stage3-b3-mutation-packet/1.0"
PACKET_KIND = "kg-stage3-b3-mutation-packet"
ENABLED = False
DISABLED_REASON = (
    "Q4 is design-only. No apply entry point exists. A later executable packet requires "
    "explicit development schema/data authorization and a fresh protected-backup receipt."
)
CODE_MODULES = (
    "scripts/kg/stage3_b3_mutation_packet.py",
    "scripts/kg/stage3_b3_schema_contract.py",
    "scripts/kg/stage3_b3_baseline.py",
    "scripts/kg/stage3_b3_span_contract.py",
    "scripts/kg/stage3_meeting_result_identity.py",
    "scripts/kg/stage2_artifacts.py",
    "scripts/kg/stage1_backup_receipt.py",
    "scripts/kg/stage2_backup_verify.py",
)


class PacketRefused(RuntimeError):
    pass


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in CODE_MODULES:
        path = repo / name
        if not path.is_file():
            raise PacketRefused(f"required code module is absent: {name}")
        result[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _load_current(path: str | Path, kind: str) -> dict[str, Any]:
    artifact_path = Path(path)
    if is_obsolete(artifact_path):
        raise PacketRefused(f"{artifact_path.name} is obsolete")
    document = load_verified(artifact_path)
    if document.get("kind") != kind:
        raise PacketRefused(f"{artifact_path.name} has unexpected kind {document.get('kind')!r}")
    return document


def load_q3(*, baseline_path: str | Path,
            dry_plan_path: str | Path) -> tuple[dict[str, Any], dict[str, Any]]:
    baseline = _load_current(baseline_path, "kg-stage3-b3-span-baseline")
    dry_plan = _load_current(dry_plan_path, "kg-stage3-b3-span-dry-plan")
    binding = dry_plan.get("baseline_artifact") or {}
    if binding.get("path") != str(baseline_path) or binding.get("digest") != baseline.get("digest"):
        raise PacketRefused("Q3 dry plan does not bind this exact baseline path and digest")
    if dry_plan.get("target") != baseline.get("target"):
        raise PacketRefused("Q3 artifacts bind different targets")
    if dry_plan.get("schema_sha256") != baseline.get("schema", {}).get("sha256"):
        raise PacketRefused("Q3 artifacts bind different schema signatures")
    if dry_plan.get("input_bindings") != baseline.get("inputs"):
        raise PacketRefused("Q3 artifacts bind different input projections")
    accounting = dry_plan.get("accounting") or {}
    coverage = baseline.get("coverage") or {}
    for plan_key, baseline_key in (("proposed", "proposed"),
                                   ("would_insert", "accepted"), ("held", "held")):
        if int(accounting.get(plan_key, -1)) != int(coverage.get(baseline_key, -2)):
            raise PacketRefused("Q3 baseline and dry-plan accounting disagree")
    if not accounting.get("reconciles") or not coverage.get("reconciles"):
        raise PacketRefused("Q3 accounting does not reconcile")
    if dry_plan.get("operations"):
        raise PacketRefused("Q4 zero-operation design refuses a non-empty Q3 operation set")
    if int(coverage.get("accepted", -1)) != 0:
        raise PacketRefused("Q4 zero-operation design requires exactly zero accepted spans")
    return baseline, dry_plan


def build_packet(*, baseline_path: str | Path, dry_plan_path: str | Path,
                 created_at: str) -> dict[str, Any]:
    baseline, dry_plan = load_q3(
        baseline_path=baseline_path, dry_plan_path=dry_plan_path)
    schema = span_schema.schema_contract()
    packet = {
        "kind": PACKET_KIND, "version": PACKET_VERSION, "created_at": created_at,
        "mode": "design-only", "enabled": False, "applied": False,
        "write_path": "absent by design", "disabled_reason": DISABLED_REASON,
        "target": dict(baseline["target"]),
        "bindings": {
            "q3_baseline": {"path": str(baseline_path), "digest": baseline["digest"]},
            "q3_dry_plan": {"path": str(dry_plan_path), "digest": dry_plan["digest"]},
            "q3_schema_sha256": baseline["schema"]["sha256"],
            "q3_input_bindings": dict(baseline["inputs"]),
            "code_hashes": code_hashes(),
        },
        "schema_contract": schema, "schema_operations": list(schema["ddl"]),
        "data_operations": [],
        "accounting": {"schema_operations": len(schema["ddl"]),
                       "data_operations": 0, "q3_accepted": 0,
                       "q3_held": int(baseline["coverage"]["held"])},
        "future_apply_contract": {
            "authorization": "explicit human approval for exact packet path and digest",
            "target": "exact bound development PostgreSQL target; production structurally refused",
            "backup": "fresh protected backup with successful isolated restore evidence",
            "transaction": "one owned SERIALIZABLE transaction; retry whole unit on serialization failure",
            "collision": "lock parents and recheck absent table plus every exact span identity",
            "operations": "execute only packet schema_operations and packet data_operations",
            "postconditions": "exact schema signature, zero rows, protected-table zero deltas",
            "receipt": "immutable O_EXCL mode-0600 receipt bound to packet, target, backup, postimage",
        },
        "future_rollback_contract": {
            "authority": "exact canonical apply-receipt path; caller mappings forbidden",
            "precondition": "receipt owns schema; schema fingerprint exact; table remains empty",
            "operation": "drop only evidence_spans inside one owned transaction",
            "refuse_if": ["rows exist", "schema drift", "dependent objects", "target drift",
                          "receipt drift or wrong packet"],
            "receipt": "immutable rollback receipt with exact preimage and terminal status",
        },
        "replay_contract": {
            "before_apply": "same packet remains disabled and writes zero",
            "after_apply": "exact receipt plus exact empty-table schema postimage yields no-op",
            "drift": "any row, schema, target, code, artifact or receipt drift refuses",
        },
    }
    packet["digest"] = canonical_sha256(packet)
    return packet


def validate_packet(packet: Mapping[str, Any], *, baseline_path: str | Path,
                    dry_plan_path: str | Path) -> list[str]:
    try:
        expected = build_packet(
            baseline_path=baseline_path, dry_plan_path=dry_plan_path,
            created_at=str(packet.get("created_at")))
    except Exception as exc:
        return [f"Q3 admission failed: {exc}"]
    problems: list[str] = []
    if dict(packet) != expected:
        problems.append("packet differs from the exact canonical current rebuild")
    if packet.get("enabled") is not False or packet.get("write_path") != "absent by design":
        problems.append("packet exposes an enabled write path")
    if packet.get("data_operations") != []:
        problems.append("zero-operation Q4 packet contains data operations")
    problems.extend(span_schema.validate_schema_contract(packet.get("schema_contract") or {}))
    return problems


def replay_check(*, table_present: bool, table_rows: int, schema_matches: bool,
                 canonical_receipt_verified: bool) -> dict[str, Any]:
    if not table_present:
        return {"status": "not-applied", "would_write": 0}
    if table_rows == 0 and schema_matches and canonical_receipt_verified:
        return {"status": "no-op-already-applied", "would_write": 0}
    return {"status": "refused-drift", "would_write": 0}


def rollback_preflight(*, packet: Mapping[str, Any], table_rows: int,
                       schema_matches: bool, dependent_objects: int,
                       canonical_receipt_verified: bool) -> list[str]:
    problems: list[str] = []
    if packet.get("kind") != PACKET_KIND:
        problems.append("wrong packet kind")
    if not canonical_receipt_verified:
        problems.append("canonical apply receipt is not verified")
    if table_rows != 0:
        problems.append("receipt-owned schema contains rows")
    if not schema_matches:
        problems.append("receipt-owned schema has drifted")
    if dependent_objects:
        problems.append("dependent objects prevent receipt-owned rollback")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--dry-plan", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    parser.add_argument("--stamp", default=None)
    args = parser.parse_args(argv)
    stamp = args.stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    value = build_packet(
        baseline_path=args.baseline, dry_plan_path=args.dry_plan, created_at=stamp)
    out = Path(args.out_dir) / f"kg-stage3-b3-mutation-packet-{stamp}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    digest = write_immutable(out, value)
    print(json.dumps({"status": "success", "path": str(out), "digest": digest,
                      "enabled": False, "data_operations": 0}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
