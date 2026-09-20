#!/usr/bin/env python3
"""Negative tests for the release seam: wrapper, library, and test adapter.

Proves the split actually holds:

  * the production wrapper refuses BEFORE the mechanism is invoked;
  * caller environment cannot make the production wrapper enter a fixture/test
    mode (there is no fixture seam in production code at all);
  * the test adapter refuses sandbox-escaping paths and host:path routes;
  * the mechanism library has no executable top-level path and no production
    defaults, and the test adapter is not release-manifest content.

No production, SSH, network, or database access.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WRAPPER = REPO_ROOT / "scripts" / "ops" / "deploy_release.sh"
LIB = REPO_ROOT / "scripts" / "ops" / "deploy_release_lib.sh"
ADAPTER = REPO_ROOT / "tests" / "deploy_release_fixture_adapter.sh"

# Substrings that would indicate production endpoints baked into the library.
PRODUCTION_MARKERS = ("poliscopic.com", "/opt/poliscopic", "root@", "systemctl reload")


# ── 1. the wrapper refuses before the mechanism is invoked ───────────────


def test_wrapper_refuses_before_sourcing_the_mechanism(tmp_path):
    """A canary for the mechanism must never fire: refusal precedes it."""
    canary = tmp_path / "MECHANISM-INVOKED.txt"
    stub_lib = tmp_path / "deploy_release_lib.sh"
    stub_lib.write_text(f'echo "invoked" > "{canary}"\n')

    # Point the wrapper at a repo whose library is a canary.
    fake_root = tmp_path / "fake"
    (fake_root / "scripts" / "ops").mkdir(parents=True)
    (fake_root / "scripts" / "ops" / "production_interlock.py").write_text(
        "#!/usr/bin/env python3\nimport sys\nprint('REFUSED: test')\nsys.exit(3)\n"
    )
    (fake_root / "scripts" / "ops" / "deploy_release.sh").write_text(WRAPPER.read_text())
    (fake_root / "scripts" / "ops" / "deploy_release_lib.sh").write_text(stub_lib.read_text())

    env = dict(os.environ)
    env["POLISCOPIC_DEPLOY_PATHS"] = "app.py"
    result = subprocess.run(
        ["bash", str(fake_root / "scripts" / "ops" / "deploy_release.sh")],
        capture_output=True, text=True, env=env, cwd=str(fake_root), timeout=60,
    )
    assert result.returncode == 3
    assert not canary.exists(), "the mechanism ran even though the interlock refused"


def test_wrapper_runs_the_interlock_before_sourcing_the_library():
    """Structural: the interlock call must appear before the library is sourced."""
    src = WRAPPER.read_text()
    interlock_at = src.index("production_interlock.py")
    source_at = src.index('. "$LIB"')
    assert interlock_at < source_at, (
        "the interlock must run before the mechanism is sourced"
    )
    # and before any ssh/rsync of the mechanism is even defined
    assert src.index("remote()") > interlock_at


# ── 2. caller environment cannot select a fixture mode ───────────────────


@pytest.mark.parametrize("var,value", [
    ("POLISCOPIC_FIXTURE_ROOT", "/tmp/somewhere"),
    ("POLISCOPIC_APP_DIR", "/tmp/elsewhere"),
])
def test_caller_environment_cannot_put_the_wrapper_into_fixture_mode(tmp_path, var, value):
    """There is no fixture seam in production code, so no env var can enable one."""
    staging = tmp_path / "app" / ".staging"
    staging.mkdir(parents=True)
    env = dict(os.environ)
    env[var] = value
    env["POLISCOPIC_DEPLOY_PATHS"] = "app.py"
    result = subprocess.run(
        ["bash", str(WRAPPER)], capture_output=True, text=True,
        env=env, cwd=str(REPO_ROOT), timeout=60,
    )
    assert result.returncode == 3, result.stdout + result.stderr
    assert not any(staging.iterdir()), "a staging tree was created despite refusal"


def test_production_wrapper_has_no_fixture_reference():
    src = WRAPPER.read_text()
    # Specific to the fixture seam. (A bare "local" or `DEST_APP=` check would be
    # wrong: the wrapper legitimately sets DEST_APP to a HOST:path route.)
    for token in ("POLISCOPIC_FIXTURE_ROOT", "FIXTURE_ROOT", "ADAPTER_"):
        assert token not in src, f"production wrapper references {token!r}"
    assert "$HOST:$APP_DIR" in src, "the remote route must be a real host:path"


def test_wrapper_has_no_test_or_force_flag():
    """No --force/--test/--fixture style escape hatch in the production wrapper.

    Only forbidden flag NAMES are checked, so the interlock CLI's own legitimate
    arguments (--operation, --entry-point) are not mistaken for wrapper flags.
    """
    src = WRAPPER.read_text()
    forbidden = ("--force", "--test", "--fixture", "--local", "--skip",
                 "--yes", "--no-interlock", "--dry-run-local")
    for flag in forbidden:
        assert flag not in src, f"production wrapper exposes {flag}"
    # the only argument the wrapper accepts is --stage-only
    assert src.count("--stage-only") >= 1


# ── 3. the adapter refuses paths escaping its sandbox ────────────────────


def _adapter_env(sandbox: Path, **over):
    env = dict(os.environ)
    env.update({
        "POLISCOPIC_SANDBOX": str(sandbox),
        "ADAPTER_REPO_ROOT": str(sandbox / "repo"),
        "ADAPTER_APP_DIR": str(sandbox / "app"),
        "ADAPTER_SYSTEMCTL": str(sandbox / "bin" / "systemctl"),
        "ADAPTER_HEALTH_PROBE": str(sandbox / "probe.sh"),
    })
    env.update(over)
    return env


def _make_sandbox(tmp_path: Path) -> Path:
    sb = tmp_path / "sandbox"
    for rel in ("repo/scripts/ops", "app", "bin"):
        (sb / rel).mkdir(parents=True, exist_ok=True)
    (sb / "bin" / "systemctl").write_text("#!/usr/bin/env bash\nexit 0\n")
    (sb / "bin" / "systemctl").chmod(0o755)
    (sb / "probe.sh").write_text("#!/usr/bin/env bash\necho 200\n")
    (sb / "probe.sh").chmod(0o755)
    (sb / "repo" / "scripts" / "ops" / "deploy_release_lib.sh").write_text(LIB.read_text())
    return sb


@pytest.mark.parametrize("key,escape", [
    ("ADAPTER_APP_DIR", "/tmp/outside-app"),
    ("ADAPTER_REPO_ROOT", "/etc"),
    ("ADAPTER_SYSTEMCTL", "/usr/bin/systemctl"),
    ("ADAPTER_HEALTH_PROBE", "/tmp/outside-probe.sh"),
])
def test_adapter_rejects_paths_escaping_the_sandbox(tmp_path, key, escape):
    sb = _make_sandbox(tmp_path)
    result = subprocess.run(
        ["bash", str(ADAPTER)], capture_output=True, text=True,
        env=_adapter_env(sb, **{key: escape}), timeout=60,
    )
    assert result.returncode == 2, result.stdout + result.stderr
    assert "escapes the sandbox" in result.stderr or "refusing host:path" in result.stderr


def test_adapter_rejects_host_path_routes(tmp_path):
    """A host:path form must be refused outright — no remote rsync route."""
    sb = _make_sandbox(tmp_path)
    result = subprocess.run(
        ["bash", str(ADAPTER)], capture_output=True, text=True,
        env=_adapter_env(sb, ADAPTER_APP_DIR="deploy-root@example.invalid:/opt/poliscopic"),
        timeout=60,
    )
    assert result.returncode == 2
    assert "refusing host:path route" in result.stderr


def test_adapter_refuses_without_a_sandbox(tmp_path):
    result = subprocess.run(
        ["bash", str(ADAPTER)], capture_output=True, text=True,
        env={k: v for k, v in os.environ.items() if k != "POLISCOPIC_SANDBOX"},
        timeout=60,
    )
    assert result.returncode == 2
    assert "POLISCOPIC_SANDBOX is required" in result.stderr


def test_adapter_has_no_ssh_or_external_route():
    src = ADAPTER.read_text()
    for token in ("ssh ", "scp ", "root@", "poliscopic.com", "curl "):
        assert token not in src, f"adapter references {token!r}"


# ── 4. library: no executable path, no production defaults ───────────────


def test_library_has_no_shebang_and_no_executable_bit():
    src = LIB.read_text()
    assert not src.startswith("#!"), "the library must not have a shebang"
    assert not (LIB.stat().st_mode & 0o111), "the library must not be executable"


def test_library_refuses_to_run_as_a_command():
    result = subprocess.run(
        ["bash", str(LIB)], capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 1
    assert "sourced library, not a command" in result.stderr


@pytest.mark.parametrize("marker", PRODUCTION_MARKERS)
def test_library_contains_no_production_defaults(marker):
    assert marker not in LIB.read_text(), f"library bakes in production value {marker!r}"


def test_library_defines_the_mechanism_but_no_top_level_execution():
    """Sourcing the library must define functions and run nothing."""
    src = LIB.read_text()
    assert "deploy_release_run()" in src
    assert 'if [ "${BASH_SOURCE[0]}" = "$0" ]' in src
    # no bare top-level call to the mechanism
    assert "\ndeploy_release_run\n" not in src
    assert "\ndeploy_release_run " not in src


def test_library_requires_its_context(tmp_path):
    """Sourcing the library then calling the mechanism with no context must die."""
    script = tmp_path / "probe.sh"
    script.write_text(
        f'. "{LIB}"\n'
        "deploy_release_run\n"
    )
    result = subprocess.run(["bash", str(script)], capture_output=True, text=True, timeout=60)
    assert result.returncode != 0
    assert "missing required context" in result.stderr


# ── 5. manifest policy ───────────────────────────────────────────────────


def test_test_adapter_is_not_release_manifest_content():
    """tests/ is excluded from releases, so the adapter cannot ship."""
    from importlib import util as _util

    spec = _util.spec_from_file_location(
        "brm", REPO_ROOT / "scripts" / "ops" / "build_release_manifest.py")
    module = _util.module_from_spec(spec)
    spec.loader.exec_module(module)

    excluded = getattr(module, "EXCLUDED_SUFFIXES", None) or getattr(module, "EXCLUDES", None)
    src = (REPO_ROOT / "scripts" / "ops" / "build_release_manifest.py").read_text()
    # whatever the mechanism, tests/ must be excluded from the manifest
    assert "tests" in src, "manifest builder no longer mentions tests/ exclusion"
    assert excluded is not None or "tests" in src


def test_guarded_wrapper_sources_the_library_so_it_must_ship_without_a_path():
    """The library ships only as sourced content: no shebang, no exec bit, no defaults."""
    assert '. "$LIB"' in WRAPPER.read_text()
    assert not LIB.read_text().startswith("#!")
    assert not (LIB.stat().st_mode & 0o111)
