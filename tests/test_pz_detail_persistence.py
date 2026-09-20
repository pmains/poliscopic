"""Isolated tests for stable P&Z detail provenance persistence."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.db.models import AgendaItem, Meeting, PZItemDetail
from scripts.db.pz_details import persist_pz_item_details


def _session_with_meeting() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Meeting.__table__.create(engine)
    AgendaItem.__table__.create(engine)
    PZItemDetail.__table__.create(engine)
    session = Session(engine)
    meeting = Meeting(
        body="pz",
        meeting_id="M-1",
        meeting_date="2026-09-08",
        meeting_type="Planning and Zoning",
        meeting_title="P&Z",
        source_url="https://example.test/meeting",
    )
    session.add(meeting)
    session.flush()
    session.add(AgendaItem(
        body="pz",
        meeting_id="M-1",
        meeting_db_id=meeting.id,
        agenda_item_number="4",
        agenda_item_id="M-1-4",
    ))
    session.commit()
    return session


def _structured_item(project_name: str) -> dict[str, object]:
    return {
        "agenda_item_number": 4,
        "case_number": "Z-4-26",
        "pz_project_name": project_name,
        "pz_applicant": "Acme Development",
        "pz_recommendation": "Approval",
    }


def test_refresh_updates_pz_detail_without_changing_provenance_id():
    session = _session_with_meeting()
    persist_pz_item_details(session, "pz", "M-1", [_structured_item("Alpha")])
    session.commit()
    first = session.execute(select(PZItemDetail)).scalar_one()
    first_id = first.id

    persist_pz_item_details(session, "pz", "M-1", [_structured_item("Alpha Revised")])
    session.commit()
    refreshed = session.execute(select(PZItemDetail)).scalar_one()

    assert refreshed.id == first_id
    assert refreshed.project_name == "Alpha Revised"


def test_refresh_retains_omitted_historical_source_row():
    session = _session_with_meeting()
    persist_pz_item_details(session, "pz", "M-1", [_structured_item("Alpha")])
    session.commit()
    original_id = session.execute(select(PZItemDetail.id)).scalar_one()

    assert persist_pz_item_details(session, "pz", "M-1", []) == 0
    session.commit()

    assert session.execute(select(PZItemDetail.id)).scalar_one() == original_id
