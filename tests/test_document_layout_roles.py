"""Stage 3 table-role inference is explicit and fail-closed."""

from scripts.docs.layout_roles import annotate_tables, header_role, infer_table_roles


def test_explicit_headers_map_to_governed_roles():
    assert header_role("Agenda Item") == "item_number"
    assert header_role("Application / Case") == "application"
    assert header_role("Action Taken") == "result"
    assert header_role("Description") == "title"
    assert header_role("Unrecognised") == "unknown"


def test_result_bearing_cells_require_an_explicit_result_header():
    table = {"cells": [
        ["Item", "Description", "Result"],
        ["4A", "Rezoning request", "Approved with stipulations"],
        ["4B", "Appeal", "Denied"],
    ]}
    roles = infer_table_roles(table)
    assert roles["header_row"] == 0
    assert roles["column_roles"] == ["item_number", "title", "result"]
    assert [(cell["row"], cell["column"], cell["text"])
            for cell in roles["result_cells"]] == [
        (1, 2, "Approved with stipulations"),
        (2, 2, "Denied"),
    ]


def test_result_words_in_unlabelled_or_non_result_columns_fail_closed():
    unlabelled = {"cells": [["4A", "Approved"], ["4B", "Denied"]]}
    assert infer_table_roles(unlabelled) == {
        "version": "document-table-roles/1.0",
        "header_row": None, "column_roles": [], "result_cells": []}

    descriptive = {"cells": [
        ["Item", "Description"],
        ["4A", "Previously approved plan"],
    ]}
    assert infer_table_roles(descriptive)["result_cells"] == []


def test_annotation_preserves_detector_cells_and_adds_roles():
    table = {"table_id": "p1-t1", "cells": [["Item", "Outcome"], ["1", "Tabled"]]}
    annotated = annotate_tables([table])
    assert annotated[0]["cells"] == table["cells"]
    assert annotated[0]["semantic_roles"]["result_cells"][0]["text"] == "Tabled"
    assert "semantic_roles" not in table
