#!/usr/bin/env python3
"""B4 read-only closeout for the adjudicated sentinel extraction quarantine."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO), str(REPO / "scripts")):  # pragma: no cover
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from sqlalchemy import bindparam, text  # noqa: E402

from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import (  # noqa: E402
    assert_read_only_target, guard_engine, statement_audit)
from scripts.entities.event_normalize_remediation import (  # noqa: E402
    ADJUDICATED_SENTINEL_IDS, SENTINEL_ADJUDICATOR, SENTINEL_DECISION_ID,
    SENTINEL_DOCUMENT_ID, SENTINEL_REASON, committed_dedup_evidence)
from scripts.kg.stage1_adjudication import ADJUDICATION  # noqa: E402
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

PRODUCER_VERSION = "kg-stage3-b4-quarantine-closeout/1.0"
ARTIFACT_KIND = "kg-stage3-b4-quarantine-closeout"
CODE_MODULES = (
    "scripts/kg/stage3_b4_quarantine_closeout.py",
    "scripts/entities/event_normalize_remediation.py",
    "scripts/kg/stage1_adjudication.py",
    "scripts/kg/quarantine.py",
    "scripts/kg/stage2_artifacts.py",
)

ROWS_SQL = """
SELECT x.id, x.supporting_doc_id, x.extractor, x.extractor_version,
       x.action_verb, x.raw_text, x.text_offset_start, x.text_offset_end,
       x.quarantine_reason, x.quarantined_at, x.quarantined_by,
       x.decision_id, x.model_version, d.body, d.meeting_db_id,
       d.document_title, d.document_url, d.text_extraction_method, d.text_content
FROM meeting_event_extractions x
LEFT JOIN supporting_documents d ON d.id = x.supporting_doc_id
WHERE x.id IN :ids
ORDER BY x.id
"""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    return {name: hashlib.sha256((repo / name).read_bytes()).hexdigest()
            for name in CODE_MODULES}


def capture_rows(connection: Any, ids: Sequence[int]) -> list[dict[str, Any]]:
    if not ids:
        return []
    query = text(ROWS_SQL).bindparams(bindparam("ids", expanding=True))
    return [dict(row) for row in connection.execute(query, {"ids": list(ids)}).mappings()]


def _row_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    retained = str(row.get("text_content") or "")
    start, end = row.get("text_offset_start"), row.get("text_offset_end")
    offsets_valid = (isinstance(start, int) and isinstance(end, int)
                     and 0 <= start < end <= len(retained))
    span = retained[start:end] if offsets_valid else ""
    return {
        "id": int(row["id"]), "supporting_doc_id": row.get("supporting_doc_id"),
        "extractor": row.get("extractor"), "extractor_version": row.get("extractor_version"),
        "action_verb": row.get("action_verb"), "text_offset_start": start,
        "text_offset_end": end, "offsets_valid": offsets_valid,
        "span_sha256": hashlib.sha256(span.encode()).hexdigest() if offsets_valid else None,
        "raw_text_sha256": hashlib.sha256(str(row.get("raw_text") or "").encode()).hexdigest(),
        "document_text_sha256": hashlib.sha256(retained.encode()).hexdigest(),
        "document_title": row.get("document_title"), "document_url": row.get("document_url"),
        "document_body": row.get("body"), "meeting_db_id": row.get("meeting_db_id"),
        "text_extraction_method": row.get("text_extraction_method"),
        "quarantine": {
            "reason": row.get("quarantine_reason"),
            "at": (str(row.get("quarantined_at"))
                   if row.get("quarantined_at") is not None else None),
            "by": row.get("quarantined_by"),
            "decision_id": row.get("decision_id"), "model_version": row.get("model_version"),
        },
        "disposition": "retain_permanent_quarantine",
    }


def build_closeout(*, rows: Sequence[Mapping[str, Any]], retired_ids: Sequence[int],
                   dedup_sources: Sequence[str], created_at: str,
                   target: Mapping[str, Any], hashes: Mapping[str, str]) -> dict[str, Any]:
    expected_all = set(ADJUDICATED_SENTINEL_IDS)
    retired = set(map(int, retired_ids)) & expected_all
    expected_survivors = expected_all - retired
    observed = {int(row["id"]): row for row in rows}
    problems: list[str] = []
    if set(observed) != expected_survivors:
        problems.append("observed rows are not the exact adjudicated survivor set")
    evidence = [_row_evidence(observed[row_id]) for row_id in sorted(observed)]
    for item in evidence:
        q = item["quarantine"]
        if item["supporting_doc_id"] != SENTINEL_DOCUMENT_ID:
            problems.append(f"row {item['id']} has wrong document lineage")
        if q["reason"] != SENTINEL_REASON or q["by"] != SENTINEL_ADJUDICATOR:
            problems.append(f"row {item['id']} has wrong quarantine authority")
        if q["decision_id"] != SENTINEL_DECISION_ID or q["at"] is None:
            problems.append(f"row {item['id']} has incomplete human decision")
        if not str(q["model_version"] or "").strip():
            problems.append(f"row {item['id']} lacks model version")
        if not item["offsets_valid"]:
            problems.append(f"row {item['id']} has invalid retained-text coordinates")
    body = {
        "kind": ARTIFACT_KIND, "version": PRODUCER_VERSION, "created_at": created_at,
        "mode": "read-only", "applied": False, "write_path": "absent by design",
        "target": dict(target), "code_hashes": dict(hashes),
        "authorization": {
            "decision_id": ADJUDICATION["decision_id"],
            "decision": ADJUDICATION["decision"],
            "adjudicator": ADJUDICATION["adjudicator"],
            "reason": ADJUDICATION["quarantine_reason"],
            "document_id": ADJUDICATION["document_id"],
            "meeting_identity": "rejected_not_a_meeting",
            "body_association": ADJUDICATION["body_level_association"],
        },
        "accounting": {
            "adjudicated": len(expected_all), "retired_by_dedup": len(retired),
            "surviving_quarantined": len(evidence),
            "reconciles": len(expected_all) == len(retired) + len(evidence),
        },
        "retired_ids": sorted(retired), "survivor_ids": sorted(observed),
        "dedup_evidence_sources": list(dedup_sources), "rows": evidence,
        "rows_sha256": canonical_sha256(evidence), "problems": problems,
        "verdict": "CLOSED_GOVERNED_EXCEPTION" if not problems else "REFUSED",
        "mutations_proposed": 0,
    }
    return {**body, "digest": canonical_sha256(body)}


def validate_current(artifact: Mapping[str, Any], *, rows: Sequence[Mapping[str, Any]],
                     retired_ids: Sequence[int], dedup_sources: Sequence[str],
                     target: Mapping[str, Any]) -> list[str]:
    expected = build_closeout(
        rows=rows, retired_ids=retired_ids, dedup_sources=dedup_sources,
        created_at=str(artifact.get("created_at")), target=target, hashes=code_hashes())
    return [] if dict(artifact) == expected else [
        "B4 artifact differs from the exact authoritative current rebuild"]


def run(engine: Any, *, out_dir: Path, stamp: str | None = None) -> dict[str, Any]:
    target = assert_read_only_target(engine)
    statements = guard_engine(engine)
    dedup = committed_dedup_evidence()
    if not dedup.get("complete"):
        raise RuntimeError("committed dedup evidence is incomplete")
    retired = sorted(set(dedup["retired_extraction_ids"]) & set(ADJUDICATED_SENTINEL_IDS))
    survivors = sorted(set(ADJUDICATED_SENTINEL_IDS) - set(retired))
    with engine.connect() as connection:
        rows = capture_rows(connection, survivors)
    audit = statement_audit(statements)
    if not audit["select_only"]:
        raise RuntimeError(f"read-only audit failed: {audit}")
    created = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact = build_closeout(
        rows=rows, retired_ids=retired, dedup_sources=dedup["sources"],
        created_at=created, target=target, hashes=code_hashes())
    if artifact["problems"]:
        raise RuntimeError("B4 closeout refused: " + "; ".join(artifact["problems"]))
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"kg-stage3-b4-quarantine-closeout-{created}.json"
    digest = write_immutable(path, artifact)
    return {"status": "success", "path": str(path), "digest": digest,
            "accounting": artifact["accounting"], "verdict": artifact["verdict"],
            "read_only_audit": audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, default=REPO / "data" / "kg-plans")
    args = parser.parse_args(argv)
    print(json.dumps(run(get_engine(), out_dir=args.out_dir), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
