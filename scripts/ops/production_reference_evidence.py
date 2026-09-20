#!/usr/bin/env python3
"""Bounded, read-only row evidence for production reference debt.

This is a supervised follow-up to a verified G5 preflight artifact.  It has no
mutation or apply path: one PostgreSQL REPEATABLE READ, READ ONLY transaction
captures exact totals and bounded, row-identifying samples for the six reference
categories named by the G5 registry metrics.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
for _candidate in (REPO, REPO / "scripts", Path(__file__).resolve().parent):
    if str(_candidate) not in sys.path:
        sys.path.insert(0, str(_candidate))

import production_interlock  # noqa: E402
import production_preflight as preflight  # noqa: E402

SCHEMA = "production-reference-evidence/1"
DEFAULT_OUT_DIR = REPO / "data" / "audit"
DEFAULT_ROW_LIMIT = 10_000
MAX_ROW_LIMIT = 20_000


class Refused(RuntimeError):
    """Fail-closed evidence refusal."""


@dataclass(frozen=True)
class Category:
    name: str
    metric_keys: tuple[str, ...]
    reason: str
    action_class: str
    from_where: str
    select: str


# Candidate cardinality is deliberately computed in SQL, not inferred by this
# tool.  Upstream identities name the exact rows through which each candidate is
# reachable.  The labels classify review work; they are not repair proposals.
CATEGORIES = (
    Category(
        "meetings.body.sentinel", ("meetings.body.sentinel",),
        "body_code_is_null_blank_or_skip", "identity_review",
        """meetings m
        WHERE m.body IS NULL OR BTRIM(m.body) = '' OR m.body = '__skip__'""",
        """m.id, m.body, m.meeting_id, m.meeting_date, m.jurisdiction_id,
        m.public_body_id,
        jsonb_build_object('meeting_db_id', m.id, 'meeting_id', m.meeting_id,
                           'jurisdiction_id', m.jurisdiction_id) AS upstream_identity,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', pb.id, 'body_code', pb.body_code, 'name', pb.name,
                    'jurisdiction_id', pb.jurisdiction_id) ORDER BY pb.id)
                  FROM public_bodies pb WHERE pb.id = m.public_body_id), '[]'::jsonb)
            AS candidate_parents,
        (SELECT COUNT(*) FROM public_bodies pb WHERE pb.id = m.public_body_id)
            AS candidate_parent_count""",
    ),
    Category(
        "member_votes.body.sentinel", ("member_votes.body.sentinel",),
        "body_code_is_null_blank_or_skip", "upstream_identity_review",
        """member_votes mv
        LEFT JOIN agenda_item_votes aiv ON aiv.id = mv.agenda_item_vote_id
        WHERE mv.body IS NULL OR BTRIM(mv.body) = '' OR mv.body = '__skip__'""",
        """mv.id, mv.body, mv.agenda_item_vote_id, mv.member_id, mv.vote,
        jsonb_build_object('agenda_item_vote_id', aiv.id, 'body', aiv.body,
                           'agenda_item_id', aiv.agenda_item_id,
                           'meeting_db_id', aiv.meeting_db_id,
                           'meeting_id', aiv.meeting_id) AS upstream_identity,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', pb.id, 'body_code', pb.body_code, 'name', pb.name,
                    'jurisdiction_id', pb.jurisdiction_id) ORDER BY pb.id)
                  FROM public_bodies pb WHERE pb.body_code = aiv.body), '[]'::jsonb)
            AS candidate_parents,
        (SELECT COUNT(*) FROM public_bodies pb WHERE pb.body_code = aiv.body)
            AS candidate_parent_count""",
    ),
    Category(
        "agenda_items.body.dangling", ("agenda_items.body.dangling",),
        "body_code_has_no_registry_parent", "upstream_identity_review",
        """agenda_items ai
        LEFT JOIN meetings m ON m.id = ai.meeting_db_id
        WHERE ai.body IS NOT NULL AND BTRIM(ai.body) <> '' AND ai.body <> '__skip__'
          AND NOT EXISTS (SELECT 1 FROM public_bodies x WHERE x.body_code = ai.body)""",
        """ai.id, ai.body, ai.public_body_id, ai.agenda_item_id,
        ai.agenda_item_number, ai.meeting_db_id, ai.meeting_id, ai.jurisdiction_id,
        jsonb_build_object('meeting_db_id', m.id, 'meeting_id', m.meeting_id,
                           'body', m.body, 'public_body_id', m.public_body_id,
                           'jurisdiction_id', m.jurisdiction_id) AS upstream_identity,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', c.id, 'body_code', c.body_code, 'name', c.name,
                    'jurisdiction_id', c.jurisdiction_id) ORDER BY c.id)
                  FROM public_bodies c
                  WHERE c.id = ai.public_body_id OR c.id = m.public_body_id
                     OR c.body_code = m.body), '[]'::jsonb) AS candidate_parents,
        (SELECT COUNT(DISTINCT c.id) FROM public_bodies c
                  WHERE c.id = ai.public_body_id OR c.id = m.public_body_id
                     OR c.body_code = m.body) AS candidate_parent_count""",
    ),
    Category(
        "supporting_documents.body.dangling",
        ("supporting_documents.body.dangling",),
        "body_code_has_no_registry_parent", "upstream_identity_review",
        """supporting_documents sd
        LEFT JOIN meetings m ON m.id = sd.meeting_db_id
        WHERE sd.body IS NOT NULL AND BTRIM(sd.body) <> '' AND sd.body <> '__skip__'
          AND NOT EXISTS (SELECT 1 FROM public_bodies x WHERE x.body_code = sd.body)""",
        """sd.id, sd.body, sd.agenda_item_id, sd.agenda_item_number,
        sd.meeting_db_id, sd.meeting_id, sd.jurisdiction_id,
        jsonb_build_object(
          'meeting', jsonb_build_object('meeting_db_id', m.id,
                    'meeting_id', m.meeting_id, 'body', m.body,
                    'public_body_id', m.public_body_id,
                    'jurisdiction_id', m.jurisdiction_id),
          'agenda_items', COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', ai.id, 'agenda_item_id', ai.agenda_item_id,
                    'body', ai.body, 'public_body_id', ai.public_body_id,
                    'jurisdiction_id', ai.jurisdiction_id) ORDER BY ai.id)
             FROM agenda_items ai WHERE ai.meeting_db_id = sd.meeting_db_id
               AND ai.agenda_item_id = sd.agenda_item_id), '[]'::jsonb))
          AS upstream_identity,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', c.id, 'body_code', c.body_code, 'name', c.name,
                    'jurisdiction_id', c.jurisdiction_id) ORDER BY c.id)
          FROM public_bodies c WHERE c.id = m.public_body_id OR c.body_code = m.body
             OR c.id IN (SELECT ai.public_body_id FROM agenda_items ai
                          WHERE ai.meeting_db_id = sd.meeting_db_id
                            AND ai.agenda_item_id = sd.agenda_item_id)
             OR c.body_code IN (SELECT ai.body FROM agenda_items ai
                          WHERE ai.meeting_db_id = sd.meeting_db_id
                            AND ai.agenda_item_id = sd.agenda_item_id)), '[]'::jsonb)
          AS candidate_parents,
        (SELECT COUNT(DISTINCT c.id) FROM public_bodies c
          WHERE c.id = m.public_body_id OR c.body_code = m.body
             OR c.id IN (SELECT ai.public_body_id FROM agenda_items ai
                          WHERE ai.meeting_db_id = sd.meeting_db_id
                            AND ai.agenda_item_id = sd.agenda_item_id)
             OR c.body_code IN (SELECT ai.body FROM agenda_items ai
                          WHERE ai.meeting_db_id = sd.meeting_db_id
                            AND ai.agenda_item_id = sd.agenda_item_id))
          AS candidate_parent_count""",
    ),
    Category(
        "meetings.public_body_id.dangling_or_null",
        ("meetings.public_body_id.dangling", "meetings.public_body_id.null"),
        "public_body_id_is_null_or_has_no_registry_parent", "identity_review",
        """meetings m
        WHERE m.public_body_id IS NULL OR NOT EXISTS
          (SELECT 1 FROM public_bodies x WHERE x.id = m.public_body_id)""",
        """m.id, m.body, m.public_body_id, m.meeting_id, m.meeting_date,
        m.jurisdiction_id,
        jsonb_build_object('meeting_db_id', m.id, 'meeting_id', m.meeting_id,
                           'body', m.body, 'jurisdiction_id', m.jurisdiction_id)
          AS upstream_identity,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', pb.id, 'body_code', pb.body_code, 'name', pb.name,
                    'jurisdiction_id', pb.jurisdiction_id) ORDER BY pb.id)
                  FROM public_bodies pb WHERE pb.body_code = m.body), '[]'::jsonb)
          AS candidate_parents,
        (SELECT COUNT(*) FROM public_bodies pb WHERE pb.body_code = m.body)
          AS candidate_parent_count""",
    ),
    Category(
        "agenda_items.public_body_id.dangling_or_null",
        ("agenda_items.public_body_id.dangling", "agenda_items.public_body_id.null"),
        "public_body_id_is_null_or_has_no_registry_parent",
        "upstream_identity_review",
        """agenda_items ai
        LEFT JOIN meetings m ON m.id = ai.meeting_db_id
        WHERE ai.public_body_id IS NULL OR NOT EXISTS
          (SELECT 1 FROM public_bodies x WHERE x.id = ai.public_body_id)""",
        """ai.id, ai.body, ai.public_body_id, ai.agenda_item_id,
        ai.agenda_item_number, ai.meeting_db_id, ai.meeting_id, ai.jurisdiction_id,
        jsonb_build_object('meeting_db_id', m.id, 'meeting_id', m.meeting_id,
                           'body', m.body, 'public_body_id', m.public_body_id,
                           'jurisdiction_id', m.jurisdiction_id) AS upstream_identity,
        COALESCE((SELECT jsonb_agg(jsonb_build_object(
                    'id', c.id, 'body_code', c.body_code, 'name', c.name,
                    'jurisdiction_id', c.jurisdiction_id) ORDER BY c.id)
          FROM public_bodies c WHERE c.body_code = ai.body
             OR c.id = m.public_body_id OR c.body_code = m.body), '[]'::jsonb)
          AS candidate_parents,
        (SELECT COUNT(DISTINCT c.id) FROM public_bodies c
          WHERE c.body_code = ai.body OR c.id = m.public_body_id
             OR c.body_code = m.body) AS candidate_parent_count""",
    ),
)


def load_g5(path: Path) -> tuple[dict[str, Any], str]:
    """Load and canonically verify the required G5 artifact."""
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Refused("G5 artifact could not be read as JSON") from exc
    if not isinstance(artifact, dict):
        raise Refused("G5 artifact is not an object")
    recorded = artifact.get("digest")
    body = {key: value for key, value in artifact.items() if key != "digest"}
    actual = preflight.digest(body)
    if not isinstance(recorded, str) or recorded != actual:
        raise Refused("G5 artifact digest mismatch")
    if (artifact.get("schema") != preflight.SCHEMA or
            artifact.get("operation") != "OP-PREFLIGHT" or
            artifact.get("status") != "VALID"):
        raise Refused("G5 artifact contract or status is invalid")
    from scripts.body_code_merge_runtime import PRODUCTION_TARGET
    expected = {"database": PRODUCTION_TARGET["database"],
                "host": PRODUCTION_TARGET["host"]}
    target = artifact.get("target") or {}
    if artifact.get("pinned_target") != expected:
        raise Refused("G5 artifact is not bound to the pinned production target")
    if (target.get("database") != expected["database"] or
            target.get("configured_host") != expected["host"] or
            not target.get("cluster_system_identifier")):
        raise Refused("G5 target or cluster identity is invalid")
    transaction = artifact.get("transaction") or {}
    if (transaction.get("connection_count") != 1 or
            transaction.get("transaction_count") != 1 or
            str(transaction.get("isolation", "")).lower() != "repeatable read" or
            transaction.get("read_only") is not True):
        raise Refused("G5 artifact does not prove its transaction contract")
    return artifact, actual


def _scalar(connection: Any, statement: str, params: Mapping[str, Any] | None = None) -> Any:
    from sqlalchemy import text
    return connection.execute(text(statement), dict(params or {})).scalar_one()


def verify_live_identity(connection: Any, *, configured_host: str,
                         configured_port: int | None,
                         g5: Mapping[str, Any]) -> dict[str, Any]:
    """Prove transaction mode and equality with G5's production cluster."""
    identity = {
        "database": str(_scalar(connection, "SELECT current_database()")),
        "configured_host": configured_host,
        "configured_port": configured_port,
        "server_address": str(_scalar(connection, "SELECT inet_server_addr()")),
        "server_port": int(_scalar(connection, "SELECT inet_server_port()")),
        "cluster_system_identifier": str(_scalar(
            connection, "SELECT system_identifier FROM pg_control_system()")),
    }
    isolation = str(_scalar(connection, "SHOW transaction_isolation")).lower()
    read_only = str(_scalar(connection, "SHOW transaction_read_only")).lower()
    if isolation != "repeatable read" or read_only not in ("on", "true"):
        raise Refused("database did not prove one REPEATABLE READ, READ ONLY transaction")
    g5_target = g5["target"]
    for field in ("database", "configured_host", "configured_port",
                  "server_address", "server_port", "cluster_system_identifier"):
        if identity[field] != g5_target.get(field):
            raise Refused(f"live production identity differs from G5 field {field}")
    return identity


def collect(connection: Any, *, row_limit: int,
            g5: Mapping[str, Any]) -> dict[str, Any]:
    """Collect exact totals and bounded samples through the caller's connection."""
    from sqlalchemy import text
    registry = (g5.get("integrity") or {}).get("registry_metrics") or {}
    evidence: dict[str, Any] = {}
    for category in CATEGORIES:
        count_sql = f"/* category:{category.name}:count */ SELECT COUNT(*) FROM {category.from_where}"
        total = int(connection.execute(text(count_sql)).scalar_one() or 0)
        expected_total = sum(int(registry.get(key, 0)) for key in category.metric_keys)
        if total != expected_total:
            raise Refused(
                f"current total for {category.name} differs from bound G5 metrics")
        rows_sql = (f"/* category:{category.name}:rows */ SELECT {category.select} "
                    f"FROM {category.from_where} ORDER BY 1 LIMIT :row_limit")
        rows = [dict(row) for row in connection.execute(
            text(rows_sql), {"row_limit": row_limit}).mappings()]
        evidence[category.name] = {
            "reason": category.reason,
            "action_class": category.action_class,
            "total": total,
            "row_limit": row_limit,
            "sample_count": len(rows),
            "truncated": total > len(rows),
            "rows": rows,
        }
    return evidence


def run(*, g5_artifact: Path, output: Path, row_limit: int = DEFAULT_ROW_LIMIT,
        captured_at: str | None = None) -> dict[str, Any]:
    """Run the interlocked evidence capture and create one immutable artifact."""
    verdict = production_interlock.check(
        "OP-PREFLIGHT", entry_point="scripts/ops/production_reference_evidence.py")
    if verdict.get("status") != "ALLOWED":
        raise Refused(f"production interlock refused: {verdict.get('code')}")
    if not 1 <= row_limit <= MAX_ROW_LIMIT:
        raise Refused(f"row limit must be between 1 and {MAX_ROW_LIMIT}")
    g5, g5_digest = load_g5(g5_artifact)

    from sqlalchemy import create_engine, text
    try:
        validated_url = preflight.resolve_production_url(REPO / ".env")
        engine = create_engine(validated_url, future=True, pool_pre_ping=True)
    except Refused:
        raise
    except Exception as exc:
        raise Refused("production engine creation failed") from exc

    timestamp = captured_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with engine.connect().execution_options(
                isolation_level="REPEATABLE READ") as connection:
            with connection.begin():
                connection.execute(text("SET TRANSACTION READ ONLY"))
                identity = verify_live_identity(
                    connection, configured_host=str(engine.url.host or ""),
                    configured_port=engine.url.port, g5=g5)
                categories = collect(connection, row_limit=row_limit, g5=g5)
    except Refused:
        raise
    except Exception as exc:
        raise Refused("production evidence query failed") from exc
    finally:
        engine.dispose()

    body = {
        "schema": SCHEMA,
        "captured_at": timestamp,
        "operation": "OP-PREFLIGHT",
        "status": "VALID",
        "mode": "read-only-evidence-only",
        "g5_binding": {"path": str(g5_artifact), "digest": g5_digest},
        "target": identity,
        "transaction": {"connection_count": 1, "transaction_count": 1,
                        "isolation": "repeatable read", "read_only": True},
        "categories": categories,
    }
    artifact = {**body, "digest": preflight.digest(body)}
    try:
        preflight.write_exclusive(output, artifact)
    except Exception as exc:
        raise Refused(f"evidence artifact creation failed: {exc}") from exc
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--g5-artifact", required=True, type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--row-limit", type=int, default=DEFAULT_ROW_LIMIT)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc)
    output = args.output or DEFAULT_OUT_DIR / (
        now.strftime("%Y%m%dT%H%M%SZ") + "-production-reference-evidence.json")
    try:
        result = run(g5_artifact=args.g5_artifact, output=output,
                     row_limit=args.row_limit,
                     captured_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": "VALID", "path": str(output),
                      "digest": result["digest"],
                      "g5_digest": result["g5_binding"]["digest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
