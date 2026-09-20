#!/usr/bin/env python3
"""Buckeye Granicus agenda-block corrections.

Covers the pure helpers, the packet state machine, the six required fixture
cases (Granicus events 423, 424, 1071, 1046, one workshop, one non-PZ body), and
one PyMuPDF coordinate integration built locally — no network, no database.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO, _REPO / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scraper.platforms import granicus_agenda_blocks as B  # noqa: E402
from scraper.jurisdictions import buckeye_agenda_parse as P  # noqa: E402

_FX = _REPO / "tests" / "fixtures" / "buckeye"
_EXPECTED = json.loads((_FX / "expected.json").read_text())
_CASES = [name for name in _EXPECTED if not name.startswith("_")]


def _parse(name: str) -> B.PacketState:
    state = B.PacketState(name)
    for line in (_FX / f"{name}.txt").read_text().split("\n"):
        state.feed(line)
    return state


def _items(state: B.PacketState) -> dict[str, str]:
    return {item["agenda_item_number"]: item["agenda_item_title"]
            for item in state.items if item["item_type_category"] == "item"}


# ── resolution numbers must never become item numbers ──────────────────


def test_a_year_is_not_an_item_number():
    assert B.reject_as_item_number("2026", "2026. The Board has caused")
    assert B.is_year_like("2026") and B.is_year_like("1999")
    assert not B.is_year_like("20")


def test_a_resolution_half_is_not_an_item_number():
    text = "RES 04-26 Festival Ranch CFD GO Bonds 2026"
    assert B.is_resolution_reference(text)
    assert B.reject_as_item_number("04", text)
    assert B.reject_as_item_number("26", text)
    assert B.resolution_refs(text) == ["04-26"]


def test_a_real_item_number_is_still_accepted():
    assert not B.reject_as_item_number("4", "4.A Council to take action")
    assert not B.reject_as_item_number("107", "4.A Council to take action")
    assert not B.is_resolution_reference("4.A Council to take action")


def test_the_bond_series_prose_produces_no_item():
    """The exact live defect: '2026.' opened a feasibility-report sentence."""
    state = B.PacketState("buckeye-cfd-1071")
    for line in _EXPECTED["granicus_1071_cfd"]["resolution_prose"].split("\n"):
        state.feed(line)
    assert _items(state) == {}
    state.feed(_EXPECTED["granicus_1071_cfd"]["resolution_ref_line"])
    assert _items(state) == {}


# ── split labels come from coordinates, never from document order ──────


def test_an_aligned_adjacent_number_and_letter_join():
    upper = B.PositionedLine("4", 72.0, 82.0, 100.0, 112.0, 0)
    lower = B.PositionedLine("A", 72.5, 82.5, 114.0, 126.0, 0)
    assert B.join_split_label(upper, lower) == "4.A"


def test_a_misaligned_letter_does_not_join():
    upper = B.PositionedLine("4", 72.0, 82.0, 100.0, 112.0, 0)
    offset = B.PositionedLine("A", 300.0, 310.0, 114.0, 126.0, 0)
    assert B.join_split_label(upper, offset) is None


def test_a_distant_letter_does_not_join():
    upper = B.PositionedLine("4", 72.0, 82.0, 100.0, 112.0, 0)
    far = B.PositionedLine("A", 72.0, 82.0, 400.0, 412.0, 0)
    assert B.join_split_label(upper, far) is None


def test_a_letter_on_another_page_does_not_join():
    upper = B.PositionedLine("4", 72.0, 82.0, 100.0, 112.0, 0)
    other = B.PositionedLine("A", 72.0, 82.0, 114.0, 126.0, 1)
    assert B.join_split_label(upper, other) is None


def test_overlapping_line_boxes_still_join():
    """Real PDF line boxes overlap; adjacency uses line starts, not box edges.

    PyMuPDF reported '4' as y 88.2-103.3 and 'A' as 102.2-117.3 — the boxes
    overlap by 1.1pt, so an upper.y1/lower.y0 gap test rejects a genuine pair.
    """
    upper = B.PositionedLine("4", 72.0, 78.1, 88.2, 103.3, 0)
    lower = B.PositionedLine("A", 72.0, 79.3, 102.2, 117.3, 0)
    assert B.join_split_label(upper, lower) == "4.A"


def test_a_letter_never_joins_upward():
    """The number must be above the letter; order is not interchangeable."""
    upper = B.PositionedLine("A", 72.0, 82.0, 100.0, 112.0, 0)
    lower = B.PositionedLine("4", 72.0, 82.0, 114.0, 126.0, 0)
    assert B.join_split_label(upper, lower) is None


def test_a_letter_is_never_taken_from_document_order():
    """Two unrelated fragments in sequence must not become a label."""
    upper = B.PositionedLine("4", 72.0, 82.0, 100.0, 112.0, 0)
    unrelated = B.PositionedLine("A resolution was adopted", 72.0, 300.0, 114.0, 126.0, 0)
    assert B.join_split_label(upper, unrelated) is None


def test_replace_split_labels_folds_only_real_pairs():
    lines = [
        B.PositionedLine("2", 72.0, 82.0, 10.0, 22.0, 0),
        B.PositionedLine("A", 72.0, 82.0, 24.0, 36.0, 0),
        B.PositionedLine("Council to take action", 72.0, 200.0, 40.0, 52.0, 0),
    ]
    folded = B.replace_split_labels(lines)
    assert [line.text for line in folded] == ["2.A", "Council to take action"]


# ── boundaries ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("text,expected", [
    ("12", B.BOUNDARY_PAGE),
    ("Page 3 of 40", B.BOUNDARY_PAGE),
    ("CONSENT AGENDA ITEMS / NEW BUSINESS", B.BOUNDARY_SECTION),
    ("Adjournment", B.BOUNDARY_SECTION),
    ("Board of Directors to take action on the Tartesso West", B.BOUNDARY_SUBSTANTIVE),
    ("", B.BOUNDARY_NOISE),
])
def test_boundary_classification(text, expected):
    assert B.classify_boundary(text) == expected


# ── the state machine ──────────────────────────────────────────────────


def test_a_pending_label_emits_on_the_next_substantive_line():
    state = B.PacketState("m")
    state.feed("4.A")
    state.feed("Board of Directors to take action on the Festival Ranch")
    assert _items(state) == {"4.A": "Board of Directors to take action on the Festival Ranch"}
    assert state.held == []


def test_a_page_boundary_holds_the_pending_label_instead_of_absorbing_it():
    state = B.PacketState("m")
    state.feed("4.A")
    state.feed("12")
    state.feed("Page 13 of 40")
    state.feed("Some unrelated packet prose that follows the page break")
    assert _items(state) == {"4.A": "(see details)"}
    assert [h["agenda_item_number"] for h in state.held] == ["4.A"]
    assert state.held[0]["reason"] == "page boundary"


def test_a_section_boundary_holds_the_pending_label():
    state = B.PacketState("m")
    state.feed("4.A")
    state.feed("CONSENT AGENDA ITEMS / NEW BUSINESS")
    assert state.held[0]["agenda_item_number"] == "4.A"
    assert state.held[0]["reason"] == "section boundary"


def test_a_noise_line_does_not_become_a_title():
    state = B.PacketState("m")
    state.feed("4.A")
    state.feed("xyz")
    assert state.held[0]["reason"] == "noise before title"
    assert _items(state) == {"4.A": "(see details)"}


def test_resolution_prose_flushes_rather_than_titles_a_pending_label():
    state = B.PacketState("m")
    state.feed("2.B")
    state.feed("2026. The Board has caused the Feasibility Report to be prepared")
    assert state.held[0]["agenda_item_number"] == "2.B"
    assert state.held[0]["reason"] == "prose numbered by year or resolution"
    assert _items(state) == {"2.B": "(see details)"}


# ── PyMuPDF coordinate integration (built locally) ─────────────────────


def test_split_label_is_recovered_from_real_pdf_coordinates(tmp_path):
    """A PDF drawing '4' above 'A' yields the single label 4.A."""
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 100), "4", fontsize=11)
    page.insert_text((72, 114), "A", fontsize=11)
    page.insert_text((72, 130), "Board of Directors to take action on the Festival Ranch CFD", fontsize=11)
    pdf_path = tmp_path / "split_label.pdf"
    document.save(pdf_path)
    document.close()

    opened = fitz.open(pdf_path)
    words = opened[0].get_text("words")
    opened.close()
    lines = B.group_words_into_lines(words)
    folded = B.replace_split_labels(lines)
    texts = [line.text for line in folded]

    assert "4.A" in texts, texts
    assert "4" not in texts and "A" not in texts

    state = B.PacketState("pdf-fixture")
    for line in folded:
        state.feed(line.text)
    assert _items(state) == {
        "4.A": "Board of Directors to take action on the Festival Ranch CFD"}


def test_pdf_coordinates_keep_a_distant_letter_apart(tmp_path):
    """Alignment matters: a letter in another column is not a label half."""
    fitz = pytest.importorskip("fitz")
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 100), "4", fontsize=11)
    page.insert_text((320, 114), "A", fontsize=11)
    pdf_path = tmp_path / "misaligned.pdf"
    document.save(pdf_path)
    document.close()

    opened = fitz.open(pdf_path)
    words = opened[0].get_text("words")
    opened.close()
    folded = B.replace_split_labels(B.group_words_into_lines(words))
    assert [line.text for line in folded] == ["4", "A"]


# ── fixtures: the required cases ───────────────────────────────────────


@pytest.mark.parametrize("name", _CASES)
def test_fixture_expected_items(name):
    expected = _EXPECTED[name]
    state = _parse(name)
    items = _items(state)
    for number, title in expected["items"].items():
        assert number in items, f"{name}: missing item {number}"
        assert items[number].startswith(title[:60]), f"{name}: {number} title mismatch"


@pytest.mark.parametrize("name", _CASES)
def test_fixture_never_invents_a_resolution_number(name):
    expected = _EXPECTED[name]
    items = _items(_parse(name))
    for forbidden in expected["must_not_contain"]:
        assert forbidden not in items, f"{name}: invented item {forbidden}"


@pytest.mark.parametrize("name", _CASES)
def test_fixture_expected_holds(name):
    expected = _EXPECTED[name]
    held = [h["agenda_item_number"] for h in _parse(name).held]
    assert held == expected["held"], f"{name}: held {held}"


def test_the_1071_fixture_no_longer_reproduces_the_live_defect():
    """Guards the exact defect: item 2026 and a '(see details)' 4.A."""
    items = _items(_parse("granicus_1071_cfd"))
    assert "2026" not in items
    assert items["4.A"] == ("Board of Directors to take action on the "
                            "Festival Ranch Community Facilities District")


def test_no_case_ever_emits_a_details_only_title_for_a_resolved_label():
    for name in _CASES:
        state = _parse(name)
        for item in state.items:
            if item["item_type_category"] != "item":
                continue
            assert not (item["agenda_item_title"] == "(see details)"
                        and item["agenda_item_number"] not in
                        [h["agenda_item_number"] for h in state.held])


# ── old behaviour outside the fixtures ─────────────────────────────────


def test_the_text_line_parser_is_still_exported_and_unchanged():
    """The original signature and behaviour survive the module split."""
    assert callable(P.parse_agenda_pdf_items)
    text = ("CONSENT AGENDA ITEMS / NEW BUSINESS\n"
            "4.A Council to take action on Resolution No. 27-25 approving\n")
    items = P.parse_agenda_pdf_items(text, "m1")
    numbered = {i["agenda_item_number"]: i["agenda_item_title"]
                for i in items if i["item_type_category"] == "item"}
    assert numbered["4.A"] == "Council to take action on Resolution No. 27-25 approving"


def test_the_original_parser_still_captures_a_bare_label():
    items = P.parse_agenda_pdf_items("4.A\nCouncil to take action on something\n", "m2")
    numbered = [i["agenda_item_number"] for i in items if i["item_type_category"] == "item"]
    assert "4.A" in numbered


def test_modules_stay_under_the_line_limit():
    for module in (pathlib.Path(P.__file__),
                   _REPO / "scripts" / "scraper" / "platforms" / "granicus_agenda_blocks.py",
                   _REPO / "scripts" / "scraper" / "jurisdictions" / "buckeye_granicus.py"):
        assert len(module.read_text().split("\n")) < 500, module
