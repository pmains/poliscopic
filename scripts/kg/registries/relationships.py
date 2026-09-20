"""Relationship predicate registry (KG-INFORMATION-MODEL.md §7).

Each canonical predicate has exactly one direction.  The inverse label is a
presentation/query convenience and never creates a second stored fact.
``edge_kind`` describes temporal behavior; ``assertion_class`` separately
distinguishes source-supported from derived knowledge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scripts.kg.registries.model import frozen, require_predicate

#: Canonical node classes that may appear as predicate endpoints (§4).
#: ``agenda_subitem`` is the subitem context named in §6.1; it is not a
#: separate top-level node class but is a valid containment child.
NODE_CLASSES: tuple[str, ...] = (
    "jurisdiction", "body", "meeting", "agenda_item", "agenda_subitem",
    "document", "person", "organization", "case", "parcel", "address",
    "event", "vote", "evidence",
)


@dataclass(frozen=True)
class PredicateEntry:
    """One canonical relationship predicate."""

    predicate: str
    domain: tuple[str, ...]
    range: tuple[str, ...]
    inverse_label: str
    kind: str
    required_support: str
    allowed_evidence_classes: tuple[str, ...]
    temporal_requirement: str


_RAW_PREDICATES: tuple[PredicateEntry, ...] = (
    PredicateEntry(
        "PART_OF", ("body", "meeting", "agenda_item", "agenda_subitem"),
        ("jurisdiction", "body", "meeting", "agenda_item"), "contains", "attributional",
        "Structured container FK/registry",
        ("structured_record",), "none",
    ),
    PredicateEntry(
        "ATTACHED_TO", ("document",), ("agenda_item", "meeting"), "has document",
        "attributional", "Structured attachment",
        ("structured_record",), "none",
    ),
    PredicateEntry(
        "INSTANCE_OF", ("agenda_item",), ("case",), "heard as item", "attributional",
        "Validated identifier",
        ("structured_record", "source_html", "source_pdf_text", "source_ocr"),
        "none",
    ),
    PredicateEntry(
        "CONCERNS", ("case",), ("parcel", "address"), "concerns", "attributional",
        "Explicit identifier/location field",
        ("structured_record", "source_html", "source_pdf_text", "source_ocr"), "none",
    ),
    PredicateEntry(
        "MEMBER_OF", ("person",), ("body",), "has member", "relational",
        "Membership record", ("structured_record",), "valid_time_when_available",
    ),
    PredicateEntry(
        "PRESENT_AT", ("person",), ("meeting",), "had attendee", "relational",
        "Attendance record", ("structured_record", "vote_or_attendance_record",
                              "minutes_or_summary"),
        "observation_time_required",
    ),
    PredicateEntry(
        "PARTICIPATED_IN", ("person", "organization"), ("agenda_item", "event"),
        "had participant", "relational", "Context-specific participation evidence",
        ("structured_record", "minutes_or_summary", "source_pdf_text", "source_ocr",
         "source_html"),
        "context_required",
    ),
    PredicateEntry(
        "EMPLOYED_BY", ("person",), ("organization",), "employs", "relational",
        "Explicit employment evidence",
        ("structured_record", "minutes_or_summary", "source_pdf_text"), "known_scope",
    ),
    PredicateEntry(
        "AFFILIATED_WITH", ("person",), ("organization",), "has affiliate",
        "relational",
        "Explicit with/of affiliation language naming both parties",
        ("structured_record", "minutes_or_summary", "source_pdf_text",
         "source_ocr", "source_html"),
        "known_scope",
    ),
    PredicateEntry(
        "REPRESENTS", ("person", "organization"), ("person", "organization"),
        "represented by", "relational", "Explicit representation statement",
        ("structured_record", "minutes_or_summary", "source_pdf_text", "source_html"),
        "known_scope",
    ),
    PredicateEntry(
        "APPLIED_FOR", ("person", "organization"), ("case",), "has applicant",
        "relational", "Explicit applicant evidence",
        ("structured_record", "source_pdf_text", "source_ocr", "source_html"),
        "none",
    ),
    PredicateEntry(
        "OWNS", ("person", "organization"), ("parcel", "case"), "owned by",
        "relational", "Explicit ownership evidence",
        ("structured_record", "source_pdf_text", "source_ocr"), "known_scope",
    ),
    PredicateEntry(
        "CONSULTED_ON", ("person", "organization"), ("case", "agenda_item"),
        "had consultant", "relational", "Explicit consultant evidence",
        ("structured_record", "source_pdf_text", "source_ocr", "source_html"), "none",
    ),
    PredicateEntry(
        "OCCURRED_IN", ("event", "vote"), ("agenda_item", "meeting"), "had event",
        "attributional", "Source context",
        ("structured_record", "minutes_or_summary", "source_pdf_text", "source_ocr"),
        "observation_time_required",
    ),
    PredicateEntry(
        "ABOUT", ("event", "vote"), ("case",), "had event", "attributional",
        "Explicit identifier linkage", ("structured_record",), "none",
    ),
    PredicateEntry(
        "CAST", ("person",), ("vote",), "cast by", "relational", "Structured roll call",
        ("structured_record", "vote_or_attendance_record"), "observation_time_required",
    ),
    PredicateEntry(
        "DECIDED", ("vote",), ("agenda_item",), "decided by vote", "attributional",
        "Structured action/vote record",
        ("structured_record", "vote_or_attendance_record", "minutes_or_summary"),
        "observation_time_required",
    ),
)

PREDICATES: Mapping[str, PredicateEntry] = frozen({
    entry.predicate: entry for entry in _RAW_PREDICATES
})

#: Historical predicates that stay readable under explicit compatibility rules.
HISTORICAL_PREDICATES: tuple[str, ...] = (
    "HAS_APPLICANT", "HAS_OWNER", "HAS_ATTORNEY", "HAS_STAFF",
    "HAS_RECOMMENDATION", "REPRESENTED",
)

#: Edge kinds are structural; they never encode assertion class.
EDGE_KINDS: tuple[str, ...] = ("relational", "attributional")


def get_predicate(predicate: str) -> PredicateEntry:
    """Return one canonical predicate entry."""
    require_predicate(predicate, what="relationship predicate")
    return PREDICATES[predicate]


def direction_allows(predicate: str, domain_class: str, range_class: str) -> bool:
    """Return True when the class pair matches the canonical direction."""
    entry = PREDICATES.get(predicate)
    if entry is None:
        return False
    return domain_class in entry.domain and range_class in entry.range


def evidence_allows(predicate: str, evidence_class: str) -> bool:
    """Return True when the evidence class may support the predicate."""
    entry = PREDICATES.get(predicate)
    if entry is None:
        return False
    return evidence_class in entry.allowed_evidence_classes
