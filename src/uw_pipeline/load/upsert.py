"""Idempotent upserts keyed on natural keys.

SQLite (local dev) uses INSERT ... ON CONFLICT. SQL Server (Azure SQL) loads a #staging temp
table and MERGEs it into the target, which is how PLAN §8 says the database is written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, DateTime, String, Table, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.sql.compiler import IdentifierPreparer

from uw_pipeline.clients.db import table_schema


def upsert(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    key_cols: Sequence[str],
) -> int:
    if not rows:
        return 0
    cols = list(rows[0].keys())
    missing = set(key_cols) - set(cols)
    if missing:
        raise ValueError(f"rows lack key column(s) {sorted(missing)}")
    # One row per key, the last one winning: SQLite's ON CONFLICT accepts repeats, but a SQL
    # Server MERGE fails when the source has the same key twice (e.g. a model repeating an id).
    rows = list({tuple(r[k] for k in key_cols): r for r in rows}.values())
    dialect = conn.dialect.name
    if dialect == "sqlite":
        stmt = sqlite.insert(table)
        stmt = stmt.on_conflict_do_update(
            index_elements=list(key_cols),
            set_={c: stmt.excluded[c] for c in cols if c not in key_cols},
        )
        conn.execute(stmt, list(rows))
    elif dialect == "mssql":
        _mssql_merge(conn, table, rows, cols, key_cols)
    else:
        raise NotImplementedError(f"upsert not implemented for dialect {dialect!r}")
    return len(rows)


def insert_missing(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    key_cols: Sequence[str],
) -> int:
    """Insert rows whose key is not present yet; existing rows are left untouched."""
    if not rows:
        return 0
    rows = list({tuple(r[k] for k in key_cols): r for r in rows}.values())  # see upsert()
    dialect = conn.dialect.name
    if dialect == "sqlite":
        stmt = sqlite.insert(table).on_conflict_do_nothing(index_elements=list(key_cols))
        conn.execute(stmt, list(rows))
    elif dialect == "mssql":
        _mssql_merge(conn, table, rows, list(rows[0].keys()), key_cols, update=False)
    else:
        raise NotImplementedError(f"insert_missing not implemented for dialect {dialect!r}")
    return len(rows)


def build_merge_sql(
    preparer: IdentifierPreparer,
    target: Table,
    staging: str,
    cols: Sequence[str],
    key_cols: Sequence[str],
    *,
    update: bool = True,
    schema: str | None = None,
) -> str:
    q = preparer.quote
    tgt = preparer.format_table(target) if schema is None else f"{q(schema)}.{q(target.name)}"
    on = " AND ".join(f"t.{q(c)} = s.{q(c)}" for c in key_cols)
    updates = ", ".join(f"t.{q(c)} = s.{q(c)}" for c in cols if c not in key_cols)
    col_list = ", ".join(q(c) for c in cols)
    src_list = ", ".join(f"s.{q(c)}" for c in cols)
    matched = f"WHEN MATCHED THEN UPDATE SET {updates} " if updates and update else ""
    return (
        f"MERGE {tgt} WITH (HOLDLOCK) AS t USING {staging} AS s ON {on} "
        f"{matched}"
        f"WHEN NOT MATCHED BY TARGET THEN INSERT ({col_list}) VALUES ({src_list});"
    )


def _input_sizes(table: Table, cols: Sequence[str]) -> list[Any]:
    """Parameter types for the staging insert. pyodbc cannot describe the parameters of a
    #temp table, so fast_executemany would size each one from the first row: a longer string
    later fails ("String data, right truncation") and datetimes lose their microseconds."""
    import pyodbc  # SQL Server only

    sizes: list[Any] = []
    for c in cols:
        t = table.c[c].type
        if isinstance(t, String):  # Unicode, Text and UnicodeText too
            sizes.append((pyodbc.SQL_WVARCHAR, t.length or 0, 0))  # 0: NVARCHAR(max)
        elif isinstance(t, DateTime):
            sizes.append((pyodbc.SQL_TYPE_TIMESTAMP, 27, 7))  # DATETIME2(7) precision
        else:
            sizes.append(None)  # numbers, booleans, dates: pyodbc's own binding is exact
    return sizes


def _mssql_merge(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    cols: Sequence[str],
    key_cols: Sequence[str],
    *,
    update: bool = True,
) -> None:
    preparer = conn.dialect.identifier_preparer
    staging = "#stg_" + table.name
    col_list = ", ".join(preparer.quote(c) for c in cols)
    schema = table_schema(conn)
    tgt = preparer.format_table(table) if schema is None else (
        f"{preparer.quote(schema)}.{preparer.quote(table.name)}")  # fmt: skip
    conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))
    # SELECT TOP 0 ... INTO copies column types without the IDENTITY property of omitted columns.
    conn.execute(text(f"SELECT TOP 0 {col_list} INTO {staging} FROM {tgt}"))
    raw = conn.connection.dbapi_connection
    assert raw is not None
    cursor: Any = raw.cursor()
    try:
        cursor.fast_executemany = True
        cursor.setinputsizes(_input_sizes(table, cols))
        marks = ", ".join("?" for _ in cols)
        cursor.executemany(f"INSERT INTO {staging} ({col_list}) VALUES ({marks})",
                           [tuple(r[c] for c in cols) for r in rows])  # fmt: skip
    finally:
        cursor.close()
    conn.execute(text(build_merge_sql(preparer, table, staging, cols, key_cols, update=update,
                                      schema=schema)))  # fmt: skip
    conn.execute(text(f"DROP TABLE {staging}"))


def delete_keys(
    conn: Connection,
    table: Table,
    key_cols: Sequence[str],
    keys: Sequence[Sequence[Any]],
    *where: Any,
) -> int:
    """Delete rows whose `key_cols` values are in `keys` (plus optional extra conditions).

    Multi-column `IN ((a, b), ...)` does not exist in SQL Server, so each chunk is an OR of
    per-key ANDs, sized to stay under SQL Server's 2,100 parameters per statement."""
    from sqlalchemy import and_, delete, or_

    per = max(1, 2000 // max(1, len(key_cols)))
    total = 0
    for i in range(0, len(keys), per):
        chunk = keys[i : i + per]
        cond = or_(*(and_(*(table.c[c] == v for c, v in zip(key_cols, k, strict=True)))
                     for k in chunk))  # fmt: skip
        total += conn.execute(delete(table).where(cond, *where)).rowcount or 0
    return total
