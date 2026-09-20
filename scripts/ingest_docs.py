#!/usr/bin/env python3
"""
Supporting-document downloader and text extractor.

Downloads PDFs from ``supporting_documents``, runs the extraction cascade
(pymupdf → pdftotext → OCR), and writes results back to the database.

Usage
-----

    # Process 250 random untouched PDFs
    python3 scripts/ingest_docs.py --limit 250

    # Retry previously failed documents
    python3 scripts/ingest_docs.py --limit 100 --retry-failed

    # Only untouched docs (skip known failures)
    python3 scripts/ingest_docs.py --method null --limit 50

    # Untouched docs, excluding corrupt PDFs (no OCR retries)
    python3 scripts/ingest_docs.py --method null \\
        --exclude-method extraction_failed --limit 50

    # Combine with jurisdiction filter
    python3 scripts/ingest_docs.py --method null \\
        --jurisdiction "Valley Metro" --limit 20

    # Process specific IDs
    python3 scripts/ingest_docs.py --ids 45745,9077,28666

    # Show status
    python3 scripts/ingest_docs.py --status
"""

import argparse
import concurrent.futures
import hashlib
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

# ---------------------------------------------------------------------------
# Bootstrap: ensure ``scripts/`` is on ``sys.path`` so that ``from db``,
# ``from docs``, and ``from scraper`` imports resolve.
# ---------------------------------------------------------------------------
import sys

_script_dir = Path(__file__).resolve().parent.parent
if str(_script_dir) not in sys.path:
    sys.path.insert(0, str(_script_dir))

import requests
from sqlalchemy import text

from db import get_session
from docs.doc_constants import (
    DOWNLOAD_DIR,
    FAILURE_METHODS,
    USER_AGENT,
    WINDOWS_SSH_HOST,
)
from docs.doc_db import (
    fetch_batch,
    fetch_by_ids,
    fetch_retry_priority,
    print_status,
    write_result,
)
from docs.extract import extract_document_safe
from docs.layout_extract import write_artifact

# ---------------------------------------------------------------------------
# OnBase (Tempe) — has its own download / placeholder-detection logic.
# ---------------------------------------------------------------------------
from scraper.platforms.onbase import (
    download_attachment_document,
    download_document,
    _parse_onbase_downloadfile_params,
    TEMPE_CONFIG,
)

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  Safety constants
# ═══════════════════════════════════════════════════════════════════════════

# Domains we expect to serve supporting documents — everything else is
# flagged for review before extraction.
ALLOWED_DOMAINS: set[str] = {
    # County
    "maricopa.gov",
    "mcdot.maricopa.gov",
    # Tempe / OnBase / Hyland
    "tempe.hylandcloud.com",
    "hylandcloud.com",
    # Cities & towns (CivicClerk / Granicus / AgendaCenter)
    "buckeyeaz.gov",
    "chandleraz.gov",
    "elmirageaz.gov",
    "gilbertaz.gov",
    "glendaleaz.com",
    "goodyearaz.gov",
    "mesaaz.gov",
    "peoriaaz.gov",
    "phoenix.gov",
    "scottsdaleaz.gov",
    "surpriseaz.gov",
    "tempe.gov",
    "avondaleaz.gov",
    "queencreekaz.gov",
    "apachejunctionaz.gov",
    "fountainhillsaz.gov",
    "paradisevalleyaz.gov",
    "tucsonaz.gov",
    "wickenburgaz.gov",
    "tollesonaz.org",
    "valleymetro.org",
    # Document platforms
    "civicclerk.com",
    "civicclerk.net",
    "granicus.com",
    "granicus.net",
    "granicusideas.com",
    "legistar.com",
    "amazonaws.com",
    # Exact document CDN hosts discovered via trusted government sources.
    # Each has a long successful extraction history in supporting_documents
    # (with_text counts below are DB evidence, 2026-09-04):
    #   public.destinyhosted.com    23,866  (Chandler/El Mirage/Glendale/etc. docs)
    #   mccobagenda.databankcloud.com 17,887 (Maricopa County BOS agendas)
    #   d2kbkoa27fdvtw.cloudfront.net  708  (Buckeye docs)
    #   civicclerk.blob.core.windows.net 390 (Fountain Hills/Surprise/Tolleson)
    #   peoriaaz.primegov.com         39  (Peoria)
    #   vulcan-production.nyc3.cdn.digitaloceanspaces.com 28 (Valley Metro)
    #   azmag.gov                      1  (MAG)
    # Exact hosts only — deliberately NOT the parent domains
    # (destinyhosted.com / databankcloud.com / cloudfront.net / etc.).
    "public.destinyhosted.com",
    "mccobagenda.databankcloud.com",
    "d2kbkoa27fdvtw.cloudfront.net",
    "civicclerk.blob.core.windows.net",
    "peoriaaz.primegov.com",
    "vulcan-production.nyc3.cdn.digitaloceanspaces.com",
    "azmag.gov",
    # Backup / search
    "archive.org",
    "box.com",
    "googledrive.com",
    "googlevideo.com",
    "google.com",
    "youtube.com",
    "vimeo.com",
}

# Maximum PDF size we'll extract text from (50 MB).  Larger files are
# downloaded (for manual review) but text extraction is skipped.
MAX_PDF_BYTES = 50 * 1024 * 1024

# Maximum pages we'll extract from a PDF.  Above this threshold the
# document is quarantined for review.
MAX_PDF_PAGES = 500


# ═══════════════════════════════════════════════════════════════════════════
#  Safety checks
# ═══════════════════════════════════════════════════════════════════════════


def _extract_domain(url: str) -> str:
    """Return the registered domain from a URL, stripping www."""
    from urllib.parse import urlparse
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    # Strip leading www.
    if hostname.startswith("www."):
        hostname = hostname[4:]
    return hostname


def _check_url_safety(url: str) -> str:
    """Check URL domain against the allowlist.

    Returns ``"safe"``, ``"quarantine:unknown_domain:<domain>"``, or
    ``"reject:no_domain"``.
    """
    domain = _extract_domain(url)
    if not domain:
        return "reject:no_domain"
    # Check if domain or any parent matches the allowlist
    if any(domain == d or domain.endswith("." + d) for d in ALLOWED_DOMAINS):
        return "safe"
    return f"quarantine:unknown_domain:{domain}"


def _check_file_size_safety(data: bytes) -> str:
    """Check downloaded content size.

    Returns ``"safe"``, ``"quarantine:oversized"``, or
    ``"reject:empty"``.
    """
    size = len(data)
    if size < 100:
        return "reject:empty"
    if size > MAX_PDF_BYTES:
        return f"quarantine:oversized:{size}"
    return "safe"


def _check_pdf_structure(pdf_path: Path) -> str:
    """Basic structural validation of a downloaded PDF.

    Uses PyMuPDF (fitz) to check page count and structure integrity.
    Returns ``"safe"``, ``"quarantine:high_page_count:<N>"``,
    ``"quarantine:pdf_parse_error:<msg>"``, or
    ``"quarantine:not_a_pdf"``.
    """
    try:
        import fitz
    except ImportError:
        # Can't validate structurally — assume safe
        return "safe"
    try:
        doc = fitz.open(str(pdf_path))
        page_count = doc.page_count
        doc.close()
        if page_count > MAX_PDF_PAGES:
            return f"quarantine:high_page_count:{page_count}"
        return "safe"
    except Exception as exc:
        err = str(exc)[:80]
        return f"quarantine:pdf_parse_error:{err}"


def assess_doc(url: str, data: bytes, pdf_path: Path) -> str:
    """Run all safety checks against a downloaded document.

    Order: URL domain → file size → PDF structure.
    Returns a status string:

    - ``"safe"`` — ready for extraction
    - ``"quarantine:reason:detail"`` — downloaded but needs review
    - ``"reject:reason"`` — not worth keeping
    """
    # 1. URL domain
    status = _check_url_safety(url)
    if status != "safe":
        return status

    # 2. File size
    status = _check_file_size_safety(data)
    if status != "safe":
        return status

    # 3. PDF structure
    status = _check_pdf_structure(pdf_path)
    return status


# ═══════════════════════════════════════════════════════════════════════════
#  Text sanitisation
# ═══════════════════════════════════════════════════════════════════════════

# Control characters to strip from extracted text before DB write.
# Keeps tab (0x09), newline (0x0A), carriage return (0x0D).
_CONTROL_CHAR_TRANSLATION = str.maketrans({
    i: None for i in range(32)
    if i not in (0x09, 0x0A, 0x0D)
})


def sanitize_text(text: str) -> str:
    """Strip control characters and null bytes from extracted text."""
    # NUL bytes (already done inline in write_result, but also here)
    text = text.replace("\x00", "")
    # All other control characters except tab/newline/CR
    return text.translate(_CONTROL_CHAR_TRANSLATION)


# ═══════════════════════════════════════════════════════════════════════════
#  Per-document download
# ═══════════════════════════════════════════════════════════════════════════

_http_session = requests.Session()
_http_session.headers.update({"User-Agent": USER_AGENT})

# Raise the connection-pool ceiling to match the max thread count.
# Default pool is 10 per host, which causes warnings and slow reconnects
# when 25 workers are all hitting the same S3 host.
adapter = requests.adapters.HTTPAdapter(
    pool_connections=50,   # total connections to cache
    pool_maxsize=50,       # max per-host
)
_http_session.mount("https://", adapter)
_http_session.mount("http://", adapter)


def _is_real_pdf(data: bytes) -> bool:
    """Return True when *data* begins with a PDF magic byte sequence."""
    return data[:4] == b"%PDF" or b"%PDF" in data[:100]


def _is_databank_placeholder(data: bytes) -> bool:
    """Return True when *data* is a Maricopa County Databank JS-download page.

    Databank returns a "Downloading, Please wait" HTML page with
    ``DownloadFileBytes`` in the JavaScript — this is a signal that
    we need to hit the ``/DownloadFileBytes/`` URL directly.
    """
    return b"DownloadFileBytes" in data[:2000]


def _is_onbase_placeholder(data: bytes, url: str) -> bool:
    """Return True when *data* is a Tempe OnBase JS-download page.

    OnBase returns a "Downloading, Please wait..." HTML page with embedded
    JavaScript variables (``g_documentStreamId``) instead of raw PDF bytes.
    These are resolved by the OnBase downloader module.
    """
    return (
        b"Downloading, Please wait" in data[:500]
        and b"g_documentStreamId" in data[:2000]
        and (b"DownloadFile" in url.encode() or b"Downloadfile" in url.encode())
    )


def download_one(url: str, doc_id: int) -> Optional[Path]:
    """Download a single PDF, returning the local cache path or None.

    Attempts the download up to 3 times with back-off.  Handles three
    special cases transparently:

    1. **Databank** (Maricopa County) — retries with ``DownloadFileBytes``.
    2. **ViewMeeting** (Tempe / OnBase) — extracts the real download URL
       from the meeting page HTML, or falls back to the meeting-level
       OnBase downloader.
    3. **OnBase placeholder** (Tempe) — resolves the JS downloader page
       through the OnBase module (both attachment and meeting paths).
    """
    cache_key = url.replace("://", "_").replace("/", "_").replace("?", "_")[-80:]
    local_path = DOWNLOAD_DIR / f"doc_{doc_id}_{hashlib.md5(cache_key.encode()).hexdigest()[:12]}.pdf"

    if local_path.exists() and local_path.stat().st_size > 100:
        return local_path

    for attempt in range(3):
        try:
            response = _http_session.get(url, timeout=30, allow_redirects=True)
            data = response.content

            # ── Databank JS-page → try DownloadFileBytes URL ──
            if _is_databank_placeholder(data):
                bytes_url = url.replace("DownloadFile/", "DownloadFileBytes/")
                if bytes_url != url:
                    response = _http_session.get(bytes_url, timeout=30, allow_redirects=True)
                    data = response.content

            # ── ViewMeeting (Tempe OnBase) — extract real download link ──
            if b"ViewMeeting" in url.encode() and b"Meeting" in data[:500]:
                data = _resolve_tempe_viewmeeting(url, data, doc_id)

            # ── OnBase JS placeholder → use OnBase downloader ──
            if _is_onbase_placeholder(data, url):
                data = _resolve_onbase_download(url, data) or data

            # ── Validate ──
            if not _is_real_pdf(data) or len(data) < 100:
                if attempt < 2:
                    time.sleep(5 * (attempt + 1))
                    continue
                return None

            # Strip NUL bytes from OnBase/Hyland PDFs (HTML placeholder pages
            # sometimes embed NULs that break pdftotext).  Don't strip on
            # direct-serve PDFs (Destiny, CivicClerk) — NULs there can be
            # part of compressed content streams and removal corrupts the PDF.
            if 'onbase' in url.lower() or 'hyland' in url.lower():
                data = data.replace(b"\x00", b"")
            local_path.write_bytes(data)
            return local_path

        except requests.RequestException:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))

    return None


def _resolve_tempe_viewmeeting(url: str, data: bytes, doc_id: int) -> bytes:
    """Extract the real PDF download URL from a Tempe OnBase ViewMeeting page.

    Falls back to the meeting-level OnBase downloader if the HTML doesn't
    contain an explicit download link.
    """
    from urllib.parse import parse_qs, urlparse

    from scraper.platforms.onbase import download_document

    html_text = data.decode("utf-8", errors="replace")
    download_match = re.search(
        r'href="(/Agenda[A-Za-z]*/Documents/Download[Ff]ile/[^"]+)"',
        html_text,
    )
    if download_match:
        download_path = download_match.group(1).replace("&amp;", "&")
        download_url = (
            f"https://tempe.hylandcloud.com{download_path}"
            if download_path.startswith("/")
            else download_path
        )
        response = _http_session.get(download_url, timeout=30, allow_redirects=True)
        data = response.content
        return data

    # No downloadable link found — try meeting-level downloader
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    meeting_id = (query.get("id") or [None])[0]
    doctype = (query.get("doctype") or [None])[0]
    if meeting_id:
        try:
            dl_data = download_document(
                TEMPE_CONFIG,
                int(meeting_id),
                f"Legal_Action_Summary_{meeting_id}.pdf",
                doc_type=int(doctype or 3),
            )
            if dl_data and len(dl_data) > 100:
                return dl_data
        except Exception:
            pass
    return data


def _resolve_onbase_download(url: str, data: bytes) -> Optional[bytes]:
    """Resolve an OnBase JS placeholder page into actual PDF bytes.

    Tries the attachment path first (uses its own cookie-based session),
    then falls back to the meeting-level download.
    """
    from urllib.parse import unquote

    from scraper.platforms.onbase import (
        _parse_onbase_downloadfile_params,
        download_attachment_document,
        download_document,
    )

    try:
        dl_data = download_attachment_document(url)
    except Exception:
        dl_data = None
    if dl_data and len(dl_data) > 100:
        return dl_data

    params = _parse_onbase_downloadfile_params(url)
    if params and params["meeting_id"]:
        try:
            dl_data = download_document(
                TEMPE_CONFIG,
                int(params["meeting_id"]),
                unquote(params["filename"]),
                doc_type=int(params["document_type"] or 1),
            )
            if dl_data and len(dl_data) > 100:
                return dl_data
        except Exception:
            pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Per-document worker
# ═══════════════════════════════════════════════════════════════════════════


def process_one(doc: dict, force_extract: bool = False) -> dict:
    """Process a single document: download, check safety, extract, return result.

    Safety checks run between download and extraction.
    Quarantined documents are downloaded but not extracted — they get
    a ``quarantine:reason`` method stored in the DB for manual review.

    Set *force_extract* to ``True`` to skip safety checks (for reviewing
    quarantined documents you've already inspected).

    Returns a dict with keys ``id``, ``text``, ``method``, ``local_path``,
    ``error``, ``duration_ms``, and ``layout_artifact_path``.  The caller is responsible for persisting
    (via ``write_result`` or ``batch_update``).
    """
    doc_id = doc["id"]
    url = doc["document_url"]
    start = time.time()

    result: dict[str, Any] = {
        "id": doc_id,
        "text": None,
        "method": None,
        "local_path": None,
        "error": None,
        "duration_ms": 0,
        "layout_artifact_path": None,
        "content_hash": None,
    }

    # ── Download ──
    pdf_path = download_one(url, doc_id)
    if not pdf_path:
        result["error"] = "download_failed"
        result["duration_ms"] = int((time.time() - start) * 1000)
        return result

    # ── Safety check (skipped when force_extract=True) ──
    if not force_extract:
        data = pdf_path.read_bytes() if pdf_path.exists() else b""
        safety = assess_doc(url, data, pdf_path)

        if safety.startswith("reject:"):
            # Not worth keeping — remove and report
            pdf_path.unlink(missing_ok=True)
            result["error"] = safety
            fetch_duration = int((time.time() - start) * 1000)
            result["duration_ms"] = fetch_duration
            log.info("  Doc %d: REJECTED (%s, %dms)", doc_id, safety, fetch_duration)
            return result

        if safety.startswith("quarantine:"):
            # Downloaded but flagged — skip extraction, keep file for review
            duration_ms = int((time.time() - start) * 1000)
            result["duration_ms"] = duration_ms
            # Store quarantine reason as the "method" so write_result
            # persists it in text_extraction_method
            result["method"] = safety
            result["local_path"] = str(pdf_path)
            log.info(
                "  Doc %d: QUARANTINED (%s, %dms) \u2192 kept at %s",
                doc_id, safety, duration_ms, pdf_path,
            )
            return result
    else:
        log.info("  Doc %d: force_extract=True, skipping safety checks", doc_id)

    # ── Extract ──
    text_out, method, layout_artifact = extract_document_safe(pdf_path)
    duration_ms = int((time.time() - start) * 1000)
    result["duration_ms"] = duration_ms

    if text_out and method:
        # Sanitize text before returning
        clean_text = sanitize_text(text_out)
        # Sanitisation is part of retained-text identity. Layout extraction
        # emits no control characters; if that invariant changes, keep search
        # text but refuse to publish misaligned coordinates.
        if layout_artifact and clean_text != text_out:
            layout_artifact = None
        result["text"] = clean_text
        result["method"] = method
        result["local_path"] = str(pdf_path)
        if layout_artifact:
            result["layout_artifact_path"] = str(write_artifact(layout_artifact))
            result["content_hash"] = layout_artifact["source_pdf_sha256"]
    else:
        result["error"] = (
            method
            if (method and method.startswith("subprocess_"))
            else "extraction_failed"
        )

    # Clean up on success; keep the file on failure / quarantine for debugging
    if pdf_path.exists() and result["text"]:
        pdf_path.unlink()

    return result


# ═══════════════════════════════════════════════════════════════════════════
#  Batch processing
# ═══════════════════════════════════════════════════════════════════════════


def process_batch(docs: list[dict], workers: int = 8,
                 jurisdiction_label: str = "",
                 force_extract: bool = False) -> list[dict]:
    """Process a batch of documents concurrently.

    Each document is downloaded and extracted in a thread-pool worker.
    Results are written to the DB **immediately** as they finish, so a
    crash only loses the in-flight doc, not the whole batch.

    Set *force_extract* to ``True`` to skip safety checks (for reviewing
    quarantined documents already inspected on disk).

    Returns the list of result dicts (for summary reporting).
    """
    if not docs:
        return []

    total = len(docs)
    label = f" {jurisdiction_label}" if jurisdiction_label else ""
    log.info("Processing %d%s documents (%d workers)...", total, label, workers)
    start = time.time()

    results: list[dict] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_map = {
            executor.submit(process_one, doc, force_extract): doc
            for doc in docs
        }

        for i, future in enumerate(
            concurrent.futures.as_completed(future_map), 1
        ):
            doc = future_map[future]
            try:
                doc_result = future.result()
                write_result(doc_result)  # persist immediately
                results.append(doc_result)

                duration = doc_result.get("duration_ms", 0)
                if doc_result["text"]:
                    log.info(
                        "[%d/%d] Doc %s: %s (%d chars, %dms)",
                        i, total, doc_result["id"],
                        doc_result["method"],
                        len(doc_result["text"]),
                        duration,
                    )
                else:
                    log.info(
                        "[%d/%d] Doc %s: FAIL (%s, %dms)",
                        i, total, doc_result["id"],
                        doc_result["error"],
                        duration,
                    )
            except Exception as exc:
                error_result = {
                    "id": doc["id"],
                    "text": None,
                    "method": None,
                    "local_path": None,
                    "error": "process_error",
                    "duration_ms": 0,
                }
                write_result(error_result)
                results.append(error_result)
                log.error("[%d/%d] Doc %s: ERROR (%s)", i, total, doc["id"], exc)

    elapsed = time.time() - start
    successes = sum(1 for r in results if r["text"])
    failures_actual = total - successes
    rate = total / elapsed * 60 if elapsed > 0 else 0

    # Per-method timing summary
    by_method: dict[str, list[int]] = {}
    for r in results:
        method_key = r.get("method", "failed")
        by_method.setdefault(method_key, []).append(r.get("duration_ms", 0))

    log.info(
        "Batch done: %d success, %d failed, %ds (%d/min)",
        successes, failures_actual, int(elapsed), int(rate),
    )
    if by_method:
        log.info("  Per-method timing (ms):")
        for method_key in sorted(by_method, key=lambda x: x or ""):
            times = by_method[method_key]
            avg = sum(times) // len(times) if times else 0
            mx = max(times) if times else 0
            mn = min(times) if times else 0
            log.info(
                "    %-12s avg=%6d  min=%6d  max=%6d  count=%d",
                method_key or "failed", avg, mn, mx, len(times),
            )

    return results


# ═══════════════════════════════════════════════════════════════════════════
#  CLI — helpers
# ═══════════════════════════════════════════════════════════════════════════


def _list_jurisdictions() -> None:
    """Query the jurisdictions table and print every name.

    Names are listed sorted alphabetically with an index so the user
    can find the exact string to pass to ``--jurisdiction``.
    """
    session = get_session()
    try:
        rows = session.execute(
            text("SELECT id, name FROM jurisdictions ORDER BY name ASC")
        ).fetchall()
        if not rows:
            print("No jurisdictions found in the database.")
            return
        print(f"\n{'=' * 60}")
        print(f"  Jurisdictions ({len(rows)} total)")
        print(f"{'=' * 60}")
        for i, (jid, jname) in enumerate(rows, 1):
            print(f"  {i:>3}. {jname}  (id={jid})")
        print(f"{'=' * 60}\n")
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════
#  CLI
# ═══════════════════════════════════════════════════════════════════════════


def _resolve_jurisdiction_label(raw: str) -> str:
    """Resolve a ``--jurisdiction`` value to a human-readable name.

    If *raw* is a numeric ID, look up the jurisdiction name from the DB.
    Otherwise return the raw string as-is (it's already a name).
    Returns empty string if the ID doesn't exist.
    """
    if raw.strip().isdigit():
        from db import get_session
        from sqlalchemy import text
        session = get_session()
        try:
            row = session.execute(
                text("SELECT name FROM jurisdictions WHERE id = :jid"),
                {"jid": int(raw.strip())},
            ).fetchone()
            if row:
                return row[0]
        finally:
            session.close()
    return raw


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S %Z",
        force=True,
    )

    parser = argparse.ArgumentParser(
        description="Download and extract text from supporting documents"
    )
    parser.add_argument(
        "--limit", type=int, default=50,
        help="Doc count per batch (default: 50)",
    )
    parser.add_argument(
        "--workers", type=int, default=10,
        help="Concurrent download threads (default: 10)",
    )
    parser.add_argument(
        "--ids", type=str,
        help="Comma-separated document IDs to process",
    )
    parser.add_argument(
        "--retry-failed", action="store_true",
        help=(
            "Retry documents marked as failed (legacy full sweep, random order, "
            "no attempt cap). Prefer --retry-priority for bounded, prioritized retry."
        ),
    )
    parser.add_argument(
        "--retry-priority", action="store_true",
        help=(
            "Bounded, prioritized retry of transient failures. Order: recent "
            "download_failed → process_error → older download_failed. "
            "extraction_failed is excluded unless --include-extraction-failed. "
            "Skips docs whose extraction_attempts >= --max-attempts or whose last "
            "attempt is newer than --backoff-hours."
        ),
    )
    parser.add_argument(
        "--max-attempts", type=int, default=3,
        help="Retry-priority attempt ceiling per doc (default: 3)",
    )
    parser.add_argument(
        "--backoff-hours", type=int, default=24,
        help="Skip docs attempted within this many hours (default: 24)",
    )
    parser.add_argument(
        "--include-extraction-failed", action="store_true",
        help=(
            "Include extraction_failed docs in --retry-priority. Normally excluded: "
            "the extraction stack has not changed, so corrupt/scanned PDFs fail again."
        ),
    )
    parser.add_argument(
        "--list-jurisdictions", action="store_true",
        help="List all jurisdiction names in the database and exit",
    )
    parser.add_argument(
        "--jurisdiction", type=str,
        help=(
            "Filter by jurisdiction name or ID."
            " Name: case-insensitive substring match"
            " (e.g. 'tempe', 'valley metro')."
            " ID: plain integer matches jurisdiction.id"
            " (e.g. '2' for Tempe, '1' for Maricopa County)."
        ),
    )
    parser.add_argument(
        "--status", action="store_true",
        help="Print text-extraction status report and exit",
    )
    parser.add_argument(
        "--method", type=str,
        help=(
            "Filter by text_extraction_method. Comma-separated; use 'null' for untouched.\n"
            "  --method null                 untouched docs only\n"
            "  --method null,download_failed untouched + download failures\n"
            "  --method extraction_failed    corrupt-PDF retry only"
        ),
    )
    parser.add_argument(
        "--exclude-method", type=str,
        help=(
            "Skip docs whose method matches. Same syntax as --method.\n"
            "  --exclude-method null,extraction_failed  skip untouched + corrupt\n"
            "  --method null --exclude-method extraction_failed  no OCR retries"
        ),
    )
    parser.add_argument(
        "--review", action="store_true",
        help=(
            "Process quarantined documents (size/domain/page-count flags).\n"
            "Downloads and extracts without safety checks — use after\n"
            "inspecting the PDF on disk.\n"
            "  python scripts/ingest_docs.py --review --limit 10"
        ),
    )
    parser.add_argument(
        "--list-domains", action="store_true",
        help="Print the URL allowlist and exit",
    )

    args = parser.parse_args()

    if args.list_jurisdictions:
        _list_jurisdictions()
        return

    if args.status:
        print_status()
        return

    if args.list_domains:
        print("\n=== Allowed document URL domains ===\n")
        for d in sorted(ALLOWED_DOMAINS):
            print(f"  {d}")
        print(f"\n{len(ALLOWED_DOMAINS)} domains total")
        print()
        return

    if args.review:
        # Fetch quarantined docs for review
        docs = fetch_batch(
            args.limit,
            method_filter="quarantine",
        )
        if not docs:
            log.info("No quarantined documents to review.")
            return
        log.info(
            "Reviewing %d quarantined document(s) — safety checks disabled",
            len(docs),
        )
        juris_label = _resolve_jurisdiction_label(args.jurisdiction) if args.jurisdiction else ""
        results = process_batch(docs, workers=args.workers,
                                jurisdiction_label=juris_label,
                                force_extract=True)
    else:
        doc_ids = (
            [int(x.strip()) for x in args.ids.split(",")]
            if args.ids
            else None
        )
        docs = (
            fetch_by_ids(doc_ids)
            if doc_ids
            else (
                fetch_retry_priority(
                    args.limit,
                    max_attempts=args.max_attempts,
                    backoff_hours=args.backoff_hours,
                    include_extraction_failed=args.include_extraction_failed,
                )
                if args.retry_priority
                else fetch_batch(
                    args.limit,
                    retry_failed=args.retry_failed,
                    jurisdiction=args.jurisdiction,
                    method_filter=args.method,
                    exclude_method=args.exclude_method,
                )
            )
        )

        if not docs:
            log.info("No documents to process.")
            return

        juris_label = _resolve_jurisdiction_label(args.jurisdiction) if args.jurisdiction else ""
        results = process_batch(docs, workers=args.workers,
                                jurisdiction_label=juris_label)

    log.info(
        "Results written to DB: %d extracted, %d failed",
        sum(1 for r in results if r["text"]),
        sum(1 for r in results if r.get("error") or not r["text"]),
    )


if __name__ == "__main__":
    main()
