"""Evidence-class and assertion-class registries (KG-INFORMATION-MODEL.md §11).

Assertion class is independent of ``edge_kind``: it says whether a claim is
source-supported, human-validated, derived, or quarantined.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from scripts.kg.registries.model import frozen


@dataclass(frozen=True)
class EvidenceClassEntry:
    """One registered evidence class."""

    slug: str
    meaning: str


_RAW_EVIDENCE_CLASSES: tuple[EvidenceClassEntry, ...] = (
    EvidenceClassEntry("structured_record", "Typed field from an API or database record"),
    EvidenceClassEntry("source_html", "Captured source HTML and coordinates"),
    EvidenceClassEntry("source_pdf_text", "Text-layer extraction from a retained PDF version"),
    EvidenceClassEntry("source_ocr", "OCR output from a retained image/PDF version"),
    EvidenceClassEntry("minutes_or_summary", "Published minutes, legal-action summary, or results"),
    EvidenceClassEntry("vote_or_attendance_record", "Structured vote, roll-call, or attendance record"),
    EvidenceClassEntry("human_adjudication", "Durable human decision citing source records"),
)

EVIDENCE_CLASSES: Mapping[str, EvidenceClassEntry] = frozen(
    {entry.slug: entry for entry in _RAW_EVIDENCE_CLASSES}
)


@dataclass(frozen=True)
class AssertionClassEntry:
    """One registered assertion class."""

    slug: str
    meaning: str
    public_behavior: str


_RAW_ASSERTION_CLASSES: tuple[AssertionClassEntry, ...] = (
    AssertionClassEntry(
        "source_supported",
        "Directly supported by exact structured or textual evidence",
        "May be presented as a factual source claim with citation",
    ),
    AssertionClassEntry(
        "human_validated",
        "Human accepted a candidate against cited evidence",
        "Present with review provenance",
    ),
    AssertionClassEntry(
        "derived",
        "Computed from canonical assertions",
        "Must be labeled inferred/derived and explain its inputs",
    ),
    AssertionClassEntry(
        "quarantined",
        "Unmappable, conflicting, or insufficiently supported",
        "Excluded from ordinary knowledge queries",
    ),
)

ASSERTION_CLASSES: Mapping[str, AssertionClassEntry] = frozen(
    {entry.slug: entry for entry in _RAW_ASSERTION_CLASSES}
)

#: Assertion classes that must never be serialized as source-supported facts.
NON_SOURCE_CLASSES: tuple[str, ...] = ("derived", "quarantined")

#: Ordered agenda evidence-lineage steps (KG-INFORMATION-MODEL.md §9.1).
AGENDA_LINEAGE_STEPS: tuple[str, ...] = (
    "acquired_source_snapshot",
    "raw_extraction_or_ocr",
    "cleaned_text",
    "structured_item_subitem",
    "extraction_result",
)

#: Source classes that must retain distinct evidence identity for one agenda.
DISTINCT_AGENDA_OBSERVATIONS: tuple[str, ...] = (
    "source_html", "source_pdf_text", "source_ocr",
)


def get_evidence_class(slug: str) -> EvidenceClassEntry:
    """Return one evidence class entry."""
    return EVIDENCE_CLASSES[slug]


def get_assertion_class(slug: str) -> AssertionClassEntry:
    """Return one assertion class entry."""
    return ASSERTION_CLASSES[slug]


def is_source_supported(assertion_class: str) -> bool:
    """Return True only for assertions that cite exact evidence directly."""
    return assertion_class in ("source_supported", "human_validated")


#: Successful extraction methods actually written by repository extractors.
#: Each entry cites the writer that persists that exact stored string, so the
#: inventory is repository-derived rather than speculative.
EXTRACTION_METHOD_WRITERS: Mapping[str, str] = frozen({
    "pymupdf_layout": "scripts/docs/layout_extract.py:extract_native",
    "pdftotext_layout": "scripts/docs/extract.py:_try_pdftotext",
    "tesseract_tsv": "scripts/docs/layout_extract.py:extract_tesseract_tsv",
    "pymupdf": "scripts/docs/extract.py:136",
    "pdftotext": (
        "scripts/docs/extract.py:158; "
        "scripts/sync/extract_results_pdfs.py:157"
    ),
    "ocr_local": "scripts/docs/extract.py:198",
    "ocr_windows": "scripts/docs/extract.py:231",
    "ocr_windows_paddle": "scripts/docs/extract.py:269",
})

#: Stored ``supporting_documents.text_extraction_method`` -> the evidence class
#: that method honestly corresponds to.
#:
#: Only ``source_pdf_text`` and ``source_ocr`` are justified: a text layer read
#: by PyMuPDF or pdftotext is PDF text, and every ``ocr_*`` path is OCR.  Any
#: method absent from this map is not mappable, and callers fail closed rather
#: than guess an evidence class.
EXTRACTION_METHOD_EVIDENCE_CLASSES: Mapping[str, str] = frozen({
    "pymupdf_layout": "source_pdf_text",
    "pdftotext_layout": "source_pdf_text",
    "tesseract_tsv": "source_ocr",
    "pymupdf": "source_pdf_text",
    "pdftotext": "source_pdf_text",
    "ocr_local": "source_ocr",
    "ocr_windows": "source_ocr",
    "ocr_windows_paddle": "source_ocr",
})

#: Extraction methods that record a *failed* extraction.  They must never map to
#: an evidence class, so a failed extraction can never become a citable source
#: claim.  ``scripts/docs/doc_constants.FAILURE_METHODS`` plus the writer-specific
#: failure marker from ``scripts/sync/extract_results_pdfs.py:200``.
FAILED_EXTRACTION_METHODS: tuple[str, ...] = (
    "pdftotext-failed",
    "failed",
    "download_failed",
    "extraction_failed",
    "process_error",
)

#: Quarantined and rejected rows carry these method prefixes
#: (``scripts/ingest_docs.py``), with a dynamic reason suffix.
QUARANTINE_METHOD_PREFIX = "quarantine:"
REJECT_METHOD_PREFIX = "reject:"


class ExtractionMethodInventoryError(RuntimeError):
    """Raised when the extraction-method inventory is internally inconsistent."""


def _assert_inventory_is_justified() -> None:
    """Every mapping must cite a writer, and every cited writer must map.

    This blocks a speculative alias from surviving without repository evidence
    and blocks a justified method from silently losing its classification.
    """
    mapped, cited = set(EXTRACTION_METHOD_EVIDENCE_CLASSES), set(
        EXTRACTION_METHOD_WRITERS
    )
    unjustified = sorted(mapped - cited)
    uncited_classified = sorted(cited - mapped)
    if unjustified or uncited_classified:
        raise ExtractionMethodInventoryError(
            "extraction-method inventory is inconsistent: "
            f"mapped without a cited writer {unjustified}; "
            f"cited writer without a mapping {uncited_classified}"
        )
    for method, evidence_class in EXTRACTION_METHOD_EVIDENCE_CLASSES.items():
        if evidence_class not in EVIDENCE_CLASSES:
            raise ExtractionMethodInventoryError(
                f"extraction method {method} maps to unregistered evidence "
                f"class {evidence_class}"
            )
    for method in FAILED_EXTRACTION_METHODS:
        if method in EXTRACTION_METHOD_EVIDENCE_CLASSES:
            raise ExtractionMethodInventoryError(
                f"failure method {method} must not map to an evidence class"
            )


def evidence_class_for_extraction_method(method: str | None) -> str | None:
    """Return the honest evidence class for a stored extraction method.

    Returns ``None`` for an unrecognised, failed, quarantined, or rejected
    method, so the caller fails closed instead of presenting it as a known
    evidence class.
    """
    if not method:
        return None
    text = str(method).strip().lower()
    if text.startswith(QUARANTINE_METHOD_PREFIX):
        return None
    if text.startswith(REJECT_METHOD_PREFIX):
        return None
    if text in FAILED_EXTRACTION_METHODS:
        return None
    return EXTRACTION_METHOD_EVIDENCE_CLASSES.get(text)


_assert_inventory_is_justified()
