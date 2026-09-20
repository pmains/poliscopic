#!/usr/bin/env python3
"""``event_normalize_child_contract.py`` — the child process safety contract.

Owns four things, all policy, none duplicated elsewhere:

1. **PGOPTIONS normalisation.** The child's read-only guarantee is carried in
   ``PGOPTIONS``, so the value must contain **exactly one** authoritative
   assignment of ``default_transaction_read_only``.  Duplicate assignments,
   weakening values, quoted or otherwise ambiguous syntax, and dangling ``-c``
   all fail closed; unrelated options survive only when the whole string parses
   unambiguously.
2. **Target binding.** The child's ``DATABASE_URL`` is **pinned to the already
   verified parent target**, so the child cannot silently connect somewhere else
   via ambient environment.  Only the redacted target is ever recorded.
3. **Honest enforcement claim.** ``default_transaction_read_only`` is
   session-settable: it protects against accidental writes, it is *not* a
   security boundary.  :data:`GUARANTEE` records that precisely.
4. **Pinned execution identity.** The executable, script and working directory
   are absolute, so the fingerprinted files are exactly the files executed.

No database is contacted here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"
for _path in (_REPO_ROOT, _SCRIPTS_DIR):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db.tier import LOCAL, PRODUCTION_LIKE, UNKNOWN, classify_target, parse_target  # noqa: E402

__all__ = [
    "GUARANTEE",
    "PRODUCER_COMMAND",
    "PRODUCER_CWD",
    "PRODUCER_EXECUTABLE",
    "PRODUCER_SCRIPT",
    "READ_ONLY_GUC",
    "READ_ONLY_PGOPTION",
    "ChildContractError",
    "child_database_url",
    "redacted_url_text",
    "ChildResult",
    "child_environment",
    "child_read_only_contract",
    "compose_pgoptions",
    "default_spawn",
    "parse_pgoptions",
]

READ_ONLY_GUC = "default_transaction_read_only"
READ_ONLY_PGOPTION = f"{READ_ONLY_GUC}=on"

#: Values that weaken the guarantee.
_WEAKENING_VALUES = ("off", "false", "0", "no")
#: Values that assert it.
_STRENGTHENING_VALUES = ("on", "true", "1", "yes")

#: The accurate guarantee, recorded as evidence rather than asserted in prose.
GUARANTEE: dict[str, str] = {
    "mechanism": "PGOPTIONS sets default_transaction_read_only=on for the child session",
    "strength": (
        "session-settable protection against accidental writes; NOT a security "
        "boundary and not non-bypassable"
    ),
    "residual_trust_boundary": (
        "the producer is trusted code; a process that resets the GUC, supplies its "
        "own startup options, or uses a different connection could still write. "
        "Parent-side enforcement is a separate, independent guard."
    ),
    "read_only_role": (
        "no distinct read-only credential is configured or verifiable in this "
        "repository, so none is claimed"
    ),
}

#: Pinned execution identity: absolute, so the fingerprinted files are the run files.
PRODUCER_EXECUTABLE = str(_REPO_ROOT / ".venv" / "bin" / "python")
PRODUCER_SCRIPT = str(_REPO_ROOT / "scripts" / "entities" / "event_normalize.py")
PRODUCER_CWD = str(_REPO_ROOT)
PRODUCER_COMMAND = (PRODUCER_EXECUTABLE, "-u", PRODUCER_SCRIPT, "--dry-run", "--force")


class ChildContractError(RuntimeError):
    """The child process cannot be given a fail-closed contract."""


class ChildResult:
    """A finished child process: streams kept separate, exit code recorded."""

    __slots__ = ("exit_code", "stdout", "stderr")

    def __init__(self, exit_code: int, stdout: str, stderr: str) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr


def parse_pgoptions(raw: str | None) -> dict[str, str]:
    """Parse a ``PGOPTIONS`` string into GUC assignments, failing closed.

    Accepts ``-c name=value`` and ``--name=value``.  Rejects quotes (shell
    metacharacters make the value ambiguous), tokens without ``=``, empty names
    or values, dangling ``-c``, and **any duplicate assignment** -- a repeated GUC
    is exactly the ambiguity that let an earlier ``on`` be overridden by a later
    ``off``.
    """
    text = str(raw or "").strip()
    if not text:
        return {}
    if '"' in text or "'" in text or ";" in text or "\\" in text:
        raise ChildContractError("refusing to parse quoted or compound PGOPTIONS")

    tokens = text.split()
    options: dict[str, str] = {}
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token == "-c":
            index += 1
            if index >= len(tokens):
                raise ChildContractError("dangling -c in PGOPTIONS")
            token = tokens[index]
        elif token.startswith("-c") and len(token) > 2:
            token = token[2:]
        if token.startswith("--"):
            token = token[2:]
        if "=" not in token:
            raise ChildContractError(f"PGOPTIONS token {token!r} is not name=value")
        name, value = token.split("=", 1)
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ChildContractError(f"malformed PGOPTIONS token {token!r}")
        if name in options:
            raise ChildContractError(
                f"duplicate PGOPTIONS assignment for {name!r}; refusing to guess"
            )
        options[name] = value
        index += 1
    return options


def compose_pgoptions(raw: str | None) -> str:
    """Return a ``PGOPTIONS`` string with exactly one read-only assignment.

    An inherited assignment is validated (weakening or unrecognised values are
    refused) and normalised to ``on``; unrelated options are preserved as
    explicit ``-c name=value`` pairs.
    """
    options = parse_pgoptions(raw)
    current = options.get(READ_ONLY_GUC)
    if current is not None:
        lowered = current.lower()
        if lowered in _WEAKENING_VALUES:
            raise ChildContractError(
                f"refusing to spawn: {READ_ONLY_GUC} is set to a weakening value"
            )
        if lowered not in _STRENGTHENING_VALUES:
            raise ChildContractError(
                f"unrecognised value for {READ_ONLY_GUC}: {current!r}"
            )
    options[READ_ONLY_GUC] = "on"
    return " ".join(f"-c {name}={value}" for name, value in options.items())


def redacted_url_text(url: Any) -> str:
    """Textual URL safe for classification, evidence and messages.

    ``str(url)`` masks the password already, but routing every non-secret use
    through this one helper makes the masking explicit and greppable rather than
    incidental.
    """
    if hasattr(url, "render_as_string"):
        return url.render_as_string(hide_password=True)
    return str(url)


def child_database_url(url: Any) -> str:
    """The child's ``DATABASE_URL``: the *only* place the real credential is rendered.

    Built from the SQLAlchemy URL's own supported unmasked rendering (or from the
    plain string when a caller supplies one).  This value is handed to the child
    through its environment and nowhere else: it must never reach a log, a plan or
    result artifact, an exception message, a receipt, or a command argument.
    """
    if hasattr(url, "render_as_string"):
        return url.render_as_string(hide_password=False)
    return str(url)


def child_read_only_contract(engine_url: Any, environ: Mapping[str, str]) -> dict[str, Any]:
    """The enforced contract for the child, derived from the verified URL only."""
    url_class = classify_target(redacted_url_text(engine_url))
    if url_class == PRODUCTION_LIKE:
        raise ChildContractError("refusing to spawn the producer against a production target")
    if url_class == UNKNOWN:
        raise ChildContractError("refusing to spawn the producer against an unclassifiable target")

    target = parse_target(redacted_url_text(engine_url))
    base = {
        "url_class": url_class,
        "target_redacted": target.redacted(),
        "guarantee": dict(GUARANTEE),
    }
    if url_class == LOCAL:
        return {
            **base,
            "enforced_by": "isolated-local-target",
            "database_enforced_read_only": False,
            "dialect": "sqlite",
            "pgoptions": None,
            "env_keys_bound": ["DATABASE_URL"],
            "note": "SQLite target: no PostgreSQL-only options are applied",
        }
    composed = compose_pgoptions(environ.get("PGOPTIONS"))
    return {
        **base,
        "enforced_by": "postgresql default_transaction_read_only",
        "database_enforced_read_only": True,
        "dialect": "postgresql",
        "pgoptions": composed,
        "env_keys_bound": ["DATABASE_URL", "POLISCOPIC_DB_TIER", "PGOPTIONS"],
        "note": (
            "PostgreSQL rejects writes on the child session, and the child is pinned "
            "to the verified development target"
        ),
    }


def _env_overrides(engine_url: Any, contract: Mapping[str, Any]) -> dict[str, str]:
    """The secret-bearing environment overrides.

    Kept deliberately **out** of the contract: the contract is persisted as
    evidence, and ``DATABASE_URL`` carries credentials.  Only key *names* appear in
    the contract (``env_keys_bound``).
    """
    overrides = {"DATABASE_URL": child_database_url(engine_url)}
    if contract.get("pgoptions"):
        overrides["PGOPTIONS"] = str(contract["pgoptions"])
        overrides["POLISCOPIC_DB_TIER"] = "development"
    return overrides


def child_environment(
    engine_url: Any, environ: Mapping[str, str]
) -> tuple[dict[str, str], dict[str, Any]]:
    """The child environment (target-bound) plus the redacted contract justifying it."""
    contract = child_read_only_contract(engine_url, environ)
    env = dict(environ)
    env.update(_env_overrides(engine_url, contract))
    return env, contract


def default_spawn(
    command: Sequence[str],
    *,
    timeout: float,
    env: Mapping[str, str] | None = None,
    cwd: str | None = None,
) -> ChildResult:
    """Run the producer with streams separated, bounded, in a pinned directory."""
    completed = subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=dict(env) if env is not None else None,
        cwd=cwd or PRODUCER_CWD,
    )
    return ChildResult(completed.returncode, completed.stdout or "", completed.stderr or "")
