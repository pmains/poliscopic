#!/usr/bin/env python3
"""Read-only Stage 0 graph schema and dev/production parity report."""

import hashlib
import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect, text

from scripts.kg import stage2_parentage_contract as parentage
from scripts.schema_scope import application_schema

load_dotenv()

GRAPH_TABLES = (
    "entities", "entity_types", "entity_mentions", "entity_relationships",
    "meeting_event_types", "meeting_events", "meeting_event_extractions",
    "event_participants",
    # Stage 2 container spine: meeting parentage must be parity-checked, not
    # merely synced.  Without this table a dev/prod divergence in
    # meetings.public_body_id is invisible to the parity report.
    "meetings",
    # Stage 2 document attachment: without this table a dev/prod divergence in
    # supporting_documents.agenda_item_db_id is invisible to the parity report.
    "supporting_documents",
)

REQUIRED_COLUMNS = {
    "entities": {"id", "entity_type", "normalized_name", "canonical_entity_id", "resolution_status"},
    "entity_types": {"id", "slug", "parent_slug", "entity_type", "display_name"},
    "entity_mentions": {"id", "entity_id", "source_type", "source_id", "extracted_by", "role_in_context"},
    "entity_relationships": {"id", "from_entity_id", "to_entity_id", "relationship", "provenance_type", "provenance_id", "edge_kind", "observed_at", "valid_from", "valid_to"},
    "meeting_events": {"id", "meeting_id", "supporting_doc_id", "agenda_item_id", "event_type_id", "outcome"},
    "meeting_event_types": {"id", "slug", "parent_slug", "event_type", "display_name"},
    "meeting_event_extractions": {"id", "meeting_event_id", "extractor", "extractor_version", "supporting_doc_id", "text_offset_start", "text_offset_end"},
    "event_participants": {"meeting_event_id", "entity_id", "role_in_event"},
    # Parentage columns are contracted in scripts/kg/stage2_parentage_contract.py;
    # presence is asserted here and type/nullability there, from one declaration.
    "meetings": {"id", "public_body_id", "jurisdiction_id"},
    # The source key is contracted to survive byte-for-byte; the canonical
    # identity is the additive column.  Both are asserted present.
    "supporting_documents": {"id", "agenda_item_id", "agenda_item_db_id"},
}


def schema_signature(engine) -> dict:
    inspector = inspect(engine)
    schema = application_schema(engine)
    available = set(inspector.get_table_names(schema=schema))
    result = {}
    for table in GRAPH_TABLES:
        if table not in available:
            result[table] = {"missing": True}
            continue
        columns = [{"name": c["name"], "type": str(c["type"]),
                    "nullable": c["nullable"], "default": str(c.get("default"))}
                   for c in inspector.get_columns(table, schema=schema)]
        result[table] = {
            "columns": columns,
            "pk": sorted(inspector.get_pk_constraint(table, schema=schema).get("constrained_columns") or []),
            "unique": sorted(sorted(u.get("column_names") or []) for u in inspector.get_unique_constraints(table, schema=schema)),
            "foreign_keys": sorted((tuple(f.get("constrained_columns") or []),
                                    f.get("referred_table"),
                                    tuple(f.get("referred_columns") or []))
                                   for f in inspector.get_foreign_keys(table, schema=schema)),
            # Names are deployment artifacts; compare index semantics.
            "indexes": sorted((bool(i.get("unique")),
                               tuple(i.get("column_names") or []))
                              for i in inspector.get_indexes(table, schema=schema)),
        }
    return result


def contract_violations(signature: dict) -> list[str]:
    problems = []
    for table, required in REQUIRED_COLUMNS.items():
        entry = signature.get(table, {})
        if entry.get("missing"):
            problems.append(f"missing table: {table}")
            continue
        actual = {c["name"] for c in entry.get("columns", [])}
        for column in sorted(required - actual):
            problems.append(f"missing column: {table}.{column}")
    # Type and nullability are governed by the parentage contract's single
    # declaration, so parity cannot drift from what the sync expects.
    problems.extend(parentage.column_problems(signature))
    return problems


def row_counts(engine) -> dict[str, int]:
    with engine.connect() as c:
        return {table: int(c.execute(text(f'SELECT count(*) FROM "{table}"')).scalar())
                for table in GRAPH_TABLES}


def digest(value: dict) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def signature_differences(left: dict, right: dict) -> dict:
    differences = {}
    for table in GRAPH_TABLES:
        sections = sorted(set(left[table]) | set(right[table]))
        changed = [section for section in sections
                   if left[table].get(section) != right[table].get(section)]
        if changed:
            differences[table] = changed
    return differences


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--details", action="store_true", help="include full normalized signatures")
    args = parser.parse_args()
    dev_url = os.environ.get("DATABASE_URL")
    prod_url = os.environ.get("PROD_DATABASE_URL")
    if not dev_url or not prod_url:
        raise SystemExit("DATABASE_URL and PROD_DATABASE_URL are required")
    dev = create_engine(dev_url, pool_pre_ping=True)
    prod = create_engine(prod_url, pool_pre_ping=True)
    dev_sig, prod_sig = schema_signature(dev), schema_signature(prod)
    dev_counts, prod_counts = row_counts(dev), row_counts(prod)
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "authority": "scripts/db/kg_integrity_schema.py (Stage 0 additive graph schema authority)",
        "dev_schema_sha256": digest(dev_sig),
        "prod_schema_sha256": digest(prod_sig),
        "schema_match": dev_sig == prod_sig,
        "contract_violations": contract_violations(dev_sig),
        "dev_counts": dev_counts,
        "prod_counts": prod_counts,
        "count_deltas": {t: prod_counts[t] - dev_counts[t] for t in GRAPH_TABLES},
        "counts_match": dev_counts == prod_counts,
        "schema_table_matches": {t: dev_sig[t] == prod_sig[t] for t in GRAPH_TABLES},
        "schema_differences": signature_differences(dev_sig, prod_sig),
    }
    if args.details:
        report["dev_schema"] = dev_sig
        report["prod_schema"] = prod_sig
    print(json.dumps(report, indent=2, sort_keys=True))
    if not report["schema_match"] or report["contract_violations"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
