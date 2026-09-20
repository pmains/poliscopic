#!/usr/bin/env python3
"""Version-aware KG processing receipt contract (Stage 3 blocker 1). Pure: no DB.

A receipt is the *only* admissible proof that one exact retained text version was
observed by one exact extractor version, and it is independent of acquisition
provenance: a recorded scraper or text-pipeline row proves how the bytes arrived,
never that ``sweep_docs`` processed *that* version.

States are named honestly: a receipt's ``status`` is ``success`` or ``failed``, and
a document is ``current_proven`` only when an exact canonical *stored* receipt for
its current identity says success.  A row with no such receipt has *not* been
processed: it is ``would_process`` (planned), never ``success``.

Identity authority is imported from :mod:`scripts.kg.stage3_processing_identity`,
never re-implemented; :func:`identity_authority_binding` binds that module's
digest at run time, so authority drift is a hard failure.

Fail-closed rules enforced by this module:

* the canonical digest is **mandatory**: a receipt without an exact lowercase
  64-hex digest equal to its own canonical body is ``invalid`` and can never
  become current proof;
* a receipt with the wrong kind/version/producer, an unsupported source kind,
  extractor or extractor version, an incomplete acquisition block, or a malformed
  identity is ``invalid`` and is never proof;
* two receipts for one identity recorded at the same instant with different
  content are a ``conflict``; a conflict is never reported as a success;
* an older receipt can never supersede a newer one, a stored receipt that is
  invalid or in unresolved conflict taints its identity, and a ``failed`` receipt
  stays ``failed``: nothing here converts it to success.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from scripts.kg import stage3_processing_identity as _authority
from scripts.kg.producer_versions import PRODUCER_VERSIONS

REPO = Path(__file__).resolve().parents[2]

#: The authoritative processing-identity implementation this contract imports.
IDENTITY_AUTHORITY_MODULE = "scripts/kg/stage3_processing_identity.py"
IDENTITY_AUTHORITY_FUNCTIONS = ("content_sha256", "evidence_identity",
                                "processing_identity", "acquisition_class")

# Imported, never re-implemented: one identity authority for the whole stage.
content_sha256 = _authority.content_sha256
evidence_identity = _authority.evidence_identity
processing_identity = _authority.processing_identity
acquisition_class = _authority.acquisition_class

PRODUCER_VERSION = "kg-stage3-processing-receipt/1.0"
RECEIPT_KIND = "kg-processing-receipt"
RECEIPT_VERSION = "kg-processing-receipt/1.0"
SUPPORTED_PRODUCER_VERSIONS = ("kg-stage3-processing-receipt/1.0",)
SOURCE_KINDS = ("supporting_document",)
EXTRACTOR = _authority.EXTRACTOR
EXTRACTOR_VERSION = _authority.EXTRACTOR_VERSION
SUPPORTED_EXTRACTOR_VERSIONS = {EXTRACTOR: EXTRACTOR_VERSION}
IDENTITY_FIELDS = (
    "source_kind",
    "source_id",
    "content_sha256",
    "extraction_method",
    "extractor",
    "extractor_version",
)
STATUSES = ("success", "failed")
STATE_CONFLICT = "conflict"
STATE_INVALID = "invalid"
ACQUISITION_CLASSES = (
    "recorded_scraper_acquisition",
    "recorded_text_pipeline",
    "provenance_unrecorded",
)


def canonical_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def identity_key(identity: Sequence[Any]) -> str:
    """Stable store key for an identity; the same identity always maps here."""
    return canonical_sha256([str(part) for part in identity])


def identity_authority_binding(repo: Path = REPO) -> dict[str, Any]:
    """Bind the imported identity authority's module hash for the current run."""
    return {
        "module": IDENTITY_AUTHORITY_MODULE,
        "sha256": hashlib.sha256((repo / IDENTITY_AUTHORITY_MODULE).read_bytes()).hexdigest(),
        "functions": list(IDENTITY_AUTHORITY_FUNCTIONS),
        "extractor_version_source": "scripts/kg/producer_versions.py",
        "declared_extractor_version": PRODUCER_VERSIONS[EXTRACTOR],
    }


def identity_authority_problems(binding: Any, repo: Path = REPO) -> list[str]:
    """Re-verify a recorded binding against the authority on disk, right now."""
    expected = identity_authority_binding(repo)
    if not isinstance(binding, Mapping):
        return ["identity authority binding is missing"]
    return [f"identity authority {field} drift"
            for field in sorted(expected) if binding.get(field) != expected[field]]


def _iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def build_receipt(row: Mapping[str, Any], *, status: str, reason: str,
                  recorded_at: str) -> dict[str, Any]:
    """Build one canonical receipt for ``row``; validity is decided by the validator."""
    identity = list(processing_identity(row))
    payload = {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "producer_version": PRODUCER_VERSION,
        "processing_identity": identity,
        "source_kind": identity[0],
        "source_id": identity[1],
        "content_sha256": identity[2],
        "extraction_method": identity[3],
        "extractor": identity[4],
        "extractor_version": identity[5],
        "status": status,
        "reason": reason,
        "recorded_at": recorded_at,
        "acquisition": {
            "class": acquisition_class(row),
            "legacy_swept_at_present": row.get("swept_at") is not None,
            "does_not_prove_processing": True,
        },
    }
    return {**payload, "digest": receipt_digest(payload)}


def receipt_digest(receipt: Mapping[str, Any]) -> str:
    return canonical_sha256({k: v for k, v in receipt.items() if k != "digest"})


def _hex64(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and \
        all(ch in "0123456789abcdef" for ch in value)


def validate_receipt(receipt: Any) -> list[str]:
    """Report every reason this receipt cannot be proof.  Empty means valid."""
    if not isinstance(receipt, Mapping):
        return ["receipt must be an object"]
    problems: list[str] = []
    if receipt.get("kind") != RECEIPT_KIND:
        problems.append("kind is not the canonical processing receipt kind")
    if receipt.get("version") != RECEIPT_VERSION:
        problems.append("version is not the canonical receipt version")
    if receipt.get("producer_version") not in SUPPORTED_PRODUCER_VERSIONS:
        problems.append("producer_version is not a supported producer version")

    identity = receipt.get("processing_identity")
    if not isinstance(identity, Sequence) or isinstance(identity, (str, bytes)):
        problems.append("processing_identity must be a sequence")
        identity = []
    identity = list(identity)
    if len(identity) != len(IDENTITY_FIELDS):
        problems.append("processing_identity must carry exactly six fields")
    elif (not isinstance(identity[0], str) or not identity[0].strip() or
          not isinstance(identity[1], int) or isinstance(identity[1], bool) or
          identity[1] <= 0 or not _hex64(identity[2]) or
          any(not isinstance(identity[index], str) or not identity[index].strip()
              for index in (3, 4, 5))):
        problems.append("processing_identity fields must use their canonical typed values")

    source_kind = str(receipt.get("source_kind") or "")
    if source_kind not in SOURCE_KINDS:
        problems.append("source_kind is not supported")
    if identity and str(identity[0] or "") != source_kind:
        problems.append("source_kind disagrees with processing_identity")
    source_id = receipt.get("source_id")
    if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id <= 0:
        problems.append("source_id must be a positive integer")
    elif identity and len(identity) > 1 and str(identity[1]) != str(source_id):
        problems.append("source_id disagrees with processing_identity")
    digest = receipt.get("content_sha256")
    if not _hex64(digest):
        problems.append("content_sha256 must be 64 lowercase hex characters")
    elif identity and len(identity) > 2 and str(identity[2]) != digest:
        problems.append("content_sha256 disagrees with processing_identity")

    for index, field in ((3, "extraction_method"), (4, "extractor"), (5, "extractor_version")):
        raw_value = receipt.get(field)
        value = str(raw_value or "")
        if not isinstance(raw_value, str) or not value.strip():
            problems.append(f"{field} must not be blank")
        elif len(identity) > index and str(identity[index]) != value:
            problems.append(f"{field} disagrees with processing_identity")
    extractor = str(receipt.get("extractor") or "")
    if extractor and extractor not in SUPPORTED_EXTRACTOR_VERSIONS:
        problems.append("extractor is not supported")
    elif extractor and str(receipt.get("extractor_version") or "") != \
            SUPPORTED_EXTRACTOR_VERSIONS[extractor]:
        problems.append("extractor_version is not the declared version for this extractor")

    acquisition = receipt.get("acquisition")
    if not isinstance(acquisition, Mapping):
        problems.append("acquisition block is required")
    else:
        if acquisition.get("class") not in ACQUISITION_CLASSES:
            problems.append("acquisition.class is not registered")
        if not isinstance(acquisition.get("legacy_swept_at_present"), bool):
            problems.append("acquisition.legacy_swept_at_present must be a boolean")
        if acquisition.get("does_not_prove_processing") is not True:
            problems.append("acquisition block must not claim to prove processing")

    if receipt.get("status") not in STATUSES:
        problems.append("status is not registered")
    if receipt.get("status") == "failed" and not str(receipt.get("reason") or "").strip():
        problems.append("a failed receipt requires a reason")
    if _iso(receipt.get("recorded_at")) is None:
        problems.append("recorded_at must be an ISO-8601 instant")

    recorded = receipt.get("digest")
    if not _hex64(recorded):
        problems.append("canonical digest is required and must be a lowercase sha256")
    elif recorded != receipt_digest(receipt):
        problems.append("canonical digest does not match the receipt body")
    return problems


def _safe_key(receipt: Any) -> str | None:
    identity = receipt.get("processing_identity") if isinstance(receipt, Mapping) else None
    if not isinstance(identity, Sequence) or isinstance(identity, (str, bytes)) or not identity:
        return None
    return identity_key(list(identity))


def _state(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "processing_identity": list(entry["processing_identity"]),
        "status": str(entry["status"]),
        "reason": str(entry.get("reason") or ""),
        "recorded_at": str(_iso(entry.get("recorded_at"))),
        "digest": receipt_digest(entry),
    }


def fold_receipts(receipts: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Fold a receipt history into one current, fail-closed state per identity."""
    groups: dict[str, list[tuple[str, str, Mapping[str, Any]]]] = {}
    invalid: list[dict[str, Any]] = []
    invalid_keys: set[str] = set()
    for index, receipt in enumerate(receipts or []):
        problems = validate_receipt(receipt)
        if problems:
            invalid.append({"index": index, "problems": problems})
            key = _safe_key(receipt)
            if key is not None:
                invalid_keys.add(key)
            continue
        key = identity_key(list(receipt["processing_identity"]))
        groups.setdefault(key, []).append(
            (str(_iso(receipt["recorded_at"])), receipt_digest(receipt), receipt))

    current: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    replayed = 0
    superseded = 0
    by_status = {name: 0 for name in STATUSES}
    for key, entries in sorted(groups.items()):
        distinct: dict[str, tuple[str, Mapping[str, Any]]] = {}
        for recorded_at, digest, receipt in entries:
            if digest in distinct:
                replayed += 1
                continue
            distinct[digest] = (recorded_at, receipt)
        ordered = sorted(distinct.items(), key=lambda item: (item[1][0], item[0]))
        latest_at = ordered[-1][1][0]
        tied = [item for item in ordered if item[1][0] == latest_at]
        if len(tied) > 1:
            conflicts.append({"identity_key": key, "recorded_at": latest_at,
                              "digests": sorted(item[0] for item in tied)})
            current[key] = {"processing_identity": list(ordered[-1][1][1]["processing_identity"]),
                            "status": STATE_CONFLICT, "reason": "receipt_identity_conflict",
                            "recorded_at": latest_at, "digest": None}
            continue
        superseded += len(ordered) - 1
        state = _state(ordered[-1][1][1])
        by_status[state["status"]] += 1
        current[key] = state
    return {
        "receipts": len(receipts or []),
        "valid_receipts": sum(len(entries) for entries in groups.values()) - replayed,
        "identities": len(groups),
        "current": current,
        "by_status": by_status,
        "conflicts": conflicts,
        "invalid": invalid,
        "invalid_identities": sorted(invalid_keys),
        "replayed": replayed,
        "superseded": superseded,
    }


TERMINAL_ACTIONS = ("append", "replay", "conflict", "refuse", "invalid")


def _arrival(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {"digest": receipt_digest(entry), "recorded_at": str(_iso(entry["recorded_at"])),
            "status": str(entry["status"])}


def merge_receipts(existing: Sequence[Mapping[str, Any]] | None,
                   incoming: Sequence[Mapping[str, Any]] | None) -> dict[str, Any]:
    """Decide the single deterministic terminal action per processing identity.

    Store semantics, decided and documented here:

    * the store is **append-only**: nothing here rewrites, relabels, or deletes a
      stored row, and an arrival older than the stored state is refused, so a late
      delivery can never rewrite history;
    * one incoming batch yields **exactly one terminal action per processing
      identity** - ``append``, ``replay``, ``conflict``, ``refuse``, or ``invalid`` -
      so a batch can never write twice for one identity;
    * the terminal winner is the newest arrival by ``recorded_at`` with a
      deterministic digest tie-break, so arrival order cannot change the outcome;
    * byte-identical arrivals are duplicates and a distinct earlier arrival is
      reported as ``superseded_in_batch``; neither is written.  A caller that wants
      a superseded arrival recorded submits it alone, where it is either newer than
      the stored state (appended) or refused as older;
    * equal-instant disagreement is a ``conflict`` and never an append;
    * stored history is folded **first**: an identity whose stored receipts are
      invalid, or whose stored receipt history is an unresolved same-instant
      conflict, refuses every new arrival - including a newer valid one - until the
      conflict is resolved by a governed decision;
    * a malformed arrival with a keyable identity **taints that identity for the
      whole batch**: valid and invalid arrivals for one identity yield a refuse (or
      ``invalid`` when nothing valid arrived) with zero appends, in any order.

    Every action carries the typed payload an eventual store needs: the identity,
    the terminal action and reason, the canonical receipt to append (``None`` for
    every non-append terminal), its digest, status, and recorded time, and the
    batch arrivals it consumed.
    """
    folded_stored = fold_receipts(existing)
    index: dict[str, dict[str, str]] = {}
    tainted_invalid: set[str] = set()
    unkeyable_stored_invalid = False
    for stored in existing or []:
        key = _safe_key(stored)
        if validate_receipt(stored):
            if key is None:
                unkeyable_stored_invalid = True
            else:
                tainted_invalid.add(key)
            continue
        index.setdefault(key, {})[receipt_digest(stored)] = str(_iso(stored["recorded_at"]))
    tainted_conflict = {key for key, state in (folded_stored.get("current") or {}).items()
                        if state.get("status") == STATE_CONFLICT}

    arrivals: dict[str, list[Mapping[str, Any]]] = {}
    invalid_keys: set[str] = set()
    invalid_arrivals = 0
    unkeyable_invalid_arrivals = 0
    for entry in incoming or []:
        if validate_receipt(entry):
            invalid_arrivals += 1
            key = _safe_key(entry)
            if key is None:
                unkeyable_invalid_arrivals += 1
            else:
                invalid_keys.add(key)
            continue
        arrivals.setdefault(identity_key(list(entry["processing_identity"])), []).append(entry)

    actions: list[dict[str, Any]] = []
    duplicates = 0
    superseded = 0
    for key in sorted(set(arrivals) | invalid_keys):
        entries = arrivals.get(key, [])
        action: dict[str, Any] = {
            "identity_key": key,
            "processing_identity": list(entries[0]["processing_identity"]) if entries else None,
            "terminal": None, "reason": None, "receipt": None, "digest": None,
            "recorded_at": None, "status": None, "superseded_in_batch": [],
            "duplicates": 0, "consumed": 0,
        }
        distinct: dict[str, Mapping[str, Any]] = {}
        for entry in entries:
            digest = receipt_digest(entry)
            if digest in distinct:
                action["duplicates"] += 1
                duplicates += 1
                continue
            distinct[digest] = entry
        ordered = sorted(distinct.items(),
                         key=lambda item: (str(_iso(item[1]["recorded_at"])), item[0]))
        if unkeyable_stored_invalid:
            action.update(terminal="refuse", reason="unkeyable_invalid_stored_receipt",
                          consumed=len(ordered))
        elif unkeyable_invalid_arrivals:
            # A batch with an unkeyable malformed receipt cannot establish that its
            # valid-looking subset is complete.  Refuse every keyed arrival rather
            # than silently appending a partial batch.
            action.update(terminal="refuse", reason="unkeyable_invalid_arrival_in_batch",
                          consumed=len(ordered))
        elif key in tainted_invalid:
            action.update(terminal="refuse", reason="stored_receipt_invalid_for_identity",
                          consumed=len(ordered))
        elif key in tainted_conflict:
            action.update(terminal="refuse",
                          reason="stored_receipt_identity_conflict_requires_resolution",
                          consumed=len(ordered))
        elif not ordered:
            action.update(terminal="invalid", reason="all_arrivals_for_identity_are_invalid")
        elif key in invalid_keys:
            action.update(terminal="refuse", reason="malformed_arrival_for_identity",
                          consumed=0)
            action["superseded_in_batch"] = [
                {**_arrival(entry), "reason": "superseded_by_malformed_arrival"}
                for _digest, entry in ordered]
        else:
            latest = str(_iso(ordered[-1][1]["recorded_at"]))
            tied = [(digest, entry) for digest, entry in ordered
                    if str(_iso(entry["recorded_at"])) == latest]
            if len(tied) > 1:
                action.update(terminal="conflict", recorded_at=latest, consumed=len(tied),
                              reason="conflicting_arrivals_at_the_same_instant")
                action["superseded_in_batch"] = [
                    {**_arrival(entry), "reason": "superseded_within_batch"}
                    for _digest, entry in ordered[:len(ordered) - len(tied)]]
            else:
                digest, winner = ordered[-1]
                action.update(digest=digest, recorded_at=latest, status=str(winner["status"]),
                              receipt=dict(winner), consumed=1)
                action["superseded_in_batch"] = [
                    {**_arrival(entry), "reason": "superseded_within_batch"}
                    for _digest, entry in ordered[:-1]]
                prior = index.get(key) or {}
                if digest in prior:
                    action.update(terminal="replay", reason="receipt_already_stored")
                elif not prior:
                    action.update(terminal="append", reason="new_identity_receipt")
                else:
                    stored_newest = max(prior.values())
                    if latest > stored_newest:
                        action.update(terminal="append", reason="newer_than_stored_receipt")
                    elif latest == stored_newest:
                        action.update(terminal="conflict",
                                      reason="arrival_disagrees_with_stored_at_same_instant")
                    else:
                        action.update(terminal="refuse",
                                      reason="incoming_receipt_is_older_than_stored")
        if action["terminal"] != "append":
            action["receipt"] = None
        superseded += len(action["superseded_in_batch"])
        actions.append(action)

    counts = {name: 0 for name in TERMINAL_ACTIONS}
    for action in actions:
        counts[str(action["terminal"])] += 1
    identity_arrivals = sum(len(entries) for entries in arrivals.values())
    per_identity = all(
        len(arrivals.get(action["identity_key"], []))
        == action["duplicates"] + len(action["superseded_in_batch"]) + action["consumed"]
        for action in actions)
    reconciles = (
        len(incoming or []) == invalid_arrivals + identity_arrivals
        and identity_arrivals == duplicates + superseded + sum(a["consumed"] for a in actions)
        and per_identity
        and len(actions) == len(set(arrivals) | invalid_keys))
    return {
        "actions": actions,
        "terminal": {action["identity_key"]: action["terminal"] for action in actions},
        "counts": counts,
        "writes": counts["append"],
        "writes_by_identity": counts["append"],
        "max_writes_per_identity": 1 if counts["append"] else 0,
        "appends": [action["receipt"] for action in actions if action["terminal"] == "append"],
        "duplicates": duplicates,
        "superseded_in_batch": superseded,
        "invalid_arrivals": invalid_arrivals,
        "unkeyable_invalid_arrivals": unkeyable_invalid_arrivals,
        "arrivals": len(incoming or []),
        "identities": len(actions),
        "reconciles": reconciles,
    }
