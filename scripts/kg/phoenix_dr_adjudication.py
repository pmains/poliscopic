#!/usr/bin/env python3
"""Evidence layer for the ``phoenix-dr`` repair plan.

Reads the current development database and adjudicates each candidate meeting
against stored evidence.  Nothing here writes, and nothing here invents: a
meeting is included only when stored evidence positively supports the
``phoenix-dr`` body, and is otherwise excluded with a precise reason.

The body code is a module constant, never a caller parameter.  This is
intentionally not a generic backfill: supporting a second body means a second,
reviewed module rather than a flavour argument here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

_REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "scripts"))

from sqlalchemy import Engine, text  # noqa: E402

from scripts.kg import registries as kg_registries  # noqa: E402

#: The one body code this plan is permitted to repair.  Not a parameter.
BODY_CODE = "phoenix-dr"

#: Jurisdiction the body belongs to, resolved by slug, never hardcoded by id.
JURISDICTION_SLUG = "phoenix"

#: Proposed canonical row, using the approved values.  ``slug`` is the
#: human-readable canonical slug; ``body_code`` stays the scraper's short code.
PROPOSED_BODY_NAME = "City of Phoenix Design Review Committee"
PROPOSED_BODY_SLUG = "phoenix-design-review-committee"
PROPOSED_BODY_TYPE = "Committee"

#: Code-level evidence that ``phoenix-dr`` names a real Phoenix body.
REGISTRY_EVIDENCE = (
    "scripts/scraper/jurisdictions/phoenix_planning.py: "
    "/design review committee/i -> 'phoenix-dr', 'Design Review Committee'",
    "scripts/scraper/jurisdictions/phoenix_aem.py: "
    "'phoenix-design-review' -> 'phoenix-dr'",
)

#: A title must carry this signal to be treated as the same body.
TITLE_PATTERN = re.compile(r"design\s+review\s+committee", re.IGNORECASE)

#: Exclusion reasons.  Every non-included row carries exactly one.
EXCLUSION_ALREADY_PARENTED = "meeting is already parented"
EXCLUSION_BODY_CODE = "stored body code is not the target body"
EXCLUSION_TITLE = "meeting or document title lacks the body signal"
EXCLUSION_MISSING_DOC = "no supporting document for this meeting"
EXCLUSION_DOC_BODY_MISMATCH = "supporting document body code does not match"
EXCLUSION_DOC_PAIRING = "supporting documents do not pair one-to-one with this meeting"
EXCLUSION_UNSUPPORTED_METHOD = "unsupported text extraction method"
EXCLUSION_NO_TEXT = "supporting document has no stored analysed text"
EXCLUSION_NOT_PHOENIX_HOST = "source url is not on the phoenix.gov host"
EXCLUSION_JURISDICTION = "jurisdiction could not be resolved uniquely"


class PlanError(Exception):
    """The plan is internally inconsistent or cannot be built safely."""


@dataclass(frozen=True)
class Disposition:
    """Adjudication of one candidate meeting, with its before-row fingerprints."""

    meeting_id: int
    external_meeting_id: str
    meeting_date: str | None
    meeting_title: str | None
    stored_body_code: str | None
    current_jurisdiction_id: int | None
    current_public_body_id: int | None
    document_id: int | None
    document_title: str | None
    document_url: str | None
    document_method: str | None
    document_content_hash: str | None
    document_jurisdiction_id: int | None
    document_text_chars: int | None
    extraction_count: int
    included: bool
    exclusion_reason: str | None
    meeting_fingerprint: str
    document_fingerprint: str | None


def canonical_json(value: Any) -> str:
    """Deterministic JSON, used for every fingerprint and for the plan digest."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def fingerprint(values: Mapping[str, Any]) -> str:
    """SHA-256 over a row's canonical column values."""
    return hashlib.sha256(canonical_json(dict(values)).encode("utf-8")).hexdigest()


def assert_development_target(engine: Engine) -> dict[str, Any]:
    """Refuse anything that is not the isolated development target.

    PostgreSQL must be ``poliscopic_dev`` on a non-production host.  SQLite is
    permitted because it is the isolated test engine and cannot be production.
    """
    parts = urlsplit(str(engine.url))
    dialect = engine.dialect.name
    info: dict[str, Any] = {
        "dialect": dialect,
        "host": parts.hostname,
        "port": parts.port,
        "database": (parts.path or "").lstrip("/"),
    }
    if dialect == "sqlite":
        info["tier"] = "test-isolated"
        return info
    if dialect != "postgresql":
        raise PlanError(f"unsupported dialect {dialect!r}")
    if "ondigitalocean" in (parts.hostname or ""):
        raise PlanError("refusing a production host")
    if info["database"] != "poliscopic_dev":
        raise PlanError(f"refusing database {info['database']!r}; expected poliscopic_dev")
    info["tier"] = "development"
    return info


def fetch_rows(conn, sql: str, **params: Any) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(text(sql), params).mappings().all()]


def resolve_jurisdiction(conn) -> tuple[int, str]:
    """Resolve the City of Phoenix jurisdiction id by slug, failing closed."""
    rows = fetch_rows(
        conn, "SELECT id, name, slug FROM jurisdictions WHERE slug = :slug",
        slug=JURISDICTION_SLUG,
    )
    if len(rows) != 1:
        raise PlanError(
            f"jurisdiction slug {JURISDICTION_SLUG!r} resolved to {len(rows)} rows; "
            "expected exactly one"
        )
    return int(rows[0]["id"]), str(rows[0]["name"])


def adjudicate(meeting: Mapping[str, Any], document: Mapping[str, Any] | None) -> tuple[bool, str | None]:
    """Decide whether one meeting is uniquely supportive of the ``phoenix-dr`` body.

    Fails closed: anything not positively supported by stored evidence is
    excluded with a precise reason and is never repaired on assumption.
    """
    if meeting.get("public_body_id") is not None:
        return False, EXCLUSION_ALREADY_PARENTED
    if meeting.get("body") != BODY_CODE:
        return False, EXCLUSION_BODY_CODE
    if not TITLE_PATTERN.search(str(meeting.get("meeting_title") or "")):
        return False, EXCLUSION_TITLE
    if document is None:
        return False, EXCLUSION_MISSING_DOC
    if document.get("body") != BODY_CODE:
        return False, EXCLUSION_DOC_BODY_MISMATCH
    if document.get("meeting_db_id") != meeting.get("id"):
        return False, EXCLUSION_DOC_PAIRING
    if not TITLE_PATTERN.search(str(document.get("document_title") or "")):
        return False, EXCLUSION_TITLE
    method = document.get("text_extraction_method")
    if not method or kg_registries.evidence_class_for_extraction_method(str(method)) is None:
        return False, EXCLUSION_UNSUPPORTED_METHOD
    chars = document.get("text_chars")
    if chars is None or int(chars) <= 0:
        return False, EXCLUSION_NO_TEXT
    host = (urlsplit(str(document.get("document_url") or "")).hostname or "").lower()
    if not host.endswith("phoenix.gov"):
        return False, EXCLUSION_NOT_PHOENIX_HOST
    return True, None


MEETINGS_SQL = """
SELECT m.id, m.meeting_id, m.meeting_date, m.meeting_title, m.body,
       m.jurisdiction_id, m.public_body_id, m.source_url
FROM meetings m WHERE m.body = :body ORDER BY m.id
"""

DOCS_SQL = """
SELECT sd.id, sd.meeting_db_id, sd.body, sd.document_title, sd.document_url,
       sd.text_extraction_method, sd.content_hash, sd.jurisdiction_id,
       LENGTH(COALESCE(sd.text_content, '')) AS text_chars
FROM supporting_documents sd WHERE sd.body = :body ORDER BY sd.id
"""

EXTRACTION_COUNTS_SQL = """
SELECT sd.id AS doc_id, COUNT(e.id) AS n
FROM supporting_documents sd
LEFT JOIN meeting_event_extractions e ON e.supporting_doc_id = sd.id
WHERE sd.body = :body GROUP BY sd.id
"""


def _meeting_fingerprint(m: Mapping[str, Any]) -> str:
    return fingerprint({
        "id": m["id"], "meeting_id": m["meeting_id"], "meeting_date": m["meeting_date"],
        "meeting_title": m["meeting_title"], "body": m["body"],
        "jurisdiction_id": m["jurisdiction_id"], "public_body_id": m["public_body_id"],
    })


def _document_fingerprint(d: Mapping[str, Any]) -> str:
    return fingerprint({
        "id": d["id"], "meeting_db_id": d["meeting_db_id"], "body": d["body"],
        "document_title": d["document_title"], "document_url": d["document_url"],
        "text_extraction_method": d["text_extraction_method"],
        "content_hash": d["content_hash"], "jurisdiction_id": d["jurisdiction_id"],
        "text_chars": d["text_chars"],
    })


def collect_dispositions(conn) -> list[Disposition]:
    """Read every candidate meeting and adjudicate it against its document."""
    meetings = fetch_rows(conn, MEETINGS_SQL, body=BODY_CODE)
    docs = fetch_rows(conn, DOCS_SQL, body=BODY_CODE)
    counts = {
        int(r["doc_id"]): int(r["n"])
        for r in fetch_rows(conn, EXTRACTION_COUNTS_SQL, body=BODY_CODE)
    }

    by_meeting: dict[int, list[dict[str, Any]]] = {}
    for doc in docs:
        by_meeting.setdefault(int(doc["meeting_db_id"]), []).append(doc)

    out: list[Disposition] = []
    for m in meetings:
        paired = by_meeting.get(int(m["id"]), [])
        if len(paired) == 1:
            doc: dict[str, Any] | None = paired[0]
            included, reason = adjudicate(m, doc)
        elif not paired:
            doc = None
            included, reason = adjudicate(m, None)
        else:
            # Ambiguous pairing is never resolved by guessing: keep the first
            # document for the record but exclude the row outright.
            doc = paired[0]
            included, reason = False, EXCLUSION_DOC_PAIRING

        out.append(Disposition(
            meeting_id=int(m["id"]),
            external_meeting_id=str(m["meeting_id"]),
            meeting_date=m["meeting_date"],
            meeting_title=m["meeting_title"],
            stored_body_code=m["body"],
            current_jurisdiction_id=m["jurisdiction_id"],
            current_public_body_id=m["public_body_id"],
            document_id=int(doc["id"]) if doc else None,
            document_title=doc["document_title"] if doc else None,
            document_url=doc["document_url"] if doc else None,
            document_method=doc["text_extraction_method"] if doc else None,
            document_content_hash=doc["content_hash"] if doc else None,
            document_jurisdiction_id=doc["jurisdiction_id"] if doc else None,
            document_text_chars=(
                int(doc["text_chars"]) if doc and doc["text_chars"] is not None else None
            ),
            extraction_count=counts.get(int(doc["id"]), 0) if doc else 0,
            included=included,
            exclusion_reason=reason,
            meeting_fingerprint=_meeting_fingerprint(m),
            document_fingerprint=_document_fingerprint(doc) if doc else None,
        ))
    return out
