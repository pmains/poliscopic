"""Child DATABASE_URL handoff regressions.

The defect: the runner passed ``str(engine.url)`` — which masks the password as a
literal ``***`` — so the child could never authenticate.  These tests pin the fix
and, equally importantly, pin the *containment*: the real credential is rendered at
exactly one place (the child environment) and must not appear on any other surface.

The secret used here is synthetic and lives only in this test.
"""

from __future__ import annotations

import json

import pytest
from sqlalchemy import make_url
from sqlalchemy.engine import URL

from scripts.entities import event_normalize_child_contract as contract

#: Synthetic credential containing characters that must survive URL encoding.
RAW_PASSWORD = "p@ss:wo/rd#%21"
RAW_USER = "user@example"
SYNTHETIC_URL = (
    f"postgresql://user%40example:p%40ss%3Awo%2Frd%23%2521@dev-host.internal:5432/poliscopic_dev"
)


@pytest.fixture
def url():
    return make_url(SYNTHETIC_URL)


def _encoded_secret(url) -> str:
    """The percent-encoded form actually present in a rendered URL."""
    rendered = url.render_as_string(hide_password=False)
    return rendered.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]


# -- (1) the child receives a usable, unmasked URL ----------------------------

def test_child_environment_receives_a_usable_url(url):
    env, _ = contract.child_environment(url, {"PATH": "/usr/bin"})
    assert "DATABASE_URL" in env
    reparsed = make_url(env["DATABASE_URL"])
    assert reparsed.password == url.password
    assert reparsed.username == url.username
    assert reparsed.host == url.host and reparsed.database == url.database
    assert "***" not in env["DATABASE_URL"]


def test_the_masked_rendering_would_have_been_unusable(url):
    """Guards the regression itself: str(url) is what broke the child."""
    masked = str(url)
    assert "***" in masked
    reparsed = make_url(masked)
    assert reparsed.password != url.password


def test_delivery_is_environment_only(url):
    env, contract_doc = contract.child_environment(url, {"PATH": "/usr/bin"})
    assert env["DATABASE_URL"].startswith("postgresql://")
    # the contract records key *names*, never values
    assert contract_doc["env_keys_bound"] == ["DATABASE_URL", "POLISCOPIC_DB_TIER", "PGOPTIONS"]
    # the contract names the keys but never carries their values
    assert _encoded_secret(url) not in json.dumps(contract_doc)
    assert url.password not in json.dumps(contract_doc)


# -- (2) every serialized / redacted surface masks it -------------------------

def test_contract_and_serializations_never_contain_the_credential(url):
    env, contract_doc = contract.child_environment(url, {"PATH": "/usr/bin"})
    encoded = _encoded_secret(url)
    surfaces = {
        "contract json": json.dumps(contract_doc, sort_keys=True, default=str),
        "contract repr": repr(contract_doc),
        "target_redacted": contract_doc["target_redacted"],
        "redacted_url_text": contract.redacted_url_text(url),
    }
    for name, text in surfaces.items():
        assert url.password not in text, name
        assert encoded not in text, name
    assert env["DATABASE_URL"] != contract_doc["target_redacted"]


def test_classification_never_uses_the_unmasked_form(url):
    """Classification must be driven by the redacted text."""
    assert "***" in contract.redacted_url_text(url)
    assert contract.classify_target(contract.redacted_url_text(url)) == (
        contract.classify_target(url.render_as_string(hide_password=True)))


# -- (3) special characters round-trip ---------------------------------------

@pytest.mark.parametrize("password", [
    "p@ss:wo/rd#%21", "plain", "with spaces", "sym+bols=eq&amp", "unicode-\u00e9\u00fc",
])
def test_special_characters_round_trip_safely(password):
    url = URL.create("postgresql", username=RAW_USER, password=password,
                     host="dev-host.internal", port=5432, database="poliscopic_dev")
    env, _ = contract.child_environment(url, {})
    reparsed = make_url(env["DATABASE_URL"])
    assert reparsed.password == url.password
    assert reparsed.username == url.username
    assert reparsed.host == "dev-host.internal"
    assert reparsed.database == "poliscopic_dev"
    assert url.password not in env["DATABASE_URL"] or url.password == parsed_plain(env)


def parsed_plain(env):
    """The decoded password the child would actually present."""
    return make_url(env["DATABASE_URL"]).password


# -- (4) parent and child target identity stay equal --------------------------

def test_parent_and_child_target_identity_match(url):
    _, contract_doc = contract.child_environment(url, {})
    assert contract_doc["url_class"] == contract.classify_target(
        contract.redacted_url_text(url))
    assert url.host in contract_doc["target_redacted"]
    assert url.database in contract_doc["target_redacted"]


# -- (5) no credential leaks on child failure ---------------------------------

def test_failure_surfaces_carry_no_credential(url):
    """The command and failure strings the runner records are secret-free."""
    from scripts.entities.event_normalize_child_contract import PRODUCER_COMMAND

    encoded = _encoded_secret(url)
    rendered_command = " ".join(PRODUCER_COMMAND)
    assert url.password not in rendered_command
    assert encoded not in rendered_command
    for message in ("child exited 1", "producer timed out after 600s"):
        assert url.password not in message and encoded not in message


def test_timeout_exception_text_carries_no_credential(url):
    import subprocess

    from scripts.entities.event_normalize_child_contract import PRODUCER_COMMAND

    error = subprocess.TimeoutExpired(cmd=list(PRODUCER_COMMAND), timeout=5)
    encoded = _encoded_secret(url)
    assert url.password not in str(error)
    assert encoded not in str(error)


# -- (6) wrong target / refusal never spawns ----------------------------------

@pytest.mark.parametrize("bad_url", [
    "postgresql://u:***@db.b.db.ondigitalocean.com:25060/poliscopic",   # production host
    "postgresql://u:***@host.example:5432/some_other_db",              # unknown target
    "mysql://u:***@host.example/db",                                   # unsupported dialect
])
def test_refused_targets_raise_and_never_produce_an_environment(bad_url):
    with pytest.raises(contract.ChildContractError):
        contract.child_environment(bad_url, {})


def test_production_target_is_refused_before_any_spawn(url):
    calls: list = []

    def spawn(*args, **kwargs):
        calls.append(args)
        raise AssertionError("spawn must not be reached")

    with pytest.raises(contract.ChildContractError):
        contract.child_environment("postgresql://u:***@db.b.db.ondigitalocean.com/db", {})
    assert calls == []


def test_development_target_is_accepted_and_bound(url):
    env, contract_doc = contract.child_environment(url, {})
    assert contract_doc["database_enforced_read_only"] is True
    assert "default_transaction_read_only=on" in env["PGOPTIONS"]
    assert env["POLISCOPIC_DB_TIER"] == "development"
