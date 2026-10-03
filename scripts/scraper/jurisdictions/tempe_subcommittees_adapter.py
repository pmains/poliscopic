"""Registry adapter for Tempe's standalone subcommittee scraper."""

from __future__ import annotations

import sys


def sync(args) -> int:
    """Invoke the existing standalone parser with its expected CLI boundary."""
    from scraper.jurisdictions.tempe.subcommittees import main

    original_argv = sys.argv
    try:
        forwarded = (
            original_argv[original_argv.index("tempe-subcommittees") + 1 :]
            if "tempe-subcommittees" in original_argv
            else []
        )
        sys.argv = ["tempe-subcommittees", *forwarded]
        return main()
    finally:
        sys.argv = original_argv
