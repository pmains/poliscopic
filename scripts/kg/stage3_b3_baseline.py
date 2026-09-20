#!/usr/bin/env python3
"""Q3 read-only baseline and dry-plan generator for Stage 3 B3 evidence spans."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - CLI bootstrap
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import inspect, text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target,
    guard_engine,
    statement_audit,
)
from scripts.kg import stage3_b3_span_contract as contract  # noqa: E402
from scripts.kg import stage3_meeting_result_identity as result_identity  # noqa: E402
from scripts.kg.stage2_artifacts import compute_digest, is_obsolete, load_verified, write_immutable  # noqa: E402

PRODUCER_VERSION = "kg-stage3-b3-baseline/1.0"
RESULT_DOCUMENT_TYPE = "Meeting Result"
CODE_MODULES = (
    "scripts/kg/stage3_b3_baseline.py",
    "scripts/kg/stage3_b3_span_contract.py",
    "scripts/kg/stage3_meeting_result_identity.py",
    "scripts/kg/stage2_artifacts.py",
    "scripts/entities/event_normalize_preflight.py",
)

DOCUMENTS_SQL = """
SELECT d.id, d.meeting_db_id, d.document_type, d.text_content,
       d.text_extraction_method, d.agenda_item_db_id
FROM supporting_documents d
WHERE d.document_type = :result_type
   OR EXISTS (SELECT 1 FROM meeting_events e WHERE e.supporting_doc_id = d.id)
ORDER BY d.id
"""
EVENTS_SQL = """
SELECT e.id, e.supporting_doc_id, e.text_offset_start, e.text_offset_end
FROM meeting_events e ORDER BY e.id
"""
EXTRACTIONS_SQL = """
SELECT x.meeting_event_id, x.extractor, x.extractor_version
FROM meeting_event_extractions x
WHERE x.meeting_event_id IS NOT NULL
ORDER BY x.meeting_event_id, x.id
"""
ITEMS_SQL = """
SELECT a.id, a.meeting_db_id, a.agenda_item_number
FROM agenda_items a ORDER BY a.meeting_db_id, a.id
"""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    return {name: _sha_file(repo / name) for name in CODE_MODULES}


def _row_dicts(result: Iterable[Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in result]


def capture_rows(connection: Any) -> dict[str, list[dict[str, Any]]]:
    """Read the complete governed inputs. No substring/limit operation is used."""
    return {
        "documents": _row_dicts(connection.execute(
            text(DOCUMENTS_SQL), {"result_type": RESULT_DOCUMENT_TYPE}).mappings()),
        "events": _row_dicts(connection.execute(text(EVENTS_SQL)).mappings()),
        "extractions": _row_dicts(connection.execute(text(EXTRACTIONS_SQL)).mappings()),
        "items": _row_dicts(connection.execute(text(ITEMS_SQL)).mappings()),
    }


def schema_binding(engine: Any) -> dict[str, Any]:
    inspector = inspect(engine)
    tables = ("supporting_documents", "meeting_events",
              "meeting_event_extractions", "agenda_items")
    body: dict[str, Any] = {}
    for table in tables:
        body[table] = [{"name": col["name"], "type": str(col["type"]),
                        "nullable": bool(col.get("nullable", True))}
                       for col in inspector.get_columns(table)]
    return {"tables": body, "sha256": canonical_sha256(body)}


def _extraction_versions(rows: Sequence[Mapping[str, Any]]) -> dict[int, list[str]]:
    grouped: dict[int, set[str]] = defaultdict(set)
    for row in rows:
        event_id = row.get("meeting_event_id")
        if event_id is None:
            continue
        extractor = str(row.get("extractor") or "").strip()
        version = str(row.get("extractor_version") or "").strip()
        if extractor and version:
            grouped[int(event_id)].add(f"{extractor}/{version}")
    return {key: sorted(values) for key, values in grouped.items()}


def derive_spans(rows: Mapping[str, Sequence[Mapping[str, Any]]]) -> tuple[list[dict[str, Any]], dict[int, str | None]]:
    docs = {int(row["id"]): dict(row) for row in rows["documents"]}
    texts = {doc_id: doc.get("text_content") for doc_id, doc in docs.items()}
    versions = _extraction_versions(rows["extractions"])
    item_ids: dict[tuple[int, str], list[int]] = defaultdict(list)
    for row in rows["items"]:
        item_ids[(int(row["meeting_db_id"]), str(row["agenda_item_number"]))].append(int(row["id"]))

    spans: list[dict[str, Any]] = []
    for event in rows["events"]:
        doc_id = int(event["supporting_doc_id"])
        doc = docs.get(doc_id, {})
        text_body = doc.get("text_content") or ""
        event_versions = versions.get(int(event["id"]), [])
        spans.append(contract.build_span(
            supporting_doc_id=doc_id,
            span_kind="stage1_meeting_event",
            start=event.get("text_offset_start"), end=event.get("text_offset_end"),
            text=text_body,
            extraction_method=str(doc.get("text_extraction_method") or ""),
            extractor_version=event_versions[0] if len(event_versions) == 1 else "",
            lineage_parent_id=int(event["id"]),
            agenda_item_candidates=doc.get("agenda_item_db_id"),
        ))

    for doc_id, doc in sorted(docs.items()):
        if doc.get("document_type") != RESULT_DOCUMENT_TYPE:
            continue
        text_body = doc.get("text_content") or ""
        for item_span in result_identity.extract_item_spans(doc.get("text_content")):
            number = result_identity.normalize_item_number(item_span["token"])
            candidates = item_ids.get((int(doc.get("meeting_db_id") or 0), str(number)), [])
            spans.append(contract.build_span(
                supporting_doc_id=doc_id,
                span_kind="stage3_result_item",
                start=int(item_span["start"]), end=int(item_span["end"]), text=text_body,
                extraction_method=str(doc.get("text_extraction_method") or ""),
                extractor_version=result_identity.PRODUCER_VERSION,
                lineage_parent_id=doc_id,
                agenda_item_candidates=candidates,
            ))
    return spans, texts


def _source_counts(spans: Sequence[Mapping[str, Any]], classification: Mapping[str, Any]) -> list[dict[str, Any]]:
    held = {int(row["index"]): row["disposition"] for row in classification["holds"]}
    counts: dict[tuple[str, str, str], int] = defaultdict(int)
    for index, span in enumerate(spans):
        method = str(span.get("evidence_version", {}).get("extraction_method") or "missing")
        disposition = held.get(index, "accepted")
        counts[(str(span["span_kind"]), method, disposition)] += 1
    return [{"span_kind": key[0], "extraction_method": key[1],
             "disposition": key[2], "count": value}
            for key, value in sorted(counts.items())]


def build_baseline(*, rows: Mapping[str, Sequence[Mapping[str, Any]]], created_at: str,
                   target: Mapping[str, Any], schema: Mapping[str, Any],
                   hashes: Mapping[str, str]) -> tuple[dict[str, Any], dict[str, Any]]:
    spans, texts = derive_spans(rows)
    classification = contract.classify_spans(spans, texts)
    if not classification["reconciles"]:
        raise ValueError("span classification does not reconcile")
    document_projection = [{
        "id": int(row["id"]), "meeting_db_id": int(row.get("meeting_db_id") or 0),
        "document_type": row.get("document_type"),
        "text_chars": len(row.get("text_content") or ""),
        "text_sha256": contract.text_sha256(row.get("text_content") or ""),
        "text_extraction_method": row.get("text_extraction_method"),
        "agenda_item_db_id": row.get("agenda_item_db_id"),
    } for row in rows["documents"]]
    ends = [int(span["text_offset_end"]) for span in spans
            if isinstance(span.get("text_offset_end"), int)]
    baseline = {
        "kind": "kg-stage3-b3-span-baseline", "version": PRODUCER_VERSION,
        "created_at": created_at, "mode": "read-only", "applied": False,
        "write_path": "absent by design", "target": dict(target),
        "schema": dict(schema), "code_hashes": dict(hashes),
        "inputs": {
            "documents": len(rows["documents"]), "events": len(rows["events"]),
            "extractions": len(rows["extractions"]), "agenda_items": len(rows["items"]),
            "document_projection_sha256": canonical_sha256(document_projection),
            "event_projection_sha256": canonical_sha256(rows["events"]),
            "extraction_projection_sha256": canonical_sha256(rows["extractions"]),
            "agenda_item_projection_sha256": canonical_sha256(rows["items"]),
        },
        "coverage": {
            "proposed": classification["proposed"],
            "accepted": classification["accepted_count"],
            "held": classification["hold_count"],
            "reconciles": classification["reconciles"],
            "source_counts": _source_counts(spans, classification),
        },
        "full_text_proof": {
            "query_uses_character_limit": False,
            "configured_character_limit": None,
            "maximum_document_characters": max((d["text_chars"] for d in document_projection), default=0),
            "maximum_span_end": max(ends, default=0),
            "spans_ending_after_8000": sum(end > 8000 for end in ends),
            "coordinate_system": contract.COORDINATE_SYSTEM,
            "limitation": "coordinates address retained extracted text; PDF page/box and source-byte coordinates are unavailable",
        },
        "documents": document_projection,
        "holds": classification["holds"],
    }
    dry_plan = contract.build_dry_plan(
        classification=classification, created_at=created_at,
        target=target, code_hashes=hashes)
    dry_plan["baseline_digest"] = canonical_sha256(baseline)
    dry_plan["schema_sha256"] = schema["sha256"]
    dry_plan["input_bindings"] = dict(baseline["inputs"])
    dry_plan["digest"] = canonical_sha256({k: v for k, v in dry_plan.items() if k != "digest"})
    return baseline, dry_plan


def validate_current(*, baseline_path: Path, plan_path: Path,
                     rows: Mapping[str, Sequence[Mapping[str, Any]]],
                     target: Mapping[str, Any], schema: Mapping[str, Any],
                     hashes: Mapping[str, str]) -> list[str]:
    """Rebuild from authoritative inputs and require exact artifact equality."""
    problems: list[str] = []
    if is_obsolete(baseline_path):
        problems.append("baseline artifact is obsolete")
    if is_obsolete(plan_path):
        problems.append("dry-plan artifact is obsolete")
    try:
        baseline = load_verified(baseline_path)
        plan = load_verified(plan_path)
    except Exception as exc:
        return problems + [f"artifact verification failed: {exc}"]
    expected_baseline, expected_plan = build_baseline(
        rows=rows, created_at=str(baseline.get("created_at")), target=target,
        schema=schema, hashes=hashes)
    if {k: v for k, v in baseline.items() if k != "digest"} != expected_baseline:
        problems.append("baseline does not equal the authoritative current rebuild")
    binding = plan.get("baseline_artifact")
    expected_binding = {"path": str(baseline_path), "digest": compute_digest(baseline)}
    if binding != expected_binding:
        problems.append("dry plan does not bind this exact baseline path and digest")
    expected_plan["baseline_artifact"] = expected_binding
    expected_plan["digest"] = canonical_sha256(
        {k: v for k, v in expected_plan.items() if k != "digest"})
    if ({k: v for k, v in plan.items() if k != "digest"} !=
            {k: v for k, v in expected_plan.items() if k != "digest"}):
        problems.append("dry plan does not equal the authoritative current rebuild")
    return problems


def run(engine: Any, *, out_dir: Path, created_at: str | None = None) -> dict[str, Any]:
    target = assert_read_only_target(engine)
    statements = guard_engine(engine)
    with engine.connect() as connection:
        rows = capture_rows(connection)
    schema = schema_binding(engine)
    audit = statement_audit(statements)
    if audit.get("select_only") is not True:
        raise RuntimeError(f"read-only audit failed: {audit}")
    stamp = created_at or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    baseline, plan = build_baseline(
        rows=rows, created_at=stamp, target=target,
        schema=schema, hashes=code_hashes())
    out_dir.mkdir(parents=True, exist_ok=True)
    baseline_path = out_dir / f"kg-stage3-b3-span-baseline-{stamp}.json"
    baseline_digest = write_immutable(baseline_path, baseline)
    plan["baseline_artifact"] = {"path": str(baseline_path), "digest": baseline_digest}
    plan["digest"] = canonical_sha256({k: v for k, v in plan.items() if k != "digest"})
    plan_path = out_dir / f"kg-stage3-b3-span-dry-plan-{stamp}.json"
    plan_digest = write_immutable(plan_path, plan)
    return {"status": "success", "baseline": str(baseline_path),
            "baseline_digest": baseline_digest, "plan": str(plan_path),
            "plan_digest": plan_digest, "coverage": baseline["coverage"],
            "read_only_audit": audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    args = parser.parse_args(argv)
    print(json.dumps(run(get_engine(), out_dir=args.out_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
