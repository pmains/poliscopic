"""Phase-level composite receipt envelope for multi-domain producers.

Some phases emit over more than one assertion *domain*.  Normalizing extractions
into canonical events and linking events into the entity graph have different
value vocabularies and different row units.  Summing them into a single assertion
stream would invent numbers that describe nothing, so instead each domain keeps
its own canonical receipt and the phase exposes one **envelope** that binds them.

This module owns the envelope contract, and it is used by both the producer and
the orchestration boundary — so there is exactly one definition of a trustworthy
envelope rather than two that can drift.

What the envelope binds:

* the exact required component steps and the steps this run selected;
* dry/live mode;
* the phase's declared producer version, ``MODEL_VERSION``, and registry snapshot;
* component coverage and singularity; and
* the component receipts themselves, each reconciled independently through
  :func:`scripts.kg.emission_receipts.reconcile_receipts`.

Nothing here re-implements ontology validation or reconciliation.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "ENVELOPE_KIND",
    "REQUIRED_COMPONENT_STEPS",
    "build_envelope",
    "component_problems",
    "envelope_problems",
    "is_envelope",
]

#: Marker distinguishing an envelope from a single canonical receipt.
ENVELOPE_KIND = "phase-envelope"

#: Steps that must each produce their own canonical receipt.  Ordered for
#: deterministic reporting.
REQUIRED_COMPONENT_STEPS: tuple[str, ...] = ("normalize", "link")

#: Fields every component receipt must carry to be considered well-formed.
_REQUIRED_RECEIPT_FIELDS = (
    "producer", "producer_version", "model_version", "registry_snapshot",
    "state", "dry_run", "values", "rows",
)

#: Lifecycle state a component must have reached.
_SEALED = "sealed"


def is_envelope(payload: Any) -> bool:
    """Whether ``payload`` is a phase envelope rather than a single receipt."""
    return isinstance(payload, Mapping) and payload.get("kind") == ENVELOPE_KIND


def build_envelope(
    *,
    producer: str,
    producer_version: str,
    dry_run: bool,
    model_version: str,
    registry_snapshot: str,
    selected_steps: Iterable[str],
    components: Sequence[tuple[str, Mapping[str, Any]]],
    required_steps: Sequence[str] = REQUIRED_COMPONENT_STEPS,
) -> dict[str, Any]:
    """Build the phase envelope from already-sealed component receipts.

    ``components`` is a sequence of ``(step, receipt)`` pairs rather than a
    mapping, so a duplicated component step is *detectable* instead of being
    silently collapsed by dict construction.
    """
    required = tuple(required_steps)
    selected = tuple(selected_steps)

    ordered: list[str] = []
    mapped: dict[str, Mapping[str, Any]] = {}
    duplicates: list[str] = []
    for step, receipt in components:
        name = str(step)
        if name in mapped:
            duplicates.append(name)
            continue
        ordered.append(name)
        mapped[name] = receipt

    problems: list[str] = []
    if duplicates:
        problems.append(f"duplicate component receipt(s): {sorted(set(duplicates))}")
    expected = [step for step in required if step in selected]
    missing = sorted(set(expected) - set(mapped))
    if missing:
        problems.append(f"missing component receipt(s): {missing}")
    unexpected = sorted(set(mapped) - set(required))
    if unexpected:
        problems.append(f"unexpected component step(s): {unexpected}")

    return {
        "kind": ENVELOPE_KIND,
        "producer": producer,
        "producer_version": producer_version,
        "model_version": model_version,
        "registry_snapshot": registry_snapshot,
        "dry_run": bool(dry_run),
        "required_steps": list(required),
        "selected_steps": list(selected),
        "component_steps": ordered,
        "components": dict(mapped),
        "coverage_complete": not missing and not duplicates,
        "problems": problems,
    }


def component_problems(
    step: str,
    receipt: Any,
    *,
    dry_run: bool,
    model_version: str,
    registry_snapshot: str,
    declared_version: str | None,
    reconcile: Callable[..., list[str]],
    allowed_rejections: int = 0,
) -> list[str]:
    """Reasons a single component receipt cannot be trusted.

    Each reason is prefixed with the step so a failure names the exact component.
    """
    found: list[str] = []
    if not isinstance(receipt, Mapping):
        return [f"{step}: component receipt is {type(receipt).__name__}, not an object"]

    absent = [field for field in _REQUIRED_RECEIPT_FIELDS if field not in receipt]
    if absent:
        return [f"{step}: component receipt is partial, missing {absent}"]

    if receipt.get("state") != _SEALED:
        found.append(f"{step}: component receipt is not sealed (state "
                     f"{receipt.get('state')!r})")
    if bool(receipt.get("dry_run")) != bool(dry_run):
        found.append(
            f"{step}: component dry_run={bool(receipt.get('dry_run'))} does not "
            f"match the phase dry_run={bool(dry_run)}")
    if declared_version is None:
        found.append(f"{step}: component declares no declared version")
    elif str(receipt.get("producer_version")) != declared_version:
        found.append(
            f"{step}: component producer_version "
            f"{receipt.get('producer_version')!r} != declared {declared_version!r}")
    if receipt.get("failure"):
        found.append(f"{step}: component run failed ({receipt.get('failure')})")

    rejected = int((receipt.get("values") or {}).get("rejected") or 0)
    if rejected > allowed_rejections:
        found.append(
            f"{step}: component rejected {rejected} value(s), permitted "
            f"{allowed_rejections}")

    reconciliation = reconcile(
        [dict(receipt)],
        expected_producers=[str(receipt.get("producer"))],
        model_version=model_version,
        registry_snapshot=registry_snapshot,
    )
    for problem in reconciliation:
        found.append(f"{step}: component does not reconcile: {problem}")
    return found


def envelope_problems(
    envelope: Any,
    *,
    producer: str,
    declared_version: str | None,
    dry_run: bool,
    model_version: str,
    registry_snapshot: str,
    component_versions: Mapping[str, str],
    reconcile: Callable[..., list[str]],
    allowed_rejections: int = 0,
    require_complete_coverage: bool = True,
) -> list[str]:
    """Reasons a phase envelope cannot be trusted.

    Fails closed on a malformed envelope, a missing/duplicate/unexpected
    component, a component that is partial, unsealed, failed, rejected,
    unreconciled, or bound to the wrong mode, version, model, or snapshot.
    """
    if not is_envelope(envelope):
        return ["envelope is not a phase envelope"]

    problems: list[str] = []
    if str(envelope.get("producer")) != producer:
        problems.append(
            f"envelope producer {envelope.get('producer')!r} != phase {producer!r}")
    if declared_version is None:
        problems.append(f"phase {producer!r} has no declared version")
    elif str(envelope.get("producer_version")) != declared_version:
        problems.append(
            f"envelope producer_version {envelope.get('producer_version')!r} != "
            f"declared {declared_version!r}")
    if envelope.get("model_version") != model_version:
        problems.append(
            f"envelope model_version {envelope.get('model_version')!r} != "
            f"{model_version!r}")
    if envelope.get("registry_snapshot") != registry_snapshot:
        problems.append(
            f"envelope registry_snapshot {envelope.get('registry_snapshot')!r} != "
            f"{registry_snapshot!r}")
    if bool(envelope.get("dry_run")) != bool(dry_run):
        problems.append(
            f"envelope dry_run={bool(envelope.get('dry_run'))} does not match the "
            f"phase dry_run={bool(dry_run)}")

    for problem in envelope.get("problems") or []:
        problems.append(str(problem))

    required = tuple(envelope.get("required_steps") or ())
    selected = tuple(envelope.get("selected_steps") or ())
    if require_complete_coverage and set(selected) != set(required):
        missing = sorted(set(required) - set(selected))
        problems.append(
            f"phase run did not select every required component step: {missing}")

    steps = tuple(envelope.get("component_steps") or ())
    if len(steps) != len(set(steps)):
        problems.append(f"duplicate component steps: {list(steps)}")
    if not envelope.get("coverage_complete", False):
        problems.append("envelope reports incomplete component coverage")

    components = envelope.get("components") or {}
    if not isinstance(components, Mapping):
        problems.append("envelope components must be a mapping")
        return problems

    expected = [step for step in required if step in selected]
    for step in sorted(set(expected) - set(components)):
        problems.append(f"missing component receipt for step {step!r}")
    for step in sorted(set(components) - set(required)):
        problems.append(f"unexpected component step {step!r}")

    for step in sorted(components):
        problems.extend(component_problems(
            step,
            components[step],
            dry_run=dry_run,
            model_version=model_version,
            registry_snapshot=registry_snapshot,
            declared_version=component_versions.get(step),
            reconcile=reconcile,
            allowed_rejections=allowed_rejections,
        ))
    return problems
