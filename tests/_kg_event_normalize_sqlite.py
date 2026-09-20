"""Isolated in-memory SQLite fixture for the event-normalization read adapter.

Not a test module (the leading underscore keeps pytest from collecting it): it
holds the throwaway schema and the row builders that the adapter tests compose.

Nothing here touches the dev or production databases; every engine is a fresh
in-memory SQLite database that dies with the test.
"""

from __future__ import annotations

from sqlalchemy import create_engine, event, text

DEFAULT_TEXT = "The Council approved the item 5-0."
DEFAULT_MEETING_SOURCE = "2024-03-05-CC"
SOURCE_BYTES_HASH = "SOURCE-BYTES-HASH"

_SCHEMA = {
    "jurisdictions":
        "id INTEGER PRIMARY KEY, name TEXT, slug TEXT",
    "public_bodies":
        "id INTEGER PRIMARY KEY, jurisdiction_id INTEGER, name TEXT, slug TEXT",
    "meetings":
        "id INTEGER PRIMARY KEY, body TEXT, meeting_id TEXT, "
        "public_body_id INTEGER, jurisdiction_id INTEGER",
    "supporting_documents":
        "id INTEGER PRIMARY KEY, meeting_id TEXT, meeting_db_id INTEGER, "
        "text_content TEXT, text_extraction_method TEXT, content_hash TEXT, "
        "jurisdiction_id INTEGER",
    "meeting_event_types":
        "id INTEGER PRIMARY KEY, slug TEXT, event_type TEXT",
    "meeting_events":
        "id INTEGER PRIMARY KEY, meeting_id TEXT, supporting_doc_id INTEGER, "
        "event_type_id INTEGER, outcome TEXT, action_verb TEXT, "
        "text_offset_start INTEGER, text_offset_end INTEGER, case_number TEXT",
    "meeting_event_extractions":
        "id INTEGER PRIMARY KEY, meeting_event_id INTEGER, extractor TEXT, "
        "extractor_version TEXT, confidence REAL, supporting_doc_id INTEGER, "
        "action_verb TEXT, text_offset_start INTEGER, text_offset_end INTEGER, "
        "case_number TEXT, quarantined_at TIMESTAMP, quarantine_reason TEXT, quarantined_by TEXT, decision_id TEXT, model_version TEXT",
}


def build_engine():
    """A throwaway in-memory SQLite database shaped like the civic chain.

    The connect/begin hooks below are SQLAlchemy's documented pysqlite recipe.
    Without them the driver's implicit BEGIN handling makes ``SAVEPOINT`` and
    ``RELEASE SAVEPOINT`` leak work that the enclosing transaction should still
    control, so a nested commit could survive an outer rollback.  PostgreSQL
    behaves correctly without this; the hooks make the SQLite stand-in honour the
    same all-or-nothing guarantee instead of silently disagreeing with it.
    """
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _disable_implicit_begin(dbapi_connection, connection_record):
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _emit_explicit_begin(conn):
        conn.exec_driver_sql("BEGIN")

    with engine.begin() as conn:
        for table, columns in _SCHEMA.items():
            conn.execute(text(f"CREATE TABLE {table} ({columns})"))
    return engine


def insert(engine, table, values, shared=False):
    """Insert a row; ``shared`` reference rows are inserted at most once."""
    if shared:
        with engine.connect() as conn:
            found = conn.execute(
                text(f"SELECT 1 FROM {table} WHERE id = :row_id"),
                {"row_id": values["id"]},
            ).fetchone()
        if found is not None:
            return
    columns = ", ".join(values)
    marks = ", ".join(f":{name}" for name in values)
    with engine.begin() as conn:
        conn.execute(text(f"INSERT INTO {table} ({columns}) VALUES ({marks})"), values)


def add_jurisdiction(engine, jid=1):
    insert(engine, "jurisdictions",
           {"id": jid, "name": "Tempe", "slug": "tempe"}, shared=True)


def add_body(engine, bid=1, jid=1):
    insert(engine, "public_bodies", {
        "id": bid, "jurisdiction_id": jid, "name": "Tempe City Council",
        "slug": "tempe-cc",
    }, shared=True)


def add_event_type(engine, tid=1, slug="decision.approval"):
    insert(engine, "meeting_event_types", {
        "id": tid, "slug": slug, "event_type": "approval",
    }, shared=True)


def add_meeting(engine, mid=1, body_id=1, meeting_source=DEFAULT_MEETING_SOURCE):
    insert(engine, "meetings", {
        "id": mid, "body": "CC", "meeting_id": meeting_source,
        "public_body_id": body_id, "jurisdiction_id": 1,
    })


def add_doc(engine, did=1, meeting_db_id=1, meeting_source=DEFAULT_MEETING_SOURCE,
            text_content=DEFAULT_TEXT, method="pdftotext",
            content_hash=SOURCE_BYTES_HASH):
    insert(engine, "supporting_documents", {
        "id": did, "meeting_id": meeting_source, "meeting_db_id": meeting_db_id,
        "text_content": text_content, "text_extraction_method": method,
        "content_hash": content_hash, "jurisdiction_id": 1,
    })


def add_event(engine, eid=1, meeting_source=DEFAULT_MEETING_SOURCE, doc_id=1,
              type_id=1, outcome="approved", action_verb="approved",
              span_start=None, span_end=None, case_number=None):
    insert(engine, "meeting_events", {
        "id": eid, "meeting_id": meeting_source, "supporting_doc_id": doc_id,
        "event_type_id": type_id, "outcome": outcome, "action_verb": action_verb,
        "text_offset_start": span_start, "text_offset_end": span_end,
        "case_number": case_number,
    })


def add_extraction(engine, xid=1, doc_id=1, action_verb="approved", span_start=None,
                   span_end=None, case_number=None, extractor="pattern",
                   extractor_version="v1", confidence=0.9, event_id=None):
    insert(engine, "meeting_event_extractions", {
        "id": xid, "meeting_event_id": event_id, "extractor": extractor,
        "extractor_version": extractor_version, "confidence": confidence,
        "supporting_doc_id": doc_id, "action_verb": action_verb,
        "text_offset_start": span_start, "text_offset_end": span_end,
        "case_number": case_number,
    })


def seed(engine, **kw):
    """Build one complete civic chain plus one extraction row.

    Every link can be pointed at a non-existent row to exercise the typed
    read-failure paths, and ``linked``/``insert_event`` control whether an
    existing event row is present, dangling, or absent.
    """
    jid, bid, mid = kw.get("jid", 1), kw.get("bid", 1), kw.get("mid", 1)
    did, tid, xid = kw.get("did", 1), kw.get("tid", 1), kw.get("xid", 1)
    meeting_source = kw.get("meeting_source", DEFAULT_MEETING_SOURCE)

    add_jurisdiction(engine, jid=jid)
    add_body(engine, bid=bid, jid=kw.get("body_jid", jid))
    add_meeting(engine, mid=mid, body_id=kw.get("meeting_body_id", bid),
                meeting_source=meeting_source)
    add_doc(engine, did=did, meeting_db_id=kw.get("doc_meeting_db_id", mid),
            meeting_source=kw.get("doc_meeting_source", meeting_source),
            text_content=kw.get("text_content", DEFAULT_TEXT),
            method=kw.get("method", "pdftotext"),
            content_hash=kw.get("doc_content_hash", SOURCE_BYTES_HASH))
    add_event_type(engine, tid=tid, slug=kw.get("slug", "decision.approval"))

    event_id = 1 if kw.get("linked", False) else None
    if event_id is not None and kw.get("insert_event", True):
        add_event(engine, eid=event_id,
                  meeting_source=kw.get("event_meeting_source", meeting_source),
                  doc_id=did, type_id=tid, outcome=kw.get("outcome", "approved"),
                  action_verb=kw.get("event_action_verb", "approved"),
                  span_start=kw.get("stored_span_start"),
                  span_end=kw.get("stored_span_end"),
                  case_number=kw.get("stored_case_number"))
    add_extraction(engine, xid=xid, doc_id=kw.get("extraction_doc_id", did),
                   action_verb=kw.get("action_verb", "approved"),
                   span_start=kw.get("span_start"), span_end=kw.get("span_end"),
                   case_number=kw.get("case_number"),
                   extractor=kw.get("extractor", "pattern"),
                   extractor_version=kw.get("extractor_version", "v1"),
                   confidence=kw.get("confidence", 0.9), event_id=event_id)
    return {"jid": jid, "bid": bid, "mid": mid, "did": did, "tid": tid,
            "xid": xid, "event_id": event_id}
