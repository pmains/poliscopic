"""Stage 1 execution-packet v2 and apply-runner tests.

Isolated: the apply paths run against a throwaway in-memory SQLite database that
mimics the three tables involved.  No PostgreSQL, no network, no real apply.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, event, text

from scripts.entities import detect_entities
from scripts.entities.event_normalize_artifacts import ArtifactCollision
from scripts.kg import stage1_apply_runner as runner
from scripts.kg import stage1_backup_receipt as receipts
from scripts.kg import stage1_execution_packet as packet_module
from scripts.kg import stage1_packet_components as components
from scripts.kg import stage1_runner_checks as checks
from scripts.kg.quarantine import quarantine_reason_slugs
from scripts.kg.registries import MODEL_VERSION

SCOPE = components.meeting_scope()
QUARANTINE_IDS = components.quarantine_extraction_ids()
SENTINEL_REASON = "scraper_sentinel_non_meeting"


@pytest.fixture(autouse=True)
def _no_integrity_queries(monkeypatch):
    """The integrity snapshot needs graph tables that this fixture does not build."""
    monkeypatch.setattr(detect_entities, "_integrity_snapshot", lambda engine: {})
    monkeypatch.setattr(detect_entities, "integrity_snapshot", lambda connection: {})


# -- scope and populations -----------------------------------------------------

def test_dab_scope_is_exactly_the_37_adjudicated_meetings():
    dab = SCOPE["phoenix-dab"]
    assert len(dab) == 37
    for excluded in (14608, 14629, 15331, 17049):
        assert excluded not in dab
    assert len(SCOPE["phoenix-dr"]) == 13
    assert len(SCOPE["phoenix-ds"]) == 5
    assert sum(len(ids) for ids in SCOPE.values()) == 55


def test_populations_are_exact_disjoint_and_complete():
    proof = components.scope_proof()
    assert proof["repair_count"] == 374
    assert proof["quarantine_count"] == 18
    assert proof["total"] == 392
    assert proof["disjoint"] is True
    assert proof["overlap"] == []
    assert proof["complete"] is True


def test_quarantine_ids_match_the_reviewed_artifact():
    assert list(QUARANTINE_IDS) == [31757, 31758, 31759, 31760, 31761, 31762,
                                    31763, 31764, 31765, 51301, 51302, 51303,
                                    51304, 51305, 51306, 51307, 51308, 51309]


def test_controlled_reason_is_registered():
    assert SENTINEL_REASON in quarantine_reason_slugs()
    proof = components.scope_proof()
    assert proof["repair_count"] + proof["quarantine_count"] == 392


# -- immutable bindings --------------------------------------------------------

def test_bound_components_all_verify():
    for component in components.verify_components():
        assert component["digest_ok"], (component["id"], component.get("problems"))


def test_unbound_or_tampered_artifact_is_refused(monkeypatch, tmp_path):
    binding = dict(components.PLAN_BINDINGS["phoenix-dr"])
    binding["sha256"] = "0" * 64
    document, problems = components.load_artifact(binding)
    assert document is None
    assert problems and "sha256" in problems[0]


def test_missing_artifact_is_refused():
    document, problems = components.load_artifact(
        {"path": "data/does-not-exist.json", "sha256": "0" * 64})
    assert document is None
    assert problems


# -- derived state -------------------------------------------------------------

def _operations():
    canonical_names = components.canonical_expectation()["canonical_body_names"]
    operations = []
    for body, ids in SCOPE.items():
        operations.append({
            "order": 2, "component": body, "op": "INSERT",
            "table": "public_bodies", "rows": 1,
            "audit_timestamp": components.audit_timestamp(),
            "values": {"body_code": body, "name": canonical_names[body],
                       "slug": body, "body_type": "Committee",
                       "jurisdiction_id": 4, "description": None}})
        operations.append({"order": 2, "component": body, "op": "UPDATE",
                           "table": "meetings", "rows": len(ids),
                           "target_ids": sorted(ids),
                           "set": {"jurisdiction_id": 4,
                                   "public_body_id": "(SELECT id FROM public_bodies "
                                                     f"WHERE body_code = '{body}')"},
                           "set_null_tracked": ["jurisdiction_id", "public_body_id"]})
    return operations


def test_expected_state_is_derived_from_operations_and_validated():
    baseline = {"counts": {"meetings_null_public_body": 1483,
                           "meetings_null_jurisdiction": 60,
                           "public_bodies_total": 284}}
    derived = packet_module.derive_expected_state(baseline, _operations(), 18)
    assert derived["validated"] is True
    assert derived["expected"]["public_bodies_total"] == 287
    assert derived["expected"]["meetings_null_public_body"] == 1428
    # every in-scope meeting gets jurisdiction_id, so all 55 nulls are consumed
    assert derived["expected"]["meetings_null_jurisdiction"] == 5
    assert derived["expected"]["quarantined_extractions"] == 18


def test_derivation_records_each_count_once_with_its_steps():
    baseline = {"counts": {"meetings_null_public_body": 1483,
                           "meetings_null_jurisdiction": 60,
                           "public_bodies_total": 284}}
    derived = packet_module.derive_expected_state(baseline, _operations(), 18)
    counts = {entry["count"] for entry in derived["derivation"]}
    assert counts == {"meetings_null_public_body", "meetings_null_jurisdiction",
                      "public_bodies_total", "quarantined_extractions"}
    for entry in derived["derivation"]:
        assert entry["steps"], entry["count"]


# -- quarantine values and placeholders ---------------------------------------

def test_adjudication_supplies_the_human_fields():
    """The approved decision fills the human fields; no placeholder remains."""
    values = packet_module.quarantine_values()

    assert values["quarantine_reason"] == SENTINEL_REASON
    assert values["model_version"] == MODEL_VERSION
    assert values["human_fields_supplied"] is True
    assert values["apply_blocked_until_supplied"] is False
    # apply is now authorized (2026-09-12T02:00:00Z), so this is no longer blocked
    assert values["apply_blocked_by_authorization"] is False
    assert values["quarantined_by"] == "Peter Mains"
    assert values["decision_id"] == "kg-stage1-20260911-skip-meeting-15841"
    assert values["quarantined_at"] == "2026-09-12T00:28:45Z"


def _packet(**overrides):
    """A packet that matches the canonical expectation by construction."""
    expectation = components.canonical_expectation()
    packet = {
        "packet_version": runner.PACKET_VERSION,
        "applied": False,
        "components": [
            {"id": body, **binding, "digest_ok": True}
            for body, binding in expectation["component_paths"].items()
        ] + [{"id": "schema.quarantine_columns", **expectation["code_fingerprint"]}],
        "bindings": {"adjudication_artifact": expectation["adjudication_artifact"],
                     "quarantine_artifact": expectation["quarantine_artifact"]},
        "populations": {"repair_extraction_ids": expectation["repair_ids"],
                        "quarantine_extraction_ids": expectation["quarantine_ids"],
                        "meeting_scope": expectation["meeting_scope"]},
        "counts": {"public_body_inserts": 3, "meeting_updates": 55,
                   "quarantine_updates": 18},
        "verification": {"ready_for_human_adjudication": True},
        "adjudication": {"authorization": {"apply_authorized": True,
                                           "backup_authorized": False}},
        "quarantine": {"values": packet_module.quarantine_values(),
                       "target_ids": list(QUARANTINE_IDS)},
        "operations": _operations(),
        "baseline": {"counts": {"meetings_total": 55, "meetings_null_public_body": 55,
                                "meetings_null_jurisdiction": 55, "public_bodies_total": 0,
                                "supporting_documents_total": 0}},
        "expected_after_state": {"meetings_total": 55, "meetings_null_public_body": 0,
                                 "meetings_null_jurisdiction": 0, "public_bodies_total": 3,
                                 "quarantined_extractions": 18,
                                 "meeting_updates": 55},
    }
    packet.update(overrides)
    return packet


def test_verify_packet_requires_the_exact_digest():
    packet = _packet()
    problems = runner.verify_packet(packet, "wrong-digest")
    assert any("!= expected" in problem for problem in problems)
    assert runner.verify_packet(packet, None) == ["an exact --packet-digest is required"]


def test_verify_packet_refuses_wrong_counts():
    packet = _packet(counts={"public_body_inserts": 3, "meeting_updates": 41,
                             "quarantine_updates": 18})
    problems = runner.verify_packet(packet, "x")
    assert any("meeting_updates must be 55" in problem for problem in problems)


def test_load_packet_refuses_a_tampered_manifest(tmp_path):
    packet = _packet()
    packet["packet_digest"] = hashlib.sha256(
        json.dumps({k: v for k, v in packet.items() if k != "packet_digest"},
                   sort_keys=True, default=str).encode()).hexdigest()
    target = tmp_path / "packet.json"
    target.write_text(json.dumps(packet), encoding="utf-8")
    document, problems = runner.load_packet(str(target))
    assert not problems
    # tamper: change a bound value without recomputing the digest
    packet["counts"]["meeting_updates"] = 54
    target.write_text(json.dumps(packet), encoding="utf-8")
    document, problems = runner.load_packet(str(target))
    assert any("digest" in problem for problem in problems)


# -- backup receipt ------------------------------------------------------------

def _digest(packet):
    body = {key: value for key, value in packet.items() if key != "packet_digest"}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def _receipt(**overrides):
    # overrides are applied *before* the counts signature is computed, so a test
    # overriding counts still produces a self-consistent receipt
    counts = overrides.pop("counts", {"meetings_total": 55})
    supplied_signatures = overrides.pop("signatures", None)
    receipt = {
        "dump_path": "/backups/poliscopic_dev.dump",
        "dump_sha256": "a" * 64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "target": {"tier": "development", "host": "devhost", "database": "poliscopic_dev"},
        "scratch": {"host": "devhost", "database": "poliscopic_restore_scratch"},
        "pg_restore": {"exit_code": 0, "evidence": "pg_restore completed cleanly"},
        "counts": counts,
        "signatures": supplied_signatures or {
            "schema_sha256": "b" * 64,
            "counts_sha256": receipts.counts_fingerprint(counts)},
    }
    receipt.update(overrides)
    return receipt


def test_valid_receipt_passes():
    assert receipts.validate_receipt(_receipt())["valid"] is True


@pytest.mark.parametrize("override,expected", [
    ({"pg_restore": {"exit_code": 1, "evidence": "boom"}}, "exit_code"),
    ({"target": {"tier": "production", "host": "prod", "database": "poliscopic"}}, "production"),
    ({"scratch": {"host": "devhost", "database": "poliscopic_dev"}}, "scratch"),
    ({"dump_sha256": "not-a-digest"}, "dump_sha256"),
    ({"created_at": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}, "older"),
    ({"postgres_password": "hunter2"}, "credential"),
    ({"signatures": {"schema_sha256": "b" * 64, "counts_sha256": "c" * 64}}, "counts_sha256"),
])
def test_bad_receipts_are_refused(override, expected):
    result = receipts.validate_receipt(_receipt(**override))
    assert result["valid"] is False
    assert any(expected in problem for problem in result["problems"])


def test_missing_fields_are_refused():
    receipt = _receipt()
    del receipt["pg_restore"]
    result = receipts.validate_receipt(receipt)
    assert any("missing required field" in problem for problem in result["problems"])


# -- apply runner --------------------------------------------------------------

#: Fixed per-connection value so the reviewed ``now()`` expression is exercisable on
#: SQLite, mirroring PostgreSQL's transaction-start behaviour (every statement in a
#: transaction observes the same timestamp).
FIXED_NOW = "2026-09-12 02:00:00+00:00"


def _register_now(dbapi_connection, _record):
    """SQLite has no ``now()``; register one so the reviewed SQL is really used."""
    dbapi_connection.create_function("now", 0, lambda: FIXED_NOW)


def stage1_test_engine():
    """Engine whose schema faithfully mirrors the PostgreSQL NOT NULL contract.

    ``public_bodies.created_at``/``updated_at`` are NOT NULL with **no default**,
    exactly as on the development database, so an insert that omits them fails here
    too.  ``now()`` is registered so the reviewed timestamp expression is genuinely
    executed rather than stubbed out.
    """
    engine = create_engine("sqlite://")
    event.listen(engine, "connect", _register_now)
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE meetings (id INTEGER PRIMARY KEY, public_body_id INTEGER, "
            "jurisdiction_id INTEGER, body TEXT)"))
        connection.execute(text(
            "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT, "
            "name TEXT NOT NULL, slug TEXT NOT NULL, body_type TEXT, "
            "jurisdiction_id INTEGER NOT NULL, description TEXT, "
            "created_at TIMESTAMP NOT NULL, updated_at TIMESTAMP NOT NULL)"))
        connection.execute(text(
            "CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, body TEXT)"))
        connection.execute(text(
            "CREATE TABLE meeting_event_extractions (id INTEGER PRIMARY KEY, "
            "meeting_event_id INTEGER, supporting_doc_id INTEGER, extractor TEXT, "
            "extractor_version TEXT, raw_text TEXT, confidence REAL, created_at TEXT, "
            "action_verb TEXT, text_offset_start INTEGER, text_offset_end INTEGER, "
            "case_number TEXT)"))
        for body, ids in SCOPE.items():
            for meeting_id in ids:
                connection.execute(
                    text("INSERT INTO meetings (id, public_body_id, jurisdiction_id, body) "
                         "VALUES (:i, NULL, NULL, :b)"), {"i": meeting_id, "b": body})
        for extraction_id in QUARANTINE_IDS:
            connection.execute(
                text("INSERT INTO meeting_event_extractions (id, meeting_event_id, "
                     "supporting_doc_id) VALUES (:i, 1, 112947)"), {"i": extraction_id})
    return engine


@pytest.fixture
def engine():
    return stage1_test_engine()


def _counts(engine):
    """Observed counts; the quarantine column may not exist yet."""
    with engine.connect() as connection:
        columns = {row[1] for row in connection.execute(
            text("PRAGMA table_info(meeting_event_extractions)"))}
        quarantined = 0
        if "quarantine_reason" in columns:
            quarantined = int(connection.execute(text(
                "SELECT COUNT(*) FROM meeting_event_extractions "
                "WHERE quarantine_reason IS NOT NULL")).scalar())
        return {
            "meetings_null_public_body": int(connection.execute(text(
                "SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")).scalar()),
            "meetings_null_jurisdiction": int(connection.execute(text(
                "SELECT COUNT(*) FROM meetings WHERE jurisdiction_id IS NULL")).scalar()),
            "public_bodies": int(connection.execute(
                text("SELECT COUNT(*) FROM public_bodies")).scalar()),
            "quarantined": quarantined,
        }


def test_apply_requires_an_explicit_flag(tmp_path):
    packet = _packet()
    path = tmp_path / "p.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    result = runner.run(engine=None, packet_path=str(path), expected_digest="x", apply=False)
    assert result["status"] == "refused"
    assert result["applied"] is False


def test_wrong_receipt_refuses_and_mutates_nothing(engine):
    before = _counts(engine)
    terminal = runner.apply_packet(engine, _packet(), require_transactional_ddl=False,
                                   receipt=_receipt(pg_restore={"exit_code": 1, "evidence": "failed"}))
    assert terminal["status"] == "refused"
    assert terminal["mutations_performed"] == 0
    assert _counts(engine) == before


def test_unauthorized_apply_refuses_and_mutates_nothing(engine, tmp_path):
    """The adjudication does not authorize apply; the runner must refuse."""
    packet = _packet(adjudication={"authorization": {"apply_authorized": False}})
    packet["packet_digest"] = _digest(packet)
    path = tmp_path / "unauthorized-packet.json"
    path.write_text(json.dumps(packet), encoding="utf-8")
    before = _counts(engine)

    result = runner.run(engine, str(path), expected_digest=packet["packet_digest"],
                        apply=True, receipt=_receipt())

    assert result["status"] == "refused"
    assert any("does not authorize" in problem for problem in result["problems"])
    assert _counts(engine) == before


def test_collision_is_refused(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO public_bodies (id, body_code, name, slug, body_type, "
            "jurisdiction_id, created_at, updated_at) "
            "VALUES (1, 'phoenix-dr', 'Phoenix Design Review Committee', "
            "'phoenix-design-review-committee', 'Committee', 4, now(), now())"))
    with engine.connect() as connection:
        problems = checks.preflight_rechecks(connection, _packet())
    assert any("already exists" in problem for problem in problems)


def test_baseline_drift_is_refused(engine):
    packet = _packet()
    packet["baseline"]["counts"]["meetings_null_public_body"] = 999
    with engine.connect() as connection:
        problems = checks.preflight_rechecks(connection, packet)
    assert any("baseline drift" in problem for problem in problems)


def test_successful_apply_has_exact_counts(engine):
    """The runner must issue exactly 3 inserts, 55 meeting updates and 18 quarantines.

    Durability across the DDL boundary is asserted here only for the dialect the
    runner accepts by default (PostgreSQL).  SQLite auto-commits DDL, so it is
    refused by default and is used here purely to exercise the transaction
    mechanics; the counts below are the runner's own in-transaction evidence.
    """
    terminal = runner.apply_packet(engine, _packet(), receipt=_receipt(),
                                   require_transactional_ddl=False)

    assert terminal["status"] == "applied", terminal["problems"]
    assert terminal["rowcounts"] == {"public_bodies_inserts": 3, "meeting_updates": 55,
                                     "quarantine_updates": 18}
    outcomes = terminal["postconditions"]
    assert outcomes["satisfied"] is True, outcomes["mismatches"]
    assert outcomes["observed"]["public_bodies_total"] == 3
    assert outcomes["observed"]["meetings_null_public_body"] == 0
    assert outcomes["observed"]["meetings_null_jurisdiction"] == 0
    assert outcomes["quarantined"] == 18
    assert outcomes["parented_meetings"] == 55


def test_mid_transaction_postcondition_failure_rolls_everything_back(engine):
    before = _counts(engine)
    packet = _packet()
    packet["expected_after_state"]["public_bodies_total"] = 99
    terminal = runner.apply_packet(engine, packet, receipt=_receipt(),
                                   require_transactional_ddl=False)
    assert terminal["status"] == "rolled_back"
    assert terminal["applied"] is False
    assert terminal["mutations_performed"] == 0
    assert _counts(engine) == before
    # No quarantine row survives.  Column removal is dialect-dependent: SQLite
    # auto-commits DDL, which is exactly why the runner refuses such dialects by
    # default and the packet documents a staged rollback.
    with engine.connect() as connection:
        assert int(connection.execute(text(
            "SELECT COUNT(*) FROM meeting_event_extractions WHERE decision_id IS NOT NULL"
        )).scalar()) == 0


def test_runner_refuses_a_dialect_without_transactional_ddl(engine):
    """The production default must fail closed on SQLite."""
    terminal = runner.apply_packet(engine, _packet(), receipt=_receipt())
    assert terminal["status"] == "refused"
    assert any("cannot run DDL" in problem for problem in terminal["problems"])
    assert terminal["mutations_performed"] == 0


def test_terminal_receipt_is_immutable(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(components, "DATA_DIR", tmp_path)
    terminal = runner.apply_packet(engine, _packet(), receipt=_receipt(),
                                   require_transactional_ddl=False)
    path = runner.write_terminal_receipt(terminal)
    assert path.endswith(".json")
    with pytest.raises(ArtifactCollision):
        runner.write_terminal_receipt(terminal)


# -- parity authority ----------------------------------------------------------

def test_runner_is_the_sole_authority_for_the_quarantine_schema():
    parity = runner.parity_expectations("postgresql")
    from scripts.db import quarantine_schema

    assert parity["statements"] == list(
        quarantine_schema.statements_for_review("postgresql")["up"])
    assert parity["additive"] is True
    assert parity["additive_conflict"] == []
    assert parity["quarantine_columns"] == sorted(quarantine_schema.COLUMN_DDL)


def test_runner_and_packet_agree_on_the_schema_authority():
    parity = runner.parity_expectations("postgresql")
    assert parity["sole_authority"].endswith("stage1_apply_runner.py")
    assert runner.PACKET_VERSION == packet_module.PACKET_VERSION
    assert packet_module.QUARANTINE_REASON == SENTINEL_REASON
