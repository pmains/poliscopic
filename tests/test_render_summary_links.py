"""Renderer tests for §1 overview inline item links (Pete directive 2026-09-08).

The editorial overview no longer ships machine tokens like "(bos item 44)".
Items are referenced by inline markdown links on natural phrases, and the
deterministic renderer converts them to <a> tags — allowlisted to THIS run's
item deep links so no invented/hallucinated URL can ever reach the email.
"""

import re

import pytest

from workflows.templates.render import (
    render_newsletter,
    _summary_paragraph_html,
    _strip_item_tokens,
    _protect_street_addresses,
)

SUMMARY_DIV = re.compile(r'<div style="background: #f5f5f5.*?</div>', re.S)

ALLOWED = {
    "https://poliscopic.com/meetings/bos/4699#item-44",
    "https://poliscopic.com/meetings/phoenix-cc/1364180#item-102",
    "https://poliscopic.com/meetings/phoenix-cc/1364180#item-103",
}

ITEM_WITH_LINK = {
    "body": "bos",
    "meeting_id": "4699",
    "agenda_item_number": "44",
    "meeting_date": "2026-09-02",
    "agenda_item_title": "Down-payment assistance contract",
    "poliscopic_deep_link": "https://poliscopic.com/meetings/bos/4699#item-44",
}


def test_allowed_markdown_link_becomes_anchor():
    html = _summary_paragraph_html(
        "Supervisors would [expand the contract](https://poliscopic.com/meetings/bos/4699#item-44) to help 148 buyers.",
        ALLOWED,
    )
    assert '<a href="https://poliscopic.com/meetings/bos/4699#item-44"' in html
    assert ">expand the contract</a>" in html


def test_disallowed_link_drops_href_keeps_label():
    """A URL not in this run's item deep links must not become clickable."""
    html = _summary_paragraph_html(
        "Supervisors would [expand the contract](https://evil.example/phish) to help 148 buyers.",
        ALLOWED,
    )
    assert "evil.example" not in html
    assert "expand the contract" in html
    assert "<a " not in html


def test_stray_machine_tokens_are_stripped():
    """Defensive: no '(bos item 44)' style tokens can reach the email."""
    html = _summary_paragraph_html(
        "Supervisors (bos item 44) would expand the contract (bos items 8 and 9) now.",
        ALLOWED,
    )
    assert "(bos item 44)" not in html
    assert "(bos items 8 and 9)" not in html


def test_multiple_links_in_one_sentence():
    html = _summary_paragraph_html(
        "Hearings on a [General Plan change](https://poliscopic.com/meetings/phoenix-cc/1364180#item-102) "
        "and a [PUD](https://poliscopic.com/meetings/phoenix-cc/1364180#item-103) for 21 acres.",
        ALLOWED,
    )
    assert html.count("<a href=") == 2
    assert "item-102" in html and "item-103" in html


def test_prose_is_html_escaped():
    html = _summary_paragraph_html("Costs rose & fell <fast> — 5-10 homes/acre.", set())
    assert "&amp;" in html and "&lt;fast&gt;" in html


def test_render_newsletter_summary_links_with_items():
    """End-to-end: render_newsletter links overview items when items carry deep links."""
    summary = (
        "County supervisors would [expand the down-payment contract]"
        "(https://poliscopic.com/meetings/bos/4699#item-44) to help 148 buyers."
    )
    html = render_newsletter(
        "Housing", "September 8, 2026",
        [ITEM_WITH_LINK],
        summary=summary,
    )
    div_match = SUMMARY_DIV.search(html)
    assert div_match, "expected a summary div"
    div = div_match.group(0)
    assert '<a href="https://poliscopic.com/meetings/bos/4699#item-44"' in div
    assert "item-44" in div
    assert "(bos item 44)" not in div


def test_strip_item_tokens_utility():
    assert _strip_item_tokens("a (bos item 44) b") == "a b"
    assert _strip_item_tokens("a (phoenix-cc items 102 and 103) b") == "a b"
    assert _strip_item_tokens("a (item 90) b") == "a b"
    assert _strip_item_tokens("plain (context) text") == "plain (context) text"


# ── Gmail address auto-link protection (Pete directive 2026-09-08, SO 46341944) ──

def test_bare_street_address_is_wrapped_in_plain_anchor():
    out = _summary_paragraph_html(
        "An assisted-living home at 1338 W Lobo Avenue would serve ten residents.",
        set(),
    )
    assert '<a href="" style="color: inherit; text-decoration: none !important;' in out
    assert "1338 W Lobo Avenue</a>" in out
    assert "maps.google" not in out and "google.com/maps" not in out


def test_trailing_period_stays_outside_address_anchor():
    out = _summary_paragraph_html(
        "The home at 1338 W Lobo Avenue. Phoenix weighs its next move.",
        set(),
    )
    assert "Lobo Avenue</a>." in out
    assert "Lobo Avenue.</a>" not in out


def test_address_inside_deep_link_is_not_nested():
    """An address inside an allowed poliscopic anchor must not get a second,
    nested <a> (that is the Gmail-stitched fragment case)."""
    dl = "https://poliscopic.com/meetings/mesa-boa/1438326#item-3-d"
    out = _summary_paragraph_html(
        f"Asked to allow [a group home at 1338 W Lobo Avenue]({dl}).",
        {dl},
    )
    assert out.count("<a ") == 1
    assert 'href="{dl}"'.format(dl=dl) in out


def test_intersection_and_acreage_not_wrapped():
    out = _protect_street_addresses("rezoning 160 acres at Deer Valley Road and 231st Avenue")
    assert "<a href=\"\"" not in out
    out2 = _protect_street_addresses("17 and Rose Garden Lane, capped at 336 units")
    assert "<a href=\"\"" not in out2


def test_item_reason_address_is_protected():
    from workflows.templates.render import render_newsletter
    item = {
        "body": "mesa-boa", "meeting_id": "1438326", "agenda_item_number": "3-d",
        "meeting_date": "2026-09-09", "meeting_title": "Board of Adjustment",
        "agenda_item_title": "BOA request",
        "reason": "A 10-resident assisted-living home at 1338 West Lobo Avenue.",
        "poliscopic_deep_link": "https://poliscopic.com/meetings/mesa-boa/1438326#item-3-d",
    }
    html = render_newsletter("Housing", "September 8, 2026", [item], summary="")
    assert '<a href="" style="color: inherit;' in html
    assert "1338 West Lobo Avenue</a>" in html
