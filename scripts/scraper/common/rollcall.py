"""
Roll call and chair extraction from meeting minutes.

Reads arbitrary minutes text and extracts:
  - The chair (person presiding)
  - Attendance (present + absent members with roles)
  - Individual votes per agenda item (mover, seconder, result)

Handles several formats across Arizona municipal minutes:
  - BOS summary (explicit Ayes/Nays by name)
  - Board/commission minutes (attendance list + unanimous/split)
  - City council Results PDFs (vote counts + named dissenters)

Usage:
    from scraper.common.rollcall import parse_rollcall

    result = parse_rollcall(minutes_text)
    chair = result["chair"]
    attendance = result["attendance"]
    votes = result["votes"]
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

log = logging.getLogger(__name__)


# ── Helpers ──────────────────────────────────────────────────────────────────


def _normalize(name: str) -> str:
    """Collapse whitespace, lowercase."""
    return re.sub(r"\s+", " ", name.lower()).strip()


def _strip_prefix(name: str, prefixes: list[str]) -> str:
    """Remove prefix like 'Councilmember', 'Mayor', 'Commissioner', etc."""
    cleaned = name
    for p in prefixes:
        cleaned = re.sub(r"^\s*" + re.escape(p) + r"\s+", "", cleaned, flags=re.I)
    return cleaned.strip()


# ── Phase 1: Chair Extraction ────────────────────────────────────────────────


_CHAIR_CALLED_RE = re.compile(
    r"("  # group 1: role prefix
    r"Chairperson|Chair(?!person)|Vice\s*Chair|Vice\s*Mayor|Mayor|"
    r"Ms\.|Mr\.|Dr\."
    r")\s+"
    r"([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']*)?)"  # group 2: name (1-2 words)
    r"\s+called\s+the\s+meeting\s+to\s+order",
    re.I,
)

_MEETING_CALLED_RE = re.compile(
    r"(?:Ms\.|Mr\.|Dr\.)?\s*"
    r"([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']*)?)\s+"
    r"called\s+the\s+meeting\s+to\s+order",
    re.I,
)

_CALLED_BY_RE = re.compile(
    r"the\s+meeting\s+was\s+called\s+to\s+order\s+(?:at\s+[\d:]+\s+(?:am|pm)\s+)?by\s+"
    r"(?:Ms\.|Mr\.|Dr\.|Chairperson|Chair|Mayor)?\s*"
    r"([A-Z][a-zA-Z']+(?:\s+[A-Z][a-zA-Z']*)?)",
    re.I,
)


def extract_chair_from_header(text: str) -> dict | None:
    """Find the meeting chair from the first ~100 lines of minutes.

    Tries, in order:
      1. 'Chairperson X called the meeting to order'
      2. 'The meeting was called to order by X'
      3. 'Ms./Mr. X called the meeting to order'
      4. 'X, Chair' or 'Chair: X' in attendance section
      5. 'Mayor X' in attendance (for city councils)

    Returns:
        { "name": ..., "normalized_name": ..., "role": ..., "detection_method": ... }
        or None if no chair found.
    """
    lines = text.split("\n")[:100]
    header = "\n".join(lines)

    # Pattern 1: "Chairperson X called the meeting to order"
    m = _CHAIR_CALLED_RE.search(header)
    if m:
        role_raw = m.group(1).strip().rstrip(".")
        name = m.group(2).strip()
        valid_roles = {
            "Chairperson", "Chair", "Vice Chair", "Vice-Chair",
            "Mayor", "Vice Mayor", "Vice-Mayor",
        }
        return {
            "name": name,
            "normalized_name": _normalize(name),
            "role": role_raw if role_raw in valid_roles else None,
            "detection_method": "call_to_order_explicit",
        }

    # Pattern 2: "The meeting was called to order by X"
    m = _CALLED_BY_RE.search(header)
    if m:
        name = m.group(1).strip()
        return {
            "name": name,
            "normalized_name": _normalize(name),
            "role": "Chair",
            "detection_method": "called_by",
        }

    # Pattern 3: "Ms. X called the meeting to order" (no explicit role)
    m = _MEETING_CALLED_RE.search(header)
    if m:
        name = m.group(1).strip()
        return {
            "name": name,
            "normalized_name": _normalize(name),
            "role": "Chair",
            "detection_method": "called_by_inferred",
        }

    # Pattern 4: Look in attendance section for "X, Chair" or "X, Chairman"
    for line in lines:
        line_s = line.strip()
        m = re.search(
            r"([A-Z][a-z]+(?:\s+[A-Z][a-z']+)*)\s*[,—–-]+\s*"
            r"(?:Chair\b(?!\s*person)|Chairman)",
            line_s,
        )
        if m:
            name = m.group(1).strip()
            if name and len(name) > 3:
                return {
                    "name": name,
                    "normalized_name": _normalize(name),
                    "role": "Chair",
                    "detection_method": "attendance_list_role",
                }
        m = re.search(
            r"Chair\s*[:\--]+\s*([A-Z][a-z]+(?:\s+[A-Z][a-z']+)*)",
            line_s,
        )
        if m:
            name = m.group(1).strip()
            if name and len(name) > 3:
                return {
                    "name": name,
                    "normalized_name": _normalize(name),
                    "role": "Chair",
                    "detection_method": "attendance_list_label",
                }

    # Pattern 5: "Mayor X" in attendance
    for line in lines:
        m = re.search(r"Mayor\s+([A-Z][a-z]+(?:\s+[A-Z][a-z']+)*)", line)
        if m:
            name = m.group(1).strip()
            if name and len(name) > 3 and "Mayor" not in name:
                return {
                    "name": name,
                    "normalized_name": _normalize(name),
                    "role": "Mayor",
                    "detection_method": "mayor_attendance",
                }

    return None


# ── Phase 1b: Attendance Extraction ──────────────────────────────────────────


_ATTENDANCE_HEADERS = [
    r"Board\s+Present",
    r"Board\s+Members?\s+Present",
    r"Commissioners?\s+Present",
    r"Commission\s+Members?\s+Present",
    r"Members?\s+in\s+Attendance",
    r"Panel\s+Members?\s+in\s+Attendance",
    r"Council\s+Attendance",
    r"Roll\s+Call",
    r"Councilmembers?\s+Present",
    r"Members?\s+Present",
]

_ABSENT_HEADERS = [
    r"Board\s+Absent",
    r"Board\s+Members?\s+Absent",
    r"Commissioners?\s+Absent",
    r"Commission\s+Members?\s+Absent",
    r"Members?\s+Absent",
    r"Members?\s+not\s+present",
    r"Councilmembers?\s+Absent",
    r"Councilmember\s+.*?absent",
    r"Panel\s+Members?\s+Absent",
]

_NAME_PREFIXES = [
    "Councilmember", "Council Member", "Commissioner",
    "Board Member", "Panel member", "Panel Member", "Member",
    "Mayor", "Vice Mayor", "Vice Chair", "Vice-Chair",
    "Chairperson", "Chair", "Mr.", "Ms.", "Dr.", "Mrs.",
    "Secretary", "Ex-Officio", "Ex Officio",
]

_ROLE_LABELS = [
    "Chairperson", "Chair", "Chairman", "Vice Chair", "Vice-Chair",
    "Secretary", "Treasurer",
    "Ex-Officio", "Ex Officio",
    "Board Member", "Commissioner", "Panel Member",
]


def _parse_attendance_line(line: str, seen: set[str]) -> dict | None:
    """Try to parse a single attendance line into a member dict.

    Handles:
      'Julie Graham, Chair'
      'Neil Calfee, Board Member'
      'Councilmember Angel Encinas'
      'Mayor Kevin Hartke'
      'Chairperson Jason Diefenbacher'
    """
    line = line.strip().rstrip(",;")
    if not line or len(line) < 3:
        return None

    # Try "Name, Role" pattern
    m = re.match(
        r"([A-Za-z]+(?:\s+[A-Za-z']+)*)\s*[,\-—]+\s*("
        + "|".join(_ROLE_LABELS)
        + r")",
        line,
        re.I,
    )
    if m:
        name = m.group(1).strip()
        role = m.group(2).strip()
    else:
        # Try prefix pattern: "Role Name"
        name = line
        role = None
        for prefix in _NAME_PREFIXES:
            if re.match(re.escape(prefix) + r"\s+", line, re.I):
                name = re.sub(
                    r"^" + re.escape(prefix) + r"\s+", "", line, flags=re.I
                ).strip()
                role = prefix
                break

    name = re.sub(r"\s+", " ", name).strip()

    if not re.match(r"^[A-Za-z][A-Za-z\s'.\-]+$", name) or len(name) < 3:
        return None

    norm = _normalize(name)
    if norm in seen:
        return None
    seen.add(norm)

    return {"name": name, "normalized_name": norm, "role": role if role else None}


def _parse_names_from_header_line(line: str, seen: set[str]) -> list[dict]:
    """Parse comma-separated names from a header line like:
    "Commissioners Present: David Wilkinson, Shachi Kale, Liz Taylor"
    """
    after_colon = line.split(":", 1)
    if len(after_colon) < 2:
        return []
    name_part = after_colon[1].strip()
    if not name_part:
        return []

    result: list[dict] = []
    for candidate in re.split(r",\s*|\s+and\s+", name_part):
        candidate = candidate.strip()
        if not candidate or len(candidate) < 3:
            continue
        # Clean extra whitespace
        name = re.sub(r"\s+", " ", candidate).strip()
        if not re.match(r"^[A-Za-z][A-Za-z\s'.\-]+$", name) or len(name) < 3:
            continue
        norm = _normalize(name)
        if norm in seen:
            continue
        seen.add(norm)
        result.append({"name": name, "normalized_name": norm, "role": None})

    return result


def _find_section(text: str, headers: list[str]) -> tuple[int, int]:
    """Find the start and end of a section that matches any of the headers.

    Returns (start_line_idx, end_line_idx) or (-1, -1).
    """
    lines = text.split("\n")
    start = -1
    for i, line in enumerate(lines):
        if any(re.search(pat, line, re.I) for pat in headers):
            start = i
            break
    if start < 0:
        return -1, -1

    end = min(start + 30, len(lines))
    for i in range(start + 1, min(start + 30, len(lines))):
        stripped = lines[i].strip()
        if not stripped:
            continue
        if re.match(
            r"^(Staff\s+(Present|in Attendance)|Guests?\s+Present|"
            r"Call\s+to\s+Order|Unscheduled|Scheduled|Pledge|"
            r"Invocation|Consent\s+Agenda|\d+\.|"
            r"Members?\s+Absent|Staff|Guests)",
            stripped,
            re.I,
        ):
            end = i
            break
        if re.match(
            r"Others?\s+Present|Board\s+Absent|Commissioners?\s+Absent|"
            r"Panel\s+Members?\s+Absent",
            stripped,
            re.I,
        ):
            end = i
            break
        if not stripped and i > start + 2:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if re.match(
                r"^(Board\s+(Absent|Members?\s+Absent)|Commissioners?\s+Absent|"
                r"Members?\s+Absent|Councilmembers?\s+Absent|"
                r"Others?\s+Present|Staff\s+(Present|in\s+Attendance)|"
                r"Guests?\s+Present|Call\s+to\s+Order|\d+\.)",
                nxt,
                re.I,
            ):
                end = i
                break
            if nxt and not re.match(r"^\d+\.", nxt) and len(nxt) < 60:
                pass
            elif nxt and re.match(r"^[A-Z][a-z]+", nxt) and not re.search(
                r"called|moved|seconded|approved|passed|vote", nxt, re.I
            ):
                pass
            else:
                end = i
                break

    return start, end


def extract_attendance(text: str) -> list[dict]:
    """Extract attendance list (present + absent) from minutes text.

    Returns a list of member dicts:
        { "name": ..., "normalized_name": ..., "role": ..., "present": bool }
    """
    members: list[dict] = []
    seen: set[str] = set()

    def add_members(member_dicts: list[dict], present: bool) -> None:
        for m in member_dicts:
            norm = m["normalized_name"]
            # Skip single-word names (likely first name only / incomplete)
            if len(norm.split()) < 2 and len(norm) < 6:
                continue
            if norm not in seen:
                seen.add(norm)
                m["present"] = present
                members.append(m)

    # Extract present members
    start, end = _find_section(text, _ATTENDANCE_HEADERS)
    if start >= 0:
        raw_lines = text.split("\n")[start:end]
        # Check if first line has comma-separated format (no newline between names)
        # by counting commas in the header line vs total section
        header_line = raw_lines[0]
        comma_count_header = header_line.count(",")
        total_commas = sum(l.count(",") for l in raw_lines)

        if comma_count_header > 1 and total_commas > comma_count_header:
            # Comma-separated format that wraps across lines - join them
            joined = " ".join(l.strip() for l in raw_lines if l.strip())
            names_comma = _parse_names_from_header_line(joined, set())
            add_members(names_comma, True)
        elif comma_count_header > 1:
            # All on one line
            names_comma = _parse_names_from_header_line(header_line, set())
            add_members(names_comma, True)
        else:
            # Line-based format
            for line in raw_lines:
                parsed = _parse_attendance_line(line, seen)
                if parsed:
                    parsed["present"] = True
                    members.append(parsed)

    # Extract absent members
    start_a, end_a = _find_section(text, _ABSENT_HEADERS)
    names_comma_absent: list[dict] = []
    if start_a >= 0:
        raw_lines = text.split("\n")[start_a:end_a]
        header_line = raw_lines[0] if raw_lines else ""
        has_colon = ":" in header_line
        is_single_line = len(raw_lines) <= 2

        if has_colon and is_single_line:
            joined = " ".join(l.strip() for l in raw_lines if l.strip())
            names_comma_absent = _parse_names_from_header_line(joined, set())
            if names_comma_absent:
                add_members(names_comma_absent, False)
        if not names_comma_absent:
            for line in raw_lines:
                parsed = _parse_attendance_line(line, seen)
                if parsed:
                    parsed["present"] = False
                    members.append(parsed)

    return members


# ── Phase 2: Vote Extraction ─────────────────────────────────────────────────


_MOVER_RE = re.compile(
    r"(?:(?:Councilmember|Commissioner|Board\s+Member|Panel\s+member|"
    r"Member|Mayor|Vice\s+Mayor|Chairperson|Chair|Vice\s+Chair|Mr\.|Ms\.|Dr\.)\s+)?"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z']+)+)\s+"
    r"made\s+a\s+motion\s+to\s+(.*?)(?=,|\.|Seconded|\n)",
    re.I,
)

_MOVER2_RE = re.compile(
    r"(?:(?:Councilmember|Commissioner|Board\s+Member|Panel\s+member|"
    r"Member|Mayor|Vice\s+Mayor|Chairperson|Chair|Vice\s+Chair|Mr\.|Ms\.|Dr\.)\s+)?"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z']+)+)\s+"
    r"moved\s+to\s+(.*?)(?=,|\.|Seconded|\n)",
    re.I,
)

_SECONDER_RE = re.compile(
    r"seconded\s+by\s+"
    r"(?:"
    r"(?:Councilmember|Commissioner|Board\s+Member|Panel\s+member|"
    r"Member|Mayor|Vice\s+Mayor|Chairperson|Chair|Vice\s+Chair|Mr\.|Ms\.|Dr\.)\s+)?"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z']+)+)",
    re.I,
)

_RESULT_UNANIMOUS_RE = re.compile(
    r"(?:motion\s+)?(?:passed|approved|carried|adopted)\s+unanimously", re.I,
)
_RESULT_AYES_NAYS_RE = re.compile(
    r"Ayes:\s*(.*?)(?:\s*Nay:|\s*Absent:|\s*$)", re.I | re.DOTALL,
)
_RESULT_NAYS_RE = re.compile(r"Nay:\s*(.*?)(?:\s+(?=\d+\.)|\s*$)", re.I)
_RESULT_X_Y_RE = re.compile(
    r"(?:passed|approved|carried|adopted)\s+(?:by\s+)?(?:a\s+)?"
    r"(?:majority\s+)?(?:vote\s+)?(?:of\s+)?"
    r"(\d+)\s*[--]\s*(\d+)", re.I,
)
_RESULT_DISSENTING_RE = re.compile(
    r"(\d+)\s*[--]\s*(\d+)[,;]\s*(.*?)(?:dissenting|voting\s+(?:no|against))", re.I,
)
_RESULT_WITHDRAWN_RE = re.compile(r"\b(the|this)\s+item\s+was\s+withdrawn\b", re.I)
_RESULT_APPROVED_ALL_RE = re.compile(
    r"(?:motion\s+)?(?:was\s+)?approved\s+by\s+all\s+(?:Panel\s+)?members?\s+present", re.I,
)
_RESULT_FAILED_RE = re.compile(r"(?:motion\s+)?failed", re.I)


def _extract_names_from_result_text(
    result_text: str, attendance: list[dict]
) -> tuple[list[str], list[str]]:
    """Extract named members from Ayes/Nays result text."""
    yes_names: list[str] = []
    no_names: list[str] = []
    attendance_norms = {m["normalized_name"]: m["name"] for m in attendance}

    ayes_m = _RESULT_AYES_NAYS_RE.search(result_text)
    if not ayes_m:
        return yes_names, no_names

    raw_ayes = ayes_m.group(1).strip()
    for candidate in re.split(r"[,\n]+", raw_ayes):
        candidate = candidate.strip().rstrip(",;.:")
        if not candidate or len(candidate) < 3:
            continue
        c_norm = _normalize(candidate)
        if c_norm in attendance_norms:
            yes_names.append(attendance_norms[c_norm])
        else:
            for norm, canon in attendance_norms.items():
                if c_norm.startswith(norm) or norm.startswith(c_norm):
                    yes_names.append(canon)
                    break

    nays_m = _RESULT_NAYS_RE.search(result_text)
    if nays_m:
        raw_nays = nays_m.group(1).strip()
        for candidate in re.split(r"[,\n]+", raw_nays):
            candidate = candidate.strip().rstrip(",;.:")
            if not candidate or len(candidate) < 3:
                continue
            c_norm = _normalize(candidate)
            if c_norm in attendance_norms:
                no_names.append(attendance_norms[c_norm])
            else:
                for norm, canon in attendance_norms.items():
                    if c_norm.startswith(norm) or norm.startswith(c_norm):
                        no_names.append(canon)
                        break

    return yes_names, no_names


def _extract_dissenter_names(
    text: str, attendance: list[dict]
) -> list[dict]:
    """Extract dissenter names from text like 'Councilmembers Encinas, Orlando, and Poston dissenting'."""
    found: list[dict] = []
    parts = re.split(r",\s*|\s+and\s+|\s*&\s*", text)
    for part in parts:
        part = part.strip().strip(".;,:")
        if not part or len(part) < 3:
            continue
        part_clean = re.sub(
            r"\b(Councilmember|Councilmembers|Commissioner|Commissioners|"
            r"Board\s+Member|Panel\s+member|Member|Mayor|Vice\s+Mayor|"
            r"dissenting|voting\s+(?:no|against))\b",
            "", part, flags=re.I,
        ).strip()
        if not part_clean or len(part_clean) < 3:
            continue
        part_norm = _normalize(part_clean)
        for a in attendance:
            a_norm = a["normalized_name"]
            if part_norm == a_norm:
                if not any(f["normalized_name"] == a_norm for f in found):
                    found.append({"name": a["name"], "normalized_name": a_norm})
                break
            a_last = a_norm.split()[-1]
            if part_norm == a_last or part_norm.split()[-1] == a_last:
                already = any(
                    f["normalized_name"].split()[-1] == a_last for f in found
                )
                if not already:
                    found.append({"name": a["name"], "normalized_name": a_norm})
                break
    return found


def _find_chair_in_list(attendance: list[dict]) -> dict | None:
    """Find the chair from an attendance list."""
    for a in attendance:
        role = (a.get("role") or "").lower()
        if role in ("chair", "chairperson", "chairman", "mayor"):
            return a
    return None


def extract_votes(text: str, attendance: list[dict] | None = None) -> list[dict]:
    """Extract individual vote results from minutes text."""
    if attendance is None:
        attendance = extract_attendance(text)

    votes: list[dict] = []
    lines = text.split("\n")

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Withdrawn items
        if _RESULT_WITHDRAWN_RE.search(line):
            votes.append({
                "mover": None, "seconder": None, "motion_action": None,
                "result": "withdrawn", "unanimous": None,
                "vote_count": None, "named_votes": [],
                "chair_moved": False, "chair_seconded": False,
                "raw_text": line[:300],
            })
            i += 1
            continue

        # Mover patterns
        mover_match = _MOVER_RE.search(line) or _MOVER2_RE.search(line)
        if mover_match:
            mover_raw = mover_match.group(1)
            motion_action = (
                mover_match.group(2).strip()
                if len(mover_match.groups()) >= 2 else ""
            )
            mover = _strip_prefix(mover_raw, _NAME_PREFIXES)
            mover_norm = _normalize(mover)

            # Seconder
            seconder = None
            seconder_norm = None
            seconder_match = _SECONDER_RE.search(line)
            if not seconder_match:
                next_text = "\n".join(lines[i:i + 5])
                seconder_match = _SECONDER_RE.search(next_text)
            if seconder_match:
                seconder_raw = seconder_match.group(1)
                seconder = _strip_prefix(seconder_raw, _NAME_PREFIXES)
                seconder_norm = _normalize(seconder)

            # Result block
            result_block = "\n".join(lines[i:min(i + 10, len(lines))])

            result = "unknown"
            unanimous = False
            vote_count = None
            named_votes: list[dict] = []

            if _RESULT_WITHDRAWN_RE.search(result_block):
                result = "withdrawn"
                unanimous = None
            elif _RESULT_UNANIMOUS_RE.search(result_block):
                result = "approved"
                unanimous = True
                for a in attendance:
                    if a.get("present", True):
                        named_votes.append({
                            "name": a["name"],
                            "normalized_name": a["normalized_name"],
                            "vote": "yes",
                        })
            elif _RESULT_APPROVED_ALL_RE.search(result_block):
                result = "approved"
                unanimous = True
                for a in attendance:
                    if a.get("present", True):
                        named_votes.append({
                            "name": a["name"],
                            "normalized_name": a["normalized_name"],
                            "vote": "yes",
                        })
            elif _RESULT_FAILED_RE.search(result_block):
                result = "failed"
            elif _RESULT_DISSENTING_RE.search(result_block):
                dm = _RESULT_DISSENTING_RE.search(result_block)
                yes_count = int(dm.group(1))
                no_count = int(dm.group(2))
                vote_count = {"yes": yes_count, "no": no_count}
                dissenter_text = dm.group(3)
                dissenters = _extract_dissenter_names(dissenter_text, attendance)
                for d in dissenters:
                    named_votes.append({
                        "name": d["name"],
                        "normalized_name": d["normalized_name"],
                        "vote": "no",
                    })
                result = "approved" if yes_count >= no_count else "denied"
                unanimous = no_count == 0
            elif _RESULT_X_Y_RE.search(result_block):
                xm = _RESULT_X_Y_RE.search(result_block)
                yes_count = int(xm.group(1))
                no_count = int(xm.group(2))
                vote_count = {"yes": yes_count, "no": no_count}
                result = "approved" if no_count == 0 else "carried"
                unanimous = no_count == 0
                dissenter_text = result_block[xm.end():]
                dissenters = _extract_dissenter_names(dissenter_text, attendance)
                for d in dissenters:
                    named_votes.append({
                        "name": d["name"],
                        "normalized_name": d["normalized_name"],
                        "vote": "no",
                    })
            elif _RESULT_AYES_NAYS_RE.search(result_block):
                yes_names, no_names = _extract_names_from_result_text(
                    result_block, attendance
                )
                for y in yes_names:
                    named_votes.append({
                        "name": y,
                        "normalized_name": _normalize(y),
                        "vote": "yes",
                    })
                for n in no_names:
                    named_votes.append({
                        "name": n,
                        "normalized_name": _normalize(n),
                        "vote": "no",
                    })
                result = "approved" if not no_names else "carried"
                unanimous = not bool(no_names)
                vote_count = {"yes": len(yes_names), "no": len(no_names)}
            else:
                if re.search(r"approved|passed|carried|adopted", line, re.I):
                    result = "approved"
                    unanimous = True

            chair = _find_chair_in_list(attendance)
            is_chair_moved = (
                chair is not None
                and mover_norm == chair["normalized_name"]
            )
            is_chair_seconded = (
                chair is not None
                and seconder_norm is not None
                and seconder_norm == chair["normalized_name"]
            )

            votes.append({
                "mover": mover,
                "mover_normalized": mover_norm,
                "seconder": seconder,
                "seconder_normalized": seconder_norm,
                "motion_action": motion_action,
                "result": result,
                "unanimous": unanimous,
                "vote_count": vote_count,
                "named_votes": named_votes,
                "chair_moved": is_chair_moved,
                "chair_seconded": is_chair_seconded,
                "raw_text": line.strip()[:300],
            })

        i += 1

    return votes


# ── Main entry point ─────────────────────────────────────────────────────────


def parse_rollcall(text: str) -> dict[str, Any]:
    """Full roll call parse: chair + attendance + votes.

    Args:
        text: Full minutes text.

    Returns:
        {
            "chair": dict or None,
            "attendance": list[dict],
            "votes": list[dict],
            "chair_action_count": int,
            "chair_dissent_count": int,
        }
    """
    chair = extract_chair_from_header(text)
    attendance = extract_attendance(text)
    votes = extract_votes(text, attendance)

    if chair is None:
        chair_from_list = _find_chair_in_list(attendance)
        if chair_from_list:
            chair = {
                "name": chair_from_list["name"],
                "normalized_name": chair_from_list["normalized_name"],
                "role": chair_from_list.get("role"),
                "detection_method": "attendance_inferred",
            }

    chair_action_count = sum(
        1 for v in votes if v.get("chair_moved") or v.get("chair_seconded")
    )
    chair_dissent_count = 0
    if chair:
        chair_norm = chair["normalized_name"]
        for v in votes:
            for nv in v.get("named_votes", []):
                if nv.get("normalized_name") == chair_norm and nv.get("vote") == "no":
                    chair_dissent_count += 1

    return {
        "chair": chair,
        "attendance": attendance,
        "votes": votes,
        "chair_action_count": chair_action_count,
        "chair_dissent_count": chair_dissent_count,
    }
