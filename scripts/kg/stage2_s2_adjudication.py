#!/usr/bin/env python3
"""``stage2_s2_adjudication.py`` — grouped packet for the genuine ambiguities.

A document is *genuinely* ambiguous when its ``agenda_item_number`` matches more
than one canonical item **and** its own source key names none of them.  Nothing
stored on the row separates the candidates, so no rule may pick one: the only
honest output is an enumerated decision packet for a human.

This module writes that packet and nothing else.  It never proposes a link, and
``decisions`` is emitted empty by construction.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_classify as classify_mod  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402

__all__ = [
    "PACKET_KIND",
    "PACKET_VERSION",
    "build_packet",
    "group_ambiguities",
]

PACKET_KIND = "kg-stage2-s2-adjudication"
PACKET_VERSION = "kg-stage2-s2-adjudication/1.0"

_ITEMS_SQL = text(
    "SELECT ai.id AS agenda_item_db_id, ai.agenda_item_id AS source_key, "
    "       ai.agenda_item_number AS item_number, ai.section_level, "
    "       ai.sort_order, ai.item_type, ai.agenda_item_title, ai.agenda_item_url "
    "FROM agenda_items ai "
    "WHERE ai.meeting_db_id = :meeting AND ai.agenda_item_number = :number "
    "ORDER BY ai.sort_order NULLS LAST, ai.id"
)


def document_groups(connection: Any, holds: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Group the held ambiguities by the thing a human actually decides.

    Documents that share a meeting and an item number face the *same* candidate
    set, so they are one decision, not many.
    """
    rows: list[dict[str, Any]] = []
    statement = text(
        "SELECT id, meeting_db_id, agenda_item_number, agenda_item_id, body, "
        "       document_url, document_title, document_type "
        "FROM supporting_documents WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    ids = [int(h["document_id"]) for h in holds]
    found: dict[int, Any] = {}
    for start in range(0, len(ids), 2000):
        for row in connection.execute(statement, {"ids": ids[start:start + 2000]}).mappings():
            found[int(row["id"])] = row
    fingerprint = {int(h["document_id"]): h["document_fingerprint"] for h in holds}

    groups: dict[tuple, dict[str, Any]] = {}
    for document_id in sorted(found):
        row = found[document_id]
        key = (str(row["body"]), int(row["meeting_db_id"]), str(row["agenda_item_number"]))
        group = groups.setdefault(key, {
            "group_key": {
                "body": key[0],
                "meeting_db_id": key[1],
                "agenda_item_number": key[2],
            },
            "documents": [],
            "candidates": [],
            "candidate_count": 0,
            "decision": None,
            "status": "awaiting_human_decision",
        })
        group["documents"].append({
            "document_id": document_id,
            "document_fingerprint": fingerprint[document_id],
            "source_key": row["agenda_item_id"],
            "document_type": row["document_type"],
            "document_title": (row["document_title"] or "")[:120],
        })
        if not group["candidates"]:
            candidates = list(connection.execute(
                _ITEMS_SQL, {"meeting": key[1], "number": key[2]}
            ).mappings())
            group["candidates"] = [
                {
                    "agenda_item_db_id": int(c["agenda_item_db_id"]),
                    "source_key": c["source_key"],
                    "section_level": c["section_level"],
                    "sort_order": c["sort_order"],
                    "item_type": c["item_type"],
                    "title": (c["agenda_item_title"] or "")[:120],
                    "url": c["agenda_item_url"],
                }
                for c in candidates
            ]
            group["candidate_count"] = len(group["candidates"])
    for group in groups.values():
        group["documents"].sort(key=lambda d: d["document_id"])
    rows = sorted(groups.values(), key=lambda g: (
        -len(g["documents"]), g["group_key"]["body"], g["group_key"]["meeting_db_id"],
        g["group_key"]["agenda_item_number"],
    ))
    return rows


def build_packet(
    plan: Mapping[str, Any],
    plan_path: str,
    groups: Sequence[Mapping[str, Any]],
    *,
    packet_id: str,
    created_at: str,
) -> dict[str, Any]:
    """Assemble the immutable decision packet."""
    ambiguities = [h for h in plan.get("holds", []) if h.get("class") == "held_ambiguous"]
    documents_in_groups = sum(len(g["documents"]) for g in groups)
    problems: list[str] = []
    if documents_in_groups != len(ambiguities):
        problems.append(
            f"packet groups {documents_in_groups} documents but the plan holds {len(ambiguities)}"
        )
    for group in groups:
        if group["decision"] is not None:
            problems.append(f"group {group['group_key']} arrived with a decision")
        if group["candidate_count"] < 2:
            problems.append(
                f"group {group['group_key']} has {group['candidate_count']} candidates, "
                "which is not an ambiguity"
            )
    return {
        "kind": PACKET_KIND,
        "algorithm_version": PACKET_VERSION,
        "packet_id": packet_id,
        "created_at": created_at,
        "plan": {
            "path": str(plan_path),
            "plan_id": plan.get("plan_id"),
            "digest": artifacts.compute_digest(plan),
            "kind": plan.get("kind"),
        },
        "target": dict(plan.get("target") or {}),
        "code_hashes": dict(
            documents.code_hashes(),
            **{  # the packet binds the code that produced it, too
                "scripts/kg/stage2_s2_adjudication.py": hashlib.sha256(
                    Path(__file__).read_bytes()).hexdigest()
            },
        ),
        "counts": {
            "ambiguous_documents": len(ambiguities),
            "groups": len(groups),
            "bodies": len({g["group_key"]["body"] for g in groups}),
            "meetings": len({g["group_key"]["meeting_db_id"] for g in groups}),
            "candidates_in_total": sum(g["candidate_count"] for g in groups),
        },
        "reconciliation": {"problems": problems},
        "decision_contract": {
            "required": [
                "a chosen agenda_item_db_id that appears in the group's candidates",
                "or an explicit hold with a reason",
            ],
            "forbidden": [
                "choosing a candidate that is not in the group",
                "choosing by candidate order, title similarity, or sort_order alone",
            ],
            "note": "this packet proposes no link; every decision is a human decision",
        },
        "decisions": [],
        "groups": list(groups),
    }


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    parser.add_argument("--out-dir", default="data/kg-plans")
    args = parser.parse_args(argv)

    from db.core import get_engine
    from scripts.entities.event_normalize_preflight import guard_engine
    from scripts.kg.phoenix_dr_adjudication import assert_development_target

    plan = artifacts.load_verified(args.plan)
    engine = get_engine()
    guard_engine(engine)
    assert_development_target(engine)

    moment = datetime.now(timezone.utc)
    packet_id, created_at = documents.plan_identity(moment, None)
    with engine.connect() as connection:
        groups = document_groups(
            connection, [h for h in plan["holds"] if h.get("class") == "held_ambiguous"])
    packet = build_packet(plan, args.plan, groups, packet_id=packet_id, created_at=created_at)
    path = Path(args.out_dir) / f"kg-stage2-s2-adjudication-{packet_id}.json"
    digest = artifacts.write_immutable(path, packet)
    print(json.dumps({
        "packet": str(path),
        "digest": digest,
        "counts": packet["counts"],
        "problems": packet["reconciliation"]["problems"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
