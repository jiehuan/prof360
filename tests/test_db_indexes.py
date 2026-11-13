"""D-097: foreign keys that parallel stages delete through have an index, so SQL Server checks
them with a seek instead of a table scan (prod run 5: news workers deadlocked deleting
activities), and a local SQLite database gets indexes added after its tables were made."""

from sqlalchemy import create_engine, inspect, text

from uw_pipeline.clients.db import ensure_schema
from uw_pipeline.schema import metadata

INDEXED = [
    ("news_item", "activity_id"),
    ("followup_question_insight", "insight_id"),
    ("insight_link", "linked_insight_id"),
]


def _leading(table: str) -> set[str]:
    t = metadata.tables[table]
    cols = {next(iter(ix.columns)).name for ix in t.indexes}
    return cols | {next(iter(t.primary_key.columns)).name}


def test_deleted_through_foreign_keys_are_indexed() -> None:
    for table, column in INDEXED:
        assert column in _leading(table), f"{table}.{column}"


def test_ensure_schema_adds_missing_indexes(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'uw.db'}")
    ensure_schema(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX ix_news_item_activity_id"))
    assert "ix_news_item_activity_id" not in {
        ix["name"] for ix in inspect(engine).get_indexes("news_item")
    }
    ensure_schema(engine)  # an existing database: the index comes back
    assert "ix_news_item_activity_id" in {
        ix["name"] for ix in inspect(engine).get_indexes("news_item")
    }
