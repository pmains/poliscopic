#!/usr/bin/env python3
"""``stage2_s2_verify.py`` — verification for the Step 2 attachment plan.

The plan is only reviewable if every claim in it can be re-derived.  These
checks are deliberately independent of the generator: they re-read the artifact,
recompute its arithmetic, and — when given a connection — confirm that the rows
it names still exist with the fingerprints it recorded.

Nothing here writes.  A verification failure is reported, never repaired.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402

__all__ = [
    "REQUIRED_KEYS",
    "TARGET_FIELDS",
    "normalize_target",
    "verify",
    "verify_adjudication",
    "verify_after_state",
    "verify_identity",
    "verify_agenda_items",
    "verify_ambiguity_membership",
    "verify_code_hashes",
    "verify_disjointness",
    "verify_plan_shape",
    "verify_population_arithmetic",
    "verify_rows_unchanged",
    "verify_target_binding",
]

REQUIRED_KEYS = (
    "adjudication",
    "algorithm_version",
    "attachments",
    "baseline",
    "code_hashes",
    "counts",
    "expected_after_state",
    "holds",
    "kind",
    "plan_id",
    "reconciliation",
    "schema_impact",
    "sync_parity_impact",
    "target",
    "target_column",
)

TARGET_FIELDS = ("dialect", "host", "port", "database")


def _normalize(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    return text_value.lower() if not text_value.isdigit() else int(text_value)


def normalize_target(target: Mapping[str, Any]) -> dict[str, Any]:
    """Case/whitespace-insensitive identity; blanks never equal real values."""
    return {name: _normalize(target.get(name)) for name in TARGET_FIELDS}


def verify_plan_shape(plan: Mapping[str, Any]) -> list[str]:
    """Required keys, expected kinds, and no stray linked/held overlap."""
    problems: list[str] = []
    for key in REQUIRED_KEYS:
        if key not in plan:
            problems.append(f"plan is missing required key {key!r}")
    if plan.get("kind") != documents.PLAN_KIND:
        problems.append(f"unexpected plan kind {plan.get('kind')!r}")
    if plan.get("algorithm_version") != documents.ALGORITHM_VERSION:
        problems.append(f"unexpected algorithm version {plan.get('algorithm_version')!r}")
    if plan.get("target_column") != documents.TARGET_COLUMN:
        problems.append(f"unexpected target column {plan.get('target_column')!r}")
    if not isinstance(plan.get("attachments"), list):
        problems.append("attachments must be a list")
    if not isinstance(plan.get("holds"), list):
        problems.append("holds must be a list")
    return problems


def verify_population_arithmetic(plan: Mapping[str, Any]) -> list[str]:
    """Class counts plus totals must add up exactly, with no residue."""
    problems: list[str] = []
    counts = plan.get("counts") or {}
    attachments = plan.get("attachments") or []
    holds = plan.get("holds") or []

    if (plan.get("baseline") or {}).get("counts") != counts:
        problems.append("baseline counts and counts disagree")

    named = sum(int(counts.get(name, 0)) for name in documents.CLASSES)
    total = int(counts.get("total_documents", -1))
    if named != total:
        problems.append(f"class counts {named} do not sum to total {total}")

    deterministic = sum(int(counts.get(name, 0)) for name in documents.DISJOINT_CLASSES)
    held = sum(int(counts.get(name, 0)) for name in documents.HELD_CLASSES)
    if deterministic != int(counts.get("deterministic_links", -1)):
        problems.append("deterministic_links does not match its classes")
    if held != int(counts.get("held_total", -1)):
        problems.append("held_total does not match its classes")
    if deterministic + held != total:
        problems.append("deterministic links plus holds do not cover every document")

    if len(attachments) != deterministic:
        problems.append(f"{len(attachments)} attachments but {deterministic} claimed")
    if len(holds) != held:
        problems.append(f"{len(holds)} holds but {held} claimed")
    if len(attachments) + len(holds) != total:
        problems.append("attachments plus holds do not cover every document")

    per_class = {name: 0 for name in documents.CLASSES}
    for entry in holds:
        klass = entry.get("class")
        if klass in per_class:
            per_class[klass] += 1
    for name in documents.HELD_CLASSES:
        if per_class[name] != int(counts.get(name, -1)):
            problems.append(f"hold class {name} count does not match the plan's counts")
    linked_classes = {a.get("strategy") for a in attachments}
    unexpected = linked_classes - set(documents.STRATEGIES)
    if unexpected:
        problems.append(f"attachments carry unexpected strategies {sorted(unexpected)}")
    return problems


def verify_disjointness(plan: Mapping[str, Any]) -> list[str]:
    """No document may be both linked and held, or linked twice."""
    problems: list[str] = []
    attachments = plan.get("attachments") or []
    holds = plan.get("holds") or []

    linked_ids = [int(a["document_id"]) for a in attachments]
    held_ids = [int(h["document_id"]) for h in holds]
    if len(set(linked_ids)) != len(linked_ids):
        problems.append("a document is linked more than once")
    if len(set(held_ids)) != len(held_ids):
        problems.append("a document is held more than once")
    overlap = set(linked_ids) & set(held_ids)
    if overlap:
        problems.append(f"{len(overlap)} documents are both linked and held")

    strategies: dict[int, set[str]] = {}
    for entry in attachments:
        strategies.setdefault(int(entry["document_id"]), set()).add(entry.get("strategy"))
    multi = [d for d, s in strategies.items() if len(s) > 1]
    if multi:
        problems.append(f"{len(multi)} documents carry more than one strategy")

    if int((plan.get("reconciliation") or {}).get("linked_and_held_overlap", -1)) != 0:
        problems.append("plan does not assert zero linked/held overlap")
    return problems


def verify_ambiguity_membership(plan: Mapping[str, Any]) -> list[str]:
    """Every hold must carry its class, reason, and no canonical target."""
    problems: list[str] = []
    for entry in plan.get("holds") or []:
        klass = entry.get("class")
        if klass not in documents.HELD_CLASSES:
            problems.append(f"hold {entry.get('document_id')} has class {klass!r}")
            continue
        if entry.get("reason") != documents.HOLD_REASONS[klass]:
            problems.append(f"hold {entry.get('document_id')} reason does not match {klass}")
        if "document_fingerprint" not in entry:
            problems.append(f"hold {entry.get('document_id')} has no fingerprint")
    for entry in plan.get("attachments") or []:
        if entry.get("agenda_item_db_id") is None:
            problems.append(f"attachment {entry.get('document_id')} has no target")
        if entry.get("strategy") not in documents.STRATEGIES:
            problems.append(f"attachment {entry.get('document_id')} has no strategy")
        if not entry.get("agenda_item_fingerprint"):
            problems.append(f"attachment {entry.get('document_id')} has no target fingerprint")
    return problems


def verify_adjudication(plan: Mapping[str, Any]) -> list[str]:
    """The bounded grouping must agree with the rows it summarises."""
    problems: list[str] = []
    adjudication = plan.get("adjudication") or {}
    by_strategy = adjudication.get("links_by_strategy") or {}
    by_class = adjudication.get("holds_by_class") or {}
    if sum(int(v) for v in by_strategy.values()) != len(plan.get("attachments") or []):
        problems.append("links_by_strategy does not sum to the attachments")
    if sum(int(v) for v in by_class.values()) != len(plan.get("holds") or []):
        problems.append("holds_by_class does not sum to the holds")
    if set(by_strategy) - set(documents.STRATEGIES):
        problems.append("adjudication names an unknown strategy")
    if set(by_class) - set(documents.HELD_CLASSES):
        problems.append("adjudication names an unknown hold class")
    gaps = adjudication.get("gaps_by_key_shape") or {}
    if sum(int(v) for v in gaps.values()) != int(by_class.get("gap_missing_target", -1)):
        problems.append("gaps_by_key_shape does not sum to gap_missing_target")
    for key in ("deterministic_rules", "human_decisions_required"):
        if not adjudication.get(key):
            problems.append(f"adjudication omits {key}")
    return problems


def verify_identity(plan: Mapping[str, Any]) -> list[str]:
    """A plan's id must be the identity of its own created_at."""
    return documents.identity_problems(plan)


def verify_after_state(plan: Mapping[str, Any]) -> list[str]:
    """The declared after-state must equal the plan's own arithmetic."""
    problems: list[str] = []
    after = plan.get("expected_after_state") or {}
    counts = plan.get("counts") or {}
    if int(after.get("not_null", -1)) != int(counts.get("deterministic_links", -2)):
        problems.append("expected not-null count != deterministic links")
    if int(after.get("null", -1)) != int(counts.get("held_total", -2)):
        problems.append("expected null count != held total")
    if int(after.get("row_count_unchanged", -1)) != int(counts.get("total_documents", -2)):
        problems.append("expected row count != total documents")
    if after.get("column") != f"supporting_documents.{documents.TARGET_COLUMN}":
        problems.append("expected after-state names the wrong column")
    return problems


def verify_code_hashes(plan: Mapping[str, Any]) -> list[str]:
    """Every bound module must still hash to the recorded value."""
    problems: list[str] = []
    recorded = plan.get("code_hashes") or {}
    if not recorded:
        problems.append("plan records no code hashes")
        return problems
    live = documents.code_hashes()
    for module, digest in sorted(recorded.items()):
        current = live.get(module)
        if current is None:
            problems.append(f"bound module is missing: {module}")
        elif current != digest:
            problems.append(f"bound module changed: {module}")
    for module in sorted(set(live) - set(recorded)):
        problems.append(f"module is not bound by the plan: {module}")
    return problems


def verify_target_binding(plan: Mapping[str, Any], engine: Any) -> list[str]:
    """The plan must have been written for this exact development engine."""
    problems: list[str] = []
    recorded = normalize_target(plan.get("target") or {})
    live = normalize_target(documents.target_section(engine))
    for field in TARGET_FIELDS:
        expected = live.get(field)
        supplied = recorded.get(field)
        if supplied is None:
            # Presence is judged against the live identity: an engine whose
            # host and port are structurally absent cannot be named by them.
            # PostgreSQL — the only dialect an apply may mutate — always has
            # all four, so production binding is unaffected.
            if expected is not None:
                problems.append(f"plan does not name its {field}")
            continue
        if supplied != expected:
            problems.append(
                f"plan {field}={supplied!r} != live engine {field}={expected!r}"
            )
    return problems


def verify_rows_unchanged(plan: Mapping[str, Any], connection: Any) -> list[str]:
    """Every named row must still exist with the fingerprint the plan recorded."""
    problems: list[str] = []
    attachments = plan.get("attachments") or []
    holds = plan.get("holds") or []
    if not attachments and not holds:
        return ["plan names no documents to verify"]

    document_ids = [int(a["document_id"]) for a in attachments]
    document_ids += [int(h["document_id"]) for h in holds]
    fingerprints = {int(a["document_id"]): a["document_fingerprint"] for a in attachments}
    fingerprints.update({int(h["document_id"]): h["document_fingerprint"] for h in holds})

    seen: set[int] = set()
    statement = text(
        "SELECT id, meeting_db_id, agenda_item_id, agenda_item_number, "
        "document_url, updated_at FROM supporting_documents WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    for start in range(0, len(document_ids), 2000):
        batch = document_ids[start:start + 2000]
        for row in connection.execute(statement, {"ids": batch}).mappings():
            document_id = int(row["id"])
            seen.add(document_id)
            if documents.document_fingerprint(row) != fingerprints.get(document_id):
                problems.append(f"document {document_id} drifted since the plan was written")
    for document_id in sorted(set(document_ids) - seen):
        problems.append(f"document {document_id} is missing")
    return problems


def verify_agenda_items(plan: Mapping[str, Any], connection: Any) -> list[str]:
    """Every linked canonical agenda item must still exist unchanged."""
    problems: list[str] = []
    attachments = plan.get("attachments") or []
    expected = {int(a["agenda_item_db_id"]): a["agenda_item_fingerprint"] for a in attachments}
    if not expected:
        return ["plan links no agenda items"]
    ids = sorted(expected)
    seen: set[int] = set()
    statement = text(
        "SELECT id, meeting_db_id, agenda_item_number, agenda_item_id "
        "FROM agenda_items WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    for start in range(0, len(ids), 2000):
        batch = ids[start:start + 2000]
        for row in connection.execute(statement, {"ids": batch}).mappings():
            item_id = int(row["id"])
            seen.add(item_id)
            if documents.agenda_item_fingerprint(row) != expected.get(item_id):
                problems.append(f"agenda item {item_id} drifted since the plan was written")
    for item_id in ids:
        if item_id not in seen:
            problems.append(f"agenda item {item_id} is missing")
    return problems


def verify(plan: Mapping[str, Any], engine: Any) -> dict[str, list[str]]:
    """Run every check that does not need a connection."""
    results = {
        "shape": verify_plan_shape(plan),
        "arithmetic": verify_population_arithmetic(plan),
        "disjointness": verify_disjointness(plan),
        "ambiguity": verify_ambiguity_membership(plan),
        "after_state": verify_after_state(plan),
        "adjudication": verify_adjudication(plan),
        "identity": verify_identity(plan),
        "code_hashes": verify_code_hashes(plan),
        "target_binding": verify_target_binding(plan, engine),
    }
    with engine.connect() as connection:
        results["rows_unchanged"] = verify_rows_unchanged(plan, connection)
        results["agenda_items"] = verify_agenda_items(plan, connection)
    return results


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator path
    import argparse
    import json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True)
    args = parser.parse_args(argv)

    from db.core import get_engine

    plan = artifacts.load_verified(args.plan)
    results = verify(plan, get_engine())
    failures = {name: problems for name, problems in results.items() if problems}
    print(json.dumps({"ok": not failures, "failures": failures}, indent=2, sort_keys=True))
    return 0 if not failures else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
