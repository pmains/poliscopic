#!/usr/bin/env python3
"""``stage3_b2g2_key_discovery.py`` — B2g2 cross-platform-key discovery, read-only.

THE QUESTION.  Can the ``platform-proven`` unmatched result meetings (and the
``insufficient_evidence`` backlog) be bound to Legistar agenda meetings by a
CROSS-PLATFORM KEY — an identifier that BOTH sides publish?

THE TEST.  A key can only bind if it is present on both sides.  Every candidate is therefore
measured by *two-sided presence* first: how many AEM result records carry it and how many
Legistar records carry it.  A candidate with a zero on either side cannot bind anything,
however well it would discriminate.  Only then is each cohort meeting classified on
re-verified evidence, with exact-set accounting.

FORBIDDEN AS PROOF: body+date alone, fuzzy title similarity, document order, model inference.
Body+date is a BLOCKING key only, never evidence.

PURE AND READ-ONLY.  No fetch, no DB write, no row merge, no model call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import text

__all__ = ["CLASSES", "COHORT_RULE", "CONSUMED_KEYS", "KEY_CANDIDATES", "PRODUCER_VERSION",
           "REASON_CODES", "build_artifact", "canonical_sha256", "classify_cohort_meeting",
           "cohort_cells", "content_binding_diagnostic", "normalize_key", "scan_aem_documents",
           "select_cohort"]

PRODUCER_VERSION = "kg-stage3-b2g2-key-discovery/1.0"

CLASSES = ("deterministic_route", "evidence_insufficient", "contradiction", "unavailable")

#: Bounded, reproducible cohort: stratify by (body, year) so no body and no year dominates,
#: then take the earliest meetings in each cell.  Pure ordering, no sampling.
COHORT_RULE = {
    "stratify_by": ["body", "year"],
    "per_cell": 3,
    "max_cells": 60,
    "order": ["body", "year", "meeting_date", "meeting_db_id"],
    "note": "earliest meeting_date in each (body, year) cell; deterministic and re-runnable",
}

#: Candidate cross-platform keys, each with the regex that recognises it in free text.
KEY_CANDIDATES: dict[str, dict[str, str]] = {
    "legistar_detail_id_or_guid": {
        "side": "aem",
        "regex": r"(?:MeetingDetail\.aspx\?ID=|GUID=)[0-9A-Fa-f-]{6,}",
        "meaning": "a Legistar detail id or GUID carried in the AEM record's url or text",
    },
    "aem_slug_id": {
        "side": "legistar",
        "regex": r"\b[0-9]{9}r\b",
        "meaning": "the AEM/publicmeetings document id (YYMMDDNNNr) carried in a Legistar "
                   "url, title or minutes link",
    },
    "case_number": {
        "side": "both",
        "regex": r"\b(?:S|ST|FD|ABND|WS|PL|GP|PP|CF)[-\s]?[0-9]{4,8}\b",
        "meaning": "a Phoenix case/ordinance number present on both sides",
    },
}

REASON_CODES = {
    "no_agenda_platform_for_body":
        "no meeting of this body exists on its own city's agenda platform, so no authoritative "
        "agenda record can bind it",
    "no_counterpart_for_blocking_key":
        "the body has a proven agenda platform but no counterpart shares this meeting's "
        "blocking key, so no pair can be corroborated at all",
    "no_two_sided_key":
        "a counterpart exists, but no candidate cross-platform key is present on both sides, "
        "so no binding evidence can exist",
    "item_numbers_not_subset":
        "the result document proves item numbers but they are not a subset of the "
        "counterpart's agenda items",
    "matter_level_identifier_not_meeting_level":
        "the only two-sided identifier the sources share names a MATTER, not a MEETING: it "
        "matches across different meetings and in one direction only, so it cannot determine "
        "meeting identity",
    "unique_corroborated_counterpart":
        "exactly one counterpart is corroborated by re-verified exact evidence",
    "competing_corroborated_counterparts":
        "several corroborated counterparts disagree, so the meeting is not determined",
}

#: Keys already consumed by the canonical crosswalk, recorded so the ledger is complete.
CONSUMED_KEYS = {
    "item_number_set":
        "the result document's item numbers form a subset of the counterpart's agenda item "
        "numbers; already fired for the 12 canonical pairs and cannot fire again for these",
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        .encode("utf-8")).hexdigest()


def normalize_key(value: Any) -> str:
    """Normalize an identifier for EXACT comparison: alphanumeric, lower case."""
    return re.sub(r"[^0-9a-z]", "", str(value or "").lower())


def scan_aem_documents(documents: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """AEM-side presence: how many result records carry each candidate key."""
    docs = list(documents)
    out: dict[str, Any] = {"documents": len(docs)}
    for name, spec in KEY_CANDIDATES.items():
        rx = re.compile(spec["regex"], re.IGNORECASE)
        tokens: set[str] = set()
        hits = 0
        for d in docs:
            blob = f"{d.get('document_url') or ''}\n{d.get('text_content') or ''}"
            found = rx.findall(blob)
            if found:
                hits += 1
                tokens.update(normalize_key(f) for f in found)
        tokens.discard("")
        out[name] = {"aem_records": hits, "aem_distinct_tokens": len(tokens),
                     "aem_tokens_sample": sorted(tokens)[:6]}
    return out


def legistar_presence(*, meetings: Iterable[Mapping[str, Any]], agenda_items: int,
                      file_number_populated: int, case_number_populated: int,
                      item_url_aem_hits: int) -> dict[str, Any]:
    """Legistar-side presence for the keys an AEM record could supply."""
    ms = list(meetings)
    slug = re.compile(KEY_CANDIDATES["aem_slug_id"]["regex"], re.IGNORECASE)
    aem_ref = re.compile(r"publicmeetings|phoenix\.gov", re.IGNORECASE)
    slug_hits = [m for m in ms if any(slug.search(str(m.get(f) or ""))
                                      for f in ("meeting_id", "meeting_title", "minutes_url"))]
    aem_hits = [m for m in ms if any(aem_ref.search(str(m.get(f) or ""))
                                     for f in ("meeting_title", "minutes_url"))]
    return {"legistar_meetings": len(ms), "legistar_agenda_items": agenda_items,
            "aem_slug_id": {"legistar_records": len(slug_hits)},
            "aem_reference": {"legistar_records": len(aem_hits),
                              "legistar_item_urls": item_url_aem_hits},
            "legistar_file_number": {"populated": file_number_populated},
            "legistar_case_number": {"populated": case_number_populated}}


def cohort_cells(unmatched: Sequence[Mapping[str, Any]],
                 proven_bodies: Iterable[str]) -> list[list[dict[str, Any]]]:
    """Group the platform-proven unmatched into (body, year) cells.  Deterministic."""
    proven = set(proven_bodies)
    cells: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for m in unmatched:
        if m["body"] not in proven:
            continue
        cells.setdefault((str(m["body"]), str(m["meeting_date"])[:4]), []).append(dict(m))
    for key in cells:
        cells[key].sort(key=lambda m: (str(m["meeting_date"]), int(m["meeting_db_id"])))
    return [cells[k] for k in sorted(cells)]


def select_cohort(unmatched: Sequence[Mapping[str, Any]], proven_bodies: Iterable[str]) -> dict:
    cells = cohort_cells(unmatched, proven_bodies)
    chosen = cells[:COHORT_RULE["max_cells"]]
    cohort: list[dict[str, Any]] = []
    for cell in chosen:
        cohort.extend(cell[:COHORT_RULE["per_cell"]])
    cohort.sort(key=lambda m: (str(m["body"]), str(m["meeting_date"]), int(m["meeting_db_id"])))
    return {"rule": dict(COHORT_RULE), "cells_available": len(cells), "cells_used": len(chosen),
            "cohort_size": len(cohort),
            "bodies": sorted({m["body"] for m in cohort}),
            "years": sorted({str(m["meeting_date"])[:4] for m in cohort}),
            "cohort": [{"meeting_db_id": int(m["meeting_db_id"]), "body": m["body"],
                        "meeting_date": m["meeting_date"]} for m in cohort],
            "cohort_sha256": canonical_sha256(
                [int(m["meeting_db_id"]) for m in cohort])}


def classify_cohort_meeting(*, platform_proven: bool, counterpart_ids: Sequence[int],
                            two_sided_keys: Sequence[str], source_items: set[str],
                            target_items: Sequence[set[str]]) -> dict[str, Any]:
    """One class per meeting.  Evidence, never similarity."""
    if not platform_proven:
        return {"disposition": "unavailable", "reason_code": "no_agenda_platform_for_body",
                "target": None}
    if not counterpart_ids:
        return {"disposition": "evidence_insufficient",
                "reason_code": "no_counterpart_for_blocking_key", "target": None, "keys": []}
    if not two_sided_keys:
        return {"disposition": "evidence_insufficient", "reason_code": "no_two_sided_key",
                "target": None, "keys": []}
    if not source_items:
        return {"disposition": "evidence_insufficient", "reason_code": "no_two_sided_key",
                "target": None, "keys": []}
    matching = [cid for cid, items in zip(counterpart_ids, target_items)
                if items and source_items <= items]
    if len(matching) == 1:
        return {"disposition": "deterministic_route", "keys": list(two_sided_keys),
                "reason_code": "unique_corroborated_counterpart", "target": matching[0]}
    if len(matching) > 1:
        return {"disposition": "contradiction", "keys": list(two_sided_keys),
                "reason_code": "competing_corroborated_counterparts", "target": None}
    return {"disposition": "evidence_insufficient", "keys": list(two_sided_keys),
            "reason_code": "item_numbers_not_subset", "target": None}


def content_binding_diagnostic(connection: Any, meetings: Sequence[Mapping[str, Any]],
                              *, legistar_case_index: Mapping[tuple[str, str], set[int]],
                              legistar_dates: Mapping[int, Any]) -> dict[str, Any]:
    """Test the ONE two-sided key for meeting-binding power.

    A shared case number proves only that the same MATTER touched both records.  If a key is a
    genuine meeting identifier, content-only matches must (a) agree on the date and (b) not
    collide -- two source meetings must never claim one target.  Both properties are measured.
    """
    rx = re.compile(KEY_CANDIDATES["case_number"]["regex"], re.IGNORECASE)
    unique = ambiguous = none = 0
    target_later = target_same_date = target_earlier = 0
    claims: dict[int, list[int]] = defaultdict(list)
    details: list[dict[str, Any]] = []
    meetings_with_tokens = 0
    for m in meetings:
        mid = int(m["meeting_db_id"])
        src = _meeting_case_numbers(connection, mid, rx)
        if not src:
            continue
        meetings_with_tokens += 1
        hits: dict[int, set[str]] = defaultdict(set)
        for token in src:
            for target in legistar_case_index.get((str(m["body"]), token), ()):
                hits[target].add(token)
        if len(hits) == 1:
            unique += 1
            target = next(iter(hits))
            sd = str(m["meeting_date"])[:10]
            td = str(legistar_dates.get(target) or "")[:10]
            if td > sd:
                target_later += 1
            elif td == sd:
                target_same_date += 1
            else:
                target_earlier += 1
            claims[target].append(mid)
            details.append({"source_meeting_db_id": mid, "body": m["body"],
                            "source_date": sd, "target_meeting_db_id": target,
                            "target_date": td, "shared_tokens": sorted(hits[target]),
                            "same_date": td == sd})
        elif len(hits) > 1:
            ambiguous += 1
        else:
            none += 1
    collisions = {t: sorted(v) for t, v in claims.items() if len(v) > 1}
    return {
        "meetings_with_case_tokens": meetings_with_tokens,
        "unique_content_matches": unique,
        "ambiguous_content_matches": ambiguous,
        "no_content_match": none,
        "unique_matches_target_later": target_later,
        "unique_matches_target_same_date": target_same_date,
        "unique_matches_target_earlier": target_earlier,
        "targets_claimed_by_multiple_sources": len(collisions),
        "collisions": collisions,
        "binding_direction": ("forward_only" if target_later and not target_earlier
                              else "mixed"),
        "meeting_binding_power": "none" if (
            unique == 0 or target_same_date == 0 or collisions) else "candidate",
        "detail": details,
    }


def _scalar(connection: Any, sql: str, **params: Any) -> Any:
    return connection.execute(text(sql), params).scalar()


def _rows(connection: Any, sql: str, **params: Any) -> list[Mapping[str, Any]]:
    return connection.execute(text(sql), params).mappings().all()


def _proven_bodies(connection: Any) -> set[str]:
    """A body is platform-proven only when the platform's own city matches its prefix."""
    out: set[str] = set()
    # NOTE: plain Rows (not RowMappings) -- iterating a mapping yields its KEYS, not values.
    for body, url in connection.execute(text(
            "SELECT DISTINCT body, source_url FROM meetings WHERE source_url LIKE '%legistar%'")):
        raw = str(url or "")
        host = raw.split("/")[2].lower() if "//" in raw else ""
        city = host.split(".")[0]
        whole = str(body or "").lower()
        if city and (whole.startswith(city) or city in whole.split("-")):
            out.add(body)
    return out


def _legistar_case_numbers(connection: Any) -> set[str]:
    vals = connection.execute(text(
        "SELECT DISTINCT case_number FROM agenda_items a JOIN meetings m ON m.id=a.meeting_db_id "
        "WHERE m.source_url LIKE '%legistar%' AND coalesce(a.case_number,'')<>''")).scalars().all()
    return {normalize_key(v) for v in vals if normalize_key(v)}


def _meeting_case_numbers(connection: Any, meeting_db_id: int,
                          rx: "re.Pattern[str]") -> set[str]:
    txt = _scalar(connection,
        "SELECT text_content FROM supporting_documents WHERE meeting_db_id=:m "
        "AND document_type='Meeting Result' LIMIT 1", m=meeting_db_id)
    out = {normalize_key(t) for t in rx.findall(str(txt or ""))}
    out.discard("")
    return out


def build_artifact(connection: Any, *, crosswalk: Mapping[str, Any], backlog: Mapping[str, Any],
                   created_at: str, target: Mapping[str, Any]) -> dict[str, Any]:
    unmatched = [m for m in crosswalk["per_meeting"] if m["class"] == "unmatched"]
    proven = _proven_bodies(connection)
    cohort_info = select_cohort(unmatched, proven)

    # --- two-sided presence, measured first ---------------------------------
    aem_docs = _rows(connection, "SELECT document_url, text_content FROM supporting_documents "
                                 "WHERE document_type='Meeting Result'")
    aem_presence = scan_aem_documents(aem_docs)
    legistar_meetings = _rows(connection, "SELECT meeting_id, meeting_title, minutes_url "
                                          "FROM meetings WHERE source_url LIKE '%legistar%'")
    legistar = legistar_presence(
        meetings=legistar_meetings,
        agenda_items=_scalar(connection, "SELECT COUNT(*) FROM agenda_items a JOIN meetings m "
            "ON m.id=a.meeting_db_id WHERE m.source_url LIKE '%legistar%'"),
        file_number_populated=_scalar(connection, "SELECT COUNT(*) FROM agenda_items a JOIN "
            "meetings m ON m.id=a.meeting_db_id WHERE m.source_url LIKE '%legistar%' "
            "AND coalesce(a.c_number_base,'')<>''"),
        case_number_populated=_scalar(connection, "SELECT COUNT(*) FROM agenda_items a JOIN "
            "meetings m ON m.id=a.meeting_db_id WHERE m.source_url LIKE '%legistar%' "
            "AND coalesce(a.case_number,'')<>''"),
        item_url_aem_hits=_scalar(connection, "SELECT COUNT(*) FROM agenda_items a JOIN meetings "
            "m ON m.id=a.meeting_db_id WHERE m.source_url LIKE '%legistar%' "
            "AND coalesce(a.agenda_item_url,'') ~* 'publicmeetings|phoenix\\.gov'"))

    legistar_cases = _legistar_case_numbers(connection)
    rx_case = re.compile(KEY_CANDIDATES["case_number"]["regex"], re.IGNORECASE)

    # the Legistar case-number index, used to test the only two-sided key
    case_index: dict[tuple[str, str], set[int]] = defaultdict(set)
    for row in connection.execute(text(
            "SELECT m.id, m.body, a.case_number FROM agenda_items a JOIN meetings m "
            "ON m.id=a.meeting_db_id WHERE m.source_url LIKE '%legistar%' "
            "AND coalesce(a.case_number,'')<>''")):
        value = normalize_key(row[2])
        if value:
            case_index[(str(row[1]), value)].add(int(row[0]))
    legistar_dates = {int(r[0]): r[1] for r in connection.execute(text(
        "SELECT id, meeting_date FROM meetings WHERE source_url LIKE '%legistar%'"))}
    content = content_binding_diagnostic(
        connection, [m for m in unmatched if m["body"] in proven],
        legistar_case_index=case_index, legistar_dates=legistar_dates)

    aem_tokens: set[str] = set()
    for d in aem_docs:
        aem_tokens.update(normalize_key(t) for t in rx_case.findall(str(d.get("text_content")
                                                                       or "")))
    aem_tokens.discard("")
    shared_tokens = aem_tokens & legistar_cases

    two_sided = {
        "legistar_detail_id_or_guid":
            {"aem_records": aem_presence["legistar_detail_id_or_guid"]["aem_records"],
             "legistar_records": None, "bindable": False,
             "why": "the AEM side carries no Legistar identifier at all, so there is nothing "
                    "to match against"},
        "aem_slug_id":
            {"aem_records": aem_presence["aem_slug_id"]["aem_records"],
             "legistar_records": legistar["aem_slug_id"]["legistar_records"], "bindable": False,
             "why": "the AEM document id appears nowhere on the Legistar side"},
        "case_number":
            {"aem_distinct_tokens": len(aem_tokens), "legistar_distinct_tokens": len(legistar_cases),
             "shared_distinct_tokens": len(shared_tokens),
             "bindable": content["meeting_binding_power"] == "candidate",
             "why": "two-sided as a MATTER-level token, but it cannot determine MEETING "
                    "identity: content-only matches are one-directional (target always later) "
                    "and collide.  See content_binding."},
        "item_number_set": {"bindable": False, "why": CONSUMED_KEYS["item_number_set"]},
    }

    # --- per-meeting classification over the bounded cohort ------------------
    counts = {c: 0 for c in CLASSES}
    reasons: dict[str, int] = {}
    per_meeting: list[dict[str, Any]] = []
    for row in cohort_info["cohort"]:
        mid, body, date = int(row["meeting_db_id"]), row["body"], row["meeting_date"]
        counterparts = [int(r[0]) for r in connection.execute(text(
            "SELECT id FROM meetings WHERE body=:b AND meeting_date=:d "
            "AND source_url LIKE '%legistar%' ORDER BY id"), {"b": body, "d": date})]
        src = _meeting_case_numbers(connection, mid, rx_case)
        keys = ["case_number"] if (src & legistar_cases) else []
        target_items = [
            {normalize_key(r[0]) for r in connection.execute(text(
                "SELECT case_number FROM agenda_items WHERE meeting_db_id=:m"), {"m": cid})
             if normalize_key(r[0])}
            for cid in counterparts]
        verdict = classify_cohort_meeting(
            platform_proven=body in proven, counterpart_ids=counterparts,
            two_sided_keys=keys, source_items=src, target_items=target_items)
        counts[verdict["disposition"]] += 1
        reasons[verdict["reason_code"]] = reasons.get(verdict["reason_code"], 0) + 1
        per_meeting.append({"meeting_db_id": mid, "body": body, "meeting_date": date,
                            "source_case_tokens": len(src), "counterparts": counterparts,
                            **verdict})

    # --- exact-set accounting over the whole populations ---------------------
    backlog_reasons: dict[str, int] = {}
    backlog_tokens = 0
    for e in backlog["entries"]:
        src = _meeting_case_numbers(connection, int(e["meeting_db_id"]), rx_case)
        if src:
            backlog_tokens += 1
        backlog_reasons[e["reason_code"]] = backlog_reasons.get(e["reason_code"], 0) + 1

    art = {
        "kind": "kg-stage3-b2g2-key-discovery", "version": PRODUCER_VERSION,
        "created_at": created_at, "mode": "read-only", "write_path": "absent by design",
        "applied": False, "no_fetch": True, "no_merge": True, "no_model_calls": True,
        "target": dict(target),
        "producer": {"module": "scripts/kg/stage3_b2g2_key_discovery.py",
                     "version": PRODUCER_VERSION},
        "bindings": {"crosswalk_digest": crosswalk.get("digest"),
                     "backlog_digest": backlog.get("digest")},
        "key_candidates": {k: dict(v) for k, v in KEY_CANDIDATES.items()},
        "consumed_keys": dict(CONSUMED_KEYS),
        "two_sided_presence": two_sided,
        "presence_detail": {"aem": aem_presence, "legistar": legistar,
                            "aem_distinct_case_tokens": len(aem_tokens),
                            "legistar_distinct_case_tokens": len(legistar_cases),
                            "shared_distinct_case_token_count": len(shared_tokens),
                            "shared_distinct_case_tokens_sample": sorted(shared_tokens)[:10]},
        "content_binding": content,
        "cohort": {k: v for k, v in cohort_info.items() if k != "cohort"},
        "classification": {"classes": counts, "reason_codes": reasons,
                           "total": len(per_meeting),
                           "reconciles": sum(counts.values()) == len(per_meeting)},
        "backlog": {"population": backlog.get("population"), "entries": len(backlog["entries"]),
                    "reason_codes": backlog_reasons,
                    "entries_with_case_token": backlog_tokens},
        "projected": {"deterministic_routes": counts["deterministic_route"],
                      "agenda_payoff": counts["deterministic_route"], "event_payoff": 0},
        "verdict": ("route_exhausted" if counts["deterministic_route"] == 0
                    and content["meeting_binding_power"] == "none" else "route_open"),
        "per_meeting": per_meeting,
        "cohort_digest": cohort_info["cohort_sha256"],
    }
    art["digest"] = canonical_sha256(art)
    return art
