"""Pin the live single-row, identity-keyed linkage behaviour.

``event_normalize`` inserts one event per extraction and links *that* extraction
with the id the insert returned.  There is no batch insert, no ``RETURNING`` list,
and no positional pairing — so the shift that mislinked extraction ids 24195-24606
in the archived normalizer cannot recur on the live path.  These tests pin that.

They run against a throwaway in-memory SQLite database via the shared helper and
never touch the dev or production databases.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import text

from scripts.entities.event_normalize_storage import fetch_normalization_page
from scripts.entities.event_normalize_write_storage import SqlAlchemyWriteStorage
from scripts.entities.event_normalize_writes import apply_classification_plan
from scripts.entities.event_normalize_work_items import build_plan_from_work_items

from _kg_event_normalize_sqlite import add_extraction, build_engine, seed

_ENTITIES = Path(__file__).resolve().parents[1] / "scripts" / "entities"
_STORAGE = _ENTITIES / "event_normalize_write_storage.py"
_WRITES = _ENTITIES / "event_normalize_writes.py"


@pytest.fixture()
def engine():
    return build_engine()


def test_insert_event_returns_its_own_row_id(engine):
    """One insert, one returned id — the id belongs to the row just written."""
    storage = SqlAlchemyWriteStorage()
    values = dict(meeting_id="2024-03-05-CC", supporting_document_id=1,
                  event_type_id=1, outcome="approved", action_verb="approved",
                  span_start=5, span_end=15, case_number=None)
    with engine.begin() as conn:
        first = storage.insert_event(conn, values)
        second = storage.insert_event(
            conn, {**values, "action_verb": "denied", "span_start": 50, "span_end": 60}
        )
    assert first != second
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT action_verb, text_offset_start FROM meeting_events "
                 "WHERE id = :id"),
            {"id": second},
        ).mappings().one()
    assert (row["action_verb"], row["text_offset_start"]) == ("denied", 50)


def test_apply_links_each_extraction_to_its_own_candidate(engine):
    """Three extractions, three events, each linked to the span it came from."""
    seed(engine, linked=False, span_start=0, span_end=10)
    add_extraction(engine, xid=2, doc_id=1, action_verb="approved",
                   span_start=20, span_end=30)
    add_extraction(engine, xid=3, doc_id=1, action_verb="approved",
                   span_start=40, span_end=50)

    page = fetch_normalization_page(engine)
    plan = build_plan_from_work_items(page.work_items)
    assert len(plan.event_inserts) == 3
    apply_classification_plan(engine, plan)

    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT x.id AS extraction_id, x.text_offset_start AS x_start, "
            "       e.id AS event_id, e.text_offset_start AS e_start "
            "  FROM meeting_event_extractions x "
            "  JOIN meeting_events e ON e.id = x.meeting_event_id "
            " ORDER BY x.id"
        )).mappings().all()

    assert len(rows) == 3
    # Every extraction resolved — none was silently dropped.
    assert [r["extraction_id"] for r in rows] == [1, 2, 3]
    # Linkage is injective: no event was shared or skipped.
    assert len({r["event_id"] for r in rows}) == 3
    # And each extraction points at the event built from its own span.
    for row in rows:
        assert row["e_start"] == row["x_start"]


def test_live_write_path_has_no_positional_pairing_or_batch_insert():
    """The defect mechanism must be structurally absent from the live path."""
    for path in (_STORAGE, _WRITES):
        source = path.read_text()
        tree = ast.parse(source)
        zip_calls = [
            ast.get_source_segment(source, node) or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "zip"
        ]
        assert not zip_calls, f"{path.name} pairs by position: {zip_calls}"
        assert "execute_values" not in source, f"{path.name} batch-inserts"


def test_insert_event_is_a_single_row_returning_statement():
    """The insert carries its own ``RETURNING id``; no id list is zipped later."""
    source = _STORAGE.read_text()
    assert "RETURNING id" in source
    assert "VALUES %s" not in source
