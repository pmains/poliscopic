#!/usr/bin/env python3
"""Design-only additive Stage-3 receipt-store schema; this module never writes.

The writer supplies one JSONB receipt body. Generated columns and CHECK constraints
bind its canonical digest, identity, provenance and immutable timestamp sort key;
append-only triggers and transaction-bound revokes protect the real writer. Current
state is derived, never stored. The packet runner stays disabled.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from scripts.kg import stage3_processing_receipt as receipt_contract

TABLE = "processing_receipts"
TRIGGER_FUNCTION = "processing_receipts_append_only_guard"
TRIGGER_ROW = "processing_receipts_append_only"
TRIGGER_TRUNCATE = "processing_receipts_append_only_truncate"
CANONICAL_JSON_FUNCTION = "processing_receipts_canonical_json"
JSON_STRING_FUNCTION = "processing_receipts_json_string"
CANONICAL_DIGEST_FUNCTION = "processing_receipts_canonical_receipt_digest"
RECORDED_AT_KEY_FUNCTION = "processing_receipts_recorded_at_key"
UNIQUE_IDENTITY_DIGEST = "processing_receipts_identity_digest_key"
INDEX_HISTORY = "processing_receipts_source_history_idx"
INDEX_STATUS = "processing_receipts_status_idx"
INDEX_INSERTED = "processing_receipts_inserted_idx"
FOREIGN_KEY_SOURCE = "processing_receipts_source_id_fkey"
PRIMARY_KEY = "processing_receipts_pkey"
RECEIPT_ID_SEQUENCE = f"{TABLE}_receipt_id_seq"
SIGNATURE_VERSION = "kg-stage3-processing-receipts-schema/2.1"
HEX64 = r"^[0-9a-f]{64}$"

SOURCE_KINDS = ("supporting_document",)
EXTRACTORS = ("sweep_docs",)
STATUSES = ("success", "failed")
ACQUISITION_CLASSES = ("recorded_scraper_acquisition", "recorded_text_pipeline",
                       "provenance_unrecorded")

#: Apply-time bindings the DDL carries; an apply must supply them inside the transaction.
STATEMENT_PLACEHOLDERS = ("{{writer_role}}",)
WRITER_ROLE_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_$]{0,62}\Z")
PGCRYPTO_REQUIREMENT = {
    "extension": "pgcrypto",
    "function": "public.digest(bytea, text)",
    "required_before_apply": True,
    "reason": "the canonical receipt digest is recomputed in PostgreSQL",
}

DEF = "GENERATED ALWAYS AS"
JSONB_TEXT = "receipt_body ->> "
JSONB_PATH = "receipt_body #>> "

#: ``(column, sql declaration, payload source or derivation)``
COLUMNS = (
    ("receipt_id", "bigserial", "store-assigned row identity"),
    ("receipt_body", "jsonb NOT NULL", "the one canonical receipt body the writer supplies"),
    ("source_kind", f"text {DEF} ({JSONB_PATH}'{{processing_identity,0}}') STORED",
     "body.processing_identity[0]"),
    ("source_id", f"bigint {DEF} (({JSONB_PATH}'{{processing_identity,1}}')::bigint) STORED",
     "body.processing_identity[1]"),
    ("content_sha256", f"char(64) {DEF} ({JSONB_PATH}'{{processing_identity,2}}') STORED",
     "body.processing_identity[2]"),
    ("extraction_method",
     f"text {DEF} ({JSONB_PATH}'{{processing_identity,3}}') STORED",
     "body.processing_identity[3]"),
    ("extractor", f"text {DEF} ({JSONB_PATH}'{{processing_identity,4}}') STORED",
     "body.processing_identity[4]"),
    ("extractor_version", f"text {DEF} ({JSONB_PATH}'{{processing_identity,5}}') STORED",
     "body.processing_identity[5]"),
    ("producer_version", f"text {DEF} ({JSONB_TEXT}'producer_version') STORED",
     "body.producer_version"),
    ("receipt_digest", f"char(64) {DEF} ({JSONB_TEXT}'digest') STORED", "body.digest"),
    ("status", f"text {DEF} ({JSONB_TEXT}'status') STORED", "body.status"),
    ("reason", f"text {DEF} (coalesce({JSONB_TEXT}'reason', '')) STORED", "body.reason"),
    ("recorded_at",
     f"text {DEF} ({JSONB_TEXT}'recorded_at') STORED",
     "body.recorded_at (exact source/observation instant text)"),
    ("recorded_at_sort_key",
     f"bigint {DEF} ({RECORDED_AT_KEY_FUNCTION}({JSONB_TEXT}'recorded_at')) STORED",
     "immutable UTC microsecond sort key derived from body.recorded_at"),
    ("inserted_at", "timestamptz NOT NULL DEFAULT now()", "ingestion/system clock"),
    ("acquisition_class",
     f"text {DEF} ({JSONB_PATH}'{{acquisition,class}}') STORED", "body.acquisition.class"),
    ("legacy_swept_at_present",
     f"boolean {DEF} (({JSONB_PATH}'{{acquisition,legacy_swept_at_present}}')::boolean) STORED",
     "body.acquisition.legacy_swept_at_present"),
    ("does_not_prove_processing",
     f"boolean {DEF} (({JSONB_PATH}'{{acquisition,does_not_prove_processing}}')::boolean) STORED",
     "body.acquisition.does_not_prove_processing"),
)

#: Computed at write time for validation and audit; never stored.
DERIVED_FROM_BODY = tuple(name for name, _decl, _source in COLUMNS
                          if DEF in _decl)

#: ``(constraint, expression, rationale)``
CONSTRAINTS = (
    ("processing_receipts_body_is_object", "jsonb_typeof(receipt_body) = 'object'",
     "the canonical body is one JSON object"),
    ("processing_receipts_body_identity_shape",
     "jsonb_typeof(receipt_body -> 'processing_identity') = 'array' "
     "AND jsonb_array_length(receipt_body -> 'processing_identity') = 6",
     "the body must carry exactly the six processing-identity components"),
    ("processing_receipts_body_kind", f"receipt_body ->> 'kind' = '{receipt_contract.RECEIPT_KIND}'",
     "the body is a processing receipt, not an arbitrary JSON object"),
    ("processing_receipts_body_version",
     f"receipt_body ->> 'version' = '{receipt_contract.RECEIPT_VERSION}'",
     "the body version is the exact supported receipt version"),
    ("processing_receipts_body_producer_version",
     f"receipt_body ->> 'producer_version' = '{receipt_contract.PRODUCER_VERSION}'",
     "the body producer is the exact supported producer version"),
    ("processing_receipts_body_source_kind",
     "receipt_body ->> 'source_kind' = source_kind",
     "the flattened source kind cannot disagree with processing_identity[0]"),
    ("processing_receipts_body_source_id",
     "(receipt_body ->> 'source_id')::bigint = source_id",
     "the flattened source id cannot disagree with processing_identity[1]"),
    ("processing_receipts_body_content_sha256",
     "receipt_body ->> 'content_sha256' = content_sha256",
     "the flattened content hash cannot disagree with processing_identity[2]"),
    ("processing_receipts_body_extraction_method",
     "receipt_body ->> 'extraction_method' = extraction_method",
     "the flattened extraction method cannot disagree with processing_identity[3]"),
    ("processing_receipts_body_extractor", "receipt_body ->> 'extractor' = extractor",
     "the flattened extractor cannot disagree with processing_identity[4]"),
    ("processing_receipts_body_extractor_version",
     "receipt_body ->> 'extractor_version' = extractor_version",
     "the flattened extractor version cannot disagree with processing_identity[5]"),
    ("processing_receipts_body_digest_coherence",
     f"receipt_digest = {CANONICAL_DIGEST_FUNCTION}(receipt_body)",
     "the stored digest must be recomputed from the canonical body without digest"),
    ("processing_receipts_content_sha256_hex", f"content_sha256 ~ '{HEX64}'",
     "the bound text version is a sha256"),
    ("processing_receipts_receipt_digest_hex", f"receipt_digest ~ '{HEX64}'",
     "the canonical receipt digest is a sha256"),
    ("processing_receipts_source_kind_registered",
     "source_kind IN ('supporting_document')", "closed source-kind vocabulary"),
    ("processing_receipts_extractor_registered", "extractor IN ('sweep_docs')",
     "closed extractor vocabulary; a new extractor needs a reviewed packet"),
    ("processing_receipts_producer_version_registered", "btrim(producer_version) <> ''",
     "the producer version is required; its allowed set is enforced against the declared "
     "producer registry at write time"),
    ("processing_receipts_source_id_positive", "source_id > 0",
     "an identity names one positive source row"),
    ("processing_receipts_extraction_method_present", "btrim(extraction_method) <> ''",
     "a receipt always names the extraction method it observed"),
    ("processing_receipts_extractor_version_present", "btrim(extractor_version) <> ''",
     "a receipt always names the exact extractor version"),
    ("processing_receipts_status_registered", "status IN ('success', 'failed')",
     "the receipt contract admits exactly two statuses"),
    ("processing_receipts_failure_reason_present",
     "status <> 'failed' OR btrim(reason) <> ''", "a failure must name its reason"),
    ("processing_receipts_acquisition_class_registered",
     "acquisition_class IN ('recorded_scraper_acquisition', 'recorded_text_pipeline', "
     "'provenance_unrecorded')", "closed acquisition-provenance vocabulary"),
    ("processing_receipts_does_not_prove_processing", "does_not_prove_processing",
     "acquisition provenance may never be stored as processing proof"),
)

#: ``(index, unique, definition, rationale)``
INDEXES = (
    (UNIQUE_IDENTITY_DIGEST, True,
     f"ON {TABLE} (source_kind, source_id, content_sha256, extraction_method, extractor, "
     f"extractor_version, receipt_digest)",
     "replay-safety over the generated identity: an identical receipt is unrepresentable"),
    (INDEX_HISTORY, False, f"ON {TABLE} (source_kind, source_id, recorded_at_sort_key DESC)",
     "identity history scans for current-state derivation and audit"),
    (INDEX_STATUS, False, f"ON {TABLE} (status)", "failed-receipt sweeps"),
    (INDEX_INSERTED, False, f"ON {TABLE} (inserted_at)", "ingestion-clock audits"),
)

_GENERATED_COLUMNS_SQL = "\n".join(
    f"    {name} {declaration_sql},"
    for name, declaration_sql, _source in COLUMNS
    if name not in ("receipt_id", "receipt_body", "inserted_at"))

TABLE_DDL = f"""CREATE TABLE {TABLE} (
    receipt_id BIGSERIAL PRIMARY KEY,
    receipt_body JSONB NOT NULL,
{_GENERATED_COLUMNS_SQL}
    inserted_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT processing_receipts_body_is_object CHECK (jsonb_typeof(receipt_body) = 'object'),
    CONSTRAINT processing_receipts_body_identity_shape
        CHECK (jsonb_typeof(receipt_body -> 'processing_identity') = 'array'
               AND jsonb_array_length(receipt_body -> 'processing_identity') = 6),
    CONSTRAINT processing_receipts_body_kind
        CHECK (receipt_body ->> 'kind' = '{receipt_contract.RECEIPT_KIND}'),
    CONSTRAINT processing_receipts_body_version
        CHECK (receipt_body ->> 'version' = '{receipt_contract.RECEIPT_VERSION}'),
    CONSTRAINT processing_receipts_body_producer_version
        CHECK (receipt_body ->> 'producer_version' = '{receipt_contract.PRODUCER_VERSION}'),
    CONSTRAINT processing_receipts_body_source_kind
        CHECK (receipt_body ->> 'source_kind' = source_kind),
    CONSTRAINT processing_receipts_body_source_id
        CHECK ((receipt_body ->> 'source_id')::bigint = source_id),
    CONSTRAINT processing_receipts_body_content_sha256
        CHECK (receipt_body ->> 'content_sha256' = content_sha256),
    CONSTRAINT processing_receipts_body_extraction_method
        CHECK (receipt_body ->> 'extraction_method' = extraction_method),
    CONSTRAINT processing_receipts_body_extractor
        CHECK (receipt_body ->> 'extractor' = extractor),
    CONSTRAINT processing_receipts_body_extractor_version
        CHECK (receipt_body ->> 'extractor_version' = extractor_version),
    CONSTRAINT processing_receipts_body_digest_coherence
        CHECK (receipt_digest = {CANONICAL_DIGEST_FUNCTION}(receipt_body)),
    CONSTRAINT processing_receipts_content_sha256_hex CHECK (content_sha256 ~ '{HEX64}'),
    CONSTRAINT processing_receipts_receipt_digest_hex CHECK (receipt_digest ~ '{HEX64}'),
    CONSTRAINT processing_receipts_source_kind_registered
        CHECK (source_kind IN ('supporting_document')),
    CONSTRAINT processing_receipts_extractor_registered CHECK (extractor IN ('sweep_docs')),
    CONSTRAINT processing_receipts_producer_version_registered
        CHECK (btrim(producer_version) <> ''),
    CONSTRAINT processing_receipts_source_id_positive CHECK (source_id > 0),
    CONSTRAINT processing_receipts_extraction_method_present
        CHECK (btrim(extraction_method) <> ''),
    CONSTRAINT processing_receipts_extractor_version_present
        CHECK (btrim(extractor_version) <> ''),
    CONSTRAINT processing_receipts_status_registered
        CHECK (status IN ('success', 'failed')),
    CONSTRAINT processing_receipts_failure_reason_present
        CHECK (status <> 'failed' OR btrim(reason) <> ''),
    CONSTRAINT processing_receipts_acquisition_class_registered
        CHECK (acquisition_class IN ('recorded_scraper_acquisition',
                                     'recorded_text_pipeline', 'provenance_unrecorded')),
    CONSTRAINT processing_receipts_does_not_prove_processing
        CHECK (does_not_prove_processing)
)"""

# ``jsonb`` itself sorts object keys but its text rendering intentionally contains
# presentation spaces.  These immutable functions recreate the receipt contract's
# compact, sorted JSON representation before hashing it with pgcrypto.  The string
# function emits Python ``ensure_ascii=True`` escapes, including surrogate pairs.
JSON_STRING_DDL = f"""CREATE FUNCTION {JSON_STRING_FUNCTION}(value text)
RETURNS text LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    index integer;
    character text;
    point integer;
    escaped text := '"';
BEGIN
    FOR index IN 1..char_length(value) LOOP
        character := substr(value, index, 1);
        point := ascii(character);
        IF character = '"' THEN escaped := escaped || '\\"';
        ELSIF character = '\\' THEN escaped := escaped || '\\\\';
        ELSIF point = 8 THEN escaped := escaped || '\\b';
        ELSIF point = 9 THEN escaped := escaped || '\\t';
        ELSIF point = 10 THEN escaped := escaped || '\\n';
        ELSIF point = 12 THEN escaped := escaped || '\\f';
        ELSIF point = 13 THEN escaped := escaped || '\\r';
        ELSIF point < 32 THEN escaped := escaped || '\\u' || lpad(to_hex(point), 4, '0');
        ELSIF point <= 127 THEN escaped := escaped || character;
        ELSIF point <= 65535 THEN escaped := escaped || '\\u' || lpad(to_hex(point), 4, '0');
        ELSE
            point := point - 65536;
            escaped := escaped || '\\u' || lpad(to_hex(55296 + (point >> 10)), 4, '0');
            escaped := escaped || '\\u' || lpad(to_hex(56320 + (point & 1023)), 4, '0');
        END IF;
    END LOOP;
    RETURN escaped || '"';
END
$$"""

CANONICAL_JSON_DDL = f"""CREATE FUNCTION {CANONICAL_JSON_FUNCTION}(value jsonb)
RETURNS text LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT CASE jsonb_typeof(value)
      WHEN 'object' THEN '{{' || coalesce((
        SELECT string_agg({JSON_STRING_FUNCTION}(key) || ':' ||
                          {CANONICAL_JSON_FUNCTION}(item), ',' ORDER BY key COLLATE "C")
        FROM jsonb_each(value) AS entry(key, item)), '') || '}}'
      WHEN 'array' THEN '[' || coalesce((
        SELECT string_agg({CANONICAL_JSON_FUNCTION}(item), ',' ORDER BY ordinal)
        FROM jsonb_array_elements(value) WITH ORDINALITY AS entry(item, ordinal)), '') || ']'
      WHEN 'string' THEN {JSON_STRING_FUNCTION}(value #>> '{{}}')
      ELSE value::text
    END
$$"""

CANONICAL_DIGEST_DDL = f"""CREATE FUNCTION {CANONICAL_DIGEST_FUNCTION}(value jsonb)
RETURNS char(64) LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT encode(public.digest(convert_to({CANONICAL_JSON_FUNCTION}(value - 'digest'),
                                            'UTF8'), 'sha256'), 'hex')::char(64)
$$"""

RECORDED_AT_KEY_DDL = f"""CREATE FUNCTION {RECORDED_AT_KEY_FUNCTION}(value text)
RETURNS bigint LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE AS $$
DECLARE
    part text[]; day_count bigint; second_count bigint; fraction integer; offset_minutes integer := 0;
BEGIN
    part := regexp_match(value, '^([0-9]{{4}})-([0-9]{{2}})-([0-9]{{2}})T([0-9]{{2}}):([0-9]{{2}}):([0-9]{{2}})([.][0-9]{{1,6}})?(Z|([+-])([0-9]{{2}}):([0-9]{{2}}))$');
    IF part IS NULL OR part[4]::integer > 23 OR part[5]::integer > 59 OR part[6]::integer > 59
       OR (part[10] IS NOT NULL AND (part[10]::integer > 23 OR part[11]::integer > 59)) THEN
        RAISE EXCEPTION 'recorded_at must be an exact ISO-8601 instant';
    END IF;
    day_count := make_date(part[1]::integer, part[2]::integer, part[3]::integer) - DATE '1970-01-01';
    second_count := day_count * 86400 + part[4]::bigint * 3600 + part[5]::bigint * 60 + part[6]::bigint;
    fraction := rpad(substr(coalesce(part[7], ''), 2), 6, '0')::integer;
    IF part[8] <> 'Z' THEN
        offset_minutes := (part[10]::integer * 60 + part[11]::integer) *
                          CASE part[9] WHEN '+' THEN 1 ELSE -1 END;
    END IF;
    RETURN (second_count - offset_minutes * 60) * 1000000 + fraction;
END
$$"""

#: Ordered, additive DDL.  Nothing here is executed in this turn.
DDL = (
    JSON_STRING_DDL,
    CANONICAL_JSON_DDL,
    CANONICAL_DIGEST_DDL,
    RECORDED_AT_KEY_DDL,
    TABLE_DDL,
    f"""ALTER TABLE {TABLE} ADD CONSTRAINT {FOREIGN_KEY_SOURCE}
    FOREIGN KEY (source_id) REFERENCES supporting_documents (id) ON DELETE RESTRICT""",
    f"CREATE UNIQUE INDEX {UNIQUE_IDENTITY_DIGEST} ON {TABLE} (source_kind, source_id, "
    f"content_sha256, extraction_method, extractor, extractor_version, receipt_digest)",
    f"CREATE INDEX {INDEX_HISTORY} ON {TABLE} (source_kind, source_id, recorded_at_sort_key DESC)",
    f"CREATE INDEX {INDEX_STATUS} ON {TABLE} (status)",
    f"CREATE INDEX {INDEX_INSERTED} ON {TABLE} (inserted_at)",
    f"""CREATE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '{TABLE} is append-only; % is refused', TG_OP;
END
$$""",
    f"""CREATE TRIGGER {TRIGGER_ROW} BEFORE UPDATE OR DELETE ON {TABLE}
    FOR EACH ROW EXECUTE FUNCTION {TRIGGER_FUNCTION}()""",
    f"""CREATE TRIGGER {TRIGGER_TRUNCATE} BEFORE TRUNCATE ON {TABLE}
    FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER_FUNCTION}()""",
    f"REVOKE UPDATE, DELETE, TRUNCATE ON {TABLE} FROM PUBLIC",
    f"REVOKE UPDATE, DELETE, TRUNCATE ON {TABLE} FROM {STATEMENT_PLACEHOLDERS[0]}",
)

MIGRATION_PHASES = ("canonical_json_string", "canonical_json", "canonical_digest", "recorded_at_key", "table", "foreign_key", "index", "index", "index", "index",
                    "trigger_function", "trigger", "trigger", "privilege", "privilege")

ROLLBACK_DDL = (
    f"DROP TRIGGER {TRIGGER_TRUNCATE} ON {TABLE}",
    f"DROP TRIGGER {TRIGGER_ROW} ON {TABLE}",
    f"DROP FUNCTION {TRIGGER_FUNCTION}()",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT {FOREIGN_KEY_SOURCE}",
    f"DROP INDEX {UNIQUE_IDENTITY_DIGEST}",
    f"DROP INDEX {INDEX_HISTORY}",
    f"DROP INDEX {INDEX_STATUS}",
    f"DROP INDEX {INDEX_INSERTED}",
    f"ALTER TABLE {TABLE} DROP CONSTRAINT {PRIMARY_KEY}",
    f"ALTER SEQUENCE {RECEIPT_ID_SEQUENCE} OWNED BY NONE",
    f"DROP TABLE {TABLE}",
    f"DROP SEQUENCE {RECEIPT_ID_SEQUENCE}",
    f"DROP FUNCTION {RECORDED_AT_KEY_FUNCTION}(text)",
    f"DROP FUNCTION {CANONICAL_DIGEST_FUNCTION}(jsonb)",
    f"DROP FUNCTION {CANONICAL_JSON_FUNCTION}(jsonb)",
    f"DROP FUNCTION {JSON_STRING_FUNCTION}(text)",
)
OWNED_OBJECTS = (TABLE, PRIMARY_KEY, FOREIGN_KEY_SOURCE, UNIQUE_IDENTITY_DIGEST,
                 INDEX_HISTORY, INDEX_STATUS, INDEX_INSERTED, TRIGGER_ROW,
                 TRIGGER_TRUNCATE, TRIGGER_FUNCTION, CANONICAL_DIGEST_FUNCTION,
                 CANONICAL_JSON_FUNCTION, JSON_STRING_FUNCTION, RECORDED_AT_KEY_FUNCTION,
                 RECEIPT_ID_SEQUENCE)

CURRENT_STATE_SQL = f"""
SELECT receipt_digest, status, reason, recorded_at
FROM {TABLE}
WHERE source_kind = :source_kind AND source_id = :source_id
  AND content_sha256 = :content_sha256 AND extraction_method = :extraction_method
  AND extractor = :extractor AND extractor_version = :extractor_version
ORDER BY recorded_at_sort_key DESC, receipt_digest DESC
""".strip()

SIGNATURE_PROBES = {
    "columns": ("SELECT column_name, data_type, is_nullable, is_generated, generation_expression "
                "FROM information_schema.columns WHERE table_name = :table "
                "ORDER BY ordinal_position"),
    "constraints": ("SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = to_regclass(:table) ORDER BY conname"),
    "indexes": ("SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :table "
                "ORDER BY indexname"),
    "triggers": ("SELECT tgname, tgtype FROM pg_trigger WHERE tgrelid = to_regclass(:table) "
                 "AND NOT tgisinternal ORDER BY tgname"),
}

APPEND_ONLY_RULES = {
    "row_trigger": f"{TRIGGER_ROW} raises on UPDATE and DELETE",
    "statement_trigger": f"{TRIGGER_TRUNCATE} raises on TRUNCATE",
    "privileges": "REVOKE UPDATE, DELETE, TRUNCATE from PUBLIC and from one validated, "
                  "safely quoted writer role inside the atomic apply transaction",
    "no_upsert": "the writer may only INSERT and must not supply any column other than "
                 "receipt_body; ON CONFLICT DO UPDATE is forbidden",
    "coherence": "the body digest is recomputed with pgcrypto and every flattened identity "
                 "field is CHECK-bound to processing_identity, so contradictory proof is refused",
    "current_state": "derived on read; no current-state column may ever be added without a "
                     "new packet",
    "conflict_policy": "an identity whose newest recorded_at is shared by two different "
                       "receipt digests is unresolved and refuses new arrivals",
}


def canonical_sha256(payload: Any) -> str:
    return hashlib.sha256(json.dumps(
        payload, sort_keys=True, separators=(",", ":"), default=str
    ).encode("utf-8")).hexdigest()


def declaration() -> dict[str, Any]:
    """The formatting-independent schema declaration."""
    return {
        "version": SIGNATURE_VERSION,
        "table": TABLE,
        "columns": [{"name": name, "declaration": declaration_sql, "payload_source": source,
                     "store_assigned": name in ("receipt_id", "inserted_at")}
                    for name, declaration_sql, source in COLUMNS],
        "derived_from_body": list(DERIVED_FROM_BODY),
        "constraints": [{"name": name, "expression": expression, "rationale": rationale}
                        for name, expression, rationale in CONSTRAINTS],
        "indexes": [{"name": name, "unique": unique, "definition": definition,
                     "rationale": rationale}
                    for name, unique, definition, rationale in INDEXES],
        "append_only_rules": dict(APPEND_ONLY_RULES),
        "migration_phases": list(MIGRATION_PHASES),
        "statement_placeholders": list(STATEMENT_PLACEHOLDERS),
        "pgcrypto_requirement": dict(PGCRYPTO_REQUIREMENT),
        "owned_objects": list(OWNED_OBJECTS),
        "rollback_ddl": list(ROLLBACK_DDL),
        "source_kinds": list(SOURCE_KINDS),
        "extractors": list(EXTRACTORS),
        "statuses": list(STATUSES),
        "acquisition_classes": list(ACQUISITION_CLASSES),
    }


def schema_contract() -> dict[str, Any]:
    """The declaration plus its signature and the exact ordered DDL."""
    body = declaration()
    return {
        **body,
        "ddl": list(DDL),
        "statement_count": len(DDL),
        "declaration_sha256": canonical_sha256(body),
        "ddl_sha256": canonical_sha256(list(DDL)),
        "rollback_sha256": canonical_sha256(list(ROLLBACK_DDL)),
        "current_state_sql": CURRENT_STATE_SQL,
        "signature_probes": dict(SIGNATURE_PROBES),
    }


def declaration_problems(contract: Any) -> list[str]:
    """Refuse a contract that is not exactly the declared, signed design."""
    expected = schema_contract()
    if not isinstance(contract, Mapping):
        return ["schema contract is missing"]
    problems: list[str] = []
    for field in ("declaration_sha256", "ddl_sha256", "rollback_sha256", "statement_count",
                  "table", "migration_phases", "statement_placeholders", "pgcrypto_requirement",
                  "owned_objects", "derived_from_body"):
        if contract.get(field) != expected[field]:
            problems.append(f"schema contract {field} does not match the declared design")
    for field in ("ddl", "rollback_ddl"):
        if list(contract.get(field) or []) != list(expected[field]):
            problems.append(f"schema contract {field} does not match the declared design")
    if dict(contract.get("append_only_rules") or {}) != expected["append_only_rules"]:
        problems.append("schema contract append-only rules were modified")
    return problems


def partial_apply_problems(applied: Sequence[str], declared: Sequence[str] = DDL) -> list[str]:
    """Refuse partial application; recovery is restore from the apply backup."""
    applied = [str(statement).strip() for statement in applied]
    declared = [str(statement).strip() for statement in declared]
    if applied == declared:
        return []
    if applied and applied == declared[:len(applied)]:
        return [f"partial application: {len(applied)}/{len(declared)} declared statements are "
                f"present and {len(declared) - len(applied)} are missing; automatic repair is "
                f"forbidden - restore from the apply's backup receipt"]
    return ["applied statements are not a prefix of the declared packet; the object set is "
            "unexplained and the runner must refuse to continue"]


def quote_writer_role(writer_role: Any) -> str:
    """Validate one ordinary role name and return a SQL identifier, never SQL text."""
    value = str(writer_role or "")
    if not WRITER_ROLE_PATTERN.fullmatch(value) or value.upper() == "PUBLIC":
        raise ValueError("writer role must be one simple PostgreSQL identifier, not PUBLIC")
    return f'"{value}"'


def render_ddl(*, writer_role: Any) -> list[str]:
    """Return the exact declared DDL with the only placeholder safely substituted."""
    quoted = quote_writer_role(writer_role)
    return [statement.replace(STATEMENT_PLACEHOLDERS[0], quoted) for statement in DDL]
