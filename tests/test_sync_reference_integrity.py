#!/usr/bin/env python3
"""Reference-integrity gate tests — Brief 033.

These prove the invariants whose absence let production's ``public_bodies``
registry diverge from development's:

  * a canonical meeting code cannot be synced without its ``public_bodies`` row
    (this reproduces the 1,485 dangling Chandler/Mesa references);
  * a sentinel such as ``__skip__`` is refused/quarantined, never promoted;
  * a body-code rewrite that skips ``updated_at`` is detected, not silently
    skipped by sync — the root cause at body_code_merge_runtime.py:1589;
  * reference tables are ordered before their dependents.

Uses in-memory SQLite so the suite is hermetic and touches no real database.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

REPO_ROOT = Path(__file__).resolve().parent.parent
for _path in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from ops.reference_integrity_gate import (  # noqa: E402
    correction_update,
    dangling_references,
    integrity_problems,
    is_sentinel,
    reference_order_problems,
    unbumped_write_problems,
)

SCHEMA = """
CREATE TABLE public_bodies (
    id INTEGER PRIMARY KEY,
    jurisdiction_id INTEGER,
    name TEXT,
    slug TEXT,
    body_code TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE TABLE meetings (
    id INTEGER PRIMARY KEY,
    body TEXT,
    meeting_id TEXT,
    updated_at TEXT
);
CREATE TABLE jurisdictions (
    id INTEGER PRIMARY KEY,
    name TEXT
);
"""


def make_engine(bodies=(), meetings=()):
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for statement in SCHEMA.strip().split(";"):
            if statement.strip():
                conn.execute(text(statement))
        for i, (code, name) in enumerate(bodies, start=1):
            conn.execute(
                text(
                    "INSERT INTO public_bodies (id, jurisdiction_id, name, slug, "
                    "body_code, created_at, updated_at) VALUES "
                    "(:i, 1, :n, :s, :c, '2026-07-02', '2026-07-02')"
                ),
                {"i": i, "n": name, "s": name.lower().replace(" ", "-"), "c": code},
            )
        for i, body in enumerate(meetings, start=1):
            conn.execute(
                text("INSERT INTO meetings (id, body, meeting_id, updated_at) "
                     "VALUES (:i, :b, :m, '2026-09-01')"),
                {"i": i, "b": body, "m": f"m{i}"},
            )
    return engine


# ── sentinels ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("value", ["__skip__", "", "   ", None])
def test_sentinels_are_recognised(value):
    assert is_sentinel(value)


@pytest.mark.parametrize("value", ["bos", "chandler-pz", "phoenix-gp"])
def test_real_codes_are_not_sentinels(value):
    assert not is_sentinel(value)


# ── sync ordering ───────────────────────────────────────────────────────


def test_reference_order_flags_parent_after_dependent():
    problems = reference_order_problems(["meetings", "public_bodies", "jurisdictions"])
    assert any("public_bodies" in p and "precede" in p for p in problems)


def test_reference_order_accepts_correct_declarations():
    assert reference_order_problems(
        ["jurisdictions", "public_bodies", "meetings"]
    ) == []


def test_reference_order_flags_missing_parent():
    problems = reference_order_problems(["jurisdictions", "meetings"])
    assert any("public_bodies" in p and "missing" in p for p in problems)


def test_shipped_sync_declarations_respect_required_order():
    """The real release declaration must satisfy the invariant."""
    from db.sync_declarations import ALL_SYNC_TABLES

    assert reference_order_problems(ALL_SYNC_TABLES) == []


# ── dangling references ─────────────────────────────────────────────────


def test_dangling_references_finds_orphans():
    engine = make_engine(
        bodies=[("chandler-pz", "Chandler Planning & Zoning Commission")],
        meetings=["chandler-pz", "chandler-pz", "chandler-planning-zoning-commission"],
    )
    dangling = dangling_references(engine)
    assert dangling[("meetings", "body")] == {
        "chandler-planning-zoning-commission": 1
    }


def test_resolved_references_report_nothing():
    engine = make_engine(
        bodies=[("chandler-pz", "Chandler Planning & Zoning Commission")],
        meetings=["chandler-pz"],
    )
    assert ("meetings", "body") not in dangling_references(engine)


# ── fail-closed verdict: the production failure ─────────────────────────


def test_canonical_code_cannot_sync_without_its_public_bodies_row():
    """Reproduces production: 114 meetings on chandler-pz, no body row."""
    engine = make_engine(meetings=["chandler-pz"] * 3)
    problems = integrity_problems(engine, scope_codes=["chandler-pz"])
    assert problems, "gate must refuse a canonical code with no registry row"
    assert any("chandler-pz" in p for p in problems)


def test_gate_passes_once_the_registry_row_exists():
    engine = make_engine(
        bodies=[("chandler-pz", "Chandler Planning & Zoning Commission")],
        meetings=["chandler-pz"] * 3,
    )
    assert integrity_problems(engine, scope_codes=["chandler-pz"]) == []


def test_sentinel_is_refused_and_never_promoted():
    engine = make_engine(bodies=[("bos", "Board of Supervisors")], meetings=["__skip__"])
    problems = integrity_problems(engine, scope_codes=["bos"])
    assert any("__skip__" in p and "SENTINEL" in p for p in problems)


def test_empty_body_value_is_refused():
    engine = make_engine(bodies=[("bos", "Board of Supervisors")], meetings=[""])
    problems = integrity_problems(engine, scope_codes=["bos"])
    assert any("SENTINEL" in p for p in problems)


def test_tracked_exception_is_tolerated_but_must_be_declared():
    """phoenix-gp is a real body with its own defect — tolerated only if listed."""
    engine = make_engine(meetings=["phoenix-gp"] * 2)

    undeclared = integrity_problems(engine)
    assert undeclared, "an undeclared dangling code must still be caught"

    declared = integrity_problems(engine, tracked_exceptions=["phoenix-gp"])
    assert declared == []


def test_scope_isolates_the_merge_operation():
    """Class 3/4/5 defects must not block the Chandler/Mesa correction."""
    engine = make_engine(
        bodies=[("chandler-planning-zoning-commission", "legacy")],
        meetings=["chandler-pz", "phoenix-gp", "peoria-pz", "__skip__"],
    )
    scoped = integrity_problems(engine, scope_codes=["chandler-pz"])
    assert any("chandler-pz" in p for p in scoped)
    assert not any("phoenix-gp" in p for p in scoped)
    assert any("SENTINEL" in p for p in scoped), "sentinels are always blockers"


# ── the updated_at root cause ───────────────────────────────────────────


def test_correction_update_advances_updated_at():
    sql = correction_update(
        "public_bodies", set_column="body_code", key_column="body_code"
    )
    assert "updated_at" in sql
    assert "CURRENT_TIMESTAMP" in sql


def test_correction_update_refuses_a_write_that_cannot_converge():
    with pytest.raises(ValueError, match="sync stamp"):
        correction_update(
            "public_bodies",
            set_column="body_code",
            key_column="body_code",
            stamp_column="",
        )


def test_lint_detects_the_exact_production_defect():
    """The shape at body_code_merge_runtime.py:1589 — no updated_at."""
    source = (
        'connection.execute(text("UPDATE public_bodies SET body_code=:new '
        'WHERE body_code=:old"), {"old": old, "new": new})'
    )
    problems = unbumped_write_problems(source, tables=["public_bodies"])
    assert len(problems) == 1
    assert "updated_at" in problems[0]


def test_lint_accepts_a_stamped_write():
    source = (
        'connection.execute(text("UPDATE public_bodies SET body_code=:new, '
        'updated_at=CURRENT_TIMESTAMP WHERE body_code=:old"))'
    )
    assert unbumped_write_problems(source, tables=["public_bodies"]) == []


def test_lint_ignores_writes_to_unwatched_tables():
    source = 'text("UPDATE agenda_items SET body=:new WHERE body=:old")'
    assert unbumped_write_problems(source, tables=["public_bodies"]) == []


def test_lint_ignores_non_body_writes():
    source = 'text("UPDATE public_bodies SET name=:n WHERE body_code=:old")'
    assert unbumped_write_problems(source, tables=["public_bodies"]) == []


def test_shipped_merge_runtime_has_no_unstamped_body_write():
    """The reviewed runtime must not contain the defect class this gate blocks.

    Regression test for Brief 033 §1.  This carried a strict ``xfail`` until the
    runtime stamped ``updated_at`` on its rewrites (Brief 037 §2, defect instance
    1).  The marker was removed when the defect was fixed — which is what
    ``strict=True`` was there to force.
    """
    runtime = REPO_ROOT / "scripts" / "body_code_merge_runtime.py"
    if not runtime.is_file():
        pytest.skip("merge runtime not present")
    problems = unbumped_write_problems(
        runtime.read_text(encoding="utf-8"), tables=["public_bodies"]
    )
    assert problems == [], (
        "body_code_merge_runtime.py rewrites public_bodies.body_code without "
        f"advancing updated_at — the row will never reach production: {problems}"
    )
