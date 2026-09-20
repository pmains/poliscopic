from copy import deepcopy

from scripts.kg import stage3_b3_span_contract as C


TEXT = "Préface\nITEM 4.A approved\n"


def span(**overrides):
    values = dict(
        supporting_doc_id=17,
        span_kind="stage3_result_item",
        start=8,
        end=len(TEXT),
        text=TEXT,
        extraction_method="pymupdf",
        extractor_version="b1/1.0",
        lineage_parent_id=91,
        agenda_item_candidates=[404],
    )
    values.update(overrides)
    return C.build_span(**values)


def test_identity_and_coordinates_are_explicit():
    candidate = span()
    assert C.span_identity(candidate) == (17, "stage3_result_item", 8, len(TEXT))
    assert candidate["coordinate_system"] == "unicode_codepoint_half_open"
    assert candidate["span_sha256"] == C.text_sha256(TEXT[8:])
    assert candidate["agenda_item_db_id"] == 404
    assert candidate["link_status"] == C.LINK_BOUND
    assert C.validate_span(candidate, TEXT) == []


def test_evidence_version_binds_document_text_and_extractor():
    candidate = span()
    assert candidate["evidence_version_identity"] == [
        17, C.text_sha256(TEXT), "pymupdf", "b1/1.0"
    ]
    assert "evidence content fingerprint drift" in C.validate_span(candidate, TEXT + "changed")


def test_offset_tamper_and_out_of_range_fail_closed():
    tampered = span()
    tampered["text_offset_start"] += 1
    assert "span fingerprint mismatch" in C.validate_span(tampered, TEXT)
    outside = span(end=len(TEXT) + 1)
    assert "offsets exceed retained text" in C.validate_span(outside, TEXT)


def test_span_fingerprint_tamper_fails_closed():
    candidate = span()
    candidate["span_sha256"] = "0" * 64
    assert "span fingerprint mismatch" in C.validate_span(candidate, TEXT)


def test_missing_lineage_fails_closed():
    candidate = span()
    candidate["lineage"] = {}
    problems = C.validate_span(candidate, TEXT)
    assert "lineage.source must equal span_kind" in problems
    assert "lineage.parent_id must be a positive integer" in problems


def test_missing_text_has_its_own_hold():
    candidate = span(text="", start=0, end=1)
    result = C.classify_spans([candidate], {17: ""})
    assert result["holds"][0]["disposition"] == "hold_missing_text"


def test_unlinked_and_ambiguous_containers_are_named_holds():
    unlinked = span(agenda_item_candidates=None)
    ambiguous = span(start=0, end=7, agenda_item_candidates=[404, 405])
    result = C.classify_spans([unlinked, ambiguous], {17: TEXT})
    assert [row["disposition"] for row in result["holds"]] == [
        "hold_unlinked_container", "hold_ambiguous_container"
    ]
    assert result["reconciles"] is True


def test_invalid_coordinates_are_not_masked_by_unlinked_container():
    candidate = span(start=-1, end=3, agenda_item_candidates=None)
    result = C.classify_spans([candidate], {17: TEXT})
    assert result["holds"][0]["disposition"] == "hold_invalid_offsets"


def test_duplicate_identity_is_held_even_when_content_matches():
    candidate = span()
    result = C.classify_spans([candidate, deepcopy(candidate)], {17: TEXT})
    assert result["accepted_count"] == 1
    assert result["holds"][0]["disposition"] == "hold_duplicate_identity"
    assert result["reconciles"] is True


def test_every_proposal_is_accepted_or_held_once():
    valid = span()
    unlinked = span(start=0, end=7, agenda_item_candidates=None)
    result = C.classify_spans([valid, unlinked], {17: TEXT})
    receipt = C.accounting(result)
    assert receipt == {
        "proposed": 2,
        "would_insert": 1,
        "held": 1,
        "holds_by_disposition": {
            "hold_missing_text": 0,
            "hold_invalid_offsets": 0,
            "hold_fingerprint_mismatch": 0,
            "hold_missing_evidence_version": 0,
            "hold_missing_lineage": 0,
            "hold_unlinked_container": 1,
            "hold_ambiguous_container": 0,
            "hold_duplicate_identity": 0,
        },
        "reconciles": True,
    }


def test_dry_plan_has_no_write_path_and_binds_code():
    result = C.classify_spans([span()], {17: TEXT})
    plan = C.build_dry_plan(
        classification=result,
        created_at="2026-09-14T00:00:00Z",
        target={"tier": "test"},
        code_hashes={"stage3_b3_span_contract.py": "abc"},
    )
    assert plan["write_path"] == "absent by design"
    assert plan["applied"] is False
    assert plan["accounting"]["reconciles"] is True
    assert plan["digest"] == C.canonical_sha256({k: v for k, v in plan.items()
                                                  if k != "digest"})


def test_unchanged_replay_is_noop_and_drift_conflicts():
    result = C.classify_spans([span()], {17: TEXT})
    plan = C.build_dry_plan(
        classification=result, created_at="now", target={}, code_hashes={"contract": "x"}
    )
    candidate = plan["operations"][0]
    identity = C.span_identity(candidate)
    replay = C.replay_check(plan=plan, existing={identity: candidate["span_sha256"]})
    assert replay["is_noop"] is True
    assert replay["would_write"] == 0
    drift = C.replay_check(plan=plan, existing={identity: "0" * 64})
    assert drift["is_noop"] is False
    assert drift["conflicts"] == [identity]


def test_no_database_or_write_surface_is_exported():
    assert not hasattr(C, "apply")
    assert not hasattr(C, "engine")
    assert not hasattr(C, "connection")
