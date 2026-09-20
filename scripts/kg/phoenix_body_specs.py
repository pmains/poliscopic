#!/usr/bin/env python3
"""``phoenix_body_specs.py`` — the approved Phoenix body plans, and spec binding.

Three Phoenix bodies are identified as missing by the 392-blocker adjudication.
Each needs the *same* repair contract (register the body, parent its meetings), so
the contract itself lives once in :mod:`scripts.kg.phoenix_dr_plan_body`.  This
module supplies only the per-body **configuration** — canonical values, registry
evidence and expected counts — behind an explicit allowlist.

Why a binding context manager rather than a parameter
-----------------------------------------------------
``phoenix_dr_adjudication`` and ``phoenix_dr_plan_body`` deliberately expose the
target body as module constants ("The body code is a module constant, never a
caller parameter"), which is what stops the plan machinery from becoming a
permissive generic backfill.  Rather than restructure that reviewed code, or copy
its policy into a second builder, :func:`bound_spec` re-binds exactly those
constants for one plan build and restores them in ``finally``.

The allowlist stays the gate: an unapproved body code cannot be planned at all.
This is a deliberate trade-off, noted in the Step 5 report: it is safe and
single-threaded, but a follow-up parameterisation refactor would read more
cleanly than module-attribute rebinding.

Nothing here writes to a database.
"""

from __future__ import annotations

import re
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

__all__ = [
    "APPROVED_BODIES",
    "DEFAULT_BODY_CODE",
    "BodySpec",
    "approved_spec",
    "assert_no_body_collision",
    "bound_spec",
    "body_specs",
]

#: The body whose plan already exists; other bodies are compared against it.
DEFAULT_BODY_CODE = "phoenix-dr"

#: Existing ``public_bodies`` rows for City of Phoenix use this prefix (24 of 24).
DB_NAME_CONVENTION = "Phoenix <Body>"


@dataclass(frozen=True)
class BodySpec:
    """One approved body: canonical values, evidence, and expected population."""

    body_code: str
    name: str
    slug: str
    body_type: str
    title_pattern: str
    registry_evidence: tuple[str, ...]
    expected_meetings: int
    expected_extractions: int
    convention_derived_fields: tuple[str, ...]
    confirmation_note: str

    def identity(self) -> dict[str, Any]:
        """The canonical row values this spec proposes."""
        return {
            "body_code": self.body_code,
            "name": self.name,
            "slug": self.slug,
            "body_type": self.body_type,
        }


APPROVED_BODIES: Mapping[str, BodySpec] = {
    "phoenix-dr": BodySpec(
        body_code="phoenix-dr",
        name="Phoenix Design Review Committee",
        slug="phoenix-design-review-committee",
        body_type="Committee",
        title_pattern=r"design\s+review\s+committee",
        registry_evidence=(
            "scripts/scraper/jurisdictions/phoenix_planning.py: "
            "/design review committee/i -> 'phoenix-dr', 'Design Review Committee'",
            "scripts/scraper/jurisdictions/phoenix_aem.py: "
            "'phoenix-design-review' -> 'phoenix-dr'",
        ),
        expected_meetings=13,
        expected_extractions=126,
        convention_derived_fields=("name", "slug", "body_type"),
        confirmation_note=(
            "Canonical name approved 2026-09-11 as 'Phoenix Design Review Committee', "
            f"aligning with the database convention ({DB_NAME_CONVENTION}, 24 of 24 City of "
            "Phoenix rows).  slug/body_code/body_type remain convention-derived."
        ),
    ),
    "phoenix-dab": BodySpec(
        body_code="phoenix-dab",
        name="Phoenix Development Advisory Board",
        slug="phoenix-development-advisory-board",
        body_type="Board",
        title_pattern=r"development\s+advisory\s+board",
        registry_evidence=(
            "scripts/scraper/jurisdictions/phoenix_planning.py: "
            "/development advisory board/i -> 'phoenix-dab', 'Development Advisory Board'",
            "scripts/scraper/jurisdictions/phoenix_aem.py: "
            "'phoenix-development-advisory' -> 'phoenix-dab'",
        ),
        expected_meetings=37,
        expected_extractions=226,
        convention_derived_fields=("name", "slug", "body_type"),
        confirmation_note=(
            f"name follows the database convention ({DB_NAME_CONVENTION}) and therefore does "
            "NOT match the 'City of Phoenix …' form used for phoenix-dr; body_type 'Board' "
            "is derived from the registry display name, not stored evidence."
        ),
    ),
    "phoenix-ds": BodySpec(
        body_code="phoenix-ds",
        name="Phoenix Design Standards Committee",
        slug="phoenix-design-standards-committee",
        body_type="Committee",
        title_pattern=r"design\s+standards\s+committee",
        registry_evidence=(
            "scripts/scraper/jurisdictions/phoenix_planning.py: "
            "/design standards committee/i -> 'phoenix-ds', 'Design Standards Committee'",
            "scripts/scraper/jurisdictions/phoenix_aem.py: "
            "'phoenix-design-standards' -> 'phoenix-ds'",
        ),
        expected_meetings=5,
        expected_extractions=22,
        convention_derived_fields=("name", "slug", "body_type"),
        confirmation_note=(
            f"name follows the database convention ({DB_NAME_CONVENTION}); body_type "
            "'Committee' is derived from the registry display name."
        ),
    ),
}


def approved_spec(body_code: str) -> BodySpec:
    """The approved spec for a body code, or raise.

    The allowlist is the only way to plan a body: an unrecognised code is refused
    rather than defaulted.
    """
    try:
        return APPROVED_BODIES[body_code]
    except KeyError:
        raise KeyError(
            f"body {body_code!r} is not an approved repair target; "
            f"approved: {sorted(APPROVED_BODIES)}"
        ) from None


def body_specs(codes: Sequence[str] | None = None) -> list[BodySpec]:
    """The approved specs, optionally restricted to ``codes``, in stable order."""
    if codes is None:
        return [APPROVED_BODIES[c] for c in sorted(APPROVED_BODIES)]
    return [approved_spec(c) for c in codes]


def assert_no_body_collision(conn: Any, spec: BodySpec) -> None:
    """Refuse if any of body_code, slug or name is already taken.

    Collision checking is a precondition, not policy, so it lives here where the
    spec is known; the plan builder keeps its own body_code/slug guard as defence.
    """
    from sqlalchemy import text

    rows = conn.execute(
        text(
            "SELECT id, name, slug, body_code FROM public_bodies "
            "WHERE body_code = :code OR slug = :slug OR name = :name"
        ),
        {"code": spec.body_code, "slug": spec.slug, "name": spec.name},
    ).mappings().all()
    if rows:
        raise ValueError(
            f"refusing to plan {spec.body_code!r}: collision on body_code/slug/name "
            f"with {[dict(r) for r in rows]}"
        )


def _bindings(spec: BodySpec) -> list[tuple[Any, str, Any]]:
    """(module, attribute, value) triples that select a body for planning."""
    from scripts.kg import phoenix_dr_adjudication as adjudication
    from scripts.kg import phoenix_dr_plan_body as plan_body

    pattern = re.compile(spec.title_pattern, re.IGNORECASE)
    return [
        (adjudication, "BODY_CODE", spec.body_code),
        (adjudication, "TITLE_PATTERN", pattern),
        (adjudication, "PROPOSED_BODY_NAME", spec.name),
        (adjudication, "PROPOSED_BODY_SLUG", spec.slug),
        (adjudication, "PROPOSED_BODY_TYPE", spec.body_type),
        (adjudication, "REGISTRY_EVIDENCE", tuple(spec.registry_evidence)),
        (plan_body, "BODY_CODE", spec.body_code),
        (plan_body, "TITLE_PATTERN", pattern),
        (plan_body, "PROPOSED_BODY_NAME", spec.name),
        (plan_body, "PROPOSED_BODY_SLUG", spec.slug),
        (plan_body, "PROPOSED_BODY_TYPE", spec.body_type),
        (plan_body, "REGISTRY_EVIDENCE", tuple(spec.registry_evidence)),
    ]


@contextmanager
def bound_spec(spec: BodySpec) -> Iterator[BodySpec]:
    """Bind the reviewed plan machinery to one approved body, then restore it."""
    bindings = _bindings(spec)
    previous = [(module, name, getattr(module, name, None), hasattr(module, name))
                for module, name, _ in bindings]
    for module, name, value in bindings:
        setattr(module, name, value)
    try:
        yield spec
    finally:
        for module, name, old_value, existed in previous:
            if existed:
                setattr(module, name, old_value)
            else:
                delattr(module, name)
