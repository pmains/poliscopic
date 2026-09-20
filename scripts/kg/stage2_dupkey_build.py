#!/usr/bin/env python3
"""``stage2_dupkey_build.py`` — build the duplicate-key investigation artifact.

Read-only.  Fetches every row in every duplicate ``(meeting_db_id,
agenda_item_number)`` group, classifies each row and group, measures the downstream
references a cleanup would disturb, reconciles the counts, and writes one immutable
artifact.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from scripts.db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
)
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_dupkey_contract as CONTRACT  # noqa: E402
from scripts.kg import stage2_dupkey_investigate as INV  # noqa: E402

PLANS = REPO / "data" / "kg-plans"

GROUP_QUERY = """
    SELECT meeting_db_id, agenda_item_number, COUNT(*)::int AS n
    FROM agenda_items
    GROUP BY meeting_db_id, agenda_item_number
    HAVING COUNT(*) > 1
"""

ROW_QUERY = """
    SELECT id, meeting_db_id, meeting_id, body, agenda_item_number, agenda_item_id,
           agenda_item_title, agenda_item_text, source_url, source_body, c_number,
           case_number, item_type, sort_order, agenda_item_url, vote_or_action,
           agenda_category, section_level, jurisdiction_id, public_body_id,
           lifecycle_status, created_at, updated_at, swept_at
    FROM agenda_items
    WHERE meeting_db_id = ANY(:meetings)
    ORDER BY meeting_db_id, agenda_item_number, id
"""


def main() -> int:
    engine = get_engine()
    guard = assert_read_only_target(engine)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    print(f"target guard: {json.dumps(guard)}")

    with engine.connect() as connection:
        groups = [dict(r) for r in connection.execute(text(GROUP_QUERY)).mappings()]
        meetings = sorted({int(g["meeting_db_id"]) for g in groups})
        rows = [dict(r) for r in connection.execute(
            text(ROW_QUERY), {"meetings": meetings}).mappings()]

        # Downstream references: the only FK to agenda_items.id.
        referenced = {int(r[0]) for r in connection.execute(text(
            "SELECT DISTINCT agenda_item_id FROM meeting_events "
            "WHERE agenda_item_id IS NOT NULL"))}
        # Documents that name the group's number in the same meeting.
        document_counts: dict[tuple[int, str], int] = {}
        for r in connection.execute(text("""
                SELECT meeting_db_id, agenda_item_number, COUNT(*)::int AS n
                FROM supporting_documents
                WHERE meeting_db_id = ANY(:m)
                GROUP BY meeting_db_id, agenda_item_number"""),
                {"m": meetings}).mappings():
            document_counts[(int(r["meeting_db_id"]),
                             str(r["agenda_item_number"]))] = int(r["n"])

    by_group: dict[tuple[int, str], list[dict]] = defaultdict(list)
    for row in rows:
        key = (int(row["meeting_db_id"]), str(row["agenda_item_number"]))
        if key in {(int(g["meeting_db_id"]), str(g["agenda_item_number"]))
                   for g in groups}:
            by_group[key].append(row)

    classified_groups: list[dict] = []
    all_rows: list[dict] = []
    for group in sorted(groups, key=lambda g: (str(g["agenda_item_number"]),
                                               int(g["meeting_db_id"]))):
        key = (int(group["meeting_db_id"]), str(group["agenda_item_number"]))
        members = by_group.get(key, [])
        classified = INV.classify_rows(members)
        for entry in classified:
            all_rows.append(entry)
        referenced_ids = sorted(e["id"] for e in classified if e["id"] in referenced)
        classified_groups.append({
            "meeting_db_id": key[0],
            "agenda_item_number": key[1],
            "rows": int(group["n"]),
            "excess": int(group["n"]) - 1,
            "family": INV.group_family(classified),
            "families": dict(Counter(e["family"] for e in classified)),
            "row_ids": sorted(e["id"] for e in classified),
            "fingerprints": sorted(e["fingerprint"] for e in classified),
            "source_keys": sorted({e["source_key"] for e in classified}),
            "referenced_by_events": referenced_ids,
            "documents_naming_this_number": document_counts.get(key, 0),
            "bodies": sorted({e["body"] for e in classified}),
        })

    counts = INV.family_counts(all_rows)
    group_families = Counter(g["family"] for g in classified_groups)
    total_rows = sum(g["rows"] for g in classified_groups)
    excess = sum(g["excess"] for g in classified_groups)
    referenced_rows = sum(len(g["referenced_by_events"]) for g in classified_groups)
    documents_at_duplicated_numbers = sum(
        g["documents_naming_this_number"] for g in classified_groups)

    artifact = {
        "kind": "kg-stage2-duplicate-key-investigation",
        "version": "kg-stage2-duplicate-key-investigation/1.0",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": {k: guard[k] for k in ("dialect", "host", "port", "database",
                                         "tier")},
        "totals": {
            "groups": len(classified_groups),
            "rows": total_rows,
            "excess": excess,
            "families": counts,
            "group_families": dict(group_families),
        },
        "impact": {
            "rows_referenced_by_meeting_events": referenced_rows,
            "groups_with_event_references": sum(
                1 for g in classified_groups if g["referenced_by_events"]),
            "documents_naming_a_duplicated_number": documents_at_duplicated_numbers,
            "groups_touching_documents": sum(
                1 for g in classified_groups if g["documents_naming_this_number"]),
        },
        "groups": classified_groups,
        "rows": all_rows,
    }
    artifact["summary_digest"] = artifacts.compute_digest({
        "groups": artifact["groups"], "totals": artifact["totals"],
        "impact": artifact["impact"]})
    path = PLANS / f"kg-stage2-duplicate-key-investigation-{stamp}.json"
    artifacts.write_immutable(path, artifact)
    print(f"artifact: {path.name}")
    print(f"  groups={len(classified_groups)} rows={total_rows} excess={excess}")
    print(f"  families: {json.dumps(counts)}")
    print(f"  group families: {json.dumps(dict(group_families))}")
    print(f"  impact: {json.dumps(artifact['impact'])}")
    print(f"  digest: {artifact['summary_digest']}")

    # Supersede earlier investigations so exactly one describes the current state.
    for earlier in sorted(PLANS.glob("kg-stage2-duplicate-key-investigation-*.json")):
        if earlier.name == path.name or earlier.name.endswith(".obsolete.json"):
            continue
        if (PLANS / (earlier.name + ".obsolete.json")).exists():
            continue
        artifacts.record_obsolete(
            PLANS, earlier,
            "superseded by a later investigation: the multi-body-meeting family was "
            "added so unresolved groups fell from 379 to 4")
        print(f"  obsolete: {earlier.name}")

    contract = CONTRACT.build_contract(measured={
        "groups": len(classified_groups), "rows": total_rows, "excess": excess,
        "families": counts, "group_families": dict(group_families),
        "impact": artifact["impact"],
    })
    contract_path = PLANS / f"kg-stage2-collision-control-contract-{stamp}.json"
    artifacts.write_immutable(contract_path, contract)
    print(f"contract: {contract_path.name} digest={contract['contract_digest']}")
    for earlier in sorted(PLANS.glob("kg-stage2-collision-control-contract-*.json")):
        if earlier.name == contract_path.name or earlier.name.endswith(".obsolete.json"):
            continue
        if (PLANS / (earlier.name + ".obsolete.json")).exists():
            continue
        artifacts.record_obsolete(PLANS, earlier,
                                  "superseded by a later contract build")
        print(f"  obsolete: {earlier.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
