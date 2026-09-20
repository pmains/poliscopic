"""Decomposition guarantees for the sweep_docs module family.

These tests assert the *structural* contract of the split: size limits, the
facade's public surface, the monkeypatch seams the facade documents, and the
purity of the planning and payload modules.  Nothing here touches a database or
runs the sweep.
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import textwrap

import pytest

ENTITIES_DIR = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "entities"
MODULE_GLOB = "sweep_docs*.py"
MAX_MODULE_LINES = 500

FORBIDDEN_IN_PURE_MODULES = (
    "sqlalchemy",
    "create_engine",
    "sqlite",
    "psycopg",
    "cursor",
    "execute(",
    "EmissionValidator",
    "ValidationReceipt",
    "classify_rows",
)


def _module_paths():
    return sorted(ENTITIES_DIR.glob(MODULE_GLOB))


# -- size ---------------------------------------------------------------------


def test_no_sweep_docs_module_exceeds_the_line_limit():
    assert _module_paths(), "expected to find sweep_docs modules"
    oversized = {
        path.name: len(path.read_text(encoding="utf-8").splitlines())
        for path in _module_paths()
        if len(path.read_text(encoding="utf-8").splitlines()) > MAX_MODULE_LINES
    }
    assert oversized == {}, f"modules over {MAX_MODULE_LINES} lines: {oversized}"


def test_the_module_family_is_actually_split():
    names = {path.name for path in _module_paths()}
    assert {
        "sweep_docs.py",
        "sweep_docs_batch.py",
        "sweep_docs_extraction.py",
        "sweep_docs_payloads.py",
        "sweep_docs_planning.py",
        "sweep_docs_storage.py",
    } <= names


# -- facade surface -----------------------------------------------------------


def test_facade_exposes_the_public_surface():
    import scripts.entities.sweep_docs as sweep_docs

    for name in (
        "run_sweep_docs", "process_batch", "main", "run_batch",
        "extract_entities_from_doc", "normalize_name", "validate_name",
        "clean_text", "is_garbage_text",
        "KNOWN_ORGANIZATIONS", "KNOWN_ORG_RE", "GENERAL_PATTERNS",
        "GENERAL_PATTERNS", "CASE_NUMBER_RE", "BOS_CASE_RE", "MAX_MATCH_LEN",
        "BATCH_SIZE", "WATERMARK_TABLE", "SOURCE_TYPE",
        "process_batch",
        "_write_entities", "_write_mentions", "mark_docs_swept",
        "build_classification_plan", "select_write_payloads",
    ):
        assert hasattr(sweep_docs, name), f"facade is missing {name}"

    assert callable(sweep_docs.run_sweep_docs)
    assert callable(sweep_docs.process_batch)
    assert sweep_docs.KNOWN_ORGANIZATIONS["Taylor Morrison"] == "developer"


def test_facade_defines_the_public_functions_it_advertises():
    """The facade's own source defines run_sweep_docs/process_batch/main."""
    import scripts.entities.sweep_docs as sweep_docs

    for name in ("run_sweep_docs", "process_batch", "main"):
        assert sweep_docs.__dict__[name].__module__ == sweep_docs.__name__, (
            f"{name} must be defined in the facade, not merely re-exported"
        )


def test_cli_construction_works_without_touching_a_database(monkeypatch):
    """main() builds its parser and halts at parse_args, before any DB work.

    The facade's ``argparse`` name is replaced with a stub, so the real
    ``argparse`` module is never patched and ``main()`` raises before it can
    resolve a database URL or create an engine.
    """
    import scripts.entities.sweep_docs as sweep_docs

    captured: dict = {}

    class _Stop(Exception):
        pass

    class _StubParser:
        def __init__(self, **kwargs):
            self.destinations: set = set()
            captured["description"] = kwargs.get("description")

        def add_argument(self, *args, **kwargs):
            for arg in args:
                if isinstance(arg, str) and arg.startswith("--"):
                    self.destinations.add(arg.lstrip("-").replace("-", "_"))
            return self

        def parse_args(self, *args, **kwargs):
            captured["destinations"] = set(self.destinations)
            raise _Stop()

    class _StubArgparse:
        ArgumentParser = _StubParser

    monkeypatch.setattr(sweep_docs, "argparse", _StubArgparse)
    # Defence in depth: reaching the sweep at all is a failure.
    monkeypatch.setattr(
        sweep_docs, "run_sweep_docs",
        lambda *a, **k: pytest.fail("main() must not run the sweep"),
    )

    with pytest.raises(_Stop):
        sweep_docs.main()

    assert {"dry_run", "verbose", "limit", "batch_size"} <= captured["destinations"]


# -- monkeypatch seams --------------------------------------------------------


def test_process_batch_injects_the_current_facade_seams(monkeypatch):
    """The documented seams are resolved from the facade at call time."""
    import scripts.entities.sweep_docs as sweep_docs

    seen: dict = {}

    def fake_run_batch(conn, wm, entity_cache, *, batch_size, extractor,
                       mention_writer, dry_run=False, verbose=False,
                       validator=None):
        seen.update(
            extractor=extractor, mention_writer=mention_writer,
            batch_size=batch_size, dry_run=dry_run, verbose=verbose,
            validator=validator, wm=wm, entity_cache=entity_cache,
        )
        return {"done": True}

    monkeypatch.setattr(sweep_docs, "run_batch", fake_run_batch)

    patched_extractor = object()
    patched_writer = object()
    monkeypatch.setattr(sweep_docs, "extract_entities_from_doc", patched_extractor)
    monkeypatch.setattr(sweep_docs, "_write_mentions", patched_writer)
    monkeypatch.setattr(sweep_docs, "BATCH_SIZE", 37)

    sweep_docs.process_batch(None, 5, {"cache": 1}, dry_run=True, verbose=True)

    assert seen["extractor"] is patched_extractor
    assert seen["mention_writer"] is patched_writer
    assert seen["batch_size"] == 37
    assert seen["wm"] == 5
    assert seen["entity_cache"] == {"cache": 1}
    assert seen["dry_run"] is True
    assert seen["verbose"] is True


def test_run_sweep_docs_calls_the_facade_process_batch(monkeypatch):
    """run_sweep_docs resolves process_batch from the facade, so patching works."""
    import scripts.entities.sweep_docs as sweep_docs

    assert sweep_docs.run_sweep_docs.__globals__["process_batch"] is (
        sweep_docs.process_batch
    )


# -- parity -------------------------------------------------------------------


def test_extraction_output_parity_with_the_extraction_module():
    import scripts.entities.sweep_docs as sweep_docs
    import scripts.entities.sweep_docs_extraction as extraction

    samples = [
        "Applicant: Acme LLC\nAttorney: Jane Doe\nStaff Contact: Bob Roe",
        "Owner: Taylor Morrison\nCase ZON2024-00123",
        "C-12-34-567-ABC-DEF was considered",
        "no entities here at all",
        "",
    ]
    # One module object, so the re-export is the same function.
    assert sweep_docs.extract_entities_from_doc is extraction.extract_entities_from_doc
    for text in samples:
        assert (
            sweep_docs.extract_entities_from_doc(text)
            == extraction.extract_entities_from_doc(text)
        ), text


def test_payload_selection_parity_with_the_payloads_module():
    import scripts.entities.sweep_docs as sweep_docs
    import scripts.entities.sweep_docs_payloads as payloads
    from scripts.entities.sweep_docs_planning import ExtractedCandidate

    hits = [
        {"normalized": "acme llc", "entity_type": "organization",
         "role": "applicant", "name": "Acme LLC", "confidence": 60, "_source_id": 1},
        {"normalized": "acme llc", "entity_type": "organization",
         "role": "applicant", "name": "Acme Holdings", "confidence": 90,
         "_source_id": 1},
        {"normalized": "jane doe", "entity_type": "person",
         "role": "attorney", "name": "Jane Doe", "confidence": 70, "_source_id": 2},
    ]
    candidates = [ExtractedCandidate.from_mapping(h) for h in hits]

    facade_entities, facade_mentions = sweep_docs.select_write_payloads(candidates)
    module_entities, module_mentions = payloads.select_write_payloads(candidates)

    # Direct equality and direct type identity: both paths resolve to the same
    # module object, so these are the same dataclasses, not lookalike copies.
    assert facade_entities == module_entities
    assert facade_mentions == module_mentions
    assert type(next(iter(facade_entities.values()))) is payloads.EntityPayload
    # Highest confidence wins for the duplicated assertion.
    assert module_entities[("acme llc", "organization")].name == "Acme Holdings"


# -- purity -------------------------------------------------------------------


@pytest.mark.parametrize(
    "module_name", ["sweep_docs_planning.py", "sweep_docs_payloads.py"]
)
def test_pure_modules_have_no_sql_database_or_validator_dependencies(module_name):
    source = (ENTITIES_DIR / module_name).read_text(encoding="utf-8")
    for forbidden in FORBIDDEN_IN_PURE_MODULES:
        assert forbidden not in source, f"{module_name} must not reference {forbidden}"


def test_no_module_executes_work_at_import_time():
    """Importing must not open a database, create an engine, or run a sweep.

    Only module-level statements count.  A call inside a function body (for
    example ``main()`` creating its engine) runs when that function is called,
    not at import.
    """
    forbidden_calls = ("create_engine(", "run_sweep_docs(", "engine.begin(")
    for path in _module_paths():
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not line.strip() or line[0].isspace():
                continue
            if line.startswith(("import ", "from ", "#", '\"\"\"', "'''",
                                ")", "]", "}", "def ", "class ", "@")):
                continue
            for forbidden in forbidden_calls:
                assert forbidden not in line, (
                    f"{path.name}:{number}: {forbidden} at import time"
                )


# -- single canonical namespace -------------------------------------------------

SWEEP_MODULES = (
    "scripts.entities.sweep_docs",
    "scripts.entities.sweep_docs_batch",
    "scripts.entities.sweep_docs_extraction",
    "scripts.entities.sweep_docs_payloads",
    "scripts.entities.sweep_docs_planning",
    "scripts.entities.sweep_docs_storage",
    # The emission/identity stack is genuinely reachable: the sweep validates
    # every bundle through EmissionValidator and evidence identities.  These
    # were previously omitted from the manifest, which understated the
    # producer's code evidence; they are declared and hashed here.
    "scripts.kg.emission",
    "scripts.kg.emission_bundles",
    "scripts.kg.emission_checks",
    "scripts.kg.emission_models",
    "scripts.kg.emission_receipts",
    "scripts.kg.emission_validation",
    "scripts.kg.identity",
    "scripts.kg.identity_assertions",
    "scripts.kg.identity_keys",
    "scripts.kg.producer_coverage",
    "scripts.kg.producer_versions",
    "scripts.kg.registries.evidence",
    "scripts.kg.registries.model",
    "scripts.kg.registries.roles",
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

CLEAN_PROCESS_PROOF = """
import sys

import scripts.entities.sweep_docs as facade
import scripts.entities.sweep_docs_planning as planning
import scripts.entities.sweep_docs_payloads as payloads
import scripts.entities.sweep_docs_storage as storage

assert facade.EntityAssertion is planning.EntityAssertion
assert facade.ExtractedCandidate is planning.ExtractedCandidate
assert facade.EntityPayload is payloads.EntityPayload
assert facade._write_entities is storage._write_entities

leaked = sorted(k for k in sys.modules if k == "entities" or k.startswith("entities."))
assert not leaked, leaked
print("OK")
"""


def test_clean_process_loads_one_canonical_namespace():
    """A fresh interpreter must load every sibling under scripts.entities only."""
    result = subprocess.run(
        [sys.executable, "-c", CLEAN_PROCESS_PROOF],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_facade_reexports_are_the_sibling_module_objects():
    import scripts.entities.sweep_docs as facade
    import scripts.entities.sweep_docs_planning as planning
    import scripts.entities.sweep_docs_payloads as payloads
    import scripts.entities.sweep_docs_storage as storage

    assert facade.EntityAssertion is planning.EntityAssertion
    assert facade.ExtractedCandidate is planning.ExtractedCandidate
    assert facade.EntityPayload is payloads.EntityPayload
    assert facade._write_entities is storage._write_entities


IMPORTABLE_SIBLINGS = (
    "sweep_docs_batch.py",
    "sweep_docs_extraction.py",
    "sweep_docs_payloads.py",
    "sweep_docs_planning.py",
    "sweep_docs_storage.py",
)

#: Modules that may bootstrap ``sys.path`` because they are CLI entry points.
CLI_ENTRY_POINTS = ("sweep_docs.py", "sweep_docs_preflight.py")


def test_importable_siblings_do_not_manipulate_sys_path():
    """Importable siblings must be reached by relative import, never by path."""
    for name in IMPORTABLE_SIBLINGS:
        source = (ENTITIES_DIR / name).read_text(encoding="utf-8")
        assert "sys.path" not in source, f"{name} manipulates sys.path"


def test_only_cli_entry_points_bootstrap_sys_path():
    """Only the facade and the preflight CLI may bootstrap the path."""
    for path in _module_paths():
        source = path.read_text(encoding="utf-8")
        if path.name in CLI_ENTRY_POINTS:
            assert source.count("sys.path.insert") == 1, path.name
            continue
        assert "sys.path" not in source, f"{path.name} manipulates sys.path"


def test_preflight_is_read_only_and_outside_the_fingerprint_manifest():
    """The preflight is a tool, not phase behaviour, and never writes."""
    source = (ENTITIES_DIR / "sweep_docs_preflight.py").read_text(encoding="utf-8")
    upper = source.upper()
    for verb in ("INSERT INTO", "UPDATE ", "DELETE FROM", "DROP TABLE",
                 "ALTER TABLE"):
        assert verb not in upper, f"preflight issues a write: {verb}"

    declared = tuple(_sweep_phase()["code_modules"])
    assert "scripts.entities.sweep_docs_preflight" not in declared


# -- producer code fingerprint --------------------------------------------------


def _sweep_phase():
    from scripts.entities.detect_entities import PHASES

    for phase in PHASES:
        if phase.get("name") == "sweep_docs":
            return phase
    raise AssertionError("sweep_docs phase missing from PHASES")


def test_phase_manifest_declares_every_reachable_module():
    """Exactly the declared modules: the six sweep modules plus the stack it uses."""
    declared = tuple(_sweep_phase()["code_modules"])
    assert declared == SWEEP_MODULES
    assert list(declared) == sorted(declared)
    assert len(set(declared)) == len(declared)


def test_producer_code_evidence_hashes_every_component():
    from scripts.entities.detect_entities import _producer_metadata

    metadata = _producer_metadata(_sweep_phase())

    assert metadata["code_modules"] == list(SWEEP_MODULES)
    assert metadata["code_evidence_complete"] is True
    assert metadata["code_module_errors"] == {}
    hashes = metadata["code_module_sha256"]
    assert set(hashes) == set(SWEEP_MODULES)
    assert all(isinstance(value, str) and len(value) == 64 for value in hashes.values())
    aggregate = metadata["code_sha256"]
    assert isinstance(aggregate, str) and len(aggregate) == 64
    # The aggregate is not merely one component's hash.
    assert aggregate not in set(hashes.values())


def test_changing_any_component_changes_the_aggregate_fingerprint(
    tmp_path, monkeypatch
):
    import importlib as importlib_module
    import types

    from scripts.entities import detect_entities

    phase = _sweep_phase()
    baseline = detect_entities._producer_metadata(phase)["code_sha256"]

    altered = tmp_path / "altered_component.py"
    altered.write_bytes(b"# an altered component\n")
    real_import_module = importlib_module.import_module

    for target in SWEEP_MODULES:
        def fake_import(name, package=None, *, _target=target, **kwargs):
            if name == _target:
                return types.SimpleNamespace(__file__=str(altered))
            return real_import_module(name, package, **kwargs)

        monkeypatch.setattr(importlib_module, "import_module", fake_import)
        changed = detect_entities._producer_metadata(phase)["code_sha256"]
        assert changed != baseline, f"changing {target} left the fingerprint unchanged"


def test_missing_component_fails_evidence_completeness(monkeypatch):
    import importlib as importlib_module

    from scripts.entities import detect_entities

    phase = _sweep_phase()
    target = SWEEP_MODULES[-1]
    real_import_module = importlib_module.import_module

    def fake_import(name, package=None, **kwargs):
        if name == target:
            raise ImportError(f"simulated missing component: {name}")
        return real_import_module(name, package, **kwargs)

    monkeypatch.setattr(importlib_module, "import_module", fake_import)
    metadata = detect_entities._producer_metadata(phase)

    assert metadata["code_evidence_complete"] is False
    assert metadata["code_sha256"] is None
    assert metadata["code_module_sha256"][target] is None
    assert target in metadata["code_module_errors"]
