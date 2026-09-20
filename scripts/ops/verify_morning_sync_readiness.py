#!/usr/bin/env python3
"""Fail-closed receipt gate for the scheduled Brief 031 morning data sync.

The gate accepts only the immutable terminal receipt produced by
``body_code_merge_prod.py --apply`` after its one-transaction production merge.
It performs no database or network access.  The receipt binds the reviewed
production target, plan digests, schema capabilities, backup proof, and both
canonical-name references.  A prep receipt, a manual flag, or an old receipt
cannot enable the scheduled sync.
"""

from __future__ import annotations

import argparse
import re
import stat
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_ROOT / "scripts"))

from body_code_merge_runtime import PRODUCTION_TARGET, content_digest  # noqa: E402
from kg import stage2_artifacts  # noqa: E402

RECEIPT_DIR = _ROOT / "data" / "body-code-merge" / "prod"
RECEIPT_GLOB = "body-code-merge-prod-receipt-*.json"
RECEIPT_KIND = "body-code-merge-prod-apply-receipt"
REQUIRED_MERGES = {
    ("chandler-planning-zoning-commission", "chandler-pz"),
    ("mesa-planning-zoning", "mesa-pz"),
}
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
IDENTITY_KEYS = ("database", "host", "port", "server_version", "dialect",
                 "driver", "cluster_identity")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_verified(path: Path, *, label: str) -> tuple[dict | None, list[str]]:
    """Read an owner-only immutable artifact, without any database access."""
    try:
        if stat.S_IMODE(path.stat().st_mode) != 0o600:
            return None, [f"{label} mode is not 0600"]
        return stage2_artifacts.load_verified(path), []
    except Exception as exc:
        return None, [f"{label} failed immutable-artifact verification: {exc}"]


def _full_identity(value: object, *, label: str) -> tuple[dict | None, list[str]]:
    if not isinstance(value, dict):
        return None, [f"{label} records no full target identity"]
    missing = [key for key in IDENTITY_KEYS if key not in value]
    if missing:
        return None, [f"{label} identity is missing {missing}"]
    if (value.get("database") != PRODUCTION_TARGET["database"] or
            value.get("host") != PRODUCTION_TARGET["host"] or
            not isinstance(value.get("port"), int) or value["port"] <= 0 or
            not str(value.get("server_version") or "") or
            not str(value.get("dialect") or "") or
            not str(value.get("driver") or "")):
        return None, [f"{label} is not a complete pinned production identity"]
    return {key: value[key] for key in IDENTITY_KEYS}, []


def _execution_plan(artifact: dict) -> dict | None:
    plan_digest = artifact.get("plan_digest")
    if not isinstance(plan_digest, str) or not _SHA256.fullmatch(plan_digest):
        return None
    plan = dict(artifact)
    plan.pop("plan_digest", None)
    plan.pop("artifact_created_at", None)
    plan["digest"] = plan_digest
    return plan


def _plan_binding(path: Path, artifact: dict, plan: dict, identity: dict) -> dict:
    return {"path": str(path.resolve()), "artifact_digest": artifact["digest"],
            "plan_digest": plan["digest"], "content_digest": content_digest(plan),
            "target_identity": identity}


def _verify_runbook(binding: object, *, dump_path: Path, dump_sha256: str,
                    plan_digest: str) -> list[str]:
    if not isinstance(binding, dict) or not binding.get("present"):
        return ["receipt records no restore runbook proof"]
    path = Path(binding.get("path") or "")
    try:
        if not path.is_file() or stat.S_IMODE(path.stat().st_mode) != 0o600:
            return ["restore runbook is absent or not mode 0600"]
        actual = __import__("hashlib").sha256(path.read_bytes()).hexdigest()
        if binding.get("sha256") != actual:
            return ["restore runbook digest mismatch"]
        if binding.get("dump_sha256") != dump_sha256 or binding.get("plan_digest") != plan_digest:
            return ["restore runbook is bound to another dump or plan"]
        text = path.read_text(encoding="utf-8")
        if str(dump_path) not in text or dump_sha256 not in text or plan_digest not in text:
            return ["restore runbook content is not bound to the artifact chain"]
    except OSError as exc:
        return [f"restore runbook cannot be read: {exc}"]
    return []


def validate_receipt(path: Path, *, now: datetime, max_age: timedelta) -> list[str]:
    """Verify the entire local, immutable merge proof without contacting production."""
    payload, problems = _load_verified(path, label="terminal receipt")
    if payload is None:
        return problems
    if payload.get("kind") != RECEIPT_KIND:
        problems.append("receipt is not a terminal production apply receipt")
    if payload.get("status") != "success":
        problems.append("receipt does not record a successful merge")
    if payload.get("tier") != "production" or payload.get("target") != PRODUCTION_TARGET:
        problems.append("receipt is not bound to the pinned production target")
    identity, identity_problems = _full_identity(
        payload.get("target_identity"), label="terminal receipt")
    problems.extend(identity_problems)
    created = _parse_time(payload.get("created_at"))
    if created is None or created > now or now - created > max_age:
        problems.append("receipt is missing a fresh UTC creation timestamp")
    for key in ("plan_digest", "content_digest"):
        if not isinstance(payload.get(key), str) or not _SHA256.fullmatch(payload[key]):
            problems.append(f"receipt {key} is not a SHA-256 digest")
    if not isinstance(payload.get("capabilities"), dict):
        problems.append("receipt records no schema capabilities")
    if not isinstance(payload.get("backup"), dict) or not payload["backup"].get("receipt"):
        problems.append("receipt records no verified backup proof")
    if not isinstance(payload.get("post_counts"), dict) or not payload["post_counts"]:
        problems.append("receipt records no post-merge counts")
    pairs = {(m.get("old"), m.get("new")) for m in payload.get("merges", []) if isinstance(m, dict)}
    if pairs != REQUIRED_MERGES:
        problems.append("receipt does not bind exactly the Chandler/Mesa canonical merges")

    plan_binding = payload.get("plan_artifact")
    if not isinstance(plan_binding, dict):
        return problems + ["receipt records no immutable reviewed plan proof"]
    plan_path = Path(plan_binding.get("path") or "")
    plan_artifact, plan_problems = _load_verified(plan_path, label="plan artifact")
    problems.extend(plan_problems)
    if plan_artifact is None:
        return problems
    plan = _execution_plan(plan_artifact)
    if plan is None:
        return problems + ["plan artifact records no SHA-256 runtime plan digest"]
    planned_at = _parse_time(plan_artifact.get("artifact_created_at"))
    if planned_at is None or planned_at > now or now - planned_at > max_age:
        problems.append("plan artifact is missing a fresh UTC creation timestamp")
    plan_identity, plan_identity_problems = _full_identity(
        plan.get("target_identity"), label="plan artifact")
    problems.extend(plan_identity_problems)
    if plan_identity is None:
        return problems
    expected_binding = _plan_binding(plan_path, plan_artifact, plan, plan_identity)
    if plan_binding != expected_binding:
        problems.append("terminal receipt plan proof does not match immutable plan artifact")
    if payload.get("plan_digest") != plan["digest"]:
        problems.append("terminal receipt plan digest differs from the reviewed plan")
    if payload.get("content_digest") != content_digest(plan):
        problems.append("terminal receipt content digest differs from the reviewed plan")
    if identity is not None and identity != plan_identity:
        problems.append("terminal receipt identity differs from the reviewed plan")

    backup_ref = payload.get("backup") if isinstance(payload.get("backup"), dict) else {}
    backup_path = Path(backup_ref.get("receipt") or "")
    backup, backup_problems = _load_verified(backup_path, label="backup receipt")
    problems.extend(backup_problems)
    if backup is None:
        return problems
    if backup_ref.get("receipt_digest") != backup.get("digest"):
        problems.append("terminal receipt backup digest does not match backup artifact")
    if backup.get("kind") != "body-code-merge-prod-backup-receipt" or backup.get("tier") != "production":
        problems.append("backup proof is not a production scratch receipt")
    if backup.get("problems"):
        problems.append("backup proof records problems")
    comparisons = backup.get("comparisons")
    if (not isinstance(comparisons, dict) or not comparisons or
            any(value is not True for value in comparisons.values())):
        problems.append("backup proof comparisons are empty or did not all pass")
    backup_identity, backup_identity_problems = _full_identity(
        backup.get("target"), label="backup receipt")
    problems.extend(backup_identity_problems)
    if backup_identity is not None and backup_identity != plan_identity:
        problems.append("backup identity differs from the reviewed plan")
    if backup.get("plan_artifact") != expected_binding:
        problems.append("backup proof does not bind the reviewed immutable plan")
    if backup.get("plan_content_digest") != content_digest(plan):
        problems.append("backup proof content digest differs from the reviewed plan")
    dump_path = Path(backup.get("dump_path") or "")
    try:
        if stat.S_IMODE(dump_path.stat().st_mode) != 0o600:
            problems.append("backup dump mode is not 0600")
        actual_dump = __import__("hashlib").sha256(dump_path.read_bytes()).hexdigest()
        if backup.get("dump_sha256") != actual_dump:
            problems.append("backup dump digest mismatch")
    except OSError as exc:
        problems.append(f"backup dump cannot be read: {exc}")
        actual_dump = ""
    if actual_dump:
        problems.extend(_verify_runbook(backup.get("restore_runbook"),
                                        dump_path=dump_path, dump_sha256=actual_dump,
                                        plan_digest=plan["digest"]))
        if payload.get("restore_runbook") != backup.get("restore_runbook"):
            problems.append("terminal receipt runbook proof differs from backup proof")
        problems.extend(_verify_runbook(payload.get("restore_runbook"),
                                        dump_path=dump_path, dump_sha256=actual_dump,
                                        plan_digest=plan["digest"]))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify the morning production-sync readiness receipt")
    parser.add_argument("--receipt-dir", type=Path, default=RECEIPT_DIR)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    args = parser.parse_args(argv)
    if args.max_age_hours <= 0:
        parser.error("--max-age-hours must be positive")
    now = datetime.now(timezone.utc)
    paths = sorted(args.receipt_dir.glob(RECEIPT_GLOB), key=lambda p: p.stat().st_mtime, reverse=True)
    if not paths:
        print("readiness: BLOCKED — no terminal production merge receipt found", file=sys.stderr)
        return 2
    newest = paths[0]
    problems = validate_receipt(newest, now=now, max_age=timedelta(hours=args.max_age_hours))
    if problems:
        print(f"readiness: BLOCKED — {newest}: {'; '.join(problems)}", file=sys.stderr)
        return 2
    print(f"readiness: verified {newest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
