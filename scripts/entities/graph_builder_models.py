"""Typed contracts shared by graph-builder source and persistence modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Generator, Optional


@dataclass
class EntitySpec:
    """One canonical entity required by structured source data."""
    name: str
    normalized_name: str
    entity_type: str
    jurisdiction_id: int | None = None
    is_government: bool = False


@dataclass
class EdgeSpec:
    """One source-supported relationship expressed by entity identities."""
    from_entity_norm: str
    from_type: str
    to_entity_norm: str
    to_type: str
    relationship: str
    source_type: str
    source_id: int


@dataclass
class MentionSpec:
    """Text provenance for an entity emitted with a structured relationship."""
    entity_norm: str
    entity_type: str
    source_type: str
    source_id: int
    mention_text: str
    role: str | None = None
    confidence: float = 1.0


@dataclass
class SourceStats:
    """Per-source accounting for a materialization run or dry-run plan."""
    entities_attempted: int = 0
    entities_inserted: int = 0
    entities_planned: int = 0
    edges_attempted: int = 0
    edges_inserted: int = 0
    edges_planned: int = 0
    edge_replay_collisions: int = 0
    edges_unresolved_endpoint: int = 0
    edges_planned_unwritable: int = 0
    mentions_attempted: int = 0
    mentions_inserted: int = 0
    mentions_planned: int = 0
    mention_replay_collisions: int = 0
    mentions_unresolved_entity: int = 0
    mentions_planned_unwritable: int = 0
    new_ids: dict[tuple[str, str], int] = field(default_factory=dict)
    emitted_values: list[tuple[str, str]] = field(default_factory=list)

    _COUNTER_FIELDS = (
        "entities_attempted", "entities_inserted", "entities_planned",
        "edges_attempted", "edges_inserted", "edges_planned",
        "edge_replay_collisions", "edges_unresolved_endpoint",
        "edges_planned_unwritable",
        "mentions_attempted", "mentions_inserted", "mentions_planned",
        "mention_replay_collisions", "mentions_unresolved_entity",
        "mentions_planned_unwritable",
    )

    def add(self, other: "SourceStats") -> None:
        """Fold counters, cache-refresh ids, and emitted values into this one."""
        for field_name in self._COUNTER_FIELDS:
            setattr(self, field_name, getattr(self, field_name) + getattr(other, field_name))
        self.new_ids.update(other.new_ids)
        self.emitted_values.extend(other.emitted_values)

    def as_dict(self) -> dict[str, int]:
        """Return public counters without the internal id/value collections."""
        return {field_name: getattr(self, field_name) for field_name in self._COUNTER_FIELDS}

    def proposal_accounting(self, *, dry_run: bool) -> dict[str, int]:
        """Classify every proposal exactly once for the phase receipt.

        ``planned_unwritable`` proposals are counted as ``unresolved``: they were
        proposals whose referenced row had no id yet in this run, so no row was
        written for them.  Without this the live mutation equation
        (``would_insert + would_update == committed + rolled_back``) could not
        hold, and counting them as inserts would overstate what was written.
        """
        planned_total = self.entities_planned + self.edges_planned + self.mentions_planned
        unwritable = self.edges_planned_unwritable + self.mentions_planned_unwritable
        inserted_total = (self.entities_inserted + self.edges_inserted
                          + self.mentions_inserted)
        return {
            "proposed": (self.entities_attempted + self.edges_attempted
                         + self.mentions_attempted),
            "would_insert": planned_total - unwritable,
            "would_update": 0,
            "replay_noop": ((self.entities_attempted - self.entities_planned)
                            + self.edge_replay_collisions
                            + self.mention_replay_collisions),
            "unresolved": (self.edges_unresolved_endpoint
                           + self.mentions_unresolved_entity + unwritable),
            "committed": 0 if dry_run else inserted_total,
        }


class Source:
    """Contract for a structured query that emits typed graph specifications."""
    name: str = ""
    query: str = ""
    description: str = ""

    def produce(
        self, rows: list[dict[str, object]],
    ) -> Generator[
        tuple[Optional[EntitySpec], Optional[EdgeSpec], Optional[MentionSpec]],
        None,
        None,
    ]:
        """Yield entity, edge, and mention specifications for source rows."""
        raise NotImplementedError
