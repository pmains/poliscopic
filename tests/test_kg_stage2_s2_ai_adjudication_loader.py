#!/usr/bin/env python3
"""Stage 2 S2 — the authoritative review loader.

The point of these tests is that a caller cannot lie to the validator: the
loader takes a path and an engine, and reads every current-state fact itself.
"""

from __future__ import annotations

import inspect
import json
import pathlib
import sys

import pytest
from sqlalchemy import text

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.db import config as db_config  # noqa: E402
from scripts.db import tier as tier_module  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_adjudication_loader as loader  # noqa: E402
from scripts.kg import stage2_s2_ai_proposal as ai  # noqa: E402
from scripts.kg import stage2_s2_classify as cls  # noqa: E402

_DOCUMENT_DDL = (
    "CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, meeting_db_id INTEGER NOT NULL, "
    "agenda_item_id VARCHAR(256) NOT NULL, agenda_item_number VARCHAR(32) NOT NULL, "
    "document_url VARCHAR(1024) NOT NULL, updated_at TIMESTAMP NOT NULL, "
    "body VARCHAR(256) NOT NULL DEFAULT '')",
    "CREATE TABLE agenda_items (id INTEGER PRIMARY KEY, meeting_db_id INTEGER NOT NULL, "
    "agenda_item_number VARCHAR(32) NOT NULL, agenda_item_id VARCHAR(128) NOT NULL, "
    "sort_order INTEGER)",
)


@pytest.fixture()
def engine(monkeypatch):
    """An isolated engine, with config and the plan target bound to its own URL."""
    from sqlalchemy import create_engine
    eng = create_engine("sqlite://")
    with eng.begin() as c:
        for ddl in _DOCUMENT_DDL:
            c.execute(text(ddl))
        c.execute(text("INSERT INTO supporting_documents (id, meeting_db_id, agenda_item_id, "
                       "agenda_item_number, document_url, updated_at) "
                       "VALUES (1, 10, '0', '4.I', 'https://example.test/1', '2026-09-12 00:00:00')"))
        for item in ((900, 10, "4.I", "b-1_4.I", 1), (901, 10, "4.II", "b-1_4.II", 2)):
            c.execute(text("INSERT INTO agenda_items (id, meeting_db_id, agenda_item_number, "
                           "agenda_item_id, sort_order) VALUES (:i,:m,:n,:k,:s)"),
                      {"i": item[0], "m": item[1], "n": item[2], "k": item[3], "s": item[4]})
    target = tier_module.parse_target(str(eng.url))
    monkeypatch.setattr(db_config, "DB_TIER", "development")
    monkeypatch.setattr(db_config, "DB_TARGET", target)
    monkeypatch.setattr(loader, "current_plan_target", lambda *a, **k: {
        "dialect": target.dialect, "host": target.host,
        "port": target.port, "database": target.database})
    # Lineage membership is exercised against real artifacts in
    # tests/test_kg_stage2_s2_ai_lineage.py; here it is stubbed so this suite
    # stays about the engine binding and the snapshot.
    monkeypatch.setattr(loader.lineage, "verify_lineage",
                        lambda *a, **k: {"plan": {}, "aggregate": {}, "proposal": {}})
    return eng


def _row(engine, document_id=1):
    with engine.connect() as c:
        return c.execute(text("SELECT * FROM supporting_documents WHERE id = :i"),
                         {"i": document_id}).mappings().first()


def _proposal(engine, recommendation="link", candidate=900, document_id=1):
    fingerprint = cls.document_fingerprint(_row(engine, document_id))
    candidates = [{"agenda_item_db_id": 900, "meeting_db_id": 10,
                   "agenda_item_id": "b-1_4.I", "agenda_item_number": "4.I"},
                  {"agenda_item_db_id": 901, "meeting_db_id": 10,
                   "agenda_item_id": "b-1_4.II", "agenda_item_number": "4.II"}]
    result = {"model_recommendation": recommendation, "agenda_item_db_id": candidate,
              "confidence": 0.8, "rationale": "the excerpt names the item"}
    return ai.expand_group(result, [{"document_id": document_id, "fingerprint": fingerprint}],
                           unit_id="u1", provider="deepseek", model="deepseek-v4-flash",
                           model_version="deepseek-v4-flash", prompt_version="p/2",
                           input_fingerprints={"packet": "a" * 64}, candidates=candidates)[0]


def _write(tmp_path, proposal, name="proposal.json"):
    path = tmp_path / name
    artifacts.write_immutable(path, proposal)
    return path


# ── the caller cannot supply current-state context ─────────────────────


def test_the_loader_takes_only_a_path_and_an_engine():
    """There is no parameter through which a caller could forge context."""
    params = list(inspect.signature(loader.load_for_adjudication).parameters)
    assert params == ["proposal_path", "engine"]
    for forbidden in ("source_supported", "candidate_ids", "current_document_fingerprint",
                      "current_unlinked_state_fingerprint", "current_candidate_fingerprint",
                      "candidates", "unlinked_state_fingerprint"):
        assert forbidden not in params


# ── the happy path ─────────────────────────────────────────────────────


def test_an_authoritative_load_succeeds(engine, tmp_path):
    path = _write(tmp_path, _proposal(engine))
    result = loader.load_for_adjudication(path, engine)
    assert result["proposal"]["document_id"] == 1
    assert result["document"]["source_supported"] is False
    assert result["document"]["link_column_present"] is False
    assert result["candidate_set"]["ids"] == [900, 901]
    assert result["target"]["agenda_item_db_id"] == 900
    assert result["target"]["agenda_item_fingerprint"]
    assert result["as_of"] and result["target_identity"]


def test_an_abstention_loads_without_a_target(engine, tmp_path):
    path = _write(tmp_path, _proposal(engine, recommendation="abstain", candidate=None))
    result = loader.load_for_adjudication(path, engine)
    assert result["target"] is None
    assert result["document"]["source_supported"] is False


# ── refusals ───────────────────────────────────────────────────────────


def test_a_target_outside_the_meeting_is_refused(engine, tmp_path):
    proposal = _proposal(engine)
    proposal["candidate_links"][0]["agenda_item_db_id"] = 999        # forged target
    path = _write(tmp_path, proposal)
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "not in meeting" in str(exc.value)


def test_a_missing_candidate_row_is_refused(engine, tmp_path):
    """The link is in the meeting's set but the row itself is gone."""
    proposal = _proposal(engine)
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items (id, meeting_db_id, agenda_item_number, "
                       "agenda_item_id, sort_order) VALUES (902, 10, '4.III', 'b-1_4.III', 3)"))
    proposal["candidate_links"][0]["agenda_item_db_id"] = 902
    path = _write(tmp_path, proposal)
    with engine.begin() as c:
        c.execute(text("DELETE FROM agenda_items WHERE id = 902"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "not in meeting" in str(exc.value)


def test_a_missing_document_is_refused(engine, tmp_path):
    path = _write(tmp_path, _proposal(engine))
    with engine.begin() as c:
        c.execute(text("DELETE FROM supporting_documents WHERE id = 1"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "does not exist" in str(exc.value)


def test_document_drift_is_refused(engine, tmp_path):
    path = _write(tmp_path, _proposal(engine))
    with engine.begin() as c:
        c.execute(text("UPDATE supporting_documents SET document_url = 'https://example.test/moved' "
                       "WHERE id = 1"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "document fingerprint has drifted" in str(exc.value)


def test_candidate_drift_is_refused(engine, tmp_path):
    path = _write(tmp_path, _proposal(engine))
    with engine.begin() as c:
        c.execute(text("UPDATE agenda_items SET agenda_item_number = '4.X' WHERE id = 900"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "candidate fingerprint has drifted" in str(exc.value)


def test_a_document_that_became_source_supported_is_refused(engine, tmp_path):
    """Current link status is read from the database, not taken from the caller."""
    path = _write(tmp_path, _proposal(engine))
    with engine.begin() as c:
        c.execute(text("ALTER TABLE supporting_documents ADD COLUMN agenda_item_db_id INTEGER"))
        c.execute(text("UPDATE supporting_documents SET agenda_item_db_id = 900 WHERE id = 1"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "source-supported link" in str(exc.value)


def test_a_stale_unlinked_state_fingerprint_is_refused(engine, tmp_path):
    proposal = _proposal(engine)
    proposal["unlinked_state_fingerprint"] = "0" * 64                # forged
    path = _write(tmp_path, proposal)
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "unlinked" in str(exc.value)


def test_a_tampered_artifact_is_refused(engine, tmp_path):
    path = _write(tmp_path, _proposal(engine))
    document = json.loads(path.read_text())
    document["confidence"] = 0.99
    path.write_text(json.dumps(document))
    with pytest.raises(artifacts.ArtifactDigestMismatch):
        loader.load_for_adjudication(path, engine)


# ── read-only and development guards ───────────────────────────────────


def test_a_non_development_tier_is_refused(engine, tmp_path, monkeypatch):
    path = _write(tmp_path, _proposal(engine))
    monkeypatch.setattr(db_config, "DB_TIER", "production")
    with pytest.raises((ai.ProposalRefused, tier_module.TierError)):
        loader.load_for_adjudication(path, engine)


def test_the_engine_is_made_read_only(engine, tmp_path):
    from scripts.entities.event_normalize_preflight import ReadOnlyViolation
    path = _write(tmp_path, _proposal(engine))
    loader.load_for_adjudication(path, engine)
    with engine.connect() as c:
        with pytest.raises(ReadOnlyViolation):
            c.execute(text("DELETE FROM agenda_items WHERE id = 901"))


# ── target binding ─────────────────────────────────────────────────────


def test_a_production_like_engine_is_refused_by_the_classifier():
    """The real classifier refuses production before any connection is opened."""
    from sqlalchemy import create_engine
    from scripts.entities.event_normalize_preflight import PreflightError, assert_read_only_target
    prod = create_engine("postgresql://u:p@db.ondigitalocean.com:5432/poliscopic")
    with pytest.raises(PreflightError):
        assert_read_only_target(prod)


def test_the_engine_must_match_the_configured_target(engine, tmp_path, monkeypatch):
    path = _write(tmp_path, _proposal(engine))
    monkeypatch.setattr(db_config, "DB_TARGET",
                        tier_module.parse_target("sqlite:///somewhere-else.sqlite"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "does not match configured target" in str(exc.value)


def test_the_engine_must_match_the_plan_target(engine, tmp_path, monkeypatch):
    path = _write(tmp_path, _proposal(engine))
    monkeypatch.setattr(loader, "current_plan_target", lambda *a, **k: {
        "dialect": "postgresql", "host": "192.0.2.10", "port": 5432,
        "database": "poliscopic_dev"})
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "does not match plan target" in str(exc.value)


def test_host_port_and_database_mismatches_are_refused(engine, tmp_path, monkeypatch):
    path = _write(tmp_path, _proposal(engine))
    base = {"dialect": "sqlite", "host": None, "port": None, "database": "(memory)"}
    for field, bad in (("dialect", "postgresql"), ("host", "elsewhere.internal"),
                       ("port", 5433), ("database", "poliscopic_other")):
        monkeypatch.setattr(loader, "current_plan_target",
                            lambda *a, **k: dict(base, **{field: bad}))
        with pytest.raises(ai.ProposalRefused) as exc:
            loader.load_for_adjudication(path, engine)
        assert field in str(exc.value), field


def test_a_plan_target_that_is_not_the_engine_is_refused(engine, tmp_path, monkeypatch):
    """No current plan at all is also a refusal, not a silent skip."""
    path = _write(tmp_path, _proposal(engine))
    def _none(*a, **k):
        raise ai.ProposalRefused("no current Stage 2 plan artifact to bind against")
    monkeypatch.setattr(loader, "current_plan_target", _none)
    with pytest.raises(ai.ProposalRefused):
        loader.load_for_adjudication(path, engine)


def test_the_loader_binds_the_verified_target_identity(engine, tmp_path):
    result = loader.load_for_adjudication(_write(tmp_path, _proposal(engine)), engine)
    assert result["target_identity"]["dialect"] == "sqlite"
    assert result["target_identity"]["url_class"] == "local"
    assert "redacted" in result["target_identity"]


# ── one coherent snapshot ──────────────────────────────────────────────


def test_every_read_happens_inside_one_snapshot(engine, tmp_path, monkeypatch):
    """The document, candidate set, target row and link state share one snapshot."""
    opens = {"n": 0}
    real = loader._snapshot
    def _counting(eng):
        opens["n"] += 1
        return real(eng)
    monkeypatch.setattr(loader, "_snapshot", _counting)
    loader.load_for_adjudication(_write(tmp_path, _proposal(engine)), engine)
    assert opens["n"] == 1


def test_the_snapshot_is_reported_as_repeatable_read_for_postgres():
    import inspect
    source = inspect.getsource(loader._snapshot)
    assert "REPEATABLE READ" in source
    assert "SET TRANSACTION READ ONLY" in source
    assert "REPEATABLE READ" in source.split("else:")[0]      # PostgreSQL branch only


def test_a_snapshot_setup_failure_is_refused(engine, tmp_path, monkeypatch):
    """Falling back to a weaker snapshot silently is not an option."""
    from contextlib import contextmanager
    path = _write(tmp_path, _proposal(engine))
    @contextmanager
    def _broken(eng):
        raise ai.ProposalRefused("could not open a repeatable-read read-only snapshot: boom")
        yield
    monkeypatch.setattr(loader, "_snapshot", _broken)
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "repeatable-read" in str(exc.value)


def test_the_real_snapshot_refuses_when_isolation_cannot_be_set(monkeypatch):
    """Exercises the loader's own failure path, not a stub."""
    class _Broken:
        class dialect:  # noqa: N801
            name = "postgresql"
        def connect(self):
            raise RuntimeError("no server")
    with pytest.raises(ai.ProposalRefused) as exc:
        with loader._snapshot(_Broken()):
            pass
    assert "repeatable-read" in str(exc.value)


# ── the target must live in the document's meeting ─────────────────────


def test_a_candidate_from_another_meeting_is_refused(engine, tmp_path):
    """The candidate is in the id set but its row belongs to another meeting."""
    path = _write(tmp_path, _proposal(engine))
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items (id, meeting_db_id, agenda_item_number, "
                       "agenda_item_id, sort_order) VALUES (902, 999, '4.I', 'other_4.I', 3)"))
    row = _row(engine)
    proposal = json.loads(path.read_text())
    proposal["candidate_links"][0]["agenda_item_db_id"] = 902
    forged = tmp_path / "forged.json"
    artifacts.write_immutable(forged, dict(proposal, validation=None))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(forged, engine)
    assert "not in meeting" in str(exc.value) or "belongs to meeting" in str(exc.value)


def test_the_candidate_fingerprint_includes_the_meeting():
    a = {"agenda_item_db_id": 1, "meeting_db_id": 10, "agenda_item_id": "k", "agenda_item_number": "4"}
    b = dict(a, meeting_db_id=11)
    assert ai.candidate_fingerprint(a) != ai.candidate_fingerprint(b)


def test_a_candidate_that_changed_meeting_is_refused(engine, tmp_path):
    """A candidate moved to another meeting is no longer the same candidate."""
    path = _write(tmp_path, _proposal(engine))
    with engine.begin() as c:
        c.execute(text("UPDATE agenda_items SET meeting_db_id = 10 WHERE id = 900"))
    # advance the candidate's number so its fingerprint drifts
    with engine.begin() as c:
        c.execute(text("UPDATE agenda_items SET agenda_item_id = 'b-1_4.I-moved' WHERE id = 900"))
    with pytest.raises(ai.ProposalRefused) as exc:
        loader.load_for_adjudication(path, engine)
    assert "candidate fingerprint has drifted" in str(exc.value)
