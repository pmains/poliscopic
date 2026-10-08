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
                "rezone", "rezoned", "rezoning",
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
        # Rotation-only entry: keywords describe the photograph itself, never
        # agenda prose, so this never wins a keyword pin and cannot displace
        # the river image on river stories. It joins the generic-text rotation.
        (
            "/static/uploads/virginia-lake-green-trees-grass-blue-sky.jpg",
            ["virginia lake", "green trees", "blue sky"],
        ),
        # Further rotation entries, same rule: descriptive keywords only, so
        # none of them can displace a keyword-pinned image.
        (
            "/static/uploads/knoll-lake-canoe-pine-trees.jpg",
            ["knoll lake", "canoe", "pine trees"],
        ),
        (
            "/static/uploads/papago-park-pond.jpg",
            ["papago park", "park pond"],
        ),
        (
            "/static/uploads/lockett-meadow-inner-basin-lake.jpg",
            ["lockett meadow", "inner basin"],
        ),
        (
            "/static/uploads/crescent-moon-ranch-river.jpg",
            ["crescent moon ranch", "oak creek"],
        ),
        (
            "/static/uploads/lake-powell-wolfgang-staudt.jpg",
            ["lake powell", "glen canyon"],
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
        "rezone", "rezoned", "rezoning",
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
        "/static/uploads/virginia-lake-green-trees-grass-blue-sky.jpg",
        "/static/uploads/knoll-lake-canoe-pine-trees.jpg",
        "/static/uploads/papago-park-pond.jpg",
        "/static/uploads/lockett-meadow-inner-basin-lake.jpg",
        "/static/uploads/crescent-moon-ranch-river.jpg",
        "/static/uploads/lake-powell-wolfgang-staudt.jpg",
    ],
    "environment": [
        "/static/uploads/colorado-river-flickr.jpg",
        "/static/uploads/scottsdale-water-faucet.jpg",
        "/static/uploads/virginia-lake-green-trees-grass-blue-sky.jpg",
        "/static/uploads/knoll-lake-canoe-pine-trees.jpg",
        "/static/uploads/papago-park-pond.jpg",
        "/static/uploads/lockett-meadow-inner-basin-lake.jpg",
        "/static/uploads/crescent-moon-ranch-river.jpg",
        "/static/uploads/lake-powell-wolfgang-staudt.jpg",
    ],
    "infrastructure": [
        "/static/uploads/colorado-river-flickr.jpg",
        "/static/uploads/scottsdale-water-faucet.jpg",
        "/static/uploads/virginia-lake-green-trees-grass-blue-sky.jpg",
        "/static/uploads/knoll-lake-canoe-pine-trees.jpg",
        "/static/uploads/papago-park-pond.jpg",
        "/static/uploads/lockett-meadow-inner-basin-lake.jpg",
        "/static/uploads/crescent-moon-ranch-river.jpg",
        "/static/uploads/lake-powell-wolfgang-staudt.jpg",
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


def normalize_city(value: str | None) -> str:
    """Normalize registry display names to the keys used by ``CITY_POOLS``."""
    city = (value or "").lower().strip()
    for prefix in ("city of ", "town of "):
        if city.startswith(prefix):
            city = city[len(prefix):].strip()
            break
    return city


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


#: Photo-SUBJECT tokens, so a FILENAME can be classified by topic.
#: Pete 2026-09-26: "I try to include both the jurisdiction and topic keywords in the
#: image titles for this reason" — the filename is the classification key, so a story
#: can be matched by jurisdiction, by topic, or ideally by BOTH.
FILENAME_TOPIC_TOKENS: dict[str, tuple[str, ...]] = {
    "water-environment": ("water", "faucet", "river", "canal", "reservoir", "pond",
                          "wastewater", "aquifer", "flood", "well"),
    "transportation": ("rail", "transit", "bike", "intersection", "street", "road",
                       "station", "platform", "bridge", "light-rail"),
    "public-safety": ("fire", "police", "safety", "sheriff"),
    "housing": ("apartment", "townhome", "townhome", "housing", "row-house",
                "row-houses", "multifamily", "casita", "condo"),
    "boards-commissions": ("city-hall", "council", "board", "commission", "supervisors"),
}

#: Neutral CIVIC markers. For a jurisdiction-level story with no topic-specific photo,
#: the city's civic building is the honest default rather than an unrelated landmark.
CIVIC_TOKENS = ("city-hall", "city_hall", "council", "board", "commission",
                "supervisors", "office-building", "city-center")


def _filename(path: str) -> str:
    return (path or "").rsplit("/", 1)[-1].lower()


def _photo_matches_topic(path: str, topic: str | None) -> bool:
    """Whether this PHOTO's filename carries a subject token for `topic`."""
    if not topic:
        return False
    name = _filename(path)
    return any(token in name for token in FILENAME_TOPIC_TOKENS.get(topic, ()))


def _is_civic_photo(path: str) -> bool:
    """Whether this photo is a neutral civic/government image for its city."""
    name = _filename(path)
    return any(token in name for token in CIVIC_TOKENS)


def _library_index(library=()) -> dict[str, str]:
    """Normalize uploaded-library entries into path -> searchable metadata."""
    result: dict[str, str] = {}
    for entry in library or ():
        if isinstance(entry, dict):
            path = entry.get("path") or entry.get("url") or ""
            metadata = entry.get("metadata") or " ".join(
                str(entry.get(key) or "")
                for key in ("filename", "original_name", "alt_text", "tags")
            )
        else:
            try:
                path, metadata = entry
            except (TypeError, ValueError):
                continue
        if path:
            result[str(path)] = str(metadata or "").lower()
    return result


_PERSON_IMAGE_MARKERS = (
    "portrait", " mayor ", " police chief", " fire chief", " councilmember",
    " council member", " supervisor", " senator", " representative",
    "speaking at a podium", "headshot",
)


def _library_record_index(library=()) -> dict[str, dict[str, str]]:
    """Return structured uploaded-image metadata keyed by public path."""
    result: dict[str, dict[str, str]] = {}
    for entry in library or ():
        if not isinstance(entry, dict):
            continue
        path = str(entry.get("path") or entry.get("url") or "")
        if not path:
            continue
        result[path] = {
            key: str(entry.get(key) or "").lower()
            for key in ("filename", "original_name", "alt_text", "tags")
        }
    return result


def _unnamed_person_image(record: dict[str, str], story_text: str) -> bool:
    """Reject a person-led photo unless that person is named in the story.

    Role/topic tags such as ``police`` or ``transportation`` describe what a
    public official works on; they do not make the official's portrait an
    honest illustration for every story in that beat.
    """
    blob = " ".join(record.values())
    padded = f" {blob} "
    if not any(marker in padded for marker in _PERSON_IMAGE_MARKERS):
        return False

    original = record.get("original_name", "").rsplit("/", 1)[-1]
    stem = original.rsplit(".", 1)[0]
    tokens = re.findall(r"[a-z]+", stem)
    if len(tokens) < 2:
        return True
    # Uploaded portraits conventionally end in the subject's first and last
    # name (e.g. colby-brandt or photographer_kate_gallego).
    subject = " ".join(tokens[-2:])
    return subject not in " ".join(_text_tokens(story_text))


def _metadata_matches_topic(metadata: str, topic: str | None) -> bool:
    if not topic:
        return False
    return _score(metadata, CLASSIFY_KEYWORDS.get(topic, [])) > 0


def _qualified_images(city: str | None, topic: str | None, library=()) -> list[str]:
    """Every image qualified for this story, in a MEANINGFUL order.

    City pool first, then the topic pool, then the generic fallbacks — deduped with
    declaration order preserved. Order matters: a pool's declaration order encodes
    preference (the preferred water image is deliberately first) and the weekly
    rotation indexes into it, so re-sorting would silently change the rotation.

    Neither dimension owns the selection (Pete 2026-09-26: "Neither Jurisdiction nor
    Topic is paramount"), so both contribute candidates and the RANKING decides.
    """
    ordered: list[str] = []
    seen: set[str] = set()
    sources: list[list[str]] = []
    if city:
        sources.append(list(CITY_POOLS.get(city) or ()))
    sources.append([path for path, _ in TOPIC_IMAGES.get(topic or "", ())])
    uploaded = _library_index(library)
    sources.append([
        path for path, metadata in uploaded.items()
        if (city and city in metadata) or _metadata_matches_topic(metadata, topic)
    ])
    sources.append(list(GENERIC_CARD_IMAGES))
    for source in sources:
        for path in source:
            if path not in seen:
                seen.add(path)
                ordered.append(path)
    return ordered


def _text_score(path: str, topic: str | None, text: str) -> int:
    """Story-text score for a path, using its topic-pool keyword list (0 if none)."""
    for candidate, keywords in TOPIC_IMAGES.get(topic or "", ()):
        if candidate == path:
            return _score(text, keywords)
    return 0


def _select_image(city: str | None, topic: str | None, week: int, text: str,
                  exclude=(), library=()) -> str:
    """Rank qualified images by MATCH QUALITY, then apply diversity.

    Pete 2026-09-27:
      * jurisdiction OR topic is good; jurisdiction AND topic is better;
      * neither dimension is paramount — a both-match beats a one-match, and the
        two one-matches are PEERS (no jurisdiction-first or topic-first order);
      * diversity matters — never reuse an image while another qualified
        candidate is still unused.
    """
    pool = _qualified_images(city, topic, library)
    if not pool:
        return ""
    blocked = {p for p in (exclude or ()) if p}

    # Membership in the topic's own pool is ALREADY a topic qualification, so
    # ranking must not narrow the pool to filename tokens — doing so would drop
    # perfectly good candidates (e.g. lake photos whose names carry no token) and
    # shrink the rotation, which is the opposite of the diversity requirement.
    # The token check exists only to recognise a CITY image that also suits the
    # topic. (Pete 2026-09-26)
    topic_paths = {p for p, _ in TOPIC_IMAGES.get(topic or "", ())}
    city_paths = set(CITY_POOLS.get(city or "", ()))
    uploaded = _library_index(library)
    uploaded_records = _library_record_index(library)

    # Tier = how many dimensions match: 2 = both, 1 = exactly one, 0 = neither.
    scored = []
    for path in pool:
        metadata = uploaded.get(path, "")
        if _unnamed_person_image(uploaded_records.get(path, {}), text):
            continue
        named_city = detect_city(_filename(path).replace("-", " "))
        conflicts_with_city = bool(city and named_city and named_city != city)
        by_city = bool(city) and (
            path in city_paths or named_city == city
            or city in _filename(path).replace("-", " ") or city in metadata
        )
        by_topic = ((path in topic_paths) or _photo_matches_topic(path, topic)
                    or _metadata_matches_topic(metadata, topic))
        # A photo explicitly named for another jurisdiction is a last-resort
        # diversity fallback, never a peer of a correct-city candidate.
        if conflicts_with_city:
            by_topic = False
        scored.append((int(by_city) + int(by_topic), path))

    # Freshness is a HARD constraint across the entire qualified pool, not a
    # tie-breaker inside the highest semantic tier. The old ordering first kept
    # only city+topic matches and then applied exclusions, so one "perfect"
    # image could repeat while many unused city-only or topic-only peers sat
    # available. That produced the visibly unprofessional tight loop this
    # selector is specifically meant to prevent.
    qualified = [(score, path) for score, path in scored if score > 0]
    fresh_qualified = [
        (score, path) for score, path in qualified if path not in blocked
    ]
    if fresh_qualified:
        scored = fresh_qualified
    elif qualified:
        # Every semantically qualified image is cooling down. Reuse the best
        # qualified one instead of escaping to an unrelated generic photo.
        scored = qualified
    else:
        fresh = [(score, path) for score, path in scored if path not in blocked]
        if fresh:
            scored = fresh
    best_tier = max(tier for tier, _ in scored)
    tier = [path for tier, path in scored if tier == best_tier]

    # Within the best tier, in order of priority:
    #   1. story-text precision (keeps "row house" text on the row-house photo);
    #   2. a neutral civic photo, which is what a jurisdiction story looks like.
    # Diversity was already enforced globally above.
    groups: dict[tuple, list[str]] = {}
    for path in tier:
        key = (-_text_score(path, topic, text),
               not _is_civic_photo(path))
        groups.setdefault(key, []).append(path)
    order = {path: index for index, path in enumerate(pool)}
    chosen = sorted(groups[min(groups)], key=lambda p: order.get(p, 1 << 30))
    return _rotate(chosen, week)


def _city_pick(city: str, topic: str, week: int, text: str = "", exclude=()) -> str:
    """Pick a photo from the city's pool — delegates to the shared ranker."""
    return _select_image(city, topic, week, text, exclude)

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


def _rotate(paths: list[str], week: int, exclude=()) -> str:
    """Deterministic per-week rotation through redundant images.

    ``exclude`` holds images ALREADY SHOWN on recent articles. Rotation is keyed on
    the ISO week alone, so two articles in the same week drawing on the same pool
    resolved to the identical image — which is how the 2026-09-26 weekly roundup
    ended up repeating the 2026-09-24 water article's photo. A repeat fails to
    signal that these are separate stories (Pete 2026-09-26), so images already in
    use are dropped BEFORE the rotation index is applied.

    Never returns "" merely because everything was excluded: when the filtered pool
    is empty the unfiltered pool is used as a last resort, so a small pool can never
    dead-end the caller.
    """
    if not paths:
        return ""
    blocked = {p for p in (exclude or ()) if p}
    fresh = [p for p in paths if p not in blocked]
    pool = fresh or list(paths)
    return pool[(week - 1) % len(pool)]


def _pick_from_pool(text: str, pool: list[tuple[str, list[str]]], week: int,
                    exclude=()) -> str:
    """Pick an image from a topic pool.

    - A single highest-scoring candidate is PINNED (fire story → fire truck,
      river story → river photo) in every week.
    - A tie among candidates rotates by week through the tied images.
    - No keyword match (generic story) rotates through the whole pool.
    - Images already used by recent articles are removed from consideration FIRST,
      so a pinned photo yields to rotation rather than repeating on a
      neighbouring article. With no exclusions the behaviour is unchanged.
    """
    blocked = {p for p in (exclude or ()) if p}
    candidates = [(p, kws) for p, kws in pool if p not in blocked]
    if not candidates:
        candidates = list(pool)
    if not candidates:
        return ""
    scored = [(_score(text, kws), path) for path, kws in candidates]
    best = max(s for s, _ in scored)
    if best <= 0:
        return _rotate([p for p, _ in candidates], week)
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
                        city: str | None = None, exclude=(), library=()) -> str:
    """Return the featured_image path for a newsletter article.

    topic: workflow key (housing, water-environment, public-safety,
        transportation, boards-commissions, weekly-roundup).
    top_story_text: title + summary of the run's top item.
    run_date: ISO date (YYYY-MM-DD) of the run — drives the weekly rotation;
        defaults to today (UTC).
    city: jurisdiction of the top story (Pete 2026-09-16: match the city
        where possible).  When omitted it is detected from top_story_text.
    exclude: images already shown on recent articles.  Rotation is keyed on the
        ISO week alone, so without this a roundup summarising a story can repeat
        that story's own photo (Pete 2026-09-26: rotate so a repeat cannot make
        separate articles look like the same story).
    library: uploaded image records as mappings or ``(path, metadata)`` pairs.
        Matching uses their original names, alt text, and tags, so the selector
        is not limited to a hard-coded pool.
    Returns "" when no topic matches and no sensible default exists.
    """
    week = _week_number(run_date)

    # City first (Pete directive 2026-09-16).
    city = normalize_city(city or detect_city(top_story_text))

    # A roundup/boards topic has no pool of its own: classify the TOP STORY, and
    # keep the historic property that an unrelated story yields no image at all.
    effective = topic if topic in TOPIC_IMAGES else (
        classify_top_story(top_story_text) or "")
    if not effective and not city:
        return ""
    return _select_image(city or None, effective or None, week, top_story_text,
                         exclude, library)


def card_fallback_image(tags_or_names, published_at=None, city: str | None = None,
                        exclude=()) -> str:
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
        img = _city_pick(city, "", week, " ".join(names), exclude)
        if img:
            return img

    for raw in names:
        name = (raw or "").strip().lower()
        if not name:
            continue
        for key, paths in CARD_FALLBACK_POOLS.items():
            if key in name:
                return _rotate(paths, week, exclude)
    return _rotate(GENERIC_CARD_IMAGES, week, exclude)
