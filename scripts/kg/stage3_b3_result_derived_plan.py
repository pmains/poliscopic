#!/usr/bin/env python3
"""Read-only strict B3 result-derived agenda-item container plan.

This does not claim an official agenda was acquired.  It proposes a distinctly
labelled result-derived container only where one retained Meeting Result document
proves one unique numbered result item for a meeting that currently has no agenda
items.  Database writes are intentionally absent.
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
for value in (str(REPO), str(REPO / "scripts")):
    if value not in sys.path:
        sys.path.insert(0, value)

from sqlalchemy import bindparam, text  # noqa: E402
from db.core import get_engine  # noqa: E402
from scripts.entities.event_normalize_preflight import assert_read_only_target, guard_engine, statement_audit  # noqa: E402
from scripts.kg import stage3_b3_item_linkage_plan as linkage  # noqa: E402
from scripts.kg.stage2_artifacts import is_obsolete, load_verified, write_immutable  # noqa: E402

KIND = "kg-stage3-b3-result-derived-container-plan"
VERSION = "kg-stage3-b3-result-derived-container-plan/1.0"
LINKAGE_KIND = linkage.KIND
ENABLED = False
RESULT = linkage.RESULT_KIND
EVENT = linkage.EVENT_KIND
CODE_MODULES = (
    "scripts/kg/stage3_b3_result_derived_plan.py",
    "scripts/kg/stage3_b3_item_linkage_plan.py",
    "scripts/kg/stage3_meeting_result_identity.py",
    "scripts/kg/stage2_artifacts.py",
)
DOCUMENTS_SQL = """SELECT id, body, meeting_db_id, document_type, text_content,
 text_extraction_method, agenda_item_db_id FROM supporting_documents
 WHERE id IN :ids ORDER BY id"""
EVENTS_SQL = """SELECT id, supporting_doc_id, agenda_item_id, text_offset_start, text_offset_end
 FROM meeting_events WHERE supporting_doc_id IN :ids ORDER BY id"""
ITEMS_SQL = """SELECT id, meeting_db_id, agenda_item_number, agenda_item_id FROM agenda_items
 WHERE meeting_db_id IN :meetings ORDER BY meeting_db_id, id"""


class PlanRefused(RuntimeError):
    pass


def sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def text_sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def positive(value: Any) -> int | None:
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def code_hashes(repo: Path = REPO) -> dict[str, str]:
    output = {}
    for name in CODE_MODULES:
        path = repo / name
        if not path.is_file():
            raise PlanRefused(f"missing code module: {name}")
        output[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return output


def _identity(row: Mapping[str, Any]) -> tuple[int, str, int, int] | None:
    value = row.get("identity")
    if not isinstance(value, list) or len(value) != 4:
        return None
    doc, kind, start, end = value
    if positive(doc) is None or kind not in (RESULT, EVENT) or not isinstance(start, int) or not isinstance(end, int) or start < 0 or end <= start:
        return None
    return int(doc), kind, start, end


def load_linkage(path: str | Path) -> dict[str, Any]:
    artifact = Path(path)
    if is_obsolete(artifact):
        raise PlanRefused("linkage artifact is obsolete")
    plan = load_verified(artifact)
    accounting = plan.get("accounting") or {}
    if (plan.get("kind") != LINKAGE_KIND or plan.get("mode") != "dry-run" or plan.get("applied") is not False
            or plan.get("write_path") != "absent by design" or not isinstance(plan.get("dispositions"), list)
            or accounting.get("population") != len(plan["dispositions"]) or accounting.get("reconciles") is not True):
        raise PlanRefused("linkage artifact is not an exact unapplied B3 partition")
    return plan


def source_doc_ids(plan: Mapping[str, Any]) -> list[int]:
    values = []
    for row in plan["dispositions"]:
        ident = _identity(row)
        if ident is None:
            raise PlanRefused("linkage artifact has invalid span identity")
        values.append(ident[0])
    return sorted(set(values))


def capture_rows(connection: Any, *, doc_ids: Sequence[int]) -> dict[str, list[dict[str, Any]]]:
    ids = sorted({positive(x) for x in doc_ids if positive(x) is not None})
    if not ids:
        return {"documents": [], "events": [], "items": []}
    docs = [dict(x) for x in connection.execute(text(DOCUMENTS_SQL).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings()]
    meetings = sorted({positive(x.get("meeting_db_id")) for x in docs if positive(x.get("meeting_db_id")) is not None})
    return {
        "documents": docs,
        "events": [dict(x) for x in connection.execute(text(EVENTS_SQL).bindparams(bindparam("ids", expanding=True)), {"ids": ids}).mappings()],
        "items": ([dict(x) for x in connection.execute(text(ITEMS_SQL).bindparams(bindparam("meetings", expanding=True)), {"meetings": meetings}).mappings()] if meetings else []),
    }


def _hold(source_index: int, ident: tuple[int, str, int, int], reason: str) -> dict[str, Any]:
    return {"source_index": source_index, "identity": list(ident), "outcome": reason}


def build_plan(*, linkage_path: str | Path, rows: Mapping[str, Sequence[Mapping[str, Any]]],
               target: Mapping[str, Any], created_at: str, repo: Path = REPO) -> dict[str, Any]:
    upstream = load_linkage(linkage_path)
    docs = {positive(x.get("id")): dict(x) for x in rows.get("documents", []) if positive(x.get("id")) is not None}
    items_by_meeting: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for item in rows.get("items", []):
        meeting = positive(item.get("meeting_db_id"))
        if meeting is not None:
            items_by_meeting[meeting].append(dict(item))
    events: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for event in rows.get("events", []):
        doc, start, end = positive(event.get("supporting_doc_id")), event.get("text_offset_start"), event.get("text_offset_end")
        if doc is not None and isinstance(start, int) and isinstance(end, int):
            events[(doc, start, end)].append(dict(event))

    result_rows = [x for x in upstream["dispositions"] if _identity(x) and _identity(x)[1] == RESULT]
    event_rows = [x for x in upstream["dispositions"] if _identity(x) and _identity(x)[1] == EVENT]
    by_doc: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in result_rows:
        by_doc[_identity(row)[0]].append(row)
    containers, result_links, result_holds = [], [], []
    admitted: dict[int, list[tuple[tuple[int, int], dict[str, Any]]]] = defaultdict(list)
    for doc_id, source_rows in sorted(by_doc.items()):
        doc = docs.get(doc_id)
        identifiers = [_identity(x) for x in source_rows]
        valid = doc is not None and isinstance(doc.get("text_content"), str) and positive(doc.get("meeting_db_id")) is not None
        if not valid:
            result_holds.extend(_hold(x["source_index"], ident, "hold_source_gap") for x, ident in zip(source_rows, identifiers)); continue
        expected = {x.get("evidence", {}).get("text_sha256") for x in source_rows}
        if len(expected) != 1 or text_sha(doc["text_content"]) not in expected or any(x.get("evidence", {}).get("text_extraction_method") != doc.get("text_extraction_method") for x in source_rows):
            result_holds.extend(_hold(x["source_index"], ident, "hold_evidence_drift") for x, ident in zip(source_rows, identifiers)); continue
        meeting = positive(doc.get("meeting_db_id"))
        tokens = [x.get("evidence", {}).get("token") for x in source_rows]
        if (doc.get("document_type") != "Meeting Result" or positive(doc.get("agenda_item_db_id")) is not None
                or items_by_meeting.get(meeting) or any(not isinstance(t, str) or not t for t in tokens)):
            result_holds.extend(_hold(x["source_index"], ident, "hold_existing_or_invalid_container") for x, ident in zip(source_rows, identifiers)); continue
        if len(set(tokens)) != len(tokens):
            result_holds.extend(_hold(x["source_index"], ident, "hold_duplicate_token_document") for x, ident in zip(source_rows, identifiers)); continue
        for source, ident, token in zip(source_rows, identifiers, tokens):
            key = f"result_derived:{doc_id}:{text_sha(doc['text_content'])}:{token}"
            container = {"operation": "create_result_derived_container", "source_kind": "result_derived",
                         "source_key": key, "meeting_db_id": meeting, "body": doc.get("body"),
                         "agenda_item_number": token, "source_document_id": doc_id,
                         "content_sha256": text_sha(doc["text_content"]), "text_extraction_method": doc.get("text_extraction_method"),
                         "offsets": [ident[2], ident[3]], "token": token,
                         "reservation_key": [meeting, token], "source_index": source["source_index"]}
            containers.append(container)
            result_links.append({"source_index": source["source_index"], "identity": list(ident), "container_source_key": key, "outcome": "would_link_result_span"})
            admitted[doc_id].append(((ident[2], ident[3]), container))

    event_links, event_holds = [], []
    for source in event_rows:
        ident = _identity(source); doc_id, _, start, end = ident
        matching = events.get((doc_id, start, end), [])
        if len(matching) != 1:
            event_holds.append(_hold(source["source_index"], ident, "hold_event_lineage_drift")); continue
        if positive(matching[0].get("agenda_item_id")) is not None:
            event_holds.append(_hold(source["source_index"], ident, "hold_existing_event_container")); continue
        targets = [container for (left, right), container in admitted.get(doc_id, []) if left <= start < right]
        if len(targets) == 1:
            event_links.append({"source_index": source["source_index"], "identity": list(ident), "event_id": matching[0]["id"], "container_source_key": targets[0]["source_key"], "outcome": "would_link_event_span"})
        else:
            duplicate = any(_identity(x)[0] == doc_id for x in result_holds if x["outcome"] == "hold_duplicate_token_document")
            event_holds.append(_hold(source["source_index"], ident, "hold_duplicate_token_document" if duplicate else "hold_no_admitted_result_span"))
    all_results = result_links + result_holds
    all_events = event_links + event_holds
    if len(all_results) != len(result_rows) or len(all_events) != len(event_rows):
        raise PlanRefused("result or event accounting is incomplete")
    body = {"kind": KIND, "version": VERSION, "created_at": created_at, "mode": "dry-run", "applied": False,
            "write_path": "absent by design", "enabled": False, "target": dict(target),
            "bindings": {"linkage_plan": {"path": str(linkage_path), "digest": upstream["digest"]},
                         "source_projections": {key: sha(list(rows.get(key, []))) for key in ("documents", "events", "items")}, "code_hashes": code_hashes(repo)},
            "policy": {"container_label": "result_derived", "official_agenda_claim": False,
                       "requires": ["hash_matching_result_document", "positive_meeting_fk", "zero_existing_meeting_items", "unique_token_per_document"],
                       "forbids": ["fuzzy_match", "model_inference", "meeting_membership_link", "title_or_order_match"]},
            "containers": containers, "result_span_dispositions": sorted(all_results, key=lambda x: x["source_index"]),
            "event_span_dispositions": sorted(all_events, key=lambda x: x["source_index"]),
            "accounting": {"result_spans": len(result_rows), "result_linked": len(result_links), "result_held": len(result_holds),
                           "event_spans": len(event_rows), "event_linked": len(event_links), "event_held": len(event_holds),
                           "containers": len(containers), "reconciles": len(result_rows)==len(all_results) and len(event_rows)==len(all_events), "data_operations_proposed": 0},
            "future_apply_contract": {"authorization": "explicit human approval of exact plan path and digest", "target": "exact development PostgreSQL target only", "backup": "fresh protected backup plus isolated restore proof", "transaction": "one SERIALIZABLE transaction with reservation and collision rechecks", "receipt": "immutable O_EXCL mode-0600 receipt; only receipt-owned rows are mutable"},
            "future_rollback_contract": {"authority": "exact verified apply receipt", "operation": "remove only receipt-owned result_derived containers and span links", "refuse_if": ["dependent rows", "source or target drift", "reservation collision", "receipt mismatch"]}}
    return {**body, "digest": sha(body)}


def validate_plan(plan: Mapping[str, Any], *, linkage_path: str | Path, rows: Mapping[str, Sequence[Mapping[str, Any]],], repo: Path = REPO) -> list[str]:
    expected = build_plan(linkage_path=linkage_path, rows=rows, target=plan.get("target") or {}, created_at=str(plan.get("created_at")), repo=repo)
    return [] if dict(plan) == expected else ["plan differs from exact reconstruction"]


def run(engine: Any, *, linkage_path: str | Path, out_dir: Path, created_at: str | None = None) -> dict[str, Any]:
    target = assert_read_only_target(engine); upstream = load_linkage(linkage_path); statements = guard_engine(engine)
    with engine.connect() as connection:
        rows = capture_rows(connection, doc_ids=source_doc_ids(upstream))
    audit = statement_audit(statements)
    if audit.get("select_only") is not True: raise PlanRefused(f"read-only audit failed: {audit}")
    stamp = created_at or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    plan = build_plan(linkage_path=linkage_path, rows=rows, target=target, created_at=stamp)
    if validate_plan(plan, linkage_path=linkage_path, rows=rows): raise PlanRefused("generated plan failed reconstruction")
    out_dir.mkdir(parents=True, exist_ok=True); path = out_dir / f"kg-stage3-b3-result-derived-{stamp}.json"; digest = write_immutable(path, plan)
    return {"status":"success","path":str(path),"digest":digest,"accounting":plan["accounting"],"read_only_audit":audit}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--linkage", required=True, type=Path); parser.add_argument("--out-dir", default=REPO/"data"/"kg-plans", type=Path); parser.add_argument("--stamp")
    args = parser.parse_args(argv); print(json.dumps(run(get_engine(), linkage_path=args.linkage, out_dir=args.out_dir, created_at=args.stamp), sort_keys=True)); return 0


if __name__ == "__main__": raise SystemExit(main())
