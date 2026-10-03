"""Stable reconciliation of per-meeting public-body attendance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from poliscopic.db.models import MeetingMember


@dataclass(frozen=True)
class AttendanceRecord:
    """One parsed attendance observation for a known person."""

    member_id: int
    role: str | None
    present: bool | None


def reconcile_meeting_members(
    session: Session,
    *,
    body: str,
    meeting_id: str,
    meeting_db_id: int,
    attendance: Sequence[AttendanceRecord],
) -> int:
    """Update or insert attendance while preserving graph provenance IDs.

    Existing rows absent from a later parse are retained. A missing parsed name
    is not sufficient evidence that a historical attendance record was false,
    and graph assertions may still cite that row as their source evidence.

    Returns the number of distinct attendance records in the current input.
    """
    existing_rows = session.execute(
        select(MeetingMember).where(
            MeetingMember.body == body,
            MeetingMember.meeting_id == meeting_id,
        )
    ).scalars()
    members_by_person_id = {row.member_id: row for row in existing_rows}

    for observation in attendance:
        meeting_member = members_by_person_id.get(observation.member_id)
        if meeting_member is None:
            meeting_member = MeetingMember(
                body=body,
                meeting_id=meeting_id,
                meeting_db_id=meeting_db_id,
                member_id=observation.member_id,
            )
            session.add(meeting_member)
            members_by_person_id[observation.member_id] = meeting_member
        meeting_member.meeting_db_id = meeting_db_id
        meeting_member.role = observation.role
        meeting_member.present = observation.present
        meeting_member.updated_at = datetime.now(timezone.utc)

    session.flush()
    return len({record.member_id for record in attendance})
