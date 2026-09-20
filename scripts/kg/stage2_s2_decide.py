#!/usr/bin/env python3
"""``stage2_s2_decide.py`` — record human adjudication, bind, rebind, stop.

Reads a *decision request* — the human's own words about five (document,
candidate) pairs — then, for every entry:

1. loads the proposal through the authoritative engine-bound read-only loader,
   so no fact comes from the request;
2. refuses if the loader's bound candidate is not the candidate the human named;
3. builds one immutable decision artifact;
4. rebinds the aggregate so it indexes the decisions, and emits a new plan whose
   code hashes cover the decision module.

It never promotes, never applies, and never writes to a database.  ``--dry-run``
performs every check and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.db.core import get_engine  # noqa: E402
from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_ai_adjudication_loader as loader  # noqa: E402
from scripts.kg import stage2_s2_documents as documents  # noqa: E402
from scripts.kg import stage2_s2_human_decision as human  # noqa: E402

__all__ = ["main", "record_decisions"]

PLAN_DIR = REPO / "data" / "kg-plans"


def _write(path: Path, payload: Mapping[str, Any]) -> str:
    return artifacts.write_immutable(path, dict(payload))


def record_decisions(
    request: Mapping[str, Any],
    *,
    engine: Any,
    plan_dir: Path = PLAN_DIR,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Record every decision in *request*, then rebind, or refuse entirely."""
    adjudicator = str(request.get("adjudicator") or "").strip()
    moment = request.get("decided_at") or datetime.now(timezone.utc).isoformat()
    entries = request.get("decisions") or []
    if not adjudicator:
        raise human.DecisionRefused("the decision request names no adjudicator")
    if not entries:
        raise human.DecisionRefused("the decision request carries no decisions")

    proposal_dir = plan_dir / str(request.get("proposal_dir") or "ai-proposals-v7")
    records: list[dict[str, Any]] = []
    for entry in entries:
        document_id = int(entry["document_id"])
        wanted = int(entry["candidate"])
        proposal_path = proposal_dir / f"proposal-{entry['proposal_stem']}.json"
        loaded = loader.load_for_adjudication(proposal_path, engine)

        target = loaded.get("target") or {}
        if int(target.get("agenda_item_db_id", -1)) != wanted:
            raise human.DecisionRefused(
                f"document {document_id}: the loader bound candidate "
                f"{target.get('agenda_item_db_id')}, but the decision names {wanted}")
        if int(loaded["document"]["document_id"]) != document_id:
            raise human.DecisionRefused(
                f"proposal {proposal_path.name} is document "
                f"{loaded['document']['document_id']}, not {document_id}")

        record = human.build_decision(
            loaded=loaded,
            role=entry["role"],
            description=entry["description"],
            human_stated_item=entry["item"],
            adjudicator=adjudicator,
            decision_id=entry.get("decision_id") or human.decision_id_for(moment, document_id),
            decided_at=entry.get("decided_at") or moment,
            decision=str(entry.get("decision") or human.APPROVE),
        )
        records.append(record)

    # Every decision must still describe the loader's live state before anything
    # is written; a partial write is how one decision lands and another silently
    # does not.
    chain = human.aggregate_chain(plan_dir)
    for record, entry in zip(records, entries):
        proposal_path = proposal_dir / f"proposal-{entry['proposal_stem']}.json"
        human.assert_decision_current(
            record, loader.load_for_adjudication(proposal_path, engine), chain)

    written: list[dict[str, Any]] = []
    if not dry_run:
        for record in records:
            path = plan_dir / f"kg-stage2-s2-decision-{record['decision_id']}.json"
            digest = _write(path, record)
            record["digest"] = digest
            record["path"] = path.name
            written.append({"decision_id": record["decision_id"], "path": path.name,
                            "digest": digest})

    current_plan_path, plan, plan_digest = _current_plan(plan_dir)
    current_agg_path, aggregate, aggregate_digest = _current_aggregate(plan_dir, plan_digest)

    rebound = human.bind_aggregate(
        aggregate, records,
        created_at=moment,
        supersedes={"path": current_agg_path.name, "digest": aggregate_digest},
        supersedes_reason=("rebound to index the human decisions of "
                           f"{adjudicator} at {moment}"),
    )
    if dry_run:
        return {"decisions": written or [r["decision_id"] for r in records],
                "aggregate_counts": rebound["counts"], "dry_run": True}

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    agg_path = plan_dir / f"kg-stage2-s2-ai-proposals-{stamp}.json"
    agg_digest = _write(agg_path, rebound)

    plan_id, created_at = documents.plan_identity(datetime.now(timezone.utc))
    new_plan = documents.build_plan(
        plan_id=plan_id, created_at=created_at,
        supersedes=documents.supersede_reference(current_plan_path),
    )
    plan_path = plan_dir / f"kg-stage2-s2-plan-{plan_id}.json"
    plan_digest_new = _write(plan_path, new_plan)

    return {
        "decisions": written,
        "aggregate": {"path": agg_path.name, "digest": agg_digest},
        "plan": {"path": plan_path.name, "digest": plan_digest_new,
                 "ids": sorted(new_plan.get("code_hashes", {}))},
        "aggregate_counts": rebound["counts"],
        "dry_run": False,
    }


def _current_plan(plan_dir: Path):
    from scripts.kg import stage2_s2_ai_lineage as lineage
    return lineage.current_plan(plan_dir)


def _current_aggregate(plan_dir: Path, plan_digest: str):
    from scripts.kg import stage2_s2_ai_lineage as lineage
    return lineage.current_aggregate(plan_dir, plan_digest)


def main(argv: Sequence[str] | None = None) -> int:  # pragma: no cover - operator path
    parser = argparse.ArgumentParser(description="Record Stage 2 human adjudication.")
    parser.add_argument("request", help="decision request JSON")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    request = json.loads(Path(args.request).read_text())
    result = record_decisions(request, engine=get_engine(), dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
