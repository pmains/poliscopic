#!/usr/bin/env python3
"""Read-only validator for the authoritative production operations checklist.

WHAT THIS PROVES
    That `briefs/PRODUCTION-OPERATIONS-CHECKLIST.md` still CONTAINS its required
    structure: the mandated headings, the ordered gates, a STOP condition and the
    required evidence/authorization fields in every gate, the operation-separation
    rule, the live scheduler ids, and the canonical evidence index.

WHAT THIS DOES NOT PROVE
    Nothing about runtime safety. This is a documentation-integrity check only. It
    cannot tell whether any gate was actually performed, and it cannot prevent a
    production write. Padding the document with the right strings would pass this
    test while remaining unsafe — that is a known and accepted limitation, and the
    checklist itself says so in its "What this document does not do" section.

No production access, no database access, no network. Pure text assertions.
"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
CHECKLIST = _ROOT / "briefs" / "PRODUCTION-OPERATIONS-CHECKLIST.md"

# Gate headings: "### G7 — Exact digest-bound plan and rollback ownership"
GATE_RE = re.compile(r"^### G(\d+) — ", re.M)

REQUIRED_SECTIONS = (
    "# PRODUCTION OPERATIONS CHECKLIST",
    "## 1. Operation classification",
    "## 2. Gate sequence",
    "## 3. Morning workflow",
    "## 4. Emergency / incident rules",
    "## 5. Canonical evidence index and naming convention",
    "## 6. What this document does not do",
)

# The six mandatory fields every gate must carry.
REQUIRED_FIELDS = (
    "**Operator action:**",
    "**Entry point:**",
    "**Expected result:**",
    "**Evidence artifact:**",
    "**STOP if**",
    "**Human authorization:**",
)

OPERATION_KINDS = (
    "OP-DEV", "OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE",
)

SCHEDULER_IDS = (
    "<daily-sync-job-id>",  # maricopa-daily-sync
    "<sync-checker-job-id>",  # maricopa-sync-checker
    "<prod-sync-job-id>",  # maricopa-prod-sync
)

# Artifact classes the evidence index must name.
EVIDENCE_CLASSES = (
    "Release manifest", "Backup dump", "Plan (digest-bound)", "Preimage",
    "Terminal receipt", "Postflight snapshot", "HTTP check", "Incident record",
)


def _text() -> str:
    assert CHECKLIST.exists(), f"missing checklist: {CHECKLIST}"
    return CHECKLIST.read_text()


def _flat() -> str:
    """Whitespace-normalized text.

    Phrase assertions must run against this, not the raw text: the document is
    hard-wrapped, so a literal phrase frequently spans a newline. Matching raw text
    produced false failures (e.g. "no production authorization may be issued under
    `OP-DEV`" split across two lines). This normalizes ONLY whitespace; it does not
    weaken the assertion.
    """
    return re.sub(r"\s+", " ", _text())


def _gates() -> list[tuple[int, int, str]]:
    """(gate number, char offset, gate block text) in document order."""
    text = _text()
    matches = list(GATE_RE.finditer(text))
    blocks = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        blocks.append((int(m.group(1)), m.start(), text[m.start():end]))
    return blocks


# ── structure ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("heading", REQUIRED_SECTIONS)
def test_required_section_present(heading):
    assert heading in _text(), f"required section missing: {heading!r}"


def test_gates_are_present_and_contiguous_g1_to_g13():
    nums = [n for n, _, _ in _gates()]
    assert nums == list(range(1, 14)), f"expected G1..G13 in order, found {nums}"


def test_gate_order_matches_the_mandated_sequence():
    """Each gate's purpose must appear in the mandated order."""
    order = [block.splitlines()[0] for _, _, block in _gates()]
    expected_keywords = [
        "scheduler state and production hold",
        "Git source and exact commit",
        "release allowlist / manifest digest",
        "tests and known-failure accounting",
        "pinned-target identity and integrity snapshot",
        "backup, SHA-256, inventory, scratch restore validation",
        "digest-bound plan and rollback ownership",
        "human authorization quoting operation kind and digest",
        "one bounded execution",
        "immutable terminal receipt",
        "database postconditions and public HTTP 200 checks",
        "rollback decision",
        "scheduler re-enable",
    ]
    assert len(order) == len(expected_keywords)
    for heading, keyword in zip(order, expected_keywords):
        # case-insensitive: headings are title-cased but keywords are lower-case
        assert keyword.lower() in heading.lower(), (
            f"gate {heading!r} missing purpose {keyword!r}"
        )


# ── per-gate fields and STOP language ────────────────────────────────────


@pytest.mark.parametrize("field", REQUIRED_FIELDS)
def test_every_gate_carries_the_field(field):
    missing = [n for n, _, block in _gates() if field not in block]
    assert missing == [], f"gates missing {field!r}: {missing}"


def test_every_gate_has_a_nonempty_stop_condition():
    """`**STOP if**` must be followed by real text, not left dangling."""
    empty: list[int] = []
    for num, _, block in _gates():
        m = re.search(r"\*\*STOP if\*\*(.{0,400})", block, re.S)
        assert m, f"gate G{num} has no STOP condition"
        following = m.group(1)
        # stop the slice at the next field label, then strip markdown noise
        for label in REQUIRED_FIELDS:
            idx = following.find(label)
            if idx != -1:
                following = following[:idx]
        cleaned = re.sub(r"[\s*`_—:.\-]+", "", following)
        if len(cleaned) < 20:
            empty.append(num)
    assert empty == [], f"gates with an empty/placeholder STOP condition: {empty}"


def test_g9_g12_g13_are_authorization_gated():
    """G8 is the authorization gate; G12 rollback and G13 re-enable also require it."""
    # Flatten each block: gate prose is hard-wrapped, so phrases span newlines.
    blocks = {n: re.sub(r"\s+", " ", b) for n, _, b in _gates()}
    assert "Required — this is the authorization gate." in blocks[8]
    assert "Required if rolling back" in blocks[12]
    assert "re-enabling production writing is itself an authorized act" in blocks[13]


# ── operation classification and separation ──────────────────────────────


@pytest.mark.parametrize("kind", OPERATION_KINDS)
def test_operation_kind_documented(kind):
    assert kind in _text(), f"operation kind missing from classification: {kind}"


def test_single_operation_separation_rule_is_explicit():
    text = _flat()
    assert ("No checklist run and no authorization artifact may cover more than one"
            in text), "the single-operation separation rule is not stated"
    # the production kinds must be named together as mutually exclusive
    for kind in ("OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"):
        assert kind in text
    assert "are never one operation" in text, (
        "code deploy and reconciliation must be explicitly stated as never one operation"
    )


def test_op_dev_is_documented_as_non_production():
    text = _flat()
    assert "OP-DEV" in text
    assert "touches no production" in text
    assert "no production authorization may be issued under `OP-DEV`" in text


# ── scheduler ids and held posture ───────────────────────────────────────


@pytest.mark.parametrize("job_id", SCHEDULER_IDS)
def test_scheduler_id_present(job_id):
    assert job_id in _text(), f"scheduler id missing: {job_id}"


def test_scheduler_states_recorded():
    text = _text()
    assert "maricopa-daily-sync" in text
    assert "maricopa-sync-checker" in text
    assert "maricopa-prod-sync" in text
    assert "OpenClaw scheduler is the source of truth" in text
    assert "never re-enable a disabled job" in text


# ── morning workflow ─────────────────────────────────────────────────────


def test_morning_workflow_states_the_required_rules():
    text = _flat()
    assert "may run while production is held" in text
    assert "development analysis only" in text
    assert "Production reconciliation never follows automatically" in text
    assert "An old merge receipt cannot authorize a new operation" in text
    assert "remain held — not retry production" in text


# ── emergency rules ──────────────────────────────────────────────────────


@pytest.mark.parametrize("rule", (
    "Freeze production-writing jobs first",
    "Preserve logs and backups before repair",
    "Never fabricate a missing receipt",
    "preserved-unverified",
    "Record every production write, even failed and rolled-back attempts",
    "Website health does not prove database integrity",
))
def test_emergency_rule_present(rule):
    assert rule in _text(), f"emergency rule missing: {rule!r}"


# ── evidence index ───────────────────────────────────────────────────────


@pytest.mark.parametrize("cls", EVIDENCE_CLASSES)
def test_evidence_index_names_artifact_class(cls):
    assert cls in _text(), f"evidence index missing artifact class: {cls!r}"


def test_evidence_naming_convention_is_defined():
    text = _flat()
    assert "%Y%m%dT%H%M%SZ" in text, "UTC stamp format not documented"
    assert "data/backups/" in text
    assert "data/body-code-merge/prod/" in text
    assert "data/audit/" in text


def test_evidence_authority_is_stated_honestly():
    """The checklist must not claim ignored artifacts are version-controlled."""
    text = _flat()
    assert "Every artifact above lives under ignored `data/`" in text
    assert "None of it is version-controlled" in text


# ── authority boundary ───────────────────────────────────────────────────


def test_checklist_declares_itself_the_sole_authority():
    text = _flat()
    assert "This file is the single authority for production operations." in text
    assert "briefs/PRODUCTION-OPERATIONS-CHECKLIST.md` (Git-tracked)" in text
    assert "`.gitignore` ignores the entire `docs/` tree" in text


def test_brief_039_is_labelled_local_and_not_tracked():
    """Brief 039 lives under ignored docs/ and must not be claimed as tracked."""
    text = _text()
    assert "docs/briefs/039-containment-freeze-and-recovery.md" in text
    assert "**local, NOT tracked**" in text


def test_stabilization_record_is_linked_as_tracked():
    text = _text()
    assert "briefs/20260918-repository-stabilization.md" in text
    assert "**tracked** operational record" in text


# ── honest limitation ────────────────────────────────────────────────────


def test_document_disclaims_that_validation_proves_runtime_safety():
    text = _flat()
    assert "documentation integrity check only" in text
    assert "cannot prevent a production write" in text
    assert "If this document and runtime behavior disagree" in text


def test_validator_does_not_claim_runtime_safety_in_its_own_header():
    """Guard against the test file itself overclaiming."""
    src = Path(__file__).read_text()
    assert "Nothing about runtime safety" in src
    assert "documentation-integrity check only" in src


# ── negative pins: the corrected dangerous text/commands must be ABSENT ───


def _code_fences(text: str) -> str:
    """Concatenated contents of fenced code blocks.

    The corrections deliberately MENTION the removed commands in prose ("this was
    removed because it does not exist"). So absence must be pinned against the
    executable blocks, not against the whole document.
    """
    return "\n".join(re.findall(r"```[^`]*?\n(.*?)```", text, re.S))


def test_invented_scheduler_command_is_gone_entirely():
    """`openclaw automations list` must not appear at all — it is never needed."""
    assert "openclaw automations list" not in _text()


def test_removed_commands_are_not_presented_as_executable():
    """The removed commands may be discussed, but never shown as a runnable step."""
    fences = _code_fences(_text())
    assert "--plan-only" not in fences, (
        "`--plan-only` is still presented inside an executable block"
    )
    assert "verify_morning_sync_readiness" not in fences, (
        "verify_morning_sync_readiness.py is still presented as a G5 step"
    )
    assert "openclaw automations" not in fences


def test_fixed_mutable_manifest_path_is_not_canonical_evidence():
    text = _flat()
    assert "is **no longer canonical evidence**" in text
    assert "data/release/<operation>-<OPERATION_ID>/manifest.json" in text


# ── canonical scheduler interface, verified against real CLI help ─────────


def test_canonical_scheduler_commands_are_pinned():
    text = _flat()
    assert "openclaw cron list --all --json" in text
    assert "openclaw cron enable <id>" in text
    assert "openclaw cron disable <id>" in text


def test_document_explains_why_all_is_required():
    """`cron list` hides disabled jobs by default; G1 must see them."""
    text = _flat()
    assert "excludes disabled jobs by default" in text
    assert "a check that cannot" in text and "cannot confirm it is off" in text


# ── implemented and unimplemented gate semantics ──────────────────────────


def test_g5_names_the_supported_live_preflight():
    block = re.sub(r"\s+", " ", dict((n, b) for n, _, b in _gates())[5])
    assert "production_preflight.py" in block
    assert "REPEATABLE READ" in block
    assert "READ ONLY" in block
    assert "created exclusively" in block
    assert "verify_morning_sync_readiness" not in _code_fences(block)


def test_g7_is_a_blocker_and_names_the_real_options():
    block = re.sub(r"\s+", " ", dict((n, b) for n, _, b in _gates())[7])
    assert "NO GENERAL IMMUTABLE OPERATION-PLAN BUILDER EXISTS" in block
    assert "blocked" in block
    for opt in ("--status", "--reconcile-dry-run"):
        assert opt in block, f"{opt} should be named as the real (non-planner) options"
    assert "do not present pseudocode as" in block


# ── immutability and verbatim approval ────────────────────────────────────


def test_per_operation_immutable_evidence_is_required():
    text = _flat()
    assert "OPERATION_ID" in text
    assert "mode 0600" in text
    assert "exclusive creation where implemented" in text
    assert "Exclusive creation is not implemented" in text
    assert "single-use" in text


def test_verbatim_human_approval_rules_are_pinned():
    text = _flat()
    assert "verbatim" in text
    assert "may NOT create, infer, broaden, paraphrase, or self-issue one" in text
    assert "issuance is DISABLED" in text or "issuance is disabled" in text

def test_agent_may_record_but_not_issue_authorization():
    text = _flat()
    assert "may RECORD an approval that was already given" in text
    assert "self-authorized, which is prohibited" in text


# ── backup / restore and test-relevance corrections ──────────────────────


def test_restore_verification_is_mandatory_and_offvolume():
    text = _flat()
    assert "restore verification is MANDATORY before any operation that can mutate" in text
    assert "Same-volume hardlinks are NOT protection" in text
    assert "insufficient on its own" in text
    assert "failed or unverified restore is a STOP" in text
    assert "code restart is not assumed DB-neutral" in text


def test_tests_gate_requires_relevance_not_just_accounting():
    text = _flat()
    assert "Every test relevant to the proposed operation passes" in text
    assert '"accounted for" is **not** sufficient' in text or "is **not** sufficient" in text
    assert "Unknown relevance is a STOP" in text


def test_no_duplicated_word_defects():
    """Catch real duplicated words without flagging hyphenated compounds.

    The original pattern `\\b(\\w{3,})\\s+\\1\\b` produced a FALSE POSITIVE on
    "read-only only", because `\\b` treats the hyphen as a boundary and so reads the
    `only` of `read-only` as a standalone word. The lookbehind `(?<![\\w-])` requires
    the first word to not be part of a hyphenated compound, which is correct: a
    genuine defect looks like "the the", not "read-only only".
    """
    dupes = re.findall(r"(?<![\w-])([A-Za-z]{3,})\s+\1\b", _text())
    assert dupes == [], f"duplicated word(s) present: {dupes}"


def test_duplicated_word_guard_does_not_flag_hyphenated_compounds():
    """Pin the guard's own correctness so it cannot regress to a false positive."""
    sandbox = "a read-only only mode"
    assert re.findall(r"(?<![\w-])([A-Za-z]{3,})\s+\1\b", sandbox) == []
    assert re.findall(r"(?<![\w-])([A-Za-z]{3,})\s+\1\b", "the the plan") == ["the"]


# ── blockers must be labelled, and no runtime enforcement implied ─────────


def test_blockers_section_lists_missing_controls():
    text = _flat()
    assert "## 6.1 Blockers, and which controls now exist" in text
    for bid in ("B1", "B2", "B3", "B4", "B5", "B6", "B7"):
        assert f"| {bid} |" in text, f"blocker {bid} missing"
    # Was a vacuous truthy-tuple assertion; now a real check. §6.1 must both be
    # labelled as blockers AND record which controls exist.
    assert "RESOLVED" in text
    assert "OPEN" in text
    assert "does not make production ready" in text


def test_implemented_interlock_is_documented():
    """Batch 2 behaviour must be recorded, without overclaiming."""
    text = _flat()
    assert "## 6.2 Implemented production interlock (what actually runs)" in text
    assert "scripts/ops/production_interlock.py" in text
    for kind in ("OP-CODE", "OP-RECON", "OP-RESTORE", "OP-STATUS", "OP-PREFLIGHT"):
        assert kind in text
    assert "BYPASS_ATTEMPT" in text
    assert "no environment escape hatch" in text
    assert "not a permission" in text


def test_interlock_wiring_is_documented_for_every_entry_point():
    text = _flat()
    for entry in ("`sync.sh`", "scripts/sync/sync_prod.sh", "scripts/db/sync_prod.py",
                  "scripts/ops/deploy_release.sh", "scripts/ops/deploy_code.sh",
                  "scripts/ops/reload_gunicorn.sh"):
        assert entry in text, f"entry point not documented: {entry}"
    assert "--status` and `--reconcile-dry-run` remain usable" in text


def test_checklist_does_not_claim_interlock_makes_production_ready():
    text = _flat()
    assert "Still NOT claimed by this control" in text
    for claim in ("G7, general immutable exclusive evidence",
                  "fresh backup/restore proof",
                  "release readiness remain open"):
        assert claim in text, f"missing non-claim: {claim}"


def test_checklist_does_not_claim_validator_enforces_anything():
    text = _flat()
    assert "**not** a safety mechanism by itself" in text
    assert "documentation integrity check only" in text


# ── real CLI/parser validation (offline, non-mutating) ───────────────────


def _add_argument_options(path: Path) -> set[str]:
    """Collect argparse option strings by parsing the source (no execution)."""
    tree = ast.parse(path.read_text())
    opts: set[str] = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    if arg.value.startswith("-"):
                        opts.add(arg.value)
    return opts


def test_sync_prod_parser_really_has_no_plan_only():
    """Validate the ACTUAL parser rather than trusting a keyword assertion."""
    script = _ROOT / "scripts" / "db" / "sync_prod.py"
    assert script.exists(), f"missing {script}"
    opts = _add_argument_options(script)
    assert "--plan-only" not in opts, "--plan-only unexpectedly exists in the parser"
    # Re-verified 2026-09-23: "--tables" was added DELIBERATELY so sync_prod can
    # declare exactly the write set it touches — the interlock scope must never
    # understate what the run writes. The guard below is what forces this re-check.
    assert opts == {
        "--schema-only", "--bootstrap-schema", "--status",
        "--reconcile", "--reconcile-only", "--reconcile-dry-run",
        "--tables", "--authorization-id",
    }, f"parser options changed; checklist must be re-verified: {sorted(opts)}"


def test_verify_morning_sync_readiness_does_not_query_production():
    """Documents WHY it was removed from G5: it is local-artifact-only."""
    script = _ROOT / "scripts" / "ops" / "verify_morning_sync_readiness.py"
    if not script.exists():
        pytest.skip("readiness script absent")
    src = script.read_text()
    for token in ("psycopg", "connect(", "requests", "urlopen", "PROD_DATABASE_URL"):
        assert token not in src, (
            f"{token!r} present — the script may query production, so the "
            "checklist's removal rationale needs re-checking"
        )


def test_manifest_builder_lacks_exclusive_creation_documents_blocker_b3():
    """Pins B3 as REAL: the builder overwrites, it does not create exclusively."""
    src = (_ROOT / "scripts" / "ops" / "build_release_manifest.py").read_text()
    assert "O_EXCL" not in src, (
        "exclusive creation now exists — blocker B3 and G3 should be updated"
    )
    assert "write_text" in src, (
        "manifest builder no longer uses write_text — re-verify the immutability claim"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))
