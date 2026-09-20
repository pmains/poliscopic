"""Adversarial tests for the design-only receipt store: declared schema and row rules.

Bounded by construction: these tests use small synthetic plans from the shared fixtures
rather than re-parsing the 43 MB authoritative plan, so the suite finishes predictably.
Nothing here creates a database object.
"""

import json

from _kg_stage3_processing_fixtures import append_action, receipt_for, row
from scripts.kg import stage3_processing_receipt as R
from scripts.kg import stage3_processing_receipt_store_rows as rows_mod
from scripts.kg import stage3_processing_receipt_store_schema as S


# --- schema: one canonical body, additive, append-only for the writer ---------------

def test_the_declared_schema_is_additive_and_rollback_owns_exactly_the_packet_objects():
    contract = S.schema_contract()
    assert contract["statement_count"] == len(S.DDL) == len(S.MIGRATION_PHASES)
    assert S.declaration_problems(contract) == []
    joined = " ".join(S.DDL)
    assert "DROP" not in joined.upper()
    for statement in S.DDL:
        head = statement.strip().split(None, 1)[0].upper()
        assert head in ("CREATE", "ALTER", "REVOKE"), head
    rollback = " ".join(S.ROLLBACK_DDL)
    for name in S.OWNED_OBJECTS:
        assert name in rollback, f"rollback does not own {name}"
    assert all(statement.startswith(("DROP ", "ALTER TABLE ", "ALTER SEQUENCE "))
               for statement in S.ROLLBACK_DDL)
    for foreign in ("supporting_documents", "agenda_items"):
        assert foreign not in rollback


def test_one_canonical_body_and_every_other_payload_column_is_generated():
    assert rows_mod.WRITER_SUPPLIED_COLUMNS == ("receipt_body",)
    generated = {name: declaration for name, declaration, _source in S.COLUMNS
                 if S.DEF in declaration}
    assert set(generated) == set(S.DERIVED_FROM_BODY)
    assert "receipt_body" not in generated
    for name in ("source_kind", "content_sha256", "receipt_digest", "status", "recorded_at",
                 "acquisition_class", "does_not_prove_processing"):
        assert name in generated, f"{name} must be generated from the body"
    assert "receipt_body ->> 'digest'" in generated["receipt_digest"]
    assert any("jsonb_array_length" in expression for _name, expression, _why in S.CONSTRAINTS)
    assert any(name == "processing_receipts_body_digest_coherence"
               for name, _expression, _why in S.CONSTRAINTS)
    assert f"receipt_digest = {S.CANONICAL_DIGEST_FUNCTION}(receipt_body)" in " ".join(S.DDL)
    assert "public.digest" in S.CANONICAL_DIGEST_DDL
    assert S.PGCRYPTO_REQUIREMENT["required_before_apply"] is True


def test_canonical_json_uses_a_postgresql_codepoint_primitive_that_exists():
    """The declared digest functions must be executable on supported PostgreSQL."""
    # PostgreSQL exposes ascii(text), not a unicode(text) SQL function.  The latter
    # would defer failure until the first receipt INSERT invokes the generated digest.
    assert "unicode(" not in S.JSON_STRING_DDL.lower()
    assert "ascii(" in S.JSON_STRING_DDL.lower()
    # The SQL function's branches must preserve the receipt contract's Python JSON
    # escaping for controls, BMP Unicode, and non-BMP surrogate pairs.
    expected = json.dumps({"text": "line\nµ\x01😀"}, sort_keys=True,
                          separators=(",", ":"))
    assert expected == '{"text":"line\\n\\u00b5\\u0001\\ud83d\\ude00"}'
    for fragment in ("point < 32", "to_hex(point)", "55296", "56320"):
        assert fragment in S.JSON_STRING_DDL


def test_generated_recorded_at_does_not_use_a_nonimmutable_text_timestamptz_cast():
    """PostgreSQL generated expressions must be immutable at CREATE TABLE time."""
    declaration = dict((name, sql) for name, sql, _source in S.COLUMNS)["recorded_at"]
    # The text -> timestamptz input cast is timezone/session-dependent (STABLE), so
    # PostgreSQL rejects it in a STORED generated column as "not immutable".
    assert "::timestamptz" not in declaration.lower()
    assert "recorded_at_sort_key" in S.TABLE_DDL
    assert S.RECORDED_AT_KEY_FUNCTION in S.RECORDED_AT_KEY_DDL
    assert "make_date" in S.RECORDED_AT_KEY_DDL
    assert "exact ISO-8601 instant" in S.RECORDED_AT_KEY_DDL


def test_rollback_owns_the_bigserial_sequence_or_avoids_an_implicit_sequence():
    """The apply receipt must account for every object CREATE TABLE introduces."""
    if "BIGSERIAL" in S.TABLE_DDL.upper():
        sequence = f"{S.TABLE}_receipt_id_seq"
        assert sequence in S.OWNED_OBJECTS
        assert any(sequence in statement for statement in S.ROLLBACK_DDL)


def test_database_checks_bind_the_whole_receipt_identity_to_its_canonical_body():
    """A direct JSONB insert cannot contradict receipt's duplicate identity fields."""
    joined = " ".join(S.DDL)
    for name in ("kind", "version", "producer_version", "source_kind", "source_id",
                 "content_sha256", "extraction_method", "extractor", "extractor_version"):
        assert f"processing_receipts_body_{name}" in joined
    assert "receipt_body ->> 'source_kind' = source_kind" in joined
    assert "(receipt_body ->> 'source_id')::bigint = source_id" in joined
    assert "receipt_body ->> 'extractor_version' = extractor_version" in joined


def test_schema_declaration_tampering_is_refused():
    contract = S.schema_contract()
    for field in ("declaration_sha256", "ddl_sha256", "rollback_sha256", "statement_count",
                  "table", "migration_phases", "derived_from_body", "owned_objects"):
        broken = dict(contract)
        broken[field] = "tampered"
        assert S.declaration_problems(broken), f"{field} tamper was accepted"
    for field in ("ddl", "rollback_ddl"):
        broken = dict(contract)
        broken[field] = list(contract[field])[:-1]
        assert S.declaration_problems(broken)
    broken = dict(contract)
    broken["append_only_rules"] = {}
    assert S.declaration_problems(broken)


def test_partial_application_is_never_repaired_automatically():
    assert S.partial_apply_problems(list(S.DDL)) == []
    partial = S.partial_apply_problems(list(S.DDL)[:4])
    assert any("partial application" in problem for problem in partial)
    assert any("restore from the apply's backup receipt" in problem for problem in partial)
    assert any("unexplained" in problem
               for problem in S.partial_apply_problems([S.DDL[1], S.DDL[0]]))


def test_append_only_covers_update_delete_and_truncate_for_the_writer():
    joined = " ".join(S.DDL)
    assert "BEFORE UPDATE OR DELETE" in joined
    assert "BEFORE TRUNCATE" in joined and "FOR EACH STATEMENT" in joined
    rules = S.APPEND_ONLY_RULES
    assert "TRUNCATE" in rules["statement_trigger"]
    assert "writer role" in rules["privileges"]
    assert "receipt_body" in rules["no_upsert"]


def test_privilege_statements_are_inside_the_atomic_statement_list():
    privileges = [statement for statement in S.DDL if statement.upper().startswith("REVOKE")]
    assert len(privileges) == 2
    for statement in privileges:
        assert "UPDATE, DELETE, TRUNCATE" in statement
    assert any("FROM PUBLIC" in statement for statement in privileges)
    assert any(S.STATEMENT_PLACEHOLDERS[0] in statement for statement in privileges)
    contract = S.schema_contract()
    assert "post_commit_privilege_ddl" not in contract
    assert contract["statement_placeholders"] == ["{{writer_role}}"]
    assert contract["pgcrypto_requirement"] == S.PGCRYPTO_REQUIREMENT


def test_writer_role_is_validated_and_safely_quoted_before_ddl_substitution():
    rendered = S.render_ddl(writer_role="poliscopic_writer")
    assert "FROM \"poliscopic_writer\"" in rendered[-1]
    assert S.STATEMENT_PLACEHOLDERS[0] not in " ".join(rendered)
    for unsafe in (None, "", "PUBLIC", "writer; DROP TABLE x", '"writer"'):
        try:
            S.render_ddl(writer_role=unsafe)
        except ValueError:
            pass
        else:  # pragma: no cover - makes an unsafe future relaxation explicit
            raise AssertionError(f"unsafe writer role was accepted: {unsafe!r}")


# --- rows: canonical body, derived columns, refusal rules ---------------------------

def test_typed_row_supplies_only_the_canonical_body():
    action = append_action()
    stored = rows_mod.typed_row(action)
    assert list(stored) == ["receipt_body"]
    assert rows_mod.row_problems(stored) == []
    derived = rows_mod.derived_columns(stored["receipt_body"])
    assert derived["receipt_digest"] == action["digest"]
    assert derived["status"] == "success" and derived["reason"] == ""
    assert derived["does_not_prove_processing"] is True
    assert derived["recorded_at"] == action["receipt"]["recorded_at"]
    assert rows_mod.identity_key(stored["receipt_body"]) == action["identity_key"]


def test_row_problems_refuse_extra_columns_and_invalid_bodies():
    stored = rows_mod.typed_row(append_action())
    for extra in ("identity_key", "status", "receipt_digest", "source_id"):
        broken = dict(stored)
        broken[extra] = "x"
        assert any("only ['receipt_body'] may be supplied" in problem
                   for problem in rows_mod.row_problems(broken)), extra
    broken = dict(stored)
    broken["receipt_body"] = {**stored["receipt_body"], "status": "maybe"}
    assert any("status is not registered" in problem
               for problem in rows_mod.row_problems(broken))
    for mutate, fragment in (
        (lambda body: body.update(acquisition={**body["acquisition"],
                                               "does_not_prove_processing": False}),
         "must not claim to prove processing"),
        (lambda body: body.update(digest="nothex"), "canonical digest"),
        (lambda body: body.update(extractor="other"), "extractor is not supported"),
    ):
        candidate = {"receipt_body": json.loads(json.dumps(stored["receipt_body"]))}
        mutate(candidate["receipt_body"])
        assert any(fragment in problem for problem in rows_mod.row_problems(candidate))


def test_derived_columns_mirror_the_body_and_the_store_identity():
    body = append_action()["receipt"]
    derived = rows_mod.derived_columns(body)
    assert rows_mod.identity_components(body) == tuple(
        derived[name] for name in ("source_kind", "source_id", "content_sha256",
                                   "extraction_method", "extractor", "extractor_version"))
    assert derived["source_id"] == body["processing_identity"][1]
    assert derived["legacy_swept_at_present"] is False
    assert rows_mod.derived_columns({})["source_kind"] is None


def test_duplicate_identity_and_digest_is_a_replay_not_a_second_row():
    action = append_action()
    stored = rows_mod.typed_row(action)
    planned = rows_mod.plan_appends([action], [stored])
    assert planned["insert_count"] == 0 and len(planned["replays"]) == 1
    assert planned["reconciles"] is True and planned["data_operations_proposed"] == 0
    assert rows_mod.plan_appends([action], [])["insert_count"] == 1


def test_unresolved_stored_conflict_refuses_new_arrivals():
    first = rows_mod.typed_row(append_action())
    conflicting = R.merge_receipts(
        [], [receipt_for(row(), status="failed", reason="boom")])["actions"][0]
    existing = [first, rows_mod.typed_row(conflicting)]
    assert rows_mod.current_state(existing)["conflicts"]
    planned = rows_mod.plan_appends([append_action()], existing)
    assert planned["insert_count"] == 0
    assert planned["conflicts"][0]["reason"] == "unresolved_stored_conflict"


def test_older_and_equal_instant_arrivals_are_refused_or_held():
    stored = rows_mod.typed_row(append_action())
    newer = append_action()
    newer["receipt"] = {**newer["receipt"], "recorded_at": "2026-09-14T23:00:00+00:00"}
    newer["receipt"].pop("digest")
    newer["receipt"]["digest"] = R.receipt_digest(newer["receipt"])
    newer["digest"] = newer["receipt"]["digest"]
    assert rows_mod.plan_appends([newer], [stored])["insert_count"] == 1
    older = append_action()
    older["receipt"] = {**older["receipt"], "recorded_at": "2026-09-14T11:00:00+00:00"}
    older["receipt"].pop("digest")
    older["receipt"]["digest"] = R.receipt_digest(older["receipt"])
    older["digest"] = older["receipt"]["digest"]
    refused = rows_mod.plan_appends([older], [stored])
    assert refused["insert_count"] == 0
    assert refused["refusals"][0]["reason"] == "older_than_stored_receipt"
    tied = append_action()
    tied["receipt"] = {**tied["receipt"], "status": "failed", "reason": "boom"}
    tied["receipt"].pop("digest")
    tied["receipt"]["digest"] = R.receipt_digest(tied["receipt"])
    tied["digest"] = tied["receipt"]["digest"]
    held = rows_mod.plan_appends([tied], [stored])
    assert held["insert_count"] == 0
    assert held["conflicts"][0]["reason"] == "arrival_disagrees_with_stored_at_same_instant"


def test_current_state_is_derived_newest_wins():
    older = rows_mod.typed_row(append_action())
    newer = append_action()
    newer["receipt"] = {**newer["receipt"], "recorded_at": "2026-09-14T23:30:00+00:00"}
    newer["receipt"].pop("digest")
    newer["receipt"]["digest"] = R.receipt_digest(newer["receipt"])
    newer["digest"] = newer["receipt"]["digest"]
    state = rows_mod.current_state([rows_mod.typed_row(newer), older])
    identity = rows_mod.identity_components(newer["receipt"])
    assert state["current"][identity]["receipt_digest"] == newer["receipt"]["digest"]
    assert state["identities"] == 1 and state["rows"] == 2
    assert rows_mod.current_state([])["current"] == {}


def test_more_than_one_action_per_identity_in_a_call_is_refused():
    first = append_action()
    later = append_action()
    later["receipt"] = {**later["receipt"], "recorded_at": "2026-09-14T23:45:00+00:00"}
    later["receipt"].pop("digest")
    later["receipt"]["digest"] = R.receipt_digest(later["receipt"])
    later["digest"] = later["receipt"]["digest"]
    for batch in ([first, later], [later, first]):
        planned = rows_mod.plan_appends(batch, [])
        assert planned["insert_count"] == 0, "two actions for one identity must not both insert"
        assert planned["reconciles"] is True
        assert [item["reason"] for item in planned["refusals"]] == \
            ["more_than_one_action_per_identity"] * 2
    other = append_action(row(id=2))
    mixed = rows_mod.plan_appends([first, later, other], [])
    assert mixed["insert_count"] == 1
    assert mixed["reconciles"] is True


def test_plan_appends_reconciles_every_action_it_is_given():
    good = append_action()
    second = append_action(row(id=2))
    replay_action = R.merge_receipts([receipt_for(row(id=3))],
                                     [receipt_for(row(id=3))])["actions"][0]
    planned = rows_mod.plan_appends([good, second, replay_action], [])
    assert planned["insert_count"] == 2
    assert planned["accounted"] == 3 and planned["reconciles"] is True
