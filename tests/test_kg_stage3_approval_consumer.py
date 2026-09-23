"""Focused tests for the approval-record consumer.

The consumer is the only path from a human decision to an authorization, so its tests are
about what it *refuses*.  Every refusal below is a way a broadened, substituted, replayed,
or drifted approval could otherwise slip through: a different record, a different
reviewer, a stale artifact, drifted code, a widened scope, or a second packet.

These are consumer tests, deliberately independent of the UI tests: the page not being
reachable says nothing about whether the consumer would accept the wrong record.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.kg import stage3_approval_consumer as consumer
from scripts.kg import stage3_processing_receipt as receipt
from scripts.kg import stage3_processing_receipt_apply as apply
from scripts.kg import stage3_processing_receipt_apply_packet as authorization
from scripts.kg.stage2_artifacts import write_immutable

TERMINAL_DIR = Path("data/kg-receipts")
PLANS_DIR = Path("data/kg-plans")


def _record():
    return consumer.load_record()[1]


def _mutated(**changes):
    """The real record with fields changed; the digest is recomputed to stay coherent."""
    updated = dict(_record())
    updated.update(changes)
    updated.pop("digest", None)
    updated["digest"] = receipt.canonical_sha256(updated)
    return updated


def _no_other_packets():
    """Stub for the ambiguity scan.

    The real plans directory already holds the packet this task generated, which correctly
    refuses a second packet for the same approval.  The happy-path tests therefore stub the
    scan so they exercise the build itself; the scan's own behaviour is tested explicitly.
    """
    return []


class ConsumerRefusalTest(unittest.TestCase):
    """Field-level refusals, driven through validate_record against the real artifacts."""

    @classmethod
    def setUpClass(cls):
        cls.record = _record()
        cls.artifacts = consumer.load_artifacts(cls.record)

    def assert_refused(self, record, fragment):
        problems = consumer.validate_record(record, artifacts=self.artifacts)
        self.assertTrue(problems, f"expected a refusal mentioning {fragment!r}")
        self.assertTrue(any(fragment in problem for problem in problems),
                        f"{fragment!r} not in {problems}")

    def test_the_real_record_validates(self):
        self.assertEqual(consumer.validate_record(self.record, artifacts=self.artifacts), [])

    def test_a_different_record_kind_or_version_is_refused(self):
        self.assert_refused(_mutated(kind="something-else"), "approved kind and version")
        self.assert_refused(_mutated(version="2.0"), "approved kind and version")

    def test_a_different_or_blank_reviewer_is_refused(self):
        self.assert_refused(_mutated(reviewer_name="Someone Else"), "approved reviewer")
        self.assert_refused(_mutated(reviewer_name="   "), "approved reviewer")

    def test_a_missing_or_unparseable_timestamp_is_refused(self):
        self.assert_refused(_mutated(approved_at=None), "no approval timestamp")
        self.assert_refused(_mutated(approved_at="not-a-time"), "not parseable")

    def test_a_record_claiming_execution_is_refused(self):
        self.assert_refused(_mutated(execution={"executed": True, "executed_at": "x",
                                                "authorization_packet": None,
                                                "preflight": None}),
                            "nothing executed")
        self.assert_refused(_mutated(execution={"executed": False, "executed_at": None,
                                                "authorization_packet": "deadbeef",
                                                "preflight": None}),
                            "nothing executed")

    def test_altered_review_boundary_flags_are_refused(self):
        boundary = dict(consumer.PINNED_REVIEW_BOUNDARY, is_apply_terminal_receipt=True)
        self.assert_refused(_mutated(review_boundary=boundary), "review-boundary flags")

    def test_altered_acknowledgements_are_refused(self):
        self.assert_refused(_mutated(acknowledged=consumer.PINNED_ACKNOWLEDGED[:-1]),
                            "acknowledgements")
        self.assert_refused(_mutated(acknowledged=[]), "acknowledgements")

    def test_a_different_proposal_binding_is_refused(self):
        self.assert_refused(_mutated(proposal_digest="0" * 64), "approved proposal")

    def test_approval_text_must_be_present_and_name_the_proposal(self):
        self.assert_refused(_mutated(approval_text=""), "no approval text")
        self.assert_refused(_mutated(approval_text="I approve everything"), "does not name")

    def test_code_drift_is_refused(self):
        self.assert_refused(_mutated(code_digest="f" * 64), "code binding has drifted")

    def test_target_inequalities_are_refused(self):
        self.assert_refused(_mutated(target={**self.record["target"], "database": "other"}),
                            "reviewed proposal target")

    def test_a_non_development_target_is_refused(self):
        target = dict(self.record["target"], tier="production", database="poliscopic_dev")
        problems = consumer.validate_record(_mutated(target=target), artifacts=self.artifacts)
        self.assertTrue(problems)

    def test_scope_drift_is_refused(self):
        widened = dict(self.record["scope"], remaining_writes=999999)
        self.assert_refused(_mutated(scope=widened), "scope value remaining_writes")
        narrowed = dict(self.record["scope"], cursor=6095)
        self.assert_refused(_mutated(scope=narrowed), "scope value cursor")

    def test_a_derived_count_that_disagrees_with_the_record_is_refused(self):
        drifted = dict(consumer.derive_scope(self.artifacts))
        drifted["remaining_writes"] = 1
        with patch.object(consumer, "derive_scope", lambda _artifacts: drifted):
            problems = consumer.validate_record(self.record, artifacts=self.artifacts)
        self.assertTrue(any("derived remaining_writes" in problem for problem in problems))

    def test_the_derived_scope_matches_the_reviewed_scope_exactly(self):
        derived = consumer.derive_scope(self.artifacts)
        for key, expected in consumer.PINNED_SCOPE.items():
            self.assertEqual(derived.get(key), expected, key)
        self.assertEqual(derived["held_in_consumed_prefix"], 5)


class ConsumerContractTest(unittest.TestCase):
    """Shape and side-effect guarantees of the consumer itself."""

    def test_the_consumer_exposes_no_flag_that_could_widen_acceptance(self):
        with self.assertRaises(SystemExit):
            consumer.main(["--out", "x.json", "--record-digest", "0" * 64])
        with self.assertRaises(SystemExit):
            consumer.main(["--out", "x.json", "--reviewer", "Someone Else"])

    def test_the_pinned_lineage_is_a_single_record_and_proposal(self):
        self.assertEqual(consumer.PINNED_RECORD_DIGEST,
                         "95590e342db23701344b2c20811839dbd44377ecaf381470bb003a1c9820eead")
        self.assertEqual(consumer.PINNED_PROPOSAL_DIGEST,
                         "f6a06b59e8da8bfdaef5e460a3659961931d6f3a19f0dd2dd237fa33c6202f60")
        self.assertEqual(consumer.PINNED_REVIEWER, "Peter Mains")
        self.assertEqual(len(consumer.PINNED_ACKNOWLEDGED), 6)
        self.assertEqual(consumer.load_record()[0], consumer.PINNED_RECORD)

    def test_a_missing_or_wrong_digest_record_is_refused(self):
        with patch.object(consumer, "PINNED_RECORD", Path("/nonexistent/record.json")):
            with self.assertRaises(consumer.ConsumptionRefused):
                consumer.load_record()
        with patch.object(consumer, "PINNED_RECORD_DIGEST", "0" * 64):
            with self.assertRaises(consumer.ConsumptionRefused):
                consumer.load_record()

    def test_an_obsolete_record_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / consumer.PINNED_RECORD.name
            write_immutable(target, dict(_record()))
            write_immutable(Path(directory) / f"{target.name}.obsolete.json",
                            {"kind": "artifact-obsolete", "target": target.name})
            with patch.object(consumer, "PINNED_RECORD", target):
                with self.assertRaises(consumer.ConsumptionRefused):
                    consumer.load_record()

    def test_the_consumer_reaches_no_preflight_runner_or_write_path(self):
        source = Path(consumer.__file__).read_text()
        for forbidden in ("apply_batch", "continue_batches", "serenity", "preflight.build",
                          "get_engine", "create_engine"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_an_existing_packet_for_this_approval_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            plans = Path(directory)
            write_immutable(plans / "kg-stage3-processing-receipt-authorized-apply-a.json",
                            {"kind": authorization.KIND, "state": "authorized",
                             "approval_record_digest": consumer.PINNED_RECORD_DIGEST})
            with patch.object(consumer, "existing_authorized_packets",
                              lambda: sorted(plans.glob(
                                  "kg-stage3-processing-receipt-authorized-apply-*.json"))):
                with self.assertRaises(consumer.ConsumptionRefused):
                    consumer.refuse_ambiguity()

    def test_an_existing_packet_for_another_approval_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            plans = Path(directory)
            write_immutable(plans / "kg-stage3-processing-receipt-authorized-apply-b.json",
                            {"kind": authorization.KIND, "state": "authorized",
                             "approval_record_digest": "a" * 64})
            with patch.object(consumer, "existing_authorized_packets",
                              lambda: sorted(plans.glob(
                                  "kg-stage3-processing-receipt-authorized-apply-*.json"))):
                with self.assertRaises(consumer.ConsumptionRefused):
                    consumer.refuse_ambiguity()

    def test_legacy_packets_without_an_approval_binding_do_not_block(self):
        with tempfile.TemporaryDirectory() as directory:
            plans = Path(directory)
            write_immutable(plans / "kg-stage3-processing-receipt-authorized-apply-legacy.json",
                            {"kind": authorization.KIND, "state": "authorized",
                             "plan_digest": "4dbd40317" + "0" * 55})
            with patch.object(consumer, "existing_authorized_packets",
                              lambda: sorted(plans.glob(
                                  "kg-stage3-processing-receipt-authorized-apply-*.json"))):
                self.assertIsNone(consumer.refuse_ambiguity())


class ConsumerBuildTest(unittest.TestCase):
    """The packet and its side effects."""

    def test_building_writes_exactly_one_packet_and_no_terminal_receipt(self):
        before = sorted(path.name for path in
                        TERMINAL_DIR.glob("kg-stage3-processing-receipt-apply-*.json"))
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "packet.json"
            with patch.object(consumer, "existing_authorized_packets", _no_other_packets):
                result = consumer.consume(out)
            self.assertTrue(out.is_file())
            self.assertEqual(json.loads(out.read_text())["digest"], result["packet_digest"])
        after = sorted(path.name for path in
                       TERMINAL_DIR.glob("kg-stage3-processing-receipt-apply-*.json"))
        self.assertEqual(before, after, "consuming must create no terminal receipt")

    def test_the_packet_is_authorized_and_binds_the_human_record_verbatim(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "packet.json"
            with patch.object(consumer, "existing_authorized_packets", _no_other_packets):
                consumer.consume(out)
            packet = json.loads(out.read_text())
        self.assertEqual(packet["state"], "authorized")
        self.assertIs(packet["enabled"], True)
        self.assertEqual(packet["kind"], authorization.KIND)
        self.assertEqual(packet["approval_record_digest"], consumer.PINNED_RECORD_DIGEST)
        self.assertEqual(packet["approver"], consumer.PINNED_REVIEWER)
        self.assertEqual(packet["approved_by"], consumer.PINNED_REVIEWER)
        self.assertEqual(packet["approval_text"], _record()["approval_text"])
        self.assertEqual(packet["approved_at"], _record()["approved_at"])
        self.assertEqual(packet["approval_text_sha256"],
                         __import__("hashlib").sha256(
                             _record()["approval_text"].encode()).hexdigest())
        self.assertEqual(packet["code_digest"], apply.code_digest())

    def test_the_packet_cannot_reach_an_apply_without_a_fresh_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "packet.json"
            with patch.object(consumer, "existing_authorized_packets", _no_other_packets):
                consumer.consume(out)
            packet = json.loads(out.read_text())
        artifacts = consumer.load_artifacts(_record())
        problems = apply.gate(engine=None, plan=artifacts["plan"],
                              design_packet=artifacts["design_packet"],
                              apply_packet=packet, backup_path=None,
                              authorization_token="", preflight_document=None)
        self.assertTrue(any("preflight" in problem for problem in problems))

    def test_refuses_to_overwrite_or_regenerate(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "packet.json"
            with patch.object(consumer, "existing_authorized_packets", _no_other_packets):
                consumer.consume(out)
            with self.assertRaises(consumer.ConsumptionRefused):
                consumer.consume(out)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
