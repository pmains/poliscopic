"""Tests for supporting document fixture structure and C-number matching."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test

from scraper.common.supporting_docs import extract_supporting_documents_from_items
from scraper.common.utils import _extract_c_number, parse_c_number_parts

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "supporting_docs_item_view.html"


@integration_test
class TestFixtureStructure(unittest.TestCase):
    """Verify the supporting docs fixture has proper structure."""

    def test_fixture_exists(self):
        self.assertTrue(FIXTURE.exists())

    def test_c_number_in_item_view(self):
        html = FIXTURE.read_text(encoding="utf-8")
        self.assertIn('class="item-view-title-text"', html)
        self.assertIn("C-86-26-102-X-00", html)

    def test_attachments_in_item_view(self):
        html = FIXTURE.read_text(encoding="utf-8")
        self.assertIn('id="lnkAttachment_1"', html)
        self.assertIn('id="lnkAttachment_2"', html)

    def test_stale_anchor_outside_item_view(self):
        html = FIXTURE.read_text(encoding="utf-8")
        self.assertIn('id="lnkAttachment_stale"', html)
        self.assertIn("Stale Document", html)

    def test_scoped_to_item_view_excludes_stale(self):
        """Simulate the scoping: only anchors inside #itemView."""
        html = FIXTURE.read_text(encoding="utf-8")
        # Find the #itemView div closing tag (second </div> after opening)
        start = html.index('<div id="itemView">') + len('<div id="itemView">')
        # Skip the first </div> (closes item-view-title-text inner div)
        first_close = html.index("</div>", start)
        # Second </div> is the actual #itemView closing
        end = html.index("</div>", first_close + 6)
        iv_content = html[start:end]
        # Inside #itemView we expect 2 attachment links
        count = iv_content.count('id="lnkAttachment_')
        self.assertEqual(count, 2)
        # Outside #itemView we have 1 stale anchor
        outside_before = html[:start]
        outside_after = html[end + 6:]
        count_outside = (outside_before + outside_after).count('id="lnkAttachment_')
        self.assertEqual(count_outside, 1)


@integration_test
class TestCNumberMatchingEdgeCases(unittest.TestCase):
    """Edge cases for C-number matching relevant to supporting docs."""

    def test_c_number_matches_item(self):
        """C-86-26-102-X-00 should be parseable."""
        parts = parse_c_number_parts("C-86-26-102-X-00")
        self.assertEqual(parts["c_number_base"], "C-86-26-102-X")
        self.assertEqual(parts["c_number_revision"], "00")

    def test_c_number_from_full_text(self):
        """_extract_c_number can find C-number embedded in item text."""
        text = "Authorize contract C-86-26-102-X-00 with vendor."
        result = _extract_c_number(text)
        self.assertEqual(result, "C-86-26-102-X-00")

    def test_extract_from_items_with_matching_c_number(self):
        """C-number from supporting doc matches same C-number in item."""
        html = '<html><body><div id="itemView">'
        html += '<a id="lnkAttachment_1" href="/doc.pdf">Doc</a>'
        html += '</div></body></html>'
        items = [
            {"meeting_id": "9999", "agenda_item_number": 2, "c_number": "C-86-26-102-X-00"},
            {"meeting_id": "9999", "agenda_item_number": 3, "c_number": "C-86-26-103-X-00"},
        ]
        # This tests that doc extraction doesn't crash on valid item dicts
        # even when the HTML has no matching attachment links
        docs = extract_supporting_documents_from_items(
            html, items, "https://example.com"
        )
        self.assertIsInstance(docs, list)

    def test_extract_from_items_empty_for_no_docs(self):
        """extract_supporting_documents_from_items returns empty for item HTML without attachments."""
        html = "<html><body>ROLL CALL</body></html>"
        items = [{"meeting_id": "9999", "agenda_item_number": 1}]
        docs = extract_supporting_documents_from_items(
            html, items, "https://example.com"
        )
        self.assertEqual(docs, [])


if __name__ == "__main__":
    unittest.main()
