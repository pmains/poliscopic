"""CLI boundary contract for the Tempe subcommittee registry adapter."""

import sys
from types import SimpleNamespace


def test_adapter_forwards_arguments_and_restores_process_argv(monkeypatch):
    from scraper.jurisdictions import tempe_subcommittees_adapter
    from scraper.jurisdictions.tempe import subcommittees

    original = ["scrape_agendas.py", "tempe-subcommittees", "--sync", "--limit=2"]
    observed = []
    monkeypatch.setattr(sys, "argv", original.copy())
    monkeypatch.setattr(
        subcommittees,
        "main",
        lambda: observed.append(sys.argv.copy()) or 23,
    )

    assert tempe_subcommittees_adapter.sync(SimpleNamespace()) == 23
    assert observed == [["tempe-subcommittees", "--sync", "--limit=2"]]
    assert sys.argv == original
