#!/usr/bin/env python3
"""B2 identity layer: alias model, plan refusal rules, traversal fixtures."""

from __future__ import annotations

import pathlib
import sys

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as A  # noqa: E402
from scripts.kg import stage3_b2_identity as ID  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"


def _pg():
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("these assertions read the live development tier")
    return engine


def _live(pattern, **need):
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    for p in hits:
        d = A.load_verified(p)
        if all(d.get(k) == v for k, v in need.items()):
            return d
    if not hits:
        pytest.skip(f"no {pattern} artifact yet")
    return A.load_verified(hits[-1])


# --- model / DDL ------------------------------------------------------------

def test_writer_namespaces_are_classified():
    assert ID.writer_namespace("publicmeetings-results-2026-june-260623001R") == \
        "phoenix_aem_publicmeetings_results"
    assert ID.writer_namespace("publicmeetings-notices-2026-February-260211006R") == \
        "phoenix_aem_publicmeetings_notices"
    assert ID.writer_namespace("902122", "https://phoenix.legistar.com/x") == "phoenix_legistar"
    assert ID.writer_namespace("City Council Formal Meeting-6/18/2025",
                               "https://phoenix.legistar.com/Calendar.aspx") == \
        "phoenix_legistar_calendar"
    assert ID.writer_namespace("phoenix-pdf-2022-01-05") == "phoenix_pdf"


def test_unknown_namespaces_fail_closed():
    assert ID.writer_namespace("") == ID.UNKNOWN_NAMESPACE
    assert ID.writer_namespace("something-else") == ID.UNKNOWN_NAMESPACE
    assert ID.UNKNOWN_NAMESPACE not in ID.TARGET_NAMESPACES


def test_the_registry_covers_every_observed_phoenix_writer():
    assert set(ID.WRITERS) == {"phoenix_aem_publicmeetings_results",
                               "phoenix_aem_publicmeetings_notices", "phoenix_legistar",
                               "phoenix_legistar_calendar", "phoenix_pdf"}
    assert ID.NAMESPACE_REGISTRY_VERSION.startswith("kg-stage3-writer-namespaces/")


def test_the_alias_table_pins_one_canonical_target_per_source_identity():
    sql = " ".join(ID.ddl())
    # P0-1: the identity key must include body
    assert "UNIQUE (source_system, body, external_id)" in sql
    assert "REFERENCES meetings(id)" in sql
    # P0-3: BOTH FKs restrict; nothing may disappear silently
    assert sql.count("ON DELETE RESTRICT") == 2
    assert "ON DELETE CASCADE" not in sql
    assert "ON DELETE SET NULL" not in sql


def test_the_alias_table_is_additive_and_non_destructive():
    sql = " ".join(ID.ddl()).upper()
    # Check for real destructive STATEMENTS, not incidental substrings:
    # "ON UPDATE CASCADE" is a referential action, not an UPDATE statement.
    for forbidden in ("DROP COLUMN", "DROP TABLE", "DELETE FROM", "TRUNCATE",
                      "ALTER COLUMN"):
        assert forbidden not in sql


def test_the_model_recommendation_prefers_the_alias_table_and_forbids_merging():
    ev = ID.options_evaluation()
    assert ev["chosen"] == "dedicated_alias_table"
    verdicts = {o["option"]: o["verdict"] for o in ev["options"]}
    assert verdicts["dedicated_alias_table"] == "recommended"
    assert verdicts["parent_pointer_on_meetings"] == "rejected"
    assert verdicts["merge_rows"] == "forbidden"
    assert ev["options"][2]["destructive"] is True


# --- data-plan refusal rules ------------------------------------------------

def test_the_live_data_plan_is_clean_and_collision_free():
    with _pg().connect() as c:
        doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
        # 12 proposed pairs minus 2 unsupported targets (P0-2)
        assert doc["operation_count"] == 10
        assert len(doc["dropped"]) == 2
        assert ID.validate_data_plan(doc) == []
        cc = doc["collision_checks"]
        assert cc["identity_key"] == "(source_system, body, external_id)"
        assert cc["identities_unique"] is True
        assert cc["self_reference_refused"] is True
        assert cc["all_rules_verified"] is True


def test_every_operation_is_fingerprinted_and_evidence_bound():
    doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
    for op in doc["operations"]:
        assert op["source_fingerprint"] and op["target_fingerprint"]
        assert op["evidence_sha256"]
        assert op["source_meeting_db_id"] != op["canonical_meeting_db_id"]
        assert op["canonical_agenda_items"] > 0


def test_the_plan_has_no_write_path_and_preserves_rows():
    doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
    assert doc["mode"] == "dry-run" and doc["applied"] is False
    assert doc["write_path"] == "absent by design"
    assert doc["no_merge"] is True and doc["no_row_deletion"] is True
    assert doc["bindings"]["crosswalk_digest"]


def test_the_schema_plan_is_additive_and_the_table_is_absent():
    doc = _live("kg-stage3-meeting-source-alias-schema-plan-*.json")
    assert doc["additive_only"] is True
    assert doc["touches_existing_columns"] is False
    assert doc["destructive"] is False
    assert doc["mode"] == "dry-run" and doc["write_path"] == "absent by design"
    assert doc["table"] == ID.ALIAS_TABLE
    assert doc["replay"]["expected_replay_writes"] == 0
    assert doc["bindings"]["data_plan_digest"]


# --- traversal fixtures -----------------------------------------------------

def _aliases():
    return _live("kg-stage3-meeting-source-alias-plan-*.json")["operations"]


def test_traversal_without_an_alias_holds():
    r = ID.traverse(_pg(), source_meeting_db_id=-1, item_number="1", aliases=_aliases())
    assert r["resolved"] is False and r["hold"] == "no_alias_for_source_meeting"


def test_traversal_without_an_item_number_holds():
    doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
    src = doc["operations"][0]["source_meeting_db_id"]
    r = ID.traverse(_pg(), source_meeting_db_id=src, item_number=None, aliases=_aliases())
    assert r["resolved"] is False and r["hold"] == "no_item_number_from_result_parse"


def test_traversal_resolves_a_real_numbered_item_or_holds_cleanly():
    doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
    src = doc["operations"][0]["source_meeting_db_id"]
    r = ID.traverse(_pg(), source_meeting_db_id=src, item_number="1", aliases=_aliases())
    assert r["resolved"] is True or r["hold"] in (
        "no_matching_item_number", "ambiguous_item_number")
    if r["resolved"]:
        assert r["agenda_item_db_id"] and r["canonical_meeting_db_id"]
        assert r["agenda_item_number"]


def test_traversal_never_resolves_an_absent_item_number():
    doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
    src = doc["operations"][0]["source_meeting_db_id"]
    r = ID.traverse(_pg(), source_meeting_db_id=src, item_number="99999",
                    aliases=_aliases())
    assert r["resolved"] is False and r["hold"] == "no_matching_item_number"


def test_the_recorded_traversal_proof_reconciles_and_records_hold_reasons():
    doc = _live("kg-stage3-meeting-source-alias-plan-*.json")
    tp = doc["traversal_proof"]
    assert tp["pairs"] == doc["operation_count"]
    assert tp["resolved"] + tp["held"] == tp["total"]
    assert tp["resolved"] > 0, "the alias layer must resolve at least some real items"
    # P1-4: every hold carries a reason
    for h in tp["holds"]:
        assert h.get("hold") and h.get("source_meeting_db_id")


def test_the_worksheet_and_backlog_cover_their_populations():
    ws = _live("kg-stage3-b2-contradiction-worksheet-*.json")
    assert ws["population"] == len(ws["cases"]) == 21
    assert ws["no_write_path"] is True
    for case in ws["cases"]:
        assert case["candidate_count"] >= 2
        assert case["candidates"]
        for c in case["candidates"]:
            assert "fired_rules" in c and "agenda_items" in c
    bl = _live("kg-stage3-b2-acquisition-backlog-*.json")
    assert bl["population"] == len(bl["entries"]) == 358
    assert bl["no_write_path"] is True


# --- fixture-based traversal (runs in the test tier, no live DB) ------------

def _fixture_engine():
    """An isolated SQLite fixture carrying only what traversal needs.

    A FILE-backed temp database, not ``sqlite://``: an in-memory SQLite database is
    private to each connection, so ``traverse`` would open an empty one and every
    traversal would silently look like "no rows" rather than being proven.
    """
    import tempfile
    from sqlalchemy import create_engine, text
    handle = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    handle.close()
    eng = create_engine(f"sqlite:///{handle.name}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT, "
                       "meeting_id TEXT, meeting_date TEXT, meeting_title TEXT, "
                       "source_url TEXT)"))
        c.execute(text("CREATE TABLE agenda_items (id INTEGER PRIMARY KEY, "
                       "meeting_db_id INTEGER, agenda_item_number TEXT)"))
        c.execute(text("INSERT INTO meetings VALUES "
                       "(1,'phoenix-cc','publicmeetings-results-2022-june-220607005r',"
                       "'2022-06-07','Council','http://a'),"
                       "(2,'phoenix-cc','902122','2022-06-07','Council','http://b')"))
        c.execute(text("INSERT INTO agenda_items VALUES "
                       "(10,2,'1'),(11,2,'2'),(12,2,'3')"))
    return eng


_FIXTURE_ALIASES = [{"source_meeting_db_id": 1, "canonical_meeting_db_id": 2,
                     "external_id": "publicmeetings-results-2022-june-220607005r"}]


def test_fixture_traversal_resolves_an_exact_numbered_item():
    r = ID.traverse(_fixture_engine().connect(), source_meeting_db_id=1, item_number="2",
                    aliases=_FIXTURE_ALIASES)
    assert r["resolved"] is True
    assert r["agenda_item_db_id"] == 11 and r["agenda_item_number"] == "2"
    assert r["canonical_meeting_db_id"] == 2


def test_fixture_traversal_normalizes_the_item_number():
    r = ID.traverse(_fixture_engine().connect(), source_meeting_db_id=1, item_number="02.",
                    aliases=_FIXTURE_ALIASES)
    assert r["resolved"] is True and r["agenda_item_number"] == "2"


def test_fixture_traversal_holds_when_the_number_is_absent():
    r = ID.traverse(_fixture_engine().connect(), source_meeting_db_id=1, item_number="9",
                    aliases=_FIXTURE_ALIASES)
    assert r["resolved"] is False and r["hold"] == "no_matching_item_number"


def test_fixture_traversal_holds_on_ambiguity():
    from sqlalchemy import text
    eng = _fixture_engine()
    with eng.begin() as c:
        c.execute(text("INSERT INTO agenda_items VALUES (13,2,'2')"))
    r = ID.traverse(eng.connect(), source_meeting_db_id=1, item_number="2", aliases=_FIXTURE_ALIASES)
    assert r["resolved"] is False and r["hold"] == "ambiguous_item_number"


def test_fixture_traversal_never_guesses_from_meeting_membership():
    """An item with no alias must not resolve, even though the meeting matches."""
    r = ID.traverse(_fixture_engine().connect(), source_meeting_db_id=2, item_number="1",
                    aliases=_FIXTURE_ALIASES)
    assert r["resolved"] is False and r["hold"] == "no_alias_for_source_meeting"


def test_fixture_traversal_is_deterministic():
    a = ID.traverse(_fixture_engine().connect(), source_meeting_db_id=1, item_number="3",
                    aliases=_FIXTURE_ALIASES)
    b = ID.traverse(_fixture_engine().connect(), source_meeting_db_id=1, item_number="3",
                    aliases=_FIXTURE_ALIASES)
    assert a == b


# --- adversarial validator tests (P0-1/2/3, P1-1/2/3/4) ---------------------

def _plan():
    return _live("kg-stage3-meeting-source-alias-plan-*.json")


def _resign(doc):
    doc.pop("digest", None)
    doc["digest"] = ID.canonical_sha256(doc)
    return doc


def test_a_tampered_digest_is_refused():
    import copy
    d = copy.deepcopy(_plan()); d["operations"][0]["rule"] = ["forged_rule"]
    assert any("digest" in p for p in ID.validate_data_plan(d))


def test_a_truncated_plan_is_refused():
    import copy
    d = _resign(copy.deepcopy(_plan()))
    d["operations"] = d["operations"][:-1]
    _resign(d)
    problems = ID.validate_data_plan(d)
    assert any("operation_count" in p for p in problems)


def test_an_extra_operation_is_refused_by_set_equality():
    import copy
    d = copy.deepcopy(_plan())
    expected = [o["source_meeting_db_id"] for o in d["operations"]][:-1]
    assert any("drift" in p for p in ID.validate_data_plan(d, expected_pairs=[
        (s, None) for s in expected]))


def test_a_self_reference_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][0]["canonical_meeting_db_id"] = d["operations"][0]["source_meeting_db_id"]
    _resign(d)
    assert any("self-reference" in p for p in ID.validate_data_plan(d))


def test_a_duplicate_source_identity_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][1]["external_id"] = d["operations"][0]["external_id"]
    d["operations"][1]["body"] = d["operations"][0]["body"]
    d["operations"][1]["source_system"] = d["operations"][0]["source_system"]
    _resign(d)
    assert any("duplicate source identity" in p for p in ID.validate_data_plan(d))


def test_missing_provenance_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][0]["rule"] = None
    _resign(d)
    assert any("missing provenance" in p for p in ID.validate_data_plan(d))


def test_wrong_target_namespace_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][0]["canonical_system"] = "phoenix_pdf"
    _resign(d)
    assert any("wrong target namespace" in p for p in ID.validate_data_plan(d))


def test_a_non_agenda_bearing_target_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][0]["canonical_agenda_items"] = 0
    _resign(d)
    assert any("agenda-bearing" in p for p in ID.validate_data_plan(d))


def test_a_wrong_body_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][0]["canonical_body"] = "some-other-body"
    _resign(d)
    assert any("wrong body" in p for p in ID.validate_data_plan(d))


def test_evidence_drift_is_refused():
    import copy
    d = copy.deepcopy(_plan())
    d["operations"][0]["evidence_sha256"] = "0" * 64
    _resign(d)
    assert any("evidence drift" in p for p in ID.validate_data_plan(d))


def test_a_cross_paired_plan_is_refused_live():
    """A plan whose pair does not hold against live data must be refused."""
    import copy
    from scripts.db.core import get_engine
    engine = get_engine()
    if engine.dialect.name != "postgresql":
        pytest.skip("live rule re-verification needs the development tier")
    d = copy.deepcopy(_plan())
    # cross-pair: point source 1 at another pair's canonical target
    d["operations"][0]["canonical_meeting_db_id"] = d["operations"][1]["canonical_meeting_db_id"]
    _resign(d)
    with engine.connect() as c:
        problems = ID.validate_data_plan(d, connection=c)
    assert any("rule no longer holds" in p for p in problems)


def test_an_unsafe_fk_action_is_refused():
    import copy
    d = copy.deepcopy(_live("kg-stage3-meeting-source-alias-schema-plan-*.json"))
    d["ddl"] = [s.replace("ON DELETE RESTRICT", "ON DELETE CASCADE") for s in d["ddl"]]
    _resign(d)
    assert any("unsafe FK action" in p or "RESTRICT" in p
               for p in ID.validate_schema_plan(d))


def test_a_key_without_body_is_refused():
    import copy
    d = copy.deepcopy(_live("kg-stage3-meeting-source-alias-schema-plan-*.json"))
    d["ddl"] = [s.replace("UNIQUE (source_system, body, external_id)",
                          "UNIQUE (source_system, external_id)") for s in d["ddl"]]
    _resign(d)
    assert any("must include body" in p for p in ID.validate_schema_plan(d))


def test_the_recorded_plans_refuse_after_apply_code_drift():
    assert any("stage3_b2_alias_apply.py" in problem
               for problem in ID.validate_data_plan(_plan()))
    assert any("stage3_b2_alias_apply.py" in problem for problem in
               ID.validate_schema_plan(
                   _live("kg-stage3-meeting-source-alias-schema-plan-*.json")))


def test_many_to_one_is_explicit_and_justified():
    d = _plan()
    for op in d["operations"]:
        if op["many_to_one"]:
            assert len(op["sibling_sources"]) > 1
            assert "many-to-one" in op["justification"]
            assert op["source_meeting_db_id"] in op["sibling_sources"]
        else:
            assert "one-to-one" in op["justification"]
    for target, sources in d["many_to_one"].items():
        assert len(sources) > 1


def test_dropped_targets_record_their_reason():
    d = _plan()
    assert d["dropped"], "at least one unsupported target must be recorded as dropped"
    for drop in d["dropped"]:
        assert drop["reason"]
    reasons = " ".join(x["reason"] for x in d["dropped"])
    assert "namespace" in reasons or "could not be re-verified" in reasons
