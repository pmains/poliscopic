"""Unit tests for newsletter featured-image selection (Pete directive 2026-09-08).

Single-topic newsletters map to a topic photo pool; weekly-roundup and
boards-commissions classify the top story into one of the four pools.
Images ROTATE by ISO week of the run/article date (Pete: mix it up, not the
same images each week) — but when the story text matches one candidate's
keywords, the pick is pinned to the right image (fire story → fire truck).

Selection is deterministic keyword + week scoring — no LLM.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from newsletter_images import (  # noqa: E402
    CLASSIFY_KEYWORDS,
    TOPIC_IMAGES,
    CARD_FALLBACK_POOLS,
    GENERIC_CARD_IMAGES,
    card_fallback_image,
    classify_top_story,
    pick_featured_image,
)

# Deterministic test dates (ISO weeks verified): week 37 → rotation idx 0;
# week 38 → idx 1 for 2-image pools; week 39 → idx 2 for 4-image pools.
W37 = "2026-09-08"  # week 37
W38 = "2026-09-14"  # week 38
W39 = "2026-09-21"  # week 39


def _paths(topic):
    return [p for p, _ in TOPIC_IMAGES[topic]]


def test_all_pools_have_existing_local_files():
    """Every configured image path must resolve to a real file under repo root."""
    root = Path(__file__).resolve().parent.parent
    for topic, pool in TOPIC_IMAGES.items():
        for path, _ in pool:
            assert (root / path.lstrip("/")).exists(), f"missing {path} ({topic})"


def test_public_safety_fire_text_pins_fire_truck():
    # Story clearly about fire/EMS → fire truck in ANY week.
    for date in (W37, W38, W39):
        assert pick_featured_image("public-safety", "new fire station and EMS rescue", date) == \
            "/static/uploads/phoenix-fire-truck.jpg"


def test_public_safety_police_text_pins_police_suv():
    for date in (W37, W38, W39):
        assert pick_featured_image("public-safety", "police patrol and sheriff contract", date) == \
            "/static/uploads/phoenix-police-suv.jpg"


def test_public_safety_rotates_on_generic_text():
    # No fire/police signal → rotates through both redundant images by week.
    pool = _paths("public-safety")
    a = pick_featured_image("public-safety", "general public safety operations matter", W37)
    b = pick_featured_image("public-safety", "general public safety operations matter", W38)
    assert a in pool and b in pool
    assert a != b, "expected different generic image across weeks (mixing)"


def test_transportation_rotates_on_generic_text():
    pool = _paths("transportation")  # 4 candidates
    picks = {pick_featured_image("transportation", "transportation system agenda item", d)
             for d in (W37, W38, W39)}
    assert picks <= set(pool)
    assert len(picks) >= 2, "expected rotation across weeks (mixing)"


def test_water_preferred_is_colorado_river():
    # Pete preference: colorado-river-flickr is the preferred water image.
    # Week 37 idx0 = colorado-river.
    assert pick_featured_image("water-environment", "unrelated agenda item", W37) == \
        "/static/uploads/colorado-river-flickr.jpg"


def test_water_rotates_to_faucet_in_alternate_week():
    assert pick_featured_image("water-environment", "unrelated agenda item", W38) == \
        "/static/uploads/scottsdale-water-faucet.jpg"


def test_water_river_text_pins_colorado_river():
    for date in (W37, W38, W39):
        assert pick_featured_image("water-environment", "Colorado River drought levels", date) == \
            "/static/uploads/colorado-river-flickr.jpg"


def test_housing_single_image_any_week():
    # Housing pool now also has row houses; generic housing text matches
    # multiple candidates, so allow pool membership; specific apartment text
    # should still land on the apartment building image.
    img = pick_featured_image("housing", "apartment rezone density", W37)
    assert img in _paths("housing")
    assert pick_featured_image("housing", "tempe row house infill project", W37) == \
        "/static/uploads/tempe-row-houses.jpg"


def test_roundup_classifies_police_top_story():
    img = pick_featured_image(
        "weekly-roundup",
        "Police shooting range special use permit before the commission",
        W37,
    )
    assert img == "/static/uploads/phoenix-police-suv.jpg"


def test_roundup_classifies_road_top_story():
    img = pick_featured_image(
        "weekly-roundup",
        "Elliot Road widening: six-lane arterial roadway with traffic signals",
        W37,
    )
    assert img in _paths("transportation")


def test_roundup_returns_empty_on_unrelated_top_story():
    assert pick_featured_image("weekly-roundup", "miscellaneous housekeeping items", W37) == ""


def test_boards_commissions_uses_top_story_topic():
    img = pick_featured_image(
        "boards-commissions",
        "Water infrastructure and canal maintenance contract",
        W37,
    )
    assert img in _paths("water-environment")


def test_classify_top_story_returns_known_topics():
    assert classify_top_story("apartment rezoning density") == "housing"
    assert classify_top_story("fire station and EMS") == "public-safety"
    assert classify_top_story("Elliot Road widening roadway traffic") == "transportation"
    assert classify_top_story("Colorado River water supply") == "water-environment"
    assert classify_top_story("unrelated random chatter") is None


def test_classify_keywords_exist_for_every_pool():
    for topic in TOPIC_IMAGES:
        assert CLASSIFY_KEYWORDS.get(topic), f"missing classify keywords for {topic}"


# ── Front-page card fallback (Pete directive 2026-09-08) ──

class _Tag:
    """Minimal stand-in for the Tag ORM object (has .name)."""
    def __init__(self, name):
        self.name = name


def test_card_fallback_maps_topic_tags_and_rotates():
    # Week 37 → idx0 (preferred); week 38 → idx1 (redundant image).
    pool = CARD_FALLBACK_POOLS["water"]
    assert card_fallback_image([_Tag("Water")], W37) == pool[0]
    assert card_fallback_image([_Tag("Water")], W38) == pool[1]


def test_card_fallback_accepts_plain_strings():
    # Plain tag-name strings work like Tag objects. Expected image follows
    # the pool-length-aware rotation (idx = (week-1) % len(pool)), so the
    # assertion survives pools growing as redundant photos are added.
    pool = CARD_FALLBACK_POOLS["housing"]
    assert card_fallback_image(["Housing"], W37) == pool[(37 - 1) % len(pool)]


def test_card_fallback_ignores_jurisdiction_tags_and_uses_generic():
    # Jurisdiction-only tags never match a topic pool -> generic civic pool.
    out = card_fallback_image([_Tag("Mesa"), _Tag("Tempe")], W37)
    assert out in GENERIC_CARD_IMAGES
    assert card_fallback_image([], W37) in GENERIC_CARD_IMAGES


def test_card_fallback_images_exist_on_disk():
    root = Path(__file__).resolve().parent.parent
    for paths in list(CARD_FALLBACK_POOLS.values()) + [GENERIC_CARD_IMAGES]:
        for path in paths:
            assert (root / path.lstrip("/")).exists(), f"missing {path}"
