"""
Phoenix boards.phoenix.gov member list scraper.

Scrapes board/commission member lists from boards.phoenix.gov and stores
them in the persons + body_memberships tables.

API features:
  - Boards list: https://boards.phoenix.gov/Home/BoardsList
  - Board detail: https://boards.phoenix.gov/Home/BoardsDetail/{board_id}
  - Each detail page has an "Active Member List" table with member names
  - Member names may include role suffixes like "Ex-Officio", "Chair", etc.

Mapping from boards.phoenix.gov board IDs to our public_bodies table
is maintained in BOARD_ID_TO_BODY_CODE dict.
"""

from __future__ import annotations
import logging
import re
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

BASE_URL = "https://boards.phoenix.gov"
BOARDS_LIST_URL = f"{BASE_URL}/Home/BoardsList"
BOARD_DETAIL_URL = f"{BASE_URL}/Home/BoardsDetail/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
}

# ── Board ID → body_code mapping ──
# Maps boards.phoenix.gov board IDs to our public_bodies body_code values.
# These are the boards we track in our system.
BOARD_ID_TO_BODY_CODE: dict[int, str] = {
    55: "phoenix-pc",          # Planning Commission
    8: "phoenix-boa",          # Board of Adjustment
    27: "phoenix-historic-preservation",          # Historic Preservation Commission
    29: "phoenix-human-services",          # Human Services Commission
    28: "phoenix-human-relations",          # Human Relations Commission
    22: "phoenix-environmental-quality",          # Environmental Quality & Sustainability Commission
    36: "phoenix-disability-issues",          # Mayor's Commission on Disability Issues
    53: "phoenix-womens-commission",          # Phoenix Women's Commission
    26: "phoenix-heritage-commission",          # Heritage Commission
    34: "phoenix-license-appeal",          # License Appeals Board
    57: "phoenix-fire-pension",          # Fire Pension Board (PSPRS Local Fire)
    73: "phoenix-police-pension",          # Police Pension Board (PSPRS Local Police)
    84: "phoenix-copers-board",          # City of Phoenix Retirement Board (COPERS)
    3: "phoenix-village-planning",          # Ahwatukee Foothills Village Planning Committee
    9: "phoenix-village-planning",          # Camelback East Village Planning Committee
    10: "phoenix-village-planning",         # Central City Village Planning Committee
    16: "phoenix-village-planning",         # Deer Valley Village Planning Committee
    17: "phoenix-village-planning",         # Desert View Village Planning Committee
    21: "phoenix-village-planning",         # Encanto Village Planning Committee
    23: "phoenix-village-planning",         # Estrella Village Planning Committee
    32: "phoenix-village-planning",         # Laveen Village Planning Committee
    35: "phoenix-village-planning",         # Maryvale Village Planning Committee
    39: "phoenix-village-planning",         # North Gateway Village Planning Committee
    40: "phoenix-village-planning",         # North Mountain Village Planning Committee
    42: "phoenix-village-planning",         # Paradise Valley Village Planning Committee
    59: "phoenix-village-planning",         # Rio Vista Village Planning Committee
    61: "phoenix-village-planning",         # South Mountain Village Planning Committee
    6: "phoenix-village-planning",          # Alhambra Village Planning Committee
}

# Board names that map to shared slugs but are individually listed on boards.phoenix.gov
_VILLAGE_PLANNING_BOARD_IDS: set[int] = {
    3, 6, 9, 10, 16, 17, 21, 23, 32, 35, 39, 40, 42, 59, 61,
}


def fetch_boards_list() -> list[dict]:
    """Fetch all board IDs and names from the boards list page.

    Returns:
        List of dicts with 'id' (int) and 'name' (str) for each board.
    """
    req = urllib.request.Request(BOARDS_LIST_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch boards list: %s", e)
        return []

    boards: list[dict] = []
    # Match <a href="/Home/BoardsDetail/ID">Board Name</a>
    pattern = re.compile(
        r'href="/Home/BoardsDetail/(\d+)"[^>]*>([^<]+)</a>'
    )
    for match in pattern.finditer(html):
        board_id = int(match.group(1))
        name = match.group(2).strip()
        boards.append({"id": board_id, "name": name})

    return boards


def fetch_board_detail(board_id: int) -> str:
    """Fetch the HTML for a single board detail page.

    Returns:
        HTML string of the board detail page.
    """
    url = f"{BOARD_DETAIL_URL}{board_id}"
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        log.warning("Failed to fetch board detail %s: %s", board_id, e)
        return ""


def extract_members_from_detail(html: str) -> list[dict]:
    """Extract member names from a board detail page HTML.

    This works with both JS-rendered and server-rendered member tables.
    The "Active Member List" section contains a table with member names.

    Returns:
        List of dicts with 'name' (str) and 'role' (str or None) for each member.
    """
    if not html:
        return []

    # Find the "Active Member List" section
    idx = html.find("Active Member List")
    if idx < 0:
        # Fallback: look for any <th>Member Name</th> table
        idx = html.find("Member Name")
        if idx < 0:
            return []

    # Look within this section for table rows
    section = html[idx:idx + 5000]
    members: list[dict] = []

    # Find all <td>...</td> within the section (each is a member name cell)
    td_pattern = re.compile(r"<td>(.*?)</td>", re.DOTALL)
    for td_match in td_pattern.finditer(section):
        name_raw = td_match.group(1).strip()
        if not name_raw or name_raw.startswith("<"):
            continue

        # Parse name and optionally role (e.g., "Joshua Bednarek, Ex-Officio")
        name, role = _parse_member_name(name_raw)
        if name:
            members.append({"name": name, "role": role})

    return members


def _parse_member_name(raw: str) -> tuple[str, str | None]:
    """Parse a raw member name string into (name, role).

    Handles:
      - "Jonathan Ammon" → ("Jonathan Ammon", None)
      - "Joshua Bednarek, Ex-Officio" → ("Joshua Bednarek", "Ex-Officio")
      - "Toni  Broberg" → ("Toni Broberg", None) — handles double spaces
    """
    raw = raw.strip()
    if not raw:
        return "", None

    # Normalize whitespace (single spaces)
    raw = re.sub(r"\s+", " ", raw)

    role = None
    # Check for known role suffixes
    known_roles = [
        "Ex-Officio", "Chair", "Vice Chair", "Vice-Chair", "Secretary",
        "Treasurer", "Mayor", "Councilmember", "Council Member",
    ]
    for kr in known_roles:
        if raw.endswith(f", {kr}"):
            role = kr
            raw = raw[:-(len(kr) + 2)].strip()
            break
        if raw.endswith(f" ({kr})"):
            role = kr
            raw = raw[:-(len(kr) + 3)].strip()
            break

    if not raw:
        return "", role

    return raw, role


def sync_board_members(session, body_code: str, members: list[dict]) -> int:
    """Store scraped member names as persons and body memberships.

    Uses _find_or_create_person and _ensure_membership from db.persist.

    Args:
        session: SQLAlchemy session
        body_code: Our body code (e.g. "phoenix-pc")
        members: List of dicts with 'name' and 'role' (from extract_members_from_detail)

    Returns:
        Number of members synced
    """
    from db import _find_or_create_person, _ensure_membership

    count = 0
    for member in members:
        name = member["name"]
        role = member.get("role")

        if not name:
            continue

        # Normalize name for matching
        normalized = name.lower().strip()

        person, created = _find_or_create_person(
            session, name, normalized,
            log_prefix=f"phoenix-boards[{body_code}]",
        )

        if person:
            membership = _ensure_membership(
                session, person.id, body_code,
            )
            if membership and role:
                membership.role = role
            count += 1

    session.commit()
    return count


def scrape_and_sync_board(session, board_id: int) -> int:
    """Full pipeline: fetch board detail, extract members, sync to DB.

    Args:
        session: SQLAlchemy session
        board_id: boards.phoenix.gov board ID

    Returns:
        Number of members synced, or 0 if board isn't mapped or failed.
    """
    body_code = BOARD_ID_TO_BODY_CODE.get(board_id)
    if not body_code:
        log.debug("Board ID %d has no body_code mapping, skipping", board_id)
        return 0

    html = fetch_board_detail(board_id)
    if not html:
        return 0

    members = extract_members_from_detail(html)
    if not members:
        log.warning("No members found for board %d", board_id)
        return 0

    log.info("Board %d (%s): %d members found", board_id, body_code, len(members))
    count = sync_board_members(session, body_code, members)
    log.info("Board %d: %d members synced", board_id, count)
    return count


def scrape_and_sync_all_known_boards(session) -> int:
    """Scrape and sync member lists for all known board IDs.

    Returns:
        Total number of members synced across all boards.
    """
    total = 0
    for board_id in BOARD_ID_TO_BODY_CODE:
        try:
            total += scrape_and_sync_board(session, board_id)
        except Exception as e:
            log.error("Failed to sync board %d: %s", board_id, e)
    return total


def discover_and_map_boards() -> list[dict]:
    """Discover all boards from boards.phoenix.gov and show their names and IDs.

    Returns list of board dicts for manual mapping.
    """
    return fetch_boards_list()
