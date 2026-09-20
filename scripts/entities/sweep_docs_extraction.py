"""sweep_docs_extraction.py — candidate extraction for the document sweep.

Owns the extraction patterns, the known-organization vocabulary, name
normalization and validation, and text cleaning.  It produces candidate dicts
only: no persistence, no emission validation, and no planning happen here.

``KNOWN_ORGANIZATIONS`` remains the authoritative organization vocabulary for
this producer and is re-exported by the ``sweep_docs`` facade.
"""

from __future__ import annotations

import re

from .entity_utils import (
    clean_normalized_name, normalize_entity_name, classify_entity_type,
)

__all__ = [
    "BOS_CASE_RE",
    "CASE_NUMBER_RE",
    "GENERAL_PATTERNS",
    "KNOWN_ORGANIZATIONS",
    "KNOWN_ORG_RE",
    "MAX_MATCH_LEN",
    "clean_text",
    "extract_entities_from_doc",
    "is_garbage_text",
    "normalize_name",
    "validate_name",
]


# ── Known organizations (same list as sweep_meetings.py) ────────────────

KNOWN_ORGANIZATIONS: dict[str, str] = {
    "Taylor Morrison": "developer",
    "Lennar": "developer",
    "Pulte Homes": "developer",
    "KB Home": "developer",
    "D.R. Horton": "developer",
    "Shea Homes": "developer",
    "Toll Brothers": "developer",
    "Fulton Homes": "developer",
    "Meritage Homes": "developer",
    "Richmond American Homes": "developer",
    "Beazer Homes": "developer",
    "Centex": "developer",
    "Clayton Homes": "developer",
    "Woodside Homes": "developer",
    "Ashton Woods": "developer",
    "M.D.C. Holdings": "developer",
    "LGI Homes": "developer",
    "Dream Finders Homes": "developer",
    "Landsea Homes": "developer",
    "Trilogy": "developer",
    "Viking Development": "developer",
    "SimonCRE": "developer",
    "Origis Development": "developer",
    "Plus Power": "developer",
    "Avantus": "developer",
    "Recurrent Energy": "developer",
    "RWE": "developer",
    "DCR Transmission": "developer",
    "Gust Rosenfeld": "law_firm",
    "Tiffany & Bosco": "law_firm",
    "Snell & Wilmer": "law_firm",
    "Rose Law Group": "law_firm",
    "Quarles & Brady": "law_firm",
    "Gammage & Burnham": "law_firm",
    "Burch & Cracchiolo": "law_firm",
    "May Potenza Baran & Gillespie": "law_firm",
    "Withey Morris Baugh": "law_firm",
    "Berry Riddell": "law_firm",
    "Pew & Lake": "law_firm",
    "Bergin Frakes Smalley Oberholtzer": "law_firm",
    "Smalley & Oberholtzer": "law_firm",
    "RVi Planning + Landscape Architecture": "planning_firm",
    "Logan Simpson": "planning_firm",
    "Kimley-Horn": "planning_firm",
    "Norris Design": "planning_firm",
    "Huitt-Zollars": "planning_firm",
    "EPS Group": "planning_firm",
    "Pinnacle Consulting": "planning_firm",
    "CVL Consultants": "planning_firm",
    "IPlan Consulting": "planning_firm",
    "Anderson Development Engineering": "planning_firm",
    "Keogh Engineering": "planning_firm",
    "Edifice Architecture": "planning_firm",
    "Arizona Public Service": "utility",
    "Salt River Project": "utility",
    "Southwest Gas": "utility",
    "Save Our Scottsdale": "advocacy_group",
}

KNOWN_ORG_RE = re.compile(
    r"(" + "|".join(re.escape(name) for name in KNOWN_ORGANIZATIONS) + r")",
    re.I,
)

MAX_MATCH_LEN = 100




def normalize_name(name: str, entity_type: str | None = None) -> str:
    """Normalize an entity name for dedup.
    Delegates to shared utility from entity_utils.py.
    For person entities, strips titles for cleaner merging.
    """
    if entity_type == 'person':
        return clean_normalized_name(name)
    return normalize_entity_name(name)


def validate_name(name: str) -> bool:
    if len(name) > MAX_MATCH_LEN or len(name) < 2:
        return False
    filler_starts = {"the ", "and ", "to ", "for ", "of ", "a ", "an ", "in ", "on ", "is "}
    if any(name.lower().startswith(f) for f in filler_starts):
        return False
    return True


def clean_text(text: str) -> str:
    """Pre-process pdftotext output before entity extraction.

    - Joins hyphenated line breaks ("Assess-\nment" → "Assessment")
    - Normalizes multiple blank lines to single newlines
    """
    text = re.sub(r'(\w)-\n(\w)', r'\1\2', text)
    text = re.sub(r'\n{3,}', r'\n\n', text)
    return text


def is_garbage_text(name: str) -> bool:
    """Return True if the extracted name is obviously not a real entity."""
    n = name.strip()
    if not n:
        return True

    # Section headers and boilerplate often extracted as names
    garbage_prefixes = (
        'recommendation', 'recommends', 'staff', 'applicant', 'owner',
        'attorney', 'council', 'notice', 'meeting', 'minutes',
        'attachment', 'appealed', 'occupied', 'membership',
        'announcement', 'memo', 'office', 'department', 'solicited',
        's and the', 's, and', ', and continue', ', the case',
        'by: ___', 'by: ____', 'by: _____',
    )
    n_lower = n.lower()
    for prefix in garbage_prefixes:
        if n_lower.startswith(prefix):
            return True

    # Single-word sentence fragments
    words = n.split()
    if len(words) <= 2:
        if n_lower[0].islower() and n[0].islower():
            return True
        if n_lower in ('memo', 'attendance', 'report', 'general', 'liaison',
                       'person', 'overview', 'presentation', 'summary'):
            return True

    # Looks like a truncated sentence fragment (comma + lowercase continuation)
    if n.startswith(',') and len(n) > 5:
        return True
    if n.startswith('.') and len(n) > 2:
        return True
    if n.startswith("'"):
        return True

    return False


GENERAL_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("applicant", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Applicant|Applicant/Owner|Applicant/Agent|Petitioner)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("attorney", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Attorney|Represented by|Represented By|Counsel|Representative)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("staff", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Staff Contact|Staff|Presenter|Prepared by|Contact)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
    ("owner", re.compile(
        r"^\s*(?:\d+[\.\)]\s+)?(?:Owner|Property Owner|Landowner)\s*:?\s*(.+?)(?:\n|$)",
        re.I | re.M,
    )),
]

CASE_NUMBER_RE = re.compile(
    r"\b(ZON|PLN|CU|SPR|CPA|MCP|SPL|USE|Z|P|CASE)[-\s]?\d{2,}[-]\d{2,}\b",
    re.I,
)

BOS_CASE_RE = re.compile(
    r"\bC-\d{2}-\d{2}-\d{3}-[A-Z0-9]+-[A-Z0-9]+\b", re.I,
)


# ── Extraction ──────────────────────────────────────────────────────────

def extract_entities_from_doc(
    text: str, *, rejections: list[dict] | None = None,
) -> list[dict]:
    """Run all extractors against document text. Returns list of candidate dicts.

    A candidate whose canonicalised name is blank is **not** an entity identity
    and is refused *before* any assertion is constructed, so it can never reach
    ``EntityAssertion``.  Each refusal is recorded in ``rejections`` when the
    caller supplies a list, so the candidate is counted rather than silently
    dropped.
    """
    candidates: list[dict] = []
    seen_entity_keys: set[tuple[str, str]] = set()

    text = clean_text(text)
    text_truncated = text[:8000]  # Limit to 8K chars

    def _refuse(name: str, etype: str, role: str, reason: str) -> None:
        if rejections is not None:
            rejections.append({
                "name": str(name)[:200],
                "entity_type": str(etype),
                "role": str(role),
                "reason": reason,
            })

    def _add_candidate(name: str, etype: str, role: str, confidence: int = 85):
        normalized = normalize_name(name, etype)
        if not normalized.strip() or not str(etype).strip():
            # e.g. the punctuation-only actor ".\"" normalises to the empty
            # string: refuse it here instead of building an invalid assertion.
            _refuse(name, etype, role, "blank_normalized_identity")
            return
        key = (normalized, etype)
        if key in seen_entity_keys:
            return
        seen_entity_keys.add(key)
        candidates.append({
            "name": name[:500],
            "normalized": normalized,
            "entity_type": etype,
            "role": role,
            "confidence": confidence,
            "source": "pattern",
        })

    # 1. General patterns
    for role, pattern in GENERAL_PATTERNS:
        for m in pattern.finditer(text_truncated):
            actor = m.group(1).strip()
            if validate_name(actor) and not is_garbage_text(actor):
                etype = classify_entity_type(actor)
                _add_candidate(actor, etype, role, 80)

    # 2. Known organization matching
    for m in KNOWN_ORG_RE.finditer(text_truncated):
        name = m.group(1).strip()
        etype = KNOWN_ORGANIZATIONS.get(name, "organization")
        # Recognizing an organization somewhere in a document establishes that it
        # was mentioned, not that it participated.  Emitting the quarantined
        # extractor label ``known_org`` here would (correctly) fail closed, and
        # the canonical role for "named in a document" is ``mentioned``.
        _add_candidate(name, etype, "mentioned", 95)

    # 3. Case / reference numbers
    for pattern in [CASE_NUMBER_RE, BOS_CASE_RE]:
        for m in pattern.finditer(text_truncated):
            case_str = m.group(0).strip()
            norm = normalize_name(case_str, 'case')
            if not norm.strip():
                _refuse(case_str, "case", "reference",
                        "blank_normalized_identity")
                continue
            if (norm, "case") not in seen_entity_keys:
                seen_entity_keys.add((norm, "case"))
                candidates.append({
                    "name": case_str.upper()[:500],
                    "normalized": norm,
                    "entity_type": "case",
                    "role": "reference",
                    "confidence": 95,
                    "source": "pattern",
                })

    return candidates
