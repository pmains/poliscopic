"""Data integrity tests for all synced public bodies.

Validates that synced data across BOS, PZ, ADJ, Health, TAB, IDA,
Tempe City Council, and Tempe commissions is structurally sound.

Uses the real database path explicitly so these tests work even when
other test modules have switched the database URL to a temp file.
"""
import os
import sys
import unittest
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test

from db import get_session, text, set_database_url
from sqlalchemy import select, func


# These tests assert the integrity of the local historical SQLite archive. That
# archive is deliberately never versioned (`.gitignore` ignores data/), so a clean
# checkout legitimately does not contain it. Make the dependency explicit and
# overridable instead of letting the whole module fail with "no such table".
_ARCHIVE_ENV = "POLISCOPIC_ARCHIVE_DB"
_real_db_path = os.environ.get(
    _ARCHIVE_ENV,
    str(Path(__file__).resolve().parent.parent / "data" / "maricopa.sqlite"),
)
if not Path(_real_db_path).is_file():
    pytest.skip(
        f"data-integrity archive not present at {_real_db_path}. These tests "
        f"assert the integrity of the local historical SQLite archive, which is "
        f"never versioned; set {_ARCHIVE_ENV}=<path> to run them.",
        allow_module_level=True,
    )


def _archive_has_schema(path: str) -> bool:
    """True when the archive actually contains the tables these tests assert on.

    Existence is not enough: `data/maricopa.sqlite` in a working checkout is a
    4 KB stub with zero tables, so every test would fail with "no such table"
    and drown the suite. The precondition is the schema, not the path.
    """
    import sqlite3

    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            names = {r[0] for r in con.execute(
                "select name from sqlite_master where type='table'")}
        finally:
            con.close()
    except Exception:
        return False
    return {"meetings", "agenda_items"} <= names


if not _archive_has_schema(_real_db_path):
    pytest.skip(
        f"the archive at {_real_db_path} has no schema (empty stub, not a "
        f"populated historical database). Set {_ARCHIVE_ENV}=<path> to a "
        "populated archive to run these data-integrity tests.",
        allow_module_level=True,
    )




def _use_real_db():
    """Force connection to the real database, not a test fixture temp DB.

    Since db.core's default DATABASE_URL now points to a temp file (not
    production), this is safe to call without saving/restoring.  Subsequent
    test modules set their own URL via set_database_url().
    """
    set_database_url(f"sqlite:///{_real_db_path}")


# _restore_db_url is not needed — the default DATABASE_URL is now a temp
# file (/tmp/poliscopic_dev.sqlite), not the production path.  Any test
# that runs after this module and calls set_database_url() or
# _reset_db_engine() will create its own temp path.


EXPECTED_BODIES = {
    "bos":       {"jur": 1, "min_meetings": 100, "label": "Maricopa BOS"},
    "pz":        {"jur": 1, "min_meetings": 10,  "label": "P&Z Commission"},
    "adj":       {"jur": 1, "min_meetings": 5,   "label": "Board of Adjustment"},
    "health":    {"jur": 1, "min_meetings": 3,   "label": "Board of Health"},
    "tab":       {"jur": 1, "min_meetings": 3,   "label": "Transportation Advisory Board"},
    "ida":       {"jur": 1, "min_meetings": 10,  "label": "Industrial Development Authority"},
    "tempe-cc":  {"jur": 2, "min_meetings": 50,  "label": "Tempe City Council"},
    "tempe-drc": {"jur": 2, "min_meetings": 20,  "label": "Tempe DRC"},
    "tempe-boa": {"jur": 2, "min_meetings": 10,  "label": "Tempe BOA"},
    "tempe-hpc": {"jur": 2, "min_meetings": 5,   "label": "Tempe HPC"},
    "tempe-ha":  {"jur": 2, "min_meetings": 5,   "label": "Tempe HA"},
    "tempe-rio": {"jur": 2, "min_meetings": 3,   "label": "Tempe Rio Salado CFD"},
    "tempe-jrc": {"jur": 2, "min_meetings": 1,   "label": "Tempe JRC"},
    "tempe-rmt": {"jur": 2, "min_meetings": 1,   "label": "Tempe RMT"},
}


@integration_test
class TestSyncedMeetingsDataIntegrity(unittest.TestCase):
    """Structural integrity tests for all synced meetings."""

    def setUp(self):
        _use_real_db()
        self.s = get_session()

    def tearDown(self):
        self.s.close()


    def _meeting_stats(self, body):
        """Return dict of meeting stats for a body."""
        row = self.s.execute(text("""
            SELECT COUNT(*) as cnt,
                   COUNT(DISTINCT meeting_id) as unique_ids,
                   MIN(meeting_date) as first,
                   MAX(meeting_date) as last,
                   SUM(item_count_actual) as items,
                   SUM(CASE WHEN sync_status IN ('complete', 'no_agenda') THEN 1 ELSE 0 END) as synced_ok
            FROM meetings WHERE body = :b
        """), {"b": body}).fetchone()
        return {
            "count": row[0], "unique": row[1], "first": row[2],
            "last": row[3], "items": row[4] or 0, "synced_ok": row[5],
        }

    def _jurisdiction(self, body):
        return self.s.execute(
            text("SELECT jurisdiction_id FROM meetings WHERE body = :b LIMIT 1"),
            {"b": body},
        ).scalar()

    # -- Per-body test generator --
    def test_all_bodies_are_represented(self):
        """Every expected body code has at least one meeting."""
        existing = set(row[0] for row in self.s.execute(
            text("SELECT DISTINCT body FROM meetings")
        ).fetchall())
        expected = set(EXPECTED_BODIES.keys())
        missing = expected - existing
        self.assertSetEqual(
            missing, set(),
            f"Bodies with no meetings: {missing}",
        )

    def test_no_null_body_codes(self):
        """No meeting has a NULL or empty body code."""
        count = self.s.execute(
            text("SELECT COUNT(*) FROM meetings WHERE body IS NULL OR body = ''")
        ).scalar()
        self.assertEqual(count, 0)

    def test_all_meetings_have_jurisdiction(self):
        """No meeting has a NULL jurisdiction_id."""
        count = self.s.execute(
            text("SELECT COUNT(*) FROM meetings WHERE jurisdiction_id IS NULL")
        ).scalar()
        self.assertEqual(count, 0, f"{count} meeting(s) with NULL jurisdiction_id")


def _make_body_tests():
    """Generate per-body test methods."""
    for body_code, cfg in EXPECTED_BODIES.items():
        def _test(self, b=body_code, cfg=cfg):
            stats = self._meeting_stats(b)
            jur = self._jurisdiction(b)
            # Jurisdiction check
            self.assertEqual(
                jur, cfg["jur"],
                f"{cfg['label']}: expected jurisdiction_id={cfg['jur']}, got {jur}",
            )
            # Minimum meeting count
            self.assertGreaterEqual(
                stats["count"], cfg["min_meetings"],
                f"{cfg['label']}: expected >= {cfg['min_meetings']} meetings, got {stats['count']}",
            )
            # No duplicate meeting_ids
            self.assertEqual(
                stats["count"], stats["unique"],
                f"{cfg['label']}: duplicate meeting_ids detected",
            )
            # Most syncs successful (some may be pending/failed for valid reasons:
            # image-based agendas, future unpublished meetings, OnBase errors)
            synced_pct = stats["synced_ok"] / max(stats["count"], 1) * 100
            self.assertGreaterEqual(
                synced_pct, 50,
                f"{cfg['label']}: only {synced_pct:.0f}% synced "
                f"({stats['synced_ok']}/{stats['count']})",
            )
            # Has agenda items
            self.assertGreater(
                stats["items"], 0,
                f"{cfg['label']}: no agenda items synced",
            )
            # Meeting date is parseable
            try:
                date.fromisoformat(stats["first"])
                date.fromisoformat(stats["last"])
            except (ValueError, TypeError):
                self.fail(f"{cfg['label']}: unparseable meeting dates")

        test_name = f"test_{body_code.replace('-', '_')}_data_integrity"
        _test.__name__ = test_name
        _test.__qualname__ = f"TestSyncedMeetingsDataIntegrity.{test_name}"
        setattr(TestSyncedMeetingsDataIntegrity, test_name, _test)


_make_body_tests()


@integration_test
class TestAgendaItemIntegrity(unittest.TestCase):
    """Structural integrity of agenda items across all bodies."""

    def setUp(self):
        _use_real_db()
        self.s = get_session()

    def tearDown(self):
        self.s.close()


    def test_no_empty_titles(self):
        """No agenda item has a NULL or empty title."""
        count = self.s.execute(
            text("SELECT COUNT(*) FROM agenda_items WHERE agenda_item_title IS NULL OR agenda_item_title = ''")
        ).scalar()
        self.assertEqual(count, 0, f"{count} item(s) with empty title")

    def test_no_empty_body_codes(self):
        """No agenda item has a NULL or empty body code."""
        count = self.s.execute(
            text("SELECT COUNT(*) FROM agenda_items WHERE body IS NULL OR body = ''")
        ).scalar()
        self.assertEqual(count, 0)

    def test_no_orphan_items(self):
        """Every agenda item has a matching meeting."""
        orphan = self.s.execute(text("""
            SELECT COUNT(*) FROM agenda_items ai
            LEFT JOIN meetings m ON m.meeting_id = ai.meeting_id AND m.body = ai.body
            WHERE m.id IS NULL
        """)).scalar()
        self.assertEqual(orphan, 0, f"{orphan} orphan agenda item(s)")

    def test_no_orphan_supporting_docs(self):
        """Every supporting doc has a matching meeting."""
        orphan = self.s.execute(text("""
            SELECT COUNT(*) FROM supporting_documents sd
            LEFT JOIN meetings m ON m.meeting_id = sd.meeting_id AND m.body = sd.body
            WHERE m.id IS NULL
        """)).scalar()
        self.assertEqual(orphan, 0, f"{orphan} orphan supporting doc(s)")

    def test_item_counts_match_metadata(self):
        """Item count in agenda_items matches meeting.item_count_actual."""
        mismatches = self.s.execute(text("""
            SELECT m.body, m.meeting_id, m.item_count_actual,
                   (SELECT COUNT(*) FROM agenda_items ai
                    WHERE ai.meeting_id = m.meeting_id AND ai.body = m.body) as actual
            FROM meetings m
            WHERE m.sync_status = 'complete'
              AND m.item_count_actual IS NOT NULL
              AND m.item_count_actual != (
                    SELECT COUNT(*) FROM agenda_items ai
                    WHERE ai.meeting_id = m.meeting_id AND ai.body = m.body
                  )
        """)).fetchall()
        self.assertEqual(
            len(mismatches), 0,
            f"Item count mismatches: {[(r[0], r[1], r[2], r[3]) for r in mismatches]}",
        )


@integration_test
class TestVoteDataIntegrity(unittest.TestCase):
    """Vote data integrity for bodies that have vote records."""

    def setUp(self):
        _use_real_db()
        self.s = get_session()

    def tearDown(self):
        self.s.close()


    def test_bos_has_votes(self):
        """BOS meetings have vote records."""
        count = self.s.execute(
            text("SELECT COUNT(*) FROM agenda_item_votes WHERE body = 'bos'")
        ).scalar()
        self.assertGreater(count, 1000, f"BOS has only {count} vote records (expected > 1000)")

    def test_bos_votes_have_supervisor_names(self):
        """BOS votes reference actual Person records."""
        orphans = self.s.execute(text("""
            SELECT COUNT(*) FROM member_votes mv
            LEFT JOIN persons p ON p.id = mv.member_id
            WHERE p.id IS NULL
        """)).scalar()
        self.assertEqual(orphans, 0, f"{orphans} member_votes with no Person record")

    def test_pz_has_votes(self):
        """P&Z meetings have vote records (from PDF minutes)."""
        count = self.s.execute(
            text("SELECT COUNT(*) FROM agenda_item_votes WHERE body = 'pz'")
        ).scalar()
        self.assertGreater(count, 50, f"PZ has only {count} vote records (expected > 50)")

    def test_votes_have_motion_results(self):
        """Vote records have non-empty motion_result."""
        missing = self.s.execute(text("""
            SELECT COUNT(*) FROM agenda_item_votes
            WHERE body IN ('bos', 'pz')
              AND (motion_result IS NULL OR motion_result = '')
        """)).scalar()
        self.assertEqual(missing, 0, f"{missing} votes without motion_result")


@integration_test
class TestMembershipDataIntegrity(unittest.TestCase):
    """BodyMembership data integrity."""

    def setUp(self):
        _use_real_db()
        self.s = get_session()

    def tearDown(self):
        self.s.close()


    def test_bos_has_memberships(self):
        """BOS has at least 5 BodyMembership records."""
        count = self.s.execute(
            text("""
                SELECT COUNT(*) FROM body_memberships bm
                JOIN public_bodies pb ON pb.id = bm.public_body_id
                WHERE pb.body_code = 'bos'
            """)
        ).scalar()
        self.assertGreaterEqual(count, 5, f"BOS has only {count} memberships")

    def test_memberships_link_to_people(self):
        """Every BodyMembership has a valid Person."""
        orphans = self.s.execute(text("""
            SELECT COUNT(*) FROM body_memberships bm
            LEFT JOIN persons p ON p.id = bm.person_id
            WHERE p.id IS NULL
        """)).scalar()
        self.assertEqual(orphans, 0, f"{orphans} memberships with no Person record")

    def test_memberships_link_to_public_body(self):
        """Every BodyMembership has a valid PublicBody."""
        orphans = self.s.execute(text("""
            SELECT COUNT(*) FROM body_memberships bm
            LEFT JOIN public_bodies pb ON pb.id = bm.public_body_id
            WHERE pb.id IS NULL
        """)).scalar()
        self.assertEqual(orphans, 0, f"{orphans} memberships with no PublicBody")


@integration_test
class TestPermitDataIntegrity(unittest.TestCase):
    """Permit data integrity."""

    def setUp(self):
        _use_real_db()
        self.s = get_session()

    def tearDown(self):
        self.s.close()


    def test_permits_exist(self):
        """Permit records exist in the database."""
        count = self.s.execute(text("SELECT COUNT(*) FROM permits")).scalar()
        self.assertGreater(count, 100000, f"Only {count} permit records (expected > 100000)")

    def test_permits_have_numbers(self):
        """All permits have non-empty permit_number."""
        missing = self.s.execute(
            text("SELECT COUNT(*) FROM permits WHERE permit_number IS NULL OR permit_number = ''")
        ).scalar()
        self.assertEqual(missing, 0, f"{missing} permits without number")

    def test_permits_have_jurisdiction(self):
        """All permits have a jurisdiction set."""
        missing = self.s.execute(
            text("SELECT COUNT(*) FROM permits WHERE jurisdiction IS NULL OR jurisdiction = ''")
        ).scalar()
        self.assertEqual(missing, 0, f"{missing} permits without jurisdiction")


if __name__ == "__main__":
    unittest.main()
