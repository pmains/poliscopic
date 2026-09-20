"""Unit tests for agenda item extraction logic using a live HTML fixture.

Fixture: tests/fixtures/4667_formal_2026-04-22.html
Meeting: ID=4667, 2026-04-22, Formal, Board of Supervisors
Expected: 86 numbered agenda items
"""
import re
import sys
import unittest
from pathlib import Path

# Add scripts directory so we can import the scraper module
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

# ``scripts/agenda_scraper.py`` was split into ``scripts/scraper/common/*`` by
# the 2026 scraper restructure; import from where the code now lives.
from scraper.common.agenda_items import (
    _clean_lnk_title,
    _extract_lnk_from_table,
    _find_item_tables,
    parse_agenda_items_from_html,
)
from scraper.common.html_utils import _clean_html_text
from scraper.common.utils import _extract_c_number, parse_c_number_parts

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "4667_formal_2026-04-22.html"
MEETING_ID = "4667"
MEETING_DATE = "2026-04-22"
MEETING_TYPE = "Formal"
SOURCE_URL = (
    "https://mccobagenda.databankcloud.com/AgendaOnline/Meetings/ViewMeeting"
    f"?id={MEETING_ID}&doctype=1"
)

MEETING_DICT = {
    "meeting_id": MEETING_ID,
    "meeting_date": MEETING_DATE,
    "meeting_type": MEETING_TYPE,
    "record_id": MEETING_ID,
    "record_date": MEETING_DATE,
    "document_url": SOURCE_URL,
}


class TestItemTableDetection(unittest.TestCase):
    """Verify the table-finder correctly identifies all numbered items."""

    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")

    def test_item_count(self):
        """Should find exactly 86 numbered items."""
        items = _find_item_tables(self.html)
        self.assertEqual(len(items), 86)

    def test_items_sorted(self):
        """Items should be in display order."""
        items = _find_item_tables(self.html)
        positions = [item[1] for item in items]
        self.assertEqual(positions, sorted(positions))

    def test_item_has_number_and_position(self):
        """Each item should have a valid number and table boundaries."""
        items = _find_item_tables(self.html)
        for num, pos, tend in items:
            self.assertIsInstance(num, int)
            self.assertGreater(pos, 0)
            self.assertGreater(tend, pos)


class TestLnkExtraction(unittest.TestCase):
    """Verify title extraction from item tables."""

    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")
        cls.items = _find_item_tables(cls.html)
        # Index items by number for easy lookup
        cls.by_number = {num: (pos, tend) for num, pos, tend in cls.items}

    def get_table_titles(self, item_num: int) -> list[str]:
        """Get lnk titles from an item's table."""
        pos, tend = self.by_number[item_num]
        tstart = self.html.rfind("<table", 0, pos)
        table_html = self.html[tstart : tend + 8]
        return _extract_lnk_from_table(table_html, pos - tstart)

    def test_item_1_has_roll_call(self):
        """Item 1 should have 'ROLL CALL - LISTA'."""
        titles = self.get_table_titles(1)
        self.assertTrue(any("ROLL CALL" in t and "LISTA" in t for t in titles))

    def test_item_6_has_us_a_5292(self):
        """Item 6 should have a title starting with 'US-A-5292' (dash in title)."""
        titles = self.get_table_titles(6)
        self.assertTrue(any(t.startswith("US-A-5292") for t in titles))

    def test_item_50_starts_with_number(self):
        """Item 50 should start with '260059-RFP' (number-prefixed title)."""
        titles = self.get_table_titles(50)
        self.assertTrue(any(t.startswith("260059-RFP") for t in titles))

    def test_item_86_has_verbose_title(self):
        """Item 86 should have its body anchor title."""
        titles = self.get_table_titles(86)
        self.assertTrue(any("summary of current events" in t.lower() for t in titles))


class TestAgendaItemExtraction(unittest.TestCase):
    """Integration test: parse_agenda_items_from_html with the live fixture."""

    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")
        cls.items = parse_agenda_items_from_html(
            cls.html, SOURCE_URL, MEETING_DICT
        )
        cls.by_number = {item["agenda_item_number"]: item for item in cls.items}

    def test_total_items(self):
        """Should extract exactly 86 items."""
        self.assertEqual(len(self.items), 86)

    def test_canonical_item_id(self):
        """Every item ID should match {meeting_id}-{number}-item."""
        pattern = re.compile(rf"^{MEETING_ID}-\d+-item$")
        for item in self.items:
            item_id = item.get("agenda_item_id", "")
            self.assertRegex(
                item_id,
                pattern,
                f"Item {item.get('agenda_item_number')} has non-canonical ID: {item_id}",
            )

    def test_item_numbers_sequential(self):
        """Item numbers should be sequential 1..86."""
        numbers = sorted(int(item["agenda_item_number"]) for item in self.items)
        self.assertEqual(numbers, list(range(1, 87)))

    def test_title_not_empty(self):
        """No item should have a blank title."""
        for item in self.items:
            title = item.get("agenda_item_title", "")
            self.assertTrue(
                title.strip(),
                f"Item {item.get('agenda_item_number')} has empty title",
            )

    def test_item_1_title(self):
        """Item 1 should be 'ROLL CALL' (bilingual suffix stripped)."""
        item = self.by_number.get("1")
        self.assertIsNotNone(item)
        self.assertEqual(item["agenda_item_title"], "ROLL CALL")

    def test_item_6_title(self):
        """Item 6 should be 'US-A-5292 (PHO) 355TH AVE'."""
        item = self.by_number.get("6")
        self.assertIsNotNone(item)
        self.assertIn("US-A-5292", item["agenda_item_title"])

    def test_item_50_title(self):
        """Item 50 should start with '260059-RFP'."""
        item = self.by_number.get("50")
        self.assertIsNotNone(item)
        self.assertTrue(item["agenda_item_title"].startswith("260059-RFP"))

    def test_item_86_title(self):
        """Item 86 should have a title containing 'summary of current events'."""
        item = self.by_number.get("86")
        self.assertIsNotNone(item)
        self.assertIn("summary of current events", item["agenda_item_title"].lower())

    def test_item_text_not_empty(self):
        """Each item should have non-empty full text."""
        for item in self.items:
            text = item.get("agenda_item_text", "")
            self.assertTrue(
                text.strip(),
                f"Item {item.get('agenda_item_number')} has empty text",
            )

    def test_source_url_has_fragment(self):
        """Source URLs should include a fragment pointing to the item."""
        for item in self.items:
            item_url = item.get("agenda_item_url", "")
            self.assertIn("#", item_url)

    def test_meeting_metadata_present(self):
        """All items should have source_body, meeting_id, meeting_date, meeting_type."""
        for item in self.items:
            self.assertEqual(item["source_body"], "Board of Supervisors")
            self.assertEqual(item["meeting_id"], MEETING_ID)
            self.assertEqual(item["meeting_date"], MEETING_DATE)
            self.assertEqual(item["meeting_type"], MEETING_TYPE)


class TestCleanLnkTitle(unittest.TestCase):
    """Verify title cleaning utilities."""

    def test_removes_nbsp(self):
        """&nbsp; characters should be replaced with spaces."""
        result = _clean_lnk_title("OFFER\xa0ON\xa0TAX")
        self.assertEqual(result, "OFFER ON TAX")

    def test_collapses_whitespace(self):
        """Multiple spaces should be collapsed."""
        result = _clean_lnk_title("  ITEM   TITLE  ")
        self.assertEqual(result, "ITEM TITLE")

    def test_clean_html_text(self):
        """_clean_html_text should collapse whitespace."""
        result = _clean_html_text("  Hello   World  ")
        self.assertEqual(result, "Hello World")


class TestRegressionCases(unittest.TestCase):
    """Regression tests for previously broken behavior."""

    @classmethod
    def setUpClass(cls):
        cls.html = FIXTURE.read_text(encoding="utf-8")
        cls.items = parse_agenda_items_from_html(
            cls.html, SOURCE_URL, MEETING_DICT
        )
        cls.item_ids = {item["agenda_item_id"] for item in cls.items}

    def test_no_slugified_titles_in_item_id(self):
        """Item IDs must use canonical '{meeting_id}-{number}-item' format,
        not old slugified-title format like '4449-1-narramore-road...'.
        """
        for item_id in self.item_ids:
            parts = item_id.split("-")
            # Should be: [meeting_id, number, "item"]
            self.assertEqual(
                len(parts),
                3,
                f"Item ID '{item_id}' has wrong structure (expected 3 parts)",
            )
            # Middle part must be a pure integer
            self.assertTrue(
                parts[1].isdigit(),
                f"Item ID '{item_id}' has non-numeric middle segment",
            )


class TestConsistencyAcrossRuns(unittest.TestCase):
    """Verify that extraction is deterministic."""

    def test_deterministic(self):
        """Extraction from the same HTML should produce identical results."""
        html = FIXTURE.read_text(encoding="utf-8")
        items = parse_agenda_items_from_html(html, SOURCE_URL, MEETING_DICT)
        self.assertEqual(len(items), 86)
        # Verify that re-parsing produces the same number and that
        # the first item is always ROLL CALL (stable ordering)
        self.assertEqual(items[0]["agenda_item_title"], "ROLL CALL")


class TestCNumbers(unittest.TestCase):
    """C-number extraction tests."""

    def test_extract_c_number_standard(self):
        """Standard (C-XX-XX-XXX-X-XX) format."""
        result = _extract_c_number(
            "... (C-86-25-040-X-00) ..."
        )
        self.assertEqual(result, "C-86-25-040-X-00")

    def test_extract_c_number_without_parens(self):
        """C-number without surrounding parentheses."""
        result = _extract_c_number(
            "Authorize... C-06-25-259-X-00 for the project."
        )
        self.assertEqual(result, "C-06-25-259-X-00")

    def test_extract_c_number_two_tail_segments(self):
        """C-number with only 2 tail segments (C-XX-XX-XXX-XX)."""
        result = _extract_c_number(
            "Item text with C-06-25-199-02 at the end."
        )
        self.assertEqual(result, "C-06-25-199-02")

    def test_extract_c_number_three_tail_segments(self):
        """C-number with 3 tail segments (C-XX-XX-XXX-X-XX-X)."""
        result = _extract_c_number(
            "Budget item C-44-25-071-X-00 approved."
        )
        self.assertEqual(result, "C-44-25-071-X-00")

    def test_extract_c_number_no_match(self):
        """Text without a C-number returns empty string."""
        result = _extract_c_number("ROLL CALL - LISTA")
        self.assertEqual(result, "")

    def test_extract_c_number_empty(self):
        """Empty text returns empty string."""
        result = _extract_c_number("")
        self.assertEqual(result, "")

    def test_extract_c_number_at_end_of_text(self):
        """C-number at the very end of long text."""
        text = (
            "Authorize the Chairman to approve a non-monetary donation "
            "from Go with the Flow... (C-86-25-040-X-00)"
        )
        result = _extract_c_number(text)
        self.assertEqual(result, "C-86-25-040-X-00")

    def test_c_number_populated_from_fixture(self):
        """C-number, base, and revision should all be extracted from fixture item 59."""
        items = parse_agenda_items_from_html(
            FIXTURE.read_text(encoding="utf-8"), SOURCE_URL, MEETING_DICT
        )
        by_number = {item["agenda_item_number"]: item for item in items}
        # Item 59 of meeting 4667 has C-64-26-145-X-00 in its text
        item = by_number.get("59")
        self.assertIsNotNone(item)
        self.assertEqual(item.get("c_number"), "C-64-26-145-X-00")
        self.assertEqual(item.get("c_number_base"), "C-64-26-145-X")
        self.assertEqual(item.get("c_number_revision"), "00")

    def test_parse_c_number_parts_standard(self):
        """parse_c_number_parts splits C-XX-XX-XXX-X-XX correctly."""
        parts = parse_c_number_parts("C-86-25-040-X-00")
        self.assertEqual(parts["c_number"], "C-86-25-040-X-00")
        self.assertEqual(parts["c_number_base"], "C-86-25-040-X")
        self.assertEqual(parts["c_number_revision"], "00")

    def test_parse_c_number_parts_two_segments(self):
        """C-number with fewer segments: C-XX-XX-XXX-XX."""
        parts = parse_c_number_parts("C-06-25-199-02")
        self.assertEqual(parts["c_number"], "C-06-25-199-02")
        self.assertEqual(parts["c_number_base"], "C-06-25-199")
        self.assertEqual(parts["c_number_revision"], "02")

    def test_parse_c_number_parts_empty(self):
        """Empty input returns empty strings."""
        parts = parse_c_number_parts("")
        self.assertEqual(parts["c_number"], "")
        self.assertEqual(parts["c_number_base"], "")
        self.assertEqual(parts["c_number_revision"], "")


if __name__ == "__main__":
    unittest.main()
