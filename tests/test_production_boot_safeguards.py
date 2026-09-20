"""Production boot safeguards.

Covers the 2026-09-14 incident class: tier selection must fail closed across
every cross-tier combination, startup diagnostics must never print credentials,
a staged release must be preflighted with the effective systemd environment
before activation, and the service restart policy must be bounded.

Offline only: no network, no database connection, no production access.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from db.tier import (
    DEVELOPMENT,
    DEVELOPMENT_LIKE,
    LOCAL,
    PRODUCTION,
    PRODUCTION_LIKE,
    TEST,
    UNKNOWN,
    TierError,
    classify_target,
    parse_target,
    redacted_url,
    resolve_database_url,
    validate_tier_target,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

DEV_URL = "postgresql://someone:s3cr3t-dev@dev-host.internal:5432/poliscopic_dev"
PROD_URL = (
    "postgresql://someone:test-password@tenant.example.invalid:25060/poliscopic"
)
UNKNOWN_URL = "postgresql://someone:pw@some-host.internal:5432/weird_db"
LOCAL_URL = "sqlite:///tmp/scratch.sqlite"


def _missing_dotenv(tmp_path: Path) -> str:
    return str(tmp_path / "no-such.env")


# ── tier resolution is fail-closed ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("tier", "url", "allowed"),
    [
        (DEVELOPMENT, LOCAL_URL, True),
        (DEVELOPMENT, DEV_URL, True),
        (DEVELOPMENT, PROD_URL, False),
        (DEVELOPMENT, UNKNOWN_URL, False),
        (TEST, LOCAL_URL, True),
        (PRODUCTION, PROD_URL, True),
        (PRODUCTION, DEV_URL, False),
        (PRODUCTION, UNKNOWN_URL, False),
        (PRODUCTION, None, False),
    ],
)
def test_resolver_matrix_fails_closed(tmp_path, tier, url, allowed):
    environ = {"POLISCOPIC_DB_TIER": tier}
    if url is not None:
        environ["DATABASE_URL"] = url
    if allowed:
        resolved, resolved_tier, target = resolve_database_url(
            environ=environ, dotenv_path=_missing_dotenv(tmp_path)
        )
        assert resolved_tier == tier
        assert target.url_class != UNKNOWN
    else:
        with pytest.raises(TierError):
            resolve_database_url(environ=environ, dotenv_path=_missing_dotenv(tmp_path))


def test_production_target_is_never_selected_without_an_explicit_declaration(tmp_path):
    """An undeclared tier derives development, which must refuse a prod target."""
    with pytest.raises(TierError):
        resolve_database_url(
            environ={"DATABASE_URL": PROD_URL}, dotenv_path=_missing_dotenv(tmp_path)
        )


@pytest.mark.parametrize("url", [DEV_URL, PROD_URL])
def test_test_tier_never_reaches_a_shared_database(tmp_path, url):
    resolved, tier, target = resolve_database_url(
        environ={"POLISCOPIC_DB_TIER": TEST, "DATABASE_URL": url},
        dotenv_path=_missing_dotenv(tmp_path),
    )
    assert tier == TEST
    assert resolved.startswith("sqlite:///")
    assert "poliscopic_dev" not in resolved
    assert "ondigitalocean.com" not in resolved


@pytest.mark.parametrize(
    ("tier", "url", "allowed"),
    [
        (DEVELOPMENT, LOCAL_URL, True),
        (DEVELOPMENT, DEV_URL, True),
        (DEVELOPMENT, PROD_URL, False),
        (DEVELOPMENT, UNKNOWN_URL, False),
        (TEST, LOCAL_URL, True),
        (TEST, DEV_URL, False),
        (TEST, PROD_URL, False),
        (PRODUCTION, PROD_URL, True),
        (PRODUCTION, DEV_URL, False),
        (PRODUCTION, LOCAL_URL, False),
        (PRODUCTION, UNKNOWN_URL, False),
    ],
)
def test_tier_target_validation_matrix(tier, url, allowed):
    if classify_target(url) == UNKNOWN:
        # An unclassifiable target is refused at parse time for every tier.
        with pytest.raises(TierError):
            parse_target(url)
        return
    target = parse_target(url)
    if allowed:
        validate_tier_target(tier, target)
    else:
        with pytest.raises(TierError):
            validate_tier_target(tier, target)


def test_unknown_tier_is_refused():
    with pytest.raises(TierError):
        validate_tier_target("staging", parse_target(DEV_URL))


def test_production_tier_requires_an_explicit_url(tmp_path):
    with pytest.raises(TierError) as exc:
        resolve_database_url(
            environ={"POLISCOPIC_DB_TIER": PRODUCTION},
            dotenv_path=_missing_dotenv(tmp_path),
        )
    assert "requires an explicit DATABASE_URL" in str(exc.value)


# ── credentials never appear ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "url",
    [DEV_URL, PROD_URL, UNKNOWN_URL, LOCAL_URL, "not-a-url", "", None],
)
def test_redacted_url_never_leaks_credentials(url):
    output = redacted_url(url)
    assert isinstance(output, str) and output
    for canary in ("s3cr3t-dev", "s3cr3t-prod", "someone", "@"):
        assert canary not in output


def test_startup_diagnostic_never_prints_credentials():
    """Importing the web entry point must not echo the database password."""
    canary = "canary-secret-2b4f19"
    env = dict(os.environ)
    env.update(
        {
            "POLISCOPIC_DB_TIER": DEVELOPMENT,
            "DATABASE_URL": f"postgresql://someone:{canary}@dev-host.internal:5432/poliscopic_dev",
            "PYTHONPATH": f"{REPO_ROOT / 'scripts'}{os.pathsep}{REPO_ROOT}",
        }
    )
    result = subprocess.run(
        [sys.executable, "-c", "import routes"],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    output = (result.stdout or "") + (result.stderr or "")
    assert canary not in output
    assert "Database target:" in output


# ── staged-release preflight ───────────────────────────────────────────────


def _load_preflight():
    spec = importlib.util.spec_from_file_location(
        "preflight_service", REPO_ROOT / "scripts" / "ops" / "preflight_service.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_preflight_parses_a_systemd_environment_line():
    preflight = _load_preflight()
    parsed = preflight.parse_environment(
        'DATABASE_URL=postgresql://user:pw@host:5432/db POLISCOPIC_DB_TIER=production '
        'FLASK_SECRET_KEY="value with spaces"'
    )
    assert parsed["POLISCOPIC_DB_TIER"] == "production"
    assert parsed["FLASK_SECRET_KEY"] == "value with spaces"
    assert parsed["DATABASE_URL"].startswith("postgresql://")


def test_preflight_redacts_url_credentials_and_secret_values():
    preflight = _load_preflight()
    secrets = preflight.collect_secrets(
        {
            "DATABASE_URL": "postgresql://user:pa55word@host:5432/db",
            "NEWSLETTER_TOKEN_SECRET": "tok-8241",
        }
    )
    cleaned = preflight.redact(
        "Traceback: postgresql://user:pa55word@host:5432/db rejected tok-8241", secrets
    )
    assert "pa55word" not in cleaned
    assert "tok-8241" not in cleaned
    assert "***" in cleaned


def test_preflight_redacts_url_credentials_without_a_secret_list():
    preflight = _load_preflight()
    cleaned = preflight.redact("url=postgresql://user:hunter2@host:5432/db", [])
    assert "hunter2" not in cleaned


def test_preflight_fails_closed_when_it_cannot_run(tmp_path):
    """An unreadable unit environment is a setup error, never a false pass."""
    preflight = _load_preflight()
    code = preflight.run_preflight(
        tmp_path, "no-such-unit", "poliscopic", tmp_path / "python", timeout=5
    )
    assert code == 3


# ── rollout design is wired the way the incident requires ─────────────────


def test_restart_policy_is_bounded():
    policy = (REPO_ROOT / "scripts" / "ops" / "systemd" / "poliscopic-restart-policy.conf").read_text()
    assert "StartLimitBurst=" in policy
    assert "StartLimitIntervalSec=" in policy
    assert "RestartSec=" in policy


def test_deploy_release_preflights_before_activating_and_can_roll_back():
    """Invariants preserved across the Batch 2 wrapper/library split.

    The production CONTEXT (host, URLs, rollback naming) lives in the wrapper; the
    MECHANISM and its step ordering live in the sourced library. Both are asserted,
    so the split cannot silently drop an invariant.
    """
    script = (REPO_ROOT / "scripts" / "ops" / "deploy_release.sh").read_text()
    lib = (REPO_ROOT / "scripts" / "ops" / "deploy_release_lib.sh").read_text()

    # Production context stays in the guarded wrapper.
    assert '"https://poliscopic.com/meetings"' in script
    assert "pre-$STAMP.tgz" in script
    assert "sync.sh" not in script
    # ...and the mechanism is sourced from a library with no production defaults.
    assert '. "$LIB"' in script
    assert "poliscopic.com" not in lib

    # Preflight must be ordered before the restart (mechanism, in the library).
    assert lib.index("preflight") < lib.index("$SYSTEMCTL restart")

    # Activation itself must not delete live files; deletion is permitted only in
    # the rollback path so newly activated files do not survive a rollback.
    activation = lib[lib.index("# ── 4. activate"):lib.index("# ── 6. rollback")]
    assert "rsync" in activation
    assert "--delete" not in activation
    rollback = lib[lib.index("# ── 6. rollback"):]
    assert "rsync" in rollback and "--delete" in rollback
