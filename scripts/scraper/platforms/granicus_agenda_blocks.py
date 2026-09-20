#!/usr/bin/env python3
"""``granicus_agenda_blocks.py`` — positioned-block agenda helpers (pure).

``pdftotext`` flattens a page into lines, which loses two facts the Granicus
packets depend on:

* A dotted label is sometimes split across two blocks — ``4`` on one line and
  ``A`` on the next — because the PDF draws them as separate text runs.  Reading
  lines alone cannot tell ``4`` + ``A`` (one label) from ``4`` (a label) followed
  by an unrelated ``A``.
* A sentence can legitimately begin with a year or a resolution number —
  ``2026. The Board has caused the Feasibility Report to be prepared...`` — and a
  naive ``^(\\d+)\\.\\s+`` rule then records ``2026`` as an agenda item number.

This module holds the pure decisions: label reconstruction from coordinates,
resolution/year rejection, boundary classification, and the pending-label state
machine.  It performs no I/O and never guesses a letter from document order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Sequence

__all__ = [
    "ItemLabel",
    "PositionedLine",
    "PacketState",
    "BOUNDARY_NOISE",
    "BOUNDARY_PAGE",
    "BOUNDARY_SECTION",
    "BOUNDARY_SUBSTANTIVE",
    "classify_boundary",
    "is_resolution_reference",
    "is_year_like",
    "join_split_label",
    "label_from_text",
    "reject_as_item_number",
]

# ── label shapes ───────────────────────────────────────────────────────

# "4.A", "4.AA" — a dotted item label
_DOTTED_LABEL_RE = re.compile(r"^(\d{1,2})\.\s*([A-Z]{1,2})$")
# "4" or "4." alone
_NUMBER_ONLY_RE = re.compile(r"^(\d{1,2})\.?$")
# "4.A Council to take action ..." — label and title on one line
_INLINE_LABEL_RE = re.compile(r"^(\d{1,2})\.([A-Z]{1,2})\s+(\S.*)$")
# "A" or "A." alone
_LETTER_ONLY_RE = re.compile(r"^([A-Z]{1,2})\.?$")

# ── resolution / year rejection ────────────────────────────────────────

# "RES 04-26", "Resolution No. 01-26", "RES. 12-25"
_RESOLUTION_REF_RE = re.compile(
    r"\b(?:RES|RESO|RESOLUTION)\.?\s*(?:NO\.?\s*)?(\d{2}-\d{2})\b", re.IGNORECASE)
# a bare 01-26 style reference
_LOOSE_SERIES_RE = re.compile(r"\b(\d{2}-\d{2})\b")
_YEAR_LIKE_RE = re.compile(r"^(?:19|20)\d{2}$")

# A sentence that opens with a number and a period, then words.
_SENTENCE_NUMBER_RE = re.compile(r"^(\d{1,4})\.\s+(\S.*)$")


def is_year_like(token: str) -> bool:
    """``2026`` is a year, not an agenda item number."""
    return bool(_YEAR_LIKE_RE.match(str(token).strip()))


def is_resolution_reference(text: str) -> bool:
    """True if *text* carries a resolution reference such as ``RES 04-26``."""
    return bool(_RESOLUTION_REF_RE.search(text or ""))


def resolution_refs(text: str) -> list[str]:
    """Every ``NN-NN`` resolution reference in *text*, in order."""
    return [m.group(1) for m in _RESOLUTION_REF_RE.finditer(text or "")]


def reject_as_item_number(token: str, context: str = "") -> bool:
    """Should *token* be refused as an agenda item number?

    Refused when it is year-like, or when it appears as one half of a
    resolution reference in the surrounding *context*.  A genuine agenda item
    numbered ``2026`` does not exist in these packets; a bond series year does.
    """
    token = str(token).strip()
    if not token:
        return True
    if is_year_like(token):
        return True
    refs = resolution_refs(context)
    if refs and any(token == part for ref in refs for part in ref.split("-")):
        return True
    return False


def label_from_text(text: str) -> str | None:
    """The dotted label a single line states, or None. Never inferred."""
    match = _DOTTED_LABEL_RE.match((text or "").strip())
    return f"{match.group(1)}.{match.group(2)}" if match else None


# ── positioned lines ───────────────────────────────────────────────────


@dataclass(frozen=True)
class PositionedLine:
    """One visual line with its bounding box and source page."""

    text: str
    x0: float
    x1: float
    y0: float
    y1: float
    page: int = 0

    @property
    def height(self) -> float:
        return max(self.y1 - self.y0, 0.0)

    @property
    def left(self) -> float:
        return self.x0


def join_split_label(
    upper: PositionedLine,
    lower: PositionedLine,
    *,
    x_tolerance: float = 6.0,
    gap_ratio: float = 1.6,
) -> str | None:
    """Join ``4`` above ``A`` into ``4.A`` when the blocks align and are adjacent.

    Both halves must be pure label fragments and must sit on the same page.
    Alignment is by left edge within *x_tolerance*; adjacency is a vertical gap
    no larger than *gap_ratio* line heights.  Anything else returns None — a
    letter is never taken from document order.
    """
    if upper.page != lower.page:
        return None
    number = _NUMBER_ONLY_RE.match((upper.text or "").strip())
    letter = _LETTER_ONLY_RE.match((lower.text or "").strip())
    if not number or not letter:
        return None
    if abs(upper.left - lower.left) > x_tolerance:
        return None
    # Adjacency is measured between line STARTS, not between upper.y1 and
    # lower.y0: PDF line boxes routinely overlap a little because the ascender
    # and descender space of neighbouring lines interleave.  Using the box edges
    # rejects a genuine pair whose boxes overlap by a point.
    delta = lower.y0 - upper.y0
    if delta <= 0:
        return None
    limit = max(upper.height, lower.height, 1.0) * gap_ratio
    if delta > limit:
        return None
    return f"{number.group(1)}.{letter.group(1)}"


def group_words_into_lines(words: Iterable[Sequence], *, y_tolerance: float = 3.0) -> list[PositionedLine]:
    """Group PyMuPDF ``page.get_text("words")`` tuples into visual lines.

    Each word tuple is ``(x0, y0, x1, y1, text, block, line, word_no)``.
    """
    rows: list[list[Sequence]] = []
    for word in sorted(words, key=lambda w: (int(w[6]) if len(w) > 6 else 0,
                                             round(float(w[1]), 1), float(w[0]))):
        placed = False
        for row in rows:
            if abs(float(word[1]) - float(row[0][1])) <= y_tolerance:
                row.append(word)
                placed = True
                break
        if not placed:
            rows.append([word])
    lines: list[PositionedLine] = []
    for row in rows:
        ordered = sorted(row, key=lambda w: float(w[0]))
        text = " ".join(str(w[4]) for w in ordered)
        lines.append(PositionedLine(
            text=text,
            x0=min(float(w[0]) for w in ordered),
            x1=max(float(w[2]) for w in ordered),
            y0=min(float(w[1]) for w in ordered),
            y1=max(float(w[3]) for w in ordered),
            page=int(ordered[0][5]) if len(ordered[0]) > 5 else 0,
        ))
    return sorted(lines, key=lambda ln: (ln.page, ln.y0, ln.x0))


def replace_split_labels(lines: Sequence[PositionedLine]) -> list[PositionedLine]:
    """Fold every adjacent ``number`` + ``letter`` pair into one dotted label.

    The returned lines keep order; a joined pair collapses to a single line
    whose text is the label.  Unpaired fragments pass through untouched.
    """
    out: list[PositionedLine] = []
    index = 0
    while index < len(lines):
        current = lines[index]
        nxt = lines[index + 1] if index + 1 < len(lines) else None
        joined = join_split_label(current, nxt) if nxt is not None else None
        if joined:
            out.append(PositionedLine(joined, current.x0, max(current.x1, nxt.x1),
                                      current.y0, nxt.y1, current.page))
            index += 2
            continue
        out.append(current)
        index += 1
    return out


# ── boundaries ─────────────────────────────────────────────────────────

BOUNDARY_PAGE = "page"
BOUNDARY_SECTION = "section"
BOUNDARY_NOISE = "noise"
BOUNDARY_SUBSTANTIVE = "substantive"

_PAGE_MARKERS = ("page ", "page:", "of 1", "packet page")
_SECTION_MARKERS = (
    "CALL TO ORDER", "ROLL CALL", "ADJOURNMENT", "CONSENT AGENDA",
    "NON CONSENT", "EXECUTIVE SESSION", "PUBLIC COMMENT",
    "PRESENTATIONS", "ACTION ITEMS", "NEW BUSINESS",
)
_ACTION_PREFIXES = (
    "Council to ", "Board to ", "Board of Directors to ", "Commission to ",
    "Councilmember", "Staff to ", "No action was taken", "Presentation",
    "Discussion", "Approval of", "Recommendation", "Public hearing",
)


def classify_boundary(text: str) -> str:
    """Classify a line as page furniture, a section header, noise, or substance."""
    stripped = (text or "").strip()
    if not stripped:
        return BOUNDARY_NOISE
    upper = stripped.upper()
    if upper.isdigit() and len(stripped) <= 3:
        return BOUNDARY_PAGE
    lowered = stripped.lower()
    if any(marker in lowered for marker in _PAGE_MARKERS) and len(stripped) < 40:
        return BOUNDARY_PAGE
    if any(marker in upper for marker in _SECTION_MARKERS) and len(stripped) < 80:
        return BOUNDARY_SECTION
    if stripped.startswith(_ACTION_PREFIXES):
        return BOUNDARY_SUBSTANTIVE
    if len(stripped) >= 40:
        return BOUNDARY_SUBSTANTIVE
    return BOUNDARY_NOISE


class PacketState:
    """The pending-label state machine for a packet's item listing.

    A label seen on its own waits for the next substantive line to become its
    title.  If a boundary intervenes — a page marker, a section header, or noise
    — the pending label is emitted as a held placeholder instead of silently
    swallowing the following unrelated text.  A pending label never borrows a
    letter or a title from document order.
    """

    def __init__(self, meeting_id: str) -> None:
        self.meeting_id = meeting_id
        self.pending: str | None = None
        self.items: list[dict] = []
        self.held: list[dict] = []
        self._order = 0

    def _emit(self, number: str, title: str, raw: str, category: str = "item") -> None:
        self._order += 1
        self.items.append({
            "meeting_id": self.meeting_id,
            "agenda_item_number": number,
            "item_type_category": category,
            "section_level": 1 if category == "section" else 0,
            "agenda_item_title": title,
            "agenda_item_text": raw or title,
            "sort_order": self._order,
        })

    def _hold(self, number: str, reason: str, page: int | None = None) -> None:
        self.held.append({
            "meeting_id": self.meeting_id,
            "agenda_item_number": number,
            "reason": reason,
            "page": page,
        })

    def flush(self, reason: str) -> None:
        """A boundary arrived: the pending label gets no title from it."""
        if self.pending is not None:
            self._hold(self.pending, reason)
            self._emit(self.pending, "(see details)", "(see details)")
            self.pending = None

    def feed(self, text: str, page: int | None = None) -> None:
        """Feed one line."""
        stripped = (text or "").strip()
        if not stripped:
            return

        # A prose line numbered by a year or a resolution half is neither an
        # item number nor a title.  It must not be absorbed by a pending label:
        # "2026. The Board has caused the Feasibility Report..." is packet prose,
        # and treating it as 2.B's title is exactly how the spurious item was
        # born.  Checked before boundaries so it beats the length heuristic.
        sentence = _SENTENCE_NUMBER_RE.match(stripped)
        if sentence and reject_as_item_number(sentence.group(1), stripped):
            self.flush("prose numbered by year or resolution")
            return

        boundary = classify_boundary(stripped)

        if boundary == BOUNDARY_PAGE:
            self.flush("page boundary")
            return
        if boundary == BOUNDARY_SECTION:
            self.flush("section boundary")
            self._emit("", stripped.rstrip(".").strip(), stripped, category="section")
            return

        # Label and title on one line: emit at once, exactly as the text-line
        # parser did, so upstream behaviour is unchanged for these shapes.
        inline = _INLINE_LABEL_RE.match(stripped)
        if inline:
            self.flush("label and title on one line")
            self._emit(f"{inline.group(1)}.{inline.group(2)}",
                       inline.group(3).strip(), stripped)
            return

        label = label_from_text(stripped)
        if label:
            self.flush("consecutive label")
            self.pending = label
            return

        if self.pending is not None:
            if boundary == BOUNDARY_SUBSTANTIVE:
                number, self.pending = self.pending, None
                self._emit(number, stripped, stripped)
            else:
                self.flush("noise before title")
            return
