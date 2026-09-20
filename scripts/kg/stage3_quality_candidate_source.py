#!/usr/bin/env python3
"""Read-only contract for the authoritative Stage 3 quality candidate source.

Live-row capture is intentionally unavailable here until a reviewed development-only
reader supplies the complete projection.  The pure builder refuses to invent a
predicate, assistance mode, promotion state, link state, or source dimension.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # pragma: no cover - enables direct offline CLI use
    sys.path.insert(0, str(REPO))

from scripts.kg import stage3_quality_benchmark as benchmark
from scripts.kg import stage3_processing_plan_inputs as plan_inputs
from scripts.kg import stage3_processing_receipt as processing_receipt
from scripts.entities.event_normalize_preflight import (
    assert_read_only_target, guard_engine, statement_audit,
)
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable

KIND = "kg-stage3-quality-candidate-source"
VERSION = "kg-stage3-quality-candidate-source/1.0"
SELECTION_KIND = "kg-stage3-processing-selection-snapshot"
DEFAULT_SELECTION = "data/kg-plans/kg-stage3-processing-selection-20260915T000423Z.json"
SOURCE_KEYS = ("kind", "version", "created_at", "mode", "applied", "write_path", "target",
               "selection_binding", "code_hashes", "query_contract", "row_projection_sha256",
               "rows", "source_accounting", "candidate_cases", "candidate_cases_sha256",
               "population", "digest")
ROW_FIELDS = ("extraction_id", "source_id", "text_content", "content_sha256",
              "platform_or_source", "extraction_method", "document_type", "body", "predicate",
              "output_type", "assistance_mode", "outcome", "promotion_applicability", "promoted", "promotion_state",
              "materialization_state", "link_state", "agenda_item_db_id", "meeting_db_id",
              "start", "end", "span_sha256")
PROJECTION_SQL = """
SELECT x.id AS extraction_id, d.id AS source_id, d.text_content,
       d.text_extraction_method AS extraction_method, d.document_type, d.body,
       COALESCE(NULLIF(m.source_url, ''), NULLIF(d.document_url, '')) AS platform_or_source,
       x.extractor, x.action_verb AS predicate,
       x.text_offset_start AS start, x.text_offset_end AS "end",
       e.id AS event_id, e.supporting_doc_id AS event_source_id,
       e.agenda_item_id AS agenda_item_db_id,
       ai.meeting_db_id AS agenda_item_meeting_db_id,
       d.meeting_db_id AS document_meeting_db_id
FROM meeting_event_extractions x
JOIN supporting_documents d ON d.id = x.supporting_doc_id
LEFT JOIN meetings m ON m.id = d.meeting_db_id
LEFT JOIN meeting_events e ON e.id = x.meeting_event_id
LEFT JOIN agenda_items ai ON ai.id = e.agenda_item_id
WHERE d.id = ANY(:source_ids)
ORDER BY x.id
"""
QUERY_CONTRACT = {
    "joins": ["meeting_event_extractions x -> supporting_documents d",
              "supporting_documents d -> meetings m (source dimensions only)",
              "meeting_event_extractions x -> meeting_events e (link state only)"],
    "prohibited_inference": "meeting_events existence must not determine promoted",
    "selection": "d.id must be an exact member of the bound processing selection snapshot",
    "mode": "development read-only SELECT projection only",
}
PROMOTION_AUTHORITY_REQUIREMENT = {
    "identity": "meeting_event_extractions.id",
    "fields": ["explicit promoted boolean", "promotion decision or receipt identity"],
    "prohibition": "meeting_events existence is link state, never promotion authority",
}
DEVELOPMENT_TARGET = {"tier": "development", "database": "poliscopic_dev",
                      "dialect": "postgresql"}
EXTRACTOR_ASSISTANCE = {"pattern": "deterministic"}


class CandidateSourceRefused(ValueError):
    """Rows or their authoritative selection binding are incomplete."""


def canonical_sha256(value: Any) -> str:
    return benchmark.canonical_sha256(value)


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(char in "0123456789abcdef" for char in value)


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    names = ("scripts/kg/stage3_quality_candidate_source.py", "scripts/kg/stage3_quality_cohort.py",
             "scripts/kg/stage3_quality_benchmark.py", "scripts/kg/stage2_artifacts.py")
    return {name: _sha(repo / name) for name in names}


def _load_selection(path: str | Path) -> tuple[dict[str, Any], dict[str, Any], set[int]]:
    path = Path(path)
    if is_obsolete(path):
        raise CandidateSourceRefused(f"selection artifact is obsolete: {path.name}")
    selection = load_verified(path)
    if selection.get("kind") != SELECTION_KIND:
        raise CandidateSourceRefused("selection artifact kind is not canonical")
    target = selection.get("target")
    if not isinstance(target, Mapping):
        raise CandidateSourceRefused("selection artifact target must be an object")
    wrong = {key: (target.get(key), expected) for key, expected in DEVELOPMENT_TARGET.items()
             if target.get(key) != expected}
    if wrong:
        detail = "; ".join(f"{key}={actual!r}, expected {expected!r}"
                           for key, (actual, expected) in sorted(wrong.items()))
        raise CandidateSourceRefused(f"selection artifact is not the exact development target: {detail}")
    if str(target.get("url_class", "development")) != "development":
        raise CandidateSourceRefused("selection artifact url_class is not development")
    if (selection.get("version") != plan_inputs.SELECTION_VERSION
            or selection.get("mode") != "read-only" or selection.get("applied") is not False
            or selection.get("write_path") != "absent by design"
            or selection.get("classification_fields") != list(plan_inputs.CLASSIFICATION_FIELDS)):
        raise CandidateSourceRefused("selection artifact contract is not canonical")
    entries = selection.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CandidateSourceRefused("selection artifact has no authoritative entries")
    if selection.get("selected") != len(entries):
        raise CandidateSourceRefused("selection artifact selected count differs from its entries")
    if not _hex64(selection.get("identity_sha256")):
        raise CandidateSourceRefused("selection artifact identity digest is invalid")
    ids = {row.get("source_id") for row in entries if isinstance(row, Mapping)}
    if len(ids) != len(entries) or any(not isinstance(value, int) or value <= 0 for value in ids):
        raise CandidateSourceRefused("selection entries lack unique positive source IDs")
    canonical_entries = [{field: row.get(field) for field in plan_inputs.CLASSIFICATION_FIELDS}
                         for row in entries]
    if (canonical_entries != entries
            or selection.get("entries_sha256") != plan_inputs.eligibility_sha256(entries)):
        raise CandidateSourceRefused("selection entries differ from their canonical reconstruction")
    identities = [[processing_receipt.SOURCE_KINDS[0], row["source_id"],
                   row["content_sha256"], row["extraction_method"],
                   processing_receipt.EXTRACTOR, processing_receipt.EXTRACTOR_VERSION]
                  for row in entries]
    if selection.get("identity_sha256") != plan_inputs.selection_identity_sha256(identities):
        raise CandidateSourceRefused("selection identity differs from its canonical reconstruction")
    binding = {"path": str(path), "kind": selection["kind"], "digest": selection["digest"],
               "file_sha256": _sha(path), "selected": selection.get("selected"),
               "identity_sha256": selection.get("identity_sha256")}
    return selection, binding, ids


def _positive_or_none(row: Mapping[str, Any], name: str) -> int | None:
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise CandidateSourceRefused(f"{name} must be a positive integer or null")
    return value


def _case(row: Mapping[str, Any], *, selected_ids: set[int]) -> dict[str, Any]:
    if set(row) != set(ROW_FIELDS):
        raise CandidateSourceRefused("row projection key set is not canonical")
    extraction_id = _positive_or_none(row, "extraction_id")
    source_id = _positive_or_none(row, "source_id")
    if extraction_id is None:
        raise CandidateSourceRefused("extraction_id is required")
    if source_id not in selected_ids:
        raise CandidateSourceRefused("row source_id is outside the exact selection artifact")
    text = row.get("text_content")
    if not isinstance(text, str) or not text:
        raise CandidateSourceRefused("row retained text is missing")
    if row.get("content_sha256") != benchmark.text_sha256(text):
        raise CandidateSourceRefused("row content_sha256 differs from retained text")
    start, end = row.get("start"), row.get("end")
    if not isinstance(start, int) or isinstance(start, bool) or not isinstance(end, int) or isinstance(end, bool):
        raise CandidateSourceRefused("row evidence offsets are not integers")
    if not 0 <= start < end <= len(text):
        raise CandidateSourceRefused("row evidence offsets are outside retained text")
    if row.get("span_sha256") != benchmark.text_sha256(text[start:end]):
        raise CandidateSourceRefused("row span_sha256 differs from retained text")
    applicability, promoted, promotion_state = (row.get("promotion_applicability"),
                                                 row.get("promoted"), row.get("promotion_state"))
    try:
        benchmark._promotion({"promotion_applicability": applicability,
                              "promoted": promoted, "promotion_state": promotion_state})
    except benchmark.BenchmarkRefused as exc:
        raise CandidateSourceRefused(f"row promotion state is invalid: {exc}") from exc
    item_id, meeting_id = _positive_or_none(row, "agenda_item_db_id"), _positive_or_none(row, "meeting_db_id")
    link_state = row.get("link_state")
    expected_link = "agenda_item" if item_id else "meeting" if meeting_id else "unlinked"
    if link_state != expected_link:
        raise CandidateSourceRefused("row link_state differs from its canonical IDs")
    expected_materialization = "candidate_only" if link_state == "unlinked" else "materialized"
    if row.get("materialization_state") != expected_materialization:
        raise CandidateSourceRefused("row materialization_state differs from its authoritative link state")
    source = {name: row.get(name) for name in
              ("platform_or_source", "extraction_method", "document_type", "body")}
    output = {name: row.get(name) for name in
              ("predicate", "output_type", "assistance_mode", "outcome")}
    output.update({"promotion_applicability": applicability, "promoted": promoted,
                   "promotion_state": promotion_state,
                   "materialization_state": row["materialization_state"],
                   "link_state": link_state, "agenda_item_db_id": item_id, "meeting_db_id": meeting_id})
    case = {"case_id": f"meeting_event_extraction:{extraction_id}", "source": source,
            "output": output, "document": {"source_kind": "supporting_document", "source_id": source_id,
            "content_sha256": row["content_sha256"]}, "retained_text": text,
            "evidence": {"coordinate_system": benchmark.COORDINATE_SYSTEM, "start": start, "end": end,
                         "span_sha256": row["span_sha256"]}}
    problems = benchmark.validate_case(case)
    if problems:
        raise CandidateSourceRefused("row does not form a complete benchmark case: " + "; ".join(problems))
    return case


def build_source(*, rows: Sequence[Mapping[str, Any]], selection_path: str | Path,
                 created_at: str, repo: Path = REPO) -> dict[str, Any]:
    selection, binding, selected_ids = _load_selection(selection_path)
    rows = sorted([dict(row) for row in rows], key=lambda row: row.get("extraction_id", -1))
    extraction_ids = [row.get("extraction_id") for row in rows]
    if len(set(extraction_ids)) != len(extraction_ids):
        raise CandidateSourceRefused("row projection contains duplicate extraction IDs")
    unexpected = sorted({row.get("source_id") for row in rows} - selected_ids,
                        key=lambda value: str(value))
    if unexpected:
        raise CandidateSourceRefused(f"row projection contains unexpected source IDs: {unexpected[:10]}")
    cases = [_case(row, selected_ids=selected_ids) for row in rows]
    if not cases or len({row["case_id"] for row in cases}) != len(cases):
        raise CandidateSourceRefused("row projection must yield unique nonempty candidate cases")
    by_source: dict[int, list[int]] = {source_id: [] for source_id in selected_ids}
    for row in rows:
        by_source[int(row["source_id"])].append(int(row["extraction_id"]))
    accounting_entries = [
        {"source_id": int(entry["source_id"]),
         "candidate_count": len(by_source[int(entry["source_id"])]),
         "extraction_ids": sorted(by_source[int(entry["source_id"])])}
        for entry in selection["entries"]
    ]
    source_accounting = {
        "selected_sources": len(accounting_entries),
        "zero_candidate_sources": sum(not entry["candidate_count"] for entry in accounting_entries),
        "sources_with_candidates": sum(bool(entry["candidate_count"]) for entry in accounting_entries),
        "candidate_rows": len(rows), "entries": accounting_entries,
        "entries_sha256": canonical_sha256(accounting_entries),
    }
    body = {"kind": KIND, "version": VERSION, "created_at": created_at, "mode": "read-only",
            "applied": False, "write_path": "absent by design", "target": dict(selection["target"]),
            "selection_binding": binding, "code_hashes": code_hashes(repo), "query_contract": QUERY_CONTRACT,
            "row_projection_sha256": canonical_sha256(rows), "rows": rows,
            "source_accounting": source_accounting,
            "candidate_cases": cases, "candidate_cases_sha256": canonical_sha256(cases),
            "population": {"rows": len(rows), "candidate_cases": len(cases),
                           "case_sha256": benchmark.population_digest(cases)}}
    return {**body, "digest": canonical_sha256(body)}


def validate_source(source: Mapping[str, Any], *, repo: Path = REPO) -> list[str]:
    if not isinstance(source, Mapping):
        return ["candidate source must be an object"]
    problems = []
    if set(source) != set(SOURCE_KEYS):
        problems.append("candidate source key set is not canonical")
    if source.get("digest") != canonical_sha256({k: v for k, v in source.items() if k != "digest"}):
        problems.append("candidate source digest does not match body")
    if source.get("query_contract") != QUERY_CONTRACT:
        problems.append("candidate source query contract differs")
    if source.get("code_hashes") != code_hashes(repo):
        problems.append("candidate source code hashes differ")
    cases = source.get("candidate_cases")
    if not isinstance(cases, list) or not cases:
        problems.append("candidate source has no candidate cases")
    elif source.get("candidate_cases_sha256") != canonical_sha256(cases):
        problems.append("candidate case digest differs from cases")
    else:
        try:
            if source.get("population") != {"rows": len(cases), "candidate_cases": len(cases),
                                            "case_sha256": benchmark.population_digest(cases)}:
                problems.append("candidate source population differs from cases")
            for case in cases:
                if benchmark.validate_case(case):
                    problems.append("candidate source carries an incomplete case")
                    break
        except Exception as exc:  # noqa: BLE001 - malformed candidates are invalid evidence
            problems.append(f"candidate source cases cannot be verified: {exc}")
    binding = source.get("selection_binding") or {}
    try:
        selection, expected, _ids = _load_selection(binding.get("path"))
        if binding != expected or source.get("target") != selection.get("target"):
            problems.append("candidate source selection binding differs from disk")
        rebuilt = build_source(rows=source.get("rows") or [], selection_path=binding.get("path"),
                               created_at=str(source.get("created_at")), repo=repo)
        if rebuilt != dict(source):
            problems.append("candidate source differs from exact row reconstruction")
    except (CandidateSourceRefused, OSError, TypeError, ValueError) as exc:
        problems.append(f"candidate source selection cannot be verified: {exc}")
    return problems


def promotion_authority_problems() -> list[str]:
    """Known schema declarations contain no explicit extraction-promotion record."""
    return [
        "no registered table or field records an explicit promoted boolean for "
        "meeting_event_extractions.id",
        "no registered promotion decision/receipt identity is joinable to "
        "meeting_event_extractions.id",
    ]


def _engine_identity(engine: Any) -> dict[str, Any]:
    """Read target identity from engine metadata without opening a connection."""
    dialect = getattr(getattr(engine, "dialect", None), "name", None)
    url = getattr(engine, "url", None)
    database = getattr(url, "database", None)
    host = getattr(url, "host", None)
    port = getattr(url, "port", None)
    if database is None:
        parts = urlsplit(str(url or ""))
        database, host, port = (parts.path or "").lstrip("/"), parts.hostname, parts.port
    return {"dialect": dialect, "database": database, "host": host, "port": port}


def _guard_engine_target(engine: Any, target: Mapping[str, Any]) -> None:
    """Refuse non-development or artifact-drifted engines before connection."""
    live = _engine_identity(engine)
    for key in ("dialect", "database"):
        expected = DEVELOPMENT_TARGET[key]
        if live.get(key) != expected:
            raise CandidateSourceRefused(
                f"engine is not the exact development target: {key}={live.get(key)!r}, "
                f"expected {expected!r}")
    for key in ("dialect", "database", "host", "port"):
        if key in target and target.get(key) != live.get(key):
            raise CandidateSourceRefused(
                f"engine target differs from selection artifact: {key}={live.get(key)!r}, "
                f"artifact recorded {target.get(key)!r}")


def _project_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Convert only explicit database fields into the canonical row projection."""
    text = row.get("text_content")
    if not isinstance(text, str) or not text:
        raise CandidateSourceRefused(f"extraction {row.get('extraction_id')} has no retained text")
    extractor = str(row.get("extractor") or "")
    if extractor not in EXTRACTOR_ASSISTANCE:
        raise CandidateSourceRefused(
            f"extraction {row.get('extraction_id')} has unsupported assistance authority: {extractor!r}")
    event_id = row.get("event_id")
    if event_id is not None and row.get("event_source_id") != row.get("source_id"):
        raise CandidateSourceRefused(
            f"extraction {row.get('extraction_id')} materializes an event from another source")
    item_id = row.get("agenda_item_db_id") if event_id is not None else None
    meeting_id = row.get("document_meeting_db_id") if event_id is not None else None
    if (item_id is not None
            and row.get("agenda_item_meeting_db_id") != row.get("document_meeting_db_id")):
        raise CandidateSourceRefused(
            f"extraction {row.get('extraction_id')} links an agenda item from another meeting")
    link_state = "agenda_item" if item_id is not None else "meeting" if event_id is not None else "unlinked"
    start, end = row.get("start"), row.get("end")
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= len(text):
        raise CandidateSourceRefused(
            f"extraction {row.get('extraction_id')} has invalid evidence coordinates")
    return {
        "extraction_id": row.get("extraction_id"), "source_id": row.get("source_id"),
        "text_content": text, "content_sha256": benchmark.text_sha256(text),
        "platform_or_source": row.get("platform_or_source"),
        "extraction_method": row.get("extraction_method"),
        "document_type": row.get("document_type"), "body": row.get("body"),
        "predicate": row.get("predicate"), "output_type": "meeting_event_extraction",
        "assistance_mode": EXTRACTOR_ASSISTANCE[extractor],
        "outcome": "success" if event_id is not None else "held",
        "promotion_applicability": "not_represented", "promoted": None,
        "promotion_state": "not_represented",
        "materialization_state": "materialized" if event_id is not None else "candidate_only",
        "link_state": link_state, "agenda_item_db_id": item_id, "meeting_db_id": meeting_id,
        "start": start, "end": end, "span_sha256": benchmark.text_sha256(text[start:end]),
    }


def capture_rows(engine: Any, *, selection_path: str | Path) -> list[dict[str, Any]]:
    """Capture one coherent, SELECT-only development projection or fail closed."""
    selection, _binding, selected_ids = _load_selection(selection_path)
    _guard_engine_target(engine, selection["target"])
    guarded_target = assert_read_only_target(engine)
    if guarded_target != selection["target"]:
        raise CandidateSourceRefused("read-only guard target differs from the exact selection target")
    statements = guard_engine(engine)
    try:
        from sqlalchemy import text
        with engine.connect().execution_options(isolation_level="REPEATABLE READ") as connection:
            transaction = connection.begin()
            try:
                connection.execute(text("SET TRANSACTION READ ONLY"))
                raw = connection.execute(text(PROJECTION_SQL),
                                         {"source_ids": sorted(selected_ids)}).mappings().all()
            finally:
                transaction.rollback()
    except CandidateSourceRefused:
        raise
    except Exception as exc:
        raise CandidateSourceRefused(f"development read-only projection failed: {exc}") from exc
    audit = statement_audit(statements)
    if not audit.get("select_only"):
        raise CandidateSourceRefused(f"read-only statement audit failed: {audit}")
    projected = [_project_row(row) for row in raw]
    expected_content = {int(entry["source_id"]): entry["content_sha256"]
                        for entry in selection["entries"]}
    drifted = sorted({row["source_id"] for row in projected
                      if row["content_sha256"] != expected_content[row["source_id"]]})
    if drifted:
        raise CandidateSourceRefused(
            f"retained text differs from selection identity for {len(drifted)} sources: {drifted[:10]}")
    return projected


def execution_blocker(selection_path: str | Path) -> dict[str, Any]:
    selection, binding, _ids = _load_selection(selection_path)
    return {"status": "blocked", "code": "missing_explicit_extraction_promotion_authority",
            "selection_binding": binding, "target": selection["target"], "query_contract": QUERY_CONTRACT,
            "promotion_authority_requirement": PROMOTION_AUTHORITY_REQUIREMENT,
            "promotion_authority_problems": promotion_authority_problems(),
            "required_projection_fields": list(ROW_FIELDS),
            "reason": "capture is refused before connection; this module will not infer promotion from meeting_events"}


def generate_artifacts(engine: Any, *, selection_path: str | Path, source_path: str | Path,
                       cohort_path: str | Path, created_at: str) -> dict[str, Any]:
    """Validate the complete snapshot before emitting either immutable artifact."""
    rows = capture_rows(engine, selection_path=selection_path)
    source = build_source(rows=rows, selection_path=selection_path, created_at=created_at)
    problems = validate_source(source)
    if problems:
        raise CandidateSourceRefused("candidate source validation failed: " + "; ".join(problems))
    from scripts.kg import stage3_quality_cohort as cohort
    with tempfile.TemporaryDirectory(prefix="kg-stage3-quality-") as directory:
        staged_source = Path(directory) / "candidate-source.json"
        write_immutable(staged_source, source)
        cohort_value = cohort.build_cohort(source_path=staged_source, created_at=created_at)
        cohort_problems = cohort.validate_cohort(cohort_value)
        if cohort_problems:
            raise CandidateSourceRefused("candidate cohort validation failed: " + "; ".join(cohort_problems))
    cohort_value["source_binding"]["path"] = str(source_path)
    cohort_value["digest"] = cohort.canonical_sha256(
        {key: value for key, value in cohort_value.items() if key != "digest"})
    write_immutable(source_path, source)
    # Reconstruction is now against the durable intended source path.  All other
    # fields were already validated against the byte-identical staged source.
    cohort_problems = cohort.validate_cohort(cohort_value)
    if cohort_problems:
        raise CandidateSourceRefused("final cohort binding validation failed: " + "; ".join(cohort_problems))
    write_immutable(cohort_path, cohort_value)
    return {"status": "success", "target": source["target"], "rows": len(rows),
            "candidate_source": {"path": str(source_path), "digest": source["digest"]},
            "cohort": {"path": str(cohort_path), "digest": cohort_value["digest"]},
            "promotion": {"applicability": "not_represented", "inferred": False}}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--selection", type=Path, default=REPO / DEFAULT_SELECTION)
    parser.add_argument("--stamp", required=True)
    parser.add_argument("--source-out", type=Path, required=True)
    parser.add_argument("--cohort-out", type=Path, required=True)
    args = parser.parse_args(argv)
    from scripts.db.core import get_engine
    try:
        result = generate_artifacts(get_engine(), selection_path=args.selection,
                                    source_path=args.source_out, cohort_path=args.cohort_out,
                                    created_at=args.stamp)
    except CandidateSourceRefused as exc:
        print(json.dumps({"status": "blocked", "reason": str(exc)}, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
