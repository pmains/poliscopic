#!/usr/bin/env python3
"""Build an immutable, machine-readable production release manifest.

Brief 032 release preparation, task A.

The release must contain the dependency-complete code for exactly three things:

  1. Brief 032 dropped-column-churn prevention;
  2. the reviewed Chandler/Mesa canonical body-code merge behaviour and the
     naming-convention correction, as used by the morning scrape/sync;
  3. the production data-reconciliation path.

`scripts/db/migrations.py` alone is NOT sufficient — that assumption is what
this tool exists to falsify.  It resolves the transitive import closure from the
declared entry points, then applies explicit exclusions.

Output: JSON manifest (repo-relative paths + SHA-256 + mode + size) and the
space-separated POLISCOPIC_DEPLOY_PATHS string for deploy_release.sh.

Read-only: it hashes files and writes only its own output.  It deploys nothing.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# ── entry points ────────────────────────────────────────────────────────
# 1. churn prevention
ENTRY_CHURN = ["scripts/db/migrations.py"]
# 2. canonical body-code merge + naming correction (morning scrape/sync)
ENTRY_MERGE = [
    "scripts/migrate_body_codes.py",
    "scripts/scraper/main.py",
    "scripts/db/names.py",
]
# 3. reconciliation.  The reference-integrity gate ships with it: sync is what
#    must refuse to move a child row whose parent registry row is absent, and
#    the gate is what proves it (Brief 033 §5).
ENTRY_RECONCILE = [
    "scripts/db/sync_prod.py",
    "scripts/ops/reference_integrity_gate.py",
]

# 4. the web application.  Without this the closure never reaches `routes/`,
#    because the app is entered through app.py, not through the scraper or sync
#    path — so the manifest silently omitted every route change.  That gap was
#    found while preparing the 2026-09-18 body-registry release, whose front-page
#    and meeting-route changes fell outside the closure.
#
#    Only the web app belongs here.  The scrape and entity pipelines run on the
#    development host (the 03:00 cron is local), so their modules — role_classifier,
#    tempe/drc_summary, workflows/templates/render — are NOT part of what
#    production executes and must not drag their (largely excluded) dependency
#    trees into the release.  Adding them produced a release with unresolved
#    local imports; removing them makes the closure self-consistent.
ENTRY_WEB = [
    "app.py",
]

# Templates are runtime dependencies that Python's AST import closure cannot
# discover.  Keep this list explicit: the release is an allowlist, not a copy of
# the ambient templates directory.
ENTRY_WEB_ASSETS = [
    "templates/admin/_nav.html",
    "templates/admin/subscribers.html",
    "templates/article.html",
    "templates/base.html",
    "templates/front_page.html",
    "templates/home.html",
    "templates/kg_quality_review.html",
    "templates/newsletter/_subscribe_widget.html",
    "templates/newsletter/confirm.html",
    "templates/newsletter/landing.html",
    "templates/newsletter/manage.html",
    "templates/newsletter/success.html",
    "templates/newsletter/unsubscribed.html",
    "templates/privacy.html",
    "templates/terms.html",
]

ENTRIES = (ENTRY_CHURN + ENTRY_MERGE + ENTRY_RECONCILE + ENTRY_WEB
           + ENTRY_WEB_ASSETS)

# ── exclusions ──────────────────────────────────────────────────────────
# Explicitly out of scope for this release.  Anything caught here is reported,
# never silently dropped, so the exclusion is auditable.
EXCLUDE_PREFIXES = (
    "tests/",
    "scripts/kg/",          # KG experimental / apply code
    "docs/",
    "notebooks/",
    "contracts/",
    "data/",
)

# Narrow exceptions to the prefix rules above.  The reconciliation path imports
# `kg.stage2_parentage_contract` at MODULE level (with a `scripts.kg` fallback),
# so excluding the whole scripts/kg/ tree would ship a release whose
# sync_prod/sync_schema cannot be imported at all.  That file is stdlib-only and
# self-contained, so exactly it (plus the package marker) is included.
EXCLUDE_PREFIX_EXCEPTIONS = {
    "scripts/kg/__init__.py",
    "scripts/kg/stage2_parentage_contract.py",
}
EXCLUDE_EXACT = {
    # recovery proof / preflight utilities: dev-side tooling, not runtime
    "scripts/db/attnum_recovery_proof.sh",
    "scripts/db/migrate_col_preflight.py",
    "scripts/ops/build_release_manifest.py",
    # apply runners for a merge that has not been executed
    "scripts/body_code_merge_prod.py",
    "scripts/body_code_merge_runtime.py",
    "scripts/body_code_merge_backup.py",
    "scripts/body_code_merge.py",
    # Newsletter publication/maintenance commands are not web dependencies.
    "scripts/publish_newsletter_article.py",
    "scripts/remove_newsletter_articles.py",
    "scripts/editorial_sync.py",
    "scripts/backfill_article_links.py",
    "scripts/send_plain_email.py",
    "scripts/email_article.py",
}
EXCLUDE_SUFFIXES = (".pyc", ".log", ".sqlite", ".sqlite3", ".ipynb")

# Module search roots, in the order the app resolves them. The canonical
# installable package must be considered before its flat compatibility shims.
MODULE_ROOTS = (REPO_ROOT / "src", REPO_ROOT / "scripts", REPO_ROOT)


def module_name_for(path: Path) -> str:
    """Canonical dotted module name for a project file, or '' if N/A."""
    for root in MODULE_ROOTS:
        try:
            sub = path.relative_to(root)
        except ValueError:
            continue
        if sub.suffix != ".py":
            return ""
        parts = list(sub.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)
    return ""


def local_modules() -> dict[str, Path]:
    """Every importable project module -> file path."""
    out: dict[str, Path] = {}
    for root in MODULE_ROOTS:
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            if any(part in {"__pycache__", ".venv", "venv", ".git",
                            ".staging", ".releases"} for part in path.parts):
                continue
            name = module_name_for(path)
            if name:
                out.setdefault(name, path)
    return out


def registry_adapter_entries() -> list[str]:
    """Return dynamically declared scraper adapters as release dependencies.

    Adapter modules are strings in the typed source registry, so Python's AST
    import walk cannot discover them. Parse only that declarative keyword and
    resolve it through the same local-module catalog used by the manifest.
    """
    registry = REPO_ROOT / "scripts" / "scraper" / "source_registry.py"
    tree = ast.parse(registry.read_text(encoding="utf-8"))
    modules = local_modules()
    entries: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.keyword) or node.arg != "adapter_module":
            continue
        if (
            not isinstance(node.value, ast.Constant)
            or not isinstance(node.value.value, str)
        ):
            raise RuntimeError("source registry adapter_module must be a string literal")
        target = modules.get(node.value.value)
        if target is None:
            raise RuntimeError(f"source registry adapter is not importable: {node.value.value}")
        entries.add(str(target.relative_to(REPO_ROOT)))
    return sorted(entries)


def imports_of(path: Path) -> set[str]:
    """Dotted module names imported by `path` (AST, so comments don't count)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"WARN: cannot parse {path}: {exc}", file=sys.stderr)
        return set()
    found: set[str] = set()
    current = module_name_for(path)
    package = current if path.name == "__init__.py" else current.rpartition(".")[0]
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if node.level:
                parts = package.split(".") if package else []
                ascend = node.level - 1
                if ascend > len(parts):
                    continue
                prefix = parts[: len(parts) - ascend]
                if module:
                    prefix.extend(module.split("."))
                module = ".".join(prefix)
            if module:
                found.add(module)
                for alias in node.names:
                    found.add(f"{module}.{alias.name}")
    return found


def is_excluded(rel: str) -> tuple[bool, str]:
    if rel in EXCLUDE_PREFIX_EXCEPTIONS:
        return False, ""
    if rel in EXCLUDE_EXACT:
        return True, "explicitly excluded"
    for prefix in EXCLUDE_PREFIXES:
        if rel.startswith(prefix):
            return True, f"excluded prefix {prefix!r}"
    if rel.endswith(EXCLUDE_SUFFIXES):
        return True, "excluded suffix"
    return False, ""


def resolve(entries: list[str]) -> tuple[list[str], dict[str, str]]:
    """Transitive closure of project files reachable from `entries`."""
    modules = local_modules()
    by_rel = {str(p.relative_to(REPO_ROOT)): p for p in modules.values()}

    seen: set[str] = set()
    excluded: dict[str, str] = {}
    queue = list(entries)

    while queue:
        rel = queue.pop()
        if rel in seen:
            continue
        path = by_rel.get(rel)
        if path is None:
            path = REPO_ROOT / rel
            if not path.is_file():
                excluded.setdefault(rel, "entry point not found")
                continue
        skip, why = is_excluded(rel)
        if skip:
            excluded[rel] = why
            continue
        seen.add(rel)

        for name in imports_of(path) if path.suffix == ".py" else ():
            # try the name and each dotted prefix for a project-local file
            parts = name.split(".")
            for i in range(len(parts), 0, -1):
                candidate = ".".join(parts[:i])
                target = modules.get(candidate)
                if target is not None:
                    queue.append(str(target.relative_to(REPO_ROOT)))
                    break
    return sorted(seen), excluded


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def reference_preconditions() -> tuple[list[str], dict]:
    """Evaluate the sync-ordering invariant the release depends on.

    Brief 033 §5: a release can be dependency-complete and still ship a defect.
    The manifest must therefore assert *behaviour*, not only hash files.
    """
    for root in MODULE_ROOTS:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
    from ops.reference_integrity_gate import REQUIRED_ORDER, reference_order_problems
    from db.sync_declarations import ALL_SYNC_TABLES

    problems = reference_order_problems(ALL_SYNC_TABLES)
    detail = {
        "required_order": [list(pair) for pair in REQUIRED_ORDER],
        "sync_table_count": len(ALL_SYNC_TABLES),
        "ordering_ok": not problems,
        "problems": problems,
        "zero_dangling_precondition": (
            "enforced at preflight by reference_integrity_gate: no canonical "
            "meeting code may be synced without its public_bodies row"
        ),
        "sentinel_policy": (
            "__skip__ and empty body codes are refused/quarantined, never "
            "promoted into public_bodies"
        ),
    }
    return problems, detail


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="data/bridge/release-manifest.json")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    adapter_entries = registry_adapter_entries()
    files, excluded = resolve(ENTRIES + adapter_entries)

    precondition_problems, preconditions = reference_preconditions()
    if precondition_problems:
        for problem in precondition_problems:
            print(f"REFUSE: {problem}", file=sys.stderr)
        print(
            "\nrefusing to build a release manifest: sync-ordering invariant "
            "violated (Brief 033 §5)",
            file=sys.stderr,
        )
        return 1

    records = []
    for rel in files:
        path = REPO_ROOT / rel
        stat = path.stat()
        records.append({
            "path": rel,
            "sha256": sha256(path),
            "bytes": stat.st_size,
            "mode": oct(stat.st_mode & 0o777),
        })

    manifest = {
        "kind": "poliscopic-release-manifest",
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "entry_points": {
            "churn_prevention": ENTRY_CHURN,
            "body_code_merge_and_naming": ENTRY_MERGE,
            "data_reconciliation": ENTRY_RECONCILE,
            "web_application": ENTRY_WEB,
            "web_assets": ENTRY_WEB_ASSETS,
            "source_registry_adapters": adapter_entries,
        },
        "files": records,
        "file_count": len(records),
        "preconditions": preconditions,
        "excluded": {k: excluded[k] for k in sorted(excluded)},
    }

    body = json.dumps(manifest, indent=2, sort_keys=True)
    manifest["manifest_digest"] = hashlib.sha256(body.encode()).hexdigest()

    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                        encoding="utf-8")
    os.chmod(out_path, 0o600)

    deploy_paths = " ".join(r["path"] for r in records)
    (out_path.with_suffix(".paths")).write_text(deploy_paths + "\n",
                                                encoding="utf-8")
    os.chmod(out_path.with_suffix(".paths"), 0o600)

    if not args.quiet:
        print(f"manifest : {out_path}")
        print(f"digest   : {manifest['manifest_digest']}")
        print(f"files    : {len(records)}")
        print(f"excluded : {len(manifest['excluded'])}")
        print()
        for r in records:
            print(f"  {r['mode']}  {r['sha256'][:12]}  {r['path']}")
        print()
        print("POLISCOPIC_DEPLOY_PATHS (see .paths file)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
