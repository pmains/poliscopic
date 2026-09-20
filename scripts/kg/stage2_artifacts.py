#!/usr/bin/env python3
"""``stage2_artifacts.py`` — digest-bound, write-once Stage 2 artifacts.

A Stage 2 plan is a *decision record*: it names the exact rows that will change
and the exact bodies they will point at.  If it can be edited after review, the
review meant nothing.  Two properties therefore hold for every artifact here:

* **Immutable.**  Files are created with ``O_CREAT | O_EXCL`` and mode ``0600``.
  Check-then-write is not safe, so creation is the atomic test: an existing path
  is refused, never overwritten.
* **Digest-bound.**  The artifact carries a SHA-256 over its own canonical body
  (all keys except ``digest``).  Reading re-derives the digest and refuses a
  mismatch, so silent tampering is a hard failure rather than a surprise later.

Nothing here contacts a database.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "ArtifactCollision",
    "ArtifactDigestMismatch",
    "DIGEST_FIELD",
    "LEGACY_DIGEST_FIELDS",
    "canonical_json",
    "compute_digest",
    "load_verified",
    "plan_filename",
    "receipt_filename",
    "recorded_digest",
    "OBSOLETE_SUFFIX",
    "is_obsolete",
    "record_obsolete",
    "write_immutable",
]

#: The single field excluded from its own digest, and the one every artifact
#: records.  Receipts historically also reported this value to stdout under a
#: different name; :func:`recorded_digest` reads both so older consumers keep
#: working while the written artifact keeps exactly one field.
DIGEST_FIELD = "digest"

#: Read-only aliases accepted by :func:`recorded_digest`; never written.
LEGACY_DIGEST_FIELDS = ("receipt_digest",)


class ArtifactCollision(RuntimeError):
    """The artifact path already exists; refusing to overwrite it."""


class ArtifactDigestMismatch(RuntimeError):
    """The artifact's content no longer matches its recorded digest."""


def canonical_json(payload: Any) -> str:
    """Serialise deterministically, so a digest is reproducible."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def compute_digest(payload: Mapping[str, Any]) -> str:
    """SHA-256 over the artifact body, excluding the digest field itself."""
    body = {k: v for k, v in payload.items() if k != DIGEST_FIELD}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def write_immutable(path: str | Path, payload: Mapping[str, Any]) -> str:
    """Create ``path`` atomically with ``payload``; return the file's digest.

    The recorded ``digest`` is always recomputed here rather than trusted, so a
    caller cannot write an artifact whose digest disagrees with its content.
    """
    document = dict(payload)
    digest = compute_digest(document)
    document[DIGEST_FIELD] = digest
    text = json.dumps(document, indent=2, sort_keys=True, default=str) + "\n"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    try:
        descriptor = os.open(str(path), flags, 0o600)
    except FileExistsError as exc:
        raise ArtifactCollision(f"artifact already exists: {path}") from exc
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    return digest


def recorded_digest(document: Mapping[str, Any]) -> str | None:
    """Return an artifact's own digest, tolerating the historical alias.

    Only ``digest`` is ever written; the alias exists so a consumer written
    against the older stdout field name does not break.  Reading never accepts
    an alias *in place of* a present canonical field.
    """
    if DIGEST_FIELD in document:
        return document[DIGEST_FIELD]
    for alias in LEGACY_DIGEST_FIELDS:
        if alias in document:
            return document[alias]
    return None


def load_verified(path: str | Path) -> dict[str, Any]:
    """Read an artifact and refuse it if its digest does not match."""
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ArtifactDigestMismatch(f"{path} is not an object artifact")
    recorded = document.get(DIGEST_FIELD)
    if not isinstance(recorded, str):
        raise ArtifactDigestMismatch(f"{path} carries no digest")
    actual = compute_digest(document)
    if actual != recorded:
        raise ArtifactDigestMismatch(
            f"{path} digest mismatch: recorded {recorded}, computed {actual}"
        )
    return document


def plan_filename(plan_id: str) -> str:
    """Canonical filename for a plan artifact."""
    return f"kg-stage2-s1-plan-{plan_id}.json"


def receipt_filename(plan_id: str) -> str:
    """Canonical filename for a terminal receipt artifact."""
    return f"kg-stage2-s1-receipt-{plan_id}.json"

#: Suffix of the immutable sidecar that marks an artifact obsolete for use.
OBSOLETE_SUFFIX = ".obsolete.json"


def record_obsolete(out_dir: str | Path, target: str | Path, reason: str) -> tuple[Path, str]:
    """Mark an artifact obsolete for decision use, without touching it.

    The marker is a separate write-once file naming the target, its digest and
    the reason.  The historical artifact keeps its bytes: an obsolete artifact
    is still evidence of what was decided when it was produced.
    """
    target_path = Path(target)
    document = load_verified(target_path)
    payload = {
        "kind": "artifact-obsolete",
        "target": target_path.name,
        "target_digest": compute_digest(document),
        "target_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
        "reason": reason,
        "recorded_at": __import__("datetime").datetime.now(
            __import__("datetime").timezone.utc).isoformat(),
        "usable_for_decision": False,
    }
    marker = Path(out_dir) / f"{target_path.name}{OBSOLETE_SUFFIX}"
    digest = write_immutable(marker, payload)
    return marker, digest


def is_obsolete(target: str | Path) -> dict[str, Any] | None:
    """The obsolescence record for an artifact, or ``None`` if it is current."""
    target_path = Path(target)
    marker = target_path.parent / f"{target_path.name}{OBSOLETE_SUFFIX}"
    if not marker.exists():
        return None
    return load_verified(marker)
