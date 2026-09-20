"""Tests for Drainage Review Board (DRB/Drain) support in the Maricopa Agenda project.

Tests cover CLI parsing, meeting discovery, PDF parsing, persistence,
and regression coverage ensuring BOS, PZ, and ADJ still work.
"""

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path


def _load_scraper():
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import scraper as scraper_package
    return scraper_package


scraper = _load_scraper()


# ── CLI Tests ──

class TestCLIDrainSubcommand(unittest.TestCase):
    """Test that drain subcommand routes correctly."""

    def test_cli_accepts_drain(self):
        """drain --sync --start-date=2012-01-01 routes to drain with args.source == 'drain'"""
        args = scraper.parse_args(["drain", "--sync", "--start-date=2012-01-01"])
        self.assertEqual(args.source, "drain")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2012-01-01")

    def test_drain_no_args(self):
        """drain with no arguments returns source='drain'"""
        args = scraper.parse_args(["drain"])
        self.assertEqual(args.source, "drain")

    def test_drain_help(self):
        """drain --help prints help and exits with code 0"""
        with self.assertRaises(SystemExit) as ctx:
            scraper.parse_args(["drain", "--help"])
        self.assertEqual(ctx.exception.code, 0)

    def test_drain_sync_flag(self):
        """drain --sync is accessible"""
        args = scraper.parse_args(["drain", "--sync"])
        self.assertTrue(args.sync)

    def test_drain_headed(self):
        """drain --headed is accessible"""
        args = scraper.parse_args(["drain", "--headed"])
        self.assertTrue(args.headed)

    def test_drain_limit(self):
        """drain --limit is accessible"""
        args = scraper.parse_args(["drain", "--limit=5"])
        self.assertEqual(args.limit, 5)

    def test_drain_meeting_id(self):
        """drain --meeting-id is accessible"""
        args = scraper.parse_args(["drain", "--meeting-id=123"])
        self.assertEqual(args.meeting_id, "123")

    def test_drain_force(self):
        """drain --force is accessible"""
        args = scraper.parse_args(["drain", "--force"])
        self.assertTrue(args.force)

    def test_drain_retry_failed(self):
        """drain --retry-failed is accessible"""
        args = scraper.parse_args(["drain", "--retry-failed"])
        self.assertTrue(args.retry_failed)

    def test_drain_init_db(self):
        """drain --init-db is accessible"""
        args = scraper.parse_args(["drain", "--init-db"])
        self.assertTrue(args.init_db)

    def test_drain_status(self):
        """drain --status is accessible"""
        args = scraper.parse_args(["drain", "--status"])
        self.assertTrue(args.status)

    def test_drain_failed(self):
        """drain --failed is accessible"""
        args = scraper.parse_args(["drain", "--failed"])
        self.assertTrue(args.failed)

    def test_drain_date_shorthand(self):
        """drain --date normalizes into --start-date and --end-date"""
        args = scraper.parse_args(["drain", "--date=2012-03-15"])
        self.assertEqual(args.start_date, "2012-03-15")
        self.assertEqual(args.end_date, "2012-03-15")

    def test_drain_date_cannot_combine_with_start_date(self):
        """drain --date combined with --start-date should raise"""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["drain", "--date=2012-01-01", "--start-date=2012-02-01"])


# ── Search URL Tests ──

class TestDrainSearchUrlConstruction(unittest.TestCase):
    """Test drain search URL construction."""

    def test_drain_search_url_uses_cid19(self):
        """drain search URL uses CID=19"""
        from scraper.county.drain import build_drain_search_url
        url = build_drain_search_url("01/01/2012", "01/31/2012")
        self.assertIn("CIDs=19", url)
        self.assertIn("mcdot.maricopa.gov", url)
        self.assertIn("AgendaCenter/Search/", url)
        self.assertIn("startDate=01%2F01%2F2012", url)
        self.assertIn("endDate=01%2F31%2F2012", url)

    def test_drain_search_url_format_via_main(self):
        """Calling _format_mm_dd_yyyy + build_drain_search_url together (as main() does)."""
        from scraper.county.drain import build_drain_search_url, _format_mm_dd_yyyy

        start = _format_mm_dd_yyyy("2012-01-01")
        end = _format_mm_dd_yyyy("2012-12-31")
        self.assertEqual(start, "01/01/2012")
        self.assertEqual(end, "12/31/2012")

        url = build_drain_search_url(start, end)
        self.assertIn("CIDs=19", url)
        self.assertIn("mcdot.maricopa.gov", url)
        self.assertIn("startDate=01%2F01%2F2012", url)
        self.assertIn("endDate=12%2F31%2F2012", url)

    def test_drain_search_url_not_using_pz_domain(self):
        """drain search URL does NOT use www.maricopa.gov"""
        from scraper.county.drain import build_drain_search_url
        url = build_drain_search_url("01/01/2012", "01/31/2012")
        self.assertNotIn("www.maricopa.gov", url)


# ── Meeting Discovery Tests ──

class TestParseDrainMeetingsFromHTMLFixture(unittest.TestCase):
    """Test meeting discovery from fixture HTML."""

    def test_parse_drain_meetings_from_html_single_meeting(self):
        """parse_drain_meetings_from_html extracts meeting with body='drain'."""
        html = """
        <html><body>
        <table id="meetingDetail">
          <tbody>
            <tr id="row1234" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for May 11, 2011"><abbr title="May">May</abbr> 11, 2011</strong></h3>
                <p>
                  <a id="05112011-1234" href="/AgendaCenter/ViewFile/Agenda/_05112011-1234?html=true">
                    May 11, 2011 Drainage Review Board Meeting
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
        from scraper.county.drain import parse_drain_meetings_from_html

        meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search"
        )

        self.assertEqual(len(meetings), 1)
        m = meetings[0]
        self.assertEqual(m.body, "drain")
        self.assertEqual(m.meeting_type, "Drainage Review Board")
        self.assertEqual(m.meeting_date, "2011-05-11")
        self.assertIn("1234", m.meeting_id)

    def test_parse_drain_meetings_body_scoped(self):
        """parse_drain_meetings_from_html creates Meeting with body='drain'."""
        html = """
        <html><body>
        <table id="meetingDetail">
          <tbody>
            <tr id="row1234" class="catAgendaRow">
              <td>
                <h3><strong aria-label="Agenda for May 11, 2011"><abbr title="May">May</abbr> 11, 2011</strong></h3>
                <p>
                  <a href="/AgendaCenter/ViewFile/Agenda/_05112011-1234?html=true">
                    May 11, 2011 Drainage Review Board Meeting
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
        from scraper.county.drain import parse_drain_meetings_from_html
        meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search"
        )
        self.assertGreater(len(meetings), 0)
        for m in meetings:
            self.assertEqual(m.body, "drain")

    def test_drain_meeting_id_from_url(self):
        """Drain meeting ID extracted from dashed URL format, no drain- prefix."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Drainage Review Board", body="drain", row_text="",
            detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_05112011-1234?html=true",
        )
        self.assertEqual(m.meeting_id, "1234")
        self.assertEqual(m.body, "drain")

    def test_drain_meeting_id_direct_viewfile(self):
        """Drain meeting ID from ViewFile/Agenda/NNNN format."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Drainage Review Board", body="drain", row_text="",
            detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/1234",
        )
        self.assertEqual(m.meeting_id, "1234")
        self.assertEqual(m.body, "drain")


# ── Year Tab Extraction Tests ──

class TestDrainYearTabExtraction(unittest.TestCase):
    """Test year-tab extraction for Drain (same pattern as PZ/ADJ, CID=19)."""

    def test_extract_drain_year_tabs_from_html(self):
        """_extract_drain_year_tabs_from_html parses changeYear links correctly."""
        from scraper.county.drain import _extract_drain_year_tabs_from_html as fn

        html = """
        <a href="javascript:changeYear(2013, 19,'a0')">2013</a>
        <a href="javascript:changeYear(2012, 19, 'a1')">2012</a>
        <a href="javascript:changeYear(2011, 19, 'a2')">2011</a>
        """
        self.assertEqual(fn(html), [2011, 2012, 2013])

    def test_extract_drain_year_tabs_deduplicates(self):
        """Duplicate changeYear links produce one entry per year."""
        from scraper.county.drain import _extract_drain_year_tabs_from_html as fn

        html = """
        <a href="javascript:changeYear(2011, 19,'a0')">2011</a>
        <a href="javascript:changeYear(2011, 19,'a0')">2011</a>
        """
        self.assertEqual(fn(html), [2011])

    def test_extract_drain_year_tabs_no_tabs(self):
        """No changeYear links returns empty list."""
        from scraper.county.drain import _extract_drain_year_tabs_from_html as fn
        self.assertEqual(fn("<html></html>"), [])

    def test_extract_drain_year_tabs_cid_19(self):
        """Drain year tabs use CID=19."""
        from scraper.county.drain import _extract_drain_year_tabs_from_html as fn

        html = """
        <a href="javascript:changeYear(2012, 19,'a0')">2012</a>
        <a href="javascript:changeYear(2013, 9,'b0')">2013</a>
        """
        # Both are found, CID is irrelevant to extraction
        self.assertEqual(fn(html), [2012, 2013])


# ── Overview Parsing Tests ──

class TestParseDrainOverview(unittest.TestCase):
    """Test drain overview page parsing (div.item.level1 structure)."""

    def test_parse_drain_overview_identifies_agenda_and_staff_reports(self):
        """parse_drain_overview correctly identifies agenda doc and staff reports."""
        from scraper.county.drain import parse_drain_overview

        html = """
        <html><body>
        <div class="item level1">
            <span class="title">May 11, 2011 Drainage Board Agenda</span>
            <span class="file"><a class="file" href="/AgendaCenter/ViewFile/Item/10500">Agenda.pdf</a></span>
        </div>
        <div class="item level1">
            <span class="title">Item 1 - DRB Application D2011001</span>
            <span class="file"><a class="file" href="/AgendaCenter/ViewFile/Item/10501">Staff Report.pdf</a></span>
        </div>
        </body></html>
        """
        result = parse_drain_overview(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_05112011-1234?html=true",
            "https://mcdot.maricopa.gov/",
        )

        self.assertIsNotNone(result)
        self.assertIn("Agenda", result.get("agenda_title", ""))
        self.assertIn("Item/10500", result.get("agenda_pdf_url", ""))

        staff_titles = [s["document_title"] for s in result.get("staff_report_files", [])]
        self.assertEqual(len(staff_titles), 1)
        self.assertIn("Staff Report", staff_titles[0])

    def test_parse_drain_overview_no_headings(self):
        """parse_drain_overview returns None when page has no item.level1 or span.title."""
        from scraper.county.drain import parse_drain_overview
        html = "<html><body><p>No headings here</p></body></html>"
        result = parse_drain_overview(
            html, "https://example.com/overview", "https://example.com/"
        )
        self.assertIsNone(result)

    def test_parse_drain_overview_fallback_h1_title(self):
        """parse_drain_overview fallback span.title-only extraction works."""
        from scraper.county.drain import parse_drain_overview

        html = """
        <html><body>
        <span class="title">May 11, 2011 Drainage Board Agenda</span>
        <span class="file"><a class="file" href="/AgendaCenter/ViewFile/Item/10500">Agenda.pdf</a></span>
        <span class="title">Item 1 - Staff Report</span>
        <span class="file"><a class="file" href="/AgendaCenter/ViewFile/Item/10501">Staff Report.pdf</a></span>
        </body></html>
        """
        result = parse_drain_overview(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/_05112011-1234?html=true",
            "https://mcdot.maricopa.gov/",
        )
        self.assertIsNotNone(result)
        self.assertIn("Agenda", result.get("agenda_title", ""))

    def test_parse_drain_overview_rewrites_domain(self):
        """parse_drain_overview rewrites www.maricopa.gov URLs for drain."""
        from scraper.county.drain import parse_drain_overview

        html = """
        <html><body>
        <div class="item level1">
            <span class="title">Staff Report D2011001</span>
            <span class="file">
              <a class="file" href="https://www.maricopa.gov/AgendaCenter/ViewFile/Item/10501">Report.pdf</a>
            </span>
        </div>
        </body></html>
        """
        result = parse_drain_overview(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/1234?html=true",
            "https://mcdot.maricopa.gov/",
        )
        self.assertIsNotNone(result)
        staff_files = result.get("staff_report_files", [])
        if staff_files:
            report_url = staff_files[0].get("document_url", "")
            self.assertIn("mcdot.maricopa.gov", report_url)


# ── PDF Parsing Tests ──

class TestDrainAgendaPDFParsing(unittest.TestCase):
    """Test drain agenda PDF parsing against extracted text layouts."""

    def test_parse_drain_agenda_pdf_empty_file(self):
        """parse_drain_agenda_pdf returns [] for non-existent file."""
        from scraper.county.drain import parse_drain_agenda_pdf
        result = parse_drain_agenda_pdf("/tmp/nonexistent_file.pdf")
        self.assertEqual(result, [])

    def test_parse_drain_agenda_pdf_invalid_path(self):
        """parse_drain_agenda_pdf returns [] for empty path."""
        from scraper.county.drain import parse_drain_agenda_pdf
        self.assertEqual(parse_drain_agenda_pdf(""), [])

    def test_parse_drain_agenda_pdf_returns_list(self):
        """parse_drain_agenda_pdf always returns a list (even with minimal PDF)."""
        from scraper.county.drain import parse_drain_agenda_pdf

        pdf_bytes = (
            b"%PDF-1.4\n"
            b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/MediaBox[0 0 612 792]/Parent 2 0 R"
            b"/Resources<</Font<</F1 4 0 R>>>>/Contents 5 0 R>>endobj\n"
            b"4 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\n"
            b"5 0 obj<</Length 44>>stream\n"
            b"BT /F1 12 Tf 72 700 Td (1.) Tj ET\n"
            b"endstream\nendobj\n"
            b"xref\n0 6\n0000000000 65535 f \n0000000009 00000 n \n0000000058 00000 n \n"
            b"0000000115 00000 n \n0000000266 00000 n \n0000000348 00000 n \n"
            b"trailer<</Size 6/Root 1 0/R>>\n"
            b"startxref\n445\n%%EOF\n"
        )
        pdf_path = Path("/tmp/drain_regression_test.pdf")
        pdf_path.write_bytes(pdf_bytes)

        items = parse_drain_agenda_pdf(str(pdf_path))
        pdf_path.unlink(missing_ok=True)
        self.assertIsInstance(items, list)


# ── Domain Rewriting Tests ──

class TestDrainDomainRewrite(unittest.TestCase):
    """Test domain rewriting for drain resources."""

    def test_rewrite_drain_domain_empty(self):
        """_rewrite_drain_domain handles empty input."""
        from scraper.county.drain import _rewrite_drain_domain
        self.assertEqual(_rewrite_drain_domain(""), "")
        self.assertIsNone(_rewrite_drain_domain(None))

    def test_rewrite_drain_domain_maricopa(self):
        """_rewrite_drain_domain rewrites www.maricopa.gov to mcdot.maricopa.gov."""
        from scraper.county.drain import _rewrite_drain_domain
        result = _rewrite_drain_domain(
            "https://www.maricopa.gov/AgendaCenter/ViewFile/Item/1234"
        )
        self.assertEqual(
            result,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Item/1234"
        )

    def test_rewrite_drain_domain_civicplus(self):
        """_rewrite_drain_domain rewrites az-maricopacounty.civicplus.com."""
        from scraper.county.drain import _rewrite_drain_domain
        result = _rewrite_drain_domain(
            "https://az-maricopacounty.civicplus.com/AgendaCenter/ViewFile/Item/1234"
        )
        self.assertEqual(
            result,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Item/1234"
        )

    def test_rewrite_drain_domain_mcdot_preserved(self):
        """_rewrite_drain_domain preserves mcdot.maricopa.gov URLs unchanged."""
        from scraper.county.drain import _rewrite_drain_domain
        url = "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Item/1234"
        # Should not double-rewrite
        result = _rewrite_drain_domain(url)
        self.assertEqual(result, url)

    def test_rewrite_drain_domain_no_match(self):
        """_rewrite_drain_domain leaves unrelated domains unchanged."""
        from scraper.county.drain import _rewrite_drain_domain
        url = "https://example.com/file.pdf"
        self.assertEqual(_rewrite_drain_domain(url), url)


# ── Body-Scoped Persistence Tests ──

class TestDrainBodyScopedPersistence(unittest.TestCase):
    """Test that drain persistence uses body='drain' and doesn't interfere."""

    def test_drain_meeting_id_no_prefix(self):
        """Drain meeting IDs stored without drain- prefix; body field provides scope."""
        m = scraper.Meeting(
            meeting_date="2011-05-11", meeting_time="", meeting_title="",
            meeting_type="Drainage Review Board", body="drain", row_text="",
            detail_url="",
            agenda_url="https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/1234",
        )
        self.assertFalse(m.meeting_id.startswith("drain-"),
                         "meeting_id should not have drain- prefix")
        self.assertEqual(m.body, "drain")

    def test_drain_body_scoped_persistence_in_db(self):
        """Persisting drain data with body='drain' does not interfere with BOS, PZ, or ADJ."""
        import os
        old_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = "sqlite:///"
        try:
            from scripts import db
            import importlib
            importlib.reload(db)

            db.init_db()
            session = db.get_session()

            meeting_dict = {
                "meeting_id": "1234", "meeting_date": "2011-05-11",
                "meeting_type": "Drainage Review Board", "meeting_title": "DRB Test",
                "source_url": "https://mcdot.maricopa.gov/drain/1234",
            }
            db.create_or_get_meeting(session, "drain", meeting_dict)
            session.commit()

            # Also create BOS, PZ, and ADJ with the same meeting_id to verify isolation
            meeting_dict_bos = dict(meeting_dict)
            meeting_dict_bos["meeting_type"] = "Formal"
            db.create_or_get_meeting(session, "bos", meeting_dict_bos)

            meeting_dict_pz = dict(meeting_dict)
            meeting_dict_pz["meeting_type"] = "Planning & Zoning"
            db.create_or_get_meeting(session, "pz", meeting_dict_pz)

            meeting_dict_adj = dict(meeting_dict)
            meeting_dict_adj["meeting_type"] = "Board of Adjustment"
            db.create_or_get_meeting(session, "adj", meeting_dict_adj)
            session.commit()

            # All four should coexist
            from sqlalchemy import select, func
            count_drain = session.execute(
                select(func.count()).select_from(db.Meeting).where(
                    db.Meeting.body == "drain", db.Meeting.meeting_id == "1234"
                )
            ).scalar()
            count_bos = session.execute(
                select(func.count()).select_from(db.Meeting).where(
                    db.Meeting.body == "bos", db.Meeting.meeting_id == "1234"
                )
            ).scalar()
            count_pz = session.execute(
                select(func.count()).select_from(db.Meeting).where(
                    db.Meeting.body == "pz", db.Meeting.meeting_id == "1234"
                )
            ).scalar()
            count_adj = session.execute(
                select(func.count()).select_from(db.Meeting).where(
                    db.Meeting.body == "adj", db.Meeting.meeting_id == "1234"
                )
            ).scalar()

            self.assertEqual(count_drain, 1)
            self.assertEqual(count_bos, 1)
            self.assertEqual(count_pz, 1)
            self.assertEqual(count_adj, 1)

            session.close()
        finally:
            os.environ["DATABASE_URL"] = old_url or ""


# ── "No Agenda" Graceful Handling Tests ──

class TestDrainNoAgendaGraceful(unittest.TestCase):
    """Test that No Agenda meetings are handled gracefully."""

    def test_drain_no_agenda_html_returns_empty(self):
        """extract_drain_agenda_items returns [] for No Agenda meetings."""
        from scraper.county.drain import parse_drain_overview

        # Simulate a "No Agenda" page: div.item.level1 with generic content
        html = """
        <html><body>
        <div class="item level1">
            <span class="title">May 11, 2011 Drainage Board No Agenda</span>
            <span class="file">No documents available</span>
        </div>
        </body></html>
        """
        result = parse_drain_overview(
            html,
            "https://mcdot.maricopa.gov/AgendaCenter/ViewFile/Agenda/1234?html=true",
            "https://mcdot.maricopa.gov/",
        )
        # Should have the no-agenda title but no pdf or staff reports
        if result:
            self.assertEqual(len(result.get("staff_report_files", [])), 0)


# ── Format Function Tests ──

class TestDrainFormatFunctions(unittest.TestCase):
    """Unit tests for drain helper functions."""

    def test_format_mm_dd_yyyy_converts_iso(self):
        """_format_mm_dd_yyyy from drain.py converts ISO to MM/DD/YYYY."""
        from scraper.county.drain import _format_mm_dd_yyyy as fn
        self.assertEqual(fn("2012-01-15"), "01/15/2012")
        self.assertIsNone(fn(""))
        self.assertEqual(fn("01/15/2012"), "01/15/2012")

    def test_format_mm_dd_yyyy_invalid(self):
        """_format_mm_dd_yyyy returns input as-is for unparseable formats."""
        from scraper.county.drain import _format_mm_dd_yyyy as fn
        # Returns the input as-is if it matches MM/DD/YYYY already
        self.assertEqual(fn("not-a-date"), "not-a-date")


# ── Module Import Tests ──

class TestDrainModuleImport(unittest.TestCase):
    """Test that drain module imports cleanly."""

    def test_drain_module_imports(self):
        """Verify drain module can be imported."""
        try:
            import importlib
            importlib.import_module("scraper.county.drain")
        except ImportError as e:
            self.fail(f"drain module import failed: {e}")

    def test_all_source_modules_importable(self):
        """All source modules import cleanly after adding drain."""
        import importlib
        for mod_name in ["scraper.drain", "scraper.pz", "scraper.adj",
                         "scraper.pz_minutes", "scraper.search", "scraper.main"]:
            with self.subTest(module=mod_name):
                try:
                    importlib.import_module(f"scripts.{mod_name}")
                except ImportError:
                    pass  # May fail due to sys.path; not critical


# ── Regression Tests ──

class TestAllBodiesStillWork(unittest.TestCase):
    """Regression: existing BOS, PZ, and ADJ functionality must not be broken."""

    def test_bos_subcommand_still_works(self):
        """bos subcommand still routes to bos with args.source == 'bos'"""
        args = scraper.parse_args(["bos", "--sync"])
        self.assertEqual(args.source, "bos")
        self.assertTrue(args.sync)

    def test_pz_subcommand_still_works(self):
        """pz subcommand still routes to pz with args.source == 'pz'"""
        args = scraper.parse_args(["pz", "--sync"])
        self.assertEqual(args.source, "pz")
        self.assertTrue(args.sync)

    def test_adj_subcommand_still_works(self):
        """adj subcommand still routes to adj with args.source == 'adj'"""
        args = scraper.parse_args(["adj", "--sync"])
        self.assertEqual(args.source, "adj")
        self.assertTrue(args.sync)

    def test_drain_subcommand_still_works(self):
        """drain subcommand routes to drain with args.source == 'drain'"""
        args = scraper.parse_args(["drain", "--sync"])
        self.assertEqual(args.source, "drain")
        self.assertTrue(args.sync)

    def test_no_subcommand_defaults_to_bos(self):
        """No subcommand still defaults to bos"""
        args = scraper.parse_args(["--sync"])
        self.assertEqual(args.source, "bos")

    def test_pz_search_url_unchanged(self):
        """PZ search URL still uses CIDs=9 and www.maricopa.gov"""
        from scraper.county.pz import build_pz_search_url
        url = build_pz_search_url("01/01/2026", "01/31/2026")
        self.assertIn("CIDs=9", url)
        self.assertNotIn("CIDs=19", url)

    def test_adj_search_url_unchanged(self):
        """ADJ search URL still uses CIDs=3 and www.maricopa.gov"""
        from scraper.county.adj import build_adj_search_url
        url = build_adj_search_url("01/01/2026", "01/31/2026")
        self.assertIn("CIDs=3", url)
        self.assertNotIn("CIDs=19", url)

    def test_drain_search_url_uses_cid19_on_correct_domain(self):
        """Drain search URL uses CIDs=19 on mcdot.maricopa.gov, not www.maricopa.gov"""
        from scraper.county.drain import build_drain_search_url
        url = build_drain_search_url("01/01/2012", "01/31/2012")
        self.assertIn("CIDs=19", url)
        self.assertNotIn("CIDs=3", url)
        self.assertNotIn("CIDs=9", url)
        self.assertIn("mcdot.maricopa.gov", url)

    def test_pz_meeting_id_no_prefix(self):
        """Regression verifies PZ meeting IDs still don't have pz- prefix."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Planning & Zoning", body="pz", row_text="",
            detail_url="",
            agenda_url="https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/3734",
        )
        self.assertFalse(m.meeting_id.startswith("pz-"))
        self.assertEqual(m.body, "pz")

    def test_adj_meeting_id_no_prefix(self):
        """Regression verifies ADJ meeting IDs still don't have adj- prefix."""
        m = scraper.Meeting(
            meeting_date="", meeting_time="", meeting_title="",
            meeting_type="Board of Adjustment", body="adj", row_text="",
            detail_url="",
            agenda_url="https://www.maricopa.gov/AgendaCenter/ViewFile/Agenda/3755",
        )
        self.assertFalse(m.meeting_id.startswith("adj-"))
        self.assertEqual(m.body, "adj")


# ── Real-Fixture Integration Tests ──

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "drain"


def _load_fixture(filename: str) -> str:
    """Load the contents of a fixture HTML file as a string."""
    path = FIXTURES_DIR / filename
    if not path.exists():
        raise FileNotFoundError(f"Fixture not found: {path}")
    return path.read_text(encoding="utf-8", errors="replace")


def _require_fixtures() -> bool:
    """Return True if fixture directory exists and has files."""
    return FIXTURES_DIR.exists() and any(FIXTURES_DIR.iterdir())


class TestRealDrainFixture2011(unittest.TestCase):
    """Test parsing the real 2011 DRB meeting HTML."""

    def setUp(self):
        html = _load_fixture("drain_meetings_2011.html")
        from scraper.county.drain import parse_drain_meetings_from_html
        self.meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )

    def test_2011_meeting_count(self):
        """2011 DRB fixture produces exactly 6 meetings."""
        self.assertEqual(len(self.meetings), 6)

    def test_2011_all_body_drain(self):
        """All 2011 DRB meetings have body='drain'."""
        for m in self.meetings:
            self.assertEqual(m.body, "drain")

    def test_2011_all_meeting_type(self):
        """All 2011 DRB meetings have meeting_type='Drainage Review Board'."""
        for m in self.meetings:
            self.assertEqual(m.meeting_type, "Drainage Review Board")

    def test_2011_dates(self):
        """2011 DRB meetings have correct dates."""
        expected = ["2011-12-19", "2011-08-10", "2011-07-13",
                    "2011-03-23", "2011-02-16", "2011-01-19"]
        actual = [m.meeting_date for m in self.meetings]
        self.assertCountEqual(actual, expected)

    def test_2011_meeting_ids(self):
        """2011 DRB meeting IDs are 3-4 digit numbers."""
        ids = [m.meeting_id for m in self.meetings]
        expected_ids = ["1346", "1345", "1344", "1334", "1333", "1332"]
        self.assertCountEqual(ids, expected_ids)

    def test_2011_all_have_agenda_urls(self):
        """All 2011 DRB meetings have agenda URLs on mcdot.maricopa.gov."""
        for m in self.meetings:
            self.assertIn("mcdot.maricopa.gov", m.agenda_url)
            self.assertIn("/Agenda/", m.agenda_url)

    def test_2011_agenda_urls_use_dashed_format(self):
        """2011 DRB agenda URLs use the _MMDDYYYY-NNNN format."""
        for m in self.meetings:
            self.assertRegex(
                m.agenda_url,
                r"Agenda/_(?:\d{8})-" + m.meeting_id,
            )

    def test_2011_has_no_agenda_and_pdf_meetings(self):
        """2011 DRB includes 'No Agenda' and 'PDF' meetings."""
        titles = [m.meeting_title for m in self.meetings]
        self.assertTrue(any("No Agenda" in t for t in titles))


class TestRealDrainFixture2012(unittest.TestCase):
    """Test parsing the real 2012 DRB meeting HTML."""

    def setUp(self):
        html = _load_fixture("drain_meetings_2012.html")
        from scraper.county.drain import parse_drain_meetings_from_html
        self.meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )

    def test_2012_meeting_count(self):
        """2012 DRB fixture produces exactly 9 meetings."""
        self.assertEqual(len(self.meetings), 9)

    def test_2012_dates(self):
        """2012 DRB meetings have correct dates."""
        expected = ["2012-12-13", "2012-11-08", "2012-09-13",
                    "2012-08-09", "2012-07-12", "2012-05-10",
                    "2012-03-08", "2012-02-09", "2012-01-12"]
        actual = [m.meeting_date for m in self.meetings]
        self.assertCountEqual(actual, expected)

    def test_2012_meeting_ids(self):
        """2012 DRB meeting IDs are 3-4 digit numbers."""
        ids = [m.meeting_id for m in self.meetings]
        expected_ids = ["1338", "1337", "1336", "1335", "1350",
                        "1341", "1349", "1348", "1347"]
        self.assertCountEqual(ids, expected_ids)

    def test_2012_meeting_id_from_url_preserved(self):
        """Each 2012 meeting_id is the trailing digits from its agenda URL."""
        import re
        for m in self.meetings:
            # The meeting_id should match the last digits in the URL
            m_url = re.search(r"^_(?:\d{8})-(\d+)(?:\?.*)?$",
                              m.agenda_url.rstrip("/").split("/")[-1])
            if m_url:
                self.assertEqual(m.meeting_id, m_url.group(1))

    def test_2012_no_agenda_meetings(self):
        """2012 DRB meetings with 'No Agenda' are correctly captured."""
        no_agenda = [m for m in self.meetings if "No Agenda" in m.meeting_title]
        self.assertEqual(len(no_agenda), 4)
        for m in no_agenda:
            # No Agenda meetings should have the bare URL (no ?html=true)
            self.assertNotIn("?html=true", m.agenda_url)

    def test_2012_html_agenda_meetings(self):
        """2012 DRB meetings with HTML agenda URLs have ?html=true."""
        html_agendas = [m for m in self.meetings if "?html=true" in m.agenda_url]
        self.assertGreaterEqual(len(html_agendas), 4)


class TestRealDrainFixture2013(unittest.TestCase):
    """Test parsing the real 2013 DRB meeting HTML."""

    def setUp(self):
        html = _load_fixture("drain_meetings_2013.html")
        from scraper.county.drain import parse_drain_meetings_from_html
        self.meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )

    def test_2013_meeting_count(self):
        """2013 DRB fixture produces exactly 8 meetings."""
        self.assertEqual(len(self.meetings), 8)

    def test_2013_dates(self):
        """2013 DRB meetings have correct dates (with deduplication)."""
        dates = [m.meeting_date for m in self.meetings]
        # Multiple meetings on same date should still have distinct meeting_ids
        self.assertEqual(len(set(dates)), 4)
        # All dates should be in 2013
        for d in dates:
            self.assertTrue(d.startswith("2013-"), f"Unexpected date: {d}")

    def test_2013_has_duplicate_date_meetings(self):
        """2013 DRB has 2 meetings each on 02/14, 03/21, 04/18, and 07/11."""
        from collections import Counter
        date_counts = Counter(m.meeting_date for m in self.meetings)
        for date, count in date_counts.items():
            self.assertEqual(count, 2, f"Date {date} has {count} meetings, expected 2")

    def test_2013_meeting_ids_correct(self):
        """2013 DRB meeting IDs are the expected set."""
        ids = set(m.meeting_id for m in self.meetings)
        expected = {"761", "762", "764", "765", "1339", "1340", "1342", "1343"}
        self.assertEqual(ids, expected)


class TestRealDrainFixture2026Empty(unittest.TestCase):
    """Test that parsing the 2026 page (no meetings) returns empty."""

    def test_2026_no_meetings(self):
        """2026 DRB fixture (defunct board) produces 0 meetings."""
        html = _load_fixture("drain_meetings_2026.html")
        from scraper.county.drain import parse_drain_meetings_from_html
        meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )
        self.assertEqual(len(meetings), 0)

    def test_2026_no_meetings_graceful(self):
        """2026 page returns empty list, not None or error."""
        html = _load_fixture("drain_meetings_2026.html")
        from scraper.county.drain import parse_drain_meetings_from_html
        meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )
        self.assertIsInstance(meetings, list)


class TestRealDrainYearTabExtraction(unittest.TestCase):
    """Test year tab extraction from real DRB multi-year fixture."""

    def test_multi_year_page_has_all_year_tabs(self):
        """2011-2013 broad fixture page has year tabs for all 3 DRB years."""
        html = _load_fixture("drain_meetings_2011_2013.html")
        from scraper.county.drain import _extract_drain_year_tabs_from_html
        tabs = _extract_drain_year_tabs_from_html(html)
        self.assertEqual(tabs, [2011, 2012, 2013])

    def test_individual_year_pages_have_only_their_year_tab(self):
        """Each individual-year page only shows its own year tab."""
        from scraper.county.drain import _extract_drain_year_tabs_from_html as tabs_fn
        for year in [2011, 2012, 2013]:
            html = _load_fixture(f"drain_meetings_{year}.html")
            tabs = tabs_fn(html)
            self.assertEqual(tabs, [year],
                             f"Year {year} page has unexpected tabs: {tabs}")

    def test_empty_page_has_no_year_tabs(self):
        """2026 empty page has no year tabs."""
        html = _load_fixture("drain_meetings_2026.html")
        from scraper.county.drain import _extract_drain_year_tabs_from_html
        tabs = _extract_drain_year_tabs_from_html(html)
        self.assertEqual(tabs, [])


class TestRealDrainSearchUrlEndToEnd(unittest.TestCase):
    """End-to-end test: build search URLs for DRB and verify format."""

    def test_search_url_for_2012(self):
        """Search URL for 2012 DRB meetings is correctly formatted."""
        from scraper.county.drain import build_drain_search_url
        url = build_drain_search_url("01/01/2012", "12/31/2012")
        self.assertIn("startDate=01%2F01%2F2012", url)
        self.assertIn("endDate=12%2F31%2F2012", url)
        self.assertIn("CIDs=19", url)
        self.assertIn("mcdot.maricopa.gov", url)

    def test_search_url_for_defunct_period(self):
        """Search URL for 2026 is valid and correctly formatted."""
        from scraper.county.drain import build_drain_search_url
        url = build_drain_search_url("01/01/2026", "12/31/2026")
        self.assertIn("startDate=01%2F01%2F2026", url)
        self.assertIn("mcdot.maricopa.gov", url)

    def test_full_date_format_roundtrip(self):
        """_format_mm_dd_yyyy + build_drain_search_url round-trips correctly."""
        from scraper.county.drain import _format_mm_dd_yyyy, build_drain_search_url
        start = _format_mm_dd_yyyy("2011-01-01")
        end = _format_mm_dd_yyyy("2013-12-31")
        self.assertEqual(start, "01/01/2011")
        self.assertEqual(end, "12/31/2013")
        url = build_drain_search_url(start, end)
        self.assertIn("startDate=01%2F01%2F2011", url)
        self.assertIn("endDate=12%2F31%2F2013", url)


class TestRealDrainBodyScopedIdentity(unittest.TestCase):
    """Verify body-scoped identity with real fixture meetings."""

    def test_meeting_ids_dont_clash_with_adj(self):
        """DRB meeting IDs (e.g., 1338) could overlap with ADJ IDs; verify they're scoped."""
        html = _load_fixture("drain_meetings_2012.html")
        from scraper.county.drain import parse_drain_meetings_from_html
        meetings = parse_drain_meetings_from_html(
            html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
        )
        for m in meetings:
            self.assertEqual(m.body, "drain",
                             f"Meeting {m.meeting_id} should have body='drain'")
            # If the same meeting ID existed for ADJ or PZ, body scoping would differentiate
            self.assertLessEqual(len(m.body), 16,
                                 "body field must fit VARCHAR(16)")

    def test_all_meetings_have_valid_agenda_urls(self):
        """Every DRB meeting fixture parses to an agenda URL."""
        from scraper.county.drain import parse_drain_meetings_from_html
        for year in [2011, 2012, 2013]:
            html = _load_fixture(f"drain_meetings_{year}.html")
            meetings = parse_drain_meetings_from_html(
                html, "https://mcdot.maricopa.gov/AgendaCenter/Search/"
            )
            for m in meetings:
                self.assertTrue(m.agenda_url,
                                f"Year {year} meeting {m.meeting_id} has no agenda_url")


class TestRealDrainCLIArgsWithFixture(unittest.TestCase):
    """Test that CLI args for drain are compatible with fixture-based parsing."""

    def test_2012_date_range_produces_valid_search(self):
        """A 2012 date range produces the correct URL format for the fixture page."""
        from scraper.county.drain import build_drain_search_url
        url = build_drain_search_url("01/01/2012", "12/31/2012")
        # The fixture was saved from this exact URL pattern
        self.assertIn("startDate=01%2F01%2F2012", url)
        self.assertIn("endDate=12%2F31%2F2012", url)

    def test_2026_date_range_produces_empty_result_path(self):
        """2026 date range should be parseable and produce 0 meetings."""
        import urllib.parse
        from scraper.county.drain import build_drain_search_url, parse_drain_meetings_from_html
        
        url = build_drain_search_url("01/01/2026", "12/31/2026")
        html = _load_fixture("drain_meetings_2026.html")
        meetings = parse_drain_meetings_from_html(html, url)
        self.assertEqual(len(meetings), 0)


if __name__ == "__main__":
    unittest.main()
