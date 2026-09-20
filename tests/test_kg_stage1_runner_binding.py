"""Canonical-binding and in-transaction checks for the Stage 1 apply runner.

The point of these tests is that a forged manifest is refused **even when it is
correctly re-hashed**, because the runner compares it against the expectation
reconstructed from the reviewed sources rather than trusting its own consistency.

Isolated: no PostgreSQL, no network, no real apply.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, text

from scripts.kg import stage1_apply_runner as runner
from scripts.kg import stage1_packet_components as components
from scripts.kg import stage1_runner_checks as checks

from test_kg_stage1_packet_v2 import (
    _counts,
    _digest,
    _packet,
    _receipt,
    stage1_test_engine,
)

SCOPE = components.meeting_scope()


@pytest.fixture
def engine():
    return stage1_test_engine()


def _rehashed(packet):
    """A forged packet with a correctly recomputed digest."""
    packet["packet_digest"] = _digest(packet)
    return packet


# -- the canonical packet passes ----------------------------------------------

def test_the_canonical_packet_satisfies_the_binding():
    assert checks.canonical_problems(_packet()) == []


def test_rehashed_canonical_packet_still_satisfies_the_binding():
    assert checks.canonical_problems(_rehashed(_packet())) == []


# -- forged-but-rehashed packets are refused ----------------------------------

def test_forged_operations_are_refused_even_when_rehashed():
    packet = _packet()
    for operation in packet["operations"]:
        if operation["op"] == "UPDATE":
            operation["set"] = {"public_body_id": "(SELECT id FROM public_bodies WHERE 1=1)",
                                "jurisdiction_id": 99}
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("name" in problem or "body_code" in problem or "scope" in problem
               or "targets" in problem for problem in problems) or problems


def test_forged_target_ids_are_refused_even_when_rehashed():
    packet = _packet()
    for operation in packet["operations"]:
        if operation["op"] == "UPDATE" and operation["component"] == "phoenix-dab":
            operation["target_ids"] = operation["target_ids"] + [14608]
            operation["rows"] = len(operation["target_ids"])
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("targets" in problem for problem in problems)


def test_forged_meeting_scope_is_refused_even_when_rehashed():
    packet = _packet()
    packet["populations"]["meeting_scope"]["phoenix-dab"] = (
        packet["populations"]["meeting_scope"]["phoenix-dab"] + [14629])
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("scope" in problem for problem in problems)


def test_forged_quarantine_ids_are_refused_even_when_rehashed():
    packet = _packet()
    packet["quarantine"]["target_ids"] = packet["quarantine"]["target_ids"] + [999999]
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("quarantine target ids" in problem for problem in problems)


def test_forged_body_name_is_refused_even_when_rehashed():
    packet = _packet()
    for operation in packet["operations"]:
        if operation["op"] == "INSERT":
            operation["values"]["name"] = "Phoenix Made Up Committee"
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("canonical" in problem for problem in problems)


def test_forged_human_fields_are_refused_even_when_rehashed():
    packet = _packet()
    packet["quarantine"]["values"]["quarantined_by"] = "Someone Else"
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("quarantined_by" in problem for problem in problems)


# -- reasons are validated against the live registry --------------------------

def test_registered_but_non_canonical_reason_is_refused():
    packet = _packet()
    packet["quarantine"]["values"]["quarantine_reason"] = "title_label"
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("quarantine reason" in problem for problem in problems)


def test_unregistered_reason_is_refused_by_the_live_registry():
    packet = _packet()
    packet["quarantine"]["values"]["quarantine_reason"] = "totally_made_up"
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("registry" in problem or "quarantine reason" in problem
               for problem in problems)


def test_packet_supplied_reason_list_cannot_authorize_a_reason():
    """A packet may not whitelist its own reason."""
    packet = _packet()
    packet["quarantine"]["values"]["quarantine_reason"] = "totally_made_up"
    packet["quarantine"]["reason_slugs"] = ["totally_made_up"]
    problems = checks.canonical_problems(_rehashed(packet))
    assert problems


# -- stale code and component bindings ----------------------------------------

def test_stale_component_hash_is_refused():
    packet = _packet()
    for component in packet["components"]:
        component["sha256"] = "0" * 64
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("sha256" in problem for problem in problems)


def test_stale_code_fingerprint_is_refused():
    packet = _packet()
    for component in packet["components"]:
        if component["id"] == "schema.quarantine_columns":
            component["quarantine_schema_sha256"] = "1" * 64
    problems = checks.canonical_problems(_rehashed(packet))
    assert any("schema" in problem for problem in problems)


def test_rehashed_manifest_alone_is_not_enough(engine):
    """Digest self-consistency is necessary but not sufficient."""
    forged = _rehashed(_packet())
    forged["populations"]["repair_extraction_ids"] = forged[
        "populations"]["repair_extraction_ids"][:-1]
    forged["packet_digest"] = _digest(forged)
    terminal = runner.apply_packet(engine, forged, receipt=_receipt(),
                                   require_transactional_ddl=False)
    assert terminal["status"] == "refused"
    assert any("canonical binding" in problem for problem in terminal["problems"])
    assert terminal["mutations_performed"] == 0


# -- in-transaction enforcement ------------------------------------------------

def test_wrong_baseline_is_refused_inside_the_transaction(engine):
    packet = _packet()
    packet["baseline"]["counts"]["meetings_total"] = 999999
    # keep the receipt consistent with the packet so the in-transaction check is
    # the one that fires, rather than the receipt binding
    terminal = runner.apply_packet(
        engine, packet, receipt=_receipt(counts={"meetings_total": 999999}),
        require_transactional_ddl=False)
    assert terminal["status"] == "refused"
    assert any("baseline drift" in problem for problem in terminal["problems"])
    assert _counts(engine)["public_bodies"] == 0


def test_wrong_backup_receipt_binding_is_refused(engine):
    terminal = runner.apply_packet(engine, _packet(),
                                   receipt=_receipt(counts={"meetings_total": 999999}),
                                   require_transactional_ddl=False)
    assert terminal["status"] == "refused"
    assert any("backup receipt" in problem for problem in terminal["problems"])
    assert _counts(engine)["public_bodies"] == 0


def test_postconditions_run_on_the_active_transaction_connection(engine):
    """The check must see uncommitted writes, so it cannot use another connection."""
    seen = {}

    def spy(connection):
        seen["in_transaction"] = connection.in_transaction()
        seen["uncommitted_bodies"] = int(
            connection.execute(text("SELECT COUNT(*) FROM public_bodies")).scalar())
        seen["uncommitted_quarantined"] = int(connection.execute(text(
            "SELECT COUNT(*) FROM meeting_event_extractions "
            "WHERE quarantine_reason IS NOT NULL")).scalar())
        return {}

    terminal = runner.apply_packet(engine, _packet(), receipt=_receipt(),
                                   require_transactional_ddl=False,
                                   integrity_provider=spy)

    assert terminal["status"] == "applied", terminal["problems"]
    assert seen["in_transaction"] is True
    assert seen["uncommitted_bodies"] == 3
    assert seen["uncommitted_quarantined"] == 18


def test_postconditions_take_a_connection_not_an_engine(engine):
    """Passing an engine must fail fast rather than silently opening a connection."""
    with pytest.raises(Exception):
        checks.postconditions(engine, _packet(), 18, integrity_provider=lambda c: {})
