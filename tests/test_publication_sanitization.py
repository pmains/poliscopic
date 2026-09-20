from __future__ import annotations

import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent

BANNED_LITERALS = (
    "/Users/" + "pmains",
    "C:" + r"\Users\Peter",
    "100.91." + "173.66",
    "db-" + "pgsql-",
    "root@" + "poliscopic.com",
    "poliscopic@" + "poliscopic.com",
    "windows-" + "tailscale",
    "DESKTOP-" + "251LIVN",
    "peter.mains@" + "gmail.com",
    "01a" + "07e",
)


def test_tracked_text_does_not_publish_operational_identifiers() -> None:
    tracked = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout.split(b"\0")

    violations: list[str] = []
    for raw_path in tracked:
        if not raw_path:
            continue
        relative = raw_path.decode("utf-8", errors="surrogateescape")
        path = REPO_ROOT / relative
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for literal in BANNED_LITERALS:
            if literal in text:
                violations.append(f"{relative}: {literal}")

    assert not violations, "publication-sensitive literals found:\n" + "\n".join(violations)
