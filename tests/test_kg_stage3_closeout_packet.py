from pathlib import Path

import pytest

from scripts.kg import stage3_closeout_packet as C

B4 = Path("data/kg-plans/kg-stage3-b4-quarantine-closeout-20260914T212201Z.json")
ELIGIBILITY = Path("data/kg-plans/kg-stage3-source-eligibility-20260914T211300Z.json")
PROCESSING = Path("data/kg-plans/kg-stage3-processing-identity-20260914T213023Z.json")
B3 = Path("data/kg-plans/kg-stage3-b3-span-baseline-20260914T204038Z.json")


def build():
    return C.build(b4_path=B4, eligibility_path=ELIGIBILITY,
                   processing_path=PROCESSING, b3_baseline_path=B3,
                   created_at="now")


def test_current_packet_is_honest_and_not_ready():
    value = build()
    assert value["verdict"] == "STAGE3_NOT_READY"
    assert value["gates"]["b4_quarantine_closed"] is True
    assert value["gates"]["document_text"]["total"] == 65510
    assert value["gates"]["document_text"]["with_supported_text"] == 64580
    assert value["gates"]["document_text"]["passes"] is True
    assert value["gates"]["current_processing"]["current_proven"] == 0
    assert value["gates"]["current_processing"]["passes"] is False
    assert value["gates"]["quality_benchmark"]["passes"] is False


def test_exact_artifacts_and_target_are_bound():
    value = build()
    assert set(value["bindings"]) == {"b4", "eligibility", "processing", "b3"}
    assert all(binding["digest"] for binding in value["bindings"].values())
    assert value["target"]["database"] == "poliscopic_dev"
    assert set(value["code_hashes"]) == set(C.CODE_MODULES)
    assert value["gates"]["document_text"]["document_exceptions"] == 930


def test_digest_and_no_write_path():
    value = build()
    assert value["digest"] == C.canonical_sha256(
        {k: v for k, v in value.items() if k != "digest"})
    assert value["write_path"] == "absent by design"
    assert value["mutations_proposed"] == 0
    assert not hasattr(C, "apply")


def test_obsolete_component_refuses():
    old = Path("data/kg-plans/kg-stage3-processing-identity-20260914T211621Z.json")
    with pytest.raises(ValueError, match="obsolete"):
        C.build(b4_path=B4, eligibility_path=ELIGIBILITY,
                processing_path=old, b3_baseline_path=B3, created_at="now")
