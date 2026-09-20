#!/usr/bin/env python3
"""Serialized, resumable continuation for Stage 3 processing receipt batches.

Every document batch remains the guarded runner's own SERIALIZABLE transaction.
This driver only advances immutable plan offsets after validating terminal
receipts, so it never invokes the processor again for a completed offset.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply as apply  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-processing-receipt-continuation"
VERSION = "1.0"


class ContinuationRefused(RuntimeError):
    pass


def _valid_terminal(value: Mapping[str, Any], *, packet: Mapping[str, Any],
                    plan: Mapping[str, Any], offset: int, selected: int) -> list[str]:
    problems: list[str] = []
    if value.get("kind") != "kg-stage3-processing-receipt-apply-terminal" or value.get("version") != "1.0":
        problems.append("terminal kind/version is wrong")
    if value.get("authorized_packet_digest") != packet.get("digest"):
        problems.append("terminal packet binding differs")
    if value.get("plan_digest") != plan.get("digest"):
        problems.append("terminal plan binding differs")
    if value.get("offset") != offset or value.get("selected") != selected:
        problems.append("terminal window differs")
    if value.get("swept_at_updates") != 0:
        problems.append("terminal records swept_at updates")
    for field in ("success", "failed", "held", "replay"):
        if not isinstance(value.get(field), int) or value[field] < 0:
            problems.append(f"terminal {field} is invalid")
    if sum(int(value.get(field) or 0) for field in ("success", "failed", "held", "replay")) != selected:
        problems.append("terminal accounting does not reconcile")
    return problems


def _existing(terminal_dir: Path, *, packet: Mapping[str, Any], plan: Mapping[str, Any],
              offset: int, selected: int) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for path in terminal_dir.glob("kg-stage3-processing-receipt-apply-*.json"):
        try:
            document = load_verified(path)
        except Exception as exc:  # corrupted local evidence is a hard stop
            raise ContinuationRefused(f"terminal artifact {path.name} cannot be verified: {exc}") from exc
        if document.get("offset") == offset and document.get("authorized_packet_digest") == packet.get("digest"):
            problems = _valid_terminal(document, packet=packet, plan=plan, offset=offset, selected=selected)
            if problems:
                raise ContinuationRefused(f"terminal artifact {path.name} is invalid: {problems}")
            matches.append({**document, "terminal_receipt_path": str(path)})
    if len(matches) > 1:
        raise ContinuationRefused(f"more than one terminal receipt claims offset {offset}")
    return matches[0] if matches else None


def _aggregate(*, packet: Mapping[str, Any], plan: Mapping[str, Any], windows: list[Mapping[str, Any]],
               start_offset: int, max_batches: int, stopped: str | None) -> dict[str, Any]:
    totals = {field: sum(int(window.get(field) or 0) for window in windows)
              for field in ("selected", "success", "failed", "held", "replay", "swept_at_updates")}
    body = {"kind": KIND, "version": VERSION, "authorized_packet_digest": packet["digest"],
            "plan_digest": plan["digest"], "start_offset": start_offset, "max_batches": max_batches,
            "windows": [{"offset": window["offset"], "digest": window["digest"],
                         "path": window["terminal_receipt_path"], "selected": window["selected"],
                         "success": window["success"], "failed": window["failed"],
                         "held": window["held"], "replay": window["replay"]} for window in windows],
            "totals": totals, "outcome": "stopped" if stopped else "complete_window",
            "stopped_reason": stopped}
    return {**body, "digest": apply.receipt.canonical_sha256(body)}


def continue_batches(engine: Any, *, plan: Mapping[str, Any], design_packet: Mapping[str, Any],
                     apply_packet: Mapping[str, Any], backup_path: Path, token: str,
                     terminal_dir: Path, aggregate_out: Path, start_offset: int,
                     max_batches: int) -> dict[str, Any]:
    if not terminal_dir.is_dir() or max_batches < 1 or start_offset < 0:
        raise ContinuationRefused("existing terminal directory, non-negative offset, and positive max batches are required")
    if aggregate_out.exists():
        raise ContinuationRefused("aggregate receipt path already exists")
    records = list(plan.get("records") or [])
    windows: list[dict[str, Any]] = []
    stopped: str | None = None
    batches_started = 0
    offset = start_offset
    while offset < len(records) and batches_started < max_batches:
        selected = min(int(apply_packet["batch_size"]), len(records) - offset)
        terminal = _existing(terminal_dir, packet=apply_packet, plan=plan, offset=offset, selected=selected)
        if terminal is None:
            terminal = apply.apply_batch(engine, plan=plan, design_packet=design_packet,
                                         apply_packet=apply_packet, backup_path=backup_path,
                                         authorization_token=token, offset=offset,
                                         terminal_dir=terminal_dir)
            terminal = load_verified(terminal["terminal_receipt_path"])
            terminal["terminal_receipt_path"] = str(terminal_dir / f"kg-stage3-processing-receipt-apply-{terminal['digest']}.json")
            problems = _valid_terminal(terminal, packet=apply_packet, plan=plan,
                                       offset=offset, selected=selected)
            if problems:
                raise ContinuationRefused(f"new terminal receipt is invalid: {problems}")
            batches_started += 1
        windows.append(terminal)
        if terminal["failed"]:
            stopped = "terminal_failure"
            break
        offset += selected
    aggregate = _aggregate(packet=apply_packet, plan=plan, windows=windows, start_offset=start_offset,
                           max_batches=max_batches, stopped=stopped)
    write_immutable(aggregate_out, aggregate)
    result = {**aggregate, "aggregate_receipt_path": str(aggregate_out)}
    if stopped:
        raise ContinuationRefused(json.dumps(result, sort_keys=True))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--apply", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--authorization-token", required=True)
    parser.add_argument("--terminal-dir", type=Path, required=True)
    parser.add_argument("--aggregate-out", type=Path, required=True)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--max-batches", type=int, required=True)
    args = parser.parse_args(argv)
    result = continue_batches(get_engine(), plan=load_verified(args.plan), design_packet=load_verified(args.design),
                              apply_packet=load_verified(args.apply), backup_path=args.backup,
                              token=args.authorization_token, terminal_dir=args.terminal_dir,
                              aggregate_out=args.aggregate_out, start_offset=args.start_offset,
                              max_batches=args.max_batches)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
