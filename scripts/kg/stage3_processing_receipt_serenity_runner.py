#!/usr/bin/env python3
"""Serenity controller for the bounded Stage 3 processing-receipt backfill.

This is deliberately a controller, not a second writer.  Each call to the
guarded apply module owns exactly one 500-record SERIALIZABLE transaction.  A
write-once checkpoint is written only after its terminal receipt validates.
Consequently an interrupted controller resumes at the checkpoint; an
interruption after commit but before checkpoint is recovered as a zero-write
replay terminal.  No database transaction spans batches.

The controller emits a compact atomic ``REPORT:`` file.  If an explicitly
configured Codex thread is supplied, it then asks the supported local wake
bridge to deliver that file.  The report remains the recovery authority if the
wake cannot be delivered.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply as apply  # noqa: E402
from scripts.kg import stage3_processing_receipt_continue as continuation  # noqa: E402
from scripts.kg import stage3_processing_receipt_preflight as preflight  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-processing-receipt-serenity-checkpoint"
VERSION = "1.0"
BATCH_SIZE = 500
REPORT_LIMIT = 64 * 1024


class SerenityRefused(RuntimeError):
    """A fail-closed execution or recovery condition."""


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def _ref(path: Path, document: Mapping[str, Any]) -> dict[str, str]:
    return {"path": str(path), "digest": str(document["digest"])}


def _totals(values: Mapping[str, Any] | None = None) -> dict[str, int]:
    result = {name: 0 for name in ("selected", "success", "failed", "held", "replay",
                                   "swept_at_updates")}
    for name in result:
        value = (values or {}).get(name, 0)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise SerenityRefused(f"checkpoint {name} is not a non-negative integer")
        result[name] = value
    return result


def _add_totals(left: Mapping[str, Any], right: Mapping[str, Any]) -> dict[str, int]:
    return {name: _totals(left)[name] + _totals(right)[name] for name in _totals()}


def _seed(path: Path, plan: Mapping[str, Any]) -> tuple[int, dict[str, int], dict[str, str]]:
    """Accept only an immutable, contiguous zero-failure historical aggregate."""
    value = load_verified(path)
    if value.get("kind") != continuation.KIND or value.get("version") != continuation.VERSION:
        raise SerenityRefused(f"seed {path} is not a receipt continuation")
    if value.get("plan_digest") != plan.get("digest") or value.get("outcome") != "complete_window":
        raise SerenityRefused(f"seed {path} does not bind the current completed plan")
    offset = value.get("start_offset")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise SerenityRefused(f"seed {path} has an invalid start offset")
    total = _totals(value.get("totals"))
    if total["failed"] or total["swept_at_updates"]:
        raise SerenityRefused(f"seed {path} carries a failure or swept_at update")
    cursor = offset
    observed = _totals()
    for window in list(value.get("windows") or []):
        selected = window.get("selected")
        if window.get("offset") != cursor or not isinstance(selected, int) or selected <= 0:
            raise SerenityRefused(f"seed {path} has non-contiguous windows")
        for name in ("success", "failed", "held", "replay"):
            item = window.get(name)
            if not isinstance(item, int) or item < 0:
                raise SerenityRefused(f"seed {path} has an invalid {name} window count")
        if sum(window[name] for name in ("success", "failed", "held", "replay")) != selected:
            raise SerenityRefused(f"seed {path} window accounting does not reconcile")
        observed = _add_totals(observed, {"selected": selected, "success": window["success"],
                                          "failed": window["failed"], "held": window["held"],
                                          "replay": window["replay"], "swept_at_updates": 0})
        cursor += selected
    if observed != total:
        raise SerenityRefused(f"seed {path} aggregate accounting differs from its windows")
    return cursor, total, _ref(path, value)


def _terminal(path: Path, *, packet: Mapping[str, Any], plan: Mapping[str, Any],
              preflight_document: Mapping[str, Any]) -> dict[str, Any]:
    value = load_verified(path)
    selected = value.get("selected")
    offset = value.get("offset")
    if not isinstance(offset, int) or not isinstance(selected, int) or selected <= 0:
        raise SerenityRefused(f"terminal {path} has invalid window coordinates")
    problems = continuation._valid_terminal(value, packet=packet, plan=plan, offset=offset,
                                            selected=selected,
                                            preflight_document=preflight_document)
    if problems:
        raise SerenityRefused(f"terminal {path} is invalid: {problems}")
    return value


def _recovered_terminal(terminal_dir: Path, preflight_dir: Path, *, packet: Mapping[str, Any],
                        plan: Mapping[str, Any], offset: int, selected: int) -> tuple[Path, dict[str, Any], Path, dict[str, Any]] | None:
    """Find exactly one committed terminal that predates its checkpoint.

    This closes the only post-commit crash window.  The matching preflight is
    located by digest rather than treated as current: it was current when the
    terminal was committed, and the checkpoint binds that historical proof.
    """
    preflights: dict[str, tuple[Path, dict[str, Any]]] = {}
    for path in preflight_dir.glob("kg-stage3-processing-receipt-preflight-*.json"):
        value = load_verified(path)
        if value.get("kind") == preflight.KIND:
            digest = str(value.get("digest") or "")
            if digest in preflights:
                raise SerenityRefused(f"more than one preflight carries digest {digest}")
            preflights[digest] = (path, value)
    found: list[tuple[Path, dict[str, Any], Path, dict[str, Any]]] = []
    for path in terminal_dir.glob("kg-stage3-processing-receipt-apply-*.json"):
        value = load_verified(path)
        if value.get("offset") != offset or value.get("authorized_packet_digest") != packet.get("digest"):
            continue
        if value.get("selected") != selected or value.get("plan_digest") != plan.get("digest"):
            raise SerenityRefused(f"terminal {path} overlaps this cursor with incompatible bindings")
        item = preflights.get(str(value.get("preflight_digest") or ""))
        if item is None:
            raise SerenityRefused(f"terminal {path} has no immutable matching preflight")
        preflight_path, preflight_document = item
        found.append((path, _terminal(path, packet=packet, plan=plan,
                                      preflight_document=preflight_document),
                      preflight_path, preflight_document))
    if len(found) > 1:
        raise SerenityRefused(f"more than one terminal claims cursor {offset}")
    return found[0] if found else None


def _checkpoint_document(*, plan: Mapping[str, Any], packet: Mapping[str, Any],
                         backup: Path, offset: int, totals: Mapping[str, Any],
                         seed_artifacts: Sequence[Mapping[str, str]] | None = None,
                         previous: Mapping[str, str] | None = None,
                         terminal: Mapping[str, str] | None = None,
                         preflight_ref: Mapping[str, str] | None = None,
                         stopped: str | None = None) -> dict[str, Any]:
    if (seed_artifacts is None) == (previous is None):
        raise SerenityRefused("a checkpoint needs exactly a bootstrap seed or a prior checkpoint")
    body: dict[str, Any] = {
        "kind": KIND, "version": VERSION, "plan_digest": plan["digest"],
        "apply_packet_digest": packet["digest"], "backup_path": str(backup.resolve()),
        "offset": offset, "totals": _totals(totals), "stopped_reason": stopped,
    }
    if seed_artifacts is not None:
        body["seed_artifacts"] = list(seed_artifacts)
    else:
        body["previous_checkpoint"] = dict(previous or {})
        body["terminal"] = dict(terminal or {})
        body["preflight"] = dict(preflight_ref or {})
    return body


def _checkpoint_path(directory: Path, *, label: str) -> Path:
    return directory / f"kg-stage3-processing-receipt-serenity-{label}-{_stamp()}.json"


def _write_checkpoint(directory: Path, document: Mapping[str, Any], *, label: str) -> tuple[Path, dict[str, Any]]:
    path = _checkpoint_path(directory, label=label)
    write_immutable(path, document)
    return path, load_verified(path)


def _load_checkpoint(path: Path, *, plan: Mapping[str, Any], packet: Mapping[str, Any],
                     backup: Path) -> dict[str, Any]:
    value = load_verified(path)
    if value.get("kind") != KIND or value.get("version") != VERSION:
        raise SerenityRefused(f"checkpoint {path} kind/version differs")
    if value.get("plan_digest") != plan.get("digest") or value.get("apply_packet_digest") != packet.get("digest"):
        raise SerenityRefused(f"checkpoint {path} plan or packet binding differs")
    if value.get("backup_path") != str(backup.resolve()):
        raise SerenityRefused(f"checkpoint {path} backup binding differs")
    offset = value.get("offset")
    if not isinstance(offset, int) or offset < 0 or offset > len(plan.get("records") or []):
        raise SerenityRefused(f"checkpoint {path} offset is invalid")
    totals = _totals(value.get("totals"))
    if totals["selected"] != offset or totals["failed"] or totals["swept_at_updates"]:
        raise SerenityRefused(f"checkpoint {path} has unsafe aggregate accounting")
    return value


def _heads(directory: Path, *, plan: Mapping[str, Any], packet: Mapping[str, Any], backup: Path) -> list[tuple[Path, dict[str, Any]]]:
    """Return one verified head; malformed matching checkpoint evidence is a stop."""
    values: list[tuple[Path, dict[str, Any]]] = []
    referenced: set[str] = set()
    for path in directory.glob("kg-stage3-processing-receipt-serenity-*.json"):
        value = _load_checkpoint(path, plan=plan, packet=packet, backup=backup)
        values.append((path, value))
        prior = value.get("previous_checkpoint")
        if isinstance(prior, Mapping):
            referenced.add(str(prior.get("digest") or ""))
    heads = [(path, value) for path, value in values if str(value.get("digest")) not in referenced]
    if len(heads) > 1:
        raise SerenityRefused("more than one Serenity checkpoint head exists")
    return heads


def _verify_chain(path: Path, value: Mapping[str, Any], *, plan: Mapping[str, Any],
                  packet: Mapping[str, Any], backup: Path) -> None:
    """Verify every checkpoint/terminal link back to its immutable bootstrap."""
    visited: set[str] = set()
    current_path, current = path, value
    while True:
        digest = str(current.get("digest") or "")
        if not digest or digest in visited:
            raise SerenityRefused("checkpoint lineage is cyclic or missing a digest")
        visited.add(digest)
        prior = current.get("previous_checkpoint")
        if prior is None:
            seeds = current.get("seed_artifacts")
            if not isinstance(seeds, list) or not seeds:
                raise SerenityRefused("bootstrap checkpoint has no seed artifacts")
            if seeds == [{"path": "genesis", "digest": "genesis"}]:
                if current["offset"] != 0 or _totals(current["totals"]) != _totals():
                    raise SerenityRefused("genesis checkpoint is not zero-offset and zero-total")
                return
            offset = 0
            totals = _totals()
            for seed_ref in seeds:
                seed_path = Path(str(seed_ref.get("path") or ""))
                cursor, next_totals, verified = _seed(seed_path, plan)
                if verified != dict(seed_ref) or cursor - next_totals["selected"] != offset:
                    raise SerenityRefused("bootstrap seed reference or coverage differs")
                offset = cursor
                totals = _add_totals(totals, next_totals)
            if offset != current["offset"] or totals != _totals(current["totals"]):
                raise SerenityRefused("bootstrap checkpoint totals differ from its seeds")
            return
        if not isinstance(prior, Mapping):
            raise SerenityRefused("checkpoint prior reference is malformed")
        prior_path = Path(str(prior.get("path") or ""))
        previous = _load_checkpoint(prior_path, plan=plan, packet=packet, backup=backup)
        if _ref(prior_path, previous) != dict(prior):
            raise SerenityRefused("checkpoint prior reference digest differs")
        preflight_ref = current.get("preflight")
        terminal_ref = current.get("terminal")
        if not isinstance(preflight_ref, Mapping) or not isinstance(terminal_ref, Mapping):
            raise SerenityRefused("checkpoint terminal or preflight reference is missing")
        preflight_path = Path(str(preflight_ref.get("path") or ""))
        preflight_document = load_verified(preflight_path)
        if _ref(preflight_path, preflight_document) != dict(preflight_ref):
            raise SerenityRefused("checkpoint preflight digest differs")
        terminal_path = Path(str(terminal_ref.get("path") or ""))
        terminal = _terminal(terminal_path, packet=packet, plan=plan,
                             preflight_document=preflight_document)
        if _ref(terminal_path, terminal) != dict(terminal_ref):
            raise SerenityRefused("checkpoint terminal digest differs")
        if terminal["offset"] != previous["offset"] or current["offset"] != terminal["offset"] + terminal["selected"]:
            raise SerenityRefused("checkpoint cursor is not contiguous")
        expected = _add_totals(previous["totals"], terminal)
        if _totals(current["totals"]) != expected:
            raise SerenityRefused("checkpoint totals do not include exactly one terminal")
        current_path, current = prior_path, previous


def _bootstrap(checkpoint_dir: Path, *, seed_paths: Sequence[Path], plan: Mapping[str, Any],
               packet: Mapping[str, Any], backup: Path) -> tuple[Path, dict[str, Any]]:
    if not seed_paths:
        if len(plan.get("records") or []) == 0:
            raise SerenityRefused("empty plans cannot be bootstrapped")
        document = _checkpoint_document(plan=plan, packet=packet, backup=backup, offset=0,
                                        totals=_totals(), seed_artifacts=[])
        # An offset-zero execution has no historic receipt.  Use a self-contained
        # genesis marker rather than accepting an omitted or mutable cursor.
        document["seed_artifacts"] = [{"path": "genesis", "digest": "genesis"}]
        return _write_checkpoint(checkpoint_dir, document, label="bootstrap")
    offset, totals, refs = 0, _totals(), []
    for seed_path in seed_paths:
        cursor, next_totals, ref = _seed(seed_path, plan)
        if cursor - next_totals["selected"] != offset:
            raise SerenityRefused("supplied seed continuations are not contiguous")
        offset, totals = cursor, _add_totals(totals, next_totals)
        refs.append(ref)
    document = _checkpoint_document(plan=plan, packet=packet, backup=backup, offset=offset,
                                    totals=totals, seed_artifacts=refs)
    return _write_checkpoint(checkpoint_dir, document, label="bootstrap")


def _preflight_path(directory: Path) -> Path:
    return directory / f"kg-stage3-processing-receipt-preflight-serenity-{_stamp()}.json"


def _fresh_preflight(engine: Any, *, directory: Path, plan: Mapping[str, Any], design: Mapping[str, Any],
                     packet: Mapping[str, Any], backup: Path) -> tuple[Path, dict[str, Any]]:
    path = _preflight_path(directory)
    value = preflight.build(engine=engine, plan=plan, design_packet=design, apply_packet=packet,
                            backup_path=backup, current_code_digest=apply.code_digest())
    write_immutable(path, value)
    return path, load_verified(path)


def _report(path: Path, *, outcome: str, checkpoint: Mapping[str, Any] | None,
            error: str | None, notified: str, plan: Mapping[str, Any] | None = None,
            packet: Mapping[str, Any] | None = None, backup: Path | None = None) -> None:
    if path.suffix != ".pending" or path.exists():
        raise SerenityRefused("report path must be a new .pending file")
    lines = ["REPORT: Stage 3 Serenity receipt backfill", f"Outcome: {outcome}",
             "Queue item: Q5 current-version processing receipts.",
             f"Artifacts/digests: plan={((plan or {}).get('digest') or 'unavailable')}; "
             f"apply_packet={((packet or {}).get('digest') or 'unavailable')}; "
             f"backup={str(backup) if backup else 'unavailable'}.",
             "Tests: runtime executes no tests; only the reviewed, digest-bound code is admitted.",
             "Failures repaired: none by the controller; a refusal or failed terminal stops immediately.",
             "Remaining P0/P1: none asserted by the controller; inspect any named refusal before retry.",
             f"Wake: {notified}."]
    if checkpoint:
        lines.extend([f"Checkpoint: {checkpoint.get('path')} ({checkpoint.get('digest')}).",
                      f"Offset: {checkpoint.get('offset')}; totals: {json.dumps(checkpoint.get('totals'), sort_keys=True)}."])
    if error:
        lines.append(f"Failure/refusal: {error}")
    lines.append("Mutations: receipt batches only; no swept_at, production, sync, or deployment action.")
    lines.append("Next: receipt completion verification, then the separately gated B3 apply packet.")
    content = "\n".join(lines) + "\n"
    if len(content.encode("utf-8")) > REPORT_LIMIT:
        raise SerenityRefused("report exceeds supported bridge limit")
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content); handle.flush(); os.fsync(handle.fileno())


def _notify(report: Path, thread_id: str | None) -> str:
    if not thread_id:
        return "not attempted: --notify-thread-id was not supplied; .pending is authoritative"
    command = [sys.executable, "-u", str(REPO / "scripts/ops/notify_codex_thread.py"),
               "--report", str(report), "--thread-id", thread_id]
    result = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            check=False)
    if result.returncode:
        return f"attempted but failed ({result.stderr.strip() or result.returncode}); .pending retained"
    return "delivered by scripts/ops/notify_codex_thread.py"


def run(engine: Any, *, plan: Mapping[str, Any], design: Mapping[str, Any], packet: Mapping[str, Any],
        backup: Path, token: str, terminal_dir: Path, preflight_dir: Path, checkpoint_dir: Path,
        seed_paths: Sequence[Path], renewal_seconds: int, report_out: Path,
        notify_thread_id: str | None = None) -> dict[str, Any]:
    """Run independent batches to completion or the first refusal/failure."""
    directories = (terminal_dir, preflight_dir, checkpoint_dir, report_out.parent)
    if any(not path.is_dir() for path in directories):
        raise SerenityRefused("terminal, preflight, checkpoint, and report directories must already exist")
    if packet.get("batch_size") != BATCH_SIZE:
        raise SerenityRefused(f"authorized packet must retain the {BATCH_SIZE}-row batch size")
    if renewal_seconds < 1 or renewal_seconds > preflight.MAX_AGE_SECONDS:
        raise SerenityRefused("preflight renewal interval must be positive and no older than the preflight maximum")
    heads = _heads(checkpoint_dir, plan=plan, packet=packet, backup=backup)
    if heads:
        checkpoint_path, checkpoint = heads[0]
        _verify_chain(checkpoint_path, checkpoint, plan=plan, packet=packet, backup=backup)
    else:
        checkpoint_path, checkpoint = _bootstrap(checkpoint_dir, seed_paths=seed_paths, plan=plan,
                                                  packet=packet, backup=backup)
    print(json.dumps({"event": "resume", "offset": checkpoint["offset"],
                      "checkpoint": str(checkpoint_path)}, sort_keys=True), flush=True)
    preflight_path: Path | None = None
    preflight_document: dict[str, Any] | None = None
    preflight_started = 0.0
    records = list(plan.get("records") or [])
    while checkpoint["offset"] < len(records):
        if preflight_document is None or monotonic() - preflight_started >= renewal_seconds:
            preflight_path, preflight_document = _fresh_preflight(engine, directory=preflight_dir,
                plan=plan, design=design, packet=packet, backup=backup)
            preflight_started = monotonic()
            print(json.dumps({"event": "preflight", "digest": preflight_document["digest"],
                              "path": str(preflight_path)}, sort_keys=True), flush=True)
        offset = int(checkpoint["offset"])
        try:
            selected = min(BATCH_SIZE, len(records) - offset)
            recovered = _recovered_terminal(terminal_dir, preflight_dir, packet=packet, plan=plan,
                                            offset=offset, selected=selected)
            if recovered is None:
                terminal = apply.apply_batch(engine, plan=plan, design_packet=design, apply_packet=packet,
                    backup_path=backup, authorization_token=token, offset=offset,
                    terminal_dir=terminal_dir, preflight_document=preflight_document)
                terminal_path = Path(str(terminal["terminal_receipt_path"]))
                terminal = _terminal(terminal_path, packet=packet, plan=plan,
                                     preflight_document=preflight_document)
            else:
                terminal_path, terminal, preflight_path, preflight_document = recovered
                print(json.dumps({"event": "recover_terminal", "offset": offset,
                                  "terminal": terminal["digest"]}, sort_keys=True), flush=True)
        except Exception as exc:
            raise SerenityRefused(f"batch at offset {offset} refused/failed: {type(exc).__name__}: {exc}") from exc
        new_totals = _add_totals(checkpoint["totals"], terminal)
        document = _checkpoint_document(plan=plan, packet=packet, backup=backup,
            offset=offset + terminal["selected"], totals=new_totals,
            previous=_ref(checkpoint_path, checkpoint), terminal=_ref(terminal_path, terminal),
            preflight_ref=_ref(preflight_path, preflight_document),
            stopped="terminal_failure" if terminal["failed"] else None)
        checkpoint_path, checkpoint = _write_checkpoint(checkpoint_dir, document, label="checkpoint")
        checkpoint["path"] = str(checkpoint_path)
        print(json.dumps({"event": "checkpoint", "offset": checkpoint["offset"],
                          "terminal": terminal["digest"], "totals": checkpoint["totals"]},
                         sort_keys=True), flush=True)
        if terminal["failed"]:
            raise SerenityRefused(f"terminal failure recorded at offset {offset}")
    checkpoint["path"] = str(checkpoint_path)
    _report(report_out, outcome="complete", checkpoint=checkpoint, error=None,
            notified="pending explicit post-report delivery", plan=plan, packet=packet, backup=backup)
    wake = _notify(report_out, notify_thread_id)
    print(json.dumps({"event": "complete", "report": str(report_out), "wake": wake}, sort_keys=True), flush=True)
    return checkpoint


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("plan", "design", "apply", "backup"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--authorization-token", required=True)
    parser.add_argument("--terminal-dir", type=Path, required=True)
    parser.add_argument("--preflight-dir", type=Path, required=True)
    parser.add_argument("--checkpoint-dir", type=Path, required=True)
    parser.add_argument("--seed", type=Path, action="append", default=[])
    parser.add_argument("--preflight-renewal-seconds", type=int, default=900)
    parser.add_argument("--report-out", type=Path, required=True)
    parser.add_argument("--notify-thread-id", default=os.environ.get("POLISCOPIC_CODEX_THREAD"),
                        help="optional manager thread; defaults to POLISCOPIC_CODEX_THREAD")
    args = parser.parse_args(argv)
    checkpoint: dict[str, Any] | None = None
    try:
        checkpoint = run(get_engine(), plan=load_verified(args.plan), design=load_verified(args.design),
            packet=load_verified(args.apply), backup=args.backup, token=args.authorization_token,
            terminal_dir=args.terminal_dir, preflight_dir=args.preflight_dir,
            checkpoint_dir=args.checkpoint_dir, seed_paths=args.seed,
            renewal_seconds=args.preflight_renewal_seconds, report_out=args.report_out,
            notify_thread_id=args.notify_thread_id)
        return 0
    except Exception as exc:
        try:
            _report(args.report_out, outcome="stopped", checkpoint=checkpoint,
                    error=f"{type(exc).__name__}: {exc}",
                    notified="not attempted because execution did not complete")
        except Exception as report_error:
            print(f"Serenity report failure: {report_error}", file=sys.stderr, flush=True)
        print(f"Serenity receipt runner stopped: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
