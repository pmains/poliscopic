"""Typed proposal accounting for the entity-resolver subphases.

The resolver mutates the graph in three subphases.  Each produces *proposals*,
and every proposal must be classified **exactly once** — identically in dry and
live runs — as one of:

``would_insert``
    The proposal's dominant effect is creating canonical rows (a composite split
    that materialises a real person/organisation pair).

``would_update``
    The proposal's dominant effect is changing existing rows (a merge re-points
    mentions and edges, then marks the victim).

``replay_noop``
    The proposal would change nothing, so there is nothing to write.

``unresolved``
    The proposal cannot be applied safely: its endpoints cannot be identified, or
    the ontology value it would emit is not canonical emission vocabulary.

Two units are deliberately **not** conflated:

* a **proposal** is a resolution decision (one merge or one split), which is
  what the classification counts; and
* **row effects** (entities created, entities merged, mentions/edges re-pointed)
  are reported as separate additive counters.

Scan and comparison counts (``compared``) are never proposals.  A pair that
merely got scored is a comparison, not an assertion, and is reported separately.

Canonical validation is delegated to the authoritative registry checker in
:mod:`scripts.kg.emission_checks`; there is no second validator here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from scripts.kg.emission_checks import check

__all__ = [
    "PROPOSAL_REPLAY_NOOP",
    "PROPOSAL_UNRESOLVED",
    "PROPOSAL_WOULD_INSERT",
    "PROPOSAL_WOULD_UPDATE",
    "SubphaseProposals",
    "aggregate_proposals",
    "seal_resolver_receipt",
    "subphase_result",
    "is_canonical_emission",
]

PROPOSAL_WOULD_INSERT = "would_insert"
PROPOSAL_WOULD_UPDATE = "would_update"
PROPOSAL_REPLAY_NOOP = "replay_noop"
PROPOSAL_UNRESOLVED = "unresolved"


def is_canonical_emission(category: str, value: str) -> bool:
    """Whether ``value`` may be emitted for ``category`` right now.

    Delegates to the authoritative registry checker.  A compatibility-mapped,
    quarantined, or prohibited value is not emittable.
    """
    try:
        check(category, str(value))
    except Exception:
        return False
    return True


@dataclass(frozen=True)
class SubphaseProposals:
    """One resolver subphase's proposal accounting.

    ``proposals`` is satisfied by construction from the four classification
    counters, so the aggregate equation cannot silently drift.
    """

    subphase: str
    would_insert: int = 0
    would_update: int = 0
    replay_noop: int = 0
    unresolved: int = 0
    committed: int = 0
    created_entities: int = 0
    merged_entities: int = 0
    compared: int = 0
    refused_values: tuple[str, ...] = field(default_factory=tuple)
    emitted_values: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def proposed(self) -> int:
        """Proposals that received a classification."""
        return (self.would_insert + self.would_update
                + self.replay_noop + self.unresolved)

    @property
    def mutations(self) -> int:
        """Proposals whose dominant effect changes stored rows."""
        return self.would_insert + self.would_update

    def problems(self, *, dry_run: bool) -> list[str]:
        found: list[str] = []
        if dry_run and self.committed:
            found.append(
                f"{self.subphase}: dry run reported {self.committed} committed row(s)"
            )
        if not dry_run and self.committed > self.mutations:
            found.append(
                f"{self.subphase}: committed {self.committed} exceeds proposals "
                f"{self.mutations}"
            )
        return found


def aggregate_proposals(subphases: Sequence[SubphaseProposals], *,
                        dry_run: bool):
    """Aggregate per-subphase truth into one receipt accounting.

    Per-subphase detail is preserved by the caller; this only folds the
    classification counters, which is why no subphase has to know about the
    others.
    """
    from scripts.entities.phase_receipt import RowAccounting

    would_insert = sum(item.would_insert for item in subphases)
    would_update = sum(item.would_update for item in subphases)
    replay_noop = sum(item.replay_noop for item in subphases)
    unresolved = sum(item.unresolved for item in subphases)
    committed = 0 if dry_run else sum(item.committed for item in subphases)
    mutations = would_insert + would_update
    rolled_back = 0 if dry_run else max(0, mutations - committed)
    return RowAccounting(
        proposed=would_insert + would_update + replay_noop + unresolved,
        would_insert=would_insert,
        would_update=would_update,
        replay_noop=replay_noop,
        unresolved=unresolved,
        committed=committed,
        rolled_back=rolled_back,
    )


def seal_resolver_receipt(validator, subphases, *, dry_run: bool) -> dict:
    """Close the phase's validator into exactly one sealed resolver receipt.

    Row accounting comes from the per-subphase proposals; the validator has
    already validated every emitted bundle.  This is the single seal path used by
    both the phase and its tests, so there is no second receipt construction.
    """
    accounting = aggregate_proposals(subphases, dry_run=dry_run)
    validator.complete_validation()
    if not dry_run:
        validator.begin_writes()
    validator.classify_rows(
        would_insert=accounting.would_insert,
        would_update=accounting.would_update,
        replay_noop=accounting.replay_noop,
        unresolved=accounting.unresolved,
    )
    if not dry_run and accounting.committed:
        validator.commit(accounting.committed)
    return validator.seal().serialize()


def subphase_result(subphase: str, accounting: SubphaseProposals, **legacy) -> dict:
    """Return a subphase result carrying legacy counters and its accounting.

    Legacy counters keep their historical meaning exactly; the typed accounting
    travels under an internal key the orchestrator pops, so per-subphase truth is
    preserved without changing any existing result key.
    """
    return {**legacy, "_proposals": accounting}
