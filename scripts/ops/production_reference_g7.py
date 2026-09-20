#!/usr/bin/env python3
"""Offline G3/G7 preparation for the one reviewed reference OP-REPAIR.

This module can hash committed code and assemble/verify evidence.  It has no
database, network, authorization, or apply path.  A valid result explicitly
remains blocked at G8.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import stat
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

REPO = Path(__file__).resolve().parents[2]
OPS = Path(__file__).resolve().parent
import sys
for _path in (REPO, REPO / "scripts", OPS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import production_preflight as preflight  # noqa: E402

MANIFEST_SCHEMA = "production-reference-op-repair-manifest/1"
G7_SCHEMA = "production-reference-op-repair-g7/1"
G6_SCHEMA = "production-g6-backup-proof/1"
CANDIDATE_SCHEMA = "production-reference-repair-plan/1"
EVIDENCE_SCHEMA = "production-reference-evidence/1"
G5_SCHEMA = "production-g5-preflight/1"

# Closed, operation-specific surface.  A general release closure is not
# authority for this repair and is deliberately rejected by schema.
SURFACE = (
    "scripts/ops/production_interlock.py",
    "scripts/ops/production_preflight.py",
    "scripts/ops/production_reference_evidence.py",
    "scripts/ops/production_reference_repair_plan.py",
    "scripts/ops/production_g6_backup.py",
    "scripts/ops/production_reference_g7.py",
)
EXPECTED = {"meetings": 1124, "agenda_items": 6849}
EXPECTED_TOTAL = 7973
EXPECTED_QUARANTINE = 3621
EXPECTED_CANDIDATE_DIGEST = (
    "13dba3455e03a423619405d75647143acef9bab9aca0b6879726fd919c0085bc"
)
COMPARISON_KEYS = {
    "off_volume_bytes", "off_volume_sha256", "counts", "schema", "integrity",
    "proposal_preimages",
}


class Refused(RuntimeError):
    """Fail-closed preparation refusal."""


def _run_git(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=REPO, text=True,
                            capture_output=True)
    if result.returncode:
        raise Refused(f"git {' '.join(args)} refused")
    return result.stdout


def _git_bytes(*args: str) -> bytes:
    result = subprocess.run(["git", *args], cwd=REPO, capture_output=True)
    if result.returncode:
        raise Refused(f"git {' '.join(args)} refused")
    return result.stdout


def _load(path: Path, label: str, schema: str | None = None) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise Refused(f"{label} is not readable JSON") from exc
    if not isinstance(value, dict):
        raise Refused(f"{label} is not an object")
    if schema is not None and value.get("schema") != schema:
        raise Refused(f"{label} schema is not operation-specific {schema}")
    body = {key: item for key, item in value.items() if key != "digest"}
    if value.get("digest") != preflight.digest(body):
        raise Refused(f"{label} digest mismatch")
    return value


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    preflight.write_exclusive(path, payload)
    if stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise Refused(f"output mode is not 0600: {path}")


def _parse_time(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise Refused(f"{label} is absent")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Refused(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise Refused(f"{label} lacks a timezone")
    return parsed.astimezone(timezone.utc)


def build_manifest(output: Path) -> dict[str, Any]:
    """Hash exact committed blobs from a clean checkout, then create once."""
    status = _run_git("status", "--porcelain=v1", "--untracked-files=all")
    if status:
        raise Refused("Git worktree is not exactly clean")
    commit = _run_git("rev-parse", "HEAD").strip()
    if len(commit) != 40:
        raise Refused("exact Git commit is unavailable")
    files = []
    for relative in SURFACE:
        listed = _run_git("ls-tree", commit, "--", relative).strip().split()
        if len(listed) < 4 or listed[1] != "blob":
            raise Refused(f"surface path is not a committed blob: {relative}")
        committed = _git_bytes("show", f"{commit}:{relative}")
        path = REPO / relative
        if not path.is_file() or path.read_bytes() != committed:
            raise Refused(f"working file differs from committed blob: {relative}")
        files.append({"path": relative, "git_mode": listed[0],
                      "git_blob": listed[2],
                      "sha256": hashlib.sha256(committed).hexdigest(),
                      "bytes": len(committed)})
    body = {"schema": MANIFEST_SCHEMA, "operation": "OP-REPAIR",
            "exact_commit": commit, "authority": "preparation-surface-only",
            "content_authority": "committed-blobs-only",
            "executor_included": False, "mutable_paths_authority": False, "files": files,
            "file_count": len(files), "applies_anything": False}
    artifact = {**body, "digest": preflight.digest(body)}
    _write(output, artifact)
    return artifact


def _validate_candidate(candidate: Mapping[str, Any]) -> tuple[list[dict[str, Any]], str]:
    if (candidate.get("operation") != "OP-REPAIR" or
            candidate.get("status") != "CANDIDATE-NOT-AUTHORIZABLE" or
            candidate.get("apply_blocked") is not True):
        raise Refused("candidate is not unchanged apply-blocked evidence")
    if candidate.get("digest") != EXPECTED_CANDIDATE_DIGEST:
        raise Refused("candidate is not the exact reviewed semantic digest")
    proposals, quarantine = candidate.get("proposals"), candidate.get("quarantine")
    if not isinstance(proposals, list) or not isinstance(quarantine, list):
        raise Refused("candidate populations are absent")
    if candidate.get("counts") != {"proposals": EXPECTED_TOTAL,
                                    "quarantine": EXPECTED_QUARANTINE}:
        raise Refused("candidate counts drifted")
    seen: set[tuple[str, int]] = set()
    by_table: dict[str, int] = {}
    operations = []
    for proposal in proposals:
        table = proposal.get("table") if isinstance(proposal, dict) else None
        primary = proposal.get("primary_key") if isinstance(proposal, dict) else None
        row_id = primary.get("id") if isinstance(primary, dict) else None
        before, change = proposal.get("before"), proposal.get("set")
        if (table not in EXPECTED or not isinstance(row_id, int) or
                not isinstance(before, dict) or before.get("id") != row_id or
                not isinstance(change, dict) or set(change) != {"public_body_id"} or
                not isinstance(change["public_body_id"], int)):
            raise Refused("candidate contains a non-update-only operation")
        key = (table, row_id)
        if key in seen:
            raise Refused("candidate contains duplicate operations")
        seen.add(key)
        by_table[table] = by_table.get(table, 0) + 1
        operations.append({"kind": "UPDATE", "table": table,
                           "primary_key": {"id": row_id},
                           "set": {"public_body_id": change["public_body_id"]},
                           "preimage_digest": preflight.digest(before)})
    if len(operations) != EXPECTED_TOTAL or by_table != EXPECTED:
        raise Refused("operation population differs from the reviewed cohort")
    quarantine_keys: set[tuple[str, str | None, int]] = set()
    for entry in quarantine:
        category = entry.get("category") if isinstance(entry, dict) else None
        table = entry.get("table") if isinstance(entry, dict) else None
        primary = entry.get("primary_key") if isinstance(entry, dict) else None
        row_id = primary.get("id") if isinstance(primary, dict) else None
        if not isinstance(category, str) or not isinstance(row_id, int):
            raise Refused("quarantine contains a malformed identity")
        quarantine_key = (category, table, row_id)
        if quarantine_key in quarantine_keys:
            raise Refused("quarantine contains duplicate identities")
        quarantine_keys.add(quarantine_key)
        if table in EXPECTED and isinstance(row_id, int):
            key = (table, row_id)
            if key in seen:
                raise Refused("quarantine overlaps operations")
    return operations, preflight.digest(quarantine)


def _same_target(left: Mapping[str, Any], right: Mapping[str, Any], label: str) -> None:
    keys = ("database", "configured_host", "configured_port", "server_address",
            "server_port", "cluster_system_identifier")
    if any(left.get(key) != right.get(key) for key in keys):
        raise Refused(f"{label} target binding differs")


def _verify_manifest_checkout(manifest: Mapping[str, Any]) -> None:
    if manifest.get("operation") != "OP-REPAIR" or manifest.get("applies_anything") is not False:
        raise Refused("manifest is not the non-applying operation-specific manifest")
    if tuple(item.get("path") for item in manifest.get("files", [])) != SURFACE:
        raise Refused("manifest surface is not the closed OP-REPAIR surface")
    if manifest.get("file_count") != len(SURFACE):
        raise Refused("manifest file count differs")
    if (manifest.get("authority") != "preparation-surface-only" or
            manifest.get("content_authority") != "committed-blobs-only" or
            manifest.get("executor_included") is not False):
        raise Refused("manifest overstates its preparation-only surface")
    if _run_git("status", "--porcelain=v1", "--untracked-files=all"):
        raise Refused("Git worktree is not exactly clean")
    commit = _run_git("rev-parse", "HEAD").strip()
    if commit != manifest.get("exact_commit"):
        raise Refused("manifest exact commit is not checked out")
    for item in manifest["files"]:
        relative = item["path"]
        listed = _run_git("ls-tree", commit, "--", relative).strip().split()
        if len(listed) < 4 or listed[1] != "blob" or listed[2] != item.get("git_blob"):
            raise Refused(f"manifest Git blob differs: {relative}")
        content = _git_bytes("show", f"{commit}:{relative}")
        if (hashlib.sha256(content).hexdigest() != item.get("sha256") or
                len(content) != item.get("bytes") or listed[0] != item.get("git_mode")):
            raise Refused(f"manifest committed content differs: {relative}")


def _verify_chain(candidate: Mapping[str, Any], candidate_path: Path,
                  evidence: Mapping[str, Any], g5: Mapping[str, Any],
                  g6: Mapping[str, Any], current: datetime) -> dict[str, Any]:
    if evidence.get("status") != "VALID" or g5.get("status") != "VALID":
        raise Refused("G5/reference evidence is not VALID")
    if evidence.get("g5_binding", {}).get("digest") != g5["digest"]:
        raise Refused("reference evidence does not bind exact G5")
    bindings = candidate.get("bindings") or {}
    if (bindings.get("reference_evidence", {}).get("digest") != evidence["digest"] or
            bindings.get("g5", {}).get("digest") != g5["digest"]):
        raise Refused("candidate evidence chain differs")
    for artifact, label in ((evidence, "reference evidence"), (g5, "G5"),
                            (g6, "G6")):
        _same_target(candidate.get("target") or {}, artifact.get("target") or {}, label)
    comparisons = g6.get("comparisons") or {}
    if (g6.get("status") != "VALID" or g6.get("operation") != "OP-REPAIR" or
            g6.get("problems") != [] or set(comparisons) != COMPARISON_KEYS or
            not all(comparisons.values())):
        raise Refused("G6 is not a fully verified VALID receipt")
    g6_created = _parse_time(g6.get("created_at"), "G6 created_at")
    g6_expiry = _parse_time(g6.get("expires_at"), "G6 expires_at")
    if current < g6_created - timedelta(minutes=5) or current > g6_expiry:
        raise Refused("G6 receipt is not currently valid")
    if current - g6_created > timedelta(hours=4):
        raise Refused("G6 receipt is not fresh")
    binding = g6.get("candidate_binding") or {}
    if (binding.get("semantic_digest") != candidate["digest"] or
            binding.get("raw_sha256") != hashlib.sha256(candidate_path.read_bytes()).hexdigest() or
            binding.get("upstream_bindings") != candidate.get("bindings")):
        raise Refused("G6 does not bind the exact candidate")
    baseline = _load(Path(str(g6.get("baseline_path", ""))), "G6 baseline")
    if baseline["digest"] != g6.get("baseline_digest"):
        raise Refused("G6 baseline binding differs")
    _same_target(candidate.get("target") or {}, baseline.get("target") or {}, "G6 baseline")
    if not baseline.get("schema_sha256"):
        raise Refused("G6 baseline has no schema binding")
    preimages = baseline.get("proposal_preimages") or {}
    if (preimages.get("count") != EXPECTED_TOTAL or
            preimages.get("by_table") != EXPECTED or
            not isinstance(preimages.get("digest"), str) or
            len(preimages["digest"]) != 64 or
            g6.get("proposal_preimages") != preimages):
        raise Refused("G6 proposal-preimage proof differs")
    dump = g6.get("dump") or {}
    dump_path = Path(str(dump.get("path", "")))
    if (not dump_path.is_file() or dump.get("bytes") != dump_path.stat().st_size or
            not isinstance(dump.get("bytes"), int) or dump["bytes"] <= 0 or
            dump.get("sha256") != hashlib.sha256(dump_path.read_bytes()).hexdigest()):
        raise Refused("G6 dump is absent, empty, or hash-mismatched")
    off_volume = g6.get("off_volume") or {}
    if (off_volume.get("retained") is not True or
            off_volume.get("sha256") != dump.get("sha256") or
            off_volume.get("bytes") != dump.get("bytes") or
            not isinstance(off_volume.get("machine"), str) or
            not off_volume.get("machine") or
            off_volume.get("host") in {None, "", candidate["target"].get("configured_host")} or
            not off_volume.get("volume")):
        raise Refused("G6 off-volume retention proof is invalid")
    scratch = g6.get("scratch") or {}
    required_scratch = ("restored_from_off_volume", "counts_match", "schema_match",
                        "integrity_match", "force_dropped", "absence_proved")
    if any(scratch.get(key) is not True for key in required_scratch):
        raise Refused("G6 scratch restore/teardown proof is incomplete")
    return baseline


def build_g7(*, candidate_path: Path, evidence_path: Path, g5_path: Path,
             g6_path: Path, manifest_path: Path, output: Path,
             rollback_owner: str, expires_at: str, now: datetime | None = None,
             nonce: str | None = None) -> dict[str, Any]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    candidate = _load(candidate_path, "candidate", CANDIDATE_SCHEMA)
    evidence = _load(evidence_path, "reference evidence", EVIDENCE_SCHEMA)
    g5 = _load(g5_path, "G5", G5_SCHEMA)
    g6 = _load(g6_path, "G6", G6_SCHEMA)
    manifest = _load(manifest_path, "manifest", MANIFEST_SCHEMA)
    operations, quarantine_digest = _validate_candidate(candidate)

    baseline = _verify_chain(candidate, candidate_path, evidence, g5, g6, current)
    g6_expiry = _parse_time(g6.get("expires_at"), "G6 expires_at")
    _verify_manifest_checkout(manifest)
    owner = rollback_owner.strip()
    if len(owner.split()) < 2 or owner.lower() in {"tbd", "todo", "unknown", "owner"}:
        raise Refused("rollback owner must be a named non-placeholder person")
    expiry = _parse_time(expires_at, "G7 expires_at")
    if not current < expiry <= current + timedelta(hours=4) or expiry > g6_expiry:
        raise Refused("G7 expiry must be future, <=4h, and within G6 validity")
    nonce_value = nonce or secrets.token_hex(32)
    if len(nonce_value) != 64 or any(ch not in "0123456789abcdef" for ch in nonce_value):
        raise Refused("nonce is not 256-bit lowercase hexadecimal")

    body = {
        "schema": G7_SCHEMA, "operation": "OP-REPAIR",
        "status": "G7-CANDIDATE-EXECUTOR-MISSING",
        "apply_blocked": True,
        "missing_g7_bindings": ["committed_executor_surface", "G8_runtime_validator"],
        "authorization": "none - this plan authorizes nothing",
        "applies_anything": False,
        "created_at": current.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": expiry.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "nonce": nonce_value, "rollback_owner": owner,
        "target": candidate["target"],
        "schema_sha256": baseline["schema_sha256"],
        "bindings": {
            "exact_commit": manifest["exact_commit"],
            "manifest_digest": manifest["digest"],
            "candidate_digest": candidate["digest"],
            "reference_evidence_digest": evidence["digest"],
            "g5_digest": g5["digest"], "g6_digest": g6["digest"],
            "g6_baseline_digest": baseline["digest"],
        },
        "operations": operations,
        "operation_counts": {"total": EXPECTED_TOTAL, "by_table": EXPECTED},
        "quarantine": {"count": EXPECTED_QUARANTINE,
                       "canonical_digest": quarantine_digest,
                       "excluded_from_operations": True},
    }
    artifact = {**body, "digest": preflight.digest(body)}
    _write(output, artifact)
    return artifact


def verify_g7(path: Path, *, candidate_path: Path, evidence_path: Path,
              g5_path: Path, g6_path: Path, manifest_path: Path,
              now: datetime | None = None) -> dict[str, Any]:
    """Independently reopen all artifacts and verify the complete binding chain."""
    artifact = _load(path, "G7", G7_SCHEMA)
    rebuilt_path = path.parent / (path.name + ".verification-prohibited")
    # Re-check populations and every upstream digest without creating output.
    candidate = _load(candidate_path, "candidate", CANDIDATE_SCHEMA)
    evidence = _load(evidence_path, "reference evidence", EVIDENCE_SCHEMA)
    g5 = _load(g5_path, "G5", G5_SCHEMA)
    g6 = _load(g6_path, "G6", G6_SCHEMA)
    manifest = _load(manifest_path, "manifest", MANIFEST_SCHEMA)
    operations, quarantine_digest = _validate_candidate(candidate)
    expected = artifact.get("bindings") or {}
    exact = {"exact_commit": manifest["exact_commit"],
             "manifest_digest": manifest["digest"],
             "candidate_digest": candidate["digest"],
             "reference_evidence_digest": evidence["digest"],
             "g5_digest": g5["digest"], "g6_digest": g6["digest"]}
    if any(expected.get(key) != value for key, value in exact.items()):
        raise Refused("G7 upstream binding mismatch")
    if artifact.get("operations") != operations:
        raise Refused("G7 operation population differs from candidate")
    quarantine = artifact.get("quarantine") or {}
    if (quarantine.get("count") != EXPECTED_QUARANTINE or
            quarantine.get("canonical_digest") != quarantine_digest or
            quarantine.get("excluded_from_operations") is not True):
        raise Refused("G7 quarantine binding differs")
    if (artifact.get("status") != "G7-CANDIDATE-EXECUTOR-MISSING" or
            artifact.get("apply_blocked") is not True or
            artifact.get("missing_g7_bindings") !=
            ["committed_executor_surface", "G8_runtime_validator"] or
            artifact.get("authorization") != "none - this plan authorizes nothing" or
            artifact.get("applies_anything") is not False):
        raise Refused("G7 blocking status changed")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    created = _parse_time(artifact.get("created_at"), "G7 created_at")
    expiry = _parse_time(artifact.get("expires_at"), "G7 expires_at")
    if not created <= current < expiry or expiry - created > timedelta(hours=4):
        raise Refused("G7 has expired")
    nonce = artifact.get("nonce")
    if (not isinstance(nonce, str) or len(nonce) != 64 or
            any(ch not in "0123456789abcdef" for ch in nonce)):
        raise Refused("G7 nonce is invalid")
    owner = artifact.get("rollback_owner")
    if not isinstance(owner, str) or len(owner.strip().split()) < 2:
        raise Refused("G7 rollback owner is invalid")
    if artifact.get("operation_counts") != {"total": EXPECTED_TOTAL,
                                             "by_table": EXPECTED}:
        raise Refused("G7 operation counts differ")
    # Full semantic checks not expressible as digest equality.
    if evidence.get("g5_binding", {}).get("digest") != g5["digest"]:
        raise Refused("reference evidence/G5 chain differs")
    if g6.get("status") != "VALID" or current > _parse_time(g6.get("expires_at"), "G6 expires_at"):
        raise Refused("G6 is no longer VALID")
    baseline = _verify_chain(candidate, candidate_path, evidence, g5, g6, current)
    if expected.get("g6_baseline_digest") != baseline["digest"]:
        raise Refused("G7/G6 baseline binding differs")
    _same_target(artifact.get("target") or {}, baseline.get("target") or {}, "G7 baseline")
    if artifact.get("schema_sha256") != baseline.get("schema_sha256"):
        raise Refused("G7 schema binding differs")
    _verify_manifest_checkout(manifest)
    del rebuilt_path  # documents that verification never writes a shadow artifact
    return artifact


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    manifest = sub.add_parser("manifest")
    manifest.add_argument("--output", required=True, type=Path)
    envelope = sub.add_parser("envelope")
    envelope.add_argument("--output", required=True, type=Path)
    envelope.add_argument("--candidate", required=True, type=Path)
    envelope.add_argument("--evidence", required=True, type=Path)
    envelope.add_argument("--g5", required=True, type=Path)
    envelope.add_argument("--g6", required=True, type=Path)
    envelope.add_argument("--manifest", required=True, type=Path)
    envelope.add_argument("--rollback-owner", required=True)
    envelope.add_argument("--expires-at", required=True)
    verify = sub.add_parser("verify")
    for item in (verify,):
        item.add_argument("--g7", required=True, type=Path)
        item.add_argument("--candidate", required=True, type=Path)
        item.add_argument("--evidence", required=True, type=Path)
        item.add_argument("--g5", required=True, type=Path)
        item.add_argument("--g6", required=True, type=Path)
        item.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "manifest":
            result = build_manifest(args.output)
        elif args.command == "envelope":
            result = build_g7(candidate_path=args.candidate,
                              evidence_path=args.evidence, g5_path=args.g5,
                              g6_path=args.g6, manifest_path=args.manifest,
                              output=args.output,
                              rollback_owner=args.rollback_owner,
                              expires_at=args.expires_at)
        else:
            result = verify_g7(args.g7, candidate_path=args.candidate,
                               evidence_path=args.evidence, g5_path=args.g5,
                               g6_path=args.g6, manifest_path=args.manifest)
    except Refused as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 3
    print(json.dumps({"status": result.get("status", "VALID"),
                      "digest": result["digest"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
