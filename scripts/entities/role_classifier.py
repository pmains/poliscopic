#!/usr/bin/env python3
"""
role_classifier.py — ML role classification using 6-signal XGBoost ensemble.

Phase 4 of the entity pipeline.  Loads the pre-trained XGBoost ensemble that
combines 6 signals: fastText probs, sentence embeddings (name + context),
TF-IDF context windows, surface-form features, and body group.

Trained at 95.5% accuracy on 29k labeled mentions.

Usage:
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/role_classifier.py
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/role_classifier.py --dry-run
    PYTHONPATH=scripts .venv/bin/python3 scripts/entities/role_classifier.py --force
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import pickle
import re
import sys
import time
from collections.abc import MutableMapping
from typing import Any, Protocol, cast

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))

DEFAULT_SCAN_LIMIT = 10_000
CPU_THREAD_COUNT = int(os.environ.get("ROLE_CLASSIFIER_CPU_THREADS", "1"))


def _configure_cpu_environment(
    environment: MutableMapping[str, str], thread_count: int
) -> None:
    """Bound native inference threads before importing numeric libraries.

    The development runtime stalls in MiniLM inference with its default native
    thread pools. Environment configuration is required before importing
    PyTorch-backed libraries; changing PyTorch thread pools afterward causes a
    native crash in this runtime.
    """
    if thread_count < 1:
        raise ValueError("ROLE_CLASSIFIER_CPU_THREADS must be at least 1")
    configured_count = str(thread_count)
    environment.setdefault("OMP_NUM_THREADS", configured_count)
    environment.setdefault("MKL_NUM_THREADS", configured_count)
    environment.setdefault("TOKENIZERS_PARALLELISM", "false")


_configure_cpu_environment(os.environ, CPU_THREAD_COUNT)

import numpy as np
import xgboost as xgb
from sklearn.feature_extraction.text import TfidfVectorizer
from db import get_engine
from sqlalchemy.engine import Engine
from sqlalchemy import text

log = logging.getLogger("role_classifier")

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")

# Model artifacts
XGB_PATH = os.path.join(ROOT, "data", "role_ensemble_xgb.json")
TFIDF_PATH = os.path.join(ROOT, "data", "role_ensemble_tfidf.pkl")
LABELS_PATH = os.path.join(ROOT, "data", "role_ensemble_labels.pkl")

# Artifact dependencies are loaded only when classification is requested.  This
# keeps importing the phase safe for orchestration and isolated unit tests.
ROLE_NAMES: list[str] | None = None
TFIDF: TfidfVectorizer | None = None
SENTENCE_ENCODER: Any | None = None
XGB_MODEL: xgb.Booster | None = None

BODY_GROUPS = {"phoenix-cc":0,"tempe-cc":0,"chandler-cc":0,"scottsdale-cc":0,"mesa-city-council":0,
               "glendale-cc":0,"goodyear-cc":0,"gilbert-cc":0,"bos":1,"pz":2,"phoenix-pc":2,
               "phoenix-ti":3,"phoenix-ps":3,"phoenix-ed":3,"phoenix-boa":4,"scottsdale-boa":4}
# Keep feature work bounded independently of the write transaction size.  The
# latter remains 500 so a feature-batching change cannot alter write batching.
FEATURE_BATCH_SIZE = 256
BATCH_SIZE = 500

# Roles we can confidently assign from structured data — skip these
STRUCTURAL_ROLES = {"applicant", "attorney", "staff", "owner", "presenter",
                    "representative", "reference", "mentioned",
                    "iga_counterparty"}


class FastTextPredictor(Protocol):
    """The small portion of fastText's model API used by feature generation."""

    def predict(self, text: str, k: int = 1) -> tuple[list[str], list[float]]:
        """Return predicted labels and their probabilities."""


def _load_fasttext_model() -> FastTextPredictor:
    """Load the role fastText model once for a classification invocation."""
    import fasttext

    return fasttext.load_model(os.path.join(ROOT, "data", "role_classifier.bin"))


def load_model() -> bool:
    """Load the persisted ensemble artifacts once for the current process."""
    global ROLE_NAMES, TFIDF, SENTENCE_ENCODER, XGB_MODEL

    if all(model is not None for model in
           (ROLE_NAMES, TFIDF, SENTENCE_ENCODER, XGB_MODEL)):
        return True

    with open(LABELS_PATH, "rb") as labels_file:
        ROLE_NAMES = cast(list[str], pickle.load(labels_file))
    with open(TFIDF_PATH, "rb") as tfidf_file:
        TFIDF = cast(TfidfVectorizer, pickle.load(tfidf_file))
    # This import pulls in PyTorch and Transformers. Keep it on the actual
    # inference path so orchestration and fake-model unit tests can import the
    # phase without initializing a 700+ MiB native runtime.
    from sentence_transformers import SentenceTransformer
    SENTENCE_ENCODER = SentenceTransformer(
        "sentence-transformers/all-MiniLM-L6-v2",
    )
    XGB_MODEL = xgb.Booster()
    XGB_MODEL.load_model(XGB_PATH)
    return True


def extract_surface(name: str, ctx: str) -> list[int]:
    n, c = name.lower(), ctx.lower()
    return [len(name), len(name.split()),
            1 if name.isupper() and len(name)>3 else 0,
            1 if name and name[0].isupper() else 0,
            1 if "&" in n or " AND " in name else 0,
            1 if "," in name else 0, name.count(","),
            1 if any(w in n for w in ["attorney","esq","llc","inc","corp","plc","ltd"]) else 0,
            1 if any(w in n for w in ["jr","sr","iii","phd","md","jd"]) else 0,
            1 if any(w in n for w in ["city of","county of","town of","arizona","state of"]) else 0,
            1 if any(w in c for w in ["applicant","attorney","representative","staff"]) else 0,
            1 if name.endswith(".") else 0, 1 if "." in name else 0]


def build_features(names: list[str], contexts: list[str], full_texts: list[str],
                   bodies: list[str], *,
                   fasttext_model: FastTextPredictor | None = None) -> np.ndarray:
    """Build full 1297-dimensional features for one contiguous input batch.

    ``fasttext_model`` is injectable so callers processing multiple feature
    batches can reuse the 769 MiB model instead of loading it per batch.
    """
    if ROLE_NAMES is None or TFIDF is None or SENTENCE_ENCODER is None:
        load_model()
    role_names = ROLE_NAMES
    tfidf = TFIDF
    sentence_encoder = SENTENCE_ENCODER
    if role_names is None or tfidf is None or sentence_encoder is None:
        raise RuntimeError("role-classification feature artifacts failed to load")

    ft_model = fasttext_model or _load_fasttext_model()

    # Signal 1: fastText probs
    ft_prob = np.zeros((len(names), len(role_names)))
    for i in range(len(names)):
        txt = (contexts[i] + " " + names[i]).replace("\n", " ").replace("\r", " ")
        if not txt.strip(): continue
        p = ft_model.predict(txt, k=len(role_names))
        for j, lb in enumerate(p[0]):
            role = lb.replace("__label__", "")
            if role in role_names:
                ft_prob[i, role_names.index(role)] = float(p[1][j])

    # Signal 2: sentence embeddings (name)
    name_emb = sentence_encoder.encode(
        names, batch_size=32, show_progress_bar=False,
    )

    # Signal 3: sentence embeddings (context window)
    ctx_windows = []
    for i in range(len(names)):
        ft = full_texts[i] or ""
        nm = names[i] or ""
        if ft:
            idx = ft.lower().find(nm.lower()[:30])
            if idx >= 0:
                ctx_windows.append(ft[max(0,idx-10):min(len(ft),idx+len(nm)+100)])
            else:
                ctx_windows.append(nm)
        else:
            ctx_windows.append(nm)
    ctx_emb = sentence_encoder.encode(
        ctx_windows, batch_size=32, show_progress_bar=False,
    )

    # Signal 4: TF-IDF on context
    tfidf_feats = tfidf.transform(ctx_windows).toarray()

    # Signal 5: surface features
    surf = np.array([extract_surface(names[i], contexts[i]) for i in range(len(names))])

    # Signal 6: body group
    body_f = np.zeros((len(names), 6))
    for i, b in enumerate(bodies):
        body_f[i, BODY_GROUPS.get(b, 5)] = 1

    return np.hstack([ft_prob, name_emb, ctx_emb, tfidf_feats, surf, body_f])


def classify_entity(entity_name: str, context_snippet: str = "",
                    full_text: str = "", body: str = "") -> tuple[str, float]:
    """Predict the role of an entity using the full XGBoost ensemble.

    Returns (role, confidence).  For single-entity use; batch use
    should call build_features() + XGB_MODEL directly.
    """
    load_model()
    role_names = ROLE_NAMES
    xgb_model = XGB_MODEL
    if role_names is None or xgb_model is None:
        raise RuntimeError("role-classification artifacts failed to load")
    features = build_features([entity_name], [context_snippet],
                               [full_text], [body])
    d = xgb.DMatrix(features)
    preds = xgb_model.predict(d)[0]
    idx = int(np.argmax(preds))
    return role_names[idx], float(preds[idx])


def _selection_evidence(selected_ids: list[int]) -> dict[str, object]:
    """Return reproducible evidence for one ordered bounded selection.

    IDs are serialized as a compact JSON array before hashing, so the digest
    captures both membership and order.  Empty selections use the digest of
    ``[]`` and ``None`` endpoints.
    """
    serialized_ids = json.dumps(selected_ids, separators=(",", ":"))
    return {
        "selected_ids_sha256": hashlib.sha256(
            serialized_ids.encode("utf-8")
        ).hexdigest(),
        "selected_first_id": selected_ids[0] if selected_ids else None,
        "selected_last_id": selected_ids[-1] if selected_ids else None,
    }


def run_role_classifier(
    engine: Engine,
    dry_run: bool = False,
    force: bool = False,
    confidence: float = 0.5,
    verbose: bool = False,
    limit: int = DEFAULT_SCAN_LIMIT,
) -> dict[str, object]:
    """Run role classification phase. Returns structured result dict.

    Loads the XGBoost ensemble, scans unclassified entity_mentions,
    predicts roles, and bulk-updates the DB.
    """
    if limit < 1:
        raise ValueError("role-classifier limit must be at least 1")

    total_updated = 0
    total_scanned = 0
    distribution: dict[str, int] = {}
    start_ts = time.time()
    # Per-proposal accounting.  A *proposal* is one selected mention that reaches
    # the decision point, never an aggregate scan count.
    from scripts.entities.role_classifier_proposals import (
        PROPOSAL_REPLAY_NOOP,
        PROPOSAL_UNRESOLVED,
        classify_role_proposal,
        role_classifier_accounting,
    )
    from scripts.entities.phase_receipt import build_phase_receipt

    role_proposals = 0
    role_would_update = 0
    role_replay_noop = 0
    role_unresolved = 0
    pending_updates: list[tuple[str, int]] = []
    emission_roles: list[str] = []

    where_clauses = ["1=1"]
    if not force:
        where_clauses.append("(em.role_in_context IS NULL OR em.role_in_context = '')")

    with engine.connect() as conn:
        total_mention_count = conn.execute(text(
            "SELECT COUNT(*) FROM entity_mentions"
        )).scalar()

        rows = conn.execute(text(f"""
            SELECT em.id, e.name, em.context_snippet, em.role_in_context,
                   ai.body, ai.agenda_item_text
            FROM entity_mentions em
            JOIN entities e ON em.entity_id = e.id
            LEFT JOIN agenda_items ai ON ai.id = CAST(em.source_id AS INTEGER)
            WHERE {' AND '.join(where_clauses)}
            ORDER BY em.id ASC
            LIMIT :limit
        """), {"limit": limit}).fetchall()

        log.info("Total mentions: %d. Scanning %d mentions.",
                 total_mention_count, len(rows))
        total_scanned = len(rows)
        selection_evidence = _selection_evidence([int(row[0]) for row in rows])
        if not rows:
            elapsed = time.time() - start_ts
            # An empty selection is a genuinely empty run, so its receipt is
            # honestly zero rather than fabricated.
            return {
                "success": True,
                "total_scanned": 0,
                "total_updated": 0,
                "duration_s": round(elapsed, 1),
                "distribution": {},
                "dry_run": dry_run,
                "role_proposals": 0,
                "role_would_update": 0,
                "role_replay_noop": 0,
                "role_unresolved": 0,
                **selection_evidence,
                "validation_receipt": build_phase_receipt(
                    "role_classifier", dry_run=dry_run,
                    rows=role_classifier_accounting({}, committed=0,
                                                    dry_run=dry_run),
                ),
            }

        # Load once, then keep both feature matrices and their progress bounded.
        # This must happen after the empty return: a no-op phase should not load
        # the large fastText artifact.
        load_model()
        role_names = ROLE_NAMES
        xgb_model = XGB_MODEL
        if role_names is None or xgb_model is None:
            raise RuntimeError("role-classification artifacts failed to load")
        fasttext_model = _load_fasttext_model()
        log.info("Building features in batches of %d mentions...", FEATURE_BATCH_SIZE)

        # Accumulate candidates across feature batches so the existing 500-row
        # write flush semantics remain exactly unchanged.
        update_data = []
        for batch_start in range(0, len(rows), FEATURE_BATCH_SIZE):
            batch_end = min(batch_start + FEATURE_BATCH_SIZE, len(rows))
            batch_rows = rows[batch_start:batch_end]
            names = [row[1] or "" for row in batch_rows]
            contexts = [row[2] or "" for row in batch_rows]
            current_roles = [row[3] or "" for row in batch_rows]
            bodies = [row[4] or "" for row in batch_rows]
            full_texts = [row[5] or "" for row in batch_rows]

            log.info("Building feature batch %d-%d of %d.",
                     batch_start + 1, batch_end, len(rows))
            features = build_features(
                names, contexts, full_texts, bodies,
                fasttext_model=fasttext_model,
            )
            dmatrix = xgb.DMatrix(features)
            predictions = xgb_model.predict(dmatrix)
            predicted_labels = np.argmax(predictions, axis=1)
            confidences = np.max(predictions, axis=1)

            for batch_index, row in enumerate(batch_rows):
                mention_id = row[0]
                current_role = current_roles[batch_index] or ""
                if not force and current_role in STRUCTURAL_ROLES:
                    continue

                predicted_role = role_names[predicted_labels[batch_index]]
                pred_confidence = float(confidences[batch_index])
                distribution[predicted_role] = distribution.get(predicted_role, 0) + 1

                # Classified identically in dry and live runs: the selected
                # mention set is the same, so a dry run describes exactly what a
                # live run would have written.
                role_proposals += 1
                classification = classify_role_proposal(
                    mention_id=mention_id,
                    predicted_role=predicted_role,
                    current_role=current_role,
                    confidence=pred_confidence,
                    threshold=confidence,
                )
                if classification == PROPOSAL_UNRESOLVED:
                    role_unresolved += 1
                    continue
                # Both would-update and replay proposals are emittable values and
                # are validated as such in dry and live runs alike.
                emission_roles.append(predicted_role)
                if classification == PROPOSAL_REPLAY_NOOP:
                    role_replay_noop += 1
                    continue
                role_would_update += 1
                if verbose and dry_run and batch_start + batch_index < 10:
                    log.info("  %s → %s (%.2f) [mid=%d]",
                             names[batch_index][:30], predicted_role,
                             pred_confidence, mention_id)
                if not dry_run:
                    pending_updates.append((predicted_role, mention_id))

    # Writes happen only after the whole selected set has been classified, and
    # only in live mode.  Committed reflects rows the transaction actually wrote.
    committed = 0
    failure: str | None = None
    if not dry_run and pending_updates:
        try:
            for start in range(0, len(pending_updates), BATCH_SIZE):
                chunk = pending_updates[start:start + BATCH_SIZE]
                _flush_updates(engine, chunk)
                committed += len(chunk)
            total_updated = committed
        except Exception as error:
            failure = f"{type(error).__name__}: {error}"
            log.error("  ✗ role update failed: %s", error)

    elapsed = time.time() - start_ts

    return {
        "success": failure is None,
        "total_scanned": total_scanned,
        "total_updated": total_updated,
        "duration_s": round(elapsed, 1),
        "distribution": dict(sorted(distribution.items(), key=lambda x: -x[1])[:10]),
        "dry_run": dry_run,
        "role_proposals": role_proposals,
        "role_would_update": role_would_update,
        "role_replay_noop": role_replay_noop,
        "role_unresolved": role_unresolved,
        **selection_evidence,
        "validation_receipt": build_phase_receipt(
            "role_classifier",
            dry_run=dry_run,
            values=[("role", role) for role in emission_roles],
            rows=role_classifier_accounting(
                {"would_update": role_would_update,
                 "replay_noop": role_replay_noop,
                 "unresolved": role_unresolved},
                committed=committed, dry_run=dry_run,
            ),
            failure=failure,
        ),
    }


def main():
    parser = argparse.ArgumentParser(
        description="XGBoost ensemble role classification (95.5% accuracy)"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would be classified without writing")
    parser.add_argument("--force", action="store_true",
                        help="Re-classify even entities with existing roles")
    parser.add_argument("--confidence", type=float, default=0.5,
                        help="Minimum confidence to apply (default: 0.5)")
    parser.add_argument(
        "--limit", type=int, default=DEFAULT_SCAN_LIMIT,
        help=f"Maximum mentions to classify (default: {DEFAULT_SCAN_LIMIT})",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    engine = get_engine()
    result = run_role_classifier(
        engine,
        dry_run=args.dry_run,
        force=args.force,
        confidence=args.confidence,
        verbose=args.verbose,
        limit=args.limit,
    )

    mode = "DRY RUN" if result["dry_run"] else "DONE"
    log.info("%s — %d scanned, %d updated in %.0fs",
             mode, result["total_scanned"], result["total_updated"],
             result["duration_s"])
    if result["distribution"]:
        log.info("Distribution: %s", result["distribution"])

    print(json.dumps({"phase": "role_classifier", **result}))


def _flush_updates(engine: Engine, updates: list[tuple[str, int]]) -> None:
    """Bulk update role classifications.

    Kept as a module-level seam: callers and tests patch
    ``role_classifier._flush_updates``.  The statement itself lives in
    :mod:`scripts.entities.role_classifier_proposals`.
    """
    from scripts.entities.role_classifier_proposals import flush_role_updates

    flush_role_updates(engine, updates)


if __name__ == "__main__":
    main()
