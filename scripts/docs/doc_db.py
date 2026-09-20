"""
Database query and write helpers for the document downloader.

Provides:
- ``fetch_batch()`` / ``fetch_by_ids()`` — fetch documents to process.
- ``write_result()`` — persist a single extraction result.
- ``print_status()`` — full text-extraction status summary for the CLI.
- ``get_pending_count()`` / ``get_failed_count()`` — quick head counts.

All functions use the project's ``db.get_session()`` for connection
management, so they work with PostgreSQL, SQLite, or any other
configured backend.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from db import get_session
from docs.doc_constants import FAILURE_METHODS

log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
#  WHERE-clause builders
# ═══════════════════════════════════════════════════════════════════════════


def _pdf_where_clause() -> str:
    """Return an SQL WHERE-snippet matching rows that look like PDFs.

    The file_extension column has inconsistent values across scrapers:
    ``'pdf'``, ``'.pdf'``, ``'PDF'``, or even NULL/empty for old imports.
    This clause catches all of them.
    """
    return (
        "(LOWER(file_extension) IN ('pdf', '.pdf') "
        "OR file_extension IS NULL OR file_extension = '')"
    )


def _jurisdiction_join(jurisdiction: Optional[str]) -> str:
    """Return an SQL JOIN fragment that restricts to *jurisdiction*.

    When *jurisdiction* is None the fragment is empty (no filter).

    If *jurisdiction* is a plain integer (e.g. ``"22"``) it is treated as
    a jurisdiction ID and matched exactly.  If it is a name string it is
    sanitised to prevent SQL injection (alphanumeric, spaces, hyphens,
    parens) and matched case-insensitively via LIKE.
    """
    if not jurisdiction:
        return ""
    if jurisdiction.strip().isdigit():
        return (
            "\n        JOIN meetings m ON sd.meeting_db_id = m.id"
            "\n        JOIN jurisdictions j ON m.jurisdiction_id = j.id"
            "\n        AND j.id = :jurisdiction_id"
        )
    safe = "".join(
        c for c in jurisdiction
        if c.isalnum() or c.isspace() or c in ("-", "(", ")")
    )
    return (
        "\n        JOIN meetings m ON sd.meeting_db_id = m.id"
        "\n        JOIN jurisdictions j ON m.jurisdiction_id = j.id"
        "\n        AND LOWER(j.name) LIKE LOWER(:jurisdiction_filter)"
    )


# ═══════════════════════════════════════════════════════════════════════════
#  Quick-count helpers
# ═══════════════════════════════════════════════════════════════════════════


def get_pending_count() -> int:
    """Count documents that still need text extraction."""
    session = get_session()
    try:
        return session.execute(
            text(
                f"SELECT COUNT(*) FROM supporting_documents\n"
                f"WHERE (text_content IS NULL OR text_content = '')\n"
                f"  AND {_pdf_where_clause()}\n"
                f"  AND (text_extraction_method IS NULL\n"
                f"       OR text_extraction_method NOT IN {FAILURE_METHODS})"
            )
        ).scalar()
    finally:
        session.close()


def get_failed_count() -> int:
    """Count documents previously marked as failed (all failure types)."""
    session = get_session()
    try:
        return session.execute(
            text(
                f"SELECT COUNT(*) FROM supporting_documents\n"
                f"WHERE text_extraction_method IN {FAILURE_METHODS}"
            )
        ).scalar()
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════
#  Fetch helpers
# ═══════════════════════════════════════════════════════════════════════════


def fetch_batch(
    limit: int,
    retry_failed: bool = False,
    jurisdiction: Optional[str] = None,
    method_filter: Optional[str] = None,
    exclude_method: Optional[str] = None,
) -> list[dict]:
    """Fetch a batch of ``supporting_documents`` rows to process.

    Parameters
    ----------
    limit : int
        Maximum number of rows to return.  Rows are selected at random.
    retry_failed : bool
        If true, fetch rows whose ``text_extraction_method`` is one of
        ``FAILURE_METHODS``.  Ignored when *method_filter* is given.
    jurisdiction : str or None
        If given, only rows whose meeting belongs to a jurisdiction whose
        name contains this string (case-insensitive LIKE) are returned.
    method_filter : str or None
        Comma-separated list of ``text_extraction_method`` values to
        include.  Use ``"null"`` (or ``"untouched"``) for rows never
        attempted.  Overrides *retry_failed*.
    exclude_method : str or None
        Comma-separated list of ``text_extraction_method`` values to
        exclude.  Same syntax as *method_filter* but applied as an
        AND NOT condition.

    Returns
    -------
    list[dict]
        Each dict has keys ``id``, ``document_url``, ``document_title``,
        ``file_extension``, ``meeting_db_id``, ``text_extraction_method``.
    """
    session = get_session()
    order_clause = "ORDER BY RANDOM()"
    method_where = _build_method_filter(retry_failed, method_filter)
    exclude_where = _build_exclude_filter(exclude_method)
    jurisdiction_param = _build_jurisdiction_param(jurisdiction)

    sql = (
        f"SELECT sd.id, sd.document_url, sd.document_title, sd.file_extension,\n"
        f"       sd.meeting_db_id, sd.text_extraction_method\n"
        f"FROM supporting_documents sd\n"
        f"{_jurisdiction_join(jurisdiction)}\n"
        f"WHERE sd.document_url IS NOT NULL AND sd.document_url != ''\n"
        f"  AND {_pdf_where_clause()}\n"
        f"  {method_where}\n"
        f"  {exclude_where}\n"
        f"{order_clause}\n"
        f"LIMIT :limit"
    )

    try:
        rows = session.execute(text(sql), jurisdiction_param | {"limit": limit}).fetchall()
    except Exception:
        # Fallback: Postgres uses RANDOM() instead of SQLite's RANDOM()
        order_clause = "ORDER BY RANDOM()"
        sql = sql.replace("ORDER BY RANDOM()", order_clause)
        session.rollback()
        rows = session.execute(text(sql), jurisdiction_param | {"limit": limit}).fetchall()

    result = [dict(row._mapping) for row in rows]
    session.close()
    return result


def fetch_retry_priority(
    limit: int,
    max_attempts: int = 3,
    backoff_hours: int = 24,
    include_extraction_failed: bool = False,
) -> list[dict]:
    """Fetch previously-failed documents in retry-priority order.

    Not all failures are created equal.  Ordering (most likely to
    succeed first):

      1. ``download_failed`` within the last 7 days — transient network
         blips (CDN timeouts, hiccups) that usually recover on retry.
      2. ``process_error`` — genuine processing errors (the varchar-32
         overflow mislabel is reclassified away by
         ``scripts/docs/reclassify_process_error.py``, so what remains
         is real).
      3. older ``download_failed`` — lower priority; may be dead links.
      4. ``extraction_failed`` — only when *include_extraction_failed*
         is True.  Normally NOT retried: the extraction stack has not
         changed, so a corrupt/scanned PDF will fail the same way.

    Attempt gating: rows whose ``extraction_attempts`` have reached
    *max_attempts* are skipped.  Backoff: rows whose last attempt
    (``text_extracted_at``) is newer than *backoff_hours* are skipped,
    so a failure is not hammered again the same day.

    Returns
    -------
    list[dict]
        Each dict has keys ``id``, ``document_url``, ``document_title``,
        ``file_extension``, ``meeting_db_id``, ``text_extraction_method``.
    """
    from datetime import timedelta

    methods = ["download_failed", "process_error"]
    if include_extraction_failed:
        methods.append("extraction_failed")
    in_list = ", ".join(f"'{m}'" for m in methods)
    backoff_cutoff = datetime.now(timezone.utc) - timedelta(hours=backoff_hours)
    recent_cutoff = datetime.now(timezone.utc) - timedelta(days=7)

    sql = (
        f"SELECT sd.id, sd.document_url, sd.document_title, sd.file_extension, "
        f"       sd.meeting_db_id, sd.text_extraction_method\n"
        f"FROM supporting_documents sd\n"
        f"WHERE sd.document_url IS NOT NULL AND sd.document_url != ''\n"
        f"  AND {_pdf_where_clause()}\n"
        f"  AND (sd.text_content IS NULL OR sd.text_content = '')\n"
        f"  AND sd.text_extraction_method IN ({in_list})\n"
        f"  AND sd.extraction_attempts < :max_attempts\n"
        f"  AND (sd.text_extracted_at IS NULL OR sd.text_extracted_at < :backoff_cutoff)\n"
        f"ORDER BY CASE\n"
        f"    WHEN sd.text_extraction_method = 'download_failed'\n"
        f"         AND sd.text_extracted_at >= :recent_cutoff THEN 0\n"
        f"    WHEN sd.text_extraction_method = 'process_error' THEN 1\n"
        f"    WHEN sd.text_extraction_method = 'download_failed' THEN 2\n"
        f"    ELSE 3\n"
        f"  END,\n"
        f"  sd.text_extracted_at DESC NULLS LAST\n"
        f"LIMIT :limit"
    )
    session = get_session()
    try:
        rows = session.execute(
            text(sql),
            {
                "limit": limit,
                "max_attempts": max_attempts,
                "backoff_cutoff": backoff_cutoff,
                "recent_cutoff": recent_cutoff,
            },
        ).fetchall()
        return [dict(r._mapping) for r in rows]
    finally:
        session.close()


def _build_method_filter(
    retry_failed: bool, method_filter: Optional[str]
) -> str:
    """Build the WHERE snippet for the *method_filter* / *retry_failed* logic."""
    if method_filter:
        conditions = _split_method_values(method_filter, equals_mode=True)
        clause = " OR ".join(conditions)
        if len(conditions) > 1:
            clause = f"({clause})"
        return (
            f"\n  AND (sd.text_content IS NULL OR sd.text_content = '')"
            f"\n  AND {clause}"
        )
    if retry_failed:
        return f"\n  AND sd.text_extraction_method IN {FAILURE_METHODS}"
    return (
        f"\n  AND (sd.text_content IS NULL OR sd.text_content = '')"
        f"\n  AND (sd.text_extraction_method IS NULL"
        f"\n       OR sd.text_extraction_method NOT IN {FAILURE_METHODS})"
    )


def _build_exclude_filter(exclude_method: Optional[str]) -> str:
    """Build the WHERE snippet for the *exclude_method* logic."""
    if not exclude_method:
        return ""
    conditions = _split_method_values(exclude_method, equals_mode=False)
    if not conditions:
        return ""
    return " AND " + " AND ".join(conditions)


def _split_method_values(raw: str, *, equals_mode: bool) -> list[str]:
    """Parse a comma-separated method string into SQL conditions.

    Parameters
    ----------
    raw : str
        e.g. ``"null,extraction_failed"``
    equals_mode : bool
        When True, generate ``IS NULL`` / ``= 'value'`` (for inclusion).
        When False, generate ``IS NOT NULL`` / ``!= 'value'`` (for exclusion).
    """
    parts = [p.strip() for p in raw.split(",")]
    conditions: list[str] = []
    for p in parts:
        if p.lower() in ("null", "untouched"):
            conditions.append(
                f"sd.text_extraction_method IS{' NOT' if not equals_mode else ''} NULL"
            )
        elif p.lower() == "quarantine":
            op = "LIKE" if equals_mode else "NOT LIKE"
            conditions.append(f"sd.text_extraction_method {op} 'quarantine:%'")
        else:
            escaped = p.replace("'", "''")
            op = "=" if equals_mode else "!="
            conditions.append(f"sd.text_extraction_method {op} '{escaped}'")
    return conditions


def _build_jurisdiction_param(jurisdiction: Optional[str]) -> dict:
    """Build the parameter dict for the jurisdiction clause.

    Returns ``{"jurisdiction_id": <int>}`` when *jurisdiction* is numeric,
    or ``{"jurisdiction_filter": "%<safe>%"}`` when it is a name string.
    Returns an empty dict when *jurisdiction* is None.
    """
    if not jurisdiction:
        return {}
    if jurisdiction.strip().isdigit():
        return {"jurisdiction_id": int(jurisdiction.strip())}
    safe = "".join(
        c for c in jurisdiction
        if c.isalnum() or c.isspace() or c in ("-", "(", ")")
    )
    return {"jurisdiction_filter": f"%{safe}%"}


def fetch_by_ids(ids: list[int]) -> list[dict]:
    """Fetch specific ``supporting_documents`` rows by primary-key *ids*.

    Does NOT apply the PDF-extension filter — call this only when you
    already know the IDs are worth processing (e.g. from ``--ids`` CLI).
    """
    session = get_session()
    try:
        rows = session.execute(
            text(
                "SELECT id, document_url, document_title, file_extension,\n"
                "       meeting_db_id, text_extraction_method\n"
                "FROM supporting_documents\n"
                "WHERE id = ANY(:ids)"
            ),
            {"ids": ids},
        ).fetchall()
        return [dict(row._mapping) for row in rows]
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════
#  Write helpers
# ═══════════════════════════════════════════════════════════════════════════


def write_result(result: dict) -> None:
    """Persist a single extraction *result* to the database.

    Called immediately after each document finishes so that a crash (or
    user interrupt) loses at most one in-flight doc rather than the
    entire batch.

    Handles three cases:
    - **Success**: text extracted, written to ``text_content``.
    - **Quarantine**: doc downloaded but flagged; stores the quarantine
      reason in ``text_extraction_method`` (e.g. ``quarantine:oversized``)
      without setting ``text_content``.
    - **Failure**: stores the failure reason and empty text.
    """
    session = get_session()
    now = datetime.now(timezone.utc).isoformat()
    duration = result.get("duration_ms", 0)
    method = result.get("method")
    doc_id = result["id"]
    try:
        # ── Quarantine: method starts with "quarantine:" ──
        if method and method.startswith("quarantine:"):
            session.execute(
                text(
                    "UPDATE supporting_documents\n"
                    "SET text_extracted_at = :now,\n"
                    "    text_extraction_method = :method,\n"
                    "    extraction_duration_ms = :duration,\n"
                    "    local_path = CASE\n"
                    "        WHEN :local_path IS NOT NULL\n"
                    "        AND (local_path IS NULL OR local_path = '')\n"
                    "        THEN :local_path ELSE local_path END\n"
                    "WHERE id = :id"
                ),
                {
                    "now": now,
                    "method": method,
                    "duration": duration,
                    "local_path": result.get("local_path"),
                    "id": doc_id,
                },
            )
        # ── Reject: method starts with "reject:" ──
        elif method and method.startswith("reject:"):
            session.execute(
                text(
                    "UPDATE supporting_documents\n"
                    "SET text_extracted_at = :now,\n"
                    "    text_extraction_method = :method\n"
                    "WHERE id = :id"
                ),
                {
                    "now": now,
                    "method": method,
                    "id": doc_id,
                },
            )
        # ── Success: text was extracted ──
        elif result.get("text") and method:
            # PostgreSQL rejects NUL (0x00) bytes in TEXT columns.
            # Some PDFs embed them; strip before writing.
            clean_text = result["text"].replace("\x00", "")
            session.execute(
                text(
                    "UPDATE supporting_documents\n"
                    "SET text_content = :text_content,\n"
                    "    text_extracted_at = :now,\n"
                    "    text_extraction_method = :method,\n"
                    "    extraction_duration_ms = :duration,\n"
                    "    extraction_attempts = 0,\n"
                    "    content_hash = CASE\n"
                    "        WHEN content_hash IS NULL OR content_hash = ''\n"
                    "        THEN :content_hash ELSE content_hash END,\n"
                    "    local_path = CASE\n"
                    "        WHEN :local_path IS NOT NULL\n"
                    "        AND (local_path IS NULL OR local_path = '')\n"
                    "        THEN :local_path ELSE local_path END\n"
                    "WHERE id = :id"
                ),
                {
                    "text_content": clean_text,
                    "now": now,
                    "method": method,
                    "duration": duration,
                    "content_hash": result.get("content_hash"),
                    "local_path": result.get("local_path"),
                    "id": doc_id,
                },
            )
        # ── Failure ──
        else:
            fail_method = result.get("error", "extraction_failed")
            session.execute(
                text(
                    "UPDATE supporting_documents\n"
                    "SET text_content = '',\n"
                    "    text_extracted_at = :now,\n"
                    "    text_extraction_method = :fail_method,\n"
                    "    extraction_duration_ms = :duration,\n"
                    "    extraction_attempts = extraction_attempts + 1\n"
                    "WHERE id = :id"
                ),
                {
                    "now": now,
                    "duration": duration,
                    "fail_method": fail_method,
                    "id": doc_id,
                },
            )
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ═══════════════════════════════════════════════════════════════════════════
#  Status-reporting
# ═══════════════════════════════════════════════════════════════════════════


def print_status() -> None:
    """Print a full status report of text-extraction progress to stdout.

    Includes per-jurisdiction breakdowns, failure counts by type, and
    the age distribution of remaining pending documents.
    """
    session = get_session()
    try:
        total = session.execute(text("SELECT COUNT(*) FROM supporting_documents")).scalar()
        with_text = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_content IS NOT NULL AND text_content != ''"
            )
        ).scalar()
        pct = round(with_text / total * 100) if total else 0

        failed_dl = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_extraction_method = 'download_failed'"
            )
        ).scalar()
        failed_extract = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_extraction_method = 'failed'"
            )
        ).scalar()
        failed_proc = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_extraction_method = 'process_error'"
            )
        ).scalar()
        failed_extract_explicit = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_extraction_method = 'extraction_failed'"
            )
        ).scalar()
        quarantined = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_extraction_method LIKE 'quarantine:%'"
            )
        ).scalar()

        pending = session.execute(
            text(
                f"SELECT COUNT(*) FROM supporting_documents\n"
                f"WHERE (text_content IS NULL OR text_content = '')\n"
                f"  AND (text_extraction_method IS NULL\n"
                f"       OR text_extraction_method NOT IN {FAILURE_METHODS})"
            )
        ).scalar()

        print(f"\n{'=' * 68}")
        print(f"  Document Text Extraction Status")
        print(f"{'=' * 68}")
        print(f"  Total docs:       {total:>6}")
        print(f"  Extracted:         {with_text:>6}  ({pct}%)")
        print()
        print(f"  Failure breakdown:")
        print(f"    download_failed           {failed_dl:>6}")
        print(f"    failed (extraction)       {failed_extract:>6}")
        print(f"    process_error             {failed_proc:>6}")
        print(f"    extraction_failed         {failed_extract_explicit:>6}")
        if quarantined:
            print(f"    {"quarantine (needs review)":26} {quarantined:>6}")
        print(f"    ─────────────────────────────────")
        print(
            f"    All failures              "
            f"{failed_dl + failed_extract + failed_proc + failed_extract_explicit + quarantined:>6}"
        )
        print()
        print(f"  Pending (untouched): {pending:>6}")
        print(
            f"  Queue total:        "
            f"{pending + failed_dl:>6}  (pending + retriable)"
        )

        queue = pending + failed_dl
        eta_days = queue // 2000 + 1 if queue else 0
        print(f"  ETA at 2000/day:    {eta_days} day(s)")

        recent = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_content IS NOT NULL\n"
                "  AND text_extracted_at >= NOW() - INTERVAL '3 days'"
            )
        ).scalar()
        recent_done = session.execute(
            text(
                "SELECT COUNT(*) FROM supporting_documents\n"
                "WHERE text_content IS NOT NULL\n"
                "  AND text_extracted_at >= NOW() - INTERVAL '3 days'"
            )
        ).scalar()
        print(f"  Recent (last 3d):   {recent_done:>4}/{recent:<4} extracted")

        # ── Pending by age ──
        print()
        print(f"  Pending by age:")
        age_buckets = session.execute(
            text(
                f"SELECT\n"
                f"    CASE\n"
                f"        WHEN m.meeting_date >= to_char(current_date - INTERVAL '7 days', 'YYYY-MM-DD')"
                f"        THEN 'this week  '\n"
                f"        WHEN m.meeting_date >= to_char(current_date - INTERVAL '30 days', 'YYYY-MM-DD')"
                f"        THEN 'this month '\n"
                f"        ELSE 'older      '\n"
                f"    END AS bucket,\n"
                f"    COUNT(*) AS cnt\n"
                f"FROM supporting_documents sd\n"
                f"JOIN meetings m ON sd.meeting_db_id = m.id\n"
                f"WHERE (sd.text_content IS NULL OR sd.text_content = '')\n"
                f"  AND (sd.text_extraction_method IS NULL\n"
                f"       OR sd.text_extraction_method NOT IN {FAILURE_METHODS})\n"
                f"GROUP BY bucket\n"
                f"ORDER BY MIN(m.meeting_date) DESC"
            )
        ).fetchall()
        for row in age_buckets:
            print(f"    {row[0]}  {row[1]:>4}")

        # ── Failed downloads by age ──
        print()
        print(f"  Failed downloads by age:")
        dl_age = session.execute(
            text(
                "SELECT\n"
                "    CASE\n"
                "        WHEN m.meeting_date >= to_char(current_date - INTERVAL '7 days', 'YYYY-MM-DD')"
                "        THEN 'this week  '\n"
                "        WHEN m.meeting_date >= to_char(current_date - INTERVAL '30 days', 'YYYY-MM-DD')"
                "        THEN 'this month '\n"
                "        WHEN m.meeting_date >= to_char(current_date - INTERVAL '90 days', 'YYYY-MM-DD')"
                "        THEN 'last 3mo   '\n"
                "        ELSE 'older      '\n"
                "    END AS bucket,\n"
                "    COUNT(*) AS cnt\n"
                "FROM supporting_documents sd\n"
                "JOIN meetings m ON sd.meeting_db_id = m.id\n"
                "WHERE sd.text_extraction_method = 'download_failed'\n"
                "GROUP BY bucket\n"
                "ORDER BY MIN(m.meeting_date) DESC"
            )
        ).fetchall()
        for row in dl_age:
            print(f"    {row[0]}  {row[1]:>4}")

        # ── By jurisdiction ──
        print()
        print(f"  By jurisdiction:")
        by_juris = session.execute(
            text(
                f"SELECT j.name,\n"
                f"       COUNT(sd.id) AS total,\n"
                f"       SUM(CASE WHEN sd.text_content IS NOT NULL AND sd.text_content != ''"
                f"           THEN 1 ELSE 0 END) AS done,\n"
                f"       SUM(CASE WHEN sd.text_extraction_method = 'download_failed'"
                f"           THEN 1 ELSE 0 END) AS fail_dl,\n"
                f"       SUM(CASE WHEN sd.text_extraction_method = 'failed'"
                f"           THEN 1 ELSE 0 END) AS fail_ex,\n"
                f"       SUM(CASE WHEN sd.text_extraction_method IN"
                f"               ('process_error','extraction_failed')"
                f"           THEN 1 ELSE 0 END) AS fail_other,\n"
                f"       SUM(CASE WHEN (sd.text_content IS NULL OR sd.text_content = '')"
                f"                 AND (sd.text_extraction_method IS NULL"
                f"                      OR sd.text_extraction_method NOT IN {FAILURE_METHODS})"
                f"               THEN 1 ELSE 0 END) AS pend\n"
                f"FROM supporting_documents sd\n"
                f"JOIN meetings m ON sd.meeting_db_id = m.id\n"
                f"JOIN jurisdictions j ON m.jurisdiction_id = j.id\n"
                f"GROUP BY j.name\n"
                f"ORDER BY total DESC"
            )
        ).fetchall()
        header = (
            f"    {'Jurisdiction':<25s} {'Total':>5} {'Done':>5} "
            f"{'FailDL':>7} {'FailEx':>7} {'Other':>6} {'Pend':>5}"
        )
        print(header)
        print(f"    {'-' * len(header)}")
        for jurisdiction_row in by_juris:
            j_name, total_count, done, fail_dl, fail_ex, fail_other, pend = (
                jurisdiction_row
            )
            print(
                f"    {j_name:<25s} {total_count:>5} {done:>5} {fail_dl:>7} "
                f"{fail_ex:>7} {fail_other:>6} {pend:>5}"
            )

        print(f"{'=' * 68}\n")
    finally:
        session.close()
