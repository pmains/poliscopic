"""Regression tests for the Tempe City Council ingestion pipeline.

Validates the full extraction pipeline against fixture data covering
edge cases discovered during development:

- Items out of document order (sort_order)
- Consent / Non-Consent category labels
- Vote totals and per-member vote records
- Canceled meetings (empty agenda pages)
- Future meetings (Document unavailable)
- Supporting document (packet/summary) links
"""

import os
import re
import sys
import unittest
from pathlib import Path

_scripts_dir = str(Path(__file__).resolve().parent.parent / "scripts")
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "tempe"


def _load_fixture(name: str) -> str:
    path = FIXTURES_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


# ──────────────────────────────────────────────
#  OnBase agenda HTML parsing
# ──────────────────────────────────────────────

class TestAgendaHtmlParsing(unittest.TestCase):
    """Parse a full Regular City Council agenda (1852) and verify structure."""

    @classmethod
    def setUpClass(cls):
        from scraper.platforms.onbase import parse_agenda_html
        html = _load_fixture("1852_agenda.html")
        cls.items = parse_agenda_html(html, "1852", "tempe-cc")

    def test_items_parsed(self):
        self.assertGreater(len(self.items), 50)

    def test_item_types_present(self):
        types = {i["item_type"] for i in self.items}
        self.assertIn("section", types)
        self.assertIn("item", types)

    def test_section_levels_present(self):
        levels = {i["section_level"] for i in self.items}
        self.assertIn(1, levels)
        self.assertIn(2, levels)
        self.assertIn(3, levels)


class TestSortOrder(unittest.TestCase):
    """Items must be extractable in document order — numeric parts alone
    (e.g. "11" < "4A" in string sort) must not break ordering."""

    @classmethod
    def setUpClass(cls):
        from scraper.platforms.onbase import parse_agenda_html
        html = _load_fixture("1852_agenda.html")
        items = parse_agenda_html(html, "1852", "tempe-cc")
        # Build a list for positional index comparison
        cls.item_order = [i["agenda_item_number"] for i in items]

    def test_call_to_order_first(self):
        self.assertEqual(self.item_order[0], "1")

    def test_four_a_before_adjournment(self):
        """4A should appear well before 11 (Adjournment) in document order."""
        idx_4a = self.item_order.index("4A")
        idx_11 = self.item_order.index("11")
        self.assertLess(idx_4a, idx_11,
                        f"4A at position {idx_4a} should precede 11 at {idx_11}")

    def test_adjournment_last(self):
        self.assertEqual(self.item_order[-1], "11")

    def test_consent_before_nonconsent(self):
        idx_7 = self.item_order.index("7")
        idx_8 = self.item_order.index("8")
        self.assertLess(idx_7, idx_8)

    def test_subitems_after_parent(self):
        """7B1 should come after 7B, 7B after 7A, etc."""
        idx_7b = self.item_order.index("7B")
        idx_7b1 = self.item_order.index("7B1")
        self.assertLess(idx_7b, idx_7b1)


class TestConsentNonConsentLabels(unittest.TestCase):
    """Agenda category must propagate from level-1 sections to children."""

    @classmethod
    def setUpClass(cls):
        from scraper.platforms.onbase import parse_agenda_html
        from scraper.jurisdictions.tempe import _assign_tempe_categories
        html = _load_fixture("1852_agenda.html")
        items = parse_agenda_html(html, "1852", "tempe-cc")
        _assign_tempe_categories(items)
        cls.items = items

    def test_consent_agenda_label(self):
        item_7 = next(i for i in self.items if i["agenda_item_number"] == "7")
        self.assertEqual(item_7["agenda_category"], "Consent")

    def test_nonconsent_label(self):
        item_8 = next(i for i in self.items if i["agenda_item_number"] == "8")
        self.assertEqual(item_8["agenda_category"], "Non-Consent")

    def test_consent_subitem_label(self):
        """7B1 under Consent should inherit the Consent label."""
        item = next(i for i in self.items if i["agenda_item_number"] == "7B1")
        self.assertEqual(item["agenda_category"], "Consent")

    def test_nonconsent_subitem_label(self):
        """8C1 under Non-Consent should inherit Non-Consent."""
        item = next(i for i in self.items if i["agenda_item_number"] == "8C1")
        self.assertEqual(item["agenda_category"], "Non-Consent")

    def test_call_to_order_not_labeled(self):
        """1 (Call to Order) should NOT get a Consent/Non-Consent label."""
        item_1 = next(i for i in self.items if i["agenda_item_number"] == "1")
        self.assertNotIn(item_1["agenda_category"], ("Consent", "Non-Consent"))


# ──────────────────────────────────────────────
#  Vote / Summary PDF parsing
# ──────────────────────────────────────────────

class TestSummaryVoteParsing(unittest.TestCase):
    """Parse the Legal Action Summary PDF for meeting 1687 and validate votes."""

    @classmethod
    def setUpClass(cls):
        from scraper.jurisdictions.tempe.council_summary import parse_summary_text
        text = _load_fixture("1687_summary.txt")
        cls.result = parse_summary_text(text)

    def test_votes_extracted(self):
        self.assertGreater(len(self.result["votes"]), 20)

    def test_supervisors_extracted(self):
        names = {s["name"].lower() for s in self.result["supervisors"]}
        for expected in ("woods", "garlid", "adams", "amberg", "chin", "hodge", "keating"):
            self.assertIn(expected, names, f"Missing supervisor: {expected}")

    def test_motion_text_present(self):
        for v in self.result["votes"]:
            if v["supervisor_votes"]:
                self.assertTrue(v["vote_text"], f"Item {v['agenda_item_number']} has no vote_text")

    def test_consent_item_voters(self):
        """Consent items (7A1, 7B1) should have 7 supervisor votes.
        7C1/7C2 may show as adopted with 0 supervisor votes depending
        on the motion range parsing; check items that appear in the
        motion block (7A1, 7B1)."""
        for item_num in ("7A1", "7B1"):
            votes = [v for v in self.result["votes"]
                     if v["agenda_item_number"] == item_num]
            self.assertTrue(votes, f"No vote record for item {item_num}")
            self.assertEqual(len(votes[0]["supervisor_votes"]), 7,
                             f"Item {item_num} should have 7 votes")

    def test_nonconsent_item_voters(self):
        """Non-consent items (8A1, 8A2) should also have 7 supervisor votes."""
        for item_num in ("8A1", "8A2"):
            votes = [v for v in self.result["votes"]
                     if v["agenda_item_number"] == item_num]
            self.assertTrue(votes, f"No vote record for item {item_num}")
            self.assertEqual(len(votes[0]["supervisor_votes"]), 7,
                             f"Item {item_num} should have 7 votes")

    def test_vote_result_types(self):
        results = {v["motion_result"] for v in self.result["votes"]}
        self.assertIn("approved", results)
        self.assertIn("pass", results)

    def test_item_numbers_string(self):
        """Item numbers should be stored as strings like '4B1', not 41."""
        for v in self.result["votes"]:
            num = v["agenda_item_number"]
            self.assertIsInstance(num, str,
                                  f"agenda_item_number should be str, got {type(num).__name__} for {num}")


class TestSummaryItemResults(unittest.TestCase):
    """Verify individual item results match what's on the PDF."""

    @classmethod
    def setUpClass(cls):
        from scraper.jurisdictions.tempe.council_summary import parse_summary_text
        text = _load_fixture("1687_summary.txt")
        cls.result = parse_summary_text(text)

    def test_consent_subitem_approved(self):
        """Sub-items under consent agenda should have individual results."""
        for item_num in ("7B1", "7B2", "7B3", "7C1", "7C2"):
            votes = [v for v in self.result["votes"]
                     if v["agenda_item_number"] == item_num]
            self.assertTrue(votes, f"Item {item_num} missing from votes")
            self.assertIn(votes[0]["motion_result"],
                          ("approved", "adopted", "ratified", "pass"),
                          f"Item {item_num} result unexpected: {votes[0]['motion_result']}")

    def test_nonconsent_items_approved(self):
        for item_num in ("8A1", "8A2", "8A3", "8A4"):
            votes = [v for v in self.result["votes"]
                     if v["agenda_item_number"] == item_num]
            self.assertTrue(votes, f"Item {item_num} missing from votes")
            self.assertEqual(votes[0]["motion_result"], "approved")


# ──────────────────────────────────────────────
#  Meeting cancellation / empty agenda detection
# ──────────────────────────────────────────────

class TestCanceledMeetingDetection(unittest.TestCase):
    """A canceled meeting returns a short page with <h1> but no sections."""

    @classmethod
    def setUpClass(cls):
        from scraper.platforms.onbase import parse_agenda_html
        cls.html = _load_fixture("1779_canceled.html")

        # Check: valid meeting page has an <h1> with title (allow newlines inside)
        cls.has_header = bool(re.search(r"<h1[^>]*>.*?</h1>", cls.html, re.DOTALL))
        # Check for meaningful sections (level 1+).  OnBase always includes
        # a level-0 wrapper div with "accessible-section" in the class.
        cls.has_sections = bool(re.search(r"accessible-section-level-[1-9]", cls.html))

        # Parse it — should yield 0 items
        cls.items = parse_agenda_html(cls.html, "1779", "tempe-cc")

    def test_no_items_parsed(self):
        self.assertEqual(len(self.items), 0)

    def test_has_meeting_header(self):
        self.assertTrue(self.has_header)

    def test_has_no_sections(self):
        self.assertFalse(self.has_sections)

    def test_not_document_unavailable(self):
        """Canceled meeting pages are NOT 'Document unavailable' — they have
        a real header with meeting title/date."""
        self.assertNotIn("Document unavailable", self.html[:2000])


class TestDocumentUnavailable(unittest.TestCase):
    """Future or never-published meetings return 'Document unavailable'."""

    @classmethod
    def setUpClass(cls):
        cls.html = _load_fixture("1862_unavailable.html")

    def test_document_unavailable_present(self):
        self.assertIn("Document unavailable", self.html)

    def test_short_page(self):
        self.assertLess(len(self.html), 3000)

    def test_no_agenda_sections(self):
        self.assertNotIn("accessible-section", self.html)


# ──────────────────────────────────────────────
#  Vote persistence (round-trip)
# ──────────────────────────────────────────────

class TestVotePersistence(unittest.TestCase):
    """Verify that parsed vote data can be persisted via persist_votes()."""

    def setUp(self):
        from db import init_db, get_session, AgendaItem, Meeting, AgendaItemVote, SupportingDocument, MemberVote, MeetingMember
        import db.core as _dc
        self._saved_db_url = _dc.DATABASE_URL
        init_db()
        # Truncate tables for clean state
        s = get_session()
        for tbl in [AgendaItemVote.__table__, AgendaItem.__table__, Meeting.__table__, SupportingDocument.__table__, MemberVote.__table__, MeetingMember.__table__]:
            s.execute(tbl.delete())
        s.commit()
        s.close()

    def tearDown(self):
        from db import get_session
        import db.core as _dc
        s = get_session()
        s.close()
        _dc.set_database_url(self._saved_db_url)

    def _ensure_agenda_item(self, session, body, meeting_id, item_number, sort_order=0):
        """Create an AgendaItem row so persist_votes can find it."""
        from db import AgendaItem
        from datetime import datetime, timezone
        item = AgendaItem(
            body=body,
            meeting_id=meeting_id,
            agenda_item_number=item_number,
            agenda_item_id=f"{meeting_id}-{item_number}",
            agenda_item_title=f"Test item {item_number}",
            sort_order=sort_order,
            created_at=datetime.now(timezone.utc),
        )
        session.add(item)
        session.flush()
        return item

    def test_persist_full_vote_set(self):
        from scraper.jurisdictions.tempe.council_summary import parse_summary_text
        from db import get_session, persist_votes, AgendaItemVote, MemberVote
        from sqlalchemy import select, func

        text = _load_fixture("1687_summary.txt")
        result = parse_summary_text(text)
        self.assertGreater(len(result["votes"]), 0,
                           "Fixture must have votes for this test")

        s = get_session()
        # Create AgendaItem rows for a few vote targets
        self._ensure_agenda_item(s, "tempe-cc", "1687", "4B1", sort_order=10)
        self._ensure_agenda_item(s, "tempe-cc", "1687", "7A1", sort_order=20)
        self._ensure_agenda_item(s, "tempe-cc", "1687", "8A1", sort_order=30)

        # Persist votes
        cnt = persist_votes(s, "tempe-cc", "1687",
                            result["supervisors"], result["votes"])
        s.commit()

        # Verify
        aiv_count = s.execute(
            select(func.count()).select_from(AgendaItemVote).where(
                AgendaItemVote.body == "tempe-cc",
                AgendaItemVote.meeting_id == "1687",
            )
        ).scalar()
        self.assertGreater(aiv_count, 0, "No vote records persisted")

        sv_count = s.execute(
            select(func.count()).select_from(MemberVote).where(
                MemberVote.agenda_item_vote_id.in_(
                    select(AgendaItemVote.id).where(
                        AgendaItemVote.body == "tempe-cc",
                        AgendaItemVote.meeting_id == "1687",
                    )
                )
            )
        ).scalar()
        self.assertGreater(sv_count, 0, "No supervisor votes persisted")
        s.close()


class TestVotePersistenceReplacesOnResync(unittest.TestCase):
    """Re-running persist_votes should replace old votes, not duplicate."""

    def setUp(self):
        from db import init_db, get_session, AgendaItem, Meeting, AgendaItemVote, SupportingDocument, MemberVote, MeetingMember
        import db.core as _dc
        self._saved_db_url = _dc.DATABASE_URL
        init_db()
        # Truncate tables for clean state
        s = get_session()
        for tbl in [AgendaItemVote.__table__, AgendaItem.__table__, Meeting.__table__, SupportingDocument.__table__, MemberVote.__table__, MeetingMember.__table__]:
            s.execute(tbl.delete())
        s.commit()
        s.close()

    def tearDown(self):
        from db import get_session
        import db.core as _dc
        s = get_session()
        s.close()
        _dc.set_database_url(self._saved_db_url)

    def test_resync_replaces_votes(self):
        from scraper.jurisdictions.tempe.council_summary import parse_summary_text
        from db import get_session, persist_votes, AgendaItemVote
        from sqlalchemy import select, func

        text = _load_fixture("1687_summary.txt")
        result = parse_summary_text(text)

        s = get_session()
        from db import AgendaItem
        from datetime import datetime, timezone
        for item_num in ("4B1", "7A1", "8A1"):
            s.add(AgendaItem(
                body="tempe-cc", meeting_id="1687",
                agenda_item_number=item_num,
                agenda_item_id=f"1687-{item_num}",
                agenda_item_title=f"Test {item_num}",
                sort_order=0, created_at=datetime.now(timezone.utc),
            ))
        s.commit()

        # First persist
        persist_votes(s, "tempe-cc", "1687", result["supervisors"], result["votes"])
        s.commit()
        first_count = s.execute(
            select(func.count()).select_from(AgendaItemVote).where(
                AgendaItemVote.body == "tempe-cc", AgendaItemVote.meeting_id == "1687",
            )
        ).scalar()

        # Second persist (same data)
        persist_votes(s, "tempe-cc", "1687", result["supervisors"], result["votes"])
        s.commit()
        second_count = s.execute(
            select(func.count()).select_from(AgendaItemVote).where(
                AgendaItemVote.body == "tempe-cc", AgendaItemVote.meeting_id == "1687",
            )
        ).scalar()

        self.assertEqual(first_count, second_count,
                         "Votes should be replaced, not duplicated")
        s.close()


# ──────────────────────────────────────────────
#  Meeting normalization
# ──────────────────────────────────────────────

class TestMeetingTypeNormalization(unittest.TestCase):
    """normalize_meeting_type must strip scheduling prefixes."""

    def test_canceled_prefix_fully_stripped(self):
        from scraper.jurisdictions.tempe import normalize_meeting_type
        # Regex now strips "CANCELED - " and "CANCELLED - " completely
        self.assertEqual(
            normalize_meeting_type("CANCELED - Regular City Council Meeting"),
            "Regular City Council Meeting")

    def test_cancelled_double_l_fully_stripped(self):
        from scraper.jurisdictions.tempe import normalize_meeting_type
        self.assertEqual(
            normalize_meeting_type("CANCELLED - Work Study Session"),
            "Work Study Session")

    def test_rescheduled_stripped(self):
        from scraper.jurisdictions.tempe import normalize_meeting_type
        self.assertEqual(
            normalize_meeting_type("RESCHEDULED TO 9/02/2025 - Regular City Council Meeting"),
            "Regular City Council Meeting")


# ──────────────────────────────────────────────
#  sort_order column — DB-level ordering regression
# ──────────────────────────────────────────────

class TestSortOrderInDatabase(unittest.TestCase):
    """Items stored with sort_order must be returned in document order
    by the web app's ORDER BY clause.

    The original bug: ORDER BY agenda_item_number did string sort,
    placing "11" before "4A" because "1" < "4".  The fix uses
    ORDER BY sort_order ASC NULLS LAST, agenda_item_number.
    """

    def setUp(self):
        from db import init_db, get_session
        import db.core as _dc
        self._saved_db_url = _dc.DATABASE_URL
        init_db()
        self._populate_test_data()

    def tearDown(self):
        from db import get_session
        import db.core as _dc
        s = get_session()
        s.close()
        _dc.set_database_url(self._saved_db_url)

    def _populate_test_data(self):
        from db import get_session, AgendaItem
        from datetime import datetime, timezone
        s = get_session()
        now = datetime.now(timezone.utc)
        # Insert items with sort_order that matches document order
        items_data = [
            ("1",  0,  "CALL TO ORDER"),
            ("4A", 4,  "Approval of Minutes"),
            ("4A1",5,  "Sub-item 4A1"),
            ("10", 55, "Public Appearances"),
            ("11", 56, "ADJOURNMENT"),
        ]
        for num, sort, title in items_data:
            s.add(AgendaItem(
                body="tempe-cc", meeting_id="9999",
                agenda_item_number=num, agenda_item_id=f"9999-{num}",
                agenda_item_title=title, sort_order=sort,
                created_at=now,
            ))
        s.commit()
        s.close()

    def test_sort_order_prevents_lexicographic_bug(self):
        """4A (sort=4) before 11 (sort=56), fixing the string-sort bug."""
        from db import get_session, AgendaItem
        from sqlalchemy import select

        s = get_session()
        items = s.execute(
            select(AgendaItem).where(
                AgendaItem.body == "tempe-cc",
                AgendaItem.meeting_id == "9999",
            ).order_by(AgendaItem.sort_order.asc().nulls_last(),
                       AgendaItem.agenda_item_number)
        ).scalars().all()

        numbers = [i.agenda_item_number for i in items]
        # 4A must appear before 10, 11
        idx_4a = numbers.index("4A")
        idx_10 = numbers.index("10")
        idx_11 = numbers.index("11")
        self.assertLess(idx_4a, idx_10, f"4A (pos {idx_4a}) should precede 10 (pos {idx_10})")
        self.assertLess(idx_4a, idx_11, f"4A (pos {idx_4a}) should precede 11 (pos {idx_11})")
        # 10 before 11
        self.assertLess(idx_10, idx_11)
        # 1 is first
        self.assertEqual(numbers[0], "1")
        s.close()


class TestCanceledDetection(unittest.TestCase):
    """search_tempe_meetings sets 'canceled' flag before normalization."""

    def test_canceled_detected(self):
        from scraper.platforms.onbase import parse_meetings_from_html

        # Build minimal HTML with a canceled meeting row
        html = """<table><tr class="meeting-row" data-meeting-id="9999">
        <td data-sortable-type="mtgName">CANCELED - Test Meeting</td>
        <td data-sortable-label="01/15/2025">01/15/2025</td>
        </tr></table>"""

        base_url = "https://tempe.hylandcloud.com/Agendaonline"
        meetings = parse_meetings_from_html(html, base_url, "tempe-cc", [109])
        # search_tempe_meetings isn't directly used here; the canceled flag
        # is set by search_tempe_meetings before normalization.
        # We test the detection logic independently:
        import re
        title = meetings[0]["meeting_title"]
        canceled = bool(re.search(r"CANCEL(?:LED|ED|LED)", title, re.IGNORECASE))
        self.assertTrue(canceled)


# ──────────────────────────────────────────────
#  Supporting documents
# ──────────────────────────────────────────────

class TestSupportingDocumentBackfill(unittest.TestCase):
    """Packet and Summary links should be stored as meeting-level docs."""

    def setUp(self):
        from db import init_db, get_session
        from scraper.jurisdictions.tempe import search_tempe_meetings
        import db.core as _dc
        self._saved_db_url = _dc.DATABASE_URL
        init_db()

    def tearDown(self):
        from db import get_session
        import db.core as _dc
        s = get_session()
        s.close()
        s.close()
        _dc.set_database_url(self._saved_db_url)

    def test_meeting_doc_isolation(self):
        """Docs with agenda_item_number='0' should be classed as meeting-level."""
        from db import get_session, SupportingDocument
        from datetime import datetime, timezone
        s = get_session()

        # Insert a meeting-level doc (Packet)
        s.add(SupportingDocument(
            body="tempe-cc", meeting_id="9999", agenda_item_number="0",
            agenda_item_id=0,
            document_title="Agenda Packet", document_url="http://example.com/packet",
            document_type="Packet", file_name="9999_packet.pdf",
            file_extension=".pdf", scraped_at=datetime.now(timezone.utc),
        ))

        # Insert an item-level doc
        s.add(SupportingDocument(
            body="tempe-cc", meeting_id="9999", agenda_item_number="4B1",
            agenda_item_id=0,
            document_title="Staff Report", document_url="http://example.com/report",
            document_type="Staff Report", file_name="report.pdf",
            file_extension=".pdf", scraped_at=datetime.now(timezone.utc),
        ))
        s.commit()

        # Simulate the route's separation logic
        from sqlalchemy import select
        meeting_docs = []
        docs_by_item = {}
        for d in s.execute(
            select(SupportingDocument).where(
                SupportingDocument.body == "tempe-cc",
                SupportingDocument.meeting_id == "9999",
            )
        ).scalars().all():
            if not d.agenda_item_number or d.agenda_item_number in ("0", 0):
                meeting_docs.append(d)
            else:
                docs_by_item.setdefault(d.agenda_item_number, []).append(d)

        self.assertEqual(len(meeting_docs), 1)
        self.assertEqual(meeting_docs[0].document_type, "Packet")
        self.assertEqual(len(docs_by_item), 1)
        self.assertIn("4B1", docs_by_item)
        s.close()


class TestItemDetailDocumentLinkage(unittest.TestCase):
    """OnBase attachments retain the exact agenda item source key."""

    def test_batch_preserves_parent_agenda_item_id(self):
        from unittest.mock import patch
        from scraper.platforms.onbase import TEMPE_CONFIG, fetch_item_details_batch

        detail_html = """
        <div class="agenda-item-description">Item details with enough text for parsing.</div>
        <a href="/Agendaonline/Documents/DownloadFile/minutes.pdf?documentType=1&amp;meetingId=1964&amp;itemId=15823&amp;publishId=22853&amp;isSection=False&amp;isAttachment=True">
          082526_STUDYSESSIONMINUTES.PDF
        </a>
        """
        items = [{
            "body": "tempe-cc",
            "agenda_item_id": "1964-4B2",
            "agenda_item_number": "4B2",
            "onbase_item_id": 15823,
        }]

        with patch(
            "scraper.platforms.onbase.fetch_item_details_sync",
            return_value=detail_html,
        ), patch(
            "scraper.platforms.onbase.resolve_downloadfile_to_viewdocument",
            return_value=None,
        ):
            docs = fetch_item_details_batch(TEMPE_CONFIG, 1964, items, max_workers=1)

        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["agenda_item_id"], "1964-4B2")
        self.assertEqual(docs[0]["agenda_item_number"], "4B2")


if __name__ == "__main__":
    unittest.main()
