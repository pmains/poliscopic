"""Pure extraction contract for base outcomes and exact qualifiers."""

from __future__ import annotations

import re
from typing import Any

from scripts.kg.registries.events import canonicalize_outcome


_QUALIFIER_EVIDENCE = {
    "with_conditions": re.compile(r"\bwith\s+conditions\b", re.I),
    "with_stipulations": re.compile(r"\bwith\s+stipulations\b", re.I),
    "subject_to": re.compile(
        r"\bsubject\s+to(?:\s+(?:conditions|stipulations))?\b", re.I
    ),
    "as_amended": re.compile(r"\bas\s+amended\b", re.I),
}


def extracted_outcome_fields(
    legacy_outcome: str, action_text: str, action_start: int
) -> dict[str, Any]:
    """Return canonical base plus additive exact qualifier evidence fields."""
    canonical = canonicalize_outcome(legacy_outcome)
    fields: dict[str, Any] = {"outcome": canonical.base}
    if canonical.qualifier is None:
        return fields
    pattern = _QUALIFIER_EVIDENCE.get(canonical.qualifier)
    match = pattern.search(action_text) if pattern else None
    if match is None:
        raise ValueError(
            f"qualified outcome {legacy_outcome!r} lacks exact "
            f"{canonical.qualifier!r} evidence"
        )
    fields.update({
        "outcome_qualifier": canonical.qualifier,
        "legacy_outcome": legacy_outcome,
        "outcome_qualifier_text": match.group(0),
        "outcome_qualifier_offset_start": action_start + match.start(),
        "outcome_qualifier_offset_end": action_start + match.end(),
    })
    return fields
