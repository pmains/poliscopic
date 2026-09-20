"""Runner target-assertion and guard-routing regressions (post-STOP corrections).

Proves the two pre-apply defects stay fixed:

1. ``assert_development_target`` runs on **every** path before database work, so a
   production or non-development target is refused in both read-only and apply
   mode.
2. The mutating-statement guard is installed **only** for read-only runs; the apply
   path must never carry it.

Also proves the apply reaches its transaction only after the authorization, digest
and backup checks, and that refusal paths mutate nothing.

Isolated: no PostgreSQL, no network, no real apply.
"""

from __future__ import annotations

import json
import types

import pytest
from sqlalchemy import create_engine, text

from scripts.kg import stage1_apply_runner as runner
from scripts.kg.phoenix_dr_adjudication import PlanError
from scripts.entities.event_normalize_preflight import ReadOnlyViolation

from test_kg_stage1_packet_v2 import (
    _counts,
    _digest,
    _packet,
    _receipt,
    stage1_test_engine,
)

PRODUCTION_URL = (
    "postgresql://poliscopic:***@tenant.example.ondigitalocean.com:25060/poliscopic"
)
WRONG_DB_URL = "postgresql://poliscopic:***@dev-host.internal:5432/poliscopic_staging"


class FakeTarget:
    """A target with only what ``assert_development_target`` inspects."""

    def __init__(self, url: str, dialect: str = "postgresql") -> None:
        self.url = url
        self.dialect = types.SimpleNamespace(name=dialect)


class RecordingEngine:
    """Proxy that counts transaction starts and otherwise delegates."""

    def __init__(self, engine) -> None:
        self._engine = engine
        self.begin_calls = 0

    @property
    def url(self):
        return self._engine.url

    @property
    def dialect(self):
        return self._engine.dialect

    def connect(self):
        return self._engine.connect()

    def begin(self):
        self.begin_calls += 1
        return self._engine.begin()


@pytest.fixture
def engine():
    return stage1_test_engine()


# -- (1) the development target is asserted on every path ----------------------

@pytest.mark.parametrize("apply_mode", [True, False])
def test_production_host_is_refused_in_both_modes(apply_mode):
    with pytest.raises(PlanError) as error:
        runner.configure_engine(FakeTarget(PRODUCTION_URL), apply=apply_mode)
    assert "production host" in str(error.value)


@pytest.mark.parametrize("apply_mode", [True, False])
def test_non_development_database_is_refused_in_both_modes(apply_mode):
    with pytest.raises(PlanError) as error:
        runner.configure_engine(FakeTarget(WRONG_DB_URL), apply=apply_mode)
    assert "expected poliscopic_dev" in str(error.value)


@pytest.mark.parametrize("apply_mode", [True, False])
def test_unsupported_dialect_is_refused_in_both_modes(apply_mode):
    with pytest.raises(PlanError):
        runner.configure_engine(FakeTarget("mysql://u:p@h/db", dialect="mysql"), apply=apply_mode)


def test_apply_packet_asserts_the_target_even_when_called_directly():
    """Direct callers must not be able to skip the target assertion."""
    terminal = runner.apply_packet(FakeTarget(PRODUCTION_URL), _packet(), receipt=_receipt())
    assert terminal["status"] == "refused"
    assert terminal["mutations_performed"] == 0
    assert any("target refused" in problem for problem in terminal["problems"])


def test_run_refuses_a_production_target_in_both_modes(tmp_path):
    packet = _packet()
    packet["packet_digest"] = _digest(packet)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    for apply_mode in (True, False):
        result = runner.run(FakeTarget(PRODUCTION_URL), str(path),
                            expected_digest=packet["packet_digest"], apply=apply_mode,
                            receipt=_receipt())
        assert result["status"] == "refused", apply_mode
        assert any("target refused" in problem for problem in result["problems"])


# -- (2) the guard is installed only for read-only runs ------------------------

def test_read_only_mode_installs_the_mutation_guard(engine):
    runner.configure_engine(engine, apply=False)
    with pytest.raises(ReadOnlyViolation):
        with engine.begin() as connection:
            connection.execute(text("INSERT INTO public_bodies (id) VALUES (1)"))
    assert _counts(engine)["public_bodies"] == 0


def test_apply_mode_does_not_install_the_mutation_guard(engine):
    runner.configure_engine(engine, apply=True)
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO public_bodies (id, body_code, name, slug, jurisdiction_id, "
            "created_at, updated_at) VALUES (1, 'x', 'n', 's', 1, now(), now())"))
    assert _counts(engine)["public_bodies"] == 1


# -- (3) the transaction is reached only after every check ---------------------

def test_apply_reaches_the_transaction_after_all_checks(engine):
    recording = RecordingEngine(engine)
    terminal = runner.apply_packet(recording, _packet(), receipt=_receipt(),
                                   require_transactional_ddl=False,
                                   integrity_provider=lambda connection: {})
    assert terminal["status"] == "applied", terminal["problems"]
    assert recording.begin_calls == 1
    assert terminal["rowcounts"] == {"public_bodies_inserts": 3, "meeting_updates": 55,
                                     "quarantine_updates": 18}


def test_bad_receipt_never_opens_a_transaction(engine):
    recording = RecordingEngine(engine)
    before = _counts(engine)
    terminal = runner.apply_packet(recording, _packet(), receipt=None,
                                   require_transactional_ddl=False)
    assert terminal["status"] == "refused"
    assert recording.begin_calls == 0
    assert _counts(engine) == before


def test_canonical_binding_failure_never_opens_a_transaction(engine):
    recording = RecordingEngine(engine)
    forged = _packet()
    forged["populations"]["meeting_scope"]["phoenix-dab"] = (
        forged["populations"]["meeting_scope"]["phoenix-dab"] + [14608])
    before = _counts(engine)
    terminal = runner.apply_packet(recording, forged, receipt=_receipt(),
                                   require_transactional_ddl=False)
    assert terminal["status"] == "refused"
    assert recording.begin_calls == 0
    assert _counts(engine) == before


def test_target_refusal_never_opens_a_transaction():
    recording = RecordingEngine(FakeTarget(PRODUCTION_URL))
    terminal = runner.apply_packet(recording, _packet(), receipt=_receipt())
    assert terminal["status"] == "refused"
    assert recording.begin_calls == 0


def test_unauthorized_packet_never_opens_a_transaction(engine, tmp_path):
    packet = _packet(adjudication={"authorization": {"apply_authorized": False}})
    packet["packet_digest"] = _digest(packet)
    path = tmp_path / "unauthorized.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    recording = RecordingEngine(engine)
    before = _counts(engine)
    result = runner.run(recording, str(path), expected_digest=packet["packet_digest"],
                        apply=True, receipt=_receipt())
    assert result["status"] == "refused"
    assert any("does not authorize" in problem for problem in result["problems"])
    assert recording.begin_calls == 0
    assert _counts(engine) == before


def test_wrong_digest_never_opens_a_transaction(engine, tmp_path):
    packet = _packet()
    packet["packet_digest"] = _digest(packet)
    path = tmp_path / "p.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    recording = RecordingEngine(engine)
    result = runner.run(recording, str(path), expected_digest="0" * 64, apply=True,
                        receipt=_receipt())
    assert result["status"] == "refused"
    assert recording.begin_calls == 0
    assert _counts(engine)["public_bodies"] == 0
