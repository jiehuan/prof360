"""Database engine construction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, MetaData, create_engine, inspect, make_url, text
from sqlalchemy.schema import CreateColumn

from uw_pipeline.schema import metadata


def make_engine(url: str) -> Engine:
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)
        return create_engine(url)
    if parsed.get_backend_name() == "mssql":
        return create_engine(url, fast_executemany=True)
    return create_engine(url)


def ensure_schema(engine: Engine, md: MetaData = metadata) -> None:
    """Create missing tables and add missing columns (local SQLite only; Azure SQL migrations
    are owned elsewhere). New columns must be nullable or have a server default."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("ensure_schema is only for local SQLite; apply migrations for other DBs")
    md.create_all(engine)
    inspector = inspect(engine)
    with engine.begin() as conn:
        for table in md.sorted_tables:
            existing = {c["name"] for c in inspector.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl = CreateColumn(col).compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN {ddl}'))
