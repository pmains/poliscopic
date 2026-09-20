"""persist module."""

import logging
import re
from datetime import date, datetime, timezone
from typing import Optional

log = logging.getLogger(__name__)

from sqlalchemy import func, inspect as sa_inspect, select, text, or_
from sqlalchemy.orm import Session

from db.models import (Base, Meeting, AgendaItem, SupportingDocument,
    AgendaItemVote, MemberVote, Supervisor, Case, CaseEvent,
    PZItemDetail, MeetingMember, PublicBodyMember, Person,
    BodyMembership, PublicBody, MeetingAttendance, MemberVote,
    IngestFailure)
from db.core import get_engine, get_session
from db.queries import _resolve_jurisdiction_id
from db.meeting_utils import (
    normalize_meeting_type, extract_meeting_context,
    extract_meeting_body, build_meeting_display_name,
)
from db.meeting_members import AttendanceRecord, reconcile_meeting_members

# ── Name validation safeguard for persist_votes ──
# Known first names extracted from existing Person records in our database.
# Used to verify that a candidate name contains at least one plausible
# first or last name, rejecting agenda-item titles and section headers.
_KNOWN_FIRST_NAMES = frozenset({
    "mark", "scott", "rich", "jennifer", "alicia", "francisco", "dorean",
    "john", "julie", "kevin", "angel", "christine", "matt", "jane",
    "bill", "clint", "steve", "thomas", "debbie", "kate", "kelly",
    "jen", "brooke", "jennifer", "nikki", "arlene", "doreen", "berdetta",
    "randy", "corey", "joel", "jack", "lucas", "lily", "erik", "spike",
    # Common test-friendly and data names
    "alice", "bob", "david", "michael", "james", "robert", "mary",
    "linda", "patricia", "barbara", "elizabeth", "susan", "jessica",
    "sarah", "karen", "nancy", "lisa", "betty", "margaret", "sandra",
    "ashley", "kimberly", "emily", "donna", "michelle", "dorothy",
    "jimmy", "jay", "greg", "kevin", "francisca", "jan", "mihai", "linda",
    "alex", "warren", "derrik", "mitchell", "jackie", "denny",
    "tom", "john", "jane", "od",
})

_KNOWN_LAST_NAMES = frozenset({
    "freeman", "somers", "adams", "duff", "goforth",
    "heredia", "taylor", "giles", "spilsbury", "hartke", "encinas",
    "ellis", "orlando", "harris", "poston", "hawkins", "sehgal",
    "garcia", "bivens", "carroll", "reed", "hernandez", "korte",
    "briggs", "brown", "hackel", "udall", "keim", "hodge",
    # Test fixture names and common last names
    "amberg", "chin", "keating", "smith", "jones", "doe",
    "taylor", "miller", "wilson", "moore", "anderson", "thomas",
    "jackson", "white", "harris", "martin", "thompson", "clark",
    "williamson", "woods", "arredondo", "garlid", "navarro", "shah",
    "ballesteros", "gage", "starr", "evans", "cegar", "johnson",
    "jones", "valenzuela", "lesko", "galvin", "stewart", "lake",
    "beck", "crawford", "finn", "stokes", "dunn", "edwards", "bullock",
    "meggesto", "thompson", "cook", "santos", "brophy", "rodriguez",
    "baker", "curley", "landolt", "lindblom", "swart", "arnett",
    "danzeisen", "montoya", "leighton", "toma", "milhaven", "finter",
    "whitney", "rochwalik", "schlosser", "chucri", "hickman", "gallardo",
    "fackler", "kurooka", "lamp", "lerner", "melcher", "senat", "williams", "justice", "davis",
})


def _name_has_plausible_component(name: str) -> bool:
    """Does *name* contain at least one word that looks like a real first or last name?

    This is the core structural check that separates real person names
    ("Mark Freeman", "Jennifer Duff") from section headers and agenda
    titles ("Study Session", "Admin Spaces", "Previous Studies").
    """
    if not name or not isinstance(name, str):
        return False
    words = name.lower().split()
    for w in words:
        if w in _KNOWN_FIRST_NAMES or w in _KNOWN_LAST_NAMES:
            return True
    return False


def _name_looks_like_a_person(name: str) -> bool:
    """Quick sanity check: does *name* look like a real person's name?

    Uses structural checks + a known-name dictionary to reject section
    headers, presentation titles, and other garbage that leaks into
    supervisor lists from minutes PDFs.
    """
    if not name or not isinstance(name, str):
        return False
    name = name.strip()
    if not name or len(name) < 2:
        return False

    # Core structural check: must contain a known-first or known-last name
    if not _name_has_plausible_component(name):
        return False

    words = name.split()

    # Reject if every word starts lowercase (text artifacts like "mayor giles conducted")
    if len(words) >= 2 and all(w[0].islower() for w in words):
        return False

    # Reject if all caps (section headers like "BUDGET OVERVIEW")
    if name.upper() == name and len(name) > 4:
        return False

    # Reject if too many words (> 3)
    if len(words) > 3:
        return False

    # Reject if too long (> 35 chars)
    if len(name) > 35:
        return False

    # Reject names containing numbers
    for ch in name:
        if ch.isdigit():
            return False

    return True


def _find_or_create_person(
    session: Session,
    name: str,
    normalized_name: str,
    *,
    log_prefix: str = "",
) -> tuple[Person, bool]:
    """Find an existing Person by fuzzy matching, or create a new one.

    Matching strategy (in order):
    1. Exact match on normalized_name.
    2. Substring match: any word from the candidate name appears as a word
       in an existing normalized_name (catches "Hartke" matching "kevin hartke").
    3. Reverse substring: any word from an existing name matches the candidate
       (catches partial full-name input matching an existing single-name record).

    Returns (Person, was_created) tuple.
    """
    norm = normalized_name.lower().strip()
    name_clean = name.strip()

    # 1. Exact match
    existing = session.execute(
        select(Person).where(Person.normalized_name == norm)
    ).scalar_one_or_none()
    if existing:
        existing.name = name_clean
        existing.updated_at = datetime.now(timezone.utc)
        return existing, False

    # 2. Substring match: name parts of candidate appear in existing records
    #    (e.g. "hartke" in "kevin hartke", "curley" in "kevin curley")
    #
    #    Safety: when both the candidate and the existing record have 2+ words,
    #    require at least 2 shared words to avoid merging different people who
    #    happen to share a surname (e.g. "Michael McGee" vs "Kate Brophy McGee"
    #    should NOT merge just because both contain "mcgee").
    candidate_words = set(norm.split())
    if candidate_words:
        # Build OR clause: for each word, check if any existing normalized_name
        # contains that word as a whole word
        word_conditions = []
        for word in candidate_words:
            if len(word) >= 3:  # ignore very short words
                word_conditions.append(Person.normalized_name.like(f"% {word}"))
                word_conditions.append(Person.normalized_name.like(f"{word} %"))
                word_conditions.append(Person.normalized_name == word)
        if word_conditions:
            matches = session.execute(
                select(Person).where(or_(*word_conditions))
            ).scalars().all()
            if matches:
                # Prefer full-name records (contain space) over single-name
                full = [m for m in matches if " " in (m.name or "")]
                best = full[0] if full else matches[0]
                # Count overlapping words to avoid surname-only merges
                existing_words = set(best.normalized_name.split())
                overlap = candidate_words & existing_words
                candidate_has_multiple = len(candidate_words) > 1
                existing_has_multiple = len(existing_words) > 1
                # Require at least 2 shared words when both sides have 2+ words
                if candidate_has_multiple and existing_has_multiple and len(overlap) < 2:
                    log.debug(
                        "%sSkipped fuzzy-match '%s' vs '%s' (only %d shared word(s): %s)",
                        log_prefix, norm, best.normalized_name, len(overlap), overlap,
                    )
                else:
                    # Don't overwrite a multi-word name with a single word
                    # (e.g. "Adams" should not replace "Jennifer Adams")
                    if len(candidate_words) >= len(existing_words):
                        best.name = name_clean
                    best.updated_at = datetime.now(timezone.utc)
                    log.info(
                        "%sFuzzy-matched '%s' → existing Person %d (%s)",
                        log_prefix, norm, best.id, best.normalized_name,
                    )
                    return best, False

    # 3. Create new Person record
    new_p = Person(name=name_clean, normalized_name=norm)
    session.add(new_p)
    session.flush()
    log.info(
        "%sCreated new Person %d: '%s' (norm=%s)",
        log_prefix, new_p.id, name_clean, norm,
    )
    return new_p, True



def create_or_get_meeting(session: Session, body: str, meeting_dict: dict) -> Meeting:
    """Get or create a meeting row, setting sync_status=pending for new rows.

    Jurisdiction ID is resolved from the public body table so that every
    meeting gets the correct jurisdiction regardless of sync code path.
    """
    meeting_id = meeting_dict.get("meeting_id", "")
    existing = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if existing:
        # If the resolved jurisdiction differs (e.g. public body was added
        # after the meeting was first synced), update it.
        resolved_jur = _resolve_jurisdiction_id(session, body)
        if resolved_jur and existing.jurisdiction_id != resolved_jur:
            existing.jurisdiction_id = resolved_jur
            existing.updated_at = datetime.now(timezone.utc)
        return existing
    jurisdiction_id = _resolve_jurisdiction_id(session, body)
    meeting = Meeting(
        body=body,
        meeting_id=meeting_id,
        meeting_date=meeting_dict.get("meeting_date", ""),
        meeting_type=meeting_dict.get("meeting_type", ""),
        meeting_title=meeting_dict.get("meeting_title", ""),
        meeting_title_raw=meeting_dict.get("meeting_title", ""),
        source_url=meeting_dict.get("source_url", ""),
        minutes_url=meeting_dict.get("minutes_url", None),
        sync_status="pending",
        jurisdiction_id=jurisdiction_id,
    )
    session.add(meeting)
    return meeting


# ── Error classification ──
# Tags let us distinguish transient errors (retry later) from code bugs (fix code)
# from data issues (fix config/data).  Prepended as [TAG] prefix on last_error.

def _classify_error(error_str: str) -> str:
    """Return error category tag: TRANSIENT, CODE, DATA, or UNKNOWN."""
    if not error_str:
        return "UNKNOWN"
    error_lower = error_str.lower()

    # Transient — infrastructure / networking issues, retryable
    transient_patterns = [
        r"5\d{2}",
        r"timeout", r"timed ?out",
        r"connection refused", r"connection reset",
        r"connectionerror", r"dns lookup",
        r"network is unreachable", r"econnrefused", r"econnreset",
        r"read timed out", r"operation timed out",
        r"ssl", r"certificate verify",
        r"ratelimit", r"rate.limit", r"429",
        r"received http 0",
        r"temporary failure", r"temporarily unavailable",
        r"server error", r"internal server error",
        r"gateway", r"bad gateway",
        r"service unavailable", r"service temporarily",
        r"too many requests",
        r"net::err_",
        r"websocket",
        r"playwright.*timeout",
    ]
    for pattern in transient_patterns:
        if re.search(pattern, error_lower, re.IGNORECASE):
            return "TRANSIENT"

    # Code bug — programming errors that need a code change
    code_patterns = [
        r"typeerror", r"attributeerror",
        r"importerror", r"modulenotfounderror",
        r"keyerror", r"indexerror",
        r"valueerror", r"zerodivisionerror",
        r"assertionerror", r"notimplementederror",
        r"syntaxerror", r"nameerror",
        r"recursionerror", r"runtimeerror",
        r"operator does not exist", r"type mismatch",
        r"cannot import", r"no module named",
        r"object.*has no attribute",
        r"unsupported operand",
        r"invalid literal for int",
        r"not subscriptable", r"not iterable",
        r"list index out of range",
        r"not enough values to unpack",
        r"undefinedfunction", r"undefinedcolumn",
        r"psycopg2", r"database error",
        r"dialect.*not supported",
        r"sql type.*not recognized",
    ]
    for pattern in code_patterns:
        if re.search(pattern, error_lower, re.IGNORECASE):
            return "CODE"

    # Data issue — configuration, data integrity, missing references
    data_patterns = [
        r"body not found", r"unknown body",
        r"jurisdiction.*not found", r"unknown jurisdiction",
        r"cannot determine body", r"no.*body.*field",
        r"not found in database", r"meeting .* not found",
        r"foreign key.*violation", r"duplicate key.*violation",
        r"unique constraint", r"null value.*violation",
        r"not-null constraint", r"invalid input value",
        r"relation .* not exist", r"column .* not exist",
        r"scalar.*returned more than one",
        r"integrity.*constraint",
        r"body.*empty", r"meeting.*empty",
        r"cannot.*resolve", r"unable to determine",
        r"missing.*flag", r"no.*source.*flag",
        r"could not determine", r"could not resolve",
    ]
    for pattern in data_patterns:
        if re.search(pattern, error_lower, re.IGNORECASE):
            return "DATA"

    return "UNKNOWN"


def record_ingest_failure(
    session: Session,
    error: str,
    *,
    source: str = "unknown",
    body: Optional[str] = None,
    meeting_id: Optional[str] = None,
    meeting_date: Optional[str] = None,
    context: Optional[str] = None,
) -> "IngestFailure":
    """Record an import-side failure that can't reach the meetings table."""
    category = _classify_error(error)
    record = IngestFailure(
        error_category=category,
        source=source,
        body=body,
        meeting_id=meeting_id,
        meeting_date=meeting_date,
        error=error[:2000],
        context=context[:2000] if context else None,
    )
    session.add(record)
    return record


def _tag_error(error: str, category: str) -> str:
    """Prepend category tag to error string if not already tagged."""
    if not error:
        return error
    tag = f"[{category}]"
    if error.startswith(tag):
        return error
    return f"{tag} {error}"


def update_sync_status(
    session: Session,
    body: str,
    meeting_id: str,
    status: str,
    *,
    item_count_expected: Optional[int] = None,
    item_count_actual: Optional[int] = None,
    supporting_doc_count: Optional[int] = None,
    items_extracted: Optional[bool] = None,
    supporting_docs_extracted: Optional[bool] = None,
    error: Optional[str] = None,
) -> Meeting:
    """Update sync tracking fields on a meeting row."""
    meeting = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if not meeting:
        raise ValueError(f"{body} meeting {meeting_id} not found")

    now = datetime.now(timezone.utc)
    meeting.sync_status = status
    meeting.last_attempted_at = now
    meeting.updated_at = now

    if status == "complete":
        meeting.last_synced_at = now
        meeting.retry_count = 0
        meeting.last_error = None
    elif status in ("manual_review", "no_agenda"):
        # These are classifications, not failures; don't increment retries
        meeting.retry_count = 0
        if error:
            meeting.last_error = _tag_error(error, _classify_error(error))
        if status == "no_agenda":
            meeting.last_synced_at = now
    else:
        meeting.retry_count = (meeting.retry_count or 0) + 1
        if error:
            meeting.last_error = _tag_error(error, _classify_error(error))

    if item_count_expected is not None:
        meeting.item_count_expected = item_count_expected
    if item_count_actual is not None:
        meeting.item_count_actual = item_count_actual
    if supporting_doc_count is not None:
        meeting.supporting_doc_count = supporting_doc_count
    if items_extracted is not None:
        meeting.items_extracted = items_extracted
    if supporting_docs_extracted is not None:
        meeting.supporting_docs_extracted = supporting_docs_extracted

    return meeting

def upsert_meeting(session: Session, body: str, meeting: Meeting) -> Meeting:
    """Insert or update a meeting by (body, meeting_id)."""
    existing = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting.meeting_id,
        )
    ).scalar_one_or_none()
    if existing:
        existing.meeting_date = meeting.meeting_date
        existing.meeting_type = meeting.meeting_type
        existing.meeting_title = meeting.meeting_title
        existing.source_url = meeting.source_url
        existing.updated_at = datetime.now(timezone.utc)
        return existing
    session.add(meeting)
    return meeting

def persist_meeting(
    session: Session,
    body: str,
    meeting_id: str,
    agenda_item_dicts: list[dict],
    supporting_doc_dicts: Optional[list[dict]] = None,
) -> int:
    """Transactionally persist a meeting's agenda items and supporting docs.

    WARNING: This replaces ALL existing agenda_items and supporting_documents
    for the given meeting_id within the body scope. Callers should only invoke
    this after successfully extracting data into memory and validating.

    Steps:
    1. Delete existing agenda_items for this (body, meeting_id).
    2. Delete existing supporting_documents for this (body, meeting_id).
    3. Insert new agenda_item rows and supporting doc rows.
    4. Verify the inserted count matches expected.
    5. Commit only if validation passes; rollback on failure.

    Returns the number of agenda items persisted.
    Raises ValueError if the count doesn't match.
    """
    expected_count = len(agenda_item_dicts)
    inserted_item_count = 0
    inserted_doc_count = 0

    # Look up the meeting's PK for meeting_db_id references
    meeting_row = session.execute(
        select(Meeting.id)
        .where(Meeting.body == body, Meeting.meeting_id == meeting_id)
    ).scalar_one_or_none()
    if meeting_row is None:
        raise ValueError(
            f"Meeting not found: body={body} meeting_id={meeting_id}. "
            f"Call upsert_meeting() first."
        )
    meeting_db_id_val = meeting_row

    # Delete existing rows for this meeting within body scope
    session.execute(
        AgendaItem.__table__.delete().where(
            AgendaItem.body == body,
            AgendaItem.meeting_id == meeting_id,
        )
    )
    session.execute(
        SupportingDocument.__table__.delete().where(
            SupportingDocument.body == body,
            SupportingDocument.meeting_id == meeting_id,
        )
    )

    seen_item_ids: set[str] = set()
    for sort_idx, item_dict in enumerate(agenda_item_dicts):
        aii = item_dict.get("agenda_item_id", "")
        if aii:
            if aii in seen_item_ids:
                log.warning(
                    "duplicate agenda_item_id %s for meeting %s — skipping",
                    aii, meeting_id,
                )
                continue
            seen_item_ids.add(aii)
        item = AgendaItem(
            body=body,
            meeting_id=meeting_id,
            meeting_db_id=meeting_db_id_val,
            agenda_item_number=str(item_dict.get("agenda_item_number", "0") or "0"),
            agenda_item_id=aii,
            agenda_item_title=item_dict.get("agenda_item_title", ""),
            agenda_item_text=item_dict.get("agenda_item_text", ""),
            agenda_item_url=item_dict.get("agenda_item_url", ""),
            vote_or_action=item_dict.get("vote_or_action", ""),
            source_body=item_dict.get("source_body") or item_dict.get("body") or body,
            source_url=item_dict.get("source_url", ""),
            c_number=item_dict.get("c_number", ""),
            c_number_base=item_dict.get("c_number_base", ""),
            c_number_revision=item_dict.get("c_number_revision", None),
            case_number=item_dict.get("case_number", ""),
            item_type=item_dict.get("item_type", ""),
            section_level=item_dict.get("section_level"),
            sort_order=sort_idx,
            agenda_category=item_dict.get("agenda_category", ""),
        )
        session.add(item)
        inserted_item_count += 1

    # Flush agenda items first to get IDs before handling supporting docs
    session.flush()

    if supporting_doc_dicts:
        for doc_dict in supporting_doc_dicts:
            # Check if this document already exists across any meeting.
            # The unique constraint (agenda_item_id, document_url) doesn't
            # include meeting_id, so we may conflict with docs from other
            # meetings.
            existing = session.execute(
                select(SupportingDocument.id).where(
                    SupportingDocument.agenda_item_id == str(doc_dict.get("agenda_item_id", "0")),
                    SupportingDocument.document_url == doc_dict.get("document_url", ""),
                )
            ).scalar_one_or_none()
            if existing:
                log.warning(
                    "Skipping duplicate supporting doc url=%s (title=%s)",
                    doc_dict.get("document_url", ""),
                    doc_dict.get("document_title", ""),
                )
                continue
            doc = SupportingDocument(
                body=body,
                agenda_item_id=str(doc_dict.get("agenda_item_id", "0")),
                meeting_id=meeting_id,
                meeting_db_id=meeting_db_id_val,
                agenda_item_number=str(doc_dict.get("agenda_item_number", "0") or "0"),
                c_number=doc_dict.get("c_number"),
                c_number_base=doc_dict.get("c_number_base"),
                c_number_revision=doc_dict.get("c_number_revision"),
                document_title=doc_dict.get("document_title", ""),
                document_url=doc_dict.get("document_url", ""),
                document_type=doc_dict.get("document_type"),
                file_name=doc_dict.get("file_name"),
                file_extension=(
                    doc_dict.get("file_extension")
                    or _infer_extension(doc_dict.get("document_url", ""))
                ),
                scraped_at=datetime.now(timezone.utc),
            )
            session.add(doc)
            inserted_doc_count += 1

    # Delete existing CaseEvent records for this meeting (idempotency)
    session.execute(
        CaseEvent.__table__.delete().where(
            CaseEvent.meeting_db_id == meeting_db_id_val,
        )
    )
    session.flush()

    # Create case and event records for items with case_number
    meeting_date = agenda_item_dicts[0].get("meeting_date", "") if agenda_item_dicts else ""
    for item_dict in agenda_item_dicts:
        # Determine source from source_body
        source_body = (item_dict.get("source_body") or "").strip()
        item_source = "PZ" if "planning" in source_body.lower() else "BOS"
        _upsert_case_and_event(
            session, meeting_db_id_val, meeting_id, meeting_date, item_dict, body, source=item_source
        )

    session.commit()
    return inserted_item_count


def _infer_extension(url: str) -> str | None:
    """Infer a file extension from the document URL if possible."""
    from urllib.parse import urlparse, unquote
    path = unquote(urlparse(url).path).rstrip("/")
    # Get the last path segment and look for an extension
    last_segment = path.rsplit("/", 1)[-1] if "/" in path else path
    if "." in last_segment:
        ext = last_segment.rsplit(".", 1)[-1].lower()
        known = {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "png", "jpg", "jpeg", "gif"}
        if ext in known:
            return f".{ext}"
    return None


def _upsert_case_and_event(
    session: Session,
    meeting_db_id: int,
    meeting_id: str,
    meeting_date: str,
    item_dict: dict,
    body: str,
    source: str = "BOS",
) -> Optional[Case]:
    """Upsert a Case record and create a CaseEvent for an agenda item.

    If the item has no case_number, returns None.
    """
    case_number = (item_dict.get("case_number") or "").strip()
    if not case_number:
        return None

    case_number_upper = case_number.upper()

    # Determine case_type
    case_type = ""
    for prefix in ["CPA", "Z", "GPA"]:
        if case_number_upper.startswith(prefix):
            case_type = prefix
            break

    # Create or get case
    case = session.execute(
        select(Case).where(Case.case_number == case_number_upper)
    ).scalar_one_or_none()
    if not case:
        case = Case(
            case_number=case_number_upper,
            case_type=case_type,
            normalized_case_number=re.sub(r"[^A-Z0-9]", " ", case_number_upper).strip(),
            description=(item_dict.get("agenda_item_title", "") or "")[:500],
        )
        session.add(case)
        session.flush()

    # Look up the DB agenda_item by meeting_id + agenda_item_id to get its ID
    db_item = session.execute(
        select(AgendaItem).where(
            AgendaItem.body == body,
            AgendaItem.meeting_id == meeting_id,
            AgendaItem.agenda_item_id == item_dict.get("agenda_item_id", ""),
        )
    ).scalar_one_or_none()
    agenda_item_db_id = db_item.id if db_item else None

    # Create event
    event = CaseEvent(
        case_id=case.id,
        meeting_id=meeting_id,
        meeting_db_id=meeting_db_id,
        body=body,
        agenda_item_id=agenda_item_db_id,
        source=source,
        event_type="agenda" if source == "BOS" else "hearing",
        event_date=meeting_date,
        notes=None,
    )
    session.add(event)
    return case

def _meeting_date_is_today_or_future(date_str: Optional[str]) -> bool:
    """True if an ISO-format meeting date is today or in the future.

    Used to decide whether a 0-item meeting is merely waiting for its agenda
    to be published (pending) or is genuinely done (complete).
    """
    if not date_str:
        return False
    s = str(date_str).strip()
    today = datetime.now(timezone.utc).date()
    try:
        return datetime.fromisoformat(s).date() >= today
    except (ValueError, TypeError):
        # Non-ISO/parseable date — fall back to lexical compare, else conservative
        return s >= today.isoformat()


def replace_meeting_data_safe(
    session: Session,
    body: str,
    meeting_id: str,
    meeting_dict: dict,
    agenda_item_dicts: list[dict],
    supporting_doc_dicts: Optional[list[dict]] = None,
) -> int:
    """Safely replace meeting data within a transaction.

    This creates the meeting row (body-scoped) if needed, replaces items/docs,
    and updates sync status to 'complete' on success.
    Returns the number of agenda items persisted.

    On failure, rolls back and raises.
    """
    try:
        # Classify lifecycle status for each agenda item
        if agenda_item_dicts:
            try:
                from scraper.common.lifecycle import extract_lifecycle_from_items
                extract_lifecycle_from_items(agenda_item_dicts)
            except Exception:
                pass  # Non-fatal — continue without lifecycle classification

        # Ensure meeting row exists (creates if new)
        meeting = create_or_get_meeting(session, body, meeting_dict)

        # Update meeting metadata from meeting_dict (preserves existing
        # values when meeting_dict has empty fields, e.g. --meeting-id path)
        if meeting_dict.get("meeting_date"):
            meeting.meeting_date = meeting_dict["meeting_date"]
        if meeting_dict.get("meeting_type"):
            meeting.meeting_type = meeting_dict["meeting_type"]
        if meeting_dict.get("meeting_title"):
            meeting.meeting_title = meeting_dict["meeting_title"]
        if meeting_dict.get("source_url"):
            meeting.source_url = meeting_dict["source_url"]
        if meeting_dict.get("minutes_url"):
            meeting.minutes_url = meeting_dict["minutes_url"]

        # Store raw title and normalize fields
        if meeting_dict.get("meeting_title"):
            meeting.meeting_title_raw = meeting_dict["meeting_title"]

        if meeting_dict.get("meeting_type") or meeting_dict.get("meeting_title"):
            raw_type = meeting_dict.get("meeting_type", "")
            raw_title = meeting_dict.get("meeting_title", "")

            meeting.meeting_type = normalize_meeting_type(raw_type, raw_title)
            meeting.meeting_context = extract_meeting_context(raw_title, raw_type)
            meeting.meeting_body = extract_meeting_body(raw_title)
            meeting.display_name = build_meeting_display_name(
                meeting.meeting_type,
                meeting.meeting_date,
                meeting.meeting_context,
            )

        persisted = persist_meeting(
            session,
            body,
            meeting_id,
            agenda_item_dicts,
            supporting_doc_dicts,
        )

        doc_count = len(supporting_doc_dicts) if supporting_doc_dicts else 0

        # Brief 006: a future-dated meeting with 0 agenda items isn't
        # "complete" — its agenda simply hasn't been published yet (rows are
        # created weeks early from the calendar). Mark it pending so the
        # normal daily sync path re-fetches it once the agenda drops. Past
        # meetings with 0 items keep "complete" (behavior unchanged).
        if not agenda_item_dicts and _meeting_date_is_today_or_future(meeting.meeting_date):
            update_sync_status(
                session,
                body,
                meeting_id,
                "pending",
                item_count_expected=meeting.item_count_expected or 0,
                item_count_actual=0,
                supporting_doc_count=doc_count,
                items_extracted=False,
                supporting_docs_extracted=bool(supporting_doc_dicts),
                error="Agenda not yet published",
            )
        else:
            update_sync_status(
                session,
                body,
                meeting_id,
                "complete",
                item_count_expected=meeting.item_count_expected or len(agenda_item_dicts),
                item_count_actual=len(agenda_item_dicts),
                supporting_doc_count=doc_count,
                items_extracted=True,
                supporting_docs_extracted=bool(supporting_doc_dicts),
                error=None,
            )
        session.commit()
        return persisted

    except Exception:
        session.rollback()
        raise

def _detect_vote_attributes(aiv_list: list[AgendaItemVote]) -> None:
    """Detect split_vote, unanimous, and majority_position on each AgendaItemVote.

    This is called from persist_votes() after all votes are committed.
    It examines supervisor_votes / member_votes in the DB to determine
    vote-level attributes.
    """
    if not aiv_list:
        return
    session = Session.object_session(aiv_list[0])
    if not session or aiv_list[0] is None:
        return
    for aiv in aiv_list:
        # Gather supervisor votes for this aiv
        sv_rows = session.execute(
            select(MemberVote).where(
                MemberVote.agenda_item_vote_id == aiv.id
            )
        ).scalars().all()
        if not sv_rows:
            continue

        # Determine the set of distinct substantive votes (yes/no)
        # excluding abstain/recused/absent/not_voting
        substantive = [sv.vote for sv in sv_rows if sv.vote in ("yes", "no")]
        if not substantive:
            # All abstentions/recusals — not split, not unanimous
            aiv.unanimous = None
            aiv.is_split_vote = False
            aiv.majority_position = "unknown"
            continue

        vote_set = set(substantive)
        aiv.is_split_vote = len(vote_set) > 1
        aiv.unanimous = len(vote_set) == 1

        # Determine majority position
        yes_count = sum(1 for v in substantive if v == "yes")
        no_count = sum(1 for v in substantive if v == "no")
        if yes_count > no_count:
            aiv.majority_position = "yes"
        elif no_count > yes_count:
            aiv.majority_position = "no"
        else:
            aiv.majority_position = "tie"

        # Flag dissenting supervisor votes
        if aiv.majority_position and aiv.majority_position not in ("tie", "unknown"):
            for sv in sv_rows:
                if (
                    sv.vote in ("yes", "no")
                    and sv.vote != aiv.majority_position
                ):
                    sv.is_dissent = True

def _ensure_membership(
    session: Session,
    person_id: int,
    body: str,
    meeting_date: Optional[date] = None,
) -> Optional[BodyMembership]:
    """Ensure a BodyMembership row exists for this person + body.

    Creates a membership using meeting_date (or today) as term_start if
    one doesn't already exist.  Returns the existing or new membership.
    """
    # Resolve public_body_id from body code
    pb = session.execute(
        select(PublicBody).where(PublicBody.body_code == body)
    ).scalar_one_or_none()
    if pb is None:
        return None

    # Check existing membership
    existing = session.execute(
        select(BodyMembership)
        .where(BodyMembership.person_id == person_id)
        .where(BodyMembership.public_body_id == pb.id)
        .order_by(BodyMembership.term_start.desc())
        .limit(1)
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    # Look up person for role/title info
    person = session.execute(
        select(Person).where(Person.id == person_id)
    ).scalar_one_or_none()
    if person is None:
        return None

    term_start = meeting_date or date.today()
    membership = BodyMembership(
        person_id=person_id,
        public_body_id=pb.id,
        role=getattr(person, 'title', None) or None,
        term_start=term_start,
        selection_method="elected" if body == "bos" else "appointed",
    )
    session.add(membership)
    session.flush()
    return membership


def _normalize_motion_result(raw: Optional[str]) -> str:
    """Normalize motion_result to a standard short form.

    Extracts the first word and maps common action verbs so that all
    parsers (summary_dom, votes, tempe_summary) produce consistent
    badge-friendly values.  The full raw text is preserved in
    ``agenda_item_votes.vote_text`` and ``agenda_items.agenda_item_text``,
    so no detail is lost.

    Verbs not in the map pass through unchanged.
    """
    if not raw:
        return "approved"
    raw = raw.strip()
    first = raw.split(None, 1)[0].lower() if raw else ""
    mapped = {
        "approve": "approved",
        "appoint": "approved",
        "concur": "approved",
        "deny": "denied",
        "denied": "denied",
        "continue": "continued",
        "continued": "continued",
        "withdraw": "withdrawn",
        "accept": "accepted",
        "reject": "rejected",
    }.get(first)
    return mapped if mapped is not None else raw


def persist_votes(
    session: Session,
    body: str,
    meeting_id: str,
    supervisors: list[dict],
    votes: list[dict],
) -> int:
    """Save vote records for a meeting.

    Inserts agenda_item_votes rows linked to the meeting, then stores
    individual supervisor votes in member_votes or supervisor_votes.
    Returns the number of vote records saved.
    """
    # Suppress identity-map warning from SQLite reusing PK IDs after DELETE
    import warnings
    from sqlalchemy import exc as sa_exc
    warnings.filterwarnings("ignore", category=sa_exc.SAWarning, module="db")
    """Persist supervisor info and vote results for a meeting.

    Creates meeting_members (attendance) records and member_votes (vote)
    records for a meeting.

    1. Upsert supervisor records (by normalized_name).
    2. Update or insert meeting_members by their stable meeting/member key.
    3. Replace agenda_item_votes and member_votes for this meeting_id.
    4. Commit transactionally.

    Returns the number of vote records persisted.
    """
    # Look up meeting date for membership creation
    meeting_date = None
    meeting_row = session.execute(
        select(Meeting).where(
            Meeting.body == body,
            Meeting.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()
    if meeting_row and meeting_row.meeting_date:
        try:
            meeting_date = date.fromisoformat(meeting_row.meeting_date)
        except (ValueError, TypeError):
            pass
    meeting_db_id_val = meeting_row.id if meeting_row else 0

    # 1. Upsert supervisors (with name validation)
    supervisor_map: dict[str, int] = {}
    for sup in supervisors:
        norm = sup.get("normalized_name", sup.get("name", "").lower().strip())
        if not norm:
            continue
        sup_name = sup.get("name", "")

        # Safety check: reject names that don't look like a person
        if not _name_looks_like_a_person(sup_name):
            log.warning("Rejecting non-person name in persist_votes: %r", sup_name)
            continue

        with session.no_autoflush:
            existing = session.execute(
                select(Supervisor).where(Supervisor.normalized_name == norm)
            ).scalar_one_or_none()
        if existing:
            existing.name = sup_name
            existing.updated_at = datetime.now(timezone.utc)
            # Ensure BodyMembership exists for this person + body
            _ensure_membership(session, existing.id, body, meeting_date)
            supervisor_map[norm] = existing.id
        else:
            person, _ = _find_or_create_person(
                session, sup_name, norm,
                log_prefix="persist_votes[",
            )
            supervisor_map[norm] = person.id

            # If Tempe council member, pass role info to membership
            role = None
            if "tempe" in body:
                titler_map = {"woods": "Mayor", "garlid": "Vice Mayor"}
                role = titler_map.get(norm, "Councilmember")

            # Ensure BodyMembership exists for new person + body
            membership = _ensure_membership(session, person.id, body, meeting_date)
            if membership and role:
                membership.role = role

    # 2. Load vote rows before replacing them. Use no_autoflush to prevent
    # stale pending objects from a previous
    # failed call from being re-inserted by query-invoked autoflush, which
    # would collide with the fresh inserts below.
    with session.no_autoflush:
        existing_aiv_rows = session.execute(
            select(AgendaItemVote).where(
                AgendaItemVote.body == body,
                AgendaItemVote.meeting_id == meeting_id,
            )
        ).scalars().all()
    existing_aiv_ids = [r.id for r in existing_aiv_rows]
    if existing_aiv_ids:
        session.execute(
            MemberVote.__table__.delete().where(
                MemberVote.agenda_item_vote_id.in_(existing_aiv_ids)
            )
        )
    session.execute(
        AgendaItemVote.__table__.delete().where(
            AgendaItemVote.body == body,
            AgendaItemVote.meeting_id == meeting_id,
        )
    )
    session.flush()
    # Skip expire_all — it causes identity map conflicts when called
    # from within an active transaction (e.g. PZ sync loop).
    vote_count = 0

    # 3. Reconcile current attendance without changing existing row IDs.
    attendance_records: list[AttendanceRecord] = []
    for sup in supervisors:
        norm = sup.get("normalized_name", sup.get("name", "").lower().strip())
        sup_id = supervisor_map.get(norm)
        if sup_id is None:
            continue
        attendance_records.append(AttendanceRecord(
            member_id=sup_id,
            role=sup.get("role"),
            present=sup.get("present", True),
        ))
    reconcile_meeting_members(
        session,
        body=body,
        meeting_id=meeting_id,
        meeting_db_id=meeting_db_id_val,
        attendance=attendance_records,
    )

    # 4. Insert vote records
    seen_item_db_ids: set[int] = set()
    for vote in votes:
        item_number = str(vote.get("agenda_item_number", "0"))
        # Look up the actual AgendaItem database row for FK reference
        with session.no_autoflush:
            db_agenda_item = session.execute(
                select(AgendaItem).where(
                    AgendaItem.body == body,
                    AgendaItem.meeting_id == meeting_id,
                    AgendaItem.agenda_item_number == item_number,
                )
            ).scalar_one_or_none()
        if db_agenda_item:
            db_agenda_item_id = db_agenda_item.id
        else:
            # No matching agenda item — use a synthetic ID to avoid UNIQUE constraint collisions
            db_agenda_item_id = -1 * hash(f"{body}:{meeting_id}:{item_number}") % (2**31)
        if db_agenda_item_id in seen_item_db_ids:
            # Duplicate item number (e.g., agenda has two #2 entries for
            # different sub-items). Skip rather than violate the unique
            # constraint on agenda_item_id.
            continue
        seen_item_db_ids.add(db_agenda_item_id)
        aiv = AgendaItemVote(
            body=body,
            agenda_item_id=db_agenda_item_id,
            meeting_id=meeting_id,
            meeting_db_id=meeting_db_id_val,
            agenda_item_number=item_number,
            c_number=vote.get("c_number"),
            c_number_base=vote.get("c_number_base"),
            motion_result=_normalize_motion_result(vote.get("motion_result")),
            vote_text=vote.get("vote_text"),
        )
        session.add(aiv)
        session.flush()

        # Insert individual supervisor votes
        for sv in vote.get("member_votes", vote.get("supervisor_votes", [])):
            name = sv.get("name", "")
            norm_name = name.lower().strip()
            sup_id = supervisor_map.get(norm_name)
            if sup_id is None:
                # Try to find in DB without upserting
                with session.no_autoflush:
                    existing_sup = session.execute(
                        select(Supervisor).where(Supervisor.normalized_name == norm_name)
                    ).scalar_one_or_none()
                if existing_sup:
                    sup_id = existing_sup.id
                    supervisor_map[norm_name] = sup_id
                else:
                    # Guard against text fragments parsed as names from minutes PDFs
                    if not _name_looks_like_a_person(name):
                        log.warning(
                            "persist_votes[sv] skipping non-person name: %r", name
                        )
                        continue
                    # Create a new supervisor record for this name (with fuzzy dedup)
                    person, _ = _find_or_create_person(
                        session, name, norm_name,
                        log_prefix="persist_votes[sv]",
                    )
                    sup_id = person.id
                    supervisor_map[norm_name] = sup_id

                    # Ensure BodyMembership for this new person
                    _ensure_membership(session, person.id, body, meeting_date)

            # Normalize vote value: aye→yes, nay→no; preserve original in raw_vote_text
            raw_vote = sv.get("vote", "unknown")
            norm_vote = raw_vote.lower().strip()
            if norm_vote in ("aye",):
                norm_vote = "yes"
            elif norm_vote in ("nay",):
                norm_vote = "no"

            sv_rec = MemberVote(
                body=body,
                agenda_item_vote_id=aiv.id,
                member_id=sup_id,
                vote=norm_vote,
                raw_vote_text=sv.get("raw_vote_text") or raw_vote,
            )
            session.add(sv_rec)

        vote_count += 1

    # 5. Flush to get AIV IDs, then detect vote attributes
    session.flush()
    # Reload AIVs to get their IDs for attribute detection
    aiv_rows = [r for r in session.execute(
        select(AgendaItemVote).where(
            AgendaItemVote.body == body,
            AgendaItemVote.meeting_id == meeting_id,
        )
    ).scalars().all() if r is not None]
    _detect_vote_attributes(aiv_rows)

    # 6. Infer absences and abstentions from missing votes
    #    After all explicit votes are stored, check each supervisor present at
    #    this meeting against the total number of AIVs.
    #    -  0 votes for this meeting  → member was absent (update MeetingMember.present)
    #    -  >0 but < total AIVs       → supervisor abstained on the missing items
    #
    #    Only infer on items where a vote was actually taken.  Skip:
    #    -  Items with motion_result="withdrawn" (no vote was taken)
    #    -  Items whose vote_text lacks "Ayes:" or "Nay:" (informational
    #       presentations, proclamations, etc. where no roll call occurred)
    if aiv_rows:
        votable_aivs = [
            aiv for aiv in aiv_rows
            if aiv.motion_result != "withdrawn"
            and aiv.vote_text
            and ("Ayes:" in aiv.vote_text or "Nay:" in aiv.vote_text)
        ]
        aiv_ids = [aiv.id for aiv in votable_aivs]
        aiv_count = len(aiv_ids)
        # Load all MeetingMember rows for this meeting
        mm_rows = session.execute(
            select(MeetingMember).where(
                MeetingMember.body == body,
                MeetingMember.meeting_id == meeting_id,
            )
        ).scalars().all()
        for mm in mm_rows:
            sv_count = session.execute(
                select(func.count()).select_from(MemberVote).where(
                    MemberVote.member_id == mm.member_id,
                    MemberVote.agenda_item_vote_id.in_(aiv_ids),
                )
            ).scalar()
            if sv_count == 0:
                # Member was listed as present but never voted — mark absent
                mm.present = False
            elif sv_count < aiv_count:
                # Member voted on some items but not all — abstain on the rest
                existing_aiv_ids = set(
                    row[0] for row in session.execute(
                        select(MemberVote.agenda_item_vote_id).where(
                            MemberVote.member_id == mm.member_id,
                            MemberVote.agenda_item_vote_id.in_(aiv_ids),
                        )
                    ).all()
                )
                for aiv_id in aiv_ids:
                    if aiv_id not in existing_aiv_ids:
                        session.add(MemberVote(
                            body=body,
                            agenda_item_vote_id=aiv_id,
                            member_id=mm.member_id,
                            vote="abstain",
                            raw_vote_text="inferred abstention — no vote recorded on this item",
                        ))
                        vote_count += 1

    # 7. Commit
    session.commit()
    return vote_count

def infer_absence_for_meeting(
    session: Session,
    body: str,
    meeting_id: str,
    known_member_ids: list[int],
    voting_member_ids: list[int],
) -> list["MeetingAttendance"]:
    """Infer absence for members who did not vote while others voted.

    Args:
        session: DB session
        body: Body scope
        meeting_id: Meeting identifier
        known_member_ids: All member IDs known to be active for this body
        voting_member_ids: Member IDs who actually voted on any item

    Returns:
        List of new MeetingAttendance records (not yet committed)
    """
    inferred: list[MeetingAttendance] = []
    voting_set = set(voting_member_ids)
    for mid in known_member_ids:
        if mid not in voting_set:
            att = MeetingAttendance(
                body=body,
                meeting_id=meeting_id,
                member_id=mid,
                attendance_status="inferred_absent",
                source_text="Member did not vote while others voted on agenda items",
                inference_method="missing_vote_when_others_voted",
            )
            session.add(att)
            inferred.append(att)
    return inferred


def persist_pz_votes(
    session: Session,
    meeting_id: str,
    votes: list[dict],
    absent_commissioner_names: Optional[list[str]] = None,
) -> int:
    """Persist PZ commission votes extracted from meeting minutes.

    1. Match each vote to the corresponding agenda_item_vote by case number.
    2. Create/update AgendaItemVote records with motion results.
    3. Upsert Person records for commissioners.
    4. Create MemberVote records for each commissioner's vote.
    5. Create "absent" MemberVote records for absent commissioners.

    Args:
        session: DB session
        meeting_id: The PZ meeting identifier
        votes: List of vote dicts from pz_minutes.parse_minutes_votes()
        absent_commissioner_names: List of names from MEMBERS ABSENT section

    Returns:
        Number of vote records persisted.
    """
    count = 0
    body = "pz"

    # Get existing agenda items for this meeting, keyed by case_number
    items = session.execute(
        select(AgendaItem).where(
            AgendaItem.body == body,
            AgendaItem.meeting_id == meeting_id,
        )
    ).scalars().all()

    # Build case_number -> AgendaItem lookup
    case_item_map: dict[str, AgendaItem] = {}
    for item in items:
        cn = (item.case_number or "").strip().upper()
        if cn:
            case_item_map[cn] = item

    for vote in votes:
        case_number = (vote.get("case_number") or "").upper().strip()
        if not case_number:
            log.warning("PZ vote missing case number, skipping")
            continue

        item = case_item_map.get(case_number)
        if not item:
            log.warning("No agenda item found for case %s in meeting %s", case_number, meeting_id)
            continue

        # Create or find AgendaItemVote
        aiv = session.execute(
            select(AgendaItemVote).where(
                AgendaItemVote.body == body,
                AgendaItemVote.meeting_id == meeting_id,
                AgendaItemVote.agenda_item_number == item.agenda_item_number,
            )
        ).scalar_one_or_none()

        motion_result = _normalize_motion_result(vote.get("motion_result"))
        tally_yes = vote.get("tally_yes", 0)
        tally_no = vote.get("tally_no", 0)
        is_split = tally_no > 0

        if aiv:
            aiv.motion_result = motion_result
            aiv.is_split_vote = is_split
            aiv.unanimous = not is_split
        else:
            # Create new agenda item vote
            aiv = AgendaItemVote(
                body=body,
                agenda_item_id=item.id,
                meeting_id=meeting_id,
                agenda_item_number=item.agenda_item_number,
                c_number=case_number,
                c_number_base=case_number,
                motion_result=motion_result,
                vote_text=f"{tally_yes}-{tally_no}",
                is_split_vote=is_split,
                unanimous=not is_split,
                majority_position="yes" if tally_yes > tally_no else "no",
            )
            session.add(aiv)
            session.flush()

        # Upsert commissioners as Person records
        # Build name -> person_id map from both ayes and nays lists
        commissioner_names: set[str] = set()
        for name in vote.get("ayes", []):
            commissioner_names.add(name.strip())
        for name in vote.get("nays", []):
            commissioner_names.add(name.strip())

        name_to_id: dict[str, int] = {}
        for name in commissioner_names:
            norm = name.lower().strip().rstrip(".")
            person, _ = _find_or_create_person(
                session, name.strip(), norm,
                log_prefix="persist_pz_votes[",
            )
            name_to_id[name] = person.id

        # Delete existing member votes for this AIV
        session.execute(
            MemberVote.__table__.delete().where(
                MemberVote.agenda_item_vote_id == aiv.id,
            )
        )

        # Insert Ayes
        for name in vote.get("ayes", []):
            name = name.strip()
            pid = name_to_id.get(name)
            if not pid:
                continue
            session.add(MemberVote(
                body=body,
                agenda_item_vote_id=aiv.id,
                member_id=pid,
                vote="yes",
                is_dissent=False,
            ))
            count += 1

        # Insert Nays
        for name in vote.get("nays", []):
            name = name.strip()
            pid = name_to_id.get(name)
            if not pid:
                continue
            session.add(MemberVote(
                body=body,
                agenda_item_vote_id=aiv.id,
                member_id=pid,
                vote="no",
                is_dissent=True,
            ))
            count += 1

    # ─── Absent commissioners ───
    if absent_commissioner_names:
        for name in absent_commissioner_names:
            norm = name.lower().strip().rstrip(".")
            person, _ = _find_or_create_person(
                session, name.strip(), norm,
                log_prefix="persist_pz_votes[absent]",
            )
            pid = person.id
            aivs = session.execute(
                select(AgendaItemVote).where(
                    AgendaItemVote.body == body,
                    AgendaItemVote.meeting_id == meeting_id,
                )
            ).scalars().all()
            for aiv in aivs:
                existing_mv = session.execute(
                    select(MemberVote).where(
                        MemberVote.agenda_item_vote_id == aiv.id,
                        MemberVote.member_id == pid,
                    )
                ).scalar_one_or_none()
                if existing_mv:
                    continue
                session.add(MemberVote(
                    body=body,
                    agenda_item_vote_id=aiv.id,
                    member_id=pid,
                    vote="absent",
                    is_dissent=False,
                ))
                count += 1

    session.commit()
    return count
