"""Canonical government authorities covered by scraper sources.

Jurisdictions are independent governing authorities, not a geographic tree.
Municipalities located in Maricopa County are not children of Maricopa County
government.  Regional authorities such as MAG and Valley Metro are likewise
represented independently.  Geographic containment, service area, and legal
relationships belong in separate data structures if the application needs
them later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


AuthorityKind = Literal["county", "municipality", "regional"]


@dataclass(frozen=True, slots=True)
class GovernmentAuthority:
    """One independent government or regional public authority."""

    slug: str
    name: str
    kind: AuthorityKind
    state: str = "AZ"
    form: str | None = None


GOVERNMENT_AUTHORITIES: tuple[GovernmentAuthority, ...] = (
    GovernmentAuthority("maricopa-county", "Maricopa County", "county"),
    GovernmentAuthority("apache-junction", "City of Apache Junction", "municipality"),
    GovernmentAuthority("avondale", "City of Avondale", "municipality"),
    GovernmentAuthority("buckeye", "City of Buckeye", "municipality"),
    GovernmentAuthority("chandler", "City of Chandler", "municipality"),
    GovernmentAuthority("el-mirage", "City of El Mirage", "municipality"),
    GovernmentAuthority("flagstaff", "City of Flagstaff", "municipality"),
    GovernmentAuthority("fountain-hills", "Town of Fountain Hills", "municipality"),
    GovernmentAuthority("gilbert", "Town of Gilbert", "municipality"),
    GovernmentAuthority("glendale", "City of Glendale", "municipality"),
    GovernmentAuthority("goodyear", "City of Goodyear", "municipality"),
    GovernmentAuthority("litchfield-park", "City of Litchfield Park", "municipality"),
    GovernmentAuthority("mesa", "City of Mesa", "municipality"),
    GovernmentAuthority("paradise-valley", "Town of Paradise Valley", "municipality"),
    GovernmentAuthority("peoria", "City of Peoria", "municipality"),
    GovernmentAuthority("phoenix", "City of Phoenix", "municipality"),
    GovernmentAuthority("queen-creek", "Town of Queen Creek", "municipality"),
    GovernmentAuthority("scottsdale", "City of Scottsdale", "municipality"),
    GovernmentAuthority("surprise", "City of Surprise", "municipality"),
    GovernmentAuthority("tempe", "City of Tempe", "municipality"),
    GovernmentAuthority("tolleson", "City of Tolleson", "municipality"),
    GovernmentAuthority("tucson", "City of Tucson", "municipality"),
    GovernmentAuthority("wickenburg", "Town of Wickenburg", "municipality"),
    GovernmentAuthority("youngtown", "Town of Youngtown", "municipality"),
    GovernmentAuthority("yuma", "City of Yuma", "municipality"),
    GovernmentAuthority(
        "mag",
        "Maricopa Association of Governments",
        "regional",
        form="council_of_governments",
    ),
    GovernmentAuthority(
        "valley-metro",
        "Valley Metro Regional Public Transportation Authority",
        "regional",
        form="transportation_authority",
    ),
)

_BY_SLUG = {authority.slug: authority for authority in GOVERNMENT_AUTHORITIES}

if len(_BY_SLUG) != len(GOVERNMENT_AUTHORITIES):
    raise ValueError("government authority slugs must be unique")


def authority_by_slug(slug: str) -> GovernmentAuthority:
    """Return one canonical authority or reject an unknown identity."""
    try:
        return _BY_SLUG[slug]
    except KeyError as exc:
        raise KeyError(f"unknown government authority: {slug}") from exc


def authority_slugs() -> frozenset[str]:
    """Return all canonical authority slugs."""
    return frozenset(_BY_SLUG)
