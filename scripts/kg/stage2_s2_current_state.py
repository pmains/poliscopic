#!/usr/bin/env python3
"""``stage2_s2_current_state.py`` — the authoritative live state, and its compare.

Split out of the apply runner so both stay readable.  The state is derived from
the connection the admission already holds, so the fingerprint and the decisions
made from it describe **one snapshot**.  No caller supplies any part of it.

What the capture covers, and why each part is here:

* **every bound hold document** for both the 203-document repair population and
  the 101-document correction population, read with **every field the witness
  fingerprint covers** (``WITNESS_ROW_FIELDS``), and their row fingerprints
  **recomputed** from the live row rather than taken from the artifact;
* the **exact live hold membership and unlinked state, per plan**, rederived from
  the rows and digested with the same canonical function the plans bound, so a
  document whose meeting or recorded item moved changes that plan's digest;
* **every witness span** — number, title, revision and listing — as the live text
  at the bound coordinates, so a shifted or rewritten document cannot resolve to
  the recorded evidence;
* **every proposed natural key** a materialise or new-item operation would create,
  with its occupancy, so an absent-key race is a refusal before the write;
* the affected **agenda items**, so a moved target is visible.

``verify_live_state`` then compares the capture against **both plans' bound
baselines and the plan-bound current-state digest** — not merely against the
plans' internal consistency.
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402

__all__ = [
    "PLAN_ROLES",
    "SPAN_KINDS",
    "WITNESS_ROW_FIELDS",
    "WITNESS_ROW_SQL",
    "capture_current_state",
    "verify_live_state",
]

#: The two plans the admission is bound to, and the role name each carries.
PLAN_ROLES = ("repair", "correction")

#: The span kinds a witness may bind.  Every one that is present must resolve.
SPAN_KINDS = ("number_span", "title_span", "revision_span", "listing_span")

#: The document-row fields the witness fingerprint covers.  Mirrored from the
#: evidence module so the capture reads exactly the fields a witness hashes.
WITNESS_ROW_FIELDS = ("id", "meeting_db_id", "body", "agenda_item_number",
                      "document_url", "file_name", "document_title")

#: Operations that would create a row, and therefore claim a natural key.
CREATING_ACTIONS = ("materialise", "new_item_row")

#: The exact column list the witness fingerprint is computed from.  Published so a
#: plan builder reads the *same* raw values — a COALESCE on one side and not the
#: other would change a NULL to `''` and make every fingerprint disagree.
WITNESS_ROW_SQL = ("id, meeting_db_id, body, agenda_item_number, "
                   "agenda_item_number AS item_number, document_url, file_name, "
                   "document_title, agenda_item_id")


class LiveStateRefused(RuntimeError):
    """The live state does not match what the plans bound."""


def _row_fingerprint(row: Mapping[str, Any]) -> str:
    from scripts.kg.stage2_s2_evidence_materialize import document_row_fingerprint

    return document_row_fingerprint(row)


def _text_sha256(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _plan_units(repair_plan: Mapping[str, Any],
                correction_plan: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    return list(zip(PLAN_ROLES, (repair_plan, correction_plan)))


def _rows_of(plan: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(plan.get("rows") or []) + list(plan.get("operations") or [])


def _bound_hold_ids(plan: Mapping[str, Any]) -> list[int]:
    population = (plan.get("bindings") or {}).get("hold_population") or {}
    return sorted(int(d) for d in population.get("document_ids") or [])


def _witness_spans(*plans: Mapping[str, Any]) -> dict[int, dict[str, Mapping[str, Any]]]:
    """Every span each plan binds, keyed by the document it is read from."""
    spans: dict[int, dict[str, Mapping[str, Any]]] = {}
    for plan in plans:
        for row in _rows_of(plan):
            witness = row.get("witness") or {}
            if not witness:
                continue
            document_id = int(witness["document_id"])
            bucket = spans.setdefault(document_id, {})
            for kind in SPAN_KINDS:
                if witness.get(kind):
                    bucket.setdefault(kind, witness[kind])
    return spans


def _affected_meetings(*plans: Mapping[str, Any]) -> list[int]:
    meetings: set[int] = set()
    for plan in plans:
        for row in _rows_of(plan):
            if "meeting_db_id" in row:
                meetings.add(int(row["meeting_db_id"]))
    return sorted(meetings)


def _proposed_keys(*plans: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every natural key a creating operation would claim, deduplicated."""
    claimed: dict[tuple[int, str], dict[str, Any]] = {}
    for plan, label in zip(plans, PLAN_ROLES):
        for row in _rows_of(plan):
            if row.get("action") not in CREATING_ACTIONS:
                continue
            number = row.get("agenda_item_number") or row.get("to_label")
            if number is None:
                continue
            key = (int(row["meeting_db_id"]), str(number))
            claimed.setdefault(key, {"meeting_db_id": key[0],
                                     "agenda_item_number": key[1],
                                     "claimed_by": f"{label}:{row.get('action')}"})
    return [claimed[k] for k in sorted(claimed)]


def collision_population(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Every duplicated natural key among the affected rows, digested.

    Pre-existing duplicates are **real data**, not an error: many meetings carry a
    placeholder number claimed by several rows.  They are therefore reported and
    bound — so the collision state is part of the plan-bound digest — but only a
    collision the plan would *touch* refuses the admission.
    """
    buckets: dict[tuple[int, str], list[int]] = {}
    for item in items:
        key = (int(item["meeting_db_id"]), str(item["agenda_item_number"]))
        buckets.setdefault(key, []).append(int(item["id"]))
    colliding = {k: sorted(v) for k, v in buckets.items() if len(v) > 1}
    keys = sorted(f"{m}|{n}" for (m, n) in colliding)
    rows = sorted(i for ids in colliding.values() for i in ids)
    return {
        "distinct_keys": len(colliding),
        "involved_rows": len(rows),
        "excess": sum(len(ids) - 1 for ids in colliding.values()),
        "keys": keys,
        "keys_digest": binding.canonical_sha256(keys),
        "row_ids_digest": binding.canonical_sha256(rows),
    }


def _column_present(connection: Any, table: str, column: str) -> bool:
    """Is the additive column there yet?  Absent is the expected state."""
    from sqlalchemy import text as _text

    dialect = connection.dialect.name
    if dialect == "postgresql":
        return bool(connection.execute(_text(
            "SELECT COUNT(*) FROM information_schema.columns "
            "WHERE table_name = :t AND column_name = :c"),
            {"t": table, "c": column}).scalar())
    try:
        connection.execute(_text(f"SELECT {column} FROM {table} LIMIT 0"))
        return True
    except Exception:  # noqa: BLE001 - a missing column is the expected answer
        return False


def _in_clause(column: str, values: Sequence[Any]) -> tuple[str, dict[str, Any]]:
    keys = [f"v{i}" for i in range(len(values))]
    return (f"{column} IN ({', '.join(':' + k for k in keys)})",
            {k: v for k, v in zip(keys, values)})


def _document_rows(connection: Any,
                   document_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Read every field the witness fingerprint covers, plus the retained text."""
    from sqlalchemy import text as _text

    if not document_ids:
        return {}
    where, params = _in_clause("id", list(document_ids))
    rows = connection.execute(_text(
        f"SELECT {WITNESS_ROW_SQL}, COALESCE(text_content, '') AS text_content "
        f"FROM supporting_documents WHERE {where}"), params).mappings()
    return {int(r["id"]): dict(r) for r in rows}


def _span_observation(text: str, span: Mapping[str, Any]) -> dict[str, Any]:
    start, end = int(span["start"]), int(span["end"])
    fits = 0 <= start <= end <= len(text)
    live = text[start:end] if fits else ""
    return {"kind": span.get("kind"), "start": start, "end": end,
            "fits": fits, "length": len(live),
            "sha256": _text_sha256(live),
            "recorded_text_sha256": _text_sha256(str(span.get("span") or ""))}


def capture_current_state(
    connection: Any,
    *,
    repair_plan: Mapping[str, Any],
    correction_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Derive the authoritative live state from THIS connection."""
    from sqlalchemy import text as _text

    from scripts.kg import stage2_s2_documents as documents_mod

    plans = (repair_plan, correction_plan)
    meetings = _affected_meetings(*plans)
    witness_ids = sorted(_witness_spans(*plans))
    spans_by_document = _witness_spans(*plans)
    hold_ids_by_role = {role: _bound_hold_ids(plan) for role, plan in _plan_units(*plans)}
    all_document_ids = sorted(set(witness_ids) | {d for ids in hold_ids_by_role.values()
                                                  for d in ids})

    url = getattr(getattr(connection, "engine", None), "url", None)
    target = {
        "dialect": getattr(url, "drivername", None) or getattr(connection, "dialect", None).name,
        "host": getattr(url, "host", None),
        "port": getattr(url, "port", None),
        "database": getattr(url, "database", None),
    }

    items: list[dict[str, Any]] = []
    if meetings:
        where, params = _in_clause("meeting_db_id", meetings)
        rows = connection.execute(_text(
            "SELECT meeting_db_id, agenda_item_number, id, "
            "COALESCE(agenda_item_title, '') AS title, "
            "COALESCE(agenda_item_id, '') AS agenda_item_id, "
            "COALESCE(sort_order, 0) AS sort_order FROM agenda_items "
            f"WHERE {where} ORDER BY meeting_db_id, agenda_item_number, id"), params).mappings()
        items = [{"meeting_db_id": int(r["meeting_db_id"]),
                  "agenda_item_number": str(r["agenda_item_number"]),
                  "id": int(r["id"]), "title": str(r["title"]),
                  "agenda_item_id": str(r["agenda_item_id"]),
                  "sort_order": int(r["sort_order"])} for r in rows]

    live_documents = _document_rows(connection, all_document_ids)
    column = documents_mod.TARGET_COLUMN
    has_column = _column_present(connection, "supporting_documents", column)
    hold_id_set = {d for ids in hold_ids_by_role.values() for d in ids}

    documents_state: list[dict[str, Any]] = []
    preimages: list[dict[str, Any]] = []
    hold_rows: dict[str, list[dict[str, Any]]] = {role: [] for role in PLAN_ROLES}
    missing_documents: list[int] = []
    for document_id in all_document_ids:
        row = live_documents.get(document_id)
        if row is None:
            missing_documents.append(document_id)
            continue
        text = str(row.get("text_content") or "")
        linked = None
        if has_column:
            linked = connection.execute(_text(
                f"SELECT {column} FROM supporting_documents WHERE id = :i"),
                {"i": document_id}).scalar()
        documents_state.append({
            "id": document_id,
            "meeting_db_id": int(row["meeting_db_id"]),
            "agenda_item_number": str(row.get("agenda_item_number") or ""),
            "document_row_fingerprint": _row_fingerprint(row),
            "content_sha256": _text_sha256(text),
            "text_length": len(text),
            "is_witness": document_id in spans_by_document,
            "bound_hold_roles": sorted(role for role, ids in hold_ids_by_role.items()
                                       if document_id in ids),
            "column_present": has_column,
            "unlinked": True if not has_column else linked is None,
            "spans": {kind: _span_observation(text, span)
                      for kind, span in sorted(spans_by_document.get(document_id, {}).items())},
        })
        for role, ids in hold_ids_by_role.items():
            if document_id in ids:
                # The whole row, so the fingerprint is recomputed from exactly the
                # fields the plan hashed — not from a trimmed copy that would
                # silently change the digest.
                hold_rows[role].append(dict(row))
        preimages.append({"id": document_id,
                          "agenda_item_id": str(row.get("agenda_item_id") or "").strip() or None,
                          "agenda_item_number": str(row.get("agenda_item_number") or "") or None})

    hold_live = {role: binding.hold_population(rows) for role, rows in hold_rows.items()}
    occupied = {(int(i["meeting_db_id"]), str(i["agenda_item_number"])) for i in items}
    proposed = []
    for claim in _proposed_keys(*plans):
        key = (claim["meeting_db_id"], claim["agenda_item_number"])
        proposed.append({**claim, "occupied": key in occupied,
                         "occupant_ids": [i["id"] for i in items
                                          if (i["meeting_db_id"], i["agenda_item_number"]) == key]})

    body = {
        "target": target,
        "items": items,
        "documents": documents_state,
        "preimages": preimages,
        "hold_live": hold_live,
        "proposed_keys": proposed,
        "missing_documents": missing_documents,
        "bound_hold_documents": hold_id_set,
        "collisions": collision_population(items),
        "column_present": has_column,
    }
    body["hold_live"] = {role: pop for role, pop in hold_live.items()}
    body["sha256"] = binding.canonical_sha256(body)
    body["counts"] = {
        "items": len(items), "documents": len(documents_state),
        "bound_holds": len(hold_id_set), "witnesses": len(witness_ids),
        "missing_documents": len(missing_documents),
        "proposed_keys": len(proposed),
    }
    return body


def verify_live_state(
    current: Mapping[str, Any],
    *,
    repair_plan: Mapping[str, Any],
    correction_plan: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare the capture against BOTH plans' bound baseline and current digest."""
    problems: list[str] = []
    documents = {int(d["id"]): d for d in current.get("documents") or []}
    hold_live = current.get("hold_live") or {}

    # Baseline and population equality come FIRST: they are statements about the
    # plans' own authority, and a witness complaint must not crowd them out.
    for label, plan in _plan_units(repair_plan, correction_plan):
        bindings = plan.get("bindings") or {}
        bound_population = bindings.get("hold_population") or {}
        baseline = bindings.get("baseline") or {}
        live = hold_live.get(label) or {}
        if not baseline.get("population_sha256"):
            problems.append(f"{label}: the plan binds no baseline population digest")
        if baseline.get("population_count") != bound_population.get("count"):
            problems.append(f"{label}: the baseline count disagrees with the bound population")
        bound_ids = sorted(int(d) for d in bound_population.get("document_ids") or [])
        live_ids = sorted(int(d) for d in live.get("document_ids") or [])
        if bound_ids != live_ids:
            problems.append(
                f"{label}: live hold membership differs (missing "
                f"{sorted(set(bound_ids) - set(live_ids))[:5]}, extra "
                f"{sorted(set(live_ids) - set(bound_ids))[:5]})")
        if bound_population.get("sha256") != live.get("sha256"):
            problems.append(f"{label}: the live hold population digest has drifted")
        if baseline.get("population_sha256") != live.get("sha256"):
            problems.append(f"{label}: the live population does not match the bound baseline")
        if bindings.get("current_state_sha256") != current.get("sha256"):
            problems.append(f"{label}: does not bind the current-state digest")
        for entry in bound_population.get("entries") or []:
            document_id = int(entry["id"])
            document = documents.get(document_id)
            if document is None:
                problems.append(f"{label}: bound hold document {document_id} is absent")
                continue
            if entry.get("row_fingerprint") and \
                    document.get("document_row_fingerprint") != entry["row_fingerprint"]:
                problems.append(f"{label}: bound hold document {document_id} has drifted")
            if not document.get("unlinked"):
                problems.append(f"{label}: bound hold document {document_id} is not unlinked")
            if label not in (document.get("bound_hold_roles") or []):
                problems.append(f"{label}: document {document_id} did not rederive as its hold")

    # Every witness: present, row fingerprint, content hash, and every span.
    for label, plan in _plan_units(repair_plan, correction_plan):
        for row in _rows_of(plan):
            witness = row.get("witness") or {}
            if not witness:
                continue
            document_id = int(witness["document_id"])
            where = f"{label}: witness {document_id}"
            document = documents.get(document_id)
            if document is None:
                problems.append(f"{where} is absent")
                continue
            if document.get("document_row_fingerprint") != witness.get("document_row_fingerprint"):
                problems.append(f"{where} has drifted")
            if document.get("content_sha256") != witness.get("content_sha256"):
                problems.append(f"{where} content has changed")
            for kind in SPAN_KINDS:
                span = witness.get(kind)
                if not span:
                    continue
                observed = (document.get("spans") or {}).get(kind)
                if not observed:
                    problems.append(f"{where} binds {kind} but it was not captured")
                elif not observed.get("fits"):
                    problems.append(f"{where} {kind} does not fit the live text")
                elif int(observed["start"]) != int(span["start"]) or \
                        int(observed["end"]) != int(span["end"]):
                    problems.append(f"{where} {kind} coordinates moved")
                elif observed.get("sha256") != span.get("sha256"):
                    problems.append(f"{where} {kind} text hash changed")
                elif observed.get("recorded_text_sha256") != observed.get("sha256"):
                    problems.append(f"{where} {kind} recorded text does not match the live text")

    # Every proposed natural key must be unoccupied before the write.
    for claim in current.get("proposed_keys") or []:
        if claim.get("occupied"):
            problems.append(
                f"proposed key {claim['meeting_db_id']}/{claim['agenda_item_number']} "
                f"is already occupied by {claim.get('occupant_ids')}")

    # Collisions are reported and digested; only one the plan would TOUCH refuses.
    # Pre-existing duplicates among untouched rows are data, not a defect.
    collisions = current.get("collisions") or {}
    colliding_keys = set(collisions.get("keys") or [])
    for claim in current.get("proposed_keys") or []:
        key = f"{claim['meeting_db_id']}|{claim['agenda_item_number']}"
        if key in colliding_keys:
            problems.append(
                f"proposed key {key} already collides in the live population")

    if current.get("missing_documents"):
        problems.append(f"{len(current['missing_documents'])} bound documents are absent")

    if problems:
        raise LiveStateRefused("; ".join(problems[:5]))
    return {"documents_checked": len(documents),
            "items_checked": len(current.get("items") or []),
            "bound_holds": len(current.get("bound_hold_documents") or []),
            "collision_keys": (current.get("collisions") or {}).get("distinct_keys", 0),
            "collision_rows": (current.get("collisions") or {}).get("involved_rows", 0),
            "collisions_touched_by_a_plan": 0,
            "proposed_keys": len(current.get("proposed_keys") or []),
            "state_sha256": current.get("sha256")}
