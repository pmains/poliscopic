"""Contract tests for the event extraction subprocess envelope."""

from types import SimpleNamespace

import pytest

from scripts.entities import event_extractor


def _stats(step):
    fields = event_extractor.REQUIRED_STEP_FIELDS[step]
    return {field: 0 for field in fields}


@pytest.mark.parametrize("step", ["extract", "normalize", "link"])
def test_parse_accepts_each_step_envelope_after_human_log_lines(step):
    envelope = {"step": step, "success": True, "stats": _stats(step)}
    stdout = "starting work\nprocessed one batch\n" + event_extractor.json.dumps(envelope)

    result, contract_error = event_extractor._parse_step_result(step, stdout)

    assert result == envelope
    assert contract_error is None


def test_run_step_missing_json_with_zero_exit_is_contract_failure(monkeypatch):
    monkeypatch.setattr(
        event_extractor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="completed\n", stderr=""),
    )
    monkeypatch.setattr(event_extractor.time, "time", lambda: 10.0)

    result = event_extractor.run_step("extract")

    assert result["ok"] is False
    assert result["result"] is None
    assert result["contract_error"] == "missing JSON result envelope"


def test_parse_malformed_json_fails():
    result, contract_error = event_extractor._parse_step_result(
        "extract", "human output\n{not valid json\n"
    )

    assert result is None
    assert contract_error == "missing JSON result envelope"


def test_parse_wrong_step_fails():
    envelope = {"step": "normalize", "success": True, "stats": _stats("normalize")}

    result, contract_error = event_extractor._parse_step_result(
        "extract", event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert "result step mismatch" in contract_error


def test_parse_success_false_fails():
    envelope = {"step": "link", "success": False, "stats": _stats("link")}

    result, contract_error = event_extractor._parse_step_result(
        "link", event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert contract_error == "step result did not report success=true"


def test_parse_missing_required_accounting_field_fails():
    stats = _stats("link")
    stats.pop("participant_attempts")
    envelope = {"step": "link", "success": True, "stats": stats}

    result, contract_error = event_extractor._parse_step_result(
        "link", event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert "participant_attempts" in contract_error


@pytest.mark.parametrize("bad_value", [-1, 1.5, "1", True])
def test_parse_rejects_invalid_accounting_counter(bad_value):
    stats = _stats("extract")
    stats["events_inserted"] = bad_value
    envelope = {"step": "extract", "success": True, "stats": stats}

    result, contract_error = event_extractor._parse_step_result(
        "extract", event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert "events_inserted" in contract_error


@pytest.mark.parametrize(
    ("step", "updates", "error_fragment"),
    [
        ("extract", {"events_found": 1, "events_inserted": 1,
                     "skipped_existing": 1}, "exceeds events_found"),
        ("normalize", {"extractions_examined": 2, "normalizable": 1,
                       "skipped_unmapped_type": 0}, "does not balance"),
        ("normalize", {"extractions_examined": 1, "normalizable": 1,
                       "events_planned": 0}, "planning does not balance"),
        ("normalize", {"extractions_examined": 1, "normalizable": 1,
                       "events_planned": 1, "events_inserted": 1,
                       "extraction_links_updated": 0},
         "event/link accounting does not balance"),
        ("link", {"participant_attempts": 2, "participants_inserted": 1},
         "participant accounting does not balance"),
    ],
)
def test_parse_rejects_impossible_accounting_totals(step, updates, error_fragment):
    stats = _stats(step)
    stats.update(updates)
    envelope = {"step": step, "success": True, "stats": stats}

    result, contract_error = event_extractor._parse_step_result(
        step, event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert error_fragment in contract_error


def test_parse_accepts_link_dry_run_planned_outcomes():
    stats = _stats("link")
    stats.update({
        "participant_attempts": 4,
        "participants_planned_insert": 2,
        "participants_planned_update": 1,
        "participant_replay_collisions": 1,
    })
    envelope = {"step": "link", "success": True, "dry_run": True, "stats": stats}

    result, contract_error = event_extractor._parse_step_result(
        "link", event_extractor.json.dumps(envelope)
    )

    assert result == envelope
    assert contract_error is None


def test_parse_rejects_link_live_planned_actual_mismatch():
    stats = _stats("link")
    stats.update({
        "participant_attempts": 1,
        "participants_planned_update": 1,
    })
    envelope = {"step": "link", "success": True, "dry_run": False, "stats": stats}

    result, contract_error = event_extractor._parse_step_result(
        "link", event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert "live participant accounting does not balance" in contract_error


def test_parse_rejects_link_written_mutation_mismatch():
    stats = _stats("link")
    stats.update({
        "participant_attempts": 1,
        "participants_planned_insert": 1,
        "participants_inserted": 1,
        "participants_written": 0,
        "participants_mutated": 1,
    })
    envelope = {"step": "link", "success": True, "dry_run": False, "stats": stats}

    result, contract_error = event_extractor._parse_step_result(
        "link", event_extractor.json.dumps(envelope)
    )

    assert result is None
    assert "written participant accounting does not balance" in contract_error


def test_run_step_nonzero_exit_with_valid_json_remains_failed(monkeypatch):
    envelope = {"step": "normalize", "success": True, "stats": _stats("normalize")}
    monkeypatch.setattr(
        event_extractor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=7,
            stdout="normal progress\n" + event_extractor.json.dumps(envelope) + "\n",
            stderr="warning from child\n",
        ),
    )
    clock = iter([100.0, 100.5])
    monkeypatch.setattr(event_extractor.time, "time", lambda: next(clock))

    result = event_extractor.run_step("normalize")

    assert result["ok"] is False
    assert result["returncode"] == 7
    assert result["result"] == envelope
    assert result["contract_error"] is None
    assert result["elapsed"] == pytest.approx(0.5)


@pytest.mark.parametrize(
    ("stderr", "expected_level"),
    [
        ("[INFO] child progress", "INFO"),
        ("[WARNING] child warning", "WARNING"),
        ("child output without a level", "WARNING"),
    ],
)
def test_run_step_relays_stderr_at_embedded_or_default_level(
    monkeypatch, caplog, stderr, expected_level
):
    envelope = {"step": "extract", "success": True, "stats": _stats("extract")}
    monkeypatch.setattr(
        event_extractor.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=0,
            stdout=event_extractor.json.dumps(envelope) + "\n",
            stderr=stderr + "\n",
        ),
    )
    clock = iter([100.0, 100.5])
    monkeypatch.setattr(event_extractor.time, "time", lambda: next(clock))

    with caplog.at_level("DEBUG", logger="event_extractor"):
        result = event_extractor.run_step("extract")

    relayed = [record for record in caplog.records if record.getMessage().endswith(stderr)]
    assert len(relayed) == 1
    assert relayed[0].levelname == expected_level
    assert relayed[0].getMessage() == f"  [extract] {stderr}"
    assert result["result"] == envelope
    assert result["contract_error"] is None
