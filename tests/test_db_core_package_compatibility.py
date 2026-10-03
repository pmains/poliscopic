"""Identity gates for the packaged ORM and engine/session boundary."""

from __future__ import annotations

import importlib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_legacy_and_canonical_model_imports_share_one_registry():
    legacy = importlib.import_module("db.models")
    canonical = importlib.import_module("poliscopic.db.models")

    assert legacy is canonical
    assert legacy.Base is canonical.Base
    assert legacy.Base.metadata.tables is canonical.Base.metadata.tables
    assert Path(canonical.__file__).resolve() == (
        ROOT / "src" / "poliscopic" / "db" / "models.py"
    )


def test_legacy_and_canonical_core_imports_share_engine_state():
    legacy = importlib.import_module("db.core")
    canonical = importlib.import_module("poliscopic.db.core")

    assert legacy is canonical
    assert legacy.get_engine is canonical.get_engine
    assert legacy.get_session is canonical.get_session
    assert legacy.session_scope is canonical.session_scope
    assert legacy.transaction_scope is canonical.transaction_scope
    assert Path(canonical.__file__).resolve() == (
        ROOT / "src" / "poliscopic" / "db" / "core.py"
    )


def test_legacy_leaf_modules_are_canonical_aliases():
    for name in (
        "helper",
        "meeting_members",
        "pz_details",
        "sql_read_only_guard",
        "sync_targets",
        "votes",
    ):
        legacy = importlib.import_module(f"db.{name}")
        canonical = importlib.import_module(f"poliscopic.db.{name}")

        assert legacy is canonical
        assert Path(canonical.__file__).resolve() == (
            ROOT / "src" / "poliscopic" / "db" / f"{name}.py"
        )
