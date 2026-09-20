#!/usr/bin/env python3
"""Compare reviewed legacy candidates with fresh layout-aware PDF extraction.

This is a read-only diagnostic benchmark. It downloads public source PDFs,
runs the governed extraction cascade in memory, and writes immutable JSON plus
a Markdown report. It never connects to a database or writes layout artifacts.

The reviewed packet measures legacy *candidate precision*, not document-level
recall. Accordingly, this benchmark asks whether an accepted legacy candidate
is retained and whether a rejected legacy candidate is suppressed at the same
local source context. It does not treat that result as population accuracy.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.docs.extract import extract_document_safe
from scripts.entities.event_extract import extract_events_from_text
from scripts.kg.stage2_artifacts import load_verified, write_immutable

DEFAULT_PACKET = REPO / "data/kg-plans/kg-stage3-quality-review-packet-20260916T002029Z.json"
DEFAULT_LABELS = REPO / "data/kg-review-labels/45280941fef959143ba7d754675d9ca0d27dfbd44cdb46be510bd5edbe1a9873.json"
DEFAULT_SOURCE = REPO / "data/kg-plans/kg-stage3-quality-candidate-source-20260916T002029Z.json"
DEFAULT_CORRECTIONS = REPO / "data/kg-review-labels/stage3-meeting-event-corrections-v2.json"
DEFAULT_ROOT = REPO / "data/document-layout-benchmark"
SEED = "document-layout-diagnostic-v1"
TOKEN_RE = re.compile(r"[a-z0-9]+")
STOPWORDS = {
    "a", "an", "and", "as", "at", "be", "by", "for", "from", "in",
    "is", "it", "of", "on", "or", "the", "this", "to", "was", "with",
}
OUTCOME_ORACLE_VERSION = "stage3-benchmark-outcome-oracle/1.0"
OUTCOME_ORACLE = {
    "adopted": "adopted",
    "amended": "amended",
    "approved": "approved",
    "approved subject to": "approved_with_conditions",
    "approved with stipulations": "approved_with_conditions",
    "called to order": "called_to_order",
    "continued": "continued",
    "deferred": "deferred",
    "denied": "denied",
    "discussed": "discussed",
    "discussion only": "discussed",
    "extended": "extended",
    "for discussion": "discussed",
    "introduced": "introduced",
    "no action": "no_action",
    "no response": "no_action",
    "preliminary review": "discussed",
    "received": "received",
    "received and filed": "received",
    "sustained": "sustained",
    "tabled": "tabled",
    "vacated": "vacated",
    "withdrawn": "withdrawn",
}


def _outcome_oracle_digest() -> str:
    body = json.dumps(OUTCOME_ORACLE, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _load(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected object: {path}")
    return value


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rank(case_id: str) -> str:
    return hashlib.sha256(f"{SEED}:{case_id}".encode()).hexdigest()


def _normalized_predicate(value: str) -> str:
    return " ".join(str(value).split()).casefold()


def _tokens(value: str) -> set[str]:
    return {word for word in TOKEN_RE.findall(value.casefold()) if word not in STOPWORDS}


def _context_score(left: str, right: str) -> float:
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    overlap = len(a & b)
    containment = overlap / min(len(a), len(b))
    jaccard = overlap / len(a | b)
    return round((0.7 * containment) + (0.3 * jaccard), 4)


def _local_context(text: str, start: int, end: int) -> str:
    """Return the action line plus one adjacent non-empty line each way."""
    lines = text.splitlines()
    offset = 0
    hit = 0
    for index, line in enumerate(lines):
        line_end = offset + len(line)
        if start <= line_end or (start < offset and end <= line_end):
            hit = index
            break
        offset = line_end + 1
    selected = []
    for index in range(max(0, hit - 1), min(len(lines), hit + 2)):
        if lines[index].strip():
            selected.append(lines[index].strip())
    return " ".join(selected)


def _canonical_outcome(predicate: str) -> str:
    normalized = _normalized_predicate(predicate)
    return OUTCOME_ORACLE.get(normalized, normalized.replace(" ", "_"))


def apply_label_corrections(
    labels: dict[str, Any], correction_document: dict[str, Any] | None,
    *, base_path: Path,
) -> dict[str, Any]:
    """Apply a validated additive overlay without changing the base ledger."""
    if not correction_document:
        return labels
    from scripts.kg.stage3_review_label_corrections import validate_corrections

    problems = validate_corrections(correction_document, base_path)
    if problems:
        raise ValueError("invalid label corrections: " + "; ".join(problems))
    effective = json.loads(json.dumps(labels))
    for correction in correction_document["corrections"]:
        target = effective["labels"][correction["case_id"]]
        target.update(correction["after"])
        target["correction_reason"] = correction["reason"]
        target["corrected_by_overlay"] = True
    return effective


def _classify_match(
    decision: str, matched: bool, same_outcome_exists: bool
) -> tuple[str, bool | None]:
    """Separate extraction absence from unresolved context alignment."""
    if decision == "accept":
        if matched:
            return "retained", True
        if same_outcome_exists:
            return "alignment_unresolved", None
        return "missed", False
    if matched:
        return "persisted", False
    return "suppressed", True


def _round_robin(records: Iterable[dict[str, Any]], count: int) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[_normalized_predicate(record["predicate"])].append(record)
    for values in groups.values():
        values.sort(key=lambda value: _rank(value["case_id"]))
    chosen: list[dict[str, Any]] = []
    keys = sorted(groups, key=lambda key: hashlib.sha256(f"{SEED}:{key}".encode()).hexdigest())
    while len(chosen) < count and keys:
        remaining = []
        for key in keys:
            if groups[key] and len(chosen) < count:
                chosen.append(groups[key].pop(0))
            if groups[key]:
                remaining.append(key)
        keys = remaining
    return chosen


def _review_records(
    packet: dict[str, Any], labels: dict[str, Any], source: dict[str, Any],
    *, unique_documents: bool = True,
) -> list[dict[str, Any]]:
    """Materialize reviewed records, optionally deduplicated by source PDF."""
    source_cases = {case["case_id"]: case for case in source["candidate_cases"]}
    records = []
    seen_sources: set[int] = set()
    for item in sorted(packet["items"], key=lambda value: _rank(value["case_id"])):
        label = labels["labels"].get(item["case_id"])
        original = source_cases.get(item["case_id"])
        source_id = int(item["document"]["source_id"])
        if not label or not original or (unique_documents and source_id in seen_sources):
            continue
        seen_sources.add(source_id)
        records.append({
            "case_id": item["case_id"],
            "source_id": source_id,
            "decision": label["decision"],
            "notes": label.get("notes", ""),
            "source_detail": label.get("source_detail"),
            "predicate": item["candidate"]["predicate"],
            "body": item["projection"]["dimensions"]["body"],
            "legacy_method": item["projection"]["dimensions"]["extraction_method"],
            "url": item["projection"]["dimensions"]["platform_or_source"],
            "evidence": item["evidence"],
            "legacy_text": original["retained_text"],
        })
    return records


def select_full_packet(
    packet: dict[str, Any], labels: dict[str, Any], source: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return every reviewed candidate in stable packet order, including shared PDFs."""
    records = _review_records(
        packet, labels, source, unique_documents=False,
    )
    expected = sum(
        item["case_id"] in labels["labels"]
        for item in packet["items"]
    )
    if len(records) != expected:
        raise ValueError(f"materialized {len(records)} of {expected} labeled packet cases")
    return records


def select_sample(
    packet: dict[str, Any], labels: dict[str, Any], source: dict[str, Any], size: int
) -> list[dict[str, Any]]:
    """Choose a deterministic, failure-enriched sample with unique documents."""
    if size < 4 or size % 2:
        raise ValueError("sample size must be an even integer of at least 4")
    records = _review_records(packet, labels, source)

    half = size // 2
    rejects = [record for record in records if record["decision"] == "reject"]
    accepts = [record for record in records if record["decision"] == "accept"]
    # The motivating failure is always represented when available.
    selected_rejects = [r for r in rejects if r["case_id"] == "meeting_event_extraction:40302"]
    used = {r["source_id"] for r in selected_rejects}
    selected_rejects += _round_robin(
        (r for r in rejects if r["source_id"] not in used), half - len(selected_rejects)
    )
    # Include every unique document represented by the old OCR path, then add
    # predicate-diverse pdftotext controls.
    ocr_accepts = sorted(
        (r for r in accepts if r["legacy_method"] == "ocr_local"),
        key=lambda value: _rank(value["case_id"]),
    )
    selected_accepts = ocr_accepts[:half]
    used = {r["source_id"] for r in selected_accepts}
    selected_accepts += _round_robin(
        (r for r in accepts if r["source_id"] not in used), half - len(selected_accepts)
    )
    selected = selected_rejects[:half] + selected_accepts[:half]
    if len(selected) != size:
        raise ValueError(f"could select only {len(selected)} of {size} cases")
    return sorted(selected, key=lambda value: (value["decision"], value["case_id"]))


def select_replay(
    packet: dict[str, Any], labels: dict[str, Any], source: dict[str, Any],
    case_ids: list[str],
) -> list[dict[str, Any]]:
    """Rebuild the exact prior case set using current correction overlays."""
    records = {record["case_id"]: record for record in _review_records(packet, labels, source)}
    missing = [case_id for case_id in case_ids if case_id not in records]
    if missing:
        raise ValueError(f"replay cases absent from bound packet: {missing}")
    selected = [records[case_id] for case_id in case_ids]
    if len({record["source_id"] for record in selected}) != len(selected):
        raise ValueError("replay is not a unique-document sample")
    return selected


def _download(url: str, destination: Path) -> tuple[Path, bool]:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() and destination.read_bytes()[:4] == b"%PDF":
        return destination, True
    request = urllib.request.Request(url, headers={"User-Agent": "Poliscopic layout benchmark/1.0"})
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
            if not body.startswith(b"%PDF"):
                raise ValueError(f"response is not a PDF ({len(body)} bytes)")
            temporary = destination.with_suffix(".pdf.part")
            temporary.write_bytes(body)
            temporary.replace(destination)
            return destination, False
        except (OSError, urllib.error.URLError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1 + attempt)
    raise RuntimeError(f"download failed: {url}: {last_error}")


def compare_case(record: dict[str, Any], cache_dir: Path) -> dict[str, Any]:
    pdf_path, cached = _download(record["url"], cache_dir / f'{record["source_id"]}.pdf')
    started = time.monotonic()
    new_text, method, artifact = extract_document_safe(pdf_path)
    seconds = round(time.monotonic() - started, 3)
    if not new_text or not method or not artifact:
        return {**{k: v for k, v in record.items() if k != "legacy_text"},
                "status": "extraction_failed", "new_method": method,
                "elapsed_seconds": seconds, "pdf_sha256": _digest(pdf_path),
                "cached_pdf": cached}

    start, end = int(record["evidence"]["start"]), int(record["evidence"]["end"])
    legacy_context = _local_context(record["legacy_text"], start, end)
    expected_outcome = _canonical_outcome(record["predicate"])
    events = extract_events_from_text(record["source_id"], new_text, artifact)
    scored = []
    for event in events:
        score = _context_score(legacy_context, event["raw_text"])
        scored.append((score, event))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    same_outcome = [pair for pair in scored if pair[1]["outcome"] == expected_outcome]
    matched_score, matched_event = same_outcome[0] if same_outcome else (0.0, None)
    matched = bool(matched_event and matched_score >= 0.28)
    status, benchmark_correct = _classify_match(
        record["decision"], matched, bool(same_outcome)
    )
    closest_score, closest_event = scored[0] if scored else (0.0, None)
    pages = artifact.get("pages", [])
    rows = [row for page in pages for row in page.get("rows", [])]
    boxed_rows = sum(1 for row in rows if row.get("bbox") is not None)
    legacy_tokens, new_tokens = _tokens(record["legacy_text"]), _tokens(new_text)
    text_overlap = len(legacy_tokens & new_tokens) / max(len(legacy_tokens | new_tokens), 1)
    return {
        **{k: v for k, v in record.items() if k != "legacy_text"},
        "status": status,
        "benchmark_correct": benchmark_correct,
        "expected_outcome": expected_outcome,
        "legacy_context": legacy_context,
        "new_method": method,
        "elapsed_seconds": seconds,
        "pdf_sha256": _digest(pdf_path),
        "cached_pdf": cached,
        "new_text_chars": len(new_text),
        "legacy_text_chars": len(record["legacy_text"]),
        "vocabulary_jaccard": round(text_overlap, 4),
        "page_count": len(pages),
        "row_count": len(rows),
        "boxed_row_count": boxed_rows,
        "table_count": len(artifact.get("tables", [])),
        "new_event_count": len(events),
        "matched_context_score": matched_score,
        "matched_event": matched_event,
        "closest_context_score": closest_score,
        "closest_event": closest_event,
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    usable = [row for row in results if row.get("status") != "extraction_failed"]
    resolved = [row for row in usable if row.get("benchmark_correct") is not None]
    accepts = [row for row in usable if row["decision"] == "accept"]
    rejects = [row for row in usable if row["decision"] == "reject"]
    methods: dict[str, int] = defaultdict(int)
    for row in usable:
        methods[str(row["new_method"])] += 1
    return {
        "documents": len(results),
        "extracted": len(usable),
        "failed": len(results) - len(usable),
        "accepted_controls": len(accepts),
        "accepted_retained": sum(row["status"] == "retained" for row in accepts),
        "accepted_alignment_unresolved": sum(
            row["status"] == "alignment_unresolved" for row in accepts
        ),
        "rejected_controls": len(rejects),
        "rejected_suppressed": sum(row["status"] == "suppressed" for row in rejects),
        "diagnostic_agreement": round(
            sum(bool(row.get("benchmark_correct")) for row in resolved) / max(len(resolved), 1), 4
        ),
        "diagnostic_resolved": len(resolved),
        "new_methods": dict(sorted(methods.items())),
        "documents_with_detected_tables": sum(row.get("table_count", 0) > 0 for row in usable),
        "mean_vocabulary_jaccard": round(
            sum(row.get("vocabulary_jaccard", 0) for row in usable) / max(len(usable), 1), 4
        ),
        "elapsed_seconds": round(sum(row.get("elapsed_seconds", 0) for row in results), 3),
    }


def _markdown(payload: dict[str, Any]) -> str:
    summary = payload["summary"]
    lines = [
        "# Layout-aware extraction diagnostic",
        "",
        f"Generated: `{payload['created_at']}`  ",
        f"Cases: **{summary['documents']} reviewed candidates** "
        f"({summary['accepted_controls']} accepted controls; {summary['rejected_controls']} rejected/error controls)",
        "",
        "> This is a deliberately failure-enriched diagnostic sample, not a population accuracy estimate. "
        "It measures survival/suppression of previously reviewed candidates, not recall of every event in each document.",
        "",
        "## Summary",
        "",
        f"- Extracted: **{summary['extracted']}/{summary['documents']}**",
        f"- Accepted legacy candidates retained: **{summary['accepted_retained']}/{summary['accepted_controls']}**",
        f"- Accepted controls with unresolved alignment: **{summary['accepted_alignment_unresolved']}**",
        f"- Rejected legacy candidates suppressed: **{summary['rejected_suppressed']}/{summary['rejected_controls']}**",
        f"- Diagnostic agreement: **{summary['diagnostic_agreement']:.1%}**",
        f"- New extraction methods: `{json.dumps(summary['new_methods'], sort_keys=True)}`",
        f"- Documents with detected tables: **{summary['documents_with_detected_tables']}**",
        f"- Mean legacy/new vocabulary Jaccard: **{summary['mean_vocabulary_jaccard']:.3f}**",
        f"- Extraction time: **{summary['elapsed_seconds']:.1f}s**",
        "",
        "## Case results",
        "",
        "| Case | Human label | Legacy → new | Result | Context score | Tables |",
        "|---|---|---|---|---:|---:|",
    ]
    for row in payload["results"]:
        lines.append(
            f"| [{row['case_id']}]({row['url']}) | {row['decision']} | "
            f"`{_normalized_predicate(row['predicate'])}` → `{row.get('new_method')}` | "
            f"**{row['status']}** | {row.get('matched_context_score', 0):.3f} | "
            f"{row.get('table_count', 0)} |"
        )
    lines += ["", "## Audit details", ""]
    for row in payload["results"]:
        lines += [
            f"### {row['case_id']} — {row['status']}",
            "",
            f"- Source: [{row['body']}]({row['url']})",
            f"- Human decision: `{row['decision']}`; legacy method: `{row['legacy_method']}`; "
            f"new method: `{row.get('new_method')}`",
            f"- Legacy predicate: `{_normalized_predicate(row['predicate'])}`; "
            f"canonical outcome: `{row.get('expected_outcome')}`",
            f"- Reviewer note: {row.get('notes') or '—'}",
            f"- Legacy local context: `{html.escape(row.get('legacy_context', ''))}`",
        ]
        closest = row.get("closest_event")
        if closest:
            lines.append(
                f"- Closest new row/action ({row.get('closest_context_score', 0):.3f}): "
                f"`{html.escape(closest.get('raw_text', ''))}` → `{closest.get('outcome')}`"
            )
        else:
            lines.append("- Closest new row/action: —")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-size", type=int, default=24)
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--corrections", type=Path, default=DEFAULT_CORRECTIONS)
    parser.add_argument("--replay", type=Path)
    parser.add_argument(
        "--full-packet", action="store_true",
        help="evaluate every reviewed candidate, including multiple cases from one PDF",
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args()

    packet, labels, source = _load(args.packet), _load(args.labels), _load(args.source)
    corrections = load_verified(args.corrections) if args.corrections.exists() else None
    labels = apply_label_corrections(labels, corrections, base_path=args.labels)
    replay = load_verified(args.replay) if args.replay else None
    if replay and args.full_packet:
        parser.error("--replay and --full-packet are mutually exclusive")
    if replay:
        replay_ids = [str(result["case_id"]) for result in replay.get("results", [])]
        selected = select_replay(packet, labels, source, replay_ids)
        if len(selected) != args.sample_size:
            raise ValueError(
                f"replay contains {len(selected)} cases, expected --sample-size {args.sample_size}"
            )
    elif args.full_packet:
        selected = select_full_packet(packet, labels, source)
    else:
        selected = select_sample(packet, labels, source, args.sample_size)
    cache_dir = args.root / "pdf-cache"
    results = []
    for index, record in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {record['case_id']} {record['url']}", flush=True)
        try:
            result = compare_case(record, cache_dir)
        except Exception as exc:
            result = {**{k: v for k, v in record.items() if k != "legacy_text"},
                      "status": "extraction_failed", "error": str(exc)}
        results.append(result)
        print(f"  -> {result['status']} ({result.get('new_method', 'none')})", flush=True)

    created_at = datetime.now(timezone.utc).isoformat()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = args.root / "runs"
    run_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "kind": "document-layout-diagnostic-benchmark",
        "version": "1.1",
        "created_at": created_at,
        "mode": "read-only",
        "sample_policy": {
            "seed": SEED,
            "unique_documents": not args.full_packet,
            "full_packet": args.full_packet,
            "accepted_controls": sum(record["decision"] == "accept" for record in selected),
            "rejected_controls": sum(record["decision"] == "reject" for record in selected),
            "failure_enriched": True,
            "population_estimate": False,
            "match_threshold": 0.28,
            "replay_of": str(args.replay) if args.replay else None,
        },
        "bindings": {
            "outcome_oracle": {
                "version": OUTCOME_ORACLE_VERSION,
                "sha256": _outcome_oracle_digest(),
            },
            "packet": {"path": str(args.packet), "sha256": _digest(args.packet)},
            "labels": {"path": str(args.labels), "sha256": _digest(args.labels)},
            "source": {"path": str(args.source), "sha256": _digest(args.source)},
            "corrections": (
                {"path": str(args.corrections), "sha256": _digest(args.corrections)}
                if corrections else None
            ),
            "replay": (
                {"path": str(args.replay), "sha256": _digest(args.replay)}
                if replay else None
            ),
        },
        "summary": _summary(results),
        "results": results,
    }
    json_path = run_dir / f"layout-diagnostic-{stamp}.json"
    markdown_path = run_dir / f"layout-diagnostic-{stamp}.md"
    digest = write_immutable(json_path, payload)
    markdown_path.write_text(_markdown({**payload, "digest": digest}), encoding="utf-8")
    print(f"JSON {json_path}", flush=True)
    print(f"REPORT {markdown_path}", flush=True)
    print(f"DIGEST {digest}", flush=True)
    print(json.dumps(payload["summary"], sort_keys=True), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
