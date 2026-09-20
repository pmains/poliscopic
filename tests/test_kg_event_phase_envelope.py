"""Event-pipeline phase-envelope tests.

The event phase emits over two different assertion domains — event normalization
and entity linking — so it exposes one *envelope* instead of a fabricated summed
stream.  These tests drive the envelope contract directly through the canonical
reconciliation authority, with no database, no subprocess, and no pipeline run.
"""

from __future__ import annotations

import copy

import pytest

from scripts.kg import orchestration_receipts as orch
from scripts.kg import phase_envelope as pe
from scripts.kg import registries as r
from scripts.kg.emission import EmissionValidator
from scripts.kg.emission_receipts import reconcile_receipts
from scripts.kg.producer_versions import (
    declared_component_versions,
    declared_producer_version,
)

PRODUCER = "event_pipeline"
VERSIONS = declared_component_versions(PRODUCER)
PHASE_VERSION = declared_producer_version(PRODUCER)


def component_receipt(version: str, *, dry_run: bool = True,
                      producer: str = PRODUCER) -> dict:
    """A real, sealed component receipt for one domain."""
    validator = EmissionValidator(producer, version, dry_run=dry_run)
    validator.start_batch()
    validator.complete_validation()
    if not dry_run:
        validator.begin_writes()
    validator.classify_rows(would_insert=0)
    return validator.seal().serialize()


def envelope(*, dry_run: bool = True, steps=("normalize", "link"),
             components=None, version: str | None = None,
             model_version: str | None = None,
             snapshot: str | None = None) -> dict:
    """A well-formed envelope, overridable per field."""
    if components is None:
        components = [(step, component_receipt(VERSIONS[step], dry_run=dry_run))
                      for step in steps]
    return pe.build_envelope(
        producer=PRODUCER,
        producer_version=version or PHASE_VERSION or "unknown",
        dry_run=dry_run,
        model_version=model_version or r.MODEL_VERSION,
        registry_snapshot=snapshot or r.snapshot_sha256(),
        selected_steps=steps,
        components=components,
    )


def problems(env: dict, *, dry_run: bool = True,
             require_complete_coverage: bool = True) -> list[str]:
    return pe.envelope_problems(
        env,
        producer=PRODUCER,
        declared_version=PHASE_VERSION,
        dry_run=dry_run,
        model_version=r.MODEL_VERSION,
        registry_snapshot=r.snapshot_sha256(),
        component_versions=VERSIONS,
        reconcile=reconcile_receipts,
        require_complete_coverage=require_complete_coverage,
    )


def enforce(env) -> dict:
    return orch.enforce_phase_receipt(
        PRODUCER, raw_result={"validation_receipt": env}, dry_run=True)


# ── success path ────────────────────────────────────────────────────────


def test_full_pipeline_envelope_reconciles_with_no_problems():
    env = envelope()
    assert problems(env) == []
    assert env["coverage_complete"] is True
    assert sorted(env["component_steps"]) == ["link", "normalize"]


def test_orchestration_accepts_the_full_envelope():
    result = enforce(envelope())
    assert result["ok"] is True, result["reasons"]
    assert any(c["check"] == "phase_envelope" for c in result["checks"])


def test_envelope_invents_no_summed_value_or_row_totals():
    env = envelope()
    # No aggregated assertion stream is manufactured at the phase level.
    assert "values" not in env
    assert "rows" not in env
    # Each domain keeps its own receipt intact.
    assert set(env["components"]) == {"normalize", "link"}
    assert env["components"]["normalize"]["producer_version"] == VERSIONS["normalize"]
    assert env["components"]["link"]["producer_version"] == VERSIONS["link"]


def test_each_component_reconciles_independently():
    env = envelope()
    for step, receipt in env["components"].items():
        assert reconcile_receipts(
            [receipt], expected_producers=[PRODUCER],
            model_version=r.MODEL_VERSION,
            registry_snapshot=r.snapshot_sha256(),
        ) == [], step


# ── step filtering and coverage ─────────────────────────────────────────


def test_step_filtering_builds_a_single_component_envelope():
    env = envelope(steps=("link",))
    assert env["component_steps"] == ["link"]
    assert env["coverage_complete"] is True
    assert problems(env, require_complete_coverage=False) == []


def test_step_filtering_cannot_claim_full_phase_coverage():
    env = envelope(steps=("link",))
    found = problems(env, require_complete_coverage=True)
    assert any("did not select every required component step" in p for p in found)


def test_enforcement_refuses_a_filtered_run():
    result = enforce(envelope(steps=("link",)))
    assert result["ok"] is False


# ── refusal matrix ──────────────────────────────────────────────────────


def test_missing_component_is_refused():
    env = envelope(components=[
        ("link", component_receipt(VERSIONS["link"])),
    ])
    found = problems(env)
    assert any("missing component receipt" in p for p in found)
    assert enforce(env)["ok"] is False


def test_duplicate_component_is_refused():
    env = envelope(components=[
        ("link", component_receipt(VERSIONS["link"])),
        ("link", component_receipt(VERSIONS["link"])),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    assert env["coverage_complete"] is False
    found = problems(env)
    assert any("duplicate" in p for p in found)


def test_partial_component_is_refused():
    receipt = component_receipt(VERSIONS["link"])
    receipt.pop("rows")
    env = envelope(components=[
        ("link", receipt),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    found = problems(env)
    assert any("partial" in p for p in found)


def test_malformed_component_is_refused():
    env = envelope(components=[
        ("link", "not-a-receipt"),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    found = problems(env)
    assert any("not an object" in p for p in found)


def test_rejected_value_in_a_component_is_refused():
    receipt = component_receipt(VERSIONS["link"])
    receipt["values"] = dict(receipt["values"])
    receipt["values"]["attempted"] = receipt["values"]["attempted"] + 1
    receipt["values"]["rejected"] = receipt["values"]["rejected"] + 1
    receipt["rejections"] = [{"category": "role", "value": "known_org",
                              "reason": "quarantined", "source": "test"}]
    env = envelope(components=[
        ("link", receipt),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    found = problems(env)
    assert any("rejected" in p for p in found)


def test_failed_component_is_refused():
    receipt = component_receipt(VERSIONS["link"])
    receipt["failure"] = "RuntimeError: boom"
    env = envelope(components=[
        ("link", receipt),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    assert any("failed" in p for p in problems(env))


def test_unsealed_component_is_refused():
    receipt = component_receipt(VERSIONS["link"])
    receipt["state"] = "collecting"
    env = envelope(components=[
        ("link", receipt),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    assert any("not sealed" in p for p in problems(env))


def test_unreconciled_component_is_refused():
    receipt = component_receipt(VERSIONS["link"])
    receipt["rows"] = dict(receipt["rows"])
    receipt["rows"]["proposed"] = receipt["rows"]["proposed"] + 3
    env = envelope(components=[
        ("link", receipt),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    found = problems(env)
    assert any("does not reconcile" in p for p in found)


def test_wrong_component_dry_run_is_refused():
    env = envelope(components=[
        ("link", component_receipt(VERSIONS["link"], dry_run=False)),
        ("normalize", component_receipt(VERSIONS["normalize"], dry_run=True)),
    ])
    assert any("dry_run" in p for p in problems(env))


def test_wrong_component_version_is_refused():
    env = envelope(components=[
        ("link", component_receipt("2000-01-01.0")),
        ("normalize", component_receipt(VERSIONS["normalize"])),
    ])
    assert any("!= declared" in p for p in problems(env))


def test_wrong_phase_version_is_refused():
    env = envelope(version="event_pipeline/9.9")
    assert any("producer_version" in p for p in problems(env))


def test_wrong_model_version_is_refused():
    env = envelope(model_version="kg-model/0.0")
    assert any("model_version" in p for p in problems(env))


def test_wrong_registry_snapshot_is_refused():
    env = envelope(snapshot="0" * 64)
    assert any("registry_snapshot" in p for p in problems(env))


def test_unexpected_component_step_is_refused():
    env = envelope(components=[
        ("link", component_receipt(VERSIONS["link"])),
        ("normalize", component_receipt(VERSIONS["normalize"])),
        ("extract", component_receipt("whatever")),
    ])
    assert any("unexpected component step" in p for p in problems(env))


def test_envelope_that_is_not_an_envelope_is_refused():
    assert pe.envelope_problems({}, producer=PRODUCER, declared_version=PHASE_VERSION,
                                dry_run=True, model_version=r.MODEL_VERSION,
                                registry_snapshot=r.snapshot_sha256(),
                                component_versions=VERSIONS,
                                reconcile=reconcile_receipts) != []


def test_single_receipt_payload_is_not_treated_as_an_envelope():
    receipt = component_receipt(PHASE_VERSION or "x")
    assert pe.is_envelope(receipt) is False
    result = orch.enforce_phase_receipt(
        PRODUCER, raw_result={"validation_receipt": receipt}, dry_run=True)
    # It is handled by the ordinary single-receipt path, not the envelope path.
    assert not any(c["check"] == "phase_envelope" for c in result["checks"])


# ── live mode ───────────────────────────────────────────────────────────


def test_live_envelope_with_committed_components_is_accepted():
    env = envelope(dry_run=False, components=[
        ("link", component_receipt(VERSIONS["link"], dry_run=False)),
        ("normalize", component_receipt(VERSIONS["normalize"], dry_run=False)),
    ])
    assert problems(env, dry_run=False) == []


def test_envelope_dry_run_mismatch_is_refused():
    env = envelope(dry_run=True)
    assert any("dry_run" in p for p in problems(env, dry_run=False))


# ── additive preservation ───────────────────────────────────────────────


def test_phase_result_exposes_one_receipt_and_keeps_component_evidence():
    """The phase returns an envelope *and* the per-step receipts additively."""
    import inspect
    from scripts.entities import event_extractor

    source = inspect.getsource(event_extractor.run_event_pipeline)
    assert '"validation_receipt": envelope' in source
    assert '"validation_receipts": receipts' in source
    # Existing keys are untouched.
    for key in ('"success"', '"duration_s"', '"steps"', '"accounting"',
                '"pending"', '"dry_run"'):
        assert key in source


def test_envelope_module_does_not_duplicate_a_validator():
    import pathlib
    source = pathlib.Path(pe.__file__).read_text(encoding="utf-8")
    assert "def check" not in source
    assert "emission_checks" not in source


# ── version declaration drift ───────────────────────────────────────────


def test_declared_component_versions_match_the_sealing_modules():
    """The declaration must equal what each component actually seals with.

    The versions are literals in the authoritative registry (importing the
    producer modules there would widen every phase's code-evidence manifest), so
    this drift test is what keeps the declaration honest.
    """
    from scripts.entities.event_link import LINKER_VERSION
    from scripts.entities.event_normalize_runtime import NORMALIZER_VERSION

    declared = declared_component_versions(PRODUCER)
    assert declared == {"link": LINKER_VERSION, "normalize": NORMALIZER_VERSION}


def test_undeclared_producer_has_no_component_versions():
    assert declared_component_versions("graph_builder") == {}


def test_component_versions_cover_every_required_step():
    assert set(VERSIONS) == set(pe.REQUIRED_COMPONENT_STEPS)
