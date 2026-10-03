"""
Entity Mention Annotation — Flask routes.

Presents a stratified sample of ~300 agenda items for pure entity-span
annotation.  No type labels — just "is this span an entity?"

Supports:
  • One item at a time (item number / 300 progress)
  • Pre-highlighted candidate spans (auto-detected)
  • Accept / reject individual spans
  • Select arbitrary text to add new spans
  • Navigate: next, previous, jump to unreviewed
  • Progress tracking with auto-save
  • Export completed annotations as training data
"""

from __future__ import annotations

import json
import hashlib
import logging
from pathlib import Path
from flask import Blueprint, render_template, request, jsonify, Response

log = logging.getLogger(__name__)

annotation_bp = Blueprint("annotation", __name__, url_prefix="/annotation")

# Where the sample file lives
SAMPLE_PATH = Path(__file__).resolve().parent.parent / "data" / "mention-sample.json"

# In-memory cache of the sample (reloaded from disk on each request so edits persist)
_sample_cache: dict | None = None


def _load_sample() -> dict:
    global _sample_cache
    if _sample_cache is not None:
        return _sample_cache
    if not SAMPLE_PATH.exists():
        return {"meta": {}, "items": []}
    with open(SAMPLE_PATH) as f:
        _sample_cache = json.load(f)
    return _sample_cache


def _save_sample(data: dict):
    """Persist sample back to disk."""
    with open(SAMPLE_PATH, "w") as f:
        json.dump(data, f, indent=2, default=str)


@annotation_bp.route("/")
def annotation_dashboard():
    """Dashboard showing progress across all annotation items."""
    data = _load_sample()
    items = data.get("items", [])

    total = len(items)
    reviewed = sum(1 for it in items if it.get("annotation_status") == "reviewed")
    in_progress = sum(1 for it in items if it.get("annotation_status") == "in_progress")
    unreviewed = sum(1 for it in items if it.get("annotation_status", "unreviewed") == "unreviewed")
    total_human_spans = sum(len(it.get("human_spans", [])) for it in items)

    # Breakdown by jurisdiction
    jur_counts: dict[str, int] = {}
    jur_reviewed: dict[str, int] = {}
    for it in items:
        jur = it.get("jurisdiction", "Unknown")
        jur_counts[jur] = jur_counts.get(jur, 0) + 1
        if it.get("annotation_status") == "reviewed":
            jur_reviewed[jur] = jur_reviewed.get(jur, 0) + 1

    # Find first unreviewed
    first_unreviewed = None
    for it in items:
        if it.get("annotation_status", "unreviewed") == "unreviewed":
            first_unreviewed = it.get("sample_id")
            break

    return render_template(
        "entity_annotation.html",
        page="dashboard",
        total=total,
        reviewed=reviewed,
        in_progress=in_progress,
        unreviewed=unreviewed,
        total_human_spans=total_human_spans,
        jur_counts=sorted(jur_counts.items(), key=lambda x: -x[1]),
        jur_reviewed=jur_reviewed,
        first_unreviewed=first_unreviewed,
    )


@annotation_bp.route("/review/<int:sample_id>")
def annotation_review(sample_id: int):
    """Review a single annotation item."""
    data = _load_sample()
    items = data.get("items", [])

    item = next((it for it in items if it.get("sample_id") == sample_id), None)
    if not item:
        return render_template("404.html"), 404

    # Get next/previous IDs for navigation
    prev_id = sample_id - 1 if sample_id > 1 else None
    next_id = sample_id + 1 if sample_id < len(items) else None

    # Auto-set status to in_progress when first viewed
    if item.get("annotation_status", "unreviewed") == "unreviewed":
        item["annotation_status"] = "in_progress"
        _save_sample(data)

    # Find first unreviewed for jump-to-next-unreviewed
    first_unreviewed = None
    for it in items:
        if it.get("annotation_status", "unreviewed") == "unreviewed":
            first_unreviewed = it.get("sample_id")
            break

    return render_template(
        "entity_annotation.html",
        page="review",
        item=item,
        prev_id=prev_id,
        next_id=next_id,
        first_unreviewed=first_unreviewed,
        total=len(items),
    )


@annotation_bp.route("/api/save-spans", methods=["POST"])
def api_save_spans():
    """Save human-annotated spans for an item."""
    data = _load_sample()
    body = request.get_json(force=True)

    sample_id = body.get("sample_id")
    human_spans = body.get("human_spans", [])
    status = body.get("status", "in_progress")

    item = next((it for it in data.get("items", []) if it.get("sample_id") == sample_id), None)
    if not item:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    item["human_spans"] = human_spans
    item["annotation_status"] = status
    _save_sample(data)

    return jsonify({"ok": True, "saved": len(human_spans), "status": status})


@annotation_bp.route("/api/save-notes", methods=["POST"])
def api_save_notes():
    """Save annotation notes for an item."""
    data = _load_sample()
    body = request.get_json(force=True)

    sample_id = body.get("sample_id")
    notes = body.get("notes", "")

    item = next((it for it in data.get("items", []) if it.get("sample_id") == sample_id), None)
    if not item:
        return jsonify({"ok": False, "error": "Item not found"}), 404

    item["notes"] = notes
    _save_sample(data)

    return jsonify({"ok": True})


@annotation_bp.route("/api/stats")
def api_stats():
    """Return annotation progress stats as JSON."""
    data = _load_sample()
    items = data.get("items", [])

    total = len(items)
    reviewed = sum(1 for it in items if it.get("annotation_status") == "reviewed")
    in_progress = sum(1 for it in items if it.get("annotation_status") == "in_progress")
    unreviewed = sum(1 for it in items if it.get("annotation_status", "unreviewed") == "unreviewed")
    total_human_spans = sum(len(it.get("human_spans", [])) for it in items)
    total_auto_spans = sum(len(it.get("entity_spans", [])) for it in items)

    return jsonify({
        "total": total,
        "reviewed": reviewed,
        "in_progress": in_progress,
        "unreviewed": unreviewed,
        "total_human_spans": total_human_spans,
        "total_auto_spans": total_auto_spans,
        "pct_complete": round(reviewed / total * 100, 1) if total else 0,
    })


@annotation_bp.route("/api/reload")
def api_reload():
    """Reload sample from disk (clears in-memory cache)."""
    global _sample_cache
    _sample_cache = None
    data = _load_sample()
    return jsonify({"ok": True, "items": len(data.get("items", []))})


@annotation_bp.route("/export")
def export_training_data():
    """Export completed annotations as training data (JSONL format).

    Each line has text, unchanged human spans, and meta with annotation status,
    source field, schema version and negative-example flag. Reviewed empty span
    lists are confirmed negatives; absent/malformed lists are never inferred to
    be negatives. Headers report total, positive, negative and span counts plus
    the SHA-256 of the exact exported bytes. Invalid source records return 422
    rather than silently producing a partial training dataset.
    """
    data = _load_sample()
    items = data.get("items", [])
    if not isinstance(items, list):
        return jsonify({"ok": False, "error": "Annotation items must be a list"}), 422
    lines = []
    positive_count = negative_count = span_count = 0
    seen_ids = set()
    for index, it in enumerate(items):
        if not isinstance(it, dict) or it.get("annotation_status") not in (
                "reviewed", "in_progress", "unreviewed"):
            return jsonify({"ok": False, "error": f"Item {index}: invalid annotation status"}), 422
        if it["annotation_status"] != "reviewed":
            continue
        retained = it.get("text")
        spans = it.get("human_spans")
        sample_id = it.get("sample_id")
        if (not isinstance(retained, str) or not isinstance(spans, list)
                or not isinstance(sample_id, int) or isinstance(sample_id, bool)
                or sample_id <= 0 or sample_id in seen_ids):
            return jsonify({"ok": False, "error": f"Item {index}: invalid reviewed text, spans or sample ID"}), 422
        seen_ids.add(sample_id)
        for span_index, span in enumerate(spans):
            start = span.get("start") if isinstance(span, dict) else None
            end = span.get("end") if isinstance(span, dict) else None
            if (not isinstance(start, int) or isinstance(start, bool)
                    or not isinstance(end, int) or isinstance(end, bool)
                    or not 0 <= start < end <= len(retained)
                    or span.get("text") != retained[start:end]):
                return jsonify({"ok": False, "error": f"Item {index}, span {span_index}: invalid human span"}), 422
        positive_count += bool(spans)
        negative_count += not spans
        span_count += len(spans)
        # Build training record: text + character-level spans
        record = {
            "text": retained,
            "spans": [
                {"start": s["start"], "end": s["end"], "text": s["text"]}
                for s in it["human_spans"]
            ],
            "meta": {
                "sample_id": it["sample_id"],
                "jurisdiction": it.get("jurisdiction"),
                "meeting_type": it.get("meeting_type"),
                "meeting_date": it.get("meeting_date"),
                "annotation_status": "reviewed",
                "annotation_source": "human_spans",
                "export_schema_version": "mention-training/2.0",
                "negative_example": not spans,
            },
        }
        lines.append(json.dumps(record, default=str))

    payload = "\n".join(lines)
    return Response(
        payload,
        mimetype="application/jsonl",
        headers={
            "Content-Disposition": f"attachment; filename=mention-training-{len(lines)}items.jsonl",
            "X-Annotation-Export-Schema": "mention-training/2.0",
            "X-Annotation-Items": str(len(lines)),
            "X-Annotation-Positive-Items": str(positive_count),
            "X-Annotation-Negative-Items": str(negative_count),
            "X-Annotation-Spans": str(span_count),
            "X-Annotation-Export-SHA256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        },
    )
