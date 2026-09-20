#!/usr/bin/env python3
"""Preflight a staged release using the effective systemd environment.

Runs on the production host (as root) *before* a release is activated:

    preflight_service.py --release-dir /opt/poliscopic/.staging/<stamp> \
        --unit poliscopic --user poliscopic

It answers one question: would this staged code boot under the exact
environment systemd will hand the service?  It:

  1. reads the unit's effective ``Environment=`` and ``WorkingDirectory=`` from
     systemd (the drop-ins are the single source of truth),
  2. runs ``python -c "import app"`` from the staged directory, as the service
     user, with that exact environment,
  3. prints a redacted verdict.

Credentials are never printed: the parsed environment is used, never echoed, and
captured output is scrubbed of every secret value and of any ``user:password@``
pair before it is shown.  Exit codes: 0 bootable, 2 boot failed, 3 preflight
itself could not run.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

DEFAULT_APP_DIR = "/opt/poliscopic"

_SECRET_KEY_HINT = re.compile(r"token|secret|password|passwd|key|credential", re.I)
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)[^/\s:@]+:[^/\s@]+@")
_MAX_DETAIL_LINES = 25


def parse_environment(text: str) -> dict[str, str]:
    """Parse a ``systemctl show -p Environment`` value into a mapping.

    systemd prints space-separated ``KEY=VALUE`` pairs with shell-style quoting
    for values that contain spaces, so ``shlex`` is the correct reader.
    """
    env: dict[str, str] = {}
    for token in shlex.split(text or ""):
        if "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key:
            env[key] = value
    return env


def collect_secrets(env: dict[str, str]) -> list[str]:
    """Values that must never be echoed: explicit secrets and URL passwords."""
    secrets: list[str] = []
    for key, value in env.items():
        if not value:
            continue
        if _SECRET_KEY_HINT.search(key):
            secrets.append(value)
        match = re.search(r"://[^/\s:@]+:([^/\s@]+)@", value)
        if match:
            secrets.append(match.group(1))
        if value.strip():
            secrets.append(value.strip())
    # Longest first so overlapping values are fully masked.
    return sorted({s for s in secrets if len(s) >= 3}, key=len, reverse=True)


def redact(text: str, secrets: list[str]) -> str:
    """Remove credentials and known secret values from captured output."""
    cleaned = _URL_CREDENTIALS.sub(r"\g<scheme>***:***@", text or "")
    for secret in secrets:
        if secret in cleaned:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


def effective_environment(unit: str) -> tuple[dict[str, str], str]:
    """Return ``(environment, working_dir)`` as systemd resolves them for unit."""
    def show(prop: str) -> str:
        result = subprocess.run(
            ["systemctl", "show", unit, f"-p{prop}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"systemctl show {unit} -p{prop} failed")
        for line in result.stdout.splitlines():
            prefix, _, value = line.partition("=")
            if prefix == prop:
                return value
        return ""

    return parse_environment(show("Environment")), show("WorkingDirectory")


def run_preflight(
    release_dir: Path,
    unit: str,
    user: str,
    interpreter: Path,
    timeout: int,
) -> int:
    try:
        service_env, working_dir = effective_environment(unit)
    except Exception as exc:  # pragma: no cover - host-dependent
        print(f"preflight: cannot read {unit} environment: {exc}", file=sys.stderr)
        return 3

    if not release_dir.is_dir():
        print(f"preflight: release directory missing: {release_dir}", file=sys.stderr)
        return 3
    if not interpreter.exists():
        print(f"preflight: interpreter missing: {interpreter}", file=sys.stderr)
        return 3

    env = {
        "PATH": f"{interpreter.parent}:/usr/local/bin:/usr/bin:/bin",
        "HOME": f"/home/{user}",
        "LANG": "C.UTF-8",
    }
    env.update(service_env)

    secrets = collect_secrets(service_env)

    command = [str(interpreter), "-c", "import app"]
    try:
        result = subprocess.run(
            command,
            cwd=str(release_dir),
            env=env,
            user=user,
            group=user,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except PermissionError:
        print("preflight: must run as root to drop to the service user", file=sys.stderr)
        return 3
    except subprocess.TimeoutExpired:
        print(f"preflight: import exceeded {timeout}s")
        return 2
    except Exception as exc:  # pragma: no cover - host-dependent
        print(f"preflight: could not launch interpreter: {exc}", file=sys.stderr)
        return 3

    if result.returncode == 0:
        print(
            f"preflight: OK - {release_dir} imports app under {unit} environment "
            f"(cwd {working_dir or '(unset)'}, user {user})"
        )
        return 0

    detail = redact((result.stderr or result.stdout or "").strip(), secrets)
    tail = "\n".join(detail.splitlines()[-_MAX_DETAIL_LINES:])
    print(f"preflight: FAILED - staged release cannot boot (exit {result.returncode})")
    if tail:
        print(tail)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Boot-preflight a staged release")
    parser.add_argument("--release-dir", required=True)
    parser.add_argument("--unit", default="poliscopic")
    parser.add_argument("--user", default="poliscopic")
    parser.add_argument(
        "--python",
        default=str(Path(DEFAULT_APP_DIR) / ".venv" / "bin" / "python"),
        help="interpreter used for the import check (default: the deployed venv)",
    )
    parser.add_argument("--timeout", type=int, default=120)
    args = parser.parse_args(argv)

    return run_preflight(
        Path(args.release_dir).resolve(),
        args.unit,
        args.user,
        Path(args.python),
        args.timeout,
    )


if __name__ == "__main__":
    sys.exit(main())
