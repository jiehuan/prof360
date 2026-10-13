"""Idempotent upserts keyed on natural keys.

SQLite (local dev) uses INSERT ... ON CONFLICT. SQL Server (Azure SQL) loads a #staging temp
table and MERGEs it into the target, which is how PLAN §8 says the database is written.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import Connection, Table, text
from sqlalchemy.dialects import sqlite
from sqlalchemy.sql.compiler import IdentifierPreparer


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


def build_merge_sql(
    preparer: IdentifierPreparer,
    target: Table,
    staging: str,
    cols: Sequence[str],
    key_cols: Sequence[str],
) -> str:
    q = preparer.quote
    tgt = preparer.format_table(target)
    on = " AND ".join(f"t.{q(c)} = s.{q(c)}" for c in key_cols)
    updates = ", ".join(f"t.{q(c)} = s.{q(c)}" for c in cols if c not in key_cols)
    col_list = ", ".join(q(c) for c in cols)
    src_list = ", ".join(f"s.{q(c)}" for c in cols)
    matched = f"WHEN MATCHED THEN UPDATE SET {updates} " if updates else ""
    return (
        f"MERGE {tgt} WITH (HOLDLOCK) AS t USING {staging} AS s ON {on} "
        f"{matched}"
        f"WHEN NOT MATCHED BY TARGET THEN INSERT ({col_list}) VALUES ({src_list});"
    )


def _mssql_merge(
    conn: Connection,
    table: Table,
    rows: Sequence[Mapping[str, Any]],
    cols: Sequence[str],
    key_cols: Sequence[str],
) -> None:
    preparer = conn.dialect.identifier_preparer
    staging = "#stg_" + table.name
    col_list = ", ".join(preparer.quote(c) for c in cols)
    tgt = preparer.format_table(table)
    conn.execute(text(f"DROP TABLE IF EXISTS {staging}"))
    # SELECT TOP 0 ... INTO copies column types without the IDENTITY property of omitted columns.
    conn.execute(text(f"SELECT TOP 0 {col_list} INTO {staging} FROM {tgt}"))
    params = ", ".join(f":{c}" for c in cols)
    conn.execute(text(f"INSERT INTO {staging} ({col_list}) VALUES ({params})"), list(rows))
    conn.execute(text(build_merge_sql(preparer, table, staging, cols, key_cols)))
    conn.execute(text(f"DROP TABLE {staging}"))
