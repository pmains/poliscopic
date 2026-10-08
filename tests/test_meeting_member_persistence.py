"""Isolated tests for stable attendance provenance persistence."""

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from scripts.scraper.backfill_votes import _persist_minutes_votes
from scripts.db.meeting_members import AttendanceRecord, reconcile_meeting_members
from scripts.db.models import AgendaItem, AgendaItemVote, MemberVote, MeetingMember, Person


def _isolated_session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    MeetingMember.__table__.create(engine)
    return Session(engine)


def test_refresh_preserves_meeting_member_provenance_id():
    session = _isolated_session()
    original = [AttendanceRecord(member_id=7, role="Chair", present=True)]
    reconcile_meeting_members(
        session,
        body="bos",
        meeting_id="M-1",
        meeting_db_id=10,
        attendance=original,
    )
    session.commit()
    original_id = session.execute(select(MeetingMember.id)).scalar_one()

    refreshed = [AttendanceRecord(member_id=7, role="Member", present=False)]
    reconcile_meeting_members(
        session,
        body="bos",
        meeting_id="M-1",
        meeting_db_id=10,
        attendance=refreshed,
    )
    session.commit()
    row = session.execute(select(MeetingMember)).scalar_one()

    assert row.id == original_id
    assert row.role == "Member"
    assert row.present is False


def test_refresh_retains_omitted_historical_attendance():
    session = _isolated_session()
    reconcile_meeting_members(
        session,
        body="bos",
        meeting_id="M-1",
        meeting_db_id=10,
        attendance=[AttendanceRecord(member_id=7, role=None, present=True)],
    )
    session.commit()
    original_id = session.execute(select(MeetingMember.id)).scalar_one()

    reconcile_meeting_members(
        session,
        body="bos",
        meeting_id="M-1",
        meeting_db_id=10,
        attendance=[],
    )
    session.commit()

    assert session.execute(select(MeetingMember.id)).scalar_one() == original_id


def test_minutes_vote_refresh_reconciles_member_provenance():
    """Minutes reprocessing keeps graph-source attendance rows resolvable."""
    session = _isolated_session()
    Person.__table__.create(session.bind)
    AgendaItemVote.__table__.create(session.bind)
    MemberVote.__table__.create(session.bind)

    initial_supervisors = [
        {
            "name": "Alice Chair",
            "normalized_name": "alice chair",
            "role": "Chair",
            "present": True,
        },
        {
            "name": "Bob Member",
            "normalized_name": "bob member",
            "role": "Member",
            "present": True,
        },
    ]
    _persist_minutes_votes(session, "bos", "M-1", 10, initial_supervisors, [])
    session.commit()
    original_ids = {
        row.member_id: row.id
        for row in session.execute(select(MeetingMember)).scalars()
    }
    alice_member_id = session.execute(
        select(Person.id).where(Person.normalized_name == "alice chair")
    ).scalar_one()
    bob_member_id = session.execute(
        select(Person.id).where(Person.normalized_name == "bob member")
    ).scalar_one()

    _persist_minutes_votes(session, "bos", "M-1", 10, [
        {
            "name": "Alice Chair",
            "normalized_name": "alice chair",
            "role": "Member",
            "present": False,
        },
    ], [])
    session.commit()
    rows = {
        row.member_id: row
        for row in session.execute(select(MeetingMember)).scalars()
    }

    # Alice was refreshed in place, and Bob remains a valid graph source even
    # though the later minutes parse did not mention him.
    assert rows.keys() == original_ids.keys()
    assert rows[alice_member_id].id == original_ids[alice_member_id]
    assert rows[alice_member_id].role == "Member"
    assert rows[alice_member_id].present is False
    assert rows[bob_member_id].id == original_ids[bob_member_id]


def test_minutes_vote_persists_body_on_member_votes():
    """A minutes backfill must preserve the public-body reference on each vote."""
    session = _isolated_session()
    Person.__table__.create(session.bind)
    AgendaItem.__table__.create(session.bind)
    AgendaItemVote.__table__.create(session.bind)
    MemberVote.__table__.create(session.bind)

    supervisors = [{
        "name": "Alice Chair",
        "normalized_name": "alice chair",
        "role": "Chair",
        "present": True,
    }]
    votes = [{
        "agenda_item_number": "1",
        "supervisor_votes": [{
            "name": "Alice Chair",
            "normalized_name": "alice chair",
            "vote": "yes",
        }],
    }]

    _persist_minutes_votes(session, "glendale-gsc", "6060", 10, supervisors, votes)
    row = session.execute(select(MemberVote)).scalar_one()

    assert row.body == "glendale-gsc"
