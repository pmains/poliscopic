#!/usr/bin/env python3
"""``producer_manifest.py`` — authoritative producer fingerprints per phase.

A phase's manifest is the single declaration of which modules constitute its
executable behaviour.  It is deliberately **not** restricted to
``scripts/entities``: any qualified module in the repository may belong to a
manifest.  ``scripts.kg.quarantine`` is the motivating case — the event pipeline
executes it, but it lives outside the entities package, and omitting it made the
fingerprint blind to a real behaviour change.

Guarantees this module preserves:

* **Phase ownership** — each phase declares its own modules; the declaration is
  the authority for that phase and no other list is consulted.
* **Deterministic order** — a manifest that repeats a module or is not declared in
  sorted order is reported as an error instead of being hashed silently.
* **Legacy behaviour** — a phase with no ``code_modules`` manifests exactly its
  single ``module`` and keeps that module's historic direct-file hash.
* **Fail-visible evidence** — an unimportable module, an unavailable source file,
  or an unqualified path is recorded in ``code_module_errors`` and makes the
  aggregate unavailable; it is never silently omitted.

The completeness of a declaration is verifiable: :func:`undocumented_imports`
walks the real import graph, so a newly imported module that is missing from the
manifest is reported rather than trusted.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import re
from pathlib import Path

__all__ = [
    "PHASE_CODE_MODULES",
    "declared_modules",
    "import_closure",
    "is_qualified_module",
    "producer_metadata",
    "undocumented_imports",
]

#: A qualified dotted module path: two or more identifiers, no path syntax.
_QUALIFIED_MODULE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")

#: The authoritative per-phase producer manifests.
PHASE_CODE_MODULES: dict[str, tuple[str, ...]] = {
    "graph_builder": (
            "scripts.entities.entity_utils",
            "scripts.entities.graph_builder",
            "scripts.entities.graph_builder_materialization",
            "scripts.entities.graph_builder_models",
            "scripts.entities.graph_builder_runtime",
            "scripts.entities.graph_builder_sources",
            "scripts.entities.phase_receipt",
            "scripts.kg.emission",
            "scripts.kg.emission_bundles",
            "scripts.kg.emission_checks",
            "scripts.kg.emission_models",
            "scripts.kg.emission_receipts",
            "scripts.kg.emission_validation",
            "scripts.kg.identity",
            "scripts.kg.identity_assertions",
            "scripts.kg.identity_keys",
            "scripts.kg.producer_coverage",
            "scripts.kg.producer_versions",
            "scripts.kg.registries.evidence",
            "scripts.kg.registries.model",
            "scripts.kg.registries.roles",
    ),
    "sweep_docs": (
            "scripts.entities.sweep_docs",
            "scripts.entities.sweep_docs_batch",
            "scripts.entities.sweep_docs_extraction",
            "scripts.entities.sweep_docs_payloads",
            "scripts.entities.sweep_docs_planning",
            "scripts.entities.sweep_docs_storage",
            "scripts.kg.emission",
            "scripts.kg.emission_bundles",
            "scripts.kg.emission_checks",
            "scripts.kg.emission_models",
            "scripts.kg.emission_receipts",
            "scripts.kg.emission_validation",
            "scripts.kg.identity",
            "scripts.kg.identity_assertions",
            "scripts.kg.identity_keys",
            "scripts.kg.producer_coverage",
            "scripts.kg.producer_versions",
            "scripts.kg.registries.evidence",
            "scripts.kg.registries.model",
            "scripts.kg.registries.roles",
    ),
    "event_pipeline": (
            "scripts.entities.event_extract",
            "scripts.entities.event_extractor",
            "scripts.entities.event_link",
            "scripts.entities.event_link_storage",
            "scripts.entities.event_normalize",
            "scripts.entities.event_normalize_accounting",
            "scripts.entities.event_normalize_emission",
            "scripts.entities.event_normalize_models",
            "scripts.entities.event_normalize_planning",
            "scripts.entities.event_normalize_query",
            "scripts.entities.event_normalize_receipts",
            "scripts.entities.event_normalize_runtime",
            "scripts.entities.event_normalize_snapshot",
            "scripts.entities.event_normalize_storage",
            "scripts.entities.event_normalize_work_items",
            "scripts.entities.event_normalize_write_contract",
            "scripts.entities.event_normalize_write_result",
            "scripts.entities.event_normalize_write_storage",
            "scripts.entities.event_normalize_write_units",
            "scripts.entities.event_normalize_writes",
            "scripts.entities.event_vocabulary",
            "scripts.kg.emission",
            "scripts.kg.emission_bundles",
            "scripts.kg.emission_checks",
            "scripts.kg.emission_models",
            "scripts.kg.emission_receipts",
            "scripts.kg.emission_validation",
            "scripts.kg.identity",
            "scripts.kg.identity_assertions",
            "scripts.kg.identity_keys",
            "scripts.kg.phase_envelope",
            "scripts.kg.producer_coverage",
            "scripts.kg.producer_versions",
            "scripts.kg.quarantine",
            "scripts.kg.registries.compatibility",
            "scripts.kg.registries.entity_taxonomy",
            "scripts.kg.registries.events",
            "scripts.kg.registries.evidence",
            "scripts.kg.registries.model",
            "scripts.kg.registries.relationships",
            "scripts.kg.registries.roles",

    ),
}


def is_qualified_module(dotted: str) -> bool:
    """Whether ``dotted`` is a qualified module path rather than a file path."""
    return isinstance(dotted, str) and bool(_QUALIFIED_MODULE.fullmatch(dotted))


def declared_modules(phase: dict) -> tuple[str, ...]:
    """The declared producer modules for ``phase``.

    Legacy fallback: a phase that declares no ``code_modules`` manifests exactly
    its single ``module`` field.
    """
    declared = phase.get("code_modules")
    if declared:
        return tuple(declared)
    module = phase.get("module")
    return (module,) if module else ()


def _manifest_problems(declared: tuple[str, ...]) -> dict[str, str]:
    """Declaration-level problems that must not be hashed over."""
    problems: dict[str, str] = {}
    if len(set(declared)) != len(declared):
        problems["manifest"] = "duplicate module path in code_modules"
    elif list(declared) != sorted(declared):
        problems["manifest"] = "code_modules must be declared in sorted order"
    for dotted in declared:
        if not is_qualified_module(dotted):
            problems[dotted] = "not a qualified dotted module path"
    return problems


def producer_metadata(phase: dict) -> dict[str, object]:
    """Durable producer identity and source evidence for ``phase``.

    ``code_sha256`` keeps its historic direct-file value for one-module phases;
    for multi-module phases it is a deterministic aggregate of module path plus
    per-module hash, in declared order.
    """
    module_path = phase.get("module")
    declared = declared_modules(phase)
    module_hashes: dict[str, str | None] = {}
    module_errors: dict[str, str] = {}
    metadata: dict[str, object] = {
        "module": module_path,
        "function": phase.get("run_fn_name"),
        "code_sha256": None,
        "code_modules": list(declared),
        "code_module_sha256": module_hashes,
        "code_module_errors": module_errors,
        "code_evidence_complete": False,
    }
    if not declared:
        return metadata

    module_errors.update(_manifest_problems(declared))

    for dotted in declared:
        if dotted in module_errors:
            module_hashes[dotted] = None
            continue
        try:
            module = importlib.import_module(dotted)
            source_path = getattr(module, "__file__", None)
            if not source_path:
                raise OSError("module source file unavailable")
            with open(source_path, "rb") as source:
                module_hashes[dotted] = hashlib.sha256(source.read()).hexdigest()
        except (ImportError, OSError, TypeError) as error:
            module_hashes[dotted] = None
            module_errors[dotted] = f"{type(error).__name__}: {error}"

    if module_errors:
        return metadata
    if len(declared) == 1:
        metadata["code_sha256"] = module_hashes[declared[0]]
        metadata["code_evidence_complete"] = True
        return metadata

    aggregate = hashlib.sha256()
    for dotted in declared:
        module_hash = module_hashes[dotted]
        assert module_hash is not None  # guarded by the no-errors return above
        aggregate.update(dotted.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(module_hash.encode("ascii"))
        aggregate.update(b"\n")
    metadata["code_sha256"] = aggregate.hexdigest()
    metadata["code_evidence_complete"] = True
    return metadata


def _module_path(dotted: str, repo_root: Path) -> Path | None:
    """Repository source file for ``dotted``, or ``None`` when it is not ours."""
    if not is_qualified_module(dotted):
        return None
    candidate = repo_root / (dotted.replace(".", "/") + ".py")
    return candidate if candidate.is_file() else None


def _module_imports(path: Path) -> set[str]:
    """Module names imported by ``path`` (best effort, never raises)."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError):
        return set()
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
            found.update(f"{node.module}.{alias.name}" for alias in node.names)
    return found


def import_closure(seed_modules, repo_root) -> tuple[str, ...]:
    """Every repository module reachable from ``seed_modules`` by import."""
    resolved: set[str] = set()
    pending = list(seed_modules)
    while pending:
        dotted = pending.pop()
        if dotted in resolved:
            continue
        path = _module_path(dotted, repo_root)
        if path is None:
            continue
        resolved.add(dotted)
        pending.extend(_module_imports(path))
    return tuple(sorted(resolved))


def undocumented_imports(phase: dict, repo_root) -> tuple[str, ...]:
    """Behaviour-bearing modules reachable from the declaration but not declared."""
    declared = set(declared_modules(phase))
    return tuple(
        module for module in import_closure(declared, repo_root)
        if module not in declared
    )
