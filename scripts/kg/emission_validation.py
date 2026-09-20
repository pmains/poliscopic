"""Registry-aware emission validator and lifecycle (Brief 018 Step 4).

Every ontology-bearing value passes through here *before* mutation
classification and before any database write, in both dry and live mode.
Replay/no-op candidates are validated too: they are proposed emissions even when
the write is a no-op.

Lifecycle::

    collecting -> validation_complete -> writing -> (next batch) collecting
                                                         |
                                                    sealed / failed

Per-category registry checks live in :mod:`scripts.kg.emission_checks`; bundle
completeness rules live in :mod:`scripts.kg.emission_bundles`.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from scripts.kg import registries as r
from scripts.kg.emission_bundles import (
    EmissionBundle,
    bundle_problems,
    required_bundle_categories,
)
from scripts.kg.emission_checks import check
from scripts.kg.emission_models import (
    CATEGORIES,
    STATE_COLLECTING,
    STATE_FAILED,
    STATE_SEALED,
    STATE_VALIDATION_COMPLETE,
    STATE_WRITING,
    BoundaryStateError,
    EmissionError,
    Rejection,
)
from scripts.kg.emission_receipts import ValidationReceipt


class EmissionValidator:
    """Registry-aware gate enforcing the emission lifecycle."""

    def __init__(
        self,
        producer: str,
        producer_version: str = "unknown",
        *,
        dry_run: bool = False,
    ) -> None:
        if not producer:
            raise EmissionError("producer name is required")
        self.receipt = ValidationReceipt(
            producer=producer,
            producer_version=producer_version,
            model_version=r.MODEL_VERSION,
            registry_snapshot=r.snapshot_sha256(),
            dry_run=dry_run,
        )
        self._batch_mark: tuple[int, ...] = (0, 0, 0, 0, 0, 0, 0)

    # -- lifecycle -------------------------------------------------------

    @property
    def state(self) -> str:
        return self.receipt.state

    def _require(self, *allowed: str, action: str) -> None:
        if self.state not in allowed:
            raise BoundaryStateError(
                f"{self.receipt.producer}: cannot {action} in state {self.state} "
                f"(allowed: {', '.join(allowed)})"
            )

    def _batch_deltas(self) -> tuple[int, ...]:
        """Row totals accumulated since the current batch began."""
        mark, receipt = self._batch_mark, self.receipt
        return (
            receipt.rows_proposed - mark[0],
            receipt.rows_would_insert - mark[1],
            receipt.rows_would_update - mark[2],
            receipt.rows_replay_noop - mark[3],
            receipt.rows_unresolved - mark[4],
            receipt.rows_committed - mark[5],
            receipt.rows_rolled_back - mark[6],
        )

    def _mark_batch(self) -> None:
        self._batch_mark = (
            self.receipt.rows_proposed,
            self.receipt.rows_would_insert,
            self.receipt.rows_would_update,
            self.receipt.rows_replay_noop,
            self.receipt.rows_unresolved,
            self.receipt.rows_committed,
            self.receipt.rows_rolled_back,
        )

    def start_batch(self) -> None:
        """Begin a new collect/validate/write cycle.

        A batch may not be abandoned: the preceding batch must reconcile before
        the lifecycle returns to collecting.
        """
        if self.state in (STATE_WRITING, STATE_VALIDATION_COMPLETE):
            (proposed, would_insert, would_update, replay, unresolved,
             committed, rolled_back) = self._batch_deltas()
            classified = would_insert + would_update + replay + unresolved
            if proposed != classified:
                raise BoundaryStateError(
                    f"{self.receipt.producer}: cannot start a new batch while the "
                    f"previous one is unreconciled (proposed {proposed} != "
                    f"classified {classified})"
                )
            if self.receipt.dry_run:
                if committed or rolled_back:
                    raise BoundaryStateError(
                        f"{self.receipt.producer}: cannot start a new batch after a "
                        f"dry run mutated rows (committed {committed}, "
                        f"rolled_back {rolled_back})"
                    )
            else:
                mutations = would_insert + would_update
                if mutations != committed + rolled_back:
                    raise BoundaryStateError(
                        f"{self.receipt.producer}: cannot start a new batch with "
                        f"classified but unaccounted mutations "
                        f"(would_insert {would_insert} + would_update {would_update} "
                        f"= {mutations} != committed {committed} "
                        f"+ rolled_back {rolled_back})"
                    )
            self.receipt.state = STATE_COLLECTING
        self._require(STATE_COLLECTING, action="start a batch")
        self._mark_batch()

    def complete_validation(self) -> None:
        self._require(STATE_COLLECTING, action="complete validation")
        self.receipt.state = STATE_VALIDATION_COMPLETE

    def begin_writes(self) -> None:
        self._require(STATE_VALIDATION_COMPLETE, action="begin writes")
        self.receipt.state = STATE_WRITING

    # -- ontology value accounting ---------------------------------------

    def reject(
        self, category: str, value: Any, reason: str, *, source: str = "unknown",
    ) -> None:
        """Record a refused emission (counted, retained, never dropped)."""
        self.receipt.values_attempted += 1
        self.receipt.values_rejected += 1
        self.receipt.rejections.append(Rejection(
            category=category, value="<none>" if value is None else str(value),
            reason=reason, source=source,
        ))

    def _observe(self, category: str, value: str) -> None:
        bucket = self.receipt.observed.setdefault(category, {})
        bucket[value] = bucket.get(value, 0) + 1

    def _accept(self, category: str, value: str) -> str:
        self.receipt.values_attempted += 1
        self.receipt.values_accepted += 1
        self._observe(category, value)
        return value

    # -- row accounting --------------------------------------------------

    def classify_rows(
        self,
        *,
        would_insert: int = 0,
        would_update: int = 0,
        replay_noop: int = 0,
        unresolved: int = 0,
    ) -> None:
        """Classify proposed rows exactly once (cumulative)."""
        self._require(
            STATE_VALIDATION_COMPLETE, STATE_WRITING, action="classify rows",
        )
        would_insert = int(would_insert)
        would_update = int(would_update)
        replay_noop = int(replay_noop)
        unresolved = int(unresolved)
        self.receipt.rows_would_insert += would_insert
        self.receipt.rows_would_update += would_update
        self.receipt.rows_replay_noop += replay_noop
        self.receipt.rows_unresolved += unresolved
        self.receipt.rows_proposed += (
            would_insert + would_update + replay_noop + unresolved
        )

    def reclassify_assertion(self, assertion: Any, reason: str = "") -> None:
        """Reclassify exactly one **named** assertion as a replay no-op.

        Called only when a write reveals that this specific assertion already
        exists, i.e. a genuine concurrency conflict.  The assertion is named, so
        the reclassification is exact.

        There is deliberately no count-based variant: an aggregate
        ``proposed - committed`` shortfall cannot identify *which* assertion
        raced, so it must never be used to drive this accounting.
        """
        identity = getattr(assertion, "identity", None)
        if identity is None:
            raise TypeError(
                f"{self.receipt.producer}: reclassify_assertion requires an "
                "EntityAssertion or MentionAssertion (something with .identity)"
            )
        if self._batch_deltas()[1] < 1:
            raise BoundaryStateError(
                f"{self.receipt.producer}: cannot reclassify assertion "
                f"{tuple(identity)!r} as replay; this batch classified no inserts"
            )
        self.receipt.rows_would_insert -= 1
        self.receipt.rows_replay_noop += 1
        self.receipt.reclassified_conflicts.append({
            "identity": list(identity),
            "kind": type(assertion).__name__,
            "reason": reason,
        })

    def commit(self, count: int) -> None:
        """Record rows actually persisted by a committed transaction."""
        self._require(STATE_WRITING, action="commit rows")
        self.receipt.rows_committed += int(count)

    def rollback(self, count: int, reason: str) -> None:
        """Record mutations lost to a rolled-back transaction and fail the run.

        Cumulative proposed/classified totals are never reduced: a rolled-back
        mutation is *explained* by ``rows_rolled_back``.
        """
        self._require(
            STATE_VALIDATION_COMPLETE, STATE_WRITING, action="rollback",
        )
        self.receipt.rows_rolled_back += int(count)
        self.fail(reason)

    def fail(self, reason: str) -> None:
        self.receipt.failure = reason
        self.receipt.state = STATE_FAILED

    def note_derived_exclusion(self, count: int, reason: str) -> None:
        """Record proposals withheld because they are derived, not observed."""
        if not count:
            return
        self.receipt.derived_excluded += int(count)
        if reason not in self.receipt.derived_exclusion_reasons:
            self.receipt.derived_exclusion_reasons.append(reason)

    def seal(self) -> ValidationReceipt:
        """Close the receipt.  A failed run still seals, so prior commits stay visible."""
        self.receipt.state = STATE_SEALED
        return self.receipt

    # -- validation API --------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Expose ``validator.<category>()`` which checks and records acceptance."""
        if name in CATEGORIES:
            def record(value: Any, *, source: str = "unknown", **criteria: Any) -> str:
                return self._accept(name, check(name, value, **criteria))
            return record
        raise AttributeError(name)

    def _check(self, category: str, value: Any, **criteria: Any) -> str:
        """Run one pure check, raising on failure and recording nothing."""
        return check(category, value, **criteria)

    def validate(self, category: str, value: Any, **kwargs: Any) -> str | None:
        """Dispatch to a check, recording or raising on failure.

        Only legal while collecting: validation must precede mutation
        classification and writes.
        """
        if category not in CATEGORIES:
            raise EmissionError(f"unknown validation category {category}")
        self._require(STATE_COLLECTING, action=f"validate {category}")
        source = kwargs.pop("source", "unknown")
        try:
            return self._accept(category, self._check(category, value, **kwargs))
        except EmissionError as error:
            self.reject(category, value, str(error), source=source)
            return None

    # -- bundles ---------------------------------------------------------

    @staticmethod
    def _bundle_criteria(bundle: EmissionBundle, category: str) -> dict[str, Any]:
        if category == "role":
            return {
                "context_class": bundle.context_class,
                "assertion_kind": bundle.kind,
            }
        if category == "relationship":
            return {"from_class": bundle.from_class, "to_class": bundle.to_class}
        return {}

    def validate_bundle(
        self, bundle: EmissionBundle, *, source: str = "unknown",
    ) -> dict[str, str]:
        """Validate a complete bundle atomically.

        Every component is validated into staged state first.  If *any* component
        fails, zero accepted observations are committed and exactly one bundle
        rejection is recorded.  Only when every component passes are the accepted
        observations merged into the receipt.
        """
        self._require(STATE_COLLECTING, action="validate bundle")
        problems = bundle_problems(bundle)
        if problems:
            self.reject(
                f"bundle:{bundle.kind}", bundle.kind, "; ".join(problems),
                source=source,
            )
            raise EmissionError(
                f"{bundle.kind} bundle is invalid: " + "; ".join(problems)
            )

        staged: dict[str, str] = {}
        staged_observations: list[tuple[str, str]] = []
        failures: list[str] = []

        for category in required_bundle_categories(
            bundle.kind, bundle.assertion_class
        ):
            if category == "derived_inputs":
                staged[category] = ",".join(
                    key.digest for key in bundle.derived_inputs
                )
                continue
            value = getattr(bundle, category)
            try:
                canonical = self._check(
                    category, value, **self._bundle_criteria(bundle, category)
                )
            except EmissionError as error:
                failures.append(f"{category}={value!r}: {error}")
                continue
            staged[category] = canonical
            staged_observations.append((category, canonical))

        if bundle.outcome:
            # Base and qualifier are validated and observed separately, so a
            # qualified outcome is never certified as one raw string.
            try:
                canonical_base = self._check("outcome", bundle.outcome)
            except EmissionError as error:
                failures.append(f"outcome={bundle.outcome!r}: {error}")
            else:
                staged["outcome"] = canonical_base
                staged_observations.append(("outcome", canonical_base))
                resolved = bundle.outcome_qualifier
                if resolved is None:
                    resolved = r.canonicalize_outcome(bundle.outcome).qualifier
                if resolved is not None:
                    try:
                        canonical_qualifier = self._check(
                            "outcome_qualifier", resolved
                        )
                    except EmissionError as error:
                        failures.append(
                            f"outcome_qualifier={resolved!r}: {error}"
                        )
                    else:
                        staged["outcome_qualifier"] = canonical_qualifier
                        staged_observations.append(
                            ("outcome_qualifier", canonical_qualifier)
                        )

        if failures:
            # Commit nothing that passed earlier, and record exactly one
            # rejection for the bundle as a whole.
            self.reject(
                f"bundle:{bundle.kind}", bundle.kind,
                "bundle refused: " + "; ".join(failures), source=source,
            )
            raise EmissionError(
                f"{bundle.kind} bundle refused: " + "; ".join(failures)
            )

        for category, canonical in staged_observations:
            self.receipt.values_attempted += 1
            self.receipt.values_accepted += 1
            self._observe(category, canonical)
        return staged


def emit_validated(
    validator: EmissionValidator,
    category: str,
    candidates: Iterable[Any],
    *,
    to_value: Any,
    source: str = "unknown",
    strict: bool = True,
    **criteria: Any,
) -> list[tuple[Any, str]]:
    """Apply the standard emission pattern to a batch of candidates.

    detect candidate -> express canonically -> validate -> accepted or refused.
    Accepted candidates are returned paired with their canonical value so the
    caller writes canonical vocabulary.  Refusals are recorded and, by default,
    fail closed before anything is written.
    """
    accepted: list[tuple[Any, str]] = []
    refused: list[Rejection] = []
    for candidate in candidates:
        value = to_value(candidate) if callable(to_value) else candidate
        canonical = validator.validate(category, value, source=source, **criteria)
        if canonical is None:
            refused.append(validator.receipt.rejections[-1])
            continue
        accepted.append((candidate, canonical))
    if refused and strict:
        reasons = "; ".join(f"{item.value} ({item.reason})" for item in refused[:5])
        raise EmissionError(
            f"{validator.receipt.producer}: refused {len(refused)} {category} "
            f"candidate(s): {reasons}"
        )
    return accepted
