#!/usr/bin/env python3
"""``stage1_packet_components.py`` — immutable, reviewed component bindings.

Every artifact the Stage 1 packet may bind is pinned here by **exact path and
SHA-256**.  There is no lexical or "newest file" discovery: an artifact that is
not explicitly bound is refused, and a bound artifact whose bytes do not match its
pinned hash is refused.  That removes the class of defect where a rebuild or a
rename silently changed what the packet would execute.

Scope is derived from the adjudication artifact rather than restated:

* each body's meeting scope and extraction ids come from the adjudication groups,
* the four extraction-less ``phoenix-dab`` meetings are therefore outside scope by
  construction (they carry no blocker),
* the 374 repair ids and the 18 quarantine ids are proved disjoint and complete.

Nothing here writes to a database.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

_REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = _REPO_ROOT / "data"

__all__ = [
    "ADJUDICATION_ARTIFACT",
    "DATA_DIR",
    "PLAN_BINDINGS",
    "QUARANTINE_ARTIFACT",
    "QUARANTINE_ATTRIBUTES",
    "SCHEMA_COMPONENT",
    "body_scope",
    "AUDIT_TIMESTAMP_COLUMNS",
    "AUDIT_TIMESTAMP_EXPRESSION",
    "audit_timestamp",
    "canonical_expectation",
    "canonical_row_fingerprint",
    "load_artifact",
    "meeting_scope",
    "quarantine_extraction_ids",
    "repair_extraction_ids",
    "scope_proof",
    "declares_unapplied",
    "sha256_file",
    "verify_components",
]

#: The adjudication artifact that defines every body's blocker scope.
ADJUDICATION_ARTIFACT: Mapping[str, str] = {
    "path": "data/kg-stage1-392-blocker-adjudication-20260911T175134Z.json",
    "sha256": "17bd7998ee5033d2298ce26b2aba8488d29f85d2a8081d3f894a53935b81f87f",
}

#: The reviewed repair plans, pinned by path and hash.
PLAN_BINDINGS: Mapping[str, Mapping[str, str]] = {
    "phoenix-dr": {
        "path": "data/kg-stage1-step2-phoenix-design-review-committee-repair-plan-20260911T220712Z.json",
        "sha256": "57a68eeea27112e6433edf3a8cdb5af21208880fead57b23fafa00ede3cebbf1",
        "plan_digest": "f01b2cb9d0e383f23409ffa28539a32270dac90bb9d5831dd3ec7ee31cdb5c26",
    },
    "phoenix-dab": {
        "path": "data/kg-stage1-step2-phoenix-development-advisory-board-repair-plan-20260911T220712Z.json",
        "sha256": "decd1b5fadaa2fc978adfd1dd991a83e69403481a859022da2669757dc8fe4a8",
        "plan_digest": "d4c05e3a3d1da34a2d8d4fc9fc2f79f6967e9eaa1b799c086d6a4d3bd75fd183",
    },
    "phoenix-ds": {
        "path": "data/kg-stage1-step2-phoenix-design-standards-committee-repair-plan-20260911T220712Z.json",
        "sha256": "babef8cb5c7153a015d200425b53ffcee738d6cf4dabd3cd4da6e6f3a826444b",
        "plan_digest": "951f6771f9360997683eec44ac3a60019f4064733b7377fd22c5d995505cb9ab",
    },
}

#: The reviewed quarantine plan (targets, reasons scope, evidence).
QUARANTINE_ARTIFACT: Mapping[str, str] = {
    "path": "data/kg-stage1-quarantine-plan-skip-meeting-15841-20260911T184202Z.json",
    "sha256": "92cc4ebcb4e6900dcbed00be490c46711d13089f887a877bac675963f02ef2d9",
}

#: The quarantine DDL component: a code module, not a plan artifact.
SCHEMA_COMPONENT: Mapping[str, Any] = {
    "id": "schema.quarantine_columns",
    "order": 1,
    "kind": "schema",
    "module": "scripts/db/quarantine_schema.py",
    "semantics_module": "scripts/kg/quarantine.py",
    "scope": "ALTER TABLE meeting_event_extractions (5 nullable columns + 1 index)",
    "rowcount": 0,
    "depends_on": (),
}

#: Columns a quarantine writes; the three human fields are approval placeholders.
QUARANTINE_ATTRIBUTES = (
    "quarantine_reason",
    "quarantined_at",
    "quarantined_by",
    "decision_id",
    "model_version",
)

#: The human fields that must be supplied by an adjudicator before any apply.
HUMAN_REQUIRED_FIELDS = ("quarantined_by", "decision_id", "quarantined_at")

#: ``__skip__`` is the scraper sentinel group; its rows are quarantined, not repaired.
QUARANTINE_BODY_CODE = "__skip__"


_ROW_FINGERPRINT_FIELDS = (
    "id", "meeting_event_id", "supporting_doc_id", "extractor", "extractor_version",
    "action_verb", "confidence", "text_offset_start", "text_offset_end",
    "case_number", "created_at",
)


def canonical_row_fingerprint(row: Mapping[str, Any]) -> str:
    """Deterministic fingerprint of one ``meeting_event_extractions`` row."""
    material = {field: row.get(field) for field in _ROW_FINGERPRINT_FIELDS}
    material["raw_text_sha256"] = hashlib.sha256(
        (row.get("raw_text") or "").encode("utf-8")
    ).hexdigest()
    encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def declares_unapplied(document: Mapping[str, Any]) -> bool:
    """Whether an artifact declares that it has not been applied.

    Two reviewed forms are accepted: an explicit ``not_applied.applied is False``,
    or an ``authorization`` block that requires separate explicit approval.  An
    artifact that claims to have been applied is always refused.
    """
    if document.get("applied") is True:
        return False
    not_applied = document.get("not_applied")
    if isinstance(not_applied, Mapping):
        if not_applied.get("applied") is True:
            return False
        if not_applied.get("applied") is False:
            return True
    if document.get("applied") is False:
        return True
    authorization = document.get("authorization") or {}
    return authorization.get("apply_requires_separate_explicit_approval") is True


def sha256_file(path: Path) -> str:
    """SHA-256 of a file's bytes."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_artifact(binding: Mapping[str, str]) -> tuple[dict[str, Any] | None, list[str]]:
    """Load a pinned artifact, refusing anything whose bytes have drifted."""
    path = _REPO_ROOT / binding["path"]
    problems: list[str] = []
    if not path.is_file():
        return None, [f"missing artifact {binding['path']}"]
    actual = sha256_file(path)
    if actual != binding["sha256"]:
        problems.append(
            f"{binding['path']}: sha256 {actual} != pinned {binding['sha256']}"
        )
        return None, problems
    return json.loads(path.read_text(encoding="utf-8")), problems


def _adjudication() -> dict[str, Any]:
    document, problems = load_artifact(ADJUDICATION_ARTIFACT)
    if problems:
        raise ValueError(f"adjudication artifact refused: {problems}")
    assert document is not None
    return document


def _group(body_code: str) -> Mapping[str, Any]:
    document = _adjudication()
    for group in document.get("groups") or ():
        if group.get("body_code") == body_code:
            return group
    raise ValueError(f"no adjudication group for body {body_code!r}")


def body_scope(body_code: str) -> dict[str, Any]:
    """The adjudicated blocker scope for one body: meetings and extractions."""
    group = _group(body_code)
    return {
        "body_code": body_code,
        "classification": group.get("classification"),
        "meeting_ids": sorted(int(value) for value in group.get("meeting_ids") or ()),
        "extraction_ids": sorted(int(value) for value in group.get("extraction_ids") or ()),
        "rationale": group.get("rationale"),
        "recommended_action": group.get("recommended_action"),
    }


def repair_extraction_ids() -> tuple[int, ...]:
    """The exact 374 repair extraction ids, across the three repairable bodies."""
    ids: set[int] = set()
    for body_code in PLAN_BINDINGS:
        ids.update(body_scope(body_code)["extraction_ids"])
    return tuple(sorted(ids))


def quarantine_extraction_ids() -> tuple[int, ...]:
    """The exact 18 quarantine extraction ids."""
    return tuple(sorted(body_scope(QUARANTINE_BODY_CODE)["extraction_ids"]))


def meeting_scope() -> dict[str, tuple[int, ...]]:
    """Each repairable body's reviewed meeting scope."""
    return {
        body_code: tuple(body_scope(body_code)["meeting_ids"])
        for body_code in PLAN_BINDINGS
    }


def scope_proof(expected_repair: int = 374, expected_quarantine: int = 18) -> dict[str, Any]:
    """Prove the repair and quarantine populations are exact, disjoint and complete."""
    repair = set(repair_extraction_ids())
    quarantine = set(quarantine_extraction_ids())
    meetings = meeting_scope()
    per_body = {
        body_code: {
            "meetings": len(scope),
            "extractions": len(body_scope(body_code)["extraction_ids"]),
        }
        for body_code, scope in meetings.items()
    }
    return {
        "repair_ids": sorted(repair),
        "quarantine_ids": sorted(quarantine),
        "repair_count": len(repair),
        "quarantine_count": len(quarantine),
        "total": len(repair) + len(quarantine),
        "overlap": sorted(repair & quarantine),
        "disjoint": not (repair & quarantine),
        "repair_ids_exact": len(repair) == expected_repair,
        "quarantine_ids_exact": len(quarantine) == expected_quarantine,
        "complete": len(repair) + len(quarantine) == expected_repair + expected_quarantine,
        "meetings_by_body": per_body,
        "meeting_total": sum(len(scope) for scope in meetings.values()),
        "meeting_total_exact": sum(len(scope) for scope in meetings.values()) == 55,
    }


#: The audit columns every ``public_bodies`` insert must populate.
#:
#: Verified read-only against the live schema: ``created_at`` and ``updated_at`` are
#: ``NOT NULL`` with no server default, and the ORM supplies them Python-side
#: (``scripts/db/models.py:444-445``).  A bulk ``text()`` INSERT must therefore
#: provide them explicitly; omitting them aborts the whole transaction.
AUDIT_TIMESTAMP_COLUMNS = ("created_at", "updated_at")

#: The single reviewed SQL expression used to populate those columns.
#:
#: ``now()`` is PostgreSQL's *transaction-start* timestamp: every statement in the
#: apply transaction observes the same value, so the three body inserts record one
#: consistent, machine-observed creation/update time.  It is deliberately not
#: ``clock_timestamp()`` (which varies per statement) and not a hardcoded historical
#: date, which would fabricate a creation time that was never observed.
AUDIT_TIMESTAMP_EXPRESSION = "now()"

AUDIT_TIMESTAMP_SEMANTICS = (
    "PostgreSQL transaction-start time (now()); identical for every statement in the "
    "apply transaction, so the three body inserts share one consistent machine-observed "
    "creation/update time.  No historical creation time is invented."
)


def audit_timestamp() -> dict[str, Any]:
    """The reviewed audit-timestamp contract for ``public_bodies`` inserts."""
    return {
        "expression": AUDIT_TIMESTAMP_EXPRESSION,
        "columns": list(AUDIT_TIMESTAMP_COLUMNS),
        "semantics": AUDIT_TIMESTAMP_SEMANTICS,
    }


def canonical_expectation() -> dict[str, Any]:
    """The canonical expectation a packet must match, reconstructed from sources.

    This is deliberately *not* derived from any caller-supplied packet: the runner
    compares a supplied packet against this, so a forged manifest that is internally
    consistent (and even correctly re-hashed) is still refused.
    """
    from scripts.kg.stage1_adjudication import ADJUDICATION, canonical_body_names

    return {
        "adjudication_artifact": dict(ADJUDICATION_ARTIFACT),
        "quarantine_artifact": dict(QUARANTINE_ARTIFACT),
        "component_paths": {body: dict(binding) for body, binding in PLAN_BINDINGS.items()},
        "repair_ids": list(repair_extraction_ids()),
        "quarantine_ids": list(quarantine_extraction_ids()),
        "meeting_scope": {body: list(ids) for body, ids in meeting_scope().items()},
        "canonical_body_names": canonical_body_names(),
        "quarantine_reason": ADJUDICATION["quarantine_reason"],
        "audit_timestamp": audit_timestamp(),
        "human_fields": {
            "quarantined_by": ADJUDICATION["adjudicator"],
            "decision_id": ADJUDICATION["decision_id"],
            "quarantined_at": ADJUDICATION["decided_at"],
        },
        "code_fingerprint": {
            "quarantine_schema_sha256": sha256_file(_REPO_ROOT / str(SCHEMA_COMPONENT["module"])),
            "quarantine_semantics_sha256": sha256_file(
                _REPO_ROOT / str(SCHEMA_COMPONENT["semantics_module"])),
        },
        "expected_state": {
            "public_bodies_total": len(PLAN_BINDINGS),
            "meeting_updates": sum(len(ids) for ids in meeting_scope().values()),
            "quarantine_updates": len(quarantine_extraction_ids()),
        },
    }


def verify_components() -> list[dict[str, Any]]:
    """Verify every bound component.  Unbound artifacts cannot appear here."""
    verified: list[dict[str, Any]] = []
    schema = dict(SCHEMA_COMPONENT)
    schema["digest_ok"] = True
    schema["module_sha256"] = sha256_file(_REPO_ROOT / str(schema["module"]))
    schema["semantics_module_sha256"] = sha256_file(
        _REPO_ROOT / str(schema["semantics_module"])
    )
    schema["quarantine_schema_sha256"] = schema["module_sha256"]
    schema["quarantine_semantics_sha256"] = schema["semantics_module_sha256"]
    verified.append(schema)

    for body_code, binding in PLAN_BINDINGS.items():
        document, problems = load_artifact(binding)
        digest_ok = True
        plan_digest_value = None
        if document is not None:
            from scripts.kg.phoenix_dr_plan_body import plan_digest

            plan_digest_value = plan_digest(document)
            if plan_digest_value != binding["plan_digest"]:
                problems = problems + [
                    f"plan digest {plan_digest_value} != pinned {binding['plan_digest']}"
                ]
            if not declares_unapplied(document):
                problems = problems + ["artifact does not declare itself unapplied"]
        digest_ok = not problems
        scope = body_scope(body_code)
        verified.append({
            "id": body_code,
            "order": 2,
            "kind": "data",
            "path": binding["path"],
            "sha256": binding["sha256"],
            "plan_digest": binding["plan_digest"],
            "recomputed_plan_digest": plan_digest_value,
            "artifact_present": document is not None,
            "digest_ok": digest_ok,
            "problems": problems,
            "table_scope": "public_bodies + meetings",
            "meetings": len(scope["meeting_ids"]),
            "extractions": len(scope["extraction_ids"]),
            "meeting_ids": scope["meeting_ids"],
            "extraction_ids": scope["extraction_ids"],
        })

    quarantine, problems = load_artifact(QUARANTINE_ARTIFACT)
    quarantine_ok = not problems
    if quarantine is not None:
        if not declares_unapplied(quarantine):
            problems = problems + ["quarantine artifact does not declare itself unapplied"]
        if quarantine.get("non_destructive") is not True:
            problems = problems + ["quarantine artifact is not declared non-destructive"]
        if tuple(sorted(int(v) for v in (quarantine.get("evidence") or {}).get("extraction_ids") or ())
                 ) != quarantine_extraction_ids():
            problems = problems + ["quarantine artifact ids disagree with the adjudication"]
        quarantine_ok = not problems
    verified.append({
        "id": "data.quarantine_skip_18",
        "order": 3,
        "kind": "data",
        "path": QUARANTINE_ARTIFACT["path"],
        "sha256": QUARANTINE_ARTIFACT["sha256"],
        "artifact_present": quarantine is not None,
        "digest_ok": quarantine_ok,
        "problems": problems,
        "table_scope": "meeting_event_extractions",
        "meetings": 1,
        "extractions": 18,
        "meeting_ids": sorted(body_scope(QUARANTINE_BODY_CODE)["meeting_ids"]),
        "extraction_ids": list(quarantine_extraction_ids()),
        "depends_on": ("schema.quarantine_columns",),
    })
    return verified
