"""Pure, conservative context classification for Meeting Result actions.

The event extractor sees action-shaped words in both actual dispositions and
agenda, historical, and explanatory prose.  This module rejects only contexts
that provide deterministic evidence that an action is not a result of the
current meeting.  It has no database or network dependencies.
"""

from __future__ import annotations

import re


_MODAL_BEFORE = re.compile(
    r"\b(?:may|might|could|should|would|will|shall|can|is\s+expected\s+to|"
    r"is\s+scheduled\s+to|to\s+be)\s+(?:be\s+)?$",
    re.IGNORECASE,
)
_NEGATION_BEFORE = re.compile(
    r"\b(?:not|never|wasn't|was\s+not|isn't|is\s+not)\s+(?:be\s+|being\s+)?$",
    re.IGNORECASE,
)
_PRIOR_MARKER = re.compile(
    r"\b(?:previously|formerly|earlier|prior(?:ly)?|last\s+(?:meeting|month|year)|"
    r"at\s+(?:a|the)\s+(?:previous|prior|earlier)\s+meeting)\b",
    re.IGNORECASE,
)
_FUTURE_MARKER = re.compile(
    r"\b(?:at\s+(?:a|the)\s+(?:next|future)\s+meeting|at\s+a\s+later\s+date|"
    r"in\s+a\s+future\s+session)\b",
    re.IGNORECASE,
)
_AGENDA_MARKER = re.compile(
    r"\b(?:agenda|purpose|staff\s+requests?|requested\s+action|recommended\s+action|"
    r"possible\s+action|for\s+(?:discussion|consideration)(?:\s+and\s+possible\s+action)?)\b",
    re.IGNORECASE,
)
_EXECUTIVE_MARKER = re.compile(
    r"\bexecutive\s+session\b|\bdiscussion\s+or\s+consultation\b",
    re.IGNORECASE,
)
_ITEM_MARKER = re.compile(r"(?m)(?:^|\s)(?:item\s+)?(?:\d+[A-Z]?(?:\.\d+)*|[A-Z]\d+)\s*[.):]")


def _inside_balanced_quote(text: str, start: int, end: int) -> bool:
    """Return true when the action is enclosed by a quote in this evidence row."""
    for left, right in (("“", "”"), ('"', '"'), ("‘", "’"), ("'", "'")):
        opening = text.rfind(left, 0, start)
        if opening < 0:
            continue
        closing = text.find(right, end)
        if closing >= 0 and (left != right or text.count(left, opening, start + 1) % 2):
            return True
    return False


def non_current_result_reason(
    action: str,
    evidence_text: str,
    start: int,
    end: int,
) -> str | None:
    """Classify deterministic non-current-result contexts, otherwise return ``None``.

    ``start`` and ``end`` are offsets into ``evidence_text``.  The function is
    deliberately pure and never changes or translates source coordinates.
    """
    if not (0 <= start < end <= len(evidence_text)):
        return "invalid_evidence_coordinates"

    before = evidence_text[max(0, start - 100):start]
    after = evidence_text[end:min(len(evidence_text), end + 100)]
    whole = evidence_text
    normalized = re.sub(r"\s+", " ", action.casefold()).strip()

    if _inside_balanced_quote(whole, start, end):
        return "quoted_reference"
    if _NEGATION_BEFORE.search(before):
        return "negated"
    if _MODAL_BEFORE.search(before):
        return "future_or_modal"
    if _PRIOR_MARKER.search(before) or _PRIOR_MARKER.search(after):
        return "prior_reference"
    if _FUTURE_MARKER.search(before) or _FUTURE_MARKER.search(after):
        return "future_reference"

    # Generic action words inside an executive-session description are not a
    # public disposition.  An explicit outcome such as "no action" remains
    # eligible so a real result is not erased by the context guard.
    if normalized != "no action" and _EXECUTIVE_MARKER.search(whole):
        return "executive_session_description"

    # Agenda labels and requested/recommended-action prose describe what may be
    # considered, not what happened.  Strong past-tense result wording at the
    # beginning of a row is retained as a positive control.
    if normalized != "no action" and _AGENDA_MARKER.search(whole) and not re.match(
        r"^\s*(?:item\s+)?(?:\d+[A-Z]?[.):]\s*)?"
        r"(?:approved|denied|continued|tabled|adopted|received|discussed|"
        r"withdrawn|introduced|amended|sustained|vacated|extended|deferred)\b",
        whole,
        re.IGNORECASE,
    ):
        return "agenda_or_boilerplate"

    # A visual row containing multiple item labels is ambiguous unless the
    # action occurs before the second label.  Never attach a later item's result
    # to the first item's context.
    item_markers = list(_ITEM_MARKER.finditer(whole))
    if len(item_markers) > 1 and start >= item_markers[1].start():
        return "adjacent_item_ambiguous"

    return None
