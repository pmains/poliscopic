"""Isolated batching tests for the role-classification phase.

The real classifier artifacts are deliberately replaced with deterministic
fakes.  These tests use only the function-scoped SQLite fixture and never load
the 769 MiB fastText model or make a model-network request.
"""

import logging
import hashlib
import json

import numpy as np
import pytest
from sqlalchemy import text

from db.models import Entity, EntityMention
import scripts.entities.role_classifier as classifier


@pytest.fixture()
def engine(fresh_session):
    """Return the isolated temporary SQLite engine."""
    return fresh_session.get_bind()


def _seed_mentions(session, count: int = 5, current_roles: list[str] | None = None) -> list[int]:
    entity = Entity(
        entity_type="organization",
        name="Batch Test Entity",
        normalized_name="batch-test-entity",
    )
    session.add(entity)
    session.flush()

    current_roles = current_roles or [""] * count
    mentions = [
        EntityMention(
            entity_id=entity.id,
            source_type="test",
            source_id=index + 1,
            context_snippet=f"context-{index}",
            role_in_context=current_roles[index],
            extracted_by="test",
        )
        for index in range(count)
    ]
    session.add_all(mentions)
    session.commit()
    return [mention.id for mention in mentions]


class _FakeXgbModel:
    """Return stable three-class probabilities from the fake feature index."""

    def predict(self, features):
        probabilities = np.zeros((len(features), 3))
        for row_index, feature_index in enumerate(features[:, 0].astype(int)):
            probabilities[row_index, feature_index % 3] = 0.9
            probabilities[row_index, (feature_index + 1) % 3] = 0.1
        return probabilities


def _install_prediction_fakes(monkeypatch, feature_calls: list[list[str]]):
    """Install deterministic model seams and return fastText-loader calls."""
    fasttext_loads: list[object] = []
    fasttext_model = object()

    def fake_load_fasttext():
        fasttext_loads.append(fasttext_model)
        return fasttext_model

    def fake_build_features(names, contexts, full_texts, bodies, *, fasttext_model):
        assert fasttext_model is fasttext_model_sentinel
        feature_calls.append(list(contexts))
        return np.array([[int(context.rsplit("-", 1)[1])] for context in contexts])

    # Name the sentinel separately so the fake's assertion cannot accidentally
    # compare the parameter to itself.
    fasttext_model_sentinel = fasttext_model
    monkeypatch.setattr(classifier, "load_model", lambda: True)
    monkeypatch.setattr(classifier, "_load_fasttext_model", fake_load_fasttext)
    monkeypatch.setattr(classifier, "build_features", fake_build_features)
    monkeypatch.setattr(classifier, "ROLE_NAMES", ["applicant", "attorney", "staff"])
    monkeypatch.setattr(classifier, "XGB_MODEL", _FakeXgbModel())
    monkeypatch.setattr(classifier.xgb, "DMatrix", lambda features: features)
    return fasttext_loads


def test_feature_batches_are_contiguous_and_report_progress(engine, fresh_session,
                                                             monkeypatch, caplog):
    _seed_mentions(fresh_session)
    feature_calls: list[list[str]] = []
    _install_prediction_fakes(monkeypatch, feature_calls)
    monkeypatch.setattr(classifier, "FEATURE_BATCH_SIZE", 2)
    monkeypatch.setattr(classifier, "_flush_updates", lambda *_: None)

    with caplog.at_level(logging.INFO, logger=classifier.log.name):
        result = classifier.run_role_classifier(engine, force=True)

    assert feature_calls == [
        ["context-0", "context-1"],
        ["context-2", "context-3"],
        ["context-4"],
    ]
    progress = [record.message for record in caplog.records
                if record.message.startswith("Building feature batch")]
    assert progress == [
        "Building feature batch 1-2 of 5.",
        "Building feature batch 3-4 of 5.",
        "Building feature batch 5-5 of 5.",
    ]
    assert result["total_scanned"] == 5


def test_fasttext_model_is_loaded_once_for_a_multi_batch_run(engine, fresh_session,
                                                               monkeypatch):
    _seed_mentions(fresh_session)
    feature_calls: list[list[str]] = []
    fasttext_loads = _install_prediction_fakes(monkeypatch, feature_calls)
    monkeypatch.setattr(classifier, "FEATURE_BATCH_SIZE", 2)
    monkeypatch.setattr(classifier, "_flush_updates", lambda *_: None)

    classifier.run_role_classifier(engine, force=True)

    assert len(feature_calls) == 3
    assert len(fasttext_loads) == 1


def test_dry_run_and_live_make_identical_classification_decisions(engine, fresh_session,
                                                                    monkeypatch):
    mention_ids = _seed_mentions(fresh_session, current_roles=["", "attorney", "", "", ""])
    feature_calls: list[list[str]] = []
    _install_prediction_fakes(monkeypatch, feature_calls)
    monkeypatch.setattr(classifier, "FEATURE_BATCH_SIZE", 2)
    flushes: list[list[tuple[str, int]]] = []
    monkeypatch.setattr(classifier, "_flush_updates",
                        lambda _, updates: flushes.append(list(updates)))

    live = classifier.run_role_classifier(engine, force=True)
    live_feature_calls = list(feature_calls)
    feature_calls.clear()
    dry = classifier.run_role_classifier(engine, dry_run=True, force=True)

    assert live_feature_calls == feature_calls
    assert live["distribution"] == dry["distribution"]
    assert live["total_updated"] == 4
    assert dry["total_updated"] == 0
    assert flushes == [[
        ("applicant", mention_ids[0]),
        ("staff", mention_ids[2]),
        ("applicant", mention_ids[3]),
        ("attorney", mention_ids[4]),
    ]]


def test_feature_chunk_output_stacks_to_the_single_batch_output(monkeypatch):
    class FakeFastText:
        def predict(self, text, k=1):
            return (["__label__applicant"], [0.75])

    class FakeEncoder:
        def __init__(self):
            self.batch_sizes: list[int] = []

        def encode(self, inputs, *, batch_size, show_progress_bar):
            assert show_progress_bar is False
            self.batch_sizes.append(batch_size)
            return np.array([[float(len(value))] * 384 for value in inputs])

    class FakeSparseMatrix:
        def __init__(self, values):
            self.values = values

        def toarray(self):
            return np.array([[float(len(value))] * 500 for value in self.values])

    class FakeTfidf:
        def transform(self, values):
            return FakeSparseMatrix(values)

    encoder = FakeEncoder()
    monkeypatch.setattr(classifier, "ROLE_NAMES", ["applicant"])
    monkeypatch.setattr(classifier, "SENTENCE_ENCODER", encoder)
    monkeypatch.setattr(classifier, "TFIDF", FakeTfidf())

    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    contexts = ["context one", "context two", "context three", "", "context five"]
    full_texts = ["Alpha full text", "", "no match", "Delta full text", "Echo full text"]
    bodies = ["phoenix-cc", "bos", "unknown", "tempe-cc", "pz"]
    model = FakeFastText()

    full = classifier.build_features(names, contexts, full_texts, bodies,
                                     fasttext_model=model)
    chunks = [
        classifier.build_features(names[start:start + 2], contexts[start:start + 2],
                                  full_texts[start:start + 2], bodies[start:start + 2],
                                  fasttext_model=model)
        for start in range(0, len(names), 2)
    ]

    assert np.array_equal(full, np.vstack(chunks))
    assert encoder.batch_sizes == [32] * 8


def test_update_flush_threshold_spans_feature_batches(engine, fresh_session,
                                                       monkeypatch):
    mention_ids = _seed_mentions(fresh_session)
    feature_calls: list[list[str]] = []
    _install_prediction_fakes(monkeypatch, feature_calls)
    monkeypatch.setattr(classifier, "FEATURE_BATCH_SIZE", 2)
    monkeypatch.setattr(classifier, "BATCH_SIZE", 3)
    flushes: list[list[tuple[str, int]]] = []
    monkeypatch.setattr(classifier, "_flush_updates",
                        lambda _, updates: flushes.append(list(updates)))

    result = classifier.run_role_classifier(engine, force=True)

    assert result["total_updated"] == 5
    assert flushes == [
        [("applicant", mention_ids[0]), ("attorney", mention_ids[1]),
         ("staff", mention_ids[2])],
        [("applicant", mention_ids[3]), ("attorney", mention_ids[4])],
    ]


def test_scan_limit_bounds_the_verification_sample(engine, fresh_session,
                                                    monkeypatch):
    _seed_mentions(fresh_session, count=5)
    feature_calls: list[list[str]] = []
    _install_prediction_fakes(monkeypatch, feature_calls)
    monkeypatch.setattr(classifier, "_flush_updates", lambda *_: None)

    result = classifier.run_role_classifier(
        engine, dry_run=True, force=True, limit=2,
    )

    assert result["total_scanned"] == 2
    assert feature_calls == [["context-0", "context-1"]]


def test_selection_is_ordered_by_id_despite_insertion_and_query_plan_order(
    engine, fresh_session, monkeypatch
):
    """A bounded scan chooses the lowest IDs, not insertion/query-plan order."""
    entity = Entity(
        entity_type="organization",
        name="Ordered Selection Entity",
        normalized_name="ordered-selection-entity",
    )
    fresh_session.add(entity)
    fresh_session.flush()
    for mention_id, context, role in ((30, "context-2", None),
                                      (10, "context-0", ""),
                                      (20, "context-1", None)):
        fresh_session.add(EntityMention(
            id=mention_id,
            entity_id=entity.id,
            source_type="test",
            source_id=mention_id,
            context_snippet=context,
            role_in_context=role,
            extracted_by="test",
        ))
    fresh_session.commit()
    with engine.begin() as connection:
        connection.execute(text(
            "CREATE INDEX ix_role_classifier_selection_role "
            "ON entity_mentions (role_in_context)"
        ))

    feature_calls: list[list[str]] = []
    _install_prediction_fakes(monkeypatch, feature_calls)
    monkeypatch.setattr(classifier, "_flush_updates", lambda *_: None)

    result = classifier.run_role_classifier(engine, dry_run=True, force=False,
                                            limit=2)

    assert feature_calls == [["context-0", "context-1"]]
    assert result["selected_first_id"] == 10
    assert result["selected_last_id"] == 20
    assert result["selected_ids_sha256"] == hashlib.sha256(
        json.dumps([10, 20], separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_identical_dry_runs_have_stable_selection_evidence(
    engine, fresh_session, monkeypatch
):
    """Repeated dry runs report identical evidence for the same ID selection."""
    _seed_mentions(fresh_session, count=4)
    _install_prediction_fakes(monkeypatch, [])
    monkeypatch.setattr(classifier, "_flush_updates", lambda *_: None)

    first = classifier.run_role_classifier(engine, dry_run=True, force=True,
                                           limit=3)
    second = classifier.run_role_classifier(engine, dry_run=True, force=True,
                                            limit=3)

    evidence_keys = {
        "selected_ids_sha256", "selected_first_id", "selected_last_id",
    }
    assert {key: first[key] for key in evidence_keys} == {
        key: second[key] for key in evidence_keys
    }


def test_empty_selection_reports_empty_evidence(engine, monkeypatch):
    """An empty bounded scan has a deterministic digest and null endpoints."""
    result = classifier.run_role_classifier(engine, dry_run=True, force=True)

    assert result["selected_ids_sha256"] == hashlib.sha256(
        b"[]"
    ).hexdigest()
    assert result["selected_first_id"] is None
    assert result["selected_last_id"] is None


def test_cpu_environment_is_bounded_without_overriding_operator_values():
    environment = {"OMP_NUM_THREADS": "2"}

    classifier._configure_cpu_environment(environment, thread_count=1)

    assert environment == {
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "1",
        "TOKENIZERS_PARALLELISM": "false",
    }
