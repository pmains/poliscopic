from scraper.jurisdictions.flagstaff import parse_agenda, parse_listing


def test_flagstaff_card_listing_and_body_mapping():
    html = '''<div class="p-2 border-top"><h3>September 8, 2026: Planning &amp; Zoning Commission</h3>
      <a href="agenda.cfm?seq=4870">Agenda</a><a href="minutes.cfm?meetingId=4870">Minutes</a></div>'''
    row = parse_listing(html)[0]
    assert row["meeting_id"] == "4870"
    assert row["meeting_date"] == "2026-09-08"
    assert row["body_code"] == "flagstaff-pz"
    assert row["minutes_url"].endswith("minutes.cfm?meetingId=4870")


def test_flagstaff_agenda_items_and_attachments():
    meeting = {"meeting_id": "4870", "body_code": "flagstaff-pz",
               "agenda_url": "https://public.destinyhosted.com/35247/agenda/agenda.cfm?seq=4870"}
    html = '''<div class="mediumText" id="item-10">5.</div><div class="mediumText">A.</div>
      <div class="start-at-content extend-to-end"><strong>Rezoning case</strong>
      Discuss the case. <a href="docs/staff-report.pdf">Staff Report</a></div>'''
    items, docs = parse_agenda(html, meeting)
    assert items[0]["agenda_item_number"] == "5A"
    assert items[0]["agenda_item_title"] == "Rezoning case"
    assert docs[0]["agenda_item_id"] == items[0]["agenda_item_id"]
    assert docs[0]["document_url"].endswith("/agenda/docs/staff-report.pdf")


def test_flagstaff_lettered_children_use_current_parent():
    meeting = {"meeting_id": "1", "body_code": "flagstaff-cc", "agenda_url": "https://example.test/a"}
    html = '''<div class="mediumText" id="item-1">5.</div><div class="start-at-content extend-to-end">Section</div>
      <div class="mediumText" id="item-2"></div><div class="mediumText">A.</div>
      <div class="start-at-content extend-to-end">First child</div>
      <div class="mediumText" id="item-3"></div><div class="mediumText">B.</div>
      <div class="start-at-content extend-to-end">Second child</div>'''
    items, _ = parse_agenda(html, meeting)
    assert [item["agenda_item_number"] for item in items] == ["5", "5A", "5B"]
