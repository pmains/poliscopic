#!/usr/bin/env python3
"""Stage 2 Step 2 — additive document→agenda-item attachment plan invariants.

The fixture is an isolated in-memory SQLite database shaped so that all five
classification outcomes appear exactly once.  Nothing here touches PostgreSQL
or a real database.
"""

from __future__ import annotations

import json
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s1_verify as s1_verify  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402
from scripts.kg import stage2_s2_verify as verify  # noqa: E402

_DDL = (
    """
    CREATE TABLE agenda_items (
        id INTEGER PRIMARY KEY,
        meeting_db_id INTEGER NOT NULL,
        agenda_item_number VARCHAR(32) NOT NULL,
        agenda_item_id VARCHAR(128) NOT NULL
    )
    """,
    """
    CREATE TABLE supporting_documents (
        id INTEGER PRIMARY KEY,
        meeting_db_id INTEGER NOT NULL,
        agenda_item_id VARCHAR(256) NOT NULL,
        agenda_item_number VARCHAR(32) NOT NULL,
        document_url VARCHAR(1024) NOT NULL,
        updated_at TIMESTAMP NOT NULL,
        body VARCHAR(256) NOT NULL DEFAULT ''
    )
    """,
)

# id, meeting, number, item_id
_ITEMS = (
    (101, 1, "7", "A7"),
    (102, 1, "0", "A0a"),
    (103, 1, "0", "A0b"),
    (104, 1, "9", "bos-9"),
    (105, 1, "3", "A3"),
    # two items share the number "8": a genuine ambiguity, unrelated to "0"
    (106, 1, "8", "P8"),
    (107, 1, "8", "Q8"),
)

# id, meeting, source key, item number, expected class.  "0" in *both* fields is
# the placeholder a writer stores when the document records no item reference at
# all, so such a row is never used for a link.
_DOCUMENTS = (
    (1, 1, "0", "7", "attached_item_number"),
    # both fields are the placeholder: unassigned, never linked, never ambiguous
    (2, 1, "0", "0", "unassigned_placeholder"),
    (3, 1, "A3", "99", "attached_source_key"),
    (4, 1, "0", "55", "gap_missing_target"),
    (5, 1, "0", "", "unassigned_placeholder"),
    # the item number is a placeholder, but the source key names one candidate
    (6, 1, "A0a", "0", "attached_source_key"),
    # a meeting-result key carries no item reference at all
    (7, 1, "result-phoenix-cc-publicmeetings-results-2025-october-251023004R",
     "77", "meeting_level_only"),
    # item number 8 matches two canonical items and the placeholder key names
    # neither: genuinely ambiguous, and held rather than guessed
    (8, 1, "0", "8", "held_ambiguous"),
)

EXPECTED_CLASSES = {row[0]: row[4] for row in _DOCUMENTS}


def _populate(engine) -> None:
    with engine.begin() as connection:
        for statement in _DDL:
            connection.execute(text(statement))
        for item in _ITEMS:
            connection.execute(
                text(
                    "INSERT INTO agenda_items "
                    "(id, meeting_db_id, agenda_item_number, agenda_item_id) "
                    "VALUES (:id, :meeting, :number, :key)"
                ),
                {"id": item[0], "meeting": item[1], "number": item[2], "key": item[3]},
            )
        for row in _DOCUMENTS:
            connection.execute(
                text(
                    "INSERT INTO supporting_documents "
                    "(id, meeting_db_id, agenda_item_id, agenda_item_number, "
                    " document_url, updated_at, body) "
                    "VALUES (:id, :meeting, :key, :number, :url, :stamp, :body)"
                ),
                {
                    "id": row[0],
                    "meeting": row[1],
                    "key": row[2],
                    "number": row[3],
                    "url": f"https://example.test/doc/{row[0]}",
                    "stamp": "2026-09-12 00:00:00",
                    "body": "test-body",
                },
            )


@pytest.fixture()
def fixture():
    """An isolated database containing exactly one document per class."""
    engine = create_engine("sqlite://")
    _populate(engine)
    return engine


def _baseline() -> dict:
    return {"integrity": {"orphan_mentions": 0}}


def _plan(engine) -> dict:
    return documents.build_plan(
        engine,
        _baseline(),
        # A coherent pair: the id is the timestamp form of created_at.
        plan_id="20260912T000000Z",
        created_at="2026-09-12T00:00:00+00:00",
    )


# ── classification cascade ──────────────────────────────────────────────


def test_every_document_lands_in_the_expected_class(fixture):
    with fixture.connect() as connection:
        files = documents.classify(connection)
    assert {int(e["document_id"]): e["class"] for e in files} == EXPECTED_CLASSES


def test_classification_covers_every_population_exactly_once(fixture):
    with fixture.connect() as connection:
        files = documents.classify(connection)
    counts = documents.populations(files)
    assert counts["total_documents"] == len(_DOCUMENTS)
    assert counts["deterministic_links"] == 3
    assert counts["held_total"] == 5
    assert sum(counts[name] for name in documents.CLASSES) == counts["total_documents"]


def test_meeting_item_number_match_binds_the_canonical_identity(fixture):
    """A placeholder source key still links, through the item number."""
    plan = _plan(fixture)
    linked = {a["document_id"]: a for a in plan["attachments"]}
    assert linked[1]["strategy"] == "meeting_item_number"
    assert linked[1]["agenda_item_db_id"] == 101
    assert linked[1]["agenda_item_fingerprint"]


def test_the_placeholder_key_is_never_treated_as_item_zero(fixture):
    """'0' must not be matched against canonical items numbered zero."""
    with fixture.connect() as connection:
        files = documents.classify(connection)
    by_id = {int(e["document_id"]): e for e in files}
    # Document 2 carries the placeholder in both fields: it records no item
    # reference, so it is unassigned rather than resolved against item 102/103.
    assert by_id[2]["class"] == "unassigned_placeholder"
    assert by_id[2]["agenda_item_db_id"] is None
    # Document 1 carries the same placeholder key but a real number, and links.
    assert by_id[1]["class"] == "attached_item_number"
    assert by_id[1]["agenda_item_db_id"] == 101


def test_placeholder_semantics_are_declared_not_inferred_from_counts():
    assert documents.PLACEHOLDER_SOURCE_KEY == "0"


def test_a_document_with_no_item_number_is_meeting_level(fixture):
    plan = _plan(fixture)
    held = {h["document_id"]: h for h in plan["holds"]}
    assert held[5]["class"] == "unassigned_placeholder"
    assert held[5]["reason"] == documents.HOLD_REASONS["unassigned_placeholder"]


def test_alternate_source_key_match_binds_its_own_identity(fixture):
    plan = _plan(fixture)
    linked = {a["document_id"]: a for a in plan["attachments"]}
    assert linked[3]["strategy"] == "alternate_source_key"
    assert linked[3]["agenda_item_db_id"] == 105


def test_a_placeholder_number_is_resolved_by_the_source_key(fixture):
    """A real source key still links when the item number says nothing."""
    plan = _plan(fixture)
    linked = {a["document_id"]: a for a in plan["attachments"]}
    assert linked[6]["strategy"] == "alternate_source_key"
    assert linked[6]["agenda_item_db_id"] == 102      # key "A0a", not "A0b"
    assert 6 not in {h["document_id"] for h in plan["holds"]}


def test_the_source_key_rule_does_not_rescue_a_placeholder(fixture):
    """A document whose key is only the placeholder stays unassigned."""
    plan = _plan(fixture)
    held = {h["document_id"]: h for h in plan["holds"]}
    assert held[2]["class"] == "unassigned_placeholder"
    assert "unassigned" in held[2]["reason"]


def test_a_meeting_result_key_is_meeting_level_not_a_missing_item(fixture):
    """Rule R2: result-{body}-{meeting_id} keys are meeting-level artefacts."""
    plan = _plan(fixture)
    held = {h["document_id"]: h for h in plan["holds"]}
    assert held[7]["class"] == "meeting_level_only"
    assert held[7]["source_key"].startswith(documents.MEETING_LEVEL_KEY_PREFIX)
    assert 7 not in {a["document_id"] for a in plan["attachments"]}


def test_every_document_is_accounted_for_exactly_once(fixture):
    """Exhaustive, disjoint arithmetic across all five classes."""
    plan = _plan(fixture)
    counts = plan["counts"]
    named = sum(counts[name] for name in documents.CLASSES)
    assert named == counts["total_documents"]
    assert counts["deterministic_links"] + counts["held_total"] == counts["total_documents"]
    linked = [a["document_id"] for a in plan["attachments"]]
    held = [h["document_id"] for h in plan["holds"]]
    assert len(linked) + len(held) == counts["total_documents"]
    assert set(linked).isdisjoint(set(held))
    assert len(set(linked) | set(held)) == counts["total_documents"]


def test_adjudication_groups_agree_with_the_rows(fixture):
    plan = _plan(fixture)
    assert verify.verify_adjudication(plan) == []
    adjudication = plan["adjudication"]
    assert sum(adjudication["links_by_strategy"].values()) == len(plan["attachments"])
    assert sum(adjudication["holds_by_class"].values()) == len(plan["holds"])
    assert adjudication["deterministic_rules"]
    assert adjudication["human_decisions_required"]


def test_adjudication_is_part_of_the_contract(fixture):
    plan = _plan(fixture)
    plan.pop("adjudication")
    assert any("adjudication" in p for p in verify.verify_plan_shape(plan))


# ── generated identity coherence ────────────────────────────────────────


def test_plan_identity_derives_the_id_from_the_instant():
    moment = __import__("datetime").datetime(
        2026, 9, 13, 0, 17, 41, tzinfo=__import__("datetime").timezone.utc)
    plan_id, created_at = documents.plan_identity(moment)
    assert plan_id == "20260913T001741Z"
    assert documents.identity_problems({"plan_id": plan_id, "created_at": created_at}) == []


def test_a_supplied_id_that_disagrees_with_the_clock_is_refused():
    moment = __import__("datetime").datetime(
        2026, 9, 13, 0, 17, 41, tzinfo=__import__("datetime").timezone.utc)
    with pytest.raises(ValueError):
        documents.plan_identity(moment, "20260913T011000Z")


def test_identity_problems_flags_a_misaligned_pair():
    problems = documents.identity_problems(
        {"plan_id": "20260913T011000Z", "created_at": "2026-09-13T00:12:00+00:00"})
    assert problems and "not the identity" in problems[0]
    assert documents.identity_problems({}) == ["plan carries no id or no created_at"]


def test_an_item_number_match_wins_over_the_placeholder_key(fixture):
    """The item number is primary; a placeholder number is not a number."""
    plan = _plan(fixture)
    assert plan["counts"]["attached_item_number"] == 1
    assert plan["counts"]["attached_source_key"] == 2
    assert plan["counts"]["unassigned_placeholder"] == 2


def test_ambiguous_document_is_held_and_never_linked(fixture):
    """A real ambiguity: two items numbered 8, and no key to choose between."""
    plan = _plan(fixture)
    assert 8 not in {a["document_id"] for a in plan["attachments"]}
    held = {h["document_id"]: h for h in plan["holds"]}
    assert held[8]["class"] == "held_ambiguous"
    assert held[8]["reason"] == documents.HOLD_REASONS["held_ambiguous"]


def test_an_unresolvable_item_number_is_a_missing_target_gap(fixture):
    plan = _plan(fixture)
    held = {h["document_id"]: h for h in plan["holds"]}
    assert held[4]["class"] == "gap_missing_target"
    assert "never acquired" in held[4]["reason"]


def test_no_item_number_means_unassigned_not_a_missing_target(fixture):
    plan = _plan(fixture)
    held = {h["document_id"]: h for h in plan["holds"]}
    assert held[5]["class"] == "unassigned_placeholder"
    assert held[5]["source_key"] == "0"


def test_planned_scope_is_unchanged_by_classification(fixture):
    """The plan describes a column; it never claims a row was written."""
    plan = _plan(fixture)
    assert plan["schema_impact"]["applied"] is False
    assert plan["sync_parity_impact"]["applied"] is False
    assert plan["schema_impact"]["add_column"]["nullable"] is True
    assert plan["schema_impact"]["preserved"]["column"] == "agenda_item_id"


# ── verification ────────────────────────────────────────────────────────


def test_a_faithful_plan_verifies_clean(fixture):
    plan = _plan(fixture)
    results = verify.verify(plan, fixture)
    # An in-memory SQLite engine has no host, port, or database name, so the
    # plan cannot name them and the target check correctly refuses.  That check
    # is exercised against an explicit stub below; every other check must pass.
    # A nameless SQLite engine has no host, port, or database to disagree with,
    # so binding is judged on the dialect alone; PostgreSQL names all four.
    assert results["target_binding"] == []
    assert {name: problems for name, problems in results.items() if problems} == {}


def test_a_faithful_plan_verifies_clean_against_a_named_engine(fixture):
    """With a matching named identity, every non-connection check passes."""
    plan = _plan(fixture)
    engine = _StubEngine()
    plan["target"] = documents.target_section(engine)
    checks = {
        "shape": verify.verify_plan_shape(plan),
        "arithmetic": verify.verify_population_arithmetic(plan),
        "disjointness": verify.verify_disjointness(plan),
        "ambiguity": verify.verify_ambiguity_membership(plan),
        "after_state": verify.verify_after_state(plan),
        "target_binding": verify.verify_target_binding(plan, engine),
    }
    assert {name: problems for name, problems in checks.items() if problems} == {}
    # ...and the connection-backed checks pass against the fixture as well.
    with fixture.connect() as connection:
        assert verify.verify_rows_unchanged(plan, connection) == []
        assert verify.verify_agenda_items(plan, connection) == []


def test_missing_required_key_is_refused(fixture):
    plan = _plan(fixture)
    plan.pop("expected_after_state")
    assert any("expected_after_state" in p for p in verify.verify_plan_shape(plan))


def test_arithmetic_that_does_not_add_up_is_refused(fixture):
    plan = _plan(fixture)
    plan["counts"]["meeting_level_only"] += 1
    problems = verify.verify_population_arithmetic(plan)
    assert problems


def test_a_document_cannot_be_both_linked_and_held(fixture):
    plan = _plan(fixture)
    plan["holds"].append(dict(plan["holds"][0], document_id=plan["attachments"][0]["document_id"]))
    problems = verify.verify_disjointness(plan)
    assert any("both linked and held" in p for p in problems)


def test_a_document_cannot_be_linked_twice(fixture):
    plan = _plan(fixture)
    plan["attachments"].append(dict(plan["attachments"][0]))
    problems = verify.verify_disjointness(plan)
    assert any("more than once" in p for p in problems)


def test_a_hold_must_carry_its_reason_and_no_target(fixture):
    plan = _plan(fixture)
    plan["holds"][0]["reason"] = "because"
    assert verify.verify_ambiguity_membership(plan)


def test_after_state_must_match_the_counts(fixture):
    plan = _plan(fixture)
    plan["expected_after_state"]["null"] += 1
    assert verify.verify_after_state(plan)


def test_drifted_document_row_is_detected(fixture):
    plan = _plan(fixture)
    with fixture.begin() as connection:
        connection.execute(
            text("UPDATE supporting_documents SET document_url = :u WHERE id = 1"),
            {"u": "https://example.test/moved"},
        )
    with fixture.connect() as connection:
        problems = verify.verify_rows_unchanged(plan, connection)
    assert any("document 1 drifted" in p for p in problems)


def test_missing_document_row_is_detected(fixture):
    plan = _plan(fixture)
    with fixture.begin() as connection:
        connection.execute(text("DELETE FROM supporting_documents WHERE id = 5"))
    with fixture.connect() as connection:
        problems = verify.verify_rows_unchanged(plan, connection)
    assert any("document 5 is missing" in p for p in problems)


def test_drifted_agenda_item_is_detected(fixture):
    plan = _plan(fixture)
    with fixture.begin() as connection:
        connection.execute(
            text("UPDATE agenda_items SET agenda_item_number = :n WHERE id = 101"),
            {"n": "7b"},
        )
    with fixture.connect() as connection:
        problems = verify.verify_agenda_items(plan, connection)
    assert any("agenda item 101 drifted" in p for p in problems)


def test_missing_agenda_item_is_detected(fixture):
    plan = _plan(fixture)
    with fixture.begin() as connection:
        connection.execute(text("DELETE FROM agenda_items WHERE id = 105"))
    with fixture.connect() as connection:
        problems = verify.verify_agenda_items(plan, connection)
    assert any("agenda item 105 is missing" in p for p in problems)


def test_bound_code_hash_drift_is_detected(fixture, monkeypatch):
    plan = _plan(fixture)
    monkeypatch.setattr(documents, "code_hashes", lambda: {})
    problems = verify.verify_code_hashes(plan)
    assert problems and all("missing" in p for p in problems)


def test_a_plan_without_code_hashes_is_refused(fixture):
    plan = _plan(fixture)
    plan["code_hashes"] = {}
    assert verify.verify_code_hashes(plan) == ["plan records no code hashes"]


def test_a_module_the_plan_does_not_bind_is_reported(fixture, monkeypatch):
    plan = _plan(fixture)
    extended = dict(plan["code_hashes"], **{"scripts/kg/new_module.py": "0" * 64})
    monkeypatch.setattr(documents, "code_hashes", lambda: extended)
    problems = verify.verify_code_hashes(plan)
    assert any("not bound by the plan" in p for p in problems)


# ── target binding ──────────────────────────────────────────────────────


class _StubUrl:
    def __init__(self, host, port, database):
        self.host = host
        self.port = port
        self.database = database


class _StubDialect:
    name = "postgresql"


class _StubEngine:
    def __init__(self, host="192.0.2.10", port=5432, database="poliscopic_dev"):
        self.url = _StubUrl(host, port, database)
        self.dialect = _StubDialect()


def test_target_binding_accepts_the_exact_development_engine():
    engine = _StubEngine()
    plan = {"target": documents.target_section(engine)}
    assert verify.verify_target_binding(plan, engine) == []


def test_target_binding_refuses_a_different_database():
    engine = _StubEngine()
    plan = {"target": dict(documents.target_section(engine), database="poliscopic_other")}
    problems = verify.verify_target_binding(plan, engine)
    assert problems and "database" in problems[0]


def test_target_binding_refuses_host_and_port_drift():
    engine = _StubEngine()
    for field, bad in (("host", "elsewhere.internal"), ("port", 5433)):
        plan = {"target": dict(documents.target_section(engine), **{field: bad})}
        problems = verify.verify_target_binding(plan, engine)
        assert problems and field in problems[0]


def test_target_binding_refuses_a_plan_that_names_no_target():
    engine = _StubEngine()
    problems = verify.verify_target_binding({"target": {}}, engine)
    assert len(problems) == len(verify.TARGET_FIELDS)


def test_target_normalization_never_equates_blank_with_a_real_value():
    assert verify.normalize_target({"host": "", "database": None, "port": 5432})["host"] is None
    assert verify.normalize_target({"port": "5432"})["port"] == 5432
    assert verify.normalize_target({"database": " Poliscopic_Dev "})["database"] == "poliscopic_dev"


# ── artifact immutability ───────────────────────────────────────────────


def test_plan_artifact_is_write_once_and_digest_bound(tmp_path):
    path = tmp_path / "plan.json"
    digest = artifacts.write_immutable(path, {"kind": "x"})
    assert artifacts.recorded_digest(json.loads(path.read_text())) == digest
    assert (path.stat().st_mode & 0o777) == 0o600
    with pytest.raises(artifacts.ArtifactCollision):
        artifacts.write_immutable(path, {"kind": "x"})


def test_a_tampered_plan_artifact_is_refused(tmp_path):
    path = tmp_path / "plan.json"
    artifacts.write_immutable(path, {"kind": "x", "n": 1})
    document = json.loads(path.read_text())
    document["n"] = 2
    path.write_text(json.dumps(document))
    with pytest.raises(artifacts.ArtifactDigestMismatch):
        artifacts.load_verified(path)


def test_step1_binding_helpers_are_reused_not_reimplemented():
    """One target-identity implementation, shared with Stage 2 Step 1."""
    assert verify.TARGET_FIELDS == s1_verify.TARGET_FIELDS


# ── source-key type contract ────────────────────────────────────────────


def test_the_orm_declares_the_source_key_as_text_not_an_integer():
    """The model must match the varchar column and every writer's string keys."""
    from scripts.db import models

    column = models.SupportingDocument.__table__.columns["agenda_item_id"]
    assert not isinstance(column.type, __import__("sqlalchemy").Integer)
    assert column.type.length == 256
    assert column.nullable is False


def test_every_repository_writer_stores_a_string_source_key():
    """The evidence for the type change: writers emit text, including "0"."""
    import re
    root = _REPO_ROOT
    writers = [
        "scripts/scraper/platforms/civicclerk.py",
        "scripts/scraper/platforms/onbase.py",
    ]
    for relative in writers:
        source = (root / relative).read_text()
        assert re.search(r'agenda_item_id"\]?\s*[:=]\s*"0"', source), relative


# ── superseding an older draft ──────────────────────────────────────────


def test_supersede_reference_records_the_replaced_plan(tmp_path):
    path = tmp_path / "old.json"
    artifacts.write_immutable(path, {
        "kind": documents.PLAN_KIND, "plan_id": "old",
        "code_hashes": documents.code_hashes(),
    })
    reference = documents.supersede_reference(path)
    assert reference["plan_id"] == "old"
    assert reference["obsolete"] is False
    assert reference["unbound_modules"] == []


def test_supersede_reference_flags_an_obsolete_unbound_draft(tmp_path):
    """A draft that binds fewer modules is named, not silently inherited."""
    path = tmp_path / "draft.json"
    artifacts.write_immutable(path, {
        "kind": documents.PLAN_KIND, "plan_id": "draft",
        "code_hashes": {"scripts/kg/stage2_s2_documents.py": "0" * 64},
    })
    reference = documents.supersede_reference(path)
    assert reference["obsolete"] is True
    assert "scripts/kg/stage2_s2_verify.py" in reference["unbound_modules"]


def test_supersede_reference_reuses_the_shared_loader(tmp_path):
    """One artifact loader; a tampered draft is refused, not read."""
    path = tmp_path / "draft.json"
    artifacts.write_immutable(path, {"kind": documents.PLAN_KIND, "code_hashes": {}})
    document = json.loads(path.read_text())
    document["kind"] = "tampered"
    path.write_text(json.dumps(document))
    with pytest.raises(artifacts.ArtifactDigestMismatch):
        documents.supersede_reference(path)


def test_a_plan_records_its_supersedes_link(fixture):
    plan = documents.build_plan(
        fixture, _baseline(), plan_id="p", created_at="t",
        supersedes={"plan_id": "old", "digest": "d", "obsolete": False,
                    "unbound_modules": []},
    )
    assert plan["supersedes"]["plan_id"] == "old"
    assert plan["supersedes"]["obsolete"] is False
