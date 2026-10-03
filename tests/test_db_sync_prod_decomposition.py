"""Characterization tests for the sync_prod decomposition.

These pin the *behavior-preserving* contract of splitting
``scripts/db/sync_prod.py`` into focused modules: table membership and ordering,
constants, CLI surface, facade re-export identity, module size, and the
monkeypatch seams external tests rely on.

Everything runs isolated.  No database connection, no network, no sync.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
for _p in (REPO_ROOT, REPO_ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from db import sync_declarations  # noqa: E402
from db import sync_meta  # noqa: E402
from db import sync_prod  # noqa: E402
from db import sync_reconcile  # noqa: E402
from db import sync_schema  # noqa: E402
from db import sync_targets  # noqa: E402
from db import sync_uniques  # noqa: E402
from db import sync_upsert  # noqa: E402
from db import sync_validate  # noqa: E402
from db import sync_runtime  # noqa: E402

SYNC_MODULES = (
    "sync_prod", "sync_declarations", "sync_targets", "sync_schema",
    "sync_meta", "sync_uniques", "sync_reconcile", "sync_upsert", "sync_validate",
)

#: Names that must remain importable from the facade exactly as before.
FACADE_NAMES = (
    "ALL_SYNC_TABLES", "AUTO_COLUMNS", "BATCH_SIZE", "BATCH_SLEEP_S",
    "EXCLUDED_TABLES", "FULL_SYNC_TABLES", "LOCK_ID", "RECONCILE_ORDER",
    "SYNC_MODE", "_ENTITY_TAXONOMY_TABLES", "_EVENT_TABLES",
    "_mask_url", "_resolve_dev_url", "_resolve_prod_url",
    "_column_intersection", "_ensure_entity_taxonomy", "_ensure_event_tables",
    "_ensure_updated_at_on_prod", "_pk_cols", "_quoted_cols", "_table_has_updated_at",
    "_ensure_sync_meta_table", "_get_last_sync", "_set_last_sync",
    "_cleanup_multi_column_unique", "_cleanup_secondary_conflicts",
    "_cleanup_single_column_unique", "_detect_secondary_uniques",
    "_reconcile", "_reconcile_table", "_upsert_table",
    "_sync_status", "_validate", "main",
    "_bootstrap_prod_schema",
)


class EngineSpy:
    """Records engine construction; resolution must never trigger it."""

    def __init__(self) -> None:
        self.calls: list = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("resolution must not construct an engine")


# ── module shape ────────────────────────────────────────────────────────


def test_every_sync_module_is_under_500_lines():
    oversize = {}
    for name in SYNC_MODULES:
        path = REPO_ROOT / "scripts" / "db" / f"{name}.py"
        count = len(path.read_text().splitlines())
        if count >= 500:
            oversize[name] = count
    assert oversize == {}, f"modules at or over 500 lines: {oversize}"


def test_sync_prod_is_a_thin_facade():
    path = REPO_ROOT / "scripts" / "db" / "sync_prod.py"
    assert len(path.read_text().splitlines()) < 325


def test_extracted_modules_are_documented():
    for name in SYNC_MODULES:
        source = (REPO_ROOT / "scripts" / "db" / f"{name}.py").read_text()
        assert source.lstrip().startswith("#!") or source.lstrip().startswith('"""')
        assert '"""' in source.split("\n\n")[0] or '"""' in source[:400], name


# ── facade compatibility ────────────────────────────────────────────────


def test_facade_exposes_every_historical_name():
    missing = [n for n in FACADE_NAMES if not hasattr(sync_prod, n)]
    assert missing == []


def test_facade_reexports_are_the_same_objects():
    """Re-export must be identity, not a copy: patches must still take effect."""
    assert sync_prod.ALL_SYNC_TABLES is sync_declarations.ALL_SYNC_TABLES
    assert sync_prod.RECONCILE_ORDER is sync_declarations.RECONCILE_ORDER
    assert sync_prod._column_intersection is sync_schema._column_intersection
    assert sync_prod._upsert_table is sync_upsert._upsert_table
    assert sync_prod._reconcile is sync_reconcile._reconcile
    assert sync_prod._validate is sync_validate._validate
    assert sync_prod._sync_status is sync_validate._sync_status
    assert sync_prod._resolve_dev_url is sync_targets._resolve_dev_url
    assert sync_prod._detect_secondary_uniques is sync_uniques._detect_secondary_uniques
    assert sync_prod._get_last_sync is sync_meta._get_last_sync


@pytest.mark.parametrize("name", ["create_engine", "sa_inspect", "text",
                                  "Engine", "Connection", "log", "time", "json"])
def test_facade_keeps_its_module_level_dependencies(name):
    """Tests patch e.g. ``sync_prod.create_engine``; those names must still exist."""
    assert hasattr(sync_prod, name)


# ── declarations: membership and ordering ───────────────────────────────


def test_all_sync_tables_membership_is_unchanged():
    assert len(sync_prod.ALL_SYNC_TABLES) == 24
    for table in ("meetings", "public_bodies", "jurisdictions", "agenda_items",
                  "supporting_documents", "entity_mentions", "meeting_events",
                  "entity_types", "event_participants"):
        assert table in sync_prod.ALL_SYNC_TABLES


def test_parent_tables_are_synced_before_their_dependants():
    order = sync_prod.ALL_SYNC_TABLES
    assert order.index("public_bodies") < order.index("meetings")
    assert order.index("jurisdictions") < order.index("meetings")


def test_reconcile_order_membership_is_unchanged():
    assert len(sync_prod.RECONCILE_ORDER) == 23


def test_constants_are_unchanged():
    assert sync_prod.LOCK_ID == 184_729_583
    assert sync_prod.BATCH_SIZE == 2000
    assert sync_prod.BATCH_SLEEP_S == pytest.approx(0.1)
    assert sync_prod.SYNC_MODE == "incremental"


def test_table_sets_and_exclusions_are_unchanged():
    """DELIBERATELY revised in Stage B (owner decision 2).

    `public_bodies` and `jurisdictions` are reference tables and are now
    full-reference for transfer. Under an incremental `updated_at > checkpoint`
    filter, a dev parent absent on the target whose timestamp predates the
    checkpoint is never re-selected — which is exactly how production ended up
    with meetings whose public_bodies parent was missing. This test exists to make
    sync-scope changes deliberate rather than silent; this change is deliberate.
    """
    assert sorted(sync_prod.FULL_SYNC_TABLES) == [
        "entity_types", "event_participants", "jurisdictions",
        "meeting_event_extractions", "meeting_event_types", "meeting_events",
        "public_bodies",
    ]
    assert "_EVENT_TABLES" and sync_prod._ENTITY_TAXONOMY_TABLES == {"entity_types"}
    assert "articles" in sync_prod.EXCLUDED_TABLES
    assert sync_prod.AUTO_COLUMNS == {"supporting_documents": {"search_vector"}}


# ── CLI surface ─────────────────────────────────────────────────────────


def test_cli_help_smoke_lists_every_flag():
    proc = subprocess.run(
        [sys.executable, "scripts/db/sync_prod.py", "--help"],
        capture_output=True, text=True, cwd=str(REPO_ROOT),
    )
    assert proc.returncode == 0
    for flag in ("--schema-only", "--bootstrap-schema", "--status", "--reconcile",
                 "--reconcile-only", "--reconcile-dry-run"):
        assert flag in proc.stdout, flag


# ── monkeypatch seams ───────────────────────────────────────────────────


def test_url_resolution_is_reachable_on_the_facade(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:***@db.ondigitalocean.com:25060/poliscopic")
    with pytest.raises(SystemExit):
        sync_prod._resolve_dev_url()


def test_url_resolution_never_constructs_an_engine(monkeypatch):
    """The seam the tier-entrypoint tests depend on."""
    spy = EngineSpy()
    monkeypatch.setattr(sync_prod, "create_engine", spy)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("PROD_DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        sync_prod._resolve_dev_url()
    with pytest.raises(SystemExit):
        sync_prod._resolve_prod_url()
    assert spy.calls == []


def test_resolution_logs_through_the_shared_logger():
    assert sync_targets.log.name == "sync"
    assert sync_schema.log.name == "sync"
    assert sync_prod.log.name == "sync"


# ── parentage contract still reached through the facade ─────────────────


def _sqlite_engine(*statements: str):
    from sqlalchemy import create_engine, text
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        for statement in statements:
            conn.execute(text(statement))
    return engine


def test_parentage_guard_still_fires_through_the_facade():
    dev = _sqlite_engine(
        "CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT,"
        " public_body_id INTEGER, jurisdiction_id INTEGER)")
    prod = _sqlite_engine("CREATE TABLE meetings (id INTEGER PRIMARY KEY, body TEXT)")
    with pytest.raises(RuntimeError) as exc:
        sync_prod._column_intersection(dev, prod, "meetings")
    assert "public_body_id" in str(exc.value)


def test_column_intersection_still_returns_a_plain_intersection():
    dev = _sqlite_engine("CREATE TABLE cases (id INTEGER PRIMARY KEY, a TEXT, dev_only TEXT)")
    prod = _sqlite_engine("CREATE TABLE cases (id INTEGER PRIMARY KEY, a TEXT, prod_only TEXT)")
    assert sync_prod._column_intersection(dev, prod, "cases") == ["a", "id"]


# ── default sync safety: no bootstrap and one lock session ───────────────


class _Scalar:
    def __init__(self, value):
        self.value = value

    def scalar(self):
        return self.value


class _LockConnection:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.queries = []
        self.closed = False

    def execute(self, statement):
        query = str(statement)
        self.queries.append(query)
        return _Scalar(self.acquired if "pg_try_advisory_lock" in query else True)

    def close(self):
        self.closed = True


class _ProdEngine:
    def __init__(self, connection):
        self.connection = connection

    def connect(self):
        return self.connection


def _wire_runtime(monkeypatch):
    """Wire the INTERNAL runtime with mock engines.

    The sync mechanism is exercised directly through ``sync_runtime.run_sync``,
    which takes explicit engines. That preserves real coverage of bootstrap and
    lock-lifetime behaviour WITHOUT going through the guarded entry point and
    without bypassing it: the facade's own interlock behaviour is asserted
    separately below.
    """
    lock = _LockConnection()
    prod = _ProdEngine(lock)
    monkeypatch.setattr(sync_runtime, "ALL_SYNC_TABLES", ())
    monkeypatch.setattr(sync_runtime, "assert_parity", lambda: None)
    monkeypatch.setattr(sync_runtime, "dangling_counts", lambda _engine: {})
    # These tests pin the lock/bootstrap contract using mock engines. The reference
    # postcondition step needs introspectable engines, so it is stubbed here and
    # exercised separately against real SQLite engines in
    # tests/test_sync_reference_runtime.py.
    monkeypatch.setattr(sync_runtime, "_reference_postconditions", lambda *_a, **_k: [])
    return lock, object(), prod


def test_default_sync_skips_schema_bootstrap_and_holds_one_lock_session(monkeypatch):
    lock, dev, prod = _wire_runtime(monkeypatch)
    monkeypatch.setattr(
        sync_runtime, "_bootstrap_prod_schema",
        lambda _engine: pytest.fail("default data-only sync must not bootstrap schema"),
    )

    def validate(_dev, _prod):
        assert lock.closed is False
        assert len(lock.queries) == 1
        assert "pg_try_advisory_lock" in lock.queries[0]
        return True

    monkeypatch.setattr(sync_runtime, "_validate", validate)
    assert sync_runtime.run_sync(dev, prod) == 0
    assert len(lock.queries) == 2
    assert "pg_advisory_unlock" in lock.queries[1]
    assert lock.closed is True


def test_schema_bootstrap_requires_an_explicit_flag(monkeypatch):
    _lock, dev, prod = _wire_runtime(monkeypatch)
    calls = []
    monkeypatch.setattr(sync_runtime, "_bootstrap_prod_schema",
                        lambda engine: calls.append(engine))
    monkeypatch.setattr(sync_runtime, "_validate", lambda _dev, _prod: True)
    assert sync_runtime.run_sync(dev, prod, bootstrap_schema=True) == 0
    assert len(calls) == 1


# ── the guarded entry point refuses BEFORE the mechanism runs ──────────


def test_guarded_entry_point_refuses_before_invoking_the_runtime(monkeypatch):
    """The facade must refuse at the interlock before URL resolution or the
    mechanism, for every MUTATING mode. Patched to explode if reached."""
    def boom(*_a, **_k):
        pytest.fail("must not be reached: the interlock must refuse first")

    monkeypatch.setattr(sync_prod, "run_sync", boom)
    monkeypatch.setattr(sync_prod, "_resolve_dev_url", boom)
    monkeypatch.setattr(sync_prod, "_resolve_prod_url", boom)
    monkeypatch.setattr(sync_prod, "create_engine", boom)
    assert sync_prod.main() == 3
    assert sync_prod.main(reconcile=True) == 3
    assert sync_prod.main(reconcile_only=True) == 3
    assert sync_prod.main(bootstrap_schema=True) == 3
    assert sync_prod.main(schema_only=True) == 3


def test_dry_run_is_read_only_and_is_allowed_to_reach_the_runtime(monkeypatch):
    """--reconcile-dry-run is classified OP-STATUS (read-only) and is permitted
    to proceed; the interlock does not gate read-only modes. This pins that the
    mutating modes are refused while the read-only mode still works."""
    reached = []
    monkeypatch.setattr(sync_prod, "_resolve_dev_url", lambda: "dev-url")
    monkeypatch.setattr(sync_prod, "_resolve_prod_url", lambda: "prod-url")
    monkeypatch.setattr(sync_prod, "create_engine", lambda *_a, **_k: object())
    monkeypatch.setattr(sync_prod, "run_sync",
                        lambda *_a, **_k: reached.append(True) or 0)
    assert sync_prod.main(reconcile_dry_run=True) == 0
    assert reached == [True], "the read-only dry run should reach the runtime"


def test_entry_point_refusal_is_fail_closed_when_the_interlock_is_missing(monkeypatch):
    """If the interlock cannot be loaded, the facade refuses rather than proceeding."""
    import builtins

    real_import = builtins.__import__

    def deny(name, *args, **kwargs):
        if name == "production_interlock":
            raise ImportError("simulated missing interlock")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny)
    monkeypatch.setattr(sync_prod, "run_sync",
                        lambda *_a, **_k: pytest.fail("must not run without an interlock"))
    assert sync_prod.main() == 3


def test_runtime_has_no_cli_or_url_resolution():
    """The internal runtime must not resolve URLs, build engines, or be a CLI.

    Checked against the module's CODE, not its prose: the docstring deliberately
    explains what the module does NOT contain, so a raw text search produces false
    positives (the same prose-vs-code trap hit in earlier batches).
    """
    import ast as _ast

    path = REPO_ROOT / "scripts" / "db" / "sync_runtime.py"
    tree = _ast.parse(path.read_text())
    # drop the module docstring before scanning
    if (tree.body and isinstance(tree.body[0], _ast.Expr)
            and isinstance(tree.body[0].value, _ast.Constant)
            and isinstance(tree.body[0].value.value, str)):
        del tree.body[0]
    code = _ast.unparse(tree)

    for token in ("__main__", "argparse", "create_engine", "_resolve_dev_url",
                  "_resolve_prod_url", "PROD_DATABASE_URL", "DATABASE_URL",
                  "production_interlock"):
        assert token not in code, f"runtime code must not contain {token!r}"


def test_runtime_has_no_production_defaults_and_requires_engines():
    import inspect as _inspect

    signature = _inspect.signature(sync_runtime.run_sync)
    params = list(signature.parameters)
    assert params[:2] == ["dev_engine", "prod_engine"], (
        "run_sync must take explicit engines as its first two parameters"
    )
    for name in ("dev_engine", "prod_engine"):
        assert signature.parameters[name].default is _inspect.Parameter.empty, (
            f"{name} must not have a default"
        )
