#!/usr/bin/env python3
"""Rotation tests: never repeat a photo a recent article already shows.

Pete 2026-09-26: "we used it two articles ago. We need to be rotating to visually
cue people that these are separate articles and stories."

Rotation is keyed on the ISO WEEK, so two articles in the same week drawing on the
same pool resolved to the identical image — which is how the 2026-09-26 weekly
roundup repeated the 2026-09-24 water article's photo. These tests pin the fix:
images already in use are excluded BEFORE the rotation index is applied.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from newsletter_images import (  # noqa: E402
    CARD_FALLBACK_POOLS,
    CITY_POOLS,
    TOPIC_IMAGES,
    card_fallback_image,
    pick_featured_image,
)

# 2026-09-21 is ISO week 39 — the week the duplicate actually occurred in.
W39 = "2026-09-21"
WATER_STORY = "Salt River water supply canal reservoir river update"
RIVER = "/static/uploads/colorado-river-flickr.jpg"


def test_week_keyed_rotation_alone_collides():
    """Documents the defect: with no exclusions the two articles pick the SAME image."""
    water = pick_featured_image("water-environment", WATER_STORY, run_date=W39)
    roundup = pick_featured_image("weekly-roundup", WATER_STORY, run_date=W39)
    assert water == RIVER
    assert roundup == water, "precondition: same week + same pool collides"


def test_roundup_no_longer_repeats_the_articles_photo():
    """The regression this change exists to fix."""
    water = pick_featured_image("water-environment", WATER_STORY, run_date=W39)
    roundup = pick_featured_image("weekly-roundup", WATER_STORY, run_date=W39,
                                 exclude=[water])
    assert roundup, "rotation must still return an image"
    assert roundup != water


def test_rotation_overrides_a_keyword_pin():
    """A pinned photo (fire truck) yields to rotation when already in use."""
    pinned = pick_featured_image("public-safety", "house fire apartment blaze",
                                 run_date=W39)
    rotated = pick_featured_image("public-safety", "house fire apartment blaze",
                                  run_date=W39, exclude=[pinned])
    assert pinned and rotated
    assert rotated != pinned


def test_exclusion_never_dead_ends_the_picker():
    """Excluding the ENTIRE pool still returns a usable path, never "".

    Small pools (public-safety has 3, housing 5) must not strand the caller.
    """
    every = [p for p, _ in TOPIC_IMAGES["water-environment"]]
    picked = pick_featured_image("water-environment", WATER_STORY, run_date=W39,
                                 exclude=every)
    assert picked in every


def test_rotation_is_deterministic():
    kwargs = dict(run_date=W39, exclude=[RIVER])
    first = pick_featured_image("weekly-roundup", WATER_STORY, **kwargs)
    second = pick_featured_image("weekly-roundup", WATER_STORY, **kwargs)
    assert first == second


def test_consecutive_picks_stay_distinct():
    """Feeding each pick back as 'already used' never repeats."""
    story = "city council zoning variance approval"
    used: list[str] = []
    for _ in range(4):
        img = pick_featured_image("weekly-roundup", story, run_date=W39, exclude=used)
        assert img, "a pick must always be returned"
        assert img not in used, f"repeat: {img}"
        used.append(img)
    assert len(set(used)) == len(used)


def test_excluded_pick_is_not_even_considered_by_the_scorer():
    """Exclusion happens before scoring, so the next-best candidate can win."""
    water = pick_featured_image("water-environment", WATER_STORY, run_date=W39)
    assert water == RIVER
    without_river = pick_featured_image("water-environment", WATER_STORY,
                                        run_date=W39, exclude=[RIVER])
    assert without_river and without_river != RIVER


def test_city_pool_rotation_respects_exclusions():
    """City-first picks rotate too (Mesa pool has 10 entries)."""
    first = CITY_POOLS["mesa"][0]
    picked = pick_featured_image("boards-commissions", "Mesa planning and zoning",
                                 run_date=W39, city="mesa", exclude=[first])
    assert picked and picked != first


def test_exhausting_the_city_pool_yields_a_qualified_peer_not_a_repeat():
    """When every city image is used, a qualified TOPIC peer may be picked.

    Jurisdiction and topic are PEERS (Pete 2026-09-26: "Neither Jurisdiction nor
    Topic is paramount"), so exhausting one dimension must not dead-end the picker
    and must not force a repeat — it falls back to another QUALIFIED candidate.
    """
    every = list(CITY_POOLS["mesa"])
    picked = pick_featured_image("boards-commissions", "Mesa planning and zoning",
                                 run_date=W39, city="mesa", exclude=every)
    assert picked, "a pick must always be returned"
    assert picked not in every, "a used image must not be repeated while peers exist"


def test_fresh_peer_beats_blocked_perfect_city_and_topic_match():
    """A reused 2-axis match must yield to a fresh 1-axis qualified image.

    This is the production regression behind tempe-apartment-building.jpg:
    semantic tiering used to happen before the reuse check, trapping selection
    inside a one-image city+topic tier.
    """
    repeated = "/static/uploads/tempe-apartment-building.jpg"
    picked = pick_featured_image(
        "housing",
        "Tempe apartment housing development and zoning",
        run_date=W39,
        city="tempe",
        exclude=[repeated],
    )
    assert picked
    assert picked != repeated


def test_uploaded_library_metadata_participates_in_selection():
    """Uploaded images are candidates; the selector is not hard-coded-only."""
    hard_coded = list(CITY_POOLS["tempe"])
    hard_coded.extend(path for path, _ in TOPIC_IMAGES["housing"])
    uploaded = "/static/uploads/new-tempe-homes-photo.jpg"
    picked = pick_featured_image(
        "housing",
        "Tempe housing development",
        run_date=W39,
        city="tempe",
        exclude=hard_coded,
        library=[{
            "path": uploaded,
            "original_name": "neighborhood-photo.jpg",
            "alt_text": "New multifamily homes in Tempe",
            "tags": "tempe,housing,multifamily",
        }],
    )
    assert picked == uploaded


def test_registry_city_display_name_reaches_city_pool():
    """`City of Mesa` from db.names must normalize to the `mesa` pool key."""
    picked = pick_featured_image(
        "housing",
        "Park North attached-home rezoning",
        run_date=W39,
        city="City of Mesa",
        exclude=["/static/uploads/mesa-city-council-wide.jpg"],
    )
    assert picked in CITY_POOLS["mesa"]


def test_card_fallback_accepts_exclusions():
    first = card_fallback_image(["Water"], published_at=W39)
    assert first
    second = card_fallback_image(["Water"], published_at=W39, exclude=[first])
    assert second
    pool = CARD_FALLBACK_POOLS.get("water") or []
    if len(pool) > 1:
        assert second != first


def test_default_behaviour_is_unchanged_without_exclusions():
    """Backwards compatibility: no exclude argument == previous behaviour exactly."""
    for topic in TOPIC_IMAGES:
        with_default = pick_featured_image(topic, WATER_STORY, run_date=W39)
        with_empty = pick_featured_image(topic, WATER_STORY, run_date=W39, exclude=())
        assert with_default == with_empty
