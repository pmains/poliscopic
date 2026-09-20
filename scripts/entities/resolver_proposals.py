"""Typed resolver proposals: composite 'Person, Firm' splits.

Extracted from :mod:`scripts.entities.resolver` so the orchestration module stays
focused on classification and aggregation.

Proposal building is **identical in dry and live runs**.  The original builder
skipped construction entirely in dry mode, which meant a dry run could not
describe what a live run would do — the two modes must propose the same work and
differ only in whether it is written.

Nothing here writes to the database.
"""

from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

from sqlalchemy import text

__all__ = [
    "COMPOSITE_PATTERN",
    "CompositeOp",
    "PHASE3_ORGS",
    "SPLIT_EVIDENCE_CLASS",
    "SPLIT_ORG_ROLE",
    "build_composite_ops",
    "TYPE_PRIORITY",
    "build_name_variation_candidates",
    "type_priority",
    "organization_mention_bundle",
    "organization_mention_evidence",
]

#: Canonical role for an organisation mention materialised by a split.  The
#: split proves the organisation was *named* in the source occurrence; it proves
#: nothing stronger, so this is the weakest truthful role.
SPLIT_ORG_ROLE = "mentioned"

#: Evidence class of the stored agenda-item mention row the split is derived from.
SPLIT_EVIDENCE_CLASS = "structured_record"

#: ``"Person, Firm"`` — a capitalised name, comma, then the organisation.
COMPOSITE_PATTERN = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}),\s+(.+)$"
)

PHASE2_COMPOSITES = """
    SELECT id, name, normalized_name, entity_type
    FROM entities
    WHERE resolution_status = 'unresolved'
      AND entity_type IN ('organization', 'person')
    ORDER BY id
"""

#: Organisation suffixes that carry no identity on their own.
_ROLE_ONLY_ALL = {"agent", "agents", "rls", "esq", "jr", "sr", "pe",
                  "pls", "pc", "plc", "llc", "inc", "iii"}

#: Person-side values that are titles or boilerplate, not names.
_TITLE_WORDS = {"councilmember", "chairperson", "chairman", "chairwoman",
                "mayor", "vice mayor", "councilman", "councilwoman",
                "commissioner", "vice chair", "proposed request"}


class CompositeOp(NamedTuple):
    """One composite entity proposed for splitting.

    A ``NamedTuple`` so it both unpacks positionally (the persistence layer
    consumes it that way) and reads by name at the call site.
    """

    entity_id: int
    name: str
    person_name: str
    org_name: str
    person_norm: str
    org_norm: str


def build_composite_ops(conn) -> list[CompositeOp]:
    """Return every composite split proposal, without consulting ``dry_run``.

    Filtering rules are unchanged from the original builder; only the dry-mode
    early exit was removed, so dry and live now propose the same set.
    """
    rows = conn.execute(text(PHASE2_COMPOSITES)).fetchall()
    ops: list[CompositeOp] = []

    for row in rows:
        entity_id = int(row[0])
        name = str(row[1] or "")

        match = COMPOSITE_PATTERN.match(name)
        if not match:
            continue

        person_name = match.group(1).strip()
        org_name = match.group(2).strip()
        if not person_name or not org_name:
            continue

        # Skip role-only suffixes
        org_check = re.sub(r"[^a-zA-Z ]", " ", org_name.lower()).strip()
        org_all_tokens = org_check.split()
        if org_all_tokens and all(t in _ROLE_ONLY_ALL for t in org_all_tokens):
            continue

        # Skip title-like person names
        person_lower = re.sub(r"[^a-zA-Z ]", " ", person_name.lower()).strip()
        if person_lower in _TITLE_WORDS or person_lower.startswith("proposed"):
            continue

        # Skip "Jr., X"
        if re.search(r"\bjr\.?$", person_name, re.I):
            continue

        ops.append(CompositeOp(
            entity_id=entity_id,
            name=name,
            person_name=person_name,
            org_name=org_name,
            person_norm=re.sub(r"\s+", " ", person_name.lower().strip()),
            org_norm=re.sub(r"\s+", " ", org_name.lower().strip()),
        ))

    return ops


PHASE3_ORGS = """
    SELECT id, name, normalized_name, entity_type, resolution_block_key
    FROM entities
    WHERE resolution_status = 'unresolved'
      AND entity_type IN ('organization', 'developer', 'planning_firm', 'law_firm')
    ORDER BY normalized_name
"""


def organization_mention_evidence(*, source_type: str, source_id, source_text: str,
                                  org_name: str):
    """Return the exact evidence identity of the occurrence a split read.

    The content hash is of *exactly the stored text we read*, and the span marks
    where the organisation was named inside it — so the split's evidence is the
    real occurrence, not a re-derived paraphrase.
    """
    from scripts.kg.identity_keys import evidence_identity

    span_start = None
    span_end = None
    located = source_text.lower().find(org_name.lower())
    if located >= 0:
        span_start = located
        span_end = located + len(org_name)
    return evidence_identity(
        source_type=source_type,
        source_id=str(source_id),
        content_hash=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        span_start=span_start,
        span_end=span_end,
    )


def organization_mention_bundle(*, source_type: str, source_id, source_text: str,
                                org_name: str, model_version: str):
    """Build the canonical, source-supported mention bundle for a split's org.

    The organisation was named in the same occurrence, so the bundle carries the
    exact evidence identity and the weakest truthful role.  No relationship is
    expressed here: punctuation alone is not affiliation evidence.
    """
    from scripts.kg.emission_bundles import EmissionBundle

    evidence = organization_mention_evidence(
        source_type=source_type, source_id=source_id,
        source_text=source_text, org_name=org_name,
    )
    return EmissionBundle(
        kind="mention",
        entity_type="organization",
        role=SPLIT_ORG_ROLE,
        context_class="evidence",
        context_identity=evidence,
        evidence_class=SPLIT_EVIDENCE_CLASS,
        assertion_class="source_supported",
        model_version=model_version,
        evidence_identity=evidence,
    )


def build_name_variation_candidates(conn) -> list[dict]:
    """Return the organisation candidates for name-variation matching.

    These are *candidates for comparison*, not proposals: whether any pair
    becomes a proposal is decided by blocking and scoring downstream.
    """
    rows = conn.execute(text(PHASE3_ORGS)).fetchall()
    return [
        {
            "id": int(row[0]),
            "name": str(row[1] or ""),
            "norm": str(row[2] or ""),
            "type": str(row[3] or ""),
            "block": str(row[4] or ""),
        }
        for row in rows
    ]


#: Survivor precedence when two entities share a normalised name but differ in
#: type.  Lower wins; unlisted types are least preferred.
TYPE_PRIORITY: dict[str, int] = {
    "person": 0, "developer": 1, "planning_firm": 1,
    "law_firm": 1, "organization": 2, "utility": 3,
    "advocacy_group": 3, "case": 4, "parcel": 5,
    "address": 6,
}


def type_priority(entity_type: str) -> int:
    """Return the survivor precedence of ``entity_type`` (lower is preferred)."""
    return TYPE_PRIORITY.get(entity_type, 99)
