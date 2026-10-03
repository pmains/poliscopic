"""Compatibility gate for the first bounded ``src/`` package migration."""

from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_and_canonical_tier_imports_share_one_module():
    legacy = importlib.import_module("db.tier")
    canonical = importlib.import_module("poliscopic.db.tier")

    assert legacy is canonical
    assert Path(canonical.__file__).resolve() == (
        ROOT / "src" / "poliscopic" / "db" / "tier.py"
    )


def test_migrated_tier_module_keeps_repository_dotenv_location():
    tier = importlib.import_module("poliscopic.db.tier")

    assert tier._default_dotenv_path() == ROOT / ".env"


def test_package_discovery_installs_only_canonical_source_tree():
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    assert 'where = ["src"]' in pyproject
    assert 'include = ["poliscopic*"]' in pyproject
    assert 'package = true' in pyproject
