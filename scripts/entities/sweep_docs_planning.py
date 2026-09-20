"""sweep_docs_planning.py — pure row-assertion classification planning.

This module answers one question with no side effects:

    Given the candidates an extractor produced, plus what already exists in the
    database, which unique ontology row assertions would be *inserted* and which
    are *replay no-ops*?

It is deliberately **pure**.  It performs no mutation, issues no SQL, imports no
database driver, and touches no validator or receipt state.  Callers feed it
snapshots they have already read, and act on the plan themselves.

Why a plan at all
-----------------
The previous ``sweep_docs`` accounting filtered already-present rows *out* of
the count entirely, so an unchanged re-run reported ``proposed == 0`` and could
not distinguish "nothing to do" from "nothing was examined".  Classification is
therefore an explicit, inspectable artifact rather than an emergent property of
the write path.

Identity
--------
Classification keys on **exact assertion identity**, never on row counts:

    entity assertion   normalized_name + entity_type
    mention assertion  entity assertion identity + source_type + source_id
                       + role

Canonicalisation is *not* performed here.  The registries own vocabulary; this
module consumes values the caller has already canonicalised, so it can never
become a second, drifting source of truth.

Concurrency
-----------
A conflict is an *exact identity* event, not an aggregate shortfall.  This module
therefore offers :meth:`ClassificationPlan.reclassify_as_replay`, which moves one
named assertion from would-insert to replay no-op, and intentionally offers no
``proposed - committed`` style reconciliation.  Integration into the write path
is a separate, deliberate step.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

__all__ = [
    "DEFAULT_SOURCE_TYPE",
    "ClassificationPlan",
    "EntityAssertion",
    "ExtractedCandidate",
    "MentionAssertion",
    "PlanInvariantError",
    "build_classification_plan",
]

#: Source type recorded for mentions produced by the document sweep.
DEFAULT_SOURCE_TYPE = "supporting_document"


class PlanInvariantError(RuntimeError):
    """Raised when a plan violates one of its stated invariants."""


# ── Assertions ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EntityAssertion:
    """One unique entity row assertion.

    Identity is ``(normalized_name, entity_type)``.  ``entity_id`` is the
    canonical database identifier *when known*; it is excluded from equality and
    hashing (``compare=False``) precisely so that the same assertion discovered
    with and without a known id is still the same assertion.

    Canonicalisation happens upstream — ``normalized_name`` and ``entity_type``
    must already be canonical when they arrive.
    """

    normalized_name: str
    entity_type: str
    entity_id: int | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not str(self.normalized_name).strip():
            raise ValueError("EntityAssertion.normalized_name must be non-empty")
        if not str(self.entity_type).strip():
            raise ValueError("EntityAssertion.entity_type must be non-empty")
        if self.entity_id is not None and not isinstance(self.entity_id, int):
            raise ValueError("EntityAssertion.entity_id must be an int or None")

    @property
    def identity(self) -> tuple[str, str]:
        """Exact classification identity: normalized name plus entity type."""
        return (self.normalized_name, self.entity_type)

    @property
    def has_canonical_id(self) -> bool:
        return self.entity_id is not None


@dataclass(frozen=True)
class MentionAssertion:
    """One unique mention row assertion.

    Identity is the referenced entity assertion identity plus source type,
    source id, and role — a mention of the same entity from a second document,
    or under a second role, is a *different* assertion.
    """

    entity: EntityAssertion
    source_id: int
    role: str
    source_type: str = DEFAULT_SOURCE_TYPE

    def __post_init__(self) -> None:
        if not isinstance(self.entity, EntityAssertion):
            raise TypeError("MentionAssertion.entity must be an EntityAssertion")
        if not isinstance(self.source_id, int):
            raise ValueError("MentionAssertion.source_id must be an int")
        if not str(self.role).strip():
            raise ValueError("MentionAssertion.role must be non-empty")
        if not str(self.source_type).strip():
            raise ValueError("MentionAssertion.source_type must be non-empty")

    @property
    def identity(self) -> tuple[str, str, str, int, str]:
        """Exact classification identity for this mention assertion."""
        return (
            self.entity.normalized_name,
            self.entity.entity_type,
            self.source_type,
            self.source_id,
            self.role,
        )


@dataclass(frozen=True)
class ExtractedCandidate:
    """One raw extractor hit, before duplicate collapse.

    ``normalized_name`` and ``entity_type`` must already be canonical; ``role``
    must already be a canonical role from the registry vocabulary.

    ``name`` and ``confidence`` are *payload* attributes, not identity: they are
    used only to choose which duplicate wins a write, and never affect the
    assertion identity.
    """

    normalized_name: str
    entity_type: str
    role: str
    source_id: int
    source_type: str = DEFAULT_SOURCE_TYPE
    name: str = ""
    confidence: int = 0

    @classmethod
    def from_mapping(
        cls,
        candidate: Mapping[str, object],
        *,
        source_type: str = DEFAULT_SOURCE_TYPE,
    ) -> "ExtractedCandidate":
        """Build from an extractor candidate mapping.

        Accepts the ``normalized`` / ``entity_type`` / ``role`` / ``_source_id``
        keys the document sweep already produces.  ``_source_id`` is stored
        internally by the sweep, hence the leading underscore.
        """
        source_id = candidate.get("_source_id", candidate.get("source_id"))
        return cls(
            normalized_name=str(candidate["normalized"]),
            entity_type=str(candidate["entity_type"]),
            role=str(candidate["role"]),
            source_id=int(source_id),  # type: ignore[arg-type]
            source_type=source_type,
            name=str(candidate.get("name") or ""),
            confidence=int(candidate.get("confidence") or 0),
        )

    def entity_assertion(self) -> EntityAssertion:
        return EntityAssertion(self.normalized_name, self.entity_type)

    def mention_assertion(self) -> MentionAssertion:
        return MentionAssertion(
            entity=self.entity_assertion(),
            source_id=self.source_id,
            role=self.role,
            source_type=self.source_type,
        )


# ── Plan ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ClassificationPlan:
    """The classified result for one set of candidates.

    Each unique assertion appears in **exactly one** of the four buckets.
    Buckets are ordered deterministically by assertion identity, so the plan
    does not depend on the order rows were read.
    """

    entity_inserts: tuple[EntityAssertion, ...] = ()
    entity_replay_noops: tuple[EntityAssertion, ...] = ()
    mention_inserts: tuple[MentionAssertion, ...] = ()
    mention_replay_noops: tuple[MentionAssertion, ...] = ()

    # -- totals ----------------------------------------------------------

    @property
    def total_entity_proposed(self) -> int:
        return len(self.entity_inserts) + len(self.entity_replay_noops)

    @property
    def total_mention_proposed(self) -> int:
        return len(self.mention_inserts) + len(self.mention_replay_noops)

    @property
    def total_proposed(self) -> int:
        """Every unique assertion considered."""
        return self.total_entity_proposed + self.total_mention_proposed

    @property
    def total_would_insert(self) -> int:
        return len(self.entity_inserts) + len(self.mention_inserts)

    @property
    def total_replay_noop(self) -> int:
        return len(self.entity_replay_noops) + len(self.mention_replay_noops)

    # -- exact-identity lookup -------------------------------------------

    def classification_of(self, assertion: EntityAssertion | MentionAssertion) -> str:
        """Return ``"insert"`` or ``"replay_noop"`` for an exact assertion.

        Raises :class:`PlanInvariantError` if the assertion is not in the plan.
        """
        if isinstance(assertion, EntityAssertion):
            if assertion in self.entity_inserts:
                return "insert"
            if assertion in self.entity_replay_noops:
                return "replay_noop"
        elif isinstance(assertion, MentionAssertion):
            if assertion in self.mention_inserts:
                return "insert"
            if assertion in self.mention_replay_noops:
                return "replay_noop"
        else:
            raise TypeError(
                "classification_of expects an EntityAssertion or MentionAssertion"
            )
        raise PlanInvariantError(
            f"assertion {assertion.identity!r} is not present in this plan"
        )

    def reclassify_as_replay(
        self, assertion: EntityAssertion | MentionAssertion
    ) -> "ClassificationPlan":
        """Move one **exact** assertion from would-insert to replay no-op.

        Returns a new plan; raises if the assertion is not currently an insert.
        """
        if isinstance(assertion, EntityAssertion):
            if assertion not in self.entity_inserts:
                raise PlanInvariantError(
                    f"entity assertion {assertion.identity!r} is not a pending insert"
                )
            return ClassificationPlan(
                entity_inserts=_without(self.entity_inserts, assertion),
                entity_replay_noops=_sorted_unique(
                    (*self.entity_replay_noops, assertion)
                ),
                mention_inserts=self.mention_inserts,
                mention_replay_noops=self.mention_replay_noops,
            )
        if isinstance(assertion, MentionAssertion):
            if assertion not in self.mention_inserts:
                raise PlanInvariantError(
                    f"mention assertion {assertion.identity!r} is not a pending insert"
                )
            return ClassificationPlan(
                entity_inserts=self.entity_inserts,
                entity_replay_noops=self.entity_replay_noops,
                mention_inserts=_without(self.mention_inserts, assertion),
                mention_replay_noops=_sorted_unique(
                    (*self.mention_replay_noops, assertion)
                ),
            )
        raise TypeError(
            "reclassify_as_replay expects an EntityAssertion or MentionAssertion"
        )

    # -- invariants ------------------------------------------------------

    def check_invariants(self) -> None:
        """Raise :class:`PlanInvariantError` if any stated invariant is broken."""
        entity_ids = [a.identity for a in self.entity_inserts]
        entity_replay_ids = [a.identity for a in self.entity_replay_noops]
        mention_ids = [m.identity for m in self.mention_inserts]
        mention_replay_ids = [m.identity for m in self.mention_replay_noops]

        # No duplication inside a bucket.
        for label, ids in (
            ("entity_inserts", entity_ids),
            ("entity_replay_noops", entity_replay_ids),
            ("mention_inserts", mention_ids),
            ("mention_replay_noops", mention_replay_ids),
        ):
            if len(ids) != len(set(ids)):
                raise PlanInvariantError(f"duplicate assertion in {label}")

        # Every assertion in exactly one classification.
        for label, left, right in (
            ("entity", entity_ids, entity_replay_ids),
            ("mention", mention_ids, mention_replay_ids),
        ):
            overlap = set(left) & set(right)
            if overlap:
                raise PlanInvariantError(
                    f"{label} assertions classified twice: {sorted(overlap)!r}"
                )

        # Mention insertions must resolve to an entity in the same plan.
        known_entities = (
            set(entity_ids) | set(entity_replay_ids)
        )
        for mention in (*self.mention_inserts, *self.mention_replay_noops):
            if mention.entity.identity not in known_entities:
                raise PlanInvariantError(
                    f"mention {mention.identity!r} references an entity absent "
                    "from the plan"
                )

        # proposed == would_insert + replay_noop
        if self.total_proposed != self.total_would_insert + self.total_replay_noop:
            raise PlanInvariantError(
                f"proposed {self.total_proposed} != would_insert "
                f"{self.total_would_insert} + replay_noop {self.total_replay_noop}"
            )

        # Deterministic ordering.
        if tuple(self.entity_inserts) != _sorted_unique(self.entity_inserts):
            raise PlanInvariantError("entity_inserts is not in deterministic order")
        if tuple(self.entity_replay_noops) != _sorted_unique(self.entity_replay_noops):
            raise PlanInvariantError("entity_replay_noops is not in deterministic order")
        if tuple(self.mention_inserts) != _sorted_unique(self.mention_inserts):
            raise PlanInvariantError("mention_inserts is not in deterministic order")
        if tuple(self.mention_replay_noops) != _sorted_unique(
            self.mention_replay_noops
        ):
            raise PlanInvariantError(
                "mention_replay_noops is not in deterministic order"
            )


def _sorted_unique(items: Iterable) -> tuple:
    """Deduplicate by exact identity and order deterministically."""
    unique: dict[object, object] = {}
    for item in items:
        unique.setdefault(item.identity, item)
    return tuple(unique[key] for key in sorted(unique, key=_sort_key))


def _sort_key(identity: tuple) -> tuple:
    return tuple(str(part) for part in identity)


def _without(items: Sequence, target) -> tuple:
    return tuple(item for item in items if item != target)


# ── Builder ──────────────────────────────────────────────────────────────


def build_classification_plan(
    candidates: Iterable[ExtractedCandidate],
    existing_entities: Iterable[EntityAssertion] = (),
    existing_mentions: Iterable[MentionAssertion] = (),
) -> ClassificationPlan:
    """Classify unique assertions into would-insert and replay no-ops.

    Rules, in evaluation order:

    1. An entity assertion already known is a **replay no-op**; otherwise it is
       an **insert**.
    2. A mention assertion whose entity is already known is a **replay no-op**
       when that exact mention already exists, and an **insert** otherwise.
    3. A mention assertion whose entity is *created by this same plan* is always
       an **insert** — no mention can pre-exist for an entity that did not.

    Duplicate raw candidates collapse by exact assertion identity, so repeated
    extraction hits never inflate the totals.
    """
    known_entities: dict[tuple[str, str], EntityAssertion] = {}
    for entity in existing_entities:
        known_entities.setdefault(entity.identity, entity)

    known_mentions: dict[tuple, MentionAssertion] = {}
    for mention in existing_mentions:
        known_mentions.setdefault(mention.identity, mention)

    entity_inserts: list[EntityAssertion] = []
    entity_replays: list[EntityAssertion] = []
    mention_inserts: list[MentionAssertion] = []
    mention_replays: list[MentionAssertion] = []

    for candidate in candidates:
        entity = candidate.entity_assertion()
        mention = candidate.mention_assertion()

        existing_entity = known_entities.get(entity.identity)
        if existing_entity is None:
            entity_inserts.append(entity)
            # Entity is created in this plan, so its mention cannot already
            # exist and is definitionally an insert.
            mention_inserts.append(mention)
            continue

        entity_replays.append(
            EntityAssertion(
                normalized_name=existing_entity.normalized_name,
                entity_type=existing_entity.entity_type,
                entity_id=existing_entity.entity_id,
            )
        )
        if mention.identity in known_mentions:
            mention_replays.append(mention)
        else:
            mention_inserts.append(mention)

    plan = ClassificationPlan(
        entity_inserts=_sorted_unique(entity_inserts),
        entity_replay_noops=_sorted_unique(entity_replays),
        mention_inserts=_sorted_unique(mention_inserts),
        mention_replay_noops=_sorted_unique(mention_replays),
    )
    plan.check_invariants()
    return plan
