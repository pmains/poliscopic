#!/usr/bin/env python3
"""Regenerated standing authorization PROPOSAL for the daily newsletter sync.

The daily publish step pushes editorial tables to production via
``scripts/editorial_sync.py`` under an OP-RECON standing authorization. That
authorization was repeatedly voided when unrelated shared infrastructure changed
(``CODE_CHANGED``), including the 2026-10-08 failure caused solely by an edit to
``scripts/ops/production_interlock.py``.

This module builds the REPLACEMENT proposal: same operation, same entry point, same
four-table scope, same validity and max-use policy, every prior prohibition intact,
plus an explicit ``mode="upsert"`` and a hash of the production writer itself.
Shared interlock, orchestration, and development-publication files are deliberately
not bound: their deployment is independently guarded, and changes to them do not
change what this narrow authorization permits. It is a *proposal* only — it cannot
authorize anything, and the previous approval is not inherited.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[2]
for _candidate in (str(REPO_ROOT), str(REPO_ROOT / "scripts")):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

from scripts.ops.standing_authorization_proposal import (  # noqa: E402
    PROHIBITED_ACTIONS,
    PROPOSAL_KIND,
    SCHEMA_PROPOSAL,
    code_hashes,
    proposal_digest,
    write_proposal,
)

OPERATION = "OP-RECON"
#: Explicit sentinel: a proposal never carries an authorization.
NOT_AN_AUTHORIZATION = None
ENTRY_POINT = "scripts/editorial_sync.py"
TARGET = "production"
EXECUTION_MODE = "routine-upsert-only-daily-editorial-sync"
#: Explicit and bound: this authority cannot satisfy a delete, schema, repair,
#: deploy, restart, scheduler or alert request.
SYNC_MODE = "upsert"
SCOPE = ["article_sources", "article_tags", "articles", "tags"]
VALIDITY_DAYS = 90
MAX_USES = 150  # the policy of the superseded authorization; preserved
ROLLBACK_OWNER = "Pete Mains"

#: Bind the code that possesses the production-write capability, not unrelated
#: callers or shared enforcement infrastructure. Scope, target, and mode remain
#: digest-bound in the plan, and changes to this writer still void authorization.
CODE_PATHS = ("scripts/editorial_sync.py",)

PERMITTED_ACTIONS = (
    "upsert of the four editorial tables (articles, article_sources, article_tags, "
    "tags) from development into production, and only while every mandatory gate "
    "passes",
)

GATES = (
    ("verify_approved", "The run's verify step approved the newsletter article."),
    ("dev_article_created",
     "The development article exists (creation is idempotent by slug)."),
    ("mode_match", "The request declares execution mode 'upsert'."),
    ("scope_match", "The request declares exactly the four bound editorial tables."),
    ("code_match", "Every bound code hash matches the authorization."),
    ("window_ok", "The request is inside the authorization window."),
    ("uses_available", "The use budget has not been exhausted."),
    ("no_deletes", "No delete/reconcile behaviour: editorial sync is upsert-only "
                   "and never deletes production rows."),
    ("terminal_receipt", "One terminal receipt per attempt; a refusal stops the "
                         "publish step and fails the workflow."),
)
GATE_IDS = tuple(identifier for identifier, _ in GATES)


def build_proposal(*, now: datetime | None = None) -> dict[str, Any]:
    """Build the non-executable replacement proposal."""
    now = now or datetime.now(timezone.utc)
    proposal: dict[str, Any] = {
        "schema": SCHEMA_PROPOSAL,
        "kind": PROPOSAL_KIND,
        # ── by construction: not an authorization ──
        "executable": False,
        "approver": None,
        "authorization": NOT_AN_AUTHORIZATION,
        "requires_human_approval": True,
        "recorded_by": "human approval via scripts/ops/operation_authorization.py",
        # ── envelope ──
        "operation": OPERATION,
        "entry_point": ENTRY_POINT,
        "target": TARGET,
        "execution_mode": EXECUTION_MODE,
        "mode": SYNC_MODE,
        "validity_days": VALIDITY_DAYS,
        "max_uses": MAX_USES,
        "not_before": now.isoformat(),
        "not_after": (now + timedelta(days=VALIDITY_DAYS)).isoformat(),
        "scope": list(SCOPE),
        "scope_source": ("the four editorial tables the superseded plan bound; "
                         "unchanged"),
        "code_hashes": code_hashes(list(CODE_PATHS)),
        "code_binding_policy": {
            "bound": list(CODE_PATHS),
            "reason": ("bind only the production editorial writer; shared "
                       "interlock and orchestration changes are separately "
                       "deployment-controlled and must not revoke this approval"),
            "writer_change_effect": "authorization becomes invalid (CODE_CHANGED)",
            "unrelated_change_effect": "authorization remains valid",
        },
        "rollback_owner": ROLLBACK_OWNER,
        "permitted_actions": list(PERMITTED_ACTIONS),
        "prohibited_actions": list(PROHIBITED_ACTIONS),
        "prohibited_modes": ["reconcile", "reconcile-only", "schema-only",
                             "bootstrap-schema", "repair", "cleanup", "backfill",
                             "schema"],
        "mandatory_gates": [{"id": identifier, "requirement": text}
                            for identifier, text in GATES],
        "required_gate_ids": list(GATE_IDS),
        "use_accounting": {
            "consumed_only_after": ("a successful, reconciled terminal receipt"),
            "failures_and_refusals": ("do not consume a use and do not reset the "
                                      "count"),
            "known_limit": ("a read-only interlock CHECK of an allowed request also "
                            "records a use; checks should be counted deliberately"),
        },
        "supersedes": {
            "plan_digest": "0953220a2d6e16b10f47c80209eade75694c72c7a1db086bc0cc60d3903ff7a9",
            "authorization_digest":
                "523a2359ea4e7e79655f3b8c18c0ff321b3b5cacd7e70088ac9388d7196095b4",
            "archived_at": "data/release-superseded/"
                           "OP-RECON-newsletter-daily-20260921-0953220a",
            "inherits_approval": False,
        },
        "summary": (
            f"Restore the daily newsletter's production push: permit upsert of "
            f"{len(SCOPE)} editorial tables ({', '.join(sorted(SCOPE))}) from "
            f"development to production via {ENTRY_POINT} for up to "
            f"{VALIDITY_DAYS} days and {MAX_USES} successful runs, and only while "
            f"every gate passes. The authorized execution mode is '{SYNC_MODE}' — "
            "this authority cannot satisfy a reconcile, reconcile-only, schema-only, "
            "bootstrap-schema, repair, deploy, restart, scheduler or alert request. "
            "It does not permit deletes, schema changes, deployment, restarts, "
            "scheduler changes or alert changes."
        ),
    }
    proposal["digest"] = proposal_digest(proposal)
    return proposal


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build the regenerated NON-EXECUTABLE newsletter editorial "
                    "authorization proposal. Authorizes nothing.")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()

    proposal = build_proposal()
    output = arguments.output or (REPO_ROOT / "data" / "standing-proposals" /
                                  "OP-RECON-newsletter-editorial-sync.proposal.json")
    if output.exists():
        print(json.dumps({"refused": "proposal already exists", "path": str(output)},
                         indent=2))
        return 3
    write_proposal(proposal, output)
    print(json.dumps({
        "kind": proposal["kind"],
        "executable": proposal["executable"],
        "operation": proposal["operation"],
        "entry_point": proposal["entry_point"],
        "mode": proposal["mode"],
        "scope": proposal["scope"],
        "validity_days": proposal["validity_days"],
        "max_uses": proposal["max_uses"],
        "gates": len(proposal["mandatory_gates"]),
        "code_bound": sorted(proposal["code_hashes"]),
        "digest": proposal["digest"],
        "path": str(output),
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
