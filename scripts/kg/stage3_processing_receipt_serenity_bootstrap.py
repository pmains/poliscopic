#!/usr/bin/env python3
"""Durable one-process bootstrap for the Stage 3 Serenity receipt backfill.

It owns only the prerequisites that must precede the bounded writer: a fresh
canonical Stage-2 dump plus isolated restore verification, and the exact
authorized apply packet bound to that backup and the reviewed code.  It then
``exec``s the Serenity runner, retaining one PID and avoiding a second long
controller.  Every recoverable phase is an immutable run-owned artifact.  A
prerequisite refusal emits an atomic ``REPORT:`` pending record before wake.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.kg import stage3_processing_receipt_apply as apply  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_packet as authorization  # noqa: E402
from scripts.kg import stage3_processing_receipt_serenity_runner as serenity  # noqa: E402
from scripts.kg import stage3_processing_receipt_store_backup as backup_contract  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-processing-receipt-serenity-bootstrap"
VERSION = "1.0"
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{2,79}$")
DEFAULT_PLAN = REPO / "data/kg-plans/kg-stage3-processing-dry-plan-20260920T174924Z.json"
DEFAULT_DESIGN = REPO / "data/kg-plans/kg-stage3-processing-receipt-store-packet-20260920T174958Z.json"
DEFAULT_SEEDS = (
    REPO / "data/kg-receipts/kg-stage3-processing-receipt-checkpoint-20260920T212900Z.json",
    REPO / "data/kg-receipts/kg-stage3-processing-receipt-continuation-20260920T213000Z.json",
    REPO / "data/kg-receipts/kg-stage3-processing-receipt-continuation-20260920T213500Z.json",
)


class BootstrapRefused(RuntimeError):
    pass


def _ref(path: Path, document: Mapping[str, Any]) -> dict[str, str]:
    return {"path": str(path.resolve()), "digest": str(document["digest"])}


def _phase_path(state_dir: Path, run_id: str, phase: str) -> Path:
    return state_dir / f"kg-stage3-processing-receipt-bootstrap-{run_id}-{phase}.json"


def _intent_body(*, run_id: str, plan: Mapping[str, Any], design: Mapping[str, Any],
                 approver: str, writer_role: str, backup_dir: Path, packet_path: Path,
                 report_path: Path, seed_paths: Sequence[Path]) -> dict[str, Any]:
    return {"kind": KIND, "version": VERSION, "phase": "intent", "run_id": run_id,
            "plan_digest": plan["digest"], "design_digest": design["digest"],
            "code_digest": apply.code_digest(), "approver": approver,
            "writer_role": writer_role, "batch_size": serenity.BATCH_SIZE,
            "backup_dir": str(backup_dir.resolve()), "packet_path": str(packet_path.resolve()),
            "report_path": str(report_path.resolve()),
            "seed_paths": [str(path.resolve()) for path in seed_paths]}


def _intent(path: Path, **kwargs: Any) -> dict[str, Any]:
    expected = _intent_body(**kwargs)
    if path.exists():
        value = load_verified(path)
        body = {key: item for key, item in value.items() if key != "digest"}
        if body != expected:
            raise BootstrapRefused("existing run intent differs from this invocation")
        return value
    write_immutable(path, expected)
    return load_verified(path)


def _phase(intent_path: Path, intent: Mapping[str, Any], *, phase: str,
           artifact: Mapping[str, str]) -> dict[str, Any]:
    path = _phase_path(intent_path.parent, str(intent["run_id"]), phase)
    body = {"kind": KIND, "version": VERSION, "phase": phase, "run_id": intent["run_id"],
            "intent": _ref(intent_path, intent), "artifact": dict(artifact)}
    if path.exists():
        value = load_verified(path)
        if {key: item for key, item in value.items() if key != "digest"} != body:
            raise BootstrapRefused(f"existing {phase} phase differs from run intent")
        return value
    write_immutable(path, body)
    return load_verified(path)


def _read_phase(intent_path: Path, intent: Mapping[str, Any], phase: str) -> dict[str, Any] | None:
    path = _phase_path(intent_path.parent, str(intent["run_id"]), phase)
    if not path.exists():
        return None
    value = load_verified(path)
    if (value.get("kind"), value.get("version"), value.get("phase"), value.get("run_id")) != (
            KIND, VERSION, phase, intent.get("run_id")) or value.get("intent") != _ref(intent_path, intent):
        raise BootstrapRefused(f"{phase} phase is not owned by this run intent")
    artifact = value.get("artifact")
    if not isinstance(artifact, Mapping) or set(artifact) != {"path", "digest"}:
        raise BootstrapRefused(f"{phase} phase artifact reference is malformed")
    return value


def _verify_artifact(reference: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    path = Path(str(reference.get("path") or ""))
    value = load_verified(path)
    if _ref(path, value) != dict(reference):
        raise BootstrapRefused("phase artifact digest does not match its reference")
    return path, value


def _backup_candidates(backup_dir: Path, *, target: Mapping[str, Any]) -> list[tuple[Path, dict[str, Any]]]:
    values: list[tuple[Path, dict[str, Any]]] = []
    for path in backup_dir.glob("kg-stage2-backup-receipt-*.json"):
        value = load_verified(path)
        if backup_contract.backup_problems(path, target=target):
            raise BootstrapRefused(f"run-owned backup candidate {path.name} is invalid")
        values.append((path, value))
    return values


def _run_backup(backup_dir: Path) -> tuple[Path, dict[str, Any]]:
    """Call the canonical backup implementation; it streams no dump through us."""
    command = [sys.executable, "-u", str(REPO / "scripts/kg/stage3_processing_receipt_backup_run.py"),
               "--out-dir", str(backup_dir)]
    print(json.dumps({"event": "backup_started", "out_dir": str(backup_dir)}, sort_keys=True), flush=True)
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               check=False)
    if completed.returncode:
        raise BootstrapRefused(f"canonical backup failed: {completed.stderr.strip() or completed.returncode}")
    try:
        result = json.loads(completed.stdout)
        path = Path(str(result["receipt"]))
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise BootstrapRefused("canonical backup emitted no parseable receipt") from exc
    value = load_verified(path)
    if path.parent.resolve() != backup_dir.resolve():
        raise BootstrapRefused("canonical backup wrote outside this run-owned directory")
    return path, value


def _backup(intent_path: Path, intent: Mapping[str, Any], *, plan: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    existing = _read_phase(intent_path, intent, "backup")
    if existing:
        path, value = _verify_artifact(existing["artifact"])
        if path.parent.resolve() != Path(str(intent["backup_dir"])).resolve():
            raise BootstrapRefused("backup phase artifact is outside this run-owned backup directory")
    else:
        backup_dir = Path(str(intent["backup_dir"]))
        backup_dir.mkdir(parents=True, exist_ok=True)
        candidates = _backup_candidates(backup_dir, target=plan["target"])
        if len(candidates) > 1:
            raise BootstrapRefused("more than one verified run-owned backup exists without a phase receipt")
        path, value = candidates[0] if candidates else _run_backup(backup_dir)
        if backup_contract.backup_problems(path, target=plan["target"]):
            raise BootstrapRefused("fresh backup does not meet the canonical receipt contract")
        _phase(intent_path, intent, phase="backup", artifact=_ref(path, value))
    if backup_contract.backup_problems(path, target=plan["target"]):
        raise BootstrapRefused("run-owned backup no longer meets the canonical receipt contract")
    return path, value


def _packet(intent_path: Path, intent: Mapping[str, Any], *, plan: Mapping[str, Any],
            design: Mapping[str, Any], backup_path: Path, backup_document: Mapping[str, Any]) -> tuple[Path, dict[str, Any]]:
    phase = _read_phase(intent_path, intent, "authorized-apply")
    if phase:
        path, value = _verify_artifact(phase["artifact"])
        if path.resolve() != Path(str(intent["packet_path"])).resolve():
            raise BootstrapRefused("authorized packet phase artifact is outside its exact run-owned path")
    else:
        path = Path(str(intent["packet_path"]))
        if path.exists():
            value = load_verified(path)
        else:
            value = authorization.build(plan=plan, design_packet=design,
                backup_receipt_path=str(backup_path.resolve()), backup_receipt_digest=str(backup_document["digest"]),
                code_digest=apply.code_digest(), approver=str(intent["approver"]),
                writer_role=str(intent["writer_role"]), batch_size=serenity.BATCH_SIZE)
            write_immutable(path, value)
            value = load_verified(path)
        _phase(intent_path, intent, phase="authorized-apply", artifact=_ref(path, value))
    problems = authorization.validate(value, plan=plan, design_packet=design,
                                      current_code_digest=apply.code_digest())
    if value.get("backup_receipt_path") != str(backup_path.resolve()) or value.get("backup_receipt_digest") != backup_document.get("digest"):
        problems.append("authorized packet is not bound to this run-owned backup")
    if value.get("batch_size") != serenity.BATCH_SIZE:
        problems.append("authorized packet does not retain the 500-row batch size")
    if problems:
        raise BootstrapRefused("; ".join(problems))
    return path, value


def _seed(paths: Sequence[Path], plan: Mapping[str, Any]) -> int:
    cursor = 0
    for path in paths:
        end, totals, _reference = serenity._seed(path, plan)
        if end - totals["selected"] != cursor:
            raise BootstrapRefused("receipt seed artifacts are not contiguous")
        cursor = end
    if cursor != 6100:
        raise BootstrapRefused(f"receipt seed artifacts must end at offset 6100, not {cursor}")
    return cursor


def _report(path: Path, *, outcome: str, error: str | None, intent: Mapping[str, Any] | None,
            artifact: Mapping[str, Any] | None = None) -> None:
    if path.exists():
        return  # the first atomic report is the terminal authority for this run id
    checkpoint = {"path": artifact.get("path"), "digest": artifact.get("digest"), "offset": 6100,
                  "totals": {"selected": 6100}} if artifact else None
    serenity._report(path, outcome=outcome, checkpoint=checkpoint, error=error,
                     notified="pending explicit post-report delivery",
                     plan={"digest": intent.get("plan_digest")} if intent else None,
                     packet=artifact, backup=None)
    wake = serenity._notify(path, os.environ.get("POLISCOPIC_CODEX_THREAD"))
    print(json.dumps({"event": "report", "path": str(path), "wake": wake}, sort_keys=True), flush=True)


def bootstrap(*, run_id: str, plan_path: Path, design_path: Path, approver: str, writer_role: str,
              state_dir: Path, backup_root: Path, packet_dir: Path, report_path: Path,
              seed_paths: Sequence[Path], exec_runner: bool = True) -> dict[str, Any]:
    if not RUN_ID.fullmatch(run_id):
        raise BootstrapRefused("run id must be 3-80 letters, digits, underscores, or hyphens")
    if report_path.suffix != ".pending":
        raise BootstrapRefused("report path must end in .pending")
    for path in (state_dir, backup_root, packet_dir, report_path.parent):
        if not path.is_dir():
            raise BootstrapRefused("state, backup, packet, and report directories must already exist")
    plan, design = load_verified(plan_path), load_verified(design_path)
    backup_dir = backup_root / f"stage3-processing-receipt-serenity-{run_id}"
    packet_path = packet_dir / f"kg-stage3-processing-receipt-authorized-apply-serenity-{run_id}.json"
    intent_path = _phase_path(state_dir, run_id, "intent")
    intent = _intent(intent_path, run_id=run_id, plan=plan, design=design, approver=approver,
                     writer_role=writer_role, backup_dir=backup_dir, packet_path=packet_path,
                     report_path=report_path, seed_paths=seed_paths)
    _seed(seed_paths, plan)
    backup_path, backup_document = _backup(intent_path, intent, plan=plan)
    packet_path, packet = _packet(intent_path, intent, plan=plan, design=design,
                                  backup_path=backup_path, backup_document=backup_document)
    handoff = _phase(intent_path, intent, phase="handoff", artifact=_ref(packet_path, packet))
    result = {"intent": str(intent_path), "backup": str(backup_path), "apply_packet": str(packet_path),
              "handoff": str(_phase_path(state_dir, run_id, "handoff")), "offset": 6100,
              "report": str(report_path), "run_id": run_id, "handoff_digest": handoff["digest"]}
    print(json.dumps({"event": "runner_exec", **result}, sort_keys=True), flush=True)
    if exec_runner:
        argv = [sys.executable, "-u", str(REPO / "scripts/kg/stage3_processing_receipt_serenity_runner.py"),
                "--plan", str(plan_path), "--design", str(design_path), "--apply", str(packet_path),
                "--backup", str(backup_path), "--authorization-token", apply.AUTHORIZATION_TOKEN,
                "--terminal-dir", str(state_dir), "--preflight-dir", str(packet_dir),
                "--checkpoint-dir", str(state_dir), "--report-out", str(report_path)]
        for seed_path in seed_paths:
            argv.extend(("--seed", str(seed_path)))
        os.execv(sys.executable, argv)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--plan", type=Path, default=DEFAULT_PLAN)
    parser.add_argument("--design", type=Path, default=DEFAULT_DESIGN)
    parser.add_argument("--approver", required=True)
    parser.add_argument("--writer-role", required=True)
    parser.add_argument("--state-dir", type=Path, default=REPO / "data/kg-receipts")
    parser.add_argument("--backup-root", type=Path, default=REPO / "data/backups")
    parser.add_argument("--packet-dir", type=Path, default=REPO / "data/kg-plans")
    parser.add_argument("--report-out", type=Path, required=True)
    parser.add_argument("--seed", type=Path, action="append")
    parser.add_argument("--no-exec", action="store_true", help="test/bootstrap only; do not exec the writer")
    args = parser.parse_args(argv)
    seed_paths = tuple(args.seed) if args.seed else DEFAULT_SEEDS
    intent: dict[str, Any] | None = None
    try:
        bootstrap(run_id=args.run_id, plan_path=args.plan, design_path=args.design,
                  approver=args.approver, writer_role=args.writer_role, state_dir=args.state_dir,
                  backup_root=args.backup_root, packet_dir=args.packet_dir, report_path=args.report_out,
                  seed_paths=seed_paths, exec_runner=not args.no_exec)
        return 0
    except Exception as exc:
        try:
            # This intentionally has no dependency on a successfully created intent.
            _report(args.report_out, outcome="stopped", error=f"{type(exc).__name__}: {exc}", intent=intent)
        except Exception as report_error:
            print(f"Serenity bootstrap report failure: {report_error}", file=sys.stderr, flush=True)
        print(f"Serenity bootstrap stopped: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
