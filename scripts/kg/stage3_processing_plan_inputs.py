#!/usr/bin/env python3
"""Immutable inputs an authoritative Stage 3 dry plan binds, and their verification.

A plan may not assert its own proof.  Everything it relies on is bound to a file on
disk and re-derived here:

* the two authoritative evidence artifacts (source eligibility, processing identity);
* the **selected snapshot** - a write-once artifact carrying the selected snapshot
  identity digest *and the raw classification inputs of every selected row*: source
  id, retained-text digest, extraction method, extraction and scrape instants, and
  whether a document URL and a legacy ``swept_at`` were present.  Eligibility is
  therefore **re-classified from bound source fields**, never read from an asserted
  disposition, so a re-signed ``held`` -> ``planned`` rewrite cannot survive;
* the **receipt set** - a digest-bound artifact, or an explicit ``none``.  With no
  bound receipt artifact any replay, failure, or other processing-proof claim is
  refused, so replay can never be self-asserted offline.

``validate_plan`` re-derives every record's eligibility outcome, acquisition
provenance, and receipt state from those bound inputs.  Two fail-closed rules matter
especially: eligibility is classified from the bound source fields including the
classifier's own ``text_present`` whitespace semantics, and a bound receipt set that
contains an unkeyable invalid receipt refuses every processing claim rather than
merely counting it.  This module is offline: it reads bound artifacts and never
contacts a database.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.kg import stage3_processing_receipt as receipt
from scripts.kg import stage3_source_eligibility as eligibility
from scripts.kg.stage2_artifacts import is_obsolete, load_verified

REPO = Path(__file__).resolve().parents[2]

SELECTION_KIND = "kg-stage3-processing-selection-snapshot"
SELECTION_VERSION = "kg-stage3-processing-selection/3.0"
RECEIPT_SET_KIND = "kg-stage3-processing-receipt-set"
EVIDENCE_COMPONENTS = {
    "source_eligibility": "kg-stage3-source-eligibility-baseline",
    "processing_identity": "kg-stage3-processing-identity-baseline",
}
SELECTION_BINDING_KEYS = ("path", "digest", "kind", "target", "identity_sha256",
                          "selected", "population", "order")
RECEIPT_BINDING_KEYS = ("source", "count", "path", "digest", "sha256")
#: The bound source fields the canonical eligibility classifier reads.  ``text_present``
#: is the classifier's own whitespace semantics - ``bool(str(text_content or '').strip())`` -
#: bound explicitly, because a digest of raw text cannot distinguish real text from
#: whitespace-only text.
CLASSIFICATION_FIELDS = ("source_id", "content_sha256", "text_present", "extraction_method",
                         "text_extracted_at", "scraped_at", "has_document_url",
                         "legacy_swept_at_present")
EMPTY_TEXT_SHA256 = receipt.sha256_text("")
BOUND_TEXT_PLACEHOLDER = "<bound non-empty retained text>"


def canonical_sha256(payload: Any) -> str:
    return receipt.canonical_sha256(payload)


def resolve_path(value: Any, repo: Path = REPO) -> Path:
    path = Path(str(value))
    return path if path.is_absolute() else repo / path


def selection_identity_sha256(identities: Sequence[Sequence[Any]]) -> str:
    """The canonical digest over an ordered list of processing identities."""
    return canonical_sha256([[str(part) for part in identity] for identity in identities])


def eligibility_sha256(entries: Sequence[Mapping[str, Any]]) -> str:
    """The canonical digest over the ordered bound classification inputs."""
    return canonical_sha256([{field: entry.get(field) for field in CLASSIFICATION_FIELDS}
                             for entry in entries])


def _iso_or_none(value: Any) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return None
    return str(value)


def _parse_time(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def classification_entry(row: Mapping[str, Any]) -> dict[str, Any]:
    """The raw source fields one selected row contributes to the snapshot."""
    return {
        "source_id": int(row["id"]),
        "content_sha256": receipt.content_sha256(row),
        "text_present": bool(str(row.get("text_content") or "").strip()),
        "extraction_method": str(row.get("text_extraction_method") or ""),
        "text_extracted_at": _iso_or_none(row.get("text_extracted_at")),
        "scraped_at": _iso_or_none(row.get("scraped_at")),
        "has_document_url": bool(row.get("document_url")),
        "legacy_swept_at_present": row.get("swept_at") is not None,
    }


def classification_row(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild canonical classifier input from one bound snapshot entry.

    Presence is taken from the bound ``text_present`` flag - the classifier's own
    whitespace semantics - never inferred from the raw-text digest, which cannot tell
    real text from whitespace-only text.  A plausible non-empty placeholder stands in
    for present text because the classifier only asks whether the text is blank.  An
    entry without the flag is treated as text-absent, which can only add holds.
    """
    return {
        "id": int(entry.get("source_id")),
        "text_content": BOUND_TEXT_PLACEHOLDER if entry.get("text_present") else None,
        "text_extraction_method": str(entry.get("extraction_method") or ""),
        "text_extracted_at": _parse_time(entry.get("text_extracted_at")),
        "scraped_at": _parse_time(entry.get("scraped_at")),
        "document_url": "bound" if entry.get("has_document_url") else None,
        "swept_at": "bound" if entry.get("legacy_swept_at_present") else None,
    }


def build_selection_snapshot(*, created_at: str, target: Mapping[str, Any],
                             bound: Mapping[str, Any], entries: Sequence[Mapping[str, Any]],
                             evidence: Mapping[str, Any],
                             code_hashes: Mapping[str, str]) -> dict[str, Any]:
    """The write-once artifact binding exactly which selection was reconciled."""
    entries = [{field: entry.get(field) for field in CLASSIFICATION_FIELDS}
               for entry in entries]
    body = {
        "kind": SELECTION_KIND,
        "version": SELECTION_VERSION,
        "created_at": created_at,
        "mode": "read-only",
        "applied": False,
        "write_path": "absent by design",
        "target": dict(target),
        "bound": dict(bound),
        "selected": int(bound["selected"]),
        "population": int(bound["population"]),
        "classification_fields": list(CLASSIFICATION_FIELDS),
        "identity_sha256": selection_identity_sha256(
            [[receipt.SOURCE_KINDS[0], entry["source_id"], entry["content_sha256"],
              entry["extraction_method"], receipt.EXTRACTOR, receipt.EXTRACTOR_VERSION]
             for entry in entries]),
        "entries": entries,
        "entries_sha256": eligibility_sha256(entries),
        "evidence": {name: {"path": dict(item)["path"], "digest": dict(item)["digest"]}
                     for name, item in sorted(evidence.items())},
        "code_hashes": dict(code_hashes),
    }
    return {**body, "digest": canonical_sha256(body)}


def load_receipt_set(path: str | Path, repo: Path = REPO) -> tuple[list[Mapping[str, Any]],
                                                                  dict[str, Any]]:
    """Canonically load a receipt-set artifact; refuse anything that is not one."""
    resolved = resolve_path(path, repo)
    if not resolved.exists():
        raise ValueError(f"receipt set artifact is absent: {path}")
    if is_obsolete(resolved):
        raise ValueError(f"receipt set artifact is obsolete: {path}")
    document = load_verified(resolved)
    if document.get("kind") != RECEIPT_SET_KIND:
        raise ValueError(f"receipt set artifact kind is {document.get('kind')!r}")
    receipts = document.get("receipts")
    if not isinstance(receipts, Sequence) or isinstance(receipts, (str, bytes)):
        raise ValueError("receipt set artifact must carry a receipts sequence")
    if document.get("count") != len(receipts):
        raise ValueError("receipt set count does not match its receipts")
    return list(receipts), {
        "source": resolved.name, "path": str(path), "digest": document.get("digest"),
        "sha256": hashlib.sha256(resolved.read_bytes()).hexdigest(), "count": len(receipts),
    }


def _load_bound_artifact(binding: Any, expected_kind: str, plan_target: Any,
                         label: str, repo: Path) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    if not isinstance(binding, Mapping) or not binding.get("path"):
        return None, [f"{label} artifact binding has no path"]
    path = resolve_path(binding.get("path"), repo)
    if not path.exists():
        return None, [f"{label} artifact is absent: {binding.get('path')}"]
    if is_obsolete(path):
        problems.append(f"{label} artifact is obsolete: {binding.get('path')}")
    try:
        artifact = load_verified(path)
    except Exception as exc:  # noqa: BLE001 - any read failure is a refusal, not a crash
        return None, problems + [f"{label} artifact failed verification: {exc}"]
    if artifact.get("digest") != binding.get("digest"):
        problems.append(f"{label} artifact digest differs from the recorded binding")
    if artifact.get("kind") != expected_kind:
        problems.append(f"{label} artifact kind is not {expected_kind}")
    if dict(artifact.get("target") or {}) != dict(plan_target or {}):
        problems.append(f"{label} artifact target differs from the plan target")
    return artifact, problems


def evidence_problems(bindings: Any, plan_target: Any, repo: Path = REPO) -> list[str]:
    """The bound eligibility and processing-identity artifacts must be current."""
    if not isinstance(bindings, Mapping) or set(bindings) != set(EVIDENCE_COMPONENTS):
        return ["evidence binding does not cover exactly the required components"]
    problems: list[str] = []
    for component in sorted(EVIDENCE_COMPONENTS):
        binding = bindings[component]
        expected_kind = EVIDENCE_COMPONENTS[component]
        if not isinstance(binding, Mapping) or not binding.get("path"):
            problems.append(f"{component} evidence binding is missing")
            continue
        if binding.get("kind") != expected_kind:
            problems.append(f"{component} evidence kind is not {expected_kind}")
        if dict(binding.get("target") or {}) != dict(plan_target or {}):
            problems.append(f"{component} evidence target differs from the plan target")
        if not receipt._hex64(binding.get("digest")):
            problems.append(f"{component} evidence digest is not a sha256 hex string")
        _artifact, extra = _load_bound_artifact(binding, expected_kind, plan_target,
                                                component, repo)
        problems.extend(extra)
    return problems


def current_state_problems(plan: Mapping[str, Any], repo: Path = REPO) -> list[str]:
    """The recorded live current-state validation must match the bound artifacts."""
    problems: list[str] = []
    state = plan.get("current_state_validation")
    if not isinstance(state, Mapping):
        return ["current-state validation block is missing"]
    if state.get("verified") is not True:
        problems.append("current-state validation is not verified")
    if list(state.get("problems") or []):
        problems.append("current-state validation reported problems")
    bindings = plan.get("evidence_binding") or {}
    for component in EVIDENCE_COMPONENTS:
        recorded = state.get(component)
        binding = bindings.get(component) if isinstance(bindings, Mapping) else None
        if not isinstance(recorded, Mapping):
            problems.append(f"{component} current-state record is missing")
            continue
        if not isinstance(binding, Mapping):
            continue
        if recorded.get("artifact_digest") != binding.get("digest"):
            problems.append(f"{component} current-state record binds another artifact digest")
        path = resolve_path(binding.get("path"), repo)
        if not path.exists():
            continue
        try:
            artifact = load_verified(path)
        except Exception:  # noqa: BLE001 - already reported by the binding check
            continue
        for field in ("population_sha256", "accounting"):
            if recorded.get(field) != artifact.get(field):
                problems.append(f"{component} current-state {field} differs from the artifact")
    identity_state = state.get("processing_identity")
    if isinstance(identity_state, Mapping):
        account = identity_state.get("accounting")
        documents = account.get("documents") if isinstance(account, Mapping) else None
        if isinstance(documents, int) and documents != (plan.get("bound") or {}).get("population"):
            problems.append("plan population differs from the bound document population")
    return problems


def load_selection(plan: Mapping[str, Any], repo: Path = REPO
                   ) -> tuple[dict[str, Any] | None, list[dict[str, Any]] | None, list[str]]:
    """Load and verify the bound selection snapshot, returning its entries."""
    binding = plan.get("selection_binding")
    if not isinstance(binding, Mapping) or set(binding) != set(SELECTION_BINDING_KEYS):
        return None, None, ["selection binding is not canonical"]
    problems: list[str] = []
    artifact, extra = _load_bound_artifact(binding, SELECTION_KIND, plan.get("target"),
                                           "selection", repo)
    problems.extend(extra)
    records = [record for record in (plan.get("records") or []) if isinstance(record, Mapping)]
    recomputed = selection_identity_sha256(
        [record.get("processing_identity") or [] for record in records])
    if binding.get("identity_sha256") != recomputed:
        problems.append("selected snapshot identity digest does not match the plan records")
    if artifact is None:
        return None, None, problems
    entries = artifact.get("entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return artifact, None, problems + ["selection artifact carries no classification entries"]
    entries = [{field: entry.get(field) for field in CLASSIFICATION_FIELDS}
               for entry in entries if isinstance(entry, Mapping)]
    if len(entries) != len(artifact.get("entries") or []):
        problems.append("selection classification entries are malformed")
    unflagged = sum(1 for entry in entries
                    if not isinstance(entry.get("text_present"), bool))
    if unflagged:
        problems.append(f"selection entries carry no canonical text_present flag ({unflagged})")
    contradictions = sum(1 for entry in entries
                         if entry.get("text_present") is True
                         and entry.get("content_sha256") == EMPTY_TEXT_SHA256)
    if contradictions:
        problems.append("selection entries claim text the retained digest does not contain "
                        f"({contradictions})")
    if artifact.get("entries_sha256") != eligibility_sha256(entries):
        problems.append("selection classification entries do not match their digest")
    if artifact.get("classification_fields") is not None and \
            list(artifact.get("classification_fields") or []) != list(CLASSIFICATION_FIELDS):
        problems.append("selection classification fields are not the canonical set")
    if len(entries) != len(records):
        problems.append("selection entry count does not match the record count")
    for field in ("identity_sha256", "selected", "population"):
        if artifact.get(field) != binding.get(field):
            problems.append(f"selection {field} differs from the bound artifact")
    if binding.get("selected") != len(records):
        problems.append("selection selected count does not match the record count")
    if binding.get("order") != "source_id_asc":
        problems.append("selection order is not canonical")
    return artifact, entries, problems


def resolve_receipt_binding(binding: Any, repo: Path = REPO) -> tuple[dict[str, Any], list[str]]:
    """Load the bound receipt set (or the explicit empty set) and fold it."""
    if not isinstance(binding, Mapping):
        return receipt.fold_receipts([]), ["receipt binding is missing"]
    if binding.get("source") == "none":
        problems: list[str] = []
        if binding.get("count") != 0 or binding.get("path") or binding.get("digest"):
            problems.append("receipt binding claims a receipt set but binds no artifact")
        return receipt.fold_receipts([]), problems
    path = binding.get("path")
    if not isinstance(path, str) or not path:
        return receipt.fold_receipts([]), ["receipt binding has no path"]
    problems = []
    resolved = resolve_path(path, repo)
    if not resolved.exists():
        return receipt.fold_receipts([]), [f"receipt set artifact is absent: {path}"]
    if is_obsolete(resolved):
        problems.append(f"receipt set artifact is obsolete: {path}")
    try:
        receipts, derived = load_receipt_set(resolved, repo)
    except Exception as exc:  # noqa: BLE001 - any read failure is a refusal, not a crash
        return receipt.fold_receipts([]), problems + [
            f"receipt set artifact failed verification: {exc}"]
    for field in ("digest", "sha256", "count"):
        if binding.get(field) != derived[field]:
            problems.append(f"receipt set {field} differs from the bound artifact")
    unkeyable = [index for index, item in enumerate(receipts)
                 if receipt.validate_receipt(item) and receipt._safe_key(item) is None]
    if unkeyable:
        problems.append("bound receipt set contains an unkeyable invalid receipt; every "
                        "processing claim is refused until it is resolved")
    return receipt.fold_receipts(receipts), problems


def receipt_fold_problems(plan: Mapping[str, Any], folded: Mapping[str, Any]) -> list[str]:
    """The plan's receipt fold and stored-receipt counts must be the derived ones."""
    problems: list[str] = []
    recorded = plan.get("receipt_fold")
    if not isinstance(recorded, Mapping):
        return ["receipt fold block is missing"]
    derived = {
        "valid_receipts": folded["valid_receipts"], "identities": folded["identities"],
        "conflicts": len(folded["conflicts"]), "invalid": len(folded["invalid"]),
        "replayed": folded["replayed"], "superseded": folded["superseded"],
    }
    for key, value in sorted(derived.items()):
        if recorded.get(key) != value:
            problems.append(f"receipt fold {key} does not match the bound receipt set")
    accounting = plan.get("accounting")
    if not isinstance(accounting, Mapping):
        problems.append("accounting block is missing")
    elif accounting.get("stored_receipts") != dict(folded.get("by_status") or {}):
        problems.append("stored receipt counts do not match the bound receipt set")
    return problems


def _receipt_expectation(key: str, folded: Mapping[str, Any]) -> tuple | None:
    current = folded.get("current") or {}
    if key in set(folded.get("invalid_identities") or ()):
        return ("held", "receipt_invalid_fail_closed", receipt.STATE_INVALID, None)
    if key not in current:
        return None
    state = current[key]
    status = state.get("status")
    if status == "success":
        return ("replay", "exact_identity_success_receipt_present", "success",
                state.get("digest"))
    if status == "failed":
        return ("failure", str(state.get("reason") or "prior_processing_failed"), "failed",
                state.get("digest"))
    if status == receipt.STATE_CONFLICT:
        return ("held", "receipt_identity_conflict", receipt.STATE_CONFLICT, None)
    return ("held", "receipt_status_unregistered", status, None)


def rederive_record_problems(records: Sequence[Mapping[str, Any]],
                             entries: Sequence[Mapping[str, Any]] | None,
                             folded: Mapping[str, Any]) -> list[str]:
    """Re-derive each record from bound source fields; nothing is taken on trust."""
    if entries is None:
        return []
    problems: list[str] = []
    if len(entries) != len(records):
        return ["selection entries cannot re-derive a plan whose counts disagree"]
    for record, entry in zip(records, entries):
        identity = list(record.get("processing_identity") or [])
        where = f"source_id={record.get('source_id')}"
        if len(identity) != len(receipt.IDENTITY_FIELDS) \
                or str(entry.get("source_id")) != str(record.get("source_id")) \
                or str(entry.get("content_sha256")) != str(identity[2]) \
                or str(entry.get("extraction_method")) != str(identity[3]):
            problems.append(f"{where} does not describe the same identity as its bound "
                            f"selection entry")
            continue
        row = classification_row(entry)
        disposition, _reason = eligibility.classify_document(row)
        if disposition == "eligible_stale":
            expected = ("held", "source_stale_pending_text_refresh", None, None)
        elif disposition != "eligible_current":
            expected = ("held", f"not_eligible:{disposition}", None, None)
        else:
            expected = _receipt_expectation(receipt.identity_key(identity), folded) or \
                ("planned", "no_exact_identity_receipt_would_process", None, None)
        outcome, reason, status, digest = expected
        if record.get("outcome") != outcome:
            problems.append(f"{where} outcome {record.get('outcome')!r} contradicts the bound "
                            f"selection entry, which re-derives {outcome!r}")
        if record.get("reason") != reason:
            problems.append(f"{where} reason contradicts the bound selection entry")
        if record.get("receipt_status") != status:
            problems.append(f"{where} receipt status contradicts its bound receipt state")
        if record.get("receipt_digest") != digest:
            problems.append(f"{where} receipt digest is not the bound receipt's digest")
        if record.get("acquisition_provenance") != receipt.acquisition_class(row):
            problems.append(f"{where} acquisition provenance contradicts the bound entry")
        if bool(record.get("legacy_swept_at_present")) != bool(entry.get("legacy_swept_at_present")):
            problems.append(f"{where} legacy swept_at claim contradicts the bound entry")
    return problems
