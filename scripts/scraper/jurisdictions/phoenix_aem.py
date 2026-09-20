"""
City of Phoenix AEM meeting extraction for boards/commissions not on Legistar.

Phoenix uses Adobe Experience Manager (AEM) for public meeting notices and
results for all boards, commissions, and committees. The Legistar system only
covers City Council meetings and subcommittees. This scraper fills the gap.

Two JSON endpoints:

  Notices (upcoming meetings):
    https://www.phoenix.gov/.../public_meeting_table.results.json

  Results (past meeting outcomes):
    https://www.phoenix.gov/.../public_meeting_table.results.json

API features:
  - Search by title: ?q=Planning+Commission
  - Pagination: &offset=10 (10 per page default)
  - Each result has: title, path (DAM path), url (download URL),
    properties.metadata/meetingTime (ISO datetime),
    properties.metadata/meetingType ("Notice" or "Result")
  - PDFs at url are publicly downloadable from AEM DAM
"""

from __future__ import annotations
import io
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional

from pypdf import PdfReader

log = logging.getLogger(__name__)

# ── Constants ──

NOTICES_BASE = (
    "https://www.phoenix.gov/administration/departments/cityclerk/"
    "programs-services/other-public-meetings/notices/"
    "_jcr_content/root/container/container-nav/container-full-width/"
    "container-content/public_meeting_table.results.json"
)

RESULTS_BASE = (
    "https://www.phoenix.gov/administration/departments/cityclerk/"
    "programs-services/other-public-meetings/results/"
    "_jcr_content/root/container/container-nav/container-full-width/"
    "container-content/public_meeting_table.results.json"
)

DAM_BASE = "https://www.phoenix.gov"

SOURCE_SYSTEM = "phoenix-aem"
SOURCE_INSTANCE_URL = "https://www.phoenix.gov"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# Per-page default from AEM
PAGE_SIZE = 10

# ── Body name → (slug, body_code) mapping ──
# Priority-ordered: first match wins.
# Village Planning Committees are named individually in AEM but share one slug.

BODY_MAP: list[tuple[re.Pattern, str, str]] = [
    # Village Planning Committees → shared slug
    (re.compile(r"desert\s+view\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"central\s+city\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"encanto\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"paradise\s+valley\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"deer\s+valley\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"camelback\s+east\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"laveen\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"rio\s+vista\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    # ── Remaining named VPCs (15 total villages in Phoenix) ──
    (re.compile(r"ahwatukee\s+foothills\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"alhambra\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"estrella\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"maryvale\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"north\s+gateway\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"north\s+mountain\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    (re.compile(r"south\s+mountain\s+village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),
    # Catch-all: any remaining "Village Planning Committee" -> phoenix-village-planning
    (re.compile(r"village\s+planning\s+committee", re.I),
     "phoenix-village-planning", "phoenix-village-planning"),

    # Planning Commission
    (re.compile(r"planning\s+commission", re.I),
     "phoenix-planning-commission", "phoenix-pc"),

    # Board of Adjustment / Technical Appeal
    (re.compile(r"(city\s+manager'?s?\s+representative\s+hearing|"
                r"technical\s+appeal|zoning\s+adjustment|"
                r"board\s+of\s+adjustment)", re.I),
     "phoenix-board-of-adjustment", "phoenix-boa"),

    # City Council - already handled by Legistar, but may appear in AEM too
    (re.compile(r"city\s+council\s+(formal|policy|special|work\s+study)", re.I),
     "phoenix-city-council", "phoenix-cc"),
    (re.compile(r"^city\s+council\s+meeting", re.I),
     "phoenix-city-council", "phoenix-cc"),

    # Historic Preservation Commission
    (re.compile(r"historic\s+preservation", re.I),
     "phoenix-historic-preservation", "phoenix-historic-preservation"),

    # Human Services Commission
    (re.compile(r"human\s+services\s+commission", re.I),
     "phoenix-human-services", "phoenix-human-services"),

    # Human Relations Commission
    (re.compile(r"human\s+relations\s+commission", re.I),
     "phoenix-human-relations", "phoenix-human-relations"),

    # Environmental Quality & Sustainability Commission
    (re.compile(r"environmental\s+quality", re.I),
     "phoenix-environmental-quality", "phoenix-environmental-quality"),

    # Mayor's Commission on Disability Issues
    (re.compile(r"mayor'?s?\s+commission\s+on\s+disability", re.I),
     "phoenix-disability-issues", "phoenix-disability-issues"),

    # Women's Commission
    (re.compile(r"women'?s?\s+commission", re.I),
     "phoenix-womens-commission", "phoenix-womens-commission"),

    # Heritage Commission
    (re.compile(r"heritage\s+commission", re.I),
     "phoenix-heritage-commission", "phoenix-heritage-commission"),

    # Design Review Committee
    (re.compile(r"design\s+review\s+committee", re.I),
     "phoenix-design-review", "phoenix-dr"),

    # Design Standards Committee
    (re.compile(r"design\s+standards\s+committee", re.I),
     "phoenix-design-standards", "phoenix-ds"),

    # Development Advisory Board
    (re.compile(r"development\s+advisory\s+board", re.I),
     "phoenix-development-advisory", "phoenix-dab"),

    # License Appeal Board
    (re.compile(r"license\s+appeal\s+board", re.I),
     "phoenix-license-appeal", "phoenix-license-appeal"),

    # Fire Pension Board
    (re.compile(r"fire\s+pension\s+board", re.I),
     "phoenix-fire-pension", "phoenix-fire-pension"),

    # Police Pension Board
    (re.compile(r"police\s+pension\s+board", re.I),
     "phoenix-police-pension", "phoenix-police-pension"),

    # COPERS Board / Investment Committee
    (re.compile(r"copers|employees'?\s*retirement\s*system", re.I),
     "phoenix-copers-board", "phoenix-copers-board"),

    # License Appeal Board
    (re.compile(r"license\s+appeal", re.I),
     "phoenix-license-appeal", "phoenix-license-appeal"),

    # Subcommittees (that aren't in Legistar)
    (re.compile(r"subcommittee", re.I),
     "phoenix-aem-subcommittee", "phoenix-as"),
]

# Default for unmapped bodies
_DEFAULT_SLUG = "phoenix-city-council"
_DEFAULT_CODE = "phoenix-cc"


# Events that are NOT public meetings (ceremonial, non-voting events)
NON_MEETING_PATTERNS: list[re.Pattern] = [
    re.compile(r"fire\s+station\s+\d+\s+(grand\s+)?opening", re.I),
    re.compile(r"ribbon\s+cutting", re.I),
    re.compile(r"groundbreaking", re.I),
    re.compile(r"open\s+house", re.I),
    # Annual notices are full-year standing schedules, not specific meetings.
    # The individual meetings they list are covered by Legistar for bodies
    # that have annual notices (City Council, subcommittees, Ethics Commission).
    re.compile(r"annual\s+notice", re.I),
]


def is_non_meeting(title: str) -> bool:
    """Check if a title matches a known non-meeting event pattern."""
    return any(p.search(title) for p in NON_MEETING_PATTERNS)


#: Resolved body values that are sentinels, not real bodies.  Checked in one
#: place so the ordinary search path and the incremental --sync-results path
#: cannot drift apart.
SENTINEL_BODY_VALUES = frozenset({"", "__skip__", "skip", "none", "null", "nan"})


def is_sentinel_body(value: object) -> bool:
    """True when a resolved body value is a sentinel rather than a real body."""
    if value is None:
        return True
    return str(value).strip().lower() in SENTINEL_BODY_VALUES


def resolve_persistable_body(title: str) -> tuple[str, str] | None:
    """The ``(slug, body_code)`` worth converting and persisting, else ``None``.

    The single guard both ingestion paths use.  A blank title, a sentinel body or
    a non-meeting title must never reach ``convert_to_meeting_dict`` or
    persistence: such a row is unparentable and blocks normalization downstream.
    Blank titles are rejected *before* the body map can fall through to its
    default, so an unknown title can never be silently attributed to a real body.
    """
    stripped = (title or "").strip()
    if not stripped:
        return None
    if is_non_meeting(stripped):
        return None
    slug, code = resolve_body(stripped)
    if is_sentinel_body(slug) or is_sentinel_body(code):
        return None
    return slug, code


def resolve_body(title: str) -> tuple[str, str]:
    """Map an AEM result title to a (slug, body_code)."""
    if is_non_meeting(title):
        return "__skip__", "__skip__"
    for pattern, slug, code in BODY_MAP:
        if pattern.search(title):
            return slug, code
    log.debug("Unmapped AEM body: %r - falling back to default", title)
    return _DEFAULT_SLUG, _DEFAULT_CODE


# ── Bodies that need new public_bodies created ──
# These slugs don't exist in the database yet. The integration code in
# main.py will handle inserting them.

NEW_BODY_DEFINITIONS: list[dict] = [
    # body_code now equals slug.  These previously carried abbreviated codes
    # (phoenix-hp, phoenix-za, …) while the slug was already the descriptive
    # form — that mismatch is where the orphaned/duplicate Phoenix bodies came
    # from.  The 2026-09-18 dev cleanup renamed the codes to match the slugs
    # (docs/briefs/034-dev-body-cleanup-changes-2026-09-18.md).
    {"name": "Phoenix Historic Preservation Commission", "slug": "phoenix-historic-preservation",
     "body_code": "phoenix-historic-preservation", "body_type": "Commission"},
    {"name": "Phoenix Zoning Adjustment", "slug": "phoenix-zoning-adjustment",
     "body_code": "phoenix-zoning-adjustment", "body_type": "Board"},
    {"name": "Phoenix Human Services Commission", "slug": "phoenix-human-services",
     "body_code": "phoenix-human-services", "body_type": "Commission"},
    {"name": "Phoenix Human Relations Commission", "slug": "phoenix-human-relations",
     "body_code": "phoenix-human-relations", "body_type": "Commission"},
    {"name": "Phoenix Environmental Quality & Sustainability Commission",
     "slug": "phoenix-environmental-quality", "body_code": "phoenix-environmental-quality", "body_type": "Commission"},
    {"name": "Phoenix Mayor's Commission on Disability Issues",
     "slug": "phoenix-disability-issues", "body_code": "phoenix-disability-issues", "body_type": "Commission"},
    {"name": "Phoenix Women's Commission", "slug": "phoenix-womens-commission",
     "body_code": "phoenix-womens-commission", "body_type": "Commission"},
    {"name": "Phoenix Heritage Commission", "slug": "phoenix-heritage-commission",
     "body_code": "phoenix-heritage-commission", "body_type": "Commission"},
    {"name": "Phoenix License Appeal Board", "slug": "phoenix-license-appeal",
     "body_code": "phoenix-license-appeal", "body_type": "Board"},
    {"name": "Phoenix Fire Pension Board", "slug": "phoenix-fire-pension",
     "body_code": "phoenix-fire-pension", "body_type": "Board"},
    {"name": "Phoenix Police Pension Board", "slug": "phoenix-police-pension",
     "body_code": "phoenix-police-pension", "body_type": "Board"},
    {"name": "Phoenix COPERS Board",
     "slug": "phoenix-copers-board", "body_code": "phoenix-copers-board", "body_type": "Board"},
    # phoenix-as is a genuine abbreviation whose slug differs from its code, so
    # this one is NOT an identity mapping.
    {"name": "Phoenix AEM Subcommittee", "slug": "phoenix-aem-subcommittee",
     "body_code": "phoenix-as", "body_type": "Subcommittee"},
]


def _build_url(base_url: str, query: str = "", offset: int = 0) -> str:
    """Build an AEM JSON endpoint URL with optional query and pagination."""
    params: list[str] = []
    if query:
        params.append(f"q={urllib.parse.quote(query)}")
    if offset > 0:
        params.append(f"offset={offset}")
    if params:
        return f"{base_url}?{'&'.join(params)}"
    return base_url


def fetch_json(url: str, timeout: int = 30) -> dict:
    """Fetch and parse JSON from an AEM endpoint."""
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        log.warning("Failed to fetch %s: %s", url, e)
        raise


def search_aem_meetings(
    endpoint: str = "notices",
    query: str = "",
    max_results: int = 0,
) -> list[dict]:
    """Search AEM meeting results with pagination.

    Args:
        endpoint: "notices" for upcoming meeting notices, "results" for past results
        query: Optional search term (e.g. "Planning Commission")
        max_results: Max results to fetch (0 = all)

    Returns:
        List of raw JSON result dicts
    """
    base_url = NOTICES_BASE if endpoint == "notices" else RESULTS_BASE

    all_results: list[dict] = []
    offset = 0

    while True:
        url = _build_url(base_url, query, offset)
        try:
            data = fetch_json(url)
        except Exception:
            break

        results = data.get("results", [])
        if not results:
            break

        all_results.extend(results)

        if max_results and len(all_results) >= max_results:
            all_results = all_results[:max_results]
            break

        # Check if there are more pages
        total_raw = data.get("resultTotal", 0)
        if isinstance(total_raw, str):
            try:
                total = int(total_raw)
            except (ValueError, TypeError):
                total = 0
        else:
            total = int(total_raw) if total_raw else 0
        if not total:
            # Try pagination labels
            pagination = data.get("pagination", [])
            if pagination:
                last_page = None
                for p in pagination:
                    label = p.get("label", "")
                    if label.isdigit():
                        last_page = int(label)
                if last_page is not None:
                    total = last_page * PAGE_SIZE

        offset += len(results)
        if total and offset >= total:
            break

        # Safety cap
        if offset > 5000:
            log.warning("Pagination safety cap reached at %d results", offset)
            break

    return all_results


def convert_to_meeting_dict(aem_result: dict, slug: str, code: str) -> dict:
    """Convert an AEM JSON result dict to our meeting data format.

    Returns a dict suitable for create_or_get_meeting() / replace_meeting_data_safe().
    """
    title = aem_result.get("title", "") or ""
    dam_path = aem_result.get("path", "") or ""
    rel_url = aem_result.get("url", "") or ""
    full_url = urllib.parse.urljoin(DAM_BASE, rel_url) if rel_url else ""
    props = aem_result.get("properties", {}) or {}
    meeting_time_raw = props.get("metadata/meetingTime", "")
    meeting_type_raw = props.get("metadata/meetingType", "Notice") or "Notice"

    # Parse ISO datetime → date string
    meeting_date = ""
    if meeting_time_raw:
        try:
            dt = datetime.fromisoformat(meeting_time_raw.replace("Z", "+00:00"))
            meeting_date = dt.strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            log.debug("Could not parse meetingTime: %s", meeting_time_raw)
            meeting_date = meeting_time_raw[:10] if len(meeting_time_raw) >= 10 else ""

    # Build meeting_id from path (DAM path is unique)
    # Strip leading /content/dam/ and file extension
    meeting_id = ""
    if dam_path:
        meeting_id = dam_path.replace("/content/dam/phoenix/cityclerksite/", "")
        meeting_id = re.sub(r"\.pdf$", "", meeting_id, flags=re.I)
        # Normalize path separators
        meeting_id = meeting_id.replace("/", "-")

    if not meeting_id:
        meeting_id = f"{slug}-{meeting_date}-{hash(dam_path) % 10000}"

    # Build meeting type label
    mtype_label = f"Planning Commission" if "planning" in title.lower() else title

    return {
        "meeting_id": meeting_id,
        "meeting_date": meeting_date,
        "meeting_type": mtype_label,
        "meeting_title": title,
        "meeting_title_raw": title,
        "body_slug": slug,
        "body_code": code,
        "source_url": full_url,
        "source_system": SOURCE_SYSTEM,
        "source_instance_url": SOURCE_INSTANCE_URL,
        "dam_path": dam_path,
        "pdf_url": full_url,
        "meeting_time_raw": meeting_time_raw,
        "aem_type": meeting_type_raw,
    }


def search_and_convert(
    endpoint: str = "notices",
    query: str = "",
    max_results: int = 0,
    body_filter: Optional[list[str]] = None,
) -> list[dict]:
    """Search AEM and convert results to meeting dicts, optionally filtered by body slug."""
    raw_results = search_aem_meetings(endpoint, query, max_results)
    converted: list[dict] = []
    seen_ids: set[str] = set()

    for r in raw_results:
        title = r.get("title", "") or ""
        resolved = resolve_persistable_body(title)
        if resolved is None:
            continue
        slug, code = resolved

        if body_filter and slug not in body_filter:
            continue

        md = convert_to_meeting_dict(r, slug, code)
        mid = md.get("meeting_id", "")
        if mid and mid in seen_ids:
            continue
        if mid:
            seen_ids.add(mid)
        converted.append(md)

    return converted


def fetch_all_notice_bodies(max_results: int = 200) -> list[dict]:
    """Fetch all upcoming notice meetings, returning meeting dicts."""
    return search_and_convert("notices", max_results=max_results)


def fetch_body_meetings(
    body_name: str,
    endpoint: str = "notices",
    max_results: int = 100,
) -> list[dict]:
    """Fetch meetings for a specific body name across notices or results."""
    return search_and_convert(endpoint, query=body_name, max_results=max_results)


def download_pdf(pdf_url: str, timeout: int = 60) -> Optional[bytes]:
    """Download a PDF from AEM DAM."""
    try:
        req = urllib.request.Request(pdf_url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except Exception as e:
        log.debug("Failed to download PDF: %s (%s)", pdf_url, e)
        return None


# ── Results PDF Parsing ──

def extract_pdf_text(pdf_bytes: bytes) -> list[str]:
    """Extract text lines from a PDF byte stream.

    Returns a list of stripped lines (filters near-empty lines).
    """
    reader = PdfReader(io.BytesIO(pdf_bytes))
    all_lines: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text()
        for line in page_text.split("\n"):
            line = line.strip()
            if not line or line in ("\n", ""):
                continue
            all_lines.append(line)
    return all_lines


_ITEM_NUMBER_RE = re.compile(
    r"^(\d+(?:\.\w+)?)\.?\s+(.*)$"
)

_APPLICATION_RE = re.compile(
    r"Application\s*#:\s*([A-Z0-9\-]+)", re.IGNORECASE
)

_CONTINUED_FROM_RE = re.compile(
    r"Continued from (.+?)$", re.IGNORECASE
)

_COMPANION_CASE_RE = re.compile(
    r"\(Companion Case [A-Z0-9\-]+\)", re.IGNORECASE
)



_FROM_ZONING_RE = re.compile(r"^From:\s*(.*)", re.IGNORECASE)
_TO_ZONING_RE = re.compile(r"^To:\s*(.*)", re.IGNORECASE)
_ACREAGE_RE = re.compile(r"^Acreage:\s*([\d\.]+)", re.IGNORECASE)
_LOCATION_RE = re.compile(r"^Location:\s*(.*)", re.IGNORECASE)
_PROPOSAL_RE = re.compile(r"^Proposal:\s*(.*)", re.IGNORECASE)
_APPLICANT_RE = re.compile(r"^Applicant:\s*(.*)", re.IGNORECASE)
_OWNER_RE = re.compile(r"^Owner:\s*(.*)", re.IGNORECASE)
_REPRESENTATIVE_RE = re.compile(r"^Representative:\s*(.*)", re.IGNORECASE)

_NEXT_COUNCIL_HEARING_RE = re.compile(
    r"If appealed, the City Council public hearing will be held on "
    r"([A-Z][a-z]+ \d+, \d{4}) at (\d+:\d+ [ap]\.?m\.?)",
    re.IGNORECASE,
)

_CONTINUED_TO_DATE_RE = re.compile(
    r"Continued to\s+([A-Z][a-z]+ \d+, \d{4})", re.IGNORECASE
)

_ORDINANCE_ADOPTION_RE = re.compile(
    r"If not appealed, the ordinance adoption will be on "
    r"([A-Z][a-z]+ \d+, \d{4}) at (\d+:\d+ [ap]\.?m\.?)",
    re.IGNORECASE,
)


# Action keywords that signal the start of an item result
# These appear in ALL CAPS (or mixed case) on the left column before item numbers
_ACTION_PREFIXES: list[str] = [
    "approved", "denied", "continued to", "continued, without",
    "information provided", "elections held", "information requested",
    "update provided", "none held", "tabled", "remanded",
    "remanded back",
]


def _line_is_item_start(line: str) -> bool:
    """Check if a line starts with an item number like '5.' or '1.a.' or '2.'."""
    return bool(_ITEM_NUMBER_RE.match(line))


def _line_is_action(line: str) -> bool:
    """Check if a line is an action label (starts with known action word)."""
    lower = line.lower().strip()
    for prefix in _ACTION_PREFIXES:
        if lower.startswith(prefix):
            return True
    return False


def _line_is_field(line: str) -> bool:
    """Check if a line is a structured field (e.g. 'From:', 'Acreage:')."""
    return bool(
        _FROM_ZONING_RE.match(line)
        or _TO_ZONING_RE.match(line)
        or _ACREAGE_RE.match(line)
        or _LOCATION_RE.match(line)
        or _PROPOSAL_RE.match(line)
        or _APPLICANT_RE.match(line)
        or _OWNER_RE.match(line)
        or _REPRESENTATIVE_RE.match(line)
        or _APPLICATION_RE.match(line)
    )


def _line_is_item_continuation(line: str) -> bool:
    """Check if a line is a continuation of an item's metadata (not action text).

    These include companion case references, parenthetical notes, and
    specific continuation qualifiers that appear between the item number
    and its structured fields.
    """
    lower = line.lower().strip()
    # Companion case: (Companion Case Z-xxx-xx-x)
    if _COMPANION_CASE_RE.search(line):
        return True
    # Continued from/at references (parenthetical)
    if lower.startswith("(continued from") or lower.startswith("(continued at"):
        return True
    # Remanded back references that are not paired with action keywords
    if lower.strip().startswith("remanded back"):
        # "remanded back to" is part of action text for "Continued to...remanded back to"
        # But isolated "remanded back" with preceding action should stay in action
        return False
    return False


def _is_section_header(line: str) -> bool:
    """Check if line is a section header (all caps section name)."""
    if _line_is_field(line) or _line_is_item_start(line):
        return False
    stripped = line.strip()
    section_candidates = [
        "CALL TO ORDER", "APPROVAL OF MINUTES",
        "CONTINUANCE / WITHDRAWAL REQUESTS", "REZONING CASES",
        "OTHER BUSINESS", "NEXT STEPS/FUTURE MEETINGS",
        "NOTICE OF RESULTS",
    ]
    for sc in section_candidates:
        if stripped.upper() == sc:
            return True
    # Generic all-caps header
    if stripped.isupper() and len(stripped) > 5 and not _line_is_action(line):
        return True
    return False


def _is_boilerplate_header(line: str) -> bool:
    """Check if line is boilerplate header like meeting notice info.

    Excludes lines that look like structured fields (From:, To:, Acreage:,
    Applicant:, etc.) or item-numbered lines.
    """
    if _line_is_field(line) or _line_is_item_start(line):
        return False
    stripped = line.strip()
    lower = stripped.lower()
    _STARTS = [
        "notice of results",
        "city of phoenix",
        "pursuant to a.r.s.",
        "the results for the meeting are as follows",
        "to confirm the meeting location",
        "city council meetings website",
    ]
    for prefix in _STARTS:
        if lower.startswith(prefix):
            return True
    _EXACT = {"vpc"}
    if stripped.rstrip(".") in _EXACT or stripped in _EXACT:
        return True
    return False


def parse_results_items(lines: list[str]) -> list[dict]:
    """Parse results PDF text lines into structured item dicts.

    Uses a two-pass approach:
    Pass 1: Identify all item boundaries (where item numbers appear in the text).
    Pass 2: For each item's block of text, separate action prefix from field data.

    Each item dict contains:
      - item_number: str (e.g. "5", "1.a")
      - item_title: str (title/description text)
      - action: str (the action label, e.g. "Approved, per the staff memo")
      - case_number: str (from Application #:)
      - from_zoning: str
      - to_zoning: str
      - acreage: str
      - location: str
      - proposal: str
      - applicant: str
      - owner: str
      - representative: str
      - continued_from: str
      - continued_to: str (explicit date like "June 4, 2026")
    """
    # ── Pass 1: Identify item boundaries ──
    # Build a list of item_starts, each with pre_action (action text before the item)
    # and extra (continuation lines after the item header).
    item_starts: list[dict] = []
    accumulated_action: list[str] = []
    in_action_zone = False  # True if we're accumulating multi-line action text
    action_zone_lines = 0  # count consecutive lines in action zone

    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped:
            in_action_zone = False
            action_zone_lines = 0
            continue

        # Skip boilerplate and section headers
        if _is_boilerplate_header(stripped) or _is_section_header(stripped):
            in_action_zone = False
            action_zone_lines = 0
            continue

        # Check for item number at start of line
        item_match = _ITEM_NUMBER_RE.match(stripped)
        if item_match:
            item_starts.append({
                "idx": i,
                "num": item_match.group(1),
                "title": item_match.group(2).strip(),
                "pre_action": list(accumulated_action),
                "extra": [],
            })
            accumulated_action = []
            in_action_zone = False
            action_zone_lines = 0
            continue

        # Check for action + item_number on same line (e.g. "Approved 1.a. For...")
        action_item_match = re.match(
            r"^(.+?)\s+(\d+(?:\.\w+)?)\.?\s*(.*)$", stripped
        )
        if action_item_match:
            candidate_action = action_item_match.group(1).strip()
            lower_candidate = candidate_action.lower()
            is_action_item = any(
                lower_candidate.startswith(p) for p in _ACTION_PREFIXES
            )
            if is_action_item:
                item_num = action_item_match.group(2)
                title_start = action_item_match.group(3).strip()
                item_starts.append({
                    "idx": i,
                    "num": item_num,
                    "title": title_start,
                    "pre_action": [candidate_action],
                    "extra": [],
                })
                # Peek at next line for continuation text
                if i + 1 < len(lines):
                    next_stripped = lines[i + 1].strip()
                    if (next_stripped
                        and not _is_boilerplate_header(next_stripped)
                        and not _is_section_header(next_stripped)
                        and not _ITEM_NUMBER_RE.match(next_stripped)
                        and not _line_is_action(next_stripped)):
                        item_starts[-1]["extra"].append(next_stripped)
                accumulated_action = []
                in_action_zone = False
                action_zone_lines = 0
                continue

        # ── Not an item start ──
        # Determine if this line starts or continues action text
        is_action_line = _line_is_action(stripped)
        is_continuation = _line_is_item_continuation(stripped)

        if is_action_line and not is_continuation:
            # Start of action text
            accumulated_action.append(stripped)
            in_action_zone = True
            action_zone_lines = 1
        elif is_continuation:
            # Item continuation text (companion case, staff memo, etc.)
            # Belongs to the most recent item, not action
            in_action_zone = False
            action_zone_lines = 0
            if item_starts:
                item_starts[-1]["extra"].append(stripped)
            else:
                accumulated_action.append(stripped)
        elif in_action_zone and action_zone_lines < 6:
            # Continuation of action text (up to 6 lines)
            accumulated_action.append(stripped)
            action_zone_lines += 1
        elif _line_is_field(stripped):
            # Field line — goes to the next item's action accumulation
            # (these are captured between items heading into the next item)
            if accumulated_action:
                # If we have accumulated action, the field belongs to the
                # item that starts after this field block
                accumulated_action.append(stripped)
            elif item_starts:
                # No accumulated action — this field continues from the
                # previous item (shouldn't happen, but handle gracefully)
                item_starts[-1]["extra"].append(stripped)
            else:
                accumulated_action.append(stripped)
            in_action_zone = False
            action_zone_lines = 0
        else:
            # Continuation of the most recent item's text
            in_action_zone = False
            action_zone_lines = 0
            if item_starts:
                item_starts[-1]["extra"].append(stripped)

    if not item_starts:
        return []

    # ── Pass 2: Build item dicts from boundary data ──
    items: list[dict] = []

    for si_idx, si in enumerate(item_starts):
        item_num = si["num"]
        item_title = si["title"]
        pre_action = si["pre_action"]
        extra_lines = si["extra"]

        # Determine end: lines before the next item start
        if si_idx + 1 < len(item_starts):
            end_idx = item_starts[si_idx + 1]["idx"]
        else:
            end_idx = len(lines)

        # Collect all lines that belong to this item, split into:
        #   action_part: the action text (pre_action lines)
        #   data_part: item content (extra lines + lines after boundary up to next boundary)
        action_part: list[str] = list(pre_action)
        data_part: list[str] = list(extra_lines)

        # Lines after the boundary line, up to the next boundary
        for j in range(si["idx"] + 1, end_idx):
            stripped = lines[j].strip()
            if not stripped:
                continue
            if _is_boilerplate_header(stripped) or _is_section_header(stripped):
                continue
            # Skip lines already captured as "extra" (for action_item_match items)
            if si["extra"] and j == si["idx"] + 1:
                continue
            data_part.append(stripped)

        # For items without structured fields (e.g. items 9-14, 1.a-1.d),
        # the action may have absorbed title text. Try to split cleanly.
        # Check if data_part is empty or has only non-field content
        has_fields = any(_line_is_field(d) for d in data_part)

        if not has_fields and action_part:
            # Items like "Information provided and discussion held 9. Presentation..."
            # have action and title merged. The action is the first 1-3 lines
            # containing action keywords.
            clean_action: list[str] = []
            clean_data: list[str] = []
            for ap in action_part:
                if _line_is_action(ap) or (clean_action and not _line_is_action(ap)):
                    clean_action.append(ap)
                else:
                    clean_data.append(ap)
            # Merge clean_data back if original action_part had title text
            if clean_data:
                data_part = clean_data + data_part
            action_part = clean_action

        # Now split: re-classify action_part lines so non-action text
        # (e.g. continuation that was pre-pended) goes to data_part
        refined_action: list[str] = []
        for ap in action_part:
            if _line_is_action(ap) or (refined_action and not _line_is_field(ap) and not _line_is_item_continuation(ap)):
                refined_action.append(ap)
            else:
                data_part.append(ap)
        action_part = refined_action

        # Final action/data split using field boundaries
        clean_action2: list[str] = []
        clean_data2: list[str] = list(data_part)
        seen_field = False

        for ap in action_part:
            is_field = _line_is_field(ap)
            if is_field:
                seen_field = True
                clean_data2.append(ap)
            elif seen_field:
                clean_data2.append(ap)
            else:
                clean_action2.append(ap)

        # If data_part has content that looks like action text, move it
        # before the first field
        if clean_data2:
            post_action: list[str] = []
            rest_data: list[str] = []
            seen_field2 = False
            for cd in clean_data2:
                if not seen_field2 and _line_is_action(cd) and not _line_is_field(cd):
                    post_action.append(cd)
                else:
                    seen_field2 = True
                    rest_data.append(cd)
            if post_action and not clean_action2:
                clean_action2 = post_action
                clean_data2 = rest_data
            elif post_action:
                clean_action2.extend(post_action)
                clean_data2 = rest_data

        action_text = " ".join(w for w in action_part if w.strip()).strip()

        # Build the item dict
        item = {
            "item_number": item_num,
            "item_title": item_title,
            "action": action_text,
            "case_number": "",
            "from_zoning": "",
            "to_zoning": "",
            "acreage": "",
            "location": "",
            "proposal": "",
            "applicant": "",
            "owner": "",
            "representative": "",
            "continued_from": "",
            "continued_to": "",
        }

        _finalize_item(item, data_part)
        items.append(item)

    return items


def _finalize_item(item: dict, lines: list[str]) -> None:
    """Parse accumulated lines into structured item fields."""
    for line in lines:
        line = line.strip()
        if not line:
            continue

        # Application #:
        app_match = _APPLICATION_RE.search(line)
        if app_match:
            item["case_number"] = app_match.group(1)
            continue

        # Continued from
        cf_match = _CONTINUED_FROM_RE.search(line)
        if cf_match:
            item["continued_from"] = cf_match.group(1).strip()
            continue

        # Continued to (explicit date)
        ct_match = _CONTINUED_TO_DATE_RE.search(line)
        if ct_match:
            item["continued_to"] = ct_match.group(1).strip()
            continue

        # From/To zoning
        from_match = _FROM_ZONING_RE.match(line)
        if from_match:
            item["from_zoning"] = from_match.group(1).strip()
            continue

        to_match = _TO_ZONING_RE.match(line)
        if to_match:
            item["to_zoning"] = to_match.group(1).strip()
            continue

        # Acreage
        acr_match = _ACREAGE_RE.match(line)
        if acr_match:
            item["acreage"] = acr_match.group(1).strip()
            continue

        # Location
        loc_match = _LOCATION_RE.match(line)
        if loc_match:
            item["location"] = loc_match.group(1).strip()
            continue

        # Proposal
        prop_match = _PROPOSAL_RE.match(line)
        if prop_match:
            item["proposal"] = prop_match.group(1).strip()
            continue

        # Applicant
        app_match2 = _APPLICANT_RE.match(line)
        if app_match2:
            item["applicant"] = app_match2.group(1).strip()
            continue

        # Owner
        own_match = _OWNER_RE.match(line)
        if own_match:
            item["owner"] = own_match.group(1).strip()
            continue

        # Representative
        rep_match = _REPRESENTATIVE_RE.match(line)
        if rep_match:
            item["representative"] = rep_match.group(1).strip()
            continue

        # If this line doesn't match any field, it's part of the item title or
        # a continuation of a multi-line value. Append to title.
        existing = item.get("item_title", "")
        if existing:
            item["item_title"] = existing + " " + line
        else:
            item["item_title"] = line

    # Trim whitespace
    for key in list(item.keys()):
        if isinstance(item[key], str):
            item[key] = item[key].strip()

    # Deduplicate title if text was repeated during two-pass merging
    title = item.get("item_title", "")
    if title:
        # Remove duplicate consecutive phrases (e.g. "Ranger Drive Western Homes" repeated)
        half = len(title) // 2
        if half > 10 and title[:half] == title[half:half * 2]:
            item["item_title"] = title[:half].strip()
        elif half > 10 and title[:half].strip() == title[half:half * 2].strip():
            item["item_title"] = title[:half].strip()
        # Remove duplicate at the end (repeated suffix)
        words = title.split()
        if len(words) >= 6:
            # Check if the last N words duplicate the N-words before them
            for n in (6, 5, 4, 3, 2):
                if len(words) >= n * 2:
                    suffix = " ".join(words[-n:])
                    prefix = " ".join(words[-(n * 2):-n])
                    if suffix == prefix:
                        item["item_title"] = " ".join(words[:-n]).strip()
                        break

    # If no case_number was found but the item has a companion line in the title,
    # try extracting case numbers from the title
    if not item["case_number"] and item.get("item_title", ""):
        case_in_title = _APPLICATION_RE.search(item["item_title"])
        if case_in_title:
            item["case_number"] = case_in_title.group(1).strip()


def parse_meeting_level_data(lines: list[str]) -> dict:
    """Extract meeting-level data: next council hearing, ordinance adoption.

    Returns a dict with keys:
      - next_council_hearing_date: str or None
      - next_council_hearing_time: str or None
      - ordinance_adoption_date: str or None
      - ordinance_adoption_time: str or None
    """
    result: dict = {
        "next_council_hearing_date": None,
        "next_council_hearing_time": None,
        "ordinance_adoption_date": None,
        "ordinance_adoption_time": None,
    }
    full_text = " ".join(lines)

    nh_match = _NEXT_COUNCIL_HEARING_RE.search(full_text)
    if nh_match:
        result["next_council_hearing_date"] = nh_match.group(1).strip()
        result["next_council_hearing_time"] = nh_match.group(2).strip()

    oa_match = _ORDINANCE_ADOPTION_RE.search(full_text)
    if oa_match:
        result["ordinance_adoption_date"] = oa_match.group(1).strip()
        result["ordinance_adoption_time"] = oa_match.group(2).strip()

    return result


def extract_results_from_pdf(pdf_bytes: bytes) -> dict:
    """Full extraction pipeline for a Phoenix AEM results PDF.

    Returns:
      {
        "items": [list of parsed item dicts],
        "meeting_level": { meeting-level data dict },
      }
    """
    lines = extract_pdf_text(pdf_bytes)
    items = parse_results_items(lines)
    meeting_level = parse_meeting_level_data(lines)
    return {"items": items, "meeting_level": meeting_level}


def map_result_action_to_vote(action_text: str) -> str:
    """Map a results PDF action label to a short vote_or_action string.

    Phoenix results PDFs use descriptive action text like:
      "Approved, per the staff memo"  → "Approved"
      "Denied as filed and approved R1-8 zoning" → "Denied"
      "Continued to June 4, 2026" → "Continued"
      "Information provided and discussion held" → "Information provided"
    """
    if not action_text:
        return ""
    lower = action_text.lower().strip()

    if lower.startswith("approved"):
        return "Approved"
    if lower.startswith("denied"):
        return "Denied"
    if lower.startswith("continued"):
        return "Continued"
    if "elections" in lower and "held" in lower:
        return "Elections held"
    if "information provided" in lower or "discussion held" in lower:
        return "Discussion held"
    if "information requested" in lower:
        return "Information requested"
    if "update provided" in lower:
        return "Update provided"
    if "none held" in lower:
        return "None held"
    if lower.startswith("tabled"):
        return "Tabled"
    if lower.startswith("remanded"):
        return "Remanded"

    # Fall back to truncated original
    if len(action_text) > 60:
        return action_text[:60] + "..."
    return action_text


def store_results_in_db(
    session,
    body_code: str,
    meeting_id: str,
    results_data: dict,
) -> int:
    """Store results PDF extraction results into the database.

    Updates agenda_items with vote_or_action for matching items.
    Returns the number of items updated.
    """
    from db import AgendaItem as AgendaItemModel
    from sqlalchemy import select

    items = results_data.get("items", [])
    if not items:
        return 0

    updated = 0
    for item in items:
        item_num = item.get("item_number", "")
        action = item.get("action", "")
        if not action:
            continue

        vote_action = map_result_action_to_vote(action)

        # Find matching agenda_item by number within this meeting
        ag = session.execute(
            select(AgendaItemModel).where(
                AgendaItemModel.body == body_code,
                AgendaItemModel.meeting_id == meeting_id,
                AgendaItemModel.agenda_item_number == item_num,
            )
        ).scalar_one_or_none()

        if ag:
            ag.vote_or_action = vote_action
            # Optionally store detailed action in agenda_item_text
            if item.get("action"):
                detail = f"[Result: {action}]"
                if not ag.agenda_item_text:
                    ag.agenda_item_text = detail
                elif detail not in ag.agenda_item_text:
                    ag.agenda_item_text += f"\n\n{detail}"
            updated += 1

    session.commit()
    return updated


def update_meeting_with_results(
    session,
    body_code: str,
    meeting_id: str,
    results_data: dict,
) -> None:
    """Update meeting-level fields from results PDF.

    Stores next_council_hearing_date as a meeting note or custom field.
    """
    from db import Meeting as MeetingModel
    from sqlalchemy import select

    meeting = session.execute(
        select(MeetingModel).where(
            MeetingModel.body == body_code,
            MeetingModel.meeting_id == meeting_id,
        )
    ).scalar_one_or_none()

    if not meeting:
        log.warning("Meeting %s/%s not found for results update", body_code, meeting_id)
        return

    ml = results_data.get("meeting_level", {})
    nh_date = ml.get("next_council_hearing_date")
    nh_time = ml.get("next_council_hearing_time")

    if nh_date or nh_time:
        note = meeting.last_error or ""
        if note and not note.startswith("Next Council Hearing:"):
            note = ""
        parts = []
        if nh_date:
            parts.append(f"Date: {nh_date}")
        if nh_time:
            parts.append(f"Time: {nh_time}")
        nh_label = " | ".join(parts)
        meeting.last_error = f"Next Council Hearing: {nh_label}"

    # Store continued_to info as a note too
    continued_items = [
        i for i in results_data.get("items", [])
        if i.get("continued_to")
    ]
    if continued_items:
        notes = []
        for ci in continued_items:
            num = ci.get("item_number", "?")
            ct = ci.get("continued_to", "?")
            notes.append(f"Item {num} continued to {ct}")
        if notes:
            existing = meeting.last_error or ""
            continuation = "; ".join(notes)
            if existing:
                meeting.last_error = existing + " | " + continuation
            else:
                meeting.last_error = continuation

    meeting.votes_extracted = True
    session.commit()
