#!/usr/bin/env python3
"""``temporal.py`` — the three-clock temporal contract (Stage 1 exit criterion 3).

`docs/KG-INFORMATION-MODEL.md` §10 ("Temporal contract") is authoritative.  It
distinguishes three clocks, and this module makes that contract machine-readable
and validated:

======================  ==========================================  ==================
Clock                   Meaning                                     Bounds validity?
======================  ==========================================  ==================
``valid_time``          When the claim was true in the civic world  **Yes**
``source_observation``  When the source asserted/recorded it       No
``ingestion_system``    When Poliscopic acquired/extracted/…        No
======================  ==========================================  ==================

Clock fields are **schema/assertion metadata**.  They are deliberately *not*
ontology: a field name here must never also be an entity type, role, outcome,
predicate, event type, or taxonomy leaf.  :func:`ontology_conflicts` enforces that.

Two properties are load-bearing and explicitly tested:

* **Observation timestamps are not validity bounds.**  ``observed_at`` and the
  ``first_observed_at``/``last_observed_at`` summaries record *when a source said
  something*; the model states they "do not imply validity bounds".  Only
  ``valid_from``/``valid_to`` bound validity, and :func:`require_validity_bound`
  refuses anything else.
* **No field claims a column that does not exist.**  Presence is never asserted
  here: :func:`schema_mapping` asks the authoritative schema contract
  (:data:`scripts.entities.schema_parity.REQUIRED_COLUMNS`) and labels each field
  ``present`` or ``planned``.  A field declared under ``fields`` must be present; a
  field declared under ``summary_fields``/``planned_fields`` must be planned.  Any
  mismatch is a contract violation.

Single source of truth
----------------------
This module owns the temporal *semantics* — which clock owns which field.  It does
not restate the schema (that is ``schema_parity``) nor the predicate requirement
tokens (those are read from ``relationships.PREDICATES``).  Adding a new
``temporal_requirement`` token to a predicate fails validation until this module
classifies it, so the two can never drift apart silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

from scripts.kg.registries.model import RegistryError, frozen

__all__ = [
    "CLOCKS",
    "CLOCK_BY_SLUG",
    "Clock",
    "ClockField",
    "assert_temporal_contract",
    "declared_temporal_requirements",
    "ingestion_fields",
    "is_validity_bound",
    "observation_fields",
    "ontology_conflicts",
    "require_observation_field",
    "require_validity_bound",
    "schema_mapping",
    "temporal_contract",
    "temporal_requirement_clocks",
    "validity_bound_fields",
    "validate_temporal_contract",
]


@dataclass(frozen=True)
class ClockField:
    """One schema field carrying a clock's timestamp.

    ``table``/``column`` name the *schema contract* location; whether that column
    currently exists is answered by :func:`schema_mapping`, never assumed here.
    """

    table: str
    column: str
    note: str = ""

    def as_key(self) -> tuple[str, str]:
        """The ``(table, column)`` identity used for distinctness checks."""
        return (self.table, self.column)


@dataclass(frozen=True)
class Clock:
    """One of the three temporal clocks and the fields that carry it."""

    slug: str
    meaning: str
    required_behavior: str
    fields: tuple[ClockField, ...]
    defines_validity_bounds: bool
    summary_fields: tuple[ClockField, ...] = ()
    planned_fields: tuple[ClockField, ...] = ()
    immutable_or_versioned: bool = False
    planned_note: str = ""

    def all_fields(self) -> tuple[ClockField, ...]:
        """Every field this clock mentions, current and planned."""
        return tuple(self.fields) + tuple(self.summary_fields) + tuple(self.planned_fields)


#: The three clocks, in the authoritative order of `KG-INFORMATION-MODEL.md` §10.
CLOCKS: tuple[Clock, ...] = (
    Clock(
        slug="valid_time",
        meaning="When the claim was true in the civic world",
        required_behavior=(
            "Set only from explicit evidence; bounded with valid_from/valid_to"
        ),
        fields=(
            ClockField("entity_relationships", "valid_from",
                       "Lower bound of the interval during which the claim held"),
            ClockField("entity_relationships", "valid_to",
                       "Upper bound of that interval; open when still true"),
        ),
        defines_validity_bounds=True,
    ),
    Clock(
        slug="source_observation_time",
        meaning="When the source asserted or recorded the claim",
        required_behavior=(
            "Store the exact source semantic time (e.g. meeting date or publication "
            "date); never promote it to a validity bound"
        ),
        fields=(
            ClockField("entity_relationships", "observed_at",
                       "The source-semantic time asserted for this claim"),
        ),
        defines_validity_bounds=False,
        summary_fields=(
            ClockField("entity_relationships", "first_observed_at",
                       "Earliest observation; a summary, not a validity bound"),
            ClockField("entity_relationships", "last_observed_at",
                       "Latest observation; a summary, not a validity bound"),
        ),
    ),
    Clock(
        slug="ingestion_system_time",
        meaning=(
            "When Poliscopic acquired, extracted, canonicalized, revised, or retired "
            "the assertion"
        ),
        required_behavior="Immutable history or a versioned audit record",
        fields=(),
        defines_validity_bounds=False,
        immutable_or_versioned=True,
        planned_note=(
            "No ingestion/system-time column is declared in the schema contract yet. "
            "The requirement is immutability/versioning (assertion history), not a "
            "timestamp column; none is claimed."
        ),
    ),
)

#: The clocks indexed by slug.
CLOCK_BY_SLUG: Mapping[str, Clock] = frozen({clock.slug: clock for clock in CLOCKS})

#: Temporal-requirement token -> the clock that satisfies it (``None`` = no clock).
#:
#: The token *vocabulary* is owned by the predicates; this mapping only classifies
#: it.  :func:`validate_temporal_contract` fails when the two disagree.
REQUIREMENT_CLOCKS: Mapping[str, str | None] = frozen({
    "none": None,
    "context_required": None,
    "known_scope": "valid_time",
    "valid_time_when_available": "valid_time",
    "observation_time_required": "source_observation_time",
})

#: Field names that must never be used as validity bounds.
NON_VALIDITY_TIMESTAMP_NOTE = (
    "observation/summary timestamps record when a source spoke; they do not imply "
    "validity"
)


def _clock(slug: str) -> Clock:
    """Return a clock by slug, raising when it is unknown."""
    try:
        return CLOCK_BY_SLUG[slug]
    except KeyError:
        raise RegistryError(f"unknown temporal clock {slug!r}") from None


def iter_declared_fields(clocks: Sequence[Clock] = CLOCKS) -> Iterator[tuple[str, ClockField]]:
    """Yield ``(clock_slug, field)`` for every field of the given clocks."""
    for clock in clocks:
        for field in clock.all_fields():
            yield clock.slug, field


def validity_bound_fields() -> tuple[ClockField, ...]:
    """The fields that bound validity (``valid_from``/``valid_to`` only)."""
    return _clock("valid_time").fields


def observation_fields() -> tuple[ClockField, ...]:
    """Observation-time fields, including the non-validity summaries."""
    clock = _clock("source_observation_time")
    return tuple(clock.fields) + tuple(clock.summary_fields)


def ingestion_fields() -> tuple[ClockField, ...]:
    """Ingestion/system-time fields (currently none declared)."""
    return _clock("ingestion_system_time").fields


def is_validity_bound(column: str) -> bool:
    """Whether ``column`` is one of the validity bounds."""
    return column in {field.column for field in validity_bound_fields()}


def require_validity_bound(column: str) -> ClockField:
    """Return the validity-bound field for ``column``, or raise.

    This is the guard that stops observation timestamps being treated as validity:
    ``observed_at`` and the first/last summaries are explicitly refused.
    """
    for field in validity_bound_fields():
        if field.column == column:
            return field
    if column in {field.column for field in observation_fields()}:
        raise RegistryError(
            f"{column!r} is an observation timestamp, not a validity bound: "
            f"{NON_VALIDITY_TIMESTAMP_NOTE}"
        )
    raise RegistryError(
        f"{column!r} is not a validity bound; validity is bounded only by "
        f"{sorted(field.column for field in validity_bound_fields())}"
    )


def require_observation_field(column: str) -> ClockField:
    """Return the observation field for ``column``, refusing validity bounds."""
    for field in observation_fields():
        if field.column == column:
            return field
    if is_validity_bound(column):
        raise RegistryError(
            f"{column!r} is a validity bound, not an observation timestamp"
        )
    raise RegistryError(f"{column!r} is not an observation-time field")


def declared_temporal_requirements() -> tuple[str, ...]:
    """Every temporal-requirement token the predicate registry actually declares.

    Read from the predicates rather than restated, so the classification below
    cannot drift out of step with the vocabulary it classifies.
    """
    from scripts.kg.registries.relationships import PREDICATES

    return tuple(sorted({entry.temporal_requirement for entry in PREDICATES.values()}))


def temporal_requirement_clocks() -> Mapping[str, str | None]:
    """The token-to-clock classification."""
    return REQUIREMENT_CLOCKS


def schema_mapping(clocks: Sequence[Clock] = CLOCKS) -> dict[str, Any]:
    """Bind the contract to the schema contract, labelling every field's presence.

    Presence is *asked*, never asserted: a field is ``present`` only when the
    authoritative :data:`schema_parity.REQUIRED_COLUMNS` requires that column, and
    ``planned`` otherwise.
    """
    clocks = tuple(clocks)
    from scripts.entities.schema_parity import REQUIRED_COLUMNS

    def status(field: ClockField) -> str:
        required = REQUIRED_COLUMNS.get(field.table, frozenset())
        return "present" if field.column in required else "planned"

    def entry(field: ClockField) -> dict[str, Any]:
        return {
            "table": field.table,
            "column": field.column,
            "status": status(field),
            "note": field.note,
        }

    return {
        "clocks": {
            clock.slug: {
                "fields": [entry(field) for field in clock.fields],
                "summary_fields": [entry(field) for field in clock.summary_fields],
                "planned_fields": [entry(field) for field in clock.planned_fields],
                "defines_validity_bounds": clock.defines_validity_bounds,
            }
            for clock in clocks
        },
        "present": sorted(
            f"{field.table}.{field.column}"
            for _, field in iter_declared_fields(clocks) if status(field) == "present"
        ),
        "planned": sorted(
            f"{field.table}.{field.column}"
            for _, field in iter_declared_fields(clocks) if status(field) == "planned"
        ),
    }


def ontology_conflicts(clocks: Sequence[Clock] = CLOCKS) -> list[str]:
    """Clock field names that are also registered as ontology vocabulary."""
    from scripts.kg.registries.entity_taxonomy import ENTITY_TYPES
    from scripts.kg.registries.events import BASE_OUTCOMES, EVENT_TYPES
    from scripts.kg.registries.relationships import PREDICATES
    from scripts.kg.registries.roles import ROLES

    ontology = (
        set(ENTITY_TYPES) | set(EVENT_TYPES) | set(ROLES) | set(PREDICATES)
        | set(BASE_OUTCOMES)
    )
    return sorted({
        field.column for _, field in iter_declared_fields(clocks) if field.column in ontology
    })


def validate_temporal_contract(
    clocks: Sequence[Clock] = CLOCKS,
    requirements: Mapping[str, str | None] | None = None,
    declared_tokens: Sequence[str] | None = None,
) -> list[str]:
    """Return every structural problem with the temporal contract (pure).

    ``clocks``/``requirements``/``declared_tokens`` default to the shipped
    contract; tests pass deliberately broken values to prove the failure modes.
    """
    clocks = tuple(clocks)
    requirements = REQUIREMENT_CLOCKS if requirements is None else requirements
    tokens = (
        set(declared_temporal_requirements()) if declared_tokens is None
        else set(declared_tokens)
    )
    known_slugs = {clock.slug for clock in clocks}
    problems: list[str] = []

    slugs = [clock.slug for clock in clocks]
    if len(set(slugs)) != len(slugs):
        problems.append(f"duplicate clock slug(s): {sorted(slugs)}")

    seen: dict[tuple[str, str], str] = {}
    for clock_slug, field in iter_declared_fields(clocks):
        position = seen.get(field.as_key())
        if position is not None:
            problems.append(
                f"field {field.table}.{field.column} is declared by both "
                f"{position} and {clock_slug}; clock fields must be distinct"
            )
        seen[field.as_key()] = clock_slug

    for clock in clocks:
        if clock.defines_validity_bounds:
            if len(clock.fields) < 2:
                problems.append(
                    f"{clock.slug} bounds validity but declares {len(clock.fields)} "
                    "field(s); a bounded interval needs both bounds"
                )
            if clock.summary_fields:
                problems.append(
                    f"{clock.slug} bounds validity and must not also carry summary fields"
                )
        else:
            if any(field.column.startswith("valid_") for field in clock.all_fields()):
                problems.append(
                    f"{clock.slug} does not bound validity but declares a valid_* field"
                )
            if clock.summary_fields and not clock.fields:
                problems.append(
                    f"{clock.slug} carries summary fields without a primary observation field"
                )
        if clock.slug == "ingestion_system_time" and not clock.immutable_or_versioned:
            problems.append("ingestion_system_time must be immutable or versioned")
        if not clock.planned_fields and clock.planned_note and clock.fields:
            continue  # a note-only clock with declared fields is fine
        if not clock.fields and not clock.planned_fields and not clock.planned_note:
            problems.append(
                f"{clock.slug} declares no field and no planned note; it would be unverifiable"
            )

    bound_clock = next((c for c in clocks if c.defines_validity_bounds), None)
    valid_bounds = {field.column for field in (bound_clock.fields if bound_clock else ())}
    observation_clock = next(
        (c for c in clocks if c.slug == "source_observation_time"), None
    )
    observed = tuple(observation_clock.fields) + tuple(observation_clock.summary_fields) \
        if observation_clock else ()
    for field in observed:
        if field.column in valid_bounds:
            problems.append(
                f"{field.column} is an observation timestamp and must not bound validity"
            )

    classified = set(requirements)
    for token in sorted(tokens - classified):
        problems.append(
            f"predicate temporal_requirement {token!r} is not classified by the "
            "temporal contract"
        )
    for token in sorted(classified - tokens):
        problems.append(
            f"temporal contract classifies {token!r}, which no predicate declares"
        )
    for token, clock_slug in sorted(requirements.items()):
        if clock_slug is not None and clock_slug not in known_slugs:
            problems.append(f"requirement {token!r} names unknown clock {clock_slug!r}")

    conflicts = ontology_conflicts(clocks)
    if conflicts:
        problems.append(f"clock field name(s) registered as ontology vocabulary: {conflicts}")

    mapping = schema_mapping(clocks)
    for clock in clocks:
        section = mapping["clocks"].get(clock.slug)
        if section is None:
            continue
        for field_entry in section["fields"]:
            if field_entry["status"] != "present":
                problems.append(
                    f"{clock.slug} declares {field_entry['table']}.{field_entry['column']} "
                    "as a current field, but the schema contract does not require it"
                )
        for key in ("summary_fields", "planned_fields"):
            for field_entry in section[key]:
                if field_entry["status"] == "present":
                    problems.append(
                        f"{clock.slug} lists {field_entry['table']}.{field_entry['column']} "
                        f"under {key}, but the schema contract already requires it"
                    )
    return problems


def assert_temporal_contract() -> None:
    """Validate the shipped contract; raise ``RegistryError`` on any violation."""
    problems = validate_temporal_contract()
    if problems:
        raise RegistryError("temporal contract invalid: " + "; ".join(problems))


def temporal_contract() -> dict[str, Any]:
    """The contract as deterministic, JSON-ready data."""
    return {
        "clocks": [
            {
                "slug": clock.slug,
                "meaning": clock.meaning,
                "required_behavior": clock.required_behavior,
                "defines_validity_bounds": clock.defines_validity_bounds,
                "immutable_or_versioned": clock.immutable_or_versioned,
                "planned_note": clock.planned_note,
                "fields": [
                    {"table": field.table, "column": field.column, "note": field.note}
                    for field in clock.fields
                ],
                "summary_fields": [
                    {"table": field.table, "column": field.column, "note": field.note}
                    for field in clock.summary_fields
                ],
                "planned_fields": [
                    {"table": field.table, "column": field.column, "note": field.note}
                    for field in clock.planned_fields
                ],
            }
            for clock in CLOCKS
        ],
        "requirement_clocks": dict(sorted(REQUIREMENT_CLOCKS.items())),
        "schema_mapping": schema_mapping(),
        "note": (
            "Clock fields are schema/assertion metadata, never ontology entities, "
            "roles, outcomes, predicates, or taxonomy leaves."
        ),
    }
