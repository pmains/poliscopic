#!/usr/bin/env python3
"""Read-only production integrity packet + digest-bound remediation plan.

Brief 037 declared production converged on row counts.  Row counts do not show
referential integrity, so this tool gathers the *row-identifying* evidence for
the four unresolved production facts:

  1. meetings whose public_body_id is NULL
  2. meetings whose public_body_id points at no public_bodies row (dangling)
  3. meetings whose body code and public_body_id disagree
  4. schema parity between development and production:
       - FK on meetings.public_body_id (dev has one, prod does not)
       - uniqueness of public_bodies.body_code (non-unique; app uses LIMIT 1)

It writes an evidence packet and a remediation plan, each with a content digest.
It NEVER writes to any database and has no apply path.  The plan is a proposal
for review, deliberately not executable.

Usage:
    python3 scripts/ops/prod_integrity_packet.py [--out DIR]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import create_engine, inspect, text  # noqa: E402

# Row-level evidence is capped so the packet stays reviewable; the counts are
# always exact, and truncation is recorded explicitly rather than hidden.
ROW_LIMIT = 500


def sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, default=str).encode()
    ).hexdigest()


def _schema(engine) -> dict:
    insp = inspect(engine)
    out: dict = {"fk_on_public_body_id": None, "body_code_unique": None,
                 "body_code_indexes": [], "meetings_has_public_body_id": None}
    try:
        cols = {c["name"] for c in insp.get_columns("meetings")}
        out["meetings_has_public_body_id"] = "public_body_id" in cols
    except Exception:
        return out
    with engine.connect() as c:
        row = c.execute(text("""
            select con.conname
            from pg_constraint con
            join pg_class cl on cl.oid = con.conrelid
            join pg_class rf on rf.oid = con.confrelid
            where con.contype = 'f' and rf.relname = 'public_bodies'
        """)).fetchall()
        out["fk_on_public_body_id"] = [r[0] for r in row]
    for ix in insp.get_indexes("public_bodies"):
        if "body_code" in (ix.get("column_names") or []):
            out["body_code_indexes"].append(
                {"name": ix["name"], "unique": bool(ix.get("unique"))})
            if out["body_code_unique"] is None:
                out["body_code_unique"] = bool(ix.get("unique"))
    return out


def collect(engine) -> dict:
    with engine.connect() as c:
        total = c.execute(text("select count(*) from meetings")).scalar()
        null_id = c.execute(text(
            "select count(*) from meetings where public_body_id is null"
        )).scalar()
        null_body = c.execute(text(
            "select count(*) from meetings where body is null"
        )).scalar()
        dangling = c.execute(text("""
            select count(*) from meetings m
            where m.public_body_id is not null
              and not exists (select 1 from public_bodies pb
                              where pb.id = m.public_body_id)
        """)).scalar()
        disagree = c.execute(text("""
            select count(*) from meetings m
            where m.public_body_id is not null and m.body is not null
              and exists (select 1 from public_bodies pb
                          where pb.id = m.public_body_id)
              and not exists (select 1 from public_bodies pb2
                              where pb2.id = m.public_body_id
                                and pb2.body_code = m.body)
        """)).scalar()

        dangling_rows = [dict(r) for r in c.execute(text("""
            select m.id, m.body, m.public_body_id, m.meeting_id, m.meeting_date
            from meetings m
            where m.public_body_id is not null
              and not exists (select 1 from public_bodies pb
                              where pb.id = m.public_body_id)
            order by m.id limit :lim
        """), {"lim": ROW_LIMIT}).mappings()]

        disagree_rows = [dict(r) for r in c.execute(text("""
            select m.id, m.body as body_code, m.public_body_id,
                   pb.body_code as id_row_code, pb.name as id_row_name,
                   m.meeting_id, m.meeting_date
            from meetings m
            join public_bodies pb on pb.id = m.public_body_id
            where m.body is not null
              and pb.body_code <> m.body
            order by m.id limit :lim
        """), {"lim": ROW_LIMIT}).mappings()]

        null_rows = [dict(r) for r in c.execute(text("""
            select m.id, m.body, m.meeting_id, m.meeting_date
            from meetings m
            where m.public_body_id is null
            order by m.id limit :lim
        """), {"lim": ROW_LIMIT}).mappings()]

    return {
        "counts": {
            "meetings_total": total,
            "public_body_id_null": null_id,
            "body_null": null_body,
            "dangling_public_body_id": dangling,
            "body_vs_id_disagree": disagree,
        },
        "rows": {
            "dangling_public_body_id": dangling_rows,
            "body_vs_id_disagree": disagree_rows,
            "public_body_id_null": null_rows,
        },
        "truncated": {
            k: len(v) >= ROW_LIMIT for k, v in {
                "dangling_public_body_id": dangling_rows,
                "body_vs_id_disagree": disagree_rows,
                "public_body_id_null": null_rows}.items()
        },
        "row_limit": ROW_LIMIT,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=_REPO_ROOT / "data" / "audit")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    from db.core import get_engine
    from db.tier import PRODUCTION_PRIMARY_HOST
    from db.sync_targets import _resolve_prod_url

    dev = get_engine()
    prod = create_engine(_resolve_prod_url(), future=True)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    evidence = {
        "kind": "prod-integrity-evidence",
        "created_at": stamp,
        "mode": "read-only",
        "production": {
            "database": "poliscopic",
            "host": PRODUCTION_PRIMARY_HOST,
        },
        "integrity": collect(prod),
        "schema": {"production": _schema(prod), "development": _schema(dev)},
    }
    evidence["content_digest"] = sha(evidence["integrity"])

    schema_parity = {
        "fk_present": {
            "production": bool(evidence["schema"]["production"]
                               .get("fk_on_public_body_id")),
            "development": bool(evidence["schema"]["development"]
                                .get("fk_on_public_body_id")),
        },
        "body_code_unique": {
            "production": evidence["schema"]["production"].get("body_code_unique"),
            "development": evidence["schema"]["development"].get("body_code_unique"),
        },
    }

    plan = {
        "kind": "prod-integrity-remediation-plan",
        "created_at": stamp,
        "status": "PROPOSED — NOT APPLIED — NO APPLY PATH IN THIS TOOL",
        "binds_evidence_digest": evidence["content_digest"],
        "schema_parity": schema_parity,
        "steps": [
            {
                "id": "R1",
                "title": "Propagate the public_body_id backfill",
                "why": ("counts.public_body_id_null is 1412 on production and 220 "
                        "on development; the backfill in scripts/db/migrations.py "
                        "does not advance updated_at, so the sync cannot see it"),
                "action": ("stamp the development rows through the declared "
                           "propagation contract, then let the ordinary sync "
                           "carry them — no direct production write"),
                "blocked_by": "gate A (contract + tests) must land first",
            },
            {
                "id": "R2",
                "title": "Resolve the 51 dangling public_body_id values",
                "why": "rows point at public_bodies ids that do not exist",
                "action": ("derive each from its body code where the code resolves, "
                           "otherwise set NULL; needs row review, not a blanket "
                           "UPDATE"),
                "requires": "row-identifying review of rows.dangling_public_body_id",
            },
            {
                "id": "R3",
                "title": "Resolve the body/id disagreements",
                "why": "the code string and the id disagree on the same row",
                "action": "review rows.body_vs_id_disagree and fix per row",
                "requires": "row-identifying review",
            },
            {
                "id": "R4",
                "title": "Schema parity: add the missing FK on production",
                "why": "development declares meetings.public_body_id -> public_bodies",
                "action": ("ALTER TABLE meetings ADD CONSTRAINT ... after R2/R3 "
                           "leave zero violations; adding it while 51 rows dangle "
                           "will fail"),
                "blocked_by": "R2",
                "must_not": "validate before R2/R3 are clean",
            },
            {
                "id": "R5",
                "title": "Uniqueness of public_bodies.body_code — AUDIT FIRST",
                "why": ("application queries join on body_code with LIMIT 1; a "
                        "non-unique code makes the join ambiguous"),
                "action": ("do NOT add a unique constraint yet. First audit "
                           "cross-jurisdiction aliases and legitimate duplicate "
                           "semantics (the same body published under more than "
                           "one code, historic renames, per-city prefixes)"),
                "must_not": "add UNIQUE(body_code) before the alias audit",
            },
        ],
        "verification": [
            "counts.public_body_id_null == development count",
            "counts.dangling_public_body_id == 0",
            "counts.body_vs_id_disagree == 0",
            "schema_parity.fk_present.production == true",
            "no application query resolves a registry row by LIMIT 1",
        ],
    }
    plan["plan_digest"] = sha({k: v for k, v in plan.items()
                               if k not in ("plan_digest",)})

    ev_path = args.out / f"prod-integrity-evidence-{stamp}.json"
    pl_path = args.out / f"prod-integrity-plan-{stamp}.json"
    ev_path.write_text(json.dumps(evidence, indent=2, default=str))
    pl_path.write_text(json.dumps(plan, indent=2, default=str))
    os.chmod(ev_path, 0o600)
    os.chmod(pl_path, 0o600)

    ic = evidence["integrity"]["counts"]
    print("=" * 74)
    print("PRODUCTION INTEGRITY EVIDENCE (read-only)")
    print("=" * 74)
    for k, v in ic.items():
        print(f"  {k:<28} {v}")
    print()
    print(f"  schema parity fk_present: {schema_parity['fk_present']}")
    print(f"  body_code unique:         {schema_parity['body_code_unique']}")
    print()
    print(f"  content_digest: {evidence['content_digest']}")
    print(f"  plan_digest:    {plan['plan_digest']}")
    print(f"  evidence: {ev_path}")
    print(f"  plan:     {pl_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
