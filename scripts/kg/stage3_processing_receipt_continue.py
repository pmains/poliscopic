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
                    plan: Mapping[str, Any], offset: int, selected: int,
                    preflight_document: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if value.get("kind") != "kg-stage3-processing-receipt-apply-terminal" or value.get("version") != "1.0":
        problems.append("terminal kind/version is wrong")
    if value.get("authorized_packet_digest") != packet.get("digest"):
        problems.append("terminal packet binding differs")
    if value.get("plan_digest") != plan.get("digest"):
        problems.append("terminal plan binding differs")
    if value.get("preflight_digest") != preflight_document.get("digest"):
        problems.append("terminal preflight binding differs")
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
              offset: int, selected: int,
              preflight_document: Mapping[str, Any]) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for path in terminal_dir.glob("kg-stage3-processing-receipt-apply-*.json"):
        try:
            document = load_verified(path)
        except Exception as exc:  # corrupted local evidence is a hard stop
            raise ContinuationRefused(f"terminal artifact {path.name} cannot be verified: {exc}") from exc
        if document.get("offset") == offset and document.get("authorized_packet_digest") == packet.get("digest"):
            problems = _valid_terminal(document, packet=packet, plan=plan, offset=offset,
                                       selected=selected, preflight_document=preflight_document)
            if problems:
                raise ContinuationRefused(f"terminal artifact {path.name} is invalid: {problems}")
            matches.append({**document, "terminal_receipt_path": str(path)})
    if len(matches) > 1:
        raise ContinuationRefused(f"more than one terminal receipt claims offset {offset}")
    return matches[0] if matches else None


def _prior_terminal(path: Path, *, prior_packet: Mapping[str, Any], packet: Mapping[str, Any],
                    plan: Mapping[str, Any], selected: int) -> dict[str, Any]:
    """Accept one explicitly named terminal from the immediately prior code packet."""
    terminal = load_verified(path)
    # Prior-packet import is deliberately a compatibility boundary: it validates
    # the historically authorized terminal under its own contract, then binds the
    # current packet to the same target/plan/backup before any new preflight work.
    problems = []
    if terminal.get("kind") != "kg-stage3-processing-receipt-apply-terminal" or terminal.get("version") != "1.0":
        problems.append("prior terminal kind/version is wrong")
    if terminal.get("authorized_packet_digest") != prior_packet.get("digest") or terminal.get("plan_digest") != plan.get("digest"):
        problems.append("prior terminal bindings differ")
    if terminal.get("offset") != 0 or terminal.get("selected") != selected:
        problems.append("prior terminal window differs")
    if terminal.get("swept_at_updates") != 0:
        problems.append("prior terminal records swept_at updates")
    if sum(int(terminal.get(field) or 0) for field in ("success", "failed", "held", "replay")) != selected:
        problems.append("prior terminal accounting does not reconcile")
    if prior_packet.get("digest") != terminal.get("authorized_packet_digest"):
        problems.append("prior terminal does not bind the supplied prior packet")
    for field in ("target", "plan_digest", "backup_receipt_digest"):
        if prior_packet.get(field) != packet.get(field):
            problems.append(f"prior packet {field} differs from current packet")
    old_size, new_size = prior_packet.get("batch_size"), packet.get("batch_size")
    if (not isinstance(old_size, int) or not isinstance(new_size, int)
            or old_size < 1 or new_size < old_size):
        problems.append("batch-size transition is not an explicitly bounded non-decreasing change")
    if problems:
        raise ContinuationRefused(f"prior terminal cannot be imported: {problems}")
    return {**terminal, "terminal_receipt_path": str(path)}


def _prior_aggregate(path: Path, *, prior_packet: Mapping[str, Any], packet: Mapping[str, Any],
                     plan: Mapping[str, Any], start_offset: int) -> None:
    """A later cursor is admissible only when immutable prior coverage proves it."""
    value = load_verified(path)
    problems: list[str] = []
    if value.get("kind") != KIND or value.get("version") != VERSION:
        problems.append("prior aggregate kind/version is wrong")
    if value.get("authorized_packet_digest") != prior_packet.get("digest"):
        problems.append("prior aggregate does not bind supplied prior packet")
    if value.get("plan_digest") != plan.get("digest") or value.get("outcome") != "complete_window":
        problems.append("prior aggregate plan/outcome is not resumable")
    for field in ("target", "plan_digest", "backup_receipt_digest"):
        if prior_packet.get(field) != packet.get(field):
            problems.append(f"prior packet {field} differs from current packet")
    old_size, new_size = prior_packet.get("batch_size"), packet.get("batch_size")
    if (not isinstance(old_size, int) or not isinstance(new_size, int)
            or old_size < 1 or new_size < old_size):
        problems.append("batch-size transition is not an explicitly bounded non-decreasing change")
    windows = list(value.get("windows") or [])
    cursor = int(value.get("start_offset") or 0)
    if cursor != 0 or not windows:
        problems.append("prior aggregate has no offset-zero coverage")
    for window in windows:
        if window.get("offset") != cursor or not isinstance(window.get("selected"), int):
            problems.append("prior aggregate windows are not contiguous")
            break
        cursor += window["selected"]
    if cursor != start_offset:
        problems.append("prior aggregate end does not equal requested start offset")
    totals = value.get("totals") or {}
    if totals.get("failed") != 0 or totals.get("swept_at_updates") != 0:
        problems.append("prior aggregate records failed processing or swept_at updates")
    if problems:
        raise ContinuationRefused(f"prior aggregate cannot be imported: {problems}")


def _aggregate(*, packet: Mapping[str, Any], plan: Mapping[str, Any], windows: list[Mapping[str, Any]],
               start_offset: int, max_batches: int, stopped: str | None,
               preflight_document: Mapping[str, Any]) -> dict[str, Any]:
    totals = {field: sum(int(window.get(field) or 0) for window in windows)
              for field in ("selected", "success", "failed", "held", "replay", "swept_at_updates")}
    body = {"kind": KIND, "version": VERSION, "authorized_packet_digest": packet["digest"],
            "plan_digest": plan["digest"], "start_offset": start_offset, "max_batches": max_batches,
            "preflight_digest": preflight_document["digest"],
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
                     max_batches: int, prior_packet: Mapping[str, Any] | None = None,
                     prior_terminal_path: Path | None = None,
                     prior_aggregate_path: Path | None = None,
                     preflight_document: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if not terminal_dir.is_dir() or max_batches < 1 or start_offset < 0:
        raise ContinuationRefused("existing terminal directory, non-negative offset, and positive max batches are required")
    if preflight_document is None:
        raise ContinuationRefused("a current immutable preflight is required")
    if aggregate_out.exists():
        raise ContinuationRefused("aggregate receipt path already exists")
    if start_offset == 0 and (prior_packet is None) != (prior_terminal_path is None):
        raise ContinuationRefused("prior packet and prior terminal must be supplied together")
    if start_offset > 0 and (prior_packet is None or prior_aggregate_path is None):
        raise ContinuationRefused("a later start offset requires prior packet and aggregate coverage")
    if start_offset > 0 and prior_terminal_path is not None:
        raise ContinuationRefused("a later start offset imports its aggregate, not an individual terminal")
    records = list(plan.get("records") or [])
    imported_prior = (_prior_terminal(prior_terminal_path, prior_packet=prior_packet,
                                      packet=apply_packet, plan=plan,
                                      selected=min(int(apply_packet["batch_size"]), len(records)))
                      if prior_packet is not None and prior_terminal_path is not None else None)
    if prior_aggregate_path is not None:
        if prior_packet is None:
            raise ContinuationRefused("prior aggregate requires a prior packet")
        _prior_aggregate(prior_aggregate_path, prior_packet=prior_packet, packet=apply_packet,
                         plan=plan, start_offset=start_offset)
    windows: list[dict[str, Any]] = []
    stopped: str | None = None
    batches_started = 0
    offset = start_offset
    while offset < len(records) and batches_started < max_batches:
        selected = min(int(apply_packet["batch_size"]), len(records) - offset)
        terminal = imported_prior if imported_prior is not None and offset == 0 else _existing(
            terminal_dir, packet=apply_packet, plan=plan, offset=offset, selected=selected,
            preflight_document=preflight_document)
        if terminal is None:
            terminal = apply.apply_batch(engine, plan=plan, design_packet=design_packet,
                                         apply_packet=apply_packet, backup_path=backup_path,
                                         authorization_token=token, offset=offset,
                                         terminal_dir=terminal_dir,
                                         preflight_document=preflight_document)
            terminal = load_verified(terminal["terminal_receipt_path"])
            terminal["terminal_receipt_path"] = str(terminal_dir / f"kg-stage3-processing-receipt-apply-{terminal['digest']}.json")
            problems = _valid_terminal(terminal, packet=apply_packet, plan=plan,
                                       offset=offset, selected=selected,
                                       preflight_document=preflight_document)
            if problems:
                raise ContinuationRefused(f"new terminal receipt is invalid: {problems}")
            batches_started += 1
        windows.append(terminal)
        if terminal["failed"]:
            stopped = "terminal_failure"
            break
        offset += selected
    aggregate = _aggregate(packet=apply_packet, plan=plan, windows=windows, start_offset=start_offset,
                           max_batches=max_batches, stopped=stopped,
                           preflight_document=preflight_document)
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
    parser.add_argument("--preflight", type=Path, required=True)
    parser.add_argument("--authorization-token", required=True)
    parser.add_argument("--terminal-dir", type=Path, required=True)
    parser.add_argument("--aggregate-out", type=Path, required=True)
    parser.add_argument("--start-offset", type=int, default=0)
    parser.add_argument("--max-batches", type=int, required=True)
    parser.add_argument("--prior-apply", type=Path,
                        help="immutable packet that owns an explicitly imported prior terminal")
    parser.add_argument("--prior-terminal", type=Path,
                        help="terminal receipt to import instead of replaying offset zero")
    parser.add_argument("--prior-aggregate", type=Path,
                        help="immutable aggregate proving contiguous coverage before a later cursor")
    args = parser.parse_args(argv)
    result = continue_batches(get_engine(), plan=load_verified(args.plan), design_packet=load_verified(args.design),
                              apply_packet=load_verified(args.apply), backup_path=args.backup,
                              token=args.authorization_token, terminal_dir=args.terminal_dir,
                              aggregate_out=args.aggregate_out, start_offset=args.start_offset,
                              max_batches=args.max_batches,
                              prior_packet=load_verified(args.prior_apply) if args.prior_apply else None,
                              prior_terminal_path=args.prior_terminal,
                              prior_aggregate_path=args.prior_aggregate,
                              preflight_document=load_verified(args.preflight))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
