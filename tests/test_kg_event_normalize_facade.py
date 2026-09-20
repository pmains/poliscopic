"""The event-normalize facade: delegation, envelope, vocabulary, no legacy code.

Pure and offline: the CLI is driven with stubbed dependencies, so no engine, no
database and no pipeline are involved.
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

from scripts.entities import event_normalize as facade
from scripts.entities import event_normalize_runtime as runtime
from scripts.entities import event_vocabulary

ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"


def stats_stub(**overrides):
    stats = {
        "extractions_examined": 0,
        "events_planned": 0,
        "events_inserted": 0,
        "extraction_links_updated": 0,
    }
    stats.update(overrides)
    return stats


def envelope_from(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


# -- facade delegation --------------------------------------------------------


def test_facade_normalize_delegates_to_the_runtime(monkeypatch):
    calls = []

    def fake_runtime(engine, **kwargs):
        calls.append((engine, kwargs))
        return {"delegated": True}

    monkeypatch.setattr(facade, "run_normalize", fake_runtime)

    result = facade.normalize("ENGINE", limit=7, dry_run=True, force=True)

    assert result == {"delegated": True}
    assert calls == [("ENGINE", {"limit": 7, "dry_run": True, "force": True})]


def test_facade_normalize_defaults_match_the_compatibility_signature():
    import inspect

    params = inspect.signature(facade.normalize).parameters
    assert list(params) == ["engine", "limit", "dry_run", "force"]
    assert params["limit"].default is None
    assert params["dry_run"].default is False
    assert params["force"].default is False


def test_facade_owns_no_classification_or_write_logic():
    source = (ENTITIES_DIR / "event_normalize.py").read_text(encoding="utf-8")

    for forbidden in (
        "execute_values", "INSERT INTO", "SELECT ", "psycopg2",
        "while not done", "resolve_event_type_id", "build_classification_plan",
    ):
        assert forbidden not in source, forbidden

    # It delegates, and it is the runtime that owns the behavior.
    assert "run_normalize" in source
    assert "event_normalize_runtime" in source


# -- CLI ---------------------------------------------------------------------


def test_cli_parses_flags_and_forwards_them(monkeypatch):
    seen = {}

    def fake_normalize(engine, limit=None, dry_run=False, force=False):
        seen.update(limit=limit, dry_run=dry_run, force=force)
        return stats_stub()

    monkeypatch.setattr(facade, "normalize", fake_normalize)
    monkeypatch.setattr(facade, "get_engine", lambda: "ENGINE")
    monkeypatch.setattr(
        sys, "argv",
        ["event_normalize.py", "--dry-run", "--force", "--limit", "25"],
    )

    assert facade.main() == 0
    assert seen == {"limit": 25, "dry_run": True, "force": True}


def test_success_envelope_exits_zero(monkeypatch, capsys):
    monkeypatch.setattr(
        facade, "normalize", lambda *a, **k: stats_stub(events_inserted=3)
    )
    monkeypatch.setattr(facade, "get_engine", lambda: None)
    monkeypatch.setattr(sys, "argv", ["event_normalize.py"])

    assert facade.main() == 0

    payload = envelope_from(capsys.readouterr())
    assert payload["step"] == "normalize"
    assert payload["success"] is True
    assert payload["stats"]["events_inserted"] == 3


def test_failure_envelope_carries_the_exact_stats_and_receipt(
    monkeypatch, capsys
):
    receipt = {
        "producer": "event_pipeline",
        "state": "sealed",
        "failure": "replay verification failed",
        "values": {"attempted": 1, "accepted": 1, "rejected": 0},
        "rows": {"proposed": 2, "committed": 0, "rolled_back": 2},
    }
    stats = stats_stub(errors=1, validation_receipt=receipt)

    def boom(*args, **kwargs):
        raise runtime.NormalizationRunError(
            "boom", stats=stats, receipt=receipt, earlier_pages_committed=False
        )

    monkeypatch.setattr(facade, "normalize", boom)
    monkeypatch.setattr(facade, "get_engine", lambda: None)
    monkeypatch.setattr(sys, "argv", ["event_normalize.py"])

    code = facade.main()

    assert code == 1
    payload = envelope_from(capsys.readouterr())
    assert payload["step"] == "normalize"
    assert payload["success"] is False
    # The carried evidence is emitted verbatim, receipt included.
    assert payload["stats"] == stats
    assert payload["stats"]["validation_receipt"]["state"] == "sealed"
    assert payload["stats"]["validation_receipt"]["failure"]


def test_success_envelope_carries_one_sealed_reconcilable_receipt(
    monkeypatch, capsys
):
    """The success envelope carries the runtime's own sealed receipt."""
    from _kg_event_normalize_sqlite import build_engine, seed

    engine = build_engine()
    seed(engine, action_verb="approved")
    monkeypatch.setattr(facade, "get_engine", lambda: engine)
    monkeypatch.setattr(sys, "argv", ["event_normalize.py", "--dry-run"])

    assert facade.main() == 0

    payload = envelope_from(capsys.readouterr())
    receipt = payload["stats"]["validation_receipt"]

    # Exactly one receipt, sealed, and neither unit contradicts the other.
    assert isinstance(receipt, dict)
    assert receipt["state"] == "sealed"
    assert receipt["failure"] is None
    assert receipt["dry_run"] is True
    assert receipt["values"]["reconciles"] is True
    assert receipt["rows"]["reconciles"] is True
    assert receipt["rows"]["classification_reconciles"] is True
    assert payload["stats"]["errors"] == 0
    # No second, competing receipt shape is smuggled into the envelope.
    assert not any(name.endswith("validation_receipts") for name in payload)


def test_process_control_signals_are_not_wrapped(monkeypatch):
    monkeypatch.setattr(facade, "get_engine", lambda: None)
    monkeypatch.setattr(sys, "argv", ["event_normalize.py"])

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(facade, "normalize", interrupt)
    with pytest.raises(KeyboardInterrupt):
        facade.main()

    def exit_now(*args, **kwargs):
        raise SystemExit(2)

    monkeypatch.setattr(facade, "normalize", exit_now)
    with pytest.raises(SystemExit) as exc:
        facade.main()
    assert exc.value.code == 2


# -- vocabulary compatibility -------------------------------------------------


def test_facade_re_exports_the_one_vocabulary_authority():
    assert facade.VERB_MAP is event_vocabulary.VERB_MAP
    assert facade.PROCEDURAL_OUTCOMES is event_vocabulary.PROCEDURAL_OUTCOMES
    assert facade.normalize_verb is event_vocabulary.normalize_verb

    assert facade.VERB_MAP["approved"] == ("decision.approval", "approved")
    assert facade.PROCEDURAL_OUTCOMES == {
        "called_to_order", "no_action", "no_response"
    }
    assert facade.normalize_verb("  Approved \n with conditions ") == (
        "approved_with_conditions"
    )


def test_producer_vocabulary_can_still_introspect_the_producer():
    """``producer_vocabulary`` loads the producer module and reads these names."""
    module = importlib.import_module("entities.event_normalize")

    assert getattr(module, "VERB_MAP", None)
    assert getattr(module, "PROCEDURAL_OUTCOMES", None)
    assert len(module.VERB_MAP) == len(event_vocabulary.VERB_MAP)


def test_the_producer_initializers_still_import_from_the_facade():
    for name in ("event_normalize_models", "event_normalize_snapshot"):
        source = (ENTITIES_DIR / f"{name}.py").read_text(encoding="utf-8")
        assert "from scripts.entities.event_normalize import" in source, name


# -- housekeeping -------------------------------------------------------------


def test_every_normalizer_file_is_under_five_hundred_lines():
    paths = sorted(ENTITIES_DIR.glob("event_normalize*.py"))
    paths.append(ENTITIES_DIR / "event_vocabulary.py")

    assert len(paths) >= 15
    for path in paths:
        lines = len(path.read_text(encoding="utf-8").splitlines())
        assert lines < 500, path.name
