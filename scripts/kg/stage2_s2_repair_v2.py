#!/usr/bin/env python3
"""``stage2_s2_repair_v2.py`` — the v2 repair contract: v1 plus a schema signature.

V1 built the repair plan without binding the schema it was built against, so a schema
move could not be detected.  V2 adds that binding WITHOUT touching a single byte of any
module the already-applied correction artifact has bound:

* the v2 contract lives in this new module and in
  :mod:`stage2_s2_schema_signature`, neither of which exists in v1;
* v2 binds ``plan_binding.CODE_MODULES`` PLUS those two modules, so the v1 generator
  list is imported, never edited;
* the v1 validator is preserved and simply extended, so v1 plans remain verifiable
  exactly as they were.

The applied v1 correction artifact stays byte-identical and hash-valid under v1.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402
from scripts.kg import stage2_s2_plan_binding as binding  # noqa: E402
from scripts.kg import stage2_s2_repair_plan as v1  # noqa: E402
from scripts.kg import stage2_s2_row_derivation as row_derivation  # noqa: E402
from scripts.kg import stage2_s2_schema_signature as schema  # noqa: E402

__all__ = ["CODE_MODULES_V2", "PLAN_VERSION_V2", "build_plan_v2", "code_hashes_v2",
           "validate_plan_v2"]

PLAN_VERSION_V2 = "kg-stage2-s2-repair-plan/2.0"

#: The v2 bound set: the v1 generator list PLUS the two new modules.  The v1 list is
#: imported, never modified, so no v1-bound byte changes.
CODE_MODULES_V2 = tuple(binding.CODE_MODULES) + (
    "scripts/kg/stage2_s2_schema_signature.py",
    "scripts/kg/stage2_s2_repair_v2.py",
    # the v2 admission boundary: typed receipt roles for the current apply plan
    "scripts/kg/stage2_s2_admission_v2.py",
)


def code_hashes_v2() -> dict[str, str]:
    import hashlib

    out: dict[str, str] = {}
    for relative in CODE_MODULES_V2:
        path = REPO / relative
        if path.exists():
            out[relative] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def _complete_rows(document: dict[str, Any], connection: Any) -> list[str]:
    """Rewrite every materialisation with the authoritative, evidenced full row.

    The v1 repair builder inherited a legacy 8-field ``proposed_row`` that named a column
    the live table does not have and omitted twelve NOT NULL fields.  Routing it through
    :mod:`stage2_s2_row_derivation` - the same contract the applied correction uses - makes
    the row exactly the live schema.  Lives here, in a module v1 never bound, so no byte of
    the applied correction artifact changes.
    """
    rows = document.get("rows") or []
    wanted = [int(r["meeting_db_id"]) for r in rows if r.get("action") == "materialise"]
    evidence = row_derivation.meeting_evidence(connection, wanted)
    problems: list[str] = []
    for row in rows:
        if row.get("action") != "materialise":
            continue
        proposed = dict(row.get("proposed_row") or {})
        meeting = evidence.get(int(row["meeting_db_id"]))
        if meeting is None:
            problems.append(f"{row['meeting_db_id']}: no meeting evidence")
            continue
        complete = row_derivation.derive_row(
            meeting, number=str(proposed["agenda_item_number"]),
            title=str(proposed.get("agenda_item_title") or ""),
            text=str(proposed.get("agenda_item_text") or ""),
            section_level=int(proposed.get("section_level") or 0),
            sort_order=int(proposed.get("sort_order") or 0))
        bad = row_derivation.validate_row(complete)
        if bad:
            problems.append(f"{row['meeting_db_id']}|{proposed['agenda_item_number']}: "
                            f"{'; '.join(bad[:2])}")
            continue
        proposed = complete
        row["proposed_row"] = proposed
        row["row_fingerprint"] = binding.canonical_sha256(proposed)
        row["derivation"] = {
            "contract": "stage2_s2_row_derivation",
            "rules": dict(row_derivation.DERIVATION_RULES),
            "meeting_row_fingerprint": binding.canonical_sha256(meeting),
            "source_document_ids": sorted(int(d)
                                          for d in (row.get("resolves_documents") or [])),
            "witness_content_sha256": (row.get("witness") or {}).get("content_sha256"),
            "witness_document_row_fingerprint":
                (row.get("witness") or {}).get("document_row_fingerprint"),
            "row_fingerprint": row["row_fingerprint"],
        }
    return problems


def build_plan_v2(plan: Mapping[str, Any], *, connection: Any,
                  backup_path: Any = None) -> dict[str, Any]:
    """Take a v1 repair plan and emit the v2 plan: same substance, schema bound."""
    document = copy.deepcopy(dict(plan))
    problems = _complete_rows(document, connection)
    if problems:
        raise ValueError("repair rows are not evidenced: " + "; ".join(problems[:5]))
    document["version"] = PLAN_VERSION_V2
    document["contract"] = {"version": "v2", "supersedes": "v1",
                            "adds": "canonical schema-signature binding"}
    bindings = dict(document.get("bindings") or {})
    bindings["parity"] = bindings.get("parity") or {}
    bindings["code_hashes"] = code_hashes_v2()
    bindings["schema_signature"] = schema.signature(connection)
    if backup_path is not None:
        # Re-bind the protected backup to the receipt that actually carries the resolved
        # target port; the generator omitted it, so the inherited binding could never
        # satisfy the cross-target comparison.
        import json as _json
        from pathlib import Path as _Path
        receipt = _json.loads(_Path(backup_path).read_text())
        bindings["backup"] = binding.backup_binding(
            backup_path, receipt=receipt,
            plan_baseline_counts=dict(receipt.get("counts") or {}))
    document["bindings"] = bindings
    document.pop(artifacts.DIGEST_FIELD, None)
    document.pop("replay_digest", None)
    document["replay_digest"] = v1.replay_digest(document)
    document[artifacts.DIGEST_FIELD] = artifacts.compute_digest(document)
    problems = validate_plan_v2(document, connection=connection)
    if problems:
        raise ValueError("; ".join(problems[:5]))
    return document


def validate_plan_v2(plan: Mapping[str, Any], *, connection: Any | None = None
                     ) -> list[str]:
    """The v1 validator, extended — separately, so v1 itself is untouched."""
    # The v1 validator pins the v1 version string; v2 supersedes that one check and
    # keeps every other v1 rule verbatim.
    problems = [p for p in v1.validate_plan(plan)
                if not p.startswith("version must be")]
    if plan.get("version") != PLAN_VERSION_V2:
        problems.append(f"version must be {PLAN_VERSION_V2!r}")
    recorded = (plan.get("bindings") or {}).get("code_hashes") or {}
    for required in CODE_MODULES_V2:
        if required not in recorded:
            problems.append(f"v2 code hashes do not cover {required}")
    live = code_hashes_v2()
    drifted = sorted(r for r, d in recorded.items() if live.get(r) != d)
    if drifted:
        problems.append(f"v2 code has drifted for {drifted}")
    bound = (plan.get("bindings") or {}).get("schema_signature") or {}
    if not bound.get("digest"):
        problems.append("the v2 plan binds no schema signature")
    elif connection is not None:
        problems.extend(schema.verify(connection, bound))
    if plan.get(artifacts.DIGEST_FIELD) != artifacts.compute_digest(plan):
        problems.append("the recorded digest is not the artifact's canonical digest")
    return problems
