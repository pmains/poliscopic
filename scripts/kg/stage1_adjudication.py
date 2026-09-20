#!/usr/bin/env python3
"""``stage1_adjudication.py`` — the approved human adjudication for Stage 1.

This module is the single record of the approved decision.  It supplies the human
quarantine fields, the canonical body names, and the handling of the document whose
meeting identity was rejected.

The decision **does not** authorize a migration/data apply or a backup.  Both
remain separately gated, and :data:`ADJUDICATION` records that explicitly so no
downstream component can infer more authority than was granted.

Document provenance
-------------------
Meeting ``15841`` is confirmed not to be a meeting, so document ``112947`` may not
be attached to it.  The document *is*, however, associated with the Enhanced
Municipal Services District Advisory Board at the body level.  That is a real fact
and is preserved as retained source metadata with a future explicit attachment
claim — it is not erased, not converted into meeting/event provenance, and no
unrelated meeting is invented to hold it.
"""

from __future__ import annotations

from typing import Any, Mapping

__all__ = [
    "ADJUDICATION",
    "AuthorizationError",
    "FORBIDDEN_APPROVAL_TIME_FIELDS",
    "UNAVAILABLE_STATUSES",
    "assert_authorization_provenance",
    "DOCUMENT_PROVENANCE",
    "canonical_body_names",
    "document_provenance",
    "human_fields",
    "quarantine_values",
    "validate_authorization_provenance",
]

#: The approved decision, verbatim in substance.
ADJUDICATION: Mapping[str, Any] = {
    "decision_id": "kg-stage1-20260911-skip-meeting-15841",
    "adjudicator": "Peter Mains",
    "decided_at": "2026-09-12T00:28:45Z",
    "decision": "approved",
    "canonical_bodies": {
        "phoenix-dr": "Phoenix Design Review Committee",
        "phoenix-dab": "Phoenix Development Advisory Board",
        "phoenix-ds": "Phoenix Design Standards Committee",
    },
    "quarantine_reason": "scraper_sentinel_non_meeting",
    "quarantine_extraction_ids": (31757, 31758, 31759, 31760, 31761, 31762, 31763,
                                  31764, 31765, 51301, 51302, 51303, 51304, 51305,
                                  51306, 51307, 51308, 51309),
    "rejected_meeting_id": 15841,
    "meeting_15841_is_a_meeting": False,
    "document_id": 112947,
    "body_level_association": "Enhanced Municipal Services District Advisory Board",
    "approval": {
        # Who approved, what they said, and where — all bound as observed facts.
        "approved_by": "project owner",
        "approval_text": "I approve",
        "channel": "codex_thread",
        "thread_id": "<manager-thread-id>",
        # The approval message carried no timestamp we could observe, and none is
        # inferred: a message timestamp is recorded only when actually read from the
        # source, so absence is recorded as absence.
        "message_timestamp": None,
        "message_timestamp_status": "unavailable",
        # No message id was observed either, so none is invented.
        "message_id": None,
        "message_id_status": "not_inferred",
    },
    "authorization": {
        "apply_authorized": True,
        "backup_authorized": True,
        "authorization_scope": (
            "Development-only protected backup of poliscopic_dev and the adjudicated "
            "392-record development apply (374 repairs + 18 quarantines)."
        ),
        # Machine-generated when this authorization record was created.  This is NOT
        # the moment of approval: that time is unavailable (see "approval").
        "recorded_at": "2026-09-12T02:03:33Z",
        "recorded_at_source": (
            "machine clock at authorization-record creation; not the approval message "
            "time, which is unavailable"
        ),
        "note": (
            "Authorized for development only. Production access, sync, deploy, alerts, "
            "event_normalize/orchestration gates, and orchestration receipt enforcement "
            "remain out of scope and unauthorized."
        ),
    },
}

#: How the rejected meeting identity and the retained body association are modelled.
DOCUMENT_PROVENANCE: Mapping[str, Any] = {
    "supporting_document_id": ADJUDICATION["document_id"],
    "rejected_meeting_id": ADJUDICATION["rejected_meeting_id"],
    "meeting_association": "rejected_not_a_meeting",
    "meeting_association_disposition": (
        "Meeting 15841 is confirmed not to be a meeting; the document must not be "
        "attached to it and no replacement meeting is invented."
    ),
    "body_level_association": ADJUDICATION["body_level_association"],
    "body_association_preserved": True,
    # Observed, read-only, at decision time.
    "stored_body_value": "__skip__",
    "stored_meeting_db_id": ADJUDICATION["rejected_meeting_id"],
    "candidate_column": "supporting_documents.body",
    "canonical_body_row_exists": False,
    "faithful_representation_available": False,
    "blocked_on": (
        "no approved canonical public_bodies row exists for the EMSD Advisory Board, "
        "so supporting_documents.body cannot faithfully carry the association"
    ),
    "body_association_handling": "retained_source_metadata",
    "attachment_claim": {
        "kind": "document_body_association",
        "status": "future_explicit_attachment",
        "mutations_proposed": 0,
        "rationale": (
            "No existing schema column represents a document-to-body association "
            "faithfully, so the association is recorded as retained source metadata "
            "and a future explicit attachment claim rather than guessed at."
        ),
    },
    "mutations_proposed": 0,
    "note": (
        "The quarantine touches only the 18 extraction rows.  It does not modify the "
        "document, does not rewrite its body column, and creates no meeting or event "
        "provenance."
    ),
}


def canonical_body_names() -> dict[str, str]:
    """The approved canonical name for each repairable body."""
    return dict(ADJUDICATION["canonical_bodies"])


def human_fields() -> dict[str, Any]:
    """The human quarantine fields supplied by the adjudication."""
    return {
        "quarantined_by": ADJUDICATION["adjudicator"],
        "decision_id": ADJUDICATION["decision_id"],
        "quarantined_at": ADJUDICATION["decided_at"],
    }


def quarantine_values(model_version: str) -> dict[str, Any]:
    """The complete quarantine values — no placeholders remain."""
    values = {
        "quarantine_reason": ADJUDICATION["quarantine_reason"],
        "model_version": model_version,
        **human_fields(),
    }
    values.update({
        "human_fields_supplied": True,
        "apply_blocked_until_supplied": False,
        "apply_blocked_by_authorization": not ADJUDICATION["authorization"]["apply_authorized"],
    })
    return values


def document_provenance() -> dict[str, Any]:
    """The record of the document's rejected meeting and retained body association."""
    return dict(DOCUMENT_PROVENANCE)


class AuthorizationError(ValueError):
    """The authorization provenance is not honestly recorded."""


#: Field names that would present an approval timestamp we cannot support.
FORBIDDEN_APPROVAL_TIME_FIELDS = ("authorized_at", "approved_at", "approval_timestamp")

#: Statuses that honestly record metadata we do not have.
UNAVAILABLE_STATUSES = ("unavailable", "unknown", "not_inferred", "not_provided")


def validate_authorization_provenance(
    adjudication: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return every honesty problem with the authorization provenance.

    The rule this enforces: an approval that cannot be timestamped must say so.

    * the approving actor, the exact approval text, and the channel/thread are bound;
    * a message timestamp or message id may be recorded **only** when observed, so an
      unobserved one must be ``None`` carrying an explicit unavailable status;
    * the record-creation time must be labelled machine-generated and explicitly
      distinguished from the approval time;
    * a field that would present an *inferred* approval time is forbidden outright.
    """
    data = ADJUDICATION if adjudication is None else adjudication
    approval = data.get("approval") or {}
    authorization = data.get("authorization") or {}
    problems: list[str] = []

    for field in ("approved_by", "approval_text", "thread_id"):
        if not approval.get(field):
            problems.append(f"approval.{field} is required")
    if approval.get("channel") not in ("codex_thread", "chat", "email", "document"):
        problems.append(
            f"approval.channel must name a recognisable channel, got "
            f"{approval.get('channel')!r}"
        )

    for field in ("message_timestamp", "message_id"):
        status = approval.get(f"{field}_status")
        if approval.get(field) is None:
            if status not in UNAVAILABLE_STATUSES:
                problems.append(
                    f"{field} is absent, so {field}_status must honestly say so "
                    f"(one of {list(UNAVAILABLE_STATUSES)}), got {status!r}"
                )
        else:
            if status in UNAVAILABLE_STATUSES:
                problems.append(f"{field} is set but {field}_status says {status!r}")
            if not approval.get(f"{field}_source"):
                problems.append(
                    f"{field} is asserted, so {field}_source must record where it was "
                    "observed"
                )

    if not authorization.get("recorded_at"):
        problems.append("authorization.recorded_at (record-creation time) is required")
    source = str(authorization.get("recorded_at_source") or "").lower()
    if not source:
        problems.append(
            "authorization.recorded_at_source is required so the record time is not "
            "mistaken for the approval time"
        )
    elif "not the approval" not in source:
        problems.append(
            "authorization.recorded_at_source must state that recorded_at is not the "
            "approval time"
        )

    for forbidden in FORBIDDEN_APPROVAL_TIME_FIELDS:
        if forbidden in approval or forbidden in authorization:
            problems.append(
                f"{forbidden!r} would present an approval timestamp that was never "
                "observed; it must not be recorded"
            )
    return problems


def assert_authorization_provenance() -> None:
    """Validate the shipped provenance; raise ``AuthorizationError`` on any problem."""
    problems = validate_authorization_provenance()
    if problems:
        raise AuthorizationError("authorization provenance invalid: " + "; ".join(problems))


assert_authorization_provenance()
