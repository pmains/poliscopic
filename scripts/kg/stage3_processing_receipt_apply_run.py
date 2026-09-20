#!/usr/bin/env python3
"""Command-line admission for the guarded Stage 3 receipt apply.

The two modes are intentionally separate: ``--authorize`` only writes a
digest-bound apply packet; ``--apply`` consumes one.  Both modes use canonical
artifact loaders and no option supplies a database target, so the runner cannot
be redirected away from the configured development engine.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from db.core import get_engine  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply as apply  # noqa: E402
from scripts.kg import stage3_processing_receipt_apply_packet as packet  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402


def _load(path: Path) -> dict:
    return load_verified(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true",
                      help="report non-mutating target bindings before authorization or apply")
    mode.add_argument("--authorize", type=Path, metavar="PACKET",
                      help="new immutable authorized apply packet path")
    mode.add_argument("--apply", type=Path, metavar="PACKET",
                      help="existing immutable authorized apply packet path")
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--design", type=Path, required=True)
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--approver", help="required with --authorize")
    parser.add_argument("--writer-role", help="required with --authorize")
    parser.add_argument("--batch-size", type=int, help="required with --authorize")
    parser.add_argument("--authorization-token", help="required with --apply")
    parser.add_argument("--terminal-dir", type=Path, help="required with --apply")
    parser.add_argument("--offset", type=int, default=0)
    args = parser.parse_args(argv)
    if args.offset < 0:
        parser.error("--offset must be non-negative")
    plan, design = _load(args.plan), _load(args.design)
    backup = _load(args.backup)
    if args.preflight:
        print(json.dumps({"engine_target": apply._target(get_engine()),
                          "plan_target": plan.get("target"),
                          "design_target": design.get("target"),
                          "backup_target": backup.get("target")}, sort_keys=True))
        return 0
    if args.authorize:
        if not all((args.approver, args.writer_role, args.batch_size)):
            parser.error("--authorize requires --approver, --writer-role, and --batch-size")
        document = packet.build(plan=plan, design_packet=design,
                                backup_receipt_path=str(args.backup.resolve()),
                                backup_receipt_digest=str(backup["digest"]),
                                code_digest=apply.code_digest(), approver=args.approver,
                                writer_role=args.writer_role, batch_size=args.batch_size)
        digest = write_immutable(args.authorize, document)
        print(json.dumps({"outcome": "authorized", "packet": str(args.authorize),
                          "digest": digest}, sort_keys=True))
        return 0
    if args.terminal_dir is None:
        parser.error("--apply requires --terminal-dir")
    if not args.authorization_token:
        parser.error("--apply requires --authorization-token")
    document = _load(args.apply)
    result = apply.apply_batch(get_engine(), plan=plan, design_packet=design,
                               apply_packet=document, backup_path=args.backup,
                               authorization_token=args.authorization_token,
                               offset=args.offset, terminal_dir=args.terminal_dir)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
