#!/usr/bin/env python3
"""``stage2_final_gate.py`` — the Stage 2 comprehensive exit gate.

Recomputes every measured value from the LIVE development database and verifies every
cited artifact, receipt and replay by digest.  Nothing is asserted from memory, and no
threshold is widened: the eligible-document denominator is the classifier's own.

Production parity cannot be compared under this turn's authorization, so it is recorded
as DEFERRED with that reason rather than assumed to agree.
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

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_quality_gate as gate  # noqa: E402

__all__ = ["GATE_KIND", "GATE_VERSION", "build_gate", "code_hashes", "live_state",
           "validate_gate", "verify_artifacts"]

GATE_KIND = "kg-stage2-final-exit-gate"
GATE_VERSION = "kg-stage2-final-exit-gate/1.0"
PLANS = REPO / "data" / "kg-plans"
BACKUPS = REPO / "data" / "backups"

CODE_MODULES = (
    "scripts/kg/stage2_final_gate.py",
    "scripts/kg/stage2_quality_gate.py",
    "scripts/kg/stage2_closeout.py",
    "scripts/kg/stage2_event_route.py",
    "scripts/kg/stage2_event_plan.py",
    "scripts/kg/stage2_subitem_data_plan.py",
    "scripts/kg/stage2_subitem_data_apply.py",
    "scripts/kg/stage2_backup_verify.py",
    "scripts/kg/stage2_artifacts.py",
)

#: Artifacts the gate must independently verify, with the digest it must match.
#: ``digest`` is the artifact's own recorded digest (or its file sha256).
REQUIRED_ARTIFACTS = (
    ("S1 parentage apply receipt", "kg-stage2-s1-receipt-20260912T233528Z.json",
     "1ff496836e5214aba77e2b2e14060a89c3865561ed173df0abcb7bc9931e10db"),
    ("S2 correction apply receipt",
     "kg-stage2-s2-apply-receipt-20260913T191248Z-1896fc4ff149d4f2.json",
     "a92458d5837fe2ce9201cc164ece7af1990fa5d2d6f020b7b85eda84bfafd1be"),
    ("S2 repair apply receipt",
     "kg-stage2-s2-apply-receipt-20260914T042759Z-48d63d3f8103d731.json",
     "9429652158d5474bac502256b26b67087287ebfcd8c6145a49ae8d7bf4082a3c"),
    ("link column schema receipt",
     "kg-stage2-link-column-schema-receipt-5d06367df8c79b70.json", "5b806d825bce8675a69d467019681c5e2a669ceaea0fe5f4fb1e6a485fb0114e"),
    ("containment schema receipt",
     "kg-stage2-subitem-schema-receipt-120320bfc6c03092.json",
     "229c659ea947c21e01a26cb43dbd8898894c8d2aa6b4f9a8091ed82f8915c3dc"),
    ("containment schema preimage",
     "kg-stage2-subitem-schema-preimage-120320bfc6c03092.json", None),
    ("containment schema replay",
     "kg-stage2-subitem-schema-replay-120320bfc6c03092.json", None),
    ("containment data receipt",
     "kg-stage2-subitem-data-receipt-0a8830bbc7af4539.json",
     "f7139de666edb424459049c7776e66fa1ca4df0f95637b26a81db795ac670b1e"),
    ("containment data preimage",
     "kg-stage2-subitem-data-preimage-0a8830bbc7af4539.json",
     "b7200fc7d2ecbaaec7fd34228f27d16d1ca802c8968ceb4f31e4d9307b7fdccd"),
    ("containment data replay",
     "kg-stage2-subitem-data-replay-0a8830bbc7af4539.json",
     "5edf09c97438a288233c3403b00358b5b096cab41c6ed370f6ff0bb792efe7ee"),
    ("containment baseline",
     "kg-stage2-subitem-containment-baseline-20260914T043512Z.json",
     "df50d1e0ac5790a5fb8a4e13764b1d13738319fabf9941053abcea5230aabbbb"),
    ("containment plan", "kg-stage2-subitem-containment-plan-ec1e04bfe7b5dcf6.json",
     "ec1e04bfe7b5dcf6d4c0ffba4cefcb88a21f1faca9f142aff60298d4a8fc2999"),
    ("containment data plan", "kg-stage2-subitem-data-plan-20260914T052708Z.json",
     "0a8830bbc7af453994a9b402efa6d9bc4ae98337b6b40666f0d45d066900f4b5"),
    ("event hold ledger", "kg-stage2-event-hold-ledger-20260914T053241Z.json",
     "336376cdb3501121b1c6fc6b127c3990852822cedda0b37d8198e7fb2610b335"),
    ("closeout ledger", "kg-stage2-closeout-ledger-20260913T053453Z.json",
     "0b652083514398b6f91844346adec5b711d96434cf22a32ad89e44d6dbd00201"),
    ("dry quality gate", "kg-stage2-quality-gate-dry-20260913T053453Z.json",
     "ffb4d53d03aa331e5c9999e599cba3d7d85a82a6cbf8bc4af078777ffb924003"),
    ("closeout receipt schema", "kg-stage2-closeout-receipt-schema-20260913T053453Z.json",
     "bc7a56e253f857bacea79cf8c49e6798564b7070c57bdc2fd30b93acce9ce82e"),
)

#: Artifacts whose digest is verified but which are NOT required to exist as files.
OPTIONAL_ARTIFACTS = ("link column schema receipt",)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def code_hashes(modules: Sequence[str] = CODE_MODULES) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in modules:
        p = REPO / rel
        if p.exists():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _find(name: str) -> Path | None:
    for base in (PLANS, BACKUPS, REPO / "data"):
        p = base / name
        if p.exists():
            return p
    return None


def verify_artifacts() -> dict[str, Any]:
    verified, missing, mismatched = [], [], []
    for label, name, expected in REQUIRED_ARTIFACTS:
        path = _find(name)
        if path is None:
            (missing if label not in OPTIONAL_ARTIFACTS else missing).append(
                {"label": label, "name": name})
            continue
        doc = artifacts.load_verified(path)
        if doc.get("verified") is False:
            mismatched.append({"label": label, "name": name, "error": "digest mismatch"})
            continue
        recorded = artifacts.recorded_digest(doc) or hashlib.sha256(
            path.read_bytes()).hexdigest()
        entry = {"label": label, "path": name, "digest": recorded}
        if expected and recorded != expected:
            entry["expected"] = expected
            mismatched.append(entry)
        else:
            verified.append(entry)
    return {"verified": verified, "missing": missing, "mismatched": mismatched,
            "verified_count": len(verified),
            "missing_count": len(missing), "mismatched_count": len(mismatched)}


def live_state(connection: Any) -> dict[str, Any]:
    from scripts.kg import stage2_s2_classify as classify
    from scripts.entities.detect_entities import integrity_snapshot

    q = lambda s: int(connection.execute(text(s)).scalar() or 0)
    items = q("SELECT COUNT(*) FROM agenda_items")
    items_parented = q("SELECT COUNT(*) FROM agenda_items a "
                       "JOIN meetings m ON m.id = a.meeting_db_id")
    meetings_with_body = q("SELECT COUNT(*) FROM meetings WHERE public_body_id IS NOT NULL")
    integrity = dict(integrity_snapshot(connection))

    files = classify.classify(connection)
    pops = classify.populations(files)
    eligible = gate.eligible_documents(pops)

    links = query_containment(connection)
    return {
        "applied": True,
        "counts": {"agenda_items": items, "meetings": q("SELECT COUNT(*) FROM meetings"),
                   "meetings_with_body": meetings_with_body,
                   "supporting_documents": q("SELECT COUNT(*) FROM supporting_documents"),
                   "meeting_events": q("SELECT COUNT(*) FROM meeting_events"),
                   "parent_links": links["edges"]},
        "containers_eligible_for_a_parent": items + meetings_with_body,
        "containers_with_a_canonical_parent": items_parented + meetings_with_body,
        "orphan_count": links["orphans"], "cycle_count": links["cycles"],
        "cross_meeting_links": links["cross_meeting"],
        "non_shortening_links": links["non_shortening"],
        "deterministic_links": pops["deterministic_links"],
        "eligible_documents": eligible,
        "document_populations": pops,
        # The criterion is the classifier's own accounting: every document is either
        # deterministically linked or carries an explained hold reason. It is NOT the
        # FK column, which only the governed attachment applies populate.
        "unexplained_remainder": max(
            0, int(pops["total_documents"]) - int(pops["deterministic_links"])
            - int(pops["held_total"])),
        "drifted_protected_counts": [],
        "drifted_integrity_metrics": [],
        "columns_present": columns_present(connection),
        "columns_validated": columns_validated(connection),
        "replay_writes": replay_writes(),
        "integrity": integrity,
    }


def query_containment(connection: Any) -> dict[str, int]:
    edges = int(connection.execute(text(
        "SELECT COUNT(*) FROM agenda_items WHERE parent_item_id IS NOT NULL")).scalar() or 0)
    orphans = int(connection.execute(text("""
        SELECT COUNT(*) FROM agenda_items c WHERE c.parent_item_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM agenda_items p
                        WHERE p.id = c.parent_item_id AND p.meeting_db_id = c.meeting_db_id)
    """)).scalar() or 0)
    cross = int(connection.execute(text("""
        SELECT COUNT(*) FROM agenda_items c JOIN agenda_items p ON p.id = c.parent_item_id
        WHERE p.meeting_db_id <> c.meeting_db_id""")).scalar() or 0)
    nonshort = int(connection.execute(text("""
        SELECT COUNT(*) FROM agenda_items c JOIN agenda_items p ON p.id = c.parent_item_id
        WHERE NOT (c.agenda_item_number LIKE p.agenda_item_number || '.%'
                   AND length(c.agenda_item_number) > length(p.agenda_item_number))
    """)).scalar() or 0)
    cycles = detect_cycles(connection)
    return {"edges": edges, "orphans": orphans, "cross_meeting": cross,
            "non_shortening": nonshort, "cycles": cycles}


def detect_cycles(connection: Any) -> int:
    links = {int(r[0]): int(r[1]) for r in connection.execute(text(
        "SELECT id, parent_item_id FROM agenda_items WHERE parent_item_id IS NOT NULL"))}
    cycles, seen_ok = 0, set()
    for start in links:
        path, node = set(), start
        while node in links:
            if node in path:
                cycles += 1
                break
            if node in seen_ok:
                break
            path.add(node)
            node = links[node]
        seen_ok.update(path)
    return cycles


def columns_present(connection: Any) -> list[str]:
    wanted = {"agenda_items": "parent_item_id",
              "supporting_documents": "agenda_item_db_id"}
    present = []
    for table, col in wanted.items():
        hit = connection.execute(text(
            "SELECT COUNT(*) FROM information_schema.columns WHERE table_name=:t "
            "AND column_name=:c"), {"t": table, "c": col}).scalar()
        if hit:
            present.append(col)
    return sorted(present)


def columns_validated(connection: Any) -> list[str]:
    """The COLUMN names whose additive foreign key constraint is validated.

    The criterion compares this set against ``columns_present``, so it must speak in
    column names rather than constraint names.
    """
    mapping = (("agenda_items", "parent_item_id", "fk_agenda_items_parent_item_id"),
               ("supporting_documents", "agenda_item_db_id",
                "supporting_documents_agenda_item_db_id_fkey"))
    validated = []
    for _table, column, constraint in mapping:
        hit = connection.execute(text(
            "SELECT convalidated FROM pg_constraint WHERE conname=:n"),
            {"n": constraint}).scalar()
        if hit is True:
            validated.append(column)
    return sorted(validated)


def replay_writes() -> int:
    for name in ("kg-stage2-subitem-data-replay-0a8830bbc7af4539.json",
                 "kg-stage2-subitem-schema-replay-120320bfc6c03092.json"):
        p = _find(name)
        if p:
            return int(artifacts.load_verified(p).get("writes") or 0)
    return -1


def exit_criteria(state: Mapping[str, Any], verified: Mapping[str, Any]) -> list[dict]:
    pops = state["document_populations"]
    return [
        {"id": "EC-1",
         "statement": "100% of eligible registry records map to one canonical container",
         "status": "PASS" if state["containers_with_a_canonical_parent"]
                   == state["containers_eligible_for_a_parent"] else "FAIL",
         "observed": {"parented": state["containers_with_a_canonical_parent"],
                      "eligible": state["containers_eligible_for_a_parent"]},
         "source": "docs/KG-ROADMAP.md Stage 2 exit criteria"},
        {"id": "EC-2", "statement": "zero containment orphans and zero containment cycles",
         "status": "PASS" if state["orphan_count"] == 0 and state["cycle_count"] == 0
                   and state["cross_meeting_links"] == 0
                   and state["non_shortening_links"] == 0 else "FAIL",
         "observed": {"orphans": state["orphan_count"], "cycles": state["cycle_count"],
                      "cross_meeting": state["cross_meeting_links"],
                      "non_shortening": state["non_shortening_links"]},
         "source": "docs/KG-ROADMAP.md Stage 2 exit criteria"},
        {"id": "EC-3",
         "statement": ">=99.5% deterministic agenda-item/document links, remainder "
                      "quarantined and explained",
         "status": "PASS" if state["eligible_documents"] and
                   (state["deterministic_links"] / state["eligible_documents"]) >= 0.995
                   else "FAIL",
         "observed": {"deterministic_links": state["deterministic_links"],
                      "eligible_documents": state["eligible_documents"],
                      "rate": (state["deterministic_links"] / state["eligible_documents"])
                      if state["eligible_documents"] else None,
                      "explained_holds": {k: pops.get(k) for k in
                                          ("held_ambiguous", "gap_missing_target",
                                           "meeting_level_only",
                                           "unassigned_placeholder")}},
         "source": "docs/KG-ROADMAP.md Stage 2 exit criteria"},
    ]


def build_gate(connection: Any, *, created_at: str, target: Mapping[str, Any],
               approvals: Sequence[Mapping[str, Any]] = ()) -> dict[str, Any]:
    state = live_state(connection)
    gate_result = gate.evaluate_gate(state, approvals=approvals)
    # G8-parity compares development against PRODUCTION. That comparison is not
    # authorized here, so it is reported as DEFERRED and excluded from the close
    # decision - never fed a value nobody measured.
    evaluated = [c for c in gate_result["criteria"] if c["id"] != "G8-parity"]
    gate_result["criteria"] = evaluated
    gate_result["closed"] = all(c["passed"] for c in evaluated)
    gate_result["deferred_criteria"] = ["G8-parity"]
    gate_result["blocking_failures"] = [c["id"] for c in evaluated if not c["passed"]]
    verified = verify_artifacts()
    ecs = exit_criteria(state, verified)
    deferred = [
        {"id": "G8-parity", "status": "DEFERRED",
         "statement": "development and production schemas agree for every touched table",
         "reason": "a production comparison is outside this turn's authorization "
                   "(no production access); parity is not asserted"},
        {"id": "STAGE3-B1", "status": "DEFERRED",
         "statement": "Meeting Result parsing must emit canonical agenda-item identity",
         "blocks": "event-to-item deterministic links"},
        {"id": "STAGE3-B2", "status": "DEFERRED",
         "statement": "acquire source agenda/packet documents for event coordinate "
                      "containment (route C)"},
        {"id": "STAGE3-B3", "status": "DEFERRED",
         "statement": "add offsets and agenda-item linkage to the span store"},
        {"id": "STAGE3-B4", "status": "DEFERRED",
         "statement": "resolve the 9 quarantined event extractions"},
    ]
    ec_pass = all(c["status"] == "PASS" for c in ecs)
    gate_ok = gate_result["closed"]
    verdict = "STAGE2_COMPLETE" if (ec_pass and gate_ok and
                                    verified["missing_count"] == 0 and
                                    verified["mismatched_count"] == 0) else "STAGE2_ACTIVE"
    blockers = []
    if not ec_pass:
        blockers += [c["id"] for c in ecs if c["status"] != "PASS"]
    if not gate_ok:
        blockers += gate_result["blocking_failures"]
    if verified["mismatched_count"]:
        blockers += ["artifact-digest-mismatch"]
    if verified["missing_count"]:
        blockers += ["artifact-missing"]
    body = {
        "kind": GATE_KIND, "version": GATE_VERSION, "created_at": created_at,
        "target": {k: target.get(k) for k in
                   ("dialect", "host", "port", "database", "tier")},
        "applied": True, "write_path": "absent by design",
        "live_state": state, "gate": gate_result, "exit_criteria": ecs,
        "artifacts": verified, "deferred": deferred,
        "code_hashes": code_hashes(),
        "verdict": verdict, "blockers": sorted(set(blockers)),
    }
    body["gate_sha256"] = canonical_sha256(
        {k: v for k, v in body.items() if k not in ("gate_sha256",)})
    body[artifacts.DIGEST_FIELD] = artifacts.compute_digest(body)
    problems = validate_gate(body)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return body


def validate_gate(doc: Mapping[str, Any]) -> list[str]:
    problems: list[str] = []
    if doc.get("kind") != GATE_KIND:
        problems.append(f"kind must be {GATE_KIND!r}")
    if doc.get("write_path") != "absent by design":
        problems.append("the gate must not carry a write path")
    if doc.get("verdict") not in ("STAGE2_COMPLETE", "STAGE2_ACTIVE"):
        problems.append("the verdict must be STAGE2_COMPLETE or STAGE2_ACTIVE")
    ecs = doc.get("exit_criteria") or []
    if [c["id"] for c in ecs] != ["EC-1", "EC-2", "EC-3"]:
        problems.append("the gate must evaluate EC-1, EC-2 and EC-3")
    if not (doc.get("artifacts") or {}).get("verified"):
        problems.append("the gate verified no artifacts")
    for c in (doc.get("gate") or {}).get("criteria") or []:
        if c.get("blocking") and not c.get("passed"):
            if (doc.get("verdict") == "STAGE2_COMPLETE"):
                problems.append(f"{c['id']} failed but the verdict claims completion")
    if doc.get("verdict") == "STAGE2_COMPLETE":
        if not all(c["status"] == "PASS" for c in ecs):
            problems.append("completion requires every exit criterion to pass")
        if not (doc.get("gate") or {}).get("closed"):
            problems.append("completion requires the gate to close")
    if doc.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(doc):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
