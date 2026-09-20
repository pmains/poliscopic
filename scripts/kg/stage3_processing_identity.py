#!/usr/bin/env python3
"""Version-aware Stage 3 processing identity and read-only coverage baseline."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target, guard_engine, statement_audit)
from scripts.kg import stage3_source_eligibility as eligibility  # noqa: E402
from scripts.kg.producer_versions import PRODUCER_VERSIONS  # noqa: E402
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

PRODUCER_VERSION = "kg-stage3-processing-identity/1.0"
ARTIFACT_KIND = "kg-stage3-processing-identity-baseline"
EXTRACTOR = "sweep_docs"
EXTRACTOR_VERSION = PRODUCER_VERSIONS[EXTRACTOR]
DISPOSITIONS = ("current_proven", "unproven_current", "source_stale",
                "failed_current", "not_eligible")
ACQUISITION_CLASSES = ("recorded_scraper_acquisition", "recorded_text_pipeline",
                       "provenance_unrecorded")
CODE_MODULES = (
    "scripts/kg/stage3_processing_identity.py",
    "scripts/kg/stage3_source_eligibility.py",
    "scripts/kg/producer_versions.py",
    "scripts/kg/stage2_artifacts.py",
)

DOC_SQL = """
SELECT id, body, document_type, document_url, text_content, text_extraction_method,
       text_extracted_at, scraped_at, content_hash, meeting_db_id, agenda_item_db_id,
       swept_at
FROM supporting_documents ORDER BY id
"""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode()).hexdigest()


def content_sha256(row: Mapping[str, Any]) -> str:
    return hashlib.sha256(str(row.get("text_content") or "").encode()).hexdigest()


def evidence_identity(row: Mapping[str, Any]) -> tuple[str, int, str, str]:
    return ("supporting_document", int(row["id"]), content_sha256(row),
            str(row.get("text_extraction_method") or ""))


def processing_identity(row: Mapping[str, Any], *, extractor: str = EXTRACTOR,
                        extractor_version: str = EXTRACTOR_VERSION) -> tuple[Any, ...]:
    return (*evidence_identity(row), extractor, extractor_version)


def acquisition_class(row: Mapping[str, Any]) -> str:
    """Classify recorded acquisition evidence, independently of KG processing."""
    if row.get("document_url") and row.get("scraped_at") is not None:
        return "recorded_scraper_acquisition"
    if row.get("text_extraction_method") or row.get("text_extracted_at") is not None:
        return "recorded_text_pipeline"
    return "provenance_unrecorded"


def classify(row: Mapping[str, Any], receipt: Mapping[str, Any] | None = None) -> tuple[str, str]:
    eligible, reason = eligibility.classify_document(row)
    if eligible == "eligible_stale":
        return "source_stale", reason
    if eligible != "eligible_current":
        return "not_eligible", eligible
    if receipt is None:
        return "unproven_current", "no_versioned_processing_receipt"
    if tuple(receipt.get("processing_identity") or ()) != processing_identity(row):
        return "unproven_current", "receipt_identity_drift"
    if receipt.get("status") == "failed":
        return "failed_current", str(receipt.get("reason") or "processing_failed")
    if receipt.get("status") == "success":
        return "current_proven", "exact_versioned_receipt"
    return "unproven_current", "receipt_status_unregistered"


def code_hashes() -> dict[str, str]:
    return {name: hashlib.sha256((REPO / name).read_bytes()).hexdigest()
            for name in CODE_MODULES}


def build_baseline(*, rows: Sequence[Mapping[str, Any]], created_at: str,
                   target: Mapping[str, Any], eligibility_binding: Mapping[str, Any],
                   receipts: Mapping[int, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    receipts = receipts or {}
    counts = Counter({name: 0 for name in DISPOSITIONS})
    acquisition_counts = Counter({name: 0 for name in ACQUISITION_CLASSES})
    candidates: list[dict[str, Any]] = []
    projection: list[list[Any]] = []
    legacy_swept = 0
    legacy_swept_eligible = 0
    for row in rows:
        disposition, reason = classify(row, receipts.get(int(row["id"])))
        counts[disposition] += 1
        acquired = acquisition_class(row)
        acquisition_counts[acquired] += 1
        legacy_swept += int(row.get("swept_at") is not None)
        if disposition != "not_eligible" and row.get("swept_at") is not None:
            legacy_swept_eligible += 1
        identity = list(processing_identity(row))
        projection.append([int(row["id"]), acquired, disposition, reason, identity,
                           bool(row.get("swept_at"))])
        if disposition in ("unproven_current", "source_stale", "failed_current"):
            candidates.append({"supporting_doc_id": int(row["id"]),
                               "processing_identity": identity,
                               "disposition": disposition, "reason": reason,
                               "legacy_swept_at_present": row.get("swept_at") is not None})
    total = len(rows)
    body = {
        "kind": ARTIFACT_KIND, "version": PRODUCER_VERSION, "created_at": created_at,
        "mode": "read-only", "applied": False, "write_path": "absent by design",
        "target": dict(target), "code_hashes": code_hashes(),
        "eligibility_binding": dict(eligibility_binding),
        "identity": ["source_kind", "source_id", "content_sha256", "extraction_method",
                     "extractor", "extractor_version"],
        "extractor": EXTRACTOR, "extractor_version": EXTRACTOR_VERSION,
        "accounting": {"documents": total, "classified": sum(counts.values()),
                       "reconciles": total == sum(counts.values()),
                       "by_disposition": dict(sorted(counts.items())),
                       "legacy_swept_at_present": legacy_swept,
                       "legacy_swept_at_present_on_eligible": legacy_swept_eligible,
                       "versioned_receipts_present": len(receipts),
                       "requires_processing_proof": len(candidates),
                       "by_acquisition_provenance": dict(sorted(acquisition_counts.items())),
                       "acquisition_reconciles": total == sum(acquisition_counts.values())},
        "interpretation": (
            "Acquisition provenance and KG-processing proof are independent. Recorded "
            "scraper/text-pipeline evidence establishes programmatic acquisition; swept_at "
            "is a legacy processing hint only and never proves processing of an exact "
            "evidence/extractor/version identity. No re-download is implied."),
        "candidates": candidates, "candidates_sha256": canonical_sha256(candidates),
        "population_sha256": canonical_sha256(projection), "mutations_proposed": 0,
    }
    return {**body, "digest": canonical_sha256(body)}


def validate_current(artifact: Mapping[str, Any], *, rows: Sequence[Mapping[str, Any]],
                     target: Mapping[str, Any],
                     eligibility_binding: Mapping[str, Any],
                     receipts: Mapping[int, Mapping[str, Any]] | None = None) -> list[str]:
    """Rebuild the complete snapshot and reject any current-state or code drift."""
    expected = build_baseline(
        rows=rows, created_at=str(artifact.get("created_at")), target=target,
        eligibility_binding=eligibility_binding, receipts=receipts)
    return [] if dict(artifact) == expected else ["processing baseline differs from current state"]


def run(engine: Any, *, eligibility_path: Path, out_dir: Path,
        stamp: str | None = None) -> dict[str, Any]:
    if is_obsolete(eligibility_path):
        raise RuntimeError("eligibility artifact is obsolete")
    eligibility_artifact = load_verified(eligibility_path)
    target = assert_read_only_target(engine)
    if eligibility_artifact.get("target") != target:
        raise RuntimeError("eligibility artifact target differs from current target")
    statements = guard_engine(engine)
    with engine.connect() as connection:
        rows = [dict(row) for row in connection.execute(text(DOC_SQL)).mappings()]
    audit = statement_audit(statements)
    if not audit["select_only"]:
        raise RuntimeError(f"read-only audit failed: {audit}")
    created = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = build_baseline(
        rows=rows, created_at=created, target=target,
        eligibility_binding={"path": str(eligibility_path),
                             "digest": eligibility_artifact["digest"]})
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kg-stage3-processing-identity-{created}.json"
    digest = write_immutable(path, artifact)
    return {"status": "success", "path": str(path), "digest": digest,
            "accounting": artifact["accounting"], "read_only_audit": audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--eligibility", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    args = parser.parse_args(argv)
    print(json.dumps(run(get_engine(), eligibility_path=args.eligibility,
                         out_dir=args.out_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
