"""
``scripts/docs/`` — supporting-document download and layout extraction.

Modules
-------
ingest_docs (canonical entry point)     CLI entry point, batch orchestration, per-document worker.
extract.py        Governed native-layout → Poppler → positioned-OCR cascade.
layout_extract.py Immutable page/row/table/token artifacts and span resolution.
doc_db.py         Database queries and write helpers.
doc_constants.py  Shared constants (paths, hostnames, failure-method tuple).
"""
