"""sweep_docs_batch.py — the per-batch pipeline for the document sweep.

Fetches the batch's documents, extracts candidates, validates every emission
bundle against the registries before any row plan exists, builds the row plan
once ahead of the dry/live branch, and writes only the planned inserts.

The extractor and the mention writer are **injected** by the ``sweep_docs``
facade, which resolves them from its own globals at call time.  That is what
keeps ``monkeypatch.setattr(sweep_docs, "extract_entities_from_doc", ...)`` and
``monkeypatch.setattr(sweep_docs, "_write_mentions", ...)`` working after the
decomposition.
"""

from __future__ import annotations

import inspect
import logging

from .sweep_docs_planning import (
    ExtractedCandidate,
    build_classification_plan,
)
from .sweep_docs_payloads import select_write_payloads
from .sweep_docs_storage import (
    SOURCE_TYPE,
    _load_existing_entity_assertions,
    _load_existing_mention_assertions,
    _write_entities,
    mark_docs_swept,
)

log = logging.getLogger("sweep_docs")


def run_batch(conn, wm: int, entity_cache: dict, *,
              batch_size: int,
              extractor,
              mention_writer,
              dry_run: bool = False, verbose: bool = False,
              validator=None) -> dict:
    """Process one batch of supporting documents. Returns stats dict.

    ``extractor`` and ``mention_writer`` are injected by the ``sweep_docs``
    facade, which resolves them from its own globals so its monkeypatch seams
    keep working after the decomposition.

    When a validator is supplied, every ontology-bearing candidate is checked
    against the canonical registries before any write.  Refused candidates fail
    closed and are recorded with their reason.
    """
    import hashlib

    query = f"""
        SELECT sd.id, COALESCE(sd.document_title, ''),
               COALESCE(sd.text_content, ''),
               COALESCE(sd.text_extraction_method, '')
        FROM supporting_documents sd
        WHERE sd.swept_at IS NULL
          AND sd.text_content IS NOT NULL AND sd.text_content != ''
          AND sd.id > :wm
        ORDER BY sd.id
        LIMIT :limit
    """
    rows = conn.execute(
        __import__("sqlalchemy").text(query),
        {"wm": wm, "limit": batch_size},
    ).fetchall()

    if not rows:
        return {"processed": 0, "matches": 0, "entities": 0, "mentions": 0,
                "max_id": wm, "done": True}

    max_id = max(int(r[0]) for r in rows)
    if verbose:
        log.info("  scanning %d docs (id range %d-%d)", len(rows), rows[0][0], rows[-1][0])

    # Phase 1: Collect candidates
    all_candidates: list[dict] = []
    # Candidates whose canonicalised identity is blank are refused inside the
    # extractor, before any assertion exists.  They are collected here so the
    # count is reported rather than silently dropped.
    blank_identity_rejections: list[dict] = []
    extractor_takes_rejections = (
        "rejections" in inspect.signature(extractor).parameters
    )
    for r in rows:
        doc_id = int(r[0])
        title = str(r[1] or "")
        text = str(r[2] or "")
        extraction_method = str(r[3] or "") if len(r) > 3 else ""
        # Version of the *analysed text*: SHA-256 of exactly the stored text we
        # read.  This is not a source-PDF hash and must never be presented as
        # one; it identifies this extraction of this document version.
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        try:
            cs = (extractor(text, rejections=blank_identity_rejections)
                  if extractor_takes_rejections else extractor(text))
            if cs:
                for c in cs:
                    c["_source_id"] = doc_id
                    c["_content_hash"] = content_hash
                    c["_extraction_method"] = extraction_method
                all_candidates.extend(cs)
        except Exception:
            log.warning("  doc %d: extraction error, skipping", doc_id)

    # Validate every proposed emission BEFORE the dry-run return: dry and live
    # runs validate the same complete bundles; only mutation differs.
    if validator is not None:
        from scripts.kg import registries as kg_registries
        from scripts.kg.emission import EmissionBundle, EmissionError
        from scripts.kg.identity import evidence_identity

        validator.start_batch()
        for candidate in all_candidates:
            source_ref = f"supporting_document:{candidate.get('_source_id')}"
            evidence_class = kg_registries.evidence_class_for_extraction_method(
                candidate.get("_extraction_method")
            )
            if evidence_class is None:
                reason = (
                    f"unsupported extraction method "
                    f"{candidate.get('_extraction_method')!r} has no honest "
                    "evidence class"
                )
                validator.reject(
                    "bundle:mention", candidate.get("role", ""), reason,
                    source=source_ref,
                )
                raise EmissionError(f"sweep_docs: {reason} ({source_ref})")
            key = evidence_identity(
                source_type="supporting_document",
                source_id=str(candidate["_source_id"]),
                content_hash=candidate["_content_hash"],
                extraction_method=candidate.get("_extraction_method") or None,
            )
            # A mention is evidence that the source described an actor with a
            # contextual label.  It is not participation, attendance, a
            # relationship, or a completed action: the bundle kind stays
            # "mention" and no relationship is written here.
            validator.validate_bundle(
                EmissionBundle(
                    kind="mention",
                    entity_type=candidate["entity_type"],
                    role=candidate["role"],
                    context_class="evidence",
                    context_identity=key,
                    evidence_class=evidence_class,
                    assertion_class="source_supported",
                    model_version=kg_registries.MODEL_VERSION,
                    evidence_identity=key,
                ),
                source=source_ref,
            )
        validator.complete_validation()

    if not all_candidates:
        # Mark as swept even with no matches
        mark_docs_swept(conn, rows)
        return {"processed": len(rows), "matches": 0, "entities": 0,
                "mentions": 0, "proposed": 0, "would_insert": 0,
                "replay_noop": 0, "max_id": max_id, "done": False}

    # Phase 2: Load the existing classification state BEFORE classifying.
    # Legacy rows with a blank identity half are counted, never crashed on.
    existing_entities, skipped_entity_keys = _load_existing_entity_assertions(
        entity_cache)
    existing_mentions, skipped_mention_rows = _load_existing_mention_assertions(
        conn, all_candidates)
    if skipped_entity_keys or skipped_mention_rows:
        log.info(
            "  Skipped %d blank-identity entity key(s), %d blank-identity "
            "mention row(s) — unreachable by any valid assertion identity",
            skipped_entity_keys, skipped_mention_rows,
        )

    # Phase 3: Build the row plan once, ahead of the dry/live branch, so dry and
    # live execution cannot disagree about which rows are inserts.
    extracted = [
        ExtractedCandidate.from_mapping(c, source_type=SOURCE_TYPE)
        for c in all_candidates
    ]
    entity_payloads, mention_payloads = select_write_payloads(extracted)
    plan = build_classification_plan(
        extracted,
        existing_entities=existing_entities,
        existing_mentions=existing_mentions,
    )

    # Phase 4: record the classification exactly once.
    if validator is not None:
        if not dry_run:
            validator.begin_writes()
        validator.classify_rows(
            would_insert=plan.total_would_insert,
            replay_noop=plan.total_replay_noop,
        )

    # Phase 5: dry mode reports the plan totals and writes nothing.
    if dry_run:
        if verbose:
            log.info("  → %d candidates, %d would insert, %d replay no-op",
                     len(all_candidates), plan.total_would_insert,
                     plan.total_replay_noop)
        return {"processed": len(rows), "matches": len(all_candidates),
                "entities": 0, "mentions": 0,
                "proposed": plan.total_proposed,
                "would_insert": plan.total_would_insert,
                "replay_noop": plan.total_replay_noop,
                "unrepresentable_existing_entities": skipped_entity_keys,
                "unrepresentable_existing_mentions": skipped_mention_rows,
                "blank_identity_candidates": len(blank_identity_rejections),
                "max_id": max_id, "done": False}

    # Phase 6: live writes.  Only planned inserts are written.  An assertion
    # the plan classified as a replay no-op never enters an INSERT merely to
    # discover that it already exists.
    total_entities, plan = _write_entities(
        conn, plan, entity_payloads, entity_cache, validator,
    )
    total_mentions = mention_writer(conn, plan, mention_payloads, entity_cache)

    # Phase 7: Mark all fetched docs as swept
    mark_docs_swept(conn, rows)

    return {"processed": len(rows), "matches": len(all_candidates),
            "entities": total_entities, "mentions": total_mentions,
            "proposed": plan.total_proposed,
            "would_insert": plan.total_would_insert,
            "replay_noop": plan.total_replay_noop,
            "unrepresentable_existing_entities": skipped_entity_keys,
            "unrepresentable_existing_mentions": skipped_mention_rows,
            "blank_identity_candidates": len(blank_identity_rejections),
            "max_id": max_id, "done": False}
