"""Pattern tables for the pattern cascade.

Extracted from :mod:`scripts.entities.pattern_cascade` so that module stays
focused on scanning, classification and persistence.

These tables are *producer vocabulary*: which header labels the cascade reads,
which canonical relationship (if any) a role supports, and which labels are
extracted evidence rather than actors.
"""

from __future__ import annotations

import re

__all__ = [
    "BODY_PATTERNS",
    "EVIDENCE_ONLY_PATTERNS",
    "ROLE_EDGE_MAP",
    "line_field",
]


#: Roles that explicit source language supports as a *relationship*.
#:
#: Everything else stays a contextual mention.  An attorney, representative,
#: staff member, presenter or owner named in a header is *named*, not related:
#: no edge is emitted for them, because the legacy predicates they used to map to
#: (HAS_ATTORNEY / HAS_STAFF / HAS_OWNER) are not canonical emission vocabulary,
#: and naming someone does not by itself support participation.
ROLE_EDGE_MAP = {"applicant": "APPLIED_FOR"}

# ── Conservative Patterns ──────────────────────────────────────────────
# Only match labels at line-level with colon separator.
# Captured value is everything from label to end-of-line or next label.

def line_field(label):
    """Label: Value at line-level. Stops at newline or next label."""
    return re.compile(
        rf"{label}:\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )

BODY_PATTERNS: dict[str, list[tuple[str, str, re.Pattern]]] = {
    "phoenix-cc": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "representative", line_field("Representative")),
        ("text", "staff", line_field("Staff Contact")),
    ],
    # BOS — items use inline format: "Applicant & Owner: Name / Name Request: ..."
    # All labels on one line, so _line_field would capture past next label.
    # Instead, capture until the next known label or end of line.
    "bos": [
        ("text", "applicant", re.compile(
            r"Applicant(?:\s*&\s*Owner)?:\s*(.+?)(?=\s+(?:Request|Staff(?:\s+Contact)?|Site Location|Location|Commission Recommendation|Case)\s*:|\n|$)",
            re.I | re.M,
        )),
    ],
    "phoenix-pc": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "representative", line_field("Representative")),
        ("text", "staff", line_field("Staff Contact")),
    ],
    "phoenix-ti": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "representative", line_field("Representative")),
        ("text", "staff", line_field("Staff Contact")),
    ],
    "phoenix-ps": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "representative", line_field("Representative")),
        ("text", "staff", line_field("Staff Contact")),
    ],
    "phoenix-ed": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "representative", line_field("Representative")),
        ("text", "staff", line_field("Staff Contact")),
    ],
    # BOS items don't use Staff Contact: (0 matches), so no staff pattern for BOS.

    # Scottsdale — rich header format with Request:, Presenter(s):, Staff Contact(s):
    # All Scottsdale bodies share the same format; (s) is optional
    "scottsdale": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "presenter", line_field(r"Presenter(?:\(s\))?")),
        ("text", "staff", line_field(r"Staff Contact")),
    ],
    # Maricopa County-wide bodies
    "pz": [
        ("text", "applicant", line_field("Applicant")),
        ("text", "attorney", line_field("Attorney")),
        ("text", "staff", line_field("Staff Contact")),
    ],
}

#: Label patterns whose captured text is extracted *evidence*, not an actor.
#:
#: ``request`` and ``location`` describe the item being heard, not a participant
#: in it.  They are therefore never certified as knowledge-graph roles or
#: relationships; the patterns are kept here so the labels stay documented for
#: later event/outcome modeling rather than being lost.
EVIDENCE_ONLY_PATTERNS: dict[str, list[tuple[str, str, object]]] = {
    "scottsdale": [
        ("text", "request", line_field("Request")),
        ("text", "location", line_field("Location")),
    ],
}
