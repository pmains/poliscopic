#!/usr/bin/env python3
"""Regression: the Surprise CivicClerk sync branch must be executable.

An independent review found the branch was not merely a latent debug concern:

  * ``_sc_time`` was referenced 11 times and bound NOWHERE;
  * ``_surprise_t0`` was used for the total elapsed calculation and bound NOWHERE;
  * the first ``_sc_time.time()`` call happens before ``search_meetings`` runs,

so the branch raised ``NameError`` deterministically whenever it was invoked.

This test proves the correction, statically and dynamically:

  1. no ``_sc_time`` name remains anywhere in the module;
  2. inside the Surprise branch, ``_surprise_t0`` (and any other underscore-prefixed
     timing name) is bound before it is used;
  3. the branch actually REACHES its mocked ``search_meetings`` call without
     ``NameError``.

No live HTTP and no database mutation: ``search_meetings`` is mocked to return an
empty list, so the branch returns before ``get_session()`` and before any write.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import os
import re
import sys

import pytest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (_ROOT, os.path.join(_ROOT, "scripts")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

MAIN_PATH = os.path.join(_ROOT, "scripts", "scraper", "main.py")

# Underscore-prefixed names that are timing instrumentation in this branch.
_TIMING_NAME = re.compile(r"^_(?:sc_time|surprise_t0|t_[A-Za-z0-9_]*|time)$")


def _surprise_branch() -> ast.If:
    tree = ast.parse(open(MAIN_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and "surprise-civicclerk" in ast.dump(node.test):
            return node
    pytest.fail("the Surprise CivicClerk branch was not found in scripts/scraper/main.py")


# ── static: the names are gone / bound before use ────────────────────────


def test_no_sc_time_name_remains():
    source = open(MAIN_PATH, encoding="utf-8").read()
    assert "_sc_time" not in source, (
        "`_sc_time` is still present; the branch must use the module-level `time`"
    )


def test_surprise_branch_binds_timing_names_before_use():
    branch = _surprise_branch()
    # Order by SOURCE POSITION, not traversal order: ast.walk is breadth-first, so
    # walking statement-by-statement mis-orders names inside a loop body (this
    # produced false positives for `_t_mtg`/`_t_evt` on the first attempt).
    events: list[tuple[int, int, str, str]] = []
    for node in ast.walk(branch):
        if isinstance(node, ast.Name) and _TIMING_NAME.match(node.id):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                events.append((node.lineno, node.col_offset, node.id, "store"))
            elif isinstance(node.ctx, ast.Load):
                events.append((node.lineno, node.col_offset, node.id, "load"))
    events.sort()
    first_kind: dict[str, str] = {}
    for _line, _col, name, kind in events:
        first_kind.setdefault(name, kind)
    used_first = sorted(n for n, k in first_kind.items() if k == "load")
    assert events, "the branch performs no timing work at all"
    assert used_first == [], (
        "timing name(s) whose FIRST occurrence is a use rather than a binding: "
        f"{used_first}"
    )


def test_surprise_t0_is_bound_at_the_start_of_the_branch():
    branch = _surprise_branch()
    first = branch.body[0]
    assert isinstance(first, ast.Assign), (
        "the first statement of the Surprise branch should bind the start timestamp"
    )
    targets = {t.id for t in first.targets if isinstance(t, ast.Name)}
    assert "_surprise_t0" in targets, (
        f"expected `_surprise_t0` bound first, found {sorted(targets)}"
    )


# ── dynamic: the branch runs to its mocked search_meetings call ──────────


def test_surprise_branch_reaches_mocked_search_meetings(monkeypatch):
    main_mod = importlib.import_module("scraper.main")

    # A real namespace from the real parser, so every attribute the code touches
    # exists; only `source`/`sync` are overridden to select this branch.
    saved_argv = sys.argv[:]
    sys.argv = ["main.py", "pz", "--sync"]
    try:
        args = main_mod.parse_args()
    finally:
        sys.argv = saved_argv
    args.source = "surprise-civicclerk"
    args.sync = True
    args.bodies = None
    args.year = None
    args.start_date = "2026-01-01"

    monkeypatch.setattr(main_mod, "parse_args", lambda *a, **k: args)
    monkeypatch.setattr(main_mod, "setup_logger", lambda *a, **k: None)

    import db
    monkeypatch.setattr(db, "init_db", lambda *a, **k: None)

    # The branch imports search_meetings from this module at call time, so the
    # source module attribute is the correct patch target.
    civicclerk = importlib.import_module("scraper.platforms.civicclerk")
    calls: list[dict] = []

    def fake_search_meetings(config, start_date=None, **kwargs):
        calls.append({"start_date": start_date})
        return []

    monkeypatch.setattr(civicclerk, "search_meetings", fake_search_meetings)

    # An empty result makes the branch return before get_session() / any write.
    rc = asyncio.run(main_mod.main())

    assert calls, (
        "the Surprise CivicClerk branch never reached its mocked search_meetings "
        "call — it almost certainly still raises NameError"
    )
    assert rc == 0
