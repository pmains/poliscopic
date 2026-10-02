"""Contracts for the read-only repository-hygiene inventory."""

from scripts.ops.repo_hygiene_audit import (
    build_report,
    classify_path,
    parse_porcelain,
)


def test_path_classification_separates_source_evidence_and_local_state():
    assert classify_path("src/poliscopic/db/core.py") == "runtime_source"
    assert classify_path("workflows/housing.yaml") == "runtime_source"
    assert classify_path("tests/test_app.py") == "tests"
    assert classify_path("briefs/20260928-audit.md") == "research_evidence"
    assert classify_path("docs/briefs/042-example.md") == "durable_roadmap"
    assert classify_path("data/runs/example.json") == "generated_or_local_state"
    assert classify_path(".env.production") == "local_secret"
    assert classify_path(".env.example") == "runtime_configuration"


def test_porcelain_parser_handles_modified_untracked_and_renamed_paths():
    payload = (
        b" M routes/articles.py\0"
        b"?? docs/briefs/042-example.md\0"
        b"R  scripts/new_name.py\0old name.py\0"
    )

    changes = parse_porcelain(payload)

    assert [(item.status, item.path) for item in changes] == [
        (" M", "routes/articles.py"),
        ("??", "docs/briefs/042-example.md"),
        ("R ", "scripts/new_name.py"),
    ]


def test_report_is_read_only_and_surfaces_attention_categories():
    changes = parse_porcelain(
        b"?? .env.secret\0?? mystery.bin\0 M tests/test_app.py\0"
    )

    report = build_report(changes)

    assert report["read_only"] is True
    assert report["total_changes"] == 3
    assert report["attention"] == {
        "local_secrets_present": 1,
        "unclassified_paths": 1,
    }
