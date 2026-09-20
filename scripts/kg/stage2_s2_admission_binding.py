#!/usr/bin/env python3
"""``stage2_s2_admission_binding.py`` — artifact, decision and code binding.

Split out of the apply runner so both stay readable.  This module owns the part of
admission that decides whether the *reviewed objects* are genuinely the ones
authorized:

* artifacts are named by **exact path and digest**, loaded through the
  verify-on-read loader, and their canonical digest is recomputed — a caller's
  mapping or a stored digest string is never trusted;
* the current S2 plan head, aggregate head, all five decisions and their proposals
  are resolved and re-hashed **field by field** against the canonical artifacts
  *and* the aggregate entries — including the **full candidate identity** (db id,
  item id, number, meeting and fingerprint) and each decision's **lineage**:
  the plan lineage must be the current head, the proposal lineage the resolved
  proposal, and the aggregate anchor a real artifact whose digest matches;
* obsolete artifacts are refused, and a *historical* anchor is distinguished from
  a *current head* rather than conflated with it.

Everything here is read-only.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402
from scripts.kg import stage2_s2_label_correction as correction_mod  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as repair_mod  # noqa: E402

__all__ = [
    "ApplyRefused",
    "AuthorizedArtifact",
    "CANDIDATE_FIELDS",
    "DECISION_FIELDS",
    "PLAN_ROLES",
]

#: The two plans this runner is bound to, and the validator each one owns.
PLAN_ROLES = {
    "repair": (repair_mod.PLAN_KIND, repair_mod.validate_plan, "rows"),
    "correction": (correction_mod.PLAN_KIND, correction_mod.validate_plan, "operations"),
}


class ApplyRefused(RuntimeError):
    """Admission failed; nothing was written and nothing will be."""


@dataclass(frozen=True)
class AuthorizedArtifact:
    """A plan the reviewer named by exact path and exact canonical digest."""

    path: str
    digest: str


# ── 1. exact artifacts, canonically loaded ─────────────────────────────


def _load_authorized(auth: AuthorizedArtifact, *, role: str,
                     plan_dir: Path) -> dict[str, Any]:
    """Load one authorized plan: exact path, verified, re-digested, validated."""
    expected_kind, validator, _ = PLAN_ROLES[role]
    path = plan_dir / Path(auth.path).name
    if Path(auth.path).name != auth.path:
        raise ApplyRefused(f"{role}: the authorized path must be a plain name")
    if not path.exists():
        raise ApplyRefused(f"{role}: authorized artifact {auth.path!r} does not exist")
    if artifacts.is_obsolete(path) is not None:
        raise ApplyRefused(f"{role}: artifact {auth.path!r} is obsolete")

    document = artifacts.load_verified(path)          # verifies the stored digest
    recorded = artifacts.recorded_digest(document)
    recomputed = artifacts.compute_digest(document)
    if recorded != auth.digest or recomputed != auth.digest:
        raise ApplyRefused(
            f"{role}: digest mismatch (authorized {auth.digest[:16]}..., "
            f"recorded {str(recorded)[:16]}..., recomputed {recomputed[:16]}...)")
    if document.get("kind") != expected_kind:
        raise ApplyRefused(f"{role}: kind {document.get('kind')!r} is not {expected_kind!r}")
    if document.get("mode") != "dry-run":
        raise ApplyRefused(f"{role}: the plan is not a dry-run artifact")
    problems = validator(document)
    if problems:
        raise ApplyRefused(f"{role}: validator refused: {'; '.join(problems[:3])}")
    return document


# ── 2. heads, decisions, proposals, code, obsolete ────────────────────


def _resolve_artifact(name: str, plan_dir: Path) -> Path:
    """Resolve an artifact by exact filename under the plan directory.

    Proposals live in ``ai-proposals-*/`` subdirectories and decisions at the top
    level, so the name is searched across the known roots.  Zero or several
    matches is a refusal: an ambiguous resolution is how the wrong artifact gets
    validated.
    """
    roots = [plan_dir] + sorted(d for d in plan_dir.iterdir() if d.is_dir())
    hits = [root / name for root in roots if (root / name).exists()]
    live = [h for h in hits if artifacts.is_obsolete(h) is None]
    if len(live) == 1:
        return live[0]
    if not hits:
        raise ApplyRefused(f"artifact {name!r} does not resolve under {plan_dir.name}/")
    if not live:
        raise ApplyRefused(f"artifact {name!r} resolves only to obsolete copies")
    raise ApplyRefused(
        f"artifact {name!r} resolves to {len(live)} live paths: "
        f"{sorted(h.parent.name for h in live)}")


def _resolve_historical(name: str, plan_dir: Path) -> Path:
    """Resolve a *historical* artifact by exact filename, obsolete copies included.

    A decision's lineage records the aggregate it was adjudicated against, and that
    artifact may since have been superseded by the aggregate that records the
    decision.  The anchor is therefore verified as a real artifact with a matching
    digest, not required to be the current head.  Ambiguity is still a refusal.
    """
    roots = [plan_dir] + sorted(d for d in plan_dir.iterdir() if d.is_dir())
    hits = [root / name for root in roots
            if (root / name).exists() and not name.endswith(".obsolete.json")]
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise ApplyRefused(f"artifact {name!r} does not resolve under {plan_dir.name}/")
    raise ApplyRefused(
        f"artifact {name!r} resolves to {len(hits)} paths: "
        f"{sorted(h.parent.name for h in hits)}")


def _verify_heads(plan_dir: Path) -> dict[str, Any]:
    try:
        plan_path, plan, plan_digest = lineage.current_plan(plan_dir)
    except lineage.LineageRefused as exc:
        raise ApplyRefused(f"lineage refused: {exc}") from exc
    aggregate_path, aggregate, aggregate_digest = lineage.current_aggregate(
        plan_dir, plan_digest)
    for path in (plan_path, aggregate_path):
        if artifacts.is_obsolete(path) is not None:
            raise ApplyRefused(f"current head {path.name} is obsolete")
    return {
        "plan": {"path": plan_path.name, "digest": plan_digest},
        "aggregate": {"path": aggregate_path.name, "digest": aggregate_digest,
                      "document": aggregate},
    }


#: The bound decision fields compared against the canonical decision artifact.
DECISION_FIELDS = ("decision_id", "decision", "adjudicator", "decided_at",
                   "document_id", "document_role", "document_fingerprint",
                   "human_stated_item", "item_number_mismatch")

#: Every field of the candidate the human approved.  The database id and the item
#: number alone are not the candidate: two candidates can share a number in
#: different meetings, and the stored item id and its fingerprint pin the exact row.
CANDIDATE_FIELDS = ("agenda_item_db_id", "agenda_item_id", "agenda_item_number",
                    "meeting_db_id", "agenda_item_fingerprint")


def _candidate_of(record: Mapping[str, Any]) -> dict[str, Any]:
    """The candidate identity, in full, from whatever carries it."""
    candidate = record.get("candidate") or {}
    return {field: candidate.get(field) for field in CANDIDATE_FIELDS}


def verify_supersession_ancestry(plan_dir: Path, start_path: Path, start_digest: str,
                                 target_digest: str) -> dict[str, Any]:
    """Walk the recorded ``supersedes`` chain and require it to reach a digest.

    The current aggregate is not the aggregate the decisions were adjudicated
    against: the decisions anchor an earlier emission.  That is only acceptable if
    the artifact now in force can **prove** it descends from that anchor, so the
    chain of recorded supersessions is walked rather than assumed.
    """
    chain: list[dict[str, Any]] = []
    seen: set[str] = set()
    path, digest = start_path, start_digest
    for _ in range(64):  # bounded: a cycle or a runaway chain must not hang
        if digest == target_digest:
            return {"reached": True, "chain": chain}
        if digest in seen:
            return {"reached": False, "reason": "the supersession chain cycles",
                    "chain": chain}
        seen.add(digest)
        document = artifacts.load_verified(path)
        # ``supersedes`` is recorded in two shapes across the artifact history: a
        # mapping naming path+digest, and a bare path string.  Both are accepted;
        # anything else ends the walk rather than being guessed at.
        reference = document.get("supersedes")
        if isinstance(reference, Mapping):
            name = Path(str(reference.get("path") or "")).name
        elif isinstance(reference, str):
            name = Path(reference).name
        else:
            name = ""
        if not name:
            return {"reached": False,
                    "reason": f"the chain ends at {path.name} without reaching the anchor",
                    "chain": chain}
        try:
            path = _resolve_historical(name, plan_dir)
            document = artifacts.load_verified(path)
        except Exception as exc:  # noqa: BLE001 - an unresolvable link ends the walk
            return {"reached": False,
                    "reason": f"the chain names an artifact that does not resolve: {exc}",
                    "chain": chain}
        digest = artifacts.recorded_digest(document)
        chain.append({"path": path.name, "digest": digest})
    return {"reached": False, "reason": "the supersession chain is too long",
            "chain": chain}


def verify_target_equality(repair_plan: Mapping[str, Any],
                           correction_plan: Mapping[str, Any],
                           live_target: Mapping[str, Any]) -> list[str]:
    """Both plans must bind the same target, and it must be the live one."""
    problems: list[str] = []
    fields = ("dialect", "host", "port", "database", "tier")
    left = {f: ((repair_plan.get("bindings") or {}).get("target") or {}).get(f)
            for f in fields}
    right = {f: ((correction_plan.get("bindings") or {}).get("target") or {}).get(f)
             for f in fields}
    live = {f: live_target.get(f) for f in fields}
    if left != right:
        problems.append(f"the two plans bind different targets: {left} vs {right}")
    if left != live:
        problems.append(f"the plans' target {left} is not the live target {live}")
    return problems


def _verify_decisions(plan: Mapping[str, Any], *, plan_dir: Path,
                      aggregate: Mapping[str, Any],
                      heads: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Re-resolve and re-compare every decision and its proposal, exhaustively.

    A decision whose direct ``path`` is null is resolved through the bound
    aggregate, which is the only thing that can say where it lives.  Every field
    the plan binds is then compared against **both** the canonical decision
    artifact and the aggregate's own entry — a plan that bound the right count but
    the wrong adjudicator, time, role, candidate or proposal would otherwise pass.
    """
    bound = (plan.get("bindings") or {}).get("decisions") or []
    if len(bound) != binding.EXPECTED_APPROVED_DECISIONS:
        raise ApplyRefused(f"expected {binding.EXPECTED_APPROVED_DECISIONS} bound decisions")

    aggregate_decisions = aggregate.get("decisions") or {}
    per_unit: dict[int, list[dict[str, Any]]] = {}
    for unit_key, members in (aggregate.get("per_unit") or {}).items():
        for member in members:
            per_unit.setdefault(int(member["document_id"]), []).append(
                {**member, "unit": unit_key})

    bound_ids = sorted(int(e["document_id"]) for e in bound)
    aggregate_ids = sorted(int(d) for d in aggregate_decisions)
    if bound_ids != aggregate_ids:
        raise ApplyRefused(
            f"the bound decision set {bound_ids} is not the aggregate's {aggregate_ids}")

    verified: list[dict[str, Any]] = []
    for entry in bound:
        document_id = int(entry["document_id"])
        here = f"decision for document {document_id}"
        aggregate_entry = aggregate_decisions.get(str(document_id))
        if not aggregate_entry:
            raise ApplyRefused(f"{here} is not in the aggregate")

        name = entry.get("path") or aggregate_entry.get("path")
        if not name:
            raise ApplyRefused(f"{here} has no resolvable path")
        if aggregate_entry.get("path") and Path(str(name)).name != Path(
                str(aggregate_entry["path"])).name:
            raise ApplyRefused(f"{here}: the bound path is not the aggregate's path")
        path = _resolve_artifact(Path(name).name, plan_dir)
        if artifacts.is_obsolete(path) is not None:
            raise ApplyRefused(f"{here}: {name!r} is obsolete")

        record = artifacts.load_verified(path)
        digest = artifacts.recorded_digest(record)
        for label, value in (("bound", entry.get("digest")),
                             ("aggregate", aggregate_entry.get("digest"))):
            if value != digest:
                raise ApplyRefused(f"{here}: the {label} digest is not the artifact's")

        # Field-by-field: the artifact is the canonical source.
        for field in DECISION_FIELDS:
            if field == "document_id":
                if int(record.get(field, -1)) != document_id:
                    raise ApplyRefused(f"{here}: the artifact names another document")
                continue
            if record.get(field) != entry.get(field):
                raise ApplyRefused(f"{here}: {field!r} differs from the artifact")
            if field in aggregate_entry and aggregate_entry.get(field) != entry.get(field):
                raise ApplyRefused(f"{here}: {field!r} differs from the aggregate")
        bound_candidate = entry.get("candidate")
        if bound_candidate is None:
            bound_candidate = {field: entry.get(f"candidate_{field}")
                               for field in CANDIDATE_FIELDS}
        if _candidate_of(record) != {f: bound_candidate.get(f)
                                     for f in CANDIDATE_FIELDS}:
            raise ApplyRefused(
                f"{here}: the candidate identity differs from the artifact "
                f"(db id, item id, number, meeting and fingerprint must all match)")

        proposal = record.get("proposal") or {}
        proposal_path = proposal.get("path") or entry.get("proposal_path")
        proposal_digest = proposal.get("digest") or entry.get("proposal_digest")
        if not proposal_path:
            raise ApplyRefused(f"{here}: no proposal resolves")
        # The plan's OWN binding must be the artifact's: a bound path or digest the
        # artifact does not agree with would otherwise be silently overridden.
        if Path(str(entry.get("proposal_path") or "")).name != Path(proposal_path).name:
            raise ApplyRefused(f"{here}: the bound proposal path is not the artifact's")
        if entry.get("proposal_digest") != proposal_digest:
            raise ApplyRefused(f"{here}: the bound proposal digest is not the artifact's")
        if entry.get("proposal_digest") and proposal_digest != entry["proposal_digest"]:
            raise ApplyRefused(f"{here}: the proposal digest differs from the artifact")
        resolved_proposal = _resolve_artifact(Path(proposal_path).name, plan_dir)
        if artifacts.is_obsolete(resolved_proposal) is not None:
            raise ApplyRefused(f"{here}: proposal {proposal_path!r} is obsolete")
        loaded = artifacts.load_verified(resolved_proposal)
        if artifacts.recorded_digest(loaded) != proposal_digest:
            raise ApplyRefused(f"{here}: the proposal artifact has drifted")

        # Exact membership: the aggregate must carry this document exactly once,
        # under this proposal, with this digest.
        members = per_unit.get(document_id) or []
        if len(members) != 1:
            raise ApplyRefused(
                f"{here}: the aggregate carries it {len(members)} times, not once")
        member = members[0]
        if member.get("path") != proposal_path or member.get("digest") != proposal_digest:
            raise ApplyRefused(f"{here}: the aggregate's proposal membership differs")

        # The decision's lineage must be consistent and verifiable.
        #   plan      -> must be the CURRENT plan head: a decision recorded against a
        #                superseded plan is not authority for this apply.
        #   proposal  -> must be the proposal actually resolved.
        #   aggregate -> the anchor the decision was adjudicated against.  It is a
        #                historical record and may since have been superseded by the
        #                aggregate that carries the decisions, so it is verified as a
        #                real artifact with a matching digest rather than required to
        #                be the current head.  The CURRENT aggregate must carry this
        #                decision's membership, which is checked below.
        record_lineage = record.get("lineage") or {}
        # The plan's own bound lineage must BE the artifact's lineage: a block that
        # bound different heads than the artifact records would otherwise be
        # decorative, because the checks below read the artifact.
        bound_lineage = entry.get("lineage")
        if bound_lineage is not None:
            for key in ("plan", "aggregate", "proposal"):
                if dict(bound_lineage.get(key) or {}) != dict(record_lineage.get(key) or {}):
                    raise ApplyRefused(
                        f"{here}: the bound lineage[{key}] is not the artifact's")
        plan_lineage = record_lineage.get("plan") or {}
        if Path(str(plan_lineage.get("path") or "")).name != \
                (heads or {}).get("plan", {}).get("path"):
            raise ApplyRefused(f"{here}: the decision's plan lineage names another path")
        if plan_lineage.get("digest") != (heads or {}).get("plan", {}).get("digest"):
            raise ApplyRefused(f"{here}: the decision's plan lineage is not the current head")

        proposal_lineage = record_lineage.get("proposal") or {}
        if Path(str(proposal_lineage.get("path") or "")).name != Path(proposal_path).name:
            raise ApplyRefused(f"{here}: the decision's proposal lineage names another path")
        if proposal_lineage.get("digest") != proposal_digest:
            raise ApplyRefused(f"{here}: the decision's proposal lineage digest differs")

        aggregate_lineage = record_lineage.get("aggregate") or {}
        anchor_name = Path(str(aggregate_lineage.get("path") or "")).name
        if not anchor_name or not aggregate_lineage.get("digest"):
            raise ApplyRefused(f"{here}: the decision records no aggregate anchor")
        try:
            anchor = _resolve_historical(anchor_name, plan_dir)
            anchor_document = artifacts.load_verified(anchor)
        except ApplyRefused:
            raise
        except Exception as exc:  # noqa: BLE001 - an unloadable anchor is a refusal
            raise ApplyRefused(
                f"{here}: the decision's aggregate anchor does not load: {exc}") from exc
        if artifacts.recorded_digest(anchor_document) != aggregate_lineage.get("digest"):
            raise ApplyRefused(
                f"{here}: the decision's aggregate anchor digest does not match the "
                f"artifact it names")

        # The ANCHOR itself must carry the proposal this decision approves: the
        # exact path, the exact digest, and the exact unit.  An anchor that merely
        # exists is not evidence that it contained the proposal.
        unit = entry.get("proposal_unit")
        if not unit:
            raise ApplyRefused(
                f"{here}: the plan binds no adjudication unit, so the anchor's "
                f"membership cannot be checked unit-for-unit")
        if (record.get("proposal") or {}).get("decision_unit_id") != unit:
            raise ApplyRefused(
                f"{here}: the bound adjudication unit is not the artifact's")
        anchor_members = [
            {**member, "unit": unit_key}
            for unit_key, members in (anchor_document.get("per_unit") or {}).items()
            for member in members
            if int(member.get("document_id", -1)) == document_id]
        if len(anchor_members) != 1:
            raise ApplyRefused(
                f"{here}: the aggregate anchor carries it {len(anchor_members)} "
                f"times, not once")
        anchor_member = anchor_members[0]
        if Path(str(anchor_member.get("path") or "")).name != Path(proposal_path).name:
            raise ApplyRefused(f"{here}: the anchor's proposal path differs")
        if anchor_member.get("digest") != proposal_digest:
            raise ApplyRefused(f"{here}: the anchor's proposal digest differs")
        if anchor_member.get("unit") != unit:
            raise ApplyRefused(
                f"{here}: the anchor files the proposal under a different unit "
                f"({anchor_member.get('unit')!r} vs {unit!r})")

        # And the current aggregate must PROVE it descends from that anchor: the
        # recorded supersession chain is walked, not assumed.
        if heads is not None:
            head = heads.get("aggregate") or {}
            ancestry = verify_supersession_ancestry(
                plan_dir, plan_dir / str(head.get("path") or ""),
                str(head.get("digest") or ""), str(aggregate_lineage.get("digest") or ""))
            if not ancestry.get("reached"):
                raise ApplyRefused(
                    f"{here}: the current aggregate does not descend from the "
                    f"decision's anchor: {ancestry.get('reason')}")
        verified.append({
            "document_id": document_id, "decision": record["decision_id"],
            "proposal": proposal_path, "unit": member.get("unit"),
            "digest": digest, "adjudicator": record.get("adjudicator"),
            "decided_at": record.get("decided_at"),
            "lineage_matches_current": True,
        })
    return verified


def _verify_code_hashes(plan: Mapping[str, Any]) -> dict[str, str]:
    """The plan must bind the whole execution set, and every hash must be current."""
    recorded = (plan.get("bindings") or {}).get("code_hashes") or {}
    if not recorded:
        raise ApplyRefused("the plan binds no code hashes")
    absent = binding.missing_code_modules()
    if absent:
        raise ApplyRefused(f"declared execution modules are absent from disk: {absent}")
    uncovered = sorted(set(binding.CODE_MODULES) - set(recorded))
    if uncovered:
        raise ApplyRefused(
            f"the plan does not bind the complete execution set; missing: {uncovered}")
    uncovered_execution = sorted(set(binding.EXECUTION_MODULES) - set(recorded))
    if uncovered_execution:
        raise ApplyRefused(
            f"the plan does not bind the execution modules: {uncovered_execution}")
    live = binding.code_hashes(tuple(recorded))
    drifted = sorted(rel for rel, digest in recorded.items() if live.get(rel) != digest)
    if drifted:
        raise ApplyRefused(f"code has drifted for {drifted}")
    return recorded
