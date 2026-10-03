from __future__ import annotations

import json
import os
from pathlib import Path

from scripts.ops import daily_sync_backup as backup


def _generation(root: Path, index: int, *, valid: bool = True) -> tuple[Path, Path, Path]:
    tag = f"202610{index:02d}T120000Z"
    stem = backup.PREFIX + tag
    baseline = (root / f"{stem}.baseline.json").resolve()
    dump = (root / f"{stem}.dump").resolve()
    receipt = (root / f"{stem}.receipt.json").resolve()
    baseline.write_text("{}\n")
    dump.write_bytes(f"dump-{index}".encode())
    body = {
        "schema": backup.SCHEMA,
        "status": "VALID",
        "baseline_path": str(baseline),
        "dump_path": str(dump),
        "dump_sha256": backup.sha256_file(dump),
    }
    payload = {**body, "digest": backup.digest(body)}
    if not valid:
        payload["digest"] = "0" * 64
    receipt.write_text(json.dumps(payload))
    for path in (baseline, dump, receipt):
        os.chmod(path, 0o600)
    return baseline, dump, receipt


def test_retention_keeps_five_newest_verified_generations(tmp_path):
    generations = [_generation(tmp_path, index) for index in range(1, 8)]

    removed = set(backup.prune_verified(backup_dir=tmp_path))

    assert removed == {str(path) for generation in generations[:2]
                       for path in generation}
    assert all(not path.exists() for generation in generations[:2]
               for path in generation)
    assert all(path.exists() for generation in generations[2:]
               for path in generation)


def test_invalid_generation_is_never_pruned(tmp_path):
    invalid = _generation(tmp_path, 1, valid=False)
    for index in range(2, 8):
        _generation(tmp_path, index)

    backup.prune_verified(backup_dir=tmp_path)

    assert all(path.exists() for path in invalid)


def test_receipt_cannot_name_files_outside_backup_directory(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    baseline, dump, receipt = _generation(root, 1)
    outside = tmp_path / f"{backup.PREFIX}outside.dump"
    outside.write_bytes(b"outside")
    payload = json.loads(receipt.read_text())
    payload["dump_path"] = str(outside.resolve())
    payload["dump_sha256"] = backup.sha256_file(outside)
    payload["digest"] = backup.digest(
        {key: value for key, value in payload.items() if key != "digest"})
    receipt.write_text(json.dumps(payload))

    assert backup.verified_generation(receipt, backup_dir=root) is None
    assert backup.prune_verified(backup_dir=root) == []
    assert outside.exists() and baseline.exists() and dump.exists()


def test_retention_never_allows_zero_generations(tmp_path):
    try:
        backup.prune_verified(backup_dir=tmp_path, keep=0)
    except ValueError as exc:
        assert "at least one" in str(exc)
    else:
        raise AssertionError("zero-retention policy was accepted")
