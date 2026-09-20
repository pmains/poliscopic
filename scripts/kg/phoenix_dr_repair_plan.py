#!/usr/bin/env python3
"""``phoenix-dr`` repair plan — CLI facade.

Thin entry point that wires the evidence layer
(:mod:`scripts.kg.phoenix_dr_adjudication`) to the plan-body assembly
(:mod:`scripts.kg.phoenix_dr_plan_body`).

It owns three things and nothing else: target-safety assertion, independent
digest verification of a saved plan, and the command line.  All adjudication,
fingerprinting and plan construction live in the two modules above.

Usage:
    .venv/bin/python scripts/kg/phoenix_dr_repair_plan.py --out <path>
    .venv/bin/python scripts/kg/phoenix_dr_repair_plan.py --verify <path>

The generator is read-only.  Applying a plan is a separate, explicitly approved
action and is not implemented here.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_SCRIPTS = str(Path(__file__).resolve().parents[2])
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from scripts.kg.phoenix_dr_adjudication import (  # noqa: E402
    BODY_CODE,
    JURISDICTION_SLUG,
    PROPOSED_BODY_NAME,
    PROPOSED_BODY_SLUG,
    PROPOSED_BODY_TYPE,
    REGISTRY_EVIDENCE,
    Disposition,
    PlanError,
    adjudicate,
    assert_development_target,
    canonical_json,
    collect_dispositions,
    fetch_rows,
    fingerprint,
    resolve_jurisdiction,
)
from scripts.kg.phoenix_dr_plan_body import (  # noqa: E402
    build_plan,
    content_hash_disposition,
    deferred_populations,
    plan_digest,
    validate_plan,
)

__all__ = [
    "BODY_CODE",
    "JURISDICTION_SLUG",
    "PROPOSED_BODY_NAME",
    "PROPOSED_BODY_SLUG",
    "PROPOSED_BODY_TYPE",
    "REGISTRY_EVIDENCE",
    "Disposition",
    "PlanError",
    "adjudicate",
    "assert_development_target",
    "build_plan",
    "canonical_json",
    "collect_dispositions",
    "content_hash_disposition",
    "deferred_populations",
    "fetch_rows",
    "fingerprint",
    "plan_digest",
    "resolve_jurisdiction",
    "validate_plan",
    "verify_plan_file",
]


def verify_plan_file(path: str) -> tuple[bool, str]:
    """Recompute a saved plan's digest independently of its generation."""
    with open(path, encoding="utf-8") as handle:
        plan = json.load(handle)
    recomputed = plan_digest(plan)
    return recomputed == plan["digest"]["value"], recomputed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate the phoenix-dr repair plan (read-only, development only)"
    )
    parser.add_argument("--out", default=None, help="write the plan JSON here")
    parser.add_argument("--verify", default=None, help="verify a saved plan file and exit")
    args = parser.parse_args(argv)

    if args.verify:
        ok, digest = verify_plan_file(args.verify)
        print(f"verify={ok} digest={digest}")
        return 0 if ok else 1

    from db import get_engine
    from scripts.entities.detect_entities import _integrity_snapshot

    plan: Mapping[str, Any] = build_plan(
        get_engine(), integrity_provider=lambda engine: _integrity_snapshot(engine)
    )
    rendered = json.dumps(plan, indent=2, sort_keys=True, default=str)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
        print(f"[written] {args.out}")
    print(f"digest={plan['digest']['value']}")
    print(
        f"included={plan['reconciliation']['included']} "
        f"excluded={plan['reconciliation']['excluded']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
