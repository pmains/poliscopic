#!/usr/bin/env python3
"""Declarations for the dev→prod sync: table membership, ordering, and constants.

Extracted from ``scripts/db/sync_prod.py`` as a behavior-preserving split; the
code below is unchanged.  ``scripts/db/sync_prod.py`` remains the CLI facade.
"""

from __future__ import annotations

import os
import sys

# Make the shared modules importable however this module is invoked.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)


import os



LOCK_ID = 184_729_583  # arbitrary bigint for pg_advisory_lock


BATCH_SIZE = int(os.environ.get("BATCH_SIZE", "2000"))


BATCH_SLEEP_S = int(os.environ.get("BATCH_SLEEP_MS", "100")) / 1000.0


SYNC_MODE = os.environ.get("SYNC_MODE", "incremental").lower()



ALL_SYNC_TABLES = [
    # Parent-first apply order. This is the reverse of the reviewed
    # children-first RECONCILE_ORDER (plus entity_types, which is not reconciled).
    # Keep the strict parity test in sync_reference.py green when changing either.
    "entity_types",
    "entities",
    "jurisdictions",
    "public_bodies",
    "persons",
    "meetings",
    "pz_item_details",
    "supporting_documents",
    "agenda_items",
    "cases",
    "entity_relationships",
    "entity_mentions",
    "case_events",
    "meeting_members",
    "body_seats",
    "body_memberships",
    "member_votes",
    "agenda_item_votes",
    "meeting_attendance",
    "executive_session_participants",
    "meeting_event_types",
    "meeting_events",
    "meeting_event_extractions",
    "event_participants",
]



# Tables that should always do a full sync (no updated_at, or very small)
# entity_mentions has no updated_at column — always do full sync
# Event tables have no updated_at column — always full-sync
_EVENT_TABLES = {"meeting_event_types", "meeting_event_extractions",
                 "meeting_events", "event_participants"}



# entity_types has no updated_at column — always full sync (like event tables)
_ENTITY_TAXONOMY_TABLES = {"entity_types"}



# Reference tables MUST be re-sent in FULL, never incrementally. Under an
# ``updated_at > checkpoint`` filter a dev parent that is absent on the target but
# whose timestamp predates the checkpoint is never re-selected, which is exactly how
# production ended up with meetings whose public_bodies parent was missing.
# These tables are small, so a full re-send every sync is cheap insurance.
_REFERENCE_TABLES = {"public_bodies", "jurisdictions"}


FULL_SYNC_TABLES: set[str] = (_EVENT_TABLES | _ENTITY_TAXONOMY_TABLES
                              | _REFERENCE_TABLES)



# Auto-generated / derived columns to exclude from sync
AUTO_COLUMNS: dict[str, set[str]] = {
    "supporting_documents": {"search_vector"},
}



EXCLUDED_TABLES = {
    "admin_users", "admin_notifications", "article_sources", "article_tags",
    "articles", "dismissed_suggestions", "media_images", "public_body_members",
    "scanned_agenda_text", "skeet_drafts", "tags", "topic_weekly_reports",
    "topics",
}


# ── Propagation contract ──
#
# Every table that can receive a write must declare HOW that write reaches
# production.  This is the domain contract that the earlier "a table is synced
# iff it has an updated_at column" observation got wrong: that was inferred from
# whichever schema happened to be connected, so it refused legitimate
# maintenance (a test fixture with minimal DDL, a table synced wholesale).
#
#   incremental     stamped rows, propagated by  WHERE updated_at > :since
#   full_reference  small/reference tables re-sent in full on every sync; the
#                   stamp is irrelevant because there is no incremental filter
#   excluded        deliberately not propagated (local, derived, or editorial)
#
# A write to a table with no declared mode cannot be reasoned about, so
# ``propagation_mode`` fails closed rather than guessing.

PROPAGATION_INCREMENTAL = "incremental"
PROPAGATION_FULL_REFERENCE = "full_reference"
PROPAGATION_EXCLUDED = "excluded"

# Local-only tables that are never propagated (internal markers, not content).
LOCAL_ONLY_TABLES = {
    "_ingest_failures",
    "_pattern_cascade_watermark",
}


def _build_propagation() -> dict[str, str]:
    modes: dict[str, str] = {}
    for table in ALL_SYNC_TABLES:
        modes[table] = (PROPAGATION_FULL_REFERENCE
                        if table in FULL_SYNC_TABLES
                        else PROPAGATION_INCREMENTAL)
    for table in EXCLUDED_TABLES | LOCAL_ONLY_TABLES:
        modes.setdefault(table, PROPAGATION_EXCLUDED)
    return modes


PROPAGATION: dict[str, str] = _build_propagation()


# ── Mutation-audit stamping (a SEPARATE concern from transfer strategy) ──
#
# Transfer strategy answers "how does this table reach production?" (incremental
# filter vs full re-send). Audit stamping answers "must a write to this table
# advance updated_at?". These are independent: a table can be full-reference for
# transfer and STILL be required to stamp its writes.
#
# Why the separation matters (Brief 037):
# ``body_code_merge_runtime.py:1589`` renamed ``public_bodies.body_code`` in place
# WITHOUT touching ``updated_at``, so the mutation was invisible to the incremental
# filter — permanently. Stamping is the defense-in-depth that makes such a write
# observable to audits and to any incremental consumer, and it must keep applying
# even though ``public_bodies`` is now transferred in full.
AUDIT_STAMP_TABLES: set[str] = {
    "public_bodies", "jurisdictions", "meetings", "member_votes", "agenda_items",
}

# Tables that must carry a change-detection stamp for their writes to be observed.
STAMP_REQUIRED_TABLES: set[str] = (
    {t for t, m in PROPAGATION.items() if m == PROPAGATION_INCREMENTAL}
    | AUDIT_STAMP_TABLES
)


def propagation_mode(table: str) -> str:
    """Declared propagation class for ``table``; fails closed if undeclared."""
    try:
        return PROPAGATION[table]
    except KeyError:
        raise KeyError(
            f"{table!r} has no declared propagation mode. A write to it cannot "
            "be reasoned about — add it to ALL_SYNC_TABLES, FULL_SYNC_TABLES, "
            "EXCLUDED_TABLES or LOCAL_ONLY_TABLES in db/sync_declarations.py "
            "before writing to it"
        ) from None


def stamp_required(table: str) -> bool:
    """True when a write to ``table`` must advance its stamp.

    Deliberately NOT the same question as "is this table transferred
    incrementally". Transfer strategy and mutation-audit stamping are separate
    concepts: a full-reference table is re-sent wholesale, yet its writes must
    still stamp so audits — and any incremental consumer — observe the mutation.
    """
    propagation_mode(table)  # raises when undeclared
    return table in STAMP_REQUIRED_TABLES


def body_write_sql(table: str, column: str, *, has_stamp: bool) -> str:
    """SQL for a body-code rewrite that honours the propagation contract.

    This is the single sanctioned way to build a body-code UPDATE.  It stamps
    whenever stamping is required AND possible:

      * transfer class no longer decides this — a table may be ``full_reference``
        for transfer and still require an audit stamp (see AUDIT_STAMP_TABLES);
      * ``excluded`` / non-audited tables -> no stamp; propagation does not depend
        on one and no audit requires it;
      * a required stamp is skipped only when the connected schema has no
        ``updated_at`` (a minimal-DDL fixture or a local marker table legitimately
        may not).

    An undeclared table raises through ``propagation_mode``.

    The stamp-column requirement is enforced at the schema level by
    ``ops.reference_integrity_gate.stamp_column_problems()`` — so a stamp that
    is skipped here because the column is absent in a fixture is never silently
    skipped against a real database.
    """
    propagation_mode(table)  # raises when undeclared
    if not stamp_required(table) or not has_stamp:
        return f'UPDATE "{table}" SET {column}=:new WHERE {column}=:old'
    return (
        f'UPDATE "{table}" SET updated_at = CURRENT_TIMESTAMP, '
        f'{column}=:new WHERE {column}=:old'
    )




# ── Reconcile (delete propagation) ──
#
# Sync is upsert-only; rows deleted on dev are never removed from prod.
# _reconcile_table() deletes prod rows whose PK is absent from dev, so prod
# converges to dev instead of accumulating zombies. Run AFTER upserts, in
# FK-safe order (children before parents — see RECONCILE_ORDER).

# Children first: event tables (which FK to agenda_items/supporting_documents)
# before those parents, entity_relationships before entities, etc.
RECONCILE_ORDER = [
    "event_participants",
    "meeting_event_extractions",
    "meeting_events",
    "meeting_event_types",
    "executive_session_participants",
    "meeting_attendance",
    "agenda_item_votes",
    "member_votes",
    "body_memberships",
    "body_seats",
    "meeting_members",
    "case_events",
    "entity_mentions",
    "entity_relationships",
    "cases",
    "agenda_items",
    "supporting_documents",
    "pz_item_details",
    "meetings",
    "persons",
    "public_bodies",
    "jurisdictions",
    "entities",
]
