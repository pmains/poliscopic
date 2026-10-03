#!/usr/bin/env python3
"""Boundary tests for the standing daily-sync APPROVAL review surface.

The surface must be unreachable from the main app, refuse without a session-bound
token, bind approval to the exact proposal digest, write one immutable mode-0600
record, and execute nothing.
"""

from __future__ import annotations

import ast
import json
import os
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest
from flask import Flask

from scripts.ops import standing_authorization_proposal as proposal_mod

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def review(tmp_path, monkeypatch):
    """A minimal app holding only the approval blueprint, plus a real proposal."""
    proposal = proposal_mod.build_proposal(
        code_paths=proposal_mod.DEFAULT_CODE_PATHS,
        rollback_owner="Pete Mains",
        now=datetime(2026, 9, 23, tzinfo=timezone.utc),
    )
    proposal_file = tmp_path / "proposal.json"
    proposal_file.write_text(json.dumps(proposal))
    records = tmp_path / "records"

    monkeypatch.setenv("POLISCOPIC_STANDING_PROPOSAL", str(proposal_file))
    monkeypatch.setenv("POLISCOPIC_STANDING_APPROVAL_DIR", str(records))
    monkeypatch.setenv("POLISCOPIC_STANDING_SYNC_APPROVAL_UI", "1")

    from routes.standing_sync_approval import standing_sync_approval_bp

    app = Flask(__name__, template_folder=str(ROOT / "templates"))
    app.secret_key = "test-secret"
    app.register_blueprint(standing_sync_approval_bp)
    app.testing = True

    class Fixture:
        pass

    fixture = Fixture()
    fixture.app = app
    fixture.client = app.test_client()
    fixture.proposal = proposal
    fixture.proposal_file = proposal_file
    fixture.records = records
    return fixture


def _token(client) -> str:
    """Mint a session token by loading the page once."""
    client.get("/ops/standing-sync-approval/")
    with client.session_transaction() as session:
        return session["standing_sync_approval_csrf"]


def _approve(client, token, **overrides):
    form = {
        "csrf_token": token,
        "reviewer": "Pete Mains",
        "confirm_digest": "",
        "acknowledge_recurring": "yes",
    }
    form.update(overrides)
    return client.post("/ops/standing-sync-approval/approve", data=form)


# ── not reachable from the main application ──────────────────────────────


def test_blueprint_is_not_registered_in_the_main_app():
    source = (ROOT / "routes" / "__init__.py").read_text()
    assert "standing_sync_approval" not in source, (
        "the approval blueprint must not be mounted by the main application")


def test_route_404s_when_not_opted_in(review, monkeypatch):
    monkeypatch.delenv("POLISCOPIC_STANDING_SYNC_APPROVAL_UI", raising=False)
    assert review.client.get("/ops/standing-sync-approval/").status_code == 404
    assert review.client.post("/ops/standing-sync-approval/approve").status_code == 404


# ── CSRF ─────────────────────────────────────────────────────────────────


def test_post_without_token_is_refused(review):
    assert _approve(review.client, "", confirm_digest=review.proposal["digest"]
                    ).status_code == 403


def test_post_with_wrong_token_is_refused(review):
    _token(review.client)
    assert _approve(review.client, "not-the-token",
                    confirm_digest=review.proposal["digest"]).status_code == 403


def test_token_from_another_session_is_refused(review):
    token = _token(review.client)
    with review.app.test_client() as other:
        assert _approve(other, token,
                        confirm_digest=review.proposal["digest"]).status_code == 403


def test_token_is_consumed_on_success(review):
    token = _token(review.client)
    assert _approve(review.client, token,
                    confirm_digest=review.proposal["digest"]).status_code == 200
    # The same token must not work twice.
    assert _approve(review.client, token,
                    confirm_digest=review.proposal["digest"]).status_code == 403


# ── approval is bound to the exact digest ────────────────────────────────


def test_wrong_digest_is_refused(review):
    token = _token(review.client)
    response = _approve(review.client, token, confirm_digest="deadbeef")
    assert response.status_code == 400
    assert b"Type the proposal digest exactly" in response.data


def test_unnamed_reviewer_is_refused(review):
    token = _token(review.client)
    response = _approve(review.client, token, reviewer="",
                        confirm_digest=review.proposal["digest"])
    assert response.status_code == 400
    assert b"must name its author" in response.data


def test_missing_acknowledgement_is_refused(review):
    token = _token(review.client)
    response = _approve(review.client, token, acknowledge_recurring="",
                        confirm_digest=review.proposal["digest"])
    assert response.status_code == 400
    assert b"acknowledge" in response.data


# ── the record ───────────────────────────────────────────────────────────


def _record_file(review) -> Path:
    files = list(review.records.glob("*.approval.json"))
    assert len(files) == 1, f"expected exactly one record, found {files}"
    return files[0]


def test_success_writes_one_immutable_mode_0600_record(review):
    token = _token(review.client)
    assert _approve(review.client, token,
                    confirm_digest=review.proposal["digest"]).status_code == 200
    path = _record_file(review)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"expected 0600, got {oct(mode)}"
    record = json.loads(path.read_text())
    assert record["proposal_digest"] == review.proposal["digest"]
    assert record["reviewer"] == "Pete Mains"
    assert record["acknowledged_recurring_upserts_only_while_gates_pass"] is True


def test_record_authorizes_and_executes_nothing(review):
    token = _token(review.client)
    _approve(review.client, token, confirm_digest=review.proposal["digest"])
    record = json.loads(_record_file(review).read_text())
    assert record["executed"] is False
    assert record["authorizes_operation"] is False
    assert "confers no execution" in record["effect"]
    assert record["kind"] == "standing-authorization-approval-record"


def test_existing_record_is_never_replaced(review):
    token = _token(review.client)
    assert _approve(review.client, token,
                    confirm_digest=review.proposal["digest"]).status_code == 200
    before = _record_file(review).read_text()
    # A second session approves the same digest.
    with review.app.test_client() as second:
        token2 = _token(second)
        response = _approve(second, token2,
                            confirm_digest=review.proposal["digest"],
                            reviewer="Someone Else")
    assert response.status_code == 409
    assert _record_file(review).read_text() == before


def test_record_never_becomes_an_authorization_artifact(review):
    token = _token(review.client)
    _approve(review.client, token, confirm_digest=review.proposal["digest"])
    assert not list(review.records.glob("authorization.json"))
    assert "release" not in review.records.name


# ── authorization-shaped proposals are refused ───────────────────────────


@pytest.mark.parametrize("field,value", [
    ("executable", True),
    ("approver", "someone"),
    ("authorization", {"mode": "standing"}),
])
def test_authorization_shaped_proposal_is_refused(review, field, value):
    proposal = dict(review.proposal)
    proposal[field] = value
    proposal["digest"] = proposal_mod.proposal_digest(proposal)
    review.proposal_file.write_text(json.dumps(proposal))
    response = review.client.get("/ops/standing-sync-approval/")
    assert response.status_code == 409


def test_tampered_proposal_is_refused(review):
    proposal = dict(review.proposal)
    proposal["max_uses"] = 99999
    review.proposal_file.write_text(json.dumps(proposal))
    assert review.client.get("/ops/standing-sync-approval/").status_code == 409


def test_live_code_drift_is_refused_before_review_or_approval(review, monkeypatch):
    """A valid stored digest must not conceal code changed after generation."""
    from routes import standing_sync_approval as route

    monkeypatch.setattr(
        route.proposal_mod,
        "code_hashes",
        lambda paths: {path: "0" * 64 for path in paths},
    )
    assert review.client.get("/ops/standing-sync-approval/").status_code == 409
    assert review.client.post("/ops/standing-sync-approval/approve").status_code in (403, 409)
    assert not list(review.records.glob("*.approval.json"))


# ── the page, and the launcher ───────────────────────────────────────────


def test_page_explains_in_plain_english_before_technical_bindings(review):
    response = review.client.get("/ops/standing-sync-approval/")
    body = response.get_data(as_text=True)
    plain = body.index("What you are being asked to allow")
    technical = body.index("Technical bindings")
    assert plain < technical, "plain English must come before technical bindings"
    for expected in ("Maximum successful runs", "Exact tables in scope",
                     "Mandatory gates", "Permitted actions",
                     "What this does", "400", "365"):
        assert expected in body, f"review page omits {expected!r}"


def test_page_shows_the_exact_scope_and_gates(review):
    body = review.client.get("/ops/standing-sync-approval/").get_data(as_text=True)
    for table in review.proposal["scope"]:
        assert table in body
    for gate in proposal_mod.GATE_IDS:
        assert gate in body


def test_route_never_spawns_or_syncs():
    """The route must contain no execution path of any kind."""
    tree = ast.parse((ROOT / "routes" / "standing_sync_approval.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    for forbidden in ("subprocess", "sqlalchemy"):
        assert forbidden not in imported, f"route imports {forbidden!r}"
    source = (ROOT / "routes" / "standing_sync_approval.py").read_text()
    for token in ("sync_prod", "run_sync", "create_engine"):
        assert token not in source, f"route references {token!r}"


def test_launcher_binds_loopback_with_debug_and_reloader_off():
    from scripts.ops import standing_sync_approval_serve as launcher

    assert launcher.HOST == "127.0.0.1"
    assert launcher.DEBUG is False
    assert launcher.USE_RELOADER is False
    kwargs = launcher.run_kwargs([])
    assert kwargs["host"] == "127.0.0.1" and kwargs["debug"] is False
    assert kwargs["use_reloader"] is False


def test_launcher_is_the_only_opt_in_path():
    source = (ROOT / "scripts" / "ops" /
              "standing_sync_approval_serve.py").read_text()
    assert "ENABLE_FLAG" in source and "ENABLED_VALUE" in source
