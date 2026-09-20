"""Pure decision logic for the Stage 1 extraction-link repair plan.

Kept free of database and I/O so the planning rules can be tested directly.  The
planning tool imports these rather than re-deriving them, so a test failure here is
a real failure of the planner.

The approved policy is that a later pass is authoritative, so an extraction's
correct target is the unique ``meeting_events`` row from that pass whose evidence
identity matches the extraction's own.  Two further conditions decide whether a
proposed set of link updates is executable at all:

* **target uniqueness** - two extractions may not propose the same event;
* **linkage uniqueness** - a target may not remain held by an extraction that is
  not itself being repointed.

The second condition is what a positional-shift repair silently violates when the
correct event is already occupied by a duplicate extraction of the same evidence.
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Collection, Mapping, Sequence

from scripts.entities.event_link_storage import (
    confidence_increases,
    storage_confidence,
)


def unique_later_pass_target(
    candidates: Sequence[Mapping[str, Any]],
    later_date: str | None,
) -> int | None:
    """The single earlier/later-pass candidate for an extraction, or ``None``.

    ``None`` means the evidence does not identify exactly one target, which the
    planner treats as ambiguous rather than guessing.
    """
    if later_date is not None:
        matching = [c for c in candidates if str(c.get("pass_date")) == later_date]
    else:
        matching = list(candidates)
    return int(matching[0]["event_id"]) if len(matching) == 1 else None


def duplicate_proposed_targets(proposed: Sequence[int]) -> list[int]:
    """Event ids that more than one extraction proposes to take."""
    counts = Counter(proposed)
    return sorted(event_id for event_id, count in counts.items() if count > 1)


def holder_conflicts(
    proposed: Mapping[int, int],
    holders: Mapping[int, Collection[int]],
    moving: Collection[int],
) -> list[int]:
    """Holders of a proposed target that are not themselves being repointed.

    A non-empty result means repointing would leave two extractions sharing one
    event, so the operation set is not executable as-is.
    """
    return sorted({
        holder
        for target in proposed.values()
        for holder in holders.get(target, ())
        if holder not in moving
    })


def operations_are_executable(
    proposed: Mapping[int, int],
    holders: Mapping[int, Collection[int]],
) -> bool:
    """True when a link-only repair achieves one event per extraction."""
    if duplicate_proposed_targets(list(proposed.values())):
        return False
    return not holder_conflicts(proposed, holders, set(proposed))


def shared_identity_holders(
    target: int,
    holders: Mapping[int, Collection[int]],
    identities: Mapping[int, Any],
    own_identity: Any,
    exclude: int,
) -> list[int]:
    """Holders of ``target`` whose evidence identity equals ``own_identity``.

    These are duplicate extractions of the same observation rather than unrelated
    claimants, which is why a link-only repair cannot resolve them.
    """
    return sorted(
        holder for holder in holders.get(target, ())
        if holder != exclude and identities.get(holder) == own_identity
    )


def participant_conflicts(
    sets: Sequence[Collection[tuple]],
) -> list[tuple]:
    """``(entity_id, role)`` keys whose confidence differs across participant sets.

    An identical ``(entity, role, confidence)`` in two sets is a duplicate to be
    deduplicated, not a conflict.  A differing confidence for the same
    ``(entity, role)`` is a conflict and must be excluded for human adjudication.
    """
    seen: dict[tuple, set] = {}
    for participant_set in sets:
        for entity_id, role, confidence in participant_set:
            seen.setdefault((entity_id, role), set()).add(confidence)
    return sorted(key for key, values in seen.items() if len(values) > 1)


def plan_preconditions_hold(proof: Mapping[str, Any]) -> bool:
    """Whether a joint dedup plan is executable as recorded.

    The plan is executable only when the component population reconciles exactly and
    no component classified as safe has an unhandled survivor-evidence problem.
    """
    return (
        proof.get("components_reconciles") is True
        and proof.get("member_events_reconciles") is True
        and proof.get("unhandled_safe_components", 0) == 0
        and proof.get("zero_effects_outside_components") is True
    )


def classify_participant_conflict(
    event_level_basis_identical: bool,
    participant_provenance_recorded: bool,
) -> tuple[str, str]:
    """Classify one ``(entity, role)`` participant-confidence conflict.

    Maximum confidence is recommended **only** when the participant's own evidence
    basis is recorded and identical.  Because ``event_participants`` stores no
    provenance, an absent basis can never justify an automatic choice - it holds the
    conflict for human adjudication instead.
    """
    if not participant_provenance_recorded:
        return ("requires_individual_adjudication",
                "participant-level evidence basis is absent from the schema, so an "
                "identical basis cannot be proven")
    if not event_level_basis_identical:
        return ("requires_individual_adjudication",
                "event-level evidence basis differs across members")
    return ("proposed_retain_max_confidence",
            "identity, role, canonical event identity and evidence basis are identical")


def resolve_conflict_max_confidence(confidences: Sequence[Any]) -> float | None:
    """The pragmatic score-selection rule: retain the maximum existing confidence.

    This **selects a score**; it makes no claim that the two evidence bases are
    equivalent.  Participant provenance was never stored, so equivalence cannot be
    asserted from the data - only a deterministic tie-break can be applied, and the
    caller must record it as such.
    """
    values = [storage_confidence(c) for c in confidences if c is not None]
    return max(values) if values else None


def conflict_rule_fits(
    conflict_keys: Sequence[tuple],
    layers: Sequence[Mapping[int, Any]],
) -> bool:
    """Whether every conflict key matches identity and role in **all** members.

    ``layers`` is one ``{entity_id: {role: confidence}}`` map per component member.
    A key missing a role in any member does not satisfy "canonical event, entity and
    role match", so that component keeps an exception rather than being resolved by
    the maximum-confidence rule.
    """
    if not conflict_keys:
        return False
    return all(
        all(role in member_layer.get(entity_id, {}) for member_layer in layers)
        for entity_id, role in conflict_keys
    )


def effective_confidence_updates(
    resolutions: Sequence[Mapping[str, Any]],
    current_by_key: Mapping[tuple, float],
) -> list[Mapping[str, Any]]:
    """Resolutions that actually change a stored value, under float4 semantics.

    Shared by the plan generator and the scratch executor so the predicted and the
    executed row-touch counts cannot drift: both call this with the same predicate
    (:func:`confidence_increases`, i.e. compare after float32 normalization and
    update only when strictly greater).  A resolution whose retained value is
    already the stored value is a replay, not an update, and is dropped.
    """
    effective: list[Mapping[str, Any]] = []
    for resolution in resolutions:
        key = (int(resolution["survivor_id"]), int(resolution["entity_id"]),
               str(resolution["role_in_event"]))
        current = current_by_key.get(key)
        target = resolution.get("retained_confidence")
        if current is None or target is None:
            continue
        if confidence_increases(target, current):
            effective.append(resolution)
    return effective


def stale_baseline_keys(
    plan_baseline: Mapping[str, Any],
    restored: Mapping[str, Any],
    development: Mapping[str, Any],
) -> list[str]:
    """Keys where a restored dump disagrees with the plan or the live baseline.

    Only keys present in the comparison baselines are checked, so a count the plan
    never recorded (such as a schema-era quarantine count) does not masquerade as a
    mismatch.  Schema errors reported by the caller are always included, because a
    column that does not exist proves the dump predates a migration.  A non-empty
    result means the simulation must not run.
    """
    stale = {key for key in plan_baseline if restored.get(key) != plan_baseline.get(key)}
    stale |= {key for key in development
              if key != "__schema_errors" and restored.get(key) != development.get(key)}
    stale |= set(restored.get("__schema_errors") or {})
    return sorted(stale)
