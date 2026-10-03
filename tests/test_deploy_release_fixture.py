"""Fixture tests for the release mechanism, driven through the local-only seam.

The production wrapper (scripts/ops/deploy_release.sh) is interlock-gated and
refuses unconditionally while authorization validation is disabled, so it can no
longer be used to exercise the mechanism. The mechanism itself was extracted into
scripts/ops/deploy_release_lib.sh (a sourced library with no production defaults
and no executable path), and tests drive it through
tests/deploy_release_fixture_adapter.sh, which binds it to a throwaway sandbox and
refuses any path that escapes that sandbox.

Coverage preserved through the seam:
  * successful activation (code swapped, ownership/modes re-asserted, `.env` never
    staged or activated),
  * allowlist preflight + activation (live tree plus overlay),
  * failed health after activation -> automatic restore of the snapshot, non-zero
    exit, previous content back in place.

No production host, service, database, SSH, or external URL is touched.
"""

import os
import shutil
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPLOY_SCRIPT = REPO_ROOT / "scripts" / "ops" / "deploy_release.sh"
DEPLOY_LIB = REPO_ROOT / "scripts" / "ops" / "deploy_release_lib.sh"
ADAPTER = REPO_ROOT / "tests" / "deploy_release_fixture_adapter.sh"


def _fixture(tmp_path: Path) -> dict:
    fix = tmp_path / "sandbox"
    repo = fix / "repo"
    app = fix / "app"
    bin_dir = fix / "bin"
    for path in (
        repo / "scripts" / "ops",
        repo / "routes",
        app / "scripts",
        app / "routes",
        bin_dir,
    ):
        path.mkdir(parents=True, exist_ok=True)

    # Release source: what the mechanism stages and activates. The library must be
    # present beside it because the adapter resolves it from the release source.
    shutil.copy2(DEPLOY_LIB, repo / "scripts" / "ops" / "deploy_release_lib.sh")
    (repo / "app.py").write_text("STATE = 'new'\n")
    (repo / "routes" / "__init__.py").write_text("# new\n")
    (repo / "state").write_text("new\n")
    (repo / ".env").write_text("CANARY_SHOULD_NOT_BE_DEPLOYED=1\n")

    # The live app before the release.
    (app / "app.py").write_text("STATE = 'old'\n")
    (app / "routes" / "__init__.py").write_text("# old\n")
    (app / "state").write_text("old\n")

    # systemctl stub: records calls, always succeeds.
    systemctl = bin_dir / "systemctl"
    systemctl.write_text('#!/usr/bin/env bash\necho "systemctl $*" >> "$FIXTURE_LOG"\nexit 0\n')
    systemctl.chmod(0o755)

    # chown stub: the sandbox has no matching group name, and ownership is
    # asserted separately by the mode checks below.
    chown = bin_dir / "chown"
    chown.write_text('#!/usr/bin/env bash\nexit 0\n')
    chown.chmod(0o755)

    # Health probes: "always 200" and "200 only while the old release is live".
    always_ok = fix / "probe_ok.sh"
    always_ok.write_text('#!/usr/bin/env bash\necho 200\n')
    always_ok.chmod(0o755)

    old_only = fix / "probe_old_only.sh"
    old_only.write_text(
        '#!/usr/bin/env bash\n'
        'if [ "$(cat "$FIXTURE_APP/state" 2>/dev/null)" = "old" ]; then echo 200; else echo 502; fi\n'
    )
    old_only.chmod(0o755)

    return {
        "fix": fix,
        "repo": repo,
        "app": app,
        "bin": bin_dir,
        "probe_ok": always_ok,
        "probe_old_only": old_only,
        "log": fix / "systemctl.log",
    }


def _run(fixture: dict, probe: Path, extra_env: dict | None = None):
    """Run the mechanism through the sandbox-bound test adapter."""
    env = dict(os.environ)
    env.update(
        {
            "PATH": f"{fixture['bin']}{os.pathsep}{env.get('PATH', '')}",
            "POLISCOPIC_SANDBOX": str(fixture["fix"]),
            "ADAPTER_REPO_ROOT": str(fixture["repo"]),
            "ADAPTER_APP_DIR": str(fixture["app"]),
            "ADAPTER_SYSTEMCTL": str(fixture["bin"] / "systemctl"),
            "ADAPTER_HEALTH_PROBE": str(probe),
            "ADAPTER_SERVICE_USER": os.environ.get("USER") or "nobody",
            "ADAPTER_HEALTH_ATTEMPTS": "2",
            "ADAPTER_HEALTH_SLEEP": "0",
            "FIXTURE_APP": str(fixture["app"]),
            "FIXTURE_LOG": str(fixture["log"]),
        }
    )
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(ADAPTER)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
    )


def _mode(path: Path) -> str:
    return oct(path.stat().st_mode & 0o777)


def test_successful_activation_preserves_modes_and_skips_dotenv(tmp_path):
    fixture = _fixture(tmp_path)
    result = _run(fixture, fixture["probe_ok"])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "healthy" in result.stdout

    app = fixture["app"]
    assert (app / "state").read_text().strip() == "new"
    assert "STATE = 'new'" in (app / "app.py").read_text()
    # .env must never be staged or activated.
    assert not (app / ".env").exists()
    # Ownership/mode re-assert: directories traversable, files readable.
    assert _mode(app / "scripts") == "0o755"
    assert _mode(app / "routes") == "0o755"
    assert _mode(app / "app.py") == "0o644"
    # Rollback artifact recorded before activation.
    snapshots = list((app / ".releases").glob("pre-*.tgz"))
    assert len(snapshots) == 1
    # Exactly one restart for a healthy release.
    log = fixture["log"].read_text().strip().splitlines()
    assert len([line for line in log if "restart" in line]) == 1


def test_failed_health_restores_the_previous_release(tmp_path):
    fixture = _fixture(tmp_path)
    # A newly introduced allowlisted module has no entry in the pre-release
    # snapshot, so rollback must delete it rather than merely extracting over it.
    (fixture["repo"] / "new_module.py").write_text("STATE = 'new module'\n")
    result = _run(fixture, fixture["probe_old_only"])
    assert result.returncode == 1
    assert "rolling back" in result.stdout
    assert "rolled back" in result.stdout

    app = fixture["app"]
    # The previous release content is back in place.
    assert (app / "state").read_text().strip() == "old"
    assert "STATE = 'old'" in (app / "app.py").read_text()
    assert not (app / "new_module.py").exists()
    # Activation restart plus rollback restart.
    log = fixture["log"].read_text().strip().splitlines()
    assert len([line for line in log if "restart" in line]) == 2


def test_allowlist_preflight_and_activation_use_live_tree_plus_overlay(tmp_path):
    fixture = _fixture(tmp_path)
    # This local-only file represents unrelated work that must not ride along.
    (fixture["repo"] / "unrelated.py").write_text("SHOULD_NOT_DEPLOY = True\n")
    result = _run(
        fixture, fixture["probe_ok"],
        {"ADAPTER_DEPLOY_PATHS": "app.py"},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "activated allowlist: app.py" in result.stdout
    assert "STATE = 'new'" in (fixture["app"] / "app.py").read_text()
    # The non-allowlisted live route stays old and unrelated local work is absent.
    assert (fixture["app"] / "routes" / "__init__.py").read_text() == "# old\n"
    assert not (fixture["app"] / "unrelated.py").exists()


def test_production_wrapper_refuses_and_cannot_be_driven_by_the_caller(tmp_path):
    """The production path never reaches the mechanism, whatever the caller sets.

    Previously this asserted the wrapper exited 1 for a missing allowlist. It now
    refuses at the interlock with exit 3, and that refusal happens BEFORE the
    mechanism is sourced, so no staging tree and no host contact can occur.
    """
    fixture = _fixture(tmp_path)
    env = dict(os.environ)
    env["POLISCOPIC_DEPLOY_HOST"] = "not-contacted.invalid"
    env["POLISCOPIC_DEPLOY_PATHS"] = "app.py"
    env["POLISCOPIC_CODE_AUTHORIZATION_ID"] = "missing-code-release"
    # Legacy fixture knobs must have no effect on the production wrapper.
    env["POLISCOPIC_FIXTURE_ROOT"] = str(fixture["fix"])
    env["POLISCOPIC_APP_DIR"] = str(fixture["app"])
    # Hermetic: a real authorization under data/release must never be visible here,
    # or this refusal would depend on local state.
    env["POLISCOPIC_RELEASE_DIR"] = str(fixture["fix"] / "no-release")
    env["POLISCOPIC_AUDIT_DIR"] = str(fixture["fix"] / "no-audit")
    result = subprocess.run(
        ["bash", str(DEPLOY_SCRIPT)], capture_output=True, text=True,
        env=env, cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert "REFUSED" in result.stderr
    # Changed 2026-09-21: the refusal is now attributed to the specific gap — no
    # authorization artifact for OP-CODE — rather than the former blanket
    # "issuance is disabled". The leak checks below are the real invariant.
    assert "AUTHORIZATION_MISSING" in result.stderr
    # The fixture app was never touched.
    assert (fixture["app"] / "state").read_text().strip() == "old"
    assert not (fixture["app"] / ".staging").exists()
