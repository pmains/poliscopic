#!/usr/bin/env python3
"""``stage2_s1_verify.py`` — pre- and post-condition checks for Stage 2 S1.

Verification is deliberately its own module.  A runner that both performs a
mutation and decides whether it succeeded can rationalise its own work; keeping
the checks here means the apply runner consumes a verdict it did not author.

Every function returns a list of human-readable problems.  An empty list is a
pass.  No function here writes anything.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from urllib.parse import urlsplit  # noqa: E402

from sqlalchemy import bindparam, text  # noqa: E402

from scripts.kg import stage2_parentage_contract as parentage  # noqa: E402
from scripts.kg import stage2_schema_readiness as schema_readiness  # noqa: E402
from scripts.kg import stage2_s1_policy as policy  # noqa: E402

__all__ = [
    "TARGET_FIELDS",
    "assignment_ids",
    "engine_identity",
    "normalize_target",
    "fetch_meetings",
    "hold_ids",
    "verify_holds_unchanged",
    "verify_plan_shape",
    "verify_postconditions",
    "verify_preconditions",
    "verify_readiness_binding",
    "verify_target_binding",
    "verify_schema_readiness_binding",
    "verify_schema_ready",
    "verify_scope",
    "verify_target_bodies",
]


def assignment_ids(plan: Mapping[str, Any]) -> list[int]:
    """Sorted meeting ids the plan intends to write."""
    return sorted(int(e["meeting_db_id"]) for e in plan.get("assignments", ()))


def hold_ids(plan: Mapping[str, Any]) -> list[int]:
    """Sorted meeting ids the plan intends to leave alone."""
    return sorted(int(e["meeting_db_id"]) for e in plan.get("holds", ()))


def fetch_meetings(connection: Any, ids: Sequence[int]) -> dict[int, Mapping[str, Any]]:
    """Read the current state of the named meetings."""
    if not ids:
        return {}
    statement = text(
        "SELECT id, body, meeting_id, meeting_date, jurisdiction_id, public_body_id "
        "FROM meetings WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    rows = connection.execute(statement, {"ids": list(ids)}).fetchall()
    return {
        int(r[0]): {
            "id": int(r[0]),
            "body": r[1],
            "meeting_id": r[2],
            "meeting_date": r[3],
            "jurisdiction_id": r[4],
            "public_body_id": r[5],
        }
        for r in rows
    }


#: The identity dimensions an apply must bind exactly.
TARGET_FIELDS = ("dialect", "host", "port", "database")


def engine_identity(engine: Any) -> dict[str, Any]:
    """The live engine's comparable target identity.

    ``str(engine.url)`` masks the password, and only parsed identity fields are
    kept, so nothing credential-shaped can reach a receipt or a log.
    """
    parts = urlsplit(str(engine.url))
    return {
        "dialect": (engine.dialect.name or "").strip().lower() or None,
        "host": (parts.hostname or "").strip().lower() or None,
        "port": int(parts.port) if parts.port is not None else None,
        "database": (parts.path or "").lstrip("/").strip().lower() or None,
    }


def normalize_target(mapping: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a recorded target so comparison is unambiguous.

    Normalization is deliberately shallow — case and surrounding whitespace only.
    It must never make two different targets look alike: a blank or missing value
    stays ``None`` and therefore cannot equal a real host, port or database.
    """
    data = mapping or {}
    dialect, host, port, database = (data.get("dialect"), data.get("host"),
                                     data.get("port"), data.get("database"))
    return {
        "dialect": (str(dialect).strip().lower() or None) if dialect else None,
        "host": (str(host).strip().lower() or None) if host else None,
        "port": int(port) if port is not None and str(port).strip() != "" else None,
        "database": (str(database).strip().lower() or None) if database else None,
    }


def verify_target_binding(
    plan: Mapping[str, Any], engine: Any, receipt: Mapping[str, Any]
) -> list[str]:
    """Require plan target, live engine and backup receipt to agree exactly.

    Being a development-class engine is not sufficient: the plan must have been
    written for *this* engine, and the backup must have been taken from *this*
    server.  ``dialect``, ``host``, ``port`` and ``database`` are all mandatory on
    the receipt and must equal the live engine after normalization.  A different
    development database with an identical schema, a drifted host or port, or a
    receipt that omits any of the four, is refused.
    """
    problems: list[str] = []
    live = engine_identity(engine)
    planned = normalize_target(plan.get("target"))
    recorded = normalize_target((receipt or {}).get("target"))

    if not any(recorded.get(f) is not None for f in TARGET_FIELDS):
        return ["backup receipt carries no target identity to bind"]

    for field in TARGET_FIELDS:
        if planned[field] != live[field]:
            problems.append(
                f"plan target {field}={planned[field]!r} != live engine {field}={live[field]!r}"
            )

    # All four identity fields are mandatory, not optional enrichment: a
    # receipt that omits dialect or port cannot be shown to describe this
    # server, so it must not authorise a write against it.
    for field in TARGET_FIELDS:
        supplied = recorded[field]
        expected = live[field]
        if supplied is None:
            if expected is not None:
                problems.append(f"backup receipt does not name the live {field}")
            continue
        if supplied != expected:
            problems.append(
                f"backup receipt {field}={supplied!r} != live engine {field}={expected!r}"
            )
    return problems


def verify_readiness_binding(plan: Mapping[str, Any]) -> list[str]:
    """Refuse a plan whose sync/parity readiness evidence has since drifted.

    The plan binds the parentage readiness contract and the hashes of the
    modules that implement it.  If either moved after review, the reviewed plan
    no longer describes the behaviour that would run, so the apply must stop.
    """
    problems: list[str] = []
    bound = plan.get("sync_parity_readiness")
    if not isinstance(bound, Mapping):
        return ["plan records no sync/parity readiness binding"]

    if bound.get("contract") != parentage.contract_snapshot():
        problems.append("parentage readiness contract drifted since the plan was written")

    live_hashes = parentage.code_hashes()
    bound_hashes = bound.get("code_hashes") or {}
    for module, digest in sorted(live_hashes.items()):
        if bound_hashes.get(module) != digest:
            problems.append(f"bound module drifted: {module}")

    if bound.get("readiness_digest") != parentage.readiness_digest():
        problems.append("readiness digest drifted")
    return problems


def verify_schema_readiness_binding(plan: Mapping[str, Any]) -> list[str]:
    """Refuse a plan whose schema readiness evidence has since drifted."""
    problems: list[str] = []
    bound = plan.get("schema_readiness")
    if not isinstance(bound, Mapping):
        return ["plan records no schema readiness binding"]
    if bound.get("contract") != schema_readiness.contract_snapshot():
        problems.append("schema readiness contract drifted since the plan was written")
    live_hashes = schema_readiness.code_hashes()
    bound_hashes = bound.get("code_hashes") or {}
    for module, digest in sorted(live_hashes.items()):
        if bound_hashes.get(module) != digest:
            problems.append(f"schema readiness module drifted: {module}")
    if bound.get("readiness_digest") != schema_readiness.readiness_digest():
        problems.append("schema readiness digest drifted")
    return problems


def verify_schema_ready(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """Refuse a data apply unless the target schema is actually ready.

    Evaluated against the transaction's own connection, so the schema inspected
    is the schema the writes would run against.
    """
    if not isinstance(plan.get("schema_readiness"), Mapping):
        return ["plan records no schema readiness binding"]
    observed = schema_readiness.observe(connection)
    if schema_readiness.is_ready(observed):
        return []
    blocking = schema_readiness.blocking_problems(observed)
    remaining = schema_readiness.operations_for(observed)
    detail = blocking[:2] or [op["kind"] for op in remaining][:3]
    return [f"parentage schema is not ready for this target: {detail}"]


def verify_plan_shape(plan: Mapping[str, Any]) -> list[str]:
    """Structural and arithmetic checks that need no database."""
    problems: list[str] = []
    if plan.get("kind") != "kg-stage2-s1-plan":
        problems.append(f"unexpected plan kind {plan.get('kind')!r}")
    if plan.get("algorithm_version") != policy.ALGORITHM_VERSION:
        problems.append(
            f"algorithm version {plan.get('algorithm_version')!r} "
            f"expected {policy.ALGORITHM_VERSION!r}"
        )
    counts = plan.get("counts") or {}
    if not isinstance(counts, Mapping):
        problems.append("counts must be a mapping")
        return problems
    problem = policy.counts_problem({k: int(v) for k, v in counts.items()})
    if problem is not None:
        problems.append(problem)

    problems.extend(verify_readiness_binding(plan))
    problems.extend(verify_schema_readiness_binding(plan))

    assignments = plan.get("assignments") or []
    holds = plan.get("holds") or []
    if len(assignments) != int(counts.get("assignments", -1)):
        problems.append(
            f"{len(assignments)} assignment rows vs counted {counts.get('assignments')}"
        )
    if len(holds) != int(counts.get("holds", -1)):
        problems.append(f"{len(holds)} hold rows vs counted {counts.get('holds')}")

    ids = [int(e["meeting_db_id"]) for e in assignments]
    if len(set(ids)) != len(ids):
        problems.append("duplicate meeting ids among assignments")
    overlap = set(ids) & set(int(e["meeting_db_id"]) for e in holds)
    if overlap:
        problems.append(f"meetings appear in both scope sets: {sorted(overlap)[:5]}")
    for entry in assignments:
        if entry.get("target_public_body_id") is None:
            problems.append(f"assignment {entry.get('meeting_db_id')} has no target body")
    return problems


def verify_target_bodies(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """Every targeted body must exist, and the plan's recorded identity must match."""
    problems: list[str] = []
    recorded = plan.get("target_bodies") or {}
    if not recorded:
        return ["plan records no target bodies"]
    statement = text(
        "SELECT id, name, slug FROM public_bodies WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    rows = connection.execute(
        statement, {"ids": sorted(int(k) for k in recorded)}
    ).fetchall()
    live = {int(r[0]): {"name": r[1], "slug": r[2]} for r in rows}
    for key, expected in recorded.items():
        body_id = int(key)
        actual = live.get(body_id)
        if actual is None:
            problems.append(f"target body {body_id} is absent from the registry")
            continue
        if actual["name"] != expected.get("name") or actual["slug"] != expected.get("slug"):
            problems.append(
                f"target body {body_id} drifted: recorded "
                f"{expected.get('name')!r}/{expected.get('slug')!r}, live "
                f"{actual['name']!r}/{actual['slug']!r}"
            )
    return problems


def verify_preconditions(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """Everything that must hold *before* any write, checked inside the transaction."""
    problems = verify_target_bodies(connection, plan)
    problems.extend(verify_schema_ready(connection, plan))
    ids = assignment_ids(plan) + hold_ids(plan)
    current = fetch_meetings(connection, ids)
    missing = [i for i in ids if i not in current]
    if missing:
        problems.append(f"{len(missing)} planned meetings no longer exist: {missing[:5]}")

    for entry in plan.get("assignments", ()):
        mid = int(entry["meeting_db_id"])
        row = current.get(mid)
        if row is None:
            continue
        if row["public_body_id"] is not None:
            problems.append(
                f"meeting {mid} already has public_body_id={row['public_body_id']}"
            )
        if policy.meeting_fingerprint(row) != entry.get("fingerprint"):
            problems.append(f"meeting {mid} fingerprint drifted")

    for entry in plan.get("holds", ()):
        mid = int(entry["meeting_db_id"])
        row = current.get(mid)
        if row is None:
            continue
        if row["public_body_id"] is not None:
            problems.append(f"held meeting {mid} unexpectedly has a parent")
        if policy.meeting_fingerprint(row) != entry.get("fingerprint"):
            problems.append(f"held meeting {mid} fingerprint drifted")
    return problems


def verify_scope(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """The NULL-parent set must equal the plan's scope exactly — no more, no less."""
    problems: list[str] = []
    rows = connection.execute(
        text("SELECT id FROM meetings WHERE public_body_id IS NULL ORDER BY id")
    ).fetchall()
    live = {int(r[0]) for r in rows}
    planned = set(assignment_ids(plan)) | set(hold_ids(plan))
    extra = sorted(live - planned)
    absent = sorted(planned - live)
    if extra:
        problems.append(f"{len(extra)} NULL-parent meetings are not in the plan: {extra[:5]}")
    if absent:
        problems.append(f"{len(absent)} planned meetings are not NULL-parent: {absent[:5]}")
    return problems


def verify_holds_unchanged(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """Held meetings must still have no parent after the apply."""
    problems: list[str] = []
    current = fetch_meetings(connection, hold_ids(plan))
    for entry in plan.get("holds", ()):
        mid = int(entry["meeting_db_id"])
        row = current.get(mid)
        if row is None:
            problems.append(f"held meeting {mid} disappeared")
        elif row["public_body_id"] is not None:
            problems.append(f"held meeting {mid} gained a parent")
    return problems


def verify_postconditions(connection: Any, plan: Mapping[str, Any]) -> list[str]:
    """Every assignment written exactly once, every hold untouched, totals exact."""
    problems = verify_holds_unchanged(connection, plan)
    entries = plan.get("assignments", ())
    expected = {int(e["meeting_db_id"]): int(e["target_public_body_id"]) for e in entries}
    current = fetch_meetings(connection, sorted(expected))
    wrong = 0
    for mid, target in expected.items():
        row = current.get(mid)
        if row is None:
            problems.append(f"meeting {mid} disappeared")
            continue
        if row["public_body_id"] != target:
            wrong += 1
            if wrong <= 5:
                problems.append(
                    f"meeting {mid} public_body_id={row['public_body_id']} expected {target}"
                )
    counted = connection.execute(
        text(
            "SELECT COUNT(*) FROM meetings WHERE id IN :ids "
            "AND public_body_id IS NOT NULL"
        ).bindparams(bindparam("ids", expanding=True)),
        {"ids": sorted(expected)},
    ).scalar()
    if int(counted or 0) != len(expected):
        problems.append(f"{counted} of {len(expected)} assignments are parented")

    after_null = int(
        connection.execute(
            text("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NULL")
        ).scalar()
        or 0
    )
    want_null = int((plan.get("expected_after_state") or {}).get("public_body_id_null_count", -1))
    if after_null != want_null:
        problems.append(f"NULL-parent count {after_null} expected {want_null}")
    return problems
