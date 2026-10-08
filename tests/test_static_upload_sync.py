from pathlib import Path

import pytest

from scripts.ops import sync_static_uploads as sync


def test_scope_is_narrow_and_mode_is_upsert(monkeypatch, tmp_path):
    monkeypatch.setattr(sync, "UPLOADS", tmp_path.resolve())
    image = tmp_path / "test-static-upload-contract.png"
    image.write_bytes(b"png")
    seen = {}
    try:
        monkeypatch.setattr(sync, "require_production_interlock",
                            lambda op, entry, scope, mode: seen.update(
                                op=op, entry=entry, scope=scope, mode=mode))
        monkeypatch.setattr(sync, "sync_asset", lambda path: {"path": str(path)})
        assert sync.main([str(image)]) == 0
        assert seen == {"op": "OP-CODE", "entry": sync.ENTRY_POINT,
                        "scope": sync.SCOPE, "mode": "upsert"}
    finally:
        image.unlink(missing_ok=True)


@pytest.mark.parametrize("value", ["sync.sh", "static/app.css", "../outside.png"])
def test_paths_outside_uploads_refuse(value):
    with pytest.raises(sync.Refused):
        sync.validate_asset(value)


def test_non_image_and_symlink_refuse(tmp_path):
    original = sync.UPLOADS
    sync.UPLOADS = tmp_path.resolve()
    text = tmp_path / "test-static-upload-contract.txt"
    link = tmp_path / "test-static-upload-contract.png"
    text.write_text("not an image")
    link.symlink_to(text)
    try:
        with pytest.raises(sync.Refused):
            sync.validate_asset(str(text))
        with pytest.raises(sync.Refused):
            sync.validate_asset(str(link))
    finally:
        link.unlink(missing_ok=True)
        text.unlink(missing_ok=True)
        sync.UPLOADS = original


def test_transfer_has_no_delete_shell_or_restart():
    source = Path(sync.__file__).read_text()
    assert '"--delete"' not in source
    assert "shell=True" not in source
    assert "systemctl" not in source
