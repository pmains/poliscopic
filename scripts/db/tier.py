#!/usr/bin/env python3
"""Authoritative database-tier selection and target validation.

One mechanism, used by every entry point that resolves a database.  It exists
because the previous resolver let an environment variable outrank the declared
tier and never asked what the resolved target actually *was*: a duplicated or
reordered ``DATABASE_URL`` could point development tooling at production without
anything objecting.

Design rules
------------
* **The tier is data, not order.**  A target is classified from its own host and
  database name, then checked against the declared tier.  Whether a duplicate key
  wins first or last cannot change the safety decision, because every occurrence
  is examined and conflicts are refused outright.
* **Fail closed.**  An unknown tier, a production-like target under a development
  tier, a development target under a production tier, or a target that cannot be
  classified all raise :class:`TierError`.  There is no silent default.
* **The test tier never reaches a shared database.**  It only accepts an
  explicitly local/test URL, otherwise it mints a throwaway SQLite file.
* **Credentials never leave this module.**  Only :meth:`Target.redacted` is ever
  printable, and it is built from parsed parts, never from the raw URL.

This module performs no I/O beyond reading the dotenv *file text* to detect
duplicate keys.  It opens no database connection and makes no network call.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import urlsplit

__all__ = [
    "DEVELOPMENT",
    "KNOWN_TIERS",
    "PRODUCTION",
    "PRODUCTION_HOST_MARKERS",
    "PRODUCTION_PRIMARY_HOST",
    "TARGET_CLASSES",
    "TEST",
    "TierError",
    "Target",
    "classify_target",
    "detect_conflicting_definitions",
    "duplicate_keys",
    "new_test_database_path",
    "normalize_tier",
    "parse_target",
    "redacted_url",
    "require_role_url",
    "resolve_database_url",
    "resolve_role_url",
    "validate_tier_target",
]

DEVELOPMENT = "development"
TEST = "test"
PRODUCTION = "production"

KNOWN_TIERS = (DEVELOPMENT, TEST, PRODUCTION)

#: Target classes a resolved URL can fall into.
LOCAL = "local"
DEVELOPMENT_LIKE = "development"
PRODUCTION_LIKE = "production"
UNKNOWN = "unknown"
TARGET_CLASSES = (LOCAL, DEVELOPMENT_LIKE, PRODUCTION_LIKE, UNKNOWN)

#: Host suffixes that identify the production estate.  Matching is on the parsed
#: host only, so a credential or path can never influence the decision.
PRODUCTION_HOST_MARKERS = ("ondigitalocean.com", "poliscopic.com")

# Exact managed production host used by operation-specific target bindings.
# It is configuration, not public source metadata.
def _configured_production_host() -> str:
    explicit = os.environ.get("POLISCOPIC_PRODUCTION_DB_HOST", "").strip()
    if explicit:
        return explicit
    raw_url = os.environ.get("PROD_DATABASE_URL", "").strip()
    return (urlsplit(raw_url).hostname or "") if raw_url else "prod-db.example.invalid"


PRODUCTION_PRIMARY_HOST = _configured_production_host()

#: Exact database names reserved for production.
PRODUCTION_DATABASE_NAMES = frozenset({"poliscopic"})

#: Database-name suffixes that identify a development database.
DEVELOPMENT_DATABASE_SUFFIXES = ("_dev", "_development", "_local")

#: Environment keys this module understands.  Credentials are never among them.
TIER_ENV = "POLISCOPIC_DB_TIER"
URL_ENV = "DATABASE_URL"
PROD_URL_ENV = "PROD_DATABASE_URL"

_DOTENV_KEYS = (TIER_ENV, URL_ENV, PROD_URL_ENV)

_KEY_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


class TierError(RuntimeError):
    """The database tier or target cannot be resolved safely."""


@dataclass(frozen=True)
class Target:
    """A parsed, classified database target.  Never carries credentials."""

    url_class: str
    dialect: str
    host: str | None
    port: int | None
    database: str

    def redacted(self) -> str:
        """A printable form: tier class, host, port and database only."""
        location = self.host or "(local)"
        if self.port is not None:
            location = f"{location}:{self.port}"
        return f"{self.url_class} {self.dialect} {location}/{self.database}"


def normalize_tier(value: str | None) -> str:
    """Normalize a declared tier, refusing unknown values.

    Missing means "not declared": the caller derives it, and the derived tier is
    still validated.  Anything unrecognised raises rather than defaulting.
    """
    if value is None or not str(value).strip():
        raise TierError("database tier is not declared")
    tier = str(value).strip().lower()
    if tier not in KNOWN_TIERS:
        raise TierError(
            f"unknown database tier {value!r}; known tiers are {list(KNOWN_TIERS)}"
        )
    return tier


def _looks_production_host(host: str | None) -> bool:
    if not host:
        return False
    host = host.lower().rstrip(".")
    return any(host == m or host.endswith("." + m) for m in PRODUCTION_HOST_MARKERS)


def classify_target(url: str) -> str:
    """Classify a URL into a target class, or ``unknown`` when unsure.

    Classification reads only the parsed dialect, host and database path.  It
    deliberately never inspects credentials.
    """
    if not isinstance(url, str) or not url.strip():
        return UNKNOWN
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()

    if scheme.startswith("sqlite"):
        return LOCAL
    if scheme not in ("postgresql", "postgresql+psycopg2", "postgres", "postgresql+psycopg"):
        return UNKNOWN

    database = (parts.path or "").lstrip("/")
    if not database or not parts.hostname:
        return UNKNOWN
    if _looks_production_host(parts.hostname):
        return PRODUCTION_LIKE
    if database.lower() in PRODUCTION_DATABASE_NAMES:
        return PRODUCTION_LIKE
    if database.lower().endswith(DEVELOPMENT_DATABASE_SUFFIXES):
        return DEVELOPMENT_LIKE
    return UNKNOWN


def parse_target(url: str) -> Target:
    """Parse a URL into a :class:`Target`, failing closed when unclassifiable."""
    url_class = classify_target(url)
    if url_class is UNKNOWN:
        raise TierError(
            "database target cannot be classified safely; refusing to continue"
        )
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    database = (parts.path or "").lstrip("/")
    if scheme.startswith("sqlite"):
        return Target(url_class, "sqlite", None, None, database or "(memory)")
    return Target(
        url_class=url_class,
        dialect="postgresql",
        host=(parts.hostname or "").lower() or None,
        port=parts.port,
        database=database,
    )


def validate_tier_target(tier: str, target: Target) -> None:
    """Raise unless ``target`` is legitimate for ``tier``.

    Development accepts a local or development-like target and refuses anything
    production-like or unclassifiable.  Test accepts only local.  Production
    accepts only production-like, so a mis-pointed production run cannot silently
    write to a development database.
    """
    if tier not in KNOWN_TIERS:
        raise TierError(f"unknown database tier {tier!r}")
    if tier == TEST and target.url_class != LOCAL:
        raise TierError(
            f"test tier requires a local target, refused {target.redacted()}"
        )
    if tier == DEVELOPMENT and target.url_class not in (LOCAL, DEVELOPMENT_LIKE):
        raise TierError(
            f"development tier refused a {target.url_class} target: {target.redacted()}"
        )
    if tier == PRODUCTION and target.url_class != PRODUCTION_LIKE:
        raise TierError(
            f"production tier requires a production target, refused {target.redacted()}"
        )


def duplicate_keys(dotenv_path: str | os.PathLike[str] | None) -> dict[str, list[int]]:
    """Line numbers of every repeated key in a dotenv file (names only, no values)."""
    found: dict[str, list[int]] = {}
    text = _read_dotenv(dotenv_path)
    if text is None:
        return found
    for number, line in enumerate(text.splitlines(), start=1):
        match = _KEY_LINE.match(line)
        if match:
            found.setdefault(match.group(1), []).append(number)
    return {key: lines for key, lines in found.items() if len(lines) > 1}


def detect_conflicting_definitions(
    dotenv_path: str | os.PathLike[str] | None,
    keys: Iterable[str] = _DOTENV_KEYS,
) -> dict[str, list[int]]:
    """Repeated keys whose values disagree, as ``{key: line_numbers}``.

    Identical repeats are tolerated; only genuinely conflicting repeats are
    returned.  Because every occurrence is inspected, the result does not depend
    on which line a parser would have honoured.
    """
    text = _read_dotenv(dotenv_path)
    if text is None:
        return {}
    wanted = set(keys)
    seen: dict[str, list[tuple[int, str]]] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        match = _KEY_LINE.match(line)
        if match and match.group(1) in wanted:
            seen.setdefault(match.group(1), []).append((number, match.group(2).strip()))
    conflicts: dict[str, list[int]] = {}
    for key, entries in seen.items():
        if len({value for _, value in entries}) > 1:
            conflicts[key] = [number for number, _ in entries]
    return conflicts


def _read_dotenv(dotenv_path: str | os.PathLike[str] | None) -> str | None:
    if not dotenv_path:
        return None
    path = Path(dotenv_path)
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")


def _default_dotenv_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / ".env"


def resolve_role_url(
    role: str, url: str | None, *, label: str | None = None
) -> Target:
    """Validate one URL against a named role, for multi-target tooling.

    Sync and deployment tooling needs both a development and a production URL at
    once.  Each is validated against its own role, so a swapped pair is refused
    rather than silently syncing the wrong direction.  Contacts nothing.
    """
    if role not in KNOWN_TIERS:
        raise TierError(f"unknown role {role!r}")
    if url is None or not str(url).strip():
        raise TierError(f"{label or role} URL is not set")
    target = parse_target(str(url))
    if target.url_class == UNKNOWN:
        raise TierError(f"{label or role} target cannot be classified safely")
    if role == PRODUCTION and target.url_class != PRODUCTION_LIKE:
        raise TierError(
            f"{label or role} must be a production target, got {target.redacted()}"
        )
    if role in (DEVELOPMENT, TEST) and target.url_class == PRODUCTION_LIKE:
        raise TierError(
            f"{label or role} must not be a production target, got {target.redacted()}"
        )
    return target


def redacted_url(url: str | os.PathLike[str] | None) -> str:
    """A printable, credential-free description of a database URL.

    Never raises and never echoes credentials: only the dialected dialect, host,
    port and database name are emitted.  This is the single helper every startup
    diagnostic must use, so a raw URL can never reach a log by habit.
    """
    if url is None or not str(url).strip():
        return "(not set)"
    try:
        return parse_target(str(url)).redacted()
    except TierError:
        try:
            parts = urlsplit(str(url).strip())
        except ValueError:
            return "(unparseable target)"
        dialect = (parts.scheme or "?").split("+")[0]
        location = parts.hostname or "(local)"
        if parts.port is not None:
            location = f"{location}:{parts.port}"
        database = (parts.path or "").lstrip("/") or "(none)"
        return f"unclassified {dialect} {location}/{database}"


def require_role_url(
    role: str, url: str | None, *, label: str | None = None
) -> str:
    """Validate a URL for a role and return it, ready for engine construction.

    Thin wrapper over :func:`resolve_role_url` so a caller can validate and then
    build an engine without restating any rule.  Returning the URL unchanged keeps
    the existing connection semantics.
    """
    resolve_role_url(role, url, label=label)
    return str(url)


def new_test_database_path() -> str:
    """A secure, uniquely-named SQLite path for the test tier.

    ``tempfile.mkstemp`` creates the file atomically with owner-only permissions,
    which avoids the predictable-name race that ``mktemp`` has.  The file's
    lifetime is owned by the operating system's temporary directory, exactly as
    before; this module does not delete it, because a test process may still hold
    open connections to it while the interpreter is shutting down.
    """
    handle, path = tempfile.mkstemp(suffix=".sqlite", prefix="poliscopic-test-")
    os.close(handle)
    return path


def resolve_database_url(
    *,
    environ: Mapping[str, str] | None = None,
    dotenv_path: str | os.PathLike[str] | None = None,
) -> tuple[str, str, Target]:
    """Resolve ``(url, tier, target)`` under fail-closed rules.

    Declared tier wins; an undeclared tier is derived from the presence of an
    explicit URL.  The resolved target is then validated against the tier, so a
    production target can never be selected by development tooling regardless of
    variable ordering or duplication.
    """
    env = os.environ if environ is None else environ
    path = _default_dotenv_path() if dotenv_path is None else dotenv_path

    conflicts = detect_conflicting_definitions(path)
    if conflicts:
        names = ", ".join(sorted(conflicts))
        raise TierError(
            f"conflicting duplicate definitions in the environment file: {names}; "
            "refusing to guess which value was intended"
        )

    declared = env.get(TIER_ENV)
    url_env = env.get(URL_ENV)

    if declared is not None and str(declared).strip():
        tier = normalize_tier(declared)
    elif url_env:
        tier = DEVELOPMENT          # derived, then validated below
    else:
        tier = DEVELOPMENT

    if tier == PRODUCTION:
        # The production tier is available only when it is explicitly declared
        # and only for a production-like target.  Development and test tooling
        # never declare it, so the guard against accidental production access is
        # unchanged; the public web service declares it deliberately, and
        # multi-target sync tooling still validates each role separately via
        # resolve_role_url.
        prod_url = url_env or env.get(PROD_URL_ENV)
        if not prod_url:
            raise TierError(
                f"production tier requires an explicit {URL_ENV}; refusing to continue"
            )
        target = parse_target(str(prod_url))
        validate_tier_target(PRODUCTION, target)
        return str(prod_url), tier, target

    if tier == TEST:
        if url_env and classify_target(str(url_env)) == LOCAL:
            url = str(url_env)
        else:
            url = f"sqlite:///{new_test_database_path()}"
    else:
        if not url_env:
            raise TierError(
                f"{URL_ENV} is not set; set an explicit development database URL "
                "(this resolver has no silent fallback)"
            )
        url = str(url_env)

    target = parse_target(url)
    validate_tier_target(tier, target)
    return url, tier, target
