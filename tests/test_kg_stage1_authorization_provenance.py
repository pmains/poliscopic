"""Authorization-provenance honesty guards.

The defect these prevent: recording an *inferred* approval timestamp (derived from
a chat message's displayed time) as though it were observed fact.  Provenance must
bind what was actually seen — actor, approval text, channel, thread — and record
what was *not* seen as unavailable rather than filling the gap with a plausible
value.

Pure and offline.
"""

from __future__ import annotations

import copy
import json

import pytest

from scripts.kg import stage1_adjudication as adjudication


@pytest.fixture
def provenance():
    return copy.deepcopy(adjudication.ADJUDICATION)


# -- the shipped record is honest ---------------------------------------------

def test_shipped_provenance_validates_clean():
    assert adjudication.validate_authorization_provenance() == []
    adjudication.assert_authorization_provenance()  # does not raise


def test_approval_binds_actor_text_channel_and_thread():
    approval = adjudication.ADJUDICATION["approval"]
    assert approval["approved_by"] == "project owner"
    assert approval["approval_text"] == "I approve"
    assert approval["channel"] == "codex_thread"
    assert approval["thread_id"] == "<manager-thread-id>"


@pytest.mark.parametrize("field", ["message_timestamp", "message_id"])
def test_unobserved_message_metadata_is_absent_not_guessed(field):
    approval = adjudication.ADJUDICATION["approval"]
    assert approval[field] is None
    assert approval[f"{field}_status"] in adjudication.UNAVAILABLE_STATUSES


def test_no_inferred_approval_timestamp_is_recorded():
    """The old `authorized_at` value must be gone for good."""
    authorization = adjudication.ADJUDICATION["authorization"]
    for forbidden in adjudication.FORBIDDEN_APPROVAL_TIME_FIELDS:
        assert forbidden not in authorization
        assert forbidden not in adjudication.ADJUDICATION["approval"]
    assert "2026-09-12T02:00:00Z" not in json.dumps(adjudication.ADJUDICATION)


def test_record_time_is_labelled_machine_generated_and_not_the_approval_time():
    authorization = adjudication.ADJUDICATION["authorization"]
    assert authorization["recorded_at"]
    source = authorization["recorded_at_source"].lower()
    assert "machine clock" in source
    assert "not the approval" in source


# -- fabricated precision is refused ------------------------------------------

@pytest.mark.parametrize("field", ["authorized_at", "approved_at", "approval_timestamp"])
def test_inferred_approval_time_fields_are_rejected(provenance, field):
    provenance["authorization"][field] = "2026-09-12T02:00:00Z"
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any(field in problem for problem in problems)


def test_asserted_message_timestamp_without_a_source_is_rejected(provenance):
    provenance["approval"]["message_timestamp"] = "2026-09-11T19:00:00Z"
    provenance["approval"]["message_timestamp_status"] = "observed"
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any("message_timestamp_source" in problem for problem in problems)


def test_provenanced_message_timestamp_is_accepted(provenance):
    provenance["approval"]["message_timestamp"] = "2026-09-11T19:00:00Z"
    provenance["approval"]["message_timestamp_status"] = "observed"
    provenance["approval"]["message_timestamp_source"] = "codex thread message header"
    assert adjudication.validate_authorization_provenance(provenance) == []


def test_absent_timestamp_without_an_honest_status_is_rejected(provenance):
    del provenance["approval"]["message_timestamp_status"]
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any("message_timestamp_status" in problem for problem in problems)


def test_absent_timestamp_with_a_contradictory_status_is_rejected(provenance):
    provenance["approval"]["message_timestamp_status"] = "observed"
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any("message_timestamp_status" in problem for problem in problems)


def test_record_time_without_the_not_the_approval_distinction_is_rejected(provenance):
    provenance["authorization"]["recorded_at_source"] = "machine clock"
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any("not the approval" in problem for problem in problems)


def test_missing_record_time_is_rejected(provenance):
    del provenance["authorization"]["recorded_at"]
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any("recorded_at" in problem for problem in problems)


@pytest.mark.parametrize("field", ["approved_by", "approval_text", "thread_id"])
def test_missing_approval_binding_is_rejected(provenance, field):
    del provenance["approval"][field]
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any(field in problem for problem in problems)


def test_unrecognised_channel_is_rejected(provenance):
    provenance["approval"]["channel"] = "somewhere"
    problems = adjudication.validate_authorization_provenance(provenance)
    assert any("channel" in problem for problem in problems)


def test_assert_raises_on_invalid_provenance(monkeypatch):
    broken = copy.deepcopy(adjudication.ADJUDICATION)
    broken["authorization"]["authorized_at"] = "2026-09-12T02:00:00Z"
    monkeypatch.setattr(adjudication, "ADJUDICATION", broken)
    with pytest.raises(adjudication.AuthorizationError):
        adjudication.assert_authorization_provenance()


# -- the correction did not disturb scope -------------------------------------

def test_adjudicated_scope_and_identities_are_unchanged():
    record = adjudication.ADJUDICATION
    assert record["decision_id"] == "kg-stage1-20260911-skip-meeting-15841"
    assert record["adjudicator"] == "Peter Mains"
    assert record["decided_at"] == "2026-09-12T00:28:45Z"
    assert record["decision"] == "approved"
    assert record["quarantine_reason"] == "scraper_sentinel_non_meeting"
    assert len(record["quarantine_extraction_ids"]) == 18
    assert record["meeting_15841_is_a_meeting"] is False
    assert record["body_level_association"] == (
        "Enhanced Municipal Services District Advisory Board"
    )


def test_authorization_still_grants_apply_and_backup():
    authorization = adjudication.ADJUDICATION["authorization"]
    assert authorization["apply_authorized"] is True
    assert authorization["backup_authorized"] is True
    assert "development-only" in authorization["authorization_scope"].lower()
    assert "out of scope" in authorization["note"].lower()


def test_quarantine_values_still_reflect_the_authorization():
    values = adjudication.quarantine_values("kg-model/1.0")
    assert values["apply_blocked_by_authorization"] is False
    assert values["human_fields_supplied"] is True
    assert values["quarantined_by"] == "Peter Mains"
    assert values["quarantined_at"] == "2026-09-12T00:28:45Z"
