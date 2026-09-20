"""Isolated accounting tests for scripts/entities/graph_builder.py.

Covers the structured per-source counters (SourceStats) and the aggregate
phase totals returned by run_phase(): entities/edges/mentions attempted and
inserted, replay collisions (same-run duplicates and DB-existing identities),
and unresolved endpoints/entities — plus dry-run semantics (planned, never
committed).

Uses only the isolated function-scoped SQLite fixture (fresh_session); no
network, development, or production access.
"""

import pytest
from sqlalchemy import text

import scripts.entities.graph_builder as gb
import scripts.entities.graph_builder_materialization as gb_materialization
import scripts.entities.graph_builder_runtime as gb_runtime


# ── Test doubles ──────────────────────────────────────────────────────────


class _StaticSource(gb.Source):
    """A source whose query returns one dummy row and whose produce() yields
    a fixed spec list, so tests can drive run_source/run_phase deterministically."""

    query = "SELECT 1 AS marker"
    description = "static test source"

    def __init__(self, name, entities=(), edges=(), mentions=()):
        self.name = name
        self._entities = list(entities)
        self._edges = list(edges)
        self._mentions = list(mentions)

    def produce(self, rows):
        for _ in rows:
            for es in self._entities:
                yield es, None, None
            for ed in self._edges:
                yield None, ed, None
            for ms in self._mentions:
                yield None, None, ms


def _entity(norm, etype="organization"):
    return gb.EntitySpec(name=norm.title(), normalized_name=norm,
                         entity_type=etype)


def _edge(frm, to, source_type="test_src", source_id=1, relationship="HAS_APPLICANT"):
    return gb.EdgeSpec(from_entity_norm=frm[0], from_type=frm[1],
                       to_entity_norm=to[0], to_type=to[1],
                       relationship=relationship, source_type=source_type,
                       source_id=source_id)


def _mention(norm, etype="organization", source_type="test_src", source_id=1):
    return gb.MentionSpec(entity_norm=norm, entity_type=etype,
                          source_type=source_type, source_id=source_id,
                          mention_text=norm, role=None)


def _count(engine, table):
    with engine.connect() as c:
        return c.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()


def _run_source(engine, source, cache=None, dry_run=False):
    cache = {} if cache is None else cache
    with engine.begin() as conn:
        stats = gb.run_source(source, conn, cache, dry_run=dry_run,
                              verbose=False)
    return stats, cache


class _CapturingConnection:
    """Minimal execute seam for asserting materialization bind values."""

    def __init__(self):
        self.statement = None
        self.params = None

    def execute(self, statement, params):
        self.statement = statement
        self.params = params


# ── Fixtures ──────────────────────────────────────────────────────────────


@pytest.fixture()
def engine(fresh_session):
    """Engine bound to the isolated temp-file SQLite schema."""
    return fresh_session.get_bind()


# ── Source-level accounting ───────────────────────────────────────────────


def test_first_execution_inserts_every_class(engine):
    src = _StaticSource(
        "first_run",
        entities=[_entity("acme development"), _entity("chandler", "jurisdiction")],
        edges=[_edge(("acme development", "organization"),
                     ("chandler", "jurisdiction"))],
        mentions=[_mention("acme development")],
    )
    stats, cache = _run_source(engine, src)
    assert stats.entities_attempted == 2
    assert stats.entities_inserted == 2
    assert stats.edges_attempted == 1
    assert stats.edges_inserted == 1
    assert stats.edge_replay_collisions == 0
    assert stats.edges_unresolved_endpoint == 0
    assert stats.mentions_attempted == 1
    assert stats.mentions_inserted == 1
    assert stats.mention_replay_collisions == 0
    assert stats.mentions_unresolved_entity == 0
    assert len(stats.new_ids) == 2
    assert _count(engine, "entities") == 2
    assert _count(engine, "entity_relationships") == 1
    assert _count(engine, "entity_mentions") == 1


def test_identical_replay_inserts_zero_and_reports_collisions(engine):
    src = _StaticSource(
        "replay",
        entities=[_entity("acme development"), _entity("chandler", "jurisdiction")],
        edges=[_edge(("acme development", "organization"),
                     ("chandler", "jurisdiction"))],
        mentions=[_mention("acme development")],
    )
    stats1, cache = _run_source(engine, src)
    cache.update(stats1.new_ids)

    stats2, _ = _run_source(engine, src, cache=cache)
    assert stats2.entities_attempted == 2
    assert stats2.entities_inserted == 0
    assert stats2.edges_attempted == 1
    assert stats2.edges_inserted == 0
    assert stats2.edge_replay_collisions == 1
    assert stats2.edges_unresolved_endpoint == 0
    assert stats2.mentions_attempted == 1
    assert stats2.mentions_inserted == 0
    assert stats2.mention_replay_collisions == 1
    assert stats2.mentions_unresolved_entity == 0
    # DB state unchanged by the replay
    assert _count(engine, "entities") == 2
    assert _count(engine, "entity_relationships") == 1
    assert _count(engine, "entity_mentions") == 1


def test_replay_detects_every_provenance_type_emitted_by_one_source(engine):
    endpoints = [_entity("acme development"), _entity("chandler", "jurisdiction")]
    edges = [
        _edge(
            ("acme development", "organization"),
            ("chandler", "jurisdiction"),
            source_type="first_source_type",
            source_id=10,
        ),
        _edge(
            ("acme development", "organization"),
            ("chandler", "jurisdiction"),
            source_type="second_source_type",
            source_id=20,
        ),
    ]
    source = _StaticSource("mixed_provenance", entities=endpoints, edges=edges)

    first_run, entity_cache = _run_source(engine, source)
    entity_cache.update(first_run.new_ids)
    replay, _ = _run_source(engine, source, cache=entity_cache)

    assert first_run.edges_inserted == 2
    assert replay.edges_attempted == 2
    assert replay.edges_inserted == 0
    assert replay.edge_replay_collisions == 2
    assert _count(engine, "entity_relationships") == 2


def test_duplicate_specs_within_one_run_counted_as_replay_collisions(engine):
    edge = _edge(("acme development", "organization"),
                 ("chandler", "jurisdiction"))
    mention = _mention("acme development")
    src = _StaticSource(
        "dup_run",
        entities=[_entity("acme development"), _entity("chandler", "jurisdiction")],
        edges=[edge, edge],
        mentions=[mention, mention],
    )
    stats, _ = _run_source(engine, src)
    assert stats.entities_attempted == 2
    assert stats.entities_inserted == 2
    assert stats.edges_attempted == 2
    assert stats.edges_inserted == 1
    assert stats.edge_replay_collisions == 1
    assert stats.mentions_attempted == 2
    assert stats.mentions_inserted == 1
    assert stats.mention_replay_collisions == 1
    assert _count(engine, "entity_relationships") == 1
    assert _count(engine, "entity_mentions") == 1


def test_unresolved_edge_endpoint_counted_and_not_inserted(engine):
    src = _StaticSource(
        "ghost_edge",
        entities=[_entity("acme development")],
        edges=[_edge(("acme development", "organization"),
                     ("never-seen", "organization"))],
    )
    stats, _ = _run_source(engine, src)
    assert stats.edges_attempted == 1
    assert stats.edges_inserted == 0
    assert stats.edge_replay_collisions == 0
    assert stats.edges_unresolved_endpoint == 1
    assert stats.entities_inserted == 1
    assert _count(engine, "entity_relationships") == 0


def test_unresolved_mention_entity_counted_and_not_inserted(engine):
    src = _StaticSource(
        "ghost_mention",
        mentions=[_mention("never-seen")],
    )
    stats, _ = _run_source(engine, src)
    assert stats.mentions_attempted == 1
    assert stats.mentions_inserted == 0
    assert stats.mention_replay_collisions == 0
    assert stats.mentions_unresolved_entity == 1
    assert _count(engine, "entity_mentions") == 0


def test_dry_run_reports_planned_but_claims_no_inserts(engine):
    src = _StaticSource(
        "dry_run",
        entities=[_entity("acme development"), _entity("chandler", "jurisdiction")],
        edges=[_edge(("acme development", "organization"),
                     ("chandler", "jurisdiction"))],
        mentions=[_mention("acme development")],
    )
    stats, _ = _run_source(engine, src, dry_run=True)
    assert stats.entities_attempted == 2
    assert stats.entities_inserted == 0
    assert stats.entities_planned == 2
    assert stats.edges_attempted == 1
    assert stats.edges_inserted == 0
    assert stats.edges_planned == 1
    assert stats.mentions_attempted == 1
    assert stats.mentions_inserted == 0
    assert stats.mentions_planned == 1
    # Dry run must not write anything
    assert _count(engine, "entities") == 0
    assert _count(engine, "entity_relationships") == 0
    assert _count(engine, "entity_mentions") == 0


def test_mention_insert_binds_false_as_a_boolean_parameter():
    """The PostgreSQL boolean column must receive a bool, not SQLite's 0."""
    connection = _CapturingConnection()
    gb_materialization._insert_mentions(
        connection,
        [(1, _mention("acme development"))],
    )

    assert ":withdrawn0" in str(connection.statement)
    assert connection.params["withdrawn0"] is False
    assert isinstance(connection.params["withdrawn0"], bool)


def test_forced_phase_propagates_source_failure_and_rolls_back(engine, monkeypatch):
    """Force bypasses watermarks only; it never permits partial source success."""
    executed_sources = []

    class _RecordingSource(_StaticSource):
        def produce(self, rows):
            executed_sources.append(self.name)
            yield from super().produce(rows)

    failing = _RecordingSource(
        "failing_source",
        entities=[_entity("rolled back entity")],
        mentions=[_mention("rolled back entity")],
    )
    later = _RecordingSource(
        "later_source",
        entities=[_entity("must not run")],
    )

    def fail_mention_insert(connection, rows):
        raise RuntimeError("deliberate mention materialization failure")

    entity_cache = {}
    monkeypatch.setattr(gb_materialization, "_insert_mentions", fail_mention_insert)
    monkeypatch.setattr(gb_runtime, "load_all_entity_ids", lambda connection: entity_cache)
    monkeypatch.setattr(gb, "SOURCES", [failing, later])

    with pytest.raises(RuntimeError, match="deliberate mention"):
        gb.run_phase(engine, force=True, verbose=False)

    assert executed_sources == ["failing_source"]
    assert entity_cache == {}
    assert _count(engine, "entities") == 0
    assert _count(engine, "entity_mentions") == 0
    with engine.connect() as connection:
        watermark_count = connection.execute(text(
            "SELECT COUNT(*) FROM _graph_builder_watermark"
        )).scalar()
    assert watermark_count == 0


# ── Phase-level aggregation ───────────────────────────────────────────────


def test_aggregate_phase_totals_and_replay(engine, monkeypatch):
    src_a = _StaticSource(
        "src_a",
        entities=[_entity("alpha corp"), _entity("mesa", "jurisdiction")],
        edges=[_edge(("alpha corp", "organization"), ("mesa", "jurisdiction"),
                     source_type="src_a", source_id=10)],
        mentions=[_mention("alpha corp", source_type="src_a", source_id=10)],
    )
    src_b = _StaticSource(
        "src_b",
        entities=[_entity("beta llc"), _entity("tempe", "jurisdiction")],
        edges=[_edge(("beta llc", "organization"), ("tempe", "jurisdiction"),
                     source_type="src_b", source_id=20)],
        mentions=[_mention("beta llc", source_type="src_b", source_id=20)],
    )
    monkeypatch.setattr(gb, "SOURCES", [src_a, src_b])

    # Dry-run first on the empty DB: planned totals, no committed writes.
    dry = gb.run_phase(engine, dry_run=True, force=True, verbose=False)
    assert dry["dry_run"] is True
    assert dry["entities_created"] == 0
    assert dry["edges_created"] == 0
    assert dry["entities_attempted"] == 4
    assert dry["entities_inserted"] == 0
    assert dry["entities_planned"] == 4
    assert dry["edges_attempted"] == 2
    assert dry["edges_inserted"] == 0
    assert dry["edges_planned"] == 2
    assert dry["mentions_attempted"] == 2
    assert dry["mentions_inserted"] == 0
    assert dry["mentions_planned"] == 2
    assert _count(engine, "entities") == 0

    # Real run: committed inserts match legacy and explicit counters.
    live = gb.run_phase(engine, force=True, verbose=False)
    assert live["success"] is True
    assert live["dry_run"] is False
    assert live["entities_created"] == 4
    assert live["edges_created"] == 2
    assert live["entities_attempted"] == 4
    assert live["entities_inserted"] == 4
    assert live["edges_attempted"] == 2
    assert live["edges_inserted"] == 2
    assert live["edge_replay_collisions"] == 0
    assert live["edges_unresolved_endpoint"] == 0
    assert live["mentions_attempted"] == 2
    assert live["mentions_inserted"] == 2
    assert live["mention_replay_collisions"] == 0
    assert live["mentions_unresolved_entity"] == 0
    assert live["sources_total"] == 2
    assert live["sources_skipped"] == 0
    assert _count(engine, "entities") == 4
    assert _count(engine, "entity_relationships") == 2
    assert _count(engine, "entity_mentions") == 2

    # Identical replay at phase level: zero inserts, collisions only.
    replay = gb.run_phase(engine, force=True, verbose=False)
    assert replay["entities_created"] == 0
    assert replay["edges_created"] == 0
    assert replay["entities_inserted"] == 0
    assert replay["edges_attempted"] == 2
    assert replay["edges_inserted"] == 0
    assert replay["edge_replay_collisions"] == 2
    assert replay["mentions_attempted"] == 2
    assert replay["mentions_inserted"] == 0
    assert replay["mention_replay_collisions"] == 2
    assert _count(engine, "entities") == 4
    assert _count(engine, "entity_relationships") == 2
    assert _count(engine, "entity_mentions") == 2
