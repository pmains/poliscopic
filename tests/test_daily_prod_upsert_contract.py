from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync/daily_prod_upsert.sh"


def test_daily_runner_is_foreground_idempotent_and_upsert_only():
    text = SCRIPT.read_text()
    assert "nohup" not in text
    assert "sync_prod.py" in text
    assert 'AUTHORIZATION_ID="OP-RECON-standing-daily-sync"' in text
    assert ".prod-upsert-${RUN_DATE}.lock" in text
    assert "daily_sync_terminal.py" in text


def test_completion_gate_precedes_sync_and_success_marker():
    text = SCRIPT.read_text()
    gate = text.index("sync_completion_check.sh")
    sync = text.index("scripts/db/sync_prod.py")
    marker = text.index("scripts/ops/daily_sync_terminal.py", sync)
    assert gate < sync < marker


def test_public_checks_are_mandatory_before_terminal_marker():
    text = SCRIPT.read_text()
    assert "curl -fsS" in text
    assert text.index("https://poliscopic.com/") < text.index(
        "scripts/ops/daily_sync_terminal.py", text.index("scripts/db/sync_prod.py"))


def test_restore_verified_backup_precedes_the_sync():
    text = SCRIPT.read_text()
    assert text.index("scripts/ops/production_preflight.py") < text.index(
        "scripts/ops/daily_sync_backup.py") < text.index("scripts/db/sync_prod.py")
    assert "--backup-receipt \"$BACKUP_RECEIPT\"" in text
    assert "--attempt-id \"$ATTEMPT_ID\"" in text
