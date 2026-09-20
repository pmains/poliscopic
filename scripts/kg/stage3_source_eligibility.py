#!/usr/bin/env python3
"""Stage 3 governed source-eligibility matrix and read-only exception ledger."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import inspect, text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target, guard_engine, statement_audit)
from scripts.kg.stage2_artifacts import write_immutable  # noqa: E402

PRODUCER_VERSION = "kg-stage3-source-eligibility/1.0"
ARTIFACT_KIND = "kg-stage3-source-eligibility-baseline"
DISPOSITIONS = ("eligible_current", "eligible_stale", "missing_text",
                "unreadable", "unsupported", "ineligible")
SUPPORTED_METHODS = (
    "pymupdf", "ocr_local", "pdftotext",
    "pymupdf_layout", "pdftotext_layout", "tesseract_tsv",
)
FAILURE_METHODS = ("extraction_failed", "download_failed", "failed")
CODE_MODULES = (
    "scripts/kg/stage3_source_eligibility.py",
    "scripts/kg/stage2_artifacts.py",
    "scripts/entities/event_normalize_preflight.py",
)

DOC_SQL = """
SELECT id, body, document_type, document_url, text_content, text_extraction_method,
       text_extracted_at, scraped_at, content_hash, meeting_db_id, agenda_item_db_id
FROM supporting_documents ORDER BY id
"""
ITEM_SQL = """
SELECT id, body, source_body, agenda_item_text, meeting_db_id, lifecycle_status
FROM agenda_items ORDER BY id
"""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    return {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
            for name in CODE_MODULES}


def classify_document(row: Mapping[str, Any]) -> tuple[str, str]:
    body = str(row.get("text_content") or "")
    method = str(row.get("text_extraction_method") or "").strip()
    if body.strip():
        if method not in SUPPORTED_METHODS:
            return "unsupported", "text_present_with_unregistered_extraction_method"
        scraped, extracted = row.get("scraped_at"), row.get("text_extracted_at")
        if extracted is None or (scraped is not None and scraped > extracted):
            return "eligible_stale", "source_scrape_is_newer_than_text_extraction"
        return "eligible_current", "supported_text_version"
    if method in FAILURE_METHODS:
        return "unreadable", method
    if method.startswith("quarantine:high_page_count:"):
        return "unsupported", "high_page_count_quarantine"
    if method.startswith("quarantine:oversized:"):
        return "unsupported", "oversized_quarantine"
    if not method:
        return "missing_text", "no_text_and_no_extraction_disposition"
    return "unsupported", "unregistered_method_without_text"


def classify_item(row: Mapping[str, Any]) -> tuple[str, str]:
    if str(row.get("agenda_item_text") or "").strip():
        return "eligible_current", "structured_agenda_item_text_present"
    return "missing_text", "structured_agenda_item_text_absent"


def _platform(url: Any) -> str:
    return (urlparse(str(url or "")).hostname or "no-host").lower()


def capture(connection: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        "documents": [dict(row) for row in connection.execute(text(DOC_SQL)).mappings()],
        "agenda_items": [dict(row) for row in connection.execute(text(ITEM_SQL)).mappings()],
    }


def schema_binding(engine: Any) -> dict[str, Any]:
    inspector = inspect(engine)
    body = {table: [{"name": col["name"], "type": str(col["type"]),
                     "nullable": bool(col.get("nullable", True))}
                    for col in inspector.get_columns(table)]
            for table in ("supporting_documents", "agenda_items")}
    return {"tables": body, "sha256": canonical_sha256(body)}


def build_baseline(*, rows: Mapping[str, Sequence[Mapping[str, Any]]], created_at: str,
                   target: Mapping[str, Any], schema: Mapping[str, Any],
                   hashes: Mapping[str, str]) -> dict[str, Any]:
    totals = Counter({name: 0 for name in DISPOSITIONS})
    by_kind: dict[str, Counter] = {"supporting_document": Counter(), "agenda_item": Counter()}
    slices: Counter = Counter()
    exceptions: list[dict[str, Any]] = []
    projection: list[list[Any]] = []

    for row in rows["documents"]:
        disposition, reason = classify_document(row)
        totals[disposition] += 1
        by_kind["supporting_document"][disposition] += 1
        platform = _platform(row.get("document_url"))
        method = str(row.get("text_extraction_method") or "missing")
        slices[("supporting_document", platform, method, disposition)] += 1
        text_hash = hashlib.sha256(str(row.get("text_content") or "").encode()).hexdigest()
        projection.append(["supporting_document", int(row["id"]), disposition, reason,
                           text_hash, method, row.get("content_hash")])
        if disposition != "eligible_current":
            exceptions.append({"source_kind": "supporting_document", "id": int(row["id"]),
                               "disposition": disposition, "reason": reason,
                               "body": row.get("body"), "platform": platform,
                               "document_type": row.get("document_type"), "method": method})

    for row in rows["agenda_items"]:
        disposition, reason = classify_item(row)
        totals[disposition] += 1
        by_kind["agenda_item"][disposition] += 1
        source = str(row.get("source_body") or row.get("body") or "missing")
        slices[("agenda_item", source, "structured", disposition)] += 1
        text_hash = hashlib.sha256(str(row.get("agenda_item_text") or "").encode()).hexdigest()
        projection.append(["agenda_item", int(row["id"]), disposition, reason, text_hash,
                           source, row.get("lifecycle_status")])
        if disposition != "eligible_current":
            exceptions.append({"source_kind": "agenda_item", "id": int(row["id"]),
                               "disposition": disposition, "reason": reason,
                               "body": row.get("body"), "source": source})

    discovered = len(projection)
    classified = sum(totals.values())
    body = {
        "kind": ARTIFACT_KIND, "version": PRODUCER_VERSION, "created_at": created_at,
        "mode": "read-only", "applied": False, "write_path": "absent by design",
        "policy": {
            "dispositions": list(DISPOSITIONS), "supported_methods": list(SUPPORTED_METHODS),
            "failure_methods": list(FAILURE_METHODS),
            "stale_rule": "text_extracted_at absent or older than a later source scraped_at",
            "agenda_item_rule": "structured text present is current; absent text is missing",
        },
        "target": dict(target), "schema": dict(schema), "code_hashes": dict(hashes),
        "accounting": {"discovered": discovered, "classified": classified,
                       "reconciles": discovered == classified,
                       "by_disposition": dict(sorted(totals.items())),
                       "by_source_kind": {kind: dict(sorted(counts.items()))
                                          for kind, counts in by_kind.items()}},
        "source_slices": [{"source_kind": key[0], "platform_or_source": key[1],
                           "method": key[2], "disposition": key[3], "count": count}
                          for key, count in sorted(slices.items())],
        "exception_count": len(exceptions), "exceptions": exceptions,
        "exceptions_sha256": canonical_sha256(exceptions),
        "population_sha256": canonical_sha256(projection),
    }
    return {**body, "digest": canonical_sha256(body)}


def validate_current(artifact: Mapping[str, Any], *,
                     rows: Mapping[str, Sequence[Mapping[str, Any]]],
                     target: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    expected = build_baseline(
        rows=rows, created_at=str(artifact.get("created_at")), target=target,
        schema=schema, hashes=code_hashes())
    return [] if dict(artifact) == expected else [
        "eligibility artifact differs from the exact authoritative current rebuild"]


def run(engine: Any, *, out_dir: Path, stamp: str | None = None) -> dict[str, Any]:
    target = assert_read_only_target(engine)
    statements = guard_engine(engine)
    with engine.connect() as connection:
        rows = capture(connection)
    schema = schema_binding(engine)
    audit = statement_audit(statements)
    if not audit["select_only"]:
        raise RuntimeError(f"read-only audit failed: {audit}")
    created = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = build_baseline(
        rows=rows, created_at=created, target=target, schema=schema, hashes=code_hashes())
    if not artifact["accounting"]["reconciles"]:
        raise RuntimeError("eligibility accounting does not reconcile")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kg-stage3-source-eligibility-{created}.json"
    digest = write_immutable(path, artifact)
    return {"status": "success", "path": str(path), "digest": digest,
            "accounting": artifact["accounting"], "exception_count": artifact["exception_count"],
            "read_only_audit": audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    args = parser.parse_args(argv)
    print(json.dumps(run(get_engine(), out_dir=args.out_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
