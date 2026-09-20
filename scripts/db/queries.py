"""queries module."""

import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, select, text, or_, case, and_
from sqlalchemy.orm import Session

from db.models import (Person, Supervisor, Meeting, MeetingMember,
    AgendaItem, AgendaItemVote, MemberVote, MeetingAttendance,
    ExecutiveSessionParticipant, BodyMembership, PublicBody, Jurisdiction,
    BodySeat)
from db.core import get_session
from db.votes import _normalize_vote_value

def _resolve_jurisdiction_id(session: Session, body: str) -> Optional[int]:
    """Resolve a meeting's jurisdiction_id from its public body code.

    First tries an exact body_code match. If no match, falls back to
    matching by body code prefix (e.g. ``phoenix-cs`` → prefix ``phoenix``).
    This handles scrapers that create meetings with body codes that don't
    have a corresponding public_bodies row (e.g. Phoenix subcommittees,
    Gilbert task forces, surprise-legistar slugs).
    """
    from sqlalchemy import func as sa_func

    # Prefer the canonical row when duplicate body_codes exist (data bug
    # 2026-08-26: tucson/chandler had 2 rows per code; lowest id = canonical).
    pb = session.execute(
        select(PublicBody).where(PublicBody.body_code == body).order_by(PublicBody.id)
    ).scalars().first()
    if pb:
        return pb.jurisdiction_id

    # Fall back: extract prefix (e.g. "phoenix" from "phoenix-cs")
    # and find any public body with the same prefix
    prefix = body.split("-")[0] if "-" in body else body
    if prefix:
        pb = session.execute(
            select(PublicBody).where(
                PublicBody.body_code.like(prefix + "-%")
            ).limit(1)
        ).scalar_one_or_none()
        if pb:
            return pb.jurisdiction_id

    return None

def get_meetings_by_date_range(
    session: Session,
    body: str,
    start_date_iso: str,
    end_date_iso: str,
) -> list[Meeting]:
    """Get all meetings for a body with meeting_date in the given ISO date range (inclusive)."""
    q = (
        select(Meeting)
        .where(Meeting.body == body)
        .where(Meeting.meeting_date >= start_date_iso)
        .where(Meeting.meeting_date <= end_date_iso)
        .order_by(Meeting.meeting_date, Meeting.meeting_id)
    )
    return list(session.execute(q).scalars().all())

def get_meetings_by_status(
    session: Session,
    body: str,
    statuses: Optional[list[str]] = None,
    *,
    force: bool = False,
    meeting_ids: Optional[list[str]] = None,
) -> list[Meeting]:
    """Get meetings for a body filtered by sync_status and/or meeting_ids.

    If force is True, ignore status filter and return all matching meeting_ids.
    """
    q = select(Meeting).where(Meeting.body == body).order_by(Meeting.meeting_date, Meeting.meeting_id)
    if meeting_ids:
        q = q.where(Meeting.meeting_id.in_(meeting_ids))
    if not force and statuses:
        q = q.where(Meeting.sync_status.in_(statuses))
    return list(session.execute(q).scalars().all())

def get_sync_status_summary(session: Session) -> dict:
    """Get counts of meetings by sync_status."""
    rows = session.execute(
        select(
            Meeting.sync_status,
            func.count(Meeting.id).label("count"),
        )
        .group_by(Meeting.sync_status)
    ).all()

    summary = {
        "complete": 0,
        "partial": 0,
        "failed": 0,
        "pending": 0,
        "total": 0,
        "total_items": 0,
        "total_docs": 0,
    }
    for row in rows:
        status = row.sync_status or "pending"
        summary[status] = row.count
        summary["total"] += row.count

    # Aggregate item and doc counts
    agg = session.execute(
        select(
            func.coalesce(func.sum(Meeting.item_count_actual), 0),
            func.coalesce(func.sum(Meeting.supporting_doc_count), 0),
        )
    ).one()
    summary["total_items"] = agg[0]
    summary["total_docs"] = agg[1]

    return summary

def get_failed_meetings(session: Session, body: str = "bos") -> list[Meeting]:
    """Get meetings with failed or partial status (excludes manual_review)."""
    return get_meetings_by_status(session, body, ["failed", "partial"], force=False)

def get_meeting_attendance(
    session: Session,
    body: str,
    meeting_id: str,
) -> list["MeetingAttendance"]:
    """Get attendance records for a meeting."""
    rows = session.execute(
        select(MeetingAttendance).where(
            MeetingAttendance.body == body,
            MeetingAttendance.meeting_id == meeting_id,
        )
    ).scalars().all()
    return list(rows)

def get_executive_session_participants(
    session: Session,
    body: Optional[str] = None,
    meeting_id: Optional[str] = None,
    person_name: Optional[str] = None,
) -> list["ExecutiveSessionParticipant"]:
    """Get executive session participation records."""
    q = select(ExecutiveSessionParticipant)
    if body:
        q = q.where(ExecutiveSessionParticipant.body == body)
    if meeting_id:
        q = q.where(ExecutiveSessionParticipant.meeting_id == meeting_id)
    if person_name:
        q = q.where(ExecutiveSessionParticipant.normalized_name == person_name.lower())
    q = q.order_by(ExecutiveSessionParticipant.meeting_id)
    rows = session.execute(q).scalars().all()
    return list(rows)

def get_split_votes(
    session: Session,
    body: Optional[str] = None,
) -> list["AgendaItemVote"]:
    """Get all split votes, optionally filtered by body."""
    q = select(AgendaItemVote).where(AgendaItemVote.is_split_vote == True)
    if body and body.lower() != "all":
        q = q.where(AgendaItemVote.body == body)
    q = q.order_by(AgendaItemVote.meeting_id, AgendaItemVote.agenda_item_number)
    rows = session.execute(q).scalars().all()
    return list(rows)

def get_dissenting_votes(
    session: Session,
    member_name: Optional[str] = None,
) -> list[MemberVote]:
    """Get dissent votes, optionally filtered by member name."""
    from sqlalchemy import join as sa_join
    q = select(MemberVote).where(MemberVote.is_dissent == True)
    if member_name:
        norm = member_name.lower().strip()
        q = (
            select(MemberVote)
            .join(Person, MemberVote.member_id == Person.id)
            .where(
                MemberVote.is_dissent == True,
                Person.normalized_name.ilike(f"%{norm}%"),
            )
        )
    rows = session.execute(q).scalars().all()
    return list(rows)

def get_supervisor_by_slug_or_name(
    session: Session,
    slug_or_name: str,
) -> Optional[Supervisor]:
    """Look up a supervisor by URL slug or name.

    Slugs use the normalized_name field with hyphens instead of spaces.
    Falls back to partial name matching for flexibility.
    """
    from sqlalchemy import or_

    # Try slug: hyphen → space, match normalized_name exactly
    maybe_norm = slug_or_name.replace("-", " ").strip()
    sup = session.execute(
        select(Supervisor).where(Supervisor.normalized_name == maybe_norm)
    ).scalar_one_or_none()
    if sup:
        return sup

    # Try name match with noise filtering (shortest = most canonical)
    matches = session.execute(
        select(Supervisor)
        .where(
            or_(
                Supervisor.name.ilike(f"%{maybe_norm}%"),
                Supervisor.normalized_name.ilike(f"%{maybe_norm}%"),
            )
        )
        .order_by(func.length(Supervisor.name))
    ).scalars().all()
    for candidate in matches:
        if len(candidate.name) < 40 and not re.search(r"\d", candidate.name):
            return candidate

    return None

def get_bos_supervisors(session: Session) -> list[Supervisor]:
    """Get BOS supervisors with meaningful attendance (≥5 meeting appearances).

    Uses meeting_members (body='bos') to identify actual supervisors
    versus noise rows from vote-parsing artifacts.
    """
    rows = session.execute(
        select(Supervisor, func.count(MeetingMember.id).label("attendance"))
        .join(
            MeetingMember,
            MeetingMember.member_id == Supervisor.id,
        )
        .where(MeetingMember.body == "bos")
        .group_by(Supervisor.id)
        .having(func.count(MeetingMember.id) >= 5)
        .order_by(Supervisor.name)
    ).all()
    # Also filter out obvious noise (long names, digits)
    result = []
    for sup, _att in rows:
        if len(sup.name) < 40 and not re.search(r"\d", sup.name):
            result.append(sup)
    return result

def get_supervisor_vote_stats(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Get aggregated voting statistics for a supervisor.

    All counting pushed into SQL GROUP BY — Python only normalizes
    the 3-5 distinct vote values.
    """
    from collections import Counter

    # --- 1. Vote counts via SQL GROUP BY (was: load all rows in Python) ---
    q = select(MemberVote.vote, func.count(MemberVote.id).label("cnt"),
               MemberVote.agenda_item_vote_id)
    q = q.join(
        AgendaItemVote,
        AgendaItemVote.id == MemberVote.agenda_item_vote_id,
    ).where(
        MemberVote.member_id == sup_id,
        AgendaItemVote.body == body,
    ).group_by(MemberVote.vote, MemberVote.agenda_item_vote_id)
    if start_date or end_date:
        q = q.join(Meeting, and_(Meeting.id == AgendaItemVote.meeting_db_id))
        if start_date:
            q = q.where(Meeting.meeting_date >= start_date)
        if end_date:
            q = q.where(Meeting.meeting_date <= end_date)
    rows = session.execute(q).all()

    # Aggregate in Python: group by normalized vote value
    total_votes = 0
    norm_counts: Counter = Counter()
    aiv_ids: set[int] = set()
    for r in rows:
        total_votes += r.cnt
        norm_counts[_normalize_vote_value(r.vote)] += r.cnt
        aiv_ids.add(r.agenda_item_vote_id)

    yes_count = norm_counts.get("yes", 0)
    no_count = norm_counts.get("no", 0)
    abstain_count = norm_counts.get("abstain", 0)

    # --- 2. Split / majority analysis via SQL aggregation ---
    aiv_id_list = list(aiv_ids)
    split_count = 0
    with_maj = 0
    against_maj = 0

    if aiv_id_list:
        # Get per-AIV vote tallies in ONE query (was: load all rows)
        # Normalize vote values: "aye" → "yes", "nay" → "no"
        _yes_cond = or_(
            MemberVote.vote == "yes",
            MemberVote.vote.ilike("aye"),
        )
        _no_cond = or_(
            MemberVote.vote == "no",
            MemberVote.vote.ilike("nay"),
        )
        tallies = session.execute(
            select(
                MemberVote.agenda_item_vote_id,
                func.sum(case((_yes_cond, 1), else_=0)).label("yes_cnt"),
                func.sum(case((_no_cond, 1), else_=0)).label("no_cnt"),
            )
            .where(MemberVote.agenda_item_vote_id.in_(aiv_id_list))
            .group_by(MemberVote.agenda_item_vote_id)
        ).all()

        # Get this supervisor's votes for each AIV
        sup_votes = {
            r.agenda_item_vote_id: _normalize_vote_value(r.vote)
            for r in session.execute(
                select(MemberVote.agenda_item_vote_id, MemberVote.vote)
                .where(
                    MemberVote.agenda_item_vote_id.in_(aiv_id_list),
                    MemberVote.member_id == sup_id,
                )
            ).all()
        }

        for t in tallies:
            yes_sup = t.yes_cnt or 0
            no_sup = t.no_cnt or 0
            is_split = yes_sup > 0 and no_sup > 0
            if not is_split:
                continue
            split_count += 1
            if yes_sup > no_sup:
                majority = "yes"
            elif no_sup > yes_sup:
                majority = "no"
            else:
                continue  # tie
            sup_nv = sup_votes.get(t.agenda_item_vote_id)
            if sup_nv is None:
                continue
            if sup_nv == majority:
                with_maj += 1
            elif sup_nv in ("yes", "no"):
                against_maj += 1

    # --- 3. Attendance (already SQL, unchanged) ---
    present = session.execute(
        select(func.count())
        .select_from(MeetingMember)
        .where(
            MeetingMember.member_id == sup_id,
            MeetingMember.body == body,
            MeetingMember.present == True,
        )
    ).scalar() or 0

    absent = session.execute(
        select(func.count())
        .select_from(MeetingMember)
        .where(
            MeetingMember.member_id == sup_id,
            MeetingMember.body == body,
            MeetingMember.present == False,
        )
    ).scalar() or 0

    total_meetings = present + absent
    attendance_rate = round(present / total_meetings, 4) if total_meetings > 0 else None

    return {
        "total_votes": total_votes,
        "yes": yes_count,
        "no": no_count,
        "abstain": abstain_count,
        "absences": absent,
        "attendance_present": present,
        "attendance_absent": absent,
        "attendance_rate": attendance_rate,
        "split_votes_attended": split_count,
        "with_majority": with_maj,
        "against_majority": against_maj,
        "dissent_rate": round(against_maj / split_count, 4) if split_count > 0 else None,
    }

def get_supervisor_split_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Get all split votes involving this supervisor.

    Returns list of dicts with:
        meeting_id, meeting_date, meeting_type, agenda_item_number,
        agenda_item_title, c_number, supervisor_vote,
        motion_result, majority_position, with_or_against_majority

    Args:
        start_date: ISO date string (YYYY-MM-DD) to filter from (inclusive).
        end_date: ISO date string (YYYY-MM-DD) to filter to (inclusive).
    """
    # 1. Get all AIV IDs this supervisor voted on (body-scoped, date-filtered)
    q = (
        select(MemberVote.agenda_item_vote_id, MemberVote.vote)
        .join(
            AgendaItemVote,
            AgendaItemVote.id == MemberVote.agenda_item_vote_id,
        )
        .join(
            Meeting,
            and_(Meeting.id == AgendaItemVote.meeting_db_id),
        )
        .where(
            MemberVote.member_id == sup_id,
            AgendaItemVote.body == body,
        )
    )
    if start_date:
        q = q.where(Meeting.meeting_date >= start_date)
    if end_date:
        q = q.where(Meeting.meeting_date <= end_date)
    my_aiv_rows = session.execute(q).all()

    if not my_aiv_rows:
        return []

    sup_vote_map = {av.agenda_item_vote_id: _normalize_vote_value(av.vote) for av in my_aiv_rows}
    aiv_ids = list(sup_vote_map.keys())

    # 2. Get all votes on these AIVs (all supervisors)
    all_votes = session.execute(
        select(
            MemberVote.agenda_item_vote_id,
            MemberVote.vote,
        )
        .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    # Group by aiv
    aiv_group: dict[int, list[str]] = {}
    for r in all_votes:
        aiv_group.setdefault(r.agenda_item_vote_id, []).append(
            _normalize_vote_value(r.vote)
        )

    # Identify split AIVs: both yes and no votes present
    split_aiv_ids: set[int] = set()
    aiv_majority: dict[int, Optional[str]] = {}
    for aiv_id, votes in aiv_group.items():
        vote_set = set(votes)
        if "yes" in vote_set and "no" in vote_set:
            split_aiv_ids.add(aiv_id)
            yes_cnt = sum(1 for v in votes if v == "yes")
            no_cnt = sum(1 for v in votes if v == "no")
            if yes_cnt > no_cnt:
                aiv_majority[aiv_id] = "yes"
            elif no_cnt > yes_cnt:
                aiv_majority[aiv_id] = "no"
            else:
                aiv_majority[aiv_id] = "tie"

    if not split_aiv_ids:
        return []

    # 3. Fetch AIV details + meeting + agenda item info for split AIVs (raw SQL to avoid ORM ambiguity)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in split_aiv_ids)
    rows = session.execute(
        sa_text(f"""
            SELECT
                aiv.meeting_id,
                aiv.agenda_item_number,
                aiv.c_number,
                aiv.motion_result,
                ai.agenda_item_title,
                m.meeting_date,
                m.meeting_type
            FROM agenda_item_votes aiv
            LEFT JOIN agenda_items ai
                ON ai.meeting_db_id = aiv.meeting_db_id
                AND ai.body = aiv.body
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in rows:
        aiv_key = None
        # Need to find the AIV id — re-query to get id from meeting_id+item_number
        aiv = session.execute(
            select(AgendaItemVote.id)
            .where(
                AgendaItemVote.body == body,
                AgendaItemVote.meeting_id == r.meeting_id,
                AgendaItemVote.agenda_item_number == r.agenda_item_number,
            )
        ).scalar_one_or_none()
        if aiv is None:
            continue
        aiv_id = aiv
        sup_nv = sup_vote_map.get(aiv_id, "unknown")
        maj = aiv_majority.get(aiv_id)
        with_maj = None
        if maj and sup_nv in ("yes", "no") and maj not in ("tie", "unknown"):
            with_maj = "with_majority" if sup_nv == maj else "against_majority"

        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "majority_position": maj,
            "with_or_against_majority": with_maj,
        })

    return results

def get_supervisor_dissents(
    session: Session,
    sup_id: int,
    body: str = "bos",
) -> list[dict]:
    """Get split votes where this supervisor voted against the majority.

    Returns the same dict format as get_supervisor_split_votes, but only
    items where with_or_against_majority == 'against_majority'.
    """
    all_split = get_supervisor_split_votes(session, sup_id, body)
    return [s for s in all_split if s.get("with_or_against_majority") == "against_majority"]

def get_supervisor_abstentions(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Get votes where this supervisor abstained.

    Args:
        start_date: ISO date string (YYYY-MM-DD) to filter from (inclusive).
        end_date: ISO date string (YYYY-MM-DD) to filter to (inclusive).
    """
    from sqlalchemy import text as sa_text

    # Use raw SQL to avoid ORM join-ambiguity issues with the 4-table chain
    where_clauses = [
        "mv.member_id = :sup_id",
        "aiv.body = :body",
        "mv.vote IN ('abstain', 'abstained')",
    ]
    params: dict = {"sup_id": sup_id, "body": body}
    if start_date:
        where_clauses.append("m.meeting_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        where_clauses.append("m.meeting_date <= :end_date")
        params["end_date"] = end_date

    where_sql = " AND ".join(where_clauses)
    sql = sa_text(f"""
        SELECT
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            m.meeting_date,
            m.meeting_type
        FROM member_votes mv
        JOIN agenda_item_votes aiv ON aiv.id = mv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_db_id = aiv.meeting_db_id
            AND ai.body = aiv.body
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
        WHERE {where_sql}
        ORDER BY m.meeting_date, aiv.agenda_item_number
    """)
    rows = session.execute(sql, params).all()

    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "motion_result": r.motion_result or "",
        }
        for r in rows
    ]

def get_supervisor_absences(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Get meetings where this supervisor was marked absent.

    Args:
        start_date: ISO date string (YYYY-MM-DD) to filter from (inclusive).
        end_date: ISO date string (YYYY-MM-DD) to filter to (inclusive).

    Returns list of dicts with meeting_id, meeting_date, meeting_type, title.
    """
    conditions = [
        MeetingMember.member_id == sup_id,
        MeetingMember.present == False,
        Meeting.body == body,
    ]
    if start_date:
        conditions.append(Meeting.meeting_date >= start_date)
    if end_date:
        conditions.append(Meeting.meeting_date <= end_date)

    rows = session.execute(
        select(
            Meeting.meeting_id,
            Meeting.meeting_date,
            Meeting.meeting_type,
            Meeting.display_name,
            Meeting.meeting_title,
        )
        .join(
            MeetingMember,
            (MeetingMember.meeting_id == Meeting.meeting_id)
            & (MeetingMember.body == Meeting.body),
        )
        .where(*conditions)
        .order_by(Meeting.meeting_date)
    ).all()

    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "title": r.display_name or r.meeting_title or r.meeting_id,
        }
        for r in rows
    ]

def get_supervisor_full_voting_record(
    session: Session,
    sup_id: int,
    body: str = "bos",
    limit: Optional[int] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Get voting record for a supervisor.

    Args:
        limit: Max rows to return.  The member_detail page passes
               limit=25 instead of loading thousands of rows.
    """
    from sqlalchemy import text as sa_text

    limit_clause = f" LIMIT {int(limit)}" if limit else ""

    where_clauses = ["mv.member_id = :sup_id", "aiv.body = :body"]
    params: dict = {"sup_id": sup_id, "body": body}
    if start_date:
        where_clauses.append("m.meeting_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        where_clauses.append("m.meeting_date <= :end_date")
        params["end_date"] = end_date

    where_sql = " AND ".join(where_clauses)

    sql = sa_text(f"""
        SELECT
            mv.vote,
            mv.raw_vote_text,
            mv.agenda_item_vote_id,
            aiv.meeting_id,
            aiv.agenda_item_number,
            aiv.c_number,
            aiv.motion_result,
            ai.agenda_item_title,
            m.meeting_date,
            m.meeting_type
        FROM member_votes mv
        JOIN agenda_item_votes aiv ON aiv.id = mv.agenda_item_vote_id
        LEFT JOIN agenda_items ai
            ON ai.meeting_db_id = aiv.meeting_db_id
            AND ai.body = aiv.body
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
        WHERE {where_sql}
        ORDER BY m.meeting_date DESC, aiv.agenda_item_number{limit_clause}
    """)
    sup_votes = session.execute(sql, params).all()

    if not sup_votes:
        return []

    # Gather unique AIV IDs for split/majority detection
    aiv_ids = list({r.agenda_item_vote_id for r in sup_votes})

    # Get per-AIV tallies via SQL aggregation (was: load all rows into Python)
    tallies = session.execute(
        select(
            MemberVote.agenda_item_vote_id,
            func.sum(case((MemberVote.vote == "yes", 1), else_=0)).label("yes_cnt"),
            func.sum(case((MemberVote.vote == "no", 1), else_=0)).label("no_cnt"),
        )
        .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
        .group_by(MemberVote.agenda_item_vote_id)
    ).all()

    is_split: dict[int, bool] = {}
    majority: dict[int, Optional[str]] = {}
    for t in tallies:
        yes_cnt = t.yes_cnt or 0
        no_cnt = t.no_cnt or 0
        is_split[t.agenda_item_vote_id] = yes_cnt > 0 and no_cnt > 0
        if yes_cnt > 0 and no_cnt > 0:
            if yes_cnt > no_cnt:
                majority[t.agenda_item_vote_id] = "yes"
            elif no_cnt > yes_cnt:
                majority[t.agenda_item_vote_id] = "no"
            else:
                majority[t.agenda_item_vote_id] = "tie"
        else:
            majority[t.agenda_item_vote_id] = None

    results = []
    for r in sup_votes:
        aiv_id = r.agenda_item_vote_id
        sup_nv = _normalize_vote_value(r.vote)
        maj = majority.get(aiv_id)
        split_flag = is_split.get(aiv_id, False)
        with_maj = None
        if maj and split_flag and sup_nv in ("yes", "no") and maj not in ("tie", "unknown"):
            with_maj = "with_majority" if sup_nv == maj else "against_majority"

        is_inferred = bool(r.raw_vote_text and r.raw_vote_text.startswith("inferred"))
        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "vote": sup_nv,
            "is_inferred": is_inferred,
            "motion_result": r.motion_result or "",
            "is_split_vote": split_flag,
            "majority_position": maj,
            "with_or_against_majority": with_maj,
        })

    return results

def get_supervisor_slug(sup: Supervisor) -> str:
    """Get URL slug for a supervisor."""
    return sup.normalized_name.replace(" ", "-")

def get_supervisor_majority_alignment_stats(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> dict:
    """Get detailed majority alignment stats for a supervisor.

    Extends get_supervisor_vote_stats with additional analytics fields.
    Returns breakdown of unanimous vs split-vote behavior.
    """
    from collections import Counter

    # Get raw vote data
    q = select(MemberVote.vote, AgendaItemVote.id.label("aiv_id"))
    q = q.join(
        AgendaItemVote,
        AgendaItemVote.id == MemberVote.agenda_item_vote_id,
    ).where(
        MemberVote.member_id == sup_id,
        AgendaItemVote.body == body,
    )
    if start_date or end_date:
        q = q.join(Meeting, and_(Meeting.id == AgendaItemVote.meeting_db_id))
        if start_date:
            q = q.where(Meeting.meeting_date >= start_date)
        if end_date:
            q = q.where(Meeting.meeting_date <= end_date)
    rows = session.execute(q).all()

    total_votes = len(rows)
    if total_votes == 0:
        return {
            "total_votes": 0, "unanimous_votes": 0,
            "split_votes_attended": 0, "with_majority": 0,
            "against_majority": 0, "abstain_on_split": 0,
            "majority_alignment_rate": None, "dissent_rate": None,
        }

    aiv_ids = {r.aiv_id for r in rows}
    aiv_id_list = list(aiv_ids)

    # Get all votes on all relevant AIVs (for split/majority detection)
    all_v = session.execute(
        select(
            MemberVote.agenda_item_vote_id,
            MemberVote.member_id,
            MemberVote.vote,
        )
        .where(MemberVote.agenda_item_vote_id.in_(aiv_id_list))
    ).all()

    # Build per-AIV data
    aiv_data: dict[int, dict] = {}
    for av in all_v:
        aiv_data.setdefault(av.agenda_item_vote_id, {
            "votes": [],  # all normalized
            "sup_votes": {},  # sup_id -> norm_vote
        })
        nv = _normalize_vote_value(av.vote)
        aiv_data[av.agenda_item_vote_id]["votes"].append(nv)
        aiv_data[av.agenda_item_vote_id]["sup_votes"][av.member_id] = nv

    # Build sup vote map: aiv_id -> norm_vote for this specific supervisor
    sup_own_votes = {}
    for r in rows:
        sup_own_votes[r.aiv_id] = _normalize_vote_value(r.vote)

    unanimous = 0
    split_attended = 0
    with_maj = 0
    against_maj = 0
    abstain_on_split = 0

    for aiv_id in aiv_ids:
        dd = aiv_data.get(aiv_id, {})
        vlist = dd.get("votes", [])
        yes_cnt = sum(1 for v in vlist if v == "yes")
        no_cnt = sum(1 for v in vlist if v == "no")
        is_split = yes_cnt > 0 and no_cnt > 0

        if not is_split:
            unanimous += 1
            continue

        split_attended += 1
        sup_nv = sup_own_votes.get(aiv_id, "unknown")

        if sup_nv == "abstain":
            abstain_on_split += 1
            continue
        if sup_nv not in ("yes", "no"):
            continue

        if yes_cnt > no_cnt:
            maj = "yes"
        elif no_cnt > yes_cnt:
            maj = "no"
        else:
            maj = "tie"

        if maj == "tie":
            continue
        if sup_nv == maj:
            with_maj += 1
        else:
            against_maj += 1

    majority_alignment_rate = (
        round(with_maj / split_attended, 4) if split_attended > 0 else None
    )
    dissent_rate = (
        round(against_maj / split_attended, 4) if split_attended > 0 else None
    )

    return {
        "total_votes": total_votes,
        "unanimous_votes": unanimous,
        "split_votes_attended": split_attended,
        "with_majority": with_maj,
        "against_majority": against_maj,
        "abstain_on_split": abstain_on_split,
        "majority_alignment_rate": majority_alignment_rate,
        "dissent_rate": dissent_rate,
    }

def get_supervisor_voting_alignment(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Compare voting patterns between this supervisor and all others.

    For each other BOS supervisor, compute:
    - total comparable votes (both voted yes or no)
    - same votes
    - different votes
    - overall alignment percentage
    - split-vote alignment percentage

    Excludes abstentions and absences from pairwise alignment.
    """
    # Get all BOS supervisor IDs
    sup_ids = [
        r[0]
        for r in session.execute(
            select(Supervisor.id)
            .join(
                MeetingMember,
                MeetingMember.member_id == Supervisor.id,
            )
            .where(
                MeetingMember.body == body,
            )
            .group_by(Supervisor.id)
            .having(func.count(MeetingMember.id) >= 5)
            .order_by(Supervisor.name)
        ).all()
    ]

    if sup_id not in sup_ids:
        return []

    other_ids = [sid for sid in sup_ids if sid != sup_id]
    if not other_ids:
        return []

    # Get all votes for this supervisor (body-scoped, date-filtered)
    sup_aiv_votes: dict[int, str] = {}
    q = (
        select(MemberVote.agenda_item_vote_id, MemberVote.vote)
        .join(
            AgendaItemVote,
            AgendaItemVote.id == MemberVote.agenda_item_vote_id,
        )
        .where(
            MemberVote.member_id == sup_id,
            AgendaItemVote.body == body,
        )
    )
    if start_date or end_date:
        q = q.join(Meeting, and_(Meeting.id == AgendaItemVote.meeting_db_id))
        if start_date:
            q = q.where(Meeting.meeting_date >= start_date)
        if end_date:
            q = q.where(Meeting.meeting_date <= end_date)
    for r in session.execute(q).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            sup_aiv_votes[r.agenda_item_vote_id] = nv

    if not sup_aiv_votes:
        return []

    aiv_ids = list(sup_aiv_votes.keys())

    # Get all other supervisors' votes on the same AIVs
    # other_votes[other_sup_id][aiv_id] = norm_vote
    other_votes: dict[int, dict[int, str]] = {oid: {} for oid in other_ids}
    for r in session.execute(
        select(
            MemberVote.member_id,
            MemberVote.agenda_item_vote_id,
            MemberVote.vote,
        )
        .where(
            MemberVote.member_id.in_(other_ids),
            MemberVote.agenda_item_vote_id.in_(aiv_ids),
        )
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            other_votes[r.member_id][r.agenda_item_vote_id] = nv

    # Also determine which AIVs are split votes
    # For each AIV, collect all normalized votes to detect split
    all_votes_on_aiv: dict[int, list[str]] = {}
    for r in session.execute(
        select(
            MemberVote.agenda_item_vote_id,
            MemberVote.vote,
        )
        .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
    ).all():
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            all_votes_on_aiv.setdefault(r.agenda_item_vote_id, []).append(nv)

    split_aiv_ids: set[int] = set()
    for aiv_id, vl in all_votes_on_aiv.items():
        if "yes" in vl and "no" in vl:
            split_aiv_ids.add(aiv_id)

    # Get other supervisor names
    sup_names: dict[int, str] = {
        r.id: r.name
        for r in session.execute(select(Supervisor)).scalars().all()
    }

    results = []
    for oid in other_ids:
        other_name = sup_names.get(oid, f"Supervisor #{oid}")
        other_o = get_supervisor_by_slug_or_name(session, other_name)
        slug = get_supervisor_slug(other_o) if other_o else ""

        # Compare votes
        comparable_aivs = set(sup_aiv_votes.keys()) & set(other_votes[oid].keys())
        if not comparable_aivs:
            continue

        same = 0
        diff = 0
        split_same = 0
        split_diff = 0

        for aiv_id in comparable_aivs:
            sv = sup_aiv_votes[aiv_id]
            ov = other_votes[oid][aiv_id]
            if sv == ov:
                same += 1
                if aiv_id in split_aiv_ids:
                    split_same += 1
            else:
                diff += 1
                if aiv_id in split_aiv_ids:
                    split_diff += 1

        total = same + diff
        split_total = split_same + split_diff
        overall_pct = round(same / total * 100, 1) if total else None
        split_pct = round(split_same / split_total * 100, 1) if split_total else None

        results.append({
            "other_supervisor_id": oid,
            "other_name": other_name,
            "slug": slug,
            "total_comparable_votes": total,
            "same_votes": same,
            "different_votes": diff,
            "overall_alignment_pct": overall_pct,
            "split_vote_comparable": split_total,
            "split_vote_same": split_same,
            "split_vote_alignment_pct": split_pct,
        })

    return results

def get_supervisor_swing_votes(
    session: Session,
    sup_id: int,
    body: str = "bos",
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
) -> list[dict]:
    """Identify swing votes for a BOS supervisor.

    Swing vote = split vote where motion passed/failed by one vote
    and this supervisor voted with the prevailing side.
    For BOS (5 members), one-vote margin = 3-2 or 2-3.

    Returns list of dicts with meeting/agenda/vote detail.
    """
    # Get this supervisor's votes on split AIVs (date-filtered)
    q = (
        select(
            MemberVote.vote,
            MemberVote.agenda_item_vote_id,
        )
        .join(
            AgendaItemVote,
            AgendaItemVote.id == MemberVote.agenda_item_vote_id,
        )
        .where(
            MemberVote.member_id == sup_id,
            AgendaItemVote.body == body,
        )
    )
    if start_date or end_date:
        q = q.join(Meeting, and_(Meeting.id == AgendaItemVote.meeting_db_id))
        if start_date:
            q = q.where(Meeting.meeting_date >= start_date)
        if end_date:
            q = q.where(Meeting.meeting_date <= end_date)
    sup_votes = session.execute(q).all()

    if not sup_votes:
        return []

    aiv_ids = [r.agenda_item_vote_id for r in sup_votes]

    # Get all votes on these AIVs
    all_v = session.execute(
        select(
            MemberVote.agenda_item_vote_id,
            MemberVote.member_id,
            MemberVote.vote,
        )
        .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    # Build tally per AIV and find this sup's vote
    from collections import Counter
    aiv_tallies: dict[int, Counter] = {}
    sup_aiv_nv: dict[int, str] = {}
    for r in all_v:
        aiv_tallies.setdefault(r.agenda_item_vote_id, Counter())
        nv = _normalize_vote_value(r.vote)
        if nv in ("yes", "no"):
            aiv_tallies[r.agenda_item_vote_id][nv] += 1
        if r.member_id == sup_id:
            sup_aiv_nv[r.agenda_item_vote_id] = nv

    # Identify swing AIVs: split vote, margin=1, sup with prevailing side
    swing_aiv_ids: set[int] = set()
    for aiv_id, tally in aiv_tallies.items():
        yes = tally.get("yes", 0)
        no = tally.get("no", 0)
        is_split = yes > 0 and no > 0
        if not is_split:
            continue
        margin = abs(yes - no)
        if margin != 1:
            continue
        # Now check: was this supervisor's vote with the prevailing side?
        sup_nv = sup_aiv_nv.get(aiv_id)
        if sup_nv is None:
            continue
        if sup_nv not in ("yes", "no"):
            continue
        prev_side = "yes" if yes > no else "no"
        if sup_nv == prev_side:
            swing_aiv_ids.add(aiv_id)

    if not swing_aiv_ids:
        return []

    # Fetch meeting/agenda details
    ids_list = list(swing_aiv_ids)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in ids_list)
    rows = session.execute(
        sa_text(f"""
            SELECT
                aiv.id AS aiv_id,
                aiv.meeting_id,
                aiv.agenda_item_number,
                aiv.c_number,
                aiv.motion_result,
                ai.agenda_item_title,
                m.meeting_date,
                m.meeting_type
            FROM agenda_item_votes aiv
            LEFT JOIN agenda_items ai
                ON ai.meeting_db_id = aiv.meeting_db_id
                AND ai.body = aiv.body
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in rows:
        tally = aiv_tallies.get(r.aiv_id, Counter())
        sup_nv = sup_aiv_nv.get(r.aiv_id, "unknown")
        prev_side = "yes" if tally.get("yes", 0) > tally.get("no", 0) else "no"
        results.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": sup_nv,
            "motion_result": r.motion_result or "",
            "vote_tally": f"{tally.get('yes',0)}-{tally.get('no',0)}",
            "prevailing_side": prev_side,
        })

    return results

def get_public_bodies_by_jurisdiction(slug: str) -> list[PublicBody]:
    """Get all public bodies for a jurisdiction slug."""
    session = get_session()
    try:
        jurisdiction = session.execute(
            select(Jurisdiction).where(Jurisdiction.slug == slug)
        ).scalar_one_or_none()
        if not jurisdiction:
            return []
        bodies = session.execute(
            select(PublicBody).where(PublicBody.jurisdiction_id == jurisdiction.id).order_by(PublicBody.name)
        ).scalars().all()
        return list(bodies)
    finally:
        session.close()

def _enhance_member_for_template(person: Person, public_body_id: int) -> Person:
    """Add backward-compatible attributes to a Person for template rendering.

    Sets active_from, active_to, title, district_or_seat, body from the
    person's most recent BodyMembership so existing templates still work
    without those database columns.

    Uses setattr on the object's __dict__ since the columns no longer
    exist on the Person model.
    """
    session = get_session()
    try:
        membership = session.execute(
            select(BodyMembership)
            .where(BodyMembership.person_id == person.id)
            .where(BodyMembership.public_body_id == public_body_id)
            .order_by(BodyMembership.term_start.desc())
            .limit(1)
        ).scalar_one_or_none()
        if membership:
            person.__dict__["active_from"] = membership.term_start
            person.__dict__["active_to"] = membership.term_end
            person.__dict__["title"] = membership.role
            # Look up body_code from public_body_id
            pb = session.execute(
                select(PublicBody).where(PublicBody.id == public_body_id)
            ).scalar_one_or_none()
            if pb:
                person.__dict__["body"] = pb.body_code or ""
            # Look up district_or_seat from BodySeat if set
            if membership.body_seat_id:
                seat = session.execute(
                    select(BodySeat).where(BodySeat.id == membership.body_seat_id)
                ).scalar_one_or_none()
                if seat:
                    person.__dict__["district_or_seat"] = seat.seat_name
    finally:
        session.close()
    return person

def get_body_members(body_code: str, page: int = 1, per_page: int = 10) -> dict:
    """Get paginated members of a public body by body_code.

    Returns currently active members (no term_end or term_end in the future)
    ordered by most recent term start date.
    """
    session = get_session()
    try:
        today = date.today()
        offset = (page - 1) * per_page

        pb = session.execute(
            select(PublicBody).where(PublicBody.body_code == body_code)
        ).scalar_one_or_none()
        if not pb:
            return [], 0

        # Get active member ids for counting
        active_subq = (
            select(BodyMembership.person_id)
            .where(BodyMembership.public_body_id == pb.id)
            .where(BodyMembership.term_start <= today)
            .where(
                (BodyMembership.term_end.is_(None))
                | (BodyMembership.term_end >= today)
            )
        ).subquery()

        total = session.execute(
            select(func.count(active_subq.c.person_id.distinct()))
        ).scalar() or 0

        members = session.execute(
            select(Person)
            .join(BodyMembership, BodyMembership.person_id == Person.id)
            .where(BodyMembership.public_body_id == pb.id)
            .where(BodyMembership.term_start <= today)
            .where(
                (BodyMembership.term_end.is_(None))
                | (BodyMembership.term_end >= today)
            )
            .order_by(BodyMembership.term_start.desc().nullslast(), Person.name)
            .offset(offset).limit(per_page)
        ).scalars().all()
        return list(members), total
    finally:
        session.close()


def get_ingest_failures(
    session: Session,
    *,
    since: Optional[datetime] = None,
    category: Optional[str] = None,
    limit: int = 100,
) -> list[dict]:
    """Get recent import-side failures from _ingest_failures.

    Args:
        since: Only failures after this timestamp.
        category: Filter by error_category (TRANSIENT, CODE, DATA, UNKNOWN).
        limit: Max records to return.

    Returns list of dicts with keys:
        id, error_category, source, body, meeting_id, meeting_date, error, created_at
    """
    from db.models import IngestFailure
    q = select(IngestFailure).order_by(IngestFailure.created_at.desc())
    if since:
        q = q.where(IngestFailure.created_at >= since)
    if category:
        q = q.where(IngestFailure.error_category == category)
    q = q.limit(limit)
    rows = session.execute(q).scalars().all()
    return [
        {
            "id": r.id,
            "error_category": r.error_category,
            "source": r.source,
            "body": r.body,
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "error": r.error,
            "created_at": r.created_at,
        }
        for r in rows
    ]


def get_sync_report(
    session: Session,
    *,
    since: Optional[datetime] = None,
    body: Optional[str] = None,
) -> dict:
    """Get a structured summary of recent scrape failures.

    Queries both the meetings table (for scrape failures with DB records)
    and the _ingest_failures table (for import-side failures without DB records),
    groups them by error category, and returns a human-readable summary.

    When Pete asks "how did the scrape go?", this is the function to call.

    Args:
        since: Only consider failures after this time.  Defaults to 24 hours ago.
        body: If set, only failures for this body code.

    Returns dict with:
        summary: Brief text summary like "3 meetings failed (2 transient, 1 code bug)"
        by_category: List of {category, count, items} with details
        total_failures: Total failure count
        since: The cutoff time used
    """
    if since is None:
        since = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    # 1. Scrape failures from meetings table
    from db.models import IngestFailure as _IF
    meeting_q = select(Meeting).where(
        Meeting.sync_status.in_(["failed", "partial"]),
        Meeting.last_attempted_at >= since,
    )
    if body:
        meeting_q = meeting_q.where(Meeting.body == body)
    meeting_q = meeting_q.order_by(Meeting.last_attempted_at.desc())
    failed_meetings = list(session.execute(meeting_q).scalars().all())

    # 2. Ingest failures (import-side, no meeting record)
    ingest_q = select(_IF).where(_IF.created_at >= since)
    if body:
        ingest_q = ingest_q.where(_IF.body == body)
    ingest_q = ingest_q.order_by(_IF.created_at.desc())
    ingest_failures = list(session.execute(ingest_q).scalars().all())

    # 3. Categorize
    from collections import defaultdict

    by_category: dict[str, list[dict]] = defaultdict(list)

    for m in failed_meetings:
        err = m.last_error or ""
        cat = "UNKNOWN"
        for prefix in ("[TRANSIENT]", "[CODE]", "[DATA]", "[UNKNOWN]"):
            if err.startswith(prefix):
                cat = prefix.strip("[]")
                break
        by_category[cat].append({
            "type": "scrape",
            "body": m.body,
            "meeting_id": m.meeting_id,
            "meeting_date": m.meeting_date,
            "sync_status": m.sync_status,
            "retry_count": m.retry_count,
            "error": err,
            "last_attempted_at": m.last_attempted_at,
        })

    for f in ingest_failures:
        by_category[f.error_category].append({
            "type": "ingest",
            "source": f.source,
            "body": f.body,
            "meeting_id": f.meeting_id,
            "meeting_date": f.meeting_date,
            "error": f.error,
            "created_at": f.created_at,
        })

    # 4. Build summary
    total = len(failed_meetings) + len(ingest_failures)
    category_list = []
    for cat_name in ("TRANSIENT", "CODE", "DATA", "UNKNOWN"):
        items = by_category.get(cat_name, [])
        if items:
            category_list.append({
                "category": cat_name,
                "count": len(items),
                "items": items,
            })

    summary_lines = []
    if total == 0:
        summary_lines.append(f"No failures since {since.isoformat()}")
    else:
        counts_str = ", ".join(
            f"{c['count']} {c['category'].lower()}" for c in category_list
        )
        summary_lines.append(f"{total} failure(s) since {since.isoformat()}: {counts_str}")

    return {
        "summary": " | ".join(summary_lines),
        "by_category": category_list,
        "total_failures": total,
        "since": since,
    }


if __name__ == "__main__":
    """Quick diagnostic: query and print sync report from command line."""
    import sys
    from db.core import get_session
    session = get_session()
    try:
        from datetime import timedelta
        hours = int(sys.argv[1]) if len(sys.argv) > 1 else 24
        since = datetime.now(timezone.utc) - timedelta(hours=hours)
        report = get_sync_report(session, since=since)
        print(f"\n{'='*60}")
        print(f"SYNC REPORT — last {hours} hours")
        print(f"{'='*60}")
        print(report["summary"])
        for cat in report["by_category"]:
            print(f"\n  [{cat['category']}] {cat['count']} item(s):")
            for item in cat["items"][:20]:
                if item["type"] == "scrape":
                    err_short = (item["error"] or "")[:80]
                    print(f"    {item['body']} #{item['meeting_id']} ({item['meeting_date']}): {err_short}")
                else:
                    err_short = (item["error"] or "")[:80]
                    print(f"    [import] {item['source']}: {item['meeting_id']} ({item['meeting_date']}): {err_short}")
            if len(cat["items"]) > 20:
                print(f"    ... and {len(cat['items']) - 20} more")
        print()
    finally:
        session.close()

