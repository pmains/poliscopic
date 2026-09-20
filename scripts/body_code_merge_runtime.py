"""Fail-closed runtime for merging split public-body identities.

The application and scrapers use the short ``body`` codes.  Registry rows that
were changed to long slugs split meetings into two identities.  This module
keeps the short code, merges the long-code rows into it, and preserves internal
meeting/agenda-item references before deleting exact duplicate containers.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from sqlalchemy import inspect, text

from db.tier import PRODUCTION_PRIMARY_HOST


MERGES = {
    "chandler-planning-zoning-commission": "chandler-pz",
    "mesa-planning-zoning": "mesa-pz",
}

# ── Target binding (Brief 031 section C) ────────────────────────────────
# A plan is bound to (a) the exact mutation code, (b) the target identity,
# and (c) its own digest.  Guards fail closed in BOTH directions so a
# production plan can never run against development and vice versa.
SCRATCH_PREFIX = "poliscopic_merge_scratch"

DEV_TARGET = {"tier": "development", "database": "poliscopic_dev"}

# `poliscopic` is the DigitalOcean managed production database.  The host is
# pinned so a same-named database on another server cannot be mutated.
PRODUCTION_TARGET = {
    "tier": "production",
    "database": "poliscopic",
    "host": PRODUCTION_PRIMARY_HOST,
}

# Exactly the modules that can mutate rows.  A reviewed plan is bound to the
# bytes of every one of them.
MUTATION_MODULES = (
    "body_code_merge.py",
    "body_code_merge_runtime.py",
    "body_code_merge_prod.py",
)

# Plan keys that describe WHERE a plan runs, not WHAT it does.  Stripping them
# lets a scratch run be compared to the production plan on content alone.
# Plan keys that describe WHERE a plan runs, not WHAT it does.  These are the
# only fields a content digest may ignore, and only because scratch and
# production legitimately differ on them (Brief 031D item 1).  The baseline is
# deliberately NOT in this set: dropping it would let a plan be applied against
# data it was never proven on.
LOCATION_ONLY_KEYS = ("tier", "target", "target_identity", "digest")

# Historical alias.
TARGET_BINDING_KEYS = LOCATION_ONLY_KEYS

# Polymorphic references that can point at a meeting or an agenda item.  The
# permitted type values are declared explicitly so an unknown class is REFUSED
# rather than silently left unreparented (Brief 031D item 4/5).  Values observed
# in development: entity_mentions.source_type has no 'meeting' form at all,
# entity_relationships uses the plural 'meetings', and its source_type is
# uniformly NULL.
POLYMORPHIC_REFERENCES = (
    {"table": "entity_mentions", "type_column": "source_type",
     "id_column": "source_id",
     "targets": {"agenda_item": "agenda_items"}},
    {"table": "entity_relationships", "type_column": "provenance_type",
     "id_column": "provenance_id",
     "targets": {"agenda_item": "agenda_items", "meetings": "meetings"}},
    # Declared explicitly (Brief 031E item 2).  Uniformly NULL in development,
    # so any future non-null value is an undeclared class and must refuse.
    {"table": "entity_relationships", "type_column": "source_type",
     "id_column": "source_id", "targets": {}},
)

KNOWN_POLYMORPHIC_TYPES = {
    ("entity_mentions", "source_type"): frozenset({
        "supporting_document", "agenda_item", "meeting_member",
        "pz_item_detail", "body_membership"}),
    ("entity_relationships", "provenance_type"): frozenset({
        "entity_resolution", "meeting_member", "agenda_item",
        "pz_item_detail", "meetings", "public_bodies", "body_membership"}),
    ("entity_relationships", "source_type"): frozenset(),
}

# The event contract (Brief 031D item 6): meeting_events.meeting_id holds the
# EXTERNAL meeting source id, never meetings.id.  The merge must never rewrite
# it to a surviving database PK.
EVENT_TABLE = "meeting_events"
EVENT_MEETING_COLUMN = "meeting_id"

ITEM_REFERENCE_COLUMNS = (
    ("agenda_items", "parent_item_id"),
    ("agenda_item_votes", "agenda_item_id"),
    ("case_events", "agenda_item_id"),
    ("meeting_events", "agenda_item_id"),
    ("pz_item_details", "agenda_item_id"),
    ("supporting_documents", "agenda_item_db_id"),
)

# These are the only non-FK columns whose names unambiguously identify a
# database identity.  ``supporting_documents.agenda_item_id`` is deliberately
# absent: it is a source-system key, not ``agenda_items.id``.  Ambiguous
# ``agenda_item_id`` columns are refused unless they are in the legacy set
# below or have a live FK to agenda_items.
SAFE_ITEM_REFERENCE_COLUMNS = {
    ("agenda_items", "parent_item_id"),
    ("agenda_item_votes", "agenda_item_id"),
    ("case_events", "agenda_item_id"),
    ("meeting_events", "agenda_item_id"),
    ("pz_item_details", "agenda_item_id"),
    ("supporting_documents", "agenda_item_db_id"),
}

SAFE_NON_REFERENCE_COLUMNS = {
    # Source-system keys, not database identities.
    ("agenda_items", "agenda_item_id"),
    ("supporting_documents", "agenda_item_id"),
}

SAFE_MEETING_REFERENCE_COLUMNS = {
    (table, "meeting_db_id") for table in (
        "agenda_items", "agenda_item_votes", "case_events",
        "executive_session_participants", "meeting_attendance",
        "meeting_members", "pz_item_details", "supporting_documents",
    )
}
SAFE_MEETING_REFERENCE_COLUMNS |= {
    ("agenda_item_key_reservation", "meeting_db_id"),
}

SAFE_EXTERNAL_MEETING_COLUMNS = {
    (table, "meeting_id") for table in (
        "meetings", "agenda_items", "agenda_item_votes", "case_events",
        "executive_session_participants", "meeting_attendance",
        "meeting_members", "meeting_events", "pz_item_details",
        "supporting_documents", "_ingest_failures", "article_sources",
        "dismissed_suggestions", "scanned_agenda_text",
    )
}

SAFE_VOTE_REFERENCE_COLUMNS = {("member_votes", "agenda_item_vote_id")}

MEETING_DB_TABLES = (
    "agenda_item_votes", "case_events", "executive_session_participants",
    "meeting_attendance", "meeting_members", "pz_item_details",
    "supporting_documents",
)

PROTECTED_TABLES = tuple(sorted(set(
    ("meetings", "agenda_items", "agenda_item_key_reservation", "meeting_events",
     "entity_mentions")
    + MEETING_DB_TABLES + tuple(t for t, _ in ITEM_REFERENCE_COLUMNS)
)))

# KG-era tables that exist in development but NOT in production.  The merge
# must introspect their exact presence, bind it into the plan, and touch them
# only when present.  They are deliberately NOT created in production
# (Brief 031 §C).
OPTIONAL_TABLES = ("agenda_item_key_reservation", "_pattern_cascade_watermark")

# Columns the merge READS or WRITES that exist in development but NOT in
# production.  Same treatment as OPTIONAL_TABLES: introspect, bind into the
# plan, and touch only when present.
OPTIONAL_COLUMNS = (
    ("agenda_items", "parent_item_id"),
    ("supporting_documents", "agenda_item_db_id"),
)


def table_presence(connection: Any, names: tuple[str, ...]) -> dict[str, bool]:
    rows = connection.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = current_schema()
    """)).fetchall()
    present = {r[0] for r in rows}
    return {name: name in present for name in names}


def column_presence(connection: Any,
                    pairs: tuple[tuple[str, str], ...]) -> dict[str, bool]:
    rows = connection.execute(text("""
        SELECT table_name, column_name FROM information_schema.columns
        WHERE table_schema = current_schema()
    """)).fetchall()
    present = {(t, c) for t, c in rows}
    return {f"{t}.{c}": (t, c) in present for t, c in pairs}


def table_column_signature(connection: Any, table: str) -> list[list[str]]:
    """Ordered column signature for a table.

    Scoped to ``current_schema()`` deliberately: production carries the same
    table name in more than one schema (a ``dev`` schema behind a foreign data
    wrapper), and an unscoped ``information_schema`` read made the signature
    nondeterministic.
    """
    rows = connection.execute(text("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = :t
        ORDER BY column_name
    """), {"t": table}).fetchall()
    return [[str(c) for c in r] for r in rows]


def schema_capabilities(connection: Any) -> dict[str, Any]:
    """Bind presence AND relevant column signatures of the optional objects.

    Presence alone is not enough: a table could exist with a different shape,
    which would make the merge's statements wrong even though the table is
    there (Brief 031D item 3).
    """
    tables = table_presence(connection, OPTIONAL_TABLES)
    return {
        "optional_tables": tables,
        "optional_columns": column_presence(connection, OPTIONAL_COLUMNS),
        "optional_table_signatures": {
            name: (table_column_signature(connection, name)
                   if tables.get(name) else None)
            for name in OPTIONAL_TABLES
        },
    }


def _has(capabilities: dict[str, Any], table: str) -> bool:
    return bool((capabilities.get("optional_tables") or {}).get(table))


def _has_column(capabilities: dict[str, Any], table: str,
                column: str) -> bool:
    return bool((capabilities.get("optional_columns") or {})
                .get(f"{table}.{column}", True))


def assert_capabilities(connection: Any, plan: dict[str, Any]) -> dict[str, Any]:
    """Fail closed if the live schema drifted from what the plan was built on."""
    bound = plan.get("capabilities")
    if not bound:
        raise RuntimeError("refusing: plan records no schema capabilities")
    live = schema_capabilities(connection)
    if live != bound:
        raise RuntimeError(
            f"refusing: schema drift between plan and live target: "
            f"plan={bound} live={live}")
    return live

UNIQUE_BODY_TABLES = {
    "meeting_members": ("member_id",),
    "meeting_attendance": ("member_id",),
    "executive_session_participants": ("normalized_name", "agenda_item_number"),
}

IGNORED_TWIN_FIELDS = {
    "id", "body", "meeting_db_id", "created_at", "updated_at",
}


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def code_hashes() -> dict[str, str]:
    """Bind a reviewed plan to the exact mutation code that will execute it.

    Every module in ``MUTATION_MODULES`` must exist — a missing one is a hard
    failure rather than a silently shorter hash set, so a plan can never claim
    to be bound to code that was not hashed.
    """
    here = Path(__file__).resolve()
    out: dict[str, str] = {}
    for name in MUTATION_MODULES:
        path = here if name == here.name else here.with_name(name)
        if not path.is_file():
            raise RuntimeError(f"mutation module missing, cannot bind plan: {name}")
        out[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def semantic_text(value: Any) -> str:
    value = html.unescape(str(value or ""))
    value = re.sub(r"\s*/\s*", "/", value)
    value = re.sub(r"\b(\d+)\s+(st|nd|rd|th)\b", r"\1\2", value,
                   flags=re.IGNORECASE)
    return " ".join(value.split())


def compatible_destiny_urls(detail: Any, agenda: Any) -> bool:
    """Recognize item-detail and parent-agenda URLs for the same meeting."""
    if str(detail or "") == str(agenda or ""):
        return True
    left, right = urlparse(str(detail or "")), urlparse(str(agenda or ""))
    if left.netloc != "public.destinyhosted.com" or left.netloc != right.netloc:
        return False
    lq, rq = parse_qs(left.query), parse_qs(right.query)
    return (lq.get("dsp") == ["agm"] and rq.get("dsp") == ["ag"]
            and lq.get("ag") == rq.get("seq"))


def _columns(connection: Any, table: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(text("""
        SELECT column_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND table_name=:table
        ORDER BY ordinal_position
    """), {"table": table}).fetchall()]


def _body_tables(connection: Any) -> list[str]:
    rows = connection.execute(text("""
        SELECT table_name FROM information_schema.columns
        WHERE table_schema=current_schema() AND column_name='body'
        ORDER BY table_name
    """))
    return [r[0] for r in rows]


def target_identity(connection: Any) -> dict[str, Any]:
    """Read-only identity of the live connection, for plan binding.

    Host/port come from the connection's URL when available, because
    ``inet_server_addr()`` reports the resolved IP rather than the configured
    hostname — and the production guard must match the configured host.
    """
    name = str(connection.execute(text("select current_database()")).scalar_one())
    engine = getattr(connection, "engine", None)
    url = getattr(engine, "url", None)
    host = getattr(url, "host", None) if url is not None else None
    port = getattr(url, "port", None) if url is not None else None
    if not host:
        try:
            raw_host = connection.execute(text("select inet_server_addr()")).scalar()
            host = str(raw_host) if raw_host is not None else ""
        except Exception:  # pragma: no cover - driver-specific fallback
            host = ""
    if not port:
        try:
            raw_port = connection.execute(text("select inet_server_port()")).scalar()
            port = int(raw_port) if raw_port is not None else 0
        except Exception:  # pragma: no cover - driver-specific fallback
            port = 0
    version = str(connection.execute(
        text("select current_setting('server_version')")).scalar_one())
    dialect = getattr(getattr(engine, "dialect", None), "name", "") or ""
    driver = getattr(getattr(engine, "dialect", None), "driver", "") or ""
    # A stable cluster identity, when the role may read it.  Two clusters can
    # share a database name and version, so this is the strongest available
    # "is this the same server" evidence (Brief 031D item 2).
    cluster = ""
    try:
        cluster = str(connection.execute(
            text("select system_identifier from pg_control_system()")).scalar_one())
    except Exception:
        cluster = ""
    return {"database": name, "host": str(host or ""), "port": int(port or 0),
            "server_version": version, "dialect": str(dialect),
            "driver": str(driver), "cluster_identity": cluster}


def reference_inventory(connection: Any) -> dict[str, Any]:
    """Inventory every reference the merge depends on (Brief 031D item 4).

    Deterministic and dynamic: discovered from the catalog, not hand-listed, so
    a new table or column is either covered or explicitly refused.  Anything
    that looks like a meeting/item reference but is not in a known class raises.
    """
    body_tables = _body_tables(connection)

    # FK (child_table, column) -> parent table, for meeting/agenda_items only.
    fks = connection.execute(text("""
        SELECT child.relname, child_att.attname, parent.relname,
               parent_att.attname
        FROM pg_constraint constraint_row
        JOIN pg_class child ON child.oid = constraint_row.conrelid
        JOIN pg_namespace child_ns ON child_ns.oid = child.relnamespace
        JOIN pg_class parent ON parent.oid = constraint_row.confrelid
        JOIN pg_namespace parent_ns ON parent_ns.oid = parent.relnamespace
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
             AS child_key(attnum, position) ON true
        JOIN LATERAL unnest(constraint_row.confkey) WITH ORDINALITY
             AS parent_key(attnum, position)
          ON parent_key.position = child_key.position
        JOIN pg_attribute child_att
          ON child_att.attrelid = child.oid
         AND child_att.attnum = child_key.attnum
        JOIN pg_attribute parent_att
          ON parent_att.attrelid = parent.oid
         AND parent_att.attnum = parent_key.attnum
        WHERE constraint_row.contype = 'f'
          AND child_ns.nspname = current_schema()
          AND parent_ns.nspname = current_schema()
          AND parent.relname IN ('meetings', 'agenda_items', 'agenda_item_votes')
        ORDER BY child.relname, child_key.position
    """)).fetchall()
    natural_key_fks = [list(r) for r in fks if str(r[3]) != "id"]
    if natural_key_fks:
        raise RuntimeError(
            f"refusing: natural-key FK(s) require explicit non-mutation "
            f"classification: {natural_key_fks}")
    meeting_fks = [[r[0], r[1], r[2], r[3]] for r in fks
                   if r[2] in ("meetings", "agenda_items")]

    # Build the mutation inventory from the live catalog.  The historical
    # tuples remain compatibility fixtures, but are no longer the execution
    # source of truth.  FK-backed references are always safe.  A named column
    # without an FK is either a known legacy identity or an explicit refusal;
    # silently guessing would be worse than blocking a new schema.
    all_columns = connection.execute(text("""
        SELECT table_name, column_name
        FROM information_schema.columns
        WHERE table_schema = current_schema()
        ORDER BY table_name, ordinal_position
    """)).fetchall()
    columns_by_table: dict[str, set[str]] = {}
    for table, column in all_columns:
        columns_by_table.setdefault(str(table), set()).add(str(column))
    fk_refs = {(str(r[0]), str(r[1]), str(r[2]), str(r[3])) for r in fks}
    meeting_references: set[tuple[str, str]] = {
        (table, column) for table, column, parent, parent_column in fk_refs
        if parent == "meetings" and parent_column == "id"
    }
    item_references: set[tuple[str, str]] = {
        (table, column) for table, column, parent, parent_column in fk_refs
        if parent == "agenda_items" and parent_column == "id"
    }
    vote_references: set[tuple[str, str]] = {
        (table, column) for table, column, parent, parent_column in fk_refs
        if parent == "agenda_item_votes" and parent_column == "id"
    }
    for table, columns in columns_by_table.items():
        if "meeting_db_id" in columns:
            candidate = (table, "meeting_db_id")
            if candidate in SAFE_MEETING_REFERENCE_COLUMNS or any(
                    t == table and c == "meeting_db_id" and p == "meetings"
                    for t, c, p, pc in fk_refs if pc == "id"):
                meeting_references.add(candidate)
            else:
                raise RuntimeError(
                    f"refusing: ambiguous meeting_db_id reference "
                    f"{table}.meeting_db_id; add an FK or declare it")
        for column in ("agenda_item_db_id", "parent_item_id"):
            if column in columns:
                candidate = (table, column)
                if candidate in SAFE_ITEM_REFERENCE_COLUMNS or any(
                        t == table and c == column and p == "agenda_items"
                        for t, c, p, pc in fk_refs if pc == "id"):
                    item_references.add(candidate)
                else:
                    raise RuntimeError(
                        f"refusing: ambiguous agenda-item reference "
                        f"{table}.{column}; add an FK or declare it")
        if "agenda_item_vote_id" in columns:
            candidate = (table, "agenda_item_vote_id")
            if candidate in SAFE_VOTE_REFERENCE_COLUMNS or any(
                    t == table and c == "agenda_item_vote_id" and
                    p == "agenda_item_votes" for t, c, p, pc in fk_refs
                    if pc == "id"):
                vote_references.add(candidate)
            else:
                raise RuntimeError(
                    f"refusing: ambiguous agenda_item_vote_id reference "
                    f"{table}.agenda_item_vote_id; add an FK or declare it")
        if "agenda_item_id" in columns:
            candidate = (table, "agenda_item_id")
            if candidate in SAFE_ITEM_REFERENCE_COLUMNS or any(
                    t == table and c == "agenda_item_id" and p == "agenda_items"
                    for t, c, p, pc in fk_refs if pc == "id"):
                item_references.add(candidate)
            elif candidate in SAFE_NON_REFERENCE_COLUMNS:
                pass
            else:
                raise RuntimeError(
                    f"refusing: ambiguous agenda_item_id reference "
                    f"{table}.agenda_item_id; add an FK or declare it")
        if "meeting_id" in columns:
            candidate = (table, "meeting_id")
            meeting_fk = any(
                t == table and c == "meeting_id" and p == "meetings"
                for t, c, p, pc in fk_refs if pc == "id")
            if candidate in SAFE_EXTERNAL_MEETING_COLUMNS and meeting_fk:
                raise RuntimeError(
                    f"refusing: contradictory meeting_id semantics for "
                    f"{table}.meeting_id; declared external but FK targets meetings")
            if candidate not in SAFE_EXTERNAL_MEETING_COLUMNS and not meeting_fk:
                raise RuntimeError(
                    f"refusing: ambiguous meeting_id reference "
                    f"{table}.meeting_id; declare source-vs-database semantics")
    # Every declared ``meeting_id`` above is the scraper/source-system key,
    # never ``meetings.id``.  Internal database references use
    # ``meeting_db_id``.  Keep that contract authoritative even if a future
    # migration accidentally adds an FK-shaped constraint to an external key.
    # A new undeclared meeting_id column still refuses in the loop above.
    meeting_references.difference_update(SAFE_EXTERNAL_MEETING_COLUMNS)

    # Unique constraints that could collide during a merge.
    unique_column_rows = connection.execute(text("""
        SELECT relation.relname, constraint_row.conname,
               pg_get_constraintdef(constraint_row.oid),
               array_agg(attribute.attname ORDER BY key.position)
        FROM pg_constraint constraint_row
        JOIN pg_class relation ON relation.oid=constraint_row.conrelid
        JOIN pg_namespace relation_ns ON relation_ns.oid=relation.relnamespace
        JOIN LATERAL unnest(constraint_row.conkey) WITH ORDINALITY
             AS key(attnum, position) ON true
        JOIN pg_attribute attribute
          ON attribute.attrelid=relation.oid AND attribute.attnum=key.attnum
        WHERE constraint_row.contype IN ('u','p')
          AND relation_ns.nspname=current_schema()
        GROUP BY relation.relname, constraint_row.conname, constraint_row.oid
        ORDER BY relation.relname, constraint_row.conname
    """)).fetchall()
    # Exact catalog columns are authoritative.  Constraint text is retained
    # only for human-readable evidence; substring matching would incorrectly
    # classify e.g. public_body_id as a column named body.
    unique_constraints = [[r[0], r[1], r[2]] for r in unique_column_rows
                          if "body" in set(r[3])]

    # Reference columns by name, whether or not a FK enforces them.  A declared
    # reference table that is absent must be skipped, not crash the inventory
    # (Brief 031E item 3: derive from the live catalog).
    reference_columns: dict[str, list[str]] = {}
    candidates = set(body_tables) | {t for t, _ in ITEM_REFERENCE_COLUMNS}
    presence = table_presence(connection, tuple(sorted(candidates)))
    for table in sorted(candidates):
        if not presence.get(table):
            continue
        cols = [c for c in _columns(connection, table)
                if c in ("meeting_id", "meeting_db_id", "agenda_item_id",
                         "agenda_item_db_id", "agenda_item_vote_id",
                         "parent_item_id", "source_id", "provenance_id")]
        if cols:
            reference_columns[table] = sorted(cols)
    for table, column in sorted(meeting_references | item_references | vote_references):
        reference_columns.setdefault(table, [])
        if column not in reference_columns[table]:
            reference_columns[table].append(column)
            reference_columns[table].sort()

    polymorphic: dict[str, dict[str, Any]] = {}
    for spec in POLYMORPHIC_REFERENCES:
        table, type_col = spec["table"], spec["type_column"]
        known = KNOWN_POLYMORPHIC_TYPES.get((table, type_col))
        if known is None:
            raise RuntimeError(
                f"refusing: unknown polymorphic class {table}.{type_col}")
        # Always inspect a DECLARED polymorphic class.  Gating this on body
        # membership or ordinary reference-column discovery silently skipped
        # entity_mentions and entity_relationships (Brief 031E item 1).
        if not table_presence(connection, (table,)).get(table):
            polymorphic[f"{table}.{type_col}"] = {
                "values": [], "present": False,
                "id_column": spec["id_column"],
                "targets": dict(spec["targets"])}
            continue
        if type_col not in _columns(connection, table):
            raise RuntimeError(
                f"refusing: declared polymorphic column missing: "
                f"{table}.{type_col}")
        found = connection.execute(text(
            f'SELECT DISTINCT "{type_col}" FROM "{table}" '
            f'WHERE "{type_col}" IS NOT NULL')).fetchall()
        values = sorted(str(r[0]) for r in found)
        unknown = [v for v in values if v not in known]
        if unknown:
            raise RuntimeError(
                f"refusing: unknown {table}.{type_col} value(s) {unknown}; "
                "declare them before merging")
        polymorphic[f"{table}.{type_col}"] = {
            "values": values, "present": True,
            "id_column": spec["id_column"],
            "targets": dict(spec["targets"]),
        }

    unique_twin_constraints = []
    for table, name, definition in unique_constraints:
        cols = next((set(row[3]) for row in unique_column_rows
                     if row[0] == table and row[1] == name), set())
        if "body" in cols and "meeting_id" in cols:
            unique_twin_constraints.append([table, name, definition])

    return {"body_tables": sorted(body_tables),
            "meeting_agenda_fks": meeting_fks,
            "body_unique_constraints": unique_constraints,
            "unique_twin_constraints": unique_twin_constraints,
            "unique_column_sets": [
                [str(table), str(name), list(columns)]
                for table, name, _definition, columns in unique_column_rows],
            "meeting_references": [list(v) for v in sorted(meeting_references)],
            "item_references": [list(v) for v in sorted(item_references)],
            "vote_references": [list(v) for v in sorted(vote_references)],
            "reference_columns": reference_columns,
            "polymorphic": polymorphic,
            "item_reference_columns": [[t, c]
                                       for t, c in ITEM_REFERENCE_COLUMNS],
            "meeting_db_tables": sorted(MEETING_DB_TABLES)}


def assert_meeting_item_cardinality(merges: list[dict[str, Any]]) -> None:
    """Every meeting/item map must be one-to-one (Brief 031D item 4).

    A many-to-one map would mean two duplicate rows collapsing onto one
    survivor, which silently deletes data.
    """
    for merge in merges:
        for key in ("meeting_map", "item_map"):
            rows = merge.get(key) or []
            old_ids = [r["old_id"] for r in rows]
            new_ids = [r["new_id"] for r in rows]
            if len(set(old_ids)) != len(old_ids):
                raise RuntimeError(
                    f"refusing: {merge['old']} {key} has duplicate old_id")
            if len(set(new_ids)) != len(new_ids):
                raise RuntimeError(
                    f"refusing: {merge['old']} {key} is not one-to-one "
                    f"({len(old_ids)} old rows map onto {len(set(new_ids))} "
                    "surviving rows)")


def event_contract_snapshot(connection: Any) -> dict[str, Any]:
    """Capture the meeting_events source-id contract (Brief 031D item 6).

    Only rows whose key matches a real EXTERNAL meeting id are counted, so the
    postcondition can prove the merge never rewrote an event key.
    """
    if not table_presence(connection, (EVENT_TABLE,)).get(EVENT_TABLE):
        return {"present": False}
    cols = _columns(connection, EVENT_TABLE)
    if EVENT_MEETING_COLUMN not in cols:
        return {"present": False}
    rows = connection.execute(text(f"""
        SELECT id::text AS rid, "{EVENT_MEETING_COLUMN}"::text AS mid
        FROM {EVENT_TABLE} ORDER BY id
    """)).fetchall()
    mapping = [[r[0], r[1]] for r in rows]
    row = connection.execute(text(f"""
        SELECT count(*) AS total,
               count(*) FILTER (WHERE e."{EVENT_MEETING_COLUMN}" IS NOT NULL)
                   AS non_null,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM meetings m
                   WHERE m.meeting_id = e."{EVENT_MEETING_COLUMN}")) AS external_hits,
               count(*) FILTER (WHERE EXISTS (
                   SELECT 1 FROM meetings m
                   WHERE m.id::text = e."{EVENT_MEETING_COLUMN}")) AS pk_hits
        FROM {EVENT_TABLE} e
    """)).mappings().one()
    return {"present": True, **{k: int(v or 0) for k, v in dict(row).items()},
            # The EXACT id -> meeting_id mapping, deterministically ordered
            # (Brief 031E item 6).  Aggregate hit counts cannot detect a
            # substitution that keeps the totals unchanged.
            "mapping_digest": digest(mapping)}


def assert_event_contract(before: dict[str, Any], after: dict[str, Any]) -> None:
    """Refuse if any event key moved onto a database PK.

    External ids are the contract.  If a surviving row's key now matches a
    database PK, the merge rewrote it and the provenance is corrupted.
    """
    if not before.get("present") or not after.get("present"):
        return
    if after["total"] != before["total"]:
        raise RuntimeError(
            f"refusing: {EVENT_TABLE} row count changed "
            f"{before['total']} -> {after['total']}")
    if after["pk_hits"] > before["pk_hits"]:
        raise RuntimeError(
            "refusing: event keys were rewritten onto database PKs "
            f"({before['pk_hits']} -> {after['pk_hits']})")
    if after["external_hits"] < before["external_hits"]:
        raise RuntimeError(
            "refusing: event external-id matches decreased "
            f"({before['external_hits']} -> {after['external_hits']})")
    # Exact-mapping binding (Brief 031E item 6): a substitution that preserves
    # every aggregate count still changes this digest.
    if before.get("mapping_digest") != after.get("mapping_digest"):
        raise RuntimeError(
            "refusing: event id->meeting_id mapping changed "
            f"({before.get('mapping_digest')} -> {after.get('mapping_digest')})")


def classify_identity(identity: dict[str, Any]) -> str:
    """Which tier a live identity belongs to (pure function, unit-testable)."""
    name = str(identity.get("database") or "")
    if name == PRODUCTION_TARGET["database"]:
        return "production"
    if name == DEV_TARGET["database"] or name.startswith(SCRATCH_PREFIX):
        return "development"
    return "unknown"


def assert_target_identity(identity: dict[str, Any], target: dict[str, str]) -> None:
    """Fail closed unless the live identity is the intended target.

    Both directions are guarded: a production-target plan refuses a
    development connection, and a development plan refuses production.  The
    production tier additionally requires the expected host, so a same-named
    database on the wrong server cannot be mutated.
    """
    tier = target["tier"]
    database = str(identity.get("database") or "")
    if database != target["database"]:
        raise RuntimeError(
            f"refusing: target {tier} expects database {target['database']!r}, "
            f"live connection is {database!r}")
    if tier == "production":
        host = str(identity.get("host") or "")
        if host != PRODUCTION_TARGET["host"]:
            raise RuntimeError(
                f"refusing: production target expects host "
                f"{PRODUCTION_TARGET['host']!r}, live host is {host!r}")
        return
    if classify_identity(identity) != "development":
        raise RuntimeError(
            f"refusing: development target cannot act on {database!r}")
    if str(identity.get("host") or "") == PRODUCTION_TARGET["host"]:
        raise RuntimeError("refusing: development target pointed at production host")


def assert_target(connection: Any, target: dict[str, str]) -> dict[str, Any]:
    """Read the live identity and fail closed unless it is ``target``."""
    identity = target_identity(connection)
    assert_target_identity(identity, target)
    return identity


def assert_development(connection: Any) -> None:
    """Backwards-compatible guard: the historical development-only entry."""
    assert_target(connection, DEV_TARGET)


def protected_snapshot(connection: Any,
                       capabilities: dict[str, Any] | None = None,
                       inventory: dict[str, Any] | None = None) -> dict[str, Any]:
    caps = capabilities or schema_capabilities(connection)
    inventory = inventory or reference_inventory(connection)
    ref_tables = {
        table for table, _column in (
            (inventory.get("meeting_references") or [])
            + (inventory.get("item_references") or [])
            + (inventory.get("vote_references") or []))
    }
    candidate_tables = sorted(set(PROTECTED_TABLES) | ref_tables | {
        *inventory.get("body_tables", []), "entity_relationships", "member_votes"
    })
    present = table_presence(connection, tuple(candidate_tables))
    tables = tuple(t for t in candidate_tables if present.get(t)
                   if t not in OPTIONAL_TABLES or _has(caps, t))
    counts = {table: int(connection.execute(text(
        f'SELECT count(*) FROM "{table}"')).scalar() or 0)
        for table in tables}
    meeting_orphans = {}
    for table, column in inventory.get("meeting_references") or []:
        meeting_orphans[f"{table}.{column}"] = int(connection.execute(text(f"""
            SELECT count(*) FROM "{table}" t LEFT JOIN meetings m
              ON m.id=t."{column}"
            WHERE t."{column}">0 AND m.id IS NULL
        """)).scalar() or 0)
    # Event contract (Brief 031D item 6): meeting_events.meeting_id holds the
    # EXTERNAL meeting source id, never meetings.id.  Orphan detection therefore
    # joins on meetings.meeting_id; joining on the database PK would both
    # misreport orphans and invite the PK rewrite this contract forbids.
    meeting_orphans["meeting_events"] = int(connection.execute(text("""
        SELECT count(*) FROM meeting_events e
        WHERE NOT EXISTS (SELECT 1 FROM meetings m
                          WHERE m.meeting_id = e.meeting_id)
    """)).scalar() or 0)
    item_orphans = {}
    for table, column in inventory.get("item_references") or []:
        item_orphans[f"{table}.{column}"] = int(connection.execute(text(f"""
            SELECT count(*) FROM "{table}" t LEFT JOIN agenda_items a
              ON a.id=t."{column}"
            WHERE t."{column}">0 AND a.id IS NULL
        """)).scalar() or 0)
    vote_orphans = {}
    for table, column in inventory.get("vote_references") or []:
        vote_orphans[f"{table}.{column}"] = int(connection.execute(text(f"""
            SELECT count(*) FROM "{table}" t
            WHERE t."{column}">0 AND NOT EXISTS (
              SELECT 1 FROM agenda_item_votes v WHERE v.id=t."{column}")
        """)).scalar() or 0)
    # Polymorphic orphans, driven by the registry so every declared form is
    # counted and none is overlooked (Brief 031D items 5 and 9).
    for spec in POLYMORPHIC_REFERENCES:
        table, type_col = spec["table"], spec["type_column"]
        id_col = spec["id_column"]
        if not table_presence(connection, (table,)).get(table):
            continue
        if type_col not in _columns(connection, table):
            continue
        for type_value, target in sorted(spec["targets"].items()):
            item_orphans[f"{table}.{type_col}={type_value}"] = int(
                connection.execute(text(f"""
                    SELECT count(*) FROM "{table}" e
                    WHERE e."{type_col}" = :tval AND e."{id_col}" IS NOT NULL
                      AND NOT EXISTS (SELECT 1 FROM "{target}" x
                                      WHERE x.id = e."{id_col}")
                """), {"tval": type_value}).scalar() or 0)
    return {"counts": counts, "meeting_orphans": meeting_orphans,
            "item_orphans": item_orphans, "vote_orphans": vote_orphans}


def _meeting_maps(connection: Any, old: str, new: str) -> list[dict[str, Any]]:
    return [dict(r) for r in connection.execute(text("""
        SELECT o.id AS old_id, n.id AS new_id, o.meeting_id
        FROM meetings o JOIN meetings n USING (meeting_id)
        WHERE o.body=:old AND n.body=:new ORDER BY o.id
    """), {"old": old, "new": new}).mappings()]


def _item_maps(connection: Any, meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    missing = agenda_item_policy_coverage(connection)
    if missing:
        raise RuntimeError(
            "refusing: agenda_items column(s) with no explicit merge policy: "
            f"{missing}")
    columns = _columns(connection, "agenda_items")
    material = [c for c in columns if c not in AGENDA_ITEM_NON_MATERIAL]
    out: list[dict[str, Any]] = []
    for meeting in meetings:
        rows = connection.execute(text("""
            SELECT o.id AS old_id, n.id AS new_id, o.agenda_item_number
            FROM agenda_items o JOIN agenda_items n
             ON n.meeting_db_id=:new_id
             AND n.agenda_item_number=o.agenda_item_number
            WHERE o.meeting_db_id=:old_id ORDER BY o.id
        """), meeting).mappings()
        for row in rows:
            row = dict(row)
            old_row = dict(connection.execute(
                text("SELECT * FROM agenda_items WHERE id=:id"),
                {"id": row["old_id"]}).mappings().one())
            new_row = dict(connection.execute(
                text("SELECT * FROM agenda_items WHERE id=:id"),
                {"id": row["new_id"]}).mappings().one())
            for column in material:
                policy = AGENDA_ITEM_MERGE_POLICIES[column]
                try:
                    _policy_value(policy, old_row.get(column), new_row.get(column))
                except ValueError:
                    raise RuntimeError(
                        f"agenda-item conflict meeting={meeting['meeting_id']} "
                        f"number={row['agenda_item_number']} field={column}")
            if not compatible_destiny_urls(old_row.get("source_url"),
                                           new_row.get("source_url")):
                raise RuntimeError(
                    f"incompatible agenda-item URLs meeting={meeting['meeting_id']} "
                    f"number={row['agenda_item_number']}")
            out.append({"old_id": row["old_id"], "new_id": row["new_id"],
                        "meeting_id": meeting["meeting_id"],
                        "number": row["agenda_item_number"],
                        "preferred_agenda_item_id": old_row.get("agenda_item_id"),
                        "preferred_source_url": old_row.get("source_url") or "",
                        "preferred_item_url": (old_row.get("agenda_item_url") or
                                               new_row.get("agenda_item_url") or "")})
    return out


def projected_reference_collisions(connection: Any,
                                   inventory: dict[str, Any],
                                   item_maps: list[dict[str, Any]]) -> list[str]:
    """Fail before mutation when a reference rewrite can hit a unique key.

    Composite keys are treated conservatively: if both old- and new-reference
    rows exist, later projection could collide, so the plan refuses rather than
    guessing which row should survive.
    """
    unique_sets: dict[str, list[set[str]]] = {}
    for table, _name, columns in inventory.get("unique_column_sets") or []:
        unique_sets.setdefault(table, []).append(set(columns))
    problems: list[str] = []
    for table, column in inventory.get("item_references") or []:
        if not any(column in keys for keys in unique_sets.get(table, [])):
            continue
        for mapping in item_maps:
            old_exists = connection.execute(text(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE "{column}"=:value)'),
                {"value": mapping["old_id"]}).scalar()
            new_exists = connection.execute(text(
                f'SELECT EXISTS (SELECT 1 FROM "{table}" WHERE "{column}"=:value)'),
                {"value": mapping["new_id"]}).scalar()
            if old_exists and new_exists:
                problems.append(
                    f"{table}.{column}:{mapping['old_id']}->{mapping['new_id']}")
    return problems


def _unequal_unique_twins(connection: Any, table: str, old: str, new: str,
                          meetings: list[dict[str, Any]],
                          keys: tuple[str, ...]) -> list[dict[str, Any]]:
    columns = [c for c in _columns(connection, table) if c not in IGNORED_TWIN_FIELDS]
    bad: list[dict[str, Any]] = []
    for meeting in meetings:
        predicates = " AND ".join(
            f"n.\"{key}\" IS NOT DISTINCT FROM o.\"{key}\"" for key in keys)
        rows = connection.execute(text(f"""
            SELECT o.*, n.id AS twin_id FROM "{table}" o JOIN "{table}" n
              ON n.body=:new AND n.meeting_id=o.meeting_id AND {predicates}
            WHERE o.body=:old AND o.meeting_id=:meeting_id
        """), {"old": old, "new": new, **meeting}).mappings()
        for row in rows:
            old_row = dict(row)
            twin = connection.execute(text(f'SELECT * FROM "{table}" WHERE id=:id'),
                                      {"id": old_row["twin_id"]}).mappings().one()
            unequal = [c for c in columns if old_row.get(c) != twin.get(c)]
            if unequal:
                bad.append({"table": table, "old_id": old_row["id"],
                            "new_id": twin["id"], "fields": unequal})
    return bad


def _exact_unique_twins(connection: Any, table: str, old: str,
                        new: str, keys: tuple[str, ...]) -> list[dict[str, int]]:
    columns = [c for c in _columns(connection, table)
               if c not in IGNORED_TWIN_FIELDS]
    predicates = [
        f'n."{key}" IS NOT DISTINCT FROM o."{key}"' for key in keys]
    predicates.extend(
        f'n."{column}" IS NOT DISTINCT FROM o."{column}"'
        for column in columns)
    return [dict(row) for row in connection.execute(text(f"""
        SELECT o.id AS old_id, n.id AS new_id
        FROM "{table}" o JOIN "{table}" n
          ON n.body=:new AND n.meeting_id=o.meeting_id
         AND {' AND '.join(predicates)}
        WHERE o.body=:old ORDER BY o.id, n.id
    """), {"old": old, "new": new}).mappings()]


def content_digest(plan: dict[str, Any]) -> str:
    """Digest of a plan's CONTENT, ignoring location-only fields.

    The production plan and a plan built against a restored copy of production
    must agree here — including the protected-table baseline snapshot — which is
    what makes the scratch proof a meaningful rehearsal of the production merge
    rather than merely a test that the code runs (Brief 031D item 1).
    """
    body = {k: v for k, v in plan.items() if k not in LOCATION_ONLY_KEYS}
    return digest(body)


def build_plan(connection: Any,
               target: dict[str, str] | None = None) -> dict[str, Any]:
    target = dict(target or DEV_TARGET)
    identity = assert_target(connection, target)
    merges = []
    inventory = reference_inventory(connection)
    uncovered = merge_policy_coverage(connection)
    if any(uncovered.values()):
        raise RuntimeError(
            "refusing: live merge columns lack explicit policies: "
            f"{uncovered}")
    twin_tables = unique_twin_tables(inventory)
    for old, new in MERGES.items():
        registry_rows = [dict(row) for row in connection.execute(text("""
            SELECT id, body_code, slug, name FROM public_bodies
            WHERE body_code IN (:old,:new) ORDER BY body_code
        """), {"old": old, "new": new}).mappings()]
        old_rows = [row for row in registry_rows if row["body_code"] == old]
        new_rows = [row for row in registry_rows if row["body_code"] == new]
        if len(old_rows) != 1 or new_rows:
            raise RuntimeError(
                f"registry precondition failed for {old}->{new}: {registry_rows}")
        meetings = _meeting_maps(connection, old, new)
        items = _item_maps(connection, meetings)
        projected_collisions = projected_reference_collisions(
            connection, inventory, items)
        if projected_collisions:
            raise RuntimeError(
                "refusing: projected unique-reference collisions: "
                f"{projected_collisions}")
        # Lossless-twin gate (Brief 031E item 5): every meetings column must have
        # an explicit policy, and anything left must already agree.
        missing_policies = meeting_policy_coverage(connection)
        if missing_policies:
            raise RuntimeError(
                "refusing: meetings column(s) with no explicit merge policy: "
                f"{missing_policies}")
        unequal_meetings = unequal_meeting_twins(connection, meetings)
        if unequal_meetings:
            raise RuntimeError(
                f"refusing: unequal meeting twins: {unequal_meetings[:5]}")
        conflicts = []
        # Driven by the validated inventory, not the legacy constant (item 3).
        for table in twin_tables:
            conflicts.extend(_unequal_unique_twins(
                connection, table, old, new, meetings, twin_tables[table]))
        if conflicts:
            raise RuntimeError(f"non-identical unique-row collisions: {conflicts}")
        twin_dedup_maps = {
            table: _exact_unique_twins(
                connection, table, old, new, twin_tables[table])
            for table in sorted(twin_tables)
        }
        counts = {}
        for table in _body_tables(connection):
            counts[table] = int(connection.execute(
                text(f'SELECT count(*) FROM "{table}" WHERE body=:old'),
                {"old": old}).scalar() or 0)
        merges.append({"old": old, "new": new, "registry": old_rows[0],
                       "meeting_map": meetings,
                       "item_map": items, "body_counts": counts,
                       "twin_dedup_maps": twin_dedup_maps})
    assert_meeting_item_cardinality(merges)
    body = {"kind": "body-code-merge", "version": 6,
            "tier": target["tier"],
            "target": target["database"],
            "target_identity": identity,
            "capabilities": schema_capabilities(connection),
            "inventory": inventory,
            "event_contract": event_contract_snapshot(connection),
            "baseline": protected_snapshot(connection,
                                             schema_capabilities(connection),
                                             inventory),
            "code_hashes": code_hashes(), "merges": merges}
    return {**body, "digest": digest(body)}


def _reparent_polymorphic(connection: Any, temp_table: str,
                          target_table: str) -> dict[str, int]:
    """Reparent polymorphic references onto surviving rows.

    Driven by POLYMORPHIC_REFERENCES, so a reference class that is not declared
    there can never be silently skipped (Brief 031D item 5).  Any type value
    outside the declared set was already refused during inventory.
    """
    touched: dict[str, int] = {}
    for spec in POLYMORPHIC_REFERENCES:
        table, type_col = spec["table"], spec["type_column"]
        id_col = spec["id_column"]
        types = sorted(t for t, tgt in spec["targets"].items()
                       if tgt == target_table)
        if not types:
            continue
        result = connection.execute(text(f"""
            UPDATE "{table}" e SET "{id_col}" = m.new_id
            FROM {temp_table} m
            WHERE e."{type_col}" = ANY(:types) AND e."{id_col}" = m.old_id
        """), {"types": types})
        touched[f"{table}.{type_col}"] = int(result.rowcount or 0)
    return touched


def merge_lock(engine: Any):
    """Advisory lock + serializable transaction, matching the sync protocol.

    Brief 031D item 8: the merge must not run concurrently with a sync, and must
    be serializable so a concurrent writer cannot invalidate the plan between
    its verification and its commit.
    """
    from contextlib import contextmanager

    from db.sync_declarations import LOCK_ID

    @contextmanager
    def _lock():
        lock_connection = engine.connect()
        acquired = lock_connection.execute(
            text(f"SELECT pg_try_advisory_lock({LOCK_ID})")).scalar()
        if not acquired:
            lock_connection.close()
            raise RuntimeError(
                "refusing: another sync/merge holds the advisory lock")
        try:
            with engine.connect().execution_options(
                    isolation_level="SERIALIZABLE") as connection:
                transaction = connection.begin()
                try:
                    yield connection
                    transaction.commit()
                except BaseException:
                    transaction.rollback()
                    raise
        finally:
            try:
                lock_connection.execute(
                    text(f"SELECT pg_advisory_unlock({LOCK_ID})"))
            finally:
                lock_connection.close()

    return _lock()


def _vote_collisions(connection: Any, inventory: dict[str, Any],
                     merges: list[dict[str, Any]]) -> list[str]:
    """Detect body-scoped unique collisions a merge could create.

    Derived from the live inventory rather than a hand-written list, so a new
    body-scoped unique constraint is audited automatically (item 7).
    """
    problems: list[str] = []
    for merge in merges:
        old, new = merge["old"], merge["new"]
        column_sets = {(t, n): list(cols) for t, n, cols in
                       inventory.get("unique_column_sets") or []}
        for table, name, definition in inventory.get("body_unique_constraints") or []:
            columns = column_sets.get((table, name), [])
            if "body" not in columns:
                raise RuntimeError(
                    f"refusing: body unique inventory mismatch: {table}.{name}")
            keys = [column for column in columns if column != "body"]
            if not keys:
                continue
            # The expected meetings(body, meeting_id) collision is represented
            # exactly by meeting_map and is resolved only after child rows are
            # reparented.  Deleting that parent early would break the plan.
            if table == "meetings" and keys == ["meeting_id"]:
                continue
            join = " AND ".join(
                f'n."{k}" IS NOT DISTINCT FROM o."{k}"' for k in keys)
            rows = connection.execute(text(f"""
                SELECT o.id, n.id FROM "{table}" o JOIN "{table}" n
                  ON n.body = :new AND {join}
                WHERE o.body = :old
            """), {"old": old, "new": new}).fetchall()
            observed = {(int(row[0]), int(row[1])) for row in rows}
            allowed = {(int(pair["old_id"]), int(pair["new_id"]))
                       for pair in (merge.get("twin_dedup_maps") or {}).get(table, [])}
            unexpected = observed - allowed
            if unexpected:
                problems.append(
                    f"{table} ({name}): {len(unexpected)} colliding row(s)")
    return problems


def _reservation_collisions(connection: Any,
                            merges: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return reservation conflicts without creating maps or mutating rows."""
    collisions: list[dict[str, Any]] = []
    for merge in merges:
        for mapping in merge.get("meeting_map") or []:
            rows = connection.execute(text("""
                SELECT :meeting_id AS meeting_id, o.agenda_item_number
                FROM agenda_item_key_reservation o
                JOIN agenda_item_key_reservation n
                  ON n.meeting_db_id=:new_id
                 AND n.agenda_item_number=o.agenda_item_number
                WHERE o.meeting_db_id=:old_id
            """), mapping).mappings().all()
            collisions.extend(dict(row) for row in rows)
    return collisions


def unique_twin_tables(inventory: dict[str, Any]) -> dict[str, tuple[str, ...]]:
    """Body-scoped unique constraints, taken from the VALIDATED inventory.

    Replaces the hard-coded ``UNIQUE_BODY_TABLES`` so a newly discovered unique
    constraint is handled rather than silently omitted (Brief 031E item 3).
    """
    out: dict[str, tuple[str, ...]] = {}
    rows = inventory.get("unique_twin_constraints")
    if rows is None:
        rows = inventory.get("body_unique_constraints") or []
    column_sets = {(t, n): tuple(cols) for t, n, cols in
                   inventory.get("unique_column_sets") or []}
    for table, name, _definition in rows:
        keys = tuple(column for column in column_sets.get((table, name), ())
                     if column != "body")
        # Exact twin deletion is safe only for rows keyed to a meeting's
        # external source id.  Other body-scoped uniques are collision checks,
        # not deduplication instructions.
        # Parent rows have dedicated lossless map/policy machinery and must not
        # be deleted in the preliminary child-twin pass.
        if table not in {"meetings", "agenda_items"} and keys and "meeting_id" in keys:
            if table in out and out[table] != keys:
                raise RuntimeError(
                    f"refusing: multiple twin unique keys for {table}: "
                    f"{out[table]} and {keys}")
            out[table] = keys
    return out


# Explicit per-column merge policies for duplicate MEETING rows (Brief 031E
# item 5).  A column not listed here and not non-material must already agree,
# or the merge refuses.  Declaring the policy per column is what makes the
# comparison lossless rather than a hand-picked title/text/URL check.
MEETING_MERGE_POLICIES = {
    "meeting_date": "equal",
    "meeting_type": "equal",
    "meeting_title": "coalesce_new",
    "meeting_title_raw": "coalesce_new",
    "meeting_context": "coalesce_old",
    "meeting_body": "coalesce_old",
    "display_name": "coalesce_old",
    "source_url": "coalesce_new",
    "sync_status": "coalesce_new",
    "jurisdiction_id": "coalesce_new",
    "public_body_id": "coalesce_new",
    "source_system": "coalesce_new",
    "source_instance_url": "coalesce_new",
    "last_synced_at": "greatest",
    "last_attempted_at": "greatest",
    "last_error": "coalesce_new",
    "retry_count": "greatest",
    "item_count_expected": "greatest",
    "item_count_actual": "greatest",
    "supporting_doc_count": "greatest",
    "items_extracted": "or",
    "supporting_docs_extracted": "or",
    "votes_extracted": "or",
    "next_doc_check_at": "greatest",
    "minutes_url": "coalesce_old",
    "_multi_jurisdiction_backfilled": "or",
}

# Identity / provenance / bookkeeping: expected to differ between twins.
MEETING_NON_MATERIAL = frozenset({"id", "body", "meeting_id", "created_at",
                                  "updated_at"})

# Agenda-item rows get the same fail-closed treatment as meetings.  The
# semantic fields must agree; source URLs/identifiers use the old (canonical
# source) value; every other live column must be explicitly listed here or the
# plan is refused.  This prevents a newly added material column from being
# silently discarded when the old row is deleted.
AGENDA_ITEM_MERGE_POLICIES = {
    "meeting_id": "equal",
    "agenda_item_number": "equal",
    "parent_item_id": "coalesce_old",
    "agenda_item_id": "prefer_old",
    "agenda_item_title": "semantic_equal",
    "agenda_item_text": "semantic_equal",
    "agenda_item_url": "coalesce_old",
    "vote_or_action": "semantic_equal",
    "source_body": "coalesce_old",
    "source_url": "coalesce_old",
    "c_number": "equal",
    "c_number_base": "equal",
    "c_number_revision": "equal",
    "case_number": "equal",
    "item_type": "equal",
    "section_level": "equal",
    "sort_order": "equal",
    "agenda_category": "equal",
    "jurisdiction_id": "equal",
    "public_body_id": "equal",
    "lifecycle_status": "coalesce_old",
    "_multi_jurisdiction_backfilled": "or",
    "swept_at": "greatest",
    # Derived from the survivor's merged title/text.  Never copy a possibly
    # stale vector from either twin.
    "search_vector": "recompute_search_vector",
}

AGENDA_ITEM_NON_MATERIAL = frozenset({
    "id", "body", "meeting_db_id", "created_at", "updated_at",
})


def agenda_item_policy_coverage(connection: Any) -> list[str]:
    known = set(AGENDA_ITEM_MERGE_POLICIES) | set(AGENDA_ITEM_NON_MATERIAL)
    return [c for c in _columns(connection, "agenda_items") if c not in known]


def _policy_value(policy: str, old: Any, new: Any) -> Any:
    if policy == "equal":
        if old != new:
            raise ValueError("values differ")
        return new
    if policy == "semantic_equal":
        if semantic_text(old) != semantic_text(new):
            raise ValueError("semantic values differ")
        return new
    if policy == "prefer_old":
        return old
    if policy == "coalesce_old":
        return old if old not in (None, "") else new
    if policy == "coalesce_new":
        return new if new not in (None, "") else old
    if policy == "greatest":
        if old is None:
            return new
        if new is None:
            return old
        return max(old, new)
    if policy == "or":
        return bool(old) or bool(new)
    if policy == "recompute_search_vector":
        # Planning only validates that the policy is known.  The database
        # expression is evaluated against the survivor after semantic fields
        # have been validated.
        return new
    raise ValueError(f"unknown merge policy {policy!r}")


def meeting_policy_coverage(connection: Any) -> list[str]:
    """Refuse if any live meetings column lacks an explicit policy."""
    known = set(MEETING_MERGE_POLICIES) | set(MEETING_NON_MATERIAL)
    missing = [c for c in _columns(connection, "meetings") if c not in known]
    return missing


def merge_policy_coverage(connection: Any) -> dict[str, list[str]]:
    """Return all live policy drift at once, in deterministic table order."""
    return {
        "agenda_items": agenda_item_policy_coverage(connection),
        "meetings": meeting_policy_coverage(connection),
    }


def unequal_meeting_twins(connection: Any,
                          meetings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Every material column without a policy must already be equal."""
    columns = _columns(connection, "meetings")
    subject = [c for c in columns
               if c not in MEETING_NON_MATERIAL and c not in MEETING_MERGE_POLICIES]
    problems: list[dict[str, Any]] = []
    for meeting in meetings:
        for column in subject:
            count = connection.execute(text(f"""
                SELECT count(*) FROM meetings o JOIN meetings n ON n.id=:new_id
                WHERE o.id=:old_id AND o."{column}" IS DISTINCT FROM n."{column}"
            """), meeting).scalar() or 0
            if count:
                problems.append({"meeting_id": meeting["meeting_id"],
                                 "column": column})
    return problems


def _delete_exact_unique_twins(connection: Any, table: str, old: str, new: str,
                               keys: tuple[str, ...]) -> int:
    predicates = " AND ".join(
        f'n."{key}" IS NOT DISTINCT FROM o."{key}"' for key in keys)
    result = connection.execute(text(f"""
        DELETE FROM "{table}" o USING "{table}" n
        WHERE o.body=:old AND n.body=:new AND n.meeting_id=o.meeting_id
          AND {predicates}
    """), {"old": old, "new": new})
    return result.rowcount


def _legacy_delete_exact_unique_twins(connection: Any, table: str, old: str,
                                      new: str) -> int:
    keys = UNIQUE_BODY_TABLES[table]
    predicates = " AND ".join(
        f"n.\"{key}\" IS NOT DISTINCT FROM o.\"{key}\"" for key in keys)
    result = connection.execute(text(f"""
        DELETE FROM "{table}" o USING "{table}" n
        WHERE o.body=:old AND n.body=:new AND n.meeting_id=o.meeting_id
          AND {predicates}
    """), {"old": old, "new": new})
    return result.rowcount


def _merge_watermark(connection: Any, old: str, new: str) -> None:
    old_row = connection.execute(text(
        "SELECT * FROM _pattern_cascade_watermark WHERE body=:body"),
        {"body": old}).mappings().first()
    if not old_row:
        return
    new_row = connection.execute(text(
        "SELECT * FROM _pattern_cascade_watermark WHERE body=:body"),
        {"body": new}).mappings().first()
    if not new_row:
        connection.execute(text(
            "UPDATE _pattern_cascade_watermark SET body=:new WHERE body=:old"),
            {"old": old, "new": new})
        return
    connection.execute(text("""
        UPDATE _pattern_cascade_watermark SET
          last_run_at=greatest(last_run_at,:last_run_at),
          last_processed_id=greatest(last_processed_id,:last_processed_id),
          items_processed=greatest(items_processed,:items_processed),
          entities_created=greatest(entities_created,:entities_created),
          edges_created=greatest(edges_created,:edges_created)
        WHERE body=:new
    """), {"new": new, **dict(old_row)})
    connection.execute(text(
        "DELETE FROM _pattern_cascade_watermark WHERE body=:old"), {"old": old})


def _apply_row_policies(connection: Any, table: str, mappings: str,
                        policies: dict[str, str], non_material: set[str]) -> None:
    """Apply the already-validated explicit policies to surviving rows."""
    columns = [c for c in _columns(connection, table)
               if c not in non_material and c in policies]
    if not columns:
        return
    assignments = []
    for column in columns:
        policy = policies[column]
        q = f'n."{column}"'
        o = f'o."{column}"'
        if policy in ("equal", "semantic_equal"):
            # Equality was proved while planning; leave the surviving value
            # untouched rather than needlessly rewriting it.
            continue
        if policy == "prefer_old":
            expr = o
        elif policy == "coalesce_old":
            expr = f"COALESCE({o}, {q})"
        elif policy == "coalesce_new":
            expr = f"COALESCE({q}, {o})"
        elif policy == "greatest":
            expr = f"GREATEST({q}, {o})"
        elif policy == "or":
            expr = f"COALESCE({q}, FALSE) OR COALESCE({o}, FALSE)"
        elif policy == "recompute_search_vector":
            if table != "agenda_items" or column != "search_vector":
                raise RuntimeError(
                    f"recompute_search_vector is unsupported for "
                    f"{table}.{column}")
            expr = (
                "setweight(to_tsvector('english', "
                "COALESCE(n.agenda_item_title, '')), 'A') || "
                "setweight(to_tsvector('english', "
                "COALESCE(n.agenda_item_text, '')), 'B')"
            )
        else:
            raise RuntimeError(f"unknown merge policy {table}.{column}: {policy}")
        # PostgreSQL does not permit qualifying the target column on the SET
        # left-hand side (``SET n.col=...`` is parsed as a column named
        # ``n``).  Keep the alias on RHS expressions, but quote the bare
        # target column here.
        assignments.append(f'"{column}"={expr}')
    if assignments:
        connection.execute(text(f"""
            UPDATE "{table}" n SET {', '.join(assignments)}
            FROM "{table}" o JOIN {mappings} m ON m.old_id=o.id
            WHERE n.id=m.new_id
        """))


def execute_plan(connection: Any, plan: dict[str, Any]) -> dict[str, int]:
    target = {"tier": plan.get("tier", "development"),
              "database": plan["target"]}
    identity = assert_target(connection, target)
    recorded = plan.get("target_identity") or {}
    for key in ("database", "host", "port", "dialect", "driver",
                "server_version", "cluster_identity"):
        if key not in recorded:
            raise RuntimeError(
                f"refusing: plan does not bind exact target identity field {key}")
        bound = recorded.get(key)
        # An unavailable cluster identity is explicitly represented as empty;
        # all other identity fields must be concrete and equal.
        if key != "cluster_identity" and bound in (None, ""):
            raise RuntimeError(
                f"refusing: plan has empty target identity field {key}")
        if str(identity.get(key, "")) != str(bound):
            raise RuntimeError(
                f"refusing: target identity drift on {key}: "
                f"live={identity.get(key)!r} bound={bound!r}")
    current = build_plan(connection, target)
    if plan["digest"] != current["digest"]:
        raise RuntimeError("live plan drift")
    # Explicit, clearer failure for schema drift specifically.  The digest above
    # would also catch it, but this names the cause.
    capabilities = assert_capabilities(connection, plan)
    has_reservations = _has(capabilities, "agenda_item_key_reservation")
    has_watermark = _has(capabilities, "_pattern_cascade_watermark")
    stats: dict[str, int] = {}
    stats["reservations_present"] = int(has_reservations)
    stats["watermark_present"] = int(has_watermark)
    body_tables = _body_tables(connection)
    inventory = plan.get("inventory") or {}
    twin_tables = unique_twin_tables(inventory)

    # Every refusal-capable collision check precedes all DML.  Exact twin
    # pairs are allowed only when their IDs are already bound in the plan.
    collisions = _vote_collisions(connection, inventory, plan["merges"])
    if collisions:
        raise RuntimeError(
            f"refusing: body-scoped unique collisions: {collisions}")
    if has_reservations:
        reservation_collisions = _reservation_collisions(
            connection, plan["merges"])
        if reservation_collisions:
            raise RuntimeError(
                f"reservation collisions: {reservation_collisions}")

    for merge in plan["merges"]:
        old, new = merge["old"], merge["new"]
        # Remove only the exact twin IDs already accepted by the preflight and
        # cryptographically bound into the plan (Brief 031E items 3–4).
        for table, _keys in twin_tables.items():
            bound_pairs = (merge.get("twin_dedup_maps") or {}).get(table, [])
            old_ids = [int(pair["old_id"]) for pair in bound_pairs]
            actual_dedup = 0
            if old_ids:
                actual_dedup = int(connection.execute(text(
                    f'DELETE FROM "{table}" WHERE id = ANY(:old_ids)'),
                    {"old_ids": old_ids}).rowcount or 0)
            expected_dedup = len(bound_pairs)
            if actual_dedup != expected_dedup:
                raise RuntimeError(
                    f"dedup drift {old}->{new} {table}: "
                    f"planned={expected_dedup} actual={actual_dedup}")
            stats[f"{old}:{table}:dedup"] = actual_dedup
        connection.execute(text(
            "DROP TABLE IF EXISTS pg_temp._merge_item_map"))
        connection.execute(text("""
            CREATE TEMP TABLE _merge_item_map (
              old_id integer PRIMARY KEY, new_id integer NOT NULL,
              preferred_agenda_item_id text,
              preferred_source_url text NOT NULL, preferred_item_url text NOT NULL
            ) ON COMMIT DROP
        """))
        if merge["item_map"]:
            connection.execute(text("""
                INSERT INTO _merge_item_map
                  (old_id,new_id,preferred_agenda_item_id,
                   preferred_source_url,preferred_item_url)
                VALUES (:old_id,:new_id,:preferred_agenda_item_id,
                        :preferred_source_url,:preferred_item_url)
            """), merge["item_map"])
            connection.execute(text("""
                UPDATE agenda_items a SET source_url=m.preferred_source_url,
                  agenda_item_url=CASE WHEN m.preferred_item_url<>''
                                       THEN m.preferred_item_url ELSE a.agenda_item_url END
                FROM _merge_item_map m WHERE a.id=m.new_id
            """))
            staged_policies = dict(AGENDA_ITEM_MERGE_POLICIES)
            staged_policies.pop("agenda_item_id", None)
            _apply_row_policies(connection, "agenda_items", "_merge_item_map",
                                staged_policies,
                                set(AGENDA_ITEM_NON_MATERIAL))
            for table, column in inventory.get("item_references") or []:
                connection.execute(text(
                    f'UPDATE "{table}" t SET "{column}"=m.new_id '
                    f'FROM _merge_item_map m WHERE t."{column}"=m.old_id'))
            stats.update(_reparent_polymorphic(
                connection, "_merge_item_map", "agenda_items"))
            connection.execute(text("""
                DELETE FROM agenda_items a USING _merge_item_map m WHERE a.id=m.old_id
            """))
            connection.execute(text("""
                UPDATE agenda_items a
                SET agenda_item_id=m.preferred_agenda_item_id
                FROM _merge_item_map m WHERE a.id=m.new_id
            """))

        connection.execute(text(
            "DROP TABLE IF EXISTS pg_temp._merge_meeting_map"))
        connection.execute(text("""
            CREATE TEMP TABLE _merge_meeting_map (
              old_id integer PRIMARY KEY, new_id integer NOT NULL, meeting_id text NOT NULL
            ) ON COMMIT DROP
        """))
        if merge["meeting_map"]:
            connection.execute(text("""
                INSERT INTO _merge_meeting_map (old_id,new_id,meeting_id)
                VALUES (:old_id,:new_id,:meeting_id)
            """), merge["meeting_map"])
            if has_reservations:
                connection.execute(text("""
                    UPDATE agenda_item_key_reservation r SET meeting_db_id=m.new_id
                    FROM _merge_meeting_map m WHERE r.meeting_db_id=m.old_id
                """))
            _apply_row_policies(connection, "meetings", "_merge_meeting_map",
                                MEETING_MERGE_POLICIES,
                                set(MEETING_NON_MATERIAL))
            for table, column in inventory.get("meeting_references") or []:
                connection.execute(text(
                    f'UPDATE "{table}" t SET "{column}"=m.new_id '
                    f'FROM _merge_meeting_map m WHERE t."{column}"=m.old_id'))
            # Event contract (Brief 031D item 6): meeting_events.meeting_id is
            # the EXTERNAL source id and both duplicate meeting rows share it,
            # so merging bodies requires NO event rewrite.  Rewriting it to the
            # surviving database PK would corrupt provenance whenever a source
            # id happens to equal a deleted PK.  assert_event_contract() proves
            # afterwards that nothing moved.

            stats.update(_reparent_polymorphic(
                connection, "_merge_meeting_map", "meetings"))

            connection.execute(text("""
                DELETE FROM meetings o USING _merge_meeting_map m WHERE o.id=m.old_id
            """))

        if has_watermark:
            _merge_watermark(connection, old, new)
        # Every body rewrite goes through body_write_sql(), which honours the
        # table's DECLARED propagation mode (db.sync_declarations).  An
        # incremental table must advance its stamp or the corrected row is
        # invisible to the sync's ``updated_at > :since`` filter — nothing
        # errors and row counts still match.  Root cause: Brief 033 §1.
        # Verified consequence: Brief 037 §2.
        #
        # The stamp is applied when it is required AND the connected schema has
        # the column: a minimal-DDL fixture is legitimate maintenance and must
        # not be refused.  The stamp COLUMN requirement is asserted at the schema
        # level by ops.reference_integrity_gate.stamp_column_problems() against a
        # real database — not inferred per-write from whatever schema happens to
        # be connected.  An undeclared table raises (fail closed).
        from db.sync_declarations import body_write_sql

        def _rewrite_body(table: str, column: str) -> None:
            sql = body_write_sql(
                table, column,
                has_stamp="updated_at" in _columns(connection, table))
            connection.execute(text(sql), {"old": old, "new": new})

        for table in body_tables:
            if table in {"meetings", "_pattern_cascade_watermark"}:
                continue
            _rewrite_body(table, "body")
        _rewrite_body("meetings", "body")
        _rewrite_body("public_bodies", "body_code")

    problems = []
    for old in MERGES:
        for table in body_tables:
            count = int(connection.execute(text(
                f'SELECT count(*) FROM "{table}" WHERE body=:old'),
                {"old": old}).scalar() or 0)
            if count:
                problems.append(f"{table}:{old}:{count}")
        count = int(connection.execute(text(
            "SELECT count(*) FROM public_bodies WHERE body_code=:old"),
            {"old": old}).scalar() or 0)
        if count:
            problems.append(f"public_bodies:{old}:{count}")
    if problems:
        raise RuntimeError(f"postcondition old-code rows remain: {problems}")
    after = protected_snapshot(connection, capabilities, inventory)
    expected_counts = dict(plan["baseline"]["counts"])
    expected_counts["meetings"] -= sum(len(m["meeting_map"]) for m in plan["merges"])
    expected_counts["agenda_items"] -= sum(len(m["item_map"]) for m in plan["merges"])
    for merge in plan["merges"]:
        for table, pairs in (merge.get("twin_dedup_maps") or {}).items():
            expected_counts[table] -= len(pairs)
    if after["counts"] != expected_counts:
        raise RuntimeError(f"protected row-count mismatch expected={expected_counts} "
                           f"actual={after['counts']}")
    for key in ("meeting_orphans", "item_orphans", "vote_orphans"):
        if after[key] != plan["baseline"][key]:
            raise RuntimeError(f"{key} changed expected={plan['baseline'][key]} "
                               f"actual={after[key]}")
    stats.update({"meetings": after["counts"]["meetings"],
                  "agenda_items": after["counts"]["agenda_items"],
                  "meeting_orphans": sum(after["meeting_orphans"].values()),
                  "item_orphans": sum(after["item_orphans"].values())})

    # Event contract (Brief 031D item 6): prove no event key moved onto a PK.
    assert_event_contract(plan.get("event_contract") or {},
                          event_contract_snapshot(connection))
    return stats


def write_plan(plan: dict[str, Any], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"body-code-merge-plan-{stamp}.json"
    path.write_text(json.dumps(plan, indent=2, sort_keys=True, default=str) + "\n")
    return path
