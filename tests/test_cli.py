"""Tests for CLI argument parsing in agenda_scraper.py.

Tests cover source subcommands (bos, pz), backward compatibility,
and date format handling.
"""

import sys
import unittest
from pathlib import Path


def _load_scraper():
    scripts_dir = str(Path(__file__).resolve().parents[1] / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    import scraper as scraper_package
    return scraper_package


scraper = _load_scraper()


class TestParseArgsSubcommands(unittest.TestCase):
    """Test that source subcommands route correctly."""

    def test_bos_subcommand(self):
        """bos --sync --start-date=2026-01-01 routes to bos with args.source == 'bos'"""
        args = scraper.parse_args(["bos", "--sync", "--start-date=2026-01-01"])
        self.assertEqual(args.source, "bos")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2026-01-01")

    def test_pz_subcommand(self):
        """pz --sync --start-date=2026-01-01 routes to pz with args.source == 'pz'"""
        args = scraper.parse_args(["pz", "--sync", "--start-date=2026-01-01"])
        self.assertEqual(args.source, "pz")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2026-01-01")

    def test_no_subcommand_defaults_to_bos(self):
        """No subcommand defaults to bos with args.source == 'bos'"""
        args = scraper.parse_args(["--sync", "--start-date=2026-01-01"])
        self.assertEqual(args.source, "bos")
        self.assertTrue(args.sync)
        self.assertEqual(args.start_date, "2026-01-01")

    def test_bos_subcommand_no_args(self):
        """bos with no arguments returns source='bos'"""
        args = scraper.parse_args(["bos"])
        self.assertEqual(args.source, "bos")

    def test_pz_subcommand_no_args(self):
        """pz with no arguments returns source='pz'"""
        args = scraper.parse_args(["pz"])
        self.assertEqual(args.source, "pz")


class TestParseArgsDateFormats(unittest.TestCase):
    """Test that date flags accept YYYY-MM-DD format."""

    def test_start_date_iso_format(self):
        """--start-date accepts YYYY-MM-DD"""
        args = scraper.parse_args(["--start-date=2026-01-01"])
        self.assertEqual(args.start_date, "2026-01-01")

    def test_end_date_iso_format(self):
        """--end-date accepts YYYY-MM-DD"""
        args = scraper.parse_args(["--end-date=2026-05-01"])
        self.assertEqual(args.end_date, "2026-05-01")

    def test_date_shorthand(self):
        """--date normalizes into --start-date and --end-date"""
        args = scraper.parse_args(["--date=2026-03-15"])
        self.assertEqual(args.start_date, "2026-03-15")
        self.assertEqual(args.end_date, "2026-03-15")

    def test_bos_date_iso_format(self):
        """bos --start-date accepts YYYY-MM-DD"""
        args = scraper.parse_args(["bos", "--start-date=2026-01-01"])
        self.assertEqual(args.start_date, "2026-01-01")

    def test_pz_date_iso_format(self):
        """pz --start-date accepts YYYY-MM-DD"""
        args = scraper.parse_args(["pz", "--start-date=2026-01-01"])
        self.assertEqual(args.start_date, "2026-01-01")


class TestParseArgsBosFlags(unittest.TestCase):
    """Test that BOS flags are accessible."""

    def test_bos_sync_flag(self):
        """bos --sync is accessible"""
        args = scraper.parse_args(["bos", "--sync"])
        self.assertTrue(args.sync)

    def test_bos_init_db(self):
        """bos --init-db is accessible"""
        args = scraper.parse_args(["bos", "--init-db"])
        self.assertTrue(args.init_db)

    def test_bos_extract_agenda_items(self):
        """bos --extract-agenda-items is accessible"""
        args = scraper.parse_args(["bos", "--extract-agenda-items"])
        self.assertTrue(args.extract_agenda_items)

    def test_bos_download(self):
        """bos --download is accessible"""
        args = scraper.parse_args(["bos", "--download"])
        self.assertTrue(args.download)

    def test_bos_headed(self):
        """bos --headed is accessible"""
        args = scraper.parse_args(["bos", "--headed"])
        self.assertTrue(args.headed)

    def test_bos_limit(self):
        """bos --limit is accessible"""
        args = scraper.parse_args(["bos", "--limit=5"])
        self.assertEqual(args.limit, 5)

    def test_bos_meeting_id(self):
        """bos --meeting-id is accessible"""
        args = scraper.parse_args(["bos", "--meeting-id=4449"])
        self.assertEqual(args.meeting_id, "4449")

    def test_bos_retry_count(self):
        """bos --retry-count has default 3"""
        args = scraper.parse_args(["bos"])
        self.assertEqual(args.retry_count, 3)

    def test_bos_force(self):
        """bos --force is accessible"""
        args = scraper.parse_args(["bos", "--force"])
        self.assertTrue(args.force)

    def test_bos_retry_failed(self):
        """bos --retry-failed is accessible"""
        args = scraper.parse_args(["bos", "--retry-failed"])
        self.assertTrue(args.retry_failed)

    def test_bos_status(self):
        """bos --status is accessible"""
        args = scraper.parse_args(["bos", "--status"])
        self.assertTrue(args.status)

    def test_bos_failed(self):
        """bos --failed is accessible"""
        args = scraper.parse_args(["bos", "--failed"])
        self.assertTrue(args.failed)


class TestParseArgsPzFlags(unittest.TestCase):
    """Test that PZ flags are accessible under pz subcommand."""

    def test_pz_sync_flag(self):
        """pz --sync is accessible"""
        args = scraper.parse_args(["pz", "--sync"])
        self.assertTrue(args.sync)

    def test_pz_headed(self):
        """pz --headed is accessible"""
        args = scraper.parse_args(["pz", "--headed"])
        self.assertTrue(args.headed)

    def test_pz_limit(self):
        """pz --limit is accessible"""
        args = scraper.parse_args(["pz", "--limit=5"])
        self.assertEqual(args.limit, 5)

    def test_pz_meeting_id(self):
        """pz --meeting-id is accessible"""
        args = scraper.parse_args(["pz", "--meeting-id=123"])
        self.assertEqual(args.meeting_id, "123")

    def test_pz_retry_count(self):
        """pz --retry-count has default 3"""
        args = scraper.parse_args(["pz"])
        self.assertEqual(args.retry_count, 3)

    def test_pz_force(self):
        """pz --force is accessible"""
        args = scraper.parse_args(["pz", "--force"])
        self.assertTrue(args.force)

    def test_pz_init_db(self):
        """pz --init-db is accessible"""
        args = scraper.parse_args(["pz", "--init-db"])
        self.assertTrue(args.init_db)

    def test_pz_status(self):
        """pz --status is accessible"""
        args = scraper.parse_args(["pz", "--status"])
        self.assertTrue(args.status)

    def test_pz_failed(self):
        """pz --failed is accessible"""
        args = scraper.parse_args(["pz", "--failed"])
        self.assertTrue(args.failed)

    def test_pz_from_file(self):
        """pz --from-file is accessible"""
        args = scraper.parse_args(["pz", "--from-file=test.html"])
        self.assertEqual(args.from_file, "test.html")


class TestParseArgsDeprecatedSyncPz(unittest.TestCase):
    """Test that --sync-pz and related deprecated flags still parse."""

    def test_sync_pz_flag_accepted(self):
        """--sync-pz still accepted by bos parser"""
        args = scraper.parse_args(["--sync-pz"])
        # The bos parser accepts --sync-pz but it's hidden
        self.assertTrue(args.sync_pz)
        self.assertEqual(args.source, "bos")

    def test_sync_pz_with_pz_start_date(self):
        """--sync-pz with --pz-start-date still accepted"""
        args = scraper.parse_args(["--sync-pz", "--pz-start-date=01/01/2026"])
        self.assertTrue(args.sync_pz)
        self.assertEqual(args.pz_start_date, "01/01/2026")

    def test_sync_pz_with_pz_end_date(self):
        """--sync-pz with --pz-end-date still accepted"""
        args = scraper.parse_args(["--sync-pz", "--pz-end-date=05/01/2026"])
        self.assertTrue(args.sync_pz)
        self.assertEqual(args.pz_end_date, "05/01/2026")

    def test_sync_pz_with_pz_limit(self):
        """--sync-pz with --pz-limit still accepted"""
        args = scraper.parse_args(["--sync-pz", "--pz-limit=10"])
        self.assertTrue(args.sync_pz)
        self.assertEqual(args.pz_limit, 10)


class TestParseArgsDateShorthand(unittest.TestCase):
    """Test --date shorthand normalization."""

    def test_date_cannot_combine_with_start_date(self):
        """--date combined with --start-date should raise"""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["--date=2026-01-01", "--start-date=2026-02-01"])

    def test_date_cannot_combine_with_end_date(self):
        """--date combined with --end-date should raise"""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["--date=2026-01-01", "--end-date=2026-02-01"])

    def test_bos_date_cannot_combine_with_start_date(self):
        """bos --date combined with --start-date should raise"""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["bos", "--date=2026-01-01", "--start-date=2026-02-01"])

    def test_pz_date_cannot_combine_with_start_date(self):
        """pz --date combined with --start-date should raise"""
        with self.assertRaises(SystemExit):
            scraper.parse_args(["pz", "--date=2026-01-01", "--start-date=2026-02-01"])


class TestFormatMmDdYyyy(unittest.TestCase):
    """Test the _format_mm_dd_yyyy helper function."""

    def test_converts_iso_to_mm_dd_yyyy(self):
        result = scraper._format_mm_dd_yyyy("2026-01-15")
        self.assertEqual(result, "01/15/2026")

    def test_returns_none_for_empty(self):
        result = scraper._format_mm_dd_yyyy("")
        self.assertIsNone(result)

    def test_passes_through_mm_dd_yyyy(self):
        result = scraper._format_mm_dd_yyyy("01/15/2026")
        self.assertEqual(result, "01/15/2026")

    def test_passes_through_unknown_format(self):
        result = scraper._format_mm_dd_yyyy("not-a-date")
        self.assertEqual(result, "not-a-date")
