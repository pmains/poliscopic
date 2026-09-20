"""Subprocess flag routing, receipt propagation, and the fingerprint manifest.

Pure and offline: the extractor's subprocess boundary is stubbed, and the
fingerprint tests only read source files (restoring any they touch).
"""

from __future__ import annotations

import importlib
import builtins
import io
import json
import pathlib

import pytest

from scripts.entities import detect_entities
from scripts.entities import event_extractor as ex
from scripts.entities import producer_manifest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]



def normalize_stats(**overrides):
    stats = {
        "extractions_examined": 0,
        "normalizable": 0,
        "events_planned": 0,
        "events_inserted": 0,
        "extraction_links_updated": 0,
        "skipped_unmapped_type": 0,
    }
    stats.update(overrides)
    return stats


def stats_by_step():
    return {
        "extract": {
            "docs": 0, "events_found": 0, "events_inserted": 0,
            "skipped_existing": 0,
        },
        "normalize": normalize_stats(),
        "link": {name: 0 for name in ex.REQUIRED_STEP_FIELDS["link"]},
    }


def recording_run_step(seen):
    """Stub the subprocess boundary, recording the argv each step receives."""
    stats = stats_by_step()

    def run_step(step_name, extra_args=None):
        seen[step_name] = list(extra_args or [])
        return {
            "step": step_name, "ok": True, "elapsed": 0.0, "returncode": 0,
            "result": {"stats": dict(stats[step_name])},
        }

    return run_step


# -- subprocess flag routing --------------------------------------------------


def test_force_reaches_only_the_normalize_step(monkeypatch):
    seen = {}
    monkeypatch.setattr(ex, "run_step", recording_run_step(seen))
    monkeypatch.setattr(ex, "count_pending", lambda engine: 0)

    for step in ("normalize", "extract", "link"):
        ex.run_event_pipeline(None, steps=[step], force=True)

    assert "--force" in seen["normalize"]
    # extract and link never declare --force, so it must not reach them.
    assert "--force" not in seen["extract"]
    assert "--force" not in seen["link"]


def test_flag_capabilities_are_declared_per_step():
    assert ex.STEPS["normalize"]["flags"] == frozenset(
        {"--dry-run", "--force", "--limit"}
    )
    assert "--force" not in ex.STEPS["extract"]["flags"]
    assert "--force" not in ex.STEPS["link"]["flags"]


def test_dry_run_and_limit_propagation_is_unchanged(monkeypatch):
    seen = {}
    monkeypatch.setattr(ex, "run_step", recording_run_step(seen))
    monkeypatch.setattr(ex, "count_pending", lambda engine: 0)

    for step in ("extract", "normalize", "link"):
        ex.run_event_pipeline(None, steps=[step], dry_run=True, limit=5)

    for step in ("extract", "normalize", "link"):
        assert seen[step] == ["--dry-run", "--limit", "5"]


def test_absent_limit_is_not_passed(monkeypatch):
    seen = {}
    monkeypatch.setattr(ex, "run_step", recording_run_step(seen))
    monkeypatch.setattr(ex, "count_pending", lambda engine: 0)

    ex.run_event_pipeline(None, steps=["normalize"])

    assert seen["normalize"] == []


# -- child result contract ----------------------------------------------------


def test_child_contract_accepts_a_success_envelope():
    stdout = json.dumps(
        {"step": "normalize", "success": True, "stats": normalize_stats()}
    )

    envelope, error = ex._parse_step_result("normalize", stdout)

    assert error is None
    assert envelope["success"] is True


def test_child_contract_rejects_an_incomplete_envelope():
    stdout = json.dumps(
        {"step": "normalize", "success": True,
         "stats": {"extractions_examined": 1}}
    )

    envelope, error = ex._parse_step_result("normalize", stdout)

    assert error is not None


def test_child_contract_rejects_a_failure_envelope():
    """A nonzero exit is a failure even when the counters look complete."""
    stdout = json.dumps(
        {"step": "normalize", "success": False, "stats": normalize_stats()}
    )

    envelope, error = ex._parse_step_result("normalize", stdout)

    assert error is not None


# -- receipt propagation ------------------------------------------------------


def test_validation_receipt_reaches_the_pipeline_output(monkeypatch):
    receipt = {"producer": "event_pipeline", "state": "sealed", "failure": None}
    stats = normalize_stats()
    stats["validation_receipt"] = receipt

    monkeypatch.setattr(ex, "run_step", lambda step_name, extra_args=None: {
        "step": step_name, "ok": True, "elapsed": 0.0, "returncode": 0,
        "result": {"stats": stats},
    })
    monkeypatch.setattr(ex, "count_pending", lambda engine: 0)

    out = ex.run_event_pipeline(None, steps=["normalize"])

    assert out["success"] is True
    assert out["accounting"]["normalize"]["validation_receipt"] == receipt
    assert out["validation_receipts"] == {"normalize": receipt}


# -- fingerprint manifest -----------------------------------------------------


def event_pipeline_phase():
    """The declared event_pipeline phase, wherever it is registered."""
    for value in vars(detect_entities).values():
        if isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, dict) and item.get("name") == "event_pipeline":
                    return item
    raise AssertionError("the event_pipeline phase is not declared")


def test_manifest_is_authoritative_sorted_and_unique():
    """The declaration itself is authoritative: no second list lives in the test."""
    modules = tuple(event_pipeline_phase()["code_modules"])

    assert modules, "event_pipeline must declare its producer modules"
    assert list(modules) == sorted(modules)
    assert len(set(modules)) == len(modules)

    for module_path in modules:
        source = REPO_ROOT / (module_path.replace(".", "/") + ".py")
        assert source.exists(), module_path


def test_manifest_is_closed_under_import():
    """Completeness is derived from the code, so a new dependency cannot hide."""
    assert producer_manifest.undocumented_imports(event_pipeline_phase(), REPO_ROOT) == ()


def test_manifest_fingerprints_completely_and_deterministically():
    phase = event_pipeline_phase()
    metadata = detect_entities._producer_metadata(phase)

    assert metadata["code_evidence_complete"] is True
    assert metadata["code_sha256"]
    assert metadata["code_module_errors"] == {}
    assert sorted(metadata["code_module_sha256"]) == sorted(phase["code_modules"])
    assert all(metadata["code_module_sha256"].values())
    assert detect_entities._producer_metadata(phase)["code_sha256"] == metadata["code_sha256"]


def test_mutating_any_declared_component_changes_the_aggregate(monkeypatch):
    """Every declared component is genuinely part of the fingerprint."""
    phase = event_pipeline_phase()
    baseline = detect_entities._producer_metadata(phase)["code_sha256"]

    for module_path in phase["code_modules"]:
        path = pathlib.Path(importlib.import_module(module_path).__file__)
        original = path.read_bytes()
        real_open = builtins.open

        def probe_open(file, mode="r", *args, **kwargs):
            if pathlib.Path(file) == path and mode == "rb":
                return io.BytesIO(original + b"\n# fingerprint probe\n")
            return real_open(file, mode, *args, **kwargs)

        with monkeypatch.context() as scoped:
            scoped.setattr(builtins, "open", probe_open)
            mutated = detect_entities._producer_metadata(phase)["code_sha256"]
        assert mutated != baseline, module_path

    # Restored exactly, so the aggregate returns to its baseline.
    assert detect_entities._producer_metadata(phase)["code_sha256"] == baseline


def test_a_missing_component_fails_closed():
    phase = event_pipeline_phase()
    broken = dict(phase)
    broken["code_modules"] = tuple(phase["code_modules"]) + (
        "scripts.entities.definitely_not_a_module",
    )

    metadata = detect_entities._producer_metadata(broken)

    assert metadata["code_evidence_complete"] is False
    assert metadata["code_sha256"] is None
    assert "scripts.entities.definitely_not_a_module" in metadata["code_module_errors"]


def test_a_duplicate_component_fails_closed():
    phase = event_pipeline_phase()
    modules = tuple(phase["code_modules"])
    broken = dict(phase)
    broken["code_modules"] = modules + (modules[0],)

    metadata = detect_entities._producer_metadata(broken)

    assert metadata["code_evidence_complete"] is False
    assert metadata["code_sha256"] is None
    assert (
        metadata["code_module_errors"]["manifest"]
        == "duplicate module path in code_modules"
    )


@pytest.mark.parametrize("step", ["extract", "normalize", "link"])
def test_every_step_declares_a_script_and_flags(step):
    declared = ex.STEPS[step]

    assert declared["script"].endswith(".py")
    assert isinstance(declared["flags"], frozenset)
