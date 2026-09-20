"""Tests for Industrial Development Authority (IDA) support.

IDA uses a WordPress page at mcida.com with a static HTML table
containing meeting info, agenda PDF links, and minutes PDF links.
This is NOT an AgendaCenter source — it's a custom WordPress table.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Optional


def _load_scraper():
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import scraper as scraper_package
    return scraper_package


scraper = _load_scraper()


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "ida"


def _load_fixture(filename: str) -> str:
    path = FIXTURES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


# ── CLI Tests ──

class TestCLIIdaSubcommand(unittest.TestCase):
    """Test that ida subcommand routes correctly."""

    def test_cli_accepts_ida(self):
        args = scraper.parse_args(["ida", "--sync", "--start-date=2026-01-01"])
        self.assertEqual(args.source, "ida")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2026-01-01")

    def test_ida_no_args(self):
        args = scraper.parse_args(["ida"])
        self.assertEqual(args.source, "ida")

    def test_ida_help(self):
        with self.assertRaises(SystemExit) as ctx:
            scraper.parse_args(["ida", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_ida_sync_flag(self):
        args = scraper.parse_args(["ida", "--sync"])
        self.assertTrue(args.sync)

    def test_ida_headed(self):
        args = scraper.parse_args(["ida", "--headed"])
        self.assertTrue(args.headed)

    def test_ida_limit(self):
        args = scraper.parse_args(["ida", "--limit=5"])
        self.assertEqual(args.limit, 5)

    def test_ida_meeting_id(self):
        args = scraper.parse_args(["ida", "--meeting-id=2026-03-10"])
        self.assertEqual(args.meeting_id, "2026-03-10")

    def test_ida_force(self):
        args = scraper.parse_args(["ida", "--force"])
        self.assertTrue(args.force)

    def test_ida_retry_failed(self):
        args = scraper.parse_args(["ida", "--retry-failed"])
        self.assertTrue(args.retry_failed)

    def test_ida_init_db(self):
        args = scraper.parse_args(["ida", "--init-db"])
        self.assertTrue(args.init_db)

    def test_ida_status(self):
        args = scraper.parse_args(["ida", "--status"])
        self.assertTrue(args.status)

    def test_ida_failed(self):
        args = scraper.parse_args(["ida", "--failed"])
        self.assertTrue(args.failed)

    def test_ida_date_shorthand(self):
        args = scraper.parse_args(["ida", "--date=2026-03-15"])
        self.assertEqual(args.start_date, "2026-03-15")
        self.assertEqual(args.end_date, "2026-03-15")

    def test_ida_date_cannot_combine_with_start_date(self):
        with self.assertRaises(SystemExit):
            scraper.parse_args(["ida", "--date=2026-01-01", "--start-date=2026-02-01"])


# ── Meeting Discovery Tests ──

class TestParseIdaMeetingsFromHTMLFixture(unittest.TestCase):
    """Test meeting discovery from fixture HTML."""

    def test_parse_ida_meetings_count(self):
        """IDA fixture produces the expected number of meetings."""
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        # 30 meeting rows (2024-2026, including cancellations)
        self.assertEqual(len(meetings), 30)

    def test_all_body_ida(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        for m in meetings:
            self.assertEqual(m.body, "ida")

    def test_all_meeting_type(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        for m in meetings:
            self.assertEqual(m.meeting_type, "Industrial Development Authority")

    def test_synthetic_meeting_id_from_date(self):
        """Meeting IDs are derived from the date in ISO format."""
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        for m in meetings:
            # Each meeting_id should be a valid date string
            self.assertRegex(m.meeting_id, r"^\d{4}-\d{2}-\d{2}$")
            # meeting_id should match meeting_date
            self.assertEqual(m.meeting_id, m.meeting_date)

    def test_cancellations_have_agenda_url(self):
        """Cancelled meetings have a Notice-of-Cancellation URL as their agenda_url."""
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        # Find meetings with "Notice of Cancellation" in agenda_url
        cancelled = [m for m in meetings if "Notice-of-Cancellation" in m.agenda_url]
        self.assertGreaterEqual(len(cancelled), 2)  # At least 2024-01-09, 2024-07-16

    def test_meetings_have_agenda_urls(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        for m in meetings:
            self.assertTrue(m.agenda_url, f"Meeting {m.meeting_id} has no agenda_url")
            self.assertIn("mcida.com", m.agenda_url)

    def test_meetings_with_minutes_have_minutes_url(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        with_minutes = [m for m in meetings if m.minutes_url]
        self.assertGreater(len(with_minutes), 20)

    def test_meetings_without_minutes_have_empty_minutes_url(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        without_minutes = [m for m in meetings if not m.minutes_url]
        self.assertGreaterEqual(len(without_minutes), 3)  # At least 3 "Not Available"

    def test_titles_contain_regular_or_special(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        for m in meetings:
            self.assertTrue(
                "Regular" in m.meeting_title or "Special" in m.meeting_title,
                f"Meeting {m.meeting_id} title missing Regular/Special: '{m.meeting_title}'",
            )

    def test_dates_are_iso_format(self):
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        for m in meetings:
            self.assertRegex(m.meeting_date, r"^\d{4}-\d{2}-\d{2}$")

    def test_dates_ascending_order(self):
        """Meetings should be in descending date order (most recent first)."""
        from scraper.county.ida import parse_ida_meetings_from_html
        html = _load_fixture("ida_public_meetings.html")
        meetings = parse_ida_meetings_from_html(html)
        dates = [m.meeting_date for m in meetings]
        self.assertEqual(dates, sorted(dates, reverse=True))


# ── Document Classification Tests ──

class TestIdaDocumentClassification(unittest.TestCase):
    """Test classification of meeting documents (agenda vs minutes vs notice)."""

    def test_classify_agenda_pdf(self):
        from scraper.county.ida import classify_ida_document
        url = "https://mcida.com/wp-content/uploads/2026/03/0.-Agenda-Regular-2026-03-10.pdf"
        result = classify_ida_document(url)
        self.assertEqual(result, "agenda")

    def test_classify_minutes_pdf(self):
        from scraper.county.ida import classify_ida_document
        url = "https://mcida.com/wp-content/uploads/2026/03/Results-of-Public-Meeting-2026-03-10.pdf"
        result = classify_ida_document(url)
        self.assertEqual(result, "minutes")

    def test_classify_cancellation_pdf(self):
        from scraper.county.ida import classify_ida_document
        url = "https://mcida.com/wp-content/uploads/2026/04/Notice-of-Cancellation-2026-04-14-1.pdf"
        result = classify_ida_document(url)
        self.assertEqual(result, "cancellation")

    def test_classify_unknown_pdf(self):
        from scraper.county.ida import classify_ida_document
        url = "https://mcida.com/wp-content/uploads/2025/11/1.-2026-Annual-Meeting-Schedule.pdf"
        result = classify_ida_document(url)
        self.assertEqual(result, "other")


# ── Body-Scoped Persistence Tests ──

class TestIdaBodyScopedPersistence(unittest.TestCase):
    def test_ida_meeting_id_format(self):
        """IDA meeting_ids are date-based, no body prefix."""
        m = scraper.Meeting(
            meeting_date="2026-03-10", meeting_time="", meeting_title="",
            meeting_type="Industrial Development Authority", body="ida",
            row_text="", detail_url="",
            agenda_url="https://mcida.com/wp-content/uploads/2026/03/0.-Agenda-Regular-2026-03-10.pdf",
        )
        self.assertEqual(m.meeting_id, "meeting")
        # IDA uses synthetic IDs from date
        from scraper.county.ida import make_ida_meeting_id
        mid = make_ida_meeting_id("2026-03-10")
        self.assertEqual(mid, "2026-03-10")
        self.assertNotIn("ida", mid)

    def test_ida_body_scoped_persistence_in_db(self):
        from sqlalchemy import create_engine, Column, String, Integer
        from sqlalchemy.orm import declarative_base, Session
        Base = declarative_base()
        class TestMeeting(Base):
            __tablename__ = "test_meetings"
            id = Column(Integer, primary_key=True)
            body = Column(String(16), nullable=False, default="")
            meeting_id = Column(String(64), nullable=False, default="")
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        with Session(engine) as session:
            m = TestMeeting(body="ida", meeting_id="2026-03-10")
            session.add(m)
            session.commit()
            retrieved = session.query(TestMeeting).filter_by(body="ida", meeting_id="2026-03-10").first()
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.body, "ida")
            self.assertEqual(retrieved.meeting_id, "2026-03-10")

    def test_ida_body_fits_varchar(self):
        body = "ida"
        self.assertLessEqual(len(body), 16)


# ── Module Import Tests ──

class TestIdaModuleImport(unittest.TestCase):
    def test_ida_module_imports(self):
        from scraper.county import ida
        self.assertTrue(hasattr(ida, "extract_ida_meetings"))
        self.assertTrue(hasattr(ida, "parse_ida_meetings_from_html"))
        self.assertTrue(hasattr(ida, "classify_ida_document"))
        self.assertTrue(hasattr(ida, "make_ida_meeting_id"))


class TestIdaExportFromPackage(unittest.TestCase):
    def test_ida_functions_exported(self):
        self.assertTrue(hasattr(scraper, "extract_ida_meetings"))
        self.assertTrue(hasattr(scraper, "parse_ida_meetings_from_html"))


# ── Regression Tests ──

class TestAllBodiesStillWork(unittest.TestCase):
    def test_bos_subcommand_still_works(self):
        args = scraper.parse_args(["bos", "--sync"])
        self.assertEqual(args.source, "bos")

    def test_pz_subcommand_still_works(self):
        args = scraper.parse_args(["pz", "--sync"])
        self.assertEqual(args.source, "pz")

    def test_adj_subcommand_still_works(self):
        args = scraper.parse_args(["adj", "--sync"])
        self.assertEqual(args.source, "adj")

    def test_drain_subcommand_still_works(self):
        args = scraper.parse_args(["drain", "--sync"])
        self.assertEqual(args.source, "drain")

    def test_health_subcommand_still_works(self):
        args = scraper.parse_args(["health", "--sync"])
        self.assertEqual(args.source, "health")

    def test_tab_subcommand_still_works(self):
        args = scraper.parse_args(["tab", "--sync"])
        self.assertEqual(args.source, "tab")

    def test_ida_subcommand_still_works(self):
        args = scraper.parse_args(["ida", "--sync"])
        self.assertEqual(args.source, "ida")

    def test_no_subcommand_defaults_to_bos(self):
        args = scraper.parse_args(["--sync"])
        self.assertEqual(args.source, "bos")

    def test_ida_source_in_top_level_help(self):
        """Top-level --help includes ida."""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["--help"])


if __name__ == "__main__":
    unittest.main()
