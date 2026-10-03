"""Declarative registry for sources executed by the daily/weekly scheduler.

It owns scheduler facts, verified aliases and invocation commands, typed body
catalogs, and adapter dispatch where the implementation exposes a stable
``sync(args)`` contract.  Large inline handlers remain in ``main`` until they
can be extracted without changing their persistence behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import import_module
from typing import Literal

from scraper.jurisdiction_registry import authority_by_slug


ScheduleGroup = Literal["A", "B", "C", "D"]
ExecutionMode = Literal["serial", "parallel"]


@dataclass(frozen=True, slots=True)
class ScheduledSource:
    """One scraper command's stable scheduler configuration."""

    command: str
    group: ScheduleGroup
    execution: ExecutionMode
    jurisdiction_slug: str
    cli_command: str | None = None
    action_args: tuple[str, ...] = ("--sync",)
    aliases: tuple[str, ...] = ()
    adapter_module: str | None = None
    adapter_callable: str = "sync"
    accepts_date_range: bool = True
    daily_args: tuple[str, ...] = ()
    weekly_args: tuple[str, ...] = ()
    daily_bodies: tuple[str, ...] = ()
    weekly_bodies: tuple[str, ...] = ()

    @property
    def invocation_command(self) -> str:
        return self.cli_command or self.command

    def args_for(self, tier: str) -> list[str]:
        args = list(
            self.weekly_args
            if tier == "weekly" and self.weekly_args
            else self.daily_args
        )
        bodies = (
            self.weekly_bodies
            if tier == "weekly" and self.weekly_bodies
            else self.daily_bodies
        )
        if bodies:
            args.append(f"--bodies={','.join(bodies)}")
        return args


SCHEDULED_SOURCES: tuple[ScheduledSource, ...] = (
    # Group A: browser-heavy sources, deliberately serial.
    ScheduledSource("bos", "A", "serial", "maricopa-county"),
    ScheduledSource("pz", "A", "serial", "maricopa-county"),
    ScheduledSource("adj", "A", "serial", "maricopa-county"),
    ScheduledSource("health", "A", "serial", "maricopa-county"),
    ScheduledSource("drain", "A", "serial", "maricopa-county"),
    ScheduledSource("tab", "A", "serial", "maricopa-county"),
    ScheduledSource("valley-metro", "A", "serial", "valley-metro"),
    ScheduledSource(
        "tempe-subcommittees",
        "A",
        "serial",
        "tempe",
        accepts_date_range=False,
    ),
    # Group B: primary HTTP sources plus retained legacy overlap.
    ScheduledSource("chandler", "B", "parallel", "chandler"),
    ScheduledSource("tempe", "B", "parallel", "tempe"),
    ScheduledSource("mesa", "B", "parallel", "mesa"),
    ScheduledSource("scottsdale", "B", "parallel", "scottsdale"),
    ScheduledSource(
        "glendale-new",
        "B",
        "parallel",
        "glendale",
        daily_bodies=(
            "glendale-city-council",
            "glendale-planning-commission",
        ),
    ),
    ScheduledSource(
        "goodyear",
        "B",
        "parallel",
        "goodyear",
        weekly_bodies=(
            "goodyear-city-council",
            "goodyear-planning-zoning-commission",
            "goodyear-arts-culture-commission",
            "goodyear-youth-commission",
            "goodyear-water-advisory",
            "goodyear-fire-psprs",
            "goodyear-police-psprs",
            "goodyear-joint-psprs",
            "goodyear-psprs",
            "goodyear-audit-committee",
            "goodyear-notice-of-quorum",
            "goodyear-ida",
            "goodyear-parks",
            "goodyear-boa",
            "goodyear-cfd",
            "goodyear-healthcare-trust",
            "goodyear-firefighter-retirement",
            "goodyear-public-art",
        ),
    ),
    ScheduledSource("gilbert", "B", "parallel", "gilbert"),
    ScheduledSource(
        "surprise-civicclerk",
        "B",
        "parallel",
        "surprise",
        daily_bodies=(
            "surprise-pz",
            "surprise-arts",
            "surprise-veterans",
            "surprise-library",
            "surprise-parks",
            "surprise-psprs-fire",
            "surprise-psprs-police",
            "surprise-health-benefits",
            "surprise-nominations",
            "surprise-audit",
            "surprise-tourism",
            "surprise-judicial-selection",
        ),
    ),
    ScheduledSource("glendale", "B", "parallel", "glendale"),
    ScheduledSource("surprise", "B", "parallel", "surprise"),
    # Group C: remaining primary city sources.
    ScheduledSource(
        "phoenix-rss",
        "C",
        "parallel",
        "phoenix",
        aliases=("phoenix",),
    ),
    ScheduledSource("phoenix-aem", "C", "parallel", "phoenix"),
    ScheduledSource(
        "phoenix-planning",
        "C",
        "parallel",
        "phoenix",
        accepts_date_range=False,
    ),
    ScheduledSource(
        "phoenix-aem-results",
        "C",
        "parallel",
        "phoenix",
        cli_command="phoenix-aem",
        action_args=("--sync-results",),
        accepts_date_range=False,
    ),
    ScheduledSource("avondale", "C", "parallel", "avondale"),
    ScheduledSource(
        "tolleson",
        "C",
        "parallel",
        "tolleson",
        adapter_module="scraper.jurisdictions.tolleson",
    ),
    ScheduledSource(
        "fountain-hills",
        "C",
        "parallel",
        "fountain-hills",
        adapter_module="scraper.jurisdictions.fountain_hills",
    ),
    ScheduledSource(
        "litchfield-park",
        "C",
        "parallel",
        "litchfield-park",
        adapter_module="scraper.jurisdictions.litchfield_park",
    ),
    ScheduledSource(
        "youngtown",
        "C",
        "parallel",
        "youngtown",
        adapter_module="scraper.jurisdictions.youngtown",
    ),
    ScheduledSource(
        "flagstaff",
        "C",
        "parallel",
        "flagstaff",
        adapter_module="scraper.jurisdictions.flagstaff",
    ),
    ScheduledSource(
        "yuma",
        "C",
        "parallel",
        "yuma",
        adapter_module="scraper.jurisdictions.yuma",
    ),
    ScheduledSource("tucson", "C", "parallel", "tucson"),
    ScheduledSource("peoria", "C", "parallel", "peoria"),
    ScheduledSource(
        "buckeye-granicus",
        "C",
        "parallel",
        "buckeye",
        cli_command="buckeye",
    ),
    # Group D: lower-frequency and secondary sources.
    ScheduledSource("el-mirage", "D", "parallel", "el-mirage"),
    ScheduledSource(
        "paradise-valley",
        "D",
        "parallel",
        "paradise-valley",
        adapter_module="scraper.jurisdictions.paradise_valley",
    ),
    ScheduledSource(
        "queen-creek",
        "D",
        "parallel",
        "queen-creek",
        adapter_module="scraper.jurisdictions.queen_creek",
    ),
    ScheduledSource(
        "apache-junction",
        "D",
        "parallel",
        "apache-junction",
        adapter_module="scraper.jurisdictions.apache_junction",
    ),
    ScheduledSource(
        "gilbert-planning",
        "D",
        "parallel",
        "gilbert",
        adapter_module="scraper.jurisdictions.gilbert_planning",
    ),
    ScheduledSource("scottsdale-boards", "D", "parallel", "scottsdale"),
    ScheduledSource("tucson-pc", "D", "parallel", "tucson"),
    ScheduledSource("ida", "D", "parallel", "maricopa-county"),
)


# Human-facing body choices used by ``scrape_agendas.py --list-bodies`` and
# source-specific parsers.  This lives beside scheduler metadata so adding a
# source does not require maintaining a second catalog in the CLI module.
CLI_BODY_CATALOGS: dict[str, dict[str, str]] = {
    "bos": {"bos": "Board of Supervisors"},
    "pz": {"pz": "Planning & Zoning Commission"},
    "adj": {"adj": "Board of Adjustment"},
    "drain": {"drain": "Drainage Review Board (2011–2013, defunct)"},
    "health": {"health": "Board of Health"},
    "tab": {"tab": "Transportation Advisory Board"},
    "ida": {"ida": "Industrial Development Authority"},
    "mcacc": {"mcacc": "All remaining Maricopa County boards via AgendaCenter"},
    "maricopa": {
        "mc-bos": "Board of Supervisors",
        "mc-pz": "Planning & Zoning Commission",
        "mc-adj": "Board of Adjustment",
        "mc-drain": "Drainage Review Board (2011–2013, defunct)",
        "mc-health": "Board of Health",
        "mc-tab": "Transportation Advisory Board",
        "mc-ida": "Industrial Development Authority",
        "mc-mcacc": "All remaining boards via AgendaCenter",
    },
    "tempe": {
        "tempe-cc": "City Council",
        "tempe-drc": "Development Review Commission",
        "tempe-boa": "Board of Adjustment",
        "tempe-hpc": "Historic Preservation Commission",
    },
    "mesa": {
        "mesa-city-council": "City Council",
        "mesa-pz": "Planning & Zoning Board",
        "mesa-design-review-board": "Development Review Board",
        "mesa-board-of-adjustment": "Board of Adjustment",
        "mesa-historic-preservation-board": "Historic Preservation Board",
    },
    "chandler": {
        "chandler-cc": "City Council",
        "chandler-pz": "Planning & Zoning",
        "chandler-drc": "Development Review Commission",
        "chandler-boa": "Board of Adjustment",
        "chandler-hpc": "Historic Preservation Commission",
    },
    "glendale": {
        "glendale-cc": "City Council (via Legistar)",
        "glendale-pc": "Planning Commission (via AgendaQuick)",
        "glendale-boa": "Board of Adjustment",
    },
    "scottsdale": {
        "scottsdale-cc": "City Council (via PDF archive)",
        "scottsdale-pz": "Planning & Zoning",
        "scottsdale-boa": "Board of Adjustment",
        "scottsdale-drb": "Development Review Board",
        "scottsdale-hpc": "Historic Preservation Commission",
    },
    "tucson": {
        "tucson-cc": "Mayor & Council (via OnBase)",
        "tucson-pc": "Planning Commission (via listing page + PDF)",
    },
    "phoenix": {
        "phoenix-cc": "City Council (formal, policy, special, work study)",
        "phoenix-pc": "Planning Commission",
        "phoenix-cs": "Community Services Subcommittee",
        "phoenix-ed": "Economic Development Subcommittee",
        "phoenix-ps": "Public Safety Subcommittee",
        "phoenix-ti": "Transportation, Infrastructure & Planning Subcommittee",
        "phoenix-bh": "Budget Hearing",
    },
    "phoenix-aem": {
        "phoenix-village": "Village Planning Committees",
        "phoenix-planning": "Planning Commission",
        "phoenix-hpc": "Historic Preservation Commission",
    },
    "gilbert": {
        "gilbert-cc": "Town Council (via OnBase)",
        "gilbert-planning": "Planning Commission (via CivicPlus)",
    },
    "surprise": {
        "surprise-cc": "City Council",
        "surprise-pz": "Planning & Zoning",
        "surprise-boa": "Board of Adjustment",
    },
    "buckeye": {
        "buckeye-cc": "City Council",
        "buckeye-pz": "Planning & Zoning",
        "buckeye-boa": "Board of Adjustment",
        "buckeye-prc": "Parks & Recreation",
        "buckeye-hpc": "Historic Preservation",
        "buckeye-lib": "Library Board",
        "buckeye-psprs": "PSPRS Board",
        "buckeye-airport": "Airport Advisory",
        "buckeye-pollution": "Pollution Control",
        "buckeye-youth": "Youth Council",
        "buckeye-cfd": "CFD",
    },
}


def cli_body_catalogs() -> dict[str, dict[str, str]]:
    """Return copies of the human-facing body catalogs."""
    return {
        command: dict(bodies)
        for command, bodies in CLI_BODY_CATALOGS.items()
    }


def _validate_registry(
    sources: tuple[ScheduledSource, ...] = SCHEDULED_SOURCES,
) -> None:
    """Fail fast when scheduler metadata cannot produce a safe CLI command."""
    commands = [source.command for source in sources]
    duplicates = sorted({item for item in commands if commands.count(item) > 1})
    if duplicates:
        raise ValueError(f"duplicate scheduled source command(s): {duplicates}")
    aliases = [alias for source in sources for alias in source.aliases]
    duplicate_aliases = sorted(
        {alias for alias in aliases if aliases.count(alias) > 1}
    )
    if duplicate_aliases:
        raise ValueError(f"duplicate scheduled source alias(es): {duplicate_aliases}")
    alias_collisions = sorted(set(aliases) & set(commands))
    if alias_collisions:
        raise ValueError(
            f"scheduled source aliases collide with commands: {alias_collisions}"
        )
    for source in sources:
        expected = "serial" if source.group == "A" else "parallel"
        if source.execution != expected:
            raise ValueError(
                f"source {source.command!r} group {source.group} must be {expected}"
            )
        if not source.command or not source.invocation_command:
            raise ValueError("scheduled source commands must be non-empty")
        try:
            authority_by_slug(source.jurisdiction_slug)
        except KeyError as exc:
            raise ValueError(
                f"source {source.command!r} has unknown jurisdiction "
                f"{source.jurisdiction_slug!r}"
            ) from exc
        if not source.action_args or any(
            not arg.startswith("--") for arg in source.action_args
        ):
            raise ValueError(
                f"source {source.command!r} must declare CLI action flags"
            )
        if source.adapter_module and not source.adapter_module.startswith(
            "scraper."
        ):
            raise ValueError(
                f"source {source.command!r} adapter must be in scraper package"
            )
        if source.adapter_module and not source.adapter_callable:
            raise ValueError(
                f"source {source.command!r} adapter callable must be non-empty"
            )
        for tier, args in (
            ("daily", source.daily_args),
            ("weekly", source.weekly_args),
        ):
            if any(arg.startswith("--bodies=") for arg in args):
                raise ValueError(
                    f"source {source.command!r} {tier} bodies must use the body catalog"
                )
        for tier, bodies in (
            ("daily", source.daily_bodies),
            ("weekly", source.weekly_bodies),
        ):
            if any(not body.strip() for body in bodies):
                raise ValueError(
                    f"source {source.command!r} {tier} body names must be non-empty"
                )
            if len(bodies) != len(set(bodies)):
                raise ValueError(
                    f"source {source.command!r} has duplicate {tier} body names"
                )
    for command, bodies in CLI_BODY_CATALOGS.items():
        if not command.strip() or not bodies:
            raise ValueError("CLI body catalogs require a command and bodies")
        if any(not code.strip() or not description.strip()
               for code, description in bodies.items()):
            raise ValueError(
                f"CLI body catalog {command!r} contains a blank value"
            )


_validate_registry()


def source_by_command(command: str) -> ScheduledSource:
    """Return one registered source or raise a specific lookup error."""
    for source in SCHEDULED_SOURCES:
        if source.command == command:
            return source
    raise KeyError(f"unknown scheduled source: {command}")


def adapter_for_command(command: str):
    """Load the explicitly owned adapter callback for a scheduled source."""
    try:
        source = source_by_command(command)
    except KeyError:
        return None
    if source.adapter_module is None:
        return None
    module = import_module(source.adapter_module)
    adapter = getattr(module, source.adapter_callable)
    if not callable(adapter):
        raise TypeError(
            f"adapter {source.adapter_module}.{source.adapter_callable} "
            "is not callable"
        )
    return adapter


def schedule_group(group: ScheduleGroup, tier: str) -> list[tuple[str, list[str]]]:
    """Return runner-compatible entries for a group and schedule tier."""
    return [
        (source.command, source.args_for(tier))
        for source in SCHEDULED_SOURCES
        if source.group == group
    ]


def scheduled_commands() -> frozenset[str]:
    return frozenset(source.command for source in SCHEDULED_SOURCES)


def sources_for_jurisdiction(jurisdiction_slug: str) -> tuple[ScheduledSource, ...]:
    """Return all scheduled extraction sources for one governing authority."""
    authority_by_slug(jurisdiction_slug)
    return tuple(
        source
        for source in SCHEDULED_SOURCES
        if source.jurisdiction_slug == jurisdiction_slug
    )


def scheduled_cli_commands() -> frozenset[str]:
    """Return scheduled identities, invocation commands, and verified aliases."""
    return frozenset(
        command
        for source in SCHEDULED_SOURCES
        for command in (
            source.command,
            source.invocation_command,
            *source.aliases,
        )
    )


def no_date_commands() -> frozenset[str]:
    return frozenset(
        source.command
        for source in SCHEDULED_SOURCES
        if not source.accepts_date_range
    )
