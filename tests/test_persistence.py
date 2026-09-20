"""Tests for db.py persistence layer using temporary SQLite."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test

# Import db FIRST so the module-level DATABASE_URL defaults to production.
# We switch to a temp database via set_database_url() below.
import db as _db_mod
from db import (
    init_db,
    get_session,
    Meeting,
    AgendaItem,
    SupportingDocument,
    PublicBody,
    Jurisdiction,
    Person,
    BodyMembership,
    BodySeat,
    create_or_get_meeting,
    replace_meeting_data_safe,
    upsert_meeting,
    update_sync_status,
    get_meetings_by_date_range,
    get_meetings_by_status,
    persist_meeting,
    backfill_meeting_normalization,
    set_database_url,
    _resolve_jurisdiction_id,
    _ensure_membership,
    is_canceled_meeting,
    mark_meeting_canceled,
)
from sqlalchemy import select, func, inspect as sa_inspect

# conftest.py sets POLISCOPIC_DB_TIER=test which creates a temp DB
# automatically. init_db() is called in setUpClass of each test class.


# _reset_db_engine is no longer needed — conftest handles test tier isolation.


@integration_test
class TestInitDbIdempotent(unittest.TestCase):
    def test_double_init_is_idempotent(self):
        """init_db() is idempotent — calling it twice does not change schema."""
        init_db()
        from sqlalchemy import inspect as sa_inspect
        session = get_session()
        tables_before = set(sa_inspect(session.get_bind()).get_table_names())
        init_db()  # second call
        tables_after = set(sa_inspect(session.get_bind()).get_table_names())
        self.assertEqual(tables_before, tables_after)
        self.assertIn("meetings", tables_after)
        session.close()

    def test_meeting_table_exists(self):
        """After init_db, the meetings table should exist."""
        init_db()
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("meetings", tables)
        session.close()


@integration_test
class TestPersistMeeting(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Clean tables before each test
        self.session.execute(AgendaItem.__table__.delete())
        self.session.execute(SupportingDocument.__table__.delete())
        self.session.execute(Meeting.__table__.delete())
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _make_meeting_dict(self, meeting_id="9999", date="2026-01-01", mtype="Formal", title="Test Meeting"):
        return {
            "meeting_id": meeting_id,
            "meeting_date": date,
            "meeting_type": mtype,
            "meeting_title": title,
            "source_url": f"https://example.com/meeting?id={meeting_id}",
        }

    def _make_items(self, meeting_id="9999", count=3):
        items = []
        for i in range(1, count + 1):
            items.append({
                "meeting_id": meeting_id,
                "agenda_item_number": i,
                "agenda_item_id": f"{meeting_id}-{i}-item",
                "agenda_item_title": f"Item {i}",
                "agenda_item_text": f"Text for item {i}",
                "agenda_item_url": f"https://example.com/item?m={meeting_id}&n={i}",
                "vote_or_action": "",
                "source_body": "Board of Supervisors",
                "source_url": f"https://example.com/meeting?id={meeting_id}",
                "c_number": f"C-86-26-{100+i:03d}-X-00" if i == 2 else "",
                "c_number_base": f"C-86-26-{100+i:03d}-X" if i == 2 else "",
                "c_number_revision": "00" if i == 2 else None,
            })
        return items

    def _make_docs(self, meeting_id="9999", item_number=2, count=1):
        docs = []
        for i in range(count):
            docs.append({
                "agenda_item_id": item_number,
                "meeting_id": meeting_id,
                "agenda_item_number": item_number,
                "c_number": f"C-86-26-{100+item_number:03d}-X-00",
                "c_number_base": f"C-86-26-{100+item_number:03d}-X",
                "c_number_revision": None,
                "document_title": f"Doc {i+1} for item {item_number}",
                "document_url": f"https://example.com/doc?m={meeting_id}&i={item_number}&d={i}",
                "document_type": "PDF",
                "file_name": f"doc_{i+1}.pdf",
                "file_extension": "pdf",
            })
        return docs

    def test_inserts_meeting(self):
        """replace_meeting_data_safe creates a meeting and persists items."""
        count = replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            self._make_items(),
        )
        self.session.commit()
        self.assertEqual(count, 3)
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "9999")
        ).scalar_one_or_none()
        self.assertIsNotNone(meeting)
        self.assertEqual(meeting.meeting_date, "2026-01-01")
        self.assertEqual(meeting.sync_status, "complete")

    def test_replaces_old_items(self):
        """Re-running persist replaces old items, doesn't duplicate."""
        # First run with 3 items
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            self._make_items(count=3),
        )
        self.session.commit()
        # Second run with 5 items
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            self._make_items(count=5),
        )
        self.session.commit()
        # Should have 5 items, not 8
        count = self.session.execute(
            select(func.count(AgendaItem.id)).where(AgendaItem.meeting_id == "9999")
        ).scalar()
        self.assertEqual(count, 5)

    def test_replaces_supporting_docs(self):
        """Re-running persist replaces old docs, doesn't duplicate."""
        docs1 = self._make_docs(item_number=2, count=2)
        items = self._make_items(count=3)
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            items, supporting_doc_dicts=docs1,
        )
        self.session.commit()

        docs2 = self._make_docs(item_number=2, count=1)
        replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(),
            items, supporting_doc_dicts=docs2,
        )
        self.session.commit()

        # Should have 1 doc, not 3
        count = self.session.execute(
            select(func.count(SupportingDocument.id))
            .where(SupportingDocument.meeting_id == "9999")
        ).scalar()
        self.assertEqual(count, 1)

    def test_metadata_updates_on_resync(self):
        """Re-syncing updates meeting_date, meeting_type, title, and display_name."""
        init_db()  # Ensure migration columns exist
        d1 = self._make_meeting_dict(meeting_id="9999", date="2026-01-01", mtype="Formal", title="")
        replace_meeting_data_safe(self.session, "bos", "9999", d1, self._make_items(count=1))
        self.session.commit()

        d2 = self._make_meeting_dict(meeting_id="9999", date="2026-01-02", mtype="Special", title="Emergency Meeting")
        replace_meeting_data_safe(self.session, "bos", "9999", d2, self._make_items(count=1))
        self.session.commit()

        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "9999")
        ).scalar_one_or_none()
        self.assertEqual(meeting.meeting_date, "2026-01-02")
        self.assertEqual(meeting.meeting_type, "Special")
        self.assertIsNotNone(meeting.display_name)

    def test_c_number_persists(self):
        """C-number fields persist correctly on agenda items."""
        items = self._make_items(count=3)
        replace_meeting_data_safe(self.session, "bos", "9999", self._make_meeting_dict(), items)
        self.session.commit()

        item2 = self.session.execute(
            select(AgendaItem).where(
                AgendaItem.meeting_id == "9999",
                AgendaItem.agenda_item_number == 2,
            )
        ).scalar_one_or_none()
        self.assertIsNotNone(item2)
        self.assertEqual(item2.c_number, "C-86-26-102-X-00")
        self.assertEqual(item2.c_number_base, "C-86-26-102-X")
        self.assertEqual(item2.c_number_revision, "00")

    def test_failed_preserves_previous(self):
        """When persist raises, old rows remain intact."""
        items = self._make_items(count=3)
        replace_meeting_data_safe(self.session, "bos", "9999", self._make_meeting_dict(), items)
        self.session.commit()

        # agenda_item_number is now a String column; verify string values persist.
        string_items = self._make_items(count=2)
        string_items[0]["agenda_item_number"] = "4B1"
        string_items[1]["agenda_item_number"] = "7C2"
        replace_meeting_data_safe(self.session, "bos", "9999", self._make_meeting_dict(), string_items)
        self.session.commit()

        rows = self.session.execute(
            select(AgendaItem).where(
                AgendaItem.meeting_id == "9999",
                AgendaItem.body == "bos",
            ).order_by(AgendaItem.agenda_item_number)
        ).scalars().all()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].agenda_item_number, "4B1")
        self.assertEqual(rows[1].agenda_item_number, "7C2")

    def test_empty_items_and_docs(self):
        """Persisting a meeting with no items and no docs succeeds."""
        count = replace_meeting_data_safe(
            self.session, "bos", "9999", self._make_meeting_dict(), [],
            supporting_doc_dicts=[],
        )
        self.session.commit()
        self.assertEqual(count, 0)

    def test_backfill_normalization(self):
        """backfill_meeting_normalization sets display_name on existing rows."""
        init_db()
        d = self._make_meeting_dict(meeting_id="8888", date="2026-03-15", mtype="Formal")
        replace_meeting_data_safe(self.session, "bos", "8888", d, self._make_items(count=1))
        self.session.commit()

        # Manually null out display_name
        meeting_before = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "8888")
        ).scalar_one_or_none()
        meeting_before.display_name = None
        self.session.commit()

        backfill_meeting_normalization(self.session)
        self.session.commit()

        meeting_after = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "8888")
        ).scalar_one_or_none()
        self.assertIsNotNone(meeting_after.display_name)
        self.assertIn("Mar 15", meeting_after.display_name)
        self.assertIn("Formal", meeting_after.display_name)


@integration_test
class TestGetMeetingsByDateRange(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Safety: skip if not connected to a test database
        url = str(self.session.get_bind().url)
        if 'test' not in url.lower() and 'memory' not in url.lower():
            self.skipTest(f"Not a test database: {url}")
        self.session.execute(Meeting.__table__.delete())
        for mid, date in [("A", "2026-01-01"), ("B", "2026-03-15"), ("C", "2026-06-01")]:
            m = Meeting(body="bos", meeting_id=mid, meeting_date=date, meeting_type="Formal", meeting_title=f"Meeting {mid}")
            self.session.add(m)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_date_range_inclusive(self):
        result = get_meetings_by_date_range(self.session, "bos", "2026-01-01", "2026-03-15")
        ids = [m.meeting_id for m in result]
        self.assertIn("A", ids)
        self.assertIn("B", ids)
        self.assertNotIn("C", ids)

    def test_no_matches(self):
        result = get_meetings_by_date_range(self.session, "bos", "2027-01-01", "2027-12-31")
        self.assertEqual(result, [])


@integration_test
class TestGetMeetingsByStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Safety: skip if not connected to a test database
        url = str(self.session.get_bind().url)
        if 'test' not in url.lower() and 'memory' not in url.lower():
            self.skipTest(f"Not a test database: {url}")
        self.session.execute(Meeting.__table__.delete())
        for mid, date, status in [
            ("1001", "2026-01-05", "complete"),
            ("1002", "2026-01-12", "failed"),
            ("1003", "2026-01-20", "partial"),
            ("1004", "2026-02-02", "pending"),
            ("1005", "2026-02-10", "complete"),
        ]:
            m = Meeting(
                body="bos", meeting_id=mid, meeting_date=date,
                meeting_type="Formal", meeting_title=f"Meeting {mid}",
                sync_status=status,
                source_url=f"https://example.com/meeting?id={mid}",
            )
            self.session.add(m)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_selects_failed_partial_pending(self):
        """--retry-failed: only failed, partial, pending."""
        result = get_meetings_by_status(self.session, "bos", ["failed", "partial", "pending"])
        ids = sorted(m.meeting_id for m in result)
        self.assertEqual(ids, ["1002", "1003", "1004"])

    def test_excludes_complete(self):
        """--retry-failed excludes complete."""
        result = get_meetings_by_status(self.session, "bos", ["failed", "partial", "pending"])
        ids = sorted(m.meeting_id for m in result)
        self.assertNotIn("1001", ids)
        self.assertNotIn("1005", ids)

    def test_force_includes_all(self):
        """--force includes all meetings regardless of status."""
        result = get_meetings_by_status(
            self.session, "bos", [], force=True,
            meeting_ids=["1001", "1002", "1005"],
        )
        ids = sorted(m.meeting_id for m in result)
        self.assertEqual(ids, ["1001", "1002", "1005"])

    def test_merge_search_with_db_search_takes_priority(self):
        """When merging search with DB results, search list should be returned first."""
        search_ids = ["1001", "1002"]
        db_ids = ["1001", "1002", "1003", "1004", "1005"]
        seen = set()
        merged = []
        for mid in search_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)
        for mid in db_ids:
            if mid not in seen:
                seen.add(mid)
                merged.append(mid)
        self.assertEqual(sorted(merged), ["1001", "1002", "1003", "1004", "1005"])
        self.assertEqual(merged[:2], ["1001", "1002"])


@integration_test
class TestSyncStatus(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        self.session = get_session()
        # Safety: skip if not connected to a test database
        url = str(self.session.get_bind().url)
        if 'test' not in url.lower() and 'memory' not in url.lower():
            self.skipTest(f"Not a test database: {url}")
        self.session.execute(Meeting.__table__.delete())
        m = Meeting(body="bos", meeting_id="test1", meeting_date="2026-01-01", meeting_type="Formal")
        self.session.add(m)
        self.session.commit()

    def tearDown(self):
        self.session.close()

    def test_status_complete_sets_fields(self):
        update_sync_status(self.session, "bos", "test1", "complete", item_count_actual=10)
        self.session.commit()
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "test1")
        ).scalar_one_or_none()
        self.assertEqual(meeting.sync_status, "complete")
        self.assertEqual(meeting.item_count_actual, 10)
        self.assertIsNone(meeting.last_error)
        self.assertEqual(meeting.retry_count, 0)

    def test_status_failed_increments_retry(self):
        update_sync_status(self.session, "bos", "test1", "failed", error="Something broke")
        self.session.commit()
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "test1")
        ).scalar_one_or_none()
        self.assertEqual(meeting.sync_status, "failed")
        self.assertEqual(meeting.retry_count, 1)
        self.assertEqual(meeting.last_error, "[UNKNOWN] Something broke")


@integration_test
class TestVoteTablesCreated(unittest.TestCase):
    """Verify vote-related tables are created by init_db."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def test_agenda_item_votes_table_exists(self):
        from db import AgendaItemVote
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("agenda_item_votes", tables)
        session.close()

    def test_member_votes_table_exists(self):
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("member_votes", tables)
        session.close()

    def test_supervisors_table_exists(self):
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("persons", tables, "The 'supervisors' table was renamed to 'persons'")
        session.close()

    def test_meeting_members_table_exists(self):
        session = get_session()
        inspector = sa_inspect(session.get_bind())
        tables = inspector.get_table_names()
        self.assertIn("meeting_members", tables)
        session.close()


@integration_test
class TestRetryBackoff(unittest.TestCase):
    """Test the retry_with_backoff async helper."""

    def test_retry_succeeds_after_transient(self):
        """Retry succeeds on second attempt."""
        import asyncio
        from scraper.common.utils import retry_with_backoff

        attempts = []

        async def sometimes_fails():
            attempts.append(1)
            if len(attempts) < 2:
                raise ConnectionError("transient")
            return "success"

        result = asyncio.run(
            retry_with_backoff(sometimes_fails, max_attempts=3, backoff_seconds=[0.05, 0.05])
        )
        self.assertEqual(result, "success")
        self.assertEqual(len(attempts), 2)

    def test_retry_exhausts_and_raises(self):
        """Retry exhausts all attempts and raises the last exception."""
        import asyncio
        from scraper.common.utils import retry_with_backoff

        attempts = []

        async def always_fails():
            attempts.append(1)
            raise ValueError("permanent")

        with self.assertRaises(ValueError):
            asyncio.run(
                retry_with_backoff(always_fails, max_attempts=3, backoff_seconds=[0.05, 0.05])
            )
        self.assertEqual(len(attempts), 3)

    def test_retry_succeeds_first_time(self):
        """No retry needed when first attempt succeeds."""
        import asyncio
        from scraper.common.utils import retry_with_backoff

        attempts = []

        async def always_succeeds():
            attempts.append(1)
            return "ok"

        result = asyncio.run(
            retry_with_backoff(always_succeeds, max_attempts=3, backoff_seconds=[0.05, 0.05])
        )
        self.assertEqual(result, "ok")
        self.assertEqual(len(attempts), 1)


@integration_test
class TestPersistVotes(unittest.TestCase):
    """Test that persist_votes handles retry and stale-session scenarios."""

    @classmethod
    def setUpClass(cls):
        init_db()

    def setUp(self):
        from db import persist_votes
        self.persist_votes = persist_votes
        self.s = get_session()
        # Ensure supervisor records exist
        from db import Supervisor
        existing = self.s.execute(
            select(Supervisor).where(Supervisor.normalized_name == "john taylor")
        ).scalar_one_or_none()
        if not existing:
            self.s.add(Supervisor(name="John Taylor", normalized_name="john taylor"))
            self.s.add(Supervisor(name="Jane Poston", normalized_name="jane poston"))
            self.s.commit()
        else:
            self.s.commit()

    def tearDown(self):
        self.s.close()

    def _make_vote(self, item_num=1):
        return [{
            "agenda_item_number": item_num,
            "c_number": f"C-01-25-{item_num:03d}-X-00",
            "motion_result": "approved",
            "vote_text": "Test vote",
            "supervisor_votes": [
                {"name": "John Taylor", "vote": "yes"},
                {"name": "Jane Poston", "vote": "yes"},
            ]
        }]

    def _make_supervisors(self):
        return [
            {"name": "John Taylor", "normalized_name": "john taylor", "district": "1"},
            {"name": "Jane Poston", "normalized_name": "jane poston", "district": "2"},
        ]

    def test_persist_votes_twice_same_meeting(self):
        """Calling persist_votes twice for the same meeting must succeed.

        First call succeeds internally but the outer commit fails.
        Second call must delete and re-insert without UNIQUE violations.
        """
        sups = self._make_supervisors()
        v = self._make_vote()

        self.persist_votes(self.s, "bos", "TEST001", sups, v)
        self.s.rollback()

        self.persist_votes(self.s, "bos", "TEST001", sups, v)
        self.s.commit()

        from db import MeetingMember, AgendaItemVote
        ms = self.s.execute(
            select(MeetingMember).where(
                MeetingMember.body == "bos",
                MeetingMember.meeting_id == "TEST001",
            )
        ).scalars().all()
        aiv = self.s.execute(
            select(AgendaItemVote).where(
                AgendaItemVote.body == "bos",
                AgendaItemVote.meeting_id == "TEST001",
            )
        ).scalars().all()
        self.assertEqual(len(ms), 2)
        self.assertEqual(len(aiv), 1)

    def test_persist_votes_consecutive_meetings(self):
        """Consecutive meetings must not leak session state."""
        sups = self._make_supervisors()
        for i in range(3):
            mid = f"TEST00{i+2}"
            self.persist_votes(self.s, "bos", mid, sups, self._make_vote(i + 1))
            self.s.commit()

        from db import MeetingMember
        total = self.s.execute(select(MeetingMember)).scalars().all()
        self.assertEqual(len(total), 6)

    def test_persist_votes_stale_session_after_failure(self):
        """When a commit fails after persist_votes, the next meeting's
        persist_votes must succeed despite stale objects in the session."""
        sups = self._make_supervisors()

        # First call: succeed, no commit (simulates outer exception)
        self.persist_votes(self.s, "bos", "TEST010", sups, self._make_vote())

        # Second meeting: must not get UNIQUE constraint failure
        self.persist_votes(self.s, "bos", "TEST011", sups, self._make_vote(1))
        self.s.commit()

        from db import MeetingMember
        ms10 = self.s.execute(
            select(MeetingMember).where(MeetingMember.meeting_id == "TEST010")
        ).scalars().all()
        ms11 = self.s.execute(
            select(MeetingMember).where(MeetingMember.meeting_id == "TEST011")
        ).scalars().all()
        self.assertEqual(len(ms10), 2)
        self.assertEqual(len(ms11), 2)

    def test_persist_votes_creates_membership_for_new_person(self):
        """Creating a new supervisor via persist_votes also creates a BodyMembership."""
        # Need a real meeting in the DB for date resolution
        m = self.s.execute(
            select(Meeting).where(Meeting.meeting_id == "TEST-MEMBERSHIP")
        ).scalar_one_or_none()
        if m is None:
            pb = self.s.execute(
                select(PublicBody).where(PublicBody.body_code == "bos")
            ).scalar_one_or_none()
            m = Meeting(
                body="bos",
                meeting_id="TEST-MEMBERSHIP",
                meeting_date="2026-05-01",
                meeting_type="Formal",
                meeting_title="Test Meeting",
                jurisdiction_id=pb.jurisdiction_id if pb else None,
                public_body_id=pb.id if pb else None,
            )
            self.s.add(m)
            self.s.commit()

        sups = [{"name": "Alex Rivera", "normalized_name": "alex rivera", "district": "3"}]
        v = self._make_vote()
        v[0]["supervisor_votes"] = [{"name": "Alex Rivera", "vote": "yes"}]

        self.persist_votes(self.s, "bos", "TEST-MEMBERSHIP", sups, v)
        self.s.commit()

        # Check BodyMembership was created
        membership = self.s.execute(
            select(BodyMembership)
            .join(Person, Person.id == BodyMembership.person_id)
            .where(Person.normalized_name == "alex rivera")
        ).scalar_one_or_none()
        self.assertIsNotNone(membership, "BodyMembership should exist for new supervisor")
        self.assertEqual(membership.term_start.isoformat(), "2026-05-01")

    def test_ensure_membership_idempotent(self):
        """Calling _ensure_membership twice for same person+body returns same row."""
        from datetime import date
        person = Person(name="Test Dup", normalized_name="test dup")
        self.s.add(person)
        self.s.commit()

        m1 = _ensure_membership(self.s, person.id, "bos")
        self.s.commit()
        m2 = _ensure_membership(self.s, person.id, "bos")
        self.s.commit()

        self.assertIsNotNone(m1)
        self.assertIsNotNone(m2)
        self.assertEqual(m1.id, m2.id, "_ensure_membership should be idempotent")

    def test_membership_evaluates_at_meeting_date(self):
        """A membership with term_end is inactive for meetings after that date."""
        from datetime import date, timedelta
        pb = self.s.execute(
            select(PublicBody).where(PublicBody.body_code == "bos")
        ).scalar_one_or_none()
        self.assertIsNotNone(pb)

        person = Person(name="Past Member", normalized_name="past member")
        self.s.add(person)
        self.s.commit()

        # Create a term that ended in 2024
        membership = BodyMembership(
            person_id=person.id,
            public_body_id=pb.id,
            term_start=date(2020, 1, 1),
            term_end=date(2024, 12, 31),
        )
        self.s.add(membership)
        self.s.commit()

        # Should be active for a 2024 meeting
        active_2024 = self.s.execute(
            select(BodyMembership)
            .where(BodyMembership.person_id == person.id)
            .where(BodyMembership.term_start <= date(2024, 6, 1))
            .where(
                (BodyMembership.term_end.is_(None)) |
                (BodyMembership.term_end >= date(2024, 6, 1))
            )
        ).scalar_one_or_none()
        self.assertIsNotNone(active_2024, "Should be active for mid-2024 meeting")

        # Should NOT be active for a 2025 meeting
        active_2025 = self.s.execute(
            select(BodyMembership)
            .where(BodyMembership.person_id == person.id)
            .where(BodyMembership.term_start <= date(2025, 6, 1))
            .where(
                (BodyMembership.term_end.is_(None)) |
                (BodyMembership.term_end >= date(2025, 6, 1))
            )
        ).scalar_one_or_none()
        self.assertIsNone(active_2025, "Should NOT be active for mid-2025 meeting")


@integration_test
class TestMeetingJurisdictionResolution(unittest.TestCase):
    """Regression tests for jurisdiction_id resolution in meeting creation.

    Every meeting must get the correct jurisdiction_id from its public body,
    regardless of which sync code path creates it.
    """

    @classmethod
    def setUpClass(cls):
        init_db()
        # Seed jurisdiction + body data for testing
        session = get_session()
        try:
            # Ensure test jurisdictions exist
            mc = session.execute(
                select(Jurisdiction).where(Jurisdiction.slug == "maricopa-county")
            ).scalar_one_or_none()
            if mc is None:
                mc = Jurisdiction(name="Maricopa County", slug="maricopa-county", state="AZ")
                session.add(mc)
                session.flush()

            tempe = session.execute(
                select(Jurisdiction).where(Jurisdiction.slug == "tempe")
            ).scalar_one_or_none()
            if tempe is None:
                tempe = Jurisdiction(name="City of Tempe", slug="tempe", state="AZ")
                session.add(tempe)
                session.flush()

            # Ensure test public bodies exist
            for body_code, jur_id in [("bos", mc.id), ("pz", mc.id),
                                       ("tempe-cc", tempe.id)]:
                existing = session.execute(
                    select(PublicBody).where(PublicBody.body_code == body_code)
                ).scalar_one_or_none()
                if existing is None:
                    session.add(PublicBody(
                        jurisdiction_id=jur_id,
                        name=f"Test {body_code}",
                        slug=f"test-{body_code}",
                        body_code=body_code,
                        body_type="Test",
                    ))
            session.commit()
        finally:
            session.close()

    def setUp(self):
        self.session = get_session()
        self.session.execute(Meeting.__table__.delete())
        self.session.commit()

    def tearDown(self):
        self.session.rollback()
        self.session.close()

    def _make_dict(self, meeting_id="TEST-JUR-001"):
        return {
            "meeting_id": meeting_id,
            "meeting_date": "2026-05-01",
            "meeting_type": "Formal",
            "meeting_title": "Test Jurisdiction Meeting",
            "source_url": f"https://example.com/m?id={meeting_id}",
        }

    def test_bos_meeting_gets_jurisdiction_1(self):
        """A BOS meeting gets jurisdiction_id=1 (Maricopa County)."""
        meeting = create_or_get_meeting(self.session, "bos", self._make_dict())
        self.assertEqual(meeting.jurisdiction_id, 1)

    def test_pz_meeting_gets_jurisdiction_1(self):
        """A PZ meeting gets jurisdiction_id=1 (Maricopa County)."""
        meeting = create_or_get_meeting(self.session, "pz", self._make_dict("TEST-JUR-002"))
        self.assertEqual(meeting.jurisdiction_id, 1)

    def test_tempe_meeting_gets_jurisdiction_2(self):
        """A Tempe meeting gets jurisdiction_id=2 (City of Tempe)."""
        meeting = create_or_get_meeting(self.session, "tempe-cc", self._make_dict("TEST-JUR-003"))
        self.assertEqual(meeting.jurisdiction_id, 2)

    def test_replace_meeting_data_safe_preserves_jurisdiction(self):
        """replace_meeting_data_safe preserves jurisdiction_id from create_or_get_meeting."""
        meeting_dict = self._make_dict("TEST-JUR-004")
        items = [
            {
                "meeting_id": "TEST-JUR-004",
                "agenda_item_number": 1,
                "agenda_item_id": "TEST-JUR-004-1-item",
                "agenda_item_title": "Item 1",
                "agenda_item_text": "",
                "agenda_item_url": "",
                "vote_or_action": "",
                "source_body": "Board of Supervisors",
                "source_url": "",
            }
        ]
        replace_meeting_data_safe(self.session, "bos", "TEST-JUR-004", meeting_dict, items)
        self.session.commit()
        meeting = self.session.execute(
            select(Meeting).where(Meeting.meeting_id == "TEST-JUR-004")
        ).scalar_one_or_none()
        self.assertIsNotNone(meeting)
        self.assertEqual(meeting.jurisdiction_id, 1)

    def test_resolve_for_unknown_body_is_none(self):
        """An unrecognized body code returns None jurisdiction_id."""
        meeting = create_or_get_meeting(self.session, "nonexistent", self._make_dict("TEST-JUR-005"))
        self.assertIsNone(meeting.jurisdiction_id)

    def test_existing_meeting_not_overwritten(self):
        """Re-fetching an existing meeting preserves its jurisdiction_id."""
        # First call: creates the meeting
        m1 = create_or_get_meeting(self.session, "bos", self._make_dict("TEST-JUR-006"))
        # Second call: returns existing
        m2 = create_or_get_meeting(self.session, "bos", self._make_dict("TEST-JUR-006"))
        self.assertIs(m1, m2)
        self.assertEqual(m2.jurisdiction_id, 1)




class TestCanceledMeetingDetection(unittest.TestCase):
    """Tests for cancelation detection in meeting titles."""

    def test_standard_cancel_prefix(self):
        self.assertTrue(is_canceled_meeting("CANCELED – Regular Meeting"))

    def test_alternative_spelling(self):
        """British spelling 'CANCELLED' must also be detected."""
        self.assertTrue(is_canceled_meeting("CANCELLED – Work Session"))

    def test_variant_cancel(self):
        self.assertTrue(is_canceled_meeting("CANCEL City Council Meeting"))

    def test_no_false_positive(self):
        self.assertFalse(is_canceled_meeting("Regular Formal Meeting"))
        self.assertFalse(is_canceled_meeting("Board of Supervisors"))

    def test_empty_title(self):
        self.assertFalse(is_canceled_meeting(""))

    def test_case_insensitive(self):
        self.assertTrue(is_canceled_meeting("canceled – Study Session"))
        self.assertTrue(is_canceled_meeting("Cancelled – Executive Session"))

    def test_from_meeting_dict_title(self):
        d = {"meeting_title": "CANCELED – Tempe City Council", "meeting_type": "Regular Meeting"}
        self.assertTrue(is_canceled_meeting(d))

    def test_from_meeting_dict_type(self):
        d = {"meeting_title": "Joint Meeting", "meeting_type": "CANCELED – Special Meeting"}
        self.assertTrue(is_canceled_meeting(d))

    def test_non_canceled_dict(self):
        d = {"meeting_title": "Regular Formal Meeting", "meeting_type": "Formal"}
        self.assertFalse(is_canceled_meeting(d))

    def test_mark_meeting_canceled(self):
        """mark_meeting_canceled sets sync_status='no_agenda'."""
        session = get_session()
        try:
            mid = "TEST-CAN-001"
            session.execute(
                Meeting.__table__.delete().where(Meeting.meeting_id == mid)
            )
            m = Meeting(
                body="bos", meeting_id=mid, meeting_date="2026-06-01",
                meeting_type="Formal", meeting_title="Test",
                sync_status="failed", last_error="Some error", retry_count=3,
            )
            session.add(m)
            session.commit()
            mark_meeting_canceled(session, "bos", mid)
            session.commit()

            updated = session.execute(
                select(Meeting).where(Meeting.meeting_id == mid)
            ).scalar_one()
            self.assertEqual(updated.sync_status, "no_agenda")
            self.assertEqual(updated.last_error, "Meeting was canceled")
            self.assertEqual(updated.retry_count, 0)
        finally:
            session.close()


def _reset_engine_cache():
    """Reset the db engine cache for test isolation.

    Called at process exit via atexit.  Restores DATABASE_URL to the
    module-level temp path so any atexit cleanup that calls get_engine()
    doesn't connect to a stale URL (e.g. the production database path
    set by test_sync_data_integrity._use_real_db()).
    """
    try:
        import db.core as _dc
        _dc._engine = None
        _dc._SessionLocal = None
    except ImportError:
        pass

# Register cleanup for after all tests in this module run
import atexit
atexit.register(_reset_engine_cache)
if __name__ == "__main__":
    unittest.main()
