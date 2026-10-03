from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/sync/daily_prod_upsert_status.sh"


def test_status_requires_success_terminal_and_live_http_checks():
    text = SCRIPT.read_text()
    assert '"status":"success"' in text
    assert "homepage_http" in text
    assert "tempe_1964_http" in text
    assert 'HOME_CODE" = "200"' in text
    assert 'TEMPE_CODE" = "200"' in text


def test_missing_terminal_is_pending_not_success():
    text = SCRIPT.read_text()
    assert "PENDING: no successful terminal receipt" in text
    assert text.rstrip().endswith("exit 1")
