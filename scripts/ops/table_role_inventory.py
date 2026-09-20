#!/usr/bin/env python3
"""Proposed table-role declaration for the propagation contract v2.

Brief 038 §2 replaced a schema observation ("synced iff it has updated_at") with
declared *propagation* classes.  This goes one step further and classifies by
**role**, because propagation behaviour follows from what a table *is*, not from
which columns happen to exist.

    authoritative_reference  small authoritative registry; FULL reconciliation
                             with fingerprints and protected-retirement rules.
                             Never timestamp-only: a rename or a retirement must
                             converge even when no stamp moved.
    transactional_source     ingested/derived content; increment AL with an
                             explicit change-capture contract (stamp required).
    derived_rebuildable      derived/FTS/cache; NOT synced row-wise, rebuilt
                             deterministically on the target.
    local_excluded           operational/local/prod-only; NEVER propagated.
    undeclared               hard refusal.

This module is a DECLARATION ARTIFACT for review: it emits the classification and
rationale, and is not yet wired into the sync engine.  Wiring it is a reviewed
batch (Brief 039 §D), deliberately not done during containment.

Read-only with respect to databases.

Usage:
    python3 scripts/ops/table_role_inventory.py [--out PATH] [--db]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

AUTHORITATIVE = "authoritative_reference"
TRANSACTIONAL = "transactional_source"
DERIVED = "derived_rebuildable"
LOCAL = "local_excluded"
UNDECLARED = "undeclared"

# ── the declaration ──────────────────────────────────────────────────────
# (role, sync_behavior, rationale, needs_review)
ROLES: dict[str, tuple[str, str, str, bool]] = {
    # authoritative registries — full reconciliation, protected retirement
    "public_bodies": (AUTHORITATIVE,
        "full upsert of the authoritative set + fingerprint; duplicate/ambiguous "
        "canonical identity REFUSED; row removal only via an explicit retirement "
        "or alias plan",
        "registry that every body reference resolves through; a rename or a "
        "retirement must converge even when no stamp moved (Brief 038 §2)", False),
    "jurisdictions": (AUTHORITATIVE,
        "full upsert + fingerprint; protected retirement",
        "small authoritative registry referenced by every body and meeting", False),
    "entity_types": (AUTHORITATIVE,
        "full upsert + fingerprint",
        "small closed taxonomy; already full-synced today", False),
    "meeting_event_types": (AUTHORITATIVE,
        "full upsert + fingerprint",
        "small closed taxonomy; already full-synced today", False),

    # transactional / source content — incremental with change capture
    "meetings": (TRANSACTIONAL, "incremental, stamp required",
        "ingested meeting records; largest body-bearing table", False),
    "agenda_items": (TRANSACTIONAL, "incremental, stamp required",
        "ingested agenda items", False),
    "agenda_item_votes": (TRANSACTIONAL, "incremental, stamp required",
        "vote records derived from agendas", False),
    "member_votes": (TRANSACTIONAL, "incremental, stamp required",
        "per-member vote records", False),
    "meeting_members": (TRANSACTIONAL, "incremental, stamp required",
        "meeting-scoped membership rows", False),
    "body_memberships": (TRANSACTIONAL, "incremental, stamp required",
        "body-scoped membership records", False),
    "body_seats": (TRANSACTIONAL, "incremental, stamp required",
        "seat definitions per body", False),
    "meeting_attendance": (TRANSACTIONAL, "incremental, stamp required",
        "attendance records", False),
    "executive_session_participants": (TRANSACTIONAL, "incremental, stamp required",
        "executive-session participation", False),
    "case_events": (TRANSACTIONAL, "incremental, stamp required",
        "case/event rows linked to meetings", False),
    "cases": (TRANSACTIONAL, "incremental, stamp required",
        "case records", False),
    "supporting_documents": (TRANSACTIONAL, "incremental, stamp required",
        "document records and URLs", False),
    "pz_item_details": (TRANSACTIONAL, "incremental, stamp required",
        "planning-and-zoning item detail", False),
    "entities": (TRANSACTIONAL, "incremental, stamp required",
        "resolved entities from the extraction pipeline", False),
    "entity_mentions": (TRANSACTIONAL, "incremental, stamp required",
        "entity mentions; currently carried by full sync", True),
    "entity_relationships": (TRANSACTIONAL, "incremental, stamp required",
        "entity graph edges", False),
    "persons": (TRANSACTIONAL, "incremental, stamp required",
        "people dimension produced by identity resolution", True),
    "meeting_events": (TRANSACTIONAL, "incremental, stamp required",
        "event rows extracted from agendas; today carried by full sync", True),
    "meeting_event_extractions": (TRANSACTIONAL, "incremental, stamp required",
        "extraction payloads; plausibly rebuildable — needs review", True),
    "event_participants": (TRANSACTIONAL, "incremental, stamp required",
        "participants per event; today carried by full sync", True),

    # derived / cache — rebuilt, never row-synced
    "document_text_chunks": (DERIVED, "excluded from row sync; rebuilt on target",
        "derived chunking of document text", False),
    "scanned_agenda_text": (DERIVED, "excluded from row sync; rebuilt on target",
        "OCR/extraction output; deterministically re-derivable", False),
    "topic_weekly_reports": (DERIVED, "excluded from row sync; regenerated",
        "generated weekly reports, not source data", True),
    "_detect_entities_watermark": (DERIVED, "not propagated",
        "pipeline progress marker", False),
    "_event_extract_watermark": (DERIVED, "not propagated",
        "pipeline progress marker", False),
    "_graph_builder_watermark": (DERIVED, "not propagated",
        "pipeline progress marker", False),
    "_pattern_cascade_watermark": (DERIVED, "not propagated",
        "entity-pipeline progress marker (Brief 035)", False),
    "_phase5_extractor_watermark": (DERIVED, "not propagated",
        "pipeline progress marker", False),
    "_resolver_watermark": (DERIVED, "not propagated",
        "pipeline progress marker", False),
    "_sweep_docs_watermark": (DERIVED, "not propagated",
        "pipeline progress marker", False),

    # local / prod-only — must never be propagated
    "newsletter_subscribers": (LOCAL, "NEVER propagated",
        "REAL subscriber data owned by production; syncing dev→prod would corrupt "
        "it and could cross the double-opt-in boundary", False),
    "newsletter_subscriptions": (LOCAL, "NEVER propagated",
        "subscription state owned by production", False),
    "newsletter_submit_log": (LOCAL, "NEVER propagated",
        "service request log", False),
    "permits": (LOCAL, "NEVER propagated; historical rows preserved",
        "retired product data with no route, scraper dispatch, or sync writer", False),
    "permit_reports": (LOCAL, "NEVER propagated; historical rows preserved",
        "retired permit-report data with no active product consumer", False),
    "_sync_meta": (LOCAL, "NEVER propagated",
        "sync bookkeeping; propagating it would corrupt the change-detection "
        "checkpoints it records", False),
    "_ingest_failures": (LOCAL, "NEVER propagated", "local operational log", False),
    "agenda_item_key_reservation": (LOCAL, "NEVER propagated",
        "staging/key reservation table", False),
    "admin_users": (LOCAL, "NEVER propagated", "local access control", False),
    "admin_notifications": (LOCAL, "NEVER propagated", "local notifications", False),
    "dismissed_suggestions": (LOCAL, "NEVER propagated",
        "editorial review state", False),
    "media_images": (LOCAL, "NEVER propagated", "local media library", False),
    "skeet_drafts": (LOCAL, "NEVER propagated", "draft social posts", False),
    "tags": (LOCAL, "NEVER propagated",
        "editorial taxonomy; published via the editorial path", False),
    "topics": (LOCAL, "NEVER propagated",
        "editorial taxonomy; published via the editorial path", False),
    "articles": (LOCAL, "NEVER propagated (separate editorial release path)",
        "editorial content with its own receipted publication path", False),
    "article_sources": (LOCAL, "NEVER propagated (separate editorial path)",
        "editorial provenance", False),
    "article_tags": (LOCAL, "NEVER propagated", "editorial join table", False),
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=_REPO_ROOT / "data" / "audit")
    ap.add_argument("--db", action="store_true",
                    help="also list tables present in the development catalog")
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    catalog: list[str] = []
    if args.db:
        from sqlalchemy import text
        from db.core import get_engine
        with get_engine().connect() as c:
            catalog = [r[0] for r in c.execute(text("""
                select table_name from information_schema.tables
                where table_schema='public' order by table_name""")).fetchall()]

    declared = {
        t: {"role": role, "sync_behavior": behavior, "rationale": why,
            "needs_review": review}
        for t, (role, behavior, why, review) in sorted(ROLES.items())
    }
    undeclared_live = sorted(t for t in catalog if t not in declared) if args.db else []

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    inv = {
        "kind": "table-role-inventory",
        "created_at": stamp,
        "status": "PROPOSED — declaration artifact, not yet wired into the sync engine",
        "roles": {
            "authoritative_reference": "full reconciliation + fingerprints + protected retirement",
            "transactional_source": "incremental with an explicit change-capture contract",
            "derived_rebuildable": "excluded from row sync; rebuilt deterministically",
            "local_excluded": "never propagated",
            "undeclared": "hard refusal",
        },
        "declared_count": len(declared),
        "catalog_count": len(catalog),
        "undeclared_in_catalog": undeclared_live,
        "coverage_complete": not undeclared_live,
        "tables": declared,
    }
    out = args.out / f"table-role-inventory-{stamp}.json"
    out.write_text(json.dumps(inv, indent=2))
    os.chmod(out, 0o600)

    by_role: dict[str, int] = {}
    for v in declared.values():
        by_role[v["role"]] = by_role.get(v["role"], 0) + 1
    print("=" * 74)
    print("TABLE ROLE INVENTORY (proposed)")
    print("=" * 74)
    for role in (AUTHORITATIVE, TRANSACTIONAL, DERIVED, LOCAL):
        print(f"  {role:<26} {by_role.get(role, 0)}")
    print(f"  undeclared in catalog      {len(undeclared_live)}")
    for t in undeclared_live:
        print(f"      {t}")
    print(f"  needs_review flags         "
          f"{sum(1 for v in declared.values() if v['needs_review'])}")
    print(f"  artifact {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
