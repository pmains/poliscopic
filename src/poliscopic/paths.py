"""Application path discovery for checkout and installed-package execution."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

PROJECT_ROOT_ENV = "POLISCOPIC_PROJECT_ROOT"


class ProjectRootError(RuntimeError):
    """An explicitly configured application root is unusable."""


def _looks_like_checkout(path: Path) -> bool:
    """Return whether *path* has the minimal supported application shape."""
    return (path / "app.py").is_file() and (path / "scripts" / "db").is_dir()


def application_root(
    *,
    environ: Mapping[str, str] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    source_file: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Resolve the application checkout root without guessing in site-packages.

    Resolution order is explicit configuration, the source checkout containing
    this module, then the current working directory. An installed library used
    outside an application checkout returns ``None``; callers must then rely on
    explicit environment configuration rather than searching arbitrary parent
    directories for a ``.env`` file.
    """
    env = os.environ if environ is None else environ
    current = Path.cwd() if cwd is None else Path(cwd)

    configured = str(env.get(PROJECT_ROOT_ENV, "")).strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute():
            root = current / root
        root = root.resolve()
        if not root.is_dir():
            raise ProjectRootError(
                f"{PROJECT_ROOT_ENV} does not name an existing directory: {root}"
            )
        return root

    module_path = Path(__file__ if source_file is None else source_file).resolve()
    # ``src/poliscopic/paths.py`` -> repository root. In an installed wheel this
    # points above site-packages and intentionally fails the checkout-shape test.
    source_root = module_path.parents[2]
    if _looks_like_checkout(source_root):
        return source_root

    current = current.resolve()
    if _looks_like_checkout(current):
        return current
    return None


def application_dotenv_path(**kwargs: object) -> Path | None:
    """Return the checkout ``.env`` path, or ``None`` outside a checkout."""
    root = application_root(**kwargs)
    return None if root is None else root / ".env"
