#!/usr/bin/env python3
"""Read-only, exact B3 agenda-item linkage plan for governed held spans.

This is deliberately a *planning* boundary.  It reads the immutable Q3/Q5 B3
population and a current development projection, classifies every held span once,
and writes a mode-0600 immutable artifact.  It never changes a span, event,
document, agenda item, or schema.  A link is proposed only from one of three
already-stored canonical facts: the document's item FK, the event's item FK, or
the result document's own parsed item token resolved to exactly one item in that
same meeting.  Meeting membership is a guard, never a linking route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - CLI bootstrap
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import bindparam, text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target, guard_engine, statement_audit,
)
from scripts.kg import stage3_b3_disposition_plan as disposition  # noqa: E402
from scripts.kg import stage3_meeting_result_identity as result_identity  # noqa: E402
from scripts.kg.stage2_artifacts import load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-b3-item-linkage-plan"
VERSION = "kg-stage3-b3-item-linkage-plan/1.0"
UPSTREAM_KIND = disposition.KIND
RESULT_KIND = "stage3_result_item"
EVENT_KIND = "stage1_meeting_event"
OUTCOMES = (
    "would_link_document_item", "would_link_event_item", "would_link_result_number",
    "hold_invalid_span", "hold_source_gap", "hold_evidence_drift", "hold_lineage_gap",
    "hold_item_target_missing", "hold_cross_meeting_target", "hold_result_span_drift",
    "hold_result_target_missing", "hold_result_target_ambiguous", "hold_no_item_evidence",
)
CODE_MODULES = (
    "scripts/kg/stage3_b3_item_linkage_plan.py",
    "scripts/kg/stage3_b3_disposition_plan.py",
    "scripts/kg/stage3_b3_baseline.py",
    "scripts/kg/stage3_b3_span_contract.py",
    "scripts/kg/stage3_meeting_result_identity.py",
    "scripts/kg/stage2_artifacts.py",
)

DOCUMENTS_SQL = """
SELECT id, meeting_db_id, agenda_item_db_id, document_type, text_content,
       text_extraction_method
FROM supporting_documents WHERE id IN :document_ids ORDER BY id
"""
EVENTS_SQL = """
SELECT id, supporting_doc_id, agenda_item_id, text_offset_start, text_offset_end
FROM meeting_events WHERE supporting_doc_id IN :document_ids ORDER BY id
"""
ITEMS_SQL = """
SELECT id, meeting_db_id, agenda_item_number FROM agenda_items WHERE meeting_db_id IN :meeting_ids
ORDER BY meeting_db_id, id
"""


class LinkageRefused(RuntimeError):
    """The exact upstream population or current source projection is not admissible."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def text_sha256(value: Any) -> str:
    return hashlib.sha256((value if isinstance(value, str) else "").encode("utf-8")).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for name in CODE_MODULES:
        path = repo / name
        if not path.is_file():
            raise LinkageRefused(f"required code module is absent: {name}")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def _positive(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _identity(value: Any) -> tuple[int, str, int, int] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 4:
        return None
    doc_id, kind, start, end = value
    if (_positive(doc_id) is None or kind not in (EVENT_KIND, RESULT_KIND)
            or not isinstance(start, int) or isinstance(start, bool) or start < 0
            or not isinstance(end, int) or isinstance(end, bool) or end <= start):
        return None
    return int(doc_id), str(kind), start, end


def capture_rows(connection: Any, *, document_ids: Sequence[int]) -> dict[str, list[dict[str, Any]]]:
    """Capture every governed source row, without expanding into unrelated document text."""
    ids = sorted({_positive(value) for value in document_ids if _positive(value) is not None})
    if not ids:
        return {"documents": [], "events": [], "items": []}
    document_statement = text(DOCUMENTS_SQL).bindparams(bindparam("document_ids", expanding=True))
    event_statement = text(EVENTS_SQL).bindparams(bindparam("document_ids", expanding=True))
    documents = [dict(row) for row in connection.execute(
        document_statement, {"document_ids": ids}).mappings()]
    meetings = sorted({_positive(row.get("meeting_db_id")) for row in documents
                       if _positive(row.get("meeting_db_id")) is not None})
    item_statement = text(ITEMS_SQL).bindparams(bindparam("meeting_ids", expanding=True))
    return {
        "documents": documents,
        "events": [dict(row) for row in connection.execute(
            event_statement, {"document_ids": ids}).mappings()],
        "items": ([dict(row) for row in connection.execute(
            item_statement, {"meeting_ids": meetings}).mappings()] if meetings else []),
    }


def _load_upstream(path: str | Path, *, repo: Path) -> dict[str, Any]:
    artifact = Path(path)
    document = load_verified(artifact)
    if document.get("kind") != UPSTREAM_KIND:
        raise LinkageRefused(f"{artifact.name} is not the governed B3 disposition artifact")
    if disposition.validate_plan(document, repo=repo):
        raise LinkageRefused("governed B3 disposition artifact fails exact reconstruction")
    accounting = document.get("accounting") or {}
    records = document.get("dispositions")
    if (document.get("mode") != "read-only" or document.get("applied") is not False
            or document.get("write_path") != "absent by design" or not isinstance(records, list)
            or accounting.get("population") != len(records)
            or accounting.get("classified") != len(records)
            or accounting.get("reconciles") is not True):
        raise LinkageRefused("governed B3 disposition population is not an exact read-only partition")
    baseline_binding = (document.get("bindings") or {}).get("baseline") or {}
    baseline_path = Path(baseline_binding.get("path") or "")
    if not baseline_path.is_absolute():
        baseline_path = repo / baseline_path
    baseline = load_verified(baseline_path)
    if (baseline.get("kind") != disposition.BASELINE_KIND
            or baseline.get("digest") != baseline_binding.get("digest")):
        raise LinkageRefused("governed B3 disposition does not bind one verified Q3 baseline")
    document = dict(document)
    document["_baseline_documents"] = {
        int(row["id"]): dict(row) for row in (baseline.get("documents") or [])
        if _positive(row.get("id")) is not None
    }
    return document


def _item_index(rows: Sequence[Mapping[str, Any]]) -> tuple[dict[int, dict[str, Any]], dict[tuple[int, str], list[int]]]:
    by_id: dict[int, dict[str, Any]] = {}
    by_number: dict[tuple[int, str], list[int]] = defaultdict(list)
    for source in rows:
        item_id, meeting_id = _positive(source.get("id")), _positive(source.get("meeting_db_id"))
        number = result_identity.normalize_item_number(str(source.get("agenda_item_number") or ""))
        if item_id is None or meeting_id is None or item_id in by_id:
            continue
        row = {"id": item_id, "meeting_db_id": meeting_id,
               "agenda_item_number": str(source.get("agenda_item_number") or "")}
        by_id[item_id] = row
        if number:
            by_number[(meeting_id, number)].append(item_id)
    return by_id, {key: sorted(value) for key, value in by_number.items()}


def governed_document_ids(upstream: Mapping[str, Any]) -> list[int]:
    """Return the exact source-document population named by the immutable hold ledger."""
    ids = []
    for hold in upstream.get("dispositions") or []:
        identity = _identity((hold.get("evidence_identity") or {}).get("span_identity"))
        if identity is None:
            raise LinkageRefused("governed disposition contains an invalid span identity")
        ids.append(identity[0])
    return sorted(set(ids))


def _record(index: int, identity: Any, outcome: str, *, evidence: Mapping[str, Any],
            agenda_item_db_id: int | None = None) -> dict[str, Any]:
    row = {"source_index": index, "identity": list(identity) if identity else identity,
           "outcome": outcome, "evidence": dict(evidence)}
    if agenda_item_db_id is not None:
        row["agenda_item_db_id"] = agenda_item_db_id
    return row


def _target_outcome(*, target_id: int, doc_meeting: int | None,
                    items: Mapping[int, Mapping[str, Any]], success: str,
                    base: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    target = items.get(target_id)
    if target is None:
        return "hold_item_target_missing", dict(base)
    if doc_meeting is None or target.get("meeting_db_id") != doc_meeting:
        return "hold_cross_meeting_target", dict(base)
    return success, dict(base)


def classify(*, upstream: Mapping[str, Any], rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> dict[str, Any]:
    """Classify every governed held span exactly once without inventing a target."""
    docs = {_positive(row.get("id")): dict(row) for row in rows.get("documents", [])
            if _positive(row.get("id")) is not None}
    events_by_identity: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows.get("events", []):
        doc_id, start, end = _positive(row.get("supporting_doc_id")), row.get("text_offset_start"), row.get("text_offset_end")
        if doc_id is not None and isinstance(start, int) and not isinstance(start, bool) and isinstance(end, int) and not isinstance(end, bool):
            events_by_identity[(doc_id, start, end)].append(dict(row))
    items, numbers = _item_index(rows.get("items", []))
    expected_docs = upstream.get("_baseline_documents") or {}

    records: list[dict[str, Any]] = []
    for hold in upstream["dispositions"]:
        index, identity = hold.get("source_index"), _identity((hold.get("evidence_identity") or {}).get("span_identity"))
        if not isinstance(index, int) or identity is None:
            records.append(_record(index if isinstance(index, int) else -1, identity, "hold_invalid_span",
                                   evidence={"upstream_digest": upstream["digest"]}))
            continue
        doc_id, kind, start, end = identity
        doc = docs.get(doc_id)
        base = {"upstream_digest": upstream["digest"], "supporting_doc_id": doc_id,
                "span_kind": kind, "offsets": [start, end]}
        if doc is None or not isinstance(doc.get("text_content"), str):
            records.append(_record(index, identity, "hold_source_gap", evidence=base))
            continue
        body, meeting_id = doc["text_content"], _positive(doc.get("meeting_db_id"))
        expected = expected_docs.get(doc_id)
        if expected is not None and (
                text_sha256(body) != expected.get("text_sha256")
                or doc.get("text_extraction_method") != expected.get("text_extraction_method")):
            records.append(_record(index, identity, "hold_evidence_drift",
                                   evidence={**base, "text_sha256": text_sha256(body),
                                             "expected_text_sha256": expected.get("text_sha256")}))
            continue
        if end > len(body):
            records.append(_record(index, identity, "hold_evidence_drift",
                                   evidence={**base, "text_sha256": text_sha256(body)}))
            continue
        base = {**base, "text_sha256": text_sha256(body),
                "text_extraction_method": doc.get("text_extraction_method")}
        document_item = _positive(doc.get("agenda_item_db_id"))
        if document_item is not None:
            outcome, evidence = _target_outcome(
                target_id=document_item, doc_meeting=meeting_id, items=items,
                success="would_link_document_item", base={**base, "route": "document_canonical_item"})
            records.append(_record(index, identity, outcome, evidence=evidence,
                                   agenda_item_db_id=document_item if outcome.startswith("would_") else None))
            continue
        if kind == EVENT_KIND:
            matching = events_by_identity.get((doc_id, start, end), [])
            if len(matching) != 1:
                records.append(_record(index, identity, "hold_lineage_gap", evidence={**base, "event_matches": len(matching)}))
                continue
            event_item = _positive(matching[0].get("agenda_item_id"))
            if event_item is None:
                records.append(_record(index, identity, "hold_no_item_evidence",
                                       evidence={**base, "event_id": matching[0].get("id")}))
                continue
            outcome, evidence = _target_outcome(
                target_id=event_item, doc_meeting=meeting_id, items=items,
                success="would_link_event_item", base={**base, "route": "event_canonical_item",
                                                        "event_id": matching[0].get("id")})
            records.append(_record(index, identity, outcome, evidence=evidence,
                                   agenda_item_db_id=event_item if outcome.startswith("would_") else None))
            continue
        parsed = {(int(span["start"]), int(span["end"])): span
                  for span in result_identity.extract_item_spans(body)}
        span = parsed.get((start, end))
        if span is None:
            records.append(_record(index, identity, "hold_result_span_drift", evidence=base))
            continue
        token = result_identity.normalize_item_number(span["token"])
        candidates = numbers.get((meeting_id, token), []) if meeting_id is not None and token else []
        if not candidates:
            records.append(_record(index, identity, "hold_result_target_missing",
                                   evidence={**base, "token": token or None}))
        elif len(candidates) != 1:
            records.append(_record(index, identity, "hold_result_target_ambiguous",
                                   evidence={**base, "token": token, "candidate_ids": candidates}))
        else:
            target_id = candidates[0]
            records.append(_record(index, identity, "would_link_result_number",
                                   evidence={**base, "route": "result_exact_item_number", "token": token},
                                   agenda_item_db_id=target_id))
    records.sort(key=lambda row: row["source_index"])
    return {"records": records, "items": items}


def build_plan(*, upstream_path: str | Path, rows: Mapping[str, Sequence[Mapping[str, Any]]],
               target: Mapping[str, Any], created_at: str, repo: Path = REPO) -> dict[str, Any]:
    upstream = _load_upstream(upstream_path, repo=repo)
    classified = classify(upstream=upstream, rows=rows)
    records = classified["records"]
    population = len(upstream["dispositions"])
    if [row["source_index"] for row in records] != list(range(population)):
        raise LinkageRefused("every upstream held span must have exactly one source-indexed disposition")
    counts = {outcome: sum(row["outcome"] == outcome for row in records) for outcome in OUTCOMES}
    linked = sum(counts[name] for name in OUTCOMES if name.startswith("would_link_"))
    held = population - linked
    body = {
        "kind": KIND, "version": VERSION, "created_at": created_at, "mode": "dry-run",
        "applied": False, "write_path": "absent by design", "target": dict(target),
        "bindings": {
            "upstream_disposition": {"path": str(upstream_path), "digest": upstream["digest"]},
            "upstream_population": population,
            "source_projections": {name: canonical_sha256(list(rows.get(name, [])))
                                   for name in ("documents", "events", "items")},
            "code_hashes": code_hashes(repo),
        },
        "policy": {
            "allowed_routes": ["document_canonical_item", "event_canonical_item", "result_exact_item_number"],
            "forbidden": ["meeting_membership_inference", "title_similarity", "document_order", "fuzzy_match", "model_inference"],
            "cross_meeting_targets_refused": True,
        },
        "accounting": {"population": population, "by_outcome": counts, "linked": linked,
                       "held": held, "reconciles": population == linked + held,
                       "data_operations_proposed": 0},
        "dispositions": records,
        "future_apply_contract": {
            "authorization": "explicit human approval for the exact immutable plan path and digest",
            "target": "exact bound development PostgreSQL target; production refused",
            "backup": "fresh protected backup with isolated restore proof",
            "transaction": "one owned SERIALIZABLE transaction; lock every source parent and target",
            "operations": "insert only exact would_link records into receipt-owned evidence span storage",
            "postconditions": "source hashes, offsets, parent lineage, target ids and counts exactly match the plan",
            "receipt": "immutable O_EXCL mode-0600 receipt with per-row preimages and postimages",
        },
        "future_rollback_contract": {
            "authority": "exact verified apply receipt for this plan only",
            "operation": "remove only receipt-owned rows after target/schema/dependency checks",
            "refuse_if": ["source or target drift", "unowned rows", "dependent objects", "receipt mismatch"],
            "receipt": "immutable O_EXCL mode-0600 rollback receipt with terminal status",
        },
        "replay_contract": {"before_apply": "writes zero", "after_apply": "exact receipt-owned rows are a no-op",
                            "drift": "any code, artifact, source, target or receipt drift refuses"},
    }
    return {**body, "digest": canonical_sha256(body)}


def validate_plan(plan: Mapping[str, Any], *, upstream_path: str | Path,
                  rows: Mapping[str, Sequence[Mapping[str, Any]]], repo: Path = REPO) -> list[str]:
    try:
        expected = build_plan(upstream_path=upstream_path, rows=rows, target=plan.get("target") or {},
                              created_at=str(plan.get("created_at")), repo=repo)
    except Exception as exc:
        return [f"plan cannot be reconstructed: {exc}"]
    return [] if dict(plan) == expected else ["plan differs from exact bound-input reconstruction"]


def run(engine: Any, *, upstream_path: str | Path, out_dir: Path,
        created_at: str | None = None) -> dict[str, Any]:
    target = assert_read_only_target(engine)
    upstream = _load_upstream(upstream_path, repo=REPO)
    statements = guard_engine(engine)
    with engine.connect() as connection:
        rows = capture_rows(connection, document_ids=governed_document_ids(upstream))
    audit = statement_audit(statements)
    if audit.get("select_only") is not True:
        raise LinkageRefused(f"read-only audit failed: {audit}")
    stamp = created_at or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = build_plan(upstream_path=upstream_path, rows=rows, target=target, created_at=stamp)
    problems = validate_plan(plan, upstream_path=upstream_path, rows=rows)
    if problems:
        raise LinkageRefused("generated plan validation failed: " + "; ".join(problems))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kg-stage3-b3-item-linkage-{stamp}.json"
    digest = write_immutable(path, plan)
    return {"status": "success", "path": str(path), "digest": digest,
            "accounting": plan["accounting"], "read_only_audit": audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    parser.add_argument("--stamp")
    args = parser.parse_args(argv)
    print(json.dumps(run(get_engine(), upstream_path=args.upstream, out_dir=args.out_dir,
                         created_at=args.stamp), sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
