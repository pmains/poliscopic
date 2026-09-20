"""Gate CLI entry point and live remediation-state regressions.

Two defects are covered:

1. ``event_normalize_gate_runner`` had no ``__main__`` guard, so the approved CLI
   silently did nothing and still exited **0** — a no-op that reads as success.
2. The launch blocker was a hardcoded string that went stale the moment the repair
   committed, while the refusal for genuinely undispositioned records must remain.

Pure and offline: no database, no gate execution, no spawn.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from scripts.entities import event_normalize_gate_runner as gate_runner
from scripts.entities import event_normalize_preflight
from scripts.entities import event_normalize_remediation as remediation
from scripts.entities.event_normalize_remediation import (
    ADJUDICATED_SENTINEL_IDS,
    SENTINEL_DECISION_ID,
    SENTINEL_DOCUMENT_ID,
    SENTINEL_REASON,
)
from scripts.kg.stage1_adjudication import ADJUDICATION


@pytest.fixture(autouse=True)
def _stub_population(monkeypatch, tmp_path):
    """Keep these unit tests hermetic and minimal.

    The minimal fixture has no producer chain, so the population reader is stubbed to
    the non-quarantined count; and the dedup-evidence base is redirected to an empty
    directory so the tests do not depend on ambient repository state.  The real
    leakage comparison and the real evidence binding are exercised (unstubbed) in
    tests/test_kg_event_normalize_remediation.py.
    """
    def fake(engine, *, page_size, force=True):
        with engine.connect() as connection:
            examined = connection.execute(text(
                "SELECT COUNT(*) FROM meeting_event_extractions "
                "WHERE quarantined_at IS NULL")).scalar() or 0
        return {"examined": int(examined)}
    monkeypatch.setattr(event_normalize_preflight, "collect_population", fake)
    empty = tmp_path / "no-dedup-evidence"
    empty.mkdir(exist_ok=True)
    monkeypatch.setattr(remediation, "DEFAULT_BASE", empty)

MODULE = "scripts/entities/event_normalize_gate_runner.py"
REPO = Path(__file__).resolve().parents[1]


# -- (A1) the CLI cannot silently no-op ---------------------------------------

def test_module_has_a_conventional_main_guard():
    source = (REPO / MODULE).read_text(encoding="utf-8")
    assert '__name__ == "__main__"' in source
    assert "raise SystemExit(main())" in source


def test_cli_help_runs_and_exits_zero():
    """--help proves the entry point is wired: a missing guard returns silently."""
    result = subprocess.run([sys.executable, MODULE, "--help"],
                            capture_output=True, text=True, cwd=REPO)
    assert result.returncode == 0
    assert "usage" in (result.stdout + result.stderr).lower()
    assert result.stdout.strip(), "the CLI must produce output, not a silent no-op"


def test_cli_rejects_an_unknown_flag_with_a_nonzero_exit():
    result = subprocess.run([sys.executable, MODULE, "--definitely-not-a-flag"],
                            capture_output=True, text=True, cwd=REPO)
    assert result.returncode != 0, "an unusable invocation must not exit 0"


@pytest.mark.parametrize("outcome,expected", [
    ({"status": "plan", "launchable": False}, 2),
    ({"status": "plan", "launchable": True}, 0),
    ({"status": "success"}, 0),
    ({"status": "digest_mismatch"}, 3),
    ({"status": "child_exit"}, 3),
])
def test_main_returns_truthful_exit_codes(monkeypatch, outcome, expected):
    import db.core
    monkeypatch.setattr(db.core, "get_engine", lambda: object())
    monkeypatch.setattr(gate_runner, "run_attempt", lambda *a, **k: dict(outcome))
    assert gate_runner.main(["--run-id", "unit"]) == expected


def test_main_reports_a_path_collision_as_its_own_exit_code(monkeypatch):
    import db.core

    from scripts.entities.event_normalize_gate import PathCollisionError
    monkeypatch.setattr(db.core, "get_engine", lambda: object())

    def collide(*_a, **_k):
        raise PathCollisionError("attempt already exists")

    monkeypatch.setattr(gate_runner, "run_attempt", collide)
    assert gate_runner.main(["--run-id", "unit"]) == 4


# -- (A2) live remediation state ----------------------------------------------

def _engine(bodies=3, parented=55, sentinel_present=18, backlog=0, eligible_unparented=0,
            sentinel_metadata=None):
    """A fixture mirroring the remediation facts, including the evidence chain.

    ``backlog`` meetings are unparented with no extraction on the chain;
    ``eligible_unparented`` meetings are unparented *with* a non-quarantined
    extraction reachable through extraction -> event -> document -> meeting.

    ``sentinel_present`` is how many of the **adjudicated sentinel identity set** are
    stored as quarantined; the invariant is semantic, so a missing member of that set
    blocks regardless of how many rows exist in total.
    """
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE public_bodies (id INTEGER PRIMARY KEY, body_code TEXT)"))
        connection.execute(text(
            "CREATE TABLE meetings (id INTEGER PRIMARY KEY, public_body_id INTEGER)"))
        connection.execute(text(
            "CREATE TABLE supporting_documents (id INTEGER PRIMARY KEY, meeting_db_id INTEGER)"))
        connection.execute(text(
            "CREATE TABLE meeting_events (id INTEGER PRIMARY KEY, supporting_doc_id INTEGER)"))
        connection.execute(text(
            "CREATE TABLE meeting_event_extractions (id INTEGER PRIMARY KEY, "
            "meeting_event_id INTEGER, quarantined_at TEXT, quarantine_reason TEXT, "
            "quarantined_by TEXT, decision_id TEXT, model_version TEXT, "
            "supporting_doc_id INTEGER)"))
        for index, code in enumerate(remediation.APPROVED_BODY_CODES[:bodies], start=1):
            connection.execute(text("INSERT INTO public_bodies (id, body_code) "
                                    "VALUES (:i, :c)"), {"i": index, "c": code})
        for meeting_id in range(1, parented + 1):
            connection.execute(text("INSERT INTO meetings (id, public_body_id) "
                                    "VALUES (:i, 1)"), {"i": meeting_id})
        for extra in range(backlog):
            connection.execute(text("INSERT INTO meetings (id, public_body_id) "
                                    "VALUES (:i, NULL)"), {"i": 100000 + extra})
        for extraction_id in sorted(ADJUDICATED_SENTINEL_IDS)[:sentinel_present]:
            values = {
                "id": extraction_id, "meeting_event_id": 1,
                "quarantined_at": "2026-09-12T00:28:45Z",
                "quarantine_reason": SENTINEL_REASON,
                "quarantined_by": ADJUDICATION["adjudicator"],
                "decision_id": SENTINEL_DECISION_ID,
                "model_version": "kg-model/1.0",
                "supporting_doc_id": SENTINEL_DOCUMENT_ID,
            }
            values.update(sentinel_metadata or {})
            columns = ", ".join(values)
            marks = ", ".join(f":{k}" for k in values)
            connection.execute(text(
                f"INSERT INTO meeting_event_extractions ({columns}) VALUES ({marks})"),
                values)
        # eligible unparented meetings: real chain, non-quarantined extraction
        for index in range(eligible_unparented):
            meeting_id = 200000 + index
            doc_id = 300000 + index
            event_id = 400000 + index
            connection.execute(text("INSERT INTO meetings (id, public_body_id) "
                                    "VALUES (:i, NULL)"), {"i": meeting_id})
            connection.execute(text("INSERT INTO supporting_documents (id, meeting_db_id) "
                                    "VALUES (:d, :m)"), {"d": doc_id, "m": meeting_id})
            connection.execute(text("INSERT INTO meeting_events (id, supporting_doc_id) "
                                    "VALUES (:e, :d)"), {"e": event_id, "d": doc_id})
            connection.execute(text(
                "INSERT INTO meeting_event_extractions (id, meeting_event_id, quarantined_at) "
                "VALUES (:x, :e, NULL)"), {"x": 500000 + index, "e": event_id})
    return engine


def test_committed_remediation_is_recognised():
    """3 bodies + 55 meetings + 18 quarantines and nothing eligible => no blocker."""
    assert remediation.remediation_state(_engine()) is None


def test_extraction_less_backlog_does_not_block():
    """Approved scope decision: extraction-less meetings are a backlog, not a blocker."""
    assert remediation.remediation_state(_engine(backlog=1428)) is None
    backlog = remediation.parentage_backlog(_engine(backlog=1428))
    assert backlog == {"meetings": 1428,
                       "scope": "outside the event_normalize eligible evidence chain",
                       "blocking": False}


def test_eligible_unparented_meeting_still_blocks_fail_closed():
    blocker = remediation.remediation_state(_engine(eligible_unparented=2))
    assert blocker is not None
    assert "2 unparented meetings participate in the eligible, non-quarantined" in blocker


def test_quarantined_only_unparented_meeting_is_not_eligible():
    """Meeting 15841's quarantined sentinel rows cannot make it eligible."""
    engine = _engine(sentinel_present=18, backlog=1)
    assert remediation.remediation_state(engine) is None


def test_unparented_population_partitions_exactly():
    engine = _engine(backlog=1428, eligible_unparented=3)
    counts = remediation.observe(engine)
    assert counts["unparented_meetings"] == 1431
    assert counts["backlog_unparented_meetings"] + counts["eligible_unparented_meetings"] \
        == counts["unparented_meetings"]


def test_incomplete_body_remediation_still_blocks():
    blocker = remediation.remediation_state(_engine(bodies=2, parented=30))
    assert "2/3" in blocker and "30/55" in blocker


def test_missing_sentinel_survivor_still_blocks():
    """A missing member of the adjudicated set blocks, whatever the row total is.

    This replaces the old count check ("expected 18 rows, observed 17"): a count goes
    stale when an approved operation retires duplicates, so the invariant is bound to
    the adjudicated identity set instead.
    """
    blocker = remediation.remediation_state(_engine(sentinel_present=17))
    assert blocker is not None
    assert "absent and not accounted for by committed dedup evidence" in blocker


def test_wrong_sentinel_metadata_still_blocks():
    blocker = remediation.remediation_state(
        _engine(sentinel_metadata={"quarantine_reason": "scraper_sentinel_other"}))
    assert blocker is not None
    assert "wrong reason" in blocker


def test_the_stale_plan_derived_wording_is_gone():
    """The old constant claimed the repair was unapplied; it must not return."""
    source = (REPO / "scripts/entities/event_normalize_preflight.py").read_text(encoding="utf-8")
    assert "produced but not applied" not in source
    assert remediation.remediation_state(_engine()) is None


def test_observed_counts_are_reported_for_fingerprinting():
    detail = remediation.remediation_detail(_engine(backlog=1428))
    assert detail["counts"]["approved_bodies"] == 3
    assert detail["counts"]["parented_meetings"] == 55
    assert detail["counts"]["quarantined_extractions"] == 18
    assert detail["counts"]["unparented_meetings"] == 1428
    assert detail["counts"]["eligible_unparented_meetings"] == 0
    assert detail["blocker"] is None
    assert detail["parentage_backlog"]["meetings"] == 1428
