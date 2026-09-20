"""Featured-image selection for newsletter articles + front-page cards
(Pete directives 2026-09-08).

Two consumers:
1. Newsletter article featured_image (publish step): single-topic runs map to
   a topic pool; weekly-roundup / boards-commissions classify the TOP STORY
   into one of the four pools first.
2. Front-page card fallback: an article with no featured_image shows a
   topic-appropriate photo (from article tags) instead of the gray chip.

Mixing (Pete: "not exactly the same images each week — that's why I'm giving
you redundant images"): images ROTATE by ISO week of the run/article date, so
a generic story gets a different (but still appropriate) image week to week.
When the story clearly matches one candidate's keywords (fire vs police,
river vs faucet, ASU station), the rotation is confined to the matching
candidates so we never show the wrong image for a specific story.

Deterministic, no LLM, no DB state. Paths are root-relative (/static/...).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

# topic -> ordered candidates: (featured_image path, [candidate keywords]).
# Order = rotation order. First entry is the PREFERRED image for generic
# stories in the earliest week; rotation moves through the rest.
TOPIC_IMAGES: dict[str, list[tuple[str, list[str]]]] = {
    "housing": [
        (
            "/static/uploads/tempe-apartment-building.jpg",
            [
                "apartment", "housing", "residential", "zoning", "rezon",
                "affordable", "development", "home", "dwelling", "density",
                "meritage", "adu", "multi-family", "multifamily", "rental",
                "subdivision", "general plan", "lot split", "tenants",
            ],
        ),
        (
            "/static/uploads/tempe-row-houses.jpg",
            [
                "row house", "townhouse", "townhome", "infill", "housing",
                "residential", "adu", "home", "dwelling", "rental",
            ],
        ),
        (
            "/static/uploads/culdesac-tempe-apartments-path-2.jpg",
            [
                "culdesac", "apartment", "housing", "residential",
                "development", "density", "multi-family", "multifamily",
                "rental", "zoning", "rezon", "car-free", "walkable",
            ],
        ),
        (
            "/static/uploads/downtown-phoenix-apartments-mural.jpg",
            [
                "apartment", "housing", "residential", "development",
                "density", "multi-family", "multifamily", "rental",
                "downtown", "mural", "adaptive reuse",
            ],
        ),
        (
            "/static/uploads/downtown-phoenix-apartments-mural-birds.jpg",
            [
                "apartment", "housing", "residential", "development",
                "density", "multi-family", "multifamily", "rental",
                "downtown", "mural", "adaptive reuse",
            ],
        ),
    ],
    "public-safety": [
        (
            "/static/uploads/phoenix-police-suv.jpg",
            [
                "police", "sheriff", "officer", "patrol", "crime", "shooting",
                "enforcement", "gun", "arrest", "jail", "911", "range",
            ],
        ),
        (
            "/static/uploads/phoenix-fire-truck.jpg",
            [
                "fire", "ems", "rescue", "paramedic", "hazmat", "engine",
                "ambulance", "burn", "smoke", "firefighter",
            ],
        ),
        (
            "/static/uploads/phoenix-police-museum-car.jpg",
            [
                "police", "museum", "classic car", "memorial", "history",
                "vintage", "display",
            ],
        ),
    ],
    "transportation": [
        (
            "/static/uploads/rail-bridge.jpg",
            ["rail", "bridge", "train", "track", "light rail", "streetcar", "freight"],
        ),
        (
            "/static/uploads/asu-transit-station.jpg",
            ["asu", "arizona state", "university", "transit station", "bus rapid", "station"],
        ),
        (
            "/static/uploads/tempe-rail-station.jpg",
            ["tempe streetcar", "tempe rail", "tempe station"],
        ),
        (
            "/static/images/mill-ave-light-rail.jpg",
            ["mill ave", "mill avenue", "mill light rail"],
        ),
    ],
    "water-environment": [
        (
            "/static/uploads/colorado-river-flickr.jpg",
            [
                "river", "colorado", "canal", "flood", "lake", "reservoir",
                "aquifer", "cap", "salt river", "watershed", "water",
            ],
        ),
        (
            "/static/uploads/scottsdale-water-faucet.jpg",
            ["faucet", "conservation", "rebate", "usage", "drought", "tap", "gallons"],
        ),
    ],
}

# Broad per-topic keywords used ONLY for top-story classification on
# weekly-roundup / boards-commissions (which span all topics). Separate from
# candidate keywords so road/highway projects still classify as
# transportation even though no dedicated road photo exists.
CLASSIFY_KEYWORDS: dict[str, list[str]] = {
    "housing": [
        "housing", "apartment", "residential", "zoning", "rezon",
        "affordable", "development", "home", "dwelling", "density",
        "meritage", "adu", "multi-family", "multifamily", "rental",
        "subdivision", "general plan", "lot split", "tenants", "supply",
    ],
    "public-safety": [
        "police", "sheriff", "officer", "patrol", "crime", "shooting",
        "enforcement", "gun", "arrest", "jail", "911", "range", "fire",
        "ems", "rescue", "paramedic", "hazmat", "ambulance", "firefighter",
    ],
    "transportation": [
        "road", "roadway", "highway", "freeway", "interstate", "arterial",
        "traffic", "corridor", "avenue", "boulevard", "street", "lane",
        "intersection", "overpass", "transit", "bus", "light rail",
        "rail", "streetcar", "bike", "pedestrian", "sidewalk", "bridge",
        "train", "track", "station", "airport", "pavement",
    ],
    "water-environment": [
        "water", "faucet", "conservation", "rebate", "usage", "drought",
        "tap", "gallons", "river", "colorado", "canal", "flood", "lake",
        "reservoir", "aquifer", "cap", "salt river", "watershed",
        "wastewater", "stormwater", "sewer", "reclaimed",
    ],
}

_TOPIC_PRIORITY = ["housing", "public-safety", "transportation", "water-environment"]

# ── Front-page card fallback pools (tag name -> ordered image paths) ────
# Same mixing idea: an article without a featured image cycles through its
# topic's redundant photos by the week the article was published.
CARD_FALLBACK_POOLS: dict[str, list[str]] = {
    "housing": [
        "/static/uploads/tempe-apartment-building.jpg",
        "/static/uploads/tempe-row-houses.jpg",
        "/static/uploads/culdesac-tempe-apartments-path-2.jpg",
        "/static/uploads/downtown-phoenix-apartments-mural.jpg",
        "/static/uploads/downtown-phoenix-apartments-mural-birds.jpg",
    ],
    "development": [
        "/static/uploads/building-construction.jpg",
        "/static/uploads/tempe-row-houses.jpg",
    ],
    "zoning": [
        "/static/uploads/building-construction.jpg",
        "/static/uploads/tempe-row-houses.jpg",
    ],
    "water": [
        "/static/uploads/colorado-river-flickr.jpg",
        "/static/uploads/scottsdale-water-faucet.jpg",
    ],
    "environment": [
        "/static/uploads/colorado-river-flickr.jpg",
        "/static/uploads/scottsdale-water-faucet.jpg",
    ],
    "infrastructure": [
        "/static/uploads/colorado-river-flickr.jpg",
        "/static/uploads/scottsdale-water-faucet.jpg",
    ],
    "public safety": [
        "/static/uploads/phoenix-police-suv.jpg",
        "/static/uploads/phoenix-fire-truck.jpg",
        "/static/uploads/phoenix-police-museum-car.jpg",
    ],
    "transportation": [
        "/static/uploads/rail-bridge.jpg",
        "/static/uploads/asu-transit-station.jpg",
        "/static/uploads/tempe-rail-station.jpg",
        "/static/images/mill-ave-light-rail.jpg",
    ],
    "government": [
        "/static/uploads/tempe-city-hall.jpg",
        "/static/uploads/downtown-tempe.jpg",
    ],
    "weekly roundup": [
        "/static/uploads/downtown-tempe.jpg",
        "/static/uploads/downtown-phoenix.jpg",
        "/static/uploads/downtown-phoenix-roosevelt-mural.jpg",
    ],
}

GENERIC_CARD_IMAGES = [
    "/static/uploads/downtown-tempe.jpg",
    "/static/uploads/downtown-phoenix.jpg",
]

# ── City pools (Pete directive 2026-09-16) ───────────────────────────────
# "Where possible, match the city to the topic" — a Mesa story should get a
# Mesa photo, a Glendale story a Glendale photo, and so on, before falling
# back to the topic pool.  Ordered; weekly rotation applies within the pool.
CITY_POOLS: dict[str, list[str]] = {
    "mesa": [
        "/static/uploads/mesa-city-council-wide.jpg",
        "/static/uploads/mesa-city-hall-wide.jpg",
        "/static/uploads/mesa-city-hall.jpg",
        "/static/uploads/mesa-arts-center.jpg",
        "/static/uploads/mesa-city-hall-neutral.jpg",
        "/static/uploads/mesa-cone-face-sculpture.jpg",
        "/static/uploads/mesa-flags-city-hall.jpg",
        "/static/uploads/mesa-light-rail-platform-night.jpg",
        "/static/uploads/mesa-night-intersection.jpg",
        "/static/uploads/mesa-city-hall-bike.jpg",
    ],
    "tempe": [
        "/static/uploads/tempe-apartment-building.jpg",
        "/static/uploads/tempe-row-houses.jpg",
        "/static/uploads/culdesac-tempe-apartments-path-2.jpg",
        "/static/uploads/tempe-city-hall.jpg",
        "/static/uploads/downtown-tempe.jpg",
        "/static/uploads/tempe-rail-station.jpg",
        "/static/uploads/asu-transit-station.jpg",
        "/static/uploads/tempe-culdesac-mexican-restaurant-mural.JPG",
    ],
    "phoenix": [
        "/static/uploads/downtown-phoenix-apartments-mural.jpg",
        "/static/uploads/downtown-phoenix-apartments-mural-birds.jpg",
        "/static/uploads/downtown-phoenix.jpg",
        "/static/uploads/downtown-phoenix-panorama.jpg",
        "/static/uploads/downtown-phoenix-skyline.jpg",
        "/static/uploads/downtown-phoenix-roosevelt-mural.jpg",
        "/static/uploads/old-phoenix-city-hall-front-facing.jpg",
        "/static/uploads/phoenix-police-suv.jpg",
        "/static/uploads/phoenix-fire-truck.jpg",
        "/static/uploads/phoenix-police-museum-car.jpg",
    ],
    "glendale": [
        "/static/uploads/glendale-city-hall.jpg",
    ],
    "maricopa county": [
        "/static/uploads/maricopa-county-board-of-supervisors.jpg",
        "/static/uploads/maricopa-county-building.jpg",
        "/static/uploads/maricopa-county-office-building.jpg",
        "/static/uploads/maricopa-county-office.jpg",
    ],
    "scottsdale": [
        "/static/uploads/scottsdale-old-town-sign.jpg",
        "/static/uploads/scottsdale-water-faucet.jpg",
    ],
}

# Photos that are clearly about one topic — used to keep a city pick
# topically sensible (Mesa light-rail platform for a transit story).
CITY_PHOTO_TOPICS: dict[str, set[str]] = {
    "/static/uploads/mesa-light-rail-platform-night.jpg": {"transportation"},
    "/static/uploads/mesa-night-intersection.jpg": {"transportation"},
    "/static/uploads/mesa-city-hall-bike.jpg": {"transportation"},
    "/static/uploads/phoenix-police-suv.jpg": {"public-safety"},
    "/static/uploads/phoenix-fire-truck.jpg": {"public-safety"},
    "/static/uploads/phoenix-police-museum-car.jpg": {"public-safety"},
    "/static/uploads/scottsdale-water-faucet.jpg": {"water-environment"},
    "/static/uploads/tempe-rail-station.jpg": {"transportation"},
    "/static/uploads/asu-transit-station.jpg": {"transportation"},
}

_CITY_NAMES = (
    "apache junction", "avondale", "buckeye", "carefree", "cave creek",
    "chandler", "el mirage", "fountain hills", "gilbert", "glendale",
    "goodyear", "guadalupe", "litchfield park", "maricopa county", "mesa",
    "paradise valley", "peoria", "phoenix", "queen creek", "scottsdale",
    "surprise", "tempe", "tolleson", "wickenburg", "youngtown",
)


def detect_city(*texts) -> str | None:
    """Return the city named in the given text(s), if any.

    Longer names win, so "maricopa county" beats a bare "mesa" match inside
    an unrelated word.  Returns the canonical lowercase key used by
    CITY_POOLS, or None.
    """
    blob = " ".join(t for t in texts if t).lower()
    best: str | None = None
    for name in _CITY_NAMES:
        if name in blob and (best is None or len(name) > len(best)):
            best = name
    return best


def _city_pick(city: str, topic: str, week: int, text: str = "") -> str:
    """Pick a photo from the city's pool.

    City first, but never topically wrong (Pete 2026-09-16):
    1. City photos that are already candidates for this topic win, and
       keyword pinning still applies (a Tempe row-house story keeps the
       row-house photo).
    2. Otherwise the city's generic civic photos (city hall, skyline,
       murals), excluding any photo tagged with a different topic.
    """
    pool = CITY_POOLS.get(city) or []
    if not pool:
        return ""
    if topic and topic in TOPIC_IMAGES:
        topical = [(p, kws) for p, kws in TOPIC_IMAGES[topic] if p in pool]
        if topical:
            return _pick_from_pool(text, topical, week)
    candidates = [
        p for p in pool
        if not CITY_PHOTO_TOPICS.get(p) or (topic and topic in CITY_PHOTO_TOPICS[p])
    ]
    if not candidates:
        candidates = [p for p in pool if not CITY_PHOTO_TOPICS.get(p)] or pool
    return _rotate(candidates, week)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _text_tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


def _score(text: str, keywords: list[str]) -> int:
    toks = _text_tokens(text)
    score = 0
    for kw in keywords:
        kw_l = kw.lower()
        if " " in kw_l:
            if kw_l in (text or "").lower():
                score += 2
        elif kw_l in toks:
            score += 1
    return score


def _week_number(when=None) -> int:
    """ISO week (1-53) of the run/article date; defaults to today (UTC)."""
    if when is None:
        when = datetime.now(timezone.utc).date()
    elif isinstance(when, datetime):
        when = when.date() if when.tzinfo else when.date()
    elif isinstance(when, str):
        when = date.fromisoformat(when[:10])
    return when.isocalendar().week


def _rotate(paths: list[str], week: int) -> str:
    """Deterministic per-week rotation through redundant images."""
    if not paths:
        return ""
    return paths[(week - 1) % len(paths)]


def _pick_from_pool(text: str, pool: list[tuple[str, list[str]]], week: int) -> str:
    """Pick an image from a topic pool.

    - A single highest-scoring candidate is PINNED (fire story → fire truck,
      river story → river photo) in every week.
    - A tie among candidates rotates by week through the tied images.
    - No keyword match (generic story) rotates through the whole pool.
    """
    scored = [(_score(text, kws), path) for path, kws in pool]
    best = max(s for s, _ in scored)
    if best <= 0:
        return _rotate([p for p, _ in pool], week)
    winners = [path for s, path in scored if s == best]
    return _rotate(winners, week)


def classify_top_story(text: str) -> str | None:
    """Pick the topic pool whose keywords best match the top story text.

    Returns the topic key, or None when nothing matches (no image then).
    """
    best_topic: str | None = None
    best_score = 0
    for topic in _TOPIC_PRIORITY:
        s = _score(text, CLASSIFY_KEYWORDS[topic])
        if s > best_score:
            best_score, best_topic = s, topic
    return best_topic if best_score > 0 else None


def pick_featured_image(topic: str, top_story_text: str, run_date=None,
                        city: str | None = None) -> str:
    """Return the featured_image path for a newsletter article.

    topic: workflow key (housing, water-environment, public-safety,
        transportation, boards-commissions, weekly-roundup).
    top_story_text: title + summary of the run's top item.
    run_date: ISO date (YYYY-MM-DD) of the run — drives the weekly rotation;
        defaults to today (UTC).
    city: jurisdiction of the top story (Pete 2026-09-16: match the city
        where possible).  When omitted it is detected from top_story_text.
    Returns "" when no topic matches and no sensible default exists.
    """
    week = _week_number(run_date)

    # City first (Pete directive 2026-09-16).
    city = (city or detect_city(top_story_text) or "").lower().strip()
    if city:
        img = _city_pick(city, topic, week, top_story_text)
        if img:
            return img

    if topic in TOPIC_IMAGES:
        return _pick_from_pool(top_story_text, TOPIC_IMAGES[topic], week)
    cat = classify_top_story(top_story_text)
    if not cat:
        return ""
    return _pick_from_pool(top_story_text, TOPIC_IMAGES[cat], week)


def card_fallback_image(tags_or_names, published_at=None, city: str | None = None) -> str:
    """Pick a front-page card image for an article without featured_image.

    Accepts Tag objects or tag-name strings, plus the article's published_at
    (drives weekly rotation; defaults to today).  City first (Pete
    2026-09-16) when exactly one jurisdiction tag is present; otherwise the
    topic tag selects a pool; no match -> generic civic pool.  Never returns
    "" so the gray chip disappears.
    """
    week = _week_number(published_at)
    names = [getattr(t, "name", None) or t for t in (tags_or_names or [])]
    lowered = [(n or "").strip().lower() for n in names]

    if city is None:
        # Only a single jurisdiction tag is unambiguous; a multi-city
        # roundup must fall through to the topic pool.
        city_tags = {n for n in lowered if n in CITY_POOLS}
        if len(city_tags) == 1:
            city = next(iter(city_tags))
    if city:
        img = _city_pick(city, "", week, " ".join(names))
        if img:
            return img

    for raw in names:
        name = (raw or "").strip().lower()
        if not name:
            continue
        for key, paths in CARD_FALLBACK_POOLS.items():
            if key in name:
                return _rotate(paths, week)
    return _rotate(GENERIC_CARD_IMAGES, week)
