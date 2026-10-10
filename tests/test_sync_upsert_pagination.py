from pathlib import Path


SOURCE = (
    Path(__file__).resolve().parents[1] / "scripts" / "db" / "sync_upsert.py"
).read_text()


def test_full_and_incremental_pages_use_the_complete_primary_key_order():
    """OFFSET paging must be deterministic when the primary key is composite.

    Ordering only by the first key lets tied rows move between queries, causing
    some rows to be read twice and others never to be copied.
    """
    assert SOURCE.count("f'  ORDER BY {pk_sql}\\n'") == 2
    assert "ORDER BY \"{pk_col}\"" not in SOURCE
