#!/usr/bin/env python3
"""CLI for the one-operation plan (G7) and the authorization record (G8).

RECORD ONLY
    ``authorize`` transcribes a human's approval from a FILE and never composes,
    infers, paraphrases or broadens one.  Passing words the human did not say is
    a self-authorization, which the checklist prohibits.  The file's sha256 is
    recorded alongside the text so transcription can be checked.

Usage
    plan_operation.py plan --operation OP-RECON --operation-id newsletter-daily-20260921 \\
        --entry-point scripts/editorial_sync.py \\
        --scope tags,articles,article_sources,article_tags \\
        --code-path scripts/editorial_sync.py \\
        --code-path scripts/ops/production_interlock.py \\
        --rollback-owner "Pete Mains" --days 90

    plan_operation.py authorize --operation OP-RECON \\
        --operation-id newsletter-daily-20260921 \\
        --verbatim-file /tmp/approval.txt --author "Pete Mains" \\
        --mode standing --max-uses 200

    plan_operation.py show --operation OP-RECON
    plan_operation.py validate --operation OP-RECON \\
        --entry-point scripts/editorial_sync.py --scope tags,articles
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

OPS_DIR = Path(__file__).resolve().parent
if str(OPS_DIR) not in sys.path:
    sys.path.insert(0, str(OPS_DIR))

import operation_authorization as oa  # noqa: E402

#: A hex run long enough to be a digest (or a digest prefix).
_HEX_TOKEN = re.compile(r"\b[0-9a-fA-F]{6,64}\b")


def _split(value: str) -> list[str]:
    return [s.strip() for s in value.split(",") if s.strip()]


def _load_plan(operation: str, operation_id: str) -> dict:
    path = oa.operation_dir(operation, operation_id) / "plan.json"
    if not path.is_file():
        raise SystemExit(f"no plan at {path}")
    plan = json.loads(path.read_text())
    if plan.get("digest") != oa.plan_digest(plan):
        raise SystemExit("plan digest does not match its contents — refusing")
    return plan


def cmd_plan(args: argparse.Namespace) -> int:
    now = datetime.now(timezone.utc)
    plan = oa.build_plan(
        operation=args.operation,
        operation_id=args.operation_id,
        entry_point=args.entry_point,
        scope=_split(args.scope),
        code_paths=args.code_path,
        rollback_owner=args.rollback_owner,
        not_before=now,
        not_after=now + timedelta(days=args.days),
        target=args.target,
        notes=args.notes,
        mode=args.mode,
    )
    path = oa.write_plan(plan)
    print(f"plan written: {path}")
    print(f"  operation:      {plan['operation']}")
    print(f"  operation_id:   {plan['operation_id']}")
    print(f"  entry_point:    {plan['entry_point']}")
    print(f"  target:         {plan['target']}")
    print(f"  mode:           {plan['mode']}")
    print(f"  scope:          {', '.join(plan['scope'])}")
    print(f"  rollback_owner: {plan['rollback_owner']}")
    print(f"  window:         {plan['not_before']} .. {plan['not_after']}")
    print(f"  code bound:     {len(plan['code_hashes'])} file(s)")
    print(f"  DIGEST:         {plan['digest']}")
    return 0


def cmd_authorize(args: argparse.Namespace) -> int:
    plan = _load_plan(args.operation, args.operation_id)

    verbatim_path = Path(args.verbatim_file)
    if not verbatim_path.is_file():
        raise SystemExit(f"verbatim approval file not found: {verbatim_path}")
    verbatim = verbatim_path.read_text().strip()
    if not verbatim:
        raise SystemExit("verbatim approval file is empty")

    # G8: if the approval quotes a digest, it MUST be this plan's. Catches a human
    # approving a different plan, or approving one whose contents have since moved.
    quoted = _HEX_TOKEN.findall(verbatim)
    if quoted and not any(plan["digest"].startswith(tok.lower()) for tok in quoted):
        raise SystemExit(
            "the approval text quotes a digest that does not match this plan — "
            f"plan is {plan['digest'][:16]}..., quoted {quoted}"
        )

    try:
        path = oa.record_authorization(
            plan,
            verbatim_approval=verbatim,
            author=args.author,
            authorized_at=datetime.now(timezone.utc),
            mode=args.mode,
            max_uses=args.max_uses,
            verbatim_source={
                "file": str(verbatim_path),
                "sha256": hashlib.sha256(verbatim.encode("utf-8")).hexdigest(),
            },
        )
    except FileExistsError as exc:
        raise SystemExit(
            f"{exc}\n  an existing authorization is never overwritten — a new "
            "operation needs a new operation-id (single-use semantics)"
        )

    print(f"authorization recorded: {path}")
    print(f"  mode:        {args.mode}")
    print(f"  max_uses:    {args.max_uses}")
    print(f"  author:      {args.author}")
    print(f"  plan_digest: {plan['digest']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    directory = oa.release_dir()
    found = sorted(p for p in directory.glob(f"{args.operation}-*") if p.is_dir()) \
        if directory.is_dir() else []
    if not found:
        print(f"no authorization directories for {args.operation}")
        return 0
    for op_dir in found:
        print(f"=== {op_dir.name} ===")
        for name in ("plan.json", "authorization.json"):
            path = op_dir / name
            if not path.is_file():
                print(f"  {name}: ABSENT")
                continue
            payload = json.loads(path.read_text())
            print(f"  {name}:")
            for key, value in sorted(payload.items()):
                if key in ("code_hashes",):
                    print(f"    {key}: {len(value)} file(s)")
                    continue
                if key == "verbatim_approval":
                    print(f"    {key}: {value!r}")
                    continue
                print(f"    {key}: {value}")
        oid = json.loads((op_dir / "plan.json").read_text()).get("operation_id") \
            if (op_dir / "plan.json").is_file() else op_dir.name
        print(f"  uses recorded: {oa.count_uses(oid)}")
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    verdict = oa.validate(
        args.operation,
        entry_point=args.entry_point,
        scope=_split(args.scope) or None,
        target=args.target,
        # The mode is REQUIRED for a production-mutating operation: omitting it
        # must never be a way to satisfy an authorization by omission.
        mode=args.mode,
    )
    print(json.dumps(verdict, indent=2, sort_keys=True))
    return 0 if verdict.get("status") == "ALLOWED" else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="One-operation plan / authorization CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_plan = sub.add_parser("plan", help="build and record a digest-bound plan")
    p_plan.add_argument("--operation", required=True)
    p_plan.add_argument("--operation-id", required=True)
    p_plan.add_argument("--entry-point", required=True)
    p_plan.add_argument("--scope", required=True, help="comma-separated tables")
    p_plan.add_argument("--code-path", action="append", required=True,
                        help="repeatable; code to bind by sha256")
    p_plan.add_argument("--rollback-owner", required=True)
    p_plan.add_argument("--days", type=int, default=90)
    p_plan.add_argument("--target", default="production")
    p_plan.add_argument("--mode", choices=oa.KNOWN_MODES, default="upsert",
                        help="the execution mode this authorization is for; a table "
                             "scope alone cannot separate an upsert from a delete "
                             "or a schema change")
    p_plan.add_argument("--notes", default="")
    p_plan.set_defaults(func=cmd_plan)

    p_auth = sub.add_parser("authorize", help="record a human's verbatim approval")
    p_auth.add_argument("--operation", required=True)
    p_auth.add_argument("--operation-id", required=True)
    p_auth.add_argument("--verbatim-file", required=True,
                        help="file holding the human's exact words")
    p_auth.add_argument("--author", required=True)
    p_auth.add_argument("--mode", choices=("single-use", "standing"), default="single-use")
    p_auth.add_argument("--max-uses", type=int, default=1)
    p_auth.set_defaults(func=cmd_authorize)

    p_show = sub.add_parser("show", help="show plan/authorization state")
    p_show.add_argument("--operation", required=True)
    p_show.set_defaults(func=cmd_show)

    p_val = sub.add_parser("validate", help="run the validator for one request")
    p_val.add_argument("--operation", required=True)
    p_val.add_argument("--entry-point", default="")
    p_val.add_argument("--scope", default="")
    p_val.add_argument("--target", default="production")
    p_val.add_argument("--mode", default=None,
                       help="execution mode of the request being validated; "
                            "required for production-mutating operations")
    p_val.set_defaults(func=cmd_validate)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
