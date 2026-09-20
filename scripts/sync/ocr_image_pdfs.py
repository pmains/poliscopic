#!/usr/bin/env python3
"""
ocr_image_pdfs.py — OCR image-based supporting documents and agenda items.

Finds agenda items where text is empty or very short (for bodies known to serve
image-based PDFs), downloads the PDF, and runs the governed document cascade.
Tesseract TSV is the default scanned-page engine so word boxes and confidence
are retained in an immutable layout artifact. Legacy PaddleOCR remains an
explicit benchmark/fallback option rather than an automatic production model.

Usage:
    # Dry run: show what would be processed
    python3 scripts/sync/ocr_image_pdfs.py --dry-run --body buckeye-cc

    # Process a specific body
    python3 scripts/sync/ocr_image_pdfs.py --body chandler-hpc --engine tesseract

    # Process all known image-based bodies
    python3 scripts/sync/ocr_image_pdfs.py --all
"""

import sys, os, time, logging, subprocess, tempfile, urllib.request
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
from db import get_engine
from docs.extract import extract_document_safe
from docs.layout_extract import write_artifact
from sqlalchemy import text

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("ocr-image-pdfs")

# Bodies known to serve image-based PDFs (0% text coverage)
IMAGE_BODIES = [
    # Chandler sub-commissions (AgendaCenter image PDFs)
    "chandler-hpc", "chandler-hhsc", "chandler-eda", "chandler-hct",
    "chandler-air", "chandler-arts", "chandler-dvc", "chandler-cf",
    "chandler-cpr", "chandler-hrc",
    # Buckeye sub-commissions
    "buckeye-community-services", "buckeye-library", "buckeye-airport",
    "buckeye-youth",
    # Other low-coverage
    "mc-trp", "scottsdale-cc",
]

OCR_ENGINES = {
    "tesseract": {
        "cmd": ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "1"],
        "timeout": 120,
    },
    "paddleocr": {
        "module": "paddleocr",
        "func": "PaddleOCR",
        "timeout": 300,
    },
}


def download_pdf(url: str) -> str | None:
    """Download a PDF to a temp file and return the path."""
    tmp = tempfile.NamedTemporaryFile(suffix=".pdf", delete=False)
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"
        })
        with urllib.request.urlopen(req, timeout=60) as resp:
            while True:
                chunk = resp.read(8192)
                if not chunk:
                    break
                tmp.write(chunk)
        tmp.close()
        if os.path.getsize(tmp.name) == 0:
            os.unlink(tmp.name)
            return None
        return tmp.name
    except Exception as e:
        log.warning("Download failed: %s", e)
        return None


def ocr_tesseract(pdf_path: str) -> str | None:
    """Run the governed native/TSV cascade and retain its layout artifact."""
    text_output, method, artifact = extract_document_safe(Path(pdf_path))
    if text_output and method and artifact:
        write_artifact(artifact)
        log.info("    Layout extraction method: %s", method)
    return text_output


def ocr_paddleocr(pdf_path: str) -> str | None:
    """Run PaddleOCR on a PDF."""
    try:
        from paddleocr import PaddleOCR
        import fitz
        ocr = PaddleOCR(use_angle_cls=True, lang="en", show_log=False)
        doc = fitz.open(pdf_path)
        text_parts = []
        for page in doc:
            pix = page.get_pixmap(dpi=300)
            img_bytes = pix.tobytes("png")
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as img_tmp:
                img_tmp.write(img_bytes)
                img_path = img_tmp.name
            try:
                result = ocr.ocr(img_path, cls=True)
                if result and result[0]:
                    page_text = "\n".join(line[1][0] for line in result[0] if line and line[1])
                    text_parts.append(page_text)
            finally:
                try:
                    os.unlink(img_path)
                except OSError:
                    pass
        return "\n".join(text_parts) if text_parts else None
    except ImportError:
        log.error("PaddleOCR not installed. Try: pip install paddleocr paddlepaddle")
        return None
    except Exception as e:
        log.warning("PaddleOCR failed: %s", e)
        return None


def process_body(conn, engine, body: str, ocr_engine: str = "tesseract",
                 dry_run: bool = False, limit: int = 0):
    """OCR all empty-text agenda items for a body."""
    # Find items with empty/short text
    rows = conn.execute(text("""
        SELECT id, body, meeting_id, agenda_item_number,
               source_url, agenda_item_title
        FROM agenda_items
        WHERE body = :body
          AND (LENGTH(agenda_item_text) < 50 OR agenda_item_text IS NULL)
          AND source_url != ''
        ORDER BY id
        LIMIT :limit
    """), {"body": body, "limit": limit or 100000}).fetchall()

    if not rows:
        log.info("  No empty-text items found for %s", body)
        return

    log.info("  %s: %d items need OCR", body, len(rows))

    if dry_run:
        for r in rows[:5]:
            log.info("    Would OCR: id=%d url=%s", r[0], (r[4] or "")[-50:])
        return

    for r in rows:
        item_id = r[0]
        pdf_url = r[4] or ""
        if not pdf_url or "phoenix.gov" not in pdf_url:
            continue

        pdf_path = download_pdf(pdf_url)
        if not pdf_path:
            continue

        try:
            if ocr_engine == "tesseract":
                text_output = ocr_tesseract(pdf_path)
            else:
                text_output = ocr_paddleocr(pdf_path)

            if text_output and text_output.strip():
                # Store text back to agenda item
                with engine.begin() as write_conn:
                    write_conn.execute(text(
                        "UPDATE agenda_items SET agenda_item_text = :txt WHERE id = :id"
                    ), {"txt": text_output[:50000], "id": item_id})
                log.info("    OCR'd item %d: %d chars", item_id, len(text_output))
        finally:
            try:
                os.unlink(pdf_path)
            except OSError:
                pass

        time.sleep(0.5)


def main():
    engine = get_engine()
    dry_run = "--dry-run" in sys.argv
    ocr_engine = "tesseract"
    limit = 0

    for i, arg in enumerate(sys.argv):
        if arg == "--engine" and i + 1 < len(sys.argv):
            ocr_engine = sys.argv[i + 1]
        if arg == "--limit" and i + 1 < len(sys.argv):
            limit = int(sys.argv[i + 1])

    if "--all" in sys.argv:
        bodies = IMAGE_BODIES
    elif "--body" in sys.argv:
        idx = sys.argv.index("--body")
        bodies = [sys.argv[idx + 1]]
    else:
        print(__doc__.strip())
        return

    if dry_run:
        log.info("DRY RUN — no changes will be made")
    else:
        log.info("Using OCR engine: %s", ocr_engine)

    with engine.connect() as conn:
        for body in bodies:
            process_body(conn, engine, body, ocr_engine, dry_run, limit)


if __name__ == "__main__":
    main()
