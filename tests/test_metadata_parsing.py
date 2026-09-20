"""Tests for meeting metadata extraction from page text data."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from scraper.common.utils import parse_metadata_from_page_data


class TestMetadataParsing(unittest.TestCase):
    """Test parse_metadata_from_page_data with various meeting types."""

    def test_formal_board_header(self):
        """Formal meeting with bilingual board header."""
        data = {
            "bodyText": (
                "BOARD OF SUPERVISORS - JUNTA DE SUPERVISORES\n"
                "Formal Meeting\n"
                "1/29/2025 9:30 AM\n..."
            ),
            "headerText": "BOARD OF SUPERVISORS - JUNTA DE SUPERVISORES",
            "formTitle": "",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_date"], "2025-01-29")
        self.assertEqual(result["meeting_type"], "Formal Meeting")
        self.assertEqual(result["meeting_title"], "BOARD OF SUPERVISORS - JUNTA DE SUPERVISORES")

    def test_special_election_chairman(self):
        """Special/Election of Chairman with formTitle."""
        data = {
            "bodyText": "Special/Election of Chairman\n2/12/2025\n...",
            "headerText": "",
            "formTitle": "Special/Election of Chairman",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_date"], "2025-02-12")
        self.assertEqual(result["meeting_title"], "Special/Election of Chairman")

    def test_informal_meeting(self):
        """Informal meeting type detection."""
        data = {
            "bodyText": "INFORMAL MEETING\n3/20/2025 9:30 AM\nAgenda...",
            "headerText": "INFORMAL MEETING",
            "formTitle": "",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_type"], "Informal Meeting")
        self.assertEqual(result["meeting_date"], "2025-03-20")

    def test_numeric_title(self):
        """Numeric title (meeting ID) should be captured as-is."""
        data = {
            "bodyText": "4467\n4/22/2026\nFormal Meeting\n...",
            "headerText": "4467",
            "formTitle": "",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_title"], "4467")
        self.assertEqual(result["meeting_date"], "2026-04-22")

    def test_missing_body_text(self):
        """When bodyText is empty, all fields should be empty."""
        data = {"bodyText": "", "headerText": "", "formTitle": ""}
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_date"], "")
        self.assertEqual(result["meeting_type"], "")
        self.assertEqual(result["meeting_title"], "")

    def test_no_date_in_body(self):
        """When no date is found, meeting_date should be empty."""
        data = {
            "bodyText": "No date here, just text for a meeting.",
            "headerText": "Special Session",
            "formTitle": "",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_date"], "")

    def test_no_type_in_body(self):
        """When no type is found, meeting_type should be empty."""
        data = {
            "bodyText": "Just some text 1/15/2026 without a type indicator",
            "headerText": "",
            "formTitle": "Board Workshop",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_type"], "")

    def test_form_title_priority(self):
        """formTitle should take priority over headerText for meeting_title."""
        data = {
            "bodyText": "Board of Supervisors\n1/1/2026\n",
            "headerText": "Board Header",
            "formTitle": "Special Session",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_title"], "Special Session")

    def test_header_text_fallback(self):
        """When formTitle is empty, headerText should be used."""
        data = {
            "bodyText": "1/1/2026\n",
            "headerText": "Emergency Meeting",
            "formTitle": "",
        }
        result = parse_metadata_from_page_data(data)
        self.assertEqual(result["meeting_title"], "Emergency Meeting")


if __name__ == "__main__":
    unittest.main()
