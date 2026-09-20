#!/usr/bin/env python3
"""``stage2_scratch_reparse.py`` — fail-closed scratch re-parse verification.

The six Buckeye fixtures prove the parser *should* read each packet a certain
way.  They do not prove the parser reads the *stored* packets that way, because
the fixtures are derived.  Closing that gap means re-parsing the real packets —
which is a write, so it must happen somewhere that is not ``poliscopic_dev``.

This module builds the plan for that work and provides the runner.  It is
deliberately incapable of doing the work by itself:

* **plan mode** writes nothing and needs no database;
* **execution** requires an authorization record whose digest equals the plan's
  digest, and refuses a target that is not a scratch database;
* the plan itself is refused if the target is not the development tier, or if the
  protected-backup requirement is not recorded, or if any bound file has drifted.

No function here creates a database, and nothing here is called by the test
suite except through dry/plan paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "AUTHORIZATION_PHRASE",
    "ScratchReparseRefused",
    "build_plan",
    "code_hashes",
    "execute",
    "validate_plan",
]

PLAN_KIND = "kg-stage2-scratch-reparse-plan"
PLAN_VERSION = "kg-stage2-scratch-reparse/1.0"

#: Execution needs this exact phrase *and* the plan digest.  One without the
#: other is a mistake, not an authorization.
AUTHORIZATION_PHRASE = "I authorize the scratch re-parse for this plan digest"

#: Modules whose behaviour the re-parse result depends on.
CODE_MODULES = (
    "scripts/scraper/platforms/granicus_agenda_blocks.py",
    "scripts/scraper/jurisdictions/buckeye_agenda_parse.py",
    "scripts/scraper/jurisdictions/buckeye_granicus.py",
    "scripts/kg/stage2_s2_gap_reconcile.py",
)

FIXTURE_DIR = REPO / "tests" / "fixtures" / "buckeye"
EXPECTED_NAME = "expected.json"

#: The development tier's database: a read-only witness for this run.
DEV_DATABASE = "poliscopic_dev"

#: Databases this run must never execute against: the development target, the
#: other local databases, and the cluster's own catalogues.
FORBIDDEN_DATABASES = frozenset({
    "poliscopic_dev", "poliscopic", "poliscopic_prod", "production",
    "postgres", "template0", "template1",
})
DEV_HOST = os.environ.get("POLISCOPIC_DEV_DB_HOST", "192.0.2.10")
DEV_PORT = 5432


class ScratchReparseRefused(RuntimeError):
    """The plan or run is not admissible; nothing is created or written."""


@dataclass(frozen=True)
class Authorization:
    """An explicit, separate authorization to execute one plan."""

    plan_digest: str
    phrase: str
    authorized_by: str
    authorized_at: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def code_hashes() -> dict[str, str]:
    """SHA-256 of every module the re-parse result depends on."""
    return {relative: _sha256(REPO / relative)
            for relative in CODE_MODULES if (REPO / relative).exists()}


def _expected(fixture_dir: Path) -> dict[str, Any]:
    path = fixture_dir / EXPECTED_NAME
    if not path.exists():
        raise ScratchReparseRefused(f"no {EXPECTED_NAME} in {fixture_dir}")
    return json.loads(path.read_text())


def _provenance_for(name: str, case: Mapping[str, Any],
                    provenance: Mapping[str, Any]) -> dict[str, Any]:
    """Resolve a case to its `_provenance` entry.

    The provenance map is keyed by the Granicus meeting id, or by a short label
    for the workshop and non-PZ cases, while the fixture file names are longer
    (``granicus_423_cc``, ``workshop_cc``).  Splitting on the last token yields
    ``cc`` and silently binds nothing, so match on the Granicus id first and
    then on the leading tokens.
    """
    granicus = str(case.get("granicus_meeting_id") or "")
    if granicus and granicus in provenance:
        return dict(provenance[granicus])
    tokens = name.split("_")
    for key in (tokens[0], "_".join(tokens[:2]), tokens[1] if len(tokens) > 1 else ""):
        if key and key in provenance:
            return dict(provenance[key])
    return {}


def build_plan(
    *,
    target: Mapping[str, Any],
    fixture_dir: Path = FIXTURE_DIR,
    protected_backup: Mapping[str, Any],
    created_at: str | None = None,
    plan_id: str | None = None,
) -> dict[str, Any]:
    """Build the scratch re-parse plan. Creates nothing, connects to nothing."""
    expected = _expected(fixture_dir)
    provenance = expected.get("_provenance") or {}

    cases = []
    for name in sorted(n for n in expected if not n.startswith("_")):
        fixture = fixture_dir / f"{name}.txt"
        if not fixture.exists():
            raise ScratchReparseRefused(f"fixture {fixture} is missing")
        case = expected[name]
        body = case["body"]
        case_provenance = _provenance_for(name, case, provenance)
        cases.append({
            "fixture": fixture.name,
            "fixture_sha256": _sha256(fixture),
            "body": body,
            "granicus_meeting_id": case.get("granicus_meeting_id"),
            "db_meeting_id": case.get("db_meeting_id") or case_provenance.get("db_meeting_id"),
            "meeting_type": case.get("meeting_type") or case_provenance.get("type"),
            "expected_items": dict(case["items"]),
            "must_not_contain": list(case["must_not_contain"]),
            "expected_holds": list(case.get("held", [])),
            "provenance": dict(case_provenance),
        })
    if not cases:
        raise ScratchReparseRefused("no fixture cases to plan")

    moment = created_at or datetime.now(timezone.utc).isoformat()
    stamp = datetime.fromisoformat(moment.replace("Z", "+00:00")) if moment else None
    identity = plan_id or "scratch-reparse-" + (
        stamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") if stamp else "unknown")

    plan = {
        "kind": PLAN_KIND,
        "version": PLAN_VERSION,
        "plan_id": identity,
        "created_at": moment,
        "algorithm": "re-parse each stored Buckeye packet with the corrected "
                     "block parser, in a scratch database, and diff against the "
                     "stored agenda_items",
        "target": {
            "dialect": target.get("dialect"),
            "host": target.get("host"),
            "port": target.get("port"),
            "database": target.get("database"),
        },
        "development_target": {
            "dialect": "postgresql", "host": DEV_HOST, "port": DEV_PORT,
            "database": DEV_DATABASE,
        },
        "protected_backup": dict(protected_backup),
        "code_hashes": code_hashes(),
        "fixtures": cases,
        "counts": {"cases": len(cases),
                   "expected_items": sum(len(c["expected_items"]) for c in cases),
                   "expected_holds": sum(len(c["expected_holds"]) for c in cases)},
        "zero_mutation": {
            "database": DEV_DATABASE,
            "statement": "poliscopic_dev must be read-only for the whole run: "
                         "zero inserts, zero updates, zero deletes, zero DDL",
            "verification": "compare protected counts and integrity metrics "
                            "before and after; any delta fails the run",
        },
        "creates_database": False,
        "executed": False,
        "authorization_required": True,
    }
    problems = validate_plan(plan, fixture_dir=fixture_dir)
    if problems:
        raise ScratchReparseRefused("; ".join(problems[:5]))
    return plan


def validate_plan(
    plan: Mapping[str, Any], *, fixture_dir: Path = FIXTURE_DIR,
    target: Mapping[str, Any] | None = None,
) -> list[str]:
    """Every condition the plan must satisfy before it may even be stored."""
    problems: list[str] = []
    if plan.get("kind") != PLAN_KIND:
        problems.append(f"kind must be {PLAN_KIND!r}")
    if plan.get("creates_database") is not False:
        problems.append("a plan may not create a database")
    if plan.get("executed") is not False:
        problems.append("a plan must record executed=false")

    backup = plan.get("protected_backup") or {}
    if backup.get("required") is not True:
        problems.append("the protected-backup requirement must be recorded")
    if not backup.get("receipt_path"):
        problems.append("the protected-backup requirement names no receipt")
    if backup.get("verified_before_execution") is not True:
        problems.append("the backup must be verified before execution")

    recorded_target = plan.get("target") or {}
    database = str(recorded_target.get("database") or "")
    if not database:
        problems.append("the plan records no target database")
    if database == DEV_DATABASE:
        problems.append("the plan must not target poliscopic_dev: it is a read-only "
                        "witness for this run, not a destination")
    if recorded_target.get("dialect") not in (None, "postgresql"):
        problems.append("the scratch target must be PostgreSQL")
    if target is not None and dict(recorded_target) != dict(target):
        problems.append("the plan target does not match the supplied target")

    hashes = plan.get("code_hashes") or {}
    if not hashes:
        problems.append("the plan binds no code hashes")
    for relative, recorded in hashes.items():
        path = REPO / relative
        if not path.exists():
            problems.append(f"bound module {relative} is missing")
        elif _sha256(path) != recorded:
            problems.append(f"bound module {relative} has drifted since the plan")

    cases = plan.get("fixtures") or []
    if not cases:
        problems.append("the plan binds no fixtures")
    for case in cases:
        fixture = fixture_dir / str(case.get("fixture") or "")
        if not fixture.exists():
            problems.append(f"fixture {case.get('fixture')!r} is missing")
        elif _sha256(fixture) != case.get("fixture_sha256"):
            problems.append(f"fixture {case.get('fixture')!r} has drifted")
        if not case.get("db_meeting_id"):
            problems.append(f"case {case.get('fixture')!r} binds no source meeting")
        if not case.get("expected_items"):
            problems.append(f"case {case.get('fixture')!r} binds no expected items")

    if plan.get("counts") and cases and plan["counts"].get("cases") != len(cases):
        problems.append("the plan's case count does not match its fixtures")
    return problems


def execute(
    plan: Mapping[str, Any],
    authorization: Authorization | None,
    *,
    engine: Any = None,
    fixture_dir: Path = FIXTURE_DIR,
) -> dict[str, Any]:
    """Refuse unless this exact plan was separately authorized.

    There is no default that proceeds.  Without an :class:`Authorization` whose
    digest and phrase both match, this raises before touching anything.
    """
    if authorization is None:
        raise ScratchReparseRefused(
            "execution requires an explicit authorization; none was supplied")
    digest = artifacts.recorded_digest(plan) if "digest" in plan else _plan_digest(plan)
    if authorization.plan_digest != digest:
        raise ScratchReparseRefused("authorization is for a different plan digest")
    if authorization.phrase != AUTHORIZATION_PHRASE:
        raise ScratchReparseRefused("authorization phrase does not match")
    if not authorization.authorized_by.strip():
        raise ScratchReparseRefused("authorization names no authorizer")

    problems = validate_plan(plan, fixture_dir=fixture_dir)
    if problems:
        raise ScratchReparseRefused("; ".join(problems[:5]))

    if engine is None:
        raise ScratchReparseRefused(
            "no engine supplied: creating the scratch database is a separate, "
            "unauthorized act and this runner will not do it")

    database = str(engine.url.database or "")
    if database in FORBIDDEN_DATABASES:
        raise ScratchReparseRefused(
            f"refusing to execute against {database!r}: this run may only read a "
            f"scratch database")

    from sqlalchemy import text  # local: the runner stays engine-agnostic until here

    results = []
    for case in plan["fixtures"]:
        fixture = fixture_dir / str(case["fixture"])
        parsed, holds = _parse_fixture(fixture, case)
        with engine.connect() as connection:
            before = list(connection.execute(text(
                "SELECT agenda_item_number, agenda_item_title FROM agenda_items "
                "WHERE meeting_db_id = :m ORDER BY sort_order, id"),
                {"m": int(case["db_meeting_id"])}).mappings())
        stored = {str(r["agenda_item_number"]) for r in before}
        expected = {str(k) for k in case["expected_items"]}
        observed = set(parsed)
        missing = sorted(expected - observed)
        unexpected = sorted(observed - expected)
        forbidden = {token: (token not in observed)
                     for token in case.get("must_not_contain", [])}
        results.append({
            "fixture": case["fixture"],
            "db_meeting_id": case["db_meeting_id"],
            "body": case["body"],
            "stored_before": sorted(stored),
            "stored_before_titles": {str(r["agenda_item_number"]):
                                     str(r["agenda_item_title"] or "") for r in before},
            "observed_after": sorted(observed),
            "expected": sorted(expected),
            "missing_expected": missing,
            "unexpected_observed": unexpected,
            "titles": {k: parsed[k] for k in sorted(parsed)},
            "must_not_contain": forbidden,
            "holds": [h["agenda_item_number"] for h in holds],
            "passed": not missing and not unexpected and all(forbidden.values()),
        })

    return {
        "plan_id": plan.get("plan_id"),
        "plan_digest": digest,
        "scratch_database": database,
        "authorized_by": authorization.authorized_by,
        "authorized_at": authorization.authorized_at,
        "cases": results,
        "counts": {
            "cases": len(results),
            "passed": sum(1 for r in results if r["passed"]),
            "failed": sum(1 for r in results if not r["passed"]),
            "expected_items": sum(len(r["expected"]) for r in results),
            "observed_items": sum(len(r["observed_after"]) for r in results),
        },
        "writes_performed": 0,
    }


def _parse_fixture(fixture: Path, case: Mapping[str, Any]) -> tuple[dict[str, str], list[dict]]:
    """Run the corrected block parser over one fixture's packet text.

    ``buckeye_agenda_parse`` advertises ``parse_agenda_blocks`` in its
    ``__all__`` but never defines it, and that module is bound into the plan's
    code_hashes, so it must not be edited while this plan is the authorization.
    ``PacketState`` is used directly instead; it is bound and unchanged.
    """
    from scraper.platforms import granicus_agenda_blocks as blocks

    state = blocks.PacketState(str(case.get("fixture")))
    for line in fixture.read_text().split("\n"):
        state.feed(line)
    items = {item["agenda_item_number"]: item["agenda_item_title"]
             for item in state.items if item["item_type_category"] == "item"}
    return items, list(state.held)


def _plan_digest(plan: Mapping[str, Any]) -> str:
    body = {k: v for k, v in plan.items() if k not in ("digest", "created_at")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
