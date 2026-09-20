"""Database tier selection tests — fail-closed, order-independent, credential-safe.

Pure configuration tests: no database connection, no network, and no writes to
the real ``.env`` (every fixture writes its own temporary file).
"""

from __future__ import annotations

import pytest

from db.tier import (
    DEVELOPMENT,
    LOCAL,
    PRODUCTION,
    PRODUCTION_LIKE,
    TEST,
    UNKNOWN,
    TierError,
    classify_target,
    detect_conflicting_definitions,
    duplicate_keys,
    normalize_tier,
    parse_target,
    resolve_database_url,
    resolve_role_url,
    validate_tier_target,
)

DEV_URL = "postgresql://user:secret@dev-host.internal:5432/poliscopic_dev"
PROD_URL = (
    "postgresql://user:secret@tenant.example.ondigitalocean.com:25060/poliscopic"
)
# production host, development database name
PROD_HOST_DEV_DB = (
    "postgresql://user:secret@db.b.db.ondigitalocean.com:25060/poliscopic_dev"
)
# development host, production database name
DEV_HOST_PROD_DB = "postgresql://user:secret@dev-host.internal:5432/poliscopic"


def env(**overrides):
    """A clean environment with no tier and no database URL."""
    return dict(overrides)


def missing_dotenv(tmp_path):
    return str(tmp_path / "no-such.env")


# -- classification -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (DEV_URL, "development"),
        (PROD_URL, "production"),
        (PROD_HOST_DEV_DB, "production"),      # production host wins
        (DEV_HOST_PROD_DB, "production"),      # production database name wins
        ("sqlite:///tmp/x.sqlite", LOCAL),
        ("mysql://u:p@host/db", UNKNOWN),
        ("not-a-url", UNKNOWN),
        ("", UNKNOWN),
        ("postgresql://u:p@host/weird_db", UNKNOWN),
    ],
)
def test_classify_target(url, expected):
    assert classify_target(url) == expected


def test_production_like_host_with_development_database_is_still_production():
    target = parse_target(PROD_HOST_DEV_DB)
    assert target.url_class == PRODUCTION_LIKE


def test_development_like_host_with_production_database_is_still_production():
    """A dev-looking host must not smuggle a production database name through."""
    assert parse_target(DEV_HOST_PROD_DB).url_class == PRODUCTION_LIKE


def test_unclassifiable_url_fails_closed():
    with pytest.raises(TierError):
        parse_target("mysql://u:p@host/db")


def test_malformed_url_fails_closed():
    for bad in ("", "   ", "://", "postgresql://", "postgresql://host/", "nonsense"):
        with pytest.raises(TierError):
            parse_target(bad)


# -- tier normalization -------------------------------------------------------


@pytest.mark.parametrize("tier", [DEVELOPMENT, TEST, PRODUCTION])
def test_known_tiers_normalize(tier):
    assert normalize_tier(tier) == tier
    assert normalize_tier(tier.upper()) == tier
    assert normalize_tier(f"  {tier}  ") == tier


def test_missing_tier_is_not_silently_defaulted():
    with pytest.raises(TierError):
        normalize_tier(None)
    with pytest.raises(TierError):
        normalize_tier("   ")


@pytest.mark.parametrize("tier", ["staging", "prod", "dev", "sqlite", "0"])
def test_unknown_tier_fails_closed(tier):
    with pytest.raises(TierError):
        normalize_tier(tier)


# -- tier/target agreement, both directions -----------------------------------


def test_matching_targets_are_accepted():
    validate_tier_target(DEVELOPMENT, parse_target(DEV_URL))
    validate_tier_target(DEVELOPMENT, parse_target("sqlite:///tmp/x.sqlite"))
    validate_tier_target(TEST, parse_target("sqlite:///tmp/x.sqlite"))
    validate_tier_target(PRODUCTION, parse_target(PROD_URL))


def test_development_tier_rejects_a_production_target():
    with pytest.raises(TierError) as exc:
        validate_tier_target(DEVELOPMENT, parse_target(PROD_URL))
    assert "development tier refused" in str(exc.value)


def test_production_tier_rejects_a_development_target():
    with pytest.raises(TierError) as exc:
        validate_tier_target(PRODUCTION, parse_target(DEV_URL))
    assert "requires a production target" in str(exc.value)


def test_test_tier_rejects_anything_but_local():
    with pytest.raises(TierError) as exc:
        validate_tier_target(TEST, parse_target(DEV_URL))
    assert "requires a local target" in str(exc.value)


def test_unknown_tier_cannot_be_validated():
    with pytest.raises(TierError):
        validate_tier_target("staging", parse_target(DEV_URL))


# -- duplicate and conflicting definitions ------------------------------------


def write_env(tmp_path, body):
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return str(path)


def test_duplicate_identical_definitions_are_tolerated(tmp_path):
    path = write_env(tmp_path, f"DATABASE_URL={DEV_URL}\nDATABASE_URL={DEV_URL}\n")

    assert detect_conflicting_definitions(path) == {}
    assert duplicate_keys(path)["DATABASE_URL"] == [1, 2]


def test_conflicting_duplicates_are_refused(tmp_path):
    path = write_env(tmp_path, f"DATABASE_URL={DEV_URL}\nDATABASE_URL={PROD_URL}\n")

    conflicts = detect_conflicting_definitions(path)
    assert conflicts == {"DATABASE_URL": [1, 2]}

    with pytest.raises(TierError) as exc:
        resolve_database_url(environ=env(), dotenv_path=path)
    assert "conflicting duplicate definitions" in str(exc.value)


def test_reversed_order_is_also_refused(tmp_path):
    """Ordering must not decide the outcome: both orders fail closed."""
    forward = write_env(tmp_path, f"DATABASE_URL={DEV_URL}\nDATABASE_URL={PROD_URL}\n")
    with pytest.raises(TierError):
        resolve_database_url(environ=env(), dotenv_path=forward)

    reversed_path = tmp_path / "reversed.env"
    reversed_path.write_text(f"DATABASE_URL={PROD_URL}\nDATABASE_URL={DEV_URL}\n", encoding="utf-8")
    with pytest.raises(TierError):
        resolve_database_url(environ=env(), dotenv_path=str(reversed_path))


def test_first_wins_and_last_wins_cannot_change_the_safety_decision(tmp_path):
    """Identical repeats cannot change the outcome; conflicting repeats cannot resolve."""
    identical = write_env(tmp_path, f"DATABASE_URL={DEV_URL}\nDATABASE_URL={DEV_URL}\n")
    url, tier, target = resolve_database_url(
        environ={**env(), "DATABASE_URL": DEV_URL}, dotenv_path=identical
    )
    assert tier == DEVELOPMENT
    assert url == DEV_URL
    assert target.url_class == "development"

    # A conflicting file cannot resolve at all, so no parser ordering can pick
    # the production value on the caller's behalf -- not even when the process
    # environment already holds the safe development URL.
    conflicting = tmp_path / "conflicting.env"
    conflicting.write_text(
        f"DATABASE_URL={DEV_URL}\nDATABASE_URL={PROD_URL}\n", encoding="utf-8"
    )
    with pytest.raises(TierError):
        resolve_database_url(
            environ={**env(), "DATABASE_URL": DEV_URL}, dotenv_path=str(conflicting)
        )


def test_conflicting_tier_definitions_are_refused(tmp_path):
    path = write_env(
        tmp_path, "POLISCOPIC_DB_TIER=development\nPOLISCOPIC_DB_TIER=production\n"
    )
    with pytest.raises(TierError):
        resolve_database_url(environ=env(), dotenv_path=path)


# -- resolver behaviour -------------------------------------------------------


def test_derived_development_tier_from_an_explicit_url(tmp_path):
    url, tier, target = resolve_database_url(
        environ={**env(), "DATABASE_URL": DEV_URL}, dotenv_path=missing_dotenv(tmp_path)
    )
    assert tier == DEVELOPMENT
    assert url == DEV_URL
    assert target.redacted().startswith("development postgresql")


def test_development_target_cannot_be_smuggled_through_a_reordered_url(tmp_path):
    """A production-like URL is refused even though it is the only definition."""
    with pytest.raises(TierError) as exc:
        resolve_database_url(
            environ={**env(), "DATABASE_URL": PROD_URL},
            dotenv_path=missing_dotenv(tmp_path),
        )
    assert "development tier refused" in str(exc.value)


def test_missing_url_without_a_tier_fails_closed(tmp_path):
    with pytest.raises(TierError) as exc:
        resolve_database_url(environ=env(), dotenv_path=missing_dotenv(tmp_path))
    assert "no silent fallback" in str(exc.value)


def test_unknown_tier_environment_value_is_refused(tmp_path):
    with pytest.raises(TierError):
        resolve_database_url(
            environ={**env(), "POLISCOPIC_DB_TIER": "staging", "DATABASE_URL": DEV_URL},
            dotenv_path=missing_dotenv(tmp_path),
        )


def test_test_tier_mints_local_sqlite_even_when_a_remote_url_is_present(tmp_path):
    url, tier, target = resolve_database_url(
        environ={**env(), "POLISCOPIC_DB_TIER": TEST, "DATABASE_URL": DEV_URL},
        dotenv_path=missing_dotenv(tmp_path),
    )
    assert tier == TEST
    assert url.startswith("sqlite:///")
    assert target.url_class == LOCAL


def test_test_tier_honours_an_explicit_local_url(tmp_path):
    url, tier, _ = resolve_database_url(
        environ={**env(), "POLISCOPIC_DB_TIER": TEST, "DATABASE_URL": "sqlite:///tmp/t.sqlite"},
        dotenv_path=missing_dotenv(tmp_path),
    )
    assert tier == TEST
    assert url == "sqlite:///tmp/t.sqlite"


def test_production_tier_resolves_an_explicitly_declared_target(tmp_path):
    """The public service declares production deliberately; the target is still
    classified and validated before it is accepted."""
    url, tier, target = resolve_database_url(
        environ={
            **env(),
            "POLISCOPIC_DB_TIER": PRODUCTION,
            "DATABASE_URL": PROD_URL,
            "PROD_DATABASE_URL": PROD_URL,
        },
        dotenv_path=missing_dotenv(tmp_path),
    )
    assert url == PROD_URL
    assert tier == PRODUCTION
    assert target.url_class == PRODUCTION_LIKE
    assert target.database == "poliscopic"


def test_production_tier_requires_an_explicit_url(tmp_path):
    """A production declaration with no URL is refused, not defaulted."""
    with pytest.raises(TierError) as exc:
        resolve_database_url(
            environ={**env(), "POLISCOPIC_DB_TIER": PRODUCTION},
            dotenv_path=missing_dotenv(tmp_path),
        )
    assert "requires an explicit DATABASE_URL" in str(exc.value)


def test_production_tier_refuses_a_development_production_url(tmp_path):
    """The production path validates its target before accepting it."""
    with pytest.raises(TierError):
        resolve_database_url(
            environ={**env(), "POLISCOPIC_DB_TIER": PRODUCTION, "PROD_DATABASE_URL": DEV_URL},
            dotenv_path=missing_dotenv(tmp_path),
        )


# -- credential safety --------------------------------------------------------


def test_redacted_output_never_contains_credentials():
    secret = "sup3r-s3cret-pa55"
    url = f"postgresql://someuser:{secret}@dev-host.internal:5432/poliscopic_dev"

    target = parse_target(url)

    assert secret not in target.redacted()
    assert "someuser" not in target.redacted()
    assert target.redacted() == "development postgresql dev-host.internal:5432/poliscopic_dev"


def test_redacted_output_hides_credentials_for_production_targets():
    secret = "another-secret"
    url = f"postgresql://admin:{secret}@db.b.db.ondigitalocean.com:25060/poliscopic"

    target = parse_target(url)

    assert secret not in target.redacted()
    assert "admin" not in target.redacted()


def test_production_markers_are_matched_on_the_host_only():
    """A password that merely mentions a production marker must not classify."""
    url = "postgresql://u:ondigitalocean.com@dev-host.internal:5432/poliscopic_dev"
    assert classify_target(url) == "development"


# -- dual-role validation for sync tooling ------------------------------------


def test_dual_role_validation_accepts_a_correct_pair():
    dev = resolve_role_url(DEVELOPMENT, DEV_URL)
    prod = resolve_role_url(PRODUCTION, PROD_URL)
    assert dev.url_class == "development"
    assert prod.url_class == PRODUCTION_LIKE


def test_dual_role_validation_refuses_a_swapped_pair():
    """Syncing the wrong direction must be impossible, not merely unlikely."""
    with pytest.raises(TierError):
        resolve_role_url(DEVELOPMENT, PROD_URL)
    with pytest.raises(TierError):
        resolve_role_url(PRODUCTION, DEV_URL)


def test_dual_role_validation_refuses_a_missing_url():
    with pytest.raises(TierError) as exc:
        resolve_role_url(DEVELOPMENT, None, label="dev")
    assert "dev URL is not set" in str(exc.value)


def test_dual_role_validation_refuses_an_unknown_role():
    with pytest.raises(TierError):
        resolve_role_url("staging", DEV_URL)


def test_dual_role_validation_contacts_nothing():
    """Pure parse-and-compare: no engine, no connection, no network."""
    target = resolve_role_url(PRODUCTION, PROD_URL)
    assert target.host == "tenant.example.ondigitalocean.com"
    assert target.port == 25060


# -- compatibility ------------------------------------------------------------


def test_application_startup_resolves_a_development_target(tmp_path):
    """Ordinary startup: .env-style development URL resolves unchanged."""
    url, tier, target = resolve_database_url(
        environ={**env(), "DATABASE_URL": DEV_URL}, dotenv_path=missing_dotenv(tmp_path)
    )
    assert url == DEV_URL
    assert tier == DEVELOPMENT
    assert target.database == "poliscopic_dev"


def test_db_config_exports_the_historical_names():
    """db.config keeps exporting DATABASE_URL for existing importers."""
    import db.config as config

    assert config.DATABASE_URL
    assert config.DB_TIER in (DEVELOPMENT, TEST, PRODUCTION)
    assert config.DATABASE_TIER == config.DB_TIER


def test_db_core_reexports_config_url_without_connecting():
    import db.core
    import db.config as config

    assert db.core.DATABASE_URL == config.DATABASE_URL


def test_kg_cli_entry_points_share_one_resolver():
    """Every KG entry point must resolve through db.config, not its own rules."""
    import inspect

    from scripts.entities import bounded_verification, detect_entities, sweep_docs_preflight

    for module in (bounded_verification, detect_entities, sweep_docs_preflight):
        source = inspect.getsource(module)
        assert "from db.core import get_engine" in source or "from db import get_engine" in source
