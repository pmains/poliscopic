"""Every standalone production writer must hit the central interlock first."""

import ast
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRITERS = {
    "scripts/db/cleanup_prod_db.py": "OP-SCHEMA",
    "scripts/db/migrate_prod_db.py": "OP-SCHEMA",
    "scripts/editorial_sync.py": "OP-RECON",
    "scripts/remove_newsletter_articles.py": "OP-REPAIR",
    "scripts/body_code_merge_prod.py": "OP-REPAIR",
    "scripts/backfill_empty_body.py": "OP-REPAIR",
    "scripts/db/backfill_supporting_documents.py": "OP-RECON",
}
MAIN_SENSITIVE_CALL = {
    "scripts/db/cleanup_prod_db.py": "_resolve_prod_url",
    "scripts/db/migrate_prod_db.py": "_resolve_prod_url",
    "scripts/editorial_sync.py": "create_engine",
    "scripts/remove_newsletter_articles.py": "cleanup",
    "scripts/body_code_merge_prod.py": "mode_apply",
    "scripts/backfill_empty_body.py": "_resolve_prod_url",
    "scripts/db/backfill_supporting_documents.py": "create_engine",
}


def test_production_writers_call_central_interlock():
    for rel, operation in WRITERS.items():
        source = (ROOT / rel).read_text()
        tree = ast.parse(source)
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "require_production_interlock"
        ]
        assert calls, f"{rel} has no central production interlock call"
        assert operation in source, (
            f"{rel} does not classify its production write as {operation}")


def test_production_guards_precede_connection_or_write_dispatch():
    for rel, sensitive_name in MAIN_SENSITIVE_CALL.items():
        tree = ast.parse((ROOT / rel).read_text())
        main = next(
            node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "main"
        )
        calls = [node for node in ast.walk(main) if isinstance(node, ast.Call)]
        guard_lines = [
            node.lineno for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == "require_production_interlock"
        ]
        sensitive_lines = [
            node.lineno for node in calls
            if isinstance(node.func, ast.Name)
            and node.func.id == sensitive_name
        ]
        assert guard_lines and sensitive_lines, rel
        assert min(guard_lines) < min(sensitive_lines), (
            f"{rel} can reach {sensitive_name} before the production interlock")


def test_interlock_guard_fails_closed_on_unavailable_authority(monkeypatch):
    from scripts.ops import production_interlock_guard as guard

    monkeypatch.setitem(__import__("sys").modules, "production_interlock", None)
    try:
        guard.require_production_interlock("OP-REPAIR", "test")
    except SystemExit as exc:
        assert exc.code == 3
    else:
        raise AssertionError("missing interlock authority did not refuse")


def test_web_release_contains_newsletter_route_dependencies():
    from scripts.ops import build_release_manifest as manifest

    files, excluded = manifest.resolve(manifest.ENTRIES)
    assert "routes/newsletter.py" in files
    assert "scripts/newsletter_svc.py" in files
    assert "scripts/newsletter_mail.py" in files
    assert "scripts/newsletter_images.py" in files
    assert "scripts/newsletter_svc.py" not in excluded
    assert "scripts/newsletter_mail.py" not in excluded
    for asset in manifest.ENTRY_WEB_ASSETS:
        assert asset in files
        assert asset not in excluded


def test_release_contains_canonical_package_and_registry_adapters():
    from scripts.ops import build_release_manifest as manifest

    adapters = manifest.registry_adapter_entries()
    files, excluded = manifest.resolve(manifest.ENTRIES + adapters)

    assert "src/poliscopic/db/core.py" in files
    assert "src/poliscopic/db/repositories/public_bodies.py" in files
    assert "scripts/scraper/jurisdictions/tolleson.py" in adapters
    assert "scripts/scraper/jurisdictions/el_mirage_adapter.py" in adapters
    assert not (set(adapters) & set(excluded))
