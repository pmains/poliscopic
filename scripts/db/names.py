"""Single source of truth for human-readable body and jurisdiction names.

Pete 2026-09-17: names were duplicated across at least seven hand-maintained
maps (the publish script, the article template, the scraper CLI, per-city
scraper modules, the KG adjudication, the reports compiler).  The registry
already lives in the database -- ``public_bodies`` (``body_code`` -> ``name``)
and ``jurisdictions`` -- so everything should read from HERE instead of
carrying its own dict.

Deliberately NOT consolidated here:
  * Scraper *source labels* -- the exact wording an upstream site uses to
    select a body (e.g. Glendale's dropdown label, Tempe's OnBase id 112).
    Those are scraper inputs that must match the upstream site verbatim;
    they are not display names.  Call them ``source_label``, not ``name``.
  * Short labels for tight UI -- derive them from the canonical name
    (abbreviation) rather than keeping a second map.

A jurisdiction name ("Chandler") and a body name ("Chandler City Council")
are two different entities, so both exist -- that is not duplication.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Iterable, Optional

from sqlalchemy import select

logger = logging.getLogger(__name__)

# Names change rarely but are read on every rendered row, so keep a small
# in-process TTL cache.  Cleared by clear_cache() after a rename import.
_CACHE: dict[str, tuple[float, str]] = {}
_JUR_CACHE: dict[str, tuple[float, Optional[str]]] = {}
_CACHE_TTL = 300.0


def clear_cache() -> None:
    """Drop cached names (call after refreshing the body registry)."""
    _CACHE.clear()
    _JUR_CACHE.clear()


# Words that must survive jurisdiction-stripping for a name to stay
# meaningful.  "Maricopa County Board of Adjustment" -> "Board of Adjustment"
# is useful; "Mesa City Council" -> "City Council" is NOT (it stops
# identifying anything in a mixed list).  Declared once, here.
_BODY_WORDS = (
    "council", "commission", "board", "authority", "committee",
    "district", "trust", "agency", "department", "office", "panel",
    "court", "association", "program", "group", "bureau",
)

# Name styles a caller may ask for.  ONE registry, several contexts
# (Pete 2026-09-17): variants are derived here, not duplicated per call site.
NAME_STYLES = (
    "canonical",          # exactly what the registry stores
    "with-jurisdiction",  # "Maricopa County Board of Adjustment"
    "no-jurisdiction",    # "Board of Adjustment"
    "jurisdiction",       # "Maricopa County"
)


def _looks_meaningful(name: str) -> bool:
    low = name.lower()
    return any(w in low for w in _BODY_WORDS)


# Jurisdiction type wrappers.  "City of Chandler" and "Chandler" must both be
# recognised inside a body name, otherwise qualification double-prefixes it:
# "City of Chandler Chandler Planning & Zoning Commission" (Pete 2026-09-17).
_JUR_PREFIXES = ("city of ", "town of ", "village of ", "county of ")
_JUR_SUFFIXES = (" county", " city", " town", " village")


def _jurisdiction_tokens(jurisdiction: str) -> list[str]:
    """Distinctive forms of a jurisdiction name.

    'City of Chandler' -> ['City of Chandler', 'Chandler']
    'Maricopa County'  -> ['Maricopa County', 'Maricopa']
    """
    j = (jurisdiction or "").strip()
    if not j:
        return []
    low = j.lower()
    toks = [j]
    for p in _JUR_PREFIXES:
        if low.startswith(p):
            rest = j[len(p):].strip()
            if rest:
                toks.append(rest)
            break
    for s in _JUR_SUFFIXES:
        if low.endswith(s):
            base = j[: -len(s)].strip()
            if base:
                toks.append(base)
            break
    out: list[str] = []
    for t in toks:
        if t.lower() not in [x.lower() for x in out]:
            out.append(t)
    return out


def _mentions_jurisdiction(name: str, jurisdiction: str) -> bool:
    """True when the body name already carries the jurisdiction."""
    for t in _jurisdiction_tokens(jurisdiction):
        if re.search(rf"\b{re.escape(t.lower())}\b", name.lower()):
            return True
    return False


def _strip_jurisdiction(name: str, jurisdiction: str) -> str:
    """Drop a jurisdiction PREFIX only when the remainder still identifies a body."""
    if not name or not jurisdiction:
        return name
    low = name.lower()
    # Longest token first so "City of Chandler" beats "Chandler".
    for tok in sorted(_jurisdiction_tokens(jurisdiction), key=len, reverse=True):
        if low.startswith(tok.lower() + " "):
            rest = name[len(tok) + 1:].strip()
            if rest and _looks_meaningful(rest):
                return rest
            logger.debug(
                "not stripping jurisdiction from %r — remainder %r is not "
                "self-identifying", name, rest,
            )
    return name


def _qualify(name: str, jurisdiction: str) -> str:
    """Prefix the jurisdiction unless the name already carries it.

    Matches on the distinctive part, so 'City of Chandler' plus
    'Chandler Planning & Zoning Commission' is left alone rather than
    becoming 'City of Chandler Chandler Planning & Zoning Commission'.
    """
    if not name or not jurisdiction:
        return name
    if _mentions_jurisdiction(name, jurisdiction):
        return name
    return f"{jurisdiction} {name}"


def _jurisdiction_of(code: str, session=None) -> Optional[str]:
    """Cached jurisdiction lookup for a body code."""
    now = time.monotonic()
    ent = _JUR_CACHE.get(code)
    if ent and (now - ent[0]) < _CACHE_TTL:
        return ent[1]
    val = body_jurisdictions([code], session=session).get(code)
    _JUR_CACHE[code] = (time.monotonic(), val)
    return val


def get_display_name(style: str, code: str, session=None) -> str:
    """Context-aware display name.

    ``get_display_name('no-jurisdiction', 'chandler-cc')`` -> the name
    without its jurisdiction qualifier.  One canonical registry, several
    rendering contexts.  An unknown style logs and falls back to
    'canonical' rather than raising, so a template can never 500 over a
    typo'd style.
    """
    code = _coerce(code)
    if not code:
        return ""
    style = (style or "canonical").strip().lower()
    if style not in NAME_STYLES:
        logger.warning("unknown name style %r — using 'canonical'", style)
        style = "canonical"

    name = body_name(code, session=session)
    if style == "canonical":
        return name

    jur = _jurisdiction_of(code, session=session)
    if style == "jurisdiction":
        return jur or ""
    if not jur:
        return name
    if style == "with-jurisdiction":
        return _qualify(name, jur)
    return _strip_jurisdiction(name, jur)


__all__ = [
    "humanize_code",
    "body_names",
    "body_name",
    "body_jurisdictions",
    "jurisdiction_name_for_body",
    "jurisdiction_name",
    "get_display_name",
    "NAME_STYLES",
]


def humanize_code(code: str) -> str:
    """Readable last-resort fallback for a code with no registry row.

    Never returns a shouting ALL-CAPS slug (the old ``body.upper()`` bug)
    and never returns the raw code unchanged for a plain slug.
    """
    if not code:
        return ""
    return " ".join(
        w.capitalize() for w in code.replace("_", "-").split("-") if w
    )


def _coerce(code: str) -> str:
    return (code or "").strip()


def body_names(codes: Iterable[str], session=None) -> dict[str, str]:
    """Resolve body codes to canonical display names in ONE query.

    Returns ``{body_code: display_name}`` for every code requested.  A code
    with no registry row is logged (so the gap is visible) and gets a
    humanized fallback rather than a raw slug.
    """
    want = [_coerce(c) for c in dict.fromkeys(codes)]
    want = [c for c in want if c]
    if not want:
        return {}

    # Serve what the cache can, query only the rest.
    now = time.monotonic()
    cached: dict[str, str] = {}
    misses: list[str] = []
    for c in want:
        ent = _CACHE.get(c)
        if ent and (now - ent[0]) < _CACHE_TTL:
            cached[c] = ent[1]
        else:
            misses.append(c)
    if not misses:
        return {c: cached[c] for c in want}

    from db.models import PublicBody

    own = session is None
    if own:
        from db.core import get_session
        session = get_session()
    try:
        rows = session.execute(
            select(PublicBody.body_code, PublicBody.name)
            .where(PublicBody.body_code.in_(misses))
            .order_by(PublicBody.name)
        ).all()

        # A body_code matching several registry rows means duplicates exist
        # (found 2026-09-17: chandler-cf, chandler-pdc, chandler-pha).
        # Surface it instead of silently picking one.
        seen_counts: dict[str, int] = {}
        for r in rows:
            if r[0]:
                seen_counts[r[0]] = seen_counts.get(r[0], 0) + 1
        dupes = sorted(c for c, n in seen_counts.items() if n > 1)
        if dupes:
            logger.warning(
                "public_bodies has duplicate body_code rows: %s — "
                "name resolution takes the first by name; de-duplicate the "
                "registry to make this deterministic",
                ", ".join(dupes),
            )

        # First row wins (ordered by name) so the result is deterministic.
        found: dict[str, str] = {}
        for code, name in rows:
            if code and name and code not in found:
                found[code] = name

        missing = [c for c in misses if c not in found]
        if missing:
            logger.warning(
                "no registered display name for body code(s): %s — "
                "using humanized fallback (add to public_bodies to fix)",
                ", ".join(sorted(missing)),
            )
        resolved = {c: found.get(c) or humanize_code(c) for c in misses}
        when = time.monotonic()
        for c, name in resolved.items():
            _CACHE[c] = (when, name)
        return {c: (cached[c] if c in cached else resolved[c]) for c in want}
    finally:
        if own:
            session.close()


def body_name(code: str, session=None) -> str:
    """Canonical display name for a single body code."""
    code = _coerce(code)
    if not code:
        return ""
    return body_names([code], session=session).get(code, humanize_code(code))


def body_jurisdictions(codes: Iterable[str], session=None) -> dict[str, str]:
    """Resolve body codes to their jurisdiction names in ONE query."""
    want = [_coerce(c) for c in dict.fromkeys(codes)]
    want = [c for c in want if c]
    if not want:
        return {}

    from db.models import PublicBody, Jurisdiction

    own = session is None
    if own:
        from db.core import get_session
        session = get_session()
    try:
        rows = session.execute(
            select(PublicBody.body_code, Jurisdiction.name)
            .join(Jurisdiction, PublicBody.jurisdiction_id == Jurisdiction.id)
            .where(PublicBody.body_code.in_(want))
        ).all()
        found = {r[0]: r[1] for r in rows if r[0] and r[1]}
        return {c: found[c] for c in want if c in found}
    finally:
        if own:
            session.close()


def jurisdiction_name_for_body(code: str, session=None) -> Optional[str]:
    """The jurisdiction a body belongs to (e.g. 'chandler-cc' -> 'Chandler')."""
    code = _coerce(code)
    if not code:
        return None
    from db.models import PublicBody, Jurisdiction

    own = session is None
    if own:
        from db.core import get_session
        session = get_session()
    try:
        row = session.execute(
            select(Jurisdiction.name)
            .join(PublicBody, PublicBody.jurisdiction_id == Jurisdiction.id)
            .where(PublicBody.body_code == code)
        ).first()
        return row[0] if row else None
    finally:
        if own:
            session.close()


def jurisdiction_name(jurisdiction_id: Optional[int], session=None) -> Optional[str]:
    """Canonical display name for a jurisdiction id."""
    if jurisdiction_id is None:
        return None
    from db.models import Jurisdiction

    own = session is None
    if own:
        from db.core import get_session
        session = get_session()
    try:
        row = session.execute(
            select(Jurisdiction.name).where(Jurisdiction.id == jurisdiction_id)
        ).first()
        return row[0] if row else None
    finally:
        if own:
            session.close()
