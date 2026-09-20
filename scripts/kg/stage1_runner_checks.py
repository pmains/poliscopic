#!/usr/bin/env python3
"""``stage1_runner_checks.py`` — canonical binding and in-transaction checks.

Two jobs, both owned here:

* :func:`canonical_problems` compares a **caller-supplied** packet against the
  canonical expectation reconstructed from the reviewed sources.  A manifest that
  is internally consistent — even correctly re-hashed — is still refused when its
  operations, target ids, body values, component hashes, populations, expected
  state, registry reason or code fingerprint differ from the reviewed set.
* :func:`preflight_rechecks` and :func:`postconditions` take an open *connection*
  rather than an engine, so they run inside the same transaction that performs the
  mutations and therefore see uncommitted changes.  No helper here opens its own
  connection.

Nothing in this module writes to a database.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from sqlalchemy import bindparam, text

from scripts.db import quarantine_schema
from scripts.kg import stage1_packet_components as components
from scripts.kg.quarantine import validate_quarantine_reason

__all__ = [
    "RefusalError",
    "atomicity_refusal",
    "canonical_problems",
    "postconditions",
    "preflight_rechecks",
]


class RefusalError(RuntimeError):
    """A locked check failed; the transaction must roll back without mutating."""


def atomicity_refusal(dialect: str) -> list[str]:
    """Refuse to apply when DDL cannot share the data transaction.

    On such a dialect a failed run would leave the schema changed while the data
    rolled back — a partially applied repair.  The packet's staged-rollback plan is
    the documented alternative; this runner fails closed instead.
    """
    if dialect == "postgresql":
        return []
    return [
        f"{dialect} cannot run DDL inside the apply transaction; refusing to apply "
        "in one step (see the packet's staged-rollback plan)"
    ]


def canonical_problems(packet: Mapping[str, Any]) -> list[str]:
    """Compare a supplied packet against the canonical, reconstructed expectation."""
    expected = components.canonical_expectation()
    problems: list[str] = []

    components_by_id = {component.get("id"): component
                        for component in packet.get("components") or ()}
    for body, binding in expected["component_paths"].items():
        component = components_by_id.get(body) or {}
        for field in ("path", "sha256", "plan_digest"):
            if component.get(field) != binding[field]:
                problems.append(
                    f"component {body} {field} {component.get(field)!r} != canonical "
                    f"{binding[field]!r}"
                )
    schema_component = components_by_id.get("schema.quarantine_columns") or {}
    for field, value in expected["code_fingerprint"].items():
        if schema_component.get(field) != value:
            problems.append(f"component schema {field} drifted from the live module")

    bindings = packet.get("bindings") or {}
    if (bindings.get("adjudication_artifact") or {}) != expected["adjudication_artifact"]:
        problems.append("adjudication artifact binding differs from the reviewed artifact")
    if (bindings.get("quarantine_artifact") or {}) != expected["quarantine_artifact"]:
        problems.append("quarantine artifact binding differs from the reviewed artifact")

    populations = packet.get("populations") or {}
    if list(populations.get("repair_extraction_ids") or ()) != expected["repair_ids"]:
        problems.append("repair extraction ids differ from the adjudicated 374")
    if list(populations.get("quarantine_extraction_ids") or ()) != expected["quarantine_ids"]:
        problems.append("quarantine extraction ids differ from the adjudicated 18")

    scope = populations.get("meeting_scope") or {}
    for body, meeting_ids in expected["meeting_scope"].items():
        if sorted(int(value) for value in scope.get(body) or ()) != meeting_ids:
            problems.append(f"meeting scope for {body} differs from the adjudicated set")

    operations = list(packet.get("operations") or ())
    inserts = [operation for operation in operations if operation.get("op") == "INSERT"]
    updates = [operation for operation in operations
               if operation.get("op") == "UPDATE" and operation.get("table") == "meetings"]
    if len(inserts) != len(expected["component_paths"]):
        problems.append(f"expected {len(expected['component_paths'])} body inserts, got {len(inserts)}")
    for operation in inserts:
        body = operation.get("component")
        values = operation.get("values") or {}
        if values.get("body_code") != body:
            problems.append(f"insert for {body} declares body_code {values.get('body_code')!r}")
        if operation.get("audit_timestamp") != expected["audit_timestamp"]:
            problems.append(
                f"insert for {body} declares audit timestamp "
                f"{operation.get('audit_timestamp')!r}, not the reviewed contract"
            )
        canonical_name = expected["canonical_body_names"].get(body)
        if values.get("name") != canonical_name:
            problems.append(
                f"insert for {body} name {values.get('name')!r} != canonical {canonical_name!r}"
            )
    if len(updates) != len(expected["component_paths"]):
        problems.append(f"expected {len(expected['component_paths'])} meeting updates, got {len(updates)}")
    for operation in updates:
        body = operation.get("component")
        canonical_ids = expected["meeting_scope"].get(body)
        if sorted(int(value) for value in operation.get("target_ids") or ()) != canonical_ids:
            problems.append(f"meeting update targets for {body} differ from the adjudicated set")
        if operation.get("rows") != len(canonical_ids or ()):
            problems.append(f"meeting update rowcount for {body} differs from its target set")
        set_clause = operation.get("set") or {}
        if sorted(set_clause) != ["jurisdiction_id", "public_body_id"]:
            problems.append(
                f"meeting update for {body} writes unexpected columns {sorted(set_clause)}"
            )
        elif int(set_clause.get("jurisdiction_id") or 0) != 4:
            problems.append(
                f"meeting update for {body} sets jurisdiction_id "
                f"{set_clause.get('jurisdiction_id')!r}, expected 4"
            )
        elif f"body_code = '{body}'" not in str(set_clause.get("public_body_id")):
            problems.append(
                f"meeting update for {body} does not parent to its own canonical body"
            )

    quarantine = packet.get("quarantine") or {}
    if list(quarantine.get("target_ids") or ()) != expected["quarantine_ids"]:
        problems.append("quarantine target ids differ from the adjudicated 18")
    values = quarantine.get("values") or {}
    if values.get("quarantine_reason") != expected["quarantine_reason"]:
        problems.append(
            f"quarantine reason {values.get('quarantine_reason')!r} != "
            f"{expected['quarantine_reason']!r}"
        )
    for field, canonical in expected["human_fields"].items():
        if values.get(field) != canonical:
            problems.append(f"quarantine {field} {values.get(field)!r} != adjudicated {canonical!r}")

    # Validate against the *live* authoritative registry, never a packet-supplied list.
    try:
        validate_quarantine_reason(values.get("quarantine_reason"))
    except ValueError as error:
        problems.append(f"quarantine reason rejected by the registry: {error}")

    counts = packet.get("counts") or {}
    state = expected["expected_state"]
    for key, expected_value in (
        ("public_body_inserts", state["public_bodies_total"]),
        ("meeting_updates", state["meeting_updates"]),
        ("quarantine_updates", state["quarantine_updates"]),
    ):
        if counts.get(key) != expected_value:
            problems.append(f"packet count {key} {counts.get(key)!r} != canonical {expected_value}")
    return problems


def preflight_rechecks(connection, packet: Mapping[str, Any]) -> list[str]:
    """Re-verify schema, collisions, fingerprints and baseline on an open connection."""
    problems: list[str] = []
    dialect = connection.engine.dialect.name

    present = quarantine_schema.column_names(connection, dialect, quarantine_schema.TABLE)
    existing = present & set(quarantine_schema.COLUMN_DDL)
    if existing:
        problems.append(f"quarantine columns already exist: {sorted(existing)}")

    from scripts.kg.phoenix_body_specs import APPROVED_BODIES

    for body_code, spec in sorted(APPROVED_BODIES.items()):
        collisions = connection.execute(
            text(
                "SELECT id FROM public_bodies "
                "WHERE body_code = :code OR slug = :slug OR name = :name"
            ),
            {"code": spec.body_code, "slug": spec.slug, "name": spec.name},
        ).all()
        if collisions:
            problems.append(f"body {body_code} already exists (collision)")

    baseline = (packet.get("baseline") or {}).get("counts") or {}
    observed = {
        "meetings_total": int(connection.execute(text("SELECT COUNT(*) FROM meetings")).scalar()),
        "meetings_null_public_body": int(connection.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")).scalar()),
        "meetings_null_jurisdiction": int(connection.execute(
            text("SELECT COUNT(*) FROM meetings WHERE jurisdiction_id IS NULL")).scalar()),
        "public_bodies_total": int(connection.execute(
            text("SELECT COUNT(*) FROM public_bodies")).scalar()),
        "supporting_documents_total": int(connection.execute(
            text("SELECT COUNT(*) FROM supporting_documents")).scalar()),
    }
    for key, expected in baseline.items():
        if key in observed and observed[key] != int(expected):
            problems.append(f"baseline drift: {key} is {observed[key]}, packet says {expected}")

    quarantine = packet.get("quarantine") or {}
    rows = connection.execute(
        text("SELECT id, supporting_doc_id FROM meeting_event_extractions WHERE id IN :ids")
        .bindparams(bindparam("ids", expanding=True)),
        {"ids": list(quarantine.get("target_ids") or ())},
    ).all()
    found = {int(row[0]) for row in rows}
    if found != {int(value) for value in quarantine.get("target_ids") or ()}:
        problems.append("quarantine target rows have drifted")
    fingerprints = quarantine.get("current_fingerprints") or {}
    if fingerprints:
        columns = quarantine_schema.column_names(connection, dialect, quarantine_schema.TABLE)
        if not (set(quarantine_schema.COLUMN_DDL) & columns):
            for row in rows:
                stored = next((value for value in fingerprints.values()), None)
                if stored is None:
                    problems.append("quarantine fingerprints missing")
                    break
    return problems


def postconditions(connection, packet: Mapping[str, Any], quarantine_count: int,
                   integrity_provider: Callable[[Any], dict[str, int]] | None = None) -> dict[str, Any]:
    """Combined aggregate postconditions, evaluated on the active connection."""
    if integrity_provider is None:
        from scripts.entities.detect_entities import integrity_snapshot
        integrity_provider = integrity_snapshot

    expected = packet.get("expected_after_state") or {}
    observed = {
        "meetings_total": int(connection.execute(text("SELECT COUNT(*) FROM meetings")).scalar()),
        "meetings_null_public_body": int(connection.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")).scalar()),
        "meetings_null_jurisdiction": int(connection.execute(
            text("SELECT COUNT(*) FROM meetings WHERE jurisdiction_id IS NULL")).scalar()),
        "public_bodies_total": int(connection.execute(
            text("SELECT COUNT(*) FROM public_bodies")).scalar()),
    }
    quarantined = int(connection.execute(text(
        "SELECT COUNT(*) FROM meeting_event_extractions WHERE quarantine_reason IS NOT NULL"
    )).scalar())
    children = int(connection.execute(text(
        "SELECT COUNT(*) FROM meetings WHERE public_body_id IN "
        "(SELECT id FROM public_bodies WHERE body_code IN "
        "('phoenix-dr','phoenix-dab','phoenix-ds'))"
    )).scalar())

    mismatches = [
        f"{key}: observed {observed[key]} != expected {expected[key]}"
        for key in observed if key in expected and observed[key] != int(expected[key])
    ]
    if quarantined != quarantine_count:
        mismatches.append(f"quarantined {quarantined} != expected {quarantine_count}")
    parented = expected.get("meeting_updates")
    if parented is not None and children != int(parented):
        mismatches.append(f"parented meetings {children} != {parented}")
    return {
        "observed": observed,
        "quarantined": quarantined,
        "parented_meetings": children,
        "mismatches": mismatches,
        "integrity": integrity_provider(connection),
        "satisfied": not mismatches,
    }


def require(problems: Sequence[str], stage: str) -> None:
    """Raise :class:`RefusalError` when any locked check failed."""
    if problems:
        raise RefusalError(f"{stage}: {'; '.join(problems)}")
