"""Tests for Board of Health (health) support in the Maricopa Agenda project.

Board of Health uses CID=13 on mcdot.maricopa.gov.
The meeting listing uses the same AgendaCenter pattern as PZ/ADJ/DRAIN.
The agenda pages use BOS-style HTML tables or PDFs (not PZ-style PDF-first).
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


# ── Constants ──

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "health"


def _load_fixture(filename: str) -> str:
    """Load fixture HTML as string."""
    path = FIXTURES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


# ── CLI Tests ──

class TestCLIHealthSubcommand(unittest.TestCase):
    """Test that health subcommand routes correctly."""

    def test_cli_accepts_health(self):
        """health --sync routes to health with args.source == 'health'"""
        args = scraper.parse_args(["health", "--sync", "--start-date=2026-01-01"])
        self.assertEqual(args.source, "health")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2026-01-01")

    def test_health_no_args(self):
        """health with no arguments returns source='health'"""
        args = scraper.parse_args(["health"])
        self.assertEqual(args.source, "health")

    def test_health_help(self):
        """health --help prints help and exits with code 0"""
        with self.assertRaises(SystemExit) as ctx:
            scraper.parse_args(["health", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_health_sync_flag(self):
        """health --sync is accessible"""
        args = scraper.parse_args(["health", "--sync"])
        self.assertTrue(args.sync)

    def test_health_headed(self):
        """health --headed is accessible"""
        args = scraper.parse_args(["health", "--headed"])
        self.assertTrue(args.headed)

    def test_health_limit(self):
        """health --limit is accessible"""
        args = scraper.parse_args(["health", "--limit=5"])
        self.assertEqual(args.limit, 5)

    def test_health_meeting_id(self):
        """health --meeting-id is accessible"""
        args = scraper.parse_args(["health", "--meeting-id=123"])
        self.assertEqual(args.meeting_id, "123")

    def test_health_force(self):
        """health --force is accessible"""
        args = scraper.parse_args(["health", "--force"])
        self.assertTrue(args.force)

    def test_health_retry_failed(self):
        """health --retry-failed is accessible"""
        args = scraper.parse_args(["health", "--retry-failed"])
        self.assertTrue(args.retry_failed)

    def test_health_init_db(self):
        """health --init-db is accessible"""
        args = scraper.parse_args(["health", "--init-db"])
        self.assertTrue(args.init_db)

    def test_health_status(self):
        """health --status is accessible"""
        args = scraper.parse_args(["health", "--status"])
        self.assertTrue(args.status)

    def test_health_failed(self):
        """health --failed is accessible"""
        args = scraper.parse_args(["health", "--failed"])
        self.assertTrue(args.failed)

    def test_health_date_shorthand(self):
        """health --date normalizes into --start-date and --end-date"""
        args = scraper.parse_args(["health", "--date=2026-03-15"])
        self.assertEqual(args.start_date, "2026-03-15")
        self.assertEqual(args.end_date, "2026-03-15")

    def test_health_date_cannot_combine_with_start_date(self):
        """health --date combined with --start-date should raise"""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["health", "--date=2026-01-01", "--start-date=2026-02-01"])


# ── Search URL Tests ──

class TestHealthSearchUrlConstruction(unittest.TestCase):
    """Test health search URL construction."""

    def test_health_search_url_uses_cid13(self):
        """health search URL uses CID=13 on mcdot.maricopa.gov"""
        from scraper.county.health import build_health_search_url
        url = build_health_search_url("01/01/2026", "01/31/2026")
        self.assertIn("CIDs=13", url)
        self.assertIn("mcdot.maricopa.gov", url)
        self.assertIn("AgendaCenter/Search/", url)
        self.assertIn("startDate=01%2F01%2F2026", url)
        self.assertIn("endDate=01%2F31%2F2026", url)

    def test_health_search_url_format_via_main(self):
        """Calling _format_mm_dd_yyyy + build_health_search_url together."""
        from scraper.county.health import build_health_search_url, _format_mm_dd_yyyy

        start = _format_mm_dd_yyyy("2026-01-01")
        end = _format_mm_dd_yyyy("2026-12-31")
        self.assertEqual(start, "01/01/2026")
        self.assertEqual(end, "12/31/2026")

        url = build_health_search_url(start, end)
        self.assertIn("CIDs=13", url)
        self.assertIn("startDate=01%2F01%2F2026", url)
        self.assertIn("endDate=12%2F31%2F2026", url)

    def test_health_search_url_not_using_www_domain(self):
        """health search URL uses mcdot.maricopa.gov, not www.maricopa.gov"""
        from scraper.county.health import build_health_search_url
        url = build_health_search_url("01/01/2026", "01/31/2026")
        self.assertNotIn("www.maricopa.gov", url)


# ── Meeting Discovery Tests ──

class TestParseHealthMeetingsFromHTMLFixture(unittest.TestCase):
    """Test meeting discovery from fixture HTML."""

    def test_parse_health_meetings_from_html_single_meeting(self):
        """parse_health_meetings_from_html extracts meeting with body='health'."""
        html = """
        <html><body>
        <table id="meetingDetail">
          <tbody>
            <tr id="row3726" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for April 27, 2026"><abbr title="April">Apr</abbr> 27, 2026</strong></h3>
                <p>
                  <a id="04272026-3726" href="/AgendaCenter/ViewFile/Agenda/_04272026-3726?html=true">
                    Board of Health Meeting Agenda
                  </a>
                </p>
              </td>
              <td class="minutes"></td>
              <td class="media"></td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        from scraper.county.health import parse_health_meetings_from_html

        meetings = parse_health_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search"
        )

        self.assertEqual(len(meetings), 1)
        m = meetings[0]
        self.assertEqual(m.body, "health")
        self.assertEqual(m.meeting_type, "Board of Health")
        self.assertEqual(m.meeting_date, "2026-04-27")
        self.assertIn("3726", m.meeting_id)

    def test_parse_health_meetings_body_scoped(self):
        """parse_health_meetings_from_html creates Meeting with body='health'."""
        html = """
        <html><body>
        <table id="meetingDetail">
          <tbody>
            <tr id="row3615" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for January 26, 2026"><abbr title="January">Jan</abbr> 26, 2026</strong></h3>
                <p>
                  <a href="/AgendaCenter/ViewFile/Agenda/_01262026-3615">
                    Board of Health Meeting Agenda (PDF)
                  </a>
                </p>
              </td>
              <td class="minutes"></td>
              <td class="media"></td>
            </tr>
          </tbody>
        </table>
        </body></html>
        """
        from scraper.county.health import parse_health_meetings_from_html
        meetings = parse_health_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search"
        )
        self.assertGreater(len(meetings), 0)
        for m in meetings:
            self.assertEqual(m.body, "health")

    def test_health_meeting_id_from_url(self):
        """health meeting ID extracted from dashed URL format, body='health'."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Board of Health", body="health", row_text="",
            detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_04272026-3726?html=true",
        )
        self.assertEqual(m.meeting_id, "3726")
        self.assertEqual(m.body, "health")

    def test_health_meeting_id_direct_viewfile(self):
        """health meeting ID from ViewFile/Agenda/NNNN format."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Board of Health", body="health", row_text="",
            detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/3726",
        )
        self.assertEqual(m.meeting_id, "3726")
        self.assertEqual(m.body, "health")


# ── Year Tab Extraction Tests ──

class TestHealthYearTabExtraction(unittest.TestCase):
    """Test year-tab extraction for Board of Health (CID=13)."""

    def test_extract_health_year_tabs_from_html(self):
        """_extract_health_year_tabs_from_html parses changeYear links correctly."""
        from scraper.county.health import _extract_health_year_tabs_from_html as fn

        html = """
        <a href="javascript:changeYear(2026, 13,'a0')">2026</a>
        <a href="javascript:changeYear(2025, 13, 'a1')">2025</a>
        <a href="javascript:changeYear(2024, 13, 'a2')">2024</a>
        """
        self.assertEqual(fn(html), [2024, 2025, 2026])

    def test_extract_health_year_tabs_deduplicates(self):
        """Duplicate changeYear links produce one entry per year."""
        from scraper.county.health import _extract_health_year_tabs_from_html as fn

        html = """
        <a href="javascript:changeYear(2026, 13,'a0')">2026</a>
        <a href="javascript:changeYear(2026, 13,'a0')">2026</a>
        """
        self.assertEqual(fn(html), [2026])

    def test_extract_health_year_tabs_no_tabs(self):
        """No changeYear links returns empty list."""
        from scraper.county.health import _extract_health_year_tabs_from_html as fn
        self.assertEqual(fn("<html></html>"), [])

    def test_extract_health_year_tabs_cid_13(self):
        """health year tabs use CID=13, but CID is irrelevant to extraction."""
        from scraper.county.health import _extract_health_year_tabs_from_html as fn

        html = """
        <a href="javascript:changeYear(2026, 13,'a0')">2026</a>
        <a href="javascript:changeYear(2025, 9,'b0')">2025</a>
        """
        self.assertEqual(fn(html), [2025, 2026])


# ── Format Function Tests ──

class TestHealthFormatFunctions(unittest.TestCase):
    """Test _format_mm_dd_yyyy for health."""

    def test_format_mm_dd_yyyy_converts_iso(self):
        """Converts YYYY-MM-DD to MM/DD/YYYY."""
        from scraper.county.health import _format_mm_dd_yyyy
        self.assertEqual(_format_mm_dd_yyyy("2026-01-01"), "01/01/2026")
        self.assertEqual(_format_mm_dd_yyyy("2026-04-27"), "04/27/2026")
        self.assertEqual(_format_mm_dd_yyyy("2025-12-31"), "12/31/2025")

    def test_format_mm_dd_yyyy_empty(self):
        """Empty input returns None."""
        from scraper.county.health import _format_mm_dd_yyyy
        self.assertIsNone(_format_mm_dd_yyyy(""))

    def test_format_mm_dd_yyyy_invalid(self):
        """Invalid input returns the input string unchanged."""
        from scraper.county.health import _format_mm_dd_yyyy
        self.assertEqual(_format_mm_dd_yyyy("not-a-date"), "not-a-date")


# ── Real Fixture Tests ──

class TestRealHealthFixture2026(unittest.TestCase):
    """Test parsing the real 2026 Board of Health meeting HTML."""

    def setUp(self):
        html = _load_fixture("boh_meetings_2026.html")
        from scraper.county.health import parse_health_meetings_from_html
        self.meetings = parse_health_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )

    def test_2026_meeting_count(self):
        """2026 fixture produces exactly 2 meetings."""
        self.assertEqual(len(self.meetings), 2)

    def test_2026_all_body_health(self):
        """All 2026 Board of Health meetings have body='health'."""
        for m in self.meetings:
            self.assertEqual(m.body, "health")

    def test_2026_all_meeting_type(self):
        """All 2026 meetings have meeting_type='Board of Health'."""
        for m in self.meetings:
            self.assertEqual(m.meeting_type, "Board of Health")

    def test_2026_dates(self):
        """2026 Board of Health meetings have correct dates."""
        expected = ["2026-04-27", "2026-01-26"]
        actual = [m.meeting_date for m in self.meetings]
        self.assertCountEqual(actual, expected)

    def test_2026_meeting_ids(self):
        """2026 Board of Health meeting IDs are correct."""
        ids = [m.meeting_id for m in self.meetings]
        expected_ids = ["3726", "3615"]
        self.assertCountEqual(ids, expected_ids)

    def test_2026_titles(self):
        """2026 Board of Health meeting titles are correct."""
        titles = {m.meeting_id: m.meeting_title for m in self.meetings}
        self.assertIn("Board of Health Meeting Agenda", titles["3726"])
        self.assertIn("Board of Health Meeting Agenda", titles["3615"])

    def test_2026_all_have_agenda_urls(self):
        """All 2026 meetings have agenda URLs on mcdot.maricopa.gov."""
        for m in self.meetings:
            self.assertIn("mcdot.maricopa.gov", m.agenda_url)
            self.assertIn("/Agenda/", m.agenda_url)

    def test_2026_variety(self):
        """One meeting has ?html=true (HTML agenda), the other is PDF-only."""
        has_html = [m for m in self.meetings if "?html=true" in m.agenda_url]
        has_pdf = [m for m in self.meetings if "(PDF)" in m.meeting_title]
        self.assertEqual(len(has_html), 1)
        self.assertEqual(len(has_pdf), 1)


class TestRealHealthYearTabExtraction(unittest.TestCase):
    """Test year tab extraction from real Board of Health fixtures."""

    def test_broad_range_has_all_year_tabs(self):
        """2013-2026 broad fixture page has year tabs for all 14 years."""
        html = _load_fixture("boh_meetings_2013_2026.html")
        from scraper.county.health import _extract_health_year_tabs_from_html
        tabs = _extract_health_year_tabs_from_html(html)
        expected = list(range(2013, 2027))
        self.assertEqual(tabs, expected)

    def test_individual_year_pages_have_only_their_year_tab(self):
        """Each individual-year page only shows its own year tab."""
        from scraper.county.health import _extract_health_year_tabs_from_html as tabs_fn
        for year in [2024, 2025, 2026]:
            html = _load_fixture(f"boh_meetings_{year}.html")
            tabs = tabs_fn(html)
            self.assertEqual(tabs, [year],
                             f"Year {year} page has unexpected tabs: {tabs}")


class TestRealHealthOverallListPage(unittest.TestCase):
    """Test parsing the main Board of Health overview page (/Board-of-Health-13)."""

    def setUp(self):
        html = _load_fixture("boh_overview.html")
        from scraper.county.health import parse_health_meetings_from_html
        self.meetings = parse_health_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Board-of-Health-13"
        )

    def test_overview_meeting_count(self):
        """BOH overview page (2026 default tab) produces 2 meetings."""
        self.assertEqual(len(self.meetings), 2)

    def test_overview_body_health(self):
        """BOH overview meetings have body='health'."""
        for m in self.meetings:
            self.assertEqual(m.body, "health")


class TestRealHealthAgendaItemExtraction(unittest.TestCase):
    """Test extracting structured agenda items from a Board of Health agenda page."""

    def test_parse_health_agenda_html_items(self):
        """parse_health_agenda_html extracts items from the HTML agenda."""
        from scraper.county.health import parse_health_agenda_html

        html = _load_fixture("boh_agenda_04272026.html")
        items = parse_health_agenda_html(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_04272026-3726?html=true",
            "https://mcdot.maricopa.gov/",
        )

        self.assertIsInstance(items, list)
        self.assertGreaterEqual(len(items), 8)

        # Check item structure
        item1 = items[0]
        self.assertIn("agenda_item_number", item1)
        self.assertIn("agenda_item_title", item1)
        self.assertIn("agenda_item_text", item1)

        # Item 1 should be "Call to Order"
        self.assertEqual(item1["agenda_item_number"], "1")
        self.assertIn("Call to Order", item1.get("agenda_item_title", ""))

        # Last item should be "Adjournment"
        last = items[-1]
        self.assertIn("Adjournment", last.get("agenda_item_title", ""))

    def test_parse_health_agenda_html_item_fields(self):
        """Each parsed agenda item has required fields."""
        from scraper.county.health import parse_health_agenda_html

        html = _load_fixture("boh_agenda_04272026.html")
        items = parse_health_agenda_html(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_04272026-3726?html=true",
            "https://mcdot.maricopa.gov/",
        )

        required_fields = {"agenda_item_number", "agenda_item_title",
                           "agenda_item_text", "source_url", "agenda_item_url",
                           "vote_or_action", "supporting_doc_dicts"}
        for item in items:
            for field in required_fields:
                self.assertIn(field, item, f"Item {item.get('agenda_item_number')} missing field: {field}")

    def test_parse_health_agenda_html_facilitators(self):
        """Facilitator/Presenter info is captured in item text."""
        from scraper.county.health import parse_health_agenda_html

        html = _load_fixture("boh_agenda_04272026.html")
        items = parse_health_agenda_html(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_04272026-3726?html=true",
            "https://mcdot.maricopa.gov/",
        )

        # Item 1 should include President Osborne
        item1 = items[0]
        self.assertIn("President Osborne", item1.get("agenda_item_text", ""))

        # Item 4 (Public Health Finance) should include the budget desc
        item4 = next((it for it in items if "Public Health Finance" in it.get("agenda_item_title", "")), None)
        if item4:
            self.assertIn("fiscal year", item4.get("agenda_item_text", "").lower())

    def test_parse_health_agenda_html_no_agenda(self):
        """Empty or no-agenda HTML returns empty list gracefully."""
        from scraper.county.health import parse_health_agenda_html
        items = parse_health_agenda_html(
            "<html><body>No agenda</body></html>",
            "https://example.com/agenda",
            "https://example.com/",
        )
        self.assertEqual(items, [])


# ── Body-Scoped Persistence Tests ──

class TestHealthBodyScopedPersistence(unittest.TestCase):
    """Verify body-scoped identity for health."""

    def test_health_meeting_id_no_prefix(self):
        """health meeting_ids do not include a body prefix."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Board of Health", body="health", row_text="",
            detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_04272026-3726?html=true",
        )
        mid = m.meeting_id
        self.assertNotIn("health", mid)
        self.assertEqual(mid, "3726")

    def test_health_body_scoped_persistence_in_db(self):
        """Simulate DB persistence with body='health' (in-memory SQLite)."""
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
            m = TestMeeting(body="health", meeting_id="3726")
            session.add(m)
            session.commit()

            retrieved = session.query(TestMeeting).filter_by(
                body="health", meeting_id="3726"
            ).first()
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.body, "health")
            self.assertEqual(retrieved.meeting_id, "3726")

    def test_health_body_fits_varchar(self):
        """body='health' (6 chars) fits VARCHAR(16)."""
        body = "health"
        self.assertLessEqual(len(body), 16)


# ── Module Import Tests ──

class TestHealthModuleImport(unittest.TestCase):
    """Test that the health module is importable and exports expected names."""

    def test_health_module_imports(self):
        """The current county health module is importable."""
        from scraper.county import health
        self.assertTrue(hasattr(health, "build_health_search_url"))
        self.assertTrue(hasattr(health, "extract_health_meetings"))
        self.assertTrue(hasattr(health, "extract_health_agenda_items"))
        self.assertTrue(hasattr(health, "parse_health_meetings_from_html"))
        self.assertTrue(hasattr(health, "parse_health_agenda_html"))
        self.assertTrue(hasattr(health, "_format_mm_dd_yyyy"))
        self.assertTrue(hasattr(health, "_extract_health_year_tabs_from_html"))


class TestHealthExportFromPackage(unittest.TestCase):
    """Test that health names are exported from the scraper package."""

    def test_health_functions_exported(self):
        """Key health functions are accessible from the scraper package."""
        self.assertTrue(hasattr(scraper, "build_health_search_url"))
        self.assertTrue(hasattr(scraper, "parse_health_meetings_from_html"))
        self.assertTrue(hasattr(scraper, "_extract_health_year_tabs_from_html"))
        self.assertTrue(hasattr(scraper, "_format_mm_dd_yyyy"))


# ── Regression: No Impact on Other Bodies ──

class TestAllBodiesStillWork(unittest.TestCase):
    """Adding health must not break existing subcommands or bodies."""

    def test_bos_subcommand_still_works(self):
        """BOS subcommand still routes to source='bos'."""
        args = scraper.parse_args(["bos", "--sync"])
        self.assertEqual(args.source, "bos")

    def test_pz_subcommand_still_works(self):
        """PZ subcommand still routes to source='pz'."""
        args = scraper.parse_args(["pz", "--sync"])
        self.assertEqual(args.source, "pz")

    def test_adj_subcommand_still_works(self):
        """ADJ subcommand still routes to source='adj'."""
        args = scraper.parse_args(["adj", "--sync"])
        self.assertEqual(args.source, "adj")

    def test_drain_subcommand_still_works(self):
        """DRAIN subcommand still routes to source='drain'."""
        args = scraper.parse_args(["drain", "--sync"])
        self.assertEqual(args.source, "drain")

    def test_no_subcommand_defaults_to_bos(self):
        """No subcommand defaults to source='bos'."""
        args = scraper.parse_args(["--sync"])
        self.assertEqual(args.source, "bos")

    def test_health_search_url_uses_cid13_on_correct_domain(self):
        """health search URL uses CID=13 on mcdot.maricopa.gov."""
        from scraper.county.health import build_health_search_url
        url = build_health_search_url("01/01/2026", "01/31/2026")
        self.assertIn("CIDs=13", url)
        self.assertIn("mcdot.maricopa.gov", url)
        self.assertNotIn("www.maricopa.gov", url)


if __name__ == "__main__":
    unittest.main()
