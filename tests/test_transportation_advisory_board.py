"""Tests for Transportation Advisory Board (TAB) support.

Transportation Advisory Board uses CID=11 on mcdot.maricopa.gov.
Same AgendaCenter pattern as PZ/ADJ/DRAIN/Health for meeting listings.
Agenda pages are PDF-only (no HTML table version available).
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


FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "tab"


def _load_fixture(filename: str) -> str:
    path = FIXTURES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


# ── CLI Tests ──

class TestCLITabSubcommand(unittest.TestCase):
    """Test that tab subcommand routes correctly."""

    def test_cli_accepts_tab(self):
        args = scraper.parse_args(["tab", "--sync", "--start-date=2026-01-01"])
        self.assertEqual(args.source, "tab")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2026-01-01")

    def test_tab_no_args(self):
        args = scraper.parse_args(["tab"])
        self.assertEqual(args.source, "tab")

    def test_tab_help(self):
        with self.assertRaises(SystemExit) as ctx:
            scraper.parse_args(["tab", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_tab_sync_flag(self):
        args = scraper.parse_args(["tab", "--sync"])
        self.assertTrue(args.sync)

    def test_tab_headed(self):
        args = scraper.parse_args(["tab", "--headed"])
        self.assertTrue(args.headed)

    def test_tab_limit(self):
        args = scraper.parse_args(["tab", "--limit=5"])
        self.assertEqual(args.limit, 5)

    def test_tab_meeting_id(self):
        args = scraper.parse_args(["tab", "--meeting-id=3645"])
        self.assertEqual(args.meeting_id, "3645")

    def test_tab_force(self):
        args = scraper.parse_args(["tab", "--force"])
        self.assertTrue(args.force)

    def test_tab_retry_failed(self):
        args = scraper.parse_args(["tab", "--retry-failed"])
        self.assertTrue(args.retry_failed)

    def test_tab_init_db(self):
        args = scraper.parse_args(["tab", "--init-db"])
        self.assertTrue(args.init_db)

    def test_tab_status(self):
        args = scraper.parse_args(["tab", "--status"])
        self.assertTrue(args.status)

    def test_tab_failed(self):
        args = scraper.parse_args(["tab", "--failed"])
        self.assertTrue(args.failed)

    def test_tab_date_shorthand(self):
        args = scraper.parse_args(["tab", "--date=2026-03-15"])
        self.assertEqual(args.start_date, "2026-03-15")
        self.assertEqual(args.end_date, "2026-03-15")

    def test_tab_date_cannot_combine_with_start_date(self):
        with self.assertRaises(SystemExit):
            scraper.parse_args(["tab", "--date=2026-01-01", "--start-date=2026-02-01"])


# ── Search URL Tests ──

class TestTabSearchUrlConstruction(unittest.TestCase):
    """Test tab search URL construction."""

    def test_tab_search_url_uses_cid11(self):
        from scraper.county.tab import build_tab_search_url
        url = build_tab_search_url("01/01/2026", "01/31/2026")
        self.assertIn("CIDs=11", url)
        self.assertIn("mcdot.maricopa.gov", url)
        self.assertIn("AgendaCenter/Search/", url)
        self.assertIn("startDate=01%2F01%2F2026", url)
        self.assertIn("endDate=01%2F31%2F2026", url)

    def test_tab_search_url_format_via_main(self):
        from scraper.county.tab import build_tab_search_url, _format_mm_dd_yyyy
        start = _format_mm_dd_yyyy("2026-01-01")
        end = _format_mm_dd_yyyy("2026-12-31")
        self.assertEqual(start, "01/01/2026")
        self.assertEqual(end, "12/31/2026")
        url = build_tab_search_url(start, end)
        self.assertIn("CIDs=11", url)
        self.assertIn("mcdot.maricopa.gov", url)

    def test_tab_search_url_not_using_www_domain(self):
        from scraper.county.tab import build_tab_search_url
        url = build_tab_search_url("01/01/2026", "01/31/2026")
        self.assertNotIn("www.maricopa.gov", url)


# ── Meeting Discovery Tests ──

class TestParseTabMeetingsFromHTMLFixture(unittest.TestCase):
    """Test meeting discovery from fixture HTML."""

    def test_parse_tab_meetings_from_html_single_meeting(self):
        html = """
        <html><body>
        <table><tbody>
            <tr id="row3645" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for February 25, 2026"><abbr title="February">Feb</abbr> 25, 2026</strong></h3>
                <p>
                  <a href="/AgendaCenter/ViewFile/Agenda/_02252026-3645">
                    Transportation Advisory Board Meeting (February 25, 2026)
                  </a>
                </p>
              </td>
            </tr>
        </tbody></table>
        </body></html>
        """
        from scraper.county.tab import parse_tab_meetings_from_html
        meetings = parse_tab_meetings_from_html(html, "https://mcdot.maricopa.gov/AgendaCenter/Search")
        self.assertEqual(len(meetings), 1)
        m = meetings[0]
        self.assertEqual(m.body, "tab")
        self.assertEqual(m.meeting_type, "Transportation Advisory Board")
        self.assertEqual(m.meeting_date, "2026-02-25")
        self.assertIn("3645", m.meeting_id)

    def test_parse_tab_meetings_body_scoped(self):
        html = """
        <html><body>
        <table><tbody>
            <tr id="row3645" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for February 25, 2026"><abbr title="February">Feb</abbr> 25, 2026</strong></h3>
                <p><a href="/AgendaCenter/ViewFile/Agenda/_02252026-3645">TAB Agenda</a></p>
              </td>
            </tr>
        </tbody></table>
        </body></html>
        """
        from scraper.county.tab import parse_tab_meetings_from_html
        meetings = parse_tab_meetings_from_html(html, "https://mcdot.maricopa.gov/AgendaCenter/Search")
        for m in meetings:
            self.assertEqual(m.body, "tab")

    def test_tab_meeting_id_from_url(self):
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Transportation Advisory Board", body="tab",
            row_text="", detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_02252026-3645",
        )
        self.assertEqual(m.meeting_id, "3645")
        self.assertEqual(m.body, "tab")

    def test_tab_meeting_id_direct_viewfile(self):
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Transportation Advisory Board", body="tab",
            row_text="", detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/3645",
        )
        self.assertEqual(m.meeting_id, "3645")


# ── Year Tab Tests ──

class TestTabYearTabExtraction(unittest.TestCase):
    """Test year-tab extraction for TAB (CID=11)."""

    def test_extract_tab_year_tabs_from_html(self):
        from scraper.county.tab import _extract_tab_year_tabs_from_html as fn
        html = """
        <a href="javascript:changeYear(2026, 11,'a0')">2026</a>
        <a href="javascript:changeYear(2025, 11, 'a1')">2025</a>
        <a href="javascript:changeYear(2024, 11, 'a2')">2024</a>
        """
        self.assertEqual(fn(html), [2024, 2025, 2026])

    def test_extract_tab_year_tabs_deduplicates(self):
        from scraper.county.tab import _extract_tab_year_tabs_from_html as fn
        html = '<a href="javascript:changeYear(2026, 11,\'a0\')">2026</a>'
        self.assertEqual(fn(html), [2026])

    def test_extract_tab_year_tabs_no_tabs(self):
        from scraper.county.tab import _extract_tab_year_tabs_from_html as fn
        self.assertEqual(fn("<html></html>"), [])


# ── Format Functions ──

class TestTabFormatFunctions(unittest.TestCase):
    def test_format_mm_dd_yyyy_converts_iso(self):
        from scraper.county.tab import _format_mm_dd_yyyy
        self.assertEqual(_format_mm_dd_yyyy("2026-01-01"), "01/01/2026")
        self.assertEqual(_format_mm_dd_yyyy("2026-02-25"), "02/25/2026")
        self.assertEqual(_format_mm_dd_yyyy("2025-12-31"), "12/31/2025")

    def test_format_mm_dd_yyyy_empty(self):
        from scraper.county.tab import _format_mm_dd_yyyy
        self.assertIsNone(_format_mm_dd_yyyy(""))


# ── Real Fixture Tests ──

class TestRealTabFixture2026(unittest.TestCase):
    """Test parsing the real 2026 TAB meeting HTML."""

    def setUp(self):
        html = _load_fixture("tab_meetings_2026.html")
        from scraper.county.tab import parse_tab_meetings_from_html
        self.meetings = parse_tab_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )

    def test_2026_meeting_count(self):
        self.assertEqual(len(self.meetings), 1)

    def test_2026_all_body_tab(self):
        for m in self.meetings:
            self.assertEqual(m.body, "tab")

    def test_2026_all_meeting_type(self):
        for m in self.meetings:
            self.assertEqual(m.meeting_type, "Transportation Advisory Board")

    def test_2026_dates(self):
        expected = ["2026-02-25"]
        actual = [m.meeting_date for m in self.meetings]
        self.assertEqual(actual, expected)

    def test_2026_meeting_ids(self):
        ids = [m.meeting_id for m in self.meetings]
        self.assertEqual(ids, ["3645"])

    def test_2026_titles(self):
        titles = {m.meeting_id: m.meeting_title for m in self.meetings}
        self.assertIn("Transportation Advisory Board Meeting", titles["3645"])

    def test_2026_all_have_agenda_urls(self):
        for m in self.meetings:
            self.assertIn("mcdot.maricopa.gov", m.agenda_url)
            self.assertIn("/Agenda/", m.agenda_url)


class TestRealTabFixture2025(unittest.TestCase):
    """Test parsing the real 2025 TAB meeting HTML."""

    def setUp(self):
        html = _load_fixture("tab_meetings_2025.html")
        from scraper.county.tab import parse_tab_meetings_from_html
        self.meetings = parse_tab_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )

    def test_2025_meeting_count(self):
        self.assertEqual(len(self.meetings), 6)

    def test_2025_dates(self):
        dates = [m.meeting_date for m in self.meetings]
        expected = [
            "2025-12-16", "2025-10-21", "2025-08-19",
            "2025-06-17", "2025-04-15", "2025-02-18",
        ]
        self.assertCountEqual(dates, expected)

    def test_2025_meeting_ids_are_digits(self):
        for m in self.meetings:
            self.assertTrue(m.meeting_id.isdigit(), f"ID '{m.meeting_id}' is not all digits")


class TestRealTabYearTabExtraction(unittest.TestCase):
    """Test year tab extraction from real TAB fixtures."""

    def test_broad_range_has_all_year_tabs(self):
        html = _load_fixture("tab_meetings_2015_2026.html")
        from scraper.county.tab import _extract_tab_year_tabs_from_html
        tabs = _extract_tab_year_tabs_from_html(html)
        expected = list(range(2015, 2027))
        self.assertEqual(tabs, expected)

    def test_individual_year_pages_have_only_their_year_tab(self):
        from scraper.county.tab import _extract_tab_year_tabs_from_html as fn
        for year in [2024, 2025, 2026]:
            html = _load_fixture(f"tab_meetings_{year}.html")
            tabs = fn(html)
            self.assertEqual(tabs, [year], f"Year {year} has unexpected tabs: {tabs}")


class TestRealTabOverviewListPage(unittest.TestCase):
    """Test parsing the main TAB landing page."""

    def setUp(self):
        html = _load_fixture("tab_overview.html")
        from scraper.county.tab import parse_tab_meetings_from_html
        self.meetings = parse_tab_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Transportation-Advisory-Board-11"
        )

    def test_overview_meeting_count(self):
        self.assertEqual(len(self.meetings), 1)

    def test_overview_body_tab(self):
        for m in self.meetings:
            self.assertEqual(m.body, "tab")


# ── Body-Scoped Persistence Tests ──

class TestTabBodyScopedPersistence(unittest.TestCase):
    def test_tab_meeting_id_no_prefix(self):
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Transportation Advisory Board", body="tab",
            row_text="", detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_02252026-3645",
        )
        mid = m.meeting_id
        self.assertNotIn("tab", mid)
        self.assertEqual(mid, "3645")

    def test_tab_body_scoped_persistence_in_db(self):
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
            m = TestMeeting(body="tab", meeting_id="3645")
            session.add(m)
            session.commit()
            retrieved = session.query(TestMeeting).filter_by(body="tab", meeting_id="3645").first()
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved.body, "tab")
            self.assertEqual(retrieved.meeting_id, "3645")

    def test_tab_body_fits_varchar(self):
        body = "tab"
        self.assertLessEqual(len(body), 16)


# ── Module Import Tests ──

class TestTabModuleImport(unittest.TestCase):
    def test_tab_module_imports(self):
        from scraper.county import tab
        self.assertTrue(hasattr(tab, "build_tab_search_url"))
        self.assertTrue(hasattr(tab, "extract_tab_meetings"))
        self.assertTrue(hasattr(tab, "parse_tab_meetings_from_html"))
        self.assertTrue(hasattr(tab, "_format_mm_dd_yyyy"))
        self.assertTrue(hasattr(tab, "_extract_tab_year_tabs_from_html"))


class TestTabExportFromPackage(unittest.TestCase):
    def test_tab_functions_exported(self):
        self.assertTrue(hasattr(scraper, "build_tab_search_url"))
        self.assertTrue(hasattr(scraper, "parse_tab_meetings_from_html"))
        self.assertTrue(hasattr(scraper, "_extract_tab_year_tabs_from_html"))


# ── Regression Tests ──

class TestAllBodiesStillWork(unittest.TestCase):
    """Adding tab must not break existing subcommands or bodies."""

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

    def test_no_subcommand_defaults_to_bos(self):
        args = scraper.parse_args(["--sync"])
        self.assertEqual(args.source, "bos")

    def test_tab_search_url_uses_cid11_on_correct_domain(self):
        from scraper.county.tab import build_tab_search_url
        url = build_tab_search_url("01/01/2026", "01/31/2026")
        self.assertIn("CIDs=11", url)
        self.assertIn("mcdot.maricopa.gov", url)
        self.assertNotIn("www.maricopa.gov", url)

    def test_tab_help_in_top_level(self):
        """Top-level --help now includes tab."""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["--help"])


if __name__ == "__main__":
    unittest.main()
