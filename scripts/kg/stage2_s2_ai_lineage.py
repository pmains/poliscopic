#!/usr/bin/env python3
"""``stage2_s2_ai_lineage.py`` — which plan and aggregate are current.

Modification time is not evidence.  A file can be touched, copied, restored or
written by an unrelated job, and a loader that picks "the newest thing on disk"
will happily validate a proposal against a plan that was superseded an hour ago.

This module answers the lineage question from **recorded supersession** instead:

* a **head** is a plan artifact that carries no obsolescence sidecar *and* is not
  named as ``supersedes`` by any other non-obsolete plan;
* there must be **exactly one** head — zero or several is a refusal, never a
  guess;
* the **current aggregate** is the non-obsolete aggregate that binds that head's
  digest, and there must be exactly one;
* a proposal is admissible only if its plan fingerprint equals the head digest,
  it is not obsolete, and its **path and digest are both members** of the current
  aggregate.

Everything fails closed and nothing here writes.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "LineageRefused",
    "current_aggregate",
    "current_plan",
    "plan_heads",
    "proposal_membership",
    "verify_lineage",
]

PLAN_GLOB = "kg-stage2-s2-plan-*.json"
AGGREGATE_GLOB = "kg-stage2-s2-ai-proposals-*.json"
DEFAULT_DIRECTORY = REPO / "data" / "kg-plans"


class LineageRefused(RuntimeError):
    """The artifact lineage could not be established; nothing may be reviewed."""


def _is_obsolete(path: Path) -> bool:
    return (path.parent / f"{path.name}{artifacts.OBSOLETE_SUFFIX}").exists()


def _load(path: Path) -> tuple[dict[str, Any], str]:
    document = artifacts.load_verified(path)
    return document, artifacts.recorded_digest(document)


def plan_heads(directory: str | Path | None = None) -> list[tuple[Path, dict[str, Any], str]]:
    """Plans that are not obsolete and are not superseded by another live plan."""
    folder = Path(directory) if directory is not None else DEFAULT_DIRECTORY
    candidates = [p for p in sorted(folder.glob(PLAN_GLOB))
                  if not p.name.endswith(artifacts.OBSOLETE_SUFFIX) and not _is_obsolete(p)]
    loaded = [(p, *_load(p)) for p in candidates]

    superseded: set[str] = set()
    for _path, document, _digest in loaded:
        reference = (document.get("supersedes") or {})
        target = reference.get("path") or reference.get("plan_id")
        if not target:
            continue
        superseded.add(Path(str(target)).name)
        superseded.add(str(reference.get("plan_id")))

    heads = [(p, d, dg) for p, d, dg in loaded
             if p.name not in superseded and str(d.get("plan_id")) not in superseded]
    return heads


def current_plan(directory: str | Path | None = None) -> tuple[Path, dict[str, Any], str]:
    """The single verified plan head, or a refusal naming the problem."""
    heads = plan_heads(directory)
    if not heads:
        raise LineageRefused("no current plan head: every plan is obsolete or superseded")
    if len(heads) > 1:
        names = ", ".join(sorted(p.name for p, _d, _g in heads))
        raise LineageRefused(f"ambiguous plan lineage: {len(heads)} current heads ({names})")
    return heads[0]


def current_aggregate(
    directory: str | Path | None = None, plan_digest: str | None = None
) -> tuple[Path, dict[str, Any], str]:
    """The single non-obsolete aggregate that binds the current plan."""
    folder = Path(directory) if directory is not None else DEFAULT_DIRECTORY
    if plan_digest is None:
        _p, _d, plan_digest = current_plan(folder)
    live = [p for p in sorted(folder.glob(AGGREGATE_GLOB))
            if not p.name.endswith(artifacts.OBSOLETE_SUFFIX) and not _is_obsolete(p)]
    matching = []
    for path in live:
        document, digest = _load(path)
        if (document.get("manifest") or {}).get("plan_digest") == plan_digest:
            matching.append((path, document, digest))
    if not matching:
        raise LineageRefused(
            "no current aggregate binds plan digest " + str(plan_digest))
    if len(matching) > 1:
        names = ", ".join(sorted(p.name for p, _d, _g in matching))
        raise LineageRefused(
            f"ambiguous aggregate lineage: {len(matching)} current aggregates ({names})")
    return matching[0]


def _entry_problems(entry_path: Any) -> list[str]:
    """Absolute paths and traversal are ambiguous, whatever they point at."""
    problems: list[str] = []
    if entry_path in (None, ""):
        return ["aggregate entry has no path"]
    text = str(entry_path)
    if Path(text).is_absolute():
        problems.append(f"aggregate entry {text!r} is an absolute path: ambiguous")
    if ".." in Path(text).parts:
        problems.append(f"aggregate entry {text!r} contains traversal: ambiguous")
    return problems


def proposal_membership(
    aggregate: Mapping[str, Any], proposal_path: str | Path, proposal_digest: str
) -> tuple[bool, list[str]]:
    """Locate the ONE aggregate entry for this proposal and pair its digest.

    Names and digests are **not** independent sets.  Comparing them that way
    lets a proposal borrow another's membership: if proposal A's file name and
    proposal B's digest both appear somewhere in the aggregate, an
    independent-set test accepts A paired with B's digest.  Membership is a
    property of a single entry, so exactly one entry must match this artifact by
    path or basename, and *that* entry's digest must equal the proposal's.
    """
    problems: list[str] = []
    units = (aggregate.get("per_unit") or {})
    entries: list[Mapping[str, Any]] = []
    for members in units.values():
        entries.extend(members or [])
    if not entries:
        return False, ["aggregate lists no per-unit proposals"]

    requested = Path(proposal_path)
    # A caller may legitimately hand in an absolute path; it is resolved by
    # basename and is not ambiguous.  Traversal is refused because it lets the
    # requested path climb out of the store.  The ambiguity that matters is in
    # the AGGREGATE's own entry paths, checked per entry below.
    if ".." in requested.parts:
        problems.append(f"proposal path {str(proposal_path)!r} contains traversal: ambiguous")
    if problems:
        return False, problems

    name = requested.name
    matches: list[Mapping[str, Any]] = []
    for entry in entries:
        entry_problems = _entry_problems(entry.get("path"))
        if entry_problems:
            problems.extend(entry_problems)
            continue
        entry_path = Path(str(entry.get("path")))
        if entry_path.name == name or str(entry_path) == str(requested):
            matches.append(entry)

    if problems:
        return False, problems
    if not matches:
        return False, [f"proposal {name} is not a member of the current aggregate"]
    if len(matches) > 1:
        return False, [
            f"proposal {name} has {len(matches)} matching entries in the aggregate: ambiguous"]

    entry = matches[0]
    entry_digest = entry.get("digest")
    if entry_digest is None:
        problems.append(f"aggregate entry for {name} records no digest")
    elif str(entry_digest) != str(proposal_digest):
        problems.append(
            f"digest mismatch for {name}: entry records {entry_digest}, "
            f"proposal is {proposal_digest}")
    return not problems, problems


def verify_lineage(
    proposal_path: str | Path,
    proposal: Mapping[str, Any],
    directory: str | Path | None = None,
) -> dict[str, Any]:
    """Every lineage condition a reviewable proposal must satisfy."""
    folder = Path(directory) if directory is not None else DEFAULT_DIRECTORY
    path = Path(proposal_path)

    if _is_obsolete(path):
        raise LineageRefused(f"proposal {path.name} is marked obsolete")

    plan_path, plan, plan_digest = current_plan(folder)
    aggregate_path, aggregate, aggregate_digest = current_aggregate(folder, plan_digest)

    fingerprint = (proposal.get("input_fingerprints") or {}).get("plan")
    if fingerprint != plan_digest:
        raise LineageRefused(
            f"proposal plan fingerprint {fingerprint!r} does not equal the current "
            f"plan digest {plan_digest!r}")

    digest = artifacts.recorded_digest(proposal)
    member, problems = proposal_membership(aggregate, path, digest)
    if not member:
        raise LineageRefused("; ".join(problems))

    if aggregate.get("supersedes") and aggregate.get("kind") != "kg-stage2-s2-ai-proposals-aggregate":
        raise LineageRefused("current aggregate is not an aggregate artifact")

    return {
        "plan": {"path": plan_path.name, "digest": plan_digest},
        "aggregate": {"path": aggregate_path.name, "digest": aggregate_digest},
        "proposal": {"path": path.name, "digest": digest},
        "plan_heads": [p.name for p, _d, _g in plan_heads(folder)],
    }
