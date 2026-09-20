#!/usr/bin/env python3
"""Adversarial tests for the Stage 2 apply admission, receipt and rollback.

Every attack here must fail closed.  No test executes the write path: the engines
are local SQLite fixtures, and rollback refuses any non-SQLite dialect outright.
"""

from __future__ import annotations

import copy
import inspect
import json
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

_REPO = pathlib.Path(__file__).resolve().parents[1]
for _p in (_REPO, _REPO / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage1_backup_receipt as backup_receipts  # noqa: E402
from scripts.kg import stage2_s2_admission_tx as TX  # noqa: E402
from scripts.kg import stage2_s2_apply_runner as AR  # noqa: E402
from scripts.kg import stage2_s2_apply_target as AT  # noqa: E402
from scripts.kg import stage2_s2_collision as COL  # noqa: E402
from scripts.kg import stage2_s2_current_state as CS  # noqa: E402
from scripts.kg import stage2_s2_label_correction as LC  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as B  # noqa: E402
from scripts.kg import stage2_s2_receipt as RCP  # noqa: E402
from scripts.kg import stage2_s2_receipt_rollback as RB  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as RP  # noqa: E402

_PLANS = _REPO / "data" / "kg-plans"
_BACKUP = _REPO / "data" / "backups" / "kg-stage1-backup-receipt-20260914T003343Z.v2.json"


def _live(pattern):
    """The one live artifact matching a glob, resolved rather than named."""
    hits = [p for p in sorted(_PLANS.glob(pattern))
            if not p.name.endswith(".obsolete.json")
            and not (_PLANS / (p.name + ".obsolete.json")).exists()]
    assert len(hits) == 1, f"{pattern}: {[h.name for h in hits]}"
    return hits[0]


_RP = _live("kg-stage2-s2-repair-plan-*.json")
_LC = _live("kg-stage2-s2-label-correction-plan-*.json")

_TARGET = {"dialect": "postgresql", "host": "h", "port": 5432,
           "database": "poliscopic_dev"}


@pytest.fixture(autouse=True)
def historical_backup_clock(monkeypatch):
    """Keep historical-evidence tests about structure, not wall-clock age.

    Production admission still uses the real clock.  This test-only clock is one
    hour after the immutable receipt was created, so the same receipt can exercise
    field comparison without weakening the 24-hour runtime rule.
    """
    created = datetime.fromisoformat(
        json.loads(_BACKUP.read_text())["created_at"].replace("Z", "+00:00"))
    moment = created.astimezone(timezone.utc) + timedelta(hours=1)

    class _ReceiptClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment if tz is None else moment.astimezone(tz)

    monkeypatch.setattr(backup_receipts, "datetime", _ReceiptClock)


def load(p):
    return copy.deepcopy(artifacts.load_verified(p))


def _auth():
    return (AR.AuthorizedArtifact(_RP.name, artifacts.recorded_digest(load(_RP))),
            AR.AuthorizedArtifact(_LC.name, artifacts.recorded_digest(load(_LC))))


def _fixture_engine(unique=True):
    engine = create_engine("sqlite://")
    tail = ", UNIQUE (meeting_db_id, agenda_item_number)" if unique else ""
    with engine.begin() as c:
        c.execute(text("CREATE TABLE agenda_items (id INTEGER PRIMARY KEY, "
                       "meeting_db_id INTEGER, agenda_item_number TEXT, "
                       f"agenda_item_title TEXT, agenda_item_id TEXT, sort_order INTEGER{tail})"))
        c.execute(text("CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, "
                       "meeting_db_id INTEGER, body TEXT, agenda_item_number TEXT, "
                       "document_url TEXT, file_name TEXT, document_title TEXT, "
                       "agenda_item_id TEXT, text_content TEXT)"))
    return engine


class _SpyEngine:
    """An engine that records whether anyone tried to acquire a connection."""

    class dialect:
        name = "postgresql"

    def __init__(self):
        self.connect_calls = 0

    def connect(self):  # pragma: no cover - reached only on a P0 regression
        self.connect_calls += 1
        raise AssertionError("no transaction may be opened here")


# ══ P0: no public write callback, hook or callable ═════════════════════

def test_apply_takes_no_parameters_at_all():
    parameters = inspect.signature(AR.apply).parameters
    for name in ("write", "callback", "hook", "operation", "connection", "engine"):
        assert name not in parameters, name
    assert AR.APPLY_PARAMETERS == ()


def test_admit_exposes_no_write_parameter():
    parameters = inspect.signature(AR.admit).parameters
    for name in ("write", "callback", "hook", "operation", "callable"):
        assert name not in parameters, name
    assert set(AR.ADMIT_PARAMETERS) == set(parameters)
    assert "connection" not in parameters


def test_a_malicious_write_callback_is_rejected_before_it_runs():
    calls = []

    def malicious(connection, result):
        calls.append(connection)
        return {"pwned": True}

    engine = _SpyEngine()
    with pytest.raises(AR.ApplyRefused) as exc:
        AR.apply(write=malicious, engine=engine)
    assert calls == [], "the malicious callback was invoked"
    assert engine.connect_calls == 0, "a connection was acquired"
    assert "no arguments" in str(exc.value) or "write" in str(exc.value)


def test_a_positional_callback_is_also_rejected():
    engine = _SpyEngine()
    with pytest.raises(AR.ApplyRefused):
        AR.apply(lambda *a: None)
    assert engine.connect_calls == 0


def test_apply_refuses_before_opening_a_transaction():
    engine = _SpyEngine()
    with pytest.raises(AR.ApplyRefused) as exc:
        AR.apply()
    assert engine.connect_calls == 0
    assert "no transaction" in str(exc.value) or "absent by design" in str(exc.value)


def test_apply_leaves_the_database_unchanged():
    engine = _fixture_engine()
    calls = []
    with pytest.raises(AR.ApplyRefused):
        AR.apply(write=lambda c, r: calls.append(1), engine=engine,
                 repair=_auth()[0], correction=_auth()[1], backup_path=_BACKUP)
    assert calls == []
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agenda_items")).scalar() == 0


def test_the_runner_offers_no_callable_parameter_to_a_caller():
    """Only the internal unit type is a Callable; no public API accepts one."""
    for func in (AR.admit, AR.apply):
        for parameter in inspect.signature(func).parameters.values():
            assert "Callable" not in str(parameter.annotation), parameter


def test_the_runner_implements_no_write_statement():
    source = inspect.getsource(AR)
    for forbidden in ("INSERT INTO", "UPDATE supporting_documents", "DELETE FROM"):
        assert forbidden not in source, forbidden


def test_the_transaction_units_write_body_does_not_exist():
    assert not hasattr(AR, "write_body")
    assert "write" not in inspect.signature(AR._admission_unit).parameters


# ══ 1. exact artifacts, canonically loaded ═════════════════════════════

def test_a_wrong_digest_is_refused():
    repair, _ = _auth()
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._load_authorized(AR.AuthorizedArtifact(repair.path, "0" * 64),
                            role="repair", plan_dir=_PLANS)
    assert "digest mismatch" in str(exc.value)


def test_a_cross_paired_path_and_digest_is_refused():
    repair, correction = _auth()
    with pytest.raises(AR.ApplyRefused):
        AR._load_authorized(AR.AuthorizedArtifact(repair.path, correction.digest),
                            role="repair", plan_dir=_PLANS)


def test_a_path_that_is_not_a_plain_name_is_refused():
    repair, _ = _auth()
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._load_authorized(AR.AuthorizedArtifact("../x.json", repair.digest),
                            role="repair", plan_dir=_PLANS)
    assert "plain name" in str(exc.value)


def test_an_obsolete_artifact_is_refused():
    obsolete = next(_PLANS.glob("kg-stage2-s2-repair-plan-*.json.obsolete.json"))
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._load_authorized(
            AR.AuthorizedArtifact(obsolete.name.replace(".obsolete.json", ""), "0" * 64),
            role="repair", plan_dir=_PLANS)
    assert "obsolete" in str(exc.value)


def test_every_superseded_plan_is_archived_not_deleted():
    for name in ("kg-stage2-s2-repair-plan-4b703d4db4e9e595.json",
                 "kg-stage2-s2-label-correction-plan-33d333962f44c79e.json",
                 "kg-stage2-s2-repair-plan-b5bcc44af5dfeeef.json",
                 "kg-stage2-s2-label-correction-plan-5fce6f285410e17c.json"):
        assert (_PLANS / name).exists(), name
        assert (_PLANS / (name + ".obsolete.json")).exists(), name


# ══ 2. heads, decisions, code ═════════════════════════════════════════

def test_a_mutated_lineage_binding_is_refused():
    plan = load(_RP)
    plan["bindings"]["lineage"]["plan"]["digest"] = "0" * 64
    heads = AR._verify_heads(_PLANS)
    assert plan["bindings"]["lineage"]["plan"]["digest"] != heads["plan"]["digest"]


def test_a_mutated_decision_digest_is_refused():
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["digest"] = "0" * 64
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused):
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)


def test_a_mutated_code_hash_is_refused():
    plan = load(_RP)
    plan["bindings"]["code_hashes"]["scripts/kg/stage2_s2_repair_plan.py"] = "0" * 64
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_code_hashes(plan)
    assert "code has drifted" in str(exc.value)


def test_both_plans_bind_identical_decisions():
    repair, correction = load(_RP), load(_LC)
    B.assert_decisions_equal(repair, correction)
    correction["bindings"]["decisions"][0]["adjudicator"] = "someone else"
    with pytest.raises(AssertionError):
        B.assert_decisions_equal(repair, correction)


def test_every_bound_decision_resolves_and_is_deep():
    plan = load(_RP)
    heads = AR._verify_heads(_PLANS)
    verified = AR._verify_decisions(plan, plan_dir=_PLANS,
                                    aggregate=heads["aggregate"]["document"], heads=heads)
    assert len(verified) == 5
    for entry in plan["bindings"]["decisions"]:
        for field in ("path", "digest", "decision_id", "adjudicator", "decided_at",
                      "document_id", "proposal_path", "proposal_digest",
                      "document_fingerprint", "candidate", "lineage"):
            assert entry.get(field), field


# ══ 3. full decision / candidate / lineage equality ═══════════════════

@pytest.mark.parametrize("field,value", [
    ("adjudicator", "someone else"),
    ("decided_at", "1999-01-01T00:00:00Z"),
    ("document_role", "Not The Role"),
    ("human_stated_item", "9.Z"),
    ("item_number_mismatch", False),
    ("decision_id", "kg-s2-dec-nope"),
    ("document_fingerprint", "0" * 64),
])
def test_a_mutated_bound_decision_field_is_refused(field, value):
    plan = load(_RP)
    plan["bindings"]["decisions"][0][field] = value
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)
    assert field in str(exc.value) or "aggregate" in str(exc.value)


@pytest.mark.parametrize("field", list(B.CANDIDATE_FIELDS))
def test_every_candidate_field_is_compared(field):
    plan = load(_RP)
    candidate = plan["bindings"]["decisions"][0]["candidate"]
    assert field in candidate, field
    mutated = copy.deepcopy(candidate)
    mutated[field] = "TAMPERED" if field != "agenda_item_db_id" else 999999
    plan["bindings"]["decisions"][0]["candidate"] = mutated
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)
    assert "candidate" in str(exc.value)


def test_the_candidate_carries_the_full_identity():
    plan = load(_RP)
    for entry in plan["bindings"]["decisions"]:
        candidate = entry["candidate"]
        assert set(candidate) == set(B.CANDIDATE_FIELDS)
        for field in B.CANDIDATE_FIELDS:
            assert candidate[field] is not None, field


def test_a_mutated_proposal_digest_is_refused():
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["proposal_digest"] = "0" * 64
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused):
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)


def test_a_decision_missing_from_the_aggregate_is_refused():
    plan = load(_RP)
    heads = AR._verify_heads(_PLANS)
    aggregate = copy.deepcopy(heads["aggregate"]["document"])
    aggregate["decisions"] = {}
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS, aggregate=aggregate, heads=heads)
    assert "aggregate" in str(exc.value)


def test_a_decision_with_wrong_aggregate_membership_is_refused():
    plan = load(_RP)
    heads = AR._verify_heads(_PLANS)
    aggregate = copy.deepcopy(heads["aggregate"]["document"])
    document_id = str(plan["bindings"]["decisions"][0]["document_id"])
    for members in aggregate["per_unit"].values():
        for member in members:
            if str(member["document_id"]) == document_id:
                member["digest"] = "0" * 64
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS, aggregate=aggregate, heads=heads)
    assert "membership" in str(exc.value)


def test_a_decision_lineage_against_a_stale_plan_head_is_refused():
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["lineage"]["plan"]["digest"] = "0" * 64
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)
    assert "lineage" in str(exc.value)


def test_a_tampered_aggregate_anchor_is_refused():
    """The anchor may be historical, but it must match the artifact it names."""
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["lineage"]["aggregate"]["digest"] = "0" * 64
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)
    assert "lineage" in str(exc.value)


def test_an_unloadable_aggregate_anchor_is_refused():
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["lineage"]["aggregate"] = {
        "path": "no-such-aggregate.json", "digest": "0" * 64}
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused):
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)


def test_a_missing_aggregate_anchor_is_refused():
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["lineage"]["aggregate"] = {}
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)
    message = str(exc.value)
    assert "lineage" in message or "anchor" in message


def test_a_decision_lineage_naming_another_proposal_is_refused():
    plan = load(_RP)
    plan["bindings"]["decisions"][0]["lineage"]["proposal"]["digest"] = "0" * 64
    heads = AR._verify_heads(_PLANS)
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._verify_decisions(plan, plan_dir=_PLANS,
                             aggregate=heads["aggregate"]["document"], heads=heads)
    assert "lineage" in str(exc.value)


def test_the_decision_lineage_is_bound_and_anchored():
    """Plan lineage is the CURRENT head; the aggregate anchor is verified."""
    heads = AR._verify_heads(_PLANS)
    plan = load(_RP)
    for entry in plan["bindings"]["decisions"]:
        lineage = entry["lineage"]
        assert Path(lineage["plan"]["path"]).name == heads["plan"]["path"]
        assert lineage["plan"]["digest"] == heads["plan"]["digest"]
        assert lineage["proposal"]["path"] == entry["proposal_path"]
        assert lineage["proposal"]["digest"] == entry["proposal_digest"]
        anchor = _PLANS / lineage["aggregate"]["path"]
        assert anchor.exists(), lineage["aggregate"]["path"]
        assert artifacts.recorded_digest(artifacts.load_verified(anchor)) == \
            lineage["aggregate"]["digest"]


def test_the_current_aggregate_carries_every_decision():
    """The membership check is against the CURRENT aggregate, not the anchor."""
    heads = AR._verify_heads(_PLANS)
    plan = load(_RP)
    aggregate = heads["aggregate"]["document"]
    for entry in plan["bindings"]["decisions"]:
        document_id = str(entry["document_id"])
        assert document_id in aggregate["decisions"]
        members = [m for members in aggregate["per_unit"].values() for m in members
                   if str(m["document_id"]) == document_id]
        assert len(members) == 1
        assert members[0]["path"] == entry["proposal_path"]
        assert members[0]["digest"] == entry["proposal_digest"]


def test_the_historical_anchor_resolver_refuses_a_missing_artifact():
    with pytest.raises(AR.ApplyRefused) as exc:
        AR._resolve_historical("no-such-artifact.json", _PLANS)
    assert "does not resolve" in str(exc.value)


def test_the_historical_anchor_resolver_accepts_an_obsolete_artifact():
    obsolete = next(_PLANS.glob("kg-stage2-s2-ai-proposals-*.json.obsolete.json"))
    live_name = obsolete.name.replace(".obsolete.json", "")
    resolved = AR._resolve_historical(live_name, _PLANS)
    assert resolved.exists()


def test_the_aggregate_anchor_is_not_required_to_be_the_current_head():
    """It is a historical record; requiring it to be current would be wrong."""
    heads = AR._verify_heads(_PLANS)
    plan = load(_RP)
    anchors = {r["lineage"]["aggregate"]["digest"]
               for r in plan["bindings"]["decisions"]}
    assert anchors  # recorded
    assert AR._resolve_historical(
        next(iter(plan["bindings"]["decisions"]))["lineage"]["aggregate"]["path"],
        _PLANS).exists()


# ══ 4. target and backup: no self-referential evidence ════════════════

def test_a_target_host_mismatch_is_refused():
    plan = load(_RP)
    engine = create_engine("postgresql+psycopg2://u@other-host:5432/poliscopic_dev")
    with pytest.raises(AR.ApplyRefused) as exc:
        AT.verify_target(engine, plan=plan, config={"tier": "development"},
                         backup={"target": {"database": "poliscopic_dev",
                                            "dialect": "postgresql",
                                            "host": "192.0.2.10", "port": 5432,
                                            "tier": "development"}})
    assert "disagrees" in str(exc.value)


def test_a_non_development_tier_is_refused():
    plan = load(_RP)
    engine = create_engine("postgresql+psycopg2://u@192.0.2.10:5432/poliscopic_dev")
    with pytest.raises(AR.ApplyRefused):
        AT.verify_target(engine, plan=plan, config={"tier": "production"},
                         backup={"target": {}})


def test_the_backup_binding_is_never_self_referential():
    bound = load(_RP)["bindings"]["backup"]
    assert "current_baseline" not in bound
    assert "current_baseline" not in AT.COMPARED_FIELDS
    assert "current_baseline" not in inspect.signature(AT.load_backup).parameters
    assert "current_baseline" not in inspect.signature(B.backup_binding).parameters
    assert bound["evidence_source"] == "the backup receipt and the dump file on disk"


def test_the_backup_evidence_comes_from_the_receipt_and_dump():
    live = AT.load_backup(_BACKUP, target={"database": "poliscopic_dev"})
    receipt = json.loads(_BACKUP.read_text())
    assert live["source_counts"] == receipt["counts"]
    assert live["source_counts_sha256"] == receipt["signatures"]["counts_sha256"]
    assert live["source_schema_sha256"] == receipt["signatures"]["schema_sha256"]
    assert live["restore_proof"]["evidence"] == receipt["pg_restore"]["evidence"]
    assert live["receipt_stat"]["mode"] == "0o600"


def test_a_world_readable_receipt_is_refused(tmp_path):
    path = tmp_path / "loose.receipt.json"
    path.write_text(_BACKUP.read_text())
    path.chmod(0o644)
    with pytest.raises(AR.ApplyRefused) as exc:
        AT.load_backup(path, target={"database": "poliscopic_dev"})
    assert "0o600" in str(exc.value)


def test_a_fake_backup_receipt_is_refused(tmp_path):
    fake = tmp_path / "fake.receipt.json"
    fake.write_text(json.dumps({"dump_sha256": "0" * 64}))
    fake.chmod(0o600)
    with pytest.raises(AR.ApplyRefused):
        AT.load_backup(fake, target={"database": "poliscopic_dev"})


def test_a_receipt_without_a_proven_restore_is_refused(tmp_path):
    receipt = json.loads(_BACKUP.read_text())
    receipt["pg_restore"] = {"exit_code": 1, "evidence": ""}
    path = tmp_path / "unrestored.receipt.json"
    path.write_text(json.dumps(receipt))
    path.chmod(0o600)
    with pytest.raises(AR.ApplyRefused):
        AT.load_backup(path, target={"database": "poliscopic_dev"})


def test_both_plans_bind_the_backup_completely():
    for path in (_RP, _LC):
        bound = load(path)["bindings"]["backup"]
        assert bound["path"] and bound["canonical_digest"]
        assert bound["mode"] == "0o600" and bound["receipt"]["mode"] == "0o600"
        assert bound["receipt"]["uid"] is not None
        assert bound["dump_path"] and bound["dump_sha256"]
        assert bound["dump"]["mode"] and bound["dump"]["uid"] is not None
        assert bound["restore_proof"]["exit_code"] == 0
        assert bound["restore_proof"]["evidence_sha256"]
        assert bound["source_counts"] and bound["source_counts_sha256"]
        assert bound["source_schema_sha256"]
        assert bound["plan_baseline_counts"] == bound["source_counts"]
        assert bound["problems"] == []


def test_every_plan_bound_backup_field_is_compared():
    """Every field the plan BINDS is compared against the live receipt.

    The plan binds the receipt it was built with; a different live receipt must be
    reported rather than silently accepted, which is what proves the comparison runs
    over the bound fields instead of skipping them.
    """
    bound = load(_RP)["bindings"]["backup"]
    live = AT.load_backup(_BACKUP, target={"database": "poliscopic_dev"})
    # The plan binds exactly the receipt the live side loads, so every bound field must
    # compare CLEAN; a reported problem here would mean a field was not really compared.
    assert AT.verify_backup_binding(bound, live) == []


@pytest.mark.parametrize("field,value", [
    ("canonical_digest", "0" * 64),
    ("path", "another.receipt.json"),
    ("dump_path", "/nowhere.dump"),
    ("dump_sha256", "0" * 64),
    ("source_counts", {"entities": -1}),
    ("source_counts_sha256", "0" * 64),
    ("source_schema_sha256", "0" * 64),
    ("receipt", {"mode": "0o644", "uid": 1, "gid": 1, "exists": True, "size": 1}),
    ("dump", {"mode": "0o644", "uid": 1, "gid": 1, "exists": True, "size": 1}),
    ("restore_proof", {"exit_code": 1, "evidence_present": False}),
    ("target", {"database": "something_else"}),
    ("mode", "0o644"),
])
def test_a_deviating_backup_field_is_refused(field, value):
    bound = copy.deepcopy(load(_RP)["bindings"]["backup"])
    bound[field] = value
    live = AT.load_backup(_BACKUP, target={"database": "poliscopic_dev"})
    assert AT.verify_backup_binding(bound, live), field


def test_a_deviating_plan_baseline_is_refused():
    """The bound baseline must be compared, so deviating one of its keys must show."""
    bound = copy.deepcopy(load(_RP)["bindings"]["backup"])
    baseline = dict(bound.get("plan_baseline_counts") or {})
    assert baseline, "the binding carries no plan baseline to deviate"
    key = sorted(baseline)[0]
    bound["plan_baseline_counts"] = {**baseline, key: -999999999}
    live = AT.load_backup(_BACKUP, target={"database": "poliscopic_dev"})
    assert AT.verify_backup_binding(bound, live), "a deviated baseline key was not compared"


def test_a_receipt_count_that_disagrees_with_the_plan_baseline_is_refused():
    bound = copy.deepcopy(load(_RP)["bindings"]["backup"])
    bound["plan_baseline_counts"] = {"entities": 999999999}
    live = AT.load_backup(_BACKUP, target={"database": "poliscopic_dev"})
    problems = AT.verify_backup_binding(bound, live)
    assert any("plan baseline" in p for p in problems)


# ══ 5. the transaction owner acquires its own connection ═══════════════

class _Serialization(Exception):
    sqlstate = "40001"


def test_the_unit_runs_inside_one_serializable_transaction():
    seen = {}

    def unit(connection):
        seen["isolation"] = connection.get_isolation_level()
        seen["in_transaction"] = connection.in_transaction()
        return "done"

    assert TX.run_unit(_fixture_engine(), unit) == "done"
    assert str(seen["isolation"]).upper() == TX.SERIALIZABLE
    assert seen["in_transaction"] is True


def test_a_caller_supplied_connection_is_refused():
    engine = _fixture_engine()
    with engine.connect() as connection:
        with pytest.raises(TX.TransactionRefused) as exc:
            TX.run_unit(connection, lambda c: "nope")
        assert "acquires its own connection" in str(exc.value)


def test_admit_refuses_a_connection_and_has_no_parameter_for_one():
    assert "connection" not in inspect.signature(AR.admit).parameters
    with _fixture_engine().connect() as connection:
        with pytest.raises(AR.ApplyRefused) as exc:
            AR.admit(repair=_auth()[0], correction=_auth()[1], engine=connection,
                     backup_path=_BACKUP, plan_dir=_PLANS)
        assert "acquires its own connection" in str(exc.value)


def test_a_failing_unit_rolls_the_whole_transaction_back():
    engine = _fixture_engine()

    def unit(connection):
        connection.execute(text("INSERT INTO agenda_items VALUES (1, 10, '2.C', 'x', '', 1)"))
        raise RuntimeError("postcondition failed")

    with pytest.raises(RuntimeError):
        TX.run_unit(engine, unit)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agenda_items")).scalar() == 0


def test_a_serialization_failure_retries_the_entire_unit():
    attempts = []

    def unit(connection):
        attempts.append(len(attempts) + 1)
        if len(attempts) == 1:
            raise _Serialization("could not serialize access due to concurrent update")
        return "survived"

    assert TX.run_unit(_fixture_engine(), unit) == "survived"
    assert attempts == [1, 2]


def test_a_commit_time_serialization_failure_is_retried():
    """The commit itself can lose the race; the whole unit is rerun."""
    attempts = []

    class _LosingTransaction:
        def __init__(self, real, attempt):
            self._real = real
            self._attempt = attempt

        def commit(self):
            if self._attempt == 1:
                raise _Serialization("could not serialize access at commit")
            return self._real.commit()

        def rollback(self):
            return self._real.rollback()

    class _Engine:
        class dialect:
            name = "sqlite"

        def __init__(self):
            self._engine = _fixture_engine()

        def connect(self):
            connection = self._engine.connect()
            attempts.append(len(attempts) + 1)
            connection.begin = lambda: _LosingTransaction(
                type(connection).begin(connection), len(attempts))
            return connection

    assert TX.run_unit(_Engine(), lambda c: "committed") == "committed"
    assert attempts == [1, 2]


def test_the_retry_is_bounded_and_then_refuses():
    attempts = []

    def unit(connection):
        attempts.append(1)
        raise _Serialization("could not serialize access")

    with pytest.raises(TX.SerializationExhausted) as exc:
        TX.run_unit(_fixture_engine(), unit, max_attempts=3)
    assert len(attempts) == 3
    assert "serialization" in str(exc.value).lower()


def test_a_non_serialization_failure_is_not_retried():
    attempts = []

    def unit(connection):
        attempts.append(1)
        raise ValueError("not a race")

    with pytest.raises(ValueError):
        TX.run_unit(_fixture_engine(), unit, max_attempts=3)
    assert len(attempts) == 1


def test_each_attempt_gets_a_fresh_connection():
    connections = []

    def unit(connection):
        connections.append(id(connection))
        if len(connections) == 1:
            raise _Serialization("could not serialize access")
        return "ok"

    assert TX.run_unit(_fixture_engine(), unit) == "ok"
    assert len(set(connections)) == 2


def test_admit_delegates_to_the_transaction_owner(monkeypatch):
    seen = {}

    def fake_run_unit(engine, unit, *, max_attempts, on_attempt=None):
        seen["max_attempts"] = max_attempts
        seen["unit"] = callable(unit)
        return {"status": "admitted"}

    monkeypatch.setattr(AR.tx, "run_unit", fake_run_unit)
    result = AR.admit(repair=_auth()[0], correction=_auth()[1], engine=object(),
                      backup_path=_BACKUP, plan_dir=_PLANS)
    assert result["status"] == "admitted"
    assert seen["max_attempts"] == AR.MAX_SERIALIZATION_ATTEMPTS == 3
    assert seen["unit"] is True


def test_admission_refuses_without_an_engine():
    with pytest.raises(AR.ApplyRefused):
        AR.admit(repair=_auth()[0], correction=_auth()[1], engine=None,
                 backup_path=_BACKUP, plan_dir=_PLANS)


def test_the_runner_never_begins_a_transaction_itself():
    source = inspect.getsource(AR)
    assert "BEGIN" not in source.replace("in_transaction", "")
    assert "SET TRANSACTION ISOLATION" not in source


# ══ 6. strict unique-index proof ══════════════════════════════════════

def test_a_real_unique_index_is_accepted():
    with _fixture_engine().connect() as c:
        found = COL.verify_unique_index(c)
    assert found["dialect"] == "sqlite" and "UNIQUE" in found["definition"]


def test_a_missing_unique_index_is_refused():
    with _fixture_engine(unique=False).connect() as c:
        with pytest.raises(COL.CollisionRefused) as exc:
            COL.verify_unique_index(c)
    assert "no unique index" in str(exc.value)


def test_the_proof_requirements_are_published_including_immediacy():
    requirements = COL.index_proof_requirements("postgresql")
    for token in ("indisunique", "indisvalid", "indisready", "indimmediate",
                  "not_partial", "not_expression", "no_include", "exact_attrs"):
        assert token in requirements, token
    assert "deferred" in requirements["indimmediate"]


class _FakeResult:
    def __init__(self, rows=None, scalar=None):
        self._rows = list(rows or [])
        self._scalar = scalar

    def mappings(self):
        return self

    def __iter__(self):
        return iter(self._rows)

    def all(self):
        return list(self._rows)

    def scalar(self):
        return self._scalar


class _FakePGConnection:
    class dialect:
        name = "postgresql"

    def __init__(self, candidates, names=None):
        self._candidates = candidates
        self._names = names or {1: "meeting_db_id", 2: "agenda_item_number"}
        self._first = True
        self.lookups = []

    def execute(self, statement, params=None):
        if self._first:
            self._first = False
            return _FakeResult(rows=self._candidates)
        self.lookups.append(dict(params or {}))
        return _FakeResult(scalar=self._names.get(int((params or {}).get("a"))))


def _pg_candidate(**over):
    base = dict(index_name="ix_natural", table_oid=42, indisunique=True,
                indisvalid=True, indisready=True, indimmediate=True,
                is_partial=False, is_expression=False, indnatts=2, indnkeyatts=2,
                attnums=[1, 2])
    base.update(over)
    return base


def test_a_conforming_postgres_index_is_accepted():
    found = COL.verify_unique_index(_FakePGConnection([_pg_candidate()]))
    assert found["immediate"] is True


@pytest.mark.parametrize("override", [
    {"indisunique": False}, {"indisvalid": False}, {"indisready": False},
    {"indimmediate": False}, {"is_partial": True}, {"is_expression": True},
    {"indnatts": 3}, {"indnkeyatts": 1}, {"attnums": [1, 3]},
])
def test_a_hollow_postgres_index_is_refused(override):
    with pytest.raises(COL.CollisionRefused):
        COL.verify_unique_index(_FakePGConnection([_pg_candidate(**override)]))


def test_the_attribute_names_are_resolved_by_relation_oid():
    connection = _FakePGConnection([_pg_candidate()])
    COL.verify_unique_index(connection)
    assert connection.lookups
    for lookup in connection.lookups:
        assert lookup["oid"] == 42, "the lookup must key on the relation OID"
        assert "n" not in lookup


# ══ 7. receipt: path only, no forgeable object ════════════════════════

def _write_receipt(tmp_path, payload, name="receipt.json"):
    path = tmp_path / name
    artifacts.write_immutable(path, payload)
    return path


def _receipt_kwargs(**over):
    base = dict(plan={"digest": "p" * 64, "replay_digest": "r" * 64},
                plan_path="plan.json", plan_role="repair",
                target={"dialect": "postgresql", "host": "h", "port": 5432,
                        "database": "poliscopic_dev"},
                commit_status="committed", authorized_by="Peter Mains")
    base.update(over)
    return base


def test_the_receipt_module_has_no_public_token_type():
    for name in ("VerifiedReceipt", "coerce_verified", "receipt_path_of"):
        assert not hasattr(RCP, name), name
    assert "VerifiedReceipt" not in RCP.__all__
    assert not hasattr(RCP, "_VERIFIED_TOKEN")


def test_a_mapping_is_refused_as_a_receipt():
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.load_authorized_receipt(
            {"kind": RCP.RECEIPT_KIND, "plan_path": "plan.json",
             "plan_digest": "p" * 64, "commit_status": "committed",
             "target": dict(_TARGET)},
            plan_path="plan.json", plan_digest="p" * 64, target=_TARGET)
    assert "is not a receipt path" in str(exc.value)


def test_verify_receipt_refuses_a_mapping(tmp_path):
    path = _write_receipt(tmp_path, RCP.build_receipt(**_receipt_kwargs()))
    payload = json.loads(path.read_text())
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.verify_receipt(payload, plan_path="plan.json", plan_digest="p" * 64,
                           target=_TARGET, current={"items": [], "documents": []})
    assert "is not a receipt path" in str(exc.value)


def test_a_receipt_for_another_plan_digest_is_refused(tmp_path):
    path = _write_receipt(tmp_path, RCP.build_receipt(**_receipt_kwargs()))
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.load_authorized_receipt(path, plan_path="plan.json",
                                    plan_digest="0" * 64, target=_TARGET)
    assert "different plan digest" in str(exc.value)


def test_a_receipt_for_another_plan_path_is_refused(tmp_path):
    path = _write_receipt(tmp_path, RCP.build_receipt(**_receipt_kwargs()))
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.load_authorized_receipt(path, plan_path="another.json",
                                    plan_digest="p" * 64, target=_TARGET)
    assert "different plan path" in str(exc.value)


def test_a_receipt_for_another_target_is_refused(tmp_path):
    path = _write_receipt(tmp_path, RCP.build_receipt(**_receipt_kwargs()))
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.load_authorized_receipt(path, plan_path="plan.json",
                                    plan_digest="p" * 64,
                                    target={**_TARGET, "host": "OTHER"})
    assert "differs from the live target" in str(exc.value)


def test_a_receipt_for_another_replay_digest_is_refused(tmp_path):
    path = _write_receipt(tmp_path, RCP.build_receipt(**_receipt_kwargs()))
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.load_authorized_receipt(path, plan_path="plan.json",
                                    plan_digest="p" * 64, target=_TARGET,
                                    plan_replay_digest="0" * 64)
    assert "replay digest" in str(exc.value)


def test_an_obsolete_receipt_is_refused(tmp_path):
    path = _write_receipt(tmp_path, RCP.build_receipt(**_receipt_kwargs()))
    artifacts.record_obsolete(tmp_path, path, "superseded by a corrected receipt")
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.load_authorized_receipt(path, plan_path="plan.json",
                                    plan_digest="p" * 64, target=_TARGET)
    assert "obsolete" in str(exc.value)


def test_a_handwritten_file_is_refused(tmp_path):
    path = tmp_path / "handwritten.json"
    path.write_text(json.dumps({"kind": RCP.RECEIPT_KIND,
                                "commit_status": "no-op-already-applied",
                                "inserted_rows": []}))
    with pytest.raises(RCP.ReceiptRefused):
        RCP.load_authorized_receipt(path, plan_path="plan.json",
                                    plan_digest="p" * 64, target=_TARGET)


def test_a_receipt_with_no_owned_rows_proves_nothing(tmp_path):
    payload = RCP.build_receipt(**_receipt_kwargs(commit_status="no-op-already-applied"))
    path = _write_receipt(tmp_path, payload)
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.verify_receipt(path, plan_path="plan.json", plan_digest="p" * 64,
                           target=_TARGET, current={"items": [], "documents": []})
    assert "owns nothing" in str(exc.value)


def test_a_database_unchanged_shortcut_does_not_exist():
    assert "database_unchanged" not in inspect.getsource(RCP.verify_receipt)


def test_postimage_drift_is_refused(tmp_path):
    row = {"meeting_db_id": 10, "agenda_item_number": "2.C", "id": 1, "title": "",
           "agenda_item_id": "", "sort_order": 0}
    payload = RCP.build_receipt(**_receipt_kwargs(
        inserted_rows=[{"id": 1, "meeting_db_id": 10, "agenda_item_number": "2.C",
                        "row_fingerprint": AR.item_row_fingerprint(row)}],
        attachment_preimages=[{"id": 5, "agenda_item_id": None,
                               "agenda_item_number": None,
                               "postimage": {"agenda_item_id": "a",
                                             "agenda_item_number": "2.C"}}]))
    path = _write_receipt(tmp_path, payload)
    with pytest.raises(RCP.ReceiptRefused) as exc:
        RCP.verify_receipt(path, plan_path="plan.json", plan_digest="p" * 64,
                           target=_TARGET, current={
                               "items": [row],
                               "documents": [{"id": 5, "agenda_item_id": "a",
                                              "agenda_item_number": "WRONG"}]})
    assert "agenda_item_number postimage drifted" in str(exc.value)


def test_a_conforming_postimage_verifies(tmp_path):
    row = {"meeting_db_id": 10, "agenda_item_number": "2.C", "id": 1, "title": "",
           "agenda_item_id": "", "sort_order": 0}
    payload = RCP.build_receipt(**_receipt_kwargs(
        inserted_rows=[{"id": 1, "meeting_db_id": 10, "agenda_item_number": "2.C",
                        "row_fingerprint": AR.item_row_fingerprint(row)}],
        attachment_preimages=[{"id": 5, "agenda_item_id": None,
                               "agenda_item_number": None,
                               "postimage": {"agenda_item_id": "a",
                                             "agenda_item_number": "2.C"}}]))
    path = _write_receipt(tmp_path, payload)
    result = RCP.verify_receipt(path, plan_path="plan.json", plan_digest="p" * 64,
                                target=_TARGET,
                                current={"items": [row],
                                         "documents": [{"id": 5, "agenda_item_id": "a",
                                                        "agenda_item_number": "2.C"}]})
    assert result["rows"] == 1


# ══ 8. rollback: canonical plan load, no plan mapping ═════════════════

def _plan_artifact(tmp_path, name="plan.json"):
    """A real, minimal plan artifact on disk, loaded canonically."""
    path = tmp_path / name
    artifacts.write_immutable(path, {"kind": "test-rollback-plan", "mode": "dry-run",
                                     "replay_digest": "r" * 64})
    return artifacts.load_verified(path)


def _rollback_receipt(tmp_path, plan=None, **over):
    plan = plan if plan is not None else _plan_artifact(tmp_path)
    row = {"id": 1, "meeting_db_id": 10, "agenda_item_number": "2.C", "title": "ours",
           "agenda_item_id": "", "sort_order": 1}
    base = dict(plan={"digest": plan["digest"], "replay_digest": plan["replay_digest"]},
                plan_path="plan.json", plan_role="repair",
                target={"dialect": "postgresql", "host": "h", "port": 5432,
                        "database": "poliscopic_dev"},
                commit_status="committed", authorized_by="Peter Mains",
                inserted_rows=[{"id": 1, "meeting_db_id": 10,
                                "agenda_item_number": "2.C",
                                "row_fingerprint": AR.item_row_fingerprint(row)}],
                attachment_preimages=[{"id": 5, "agenda_item_id": None,
                                       "agenda_item_number": None,
                                       "postimage": {"agenda_item_id": "a",
                                                     "agenda_item_number": "2.C"}}])
    base.update(over)
    return RCP.build_receipt(**base)


def _receipt_and_plan(tmp_path, **over):
    plan = _plan_artifact(tmp_path)
    path = tmp_path / "receipt.json"
    artifacts.write_immutable(path, _rollback_receipt(tmp_path, plan=plan, **over))
    return plan, path


def _rb_kwargs(plan):
    return dict(plan_path="plan.json", plan_digest=plan["digest"], target=_TARGET,
                plan_dir=None)


def test_rollback_takes_no_plan_mapping():
    parameters = inspect.signature(RB.rollback).parameters
    assert "plan" not in parameters
    assert "receipt" not in parameters
    assert set(parameters) == {"engine", "receipt_path", "plan_path", "plan_digest",
                               "target", "plan_dir"}


def test_the_authorized_plan_is_loaded_canonically_by_path_and_digest(tmp_path):
    plan = _plan_artifact(tmp_path)
    loaded = RB.load_authorized_plan("plan.json", plan["digest"], plan_dir=tmp_path)
    assert loaded["digest"] == plan["digest"]
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.load_authorized_plan("plan.json", "0" * 64, plan_dir=tmp_path)
    assert "is not the artifact's" in str(exc.value)


def test_a_plan_path_that_is_not_a_plain_name_is_refused(tmp_path):
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.load_authorized_plan("../plan.json", "0" * 64, plan_dir=tmp_path)
    assert "plain name" in str(exc.value)


def test_rollback_refuses_a_plan_mapping():
    with pytest.raises(TypeError):
        RB.rollback(_fixture_engine(), "/tmp/r.json", plan={"digest": "x"},
                    plan_path="p.json", plan_digest="d", target=_TARGET)


def test_a_missing_authorized_plan_is_refused(tmp_path):
    _, path = _receipt_and_plan(tmp_path)
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(_fixture_engine(), path, plan_path="plan.json",
                    plan_digest="0" * 64, target=_TARGET, plan_dir=tmp_path)
    assert "digest" in str(exc.value) or "does not exist" in str(exc.value)


def test_rollback_refuses_a_production_target(tmp_path):
    """Rollback reaches the development tier, and production is structurally refused."""
    plan, path = _receipt_and_plan(tmp_path)

    class _Prod:
        class dialect:
            name = "postgresql"

        class url:
            host = "db.ondigitalocean.com"
            database = "poliscopic_dev"

    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(_Prod(), path, **_rb_kwargs(plan))
    assert "production host" in str(exc.value)


def test_rollback_refuses_an_unregistered_dialect(tmp_path):
    plan, path = _receipt_and_plan(tmp_path)

    class _Stub:
        class dialect:
            name = "mysql"

        class url:
            host = "127.0.0.1"
            database = "poliscopic_scratch"

    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(_Stub(), path, **_rb_kwargs(plan))
    assert "refusing" in str(exc.value)


def test_rollback_refuses_a_mapping_receipt(tmp_path):
    plan = _plan_artifact(tmp_path)
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(_fixture_engine(), {"receipt_id": "r1"}, plan_path="plan.json",
                    plan_digest=plan["digest"], target=_TARGET, plan_dir=tmp_path)
    assert "is not a receipt path" in str(exc.value)


def test_the_transaction_is_owned_before_the_plan_is_loaded(tmp_path):
    """A caller-supplied connection is refused before anything is read."""
    engine = _fixture_engine()
    with engine.connect() as connection:
        with pytest.raises(RB.RollbackRefused) as exc:
            RB.rollback(connection, tmp_path / "absent.json", plan_path="p.json",
                        plan_digest="d", target=_TARGET)
        assert "acquires its own connection" in str(exc.value)


def test_a_missing_receipt_is_refused_within_the_owned_transaction(tmp_path):
    plan = _plan_artifact(tmp_path)
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(_fixture_engine(), tmp_path / "absent.json", **_rb_kwargs(plan))
    assert "does not exist" in str(exc.value)


def test_rollback_preflights_both_attachment_fields_before_mutating(tmp_path):
    plan, path = _receipt_and_plan(tmp_path)
    engine = _fixture_engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items VALUES (1, 10, '2.C', 'ours', '', 1)"))
        c.execute(text("INSERT INTO agenda_items VALUES (2, 10, '2.D', 'theirs', '', 2)"))
        c.execute(text("INSERT INTO supporting_documents (id, agenda_item_id, "
                       "agenda_item_number, text_content) VALUES (5, 'a', 'WRONG', 'body')"))
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(engine, path, plan_path="plan.json", plan_digest=plan["digest"],
                    target=_TARGET, plan_dir=tmp_path)
    assert "agenda_item_number" in str(exc.value)
    with engine.connect() as c:
        remaining = [r[0] for r in c.execute(text("SELECT id FROM agenda_items ORDER BY id"))]
    assert remaining == [1, 2], "preflight mutated the database"


def test_rollback_deletes_only_the_receipt_owned_id_and_restores_preimages(tmp_path):
    plan, path = _receipt_and_plan(tmp_path)
    engine = _fixture_engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items VALUES (1, 10, '2.C', 'ours', '', 1)"))
        c.execute(text("INSERT INTO agenda_items VALUES (2, 10, '2.D', 'theirs', '', 2)"))
        c.execute(text("INSERT INTO supporting_documents (id, agenda_item_id, "
                       "agenda_item_number, text_content) VALUES (5, 'a', '2.C', 'body')"))
    result = RB.rollback(engine, path, plan_path="plan.json",
                         plan_digest=plan["digest"], target=_TARGET, plan_dir=tmp_path)
    with engine.connect() as c:
        remaining = [r[0] for r in c.execute(text("SELECT id FROM agenda_items ORDER BY id"))]
        doc = c.execute(text("SELECT agenda_item_id, agenda_item_number "
                             "FROM supporting_documents WHERE id=5")).first()
    assert result["deleted"] == [1]
    assert remaining == [2], "a row the receipt does not own was deleted"
    assert doc[0] is None and doc[1] is None, "the attachment preimage was not restored"
    assert result["deleted_by_natural_key"] is False


def test_a_failed_rollback_leaves_nothing_partly_undone(tmp_path):
    plan, path = _receipt_and_plan(
        tmp_path,
        attachment_preimages=[{"id": 999, "agenda_item_id": None,
                               "agenda_item_number": None,
                               "postimage": {"agenda_item_id": "a",
                                             "agenda_item_number": "2.C"}}])
    engine = _fixture_engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items VALUES (1, 10, '2.C', 'ours', '', 1)"))
        c.execute(text("INSERT INTO supporting_documents (id, agenda_item_id, "
                       "agenda_item_number, text_content) VALUES (5, 'a', '2.C', 'body')"))
    with pytest.raises(RB.RollbackRefused):
        RB.rollback(engine, path, plan_path="plan.json",
                    plan_digest=plan["digest"], target=_TARGET, plan_dir=tmp_path)
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agenda_items")).scalar() == 1
    assert path.exists()


def test_rollback_refuses_a_drifted_row(tmp_path):
    plan, path = _receipt_and_plan(tmp_path)
    engine = _fixture_engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items VALUES (1, 10, '2.C', 'changed', '', 1)"))
    with pytest.raises(RB.RollbackRefused) as exc:
        RB.rollback(engine, path, plan_path="plan.json",
                    plan_digest=plan["digest"], target=_TARGET, plan_dir=tmp_path)
    assert "drifted" in str(exc.value)


def test_preflight_mutates_nothing_even_when_it_passes(tmp_path):
    plan, path = _receipt_and_plan(tmp_path)
    engine = _fixture_engine()
    with engine.begin() as c:
        c.execute(text("INSERT INTO agenda_items VALUES (1, 10, '2.C', 'ours', '', 1)"))
        c.execute(text("INSERT INTO supporting_documents (id, agenda_item_id, "
                       "agenda_item_number, text_content) VALUES (5, 'a', '2.C', 'body')"))
    result = RB.preflight(engine, path, plan_path="plan.json",
                          plan_digest=plan["digest"], target=_TARGET, plan_dir=tmp_path)
    assert result["status"] == "preflight-only"
    assert result["checked_fields"] == ["agenda_item_id", "agenda_item_number"]
    with engine.connect() as c:
        assert c.execute(text("SELECT COUNT(*) FROM agenda_items")).scalar() == 1


# ══ 9. current state is derived and plan-bound ════════════════════════

def test_capture_takes_no_caller_fingerprint_argument():
    params = inspect.signature(AR.capture_current_state).parameters
    assert "fingerprint" not in params
    assert not any("caller" in p for p in params)


def test_both_plans_bind_a_baseline_and_current_state_digest():
    for path in (_RP, _LC):
        bindings = load(path)["bindings"]
        assert bindings["baseline"]["population_sha256"]
        assert bindings["current_state_sha256"]


def test_each_plan_binds_a_per_document_hold_fingerprint():
    for path in (_RP, _LC):
        entries = load(path)["bindings"]["hold_population"]["entries"]
        assert entries
        for entry in entries:
            assert len(entry["row_fingerprint"]) == 64


def test_changing_a_row_changes_the_current_state_digest():
    engine = _fixture_engine()
    with engine.connect() as c:
        before = AR.capture_current_state(c, repair_plan=load(_RP),
                                          correction_plan=load(_LC))["sha256"]
        c.execute(text("INSERT INTO agenda_items (id, meeting_db_id, agenda_item_number, "
                       "agenda_item_title, agenda_item_id, sort_order) "
                       "VALUES (999, 10428, 'Z.Z', 'x', '', 1)"))
        after = AR.capture_current_state(c, repair_plan=load(_RP),
                                         correction_plan=load(_LC))["sha256"]
    assert before != after


def _conforming_state(repair, correction):
    plans = {"repair": repair, "correction": correction}
    hold_live, documents, bound = {}, {}, set()
    for role, plan in plans.items():
        population = plan["bindings"]["hold_population"]
        hold_live[role] = population
        for entry in population["entries"]:
            document_id = int(entry["id"])
            bound.add(document_id)
            document = documents.setdefault(document_id, {
                "id": document_id, "document_row_fingerprint": entry["row_fingerprint"],
                "content_sha256": "", "unlinked": True, "bound_hold_roles": [], "spans": {}})
            document["bound_hold_roles"].append(role)
    for plan in plans.values():
        for row in (plan.get("rows") or []) + (plan.get("operations") or []):
            witness = row.get("witness") or {}
            if not witness:
                continue
            document_id = int(witness["document_id"])
            document = documents.setdefault(document_id, {
                "id": document_id, "document_row_fingerprint": None,
                "content_sha256": "", "unlinked": True, "bound_hold_roles": [], "spans": {}})
            document["document_row_fingerprint"] = witness["document_row_fingerprint"]
            document["content_sha256"] = witness["content_sha256"]
            document["spans"] = {
                kind: {"kind": kind, "start": witness[kind]["start"],
                       "end": witness[kind]["end"], "fits": True,
                       "length": witness[kind]["end"] - witness[kind]["start"],
                       "sha256": witness[kind]["sha256"],
                       "recorded_text_sha256": witness[kind]["sha256"]}
                for kind in CS.SPAN_KINDS if witness.get(kind)}
    return {"items": [], "documents": sorted(documents.values(), key=lambda d: d["id"]),
            "hold_live": hold_live, "collisions": {}, "proposed_keys": [],
            "bound_hold_documents": sorted(bound),
            "sha256": repair["bindings"]["current_state_sha256"]}


def test_a_conforming_state_is_accepted():
    repair = load(_RP)
    # The APPLIED correction artifact keeps its HISTORICAL pre-state binding, so a
    # two-plan live verification can no longer succeed - and must not be forced to.  The
    # plan that is still live-verifiable is the repair plan, and its population is the
    # post-correction unlinked set (173), not the pre-correction 203.
    report = AR.verify_live_state(_conforming_state(repair, repair),
                                  repair_plan=repair, correction_plan=repair)
    assert report["bound_holds"] == 173
    assert report["collisions_touched_by_a_plan"] == 0


def test_live_state_refuses_a_changed_witness_document():
    repair, correction = load(_RP), load(_LC)
    state = _conforming_state(repair, correction)
    witness = next(r["witness"] for r in repair["rows"] if r.get("witness"))
    next(d for d in state["documents"]
         if d["id"] == int(witness["document_id"]))["content_sha256"] = "0" * 64
    with pytest.raises(CS.LiveStateRefused) as exc:
        AR.verify_live_state(state, repair_plan=repair, correction_plan=correction)
    assert "content has changed" in str(exc.value)


def test_live_state_refuses_an_occupied_proposed_key():
    repair, correction = load(_RP), load(_LC)
    state = _conforming_state(repair, correction)
    state["proposed_keys"] = [{"meeting_db_id": 10428, "agenda_item_number": "2.C",
                               "occupied": True, "occupant_ids": [1]}]
    with pytest.raises(CS.LiveStateRefused) as exc:
        AR.verify_live_state(state, repair_plan=repair, correction_plan=correction)
    assert "already occupied" in str(exc.value)


def test_live_state_refuses_a_plan_without_a_baseline():
    repair, correction = load(_RP), load(_LC)
    repair["bindings"]["baseline"] = {}
    with pytest.raises(CS.LiveStateRefused) as exc:
        AR.verify_live_state(_conforming_state(repair, correction),
                             repair_plan=repair, correction_plan=correction)
    assert "baseline population digest" in str(exc.value)
