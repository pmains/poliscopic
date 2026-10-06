"""
Shared constants for the document downloader pipeline.

All paths, hostnames, failure-method tuples, and environment-dependent
strings live here so the other modules can import them without duplication.
"""
import os
from pathlib import Path

# ── Project paths ───────────────────────────────────────────────────────────

DOWNLOAD_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "doc_downloads"
"""Where downloaded PDFs are cached before extraction."""
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ── HTTP ────────────────────────────────────────────────────────────────────

USER_AGENT = "Poliscopic/1.0 (document-downloader; +https://poliscopic.com)"
"""User-Agent header sent with every HTTP request."""


# ── Windows remote OCR (SSH to Pete's Windows machine) ─────────────────────

WINDOWS_SSH_HOST = os.environ.get("POLISCOPIC_REMOTE_OCR_HOST", "").strip()
"""Optional SSH hostname for a separately configured remote OCR worker."""

WINDOWS_DOWNLOAD_DIR = os.environ.get("POLISCOPIC_REMOTE_OCR_DIR", "").strip()
"""Optional temporary directory on the remote OCR worker."""

WINDOWS_TESSERACT = os.environ.get("POLISCOPIC_REMOTE_TESSERACT", "").strip()
"""Optional Tesseract executable path on the remote OCR worker."""

WINDOWS_PYTHON = os.environ.get("POLISCOPIC_REMOTE_PYTHON", "").strip()
"""Optional Python interpreter on the remote OCR worker."""

WINDOWS_PADDLE_HELPER = os.environ.get("POLISCOPIC_REMOTE_PADDLE_HELPER", "").strip()
"""Optional PaddleOCR helper path on the remote OCR worker."""


# ── Failure-state definitions ───────────────────────────────────────────────

FAILURE_METHODS = ("failed", "download_failed", "extraction_failed", "process_error")
"""
Tuple of ``text_extraction_method`` values that represent failed extractions.

Includes the legacy ``"failed"`` value for backward compatibility with batches
run before the typed failure distinction was introduced (``extraction_failed``,
``download_failed``, ``process_error``).
"""

# Signal-to-name map for subprocess crashes (segfaults from PyMuPDF)
SIGNAL_NAMES: dict[int, str] = {
    11: "SIGSEGV",
    10: "SIGBUS",
    6:  "SIGABRT",
}
"""Mapping of Unix signal numbers → human-readable names for crash logging."""
