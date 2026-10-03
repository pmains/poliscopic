"""Contract tests for the scheduler-facing scraper source registry."""

import asyncio
from importlib import import_module

import pytest

from scraper.source_registry import (
    SCHEDULED_SOURCES,
    ScheduledSource,
    _validate_registry,
    adapter_for_command,
    cli_body_catalogs,
    no_date_commands,
    schedule_group,
    scheduled_cli_commands,
    source_by_command,
    sources_for_jurisdiction,
)
from scraper.jurisdiction_registry import authority_by_slug
from sync import runner


def test_scheduled_commands_are_unique_and_cover_compatibility_groups():
    commands = [source.command for source in SCHEDULED_SOURCES]

    assert len(commands) == len(set(commands)) == 40
    assert set(commands) == runner.ALL_TIER_JURISDICTIONS
    assert [*runner.GROUP_A, *runner.GROUP_B, *runner.GROUP_C, *runner.GROUP_D] == [
        *schedule_group("A", "daily"),
        *schedule_group("B", "daily"),
        *schedule_group("C", "daily"),
        *schedule_group("D", "daily"),
    ]


def test_sources_name_independent_government_authorities():
    county_sources = {source.command for source in sources_for_jurisdiction(
        "maricopa-county"
    )}
    tempe_sources = {source.command for source in sources_for_jurisdiction("tempe")}

    assert county_sources == {"bos", "pz", "adj", "health", "drain", "tab", "ida"}
    assert tempe_sources == {"tempe", "tempe-subcommittees"}
    assert county_sources.isdisjoint(tempe_sources)
    assert source_by_command("gilbert-planning").jurisdiction_slug == "gilbert"
    assert source_by_command("scottsdale-boards").jurisdiction_slug == "scottsdale"


def test_regional_authorities_are_not_modeled_as_county_children():
    mag = authority_by_slug("mag")
    valley_metro = authority_by_slug("valley-metro")

    assert mag.kind == valley_metro.kind == "regional"
    assert mag.form == "council_of_governments"
    assert valley_metro.form == "transportation_authority"
    assert not hasattr(mag, "parent_slug")
    assert not hasattr(valley_metro, "parent_slug")


def test_weekly_goodyear_override_comes_from_registry():
    daily = dict(schedule_group("B", "daily"))
    weekly = dict(schedule_group("B", "weekly"))

    assert daily["goodyear"] == []
    assert weekly["goodyear"] == source_by_command("goodyear").args_for("weekly")
    assert weekly["goodyear"][0].startswith("--bodies=goodyear-city-council,")
    assert len(source_by_command("goodyear").weekly_bodies) == 18
    assert daily["glendale-new"] == weekly["glendale-new"]


def test_body_scopes_are_declarative_not_embedded_argument_strings():
    glendale = source_by_command("glendale-new")
    surprise = source_by_command("surprise-civicclerk")

    assert glendale.daily_bodies == (
        "glendale-city-council",
        "glendale-planning-commission",
    )
    assert len(surprise.daily_bodies) == 12
    for source in SCHEDULED_SOURCES:
        assert not any(arg.startswith("--bodies=") for arg in source.daily_args)
        assert not any(arg.startswith("--bodies=") for arg in source.weekly_args)


def test_cli_body_catalogs_are_registry_owned_copies():
    from scraper.cli import JURISDICTION_BODIES

    catalogs = cli_body_catalogs()
    catalogs["tempe"]["temporary"] = "Mutation probe"

    assert "temporary" not in cli_body_catalogs()["tempe"]
    assert JURISDICTION_BODIES["scottsdale"] == {
        "scottsdale-cc": "City Council (via PDF archive)",
        "scottsdale-pz": "Planning & Zoning",
        "scottsdale-boa": "Board of Adjustment",
        "scottsdale-drb": "Development Review Board",
        "scottsdale-hpc": "Historic Preservation Commission",
    }


def test_date_argument_exceptions_come_from_registry():
    expected = {
        "tempe-subcommittees",
        "phoenix-planning",
        "phoenix-aem-results",
    }

    assert no_date_commands() == expected
    assert runner.NO_DATE_ARGS == expected
    command = runner._build_cmd(
        "phoenix-planning", [], "2026-10-01", "2026-10-16", "daily"
    )
    assert not any(item.startswith("--start-date=") for item in command)
    assert not any(item.startswith("--end-date=") for item in command)


def test_normal_source_command_keeps_date_window():
    command = runner._build_cmd(
        "yuma", [], "2026-10-01", "2026-10-16", "daily"
    )

    assert "--start-date=2026-10-01" in command
    assert "--end-date=2026-10-16" in command


def test_phoenix_results_uses_real_cli_command_and_action():
    command = runner._build_cmd(
        "phoenix-aem-results", [], "2026-10-01", "2026-10-16", "daily"
    )

    assert command[2:4] == ["phoenix-aem", "--sync-results"]
    assert "--sync" not in command
    assert not any(item.startswith("--start-date=") for item in command)
    assert not any(item.startswith("--end-date=") for item in command)


def test_buckeye_granicus_uses_the_existing_buckeye_cli_handler():
    from scraper.cli import parse_args

    command = runner._build_cmd(
        "buckeye-granicus", [], "2026-10-01", "2026-10-16", "daily"
    )

    assert command[2:4] == ["buckeye", "--sync"]
    parsed = parse_args(command[2:])
    assert parsed.source == "buckeye"


def test_unknown_source_is_refused():
    try:
        source_by_command("not-a-source")
    except KeyError as error:
        assert "unknown scheduled source" in str(error)
    else:
        raise AssertionError("unknown source was accepted")


def test_cli_recognizes_every_scheduled_command_from_registry():
    from scraper.cli import SOURCE_COMMANDS, parse_args

    assert scheduled_cli_commands() <= SOURCE_COMMANDS
    assert "phoenix" in source_by_command("phoenix-rss").aliases
    assert source_by_command("buckeye-granicus").invocation_command == "buckeye"
    phoenix_results = parse_args(["phoenix-aem", "--sync-results"])
    assert phoenix_results.source == "phoenix-aem"
    assert phoenix_results.sync_results is True


@pytest.mark.parametrize(
    "command,module_name",
    [
        ("flagstaff", "scraper.jurisdictions.flagstaff"),
        ("yuma", "scraper.jurisdictions.yuma"),
        ("youngtown", "scraper.jurisdictions.youngtown"),
        ("litchfield-park", "scraper.jurisdictions.litchfield_park"),
        ("gilbert-planning", "scraper.jurisdictions.gilbert_planning"),
        ("fountain-hills", "scraper.jurisdictions.fountain_hills"),
        ("apache-junction", "scraper.jurisdictions.apache_junction"),
        ("queen-creek", "scraper.jurisdictions.queen_creek"),
        ("paradise-valley", "scraper.jurisdictions.paradise_valley"),
        ("tolleson", "scraper.jurisdictions.tolleson"),
        ("el-mirage", "scraper.jurisdictions.el_mirage_adapter"),
    ],
)
def test_standalone_adapter_ownership_is_loadable(command, module_name):
    source = source_by_command(command)
    adapter = adapter_for_command(command)

    assert source.adapter_module == module_name
    assert adapter.__module__ == module_name
    assert adapter.__name__ == "sync"


@pytest.mark.parametrize(
    "source",
    [
        "flagstaff",
        "gilbert-planning",
        "fountain-hills",
        "apache-junction",
        "queen-creek",
        "paradise-valley",
        "tolleson",
        "el-mirage",
    ],
)
def test_main_dispatches_standalone_sources_through_registry(monkeypatch, source):
    from scraper.cli import parse_args

    scraper_main = import_module("scraper.main")

    args = parse_args([source, "--sync"])
    calls = []

    def adapter(received):
        calls.append(received)
        return 17

    monkeypatch.setattr(scraper_main, "setup_logger", lambda: None)
    monkeypatch.setattr(scraper_main, "parse_args", lambda: args)
    monkeypatch.setattr(
        scraper_main,
        "adapter_for_command",
        lambda command: adapter if command == source else None,
    )

    assert asyncio.run(scraper_main.main()) == 17
    assert calls == [args]


@pytest.mark.parametrize(
    "source, match",
    [
        (
            ScheduledSource("bad-action", "B", "parallel", "tempe", action_args=()),
            "must declare CLI action flags",
        ),
        (
            ScheduledSource(
                "embedded-bodies",
                "B",
                "parallel",
                "tempe",
                daily_args=("--bodies=one,two",),
            ),
            "must use the body catalog",
        ),
        (
            ScheduledSource(
                "duplicate-bodies",
                "B",
                "parallel",
                "tempe",
                daily_bodies=("one", "one"),
            ),
            "duplicate daily body names",
        ),
        (
            ScheduledSource(
                "alias-collision",
                "B",
                "parallel",
                "tempe",
                aliases=("alias-collision",),
            ),
            "aliases collide with commands",
        ),
        (
            ScheduledSource(
                "external-adapter",
                "B",
                "parallel",
                "tempe",
                adapter_module="outside.adapter",
            ),
            "adapter must be in scraper package",
        ),
        (
            ScheduledSource("unknown-jurisdiction", "B", "parallel", "nowhere"),
            "unknown jurisdiction",
        ),
    ],
)
def test_registry_refuses_unsafe_invocation_metadata(source, match):
    with pytest.raises(ValueError, match=match):
        _validate_registry((source,))
