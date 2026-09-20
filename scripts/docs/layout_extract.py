"""Deterministic, layout-preserving PDF extraction.

The retained plain text remains the compatibility surface for search and older
extractors.  This module additionally writes an immutable JSON artifact keyed
by source-PDF, retained-text, and complete-artifact digests.  The artifact
preserves page geometry, word boxes, visual rows, table cells, tool versions,
and source hashes.

Native PDF text is authoritative when usable.  Scanned pages use Tesseract TSV
so OCR coordinates and confidence survive.  Optional pdfplumber table parsing
augments (but never replaces) the word-box evidence.  Model-based layout tools
belong behind this deterministic representation and must earn promotion on the
reviewed benchmark before they become an automatic fallback.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import statistics
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence

from docs.doc_constants import DOWNLOAD_DIR
from docs.layout_roles import annotate_tables

LAYOUT_ARTIFACT_VERSION = "document-layout/1.2"
LAYOUT_DIR = Path(__file__).resolve().parents[2] / "data" / "document-layout"
RESULT_WORD_RE = re.compile(
    r"\b(?:approved|denied|continued|tabled|adopted|received|discussed|"
    r"withdrawn|introduced|amended|sustained|vacated|extended|deferred|"
    r"heard|no\s+action)\b",
    re.IGNORECASE,
)


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def load_artifact_for_text(
    text: str,
    source_pdf_sha256: str | None = None,
    method: str | None = None,
    version: str = LAYOUT_ARTIFACT_VERSION,
) -> dict[str, Any] | None:
    """Load one exact artifact; ambiguity between versions fails closed."""
    text_digest = sha256_text(text)
    if source_pdf_sha256:
        candidates = sorted(LAYOUT_DIR.glob(
            f"{source_pdf_sha256}-{text_digest}-*.json"
        ))
    else:
        candidates = sorted(LAYOUT_DIR.glob(f"*-{text_digest}-*.json"))
    matches = []
    for path in candidates:
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if artifact.get("retained_text_sha256") != text_digest:
            continue
        if source_pdf_sha256 and artifact.get("source_pdf_sha256") != source_pdf_sha256:
            continue
        if method and artifact.get("method") != method:
            continue
        if version and artifact.get("version") != version:
            continue
        matches.append(artifact)
    return matches[0] if len(matches) == 1 else None


def evidence_for_span(
    artifact: dict[str, Any], start: int, end: int
) -> dict[str, Any] | None:
    """Resolve retained-text offsets to their page, visual row, and word box."""
    if start < 0 or end <= start:
        return None
    for page in artifact.get("pages", []):
        for row in page.get("rows", []):
            row_start, row_end = row.get("text_start"), row.get("text_end")
            if not isinstance(row_start, int) or not isinstance(row_end, int):
                continue
            if start < row_start or end > row_end:
                continue
            tokens = [
                token for token in row.get("tokens", [])
                if isinstance(token.get("text_start"), int)
                and isinstance(token.get("text_end"), int)
                and token["text_end"] > start and token["text_start"] < end
            ]
            boxes = [token.get("bbox") for token in tokens if token.get("bbox")]
            bbox = None
            if boxes:
                bbox = [min(box[0] for box in boxes), min(box[1] for box in boxes),
                        max(box[2] for box in boxes), max(box[3] for box in boxes)]
            return {
                "page": page.get("page"),
                "row_id": row.get("row_id"),
                "row_bbox": row.get("bbox"),
                "span_bbox": bbox,
                "tokens": tokens,
                "coordinate_system": artifact.get("diagnostics", {}).get(
                    "coordinate_system"
                ),
            }
    return None


def write_artifact(artifact: dict[str, Any]) -> Path:
    """Write an immutable canonical artifact, accepting identical replay."""
    LAYOUT_DIR.mkdir(parents=True, exist_ok=True)
    body = json.dumps(artifact, sort_keys=True, separators=(",", ":")) + "\n"
    text_digest = str(artifact["retained_text_sha256"])
    source_digest = str(artifact["source_pdf_sha256"])
    artifact_digest = sha256_text(body)
    path = LAYOUT_DIR / f"{source_digest}-{text_digest}-{artifact_digest}.json"
    if path.exists():
        if path.read_text(encoding="utf-8") != body:
            raise RuntimeError(f"layout artifact collision: {path}")
        return path
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=LAYOUT_DIR,
        prefix=f".{path.name}.", suffix=".tmp", delete=False,
    ) as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    try:
        # Hard-link creation is atomic and never replaces a concurrent writer.
        os.link(temporary, path)
    except FileExistsError:
        if path.read_text(encoding="utf-8") != body:
            raise RuntimeError(f"layout artifact collision: {path}")
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _tool_version(command: Sequence[str]) -> str | None:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    output = (result.stdout or result.stderr).splitlines()
    return output[0].strip() if output else None


def _cluster_words(words: Iterable[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Cluster positioned words into stable visual rows by vertical midpoint."""
    ordered = sorted(words, key=lambda word: (
        round((float(word["bbox"][1]) + float(word["bbox"][3])) / 2, 2),
        float(word["bbox"][0]),
        str(word["text"]),
    ))
    if not ordered:
        return []
    heights = [max(float(w["bbox"][3]) - float(w["bbox"][1]), 1.0) for w in ordered]
    tolerance = max(2.0, statistics.median(heights) * 0.45)
    rows: list[list[dict[str, Any]]] = []
    centers: list[float] = []
    for word in ordered:
        center = (float(word["bbox"][1]) + float(word["bbox"][3])) / 2
        best = min(range(len(centers)), key=lambda i: abs(centers[i] - center), default=-1)
        if best >= 0 and abs(centers[best] - center) <= tolerance:
            rows[best].append(word)
            centers[best] = sum(
                (float(item["bbox"][1]) + float(item["bbox"][3])) / 2
                for item in rows[best]
            ) / len(rows[best])
        else:
            rows.append([word])
            centers.append(center)
    return [sorted(row, key=lambda word: float(word["bbox"][0]))
            for _, row in sorted(zip(centers, rows), key=lambda pair: pair[0])]


def _serialize_pages(pages: list[dict[str, Any]]) -> str:
    """Create retained text and attach exact global offsets to rows/tokens."""
    pieces: list[str] = []
    offset = 0
    for page_index, page in enumerate(pages):
        for row_index, row in enumerate(page["rows"]):
            if pieces:
                pieces.append("\n")
                offset += 1
            row["row_id"] = f"p{page_index + 1}-r{row_index + 1}"
            row["text_start"] = offset
            token_pieces: list[str] = []
            for token_index, token in enumerate(row["tokens"]):
                if token_index:
                    token_pieces.append(" ")
                    offset += 1
                token["text_start"] = offset
                token_text = str(token["text"])
                token_pieces.append(token_text)
                offset += len(token_text)
                token["text_end"] = offset
            row_text = "".join(token_pieces)
            row["text"] = row_text
            row["text_end"] = offset
            pieces.append(row_text)
        if page_index + 1 < len(pages):
            pieces.append("\n\f")
            offset += 2
    return "".join(pieces)


def _infer_result_regions(page: dict[str, Any]) -> list[dict[str, Any]]:
    """Infer bounded result columns beside repeated ``Application #`` anchors.

    This is deliberately narrow. It does not declare every left margin a result
    column; a region exists only when an application anchor, a distinct left
    column, and at least one result word are all present.
    """
    rows = page.get("rows", [])
    anchors = []
    for index, row in enumerate(rows):
        if not re.search(r"\bapplication\s*#\s*:?", row.get("text", ""), re.IGNORECASE):
            continue
        application = next(
            (token for token in row.get("tokens", [])
             if str(token.get("text", "")).casefold().startswith("application")),
            None,
        )
        if application and application.get("bbox"):
            anchors.append((index, float(application["bbox"][0]), float(row["bbox"][1])))
    regions = []
    for anchor_number, (row_index, label_x, y0) in enumerate(anchors):
        next_y = anchors[anchor_number + 1][2] if anchor_number + 1 < len(anchors) else float(
            page.get("height") or 1e9
        )
        tokens = []
        for row in rows[row_index:]:
            bbox = row.get("bbox")
            if not bbox or float(bbox[1]) >= next_y - 2:
                break
            for token in row.get("tokens", []):
                token_bbox = token.get("bbox")
                if token_bbox and float(token_bbox[2]) < label_x - 8:
                    tokens.append(token)
        region_text = " ".join(str(token.get("text", "")) for token in tokens).strip()
        if not tokens or not RESULT_WORD_RE.search(region_text):
            continue
        item_number = None
        for token in sorted(tokens, key=lambda value: float(value["bbox"][0]), reverse=True):
            match = re.fullmatch(r"(\d+)\.?", str(token.get("text", "")))
            if match:
                item_number = match.group(1)
                break
        boxes = [token["bbox"] for token in tokens]
        regions.append({
            "region_id": f"p{page['page']}-result-{anchor_number + 1}",
            "role": "result_candidate",
            "basis": "application_anchor_left_column",
            "item_number": item_number,
            "bbox": [min(box[0] for box in boxes), min(box[1] for box in boxes),
                     max(box[2] for box in boxes), max(box[3] for box in boxes)],
            "text": region_text,
            "tokens": tokens,
        })
    return regions


def _infer_table_status_regions(
    page: dict[str, Any], tables: Sequence[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Bind ``Info Only`` to an explicit result column and same visual-row item.

    A flattened phrase is never sufficient.  The table must have an explicit
    result-bearing header and item-number header, and both the status and item
    number must resolve to one exact positioned visual row.
    """
    regions = []
    seen: set[tuple[str, str]] = set()
    for table in tables:
        if table.get("page") != page.get("page"):
            continue
        table_id = str(table.get("table_id") or "")
        for cell in table.get("semantic_roles", {}).get("result_cells", []):
            status = re.sub(r"\s+", " ", str(cell.get("text") or "")).strip()
            if not re.fullmatch(r"info(?:rmation)?\s+only", status, re.I):
                continue
            item_number = re.sub(
                r"\s+", " ", str(cell.get("item_number") or "")
            ).strip()
            if not item_number:
                continue
            item_pattern = re.compile(
                rf"(?<!\w){re.escape(item_number.rstrip('.'))}\.?\s*(?!\w)", re.I
            )
            for row in page.get("rows", []):
                row_text = str(row.get("text") or "")
                if not re.search(r"\binfo(?:rmation)?\s+only\b", row_text, re.I):
                    continue
                if not item_pattern.search(row_text):
                    continue
                tokens = row.get("tokens", [])
                status_tokens = []
                for index, token in enumerate(tokens):
                    if str(token.get("text") or "").casefold() not in {"info", "information"}:
                        continue
                    if index + 1 < len(tokens) and str(
                        tokens[index + 1].get("text") or ""
                    ).casefold() == "only":
                        status_tokens = [token, tokens[index + 1]]
                        break
                if len(status_tokens) != 2 or not all(t.get("bbox") for t in status_tokens):
                    continue
                key = (str(row.get("row_id")), item_number.casefold())
                if key in seen:
                    continue
                seen.add(key)
                boxes = [token["bbox"] for token in status_tokens]
                regions.append({
                    "region_id": f"{table_id}-result-r{cell['row']}-c{cell['column']}",
                    "role": "result",
                    "basis": "explicit_result_header_same_visual_row_item",
                    "item_number": item_number.rstrip("."),
                    "bbox": [min(b[0] for b in boxes), min(b[1] for b in boxes),
                             max(b[2] for b in boxes), max(b[3] for b in boxes)],
                    "text": status,
                    "tokens": status_tokens,
                    "table_id": table_id,
                })
    return regions


def _explicit_results_column(
    pages: Sequence[dict[str, Any]],
) -> tuple[float, float] | None:
    """Return one document-wide normalized standalone RESULTS header column."""
    columns = []
    for page in pages:
        width = float(page.get("width") or 0)
        if width <= 0:
            continue
        for row in page.get("rows", []):
            # A document title (``NOTICE OF RESULTS``) or narrative use of
            # ``results`` is not a column header.  These Phoenix result lists
            # expose the role as its own visual header row.
            if re.sub(r"\s+", " ", str(row.get("text") or "")).strip().casefold() != "results":
                continue
            for token in row.get("tokens", []):
                if str(token.get("text") or "").casefold() != "results":
                    continue
                bbox = token.get("bbox")
                if bbox:
                    columns.append((((float(bbox[0]) + float(bbox[2])) / 2) / width,
                                    (float(bbox[2]) - float(bbox[0])) / width))
    if not columns:
        return None
    centers = [column[0] for column in columns]
    if max(centers) - min(centers) > 0.04:
        return None
    return sum(centers) / len(centers), max(column[1] for column in columns)


def _infer_visual_status_regions(
    page: dict[str, Any],
    results_column: tuple[float, float] | None = None,
) -> list[dict[str, Any]]:
    """Infer a status only beneath an explicit ``RESULTS`` visual-column header."""
    if results_column is None:
        results_column = _explicit_results_column([page])
    page_width = float(page.get("width") or 0)
    if results_column is None or page_width <= 0:
        return []
    header_center, header_width = results_column
    regions = []
    for row in page.get("rows", []):
        tokens = row.get("tokens", [])
        for index, token in enumerate(tokens[:-1]):
            if str(token.get("text") or "").casefold() not in {"info", "information"}:
                continue
            if str(tokens[index + 1].get("text") or "").casefold() != "only":
                continue
            status_tokens = [token, tokens[index + 1]]
            if not all(part.get("bbox") for part in status_tokens):
                continue
            status_box = [
                min(part["bbox"][0] for part in status_tokens),
                min(part["bbox"][1] for part in status_tokens),
                max(part["bbox"][2] for part in status_tokens),
                max(part["bbox"][3] for part in status_tokens),
            ]
            status_center = (
                (float(status_box[0]) + float(status_box[2])) / 2
            ) / page_width
            if abs(status_center - header_center) > max(0.06, header_width):
                continue
            item = next((
                candidate for candidate in tokens[index + 2:]
                if candidate.get("bbox")
                and float(candidate["bbox"][0]) > float(status_box[2]) + 4
                and re.fullmatch(r"\d+[A-Z]?\.?", str(candidate.get("text") or ""), re.I)
            ), None)
            if item is None:
                continue
            item_index = tokens.index(item)
            if not any(
                candidate.get("bbox")
                and float(candidate["bbox"][0]) > float(item["bbox"][2])
                and re.search(r"[A-Za-z]", str(candidate.get("text") or ""))
                for candidate in tokens[item_index + 1:]
            ):
                continue
            item_number = str(item["text"]).rstrip(".")
            regions.append({
                "region_id": f"p{page['page']}-visual-results-{row.get('row_id')}",
                "role": "result",
                "basis": "explicit_results_visual_column_same_row_item",
                "item_number": item_number,
                "bbox": status_box,
                "text": " ".join(str(part["text"]) for part in status_tokens),
                "tokens": status_tokens,
            })
    return regions


def _table_data(pdf_path: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Return optional deterministic pdfplumber cells for native PDFs."""
    try:
        import pdfplumber  # type: ignore[import-untyped]
    except ImportError:
        return [], None
    tables: list[dict[str, Any]] = []
    try:
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page_number, page in enumerate(pdf.pages, 1):
                found = page.find_tables({
                    "vertical_strategy": "lines",
                    "horizontal_strategy": "lines",
                })
                if not found:
                    found = page.find_tables({
                        "vertical_strategy": "text",
                        "horizontal_strategy": "text",
                        "min_words_vertical": 2,
                        "min_words_horizontal": 1,
                    })
                for table_number, table in enumerate(found, 1):
                    tables.append({
                        "table_id": f"p{page_number}-t{table_number}",
                        "page": page_number,
                        "bbox": [round(float(v), 3) for v in table.bbox],
                        "cells": [
                            [None if value is None else str(value) for value in row]
                            for row in table.extract()
                        ],
                    })
    except Exception:
        return [], getattr(pdfplumber, "__version__", "unknown")
    return tables, getattr(pdfplumber, "__version__", "unknown")


def extract_native(pdf_path: Path) -> tuple[str, dict[str, Any]] | None:
    """Extract native PDF words, rows, and optional table cells."""
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        fitz.TOOLS.mupdf_display_errors(False)
        fitz.TOOLS.mupdf_display_warnings(False)
        document = fitz.open(str(pdf_path))
        pages: list[dict[str, Any]] = []
        tables: list[dict[str, Any]] = []
        for page_number, page in enumerate(document, 1):
            words: list[dict[str, Any]] = []
            for item in page.get_text("words", sort=False):
                if len(item) < 5 or not str(item[4]).strip():
                    continue
                words.append({
                    "text": str(item[4]).strip(),
                    "bbox": [round(float(v), 3) for v in item[:4]],
                    "confidence": 1.0,
                    "source": "pdf_text",
                    "block": int(item[5]) if len(item) > 5 else None,
                    "line": int(item[6]) if len(item) > 6 else None,
                    "word": int(item[7]) if len(item) > 7 else None,
                })
            rows = []
            for row_words in _cluster_words(words):
                rows.append({
                    "bbox": [
                        min(word["bbox"][0] for word in row_words),
                        min(word["bbox"][1] for word in row_words),
                        max(word["bbox"][2] for word in row_words),
                        max(word["bbox"][3] for word in row_words),
                    ],
                    "tokens": row_words,
                })
            pages.append({
                "page": page_number,
                "width": round(float(page.rect.width), 3),
                "height": round(float(page.rect.height), 3),
                "rotation": int(page.rotation),
                "rows": rows,
            })
            # PyMuPDF is the zero-extra-dependency table detector. Preserve
            # its cells when ruling lines or aligned text make a table clear.
            try:
                for table_number, table in enumerate(page.find_tables().tables, 1):
                    tables.append({
                        "table_id": f"p{page_number}-t{table_number}",
                        "page": page_number,
                        "bbox": [round(float(v), 3) for v in table.bbox],
                        "cells": [
                            [None if value is None else str(value) for value in row]
                            for row in table.extract()
                        ],
                        "detector": "pymupdf",
                    })
            except Exception:
                pass
        pymupdf_version = getattr(fitz, "VersionBind", None)
        document.close()
    except Exception:
        return None
    text = _serialize_pages(pages)
    if len(text.strip()) <= 50:
        return None
    pdfplumber_version = None
    if not tables:
        tables, pdfplumber_version = _table_data(pdf_path)
        for table in tables:
            table["detector"] = "pdfplumber"
    tables = annotate_tables(tables)
    results_column = _explicit_results_column(pages)
    for page in pages:
        page["regions"] = (
            _infer_result_regions(page)
            + _infer_table_status_regions(page, tables)
            + _infer_visual_status_regions(page, results_column)
        )
    artifact = {
        "kind": "document-layout",
        "version": LAYOUT_ARTIFACT_VERSION,
        "method": "pymupdf_layout",
        "source_pdf_sha256": sha256_bytes(pdf_path.read_bytes()),
        "retained_text_sha256": sha256_text(text),
        "tools": {
            "pymupdf": pymupdf_version,
            "pdfplumber": pdfplumber_version,
        },
        "pages": pages,
        "tables": tables,
        "diagnostics": {
            "page_count": len(pages),
            "word_count": sum(len(row["tokens"]) for page in pages for row in page["rows"]),
            "table_count": len(tables),
            "result_region_count": sum(len(page.get("regions", [])) for page in pages),
            "coordinate_system": "pdf_points_top_left",
        },
    }
    return text, artifact


def extract_tesseract_tsv(
    pdf_path: Path, *, dpi: int = 300, max_pages: int = 20
) -> tuple[str, dict[str, Any]] | None:
    """OCR scanned pages to positioned tokens using Tesseract TSV."""
    try:
        import fitz  # type: ignore[import-untyped]
    except ImportError:
        return None
    pages: list[dict[str, Any]] = []
    try:
        document = fitz.open(str(pdf_path))
        for page_index in range(min(document.page_count, max_pages)):
            page = document[page_index]
            pix = page.get_pixmap(dpi=dpi)
            with tempfile.NamedTemporaryFile(suffix=".png", dir=DOWNLOAD_DIR) as image:
                image.write(pix.tobytes("png"))
                image.flush()
                result = subprocess.run(
                    ["tesseract", image.name, "stdout", "-l", "eng", "--psm", "1", "tsv"],
                    capture_output=True, text=True, timeout=180,
                )
            if result.returncode != 0:
                document.close()
                return None
            scale_x = float(page.rect.width) / max(pix.width, 1)
            scale_y = float(page.rect.height) / max(pix.height, 1)
            words: list[dict[str, Any]] = []
            for row in csv.DictReader(io.StringIO(result.stdout), delimiter="\t"):
                value = str(row.get("text") or "").strip()
                if not value or row.get("level") != "5":
                    continue
                left, top = float(row["left"]), float(row["top"])
                width, height = float(row["width"]), float(row["height"])
                confidence = max(float(row.get("conf") or -1), 0.0) / 100
                words.append({
                    "text": value,
                    "bbox": [round(left * scale_x, 3), round(top * scale_y, 3),
                             round((left + width) * scale_x, 3),
                             round((top + height) * scale_y, 3)],
                    "confidence": round(confidence, 4),
                    "source": "tesseract_tsv",
                    "block": int(row.get("block_num") or 0),
                    "line": int(row.get("line_num") or 0),
                    "word": int(row.get("word_num") or 0),
                })
            visual_rows = [{
                "bbox": [min(w["bbox"][0] for w in grouped), min(w["bbox"][1] for w in grouped),
                         max(w["bbox"][2] for w in grouped), max(w["bbox"][3] for w in grouped)],
                "tokens": grouped,
            } for grouped in _cluster_words(words)]
            pages.append({
                "page": page_index + 1,
                "width": round(float(page.rect.width), 3),
                "height": round(float(page.rect.height), 3),
                "rotation": int(page.rotation),
                "rows": visual_rows,
            })
        document.close()
    except Exception:
        return None
    text = _serialize_pages(pages)
    if len(text.strip()) <= 20:
        return None
    for page in pages:
        page["regions"] = _infer_result_regions(page)
    artifact = {
        "kind": "document-layout",
        "version": LAYOUT_ARTIFACT_VERSION,
        "method": "tesseract_tsv",
        "source_pdf_sha256": sha256_bytes(pdf_path.read_bytes()),
        "retained_text_sha256": sha256_text(text),
        "tools": {"tesseract": _tool_version(["tesseract", "--version"])},
        "pages": pages,
        "tables": [],
        "diagnostics": {
            "page_count": len(pages),
            "word_count": sum(len(row["tokens"]) for page in pages for row in page["rows"]),
            "table_count": 0,
            "result_region_count": sum(len(page.get("regions", [])) for page in pages),
            "render_dpi": dpi,
            "coordinate_system": "pdf_points_top_left",
        },
    }
    return text, artifact


def artifact_from_plain_text(
    pdf_path: Path, text: str, *, method: str, tool: str, tool_version: str | None
) -> dict[str, Any]:
    """Wrap a coordinate-less fallback without pretending it has geometry."""
    pages: list[dict[str, Any]] = []
    offset = 0
    for page_number, page_text in enumerate(text.split("\f"), 1):
        rows = []
        for row_number, line in enumerate(page_text.splitlines(), 1):
            if not line.strip():
                offset += len(line) + 1
                continue
            start = text.find(line, offset)
            end = start + len(line)
            rows.append({
                "row_id": f"p{page_number}-r{row_number}",
                "bbox": None,
                "text": line,
                "text_start": start,
                "text_end": end,
                "tokens": [],
            })
            offset = end
        pages.append({
            "page": page_number, "width": None, "height": None,
            "rotation": 0, "rows": rows,
        })
    return {
        "kind": "document-layout",
        "version": LAYOUT_ARTIFACT_VERSION,
        "method": method,
        "source_pdf_sha256": sha256_bytes(pdf_path.read_bytes()),
        "retained_text_sha256": sha256_text(text),
        "tools": {tool: tool_version},
        "pages": pages,
        "tables": [],
        "diagnostics": {
            "page_count": len(pages), "word_count": len(text.split()),
            "table_count": 0, "coordinate_system": None,
            "warning": "coordinate_less_fallback",
        },
    }


def extract_layout(pdf_path: Path) -> tuple[str | None, str | None, dict[str, Any] | None]:
    """Run the governed local cascade: native geometry, then positioned OCR."""
    native = extract_native(pdf_path)
    if native:
        text, artifact = native
        return text, "pymupdf_layout", artifact
    ocr = extract_tesseract_tsv(pdf_path)
    if ocr:
        text, artifact = ocr
        return text, "tesseract_tsv", artifact
    return None, None, None
