"""Members routes blueprint."""

import logging
from functools import wraps

from flask import Blueprint, render_template, request, jsonify, redirect
from sqlalchemy import select, func, case, text as sa_text, and_

from db import (
    Person,
    get_bos_supervisors, get_supervisor_by_slug_or_name,
    get_supervisor_vote_stats, get_supervisor_split_votes,
    get_supervisor_abstentions,
    get_supervisor_absences, get_supervisor_full_voting_record,
    get_supervisor_slug,
    get_supervisor_voting_alignment, get_supervisor_swing_votes,
    PublicBody, Meeting,
    AgendaItemVote, AgendaItem, MemberVote,
)
from poliscopic.db.core import session_scope

log = logging.getLogger(__name__)

members_bp = Blueprint("members", __name__, url_prefix="")

# ---------------------------------------------------------------------------
# BOS Member / Supervisor Voting Portal — Routes
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _date_query_string(start_date, end_date, start_year, end_year) -> str:
    """Build URL query string for date/year filter params."""
    parts = []
    if start_date:
        parts.append(f"start_date={start_date}")
    if end_date:
        parts.append(f"end_date={end_date}")
    if start_year:
        parts.append(f"start_year={start_year}")
    if end_year:
        parts.append(f"end_year={end_year}")
    if parts:
        return "?" + "&".join(parts)
    return ""


def _scoped_read(view):
    """Supply one read session and close it after the rendered response."""
    @wraps(view)
    def wrapped(*args, **kwargs):
        with session_scope() as session:
            return view(session, *args, **kwargs)

    return wrapped


def _get_pz_member_stats(session, person_id, start_date=None, end_date=None):
    """Get basic voting stats for a PZ commissioner."""
    from db.models import MemberVote, Meeting as MeetingModel
    q = select(
        func.count(MemberVote.id).label("total"),
        func.sum(case((MemberVote.vote == "yes", 1), else_=0)).label("yes"),
        func.sum(case((MemberVote.vote == "no", 1), else_=0)).label("no"),
    ).join(
        AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id,
    ).join(
        MeetingModel, and_(MeetingModel.id == AgendaItemVote.meeting_db_id),
    ).where(
        MemberVote.member_id == person_id,
        MemberVote.body == "pz",
    )
    if start_date:
        q = q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        q = q.where(MeetingModel.meeting_date <= end_date)
    row = session.execute(q).one()
    total = row.total or 0
    yes = row.yes or 0
    no = row.no or 0
    abstain = max(total - yes - no, 0)

    # Count split votes attended and dissent
    split_q = select(func.count(AgendaItemVote.id.distinct())).join(
        MemberVote, MemberVote.agenda_item_vote_id == AgendaItemVote.id,
    ).join(
        MeetingModel, and_(MeetingModel.id == AgendaItemVote.meeting_db_id),
    ).where(
        MemberVote.member_id == person_id,
        MemberVote.body == "pz",
        AgendaItemVote.is_split_vote.is_(True),
    )
    if start_date:
        split_q = split_q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        split_q = split_q.where(MeetingModel.meeting_date <= end_date)
    split_attended = session.execute(split_q).scalar() or 0

    # Count dissents (voted with minority)
    dissent_q = select(func.count(MemberVote.id)).where(
        MemberVote.member_id == person_id,
        MemberVote.body == "pz",
        MemberVote.is_dissent.is_(True),
    )
    if start_date:
        dissent_q = dissent_q.join(
            AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id
        ).join(
            MeetingModel, and_(MeetingModel.id == AgendaItemVote.meeting_db_id),
        )
        dissent_q = dissent_q.where(MeetingModel.meeting_date >= start_date)
        dissent_q = dissent_q.where(MeetingModel.meeting_date <= end_date)
    against = session.execute(dissent_q).scalar() or 0

    return {
        "total_votes": total, "yes": yes, "no": no, "abstain": abstain,
        "absences": 0, "attendance_rate": None,
        "split_votes_attended": split_attended,
        "with_majority": split_attended - against,
        "against_majority": against,
        "dissent_rate": round(against / split_attended, 4) if split_attended > 0 else None,
    }


def _get_pz_split_votes(session, person_id, start_date=None, end_date=None):
    """Get split votes involving this PZ commissioner."""
    from db.models import MemberVote, Meeting as MeetingModel
    q = select(
        AgendaItemVote.meeting_id,
        MeetingModel.meeting_date,
        MeetingModel.meeting_type,
        AgendaItemVote.agenda_item_number,
        AgendaItem.c_number,
        AgendaItem.agenda_item_title,
        MemberVote.vote.label("supervisor_vote"),
        AgendaItemVote.motion_result,
        AgendaItemVote.majority_position,
        case(
            (MemberVote.is_dissent.is_(True), "against_majority"),
            else_="with_majority",
        ).label("with_or_against_majority"),
    ).join(
        AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id,
    ).join(
        MeetingModel, and_(MeetingModel.id == AgendaItemVote.meeting_db_id),
    ).outerjoin(
        AgendaItem,
        (and_(AgendaItem.meeting_db_id == AgendaItemVote.meeting_db_id, AgendaItem.body == AgendaItemVote.body))
        & (AgendaItem.agenda_item_number == AgendaItemVote.agenda_item_number),
    ).where(
        MemberVote.member_id == person_id,
        MemberVote.body == "pz",
        AgendaItemVote.is_split_vote.is_(True),
    ).order_by(MeetingModel.meeting_date, AgendaItemVote.agenda_item_number)
    if start_date:
        q = q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        q = q.where(MeetingModel.meeting_date <= end_date)
    rows = session.execute(q).all()
    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "supervisor_vote": r.supervisor_vote or "",
            "motion_result": r.motion_result or "",
            "majority_position": r.majority_position or "",
            "with_or_against_majority": r.with_or_against_majority or "",
        }
        for r in rows
    ]


def _get_pz_dissents(session, person_id, start_date=None, end_date=None):
    """Get dissenting votes for a PZ commissioner."""
    rows = _get_pz_split_votes(session, person_id, start_date, end_date)
    return [r for r in rows if r["with_or_against_majority"] == "against_majority"]


def _get_pz_swing_votes(session, person_id, start_date=None, end_date=None):
    """Get swing votes for a PZ commissioner."""
    from db.models import MemberVote, Meeting as MeetingModel
    from collections import Counter

    # Get all split AIVs this member voted on
    q = select(
        MemberVote.agenda_item_vote_id,
        MemberVote.vote,
    ).join(
        AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id,
    ).join(
        MeetingModel, and_(MeetingModel.id == AgendaItemVote.meeting_db_id),
    ).where(
        MemberVote.member_id == person_id,
        MemberVote.body == "pz",
        AgendaItemVote.is_split_vote.is_(True),
    )
    if start_date:
        q = q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        q = q.where(MeetingModel.meeting_date <= end_date)
    rows = session.execute(q).all()

    if not rows:
        return []

    aiv_ids = [r.agenda_item_vote_id for r in rows]
    sup_vote_map = {r.agenda_item_vote_id: r.vote for r in rows}

    # Get all votes on these AIVs to tally
    all_v = session.execute(
        select(MemberVote.agenda_item_vote_id, MemberVote.vote)
        .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    aiv_tallies: dict[int, Counter] = {}
    for mv in all_v:
        aiv_tallies.setdefault(mv.agenda_item_vote_id, Counter())
        nv = mv.vote.lower() if mv.vote else ""
        if nv in ("yes", "no"):
            aiv_tallies[mv.agenda_item_vote_id][nv] += 1

    swing_aiv_ids: set[int] = set()
    for aiv_id, tally in aiv_tallies.items():
        yes = tally.get("yes", 0)
        no = tally.get("no", 0)
        if yes <= 0 or no <= 0:
            continue
        margin = abs(yes - no)
        if margin != 1:
            continue
        sup_nv = sup_vote_map.get(aiv_id, "").lower()
        if sup_nv not in ("yes", "no"):
            continue
        prev_side = "yes" if yes > no else "no"
        if sup_nv == prev_side:
            swing_aiv_ids.add(aiv_id)

    if not swing_aiv_ids:
        return []

    ids_list = list(swing_aiv_ids)
    from sqlalchemy import text as sa_text
    ids_str = ",".join(str(x) for x in ids_list)
    detail_rows = session.execute(
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
                ON ai.meeting_db_id = aiv.meeting_db_id AND ai.body = aiv.body
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
            WHERE aiv.id IN ({ids_str})
            ORDER BY m.meeting_date, aiv.agenda_item_number
        """)
    ).all()

    results = []
    for r in detail_rows:
        tally = aiv_tallies.get(r.aiv_id, Counter())
        sup_nv = sup_vote_map.get(r.aiv_id, "unknown")
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
        })
    return results


def _get_pz_full_voting_record(session, person_id, start_date=None, end_date=None, limit=25):
    """Get full voting record for a PZ commissioner."""
    from db.models import MemberVote, Meeting as MeetingModel
    q = select(
        MeetingModel.meeting_id,
        MeetingModel.meeting_date,
        MeetingModel.meeting_type,
        AgendaItemVote.agenda_item_number,
        AgendaItem.c_number,
        AgendaItem.agenda_item_title,
        MemberVote.vote,
        AgendaItemVote.motion_result,
        AgendaItemVote.is_split_vote,
        AgendaItemVote.majority_position,
        case(
            (MemberVote.is_dissent.is_(True), "against_majority"),
            else_="with_majority",
        ).label("with_or_against_majority"),
    ).join(
        AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id,
    ).join(
        MeetingModel, and_(MeetingModel.id == AgendaItemVote.meeting_db_id),
    ).outerjoin(
        AgendaItem,
        (and_(AgendaItem.meeting_db_id == AgendaItemVote.meeting_db_id, AgendaItem.body == AgendaItemVote.body))
        & (AgendaItem.agenda_item_number == AgendaItemVote.agenda_item_number),
    ).where(
        MemberVote.member_id == person_id,
        MemberVote.body == "pz",
    ).order_by(
        MeetingModel.meeting_date.desc(),
        AgendaItemVote.agenda_item_number,
    )
    if start_date:
        q = q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        q = q.where(MeetingModel.meeting_date <= end_date)
    if limit:
        q = q.limit(limit)
    rows = session.execute(q).all()
    return [
        {
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "vote": r.vote or "",
            "motion_result": r.motion_result or "",
            "is_split_vote": bool(r.is_split_vote) if r.is_split_vote is not None else False,
            "is_inferred": False,
            "majority_position": r.majority_position or "",
            "with_or_against_majority": r.with_or_against_majority or "",
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# PZ body-level analytics helpers
# ---------------------------------------------------------------------------

def _get_active_pz_commissioners(session, start_date=None, end_date=None):
    """Get PZ commissioners who have votes in the given date range."""
    from db.models import MemberVote, Meeting as MeetingModel
    q = select(
        Person.id, Person.name, Person.normalized_name,
        func.count(MemberVote.id).label("cnt"),
    )
    q = q.join(MemberVote, MemberVote.member_id == Person.id)
    q = q.join(AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id)
    q = q.join(MeetingModel, MeetingModel.id == AgendaItemVote.meeting_db_id)
    q = q.where(MemberVote.body == "pz")
    if start_date:
        q = q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        q = q.where(MeetingModel.meeting_date <= end_date)
    q = q.group_by(Person.id).order_by(Person.name)
    rows = session.execute(q).all()
    return [
        {"id": r.id, "name": r.name,
         "slug": r.normalized_name.replace(" ", "-")}
        for r in rows
    ]


def _get_pz_voting_alignment(session, person_id, other_ids,
                               start_date=None, end_date=None):
    """Pairwise voting alignment for a PZ commissioner vs others."""
    from db.models import MemberVote, Meeting as MeetingModel

    # Get this person's substantive votes
    q = select(MemberVote.agenda_item_vote_id, MemberVote.vote)
    q = q.join(AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id)
    q = q.join(MeetingModel, MeetingModel.id == AgendaItemVote.meeting_db_id)
    q = q.where(MeetingModel.body == "pz")
    q = q.where(MemberVote.member_id == person_id, MemberVote.body == "pz")
    if start_date:
        q = q.where(MeetingModel.meeting_date >= start_date)
    if end_date:
        q = q.where(MeetingModel.meeting_date <= end_date)
    my_rows = session.execute(q).all()
    my_votes: dict[int, str] = {}
    for r in my_rows:
        nv = r.vote.lower() if r.vote else ""
        if nv in ("yes", "no"):
            my_votes[r.agenda_item_vote_id] = nv
    if not my_votes:
        return []

    aiv_ids = list(my_votes.keys())

    # Batch-load split vote flags for all relevant AIVs
    split_aivs: set[int] = set()
    for chunk_start in range(0, len(aiv_ids), 500):
        chunk = aiv_ids[chunk_start:chunk_start + 500]
        for row in session.execute(
            select(AgendaItemVote.id).where(
                AgendaItemVote.id.in_(chunk),
                AgendaItemVote.is_split_vote.is_(True),
            )
        ).all():
            split_aivs.add(row[0])

    other_rows = session.execute(
        select(MemberVote.member_id, MemberVote.agenda_item_vote_id, MemberVote.vote)
        .where(
            MemberVote.member_id.in_(other_ids),
            MemberVote.agenda_item_vote_id.in_(aiv_ids),
            MemberVote.body == "pz",
        )
    ).all()

    other_votes: dict[int, dict[int, str]] = {oid: {} for oid in other_ids}
    for r in other_rows:
        nv = r.vote.lower() if r.vote else ""
        if nv in ("yes", "no"):
            other_votes[r.member_id][r.agenda_item_vote_id] = nv

    results = []
    for oid in other_ids:
        if oid == person_id:
            continue
        orow = session.execute(
            select(Person.name, Person.normalized_name).where(Person.id == oid)
        ).one_or_none()
        if not orow:
            continue
        other_name, other_norm = orow
        other_slug = other_norm.replace(" ", "-") if other_norm else None

        ov = other_votes.get(oid, {})
        common = set(my_votes.keys()) & set(ov.keys())
        if not common:
            continue

        same = diff = 0
        sp_same = sp_total = 0
        for aiv_id in common:
            if my_votes[aiv_id] == ov[aiv_id]:
                same += 1
            else:
                diff += 1
            if aiv_id in split_aivs:
                sp_total += 1
                if my_votes[aiv_id] == ov[aiv_id]:
                    sp_same += 1

        total = same + diff
        results.append({
            "other_supervisor_id": oid,
            "other_name": other_name,
            "slug": other_slug,
            "total_comparable_votes": total,
            "same_votes": same,
            "different_votes": diff,
            "overall_alignment_pct": round(same / total * 100, 1) if total else None,
            "split_vote_comparable": sp_total,
            "split_vote_same": sp_same,
            "split_vote_alignment_pct": round(sp_same / sp_total * 100, 1) if sp_total else None,
        })
    return results


def _get_pz_body_split_votes(session, start_date=None, end_date=None):
    """All PZ split votes in date range with per-member breakdown."""
    from db.models import MemberVote
    from sqlalchemy import text as sa_text

    where_parts = ["aiv.body = 'pz'", "aiv.is_split_vote = 1"]
    params: dict = {}
    if start_date:
        where_parts.append("m.meeting_date >= :start_date")
        params["start_date"] = start_date
    if end_date:
        where_parts.append("m.meeting_date <= :end_date")
        params["end_date"] = end_date

    rows = session.execute(sa_text(f"""
        SELECT aiv.id AS aiv_id, aiv.meeting_id, aiv.agenda_item_number,
               aiv.c_number, aiv.motion_result, ai.agenda_item_title,
               m.meeting_date, m.meeting_type
        FROM agenda_item_votes aiv
        LEFT JOIN agenda_items ai ON ai.meeting_db_id = aiv.meeting_db_id AND ai.body = aiv.body
            AND ai.agenda_item_number = aiv.agenda_item_number
        LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
        WHERE {" AND ".join(where_parts)}
        ORDER BY m.meeting_date DESC, aiv.agenda_item_number
    """), params).all()

    aiv_ids = [r.aiv_id for r in rows]
    if not aiv_ids:
        return []

    mv_rows = session.execute(
        select(MemberVote.agenda_item_vote_id, MemberVote.member_id,
               MemberVote.vote, Person.name, Person.normalized_name)
        .join(Person, Person.id == MemberVote.member_id)
        .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
    ).all()

    aiv_mv: dict = {}
    for mv in mv_rows:
        aiv_mv.setdefault(mv.agenda_item_vote_id, []).append({
            "name": mv.name,
            "slug": mv.normalized_name.replace(" ", "-"),
            "vote": mv.vote or "",
        })

    result = []
    for r in rows:
        mvs = aiv_mv.get(r.aiv_id, [])
        yes = sum(1 for m in mvs if m["vote"].lower() in ("yes", "aye"))
        no = sum(1 for m in mvs if m["vote"].lower() in ("no", "nay"))
        result.append({
            "meeting_id": r.meeting_id,
            "meeting_date": r.meeting_date,
            "meeting_type": r.meeting_type,
            "agenda_item_number": r.agenda_item_number,
            "agenda_item_title": r.agenda_item_title,
            "c_number": r.c_number or "",
            "motion_result": r.motion_result or "",
            "vote_tally": f"{yes}-{no}",
            "member_votes": mvs,
        })
    return result


VOTE_BADGE_CLASSES = {
    "yes": "success",
    "no": "danger",
    "abstain": "warning",
    "absent": "secondary",
    "recused": "secondary",
}

MAJORITY_BADGE_CLASSES = {
    "with_majority": "success",
    "against_majority": "danger",
}


@members_bp.route("/members")
def members():
    """Member directory — redirect to the unified bodies index."""
    return redirect("/bodies")


@members_bp.route("/api/members/<slug>/votes")
def member_votes_api(slug):
    """JSON API for the Full Voting Record table.

    Supports pagination, search, date filtering, and per-column filtering
    via query params.

    Date filtering:
        start_date=YYYY-MM-DD
        end_date=YYYY-MM-DD
        start_year=YYYY   (overrides start_date)
        end_year=YYYY     (overrides end_date)
    """
    try:
        page = int(request.args.get("page", 1))
    except ValueError:
        page = 1
    try:
        per_page = int(request.args.get("per_page", 25))
    except ValueError:
        per_page = 25
    per_page = max(10, min(100, per_page))
    search_q = (request.args.get("q") or "").strip().lower()
    filter_vote = (request.args.get("vote") or "").strip().lower()
    filter_result = (request.args.get("result") or "").strip().lower()
    filter_majority = (request.args.get("majority") or "").strip().lower()
    filter_split = (request.args.get("split") or "").strip().lower()

    # Parse date/year parameters
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    start_year = request.args.get("start_year")
    end_year = request.args.get("end_year")
    if start_year:
        start_date = f"{start_year}-01-01"
    if end_year:
        end_date = f"{end_year}-12-31"

    # Load and project the full date-filtered dataset inside one owned session.
    with session_scope() as session:
        sup = get_supervisor_by_slug_or_name(session, slug)
        if not sup:
            return jsonify(
                {"rows": [], "total": 0, "page": 1, "per_page": 25}
            ), 200

        from db.models import MemberVote as _MV

        has_pz = session.execute(
            select(func.count(_MV.id)).where(
                _MV.member_id == sup.id,
                _MV.body == "pz",
            )
        ).scalar() or 0
        if has_pz:
            all_records = _get_pz_full_voting_record(
                session,
                sup.id,
                start_date=start_date,
                end_date=end_date,
            )
        else:
            body_code = request.args.get("body", "bos")
            all_records = get_supervisor_full_voting_record(
                session,
                sup.id,
                body=body_code,
                start_date=start_date,
                end_date=end_date,
            )

    if not all_records:
        return jsonify({"rows": [], "total": 0, "page": 1, "per_page": 25}), 200

    # Apply filters
    filtered = all_records
    if search_q:
        filtered = [
            r for r in filtered
            if search_q in (r.get("agenda_item_title") or "").lower()
            or search_q in (r.get("meeting_date") or "")
            or search_q in (r.get("meeting_type") or "").lower()
            or search_q in str(r.get("agenda_item_number", ""))
            or search_q in (r.get("c_number") or "").lower()
            or search_q in (r.get("vote") or "")
            or search_q in (r.get("motion_result") or "").lower()
        ]
    if filter_vote and filter_vote != "all":
        filtered = [r for r in filtered if (r.get("vote") or "") == filter_vote]
    if filter_result and filter_result != "all":
        filtered = [r for r in filtered if (r.get("motion_result") or "") == filter_result]
    if filter_majority and filter_majority != "all":
        filtered = [
            r for r in filtered
            if (r.get("with_or_against_majority") or "") == filter_majority
        ]
    if filter_split in ("true", "1", "yes"):
        filtered = [r for r in filtered if r.get("is_split_vote")]
    elif filter_split in ("false", "0", "no"):
        filtered = [r for r in filtered if not r.get("is_split_vote")]

    total = len(filtered)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = min(start + per_page, total)
    rows = filtered[start:end]

    return jsonify({
        "rows": [
            {
                "meeting_id": r["meeting_id"],
                "meeting_date": r["meeting_date"],
                "meeting_type": r["meeting_type"],
                "agenda_item_number": r["agenda_item_number"],
                "agenda_item_title": r["agenda_item_title"],
                "c_number": r["c_number"],
                "vote": r["vote"],
                "motion_result": r["motion_result"],
                "is_split_vote": r["is_split_vote"],
                "is_inferred": r["is_inferred"],
                "majority_position": r["majority_position"],
                "with_or_against_majority": r["with_or_against_majority"],
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "per_page": per_page,
    })


@members_bp.route("/members/<jurisdiction_slug>/<body_code>/analytics")
@_scoped_read
def body_analytics(session, jurisdiction_slug, body_code):
    """Body-level analytics — cross-member voting alignment.

    Supports date/year filtering via query parameters:
        start_date=YYYY-MM-DD
        end_date=YYYY-MM-DD
        start_year=YYYY   (overrides start_date)
        end_year=YYYY     (overrides end_date)
    """
    # Parse date/year parameters
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    start_year = request.args.get("start_year")
    end_year = request.args.get("end_year")
    if start_year:
        start_date = f"{start_year}-01-01"
    if end_year:
        end_date = f"{end_year}-12-31"

    # Get all BOS supervisors, then filter by active in date range
    # Get supervisors/commissioners active in date range
    is_pz = body_code == "pz"
    if is_pz:
        all_sups = _get_active_pz_commissioners(
            session, start_date=start_date, end_date=end_date,
        )
    else:
        all_sups = get_bos_supervisors(session)
    active_sups = []
    body_stats = {
        "total_votes": 0, "yes": 0, "no": 0, "abstain": 0,
        "absences": 0, "split_votes_attended": 0,
        "with_majority": 0, "against_majority": 0,
    }
    for sup in all_sups:
        sup_id = sup["id"] if is_pz else sup.id
        if is_pz:
            stats = _get_pz_member_stats(
                session, sup_id,
                start_date=start_date, end_date=end_date,
            )
        else:
            stats = get_supervisor_vote_stats(
                session, sup_id, body=body_code,
                start_date=start_date, end_date=end_date,
            )
        if stats["total_votes"] == 0:
            continue  # no meetings or votes in this time frame
        # Normalize to dict for template compatibility (BOS Supervisor vs PZ dict)
        if not isinstance(sup, dict):
            sup = {
                "id": sup.id,
                "name": sup.name,
                "slug": sup.normalized_name.replace(" ", "-"),
            }
        active_sups.append(sup)
        body_stats["total_votes"] += stats["total_votes"]
        body_stats["yes"] += stats["yes"]
        body_stats["no"] += stats["no"]
        body_stats["abstain"] += stats["abstain"]
        body_stats["absences"] += stats["absences"]
        body_stats["split_votes_attended"] += stats["split_votes_attended"]
        body_stats["with_majority"] += stats["with_majority"]
        body_stats["against_majority"] += stats["against_majority"]

    if not active_sups:
        return render_template(
            "body_analytics.html",
            jurisdiction_slug=jurisdiction_slug,
            body_code=body_code,
            active_sups=[], body_summary=[], alignments={},
            heatmap_members=[], heatmap_matrix=[],
            body_stats=body_stats,
            start_date=start_date or "", end_date=end_date or "",
            start_year=start_year or "", end_year=end_year or "",
        )

    # Aggregate dissent rate
    sp = body_stats["split_votes_attended"]
    body_stats["dissent_rate"] = (
        round(body_stats["against_majority"] / sp, 4) if sp > 0 else None
    )

    # For each active supervisor, get voting alignment and stats
    alignments = {}
    body_summary = []
    heatmap_members = []
    heatmap_matrix = []

    if is_pz:
        for sup in active_sups:
            sid = sup["id"]
            slug = sup.get("slug", sup["name"].lower().replace(" ", "-"))
            heatmap_members.append({"name": sup["name"], "slug": slug, "id": sid})

            stats = _get_pz_member_stats(
                session, sid,
                start_date=start_date, end_date=end_date,
            )

            other_ids = [s["id"] for s in active_sups if s["id"] != sid]
            al = _get_pz_voting_alignment(
                session, sid, other_ids,
                start_date=start_date, end_date=end_date,
            )
            alignments[sid] = {
                "name": sup["name"],
                "slug": slug,
                "stats": stats,
                "pairs": al,
            }
            total = sum(a["total_comparable_votes"] for a in al)
            same = sum(a["same_votes"] for a in al)
            diff = sum(a["different_votes"] for a in al)
            sp_total = sum(a["split_vote_comparable"] for a in al)
            sp_same = sum(a["split_vote_same"] for a in al)
            body_summary.append({
                "name": sup["name"], "slug": slug,
                "total_comparable": total, "same": same, "different": diff,
                "overall_pct": round(same / total * 100, 1) if total else None,
                "split_comparable": sp_total, "split_same": sp_same,
                "split_pct": round(sp_same / sp_total * 100, 1) if sp_total else None,
            })
    else:
        for sup in active_sups:
            sid = sup["id"]
            slug = sup["slug"]
            heatmap_members.append({"name": sup["name"], "slug": slug, "id": sid})

            stats = get_supervisor_vote_stats(
                session, sid, body=body_code,
                start_date=start_date, end_date=end_date,
            )

            al = get_supervisor_voting_alignment(
                session, sid, body=body_code,
                start_date=start_date, end_date=end_date,
            )
            alignments[sid] = {
                "name": sup["name"],
                "slug": slug,
                "stats": stats,
                "pairs": al,
            }
            total = sum(a["total_comparable_votes"] for a in al)
            same = sum(a["same_votes"] for a in al)
            diff = sum(a["different_votes"] for a in al)
            sp_total = sum(a["split_vote_comparable"] for a in al)
            sp_same = sum(a["split_vote_same"] for a in al)
            body_summary.append({
                "name": sup["name"], "slug": slug,
                "total_comparable": total, "same": same, "different": diff,
                "overall_pct": round(same / total * 100, 1) if total else None,
                "split_comparable": sp_total, "split_same": sp_same,
                "split_pct": round(sp_same / sp_total * 100, 1) if sp_total else None,
            })

    # Build heatmap matrix: matrix[i][j] = alignment% of member i vs member j
    for i, sup_i in enumerate(active_sups):
        sup_i_id = sup_i["id"]
        row = []  # diagonal handled by loop (i == j)
        pairs_by_oid = {
            p["other_supervisor_id"]: p["split_vote_alignment_pct"]
            for p in alignments.get(sup_i_id, {}).get("pairs", [])
        }
        for j, sup_j in enumerate(active_sups):
            sup_j_id = sup_j["id"]
            if i == j:
                row.append(None)
            else:
                row.append(pairs_by_oid.get(sup_j_id))
        heatmap_matrix.append(row)

    # ─── Split votes in this date range ───
    split_votes_data = []
    if is_pz:
        split_votes_data = _get_pz_body_split_votes(
            session, start_date=start_date, end_date=end_date,
        )
    elif body_stats["split_votes_attended"] > 0:
        from sqlalchemy import text as sa_text
        where_parts = ["aiv.body = :body", "aiv.is_split_vote = 1"]
        params: dict = {"body": body_code}
        if start_date:
            where_parts.append("m.meeting_date >= :start_date")
            params["start_date"] = start_date
        if end_date:
            where_parts.append("m.meeting_date <= :end_date")
            params["end_date"] = end_date
        where_sql = " AND ".join(where_parts)
        rows = session.execute(sa_text(f"""
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
                ON ai.meeting_db_id = aiv.meeting_db_id AND ai.body = aiv.body
                AND ai.agenda_item_number = aiv.agenda_item_number
            LEFT JOIN meetings m ON m.id = aiv.meeting_db_id AND m.body = aiv.body
            WHERE {where_sql}
            ORDER BY m.meeting_date DESC, aiv.agenda_item_number
        """), params).all()
        aiv_ids = [r.aiv_id for r in rows]

        if aiv_ids:
            # Get per-member votes for each AIV
            member_votes = session.execute(
                select(
                    MemberVote.agenda_item_vote_id,
                    MemberVote.member_id,
                    MemberVote.vote,
                    Person.name,
                    Person.normalized_name,
                )
                .join(Person, Person.id == MemberVote.member_id)
                .where(MemberVote.agenda_item_vote_id.in_(aiv_ids))
            ).all()

            # Organize: aiv_id -> {sup_id -> vote, supervisor_name -> name, slug -> slug}
            aiv_member_votes: dict = {}
            for mv in member_votes:
                aiv_member_votes.setdefault(mv.agenda_item_vote_id, []).append({
                    "name": mv.name,
                    "slug": mv.normalized_name.replace(" ", "-"),
                    "vote": mv.vote or "",
                })

            # Build tally
            for r in rows:
                mvs = aiv_member_votes.get(r.aiv_id, [])
                yes = sum(1 for m in mvs if m["vote"].lower() in ("yes", "aye"))
                no = sum(1 for m in mvs if m["vote"].lower() in ("no", "nay"))
                split_votes_data.append({
                    "meeting_id": r.meeting_id,
                    "meeting_date": r.meeting_date,
                    "meeting_type": r.meeting_type,
                    "agenda_item_number": r.agenda_item_number,
                    "agenda_item_title": r.agenda_item_title,
                    "c_number": r.c_number or "",
                    "motion_result": r.motion_result or "",
                    "vote_tally": f"{yes}-{no}",
                    "member_votes": mvs,
                })

    return render_template(
        "body_analytics.html",
        jurisdiction_slug=jurisdiction_slug,
        body_code=body_code,
        active_sups=active_sups,
        body_summary=body_summary,
        alignments=alignments,
        body_stats=body_stats,
        heatmap_members=heatmap_members,
        heatmap_matrix=heatmap_matrix,
        split_votes=split_votes_data,
        start_date=start_date or "",
        end_date=end_date or "",
        start_year=start_year or "",
        end_year=end_year or "",
        vote_badges=VOTE_BADGE_CLASSES,
        majority_badges=MAJORITY_BADGE_CLASSES,
    )


def _load_member_detail_data(session, body_code, slug, start_date, end_date):
    """Load a member profile as render-safe values owned by the caller."""
    sup = get_supervisor_by_slug_or_name(session, slug)
    if not sup:
        return None

    slug_out = get_supervisor_slug(sup)
    is_pz = body_code == "pz"

    if is_pz:
        stats = _get_pz_member_stats(
            session, sup.id,
            start_date=start_date, end_date=end_date,
        )
        split_votes = _get_pz_split_votes(
            session, sup.id,
            start_date=start_date, end_date=end_date,
        )
        dissents = _get_pz_dissents(
            session, sup.id,
            start_date=start_date, end_date=end_date,
        )
        abstentions = []
        absences = []
        full_record = _get_pz_full_voting_record(
            session, sup.id, limit=25,
            start_date=start_date, end_date=end_date,
        )
        full_record_count = stats["total_votes"]
        swing_votes = _get_pz_swing_votes(
            session, sup.id,
            start_date=start_date, end_date=end_date,
        )
    else:
        stats = get_supervisor_vote_stats(
            session, sup.id, body=body_code,
            start_date=start_date, end_date=end_date,
        )
        split_votes = get_supervisor_split_votes(
            session, sup.id, body=body_code,
            start_date=start_date, end_date=end_date,
        )
        dissents = [s for s in split_votes if s.get("with_or_against_majority") == "against_majority"]
        abstentions = get_supervisor_abstentions(
            session, sup.id, body=body_code,
            start_date=start_date, end_date=end_date,
        )
        absences = get_supervisor_absences(
            session, sup.id, body=body_code,
            start_date=start_date, end_date=end_date,
        )
        full_record = get_supervisor_full_voting_record(
            session, sup.id, body=body_code, limit=25,
            start_date=start_date, end_date=end_date,
        )
        swing_votes = get_supervisor_swing_votes(
            session, sup.id, body=body_code,
            start_date=start_date, end_date=end_date,
        )
        count_q = (
            select(func.count())
            .select_from(MemberVote)
            .join(AgendaItemVote, AgendaItemVote.id == MemberVote.agenda_item_vote_id)
            .join(Meeting, and_(Meeting.id == AgendaItemVote.meeting_db_id))
            .where(MemberVote.member_id == sup.id, AgendaItemVote.body == body_code)
        )
        if start_date:
            count_q = count_q.where(Meeting.meeting_date >= start_date)
        if end_date:
            count_q = count_q.where(Meeting.meeting_date <= end_date)
        full_record_count = session.execute(count_q).scalar() or 0

    # Resolve body slug for the "All members" back link
    body_slug = None
    if body_code:
        _body = session.execute(
            select(PublicBody).where(PublicBody.body_code == body_code)
        ).scalar_one_or_none()
        if _body:
            body_slug = _body.slug

    return {
        "member": {
            "name": sup.name,
            "district": getattr(sup, "district", None),
        },
        "slug": slug_out,
        "body_slug": body_slug,
        "is_pz": is_pz,
        "stats": stats,
        "split_votes": split_votes,
        "dissents": dissents,
        "abstentions": abstentions,
        "absences": absences,
        "full_record": full_record,
        "full_record_count": full_record_count,
        "swing_votes": swing_votes,
    }


@members_bp.route("/members/<jurisdiction_slug>/<body_code>/<slug>")
def member_detail(jurisdiction_slug, body_code, slug):
    """Member profile by jurisdiction + body + slug — disambiguates names."""
    start_date = request.args.get("start_date")
    end_date = request.args.get("end_date")
    start_year = request.args.get("start_year")
    end_year = request.args.get("end_year")
    if start_year:
        start_date = f"{start_year}-01-01"
    if end_year:
        end_date = f"{end_year}-12-31"

    with session_scope() as session:
        detail = _load_member_detail_data(
            session,
            body_code,
            slug,
            start_date,
            end_date,
        )
    if detail is None:
        return render_template("member_detail.html", member=None, slug=slug)

    filtered_analytics_url = (
        f"/members/{jurisdiction_slug}/{body_code}/analytics"
    )

    qs = _date_query_string(start_date, end_date, start_year, end_year)
    body_param = f"body={body_code}"
    sep = "&" if qs else "?"
    full_record_api_url = (
        f"/api/members/{detail['slug']}/votes{qs}{sep}{body_param}"
    )

    return render_template(
        "member_detail.html",
        member=detail["member"],
        slug=detail["slug"],
        body_slug=detail["body_slug"],
        filtered_analytics_url=filtered_analytics_url,
        is_pz=detail["is_pz"],
        stats=detail["stats"],
        split_votes=detail["split_votes"],
        dissents=detail["dissents"],
        abstentions=detail["abstentions"],
        absences=detail["absences"],
        full_record=detail["full_record"],
        full_record_count=detail["full_record_count"],
        swing_votes=detail["swing_votes"],
        full_record_api_url=full_record_api_url,
        vote_badges=VOTE_BADGE_CLASSES,
        majority_badges=MAJORITY_BADGE_CLASSES,
        member_url=(
            f"/members/{jurisdiction_slug}/{body_code}/{detail['slug']}"
        ),
        start_date=start_date or "",
        end_date=end_date or "",
        start_year=start_year or "",
        end_year=end_year or "",
    )


@members_bp.route("/members/<slug>")
def member_detail_legacy(slug):
    """Legacy member route — redirect to qualified URL.

    Preserves query parameters (e.g., ?start_year=2025).
    Detects whether the member is a BOS supervisor or PZ commissioner.
    """
    with session_scope() as session:
        sup = get_supervisor_by_slug_or_name(session, slug)
        if sup:
            slug_out = get_supervisor_slug(sup)
            # Detect body: check if this person has PZ votes
            from db.models import MemberVote as _MV
            from sqlalchemy import select as _sel, func as _fn

            has_pz = session.execute(
                _sel(_fn.count(_MV.id)).where(
                    _MV.member_id == sup.id,
                    _MV.body == "pz",
                )
            ).scalar() or 0
            body_code = "pz" if has_pz > 0 else "bos"
            qs = request.query_string.decode() if request.query_string else ""
            target = f"/members/maricopa-county/{body_code}/{slug_out}"
            if qs:
                target += "?" + qs
            return redirect(target)
    return render_template("member_detail.html", member=None, slug=slug)


@members_bp.route("/debug/inferred-abstentions")
def debug_inferred_abstentions():
    """Review page for parser-gap detection.

    Lists all inferred abstentions grouped by meeting, so humans can
    quickly check whether the vote was truly abstained or the parser
    missed an explicit Yes/No from the summary.
    """
    # Joining AgendaItemVote → Meeting for meeting_date
    with session_scope() as session:
        rows = session.execute(
            sa_text("""
                SELECT
                    aiv.meeting_id,
                    m.meeting_date,
                    aiv.agenda_item_number,
                    aiv.vote_text,
                    mv.member_id,
                    sup.name,
                    sup.normalized_name
                FROM member_votes mv
                JOIN agenda_item_votes aiv ON aiv.id = mv.agenda_item_vote_id
                JOIN persons sup ON sup.id = mv.member_id
                LEFT JOIN meetings m
                    ON m.id = aiv.meeting_db_id AND m.body = aiv.body
                WHERE mv.raw_vote_text LIKE :prefix
                  AND aiv.body = :body
                ORDER BY aiv.meeting_id, aiv.agenda_item_number
            """)
            .bindparams(prefix="inferred%", body="bos")
        ).all()

    # Group by meeting
    by_meeting: dict[str, list] = {}
    for r in rows:
        mid = r.meeting_id
        by_meeting.setdefault(mid, []).append({
            "meeting_date": r.meeting_date,
            "agenda_item_number": r.agenda_item_number,
            "vote_text": (r.vote_text or "")[:200],
            "supervisor_name": r.name,
            "supervisor_slug": r.normalized_name.replace(" ", "-"),
        })

    return render_template(
        "debug_inferred.html",
        by_meeting=sorted(by_meeting.items(), key=lambda kv: kv[0]),
    )


@members_bp.route("/members/<slug>/analytics")
def member_analytics(slug):
    """Legacy per-member analytics — redirect to member profile."""
    qs = request.query_string.decode() if request.query_string else ""
    target = f"/members/{slug}"
    if qs:
        target += "?" + qs
    return redirect(target)
