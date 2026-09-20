#!/usr/bin/env python3
"""Acquire a bounded, immutable, untouched Stage 3 holdout from public PDFs.

The input manifest is metadata only; no semantic labels are inferred.  Every
selected URL is downloaded once into a write-once cache, verified as a PDF,
extracted with the current layout cascade, and emitted into the existing
review-only full-document holdout packet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
SCRIPTS = REPO / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from scripts.docs.extract import extract_document_safe
from scripts.entities.event_extract import extract_events_from_text
from scripts.kg.stage2_artifacts import load_verified, write_immutable
from scripts.kg.stage3_quality_holdout import build_packet, canonical_sha256, text_sha256

VERSION = "kg-stage3-quality-holdout-acquisition/1.0"
MAX_BYTES = 30 * 1024 * 1024
ALLOWED_HOSTS = ("public.destinyhosted.com", "ww2.scottsdaleaz.gov")
PDF_MAGIC = b"%PDF"


class AcquisitionRefused(ValueError):
    """A source, cache, or extraction result is not safe for the holdout."""


def document_id(url: str) -> int:
    """Stable synthetic ID, intentionally outside database identity space."""
    return int(hashlib.sha256(url.encode()).hexdigest()[:14], 16)


def source_host(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise AcquisitionRefused(f"source host/scheme is not allow-listed: {url}")
    return parsed.hostname


def validate_spec(spec: Mapping[str, Any], *, development_urls: set[str], development_pdf_hashes: set[str]) -> dict[str, Any]:
    if not isinstance(spec, Mapping):
        raise AcquisitionRefused("source specification must be an object")
    url = spec.get("url")
    if not isinstance(url, str) or not url:
        raise AcquisitionRefused("source URL is required")
    host = source_host(url)
    if url in development_urls:
        raise AcquisitionRefused("source URL overlaps the development packet")
    dimensions = {}
    for field in ("source", "body", "document_type"):
        value = spec.get(field)
        if not isinstance(value, str) or not value.strip():
            raise AcquisitionRefused(f"source {field} is required")
        dimensions[field] = value.strip()
    value = {"url": url, "host": host, **dimensions,
             "document_id": document_id(url), "untouched": True,
             "disjoint_basis": "URL not in 400-case packet; non-Phoenix allow-listed host"}
    if value["document_id"] <= 0:
        raise AcquisitionRefused("synthetic document ID is invalid")
    value["development_pdf_hashes_bound"] = len(development_pdf_hashes)
    return value


def immutable_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        if path.read_bytes() != data:
            raise AcquisitionRefused(f"immutable cache collision with different bytes: {path}")
        return
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def fetch_pdf(spec: Mapping[str, Any], *, cache_dir: Path, development_pdf_hashes: set[str], timeout: int = 30) -> dict[str, Any]:
    url = str(spec["url"])
    request = urllib.request.Request(url, headers={"User-Agent": "Poliscopic-Stage3-Holdout/1.0", "Accept": "application/pdf"})
    retrieved_at = datetime.now(timezone.utc).isoformat()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = response.read(MAX_BYTES + 1)
            headers = {str(k): str(v) for k, v in response.headers.items()}
            status = int(getattr(response, "status", 200) or 200)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise AcquisitionRefused(f"download failed for {url}: {exc}") from exc
    if status < 200 or status >= 300:
        raise AcquisitionRefused(f"download returned HTTP {status}: {url}")
    if len(data) > MAX_BYTES:
        raise AcquisitionRefused(f"source exceeds {MAX_BYTES} bytes: {url}")
    if not data.startswith(PDF_MAGIC):
        raise AcquisitionRefused(f"source is not a PDF: {url}")
    pdf_sha = hashlib.sha256(data).hexdigest()
    if pdf_sha in development_pdf_hashes:
        raise AcquisitionRefused(f"downloaded bytes overlap development PDF cache: {url}")
    doc_id = int(spec["document_id"])
    pdf_path = cache_dir / "pdf" / f"{doc_id}.pdf"
    immutable_bytes(pdf_path, data)
    return {"document_id": doc_id, "url": url, "retrieved_at": retrieved_at,
            "http_status": status, "response_headers": headers, "bytes": len(data),
            "pdf_sha256": pdf_sha, "cache_path": str(pdf_path), "development_overlap": False}


def extract_spec(spec: Mapping[str, Any], fetched: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    pdf_path = Path(str(fetched["cache_path"]))
    text, method, layout = extract_document_safe(pdf_path)
    if not isinstance(text, str) or not text.strip() or not method:
        raise AcquisitionRefused(f"current extractor produced no retained text: {fetched['url']}")
    events = extract_events_from_text(int(spec["document_id"]), text, layout_artifact=layout)
    predictions = []
    for index, event in enumerate(events):
        start, end = int(event["text_offset_start"]), int(event["text_offset_end"])
        if not 0 <= start < end <= len(text):
            raise AcquisitionRefused(f"extractor span is out of bounds: {fetched['url']}")
        predictions.append({"prediction_id": f"{spec['document_id']}:{index}",
                            "predicate": str(event["action_verb"]),
                            "outcome_base": event.get("outcome_base"),
                            "qualifier": event.get("outcome_qualifier"),
                            "qualifier_text": event.get("qualifier_text"),
                            "item_reference": event.get("layout_item_number"),
                            "temporal_attribution": None,
                            "span": {"coordinate_system": "unicode_codepoint_half_open", "start": start,
                                      "end": end, "sha256": text_sha256(text[start:end])}})
    document = {"document_id": int(spec["document_id"]), "source": spec["source"],
                "body": spec["body"], "document_type": spec["document_type"],
                "extraction_method": str(method), "source_version": fetched["pdf_sha256"],
                "untouched": True, "retained_text": text,
                "content_sha256": text_sha256(text), "predictions": predictions}
    extraction = {"document_id": int(spec["document_id"]), "url": spec["url"],
                  "pdf_sha256": fetched["pdf_sha256"], "retained_text_sha256": text_sha256(text),
                  "retained_text_chars": len(text), "extraction_method": str(method),
                  "prediction_count": len(predictions),
                  "layout_artifact": {"present": layout is not None,
                                      "kind": layout.get("kind") if layout else None,
                                      "version": layout.get("version") if layout else None}}
    return document, extraction


def acquire(*, specs: Sequence[Mapping[str, Any]], development_packet: Path,
            development_cache: Path, out_dir: Path, seed: str, max_documents: int,
            created_at: str, spec_sha256: str | None = None) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    out_dir.mkdir(parents=True, exist_ok=True)
    packet = load_verified(development_packet)
    development_urls = {str(item["stratum"]["platform_or_source"]) for item in packet.get("items", [])}
    development_ids = {int(item["document"]["source_id"]) for item in packet.get("items", [])}
    development_pdf_hashes = {hashlib.sha256(path.read_bytes()).hexdigest()
                              for path in development_cache.glob("*.pdf")}
    if len(development_pdf_hashes) != len(list(development_cache.glob("*.pdf"))):
        raise AcquisitionRefused("development cache has duplicate PDF bytes")
    normalized = [validate_spec(spec, development_urls=development_urls,
                                development_pdf_hashes=development_pdf_hashes) for spec in specs]
    if len({item["document_id"] for item in normalized}) != len(normalized):
        raise AcquisitionRefused("source specs have duplicate synthetic IDs")
    fetched, documents, extraction = [], [], []
    for spec in normalized:
        receipt = fetch_pdf(spec, cache_dir=out_dir, development_pdf_hashes=development_pdf_hashes)
        doc, report = extract_spec(spec, receipt)
        fetched.append({**receipt, "source": spec["source"], "body": spec["body"],
                        "document_type": spec["document_type"]})
        documents.append(doc); extraction.append(report)
    manifest_body = {"kind": "kg-stage3-quality-holdout-acquisition", "version": VERSION,
                     "created_at": created_at, "mode": "read-only", "applied": False,
                     "write_path": "absent by design", "development_packet": {
                         "path": str(development_packet), "digest": packet["digest"],
                         "urls": len(development_urls), "document_ids": len(development_ids),
                         "pdf_hashes": len(development_pdf_hashes)},
                     "selection": {"seed": seed, "max_documents": max_documents,
                                   "requested": len(specs), "selected": len(documents),
                                   "spec_sha256": spec_sha256,
                                   "dimensions": ["source", "body", "document_type", "extraction_method", "predicate"],
                                   "human_labels": "absent by design", "thresholds": "absent by design"},
                     "sources": fetched, "extractions": extraction}
    manifest = {**manifest_body, "digest": canonical_sha256(manifest_body)}
    binding_path = out_dir / "manifest.json"
    write_immutable(binding_path, manifest)
    inventory_binding = {"path": str(binding_path), "kind": manifest["kind"],
                         "digest": manifest["digest"], "file_sha256": hashlib.sha256(binding_path.read_bytes()).hexdigest()}
    packet_value = build_packet(documents, seed=seed, max_documents=max_documents,
                                excluded_ids=development_ids,
                                development_population_digest=packet["digest"],
                                inventory_binding=inventory_binding, created_at=created_at)
    holdout_path = out_dir / "holdout-packet.json"
    write_immutable(holdout_path, packet_value)
    return manifest, packet_value, {"manifest_path": str(binding_path), "packet_path": str(holdout_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--development-packet", type=Path, required=True)
    parser.add_argument("--development-cache", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--seed", required=True)
    parser.add_argument("--max-documents", type=int, required=True)
    parser.add_argument("--created-at", required=True)
    args = parser.parse_args(argv)
    specs = json.loads(args.spec.read_text(encoding="utf-8"))
    if not isinstance(specs, list) or not specs:
        raise AcquisitionRefused("spec must contain a nonempty list")
    manifest, packet, paths = acquire(specs=specs, development_packet=args.development_packet,
                                      development_cache=args.development_cache, out_dir=args.out_dir,
                                      seed=args.seed, max_documents=args.max_documents,
                                      created_at=args.created_at,
                                      spec_sha256=hashlib.sha256(args.spec.read_bytes()).hexdigest())
    print(json.dumps({"status": "success", "manifest": manifest["digest"],
                      "packet": packet["digest"], **paths}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
