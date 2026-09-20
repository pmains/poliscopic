"""Validation receipts and orchestration reconciliation (Brief 018 Step 4).

Two units are tracked and never conflated:

- **ontology values** — vocabulary proposals that passed or failed registry
  classification (``attempted == accepted + rejected``);
- **database rows** — proposed, classified, committed, rolled back
  (``proposed == would_insert + would_update + replay_noop + unresolved`` and
  ``would_insert + would_update == committed + rolled_back``).

An ontology value is not a row, and a row is not an ontology value.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

from scripts.kg import registries as r
from scripts.kg.emission_models import (
    PROHIBITED_ENTITY_TYPES,
    PROHIBITED_PREDICATES,
    STATE_COLLECTING,
    STATE_SEALED,
    Rejection,
)


@dataclass
class ValidationReceipt:
    """Ontology-value accounting and row accounting, in separate units."""

    producer: str
    producer_version: str
    model_version: str
    registry_snapshot: str
    dry_run: bool = False
    state: str = STATE_COLLECTING

    # -- ontology values (unit: vocabulary proposals)
    values_attempted: int = 0
    values_accepted: int = 0
    values_rejected: int = 0

    # -- database rows (unit: rows, cumulative)
    rows_proposed: int = 0
    rows_would_insert: int = 0
    rows_would_update: int = 0
    rows_replay_noop: int = 0
    rows_unresolved: int = 0
    rows_committed: int = 0
    rows_rolled_back: int = 0

    failure: str | None = None
    observed: dict[str, dict[str, int]] = field(default_factory=dict)
    rejections: list[Rejection] = field(default_factory=list)
    #: Exact assertions reclassified from insert to replay after a write showed
    #: they already existed.  Named identities, never an aggregate count.
    reclassified_conflicts: list[dict[str, Any]] = field(default_factory=list)
    #: Proposals deliberately withheld because no approved representation exists.
    derived_excluded: int = 0
    derived_exclusion_reasons: list[str] = field(default_factory=list)

    @property
    def values_reconcile(self) -> bool:
        """Return True when every proposed value was accepted or rejected."""
        return self.values_attempted == self.values_accepted + self.values_rejected

    @property
    def classification_reconciles(self) -> bool:
        """Every proposed row is classified exactly once."""
        return self.rows_proposed == (
            self.rows_would_insert
            + self.rows_would_update
            + self.rows_replay_noop
            + self.rows_unresolved
        )

    @property
    def mutation_reconciles(self) -> bool | None:
        """Every proposed mutation is either committed or rolled back.

        Returns ``None`` for a dry run: the live mutation equation is not
        applicable when nothing was mutated, and ``False`` would read as failure.
        """
        if self.dry_run:
            return None
        return (
            self.rows_would_insert + self.rows_would_update
            == self.rows_committed + self.rows_rolled_back
        )

    @property
    def rows_reconcile(self) -> bool:
        """Return True when the applicable row equations hold exactly."""
        if not self.classification_reconciles:
            return False
        if self.dry_run:
            return self.rows_committed == 0 and self.rows_rolled_back == 0
        return bool(self.mutation_reconciles)

    def serialize(self) -> dict[str, Any]:
        """Return a JSON-safe receipt with both unit equations stated."""
        return {
            "producer": self.producer,
            "producer_version": self.producer_version,
            "model_version": self.model_version,
            "registry_snapshot": self.registry_snapshot,
            "state": self.state,
            "dry_run": self.dry_run,
            "failure": self.failure,
            "values": {
                "attempted": self.values_attempted,
                "accepted": self.values_accepted,
                "rejected": self.values_rejected,
                "equation": "attempted == accepted + rejected",
                "reconciles": self.values_reconcile,
            },
            "rows": {
                "proposed": self.rows_proposed,
                "would_insert": self.rows_would_insert,
                "would_update": self.rows_would_update,
                "replay_noop": self.rows_replay_noop,
                "unresolved": self.rows_unresolved,
                "committed": self.rows_committed,
                "rolled_back": self.rows_rolled_back,
                "classification_equation": (
                    "proposed == would_insert + would_update + replay_noop "
                    "+ unresolved"
                ),
                "mutation_equation": (
                    "would_insert + would_update == committed + rolled_back"
                ),
                "classification_reconciles": self.classification_reconciles,
                "mutation_reconciles": self.mutation_reconciles,
                "reconciles": self.rows_reconcile,
            },
            "observed": {
                category: dict(sorted(values.items()))
                for category, values in sorted(self.observed.items())
            },
            "rejections": [item.serialize() for item in self.rejections],
            "reclassified_conflicts": [
                dict(item) for item in self.reclassified_conflicts
            ],
            "derived_excluded": self.derived_excluded,
            "derived_exclusion_reasons": list(self.derived_exclusion_reasons),
        }


#: Observed categories that carry compatibility classifications.  Others
#: (assertion_class, evidence_class, participation_basis, model_version) are
#: registered vocabularies but are not compatibility-mapped, so classifying
#: them would report a false ``unmapped``.
CLASSIFIED_OBSERVATION_CATEGORIES: frozenset[str] = frozenset({
    "entity_type", "role", "relationship", "outcome", "edge_kind",
    "source_reference_type",
})


def reconcile_receipts(
    receipts: Iterable[Mapping[str, Any] | ValidationReceipt],
    *,
    expected_producers: Iterable[str],
    model_version: str | None = None,
    registry_snapshot: str | None = None,
) -> list[str]:
    """Return every reason the collected receipts cannot be trusted."""
    model_version = model_version or r.MODEL_VERSION
    registry_snapshot = registry_snapshot or r.snapshot_sha256()
    problems: list[str] = []
    prioritized: list[str] = []
    by_producer: dict[str, dict[str, Any]] = {}

    for entry in receipts:
        payload = entry.serialize() if isinstance(entry, ValidationReceipt) else dict(entry)
        name = str(payload.get("producer") or "")
        if not name:
            problems.append("receipt without a producer name")
            continue
        if name in by_producer:
            problems.append(f"duplicate receipt for producer {name}")
        by_producer[name] = payload

    for name in sorted(set(expected_producers)):
        if name not in by_producer:
            problems.append(f"missing validation receipt for producer {name}")

    for name, payload in sorted(by_producer.items()):
        if payload.get("model_version") != model_version:
            problems.append(
                f"{name}: model version {payload.get('model_version')} != {model_version}"
            )
        if payload.get("registry_snapshot") != registry_snapshot:
            problems.append(
                f"{name}: registry snapshot {payload.get('registry_snapshot')} "
                f"!= {registry_snapshot}"
            )

        values = payload.get("values") or {}
        rows = payload.get("rows") or {}
        rejections = payload.get("rejections") or []

        attempted = int(values.get("attempted") or 0)
        accepted = int(values.get("accepted") or 0)
        rejected = int(values.get("rejected") or 0)
        if attempted != accepted + rejected:
            problems.append(
                f"{name}: value accounting does not reconcile "
                f"(attempted {attempted} != accepted {accepted} + rejected {rejected})"
            )
        if rejected != len(rejections):
            problems.append(
                f"{name}: {rejected} rejections recorded but {len(rejections)} "
                "carry a reason (silently discarded rejects)"
            )

        proposed = int(rows.get("proposed") or 0)
        would_insert = int(rows.get("would_insert") or 0)
        would_update = int(rows.get("would_update") or 0)
        replay = int(rows.get("replay_noop") or 0)
        unresolved = int(rows.get("unresolved") or 0)
        committed = int(rows.get("committed") or 0)
        rolled = int(rows.get("rolled_back") or 0)

        classified = would_insert + would_update + replay + unresolved
        if proposed != classified:
            problems.append(
                f"{name}: classification does not reconcile exactly "
                f"(proposed {proposed} != would_insert {would_insert} "
                f"+ would_update {would_update} + replay_noop {replay} "
                f"+ unresolved {unresolved} = {classified})"
            )
        if payload.get("dry_run"):
            if committed or rolled:
                problems.append(
                    f"{name}: dry run mutated rows (committed {committed}, "
                    f"rolled_back {rolled})"
                )
        else:
            mutations = would_insert + would_update
            if mutations != committed + rolled:
                problems.append(
                    f"{name}: mutation does not reconcile exactly "
                    f"(would_insert {would_insert} + would_update {would_update} "
                    f"= {mutations} != committed {committed} "
                    f"+ rolled_back {rolled})"
                )

        if payload.get("state") != STATE_SEALED:
            problems.append(
                f"{name}: receipt is not sealed (state {payload.get('state')})"
            )
        if payload.get("failure"):
            prioritized.append(f"{name}: run failed ({payload['failure']})")

        for category, observed in (payload.get("observed") or {}).items():
            if category not in CLASSIFIED_OBSERVATION_CATEGORIES:
                continue
            for value in observed:
                status = r.classify_value(category, str(value))
                if status in ("quarantined", "unmapped"):
                    problems.append(f"{name}: observed {category} {value} is {status}")
        for value in (payload.get("observed") or {}).get("entity_type", {}):
            if value in PROHIBITED_ENTITY_TYPES:
                problems.append(f"{name}: emitted prohibited entity type {value}")
        for value in (payload.get("observed") or {}).get("relationship", {}):
            if value in PROHIBITED_PREDICATES:
                problems.append(f"{name}: emitted prohibited predicate {value}")

    return prioritized + problems
