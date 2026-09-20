#!/usr/bin/env python3
"""``stage2_s2_receipt.py`` — the canonical apply receipt.

A receipt is the only thing that can establish what an apply did.  It is loaded
the way every other artifact is loaded — by path, through the verify-on-read
loader — and it is bound to the authorized plan's **path and digest**, and to the
target it was applied to.

**A caller cannot supply a receipt object at all.**  Every public entry point takes
a *path* and loads it internally, so the digest is checked against the file's
current bytes rather than against whatever a caller passed.  There is no token
class to mint, no sentinel to obtain, and no mapping to hand in: the only way to
get a receipt into this module is to name a file on disk that verifies.

There is deliberately **no** ``database_unchanged`` escape hatch: a receipt that
claims a no-op must still name the rows it would have owned and their exact
postimage, or it establishes nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
for _candidate in (str(REPO), str(SCRIPTS)):
    if _candidate not in sys.path:  # pragma: no cover - import bootstrap
        sys.path.insert(0, _candidate)

from scripts.kg import stage2_artifacts as artifacts  # noqa: E402

__all__ = [
    "ReceiptRefused",
    "build_receipt",
    "load_authorized_receipt",
    "verify_receipt",
]

RECEIPT_KIND = "kg-stage2-s2-apply-receipt"
RECEIPT_VERSION = "kg-stage2-s2-apply-receipt/3.0"

#: Commit statuses a receipt may carry.  There is no "unknown" and no default.
COMMIT_STATUSES = ("committed", "no-op-already-applied", "rolled-back")


class ReceiptRefused(RuntimeError):
    """The receipt is not admissible; nothing may be concluded from it."""


def build_receipt(
    *,
    plan: Mapping[str, Any],
    plan_path: str,
    plan_role: str,
    target: Mapping[str, Any],
    commit_status: str,
    inserted_rows: Sequence[Mapping[str, Any]] = (),
    attachment_preimages: Sequence[Mapping[str, Any]] = (),
    authorized_by: str = "",
) -> dict[str, Any]:
    """Assemble a receipt bound to one plan, path and target."""
    if commit_status not in COMMIT_STATUSES:
        raise ReceiptRefused(f"commit_status {commit_status!r} is not one of {COMMIT_STATUSES}")
    if not authorized_by.strip():
        raise ReceiptRefused("a receipt must name who authorized the apply")
    return {
        "kind": RECEIPT_KIND,
        "version": RECEIPT_VERSION,
        "plan_role": plan_role,
        "plan_path": Path(plan_path).name,
        "plan_digest": plan.get("digest"),
        "plan_replay_digest": plan.get("replay_digest"),
        "target": {"dialect": target.get("dialect"), "host": target.get("host"),
                   "port": target.get("port"), "database": target.get("database")},
        "commit_status": commit_status,
        "authorized_by": authorized_by,
        "inserted_rows": [
            {"id": int(r["id"]), "meeting_db_id": int(r["meeting_db_id"]),
             "agenda_item_number": str(r["agenda_item_number"]),
             "row_fingerprint": r["row_fingerprint"]} for r in inserted_rows],
        "attachment_preimages": [dict(p) for p in attachment_preimages],
    }


def load_authorized_receipt(
    path: str | Path, *,
    plan_path: str,
    plan_digest: str,
    target: Mapping[str, Any],
    plan_replay_digest: str | None = None,
) -> dict[str, Any]:
    """Load a receipt canonically and require it to be bound to the plan and target.

    ``path`` is the **only** accepted form.  A caller names a file; this function
    reads it, verifies its stored digest against its bytes, and then requires it to
    name the authorized plan's path and digest and the live target.  Nothing a
    caller passes is ever treated as the receipt itself.
    """
    if isinstance(path, Mapping) or not isinstance(path, (str, Path)):
        raise ReceiptRefused(
            f"a {type(path).__name__} is not a receipt path: only the path to a "
            f"canonical receipt file is accepted")
    p = Path(path)
    if not p.exists():
        raise ReceiptRefused(f"receipt {p.name!r} does not exist")
    if artifacts.is_obsolete(p) is not None:
        raise ReceiptRefused(f"receipt {p.name!r} is obsolete")
    try:
        receipt = artifacts.load_verified(p)      # verifies the stored digest
    except Exception as exc:  # noqa: BLE001
        raise ReceiptRefused(f"receipt {p.name!r} failed verification: {exc}") from exc

    if receipt.get("kind") != RECEIPT_KIND:
        raise ReceiptRefused(f"kind {receipt.get('kind')!r} is not {RECEIPT_KIND!r}")
    if receipt.get("commit_status") not in COMMIT_STATUSES:
        raise ReceiptRefused(f"commit_status {receipt.get('commit_status')!r} is not registered")
    if receipt.get("plan_path") != Path(plan_path).name:
        raise ReceiptRefused("the receipt names a different plan path")
    if receipt.get("plan_digest") != plan_digest:
        raise ReceiptRefused("the receipt names a different plan digest")
    if plan_replay_digest is not None and \
            receipt.get("plan_replay_digest") != plan_replay_digest:
        raise ReceiptRefused("the receipt names a different plan replay digest")
    for field in ("dialect", "host", "port", "database"):
        if receipt.get("target", {}).get(field) != target.get(field):
            raise ReceiptRefused(f"the receipt target {field!r} differs from the live target")
    return receipt


def verify_receipt(
    path: str | Path, *,
    plan_path: str,
    plan_digest: str,
    target: Mapping[str, Any],
    current: Mapping[str, Any],
    plan_replay_digest: str | None = None,
) -> dict[str, Any]:
    """Load the receipt by path, then compare its exact owned postimage.

    No shortcuts: the receipt is read from disk and verified, so the comparison
    always rests on bytes, never on a caller's word.
    """
    receipt = load_authorized_receipt(
        path, plan_path=plan_path, plan_digest=plan_digest, target=target,
        plan_replay_digest=plan_replay_digest)

    rows = receipt.get("inserted_rows")
    if rows is None:
        raise ReceiptRefused("the receipt names no inserted rows")
    if not rows:
        raise ReceiptRefused("the receipt owns nothing, so it cannot establish a postimage")

    from scripts.kg import stage2_s2_apply_runner as runner

    live = {(int(i["meeting_db_id"]), str(i["agenda_item_number"])): i
            for i in current.get("items") or []}
    seen: set[tuple[int, str]] = set()
    for entry in rows:
        key = (int(entry["meeting_db_id"]), str(entry["agenda_item_number"]))
        if key in seen:
            raise ReceiptRefused(f"the receipt names {key} twice")
        seen.add(key)
        row = live.get(key)
        if row is None:
            raise ReceiptRefused(f"receipt-owned row {key} is absent: postimage drifted")
        if runner.item_row_fingerprint(row) != entry.get("row_fingerprint"):
            raise ReceiptRefused(f"receipt-owned row {key} has drifted")

    documents = {int(d["id"]): d for d in current.get("documents") or []}
    for entry in receipt.get("attachment_preimages") or []:
        document = documents.get(int(entry["id"]))
        if document is None:
            raise ReceiptRefused(f"receipt-owned document {entry['id']} is absent")
        for field in ("agenda_item_id", "agenda_item_number"):
            if str(document.get(field) or "") != str(entry.get("postimage", {}).get(field) or ""):
                raise ReceiptRefused(
                    f"document {entry['id']} {field} postimage drifted")
    return {"status": "postimage-verified", "rows": len(rows),
            "documents": len(receipt.get("attachment_preimages") or [])}
