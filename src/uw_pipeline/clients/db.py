"""Database engine construction."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, create_engine, make_url

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


def ensure_schema(engine: Engine) -> None:
    """Create missing tables. Only for local SQLite; Azure SQL migrations are owned elsewhere."""
    if engine.dialect.name != "sqlite":
        raise RuntimeError("ensure_schema is only for local SQLite; apply migrations for other DBs")
    metadata.create_all(engine)
