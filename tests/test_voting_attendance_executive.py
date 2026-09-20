"""Tests for voting, attendance, dissent, and executive session participant tracking.

Tests cover:
- DB table creation for active voting models
- Vote analysis (split/unanimous/tie detection, dissent flagging)
- Attendance inference (explicit vs inferred absence)
- Executive session participant extraction
"""

import importlib.util
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from sqlalchemy import create_engine, select, text as sa_text
from sqlalchemy.orm import Session


# Import db normally — the tests create their own in-memory engines
# and never rely on db.core.get_engine() or db.core.DATABASE_URL,
# so there's no need to force-reload the module (which would destroy
# the shared test database URL that conftest.py set up).
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import db


class TestMeetingAttendanceTable(unittest.TestCase):
    """Test that MeetingAttendance table works."""

    def test_explicit_present(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            a = db.MeetingAttendance(
                body="bos",
                meeting_id="4690",
                member_id=1,
                attendance_status="present",
                source_text="Present: Chairman Jack",
            )
            session.add(a)
            session.commit()
            ret = session.execute(
                select(db.MeetingAttendance).where(db.MeetingAttendance.body == "bos")
            ).scalar_one()
            self.assertEqual(ret.attendance_status, "present")

    def test_inferred_absent(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            a = db.MeetingAttendance(
                body="bos",
                meeting_id="4690",
                member_id=2,
                attendance_status="inferred_absent",
                source_text="Member did not vote while others did",
                inference_method="missing_vote_when_others_voted",
            )
            session.add(a)
            session.commit()
            ret = session.execute(
                select(db.MeetingAttendance).where(db.MeetingAttendance.member_id == 2)
            ).scalar_one()
            self.assertEqual(ret.attendance_status, "inferred_absent")
            self.assertEqual(ret.inference_method, "missing_vote_when_others_voted")

    def test_attendance_statuses(self):
        """Each valid attendance status can be stored (different member_ids to avoid unique constraint)."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        valid_statuses = {"present", "absent", "excused", "late", "left_early", "unknown", "inferred_absent"}
        with Session(engine) as session:
            for i, status in enumerate(sorted(valid_statuses)):
                a = db.MeetingAttendance(
                    body="bos", meeting_id="test", member_id=i + 1,
                    attendance_status=status, source_text="test",
                )
                session.add(a)
            session.commit()
            count = session.execute(select(db.MeetingAttendance)).scalars().all()
            self.assertEqual(len(count), len(valid_statuses))


class TestAgendaItemVotesEnhancements(unittest.TestCase):
    """Test that agenda_item_votes has the new split/unanimous fields."""

    def test_columns_exist(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        from sqlalchemy import inspect as sa_inspect
        insp = sa_inspect(engine)
        cols = [c["name"] for c in insp.get_columns("agenda_item_votes")]
        self.assertIn("is_split_vote", cols)
        self.assertIn("unanimous", cols)
        self.assertIn("majority_position", cols)

    def test_split_vote_true(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            v = db.AgendaItemVote(
                body="bos", meeting_id="4690", agenda_item_id=1,
                agenda_item_number=1,
                motion_result="approved",
                is_split_vote=True, unanimous=False,
                majority_position="yes",
            )
            session.add(v)
            session.commit()
            ret = session.execute(select(db.AgendaItemVote)).scalar_one()
            self.assertTrue(ret.is_split_vote)
            self.assertFalse(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")


class TestMemberVotesTable(unittest.TestCase):
    """Test that MemberVote table works."""

    def test_member_vote_persistence(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            # Create an agenda_item_vote first
            aiv = db.AgendaItemVote(
                body="bos", meeting_id="4690", agenda_item_id=1,
                agenda_item_number=1,
                motion_result="approved",
            )
            session.add(aiv)
            session.flush()

            mv = db.MemberVote(
                agenda_item_vote_id=aiv.id,
                member_id=1,
                vote="yes",
                raw_vote_text="Aye",
                is_dissent=False,
            )
            session.add(mv)
            session.commit()
            ret = session.execute(select(db.MemberVote)).scalar_one()
            self.assertEqual(ret.vote, "yes")
            self.assertFalse(ret.is_dissent)

    def test_dissent_flag(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = db.AgendaItemVote(body="bos", meeting_id="4690", agenda_item_id=1, agenda_item_number=1, motion_result="approved", majority_position="yes")
            session.add(aiv)
            session.flush()
            mv = db.MemberVote(agenda_item_vote_id=aiv.id, member_id=1, vote="no", raw_vote_text="Nay", is_dissent=True)
            session.add(mv)
            session.commit()
            ret = session.execute(select(db.MemberVote)).scalar_one()
            self.assertTrue(ret.is_dissent)

    def test_vote_values(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        valid_votes = {"yes", "no", "abstain", "recused", "absent", "not_voting", "unknown"}
        with Session(engine) as session:
            aiv = db.AgendaItemVote(body="bos", meeting_id="4690", agenda_item_id=1, agenda_item_number=1, motion_result="approved")
            session.add(aiv)
            session.flush()
            for v in valid_votes:
                mv = db.MemberVote(agenda_item_vote_id=aiv.id, member_id=hash(v), vote=v)
                session.add(mv)
            session.commit()
            count = session.execute(select(db.MemberVote)).scalars().all()
            self.assertEqual(len(count), len(valid_votes))


class TestExecutiveSessionParticipantsTable(unittest.TestCase):
    """Test that ExecutiveSessionParticipant table works."""

    def test_persistence(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            esp = db.ExecutiveSessionParticipant(
                body="bos",
                meeting_id="4690",
                person_name="Kory Langhofer",
                normalized_name="kory langhofer",
                role_or_title="Outside Counsel",
                organization="Brown & Langhofer PLLC",
                participation_type="legal_counsel",
                agenda_item_number=1,
                source_text="Legal advice from Kory Langhofer",
                source_url="https://example.com/agenda",
            )
            session.add(esp)
            session.commit()
            ret = session.execute(
                select(db.ExecutiveSessionParticipant).where(db.ExecutiveSessionParticipant.person_name == "Kory Langhofer")
            ).scalar_one()
            self.assertEqual(ret.participation_type, "legal_counsel")

    def test_participation_types(self):
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        valid_types = {"advised", "attended", "presented", "legal_counsel", "staff", "outside_counsel", "unknown"}
        with Session(engine) as session:
            for pt in valid_types:
                esp = db.ExecutiveSessionParticipant(
                    body="bos", meeting_id="test", person_name=f"Person {pt}",
                    normalized_name=f"person {pt}", participation_type=pt,
                )
                session.add(esp)
            session.commit()
            count = session.execute(select(db.ExecutiveSessionParticipant)).scalars().all()
            self.assertEqual(len(count), len(valid_types))


class TestSplitVoteDetection(unittest.TestCase):
    """Test split vote, unanimous, and majority position detection via AgendaItemVote DB records."""

    def _make_vote(self, session, body, meeting_id, item_num, votes_list):
        """Create an AgendaItemVote with SupervisorVotes and run detect."""
        from db import Supervisor
        aiv = db.AgendaItemVote(
            body=body, meeting_id=meeting_id,
            agenda_item_id=item_num, agenda_item_number=item_num,
            motion_result="approved",
        )
        session.add(aiv)
        session.flush()
        for sv_data in votes_list:
            sup_name = sv_data["name"]
            norm = sup_name.lower()
            sup = session.execute(
                select(Supervisor).where(Supervisor.normalized_name == norm)
            ).scalar_one_or_none()
            if not sup:
                sup = Supervisor(name=sup_name, normalized_name=norm)
                session.add(sup)
                session.flush()
            mv = db.MemberVote(
                agenda_item_vote_id=aiv.id,
                member_id=sup.id,
                vote=sv_data["vote"],
            )
            session.add(mv)
        session.flush()
        return aiv

    def test_unanimous_yes(self):
        """All votes 'yes' = unanimous, not split, majority yes."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "yes"},
            ])
            db._detect_vote_attributes([aiv])
            session.commit()
            # Re-fetch to get updated values
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertFalse(ret.is_split_vote)
            self.assertTrue(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")

    def test_split_vote(self):
        """At least one yes and one no = split vote."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "no"},
            ])
            db._detect_vote_attributes([aiv])
            session.commit()
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertTrue(ret.is_split_vote)
            self.assertFalse(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")

    def test_tie_vote(self):
        """Equal yes and no = tie."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "no"},
            ])
            db._detect_vote_attributes([aiv])
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertTrue(ret.is_split_vote)
            self.assertEqual(ret.majority_position, "tie")

    def test_no_abstention_not_dissent(self):
        """Abstentions don't affect unanimous/split."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "abstain"},
            ])
            db._detect_vote_attributes([aiv])
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertFalse(ret.is_split_vote)
            self.assertTrue(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")

    def test_all_no_unanimous(self):
        """All votes 'no' = unanimous opposition, not split."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "no"},
                {"name": "Member B", "vote": "no"},
            ])
            db._detect_vote_attributes([aiv])
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertFalse(ret.is_split_vote)
            self.assertTrue(ret.unanimous)
            self.assertEqual(ret.majority_position, "no")
        """All votes 'yes' = unanimous, not split, majority yes."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "yes"},
            ])
            db._detect_vote_attributes([aiv])
            session.commit()
            # Re-fetch to get updated values
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertFalse(ret.is_split_vote)
            self.assertTrue(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")

    def test_split_vote(self):
        """At least one yes and one no = split vote."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "no"},
            ])
            db._detect_vote_attributes([aiv])
            session.commit()
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertTrue(ret.is_split_vote)
            self.assertFalse(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")

    def test_tie_vote(self):
        """Equal yes and no = tie."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "no"},
            ])
            db._detect_vote_attributes([aiv])
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertTrue(ret.is_split_vote)
            self.assertEqual(ret.majority_position, "tie")

    def test_no_abstention_not_dissent(self):
        """Abstentions don't affect unanimous/split."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "abstain"},
            ])
            db._detect_vote_attributes([aiv])
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertFalse(ret.is_split_vote)
            self.assertTrue(ret.unanimous)
            self.assertEqual(ret.majority_position, "yes")

    def test_all_no_unanimous(self):
        """All votes 'no' = unanimous opposition, not split."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            aiv = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "no"},
                {"name": "Member B", "vote": "no"},
            ])
            db._detect_vote_attributes([aiv])
            ret = session.execute(select(db.AgendaItemVote).where(db.AgendaItemVote.id == aiv.id)).scalar_one()
            self.assertFalse(ret.is_split_vote)
            self.assertTrue(ret.unanimous)
            self.assertEqual(ret.majority_position, "no")


class TestDissentDetection(unittest.TestCase):
    """Test that dissenting votes are correctly flagged."""

    def _make_vote(self, session, body, meeting_id, item_num, votes_list):
        """Create AgendaItemVote with SupervisorVotes and run detect."""
        from db import Supervisor
        aiv = db.AgendaItemVote(
            body=body, meeting_id=meeting_id,
            agenda_item_id=item_num, agenda_item_number=item_num,
            motion_result="approved",
        )
        session.add(aiv)
        session.flush()
        for sv_data in votes_list:
            sup_name = sv_data["name"]
            norm = sup_name.lower()
            sup = session.execute(
                select(Supervisor).where(Supervisor.normalized_name == norm)
            ).scalar_one_or_none()
            if not sup:
                sup = Supervisor(name=sup_name, normalized_name=norm)
                session.add(sup)
                session.flush()
            mv = db.MemberVote(
                agenda_item_vote_id=aiv.id,
                member_id=sup.id,
                vote=sv_data["vote"],
            )
            session.add(mv)
        session.flush()
        db._detect_vote_attributes([aiv])
        session.commit()
        # Re-fetch MemberVote rows to check is_dissent
        return session.execute(
            select(db.MemberVote).where(db.MemberVote.agenda_item_vote_id == aiv.id)
        ).scalars().all()

    def test_no_dissent_when_majority(self):
        """Voting with the majority is not dissent."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            svs = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "yes"},
                {"name": "Member C", "vote": "no"},
            ])
            for sv in svs:
                if sv.vote == "yes":
                    self.assertFalse(sv.is_dissent)
                elif sv.vote == "no":
                    self.assertTrue(sv.is_dissent)

    def test_abstention_not_dissent(self):
        """Abstentions should not be flagged as dissent."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            svs = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "yes"},
                {"name": "Member B", "vote": "abstain"},
            ])
            for sv in svs:
                if sv.vote == "abstain":
                    self.assertFalse(sv.is_dissent)

    def test_all_no_majority_no_dissent(self):
        """If all vote no, no one dissents."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            svs = self._make_vote(session, "bos", "4690", 1, [
                {"name": "Member A", "vote": "no"},
                {"name": "Member B", "vote": "no"},
            ])
            for sv in svs:
                self.assertFalse(sv.is_dissent)


class TestInferredAbsenceDetection(unittest.TestCase):
    """Test inference of absence when members don't vote."""

    def test_infer_absence(self):
        """A known member with no vote when others voted is inferred absent."""
        engine = create_engine("sqlite:///:memory:")
        db.Base.metadata.create_all(engine)
        with Session(engine) as session:
            # Only member A voted
            aiv = db.AgendaItemVote(body="bos", meeting_id="4690", agenda_item_id=1, agenda_item_number=1, motion_result="approved")
            session.add(aiv)
            session.flush()
            mv = db.MemberVote(agenda_item_vote_id=aiv.id, member_id=1, vote="yes")
            session.add(mv)
            session.commit()

            # Detect missing votes
            inferred = db.infer_absence_for_meeting(session, "bos", "4690", [1, 2], [1])
            self.assertEqual(len(inferred), 1)
            self.assertEqual(inferred[0].member_id, 2)
            self.assertEqual(inferred[0].attendance_status, "inferred_absent")
            self.assertEqual(inferred[0].inference_method, "missing_vote_when_others_voted")


if __name__ == "__main__":
    unittest.main()
