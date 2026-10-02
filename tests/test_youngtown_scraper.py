from scraper.jurisdictions.youngtown import parse_listing, parse_youngtown_items


def test_parse_listing_distinguishes_council_and_cfd_and_stabilizes_urls():
    html = '''<base href="https://www.youngtownaz.org/">
    <table><tr><td>09/03/26 Regular Council Meeting</td><td><a href="Documents/council.pdf?t=1">Agenda</a></td><td><a href="agenda_detail_T30_R158.php">More...</a></td></tr></table>
    <table><tr><td>09/03/26 Agua Fria Ranch CFD Special Board Meeting</td><td><a href="Documents/cfd.pdf?t=2">Agenda</a></td><td><a href="Documents/min.pdf?t=3">Minutes</a></td><td><a href="agenda_detail_T30_R159.php">More...</a></td></tr></table>'''
    rows = parse_listing(html)
    assert [r["meeting_id"] for r in rows] == ["158", "159"]
    assert [r["body_code"] for r in rows] == ["youngtown-cc", "youngtown-afr-cfd"]
    assert rows[0]["agenda_url"] == "https://www.youngtownaz.org/Documents/council.pdf"
    assert rows[1]["minutes_url"].endswith("/Documents/min.pdf")


def test_document_paths_are_url_encoded():
    rows = parse_listing('''<table><tr><td>10/01/26 Regular Council Meeting</td>
      <td><a href="Documents/Town Clerk/Agenda Final.pdf?t=9">Agenda</a></td>
      <td><a href="agenda_detail_T30_R161.php">More...</a></td></tr></table>''')
    assert "%20" in rows[0]["agenda_url"]
    assert "?" not in rows[0]["agenda_url"]


def test_parse_youngtown_items_uses_lettered_children_for_section():
    text = """1. Call to Order
8. Consent
A. Approval of minutes
continued detail
B. Approval of agreement
9. Business
A. Financial report
10. Adjournment"""
    items = parse_youngtown_items(text, "161")
    assert [item["agenda_item_number"] for item in items] == ["1", "8A", "8B", "9A", "10"]
    assert items[1]["agenda_item_title"] == "Approval of minutes"
    assert "continued detail" in items[1]["agenda_item_text"]


def test_parse_youngtown_items_deduplicates_repeated_numbering():
    items = parse_youngtown_items("1. Open\n2. Approve\n1. Open\n2. Approve", "159")
    assert [item["agenda_item_number"] for item in items] == ["1", "2"]
