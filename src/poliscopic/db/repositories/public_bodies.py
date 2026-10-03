"""Queries for the public-body directory."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BodyMembership, BodySeat, Jurisdiction, Person, PublicBody


@dataclass(frozen=True, slots=True)
class BodyMemberView:
    """Template-safe member projection detached from ORM session state."""

    id: int
    name: str
    normalized_name: str
    title: str | None
    district_or_seat: str | None
    active_from: date
    active_to: date | None


@dataclass(frozen=True, slots=True)
class PublicBodyDetail:
    body: PublicBody
    jurisdiction: Jurisdiction | None
    members: list[BodyMemberView]
    page: int
    total_pages: int
    total: int
    per_page: int


def load_public_body_directory(
    session: Session,
    jurisdiction_slug: str = "",
) -> tuple[list[Jurisdiction], list[tuple[Jurisdiction, list[PublicBody]]]]:
    """Return all filter choices and the requested directory rows.

    Session ownership remains with the caller. Bodies are fetched in one query
    rather than one query per jurisdiction; presentation-specific ordering can
    still be applied by the route.
    """
    jurisdictions = list(session.scalars(
        select(Jurisdiction).order_by(Jurisdiction.name)
    ))
    visible = [
        jurisdiction
        for jurisdiction in jurisdictions
        if not jurisdiction_slug or jurisdiction.slug == jurisdiction_slug
    ]
    if not visible:
        return jurisdictions, []

    bodies = list(session.scalars(
        select(PublicBody)
        .where(PublicBody.jurisdiction_id.in_([item.id for item in visible]))
        .order_by(PublicBody.name)
    ))
    grouped: dict[int, list[PublicBody]] = {
        jurisdiction.id: [] for jurisdiction in visible
    }
    for body in bodies:
        grouped[body.jurisdiction_id].append(body)

    return jurisdictions, [
        (jurisdiction, grouped[jurisdiction.id])
        for jurisdiction in visible
    ]


def load_public_body_detail(
    session: Session,
    slug: str,
    *,
    page: int = 1,
    per_page: int = 10,
) -> PublicBodyDetail | None:
    """Load one body and latest membership per person without N+1 sessions."""
    body = session.scalar(select(PublicBody).where(PublicBody.slug == slug))
    if body is None:
        return None

    jurisdiction = session.scalar(
        select(Jurisdiction).where(Jurisdiction.id == body.jurisdiction_id)
    )
    rows = session.execute(
        select(Person, BodyMembership, BodySeat)
        .join(BodyMembership, BodyMembership.person_id == Person.id)
        .outerjoin(BodySeat, BodySeat.id == BodyMembership.body_seat_id)
        .where(BodyMembership.public_body_id == body.id)
        .order_by(BodyMembership.term_start.desc(), Person.name)
    ).all()

    members: list[BodyMemberView] = []
    seen: set[int] = set()
    for person, membership, seat in rows:
        if person.id in seen:
            continue
        seen.add(person.id)
        members.append(BodyMemberView(
            id=person.id,
            name=person.name,
            normalized_name=person.normalized_name,
            title=membership.role,
            district_or_seat=seat.seat_name if seat is not None else None,
            active_from=membership.term_start,
            active_to=membership.term_end,
        ))

    total = len(members)
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))
    offset = (page - 1) * per_page
    return PublicBodyDetail(
        body=body,
        jurisdiction=jurisdiction,
        members=members[offset:offset + per_page],
        page=page,
        total_pages=total_pages,
        total=total,
        per_page=per_page,
    )
