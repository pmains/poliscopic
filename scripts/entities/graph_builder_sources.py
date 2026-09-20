"""Structured database sources that emit graph-builder specifications."""

from __future__ import annotations

from typing import Generator

from scripts.entities.entity_utils import (
    clean_normalized_name,
    is_firm_name,
    normalize_entity_name,
)
from scripts.entities.graph_builder_models import EdgeSpec, EntitySpec, MentionSpec, Source

GraphSpec = tuple[EntitySpec | None, EdgeSpec | None, MentionSpec | None]


class BodyMembershipSource(Source):
    """Emit people, public bodies, and membership provenance."""
    name = "body_memberships"
    description = "Board member → body membership edges"
    query = """
        SELECT DISTINCT ON (bm.person_id, bm.public_body_id)
            bm.id AS membership_id, p.name AS person_name,
            p.normalized_name AS person_norm, bm.role,
            bm.term_start::text AS term_start, pb.name AS body_name, pb.body_code
        FROM body_memberships bm
        JOIN persons p ON p.id = bm.person_id
        JOIN public_bodies pb ON pb.id = bm.public_body_id
        ORDER BY bm.person_id, bm.public_body_id, bm.term_start DESC
    """

    def produce(self, rows: list[dict[str, object]]) -> Generator[GraphSpec, None, None]:
        """Yield person-to-public-body membership edges for complete source rows."""
        for row in rows:
            person_name = str(row.get("person_name") or "").strip()
            person_norm = str(row.get("person_norm") or "").strip()
            body_name = str(row.get("body_name") or "").strip()
            body_code = str(row.get("body_code") or "").strip()
            membership_id = int(row.get("membership_id") or 0)
            if not person_name or not person_norm or not body_name or not body_code:
                continue
            yield EntitySpec(person_name, person_norm, "person"), None, None
            yield EntitySpec(body_name, body_code, "organization", is_government=True), None, None
            yield (
                None,
                EdgeSpec(
                    person_norm,
                    "person",
                    body_code,
                    "organization",
                    "MEMBER_OF",
                    "body_membership",
                    membership_id,
                ),
                MentionSpec(
                    person_norm,
                    "person",
                    "body_membership",
                    membership_id,
                    person_name,
                    "MEMBER_OF",
                ),
            )


class MeetingAttendanceSource(Source):
    """Emit people-to-meeting attendance edges with meeting entities."""
    name = "meeting_attendance"
    description = "Meeting attendance edges"
    query = """
        SELECT DISTINCT ON (mm.body, mm.meeting_id, mm.member_id)
            mm.id AS att_id, p.name AS person_name, p.normalized_name AS person_norm,
            mm.body, mm.meeting_id, mm.meeting_db_id, mm.present, m.jurisdiction_id
        FROM meeting_members mm
        JOIN persons p ON p.id = mm.member_id
        LEFT JOIN meetings m ON m.id = mm.meeting_db_id
    """

    def produce(self, rows: list[dict[str, object]]) -> Generator[GraphSpec, None, None]:
        """Yield present-at edges for valid attendance records."""
        for row in rows:
            person_name = str(row.get("person_name") or "").strip()
            person_norm = str(row.get("person_norm") or "").strip()
            body_code = str(row.get("body") or "").strip()
            meeting_id = str(row.get("meeting_id") or "").strip()
            attendance_id = int(row.get("att_id") or 0)
            jurisdiction_id = row.get("jurisdiction_id")
            if not person_name or not person_norm or not meeting_id:
                continue
            meeting_key = f"{body_code}/{meeting_id}"
            yield (
                EntitySpec(
                    f"{body_code} Meeting {meeting_id}",
                    meeting_key,
                    "meeting",
                    jurisdiction_id if isinstance(jurisdiction_id, int) else None,
                ),
                None,
                None,
            )
            yield EntitySpec(person_name, person_norm, "person"), None, None
            if row.get("present") is True:
                yield (
                    None,
                    EdgeSpec(
                        person_norm,
                        "person",
                        meeting_key,
                        "meeting",
                        "PRESENT_AT",
                        "meeting_member",
                        attendance_id,
                    ),
                    MentionSpec(
                        person_norm,
                        "person",
                        "meeting_member",
                        attendance_id,
                        person_name,
                        "PRESENT_AT",
                    ),
                )


class PZItemDetailsSource(Source):
    """Emit P&Z case, applicant, recommendation, and staff relationships."""
    name = "pz_item_details"
    description = "Applicant, case, recommendation edges"
    query = """
        SELECT DISTINCT ON (pz.id)
            pz.id AS pz_id, pz.case_number, pz.applicant, pz.recommendation,
            pz.presented_by, pz.body, pz.meeting_db_id, pz.agenda_item_number,
            m.jurisdiction_id
        FROM pz_item_details pz
        LEFT JOIN meetings m ON m.id = pz.meeting_db_id
        WHERE pz.case_number IS NOT NULL AND pz.case_number != ''
        ORDER BY pz.id
    """

    def produce(self, rows: list[dict[str, object]]) -> Generator[GraphSpec, None, None]:
        """Yield source-supported P&Z specifications for each complete case row."""
        for row in rows:
            case_number = str(row.get("case_number") or "").strip()
            applicant = str(row.get("applicant") or "").strip()
            # ``pz_item_details.recommendation`` is deliberately not read
            # here: it remains source evidence for later event/outcome
            # modeling and is never emitted as an entity, relationship or
            # role.
            presenter = str(row.get("presented_by") or "").strip()
            detail_id = int(row.get("pz_id") or 0)
            jurisdiction_id = row.get("jurisdiction_id")
            if not case_number:
                continue
            case_norm = normalize_entity_name(case_number)
            case_jurisdiction = jurisdiction_id if isinstance(jurisdiction_id, int) else None
            yield EntitySpec(case_number, case_norm, "case", case_jurisdiction), None, None
            if applicant:
                yield from self._applicant_specs(applicant, case_norm, detail_id)
            if presenter:
                presenter_norm = clean_normalized_name(presenter)
                yield EntitySpec(presenter, presenter_norm, "person"), None, None
                # The presenter is merely named in the record.  That supports a
                # contextual mention, not participation, so no HAS_STAFF and no
                # PARTICIPATED_IN edge is emitted.
                yield (
                    None,
                    None,
                    MentionSpec(
                        presenter_norm,
                        "person",
                        "pz_item_detail",
                        detail_id,
                        presenter,
                        "presenter",
                    ),
                )

    @staticmethod
    def _applicant_specs(
        applicant: str,
        case_norm: str,
        detail_id: int,
    ) -> Generator[GraphSpec, None, None]:
        """Emit a split person/firm model when the applicant text supports it."""
        pair = _split_person_and_firm(applicant)
        if pair is None:
            applicant_norm = normalize_entity_name(applicant)
            yield EntitySpec(applicant, applicant_norm, "organization"), None, None
            yield (
                None,
                EdgeSpec(
                    applicant_norm,
                    "organization",
                    case_norm,
                    "case",
                    "APPLIED_FOR",
                    "pz_item_detail",
                    detail_id,
                ),
                MentionSpec(
                    applicant_norm,
                    "organization",
                    "pz_item_detail",
                    detail_id,
                    applicant,
                    "applicant",
                ),
            )
            return
        person_name, firm_name = pair
        person_norm = clean_normalized_name(person_name)
        firm_norm = normalize_entity_name(firm_name)
        yield EntitySpec(person_name, person_norm, "person"), None, None
        yield EntitySpec(firm_name, firm_norm, "organization"), None, None
        yield (
            None,
            None,
            # The pair comes from a comma in the applicant field, which is not an
            # explicit representation statement: the person is recorded as a
            # contextual mention only, with no REPRESENTS edge.
            MentionSpec(
                person_norm,
                "person",
                "pz_item_detail",
                detail_id,
                person_name,
                "representative",
            ),
        )
        yield (
            None,
            EdgeSpec(
                firm_norm,
                "organization",
                case_norm,
                "case",
                "APPLIED_FOR",
                "pz_item_detail",
                detail_id,
            ),
            MentionSpec(
                firm_norm,
                "organization",
                "pz_item_detail",
                detail_id,
                firm_name,
                "applicant",
            ),
        )


def _split_person_and_firm(applicant: str) -> tuple[str, str] | None:
    """Return a validated person/firm pair from ``Person, Firm`` text."""
    if "," not in applicant:
        return None
    potential_person, potential_firm = (part.strip() for part in applicant.split(",", 1))
    if is_firm_name(potential_firm) and not is_firm_name(potential_person):
        return potential_person, potential_firm
    return None


def default_sources() -> list[Source]:
    """Return fresh default source objects for the public façade."""
    return [BodyMembershipSource(), MeetingAttendanceSource(), PZItemDetailsSource()]
