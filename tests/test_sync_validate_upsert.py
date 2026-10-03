from sqlalchemy import create_engine, text

from db.sync_validate import _validate


def _engine(rows):
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as conn:
        conn.execute(text("ATTACH DATABASE ':memory:' AS public"))
        for table, count in rows.items():
            conn.execute(text(f'CREATE TABLE public."{table}" (id INTEGER PRIMARY KEY)'))
            for value in range(count):
                conn.execute(
                    text(f'INSERT INTO public."{table}" (id) VALUES (:id)'),
                    {"id": value},
                )
    return engine


def test_upsert_validation_allows_production_surplus(monkeypatch):
    monkeypatch.setattr("db.sync_validate.ALL_SYNC_TABLES", ["meetings"])
    assert _validate(_engine({"meetings": 2}), _engine({"meetings": 3})) is True


def test_upsert_validation_rejects_production_deficit(monkeypatch):
    monkeypatch.setattr("db.sync_validate.ALL_SYNC_TABLES", ["meetings"])
    assert _validate(_engine({"meetings": 3}), _engine({"meetings": 2})) is False
