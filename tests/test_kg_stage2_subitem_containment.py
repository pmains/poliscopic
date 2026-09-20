#!/usr/bin/env python3
"""agenda_subitem containment — P1 audit corrections, adversarially tested.

Covers the three closing defects: the manifest must bind the **plan builder** and
recompute every hash; the plan must bind a **focused agenda_items schema** and the
exact target identity, with accurate enforcement claims; and the plan must
**canonically load** the named baseline, prove the projection is exact, and
rederive the collision population and the operation set.
"""

from __future__ import annotations

import copy
import pathlib
import sys

import pytest
from sqlalchemy import create_engine, text

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_subitem_containment as SC  # noqa: E402
from scripts.kg import stage2_subitem_manifest as SM  # noqa: E402
from scripts.kg import stage2_subitem_plan as SP  # noqa: E402
from scripts.kg import stage2_subitem_schema as SS  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"
_TARGET = {"dialect": "postgresql", "host": "192.0.2.10", "port": 5432,
           "database": "poliscopic_dev", "tier": "development"}


def _live(kind):
    """The one live (non-obsolete) artifact of a kind, resolved rather than named."""
    hits = [p for p in sorted(_PLANS.glob(f"kg-stage2-subitem-containment-{kind}-*.json"))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{kind}: {[h.name for h in hits]}"
    return hits[0]


def _item(item_id, number, meeting=10, body="b"):
    return {"id": item_id, "meeting_db_id": meeting, "body": body,
            "agenda_item_number": number}


#: A tiny population exercising root, subitem, collision and cross-meeting rows.
SMALL_ITEMS = [
    _item(1, "4"), _item(2, "4.A"), _item(3, "5"),
    _item(4, "0"), _item(5, "0"),
    _item(6, "7", meeting=20), _item(7, "7.B", meeting=20),
]


def _sqlite_schema(unique=True):
    """A real agenda_items-shaped table, so the schema signature is genuine.

    The unique variant carries an explicit named index rather than an inline
    ``UNIQUE`` constraint: SQLite backs a table constraint with an auto-index that
    SQLAlchemy's inspector filters out, which would make two genuinely different
    schemas look identical.
    """
    engine = create_engine("sqlite://")
    with engine.begin() as c:
        c.execute(text("CREATE TABLE agenda_items (id INTEGER PRIMARY KEY, "
                       "meeting_db_id INTEGER, agenda_item_number TEXT, "
                       "agenda_item_title TEXT, agenda_item_id TEXT, sort_order INTEGER)"))
        if unique:
            c.execute(text("CREATE UNIQUE INDEX ux_agenda_items_natural_key "
                           "ON agenda_items (meeting_db_id, agenda_item_number)"))
        else:
            c.execute(text("CREATE INDEX ix_agenda_items_meeting "
                           "ON agenda_items (meeting_db_id)"))
    return engine


def _small_baseline(tmp_path, items=None):
    """Write a real baseline artifact and return its path."""
    baseline = SC.audit(list(items or SMALL_ITEMS))
    baseline["created_at"] = "2026-09-12T22:00:00+00:00"
    baseline["rows_sha256"] = SM.canonical_sha256(baseline["rows"])
    path = tmp_path / "kg-stage2-subitem-containment-baseline-tiny.json"
    artifacts.write_immutable(path, baseline)
    return path


#: The broader database signature the manifest binds, so a tiny plan is complete.
FULL_SIGNATURE = {"schema_sha256": "s" * 64, "table_count": 1}


def _signature(unique=True):
    """A genuine agenda_items signature read from a real (SQLite) table."""
    engine = _sqlite_schema(unique=unique)
    with engine.connect() as connection:
        return SS.read_schema_signature(connection)


def _small_plan(tmp_path, *, schema=None, items=None):
    """Build a real plan over a tiny on-disk baseline, and return all four."""
    path = _small_baseline(tmp_path, items)
    loaded = artifacts.load_verified(path)
    signature = schema if schema is not None else _signature()
    plan = SP.build_plan(loaded, baseline_path=path,
                         created_at="2026-09-12T22:00:00+00:00", target=_TARGET,
                         schema_signature=FULL_SIGNATURE,
                         agenda_items_schema=signature)
    return plan, loaded, path, signature


# ── identity, parsing, collision dispositions ──────────────────────────

@pytest.mark.parametrize("raw,expected", [
    ("4", "4"), ("4.A", "4.A"), ("4.AA", "4.AA"), (" 4.a ", "4.A"), ("4.", "4"),
])
def test_normalization(raw, expected):
    assert SC.normalize(raw) == expected


def test_hierarchy_parsing_and_parent():
    assert SC.parse_hierarchy("4.AA")["prefix"] == "4"
    assert SC.parent_of("4.A") == "4"
    assert SC.parent_of("4") is None
    assert SC.parse_hierarchy("22C") is None


def test_classification_shapes():
    assert SC.classify_item("4", siblings=["4"])["class"] == SC.CLASS_ROOT
    assert SC.classify_item("4.A", siblings=["4", "4.A"])["class"] == SC.CLASS_SUBITEM
    assert SC.classify_item("4.AA", siblings=["4", "4.AA"])["class"] == SC.CLASS_SUBITEM
    assert SC.classify_item("4.A", siblings=["4.A"])["class"] == SC.CLASS_AMBIGUOUS
    assert SC.classify_item("22C", siblings=["22C"])["class"] == SC.CLASS_AMBIGUOUS
    assert SC.classify_item("  ", siblings=[])["class"] == SC.CLASS_INVALID


def test_a_letter_is_never_the_child_of_the_item_above_it():
    assert SC.classify_item("B", siblings=["4", "B"])["parent"] is None


def test_colliding_rows_receive_a_collision_disposition():
    base = SC.audit([_item(1, "4"), _item(2, "4"), _item(3, "5")])
    classes = {r["item_id"]: r["class"] for r in base["rows"]}
    assert classes[1] == SC.CLASS_COLLISION and classes[2] == SC.CLASS_COLLISION
    assert classes[3] == SC.CLASS_ROOT
    assert base["counts"][SC.CLASS_COLLISION] == 2


def test_the_collision_population_is_counted_and_digested():
    base = SC.audit([_item(1, "4"), _item(2, "4"), _item(3, "5"), _item(4, "5"),
                     _item(5, "5")])
    pop = base["collision_population"]
    assert pop["distinct_keys"] == 2
    assert pop["involved_rows"] == 5
    assert pop["excess"] == 3
    assert pop["keys_digest"] == SM.canonical_sha256(pop["keys"])
    assert pop["row_ids_digest"] == SM.canonical_sha256(pop["row_ids"])


def test_a_colliding_row_never_carries_a_parent():
    base = SC.audit([_item(1, "4"), _item(2, "4.A"), _item(3, "4.A")])
    for row in base["rows"]:
        if row["class"] == SC.CLASS_COLLISION:
            assert row["parent"] is None


def test_collisions_do_not_leak_into_the_class_population():
    base = SC.audit([_item(1, "4"), _item(2, "4")])
    assert sum(base["counts"].values()) == 2
    assert base["population"]["reconciles"] is True


# ── P1(1): the manifest binds the builder and recomputes every hash ────

def test_the_manifest_binds_the_plan_builder():
    assert "scripts/kg/stage2_subitem_plan.py" in SM.CODE_MODULES
    recorded = artifacts.load_verified(_live("plan"))["manifest"]
    assert set(recorded["code_hashes"]) == set(SM.CODE_MODULES)
    for required in SM.CODE_MODULES:
        assert required in recorded["code_hashes"]


def test_the_recorded_manifest_detects_semantic_registry_drift():
    manifest = artifacts.load_verified(_live("plan"))["manifest"]
    problems = SM.validate_manifest(manifest)
    assert any("scripts/kg/registries/evidence.py" in p and "stale" in p
               for p in problems)
    assert set(manifest["semantic_dependencies"]) == set(SM.SEMANTIC_DEPENDENCIES)


def test_a_new_manifest_recomputes_every_code_and_registry_hash(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = plan["manifest"]
    assert SM.validate_manifest(manifest) == []
    assert manifest["code_hashes"] == SM.code_hashes()
    assert manifest["semantic_dependencies"] == SM.dependency_hashes()


def test_an_omitted_builder_in_the_manifest_is_refused(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = copy.deepcopy(plan["manifest"])
    manifest["code_hashes"].pop("scripts/kg/stage2_subitem_plan.py")
    problems = SM.validate_manifest(manifest)
    assert any("stage2_subitem_plan.py" in p for p in problems)


def test_an_unknown_module_in_the_manifest_is_refused(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = copy.deepcopy(plan["manifest"])
    manifest["code_hashes"]["scripts/kg/not_a_module.py"] = "0" * 64
    assert any("unknown file" in p for p in SM.validate_manifest(manifest))


def test_a_stale_code_hash_is_refused(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = copy.deepcopy(plan["manifest"])
    for relative in manifest["code_hashes"]:
        manifest["code_hashes"][relative] = "0" * 64
    problems = SM.validate_manifest(manifest)
    assert any("stale" in p for p in problems)


def test_a_drifted_semantic_registry_hash_is_refused(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = copy.deepcopy(plan["manifest"])
    first = sorted(manifest["semantic_dependencies"])[0]
    manifest["semantic_dependencies"][first] = "0" * 64
    problems = SM.validate_manifest(manifest)
    assert any("semantic dependency" in p and "stale" in p for p in problems)


def test_a_dropped_semantic_registry_is_refused(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = copy.deepcopy(plan["manifest"])
    manifest["semantic_dependencies"].pop(sorted(manifest["semantic_dependencies"])[0])
    problems = SM.validate_manifest(manifest)
    assert any("binds no semantic dependency hash" in p for p in problems)


def test_the_registry_set_cannot_be_silently_narrowed():
    manifest = artifacts.load_verified(_live("plan"))["manifest"]
    assert len(manifest["semantic_dependencies"]) == len(SM.SEMANTIC_DEPENDENCIES)
    trimmed = copy.deepcopy(manifest)
    trimmed["semantic_dependencies"] = {sorted(trimmed["semantic_dependencies"])[0]:
                                        trimmed["semantic_dependencies"][
                                            sorted(trimmed["semantic_dependencies"])[0]]}
    assert SM.validate_manifest(trimmed)


def test_the_manifest_binds_no_document_decision_authority():
    manifest = artifacts.load_verified(_live("plan"))["manifest"]
    assert set(manifest["excluded_authority"]) == {
        "s2_decisions", "proposal_aggregate", "document_labels"}
    for forbidden in ("decisions", "lineage", "aggregate"):
        assert forbidden not in manifest
    assert manifest["source_authority"]["model_output_used"] is False
    assert manifest["source_authority"]["document_link_authority_used"] is False
    assert manifest["source_authority"]["table"] == "agenda_items"


def test_a_manifest_claiming_model_output_is_refused(tmp_path):
    plan, _, _, _ = _small_plan(tmp_path)
    manifest = copy.deepcopy(plan["manifest"])
    manifest["source_authority"]["model_output_used"] = True
    assert any("model output" in p for p in SM.validate_manifest(manifest))


# ── P1(2): focused agenda_items schema + exact target identity ─────────

def test_the_schema_signature_covers_columns_types_nullability_and_defaults():
    signature = _signature()
    assert signature["table"] == "agenda_items"
    assert signature["primary_key"] == ["id"]
    names = [c["name"] for c in signature["columns"]]
    for expected in ("id", "meeting_db_id", "agenda_item_number", "agenda_item_title",
                     "agenda_item_id", "sort_order"):
        assert expected in names
    for column in signature["columns"]:
        assert set(column) == {"name", "type", "nullable", "default"}
    assert body_digest(signature) == signature["digest"]


def body_digest(signature):
    return SS.canonical_sha256({k: v for k, v in signature.items() if k != "digest"})


def test_the_schema_signature_covers_indexes_and_foreign_keys():
    signature = _signature()
    unique_indexes = [i for i in signature["indexes"] if i["unique"]]
    assert unique_indexes, signature["indexes"]
    assert unique_indexes[0]["name"] == "ux_agenda_items_natural_key"
    assert unique_indexes[0]["columns"] == ["meeting_db_id", "agenda_item_number"]
    assert isinstance(signature["foreign_keys"], list)


def test_two_different_schemas_have_different_signatures():
    with_unique = _signature(unique=True)
    without = _signature(unique=False)
    assert with_unique["digest"] != without["digest"]


def test_the_plan_binds_the_agenda_items_schema_and_target_identity(tmp_path):
    plan, _, _, signature = _small_plan(tmp_path)
    contract = plan["schema_readiness"]
    assert contract["agenda_items_schema"] == signature
    identity = contract["target_identity"]
    assert identity["database"] == "poliscopic_dev"
    assert identity["tier"] == "development"
    assert identity["digest"] == SS.target_identity(_TARGET)["digest"]


def test_the_bound_schema_must_equal_the_authoritative_one(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    assert SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                            authoritative_schema=signature) == []
    drifted = copy.deepcopy(signature)
    drifted["columns"] = [c for c in drifted["columns"] if c["name"] != "sort_order"]
    drifted["digest"] = body_digest(drifted)
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=drifted)
    assert any("not the authoritative current schema" in p for p in problems)


def test_a_schema_drift_changes_the_target_signature_and_is_refused():
    other = _signature(unique=False)
    contract = SS.build_contract(target=_TARGET, agenda_items_schema=other,
                                 schema_signature=FULL_SIGNATURE, column_present=False)
    authoritative = _signature(unique=True)
    problems = SS.validate_contract(contract, authoritative_schema=authoritative)
    assert any("authoritative current schema" in p for p in problems)


def test_a_drifted_target_identity_is_refused():
    contract = SS.build_contract(target={**_TARGET, "tier": "production"},
                                 agenda_items_schema=_signature(),
                                 schema_signature=FULL_SIGNATURE, column_present=False)
    problems = SS.validate_contract(contract, authoritative_target=_TARGET)
    assert any("target identity" in p for p in problems)


def test_the_contract_records_which_rules_are_check_constraints():
    contract = SS.build_contract(target=_TARGET,
                                 agenda_items_schema=_signature(),
                                 schema_signature=FULL_SIGNATURE, column_present=False)
    enforcement = contract["enforcement"]
    assert enforcement["self_reference"]["expressible_as_check"] is True
    for rule in ("same_meeting", "number_shortening", "no_cycles"):
        assert enforcement[rule]["expressible_as_check"] is False
        assert "NOT" in enforcement["statement"] or "not" in enforcement[rule]["why"]
    assert "subquery" in enforcement["same_meeting"]["why"]
    assert "cannot read another row" in enforcement["number_shortening"]["why"]
    assert "closure property" in enforcement["no_cycles"]["why"]
    assert SS.validate_contract(contract) == []


def test_an_enforcement_claim_that_a_check_does_the_work_is_refused():
    contract = SS.build_contract(target=_TARGET,
                                 agenda_items_schema=_signature(),
                                 schema_signature=FULL_SIGNATURE, column_present=False)
    broken = copy.deepcopy(contract)
    broken["enforcement"]["same_meeting"]["expressible_as_check"] = True
    assert any("NOT expressible" in p for p in SS.validate_contract(broken))


def test_the_schema_contract_is_design_only_and_unapplied():
    contract = artifacts.load_verified(_live("plan"))["schema_readiness"]
    assert contract["kind"] == SS.SCHEMA_KIND
    assert contract["design_only"] is True and contract["implemented"] is False
    assert contract["observed"]["column_present"] is False


def test_the_schema_contract_records_every_prerequisite():
    contract = artifacts.load_verified(_live("plan"))["schema_readiness"]
    for key in ("backup", "apply", "replay", "rollback", "ordering"):
        assert contract["prerequisites"].get(key)


def test_the_schema_contract_keeps_attached_to_separate():
    separation = artifacts.load_verified(_live("plan"))["schema_readiness"]["separation"]
    assert "attached_to" in separation and "no document column" in separation["attached_to"]


def test_a_plan_without_a_schema_contract_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["schema_readiness"] = {}
    assert any("schema-readiness" in p or "kind must be" in p
               for p in SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                         authoritative_schema=signature))


# ── P1(3): canonical baseline load, exact projection, rederived collisions ──

def test_the_plan_binds_the_loaded_baseline_by_digest_and_rows(tmp_path):
    plan, baseline, path, _ = _small_plan(tmp_path)
    bound = plan["manifest"]["baseline"]
    assert bound["path"] == path.name
    assert bound["canonical_digest"] == artifacts.recorded_digest(baseline)
    assert bound["rows_digest"] == SM.canonical_sha256(baseline["rows"])
    assert bound["row_count"] == len(baseline["rows"])


def test_the_snapshot_is_the_exact_projection(tmp_path):
    plan, baseline, _, _ = _small_plan(tmp_path)
    assert SP.validate_projection(plan, baseline) == []
    assert plan["baseline_projection"]["projection_is_exact"] is True
    assert sorted(plan["baseline_projection"]["projection_fields"]) == \
        sorted(SP.SNAPSHOT_FIELDS)
    assert plan["baseline_snapshot"] == SP._snapshot(baseline)


def test_a_substituted_snapshot_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["baseline_snapshot"][0]["item_id"] = 999999
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("exact projection" in p or "digest" in p for p in problems)


def test_a_truncated_snapshot_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["baseline_snapshot"] = plan["baseline_snapshot"][:-1]
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("exact projection" in p or "digest" in p for p in problems)


def test_a_reordered_snapshot_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["baseline_snapshot"] = list(reversed(plan["baseline_snapshot"]))
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("exact projection" in p for p in problems)


def test_a_snapshot_from_another_baseline_is_refused(tmp_path):
    """The classic substitution: a plan whose snapshot came from a different run."""
    plan, baseline, path, signature = _small_plan(tmp_path)
    other_items = SMALL_ITEMS + [_item(8, "9.C")]
    other_baseline = SC.audit(other_items)
    plan = copy.deepcopy(plan)
    plan["baseline_snapshot"] = SP._snapshot(other_baseline)
    plan["baseline_rows_digest"] = SM.canonical_sha256(plan["baseline_snapshot"])
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("exact projection" in p for p in problems)


def test_a_projection_that_disclaims_exactness_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["baseline_projection"]["projection_is_exact"] = False
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("exact projection" in p for p in problems)


def test_a_missing_baseline_artifact_is_refused(tmp_path):
    plan, _, path, signature = _small_plan(tmp_path)
    path.unlink()
    problems = SP.validate_plan(plan, baseline_path=path, plan_dir=tmp_path,
                                authoritative_schema=signature)
    assert any("could not be loaded" in p for p in problems)


def test_an_obsolete_baseline_artifact_is_refused(tmp_path):
    plan, _, path, signature = _small_plan(tmp_path)
    artifacts.record_obsolete(tmp_path, path, "superseded for the test")
    problems = SP.validate_plan(plan, baseline_path=path, plan_dir=tmp_path,
                                authoritative_schema=signature)
    assert any("obsolete" in p or "could not be loaded" in p for p in problems)


def test_the_collision_population_is_rederived(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    assert SP.validate_collisions(plan, plan["baseline_snapshot"]) == []
    expected = SC.collision_population([
        {"id": r["item_id"], "meeting_db_id": r["meeting_db_id"],
         "agenda_item_number": r["number"]} for r in plan["baseline_snapshot"]])
    assert plan["collision_population"] == expected
    assert plan["collision_population"]["distinct_keys"] == 1
    assert plan["collision_population"]["involved_rows"] == 2


def test_a_deleted_collision_key_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["collision_population"]["keys"] = []
    plan["collision_population"]["distinct_keys"] = 0
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("re-derived" in p or "rederived" in p or "collision" in p
               for p in problems)


def test_an_altered_collision_count_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["collision_population"]["involved_rows"] += 1
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("involved_rows" in p for p in problems)


def test_a_forged_collision_digest_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["collision_population"]["keys_digest"] = "0" * 64
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("keys_digest" in p for p in problems)


def test_deleting_a_collision_row_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["collision_population"]["row_ids"] = []
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("row_ids" in p for p in problems)


# ── operation set: exact equality, rederived from the loaded baseline ──

def test_the_operation_set_is_exactly_rederived(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    assert SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                            authoritative_schema=signature) == []
    assert len(plan["operations"]) == 2
    assert plan["accounting"]["operation_set_digest"] == \
        SM.canonical_sha256(plan["operations"])


def test_a_missing_operation_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["operations"] = plan["operations"][:-1]
    assert any("not exactly the rederived canonical set" in p
               for p in SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                         authoritative_schema=signature))


def test_a_duplicated_operation_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["operations"] = plan["operations"] + [plan["operations"][0]]
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("rederived canonical set" in p or "more than one operation" in p
               for p in problems)


def test_a_substituted_parent_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["operations"][0]["parent_item_id"] = 999999
    assert any("does not match the rederived set" in p
               for p in SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                         authoritative_schema=signature))


def test_a_substituted_evidence_hash_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["operations"][0]["evidence_sha256"] = "0" * 64
    assert any("evidence_sha256" in p
               for p in SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                         authoritative_schema=signature))


def test_a_non_parent_link_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    plan["operations"][0]["parent_number"] = "9"
    assert any("is not the parent of" in p
               for p in SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                         authoritative_schema=signature))


def test_a_self_containing_item_is_refused(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    op = plan["operations"][0]
    op["parent_item_id"] = op["child_item_id"]
    problems = SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                authoritative_schema=signature)
    assert any("cannot contain itself" in p or "different meetings" in p
               or "rederived set" in p for p in problems)


def test_a_smaller_plan_is_not_validated_as_a_subset(tmp_path):
    plan, baseline, path, signature = _small_plan(tmp_path)
    plan = copy.deepcopy(plan)
    kept = plan["operations"][:1]
    plan["operations"] = kept
    plan["accounting"]["operation_set_digest"] = SM.canonical_sha256(kept)
    assert any("not exactly the rederived canonical set" in p
               for p in SP.validate_plan(plan, baseline=baseline, baseline_path=path,
                                         authoritative_schema=signature))


def test_operations_are_disjoint_from_the_collision_population():
    plan = artifacts.load_verified(_live("plan"))
    collision_rows = set(plan["collision_population"]["row_ids"])
    collision_keys = set(plan["collision_population"]["keys"])
    for op in plan["operations"]:
        assert op["child_item_id"] not in collision_rows
        assert op["parent_item_id"] not in collision_rows
        assert f"{op['meeting_db_id']}|{op['child_number']}" not in collision_keys


def test_attached_to_is_distinct_from_part_of():
    plan = artifacts.load_verified(_live("plan"))
    assert SP.PART_OF != SP.ATTACHED_TO
    assert plan["relations"][SP.PART_OF] == 459
    assert plan["relations"][SP.ATTACHED_TO] == 0
    assert plan["policy"]["attached_to_remains_distinct_from_part_of"] is True


# ── the recorded artifacts ────────────────────────────────────────────

def test_the_recorded_plan_is_refused_after_semantic_registry_drift():
    plan_path = _live("plan")
    baseline_path = _live("baseline")
    plan = artifacts.load_verified(plan_path)
    problems = SP.validate_plan(plan, baseline_path=baseline_path, plan_dir=_PLANS)
    assert any("scripts/kg/registries/evidence.py" in p and "stale" in p
               for p in problems)


def test_the_recorded_baseline_reconciles_with_collision_dispositions():
    baseline = artifacts.load_verified(_live("baseline"))
    assert baseline["total"] == 116806
    assert sum(baseline["counts"].values()) == 116806
    # Regenerated from the post-repair population (116,806 items).
    assert baseline["counts"] == {"root": 92745, "subitem": 459, "deeper": 0,
                                  "ambiguous": 15788, "invalid": 17,
                                  "collision_held": 7797}
    assert baseline["population"]["reconciles"] is True
    pop = baseline["collision_population"]
    assert pop["distinct_keys"] == 3009 and pop["involved_rows"] == 7797
    assert pop["excess"] == 4788


def test_the_recorded_plan_is_dry_with_no_write_path():
    plan = artifacts.load_verified(_live("plan"))
    assert plan["mode"] == "dry-run" and plan["write_path"] == "absent by design"
    assert plan["applied"] is False and plan["promoted"] is False
    assert plan["preconditions"]["schema_readiness_required"] is True
    assert len(plan["operations"]) == 459


def test_the_superseded_pair_is_archived_not_deleted():
    for name in ("kg-stage2-subitem-containment-plan-45d27d9ee50541c8.json",
                 "kg-stage2-subitem-containment-baseline-20260913T044500Z.json"):
        assert (_PLANS / name).exists(), name
        assert (_PLANS / (name + ".obsolete.json")).exists(), name
