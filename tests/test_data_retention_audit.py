import os
from pathlib import Path

from scripts.ops.data_retention_audit import classify, inventory


def test_classification_is_conservative():
    assert classify(Path("retention-hold-20260919/file.dump")) == "held_evidence"
    assert classify(Path("doc_downloads/source.pdf")) == "source_evidence"
    assert classify(Path("document-layout-benchmark/pdf-cache/source.pdf")) == "reproducible_cache"
    assert classify(Path("mystery/value.bin")) == "unclassified"


def test_inventory_deduplicates_hardlinks_for_physical_accounting(tmp_path):
    data = tmp_path / "data"
    backups = data / "backups"
    hold = data / "retention-hold-20260919"
    backups.mkdir(parents=True)
    hold.mkdir()
    original = backups / "database.dump"
    original.write_bytes(b"x" * 4096)
    os.link(original, hold / original.name)

    report = inventory(data, largest=5, now=original.stat().st_mtime)

    assert report["file_count"] == 2
    assert report["logical_path_bytes"] == 8192
    assert report["unique_inode_apparent_bytes"] == 4096
    assert report["hardlink_savings_apparent_bytes"] == 4096
    assert report["by_category"]["held_evidence"]["files"] == 1


def test_output_inventory_does_not_follow_symlinks(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    target = tmp_path / "outside"
    target.write_text("outside")
    (data / "link").symlink_to(target)
    report = inventory(data)
    assert report["file_count"] == 0
    assert report["symlink_count"] == 1
