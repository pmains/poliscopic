#!/usr/bin/env python3
"""``stage2_subitem_manifest.py`` — the containment-specific binding block.

Containment is **not** a document-attachment question.  The generic Stage 2
binding carries the five document AI decisions and the proposal aggregate,
because those govern which *document* links to which *item*.  None of that has
any authority over whether item ``4.A`` sits inside item ``4``.

So containment gets its own manifest, and it binds only what it actually rests
on:

* the containment module, its **plan builder** and its validator — the pack is
  hashed whole, so a plan cannot be *built* by one revision and *validated* by
  another;
* the semantic registries that constrain the relation it proposes;
* the baseline artifact — path, canonical digest and a digest over its rows;
* the target and the focused ``agenda_items`` schema signature it was built
  against;
* the deterministic source authority for the population: ``agenda_items``.

:func:`validate_manifest` does not take any of those on trust.  It **recomputes
every code hash and every semantic-registry hash from disk** and compares them to
what the manifest bound, so a stale or tampered manifest is refused rather than
believed.  The registry list itself is compared as a set: a manifest that simply
dropped a registry it disagrees with is refused too.

There are deliberately **no** S2 decisions, no proposal aggregate and no
document-link authority here.  Binding them would be borrowing authority for a
question they do not answer.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "MANIFEST_KIND",
    "MANIFEST_VERSION",
    "CODE_MODULES",
    "SEMANTIC_DEPENDENCIES",
    "SOURCE_AUTHORITY",
    "build_manifest",
    "canonical_sha256",
    "code_hashes",
    "dependency_hashes",
    "validate_manifest",
]

MANIFEST_KIND = "kg-stage2-subitem-manifest"
MANIFEST_VERSION = "kg-stage2-subitem-manifest/2.0"

#: Everything the containment conclusion depends on, including the plan **builder**
#: as well as the module and the validator: a plan must not be buildable by one
#: revision of the code and validatable by another.
CODE_MODULES = (
    "scripts/kg/stage2_subitem_containment.py",
    "scripts/kg/stage2_subitem_plan.py",
    "scripts/kg/stage2_subitem_manifest.py",
    "scripts/kg/stage2_subitem_schema.py",
    "scripts/kg/stage2_artifacts.py",
)

#: The registries that constrain a PART_OF relation.  Named individually so a
#: registry change is a visible drift rather than a silent one, and compared as a
#: set so one cannot be dropped.
SEMANTIC_DEPENDENCIES = (
    "scripts/kg/registries/relationships.py",
    "scripts/kg/registries/evidence.py",
    "scripts/kg/registries/model.py",
    "scripts/kg/registries/roles.py",
    "scripts/kg/registries/validation.py",
    "scripts/kg/registries/entity_taxonomy.py",
)

#: The population's identity and where it comes from.  Deterministic source data
#: only: no model output participates in containment.
SOURCE_AUTHORITY = {
    "table": "agenda_items",
    "identity": "(meeting_db_id, normalized_number)",
    "deterministic": True,
    "model_output_used": False,
    "document_link_authority_used": False,
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"),
                   default=str).encode("utf-8")).hexdigest()


def _hash_files(relative_paths: Sequence[str]) -> dict[str, str]:
    """sha256 of each named file, read from disk.  Missing files are omitted."""
    out: dict[str, str] = {}
    for relative in relative_paths:
        path = REPO / relative
        if path.exists():
            out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    """sha256 of every module the plan's behaviour depends on."""
    return _hash_files(modules)


def dependency_hashes(
    dependencies: Sequence[str] = SEMANTIC_DEPENDENCIES,
) -> dict[str, str]:
    """sha256 of every semantic registry the relation rests on."""
    return _hash_files(dependencies)


def build_manifest(
    *,
    baseline_path: str | Path,
    baseline: Mapping[str, Any],
    target: Mapping[str, Any],
    schema_signature: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Assemble the containment manifest over one baseline artifact."""
    path = Path(baseline_path)
    rows = baseline.get("rows") or []
    baseline_block = {
        "path": path.name,
        "canonical_digest": artifacts.recorded_digest(baseline),
        "rows_digest": canonical_sha256(rows),
        "row_count": len(rows),
        "counts": dict(baseline.get("counts") or {}),
        "total": baseline.get("total"),
    }
    manifest = {
        "kind": MANIFEST_KIND,
        "version": MANIFEST_VERSION,
        "code_hashes": code_hashes(),
        "semantic_dependencies": dependency_hashes(),
        "baseline": baseline_block,
        "target": {f: target.get(f) for f in
                   ("dialect", "host", "port", "database", "tier")},
        "schema_signature": dict(schema_signature) if schema_signature else None,
        "source_authority": dict(SOURCE_AUTHORITY),
        "excluded_authority": {
            "s2_decisions": "document-link decisions have no authority over item containment",
            "proposal_aggregate": "the document proposal aggregate governs links, not hierarchy",
            "document_labels": "supporting_documents/document_ids labels are not containment evidence",
        },
    }
    manifest["digest"] = artifacts.compute_digest(manifest)
    problems = validate_manifest(manifest, recompute=True)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return manifest


def _compare_hashes(bound: Mapping[str, Any], live: Mapping[str, Any],
                    label: str, problems: list[str]) -> None:
    """Every bound hash must equal the recomputed one, and the sets must match."""
    missing = sorted(set(live) - set(bound))
    if missing:
        problems.append(f"the manifest binds no {label} hash for {missing}")
    unexpected = sorted(set(bound) - set(live))
    if unexpected:
        problems.append(f"the manifest binds a {label} hash for an unknown file: "
                        f"{unexpected}")
    for relative, digest in sorted(bound.items()):
        live_digest = live.get(relative)
        if live_digest is None:
            continue
        if live_digest != digest:
            problems.append(f"the bound {label} hash for {relative} is stale "
                            f"({str(digest)[:16]}... vs {live_digest[:16]}...)")


def validate_manifest(manifest: Mapping[str, Any], *,
                      recompute: bool = True) -> list[str]:
    """Structural checks, then a full recomputation of every bound hash.

    With ``recompute`` (the default) the code hashes **and** the semantic
    dependency hashes are recomputed from disk and compared, and the covered sets
    must be exactly the declared modules and registries.  ``recompute=False`` is
    for a caller that has already established the files agree; it is never the
    default, because the whole point is that a manifest is not believed.
    """
    problems: list[str] = []
    if manifest.get("kind") != MANIFEST_KIND:
        problems.append(f"kind must be {MANIFEST_KIND!r}")
    if manifest.get("version") != MANIFEST_VERSION:
        problems.append(f"version must be {MANIFEST_VERSION!r}")

    hashes = manifest.get("code_hashes") or {}
    for required in CODE_MODULES:
        if required not in hashes:
            problems.append(f"code hashes do not cover {required}")
    if recompute:
        _compare_hashes(hashes, code_hashes(), "code", problems)
        _compare_hashes(manifest.get("semantic_dependencies") or {},
                        dependency_hashes(), "semantic dependency", problems)
    elif not manifest.get("semantic_dependencies"):
        problems.append("no semantic dependencies are bound")

    baseline = manifest.get("baseline") or {}
    for field in ("path", "canonical_digest", "rows_digest", "row_count"):
        if baseline.get(field) is None:
            problems.append(f"the baseline binding is missing {field!r}")
    if not manifest.get("target", {}).get("database"):
        problems.append("the manifest binds no target database")
    if not manifest.get("schema_signature"):
        problems.append("the manifest binds no schema signature")
    authority = manifest.get("source_authority") or {}
    if authority.get("model_output_used") is not False:
        problems.append("containment must not rest on model output")
    if authority.get("document_link_authority_used") is not False:
        problems.append("containment must not rest on document-link authority")
    if manifest.get("excluded_authority") is None:
        problems.append("the manifest must record what authority it excludes")
    if manifest.get("digest") != artifacts.compute_digest(manifest):
        problems.append("the recorded manifest digest is not canonical")
    return problems
