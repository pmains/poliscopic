"""Contextual roles and participation bases (KG-INFORMATION-MODEL.md §6).

A role describes how an actor participates in a bounded context.  A
participation basis describes what the cited evidence actually proves, and
gates whether a claim may be promoted to attendance or completed action.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scripts.kg.registries.model import frozen

#: Allowed actor classes for a role.
ACTOR_CLASSES: tuple[str, ...] = ("person", "organization", "department", "any_actor")

#: Allowed context classes for a role.
CONTEXT_CLASSES: tuple[str, ...] = (
    "agenda_item", "agenda_subitem", "case", "parcel", "meeting", "event",
    "body", "jurisdiction", "agreement_event", "evidence",
)


@dataclass(frozen=True)
class RoleEntry:
    """One canonical contextual role."""

    slug: str
    allowed_actors: tuple[str, ...]
    allowed_contexts: tuple[str, ...]
    meaning: str


_RAW_ROLES: tuple[RoleEntry, ...] = (
    RoleEntry("applicant", ("person", "organization"), ("agenda_item", "case"),
              "Applies or speaks as applicant"),
    RoleEntry("owner", ("person", "organization"), ("agenda_item", "case", "parcel"),
              "Explicitly identified owner"),
    RoleEntry("attorney", ("person", "organization"), ("agenda_item", "case"),
              "Legal representative role"),
    RoleEntry("representative", ("person",), ("agenda_item", "case"),
              "Explicit representative or appearing agent"),
    RoleEntry("consultant", ("person", "organization"), ("agenda_item", "case"),
              "Explicit professional consultant"),
    RoleEntry("staff", ("person", "department"), ("meeting", "agenda_item", "event"),
              "Government staff role"),
    RoleEntry("presenter", ("person",), ("agenda_item", "event"),
              "Explicit presenter or speaker"),
    RoleEntry("chair", ("person",), ("meeting", "event"), "Presiding chair role"),
    RoleEntry("vice_chair", ("person",), ("meeting", "event"), "Presiding vice-chair"),
    RoleEntry("member", ("person",), ("body", "meeting"), "Body member role"),
    RoleEntry("commissioner", ("person",), ("body", "meeting"), "Commission member"),
    RoleEntry("board_member", ("person",), ("body", "meeting"), "Board member"),
    RoleEntry("participant", ("person", "organization"), ("event",),
              "Source-supported participation, role otherwise unknown"),
    RoleEntry("iga_counterparty", ("organization",), ("agenda_item", "agreement_event"),
              "Counterparty to an intergovernmental agreement"),
    RoleEntry("mentioned", ("any_actor",), ("evidence", "agenda_item"),
              "Mention with no stronger contextual role"),
    RoleEntry("reference", ("any_actor",), ("evidence", "agenda_item"),
              "Referential occurrence, not participation"),
)

ROLES: Mapping[str, RoleEntry] = frozen({entry.slug: entry for entry in _RAW_ROLES})


@dataclass(frozen=True)
class ParticipationBasis:
    """What a cited evidence class proves about an actor and context."""

    slug: str
    evidence_meaning: str
    permitted_claim: str


_RAW_BASES: tuple[ParticipationBasis, ...] = (
    ParticipationBasis(
        "agenda_listing",
        "A person or organization is named under an agenda item or role label",
        "Listed in that context only",
    ),
    ParticipationBasis(
        "scheduled_role",
        "The agenda schedules the actor to present, advise, speak, or act",
        "Scheduled participation only",
    ),
    ParticipationBasis(
        "observed_attendance",
        "Minutes, roll call, or structured attendance records presence",
        "Attendance in the stated meeting or context",
    ),
    ParticipationBasis(
        "observed_action",
        "Minutes, vote record, signature, or action record states the actor acted",
        "The exact evidenced action",
    ),
)

PARTICIPATION_BASES: Mapping[str, ParticipationBasis] = frozen(
    {basis.slug: basis for basis in _RAW_BASES}
)

#: Claims a basis can never promote to without corroborating evidence.
FORBIDDEN_PROMOTIONS: Mapping[str, tuple[str, ...]] = frozen({
    "agenda_listing": ("attendance", "completed_action"),
    "scheduled_role": ("attendance", "completed_action"),
    "observed_attendance": ("completed_action", "per_item_participation"),
    "observed_action": (),
})

#: Contexts whose actors may never be promoted to observed activity by
#: meeting-wide co-occurrence alone.
CO_OCCURRENCE_CONTEXTS: tuple[str, ...] = ("meeting",)


def get_role(slug: str) -> RoleEntry:
    """Return one role entry or raise ``KeyError`` for unregistered roles."""
    return ROLES[slug]


def role_allows_context(role: str, context_class: str) -> bool:
    """Return True when ``role`` may be asserted in ``context_class``."""
    entry = ROLES.get(role)
    return bool(entry) and context_class in entry.allowed_contexts


def basis_forbids(basis: str, promotion: str) -> bool:
    """Return True when ``basis`` may not support ``promotion``."""
    return promotion in FORBIDDEN_PROMOTIONS.get(basis, ())


#: Roles whose explicit extracted label may be retained in an evidence context
#: for a *mention*.  Listing a role here never makes it valid as participation
#: in a context that does not permit it.
MENTION_EVIDENCE_ROLES: tuple[str, ...] = (
    "applicant", "owner", "attorney", "representative", "consultant",
    "staff", "presenter", "participant", "mentioned", "reference",
)


def role_allows_mention_context(role: str, context_class: str) -> bool:
    """Return True when ``role`` may label a mention in ``context_class``.

    A mention records that a source described an actor with a contextual label.
    It asserts no participation, so an explicitly extracted label may be kept in
    evidence context even when the role's participation contexts do not include
    it.  This is deliberately broader than :func:`role_allows_context` and is
    only ever consulted for mention bundles.
    """
    entry = ROLES.get(role)
    if entry is None:
        return False
    if context_class == "evidence" and role in MENTION_EVIDENCE_ROLES:
        return True
    return context_class in entry.allowed_contexts
