"""Tests for the packet-bound human quality-review workspace."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from routes.kg_quality_review import kg_quality_review_bp
import routes.kg_quality_review as review


class TestQualityReviewWorkspace(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        case = {
            "case_id": "case:1", "stratum": {"body": "test-body", "predicate": "Approved", "document_type": "Result", "extraction_method": "text", "platform_or_source": "https://example.test/source"},
            "document": {"source_id": 1}, "evidence": {"start": 0, "end": 3, "retained_text_chars": 3, "snippet": "Yes"},
            "candidate": {"output_type": "event", "outcome": "success", "link_state": "meeting", "meeting_db_id": 9, "agenda_item_db_id": None, "promoted": None},
        }
        packet = {
            "mode": "review-only", "applied": False, "digest": "packet-digest",
            "items": [case, {**case, "case_id": "case:2"}],
        }
        self.packet_path = Path(self.temp_dir.name) / "packet.json"
        self.packet_path.write_text(json.dumps(packet))
        self.packets = patch.object(review, "PACKET_PATH", self.packet_path)
        self.ledgers = patch.object(review, "LEDGER_DIR", Path(self.temp_dir.name) / "labels")
        self.packets.start()
        self.ledgers.start()
        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.jinja_env.globals["current_user"] = type("Anonymous", (), {"is_authenticated": False})()
        app.register_blueprint(kg_quality_review_bp)
        self.client = app.test_client()

    def tearDown(self):
        self.ledgers.stop()
        self.packets.stop()
        self.temp_dir.cleanup()

    def test_dashboard_and_case_render(self):
        self.assertEqual(self.client.get("/kg/quality-review/").status_code, 200)
        self.assertEqual(self.client.get("/kg/quality-review/case/case:1").status_code, 200)

    def test_labels_are_saved_separately_and_validated(self):
        response = self.client.post("/kg/quality-review/api/labels/case:1", json={
            "decision": "accept", "evidence_coordinate": "valid",
            "container_link": "correct", "source_detail": "with stipulations",
            "support": "not_applicable", "notes": "Looks right.",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["summary"], {"total": 2, "reviewed": 1, "remaining": 1})
        self.assertTrue(response.get_json()["next_url"].endswith("/case/case:2"))
        ledger = json.loads((Path(self.temp_dir.name) / "labels" / "packet-digest.json").read_text())
        self.assertEqual(ledger["labels"]["case:1"]["source_detail"], "with stipulations")
        bad = self.client.post("/kg/quality-review/api/labels/case:1", json={
            "decision": "accept", "evidence_coordinate": "valid",
            "container_link": "not_applicable", "support": "not_applicable",
        })
        self.assertEqual(bad.status_code, 400)
