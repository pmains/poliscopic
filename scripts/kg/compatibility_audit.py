"""Read-only Stage 1 compatibility audit (Brief 018 Step 2).

Classifies every distinct current value and every producer-declared value as
canonical, compatibility-mapped, quarantined, or unmapped; detects predicate
direction/domain/range violations; distinguishes taxonomy membership from
allowed leaf emission; and inventories agenda evidence lineage.

This module performs SELECTs only.  It writes a JSON artifact and a log; it
never rewrites graph rows.  The query sections it orchestrates live in
:mod:`scripts.kg.audit_sections`.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from sqlalchemy.engine import Engine

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import producer_vocabulary as producers
from scripts.kg import registries as r
from scripts.kg.audit_sections import (
    audit_agenda_lineage,
    audit_entity_types,
    audit_events,
    audit_provenance,
    audit_relationships,
    audit_roles,
)

log = logging.getLogger("kg.compatibility_audit")
PHOENIX_TZ = ZoneInfo("America/Phoenix")

#: Declared producer categories mapped to the registry category that governs
#: them.  ``None`` marks raw evidence text that has no registry vocabulary.
DECLARED_CATEGORY_MAP: Mapping[str, str | None] = {
    "entity_type": "entity_type",
    "event_type": "event_type",
    "outcome": "outcome",
    "relationship": "relationship",
    "role": "role",
    "procedure_outcome": None,
}

def audit_producer_declarations() -> dict[str, Any]:
    """Classify vocabulary imported from the real producer modules.

    Declared values are read from the producers themselves (not a duplicated
    list), so newly declared vocabulary surfaces as an unmapped value.
    """
    declared = producers.declared_vocabulary()
    sections: dict[str, Any] = {}
    unmapped: dict[str, list[str]] = {}
    for category in sorted(declared):
        registry_category = DECLARED_CATEGORY_MAP.get(category)
        values: dict[str, list[str]] = {}
        for declaration in declared[category]:
            values.setdefault(declaration.value, []).append(declaration.source)
        records = []
        for value, sources in sorted(values.items()):
            if registry_category is None:
                records.append({
                    "value": value, "sources": sorted(set(sources)),
                    "status": "raw_action_text",
                })
                continue
            normalized = (
                r.normalize_event_slug(value) if registry_category == "event_type"
                else value
            )
            status = r.classify_value(registry_category, normalized)
            records.append({
                "value": value,
                "normalized": normalized,
                "sources": sorted(set(sources)),
                "status": status,
            })
            if status == "unmapped":
                unmapped.setdefault(category, []).append(value)
        sections[category] = {"records": records, "unmapped": unmapped.get(category, [])}
    return {
        "category": "producer_declarations",
        "inspected": sorted({
            declaration.source.split(".")[0]
            for declarations in declared.values() for declaration in declarations
        }),
        "skipped": dict(producers.INTROSPECTION_LIMITS),
        "sections": sections,
        "unmapped": unmapped,
    }


def audit(engine: Engine, *, sample_limit: int = 5) -> dict[str, Any]:
    """Run the complete read-only compatibility audit."""
    sections = {
        "entity_type": audit_entity_types(engine, sample_limit),
        "role": audit_roles(engine, sample_limit),
        "relationship": audit_relationships(engine, sample_limit),
        "event": audit_events(engine, sample_limit),
        "provenance": audit_provenance(engine, sample_limit),
        "agenda_evidence_lineage": audit_agenda_lineage(engine, sample_limit),
        "producer_declarations": audit_producer_declarations(),
    }
    unmapped: dict[str, list[str]] = {}
    for key, section in sections.items():
        if isinstance(section, dict) and section.get("unmapped"):
            unmapped[key] = list(section["unmapped"])
        for flag in ("unmapped_outcomes", "unregistered_event_types"):
            if section.get(flag):
                unmapped[f"{key}.{flag}"] = list(section[flag])

    return {
        "artifact_kind": "kg_stage1_compatibility_audit",
        "model_version": r.MODEL_VERSION,
        "registry_snapshot_sha256": r.snapshot_sha256(),
        "generated_at": datetime.now(PHOENIX_TZ).isoformat(),
        "sample_limit": sample_limit,
        "sections": sections,
        "unmapped": unmapped,
        "audit_passed_no_unmapped": not unmapped,
    }


def _write_artifact(document: Mapping[str, Any], path: Path) -> None:
    """Persist the audit artifact atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, indent=2, sort_keys=True, default=str)
    temporary.replace(path)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point for the read-only compatibility audit."""
    parser = argparse.ArgumentParser(description="Read-only Stage 1 registry compatibility audit")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--sample-limit", type=int, default=5)
    arguments = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    from db import get_engine

    timestamp = datetime.now(PHOENIX_TZ).strftime("%Y%m%d-%H%M%S")
    output = arguments.output or (
        _REPO_ROOT / "data" / f"kg-stage1-compatibility-audit-{timestamp}.json"
    )
    document = audit(get_engine(), sample_limit=arguments.sample_limit)
    _write_artifact(document, output)

    sections = document["sections"]
    log.info("model %s · registry %s", document["model_version"],
             document["registry_snapshot_sha256"][:12])
    log.info("entity types: %s", sections["entity_type"]["counts"])
    log.info("roles: %s unmapped=%s", len(sections["role"]["mention_roles"]),
             sections["role"]["unmapped"])
    log.info("relationships: %s direction violations=%s",
             len(sections["relationship"]["predicates"]),
             sections["relationship"]["direction_violation_count"])
    log.info("outcomes unmapped=%s", sections["event"]["unmapped_outcomes"])
    log.info("agenda lineage: %s", sections["agenda_evidence_lineage"]["agenda_items"])
    log.info("artifact %s", output)
    return 0 if document["audit_passed_no_unmapped"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
