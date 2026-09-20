"""Human-review workspace for an immutable Stage 3 quality packet.

The packet is never modified.  Reviewer labels live in a separate, digest-bound
ledger so that the benchmark can be evaluated without treating human review as
canonical promotion.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Blueprint, abort, jsonify, render_template, request, url_for

from scripts.kg.stage3_quality_benchmark import (
    DECISIONS,
    EVIDENCE_LABELS,
    EXTRACTION_LABELS,
    LINK_LABELS,
    SUPPORT_LABELS,
)


kg_quality_review_bp = Blueprint("kg_quality_review", __name__, url_prefix="/kg/quality-review")

REPO = Path(__file__).resolve().parent.parent
PACKET_PATH = REPO / "data/kg-plans/kg-stage3-quality-review-packet-20260916T002029Z.json"
LEDGER_DIR = REPO / "data/kg-review-labels"
LABEL_FIELDS = ("decision", "evidence_coordinate", "support", "container_link", "extraction")
ALLOWED = {
    "decision": set(DECISIONS),
    "evidence_coordinate": set(EVIDENCE_LABELS),
    "support": set(SUPPORT_LABELS),
    "container_link": set(LINK_LABELS),
    "extraction": set(EXTRACTION_LABELS),
}


def _packet() -> dict[str, Any]:
    if not PACKET_PATH.is_file():
        abort(404, "The Stage 3 quality packet is not available on this server.")
    packet = json.loads(PACKET_PATH.read_text(encoding="utf-8"))
    if packet.get("mode") != "review-only" or packet.get("applied") is not False:
        abort(409, "Only an immutable review-only packet may be opened here.")
    if not isinstance(packet.get("digest"), str) or not packet["digest"]:
        abort(409, "The packet has no digest binding.")
    return packet


def _ledger_path(packet: dict[str, Any]) -> Path:
    return LEDGER_DIR / f"{packet['digest']}.json"


def _ledger(packet: dict[str, Any]) -> dict[str, Any]:
    path = _ledger_path(packet)
    if not path.exists():
        return {"packet_digest": packet["digest"], "labels": {}}
    ledger = json.loads(path.read_text(encoding="utf-8"))
    if ledger.get("packet_digest") != packet["digest"] or not isinstance(ledger.get("labels"), dict):
        abort(409, "The review ledger does not match this packet.")
    return ledger


def _write_ledger(packet: dict[str, Any], ledger: dict[str, Any]) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    destination = _ledger_path(packet)
    fd, temporary = tempfile.mkstemp(prefix=f".{packet['digest'][:12]}-", suffix=".tmp", dir=LEDGER_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(ledger, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _item(packet: dict[str, Any], case_id: str) -> dict[str, Any] | None:
    return next((item for item in packet.get("items", []) if item.get("case_id") == case_id), None)


def _applicability(item: dict[str, Any]) -> dict[str, bool]:
    candidate = item.get("candidate") or {}
    return {
        "support": candidate.get("promoted") is True,
        "container_link": any(candidate.get(key) is not None for key in ("agenda_item_db_id", "meeting_db_id")),
    }


def _validate_label(item: dict[str, Any], label: dict[str, Any]) -> str | None:
    if not isinstance(label, dict):
        return "The review label must be an object."
    for field in LABEL_FIELDS:
        if label.get(field) not in ALLOWED[field]:
            return f"{field} has an invalid value."
    expected_extraction = {
        "accept": "tp", "reject": "fp", "uncertain": "not_applicable",
        "not_applicable": "not_applicable",
    }[label["decision"]]
    if label["extraction"] != expected_extraction:
        return "The technical extraction label must match the reviewer decision."
    applicability = _applicability(item)
    if applicability["support"] != (label["support"] != "not_applicable"):
        return "Support must be not applicable unless the packet represents promotion."
    if applicability["container_link"] != (label["container_link"] != "not_applicable"):
        return "Container-link review must match the packet's available canonical link."
    return None


def _summary(packet: dict[str, Any], ledger: dict[str, Any]) -> dict[str, int]:
    case_ids = {str(item.get("case_id")) for item in packet.get("items", [])}
    reviewed = len(case_ids & set(ledger["labels"]))
    return {"total": len(case_ids), "reviewed": reviewed, "remaining": len(case_ids) - reviewed}


def _next_unreviewed(packet: dict[str, Any], ledger: dict[str, Any]) -> str | None:
    return next((item["case_id"] for item in packet.get("items", [])
                 if item["case_id"] not in ledger["labels"]), None)


@kg_quality_review_bp.get("/")
def dashboard():
    packet = _packet()
    ledger = _ledger(packet)
    return render_template("kg_quality_review.html", page="dashboard", packet=packet,
                           summary=_summary(packet, ledger), labels=ledger["labels"])


@kg_quality_review_bp.get("/case/<path:case_id>")
def review_case(case_id: str):
    packet = _packet()
    ledger = _ledger(packet)
    item = _item(packet, case_id)
    if item is None:
        abort(404)
    items = packet["items"]
    position = next(index for index, value in enumerate(items) if value["case_id"] == case_id)
    next_unreviewed = _next_unreviewed(packet, ledger)
    return render_template("kg_quality_review.html", page="case", packet=packet, item=item,
                           label=ledger["labels"].get(case_id), summary=_summary(packet, ledger),
                           position=position + 1, previous=items[position - 1]["case_id"] if position else None,
                           next_item=items[position + 1]["case_id"] if position + 1 < len(items) else None,
                           next_unreviewed=next_unreviewed, applicability=_applicability(item),
                           allowed={key: sorted(value) for key, value in ALLOWED.items()},
                           decision_options=("accept", "reject", "uncertain"),
                           claim=(f"This {item['stratum']['document_type']} records "
                                  f"“{item['stratum']['predicate']}” as a meeting result for "
                                  f"{item['stratum']['body']}."))


@kg_quality_review_bp.post("/api/labels/<path:case_id>")
def save_label(case_id: str):
    packet = _packet()
    item = _item(packet, case_id)
    if item is None:
        return jsonify({"ok": False, "error": "Unknown case ID."}), 404
    body = request.get_json(silent=True) or {}
    decision = body.get("decision")
    label = {
        "decision": decision,
        "extraction": {"accept": "tp", "reject": "fp", "uncertain": "not_applicable"}.get(decision),
        "evidence_coordinate": body.get("evidence_coordinate"),
        "container_link": body.get("container_link"),
        "support": "not_applicable",
    }
    problem = _validate_label(item, label)
    if problem:
        return jsonify({"ok": False, "error": problem}), 400
    notes = str(body.get("notes") or "").strip()
    source_detail = str(body.get("source_detail") or "").strip()[:500]
    ledger = _ledger(packet)
    ledger["labels"][case_id] = {
        **label,
        "source_detail": source_detail,
        "notes": notes,
        "saved_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_ledger(packet, ledger)
    next_case_id = _next_unreviewed(packet, ledger)
    return jsonify({
        "ok": True,
        "summary": _summary(packet, ledger),
        "next_url": (url_for("kg_quality_review.review_case", case_id=next_case_id)
                     if next_case_id else None),
    })


@kg_quality_review_bp.get("/api/progress")
def progress():
    packet = _packet()
    return jsonify(_summary(packet, _ledger(packet)))
