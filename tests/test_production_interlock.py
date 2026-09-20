#!/usr/bin/env python3
"""Batch 2 negative tests: the fail-closed production interlock.

Proves two things:

  A. The AUTHORITY (scripts/ops/production_interlock.py) fails closed on missing,
     malformed, unreadable, ambiguous, or stale hold state; refuses every
     production-mutating kind because authorization issuance is DISABLED; refuses
     unknown kinds; treats an environment bypass attempt as a refusal; and never
     treats a hold, a receipt, a commit, or an absent hold as authorization.

  B. The ENTRY POINTS refuse BEFORE invoking anything that can reach production.
     The REAL entry-point files (not rewritten stubs) are executed in a sandbox
     with canary executables on PATH for ssh/scp/rsync/systemctl/psql/curl/wget and
     nohup. If any entry point reached the network, SSH, a DB, rsync, or a restart,
     a canary would fire. Exit 3 with an empty canary log is the pass condition.

No production access, no database, no network. The sandbox makes this so by
construction: the interlock refuses first, and the canaries would record any
attempted breach.
"""

from __future__ import annotations

import ast
import importlib
import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
OPS = _ROOT / "scripts" / "ops"
INTERLOCK = OPS / "production_interlock.py"

if str(OPS) not in sys.path:
    sys.path.insert(0, str(OPS))

interlock = importlib.import_module("production_interlock")

NOW = datetime(2026, 9, 19, 3, 0, 0, tzinfo=timezone.utc)
PAST = (NOW - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
FUTURE = (NOW + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hold_dir(tmp_path, payload=None, raw=None, name="hold.json"):
    d = tmp_path / "interlock"
    d.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        (d / name).write_text(raw)
    elif payload is not None:
        (d / name).write_text(json.dumps(payload))
    return d


def _valid_payload(**over):
    payload = {
        "schema": "production-hold/1",
        "reason": "release blocked pending reviewed gates",
        "not_before": PAST,
        "not_after": FUTURE,
    }
    payload.update(over)
    return payload


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in interlock.FORBIDDEN_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("POLISCOPIC_INTERLOCK_DIR", raising=False)


# ── A. classification ────────────────────────────────────────────────────


def test_all_six_checklist_kinds_are_classified():
    for kind in ("OP-DEV", "OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"):
        assert interlock.classify(kind)["known"], kind


def test_production_kinds_are_mutating_and_readonly_kinds_are_not():
    for kind in ("OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"):
        assert interlock.classify(kind)["mutates_production"] is True, kind
    for kind in ("OP-DEV", "OP-STATUS", "OP-PREFLIGHT"):
        assert interlock.classify(kind)["mutates_production"] is False, kind


def test_unknown_kind_is_not_defaulted_to_safe():
    info = interlock.classify("OP-NOT-A-THING")
    assert info["known"] is False
    assert info["mutates_production"] is None


# ── A. every mutating kind is refused; no hold state can permit it ───────


@pytest.mark.parametrize("kind", ["OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"])
def test_mutating_kind_refused_with_no_hold(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(_hold_dir(tmp_path)))
    verdict = interlock.check(kind, now=NOW)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == interlock.AUTHORIZATION_DISABLED
    assert verdict["authorization_issuance"] == "disabled"


@pytest.mark.parametrize("kind", ["OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"])
def test_mutating_kind_refused_even_with_a_valid_hold(tmp_path, monkeypatch, kind):
    """A hold is a containment condition, NOT a permission."""
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR",
                       str(_hold_dir(tmp_path, payload=_valid_payload())))
    verdict = interlock.check(kind, now=NOW)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == interlock.AUTHORIZATION_DISABLED
    assert verdict["hold"]["state"] == "present"


def test_no_input_can_produce_allowed_for_a_mutating_kind(tmp_path, monkeypatch):
    """Exhaustive: over every hold state, a mutating kind is never ALLOWED."""
    states = {
        "absent": None,
        "malformed": "{not json",
        "empty": "",
        "valid": json.dumps(_valid_payload()),
        "expired": json.dumps(_valid_payload(not_after=PAST, not_before=PAST)),
        "future": json.dumps(_valid_payload(not_before=FUTURE, not_after=FUTURE)),
    }
    for label, raw in states.items():
        d = tmp_path / f"h-{label}"
        d.mkdir()
        if raw is not None:
            (d / "hold.json").write_text(raw)
        monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(d))

        # a stale/malformed receipt must not help either
        (d / "receipt.json").write_text(json.dumps({"kind": "merge-receipt", "ok": True}))

        verdict = interlock.check("OP-RECON", now=NOW)
        assert verdict["status"] == "REFUSED", f"{label} produced {verdict['status']}"
        assert verdict["code"] != None  # noqa: E711


# ── A. hold-state failure modes ──────────────────────────────────────────


def test_missing_hold_state_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(tmp_path / "nope"))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_MISSING


def test_malformed_hold_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(_hold_dir(tmp_path, raw="{broken")))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_MALFORMED


def test_empty_hold_is_reported(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(_hold_dir(tmp_path, raw="   ")))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_MALFORMED


def test_hold_without_reason_is_malformed(tmp_path, monkeypatch):
    payload = _valid_payload()
    payload.pop("reason")
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(_hold_dir(tmp_path, payload=payload)))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_MALFORMED


def test_ambiguous_hold_reported_when_multiple_hold_files(tmp_path, monkeypatch):
    d = _hold_dir(tmp_path, payload=_valid_payload())
    (d / "hold-backup.json").write_text(json.dumps(_valid_payload()))
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(d))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_AMBIGUOUS


def test_inverted_hold_window_is_ambiguous(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR",
                       str(_hold_dir(tmp_path, payload=_valid_payload(
                           not_before=FUTURE, not_after=PAST))))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_AMBIGUOUS


def test_expired_hold_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR",
                       str(_hold_dir(tmp_path, payload=_valid_payload(
                           not_before=PAST, not_after=PAST))))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_STALE


def test_future_hold_is_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR",
                       str(_hold_dir(tmp_path, payload=_valid_payload(
                           not_before=FUTURE, not_after=FUTURE))))
    assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_STALE


def test_unreadable_hold_is_reported(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root ignores file permissions")
    d = _hold_dir(tmp_path, payload=_valid_payload())
    (d / "hold.json").chmod(0o000)
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(d))
    try:
        assert interlock.read_hold(now=NOW)["code"] == interlock.HOLD_UNREADABLE
    finally:
        (d / "hold.json").chmod(0o600)


def test_stale_hold_still_reports_staleness_in_the_refusal(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR",
                       str(_hold_dir(tmp_path, payload=_valid_payload(
                           not_before=PAST, not_after=PAST))))
    verdict = interlock.check("OP-RECON", now=NOW)
    assert verdict["status"] == "REFUSED"
    assert verdict["hold"]["code"] == interlock.HOLD_STALE


# ── A. bypass attempts and unknown input ─────────────────────────────────


@pytest.mark.parametrize("var", ["POLISCOPIC_PRODUCTION_AUTHORIZED",
                                 "POLISCOPIC_FORCE_PROD",
                                 "POLISCOPIC_INTERLOCK_OFF",
                                 "POLISCOPIC_SKIP_INTERLOCK",
                                 "POLISCOPIC_ALLOW_PRODUCTION"])
def test_environment_bypass_attempt_is_refused(tmp_path, monkeypatch, var):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR",
                       str(_hold_dir(tmp_path, payload=_valid_payload())))
    monkeypatch.setenv(var, "1")
    verdict = interlock.check("OP-STATUS", now=NOW)  # even read-only is refused
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == interlock.BYPASS_ATTEMPT
    assert var in verdict["bypass_attempt"]


def test_unknown_kind_refused_by_check(tmp_path, monkeypatch):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(_hold_dir(tmp_path)))
    verdict = interlock.check("OP-WHATEVER", now=NOW)
    assert verdict["status"] == "REFUSED"
    assert verdict["code"] == interlock.UNKNOWN_OPERATION


# ── A. read-only modes stay usable, and only when classified read-only ───


@pytest.mark.parametrize("kind", ["OP-STATUS", "OP-PREFLIGHT", "OP-DEV"])
def test_readonly_kinds_are_allowed(tmp_path, monkeypatch, kind):
    monkeypatch.setenv("POLISCOPIC_INTERLOCK_DIR", str(_hold_dir(tmp_path)))
    verdict = interlock.check(kind, now=NOW)
    assert verdict["status"] == "ALLOWED"
    assert verdict["mutates_production"] is False


def test_only_the_three_readonly_kinds_are_ever_allowed():
    assert set(interlock.READ_ONLY) == {"OP-DEV", "OP-STATUS", "OP-PREFLIGHT"}
    assert set(interlock.PRODUCTION_MUTATING) == {
        "OP-CODE", "OP-SCHEMA", "OP-REPAIR", "OP-RECON", "OP-RESTORE"
    }


# ── A. no escape hatch exists in the module ──────────────────────────────


def test_module_has_no_force_or_override_flag():
    src = INTERLOCK.read_text()
    tree = ast.parse(src)
    flags = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            for arg in node.args:
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    flags.add(arg.value)
    assert "--force" not in flags
    assert "--yes" not in flags
    assert not any("force" in f or "override" in f or "skip" in f for f in flags), flags


def test_module_never_references_a_receipt_as_an_artifact():
    """Historical receipts are not authorization, so the authority must not read one.

    The module's PROSE explains that a historical receipt is never authorization,
    so the bare word legitimately appears. What must not appear is a reference to a
    receipt as a FILE it opens or globs.
    """
    src = INTERLOCK.read_text()
    for token in ('"receipt', "'receipt", "receipt.json", "merge-receipt",
                  "*receipt*", "verify_morning_sync_readiness"):
        assert token not in src, f"interlock references a receipt artifact: {token!r}"
    # the only external artifact it resolves is the hold file
    assert interlock.HOLD_FILENAME == "hold.json"
    assert interlock.read_hold.__name__ == "read_hold"
    assert not hasattr(interlock, "read_receipt")


def test_absence_of_hold_is_explicitly_not_authorization():
    src = interlock.check.__doc__ or ""
    assert "fails closed" in src.lower()
    text = INTERLOCK.read_text()
    assert "absence of a hold is never authorization" in text


# ── B. real entry points refuse before touching production ───────────────

ENTRY_POINTS = [
    ("sync.sh", "sync.sh"),
    ("scripts/sync/sync_prod.sh", "scripts/sync/sync_prod.sh"),
    ("scripts/ops/deploy_release.sh", "scripts/ops/deploy_release.sh"),
    ("scripts/ops/deploy_code.sh", "scripts/ops/deploy_code.sh"),
    ("scripts/ops/reload_gunicorn.sh", "scripts/ops/reload_gunicorn.sh"),
]
CANARY_PATH = ("ssh", "scp", "rsync", "systemctl", "psql", "curl", "wget", "nohup")
CANARY_MARKERS = ("canary", "CANARY")


def _build_sandbox(tmp_path: Path) -> Path:
    """Sandbox mirroring the repo layout, holding the REAL entry-point files."""
    sb = tmp_path / "repo"
    for rel in ("scripts/ops", "scripts/sync", "scripts/db", ".venv/bin"):
        (sb / rel).mkdir(parents=True, exist_ok=True)

    # REAL files, copied verbatim (same bytes as the repository)
    for rel, _ in ENTRY_POINTS:
        shutil.copy2(_ROOT / rel, sb / rel)
    shutil.copy2(INTERLOCK, sb / "scripts/ops/production_interlock.py")
    shutil.copy2(_ROOT / "scripts/db/sync_prod.py", sb / "scripts/db/sync_prod.py")

    # interpreter so `.venv/bin/python` references resolve inside the sandbox
    (sb / ".venv/bin/python").symlink_to(sys.executable)

    # canaries on PATH: any production command reached would be recorded
    shim = sb / "canarybin"
    shim.mkdir()
    log = sb / "CANARY-FIRED.txt"
    for tool in CANARY_PATH:
        p = shim / tool
        p.write_text(f'#!/bin/bash\necho "canary {tool} $*" >> "{log}"\nexit 0\n')
        p.chmod(0o755)
    return sb


def _run(sb: Path, rel: str):
    env = dict(os.environ)
    env["PATH"] = f"{sb / 'canarybin'}:{env['PATH']}"
    env["POLISCOPIC_PYTHON"] = sys.executable
    env.pop("POLISCOPIC_INTERLOCK_DIR", None)
    # make any accidental DB/prod target harmless and unreachable
    env["DATABASE_URL"] = "postgresql://x@127.0.0.1:1/x"
    env["PROD_DATABASE_URL"] = "postgresql://x@127.0.0.1:1/x"
    return subprocess.run(["bash", str(sb / rel)], cwd=str(sb), env=env,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("rel,label", ENTRY_POINTS, ids=[e[1] for e in ENTRY_POINTS])
def test_entry_point_refuses_before_any_production_command(tmp_path, rel, label):
    sb = _build_sandbox(tmp_path)
    result = _run(sb, rel)

    canary = sb / "CANARY-FIRED.txt"
    fired = canary.read_text() if canary.exists() else ""
    assert fired == "", f"{label} invoked a production command:\n{fired}"

    assert result.returncode == 3, (
        f"{label} did not refuse with exit 3 (got {result.returncode})\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    combined = result.stdout + result.stderr
    assert "REFUSED" in combined, f"{label} gave no refusal message"
    assert "AUTHORIZATION_DISABLED" in combined or "production interlock" in combined


def test_entry_point_refusal_is_machine_readable_json(tmp_path):
    sb = _build_sandbox(tmp_path)
    result = _run(sb, "sync.sh")
    payloads = [l for l in (result.stdout + result.stderr).splitlines() if l.startswith("{")]
    assert payloads, "no machine-readable refusal emitted"
    verdict = json.loads(payloads[0])
    assert verdict["status"] == "REFUSED"
    assert verdict["authorization_issuance"] == "disabled"
    assert verdict["schema"] == "production_interlock".replace("_", "-") + "/1"


# ── B. sync_prod.py refuses before resolving a production URL ────────────


def _load_sync_prod():
    spec = importlib.util.spec_from_file_location(
        "sync_prod_under_test", _ROOT / "scripts" / "db" / "sync_prod.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_sync_prod_refuses_before_opening_any_connection(monkeypatch, tmp_path):
    module = _load_sync_prod()

    def boom(*_a, **_k):
        raise AssertionError("the interlock must refuse BEFORE resolving a DB URL")

    monkeypatch.setattr(module, "_resolve_dev_url", boom)
    monkeypatch.setattr(module, "_resolve_prod_url", boom)
    monkeypatch.delenv("POLISCOPIC_INTERLOCK_DIR", raising=False)
    assert module.main(reconcile=True) == 3


def test_sync_prod_classifies_dry_run_readonly_and_mutation_as_recon(monkeypatch):
    module = _load_sync_prod()
    seen = []

    import production_interlock as real_interlock

    def spy(op, entry_point=""):
        seen.append(op)
        return {"status": "REFUSED", "code": "AUTHORIZATION_DISABLED"}

    monkeypatch.setattr(real_interlock, "check", spy)

    def boom(*_a, **_k):
        raise AssertionError("no URL resolution expected")

    monkeypatch.setattr(module, "_resolve_dev_url", boom)
    monkeypatch.setattr(module, "_resolve_prod_url", boom)

    module.main(reconcile_dry_run=True)
    module.main(reconcile=True)
    assert seen == ["OP-STATUS", "OP-RECON"]


# ── B. the checker still has no reachable production entry point ─────────


def test_sync_checker_has_no_production_reference():
    src = (_ROOT / "scripts" / "sync" / "sync_checker.sh").read_text()
    for token in ("sync.sh", "sync_prod", "deploy_release", "production_interlock.py check"):
        assert token not in src, f"checker references {token!r}"


def test_sync_prod_sh_no_longer_uses_readiness_as_a_gate():
    src = (_ROOT / "scripts" / "sync" / "sync_prod.sh").read_text()
    assert "verify_morning_sync_readiness" not in src, (
        "the stale readiness path must not gate production"
    )
    assert "production_interlock.py check" in src
