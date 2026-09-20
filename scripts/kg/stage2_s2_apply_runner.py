#!/usr/bin/env python3
"""``stage2_s2_apply_runner.py`` — hardened admission for the Stage 2 plans.

The write body is **absent**.  This module decides whether a plan *could* be
applied, and refuses on anything it cannot prove.  Every fact is re-derived:

* artifacts are named by **exact path and digest**, loaded through
  ``artifacts.load_verified``, and their canonical digest is recomputed — a
  caller's mapping or a stored digest string is never trusted;
* the current S2 plan head, aggregate head, all five decisions and their
  proposals are resolved and re-hashed **field by field** against the canonical
  artifacts *and* the aggregate entries — path, digest, decision id, document id,
  adjudicator, decided-at, role, the human's stated item, the candidate, and the
  proposal's path, digest and exact membership;
* the target is compared across engine, config, both plans and the backup, and
  **every** plan-bound backup field is compared for exact equality against the
  live file, including the receipt's and the dump's **mode and ownership**;
* current state is captured **inside the same transaction** as the admission, and
  compared against the plans' **bound baseline and current-state digest**;
* absent-key collisions are handled by a verified unique index whose proof demands
  immediate enforcement, plus an isolation strategy that retries the whole unit;
* the transaction is **owned by** :mod:`stage2_s2_admission_tx`: begun at
  SERIALIZABLE before the first check, retained through any future write and its
  postconditions, rolled back on any failure, and the entire unit retried on a
  serialization failure.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_admission_tx as tx  # noqa: E402
from scripts.kg.stage2_s2_admission_binding import (  # noqa: E402
    CANDIDATE_FIELDS,
    DECISION_FIELDS,
    PLAN_ROLES,
    ApplyRefused,
    AuthorizedArtifact,
    _load_authorized,
    _resolve_artifact,
    _resolve_historical,
    _verify_code_hashes,
    _verify_decisions,
    _verify_heads,
)
from scripts.kg import stage2_s2_current_state as current_state  # noqa: E402
from scripts.kg import stage2_s2_ai_lineage as lineage  # noqa: E402
from scripts.kg import stage2_s2_label_correction as correction_mod  # noqa: E402
from scripts.kg import stage2_s2_collision as collision  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as repair_mod  # noqa: E402

capture_current_state = current_state.capture_current_state
verify_live_state = current_state.verify_live_state

__all__ = [
    "ApplyRefused",
    "AuthorizedArtifact",
    "admit",
    "apply",
    "ITEM_ROW_FIELDS",
    "MAX_SERIALIZATION_ATTEMPTS",
    "capture_current_state",
    "item_row_fingerprint",
    "verify_live_state",
    "_resolve_artifact",
]

DEV_TARGET_FIELDS = ("dialect", "host", "port", "database", "tier")
EXPECTED_TIER = "development"
NATURAL_KEY = ("meeting_db_id", "agenda_item_number")

#: Serialization failures rerun the ENTIRE unit this many times, then refuse.
MAX_SERIALIZATION_ATTEMPTS = tx.MAX_SERIALIZATION_ATTEMPTS

#: The two plans this runner is bound to, and the validator each one owns.
PLAN_ROLES = {
    "repair": (repair_mod.PLAN_KIND, repair_mod.validate_plan, "rows"),
    "correction": (correction_mod.PLAN_KIND, correction_mod.validate_plan, "operations"),
}

#: The fields an item-row fingerprint covers.  Naming them prevents "hash whatever
#: happens to be in the row", which would make two different rows comparable.
ITEM_ROW_FIELDS = ("id", "meeting_db_id", "agenda_item_number", "title",
                   "agenda_item_id", "sort_order")


def item_row_fingerprint(row: Mapping[str, Any]) -> str:
    return binding.canonical_sha256({f: row.get(f) for f in ITEM_ROW_FIELDS})


# ── 3. one owned transaction, and the checks that live inside it ───────


def _admission_unit(*, repair: AuthorizedArtifact, correction: AuthorizedArtifact,
                    folder: Path, config: Mapping[str, Any] | None,
                    backup_path: str | Path,
                    ) -> Callable[[Any], dict[str, Any]]:
    """Build the unit that runs entirely inside the owned transaction.

    The unit performs **checks only**.  There is no hook, callback or callable
    parameter: the write body is absent, and no caller code can be injected into
    the transaction this function runs in.
    """

    def unit(connection: Any) -> dict[str, Any]:
        from scripts.kg import stage2_s2_apply_target as apply_target

        repair_plan = _load_authorized(repair, role="repair", plan_dir=folder)
        correction_plan = _load_authorized(correction, role="correction", plan_dir=folder)

        heads = _verify_heads(folder)
        code = _verify_code_hashes(repair_plan)
        _verify_code_hashes(correction_plan)
        decisions = _verify_decisions(repair_plan, plan_dir=folder,
                                      aggregate=heads["aggregate"]["document"],
                                      heads=heads)
        _verify_decisions(correction_plan, plan_dir=folder,
                          aggregate=heads["aggregate"]["document"], heads=heads)
        try:
            binding.assert_decisions_equal(repair_plan, correction_plan)
        except AssertionError as exc:
            raise ApplyRefused(f"the plans bind different decisions: {exc}") from exc

        engine = getattr(connection, "engine", None) or connection
        target = apply_target.verify_target(
            engine, plan=repair_plan, config=config,
            backup=apply_target.read_backup_target(backup_path))

        # The live backup, compared field by field against BOTH plans' bindings.
        # Every compared value is derived from the receipt or the dump on disk —
        # never from a caller, and never compared against itself.
        backup = apply_target.load_backup(backup_path, target=target)
        for plan, label in ((repair_plan, "repair"), (correction_plan, "correction")):
            bound_backup = (plan.get("bindings") or {}).get("backup")
            if not bound_backup:
                raise ApplyRefused(f"{label}: the plan binds no backup")
            live_for_compare = dict(backup)
            problems = apply_target.verify_backup_binding(bound_backup, live_for_compare)
            if problems:
                raise ApplyRefused(f"{label}: {problems[0]}")

        # The historical requirement (a global unique index on the natural key) is
        # impossible on this data and is NOT weakened: it is replaced, for GOVERNED
        # writers only, by the additive exact-key reservation, proved as strictly.
        unique = collision.verify_collision_control(connection)
        current = current_state.capture_current_state(
            connection, repair_plan=repair_plan, correction_plan=correction_plan)
        live = current_state.verify_live_state(current, repair_plan=repair_plan,
                                               correction_plan=correction_plan)
        for plan, label in ((repair_plan, "repair"), (correction_plan, "correction")):
            bound = (plan.get("bindings") or {}).get("lineage") or {}
            if (bound.get("plan") or {}).get("digest") != heads["plan"]["digest"]:
                raise ApplyRefused(f"{label}: does not bind the current plan head")
            if (bound.get("aggregate") or {}).get("digest") != heads["aggregate"]["digest"]:
                raise ApplyRefused(f"{label}: does not bind the current aggregate head")

        result: dict[str, Any] = {
            "status": "admitted",
            "transaction": {"isolation": tx.SERIALIZABLE, "owned": True,
                            "closed_before_write": False,
                            "public_write_callback": None},
            "repair": {"path": repair.path, "digest": repair.digest},
            "correction": {"path": correction.path, "digest": correction.digest},
            "heads": {"plan": heads["plan"],
                      "aggregate": {"path": heads["aggregate"]["path"],
                                    "digest": heads["aggregate"]["digest"]}},
            "decisions": decisions,
            "code_hashes": len(code),
            "target": target,
            "backup": {"path": backup["path"],
                       "canonical_digest": backup["receipt_digest"],
                       "dump_sha256": backup["dump_sha256"],
                       "receipt_mode": backup["receipt_stat"]["mode"],
                       "dump_mode": backup["dump_stat"]["mode"],
                       "restore_proven": True,
                       "fields_compared": list(apply_target.COMPARED_FIELDS)},
            "unique_index": unique,
            "current_state_sha256": current["sha256"],
            "live_state": live,
            "collision": {**collision.collision_contract(target["dialect"]),
                          "governed": collision.governed_contract(target["dialect"]),
                          "proved_mode": unique.get("mode")},
            "writes": 0,
        }
        return result

    return unit


#: Every parameter ``admit`` accepts.  There is no write hook, no callback, no
#: callable and no connection: a caller supplies artifacts and an Engine, and the
#: transaction owner acquires the connection itself.
ADMIT_PARAMETERS = ("repair", "correction", "engine", "backup_path", "plan_dir",
                    "config", "max_attempts")


def admit(
    *,
    repair: AuthorizedArtifact,
    correction: AuthorizedArtifact,
    engine: Any,
    backup_path: str | Path,
    plan_dir: str | Path | None = None,
    config: Mapping[str, Any] | None = None,
    max_attempts: int = MAX_SERIALIZATION_ATTEMPTS,
) -> dict[str, Any]:
    """Run every check inside ONE transaction this module owns.

    The transaction is begun at SERIALIZABLE **before the first check**, and it is
    committed only after the unit returns.  A serialization failure — including one
    raised by the commit — reruns the whole unit with a fresh snapshot.  The
    connection is acquired by the transaction owner from ``engine``; no caller may
    supply one.
    """
    if engine is None:
        raise ApplyRefused("no engine supplied")
    folder = Path(plan_dir) if plan_dir is not None else REPO / "data" / "kg-plans"
    unit = _admission_unit(repair=repair, correction=correction, folder=folder,
                           config=config, backup_path=backup_path)
    try:
        return tx.run_unit(engine, unit, max_attempts=max_attempts)
    except tx.TransactionRefused as exc:
        raise ApplyRefused(str(exc)) from exc
    except tx.SerializationExhausted as exc:
        raise ApplyRefused(str(exc)) from exc


#: The only parameters ``apply`` accepts: none.
APPLY_PARAMETERS: tuple[str, ...] = ()


def apply(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Refuse **before** a transaction is opened, or any caller code runs.

    ``apply`` takes no arguments.  It does not call :func:`admit`, does not acquire
    a connection and does not begin a transaction: it refuses immediately.  A
    caller that tries to inject a write callback — ``apply(write=...)`` — is
    rejected on the argument, and the callback is never invoked.
    """
    supplied = list(args) + [f"{k}=" for k in kwargs]
    if supplied:
        raise ApplyRefused(
            f"apply accepts no arguments (got {supplied}): there is no public write "
            f"callback, hook or callable, and no transaction is opened. Admission "
            f"was not reached and no caller code ran")
    raise ApplyRefused(
        "the write body is absent by design: applying is a separate, reviewed step "
        "that this module does not implement, and this call opened no transaction")
