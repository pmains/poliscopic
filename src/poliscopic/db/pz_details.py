"""Stable persistence for structured Planning and Zoning item details."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import AgendaItem, Meeting, PZItemDetail

log = logging.getLogger(__name__)


def persist_pz_item_details(
    session: Session,
    body: str,
    meeting_id: str,
    items: Sequence[Mapping[str, Any]],
) -> int:
    """Update or insert P&Z details without replacing provenance row IDs.

    The stable logical identity is ``(body, meeting_id, agenda_item_number)``.
    Rows omitted by a later scrape are retained because graph assertions may
    cite them; Stage 0 does not reinterpret missing parser output as evidence
    that a historical public record was withdrawn.

    Returns:
        Number of current input items containing structured P&Z details.
    """
    meeting_db_id = session.execute(
        select(Meeting.id).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    agenda_item_ids = {
        str(row.agenda_item_number): int(row.id)
        for row in session.execute(
            select(AgendaItem.id, AgendaItem.agenda_item_number).where(
                AgendaItem.body == body,
                AgendaItem.meeting_id == meeting_id,
            )
        )
    }
    existing_rows = session.execute(
        select(PZItemDetail)
        .where(
            PZItemDetail.body == body,
            PZItemDetail.meeting_id == meeting_id,
        )
        .order_by(PZItemDetail.id)
    ).scalars()
    details_by_item_number: dict[int, PZItemDetail] = {}
    for detail in existing_rows:
        if detail.agenda_item_number in details_by_item_number:
            log.warning(
                "Duplicate P&Z detail logical key retained: %s/%s item %s (id=%s)",
                body,
                meeting_id,
                detail.agenda_item_number,
                detail.id,
            )
            continue
        details_by_item_number[detail.agenda_item_number] = detail

    persisted = 0
    for item in items:
        if not item.get("pz_project_name"):
            continue
        item_number = int(item.get("agenda_item_number", 0))
        detail = details_by_item_number.get(item_number)
        if detail is None:
            detail = PZItemDetail(
                body=body,
                meeting_id=meeting_id,
                agenda_item_number=item_number,
            )
            session.add(detail)
            details_by_item_number[item_number] = detail

        detail.agenda_item_id = agenda_item_ids.get(str(item_number))
        detail.meeting_db_id = int(meeting_db_id or 0)
        detail.case_number = str(item.get("case_number") or "")
        detail.district = item.get("pz_district")
        detail.project_name = item.get("pz_project_name")
        detail.applicant = item.get("pz_applicant")
        detail.request = item.get("pz_request")
        detail.location = item.get("pz_location")
        detail.recommendation = item.get("pz_recommendation")
        detail.presented_by = item.get("pz_presented_by")
        detail.staff_report_url = item.get("staff_report_url")
        detail.updated_at = datetime.now(timezone.utc)
        persisted += 1

    session.flush()
    return persisted
