"""Executable isolated-cluster PostgreSQL integration tests (Brief 031E item 10).

These spin their own throwaway cluster in a temp directory.  They never touch
production, never restore a production dump, and never reuse an existing
database.  They exercise the two behaviours the review says were only claimed:

  * optional-table present/absent capability binding, and
  * refusal of an unknown polymorphic class.

They are NOT skipped: if the cluster cannot be created the test fails loudly.
"""

import os
import hashlib
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import body_code_merge_runtime as rt  # noqa: E402

_PG_CANDIDATES = [
    Path(os.environ["BODY_MERGE_PG_BIN"]) if os.environ.get("BODY_MERGE_PG_BIN") else None,
    Path("/opt/homebrew/bin"),
    Path("/opt/homebrew/opt/postgresql@18/bin"),
    Path("/usr/local/bin"),
]
PG = next((path for path in _PG_CANDIDATES if path and (path / "initdb").exists()),
          Path("/opt/homebrew/bin"))

BASE_SCHEMA = """
CREATE TABLE meetings (
    id serial PRIMARY KEY,
    meeting_id text NOT NULL,
    body text NOT NULL,
    meeting_title text,
    source_url text,
    last_synced_at timestamptz,
    UNIQUE (body, meeting_id)
);
CREATE TABLE agenda_items (
    id serial PRIMARY KEY,
    meeting_db_id integer,
    body text,
    agenda_item_number text,
    source_id integer
);
CREATE TABLE meeting_events (
    id serial PRIMARY KEY,
    meeting_id text
);
-- deliberately NO body column: the regression that hid this table before
CREATE TABLE entity_mentions (
    id serial PRIMARY KEY,
    body text,
    source_type text,
    source_id integer
);
CREATE TABLE entity_relationships (
    id serial PRIMARY KEY,
    provenance_type text,
    provenance_id integer,
    source_type text,
    source_id integer
);
CREATE TABLE meeting_members (
    id serial PRIMARY KEY,
    body text,
    meeting_id text,
    meeting_db_id integer,
    member_id integer,
    UNIQUE (body, meeting_id, member_id)
);
CREATE TABLE case_events (
    id serial PRIMARY KEY, body text, meeting_db_id integer, agenda_item_id integer
);
CREATE TABLE agenda_item_votes (
    id serial PRIMARY KEY, body text, meeting_db_id integer, agenda_item_id integer
);
CREATE TABLE pz_item_details (
    id serial PRIMARY KEY, body text, meeting_db_id integer, agenda_item_id integer
);
CREATE TABLE supporting_documents (
    id serial PRIMARY KEY, body text, meeting_db_id integer,
    agenda_item_db_id integer
);
"""

OPTIONAL_SCHEMA = """
CREATE TABLE agenda_item_key_reservation (
    id serial PRIMARY KEY,
    body text,
    meeting_db_id integer,
    agenda_item_number text
);
CREATE TABLE _pattern_cascade_watermark (
    body text PRIMARY KEY,
    last_run_at timestamptz
);
ALTER TABLE agenda_items ADD COLUMN parent_item_id integer;
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _run(args, **kwargs):
    env = dict(os.environ)
    env["LC_ALL"] = "C"          # required: PG18 aborts otherwise
    env["LANG"] = "C"
    result = subprocess.run([str(a) for a in args], text=True, env=env,
                            capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(f"{args[0]} failed: {result.stderr.strip()}")
    return result


@pytest.fixture(scope="module")
def cluster():
    """A throwaway PostgreSQL cluster; torn down even on failure."""
    if not (PG / "initdb").exists():
        pytest.fail(f"postgresql@18 binaries not found at {PG}")
    with tempfile.TemporaryDirectory(prefix="poliscopic-031e-pg-") as temp:
        data = Path(temp) / "cluster"
        port = _free_port()
        _run([PG / "initdb", "-A", "trust", "-U", "poliscopic", "-D", data,
              "--encoding=UTF8", "--locale=C"])
        _run([PG / "pg_ctl", "-D", data, "-l", Path(temp) / "postgres.log",
              "-o", f"-h 127.0.0.1 -p {port}", "-w", "start"])
        try:
            yield {"port": port, "data": data, "temp": Path(temp)}
        finally:
            _run([PG / "pg_ctl", "-D", data, "-m", "fast", "-w", "stop"],
                 check=False)


@pytest.fixture
def engine(cluster, request):
    """A fresh database per test, created from the base schema."""
    stem = "".join(c for c in request.node.name if c.isalnum()).lower()
    # Keep the runtime's identity guard meaningful: disposable databases must
    # be classified as development scratch targets, not arbitrary names.
    suffix = hashlib.sha1(request.node.name.encode()).hexdigest()[:8]
    name = f"{rt.SCRATCH_PREFIX}_{stem[:25]}_{suffix}"
    port = cluster["port"]
    _run([PG / "createdb", "-h", "127.0.0.1", "-p", port, "-U", "poliscopic",
          name])
    eng = create_engine(
        f"postgresql://poliscopic@127.0.0.1:{port}/{name}", future=True)
    with eng.begin() as connection:
        connection.execute(text(BASE_SCHEMA))
    try:
        yield eng
    finally:
        eng.dispose()


def _with_optionals(engine):
    with engine.begin() as connection:
        connection.execute(text(OPTIONAL_SCHEMA))


# ── optional-table present / absent (the previously skipped placeholder) ──

def test_capabilities_report_absence_when_optionals_are_missing(engine):
    with engine.connect() as connection:
        caps = rt.schema_capabilities(connection)
    assert caps["optional_tables"]["agenda_item_key_reservation"] is False
    assert caps["optional_tables"]["_pattern_cascade_watermark"] is False
    assert caps["optional_table_signatures"]["agenda_item_key_reservation"] is None
    assert caps["optional_columns"]["agenda_items.parent_item_id"] is False


def test_capabilities_report_presence_with_column_signatures(engine):
    _with_optionals(engine)
    with engine.connect() as connection:
        caps = rt.schema_capabilities(connection)
    assert caps["optional_tables"]["agenda_item_key_reservation"] is True
    assert caps["optional_columns"]["agenda_items.parent_item_id"] is True
    signature = caps["optional_table_signatures"]["agenda_item_key_reservation"]
    names = [row[0] for row in signature]
    assert "meeting_db_id" in names and "agenda_item_number" in names


def test_capability_drift_is_refused_between_shapes(engine):
    """A plan bound to the absent shape must refuse the present shape."""
    with engine.connect() as connection:
        absent = rt.schema_capabilities(connection)
    _with_optionals(engine)
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            rt.assert_capabilities(connection, {"capabilities": absent})
    assert "schema drift" in str(excinfo.value)


# ── unknown polymorphic class refusal (the previously skipped placeholder) ─

def test_inventory_refuses_an_unknown_polymorphic_value(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO entity_mentions (body, source_type, source_id) "
            "VALUES ('chandler-cc', 'made_up_class', 1)"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            rt.reference_inventory(connection)
    assert "made_up_class" in str(excinfo.value)


def test_inventory_accepts_declared_polymorphic_values(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO entity_mentions (body, source_type, source_id) "
            "VALUES ('chandler-cc', 'agenda_item', 1)"))
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
    entry = inventory["polymorphic"]["entity_mentions.source_type"]
    assert entry["present"] is True
    assert entry["values"] == ["agenda_item"]


def test_inventory_inspects_a_declared_table_without_a_body_column(engine):
    """The regression: entity_relationships has no body column.

    It must still be inspected, so an unknown provenance_type refuses.
    """
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO entity_relationships (provenance_type, provenance_id) "
            "VALUES ('undeclared_provenance', 5)"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            rt.reference_inventory(connection)
    assert "undeclared_provenance" in str(excinfo.value)


def test_inventory_refuses_unknown_on_the_always_null_column(engine):
    """entity_relationships.source_type is declared with an empty known set."""
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO entity_relationships (source_type, source_id) "
            "VALUES ('surprise', 7)"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError) as excinfo:
            rt.reference_inventory(connection)
    assert "surprise" in str(excinfo.value)


def test_inventory_reports_every_declared_class(engine):
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
    assert set(inventory["polymorphic"]) == {
        "entity_mentions.source_type",
        "entity_relationships.provenance_type",
        "entity_relationships.source_type",
    }
    # body-scoped unique constraints come from the live catalog
    assert "meeting_members" in inventory["body_unique_constraints"] or any(
        row[0] == "meeting_members"
        for row in inventory["body_unique_constraints"])


def test_inventory_drives_all_catalog_reference_mutations(engine):
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
    assert ["agenda_items", "meeting_db_id"] in inventory["meeting_references"]
    assert ["meeting_members", "meeting_db_id"] in inventory["meeting_references"]
    assert ["agenda_item_votes", "agenda_item_id"] in inventory["item_references"]
    assert ["meeting_events", "meeting_id"] not in inventory["meeting_references"]
    assert "meetings" not in rt.unique_twin_tables(inventory)


def test_inventory_accepts_all_declared_external_meeting_ids(engine):
    """Known source IDs never become database-PK reparent mutations."""
    declared = sorted(rt.SAFE_EXTERNAL_MEETING_COLUMNS)
    with engine.begin() as connection:
        for table, column in declared:
            connection.execute(text(
                f'CREATE TABLE IF NOT EXISTS "{table}" '
                f'(id serial PRIMARY KEY, body text, "{column}" varchar(64))'))
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
    references = {tuple(row) for row in inventory["meeting_references"]}
    assert not references.intersection(declared)


def test_inventory_refuses_unknown_meeting_id_deterministically(engine):
    with engine.begin() as connection:
        connection.execute(text(
            'CREATE TABLE z_unknown_meeting_ref '
            '(id serial PRIMARY KEY, meeting_id varchar(64))'))
        connection.execute(text(
            'CREATE TABLE a_unknown_meeting_ref '
            '(id serial PRIMARY KEY, meeting_id varchar(64))'))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match=(
                r"a_unknown_meeting_ref\.meeting_id")):
            rt.reference_inventory(connection)


def test_merge_policy_coverage_reports_both_tables_at_once(engine):
    with engine.connect() as connection:
        baseline = rt.merge_policy_coverage(connection)
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE agenda_items ADD COLUMN z_item_drift text"))
        connection.execute(text(
            "ALTER TABLE meetings ADD COLUMN a_meeting_drift text"))
    with engine.connect() as connection:
        actual = rt.merge_policy_coverage(connection)
    assert actual["agenda_items"] == baseline["agenda_items"] + ["z_item_drift"]
    assert actual["meetings"] == baseline["meetings"] + ["a_meeting_drift"]


def test_inventory_refuses_external_declaration_with_meetings_fk(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE article_sources (id integer PRIMARY KEY, "
            "meeting_id integer, CONSTRAINT article_meeting_fk "
            "FOREIGN KEY (meeting_id) REFERENCES meetings(id))"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match=(
                r"contradictory meeting_id semantics.*article_sources")):
            rt.reference_inventory(connection)


def test_fk_inventory_does_not_cross_associate_duplicate_constraint_names(engine):
    """Constraint names are table-local; catalog discovery must use OIDs."""
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE TABLE other_parent (id integer PRIMARY KEY)"))
        connection.execute(text(
            "CREATE TABLE real_meeting_ref (meeting_db_id integer, "
            "CONSTRAINT same_fk_name FOREIGN KEY (meeting_db_id) "
            "REFERENCES meetings(id))"))
        connection.execute(text(
            "CREATE TABLE unrelated_ref (meeting_db_id integer, "
            "CONSTRAINT same_fk_name FOREIGN KEY (meeting_db_id) "
            "REFERENCES other_parent(id))"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match=(
                r"unrelated_ref\.meeting_db_id")):
            rt.reference_inventory(connection)


def test_fk_inventory_does_not_accept_same_named_parent_in_other_schema(engine):
    with engine.begin() as connection:
        connection.execute(text("CREATE SCHEMA alternate"))
        connection.execute(text(
            "CREATE TABLE alternate.meetings (id integer PRIMARY KEY)"))
        connection.execute(text(
            "CREATE TABLE cross_schema_ref (meeting_db_id integer "
            "REFERENCES alternate.meetings(id))"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match=(
                r"cross_schema_ref\.meeting_db_id")):
            rt.reference_inventory(connection)


def test_inventory_refuses_natural_key_fk_as_pk_reference(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE meetings ADD CONSTRAINT meetings_source_id_uq "
            "UNIQUE (meeting_id)"))
        connection.execute(text(
            "CREATE TABLE source_key_ref (source_meeting_key text "
            "REFERENCES meetings(meeting_id))"))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match=r"natural-key FK"):
            rt.reference_inventory(connection)


def test_projected_unique_reference_collision_refuses_before_mutation(engine):
    with engine.begin() as connection:
        connection.execute(text(
            "ALTER TABLE agenda_item_votes ADD CONSTRAINT vote_item_uq "
            "UNIQUE (agenda_item_id)"))
        connection.execute(text(
            "INSERT INTO agenda_item_votes "
            "(body, meeting_db_id, agenda_item_id) VALUES "
            "('long', 1, 10), ('short', 2, 20)"))
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
        problems = rt.projected_reference_collisions(
            connection, inventory, [{"old_id": 10, "new_id": 20}])
    assert problems == ["agenda_item_votes.agenda_item_id:10->20"]


@pytest.mark.parametrize(
    ("table", "column", "needle"),
    [
        ("mystery_meeting_ref", "meeting_db_id", "ambiguous meeting_db_id"),
        ("mystery_item_ref", "agenda_item_id", "ambiguous agenda_item_id"),
        ("mystery_parent_ref", "parent_item_id", "ambiguous agenda-item reference"),
        ("mystery_item_db_ref", "agenda_item_db_id", "ambiguous agenda-item reference"),
        ("mystery_vote_ref", "agenda_item_vote_id", "ambiguous agenda_item_vote_id"),
    ],
)
def test_inventory_refuses_undeclared_non_fk_reference_columns(
        engine, table, column, needle):
    with engine.begin() as connection:
        connection.execute(text(f'CREATE TABLE "{table}" (id integer, "{column}" integer)'))
    with engine.connect() as connection:
        with pytest.raises(RuntimeError, match=needle):
            rt.reference_inventory(connection)


def test_exact_twins_are_removed_before_collision_audit(engine):
    with engine.begin() as connection:
        connection.execute(text("""
            INSERT INTO meeting_members
              (body, meeting_id, meeting_db_id, member_id)
            VALUES ('long', 'm-1', 1, 7), ('short', 'm-1', 2, 7)
        """))
    with engine.begin() as connection:
        inventory = rt.reference_inventory(connection)
        merge = {"old": "long", "new": "short"}
        assert rt._vote_collisions(connection, inventory, [merge])
        twins = rt.unique_twin_tables(inventory)
        assert "meeting_members" in twins
        merge["twin_dedup_maps"] = {
            "meeting_members": [{"old_id": 1, "new_id": 2}]
        }
        assert rt._vote_collisions(connection, inventory, [merge]) == []
        rt._delete_exact_unique_twins(
            connection, "meeting_members", "long", "short", twins["meeting_members"])
        assert rt._vote_collisions(connection, inventory, [merge]) == []


def test_inventory_does_not_treat_public_body_id_as_body(engine):
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE body_seats (
              id serial PRIMARY KEY,
              public_body_id integer NOT NULL,
              seat_name text NOT NULL,
              UNIQUE (public_body_id, seat_name)
            )
        """))
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
    assert not any(row[0] == "body_seats"
                   for row in inventory["body_unique_constraints"])


def test_twin_discovery_uses_constraint_columns_not_table_columns(engine):
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE meeting_attendance (
              id serial PRIMARY KEY,
              body text NOT NULL,
              meeting_id text,
              seat_key text,
              CONSTRAINT uq_attendance_seat UNIQUE (body, seat_key)
            )
        """))
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
    assert any(row[0] == "meeting_attendance" and row[1] == "uq_attendance_seat"
               for row in inventory["body_unique_constraints"])
    assert not any(row[0] == "meeting_attendance"
                   for row in inventory["unique_twin_constraints"])
    assert "meeting_attendance" not in rt.unique_twin_tables(inventory)


def test_inventory_only_unique_key_drives_twin_comparison(engine):
    """A live unique key must be used instead of the legacy table constant."""
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE meeting_members ADD COLUMN role text"))
        connection.execute(text("""
            ALTER TABLE meeting_members
            ADD CONSTRAINT uq_meeting_member_role UNIQUE (body, meeting_id, role)
        """))
        connection.execute(text("""
            INSERT INTO meeting_members
              (body, meeting_id, meeting_db_id, member_id, role)
            VALUES ('long', 'm-role', 1, 101, 'chair'),
                   ('short', 'm-role', 2, 202, 'chair')
        """))
    with engine.connect() as connection:
        inventory = rt.reference_inventory(connection)
        with pytest.raises(RuntimeError, match="multiple twin unique keys"):
            rt.unique_twin_tables(inventory)
        twins = {"meeting_members": ("meeting_id", "role")}
        bad = rt._unequal_unique_twins(
            connection, "meeting_members", "long", "short",
            [{"old_id": 1, "new_id": 2, "meeting_id": "m-role"}],
            twins["meeting_members"])
    assert bad and bad[0]["fields"] == ["member_id"]


def test_representative_full_merge_reparents_and_preserves_event_ids(engine, monkeypatch):
    """Exercise one complete transaction against a disposable PostgreSQL DB."""
    ddl = """
    CREATE TABLE public_bodies (id integer PRIMARY KEY, body_code text, body text);
    CREATE TABLE meetings (
      id integer PRIMARY KEY, body text, meeting_id text,
      meeting_title text, source_url text,
      UNIQUE (body, meeting_id)
    );
    CREATE TABLE agenda_items (
      id integer PRIMARY KEY, body text, meeting_id text,
      meeting_db_id integer, agenda_item_number text,
      agenda_item_id text UNIQUE,
      agenda_item_title text, agenda_item_text text, agenda_item_url text,
      vote_or_action text, source_url text,
      _multi_jurisdiction_backfilled boolean,
      swept_at timestamptz, search_vector tsvector
    );
    CREATE TABLE meeting_events (id integer PRIMARY KEY, meeting_id text, agenda_item_id integer);
    CREATE TABLE agenda_item_votes (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer, agenda_item_id integer);
    CREATE TABLE case_events (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer, agenda_item_id integer);
    CREATE TABLE pz_item_details (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer, agenda_item_id integer);
    CREATE TABLE supporting_documents (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer, agenda_item_db_id integer);
    CREATE TABLE meeting_members (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer, member_id integer,
      UNIQUE (body, meeting_id, member_id));
    CREATE TABLE meeting_attendance (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer, member_id integer);
    CREATE TABLE executive_session_participants (id integer PRIMARY KEY, body text,
      meeting_id text, meeting_db_id integer);
    CREATE TABLE entity_mentions (id integer PRIMARY KEY, body text,
      source_type text, source_id integer);
    CREATE TABLE entity_relationships (id integer PRIMARY KEY,
      provenance_type text, provenance_id integer, source_type text, source_id integer);
    CREATE TABLE _ingest_failures (id integer PRIMARY KEY, body text,
      meeting_id varchar(32), error text);
    CREATE TABLE article_sources (id integer PRIMARY KEY, body text,
      meeting_id varchar(32));
    CREATE TABLE dismissed_suggestions (id integer PRIMARY KEY, body text,
      meeting_id varchar(32));
    CREATE TABLE scanned_agenda_text (id integer PRIMARY KEY,
      meeting_id varchar(32));
    INSERT INTO public_bodies VALUES (1, 'long', 'long'), (2, 'short', 'short');
    INSERT INTO meetings VALUES
      (1, 'long', 'm-1', 'same', 'u'), (2, 'short', 'm-1', 'same', 'u');
    INSERT INTO agenda_items VALUES
      (10, 'long', 'm-1', 1, '1', 'old-source-id', 'Title', 'Text', 'http://x', 'Approved', 'http://x',
       true, '2026-09-18 02:00:00+00', to_tsvector('english', 'stale old vector')),
      (20, 'short', 'm-1', 2, '1', 'new-source-id', 'Title', 'Text', 'http://x', 'Approved', 'http://x',
       false, '2026-09-18 01:00:00+00', to_tsvector('english', 'stale new vector'));
    INSERT INTO meeting_events VALUES (30, 'external-m-1', 10);
    INSERT INTO agenda_item_votes VALUES (40, 'long', 'vote-ext', 1, 10);
    INSERT INTO case_events VALUES (50, 'long', 'case-ext', 1, 10);
    INSERT INTO pz_item_details VALUES (60, 'long', 'detail-ext', 1, 10);
    INSERT INTO supporting_documents VALUES (70, 'long', 'document-ext', 1, 10);
    INSERT INTO meeting_members VALUES (80, 'long', 'm-1', 1, 9);
    INSERT INTO meeting_members VALUES (83, 'short', 'm-1', 2, 9);
    INSERT INTO meeting_attendance VALUES (81, 'long', 'm-1', 1, 9);
    INSERT INTO executive_session_participants VALUES (82, 'long', 'm-1', 1);
    INSERT INTO entity_mentions VALUES (90, 'long', 'agenda_item', 10);
    INSERT INTO entity_relationships VALUES
      (91, 'meetings', 1, NULL, NULL),
      (92, 'agenda_item', 10, NULL, NULL);
    INSERT INTO _ingest_failures VALUES (93, 'long', 'm-1', 'pre-merge failure');
    INSERT INTO article_sources VALUES (94, 'long', 'article-ext');
    INSERT INTO dismissed_suggestions VALUES (95, 'long', 'dismissed-ext');
    INSERT INTO scanned_agenda_text VALUES (96, 'scan-ext');
    """
    with engine.begin() as connection:
        connection.execute(text("""
            DROP TABLE IF EXISTS entity_relationships, entity_mentions,
              _ingest_failures,
              supporting_documents, executive_session_participants,
              meeting_attendance, meeting_members, pz_item_details,
              case_events, agenda_item_votes, meeting_events, agenda_items,
              meetings, CASCADE
        """))
        connection.execute(text(ddl))
    with engine.connect() as connection:
        caps = rt.schema_capabilities(connection)
        inventory = rt.reference_inventory(connection)
        assert ["_ingest_failures", "meeting_id"] not in inventory["meeting_references"]
        assert "meeting_id" in inventory["reference_columns"]["_ingest_failures"]
        baseline = rt.protected_snapshot(connection, caps, inventory)
        event_contract = rt.event_contract_snapshot(connection)
        identity = rt.target_identity(connection)
    target = {"tier": "development", "database": engine.url.database}
    plan = {
        "kind": "body-code-merge", "version": 6,
        "tier": target["tier"], "target": target["database"],
        "target_identity": identity, "capabilities": caps,
        "inventory": inventory, "baseline": baseline,
        "event_contract": event_contract, "merges": [{
            "old": "long", "new": "short",
            "meeting_map": [{"old_id": 1, "new_id": 2, "meeting_id": "m-1"}],
            "item_map": [{"old_id": 10, "new_id": 20, "meeting_id": "m-1",
                          "number": "1", "preferred_agenda_item_id": "old-source-id",
                          "preferred_source_url": "http://x",
                          "preferred_item_url": "http://x"}],
            "twin_dedup_maps": {
                "meeting_members": [{"old_id": 80, "new_id": 83}]},
            "body_counts": {},
        }],
        "digest": "d" * 64,
    }
    monkeypatch.setattr(rt, "build_plan", lambda *_args, **_kwargs: plan)
    with engine.begin() as connection:
        stats = rt.execute_plan(connection, plan)
        assert stats["meetings"] == 1
        assert stats["long:meeting_members:dedup"] == 1
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM meetings WHERE body='long'")).scalar() == 0
        assert connection.execute(text("SELECT meeting_db_id FROM agenda_items WHERE id=20")).scalar() == 2
        assert connection.execute(text(
            "SELECT agenda_item_id FROM agenda_items WHERE id=20"
        )).scalar() == "old-source-id"
        merged_item = connection.execute(text("""
            SELECT _multi_jurisdiction_backfilled,
                   swept_at = '2026-09-18 02:00:00+00'::timestamptz,
                   search_vector =
                     setweight(to_tsvector('english', 'Title'), 'A') ||
                     setweight(to_tsvector('english', 'Text'), 'B')
            FROM agenda_items WHERE id=20
        """)).one()
        assert tuple(merged_item) == (True, True, True)
        assert connection.execute(text("SELECT agenda_item_id FROM meeting_events WHERE id=30")).scalar() == 20
        assert connection.execute(text("SELECT meeting_id FROM meeting_events WHERE id=30")).scalar() == "external-m-1"
        assert connection.execute(text("SELECT source_id FROM entity_mentions WHERE id=90")).scalar() == 20
        assert connection.execute(text("SELECT provenance_id FROM entity_relationships WHERE id=91")).scalar() == 2
        assert connection.execute(text("SELECT provenance_id FROM entity_relationships WHERE id=92")).scalar() == 20
        assert connection.execute(text("SELECT source_id FROM entity_relationships WHERE id=91")).scalar() is None
        assert connection.execute(text("SELECT body FROM _ingest_failures WHERE id=93")).scalar() == "short"
        assert connection.execute(text("SELECT meeting_id FROM _ingest_failures WHERE id=93")).scalar() == "m-1"
        expected_external = {
            "agenda_item_votes": "vote-ext",
            "case_events": "case-ext",
            "pz_item_details": "detail-ext",
            "supporting_documents": "document-ext",
            "meeting_members": "m-1",
            "meeting_attendance": "m-1",
            "executive_session_participants": "m-1",
            "article_sources": "article-ext",
            "dismissed_suggestions": "dismissed-ext",
        }
        for table, expected in expected_external.items():
            row = connection.execute(text(
                f'SELECT body, meeting_id FROM "{table}" LIMIT 1')).one()
            assert tuple(row) == ("short", expected)
        assert connection.execute(text(
            "SELECT meeting_id FROM scanned_agenda_text WHERE id=96"
        )).scalar() == "scan-ext"
