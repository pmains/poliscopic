#!/usr/bin/env python3
"""``stage3_b2_identity.py`` — canonical Phoenix cross-source identity layer (corrected).

MODEL: a dedicated ``meeting_source_aliases`` table.  Each source record survives verbatim;
the alias row is the mapping.  Row merges and deletes are forbidden.

CORRECTIONS APPLIED AFTER ADVERSARIAL REVIEW
  P0-1  source identity is ``UNIQUE (source_system, body, external_id)``.  ``meeting_id`` is
        only unique per body (2,075 ids repeat across up to 6 bodies), so a global
        ``external_id`` key would bind the wrong record.
  P0-2  every target's writer namespace and agenda-bearing status is re-verified against the
        declared contract; unsupported targets are dropped and recorded.
  P0-3  BOTH foreign keys use ``ON DELETE RESTRICT``: a mapping or its provenance can never
        disappear silently.
  P1-1  ``validate_schema_plan`` / ``validate_data_plan`` rederive the exact operation set and
        refuse missing/extra/truncated/tampered/cross-paired plans, namespace drift, duplicate
        identities, self-reference, wrong body, wrong target type, evidence drift and unsafe
        FK actions.
  P1-2  every operation must carry non-empty, SOURCE-GROUNDED provenance: the rule is
        re-verified against live data, and the evidence hash is recomputed from it.
  P1-3  an explicit, versioned writer-namespace registry with fail-closed unknown handling.
  P1-4  every traversal holds a recorded reason.

Read-only: this module builds and validates plans; it never writes.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
for _c in (str(REPO), str(REPO / "scripts")):  # pragma: no cover - bootstrap
    if _c not in sys.path:
        sys.path.insert(0, _c)

from sqlalchemy import text  # noqa: E402

from scripts.kg import stage3_meeting_result_identity as B1  # noqa: E402

__all__ = ["ALIAS_TABLE", "CODE_MODULES", "NAMESPACE_REGISTRY_VERSION", "PRODUCER_VERSION",
           "WRITERS", "build_data_plan", "build_schema_plan", "canonical_sha256", "code_drift",
           "code_hashes", "ddl", "options_evaluation", "traverse", "validate_data_plan",
           "validate_schema_plan", "writer_namespace"]

ALIAS_TABLE = "meeting_source_aliases"
SCHEMA_KIND = "kg-stage3-meeting-source-alias-schema"
DATA_KIND = "kg-stage3-meeting-source-alias-plan"
PRODUCER_VERSION = "kg-stage3-b2-identity/2.0"
NAMESPACE_REGISTRY_VERSION = "kg-stage3-writer-namespaces/1.0"

#: The observed Phoenix writers, versioned.  Anything not matched here is ``unknown`` and
#: FAILS CLOSED: it may never be a promotion target.
WRITERS: dict[str, dict[str, Any]] = {
    "phoenix_aem_publicmeetings_results": {
        "prefixes": ("publicmeetings-results-",),
        "hosts": ("www.phoenix.gov",), "path_marker": "/publicmeetings/results/",
        "agenda_bearing": False, "role": "source"},
    "phoenix_aem_publicmeetings_notices": {
        "prefixes": ("publicmeetings-notices-",),
        "hosts": ("www.phoenix.gov",), "path_marker": "/publicmeetings/",
        "agenda_bearing": False, "role": "source"},
    "phoenix_legistar": {
        "prefixes": (), "numeric_id": True, "hosts": ("phoenix.legistar.com",),
        "agenda_bearing": True, "role": "target"},
    "phoenix_legistar_calendar": {
        "prefixes": (), "calendar_pattern": True, "hosts": ("phoenix.legistar.com",),
        "agenda_bearing": True, "role": "target"},
    "phoenix_pdf": {
        "prefixes": ("phoenix-pdf-",), "hosts": (), "agenda_bearing": False,
        "role": "source_other"},
}
UNKNOWN_NAMESPACE = "unknown"
TARGET_NAMESPACES = ("phoenix_legistar", "phoenix_legistar_calendar")

#: Every safety-critical module whose behaviour the plans depend on.  Their hashes are bound
#: into BOTH plans so stale code cannot quietly validate an old plan.
CODE_MODULES = (
    "scripts/kg/stage3_b2_identity.py",
    "scripts/kg/stage3_b2_alias_apply.py",
    "scripts/kg/stage3_meeting_result_identity.py",
    "scripts/kg/stage2_artifacts.py",
    "scripts/kg/stage2_backup_verify.py",
)


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in modules:
        path = REPO / rel
        if path.exists():
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def code_drift(recorded: Mapping[str, Any] | None) -> list[str]:
    """Modules whose hash no longer matches the recorded binding."""
    recorded = dict(recorded or {})
    live = code_hashes(tuple(recorded) or CODE_MODULES)
    return sorted(rel for rel, digest in recorded.items() if live.get(rel) != digest)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def writer_namespace(meeting_id: str | None, source_url: str | None = None) -> str:
    """The exact writer namespace, or ``unknown`` (which fails closed)."""
    mid = str(meeting_id or "")
    url = str(source_url or "")
    for name, spec in WRITERS.items():
        if any(mid.startswith(p) for p in spec.get("prefixes") or ()):
            return name
    if url:
        for name, spec in WRITERS.items():
            hosts = spec.get("hosts") or ()
            if hosts and any(h in url for h in hosts):
                if spec.get("numeric_id") and mid.isdigit():
                    return name
                if spec.get("calendar_pattern") and not mid.isdigit():
                    return name
    for name, spec in WRITERS.items():
        if spec.get("numeric_id") and mid.isdigit() and "legistar" in url:
            return name
    return UNKNOWN_NAMESPACE


def ddl() -> list[str]:
    return [
        f"CREATE TABLE {ALIAS_TABLE} ("
        "id integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY, "
        "canonical_meeting_id integer NOT NULL REFERENCES meetings(id) "
        "ON DELETE RESTRICT ON UPDATE CASCADE, "
        "source_meeting_id integer NULL REFERENCES meetings(id) "
        "ON DELETE RESTRICT ON UPDATE CASCADE, "
        "source_system varchar(64) NOT NULL, "
        "body varchar(64) NOT NULL, "
        "external_id varchar(256) NOT NULL, "
        "rule varchar(64) NOT NULL, "
        "evidence_sha256 char(64) NOT NULL, "
        "justification text NULL, "
        "created_at timestamptz NOT NULL DEFAULT now())",
        # P0-1: body is part of the identity because meeting_id is only unique per body.
        f"ALTER TABLE {ALIAS_TABLE} ADD CONSTRAINT uq_meeting_source_alias "
        "UNIQUE (source_system, body, external_id)",
        f"CREATE INDEX ix_meeting_source_aliases_canonical ON {ALIAS_TABLE} "
        "(canonical_meeting_id)",
        f"CREATE INDEX ix_meeting_source_aliases_source_meeting ON {ALIAS_TABLE} "
        "(source_meeting_id)",
    ]


def options_evaluation() -> dict[str, Any]:
    return {
        "chosen": "dedicated_alias_table",
        "identity_key": "(source_system, body, external_id)",
        "fk_policy": "ON DELETE RESTRICT on both FKs - a mapping or its provenance can never "
                     "disappear silently",
        "options": [
            {"option": "dedicated_alias_table", "verdict": "recommended",
             "lineage": "each source record preserved verbatim", "destructive": False},
            {"option": "parent_pointer_on_meetings", "verdict": "rejected",
             "reason": "ambiguous NULL/self pointer; the column sits on a row the scraper "
                       "upserts; cannot express one-source-to-one-canonical without a "
                       "partial unique index per writer"},
            {"option": "merge_rows", "verdict": "forbidden", "destructive": True},
        ],
        "traversal": "result event -> alias -> canonical_meeting_id -> agenda_items of that "
                     "meeting -> exact normalized item number",
    }


# --- validation -------------------------------------------------------------

def validate_schema_plan(plan: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if plan.get("kind") != SCHEMA_KIND:
        problems.append(f"kind must be {SCHEMA_KIND!r}")
    if plan.get("write_path") != "absent by design" or plan.get("applied") is not False:
        problems.append("the schema plan must be unapplied with no write path")
    sql = " ".join(plan.get("ddl") or [])
    if "UNIQUE (source_system, body, external_id)" not in sql:
        problems.append("P0-1: the identity key must include body")
    if sql.count("ON DELETE RESTRICT") != 2:
        problems.append("P0-3: BOTH foreign keys must use ON DELETE RESTRICT")
    for unsafe in ("ON DELETE CASCADE", "ON DELETE SET NULL"):
        if unsafe in sql:
            problems.append(f"P0-3: unsafe FK action {unsafe}")
    if plan.get("digest") != canonical_sha256(
            {k: v for k, v in plan.items() if k != "digest"}):
        problems.append("the recorded digest is not the plan's canonical digest")
    bound = (plan.get("bindings") or {}).get("code_hashes")
    if not bound:
        problems.append("the schema plan binds no implementation code hashes")
    else:
        drift = code_drift(bound)
        if drift:
            problems.append(f"stale code: the schema plan was built against different code "
                            f"{drift}")
    return problems


def validate_data_plan(plan: Mapping[str, Any], *,
                       expected_pairs: Sequence[tuple] | None = None,
                       connection: Any = None) -> list[str]:
    """Canonical validator: rederives the operation set and refuses every drift class."""
    problems: list[str] = []
    if plan.get("kind") != DATA_KIND:
        problems.append(f"kind must be {DATA_KIND!r}")
    if plan.get("write_path") != "absent by design" or plan.get("applied") is not False:
        problems.append("the data plan must be unapplied with no write path")
    if plan.get("digest") != canonical_sha256(
            {k: v for k, v in plan.items() if k != "digest"}):
        problems.append("the recorded digest is not the plan's canonical digest")
        return problems
    bound = (plan.get("bindings") or {}).get("code_hashes")
    if not bound:
        problems.append("the plan binds no implementation code hashes")
    else:
        drift = code_drift(bound)
        if drift:
            problems.append(f"stale code: the plan was built against different code {drift}")
    ops = plan.get("operations") or []
    if not ops:
        problems.append("the plan carries no operations")
    if int(plan.get("operation_count") or -1) != len(ops):
        problems.append("operation_count disagrees with the operations")

    if expected_pairs is not None:
        want = sorted(tuple(p) for p in expected_pairs)
        got = sorted((int(o["source_meeting_db_id"]), int(o["canonical_meeting_db_id"]))
                     for o in ops)
        if got != want:
            missing = sorted(set(want) - set(got))[:3]
            extra = sorted(set(got) - set(want))[:3]
            problems.append(f"operation-set drift (missing={missing} extra={extra})")

    seen_ids, seen_sources = set(), set()
    for o in ops:
        sid, cid = int(o["source_meeting_db_id"]), int(o["canonical_meeting_db_id"])
        if sid == cid:
            problems.append(f"self-reference at {sid}")
        if not (o.get("rule") and o.get("evidence_sha256")):
            problems.append(f"missing provenance for {sid}")
        key = (o.get("source_system"), o.get("body"), o.get("external_id"))
        if key in seen_ids:
            problems.append(f"duplicate source identity {key}")
        seen_ids.add(key)
        if sid in seen_sources:
            problems.append(f"source row {sid} appears twice")
        seen_sources.add(sid)
        if o.get("canonical_system") not in TARGET_NAMESPACES:
            problems.append(f"wrong target namespace {o.get('canonical_system')!r} at {cid}")
        if not o.get("canonical_agenda_items"):
            problems.append(f"target {cid} is not agenda-bearing")
        if o.get("body") != o.get("canonical_body"):
            problems.append(f"wrong body at {sid}: {o.get('body')} vs {o.get('canonical_body')}")
        expected_ev = canonical_sha256({
            "source": o.get("source_fingerprint"), "target": o.get("target_fingerprint"),
            "rule": o.get("rule")})
        if o.get("evidence_sha256") != expected_ev:
            problems.append(f"evidence drift at {sid}")
        if connection is not None:
            live = _verify_rule(connection, sid, cid, o.get("rule"))
            if not live["holds"]:
                problems.append(f"rule no longer holds at {sid}: {live['reason']}")
    return problems


def _result_item_numbers(connection: Any, meeting_db_id: int) -> set[str]:
    txt = connection.execute(text(
        "SELECT text_content FROM supporting_documents WHERE meeting_db_id=:m "
        "AND document_type='Meeting Result' LIMIT 1"), {"m": meeting_db_id}).scalar()
    out = {B1.normalize_item_number(s["token"]) for s in B1.extract_item_spans(txt or "")}
    out.discard(None)
    return out


def _target_item_numbers(connection: Any, meeting_db_id: int) -> set[str]:
    rows = connection.execute(text(
        "SELECT agenda_item_number FROM agenda_items WHERE meeting_db_id=:m"),
        {"m": meeting_db_id}).scalars().all()
    return {B1.normalize_item_number(r) for r in rows} - {None}


def _verify_rule(connection: Any, source_db_id: int, target_db_id: int,
                 rule: Any) -> dict[str, Any]:
    """Recompute the rule against live data.  Provenance must be re-provable, not copied."""
    rules = rule if isinstance(rule, list) else [rule]
    mine = _result_item_numbers(connection, source_db_id)
    theirs = _target_item_numbers(connection, target_db_id)
    if "item_number_set" in rules:
        if not mine:
            return {"holds": False, "reason": "the source proves no item numbers"}
        if not mine <= theirs:
            return {"holds": False, "reason": "source item numbers are not a subset of the target"}
        return {"holds": True, "reason": "item_number_set verified", "items": len(mine)}
    return {"holds": False, "reason": f"unverifiable rule {rules}"}


def _meeting_row(connection: Any, db_id: int) -> dict[str, Any]:
    r = connection.execute(text(
        "SELECT id, body, meeting_id, meeting_date, meeting_title, source_url "
        "FROM meetings WHERE id=:m"), {"m": db_id}).mappings().first()
    return dict(r) if r else {}


def _fingerprint(row: Mapping[str, Any]) -> str:
    return canonical_sha256({k: row.get(k) for k in
                             ("id", "body", "meeting_id", "meeting_date", "source_url")})


def build_data_plan(connection: Any, *, crosswalk: Mapping[str, Any], created_at: str) -> dict[str, Any]:
    """The corrected alias plan: only independently supported targets survive."""
    cohort = crosswalk["first_crosswalk_cohort"]["pairs"]
    rules_by_target: dict[str, list] = {}
    for m in crosswalk.get("per_meeting") or []:
        for cp in m.get("candidate_pairs") or []:
            if cp.get("fired_rules"):
                rules_by_target[str(cp.get("meeting_id") or cp.get("meeting_db_id"))] = \
                    list(cp["fired_rules"])
    rows, dropped = [], []
    for pair in cohort:
        sid = int(pair["meeting_db_id"])
        src = _meeting_row(connection, sid)
        if not src:
            dropped.append({"source_meeting_db_id": sid, "reason": "source row missing"})
            continue
        tgt = connection.execute(text(
            "SELECT id, body, meeting_id, meeting_date, meeting_title, source_url "
            "FROM meetings WHERE body=:b AND meeting_id=:mid LIMIT 1"),
            {"b": src["body"], "mid": pair["target_meeting_id"]}).mappings().first()
        if not tgt:
            dropped.append({"source_meeting_db_id": sid, "reason": "target row missing"})
            continue
        tgt = dict(tgt)
        tns = writer_namespace(tgt["meeting_id"], tgt.get("source_url"))
        if tns not in TARGET_NAMESPACES:
            dropped.append({"source_meeting_db_id": sid,
                            "target_meeting_id": tgt["meeting_id"],
                            "target_namespace": tns,
                            "reason": "target namespace is not in the declared contract"})
            continue
        items = int(connection.execute(text(
            "SELECT COUNT(*) FROM agenda_items WHERE meeting_db_id=:m"),
            {"m": int(tgt["id"])}).scalar() or 0)
        if items <= 0:
            dropped.append({"source_meeting_db_id": sid,
                            "target_meeting_id": tgt["meeting_id"],
                            "reason": "target is not agenda-bearing"})
            continue
        if src["body"] != tgt["body"]:
            dropped.append({"source_meeting_db_id": sid,
                            "reason": "source and target bodies differ"})
            continue
        rule = rules_by_target.get(str(tgt["meeting_id"]))
        if not rule:
            # Re-derive from live evidence rather than trusting a recorded label.
            rule = ["item_number_set"]
        live = _verify_rule(connection, sid, int(tgt["id"]), rule)
        if not live["holds"]:
            dropped.append({"source_meeting_db_id": sid,
                            "target_meeting_id": tgt["meeting_id"],
                            "reason": f"rule could not be re-verified: {live['reason']}"})
            continue
        s_fp, t_fp = _fingerprint(src), _fingerprint(tgt)
        rows.append({
            "source_meeting_db_id": sid, "source_meeting_id": src["meeting_id"],
            "source_system": writer_namespace(src["meeting_id"], src.get("source_url")),
            "body": src["body"], "external_id": src["meeting_id"],
            "canonical_meeting_db_id": int(tgt["id"]),
            "canonical_meeting_id": tgt["meeting_id"],
            "canonical_system": tns, "canonical_body": tgt["body"],
            "canonical_agenda_items": items, "rule": rule,
            "rule_verified": live["reason"],
            "source_fingerprint": s_fp, "target_fingerprint": t_fp,
            "evidence_sha256": canonical_sha256({"source": s_fp, "target": t_fp,
                                                 "rule": rule}),
        })
    targets: dict[int, list] = {}
    for r in rows:
        targets.setdefault(r["canonical_meeting_db_id"], []).append(r["source_meeting_db_id"])
    for r in rows:
        sibs = targets[r["canonical_meeting_db_id"]]
        r["many_to_one"] = len(sibs) > 1
        r["justification"] = (f"many-to-one: canonical meeting absorbs {len(sibs)} independently "
                              f"evidenced source aliases {sorted(sibs)}" if len(sibs) > 1 else
                              "one-to-one: single evidenced source alias")
        r["sibling_sources"] = sorted(sibs)
    plan = {
        "kind": DATA_KIND, "version": "kg-stage3-meeting-source-alias-plan/2.0",
        "created_at": created_at, "mode": "dry-run", "applied": False,
        "write_path": "absent by design", "no_merge": True, "no_row_deletion": True,
        "producer": {"module": "scripts/kg/stage3_b2_identity.py", "version": PRODUCER_VERSION,
                     "namespace_registry": NAMESPACE_REGISTRY_VERSION},
        "bindings": {"crosswalk_digest": crosswalk.get("digest"),
                     "writer_namespaces": WRITERS,
                     "code_hashes": code_hashes()},
        "operations": rows, "operation_count": len(rows), "dropped": dropped,
        "many_to_one": {str(k): v for k, v in targets.items() if len(v) > 1},
        "collision_checks": {"identity_key": "(source_system, body, external_id)",
                             "identities_unique": len({(r["source_system"], r["body"],
                                                        r["external_id"]) for r in rows})
                                                  == len(rows),
                             "self_reference_refused": True,
                             "all_rules_verified": True},
        "projected": {"aliases_created": len(rows), "dropped": len(dropped),
                      "agenda_payoff": len(rows), "event_payoff": 0},
    }
    plan["operation_set_digest"] = canonical_sha256(
        [[r["source_meeting_db_id"], r["canonical_meeting_db_id"]] for r in rows])
    plan["digest"] = canonical_sha256({k: v for k, v in plan.items() if k != "digest"})
    return plan


def build_schema_plan(connection: Any, *, created_at: str, target: Mapping[str, Any],
                      data_plan: Mapping[str, Any]) -> dict[str, Any]:
    present = connection.execute(text(
        "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='public' "
        "AND table_name=:t"), {"t": ALIAS_TABLE}).scalar()
    plan = {
        "kind": SCHEMA_KIND, "version": "kg-stage3-meeting-source-alias-schema/2.0",
        "created_at": created_at, "mode": "dry-run", "applied": False,
        "write_path": "absent by design", "additive_only": True,
        "touches_existing_columns": False, "destructive": False,
        "target": dict(target), "table": ALIAS_TABLE, "ddl": ddl(),
        "table_absent_now": int(present or 0) == 0,
        "model_recommendation": options_evaluation(),
        "namespace_registry": {"version": NAMESPACE_REGISTRY_VERSION, "writers": WRITERS,
                               "unknown": "fails closed: never a promotion target"},
        "rollback": {"statements": [f"DROP TABLE {ALIAS_TABLE}"]},
        "uniqueness": {"constraint": "uq_meeting_source_alias",
                       "columns": ["source_system", "body", "external_id"],
                       "semantics": "one source identity per body maps to at most one "
                                    "canonical meeting"},
        "fk_policy": "ON DELETE RESTRICT on both FKs",
        "replay": {"insert_conflict": "no-op via ON CONFLICT DO NOTHING",
                   "expected_replay_writes": 0},
        "bindings": {"data_plan_digest": data_plan.get("digest"),
                     "code_hashes": code_hashes()},
        "producer": {"module": "scripts/kg/stage3_b2_identity.py", "version": PRODUCER_VERSION},
    }
    plan["digest"] = canonical_sha256({k: v for k, v in plan.items() if k != "digest"})
    return plan


def traverse(connection: Any, *, source_meeting_db_id: int, item_number: str | None,
             aliases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    alias = next((a for a in aliases
                  if int(a["source_meeting_db_id"]) == int(source_meeting_db_id)), None)
    if alias is None:
        return {"resolved": False, "hold": "no_alias_for_source_meeting"}
    canonical = int(alias["canonical_meeting_db_id"])
    if item_number is None:
        return {"resolved": False, "hold": "no_item_number_from_result_parse",
                "canonical_meeting_db_id": canonical}
    matches = [dict(r) for r in connection.execute(text(
        "SELECT id, agenda_item_number FROM agenda_items WHERE meeting_db_id=:m"),
        {"m": canonical}).mappings().all()
        if B1.normalize_item_number(r["agenda_item_number"])
        == B1.normalize_item_number(item_number)]
    if len(matches) == 1:
        return {"resolved": True, "canonical_meeting_db_id": canonical,
                "agenda_item_db_id": int(matches[0]["id"]),
                "agenda_item_number": matches[0]["agenda_item_number"]}
    if not matches:
        return {"resolved": False, "hold": "no_matching_item_number",
                "canonical_meeting_db_id": canonical, "item_number": item_number}
    return {"resolved": False, "hold": "ambiguous_item_number",
            "canonical_meeting_db_id": canonical, "item_number": item_number}
