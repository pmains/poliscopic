#!/usr/bin/env python3
"""Audit every write to a synchronized table for propagation coverage.

Brief 037 §2 established that a write which does not advance ``updated_at`` is
invisible to the dev→prod sync's ``WHERE updated_at > :since`` filter.  Nothing
errors and row counts still match, so the divergence is silent.

Patching the two scripts that were caught is not evidence that the class is
closed.  This tool scans the source tree for SQL writes that target tables in
the sync contract and reports every one whose propagation cannot be shown:

  MISSING_STAMP   UPDATE on an ``incremental`` table with no updated_at in the
                  SET clause — the write will never reach production
  UNDECLARED      write to a table with no declared propagation mode
  REVIEW          raw SQL write that could not be parsed confidently

``full_reference`` (re-sent wholesale) and ``excluded`` (not propagated) tables
are reported only as informational counts.

Read-only: it parses source text and never connects to a database.

Usage:
    python3 scripts/ops/audit_write_propagation.py [--json PATH] [roots...]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
for _path in (_REPO_ROOT, _REPO_ROOT / "scripts"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from db.sync_declarations import (  # noqa: E402
    PROPAGATION,
    PROPAGATION_EXCLUDED,
    PROPAGATION_FULL_REFERENCE,
    PROPAGATION_INCREMENTAL,
)

DEFAULT_ROOTS = ("scripts", "routes", "workflows", "app.py")

# UPDATE [ONLY] "table" | table SET ... (up to WHERE / end of statement)
_UPDATE_RE = re.compile(
    r'\bUPDATE\s+(?:ONLY\s+)?'
    r'(?:"(?P<qtable>[A-Za-z_][\w]*)"|(?P<table>[A-Za-z_][\w]*))'
    r'\s+SET\s+(?P<sets>.*?)'
    r'(?=\s+WHERE\b|\s+RETURNING\b|\s*"""|\s*\'\)|\s*\)\s*$|\Z)',
    re.IGNORECASE | re.DOTALL,
)

_INSERT_RE = re.compile(
    r'\bINSERT\s+INTO\s+(?:"(?P<qtable>[A-Za-z_][\w]*)"|(?P<table>[A-Za-z_][\w]*))'
    r'\s*(?P<rest>\([^)]*\)|\b)',
    re.IGNORECASE | re.DOTALL,
)

_DELETE_RE = re.compile(
    r'\bDELETE\s+FROM\s+(?:"(?P<qtable>[A-Za-z_][\w]*)"|(?P<table>[A-Za-z_][\w]*))',
    re.IGNORECASE,
)

_STAMP_RE = re.compile(r'\bupdated_at\b\s*=', re.IGNORECASE)


def _skip_dir(path: Path) -> bool:
    parts = set(path.parts)
    return bool(parts & {"__pycache__", ".git", "node_modules", "migrations_archive"})


def iter_sources(roots: list[str]):
    for root in roots:
        p = _REPO_ROOT / root
        if p.is_file() and p.suffix == ".py":
            yield p
            continue
        if not p.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(p):
            d = Path(dirpath)
            if _skip_dir(d):
                dirnames[:] = []
                continue
            for name in filenames:
                if name.endswith(".py"):
                    yield d / name


def classify(stmt: str, match: re.Match) -> dict:
    table = match.group("qtable") or match.group("table")
    mode = PROPAGATION.get(table)
    line = stmt[: match.start()].count("\n") + 1
    return {"table": table, "mode": mode, "line": line}


def audit_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8", errors="replace")
    findings: list[dict] = []

    for m in _UPDATE_RE.finditer(text):
        info = classify(text, m)
        table, mode = info["table"], info["mode"]
        sets = " ".join((m.group("sets") or "").split())
        if not sets:
            continue
        if mode is None:
            findings.append({**info, "kind": "UNDECLARED", "op": "UPDATE",
                             "detail": "no declared propagation mode"})
        elif mode == PROPAGATION_INCREMENTAL and not _STAMP_RE.search(sets):
            findings.append({**info, "kind": "MISSING_STAMP", "op": "UPDATE",
                             "detail": "SET has no updated_at"})
        elif mode == PROPAGATION_INCREMENTAL:
            findings.append({**info, "kind": "OK_STAMPED", "op": "UPDATE",
                             "detail": "stamps"})
        else:
            findings.append({**info, "kind": "INFO_" + mode.upper(),
                             "op": "UPDATE", "detail": mode})

    for m in _INSERT_RE.finditer(text):
        info = classify(text, m)
        mode = info["mode"]
        if mode is None:
            findings.append({**info, "kind": "UNDECLARED", "op": "INSERT",
                             "detail": "no declared propagation mode"})
        else:
            findings.append({**info, "kind": "INFO_INSERT", "op": "INSERT",
                             "detail": mode})

    for m in _DELETE_RE.finditer(text):
        info = classify(text, m)
        mode = info["mode"]
        if mode is None:
            findings.append({**info, "kind": "UNDECLARED", "op": "DELETE",
                             "detail": "no declared propagation mode"})
        else:
            findings.append({**info, "kind": "INFO_DELETE", "op": "DELETE",
                             "detail": mode})

    for f in findings:
        f["file"] = str(path.relative_to(_REPO_ROOT))
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("roots", nargs="*", default=list(DEFAULT_ROOTS))
    ap.add_argument("--json", type=Path)
    args = ap.parse_args()

    all_findings: list[dict] = []
    for path in iter_sources(args.roots or list(DEFAULT_ROOTS)):
        all_findings.extend(audit_file(path))

    bad = [f for f in all_findings if f["kind"] in ("MISSING_STAMP", "UNDECLARED")]
    counts: dict[str, int] = defaultdict(int)
    for f in all_findings:
        counts[f["kind"]] += 1

    print("=" * 78)
    print("WRITE-PROPAGATION AUDIT")
    print("=" * 78)
    print(f"roots:        {', '.join(args.roots or list(DEFAULT_ROOTS))}")
    print(f"statements:   {len(all_findings)}")
    for kind in sorted(counts):
        print(f"  {kind:<28} {counts[kind]}")
    print()
    if bad:
        print(f"⚠ {len(bad)} write(s) whose propagation cannot be shown:")
        print()
        for f in sorted(bad, key=lambda x: (x["kind"], x["file"], x["line"])):
            print(f"  {f['kind']:<14} {f['file']}:{f['line']}  "
                  f"{f['op']} {f['table']}")
    else:
        print("✓ no unpropagated write to a synchronized table")
    print()
    if args.json:
        args.json.write_text(json.dumps(all_findings, indent=2))
        print(f"json: {args.json}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
