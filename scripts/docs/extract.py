"""
Text-extraction pipeline for PDF documents.

Provides ``extract_document_safe()``, the layout-preserving entry point, and
the compatibility wrapper ``extract_text_safe()``.  Native words and OCR words
retain page coordinates in immutable artifacts; plain text remains available
for search and legacy consumers.

Method order (fastest / most authoritative first):
  1. pymupdf_layout — born-digital word boxes and visual rows
  2. pdftotext_layout — coordinate-less Poppler fallback
  3. tesseract_tsv — local OCR with word boxes and confidence
  4. ocr_windows  — Tesseract on the remote Windows machine
  5. ocr_windows_paddle — PaddleOCR (deep learning) on the remote Windows machine
"""
import json
import logging
import multiprocessing
import subprocess
from pathlib import Path
from typing import Optional

from docs.doc_constants import (
    WINDOWS_DOWNLOAD_DIR,
    WINDOWS_PADDLE_HELPER,
    WINDOWS_PYTHON,
    WINDOWS_SSH_HOST,
    WINDOWS_TESSERACT,
)
from docs.layout_extract import (
    artifact_from_plain_text,
    extract_native,
    extract_tesseract_tsv,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════


def _extract_target(path_str: str, q: multiprocessing.Queue) -> None:
    """Child-process target for extract_text_safe.

    Must be at module level so multiprocessing can pickle it.
    """
    from pathlib import Path
    import signal

    signal.signal(signal.SIGSEGV, signal.SIG_DFL)
    signal.signal(signal.SIGBUS, signal.SIG_DFL)
    try:
        # _extract_text is defined below in this module; the spawned
        # child re-imports the full module so it's always available.
        text, method = _extract_text(Path(path_str))
        q.put((text, method))
    except Exception as exc:
        q.put((None, f"subprocess_exception:{exc}"))


def extract_text_safe(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Extract text from a PDF, returning ``(text, method_name)``.

    Runs ``_extract_text()`` inline (no subprocess isolation).  The subprocess
    approach via multiprocessing deadlocks when run inside ThreadPoolExecutor
    and/or nohupd background sessions, so extraction runs directly.

    For the current document set (<0.5% segfault rate from MuPDF) the risk of
    a crash killing the caller is acceptable.  If crash rates rise, refactor
    the downloader to batch documents and restart on failure.

    Returns
    -------
    (text, method_name)
        On success.  *text* is the extracted plain text; *method_name* is
        one of ``"pymupdf_layout"``, ``"pdftotext_layout"``,
        ``"tesseract_tsv"``, ``"ocr_windows"``, or
        ``"ocr_windows_paddle"``.
    (None, method_name)
        If all methods failed.
    """
    text, method, _artifact = extract_document_safe(pdf_path)
    return text, method


def extract_document_safe(
    pdf_path: Path,
) -> tuple[Optional[str], Optional[str], Optional[dict]]:
    """Extract retained text plus a layout artifact without hiding failures.

    The first two successful tiers retain native source text.  OCR is used only
    when native extraction cannot produce meaningful text.  Remote legacy
    fallbacks remain available but are explicitly coordinate-less.
    """
    try:
        native = extract_native(pdf_path)
        if native:
            text, artifact = native
            return text, "pymupdf_layout", artifact

        text, method = _try_pdftotext(pdf_path)
        if text and method:
            artifact = artifact_from_plain_text(
                pdf_path, text, method=method, tool="pdftotext",
                tool_version=_command_version(["pdftotext", "-v"]),
            )
            return text, method, artifact

        ocr = extract_tesseract_tsv(pdf_path)
        if ocr:
            text, artifact = ocr
            return text, "tesseract_tsv", artifact

        for extractor in (_try_ocr_windows, _try_ocr_windows_paddle):
            text, method = extractor(pdf_path)
            if text and method:
                artifact = artifact_from_plain_text(
                    pdf_path, text, method=method, tool=method,
                    tool_version=None,
                )
                return text, method, artifact
        return None, None, None
    except Exception as exc:
        return None, f"subprocess_exception:{exc}", None


def _command_version(command: list[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except Exception:
        return None
    lines = (result.stdout or result.stderr).splitlines()
    return lines[0].strip() if lines else None


# ═══════════════════════════════════════════════════════════════════════════
# Extraction cascade (private)
# ═══════════════════════════════════════════════════════════════════════════
#   The functions below are tried in order by _extract_text().  Each returns
#   (text, method_name) when it gets meaningful content, or raises /
#   returns (None, None) to pass control to the next method.


def _extract_text(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Compatibility path routed through the governed cascade."""
    text, method, _artifact = extract_document_safe(pdf_path)
    return text, method


# ── Method 1: PyMuPDF (born-digital PDFs) ───────────────────────────────────


def _try_pymupdf(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Compatibility wrapper that still preserves native geometry."""
    result = extract_native(pdf_path)
    if result:
        return result[0], "pymupdf_layout"
    return None, None


# ── Method 2: pdftotext (Poppler CLI) ───────────────────────────────────────


def _try_pdftotext(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Extract text via the Poppler ``pdftotext`` command-line tool.

    Handles some PDF formats that confuse PyMuPDF.  30-second timeout per
    document (large packets can be slow)."""
    try:
        result = subprocess.run(
            ["pdftotext", "-layout", str(pdf_path), "-"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and len(result.stdout.strip()) > 50:
            return result.stdout, "pdftotext_layout"
    except Exception:
        pass
    return None, None


# ── Method 3: Local Tesseract OCR ───────────────────────────────────────────


def _try_ocr_local(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """Compatibility wrapper that requests Tesseract TSV word boxes."""
    result = extract_tesseract_tsv(pdf_path)
    if result:
        return result[0], "tesseract_tsv"
    return None, None


# ── Method 4: Windows Tesseract OCR (remote) ────────────────────────────────


def _try_ocr_windows(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """SCP the PDF to the remote Windows machine and run Tesseract there.

    Useful when the local machine lacks the fonts, language packs, or
    memory that Tesseract needs for a particular document."""
    if not all((WINDOWS_SSH_HOST, WINDOWS_DOWNLOAD_DIR, WINDOWS_TESSERACT)):
        return None, None
    try:
        _ensure_remote_dir_exists()
        remote_path = _scp_to_windows(pdf_path)
        result = subprocess.run(
            [
                "ssh",
                WINDOWS_SSH_HOST,
                f'"{WINDOWS_TESSERACT}" "{remote_path}" stdout -l eng',
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        subprocess.run(
            ["ssh", WINDOWS_SSH_HOST, f'del "{remote_path}"'],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0 and len(result.stdout.strip()) > 20:
            return result.stdout, "ocr_windows"
    except Exception:
        pass
    return None, None


# ── Method 5: Windows PaddleOCR (remote, deep learning) ─────────────────────


def _try_ocr_windows_paddle(pdf_path: Path) -> tuple[Optional[str], Optional[str]]:
    """SCP the PDF to the remote Windows machine and run PaddleOCR there.

    PaddleOCR uses a deep-learning model that is considerably more accurate
    than Tesseract on noisy scans, handwritten marginalia, or unusual fonts.
    180-second timeout.
    """
    if not all((WINDOWS_SSH_HOST, WINDOWS_DOWNLOAD_DIR,
                WINDOWS_PYTHON, WINDOWS_PADDLE_HELPER)):
        return None, None
    try:
        _ensure_remote_dir_exists()
        remote_path = _scp_to_windows(pdf_path)
        result = subprocess.run(
            [
                "ssh",
                WINDOWS_SSH_HOST,
                f'"{WINDOWS_PYTHON}" "{WINDOWS_PADDLE_HELPER}" "{remote_path}"',
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        subprocess.run(
            ["ssh", WINDOWS_SSH_HOST, f'del "{remote_path}"'],
            capture_output=True,
            timeout=10,
        )
        if result.returncode == 0:
            try:
                text_out = json.loads(result.stdout.strip())
                if text_out and len(text_out.strip()) > 20:
                    return text_out, "ocr_windows_paddle"
            except (json.JSONDecodeError, ValueError):
                pass
    except Exception:
        pass
    return None, None


# ── Windows helpers ─────────────────────────────────────────────────────────


def _ensure_remote_dir_exists() -> None:
    """Create the remote download directory on the Windows machine if needed."""
    if not WINDOWS_SSH_HOST or not WINDOWS_DOWNLOAD_DIR:
        raise RuntimeError("remote OCR host and directory are not configured")
    subprocess.run(
        [
            "ssh",
            WINDOWS_SSH_HOST,
            f'if not exist "{WINDOWS_DOWNLOAD_DIR}" mkdir "{WINDOWS_DOWNLOAD_DIR}"',
        ],
        capture_output=True,
        timeout=10,
    )


def _scp_to_windows(pdf_path: Path) -> str:
    """SCP *pdf_path* to the configured remote worker and return its path."""
    if not WINDOWS_SSH_HOST or not WINDOWS_DOWNLOAD_DIR:
        raise RuntimeError("remote OCR host and directory are not configured")
    remote_path = f"{WINDOWS_DOWNLOAD_DIR}\\{pdf_path.name}"
    subprocess.run(
        ["scp", str(pdf_path), f"{WINDOWS_SSH_HOST}:\"{remote_path}\""],
        capture_output=True,
        timeout=30,
    )
    return remote_path
