# Document extraction and layout evidence

Supporting documents must remain searchable without discarding the structure
needed to verify tables, recommendations, votes, and other page relationships.
Poliscopic therefore retains both compatible plain text and immutable layout
evidence.

## Outputs

Every successful extraction produces:

1. retained text for search and existing text consumers; and
2. a versioned layout artifact containing source and text hashes, extractor and
   tool versions, page accounting, visual rows, word boxes, confidence, table
   cells, and coordinate-system metadata.

Artifacts are stored under ignored `data/document-layout/` runtime storage and
are addressed by content hashes. Reprocessing never silently overwrites older
evidence. Readers require an exact method/version match and fail closed when
the evidence is missing or ambiguous.

## Pipeline

```text
source acquisition
  -> source identity, size, type, and completeness checks
  -> native PDF text/geometry when usable
  -> Poppler layout fallback for difficult born-digital PDFs
  -> rendered-page Tesseract TSV for scans
  -> retained text + immutable layout artifact
  -> extraction completeness gate
```

Source acquisition records the public URL, effective URL, platform adapter and
version, recovery method, acquisition time, representation, byte length, and
source-byte SHA-256. An HTML viewer returned instead of a PDF, an expired signed
URL, a partial stream, or stale database text is not a verified source.

## Extraction methods

| Method | Intended source | Evidence |
|---|---|---|
| `pymupdf_layout` | Born-digital PDF | Words, rows, tables, and page boxes |
| `pdftotext_layout` | PDF readable only through Poppler | Layout-preserving text without trustworthy coordinates |
| `tesseract_tsv` | Scanned/image page | OCR words, rows, boxes, and confidence |

Native text is preferred to OCR. Tesseract is a recognizer, not a table parser;
its token boxes pass through the same deterministic visual-row reconstruction
used for native words. Experimental OCR or table models are not automatic
production fallbacks until a reviewed benchmark and promotion explicitly
approve them.

## Completeness and lineage

A nonempty extraction is not automatically complete. The pipeline checks page
coverage, missing pages, source representation, method diagnostics, and whether
table-like or image-heavy content lacks usable structure. Partial extraction
must remain visible as partial and must not silently authorize downstream
annotation or model evaluation.

Changing retained text changes the evidence version. An annotation remains
bound to the exact source/text version on which it was made. A uniquely matched
quote/span may be migrated by an explicit rebinding process, but newly recovered
content requires fresh review; an earlier completed-empty judgment does not
automatically carry forward.

## Layout policy

For born-digital PDFs, word coordinates determine visual reading order and table
relationships. Rotated pages are mapped through the PDF rotation transform
before row clustering. Clearly decorative vertical margin rails may be excluded
from compatible search text only when conservative geometry checks pass; their
text and boxes remain in the artifact.

Column meaning comes from explicit headers or another reviewed source contract,
not from result-looking words alone. Downstream event extraction should use the
exact row or table cell when layout evidence exists instead of an arbitrary
character window from the whole document.

## Safety boundaries

Document acquisition is bounded by URL, redirect, content-type, size, and PDF
validation. Unsafe or unverifiable payloads are quarantined or rejected rather
than parsed optimistically. Stored text is untrusted content and must be escaped
at presentation boundaries.

Do not introduce another direct `pdftotext`, OCR, or plain-text database writer
for supporting documents. All persisted text must pass through the governed
extraction API so method, page coverage, hashes, and layout lineage survive.

## Primary implementation

- `scripts/docs/extract.py` — governed extraction cascade and compatibility API
- `scripts/docs/layout_extract.py` — layout artifacts, native extraction,
  Tesseract TSV, immutable storage, and span-to-box resolution
- `scripts/ingest_docs.py` — bounded supporting-document acquisition and ingest
- `scripts/sync/extract_results_pdfs.py` — Phoenix result-document ingestion
- `scripts/entities/event_extract.py` — layout-aware event candidates
- `tests/test_document_layout_pipeline.py` — layout, provenance, and hard-case
  regression coverage

Production backfills or evidence replacement require a reviewed, reversible,
versioned plan. Development benchmarks must not overwrite existing source text
or evidence merely to compare candidate extractors.
