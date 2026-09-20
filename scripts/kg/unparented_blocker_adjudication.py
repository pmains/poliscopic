#!/usr/bin/env python3
"""``unparented_blocker_adjudication.py`` — read-only adjudication of the 392.

Every extraction the event-normalize gate cannot process is blocked because its
meeting has no ``public_body_id``.  This module answers, from stored evidence
only:

* how the 392 group by source system and stored body code;
* whether each group's body is uniquely identifiable, ambiguous, unmatched, or
  structurally exceptional;
* which candidate ``public_bodies`` rows exist, and which body codes are
  registered in the scraper's own body tables (code-level evidence);
* how the 1,470 non-``phoenix-dr`` unparented meetings split into those relevant
  to the 392 and those that are unrelated historical rows.

Reuse, not duplication: the read-only guard and target refusal come from
:mod:`scripts.entities.event_normalize_preflight`, and target classification from
:mod:`db.tier`.  Nothing here writes to the database and every statement is a
``SELECT`` inside a read-only session.

Classification is conservative: naming alone never decides.  A group is
``uniquely_repairable`` only when a stored body code resolves to exactly one
candidate body, or when the code is registered in the scraper body tables with
no conflicting database row.  Otherwise the group is ``ambiguous``, ``unmatched``,
or ``structurally_exceptional``, and remains unapplied.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import text  # noqa: E402

from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
    guard_engine,
    statement_audit,
)

__all__ = [
    "CLASSIFICATIONS",
    "GROUP_SQL",
    "UNPARENTED_INVENTORY_SQL",
    "classify_group",
    "group_blocked",
    "inventory_unparented",
    "registry_codes",
    "run_adjudication",
]

#: Allowed classifications.  Anything ambiguous stays unapplied.
CLASSIFICATIONS = (
    "uniquely_repairable",
    "ambiguous",
    "unmatched",
    "structurally_exceptional",
)

#: Body codes that are sentinels rather than real bodies.
SENTINEL_CODES = ("", "__skip__", "skip", "none", "null")

#: A plausible scraper body code literal.
_CODE_LITERAL = re.compile(r"[\"']([a-z0-9][a-z0-9-]{2,})[\"']")

GROUP_SQL = """
SELECT e.id            AS extraction_id,
       e.action_verb   AS action_verb,
       sd.id           AS document_id,
       sd.body         AS document_body,
       sd.document_title AS document_title,
       sd.jurisdiction_id AS document_jurisdiction_id,
       m.id            AS meeting_id,
       m.meeting_id    AS meeting_source_id,
       m.meeting_title AS meeting_title,
       m.meeting_date  AS meeting_date,
       m.body          AS meeting_body,
       m.source_system AS source_system,
       m.public_body_id AS public_body_id,
       m.jurisdiction_id AS meeting_jurisdiction_id
FROM meeting_event_extractions e
JOIN supporting_documents sd ON sd.id = e.supporting_doc_id
JOIN meetings m ON m.id = sd.meeting_db_id
WHERE m.public_body_id IS NULL
ORDER BY source_system NULLS FIRST, meeting_body, m.id, e.id
"""

UNPARENTED_INVENTORY_SQL = """
SELECT COUNT(*)                                                    AS unparented_total,
       COUNT(*) FILTER (WHERE has_extraction)                      AS relevant_to_blockers,
       COUNT(*) FILTER (WHERE NOT has_extraction)                  AS unrelated_historical
FROM (
    SELECT m.id AS meeting_id,
           EXISTS (SELECT 1 FROM meeting_event_extractions e
                   JOIN supporting_documents sd ON sd.id = e.supporting_doc_id
                   WHERE sd.meeting_db_id = m.id) AS has_extraction
    FROM meetings m WHERE m.public_body_id IS NULL
) AS sub
"""


def registry_codes(scripts_dir: Path | None = None) -> dict[str, list[str]]:
    """Body-code literals declared by the scraper's own jurisdiction tables.

    Code-level evidence: a code that appears as a string literal in the scraper
    registry names a real body even when no ``public_bodies`` row exists yet.
    """
    root = (scripts_dir or _SCRIPTS_DIR) / "scraper" / "jurisdictions"
    found: dict[str, set[str]] = defaultdict(set)
    if not root.is_dir():
        return {}
    for path in sorted(root.glob("*.py")):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for literal in _CODE_LITERAL.findall(source):
            found[literal].add(path.name)
    return {code: sorted(files) for code, files in sorted(found.items())}


@dataclass
class BlockedGroup:
    """One (source system, body code) group of blocked extractions."""

    source_system: str
    body_code: str
    meeting_ids: list[int] = field(default_factory=list)
    extraction_ids: list[int] = field(default_factory=list)
    meeting_labels: list[str] = field(default_factory=list)
    body_labels: list[str] = field(default_factory=list)
    titles: list[str] = field(default_factory=list)
    registry_files: list[str] = field(default_factory=list)
    candidates: list[dict[str, Any]] = field(default_factory=list)
    classification: str = "unmatched"
    rationale: str = ""
    recommended_action: str = ""

    @property
    def meetings(self) -> int:
        return len(self.meeting_ids)

    @property
    def extractions(self) -> int:
        return len(self.extraction_ids)

    def as_dict(self) -> dict[str, Any]:
        return {
            "source_system": self.source_system,
            "body_code": self.body_code,
            "classification": self.classification,
            "rationale": self.rationale,
            "recommended_action": self.recommended_action,
            "meeting_count": self.meetings,
            "extraction_count": self.extractions,
            "meeting_ids": self.meeting_ids,
            "extraction_ids": self.extraction_ids,
            "meeting_labels": self.meeting_labels,
            "body_labels": self.body_labels,
            "title_samples": self.titles[:5],
            "registry_evidence": self.registry_files,
            "candidate_public_bodies": self.candidates,
        }


def classify_group(
    body_code: str, registry_files: Sequence[str], candidates: Sequence[Mapping[str, Any]]
) -> tuple[str, str, str]:
    """Classify one group from stored evidence, returning (class, why, action)."""
    if body_code in SENTINEL_CODES:
        return (
            "structurally_exceptional",
            f"body code {body_code!r} is a scraper sentinel, not a body identity",
            "quarantine: correct the scraper's body assignment before any repair",
        )
    if len(candidates) == 1:
        row = candidates[0]
        return (
            "uniquely_repairable",
            f"exactly one public_bodies candidate ({row.get('id')}: {row.get('name')!r})",
            "link meetings to the existing candidate body after review",
        )
    if len(candidates) > 1:
        return (
            "ambiguous",
            f"{len(candidates)} candidate public_bodies rows match; naming is not identity",
            "quarantine: a human must choose the body",
        )
    if registry_files:
        return (
            "uniquely_repairable",
            "no public_bodies row, but the code is declared by the scraper body "
            f"registry ({', '.join(registry_files[:3])})",
            "register the canonical body, then parent the meetings",
        )
    return (
        "unmatched",
        "no public_bodies candidate and no scraper-registry evidence",
        "quarantine: body identity cannot be established from stored evidence",
    )


def _candidates(conn, body_code: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT id, name, slug, body_code, body_type, jurisdiction_id "
            "FROM public_bodies WHERE body_code = :code OR slug = :code "
            "ORDER BY id"
        ),
        {"code": body_code},
    ).mappings().all()
    return [dict(r) for r in rows]


def group_blocked(conn, *, registry: Mapping[str, Sequence[str]] | None = None) -> list[BlockedGroup]:
    """Every blocked extraction, grouped by source system and stored body code."""
    registry = registry if registry is not None else registry_codes()
    rows = conn.execute(text(GROUP_SQL)).mappings().all()

    buckets: dict[tuple[str, str], BlockedGroup] = {}
    for row in rows:
        source = row["source_system"] or "(null)"
        code = row["meeting_body"] or ""
        group = buckets.setdefault(
            (source, code),
            BlockedGroup(source_system=source, body_code=code,
                         registry_files=list(registry.get(code, []))),
        )
        if row["meeting_id"] not in group.meeting_ids:
            group.meeting_ids.append(int(row["meeting_id"]))
        group.extraction_ids.append(int(row["extraction_id"]))
        label = row["meeting_source_id"]
        if label and label not in group.meeting_labels:
            group.meeting_labels.append(str(label))
        for body_label in (row["meeting_body"], row["document_body"]):
            if body_label and body_label not in group.body_labels:
                group.body_labels.append(str(body_label))
        for title in (row["meeting_title"], row["document_title"]):
            if title and title not in group.titles:
                group.titles.append(str(title))

    for group in buckets.values():
        group.candidates = _candidates(conn, group.body_code)
        group.classification, group.rationale, group.recommended_action = classify_group(
            group.body_code, group.registry_files, group.candidates
        )
    return sorted(buckets.values(), key=lambda g: (-g.extractions, g.body_code))


def inventory_unparented(conn) -> dict[str, Any]:
    """The unparented-meeting population, split by relevance to the 392."""
    row = conn.execute(text(UNPARENTED_INVENTORY_SQL)).mappings().one()
    return {
        "unparented_total": int(row["unparented_total"]),
        "relevant_to_blockers": int(row["relevant_to_blockers"]),
        "unrelated_historical": int(row["unrelated_historical"]),
    }


def run_adjudication(
    engine: Any,
    *,
    expect_blocked: int = 392,
    expect_phoenix_extractions: int = 126,
    expect_phoenix_meetings: int = 13,
) -> dict[str, Any]:
    """Produce the complete read-only adjudication record."""
    target = assert_read_only_target(engine)
    statements = guard_engine(engine)

    with engine.connect() as conn:
        groups = group_blocked(conn)
        inventory = inventory_unparented(conn)

    audit = statement_audit(statements)
    blocked = sum(g.extractions for g in groups)
    phoenix = next((g for g in groups if g.body_code == "phoenix-dr"), None)
    by_class: dict[str, int] = {name: 0 for name in CLASSIFICATIONS}
    for group in groups:
        by_class[group.classification] += group.extractions

    record: dict[str, Any] = {
        "tool": "unparented_blocker_adjudication",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "target": target,
        "read_only": {"guard": "per-connection read-only session + statement guard",
                      "statement_audit": audit},
        "blocked_total": blocked,
        "blocked_expected": expect_blocked,
        "reconciles_to_392": blocked == expect_blocked,
        "group_count": len(groups),
        "extractions_by_classification": by_class,
        "phoenix_dr": {
            "extractions": phoenix.extractions if phoenix else 0,
            "meetings": phoenix.meetings if phoenix else 0,
            "expected_extractions": expect_phoenix_extractions,
            "expected_meetings": expect_phoenix_meetings,
            "reconciles": bool(
                phoenix
                and phoenix.extractions == expect_phoenix_extractions
                and phoenix.meetings == expect_phoenix_meetings
            ),
            "classification": phoenix.classification if phoenix else None,
            "meeting_ids": phoenix.meeting_ids if phoenix else [],
            "extraction_ids": phoenix.extraction_ids if phoenix else [],
        },
        "groups": [g.as_dict() for g in groups],
        "unparented_inventory": inventory,
        "reconciliation": {
            "phoenix_plus_others": (
                (phoenix.extractions if phoenix else 0)
                + sum(g.extractions for g in groups if g.body_code != "phoenix-dr")
            ),
            "others_explained": sum(
                g.extractions for g in groups if g.body_code != "phoenix-dr"
            ),
        },
    }
    record["reconciles_no_overlap"] = (
        record["reconciliation"]["phoenix_plus_others"] == blocked
    )
    if not audit["select_only"]:
        raise RuntimeError(f"a mutating statement was issued: {audit['mutating_statements']}")
    return record


def render_markdown(record: Mapping[str, Any]) -> str:
    """A concise human review packet."""
    lines = [
        "# Unparented-meeting blocker adjudication (read-only)",
        "",
        f"- Generated: {record['generated_at']}",
        f"- Target: `{record['target']['redacted']}`",
        f"- Blocked extractions: **{record['blocked_total']}** "
        f"(expected {record['blocked_expected']}, reconciles: {record['reconciles_to_392']})",
        f"- Groups: {record['group_count']}",
        "",
        "## Classification totals (extractions)",
        "",
    ]
    for name, count in sorted(record["extractions_by_classification"].items()):
        lines.append(f"- {name}: {count}")
    lines += ["", "## Groups", ""]
    lines.append("| source system | body code | meetings | extractions | classification |")
    lines.append("|---|---|---|---|---|")
    for group in record["groups"]:
        lines.append(
            f"| {group['source_system']} | `{group['body_code']}` | {group['meeting_count']} "
            f"| {group['extraction_count']} | {group['classification']} |"
        )
    lines += ["", "## Phoenix Design Review", ""]
    phoenix = record["phoenix_dr"]
    lines.append(
        f"- extractions **{phoenix['extractions']}** (expected {phoenix['expected_extractions']}), "
        f"meetings **{phoenix['meetings']}** (expected {phoenix['expected_meetings']}), "
        f"reconciles: {phoenix['reconciles']}"
    )
    lines.append(f"- meeting ids: {phoenix['meeting_ids']}")
    lines += ["", "## Unparented-meeting inventory", ""]
    inv = record["unparented_inventory"]
    lines.append(f"- total unparented meetings: {inv['unparented_total']}")
    lines.append(f"- relevant to the 392 blockers: {inv['relevant_to_blockers']}")
    lines.append(f"- unrelated historical rows: {inv['unrelated_historical']}")
    lines += ["", "## Rationale per group", ""]
    for group in record["groups"]:
        lines.append(f"- `{group['body_code']}`: {group['rationale']} → {group['recommended_action']}")
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only 392-blocker adjudication")
    parser.add_argument("--out-json", required=True)
    parser.add_argument("--out-md", required=True)
    args = parser.parse_args(argv)

    from db.core import get_engine

    record = run_adjudication(get_engine())
    rendered = json.dumps(record, indent=2, sort_keys=True, default=str)
    Path(args.out_json).write_text(rendered + "\n", encoding="utf-8")
    Path(args.out_md).write_text(render_markdown(record), encoding="utf-8")
    print(f"[json] {args.out_json}")
    print(f"[md]   {args.out_md}")
    print(f"sha256 {hashlib.sha256(rendered.encode()).hexdigest()}")
    print(f"blocked={record['blocked_total']} reconciles={record['reconciles_to_392']} "
          f"phoenix={record['phoenix_dr']['extractions']}/{record['phoenix_dr']['meetings']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
