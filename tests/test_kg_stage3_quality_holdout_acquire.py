"""Adversarial tests for bounded untouched holdout acquisition."""

import hashlib
import json

import pytest

from scripts.kg import stage3_quality_holdout_acquire as A
from scripts.kg.stage2_artifacts import write_immutable


def test_spec_rejects_development_url_and_non_allowlisted_host():
    base = {"url": "https://public.destinyhosted.com/chanddocs/2026/CC/x.pdf",
            "source": "chandler", "body": "chandler-cc", "document_type": "Minutes"}
    with pytest.raises(A.AcquisitionRefused, match="overlaps"):
        A.validate_spec(base, development_urls={base["url"]}, development_pdf_hashes=set())
    bad = dict(base); bad["url"] = "https://www.phoenix.gov/x.pdf"
    with pytest.raises(A.AcquisitionRefused, match="allow-listed"):
        A.validate_spec(bad, development_urls=set(), development_pdf_hashes=set())


def test_pdf_cache_is_write_once_and_refuses_changed_bytes(tmp_path):
    path = tmp_path / "pdf" / "1.pdf"
    A.immutable_bytes(path, b"%PDF-1.7 first")
    A.immutable_bytes(path, b"%PDF-1.7 first")
    with pytest.raises(A.AcquisitionRefused, match="collision"):
        A.immutable_bytes(path, b"%PDF-1.7 changed")


def test_acquire_binds_manifest_and_unlabeled_packet_without_network(tmp_path, monkeypatch):
    development_packet = tmp_path / "development.json"
    body = {"kind": "kg-stage3-quality-benchmark-review", "digest": "placeholder",
            "items": [{"stratum": {"platform_or_source": "https://www.phoenix.gov/dev.pdf"},
                       "document": {"source_id": 7}}]}
    write_immutable(development_packet, body)
    dev_packet = json.loads(development_packet.read_text())
    # Rebuild the source body with the real immutable digest (write_immutable did this).
    dev_cache = tmp_path / "dev-cache"; dev_cache.mkdir()
    spec = {"url": "https://public.destinyhosted.com/chanddocs/2026/CC/x.pdf",
            "source": "chandler", "body": "chandler-cc", "document_type": "Minutes"}
    text = "RESULTS\nApproved\n"
    pdf_sha = hashlib.sha256(b"%PDF-new").hexdigest()
    fetched = {"document_id": A.document_id(spec["url"]), "url": spec["url"],
               "retrieved_at": "now", "http_status": 200, "response_headers": {},
               "bytes": 8, "pdf_sha256": pdf_sha, "cache_path": str(tmp_path / "out.pdf"),
               "development_overlap": False}
    def fake_fetch(value, *, cache_dir, development_pdf_hashes, timeout=30):
        return fetched
    def fake_extract(value, got):
        doc = {"document_id": value["document_id"], "source": value["source"], "body": value["body"],
               "document_type": value["document_type"], "extraction_method": "pymupdf_layout",
               "source_version": pdf_sha, "untouched": True, "retained_text": text,
               "content_sha256": A.text_sha256(text), "predictions": []}
        return doc, {"document_id": value["document_id"], "url": value["url"],
                     "pdf_sha256": pdf_sha, "retained_text_sha256": A.text_sha256(text),
                     "retained_text_chars": len(text), "extraction_method": "pymupdf_layout",
                     "prediction_count": 0, "layout_artifact": {"present": False, "kind": None, "version": None}}
    monkeypatch.setattr(A, "fetch_pdf", fake_fetch)
    monkeypatch.setattr(A, "extract_spec", fake_extract)
    manifest, packet, paths = A.acquire(specs=[spec], development_packet=development_packet,
                                        development_cache=dev_cache, out_dir=tmp_path / "out",
                                        seed="s", max_documents=1, created_at="2026-09-20T00:00:00Z")
    assert manifest["selection"]["human_labels"] == "absent by design"
    assert manifest["selection"]["thresholds"] == "absent by design"
    assert packet["review_policy"] if "review_policy" in packet else packet["mode"] == "review-only"
    assert paths["manifest_path"].endswith("manifest.json")
