"""Compatibility gates for application-root discovery and ``db.config``."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from poliscopic.paths import (
    PROJECT_ROOT_ENV,
    ProjectRootError,
    application_dotenv_path,
    application_root,
)


ROOT = Path(__file__).resolve().parents[1]


def test_application_root_finds_source_checkout():
    assert application_root(environ={}, cwd=ROOT.parent) == ROOT
    assert application_dotenv_path(environ={}, cwd=ROOT.parent) == ROOT / ".env"


def test_explicit_application_root_has_precedence(tmp_path):
    assert application_root(
        environ={PROJECT_ROOT_ENV: str(tmp_path)},
        cwd=ROOT,
    ) == tmp_path.resolve()


def test_invalid_explicit_application_root_fails_closed(tmp_path):
    missing = tmp_path / "missing"
    with pytest.raises(ProjectRootError, match=PROJECT_ROOT_ENV):
        application_root(environ={PROJECT_ROOT_ENV: str(missing)}, cwd=ROOT)


def test_installed_shape_outside_checkout_does_not_search_for_dotenv(tmp_path):
    fake_module = tmp_path / "lib" / "python" / "site-packages" / "poliscopic" / "paths.py"
    fake_module.parent.mkdir(parents=True)
    fake_module.touch()
    elsewhere = tmp_path / "work"
    elsewhere.mkdir()

    assert application_root(environ={}, cwd=elsewhere, source_file=fake_module) is None
    assert (
        application_dotenv_path(environ={}, cwd=elsewhere, source_file=fake_module)
        is None
    )


def test_legacy_and_canonical_config_imports_share_one_module():
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": str(ROOT / "scripts"),
            "POLISCOPIC_DB_TIER": "test",
            "DATABASE_URL": "sqlite:///:memory:",
            PROJECT_ROOT_ENV: str(ROOT),
        }
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import db.config as legacy; "
                "import poliscopic.db.config as canonical; "
                "assert legacy is canonical; "
                "assert canonical.DATABASE_URL == 'sqlite:///:memory:'"
            ),
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
