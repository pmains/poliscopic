#!/usr/bin/env python3
"""Untouched, source-balanced, full-document Stage 3 quality gate.

This module is deliberately manifest-first.  It never connects to a database,
downloads a source, or decides whether a span is a civic action.  A producer
must supply whole-document manifests (including every extractor prediction and
the document dimensions); a human reviewer supplies the gold action inventory
later.  The packet is therefore suitable for precision *and* recall, while the
400-case candidate packet remains a separate development regression gate.
"""

from __future__ import annotations

import hashlib
import json
import argparse
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:  # pragma: no cover - direct offline CLI use
    sys.path.insert(0, str(REPO))

from scripts.kg.stage2_artifacts import load_verified, write_immutable

VERSION = "kg-stage3-quality-holdout/1.0"
KIND = "kg-stage3-quality-holdout"
CORRECTION_KIND = "kg-stage3-quality-correction-proposal"
COORDINATE_SYSTEM = "unicode_codepoint_half_open"
DIMENSIONS = ("source", "body", "document_type", "extraction_method")
CELL_FIELDS = DIMENSIONS + ("predicate",)
TEMPORAL_LABELS = ("current_meeting", "prior_meeting", "future_action", "quoted_nested", "not_applicable")
LABEL_STATUS = ("tp", "fp", "fn")
QUALIFIER_LABELS = ("retained", "lost", "spurious", "not_applicable")
ASSOCIATION_LABELS = ("correct", "incorrect", "not_applicable")
PREDICTION_TEMPORAL_LABELS = ASSOCIATION_LABELS
TARGET = {"tier": "development", "mode": "read-only", "database": "poliscopic_dev"}


class HoldoutRefused(ValueError):
    """The holdout manifest or human labels are incomplete or unsafe."""


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=True, default=str).encode()).hexdigest()


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HoldoutRefused(f"{field} is required")
    return value.strip()


def _positive(value: Any, field: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise HoldoutRefused(f"{field} must be a positive integer")
    return value


def _span(span: Mapping[str, Any], text: str, field: str = "span") -> dict[str, Any]:
    if not isinstance(span, Mapping):
        raise HoldoutRefused(f"{field} must be an object")
    start, end = span.get("start"), span.get("end")
    if (not isinstance(start, int) or isinstance(start, bool) or
            not isinstance(end, int) or isinstance(end, bool) or not 0 <= start < end <= len(text)):
        raise HoldoutRefused(f"{field} offsets are outside retained text")
    if span.get("coordinate_system", COORDINATE_SYSTEM) != COORDINATE_SYSTEM:
        raise HoldoutRefused(f"{field} coordinate system is not canonical")
    expected = text_sha256(text[start:end])
    if span.get("sha256") != expected:
        raise HoldoutRefused(f"{field} sha256 differs from retained text")
    return {"coordinate_system": COORDINATE_SYSTEM, "start": start, "end": end, "sha256": expected}


def _prediction(prediction: Mapping[str, Any], text: str, document_id: int) -> dict[str, Any]:
    if not isinstance(prediction, Mapping):
        raise HoldoutRefused("prediction must be an object")
    result = dict(prediction)
    result["prediction_id"] = _required_text(result.get("prediction_id"), "prediction_id")
    result["predicate"] = _required_text(result.get("predicate"), "prediction.predicate")
    result["span"] = _span(result.get("span") or {}, text, "prediction.span")
    if result.get("document_id") not in (None, document_id):
        raise HoldoutRefused("prediction document_id does not match its document")
    result["document_id"] = document_id
    # These are outputs, not truth labels.  Their absence is valid for an extractor.
    result.setdefault("outcome_base", None)
    result.setdefault("qualifier", None)
    result.setdefault("qualifier_text", None)
    result.setdefault("item_reference", None)
    result.setdefault("temporal_attribution", None)
    return result


def validate_document(document: Mapping[str, Any], *, excluded_ids: set[int] | None = None) -> dict[str, Any]:
    if not isinstance(document, Mapping):
        raise HoldoutRefused("document must be an object")
    document_id = _positive(document.get("document_id"), "document_id")
    if excluded_ids and document_id in excluded_ids:
        raise HoldoutRefused(f"document {document_id} overlaps the development corpus")
    text = document.get("retained_text")
    if not isinstance(text, str) or not text:
        raise HoldoutRefused("retained_text is required for full-document review")
    if document.get("content_sha256") != text_sha256(text):
        raise HoldoutRefused(f"document {document_id} content_sha256 differs from retained text")
    dimensions = {field: _required_text(document.get(field), f"document.{field}") for field in DIMENSIONS}
    if document.get("untouched") is not True:
        raise HoldoutRefused(f"document {document_id} is not explicitly marked untouched")
    if document.get("source_version") in (None, ""):
        raise HoldoutRefused(f"document {document_id} lacks source version identity")
    predictions = [_prediction(item, text, document_id) for item in document.get("predictions", [])]
    ids = [item["prediction_id"] for item in predictions]
    if len(ids) != len(set(ids)):
        raise HoldoutRefused(f"document {document_id} has duplicate prediction IDs")
    predicates = sorted({item["predicate"] for item in predictions}) or ["no_candidate"]
    return {"document_id": document_id, **dimensions, "source_version": str(document["source_version"]),
            "content_sha256": document["content_sha256"], "retained_text": text,
            "predictions": predictions, "predicates": predicates,
            "development_overlap": False}


def inventory_cached_sources(root: str | Path, *, excluded_ids: set[int] | None = None) -> dict[str, Any]:
    """Inventory local cached files only; never opens or downloads source content."""
    root = Path(root)
    if not root.is_dir():
        raise HoldoutRefused(f"cache root does not exist: {root}")
    rows = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        match = re.fullmatch(r"\d+", path.stem)
        source_id = int(path.stem) if match else None
        overlap = source_id in (excluded_ids or set()) if source_id is not None else False
        rows.append({"relative_path": str(path.relative_to(root)), "suffix": path.suffix.lower(),
                     "bytes": path.stat().st_size, "file_sha256": digest,
                     "source_id": source_id, "development_overlap": overlap,
                     "eligible_for_holdout": path.suffix.lower() == ".pdf" and not overlap})
    return {"kind": "kg-stage3-quality-holdout-inventory", "version": VERSION,
            "mode": "read-only", "cache_root": str(root), "files": rows,
            "accounting": {"files": len(rows), "pdf_files": sum(r["suffix"] == ".pdf" for r in rows),
                            "overlapping": sum(r["development_overlap"] for r in rows),
                            "eligible_unseen": sum(r["eligible_for_holdout"] for r in rows)},
            "digest": canonical_sha256({"cache_root": str(root), "files": rows})}


def select_source_balanced(documents: Sequence[Mapping[str, Any]], *, seed: str,
                           max_documents: int, excluded_ids: set[int] | None = None) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if not isinstance(seed, str) or not seed:
        raise HoldoutRefused("selection seed is required")
    if not isinstance(max_documents, int) or isinstance(max_documents, bool) or max_documents < 1:
        raise HoldoutRefused("max_documents must be positive")
    normalized = [validate_document(doc, excluded_ids=excluded_ids) for doc in documents]
    by_id = {doc["document_id"]: doc for doc in normalized}
    if len(by_id) != len(normalized):
        raise HoldoutRefused("document IDs must be unique")
    cells: dict[tuple[str, ...], list[int]] = defaultdict(list)
    for doc in normalized:
        for predicate in doc["predicates"]:
            cells[tuple(doc[field] for field in DIMENSIONS) + (predicate,)].append(doc["document_id"])
    if len(cells) > max_documents:
        raise HoldoutRefused(f"max_documents {max_documents} cannot cover {len(cells)} source/body/type/method/predicate cells")
    ranked = lambda ids: sorted(ids, key=lambda value: (hashlib.sha256((seed + "\0" + str(value)).encode()).hexdigest(), value))
    selected: list[int] = []
    for key in sorted(cells):
        for document_id in ranked(cells[key]):
            if document_id not in selected:
                selected.append(document_id)
                break
    remainder = [doc["document_id"] for doc in normalized if doc["document_id"] not in selected]
    selected.extend(ranked(remainder)[:max_documents - len(selected)])
    selected = sorted(selected, key=lambda value: value)
    selected_docs = [by_id[value] for value in selected]
    selected_cells = Counter(tuple(doc[field] for field in DIMENSIONS) + (predicate,)
                             for doc in selected_docs for predicate in doc["predicates"])
    ledger = {"algorithm": "sha256-rank-greedy-cell-cover/1.0", "seed": seed,
              "cell_fields": list(CELL_FIELDS), "population_documents": len(normalized),
              "selected_documents": len(selected_docs), "population_cells": len(cells),
              "selected_cells": len(selected_cells),
              "cells": [{"key": list(key), "population": len(cells[key]), "selected": selected_cells[key]}
                        for key in sorted(cells)]}
    return selected_docs, ledger


def build_packet(documents: Sequence[Mapping[str, Any]], *, seed: str, max_documents: int,
                 excluded_ids: set[int], development_population_digest: str,
                 inventory_binding: Mapping[str, Any], created_at: str) -> dict[str, Any]:
    if not isinstance(development_population_digest, str) or len(development_population_digest) != 64:
        raise HoldoutRefused("development population digest is required")
    selected, ledger = select_source_balanced(documents, seed=seed, max_documents=max_documents,
                                              excluded_ids=excluded_ids)
    items = []
    for doc in selected:
        items.append({"document": {k: doc[k] for k in ("document_id", *DIMENSIONS, "source_version", "content_sha256")},
                      "retained_text": doc["retained_text"], "predictions": doc["predictions"],
                      "review": {"coverage": None, "gold_actions": None, "prediction_labels": None},
                      "allowed": {"gold_temporal_attribution": list(TEMPORAL_LABELS),
                                  "prediction_status": list(LABEL_STATUS),
                                  "qualifier": list(QUALIFIER_LABELS),
                                  "item_association": list(ASSOCIATION_LABELS),
                                  "prediction_temporal_attribution": list(PREDICTION_TEMPORAL_LABELS)}})
    body = {"kind": KIND, "version": VERSION, "created_at": created_at, "mode": "review-only",
            "applied": False, "write_path": "absent by design", "target": TARGET,
            "development_population_digest": development_population_digest,
            "exclusion": {"document_ids": sorted(excluded_ids), "reason": "400-case development corpus; no reuse"},
            "inventory_binding": dict(inventory_binding), "sampling": ledger,
            "label_contract": {"whole_document": True, "labels_every_relevant_action": True,
                               "required_gold_fields": ["action_id", "predicate", "outcome_base", "qualifier",
                                                         "qualifier_text", "span", "item_reference", "temporal_attribution"],
                               "requires_candidate_match_or_miss": True},
            "metrics": {"precision": "tp/(tp+fp)", "recall": "tp/(tp+fn)",
                        "new_false_positives": "prediction_labels.status=fp",
                        "misses": "gold action without a matched prediction",
                        "qualifier_retention": "exact qualifier retention among matched actions",
                        "item_association": "correct item reference among matched actions",
                        "temporal_attribution": "correct temporal label among matched actions",
                        "slices": list(CELL_FIELDS),
                        "slice_semantics": {"precision": "candidate predicate; source/body/document_type/extraction_method from document",
                                             "recall": "gold predicate; source/body/document_type/extraction_method from document",
                                             "minimum_denominators_required": True}},
            "threshold_contract": {"approved": False, "approved_by": None, "approved_at": None,
                                   "minimum_denominators": None, "by_slice": None,
                                   "decision_required": "human must approve every observed slice and denominator floor"},
            "items": items}
    return {**body, "digest": canonical_sha256(body)}


def _ratio(num: int, den: int) -> dict[str, Any]:
    return {"numerator": num, "denominator": den, "value": num / den if den else None, "defined": bool(den)}


def evaluate_labels(packet: Mapping[str, Any], reviews: Mapping[int | str, Mapping[str, Any]]) -> dict[str, Any]:
    """Evaluate human labels; no matching or semantic truth is inferred here."""
    if packet.get("kind") != KIND or packet.get("digest") != canonical_sha256({k: v for k, v in packet.items() if k != "digest"}):
        raise HoldoutRefused("packet digest or kind is invalid")
    results, totals = [], Counter()
    slice_counts: dict[tuple[str, ...], Counter] = defaultdict(Counter)
    for item in packet.get("items", []):
        doc = item["document"]; key = str(doc["document_id"])
        review = reviews.get(key, reviews.get(doc["document_id"]))
        if not isinstance(review, Mapping) or review.get("coverage") != "complete":
            raise HoldoutRefused(f"document {key} is not marked complete")
        actions = review.get("gold_actions")
        predictions = review.get("prediction_labels")
        if not isinstance(actions, list) or not isinstance(predictions, list):
            raise HoldoutRefused(f"document {key} lacks complete gold actions and prediction labels")
        action_ids = [_required_text(a.get("action_id"), "gold action_id") for a in actions]
        if len(action_ids) != len(set(action_ids)):
            raise HoldoutRefused(f"document {key} has duplicate gold action IDs")
        for action in actions:
            for field in ("predicate", "outcome_base", "qualifier", "qualifier_text",
                          "span", "item_reference", "temporal_attribution"):
                if field not in action:
                    raise HoldoutRefused(f"document {key} gold action {action.get('action_id')} lacks {field}")
            _required_text(action.get("predicate"), "gold predicate")
            _required_text(action.get("outcome_base"), "gold outcome_base")
            _span(action["span"], item["retained_text"], "gold action span")
            if action.get("temporal_attribution") not in TEMPORAL_LABELS:
                raise HoldoutRefused(f"document {key} has invalid gold temporal attribution")
            if action.get("qualifier") is not None and not isinstance(action.get("qualifier"), str):
                raise HoldoutRefused(f"document {key} has invalid gold qualifier")
        pred_ids = {p.get("prediction_id") for p in item["predictions"]}
        if {p.get("prediction_id") for p in predictions} != pred_ids:
            raise HoldoutRefused(f"document {key} prediction labels are not complete")
        matched = set()
        action_by_id = {action["action_id"]: action for action in actions}
        prediction_by_id = {prediction["prediction_id"]: prediction for prediction in item["predictions"]}
        for label in predictions:
            for field in ("qualifier", "item_association", "temporal_attribution"):
                if field not in label:
                    raise HoldoutRefused(f"document {key} prediction label lacks {field}")
            if label["qualifier"] not in QUALIFIER_LABELS:
                raise HoldoutRefused(f"document {key} has invalid qualifier label")
            if label["item_association"] not in ASSOCIATION_LABELS:
                raise HoldoutRefused(f"document {key} has invalid item_association label")
            if label["temporal_attribution"] not in PREDICTION_TEMPORAL_LABELS:
                raise HoldoutRefused(f"document {key} has invalid temporal_attribution label")
            status = label.get("status")
            if status not in LABEL_STATUS:
                raise HoldoutRefused(f"document {key} has invalid prediction status")
            match = label.get("matched_action_id")
            if status == "tp":
                if match not in action_ids or match in matched:
                    raise HoldoutRefused(f"document {key} has invalid or duplicate TP match")
                if prediction_by_id[label["prediction_id"]]["predicate"] != action_by_id[match]["predicate"]:
                    raise HoldoutRefused(f"document {key} TP predicate differs from matched gold predicate")
                matched.add(match)
            elif match is not None:
                raise HoldoutRefused(f"document {key} FP cannot name a matched action")
            totals[status] += 1
            prediction = prediction_by_id[label["prediction_id"]]
            pred_key = tuple(doc[field] for field in DIMENSIONS) + (prediction["predicate"],)
            slice_counts[pred_key]["precision_tp" if status == "tp" else "precision_fp"] += 1
        for action in actions:
            if action["action_id"] not in matched:
                totals["fn"] += 1
            gold_key = tuple(doc[field] for field in DIMENSIONS) + (action["predicate"],)
            slice_counts[gold_key]["recall_tp" if action["action_id"] in matched else "recall_fn"] += 1
        for action in actions:
            if action["action_id"] in matched:
                pred = next(p for p in predictions if p.get("matched_action_id") == action["action_id"])
                for field, counter in (("qualifier", "qualifier_retained"), ("item_association", "item_association"),
                                       ("temporal_attribution", "temporal_attribution")):
                    value = pred.get(field)
                    if field == "qualifier" and value not in QUALIFIER_LABELS:
                        raise HoldoutRefused(f"document {key} has invalid qualifier label")
                    if field in {"item_association", "temporal_attribution"} and value not in PREDICTION_TEMPORAL_LABELS:
                        raise HoldoutRefused(f"document {key} has invalid {field} label")
                    good = value == "retained" if field == "qualifier" else value == "correct"
                    totals[counter + ("_ok" if good else "_bad")] += 1
        results.append({"document_id": int(key), "dimensions": {field: doc[field] for field in DIMENSIONS},
                        "tp": sum(p.get("status") == "tp" for p in predictions),
                        "fp": sum(p.get("status") == "fp" for p in predictions),
                        "fn": sum(a["action_id"] not in matched for a in actions)})
    slices = {}
    for key, counts in sorted(slice_counts.items()):
        precision_den = counts["precision_tp"] + counts["precision_fp"]
        recall_den = counts["recall_tp"] + counts["recall_fn"]
        slices["|".join(key)] = {"dimensions": dict(zip(CELL_FIELDS, key)),
                                  "counts": dict(counts),
                                  "precision": _ratio(counts["precision_tp"], precision_den),
                                  "recall": _ratio(counts["recall_tp"], recall_den),
                                  "minimum_denominator": {"precision": precision_den,
                                                           "recall": recall_den}}
    return {"packet_digest": packet["digest"], "documents": results,
            "totals": dict(totals),
            "metrics": {"precision": _ratio(totals["tp"], totals["tp"] + totals["fp"]),
                        "recall": _ratio(totals["tp"], totals["tp"] + totals["fn"]),
                        "new_false_positives": totals["fp"], "misses": totals["fn"],
                        "qualifier_retention": _ratio(totals["qualifier_retained_ok"], totals["qualifier_retained_ok"] + totals["qualifier_retained_bad"]),
                        "item_association": _ratio(totals["item_association_ok"], totals["item_association_ok"] + totals["item_association_bad"]),
                        "temporal_attribution": _ratio(totals["temporal_attribution_ok"], totals["temporal_attribution_ok"] + totals["temporal_attribution_bad"]),
                        "by_source_body_document_type_extraction_method_predicate": slices}}


def evaluate_thresholds(evaluation: Mapping[str, Any], thresholds: Mapping[str, Any]) -> dict[str, Any]:
    """Apply only an explicit, human-approved threshold contract.

    Every observed slice must have explicit precision/recall thresholds and
    meet both denominator floors. Missing or undersized slices are refusals,
    never implicit passes.
    """
    if not isinstance(thresholds, Mapping) or thresholds.get("approved") is not True:
        raise HoldoutRefused("human-approved thresholds are required")
    _required_text(thresholds.get("approved_by"), "thresholds.approved_by")
    _required_text(thresholds.get("approved_at"), "thresholds.approved_at")
    floors = thresholds.get("minimum_denominators")
    if (not isinstance(floors, Mapping) or
            not isinstance(floors.get("precision"), int) or floors.get("precision", 0) < 1 or
            not isinstance(floors.get("recall"), int) or floors.get("recall", 0) < 1):
        raise HoldoutRefused("positive precision and recall denominator floors are required")
    configured = thresholds.get("by_slice")
    slices = ((evaluation.get("metrics") or {}).get(
        "by_source_body_document_type_extraction_method_predicate") or {})
    if not isinstance(configured, Mapping):
        raise HoldoutRefused("thresholds.by_slice must explicitly cover every observed slice")
    decisions = {}
    for key, metrics in sorted(slices.items()):
        policy = configured.get(key)
        if not isinstance(policy, Mapping):
            raise HoldoutRefused(f"missing human threshold for slice {key}")
        for metric in ("precision", "recall"):
            value = metrics[metric]["value"]
            denominator = metrics[metric]["denominator"]
            threshold = policy.get(metric)
            if (not isinstance(threshold, (int, float)) or isinstance(threshold, bool)
                    or not 0 <= threshold <= 1):
                raise HoldoutRefused(f"invalid {metric} threshold for slice {key}")
            if denominator < floors[metric]:
                raise HoldoutRefused(f"undersized {metric} denominator for slice {key}: {denominator} < {floors[metric]}")
            if value is None or value < threshold:
                raise HoldoutRefused(f"slice {key} fails {metric} threshold")
        decisions[key] = {"precision": True, "recall": True,
                          "denominators": {metric: metrics[metric]["denominator"] for metric in ("precision", "recall")}}
    return {"status": "PASS", "approved_by": thresholds["approved_by"],
            "approved_at": thresholds["approved_at"], "minimum_denominators": dict(floors),
            "slices": decisions}


def build_correction_proposals(packet_path: str | Path, labels_path: str | Path, *, created_at: str) -> dict[str, Any]:
    """Preserve observable evidence and request human correction; make no adjudication."""
    packet = load_verified(packet_path)
    try:
        labels = load_verified(labels_path)
        labels_integrity = "digest-verified"
    except Exception:
        # The existing human-label export predates digest-bearing artifacts.  Do
        # not rewrite it; bind its exact file SHA-256 into this new artifact.
        labels = json.loads(Path(labels_path).read_text(encoding="utf-8"))
        if not isinstance(labels, Mapping):
            raise HoldoutRefused("correction labels must be an object")
        labels_integrity = "legacy-file-sha256-only"
    wanted = {"meeting_event_extraction:47369", "meeting_event_extraction:56289"}
    items = {item["case_id"]: item for item in packet.get("items", [])}
    out = []
    for case_id in sorted(wanted):
        item = items.get(case_id)
        if item is None:
            raise HoldoutRefused(f"correction case absent: {case_id}")
        label = (labels.get("labels") or {}).get(case_id)
        if not isinstance(label, Mapping):
            raise HoldoutRefused(f"correction label absent: {case_id}")
        out.append({"case_id": case_id, "source_identity": item["document"],
                    "candidate": item["candidate"], "evidence_record": item["evidence_record"],
                    "displayed_context": item["evidence"], "original_label": dict(label),
                    "observable_discrepancies": [
                        "review note and cited evidence require independent human alignment review" if case_id.endswith("47369")
                        else "cited span is boilerplate-like context and requires independent human alignment review"],
                    "proposed_action": "HUMAN_REVIEW_REQUIRED", "adjudication": None})
    body = {"kind": CORRECTION_KIND, "version": "1.0", "created_at": created_at,
            "mode": "evidence-only", "applied": False, "write_path": "absent by design",
            "source_bindings": [{"path": str(packet_path), "sha256": hashlib.sha256(Path(packet_path).read_bytes()).hexdigest(), "digest": packet["digest"]},
                                {"path": str(labels_path), "sha256": hashlib.sha256(Path(labels_path).read_bytes()).hexdigest(),
                                 "packet_digest": labels["packet_digest"], "integrity": labels_integrity}],
            "cases": out}
    return {**body, "digest": canonical_sha256(body)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-root", type=Path)
    parser.add_argument("--development-packet", type=Path,
                        help="immutable 400-case review packet used only for exclusions")
    parser.add_argument("--correction-packet", type=Path)
    parser.add_argument("--correction-labels", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args(argv)
    if args.inventory_root is not None:
        if args.development_packet is None:
            raise HoldoutRefused("--development-packet is required for a holdout inventory")
        development = load_verified(args.development_packet)
        excluded = {int(item["document"]["source_id"]) for item in development.get("items", [])}
        inventory = inventory_cached_sources(args.inventory_root, excluded_ids=excluded)
        body = {**inventory, "created_at": args.created_at,
                "development_exclusion": {"packet_path": str(args.development_packet),
                                           "packet_digest": development.get("digest"),
                                           "document_ids": sorted(excluded)}}
    elif args.correction_packet is not None and args.correction_labels is not None:
        body = build_correction_proposals(args.correction_packet, args.correction_labels,
                                           created_at=args.created_at)
    else:
        raise HoldoutRefused("choose --inventory-root or both correction inputs")
    digest = write_immutable(args.out, body)
    print(json.dumps({"status": "success", "path": str(args.out), "digest": digest,
                      "mode": body.get("mode")}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
