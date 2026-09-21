"""Focused tests for the Stage 3 human-approval workspace.

The page records a human authorization; it must not execute, must not sign for anyone,
and must not be talked into approving something other than the exact artifacts a human
reviewed.  These tests pin rendering, exact binding, the create-once write, every refusal
path, and the absence of any database, apply, authorization-builder, preflight, or runner
call on either the GET or the POST path.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

import routes.kg_stage3_approval as review
from routes.kg_stage3_approval import kg_stage3_approval_bp
from scripts.kg import stage3_processing_receipt_apply as apply
from scripts.kg import stage3_processing_receipt_apply_proposal as proposal_mod
from scripts.kg.stage2_artifacts import load_verified, write_immutable

TARGET = {"tier": "development", "database": "poliscopic_dev"}
BACKUP_DIGEST = "b" * 64


class ApprovalWorkspaceTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)

        self.plan_path = root / "plan.json"
        plan_digest = write_immutable(self.plan_path, {
            "kind": "kg-stage3-processing-dry-plan", "version": "1.0",
            "target": dict(TARGET), "selected": []})
        plan = load_verified(self.plan_path)

        self.design_path = root / "design.json"
        design_digest = write_immutable(self.design_path, {
            "kind": "kg-stage3-processing-receipt-store-packet", "version": "2.2",
            "enabled": False, "mode": "design-only"})
        design = load_verified(self.design_path)

        self.receipt_set_path = root / "receipt-set.json"
        receipt_set_digest = write_immutable(self.receipt_set_path, {
            "kind": "kg-stage3-processing-receipt-set", "version": "1.0",
            "count": 6095, "receipts": []})

        self.backup_path = root / "backup.json"
        backup_digest = write_immutable(self.backup_path, {
            "kind": "kg-stage2-backup-receipt", "version": "1.0", "dump_sha256": "d" * 64})

        self.schedule_path = root / "schedule.json"
        schedule_digest = write_immutable(self.schedule_path, {
            "kind": "kg-stage3-receipt-batch-schedule", "version": "1.0",
            "plan_digest": plan_digest, "cursor": 6100, "batch_size": 500,
            "batches": [{"batch": index} for index in range(120)],
            "totals": {"expected_writes": 58628,
                       "expected_outcomes": {"held": 996, "planned": 58628}}})

        self.proposal_path = root / "proposal.json"
        proposal = proposal_mod.build(
            plan=plan, design_packet=design, backup_receipt_path=str(self.backup_path),
            backup_receipt_digest=backup_digest, code_digest=apply.code_digest(),
            writer_role="poliscopic", batch_size=500)
        proposal_digest = write_immutable(self.proposal_path, proposal)

        self.approval_dir = root / "approvals"
        self.expected = {
            "proposal": proposal_digest, "plan": plan_digest, "design_packet": design_digest,
            "receipt_set": receipt_set_digest, "backup_receipt": backup_digest,
            "schedule": schedule_digest,
        }
        self.original_expected = dict(review.EXPECTED)
        self.original_paths = {name: getattr(review, name) for name in (
            "PROPOSAL_PATH", "PLAN_PATH", "DESIGN_PATH", "RECEIPT_SET_PATH",
            "BACKUP_PATH", "SCHEDULE_PATH")}
        self.patches = [
            patch.object(review, "PROPOSAL_PATH", self.proposal_path),
            patch.object(review, "PLAN_PATH", self.plan_path),
            patch.object(review, "DESIGN_PATH", self.design_path),
            patch.object(review, "RECEIPT_SET_PATH", self.receipt_set_path),
            patch.object(review, "BACKUP_PATH", self.backup_path),
            patch.object(review, "SCHEDULE_PATH", self.schedule_path),
            patch.object(review, "APPROVAL_DIR", self.approval_dir),
            patch.object(review, "EXPECTED", self.expected),
        ]
        for item in self.patches:
            item.start()

        app = Flask(__name__, template_folder=str(Path(__file__).resolve().parents[1] / "templates"))
        app.jinja_env.globals["current_user"] = type("Anonymous", (), {"is_authenticated": False})()
        app.register_blueprint(kg_stage3_approval_bp)
        self.client = app.test_client()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------- #

    def form(self, **overrides):
        data = {"reviewer_name": "Peter Mains",
                "confirm_digest": self.expected["proposal"],
                "approval_text": review.approval_wording()}
        for key, _label in review.CHECKLIST:
            data[f"ack_{key}"] = "yes"
        data.update(overrides)
        return data

    @property
    def record_path(self) -> Path:
        return review._record_path(self.expected["proposal"])

    # -- rendering and intelligibility ------------------------------------ #

    def test_review_page_answers_the_plain_english_questions(self):
        body = self.client.get("/kg/stage3-approval/").get_data(as_text=True)
        for phrase in ("What will change, in plain English", "Why it is needed",
                       "What will not change", "If something fails",
                       "stops at the first failure", "held rows", "replay"):
            self.assertIn(phrase, body)
        for count in ("58,628", "120", "500", "6100", "996", "6,095"):
            self.assertIn(count, body)

    def test_page_states_it_records_authorization_and_executes_nothing(self):
        body = self.client.get("/kg/stage3-approval/").get_data(as_text=True)
        self.assertIn("NOT APPROVED", body)
        self.assertIn("records authorization", body)
        self.assertIn("does <strong>not</strong> execute", body)

    def test_technical_bindings_are_expandable_not_upfront(self):
        body = self.client.get("/kg/stage3-approval/").get_data(as_text=True)
        self.assertIn("<details", body)
        self.assertIn(self.expected["proposal"], body)
        self.assertIn(self.expected["plan"], body)

    def test_no_option_is_preselected_and_identity_is_not_inferred(self):
        body = self.client.get("/kg/stage3-approval/").get_data(as_text=True)
        self.assertNotIn("checked", body)
        self.assertNotIn('value="Peter Mains"', body)
        self.assertIn('name="reviewer_name"', body)
        self.assertIn("does\n      not infer who you are", body)

    def test_the_exact_wording_is_shown_and_bound_before_submission(self):
        body = self.client.get("/kg/stage3-approval/").get_data(as_text=True)
        self.assertIn("The exact wording you are approving", body)
        self.assertIn(review.approval_wording(), body)
        self.assertIn("58,628 append-only processing receipts", body)
        self.assertIn("120 batches", body)

    def test_all_six_acknowledgements_are_present(self):
        body = self.client.get("/kg/stage3-approval/").get_data(as_text=True)
        self.assertEqual(len(review.CHECKLIST), 6)
        for key, _label in review.CHECKLIST:
            self.assertIn(f'name="ack_{key}"', body)

    # -- the happy path and create-once semantics ------------------------- #

    def test_approval_records_an_immutable_0600_record_and_confirms(self):
        response = self.client.post("/kg/stage3-approval/approve", data=self.form())
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("Authorization recorded — nothing has executed", body)

        self.assertTrue(self.record_path.is_file())
        mode = os.stat(self.record_path).st_mode & 0o777
        self.assertEqual(mode, 0o600)
        record = load_verified(self.record_path)
        self.assertEqual(record["reviewer_name"], "Peter Mains")
        self.assertEqual(record["approval_text"], review.approval_wording())
        self.assertEqual(record["proposal_digest"], self.expected["proposal"])
        self.assertEqual(record["plan_digest"], self.expected["plan"])
        self.assertEqual(record["design_packet_digest"], self.expected["design_packet"])
        self.assertEqual(record["backup_receipt_digest"], self.expected["backup_receipt"])
        self.assertEqual(record["schedule_digest"], self.expected["schedule"])
        self.assertEqual(record["code_digest"], apply.code_digest())
        self.assertEqual(record["scope"]["remaining_writes"], 58628)
        self.assertEqual(record["scope"]["held_after_cursor"], 996)
        self.assertFalse(record["execution"]["executed"])
        self.assertIn(record["digest"], body)

    def test_the_write_leaves_no_temporary_file_behind(self):
        self.client.post("/kg/stage3-approval/approve", data=self.form())
        leftovers = [name for name in os.listdir(self.approval_dir) if name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_a_duplicate_submission_is_refused_and_does_not_replace_the_record(self):
        self.client.post("/kg/stage3-approval/approve", data=self.form())
        original = self.record_path.read_text()
        again = self.client.post("/kg/stage3-approval/approve",
                                 data=self.form(reviewer_name="Someone Else"))
        self.assertEqual(again.status_code, 409)
        self.assertEqual(self.record_path.read_text(), original)

    def test_the_record_cannot_match_any_runner_glob(self):
        name = self.record_path.name
        for pattern in ("kg-stage3-processing-receipt-apply-*.json",
                        "kg-stage3-processing-receipt-preflight-*.json",
                        "kg-stage3-processing-receipt-checkpoint-*.json"):
            self.assertFalse(Path(name).match(pattern), pattern)
        self.assertNotIn("kg-receipts", str(self.record_path))

    # -- refusal paths ---------------------------------------------------- #

    def test_blank_identity_is_refused_and_writes_nothing(self):
        for value in ("", "   "):
            response = self.client.post("/kg/stage3-approval/approve",
                                        data=self.form(reviewer_name=value))
            self.assertEqual(response.status_code, 400)
            self.assertIn("Your name is required", response.get_data(as_text=True))
        self.assertFalse(self.record_path.exists())

    def test_incomplete_acknowledgement_is_refused(self):
        data = self.form()
        del data["ack_stop_on_failure"]
        response = self.client.post("/kg/stage3-approval/approve", data=data)
        self.assertEqual(response.status_code, 400)
        self.assertIn("acknowledgement", response.get_data(as_text=True))
        self.assertFalse(self.record_path.exists())

    def test_altered_approval_wording_is_refused(self):
        response = self.client.post("/kg/stage3-approval/approve",
                                    data=self.form(approval_text="I approve everything forever"))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(self.record_path.exists())

    def test_a_form_supplied_digest_cannot_substitute_for_the_real_one(self):
        response = self.client.post("/kg/stage3-approval/approve",
                                    data=self.form(confirm_digest="f" * 64))
        self.assertEqual(response.status_code, 400)
        self.assertIn("does not match the exact bound proposal", response.get_data(as_text=True))
        self.assertFalse(self.record_path.exists())

    def test_a_stale_proposal_digest_is_refused(self):
        write_immutable(self.proposal_path.parent / "unused.json", {"kind": "x"})
        tampered = dict(load_verified(self.proposal_path), batch_size=999)
        self.proposal_path.write_text(__import__("json").dumps(tampered))
        response = self.client.get("/kg/stage3-approval/")
        self.assertEqual(response.status_code, 409)

    def test_an_obsolete_proposal_is_refused(self):
        marker = self.proposal_path.parent / f"{self.proposal_path.name}.obsolete.json"
        write_immutable(marker, {"kind": "artifact-obsolete", "target": self.proposal_path.name})
        response = self.client.get("/kg/stage3-approval/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("obsolete", response.get_data(as_text=True))

    def test_a_malformed_proposal_is_refused(self):
        broken = dict(load_verified(self.proposal_path), state="authorized", enabled=True)
        self.proposal_path.write_text(__import__("json").dumps(broken))
        self.assertEqual(self.client.get("/kg/stage3-approval/").status_code, 409)

    def test_a_schedule_that_disagrees_with_the_counts_is_refused(self):
        tampered = {"kind": "kg-stage3-receipt-batch-schedule", "version": "1.0",
                    "plan_digest": self.expected["plan"], "cursor": 6095, "batch_size": 500,
                    "batches": [{"batch": 0}],
                    "totals": {"expected_writes": 1, "expected_outcomes": {}}}
        path = Path(self.temp_dir.name) / "tampered-schedule.json"
        digest = write_immutable(path, tampered)
        # Bind the tampered artifact as the reviewed one, so the count guard is what fires.
        with patch.object(review, "SCHEDULE_PATH", path), \
                patch.object(review, "EXPECTED", {**self.expected, "schedule": digest}):
            response = self.client.get("/kg/stage3-approval/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("cursor", response.get_data(as_text=True))

    def test_a_non_development_target_is_refused(self):
        production = dict(load_verified(self.original_paths["PLAN_PATH"]),
                          target={"tier": "production", "database": "poliscopic"})
        plan_path = Path(self.temp_dir.name) / "production-plan.json"
        plan_digest = write_immutable(plan_path, production)
        proposal = proposal_mod.build(
            plan=load_verified(plan_path), design_packet=load_verified(self.design_path),
            backup_receipt_path=str(self.backup_path),
            backup_receipt_digest=self.expected["backup_receipt"],
            code_digest=apply.code_digest(), writer_role="poliscopic", batch_size=500)
        proposal_path = Path(self.temp_dir.name) / "production-proposal.json"
        proposal_digest = write_immutable(proposal_path, proposal)
        # Both artifacts are internally consistent, so only the target guard can refuse.
        with patch.object(review, "PLAN_PATH", plan_path), \
                patch.object(review, "PROPOSAL_PATH", proposal_path), \
                patch.object(review, "EXPECTED", {**self.expected, "plan": plan_digest,
                                                  "proposal": proposal_digest}):
            response = self.client.get("/kg/stage3-approval/")
        self.assertEqual(response.status_code, 409)
        self.assertIn("development target", response.get_data(as_text=True))

    # -- nothing execution-shaped is reachable ---------------------------- #

    def test_the_route_module_reaches_no_authorization_preflight_runner_or_database(self):
        self.assertFalse(hasattr(review, "authorization"))
        self.assertFalse(hasattr(review, "preflight"))
        self.assertFalse(hasattr(review, "get_engine"))
        self.assertFalse(hasattr(review, "engine"))
        source = Path(review.__file__).read_text()
        for forbidden in ("authorization.build", "preflight.build", "continue_batches",
                          "apply_batch", "get_engine", "create_engine", "serenity"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_neither_get_nor_post_invokes_the_apply_machinery(self):
        def explode(*_args, **_kwargs):
            raise AssertionError("the approval page must never execute apply machinery")

        with patch.object(apply, "gate", explode), patch.object(apply, "apply_batch", explode), \
                patch.object(proposal_mod, "build", explode):
            self.assertEqual(self.client.get("/kg/stage3-approval/").status_code, 200)
            self.assertEqual(
                self.client.post("/kg/stage3-approval/approve", data=self.form()).status_code, 200)
        self.assertTrue(self.record_path.is_file())

    # -- the pinned bindings match the real artifacts --------------------- #

    def test_pinned_bindings_match_the_real_repository_artifacts(self):
        """Guards against the page drifting from the operation it claims to present."""
        pinned = {"proposal": "f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60",
                  "plan": "73359d2df800b5d8f4e1a399bfc925141e4617e82f0509354470e6f4478eb2f9",
                  "design_packet": "ae86c35948ca6e214e5699e799897811b2517d285e003c4126c7c6a44fea852c",
                  "receipt_set": "95f8e5d04636624c679144e5b9e8f938abf1bfc816bb39f69cfc22bb05bd8a0c",
                  "backup_receipt": "3eb11bbec9bdbff7c9e7ecd5a46a0f17709c318cdebbac4425fe03d3afbccd7e",
                  "schedule": "117da8dd098d7483de49985e52545f28d1817e71c4293fb00714a2c2950616b2"}
        # The module's own defaults, captured before this test's patches replaced them.
        self.assertEqual(self.original_expected, pinned)
        for path in self.original_paths.values():
            if not path.is_file():
                self.skipTest(f"{path} is not present in this checkout")
        self.assertEqual(load_verified(self.original_paths["PROPOSAL_PATH"])["digest"],
                         pinned["proposal"])
        self.assertEqual(load_verified(self.original_paths["SCHEDULE_PATH"])["digest"],
                         pinned["schedule"])
        self.assertEqual(load_verified(self.original_paths["PLAN_PATH"])["digest"],
                         pinned["plan"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
