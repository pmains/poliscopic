#!/usr/bin/env python3
"""The production host pin must be a REAL host, and an alternate host refused.

Why this exists: the pin was the literal fallback ``prod-db.example.invalid``. The
G5 preflight fails closed when the configured target does not match the pin, so a
placeholder made that preflight impossible. The pin also must not simply mirror
whatever ``PROD_DATABASE_URL`` says, or the comparison would be tautological and
would no longer stop a same-named database on another server.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

PINNED = "db-pgsql-nyc1-46544-do-user-37013552-0.a.db.ondigitalocean.com"
PLACEHOLDER = "prod-db.example.invalid"


def _run(body: str, *, drop: tuple[str, ...] = (), extra: dict | None = None) -> str:
    """Run python with the given env keys REMOVED, scripts/ importable."""
    env = {k: v for k, v in os.environ.items() if k not in drop}
    env["PYTHONPATH"] = str(SCRIPTS)
    env.update(extra or {})
    result = subprocess.run([sys.executable, "-c", body], capture_output=True,
                            text=True, env=env, cwd=str(ROOT))
    return (result.stdout or "") + (result.stderr or "")


def test_placeholder_is_absent_when_no_environment_supplies_a_host():
    out = _run(
        "import sys; from db.tier import PRODUCTION_PRIMARY_HOST as h; print('HOST=' + h)",
        drop=("PROD_DATABASE_URL", "POLISCOPIC_PRODUCTION_DB_HOST"))
    assert f"HOST={PLACEHOLDER}" not in out, "the placeholder fallback is still in use"
    assert f"HOST={PINNED}" in out, out


def test_production_target_carries_the_real_host_without_env():
    out = _run(
        "from scripts.body_code_merge_runtime import PRODUCTION_TARGET as t;"
        "print('HOST=' + t['host']); print('DB=' + t['database']);"
        "print('TIER=' + t['tier'])",
        drop=("PROD_DATABASE_URL", "POLISCOPIC_PRODUCTION_DB_HOST"))
    assert f"HOST={PINNED}" in out, out
    assert PLACEHOLDER not in out
    # database / role / comparison inputs must be untouched
    assert "DB=poliscopic" in out
    assert "TIER=production" in out


def test_pin_agrees_with_the_configured_production_url_host():
    """The pin and the configured production URL must name the same host."""
    out = _run(
        "import os; from urllib.parse import urlsplit;"
        "from dotenv import load_dotenv;"
        "load_dotenv('.env', override=False);"
        "from db.tier import PRODUCTION_PRIMARY_HOST as h;"
        "url=os.environ.get('PROD_DATABASE_URL','');"
        "print('PIN=' + h); print('CFG=' + (urlsplit(url).hostname or ''))",
        drop=("PROD_DATABASE_URL", "POLISCOPIC_PRODUCTION_DB_HOST"))
    pin = [l for l in out.splitlines() if l.startswith("PIN=")]
    cfg = [l for l in out.splitlines() if l.startswith("CFG=")]
    assert pin and cfg, out
    if not cfg[0].split("=", 1)[1]:
        pytest.skip("PROD_DATABASE_URL is intentionally absent from a clean checkout")
    assert pin[0].split("=", 1)[1] == cfg[0].split("=", 1)[1] == PINNED, out


def test_explicit_override_still_wins():
    out = _run(
        "from db.tier import PRODUCTION_PRIMARY_HOST as h; print('HOST=' + h)",
        drop=("PROD_DATABASE_URL",),
        extra={"POLISCOPIC_PRODUCTION_DB_HOST": "override.example.test"})
    assert "HOST=override.example.test" in out, out


def test_the_pinned_host_is_not_a_placeholder_shaped_name():
    assert PLACEHOLDER not in PINNED
    for marker in (".invalid", "example.com", "example.invalid", "localhost"):
        assert marker not in PINNED


# ── an ALTERNATE host must be refused ────────────────────────────────────

def test_alternate_production_host_is_refused_by_the_preflight(tmp_path):
    """Point the configured URL at another host: the preflight must not validate."""
    alternate = "db-pgsql-nyc1-00000-do-user-00000000-0.a.db.ondigitalocean.com"
    env = {k: v for k, v in os.environ.items()
           if k not in ("PROD_DATABASE_URL", "POLISCOPIC_PRODUCTION_DB_HOST")}
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PROD_DATABASE_URL"] = (
        f"postgresql://u:not-a-real-password@{alternate}:25060/poliscopic")
    out = tmp_path / "alt-preflight.json"
    result = subprocess.run(
        [sys.executable, "scripts/ops/production_preflight.py", "--output", str(out)],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    combined = (result.stdout or "") + (result.stderr or "")
    assert result.returncode != 0 or '"status": "VALID"' not in combined, combined
    assert "VALID" not in combined or result.returncode != 0
    # it must refuse by MISMATCH, not by silently accepting the alternate host
    assert "does not match pinned production target" in combined or "refus" in combined.lower()


def test_preflight_refuses_a_development_database_name(tmp_path):
    """Same host, wrong database: still refused."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("PROD_DATABASE_URL", "POLISCOPIC_PRODUCTION_DB_HOST")}
    env["PYTHONPATH"] = str(SCRIPTS)
    env["PROD_DATABASE_URL"] = (
        f"postgresql://u:not-a-real-password@{PINNED}:25060/poliscopic_dev")
    out = tmp_path / "devdb-preflight.json"
    result = subprocess.run(
        [sys.executable, "scripts/ops/production_preflight.py", "--output", str(out)],
        capture_output=True, text=True, env=env, cwd=str(ROOT))
    combined = (result.stdout or "") + (result.stderr or "")
    assert '"status": "VALID"' not in combined, combined
