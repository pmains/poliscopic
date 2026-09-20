"""Extract vote data from Tempe Development Review Commission (DRC) Meeting Summaries.

The DRC publishes summary documents (doctype=3 in OnBase) for Regular
meetings only. Results include per-item result lines with optional inline
vote tallies (aggregate only — no individual commissioner names).

See the original source file at scripts/scraper/jurisdictions/tempe_drc_summary.py
for the full docstring with example formats.
"""

from __future__ import annotations

import logging
import re
from html.parser import HTMLParser
from typing import Optional

log = logging.getLogger(__name__)

# ── Result keywords found in DRC summary documents ──

DRC_RESULT_KEYWORDS = frozenset({
    "APPROVED",
    "DENIED",
    "FAILED",
    "WITHDRAWN",
    "RECOMMENDED APPROVAL",
    "RECOMMENDED APPROVAL ON CONSENT",
    "APPROVED ON CONSENT",
    "CONTINUED",
})

# ── Vote tally patterns ──

VOTE_TALLY_RE = re.compile(
    r"\((?P<aye>\d+)\s*-\s*(?P<nay>\d+)\s*VOTE"
    r"(?:\s*,\s*(?P<abstain>\d+)\s*ABST(?:AIN|ENTION))?\)",
    re.IGNORECASE,
)

# ── Text extraction from HTML ──


class _SummaryTextExtractor(HTMLParser):
    """Extract visible text from OnBase ViewAgenda HTML response."""

    def __init__(self):
        super().__init__()
        self.text_parts: list[str] = []
        self._skip_tags = {"script", "style", "meta", "link", "head", "title"}
        self._tag_stack: list[str] = []

    def handle_starttag(self, tag, attrs):
        self._tag_stack.append(tag)

    def handle_endtag(self, tag):
        if self._tag_stack and self._tag_stack[-1] == tag:
            self._tag_stack.pop()
            if tag not in self._skip_tags:
                self.text_parts.append("\n")

    def handle_data(self, data):
        if not any(t in self._skip_tags for t in self._tag_stack):
            text = data.strip()
            if text:
                self.text_parts.append(text + " ")


def _clean_extracted_text(raw: str) -> str:
    """Normalize whitespace in the extracted summary text."""
    text = re.sub(r"\n{3,}", "\n\n", raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"^\s+", "", text, flags=re.MULTILINE)
    return text.strip()


# ── Document fetching ──

DRC_SUMMARY_URL = (
    "https://tempe.hylandcloud.com/Agendaonline"
    "/Documents/ViewAgenda"
    "?meetingId={meeting_id}&type=summary&doctype=3"
)


def fetch_drc_summary_text(meeting_id: int) -> str:
    """Fetch the DRC meeting summary document as plain text.

    Uses the accessible HTML view from OnBase (ViewAgenda endpoint).
    Returns the extracted plain text, or empty string on failure.
    """
    import urllib.request
    from http.cookiejar import CookieJar

    url = DRC_SUMMARY_URL.format(meeting_id=meeting_id)

    cj = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))

    base = "https://tempe.hylandcloud.com/Agendaonline"
    req_base = urllib.request.Request(base, headers={"User-Agent": "Mozilla/5.0"})
    try:
        opener.open(req_base, timeout=15)
    except Exception as e:
        log.warning("Session establishment failed: %s", e)
        return ""

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with opener.open(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
    except Exception as e:
        log.warning("Failed to fetch DRC summary for meeting %d: %s", meeting_id, e)
        return ""

    if not html or len(html) < 500:
        return ""
    if "Document unavailable" in html or "Runtime Error" in html:
        return ""

    parser = _SummaryTextExtractor()
    try:
        parser.feed(html)
    except Exception as e:
        log.warning("HTML parsing error for meeting %d: %s", meeting_id, e)
        return ""

    text = "".join(parser.text_parts)
    text = _clean_extracted_text(text)
    return text


# ── Parsing ──


def _parse_vote_tally(text: str) -> dict:
    """Parse a vote tally like ``(7-0 VOTE)`` or ``(5-0 VOTE, 1 ABSTAIN)``."""
    m = VOTE_TALLY_RE.search(text)
    if not m:
        return {"aye": 0, "nay": 0, "abstain": 0}
    return {
        "aye": int(m.group("aye")),
        "nay": int(m.group("nay")),
        "abstain": int(m.group("abstain")) if m.group("abstain") else 0,
    }


def extract_items_from_summary(
    text: str,
    header_prefixes: Optional[set[str]] = None,
    section_headings: Optional[set[str]] = None,
) -> list[dict]:
    """Parse DRC summary text and extract agenda item results with vote tallies.

    Parameters
    ----------
    text : str
        Plain text extracted from summary document.
    header_prefixes : set[str], optional
        Header line prefixes to skip.
    section_headings : set[str], optional
        Section heading lines to skip entirely.

    Returns a list of dicts:
        {agenda_item_number, motion_result, vote_text, aye, nay, abstain}
    """
    lines = text.split("\n")
    items: list[dict] = []

    if header_prefixes is None:
        header_prefixes = {
            "Development Review Commission",
            "Harry E. Mitchell", "Tempe City Hall", "Virtual meeting",
            "REGULAR MEETING SUMMARY",
            "Members of the Development Review", "Visit",
            "Virtual Board & Commission Meetings",
        }

    if section_headings is None:
        section_headings = {
            "CALL TO ORDER", "CONSIDERATION OF MEETING MINUTES",
            "DEVELOPMENT PLAN REVIEW APPEAL:",
            "USE PERMITS",
            "GENERAL PLAN AMENDMENT / ZONING MAP AMENDMENT / PLANNED AREA DEVELOPMENT OVERLAY",
            "CODE TEXT AMENDMENT", "CODE TEXT AMENDMENT:", "ANNOUNCEMENTS / MISCELLANEOUS",
            "ADJOURNMENT",
        }

    current_item: Optional[dict] = None
    collected_description: list[str] = []

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            if current_item and collected_description:
                current_item["description"] = "\n".join(collected_description).strip()
            current_item = None
            collected_description = []
            continue

        if i < 15 and any(stripped.startswith(p) for p in header_prefixes):
            continue
        if stripped in header_prefixes:
            continue

        item_match = re.match(r"^(\d+[A-Za-z]?)\.?\s*$", stripped)
        if item_match:
            if current_item and collected_description:
                current_item["description"] = "\n".join(collected_description).strip()
            current_item = {
                "agenda_item_number": item_match.group(1),
                "line_idx": i,
                "description": "",
            }
            collected_description = []
            continue

        if current_item is None and re.match(r"^[A-Z][A-Z\s/]+:$", stripped):
            continue
        if current_item is None and stripped in section_headings:
            continue

        if current_item:
            result_candidate = stripped.upper()
            is_result = False
            for kw in DRC_RESULT_KEYWORDS:
                if result_candidate.startswith(kw):
                    is_result = True
                    break
                if result_candidate.startswith(kw) and VOTE_TALLY_RE.search(stripped):
                    is_result = True
                    break

            if is_result:
                tally = _parse_vote_tally(stripped)
                first_part = re.split(r"\s*[(\[]", stripped)[0].strip()
                items.append({
                    "agenda_item_number": current_item["agenda_item_number"],
                    "motion_result": first_part,
                    "vote_text": stripped,
                    "aye": tally["aye"],
                    "nay": tally["nay"],
                    "abstain": tally["abstain"],
                })
                current_item = None
                collected_description = []
            else:
                collected_description.append(stripped)
        else:
            pass

    if current_item and collected_description:
        current_item["description"] = "\n".join(collected_description).strip()

    return items


# ── Main entry point ──


def extract_drc_votes(meeting_id: int) -> dict:
    """Fetch and parse DRC meeting summary, returning vote data."""
    text = fetch_drc_summary_text(meeting_id)
    if not text:
        log.info("No DRC summary text available for meeting %d", meeting_id)
        return {"votes": []}

    items = extract_items_from_summary(text)
    log.info("DRC summary for meeting %d: %d items with results", meeting_id, len(items))
    return {"votes": items}


def backfill_drc_votes(dry_run: bool = True, limit: int = 0,
                       verbose: bool = True) -> dict:
    """Backfill DRC vote data for meetings missing summary votes."""
    import sys
    sys.path.insert(0, ".")
    from db import get_session, Meeting as MeetingModel, AgendaItemVote
    from db.persist import persist_votes
    from sqlalchemy import select, func

    session = get_session()
    meetings_with_votes = (
        select(func.distinct(AgendaItemVote.meeting_db_id))
        .select_from(AgendaItemVote)
        .where(AgendaItemVote.meeting_db_id.isnot(None))
    )

    # "tempe-development-review-commission" was retired in the 2026-09-18 dev
    # body-code cleanup; all its rows were reparented to tempe-drc
    # (docs/briefs/034-dev-body-cleanup-changes-2026-09-18.md).
    DRC_BODIES = ("tempe-drc",)

    rows = session.execute(
        select(MeetingModel.id, MeetingModel.meeting_id, MeetingModel.meeting_date,
               MeetingModel.meeting_type, MeetingModel.body)
        .where(MeetingModel.body.in_(DRC_BODIES))
        .where(MeetingModel.meeting_type.ilike("%Regular%"))
        .where(MeetingModel.sync_status.in_(("complete", "pending")))
        .where(~MeetingModel.id.in_(meetings_with_votes) | MeetingModel.id.is_(None))
        .order_by(MeetingModel.meeting_date)
    ).all()

    if not rows:
        if verbose:
            print("  (no DRC meetings without votes)")
        return {"attempted": 0, "found_votes": 0, "no_summary": 0, "errors": 0, "details": []}

    past_rows = [r for r in rows if r.meeting_date and r.meeting_date < "2026-06-01"]
    if limit:
        past_rows = past_rows[:limit]

    details: list[dict] = []
    counts = {"found": 0, "no_summary": 0, "errors": 0}

    for idx, row in enumerate(past_rows, 1):
        meeting_db_id = row.id
        meeting_id = row.meeting_id
        meeting_date = row.meeting_date or ""
        body_code = row.body

        try:
            vote_data = extract_drc_votes(int(meeting_id))
        except Exception as e:
            counts["errors"] += 1
            err_msg = str(e)[:200]
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: ERROR - {err_msg}")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                            "status": "error", "error": err_msg})
            continue

        num_votes = len(vote_data["votes"])

        if num_votes > 0:
            counts["found"] += 1
            if not dry_run:
                session = get_session()
                try:
                    persist_votes(session, body_code, meeting_id, [],
                                  vote_data["votes"])
                    session.commit()
                except Exception as e:
                    session.rollback()
                    counts["errors"] += 1
                    err_msg = str(e)[:200]
                    if verbose:
                        print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: PERSIST ERROR - {err_msg}")
                    details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                                    "status": "persist_error", "error": err_msg})
                    continue
                finally:
                    session.close()
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: {num_votes} votes")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date,
                            "status": "votes_found", "vote_count": num_votes})
        else:
            counts["no_summary"] += 1
            if verbose:
                print(f"  [{idx}/{len(past_rows)}] {meeting_id} {meeting_date}: no summary")
            details.append({"meeting_id": meeting_id, "meeting_date": meeting_date, "status": "no_summary"})

    return {"attempted": len(past_rows), "found_votes": counts["found"],
            "no_summary": counts["no_summary"], "errors": counts["errors"], "details": details}
