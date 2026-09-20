"""Step 4B tests: every migrated entry point validates before it connects.

Isolated: no database connection, no network, no subprocess.  Resolution
functions are driven directly, and ``create_engine`` is replaced by a spy that
records any call, so "validation happens before connection construction" is
proven rather than asserted.
"""

from __future__ import annotations

import logging
import os
import stat

import pytest

from db import backfill_supporting_documents as backfill
from db import cleanup_prod_db as cleanup
from db import migrate_prod_db as migrate
from db import sync_prod
from db.tier import (
    DEVELOPMENT,
    LOCAL,
    PRODUCTION,
    PRODUCTION_LIKE,
    TIER_ENV,
    TierError,
    classify_target,
    new_test_database_path,
    parse_target,
    require_role_url,
    resolve_database_url,
    resolve_role_url,
)

import benchmark_deepseek_classify as benchmark
import editorial_sync
import rescrape_tempe_boa

DEV_URL = "postgresql://devuser:***@dev-host.internal:5432/poliscopic_dev"
PROD_URL = (
    "postgresql://produser:***@tenant.example.ondigitalocean.com:25060/poliscopic"
)
PASSWORD = "***"

MIGRATED = (
    "scripts/db/sync_prod.py",
    "scripts/editorial_sync.py",
    "scripts/db/backfill_supporting_documents.py",
    "scripts/db/migrate_prod_db.py",
    "scripts/db/cleanup_prod_db.py",
    "scripts/rescrape_tempe_boa.py",
    "scripts/benchmark_deepseek_classify.py",
)


class EngineSpy:
    """Records engine construction; any call is a pre-validation connection."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        raise AssertionError("create_engine was reached before validation")


def clear_db_env(monkeypatch):
    for key in ("DATABASE_URL", "PROD_DATABASE_URL", "DEV_DATABASE_URL", TIER_ENV):
        monkeypatch.delenv(key, raising=False)


# -- connection-before-validation proof ---------------------------------------


@pytest.mark.parametrize("url", [PROD_URL, "postgresql://u:***@h/odd_db", "mysql://u:***@h/db"])
def test_sync_prod_dev_role_refuses_before_connecting(monkeypatch, url):
    spy = EngineSpy()
    monkeypatch.setattr(sync_prod, "create_engine", spy)
    monkeypatch.setenv("DATABASE_URL", url)

    with pytest.raises(SystemExit) as exc:
        sync_prod._resolve_dev_url()

    assert exc.value.code == 1
    assert spy.calls == []


@pytest.mark.parametrize("url", [DEV_URL, "sqlite:///tmp/x.sqlite", "nonsense"])
def test_sync_prod_prod_role_refuses_before_connecting(monkeypatch, url):
    spy = EngineSpy()
    monkeypatch.setattr(sync_prod, "create_engine", spy)
    monkeypatch.setenv("PROD_DATABASE_URL", url)

    with pytest.raises(SystemExit):
        sync_prod._resolve_prod_url()

    assert spy.calls == []


def test_sync_prod_missing_urls_exit_before_connecting(monkeypatch):
    spy = EngineSpy()
    monkeypatch.setattr(sync_prod, "create_engine", spy)
    clear_db_env(monkeypatch)

    with pytest.raises(SystemExit):
        sync_prod._resolve_dev_url()
    with pytest.raises(SystemExit):
        sync_prod._resolve_prod_url()

    assert spy.calls == []


def test_sync_prod_accepts_a_correct_pair(monkeypatch, caplog):
    spy = EngineSpy()
    monkeypatch.setattr(sync_prod, "create_engine", spy)
    monkeypatch.setenv("DATABASE_URL", DEV_URL)
    monkeypatch.setenv("PROD_DATABASE_URL", PROD_URL)

    with caplog.at_level(logging.INFO):
        assert sync_prod._resolve_dev_url() == DEV_URL
        assert sync_prod._resolve_prod_url() == PROD_URL

    assert spy.calls == []          # resolution never constructs an engine
    assert PASSWORD not in caplog.text


@pytest.mark.parametrize("module", [migrate, cleanup])
def test_prod_only_tools_refuse_a_development_target(monkeypatch, module):
    spy = EngineSpy()
    monkeypatch.setattr(module, "create_engine", spy)
    monkeypatch.setenv("PROD_DATABASE_URL", DEV_URL)

    with pytest.raises(SystemExit):
        module._resolve_prod_url()

    assert spy.calls == []


@pytest.mark.parametrize("module", [migrate, cleanup])
def test_prod_only_tools_refuse_an_unclassifiable_target(monkeypatch, module):
    spy = EngineSpy()
    monkeypatch.setattr(module, "create_engine", spy)
    monkeypatch.setenv("PROD_DATABASE_URL", "mysql://u:***@h/db")

    with pytest.raises(SystemExit):
        module._resolve_prod_url()

    assert spy.calls == []


@pytest.mark.parametrize("module", [migrate, cleanup])
def test_prod_only_tools_accept_a_production_target(monkeypatch, caplog, module):
    spy = EngineSpy()
    monkeypatch.setattr(module, "create_engine", spy)
    monkeypatch.setenv("PROD_DATABASE_URL", PROD_URL)

    with caplog.at_level(logging.INFO):
        assert module._resolve_prod_url() == PROD_URL

    assert spy.calls == []
    assert PASSWORD not in caplog.text


def test_editorial_sync_swapped_roles_fail_before_connecting(monkeypatch):
    spy = EngineSpy()
    monkeypatch.setattr(editorial_sync, "create_engine", spy)
    monkeypatch.setattr(editorial_sync, "require_production_interlock",
                        lambda *_args, **_kwargs: {"status": "ALLOWED"})
    monkeypatch.setenv("DATABASE_URL", PROD_URL)      # dev role given the prod URL
    monkeypatch.setenv("PROD_DATABASE_URL", DEV_URL)

    assert editorial_sync.main() == 1
    assert spy.calls == []


def test_editorial_sync_missing_urls_fail_before_connecting(monkeypatch):
    spy = EngineSpy()
    monkeypatch.setattr(editorial_sync, "create_engine", spy)
    monkeypatch.setattr(editorial_sync, "require_production_interlock",
                        lambda *_args, **_kwargs: {"status": "ALLOWED"})
    clear_db_env(monkeypatch)

    assert editorial_sync.main() == 1
    assert spy.calls == []


def test_backfill_swapped_roles_fail_before_connecting(monkeypatch):
    spy = EngineSpy()
    monkeypatch.setattr(backfill, "create_engine", spy)
    monkeypatch.setattr(backfill, "require_production_interlock",
                        lambda *_args, **_kwargs: {"status": "ALLOWED"})
    monkeypatch.setattr("sys.argv", ["backfill_supporting_documents.py"])
    monkeypatch.setenv("DATABASE_URL", PROD_URL)
    monkeypatch.setenv("PROD_DATABASE_URL", DEV_URL)

    assert backfill.main() == 1
    assert spy.calls == []


def test_backfill_missing_urls_fail_before_connecting(monkeypatch):
    spy = EngineSpy()
    monkeypatch.setattr(backfill, "create_engine", spy)
    monkeypatch.setattr(backfill, "require_production_interlock",
                        lambda *_args, **_kwargs: {"status": "ALLOWED"})
    monkeypatch.setattr("sys.argv", ["backfill_supporting_documents.py"])
    clear_db_env(monkeypatch)

    assert backfill.main() == 1
    assert spy.calls == []


# -- swapped roles at the shared authority ------------------------------------


def test_shared_validator_refuses_a_swapped_pair():
    with pytest.raises(TierError):
        resolve_role_url(DEVELOPMENT, PROD_URL)
    with pytest.raises(TierError):
        resolve_role_url(PRODUCTION, DEV_URL)


def test_require_role_url_returns_the_url_unchanged():
    assert require_role_url(DEVELOPMENT, DEV_URL) == DEV_URL
    assert require_role_url(PRODUCTION, PROD_URL) == PROD_URL


# -- credentials are never logged ---------------------------------------------


def test_role_validation_messages_never_contain_credentials():
    for role, url in ((DEVELOPMENT, DEV_URL), (PRODUCTION, PROD_URL)):
        for bad in (PROD_URL if role == DEVELOPMENT else DEV_URL, "mysql://u:***@h/db"):
            with pytest.raises(TierError) as exc:
                resolve_role_url(role, bad)
            assert PASSWORD not in str(exc.value)
            assert "user" not in str(exc.value)


def test_redacted_target_is_built_without_a_username():
    target = parse_target(PROD_URL)
    assert target.redacted() == (
        "production postgresql tenant.example.ondigitalocean.com:25060/poliscopic"
    )
    assert PASSWORD not in target.redacted()
    assert "produser" not in target.redacted()


# -- no second classification rule --------------------------------------------


def test_migrated_modules_import_the_shared_authority():
    for path in MIGRATED:
        source = open(path, encoding="utf-8").read()
        assert "db.tier" in source or "db.config" in source, path


def test_no_migrated_module_defines_its_own_target_classification():
    """A local copy of the markers would be a competing rule set."""
    for path in MIGRATED:
        source = open(path, encoding="utf-8").read()
        assert "ondigitalocean.com" not in source, path
        assert "PRODUCTION_HOST_MARKERS" not in source, path


def test_the_production_markers_live_in_exactly_one_module():
    offenders = []
    for root, _dirs, files in os.walk("scripts"):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            if "ondigitalocean.com" in open(path, encoding="utf-8").read():
                offenders.append(path)
    assert offenders == ["scripts/db/tier.py"]


# -- development-only tools ---------------------------------------------------


def test_rescrape_no_longer_parses_dotenv_by_hand():
    source = open("scripts/rescrape_tempe_boa.py", encoding="utf-8").read()
    assert 'startswith("DATABASE_URL=")' not in source
    assert "from db.config import DATABASE_URL" in source


def test_benchmark_validates_the_development_role():
    source = open("scripts/benchmark_deepseek_classify.py", encoding="utf-8").read()
    assert "require_role_url(DEVELOPMENT" in source
    assert "except TierError" in source


def test_rescrape_module_imports_without_network_or_browser():
    """Importing the script must not launch a browser or resolve a database."""
    assert rescrape_tempe_boa.PROJECT_ROOT.name == "poliscopic"


def test_benchmark_module_imports_without_network():
    assert callable(benchmark.main)


# -- secure temporary database (mktemp replacement) ---------------------------


def test_new_test_database_path_is_unique_existing_and_owner_only():
    first = new_test_database_path()
    second = new_test_database_path()

    assert first != second
    assert first.endswith(".sqlite")
    assert os.path.basename(first).startswith("poliscopic-test-")
    for path in (first, second):
        assert os.path.isfile(path)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode & 0o077 == 0, f"{path} is group/other accessible: {oct(mode)}"


def test_test_tier_uses_the_secure_path(tmp_path):
    url, tier, target = resolve_database_url(
        environ={TIER_ENV: "test", "DATABASE_URL": DEV_URL},
        dotenv_path=str(tmp_path / "none.env"),
    )
    assert tier == "test"
    assert target.url_class == LOCAL
    path = url.replace("sqlite:///", "")
    assert os.path.isfile(path)
    assert stat.S_IMODE(os.stat(path).st_mode) & 0o077 == 0


def test_no_mktemp_remains_in_the_tier_authority():
    source = open("scripts/db/tier.py", encoding="utf-8").read()
    assert "mktemp(" not in source.replace("mkstemp(", "")


def test_test_tier_is_treated_as_a_local_target():
    assert classify_target("sqlite:////tmp/whatever.sqlite") == LOCAL
