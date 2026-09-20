import importlib.util
import sys
import unittest
from argparse import Namespace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_tiers import integration_test


_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "capture_fixtures.py"


def _load_capture_fixtures():
    script_path = _SCRIPT_PATH
    if not script_path.is_file():
        pytest.skip(
            f"{script_path} does not exist in this tree: the capture_fixtures "
            "script was removed and has no replacement in the repository",
            allow_module_level=True,
        )
    spec = importlib.util.spec_from_file_location("capture_fixtures", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load capture_fixtures from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


capture_fixtures = _load_capture_fixtures()


@integration_test
class CaptureFixturesTests(unittest.TestCase):
    def test_manifest_schema_includes_validation_fields(self):
        self.assertIn("validation_status", capture_fixtures.MANIFEST_FIELDS)
        self.assertIn("html_sha256", capture_fixtures.MANIFEST_FIELDS)

    def test_validation_passes_expected_fixture(self):
        path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agendas" / "2025-01-27_informal_4448_agenda.html"
        target = capture_fixtures.FixtureTarget(
            meeting_id="4448",
            meeting_date="2025-01-27",
            meeting_type="Informal",
            source_url=capture_fixtures.agenda_url("4448"),
            reason_included="test",
        )
        result = capture_fixtures.capture_html_validation(target, path.read_text(encoding="utf-8"), source_url=target.source_url)
        self.assertTrue(result.passed)
        self.assertEqual(result.validation_status, "passed")
        self.assertEqual(len(result.html_sha256), 64)

    def test_validation_rejects_wrong_fixture_id(self):
        path = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "agendas" / "2025-01-27_informal_4448_agenda.html"
        target = capture_fixtures.FixtureTarget(
            meeting_id="4470",
            meeting_date="2025-01-27",
            meeting_type="Special",
            source_url=capture_fixtures.agenda_url("4448"),
            reason_included="test",
        )
        result = capture_fixtures.capture_html_validation(target, path.read_text(encoding="utf-8"), source_url=target.source_url)
        self.assertFalse(result.passed)
        self.assertIn("meeting_id 4470", " ".join(result.errors))

    def test_meeting_id_filter_limits_targets(self):
        args = Namespace(dry_run=True, overwrite=False, no_playwright=True, offline_targets_only=True, meeting_id="4448")
        targets = capture_fixtures.collect_targets(args)
        self.assertEqual([t.meeting_id for t in targets], ["4448"])


if __name__ == "__main__":
    unittest.main()
