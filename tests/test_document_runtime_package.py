"""The Stage 3 document runtime must survive a clean repository checkout."""

from __future__ import annotations

import importlib
import subprocess
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
RUNTIME_FILES = {
    "scripts/docs/__init__.py",
    "scripts/docs/doc_constants.py",
    "scripts/docs/doc_db.py",
    "scripts/docs/extract.py",
    "scripts/docs/layout_extract.py",
    "scripts/docs/layout_roles.py",
    "scripts/docs/benchmark_layout_sample.py",
}


def test_document_runtime_closure_is_tracked_and_importable():
    tracked = set(subprocess.run(
        ["git", "ls-files"], cwd=REPO, check=True,
        capture_output=True, text=True,
    ).stdout.splitlines())
    assert RUNTIME_FILES <= tracked
    for module in (
        "scripts.docs.extract",
        "scripts.docs.layout_extract",
        "scripts.docs.benchmark_layout_sample",
        "scripts.entities.event_extract",
        "scripts.ingest_docs",
    ):
        importlib.import_module(module)


def test_remote_ocr_is_disabled_without_explicit_configuration(monkeypatch, tmp_path):
    from scripts.docs import extract

    monkeypatch.setattr(extract, "WINDOWS_SSH_HOST", "")
    monkeypatch.setattr(extract, "WINDOWS_DOWNLOAD_DIR", "")
    monkeypatch.setattr(
        extract.subprocess, "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("remote command ran")),
    )
    pdf = tmp_path / "source.pdf"
    pdf.write_bytes(b"not needed")
    assert extract._try_ocr_windows(pdf) == (None, None)
    assert extract._try_ocr_windows_paddle(pdf) == (None, None)
