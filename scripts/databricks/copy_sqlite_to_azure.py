# Databricks notebook source
# MAGIC %md
# MAGIC # Copy a uw-pipeline SQLite database to Azure SQL
# MAGIC
# MAGIC One-time copy of `data/uw.db` (pipeline tables) or `data/app.db` (application tables) into
# MAGIC Azure SQL, keeping every row id. Guide: `docs/databricks-copy.md`.
# MAGIC
# MAGIC 1. Create the target tables first with the repo's DDL (`deploy/sql/pipeline_schema_full.sql`
# MAGIC    or `deploy/sql/app_schema_full.sql`).
# MAGIC 2. Upload the `.db` file to a Unity Catalog volume.
# MAGIC 3. Pick the environment, run with `mode = dry_run`, read the plan, then run with
# MAGIC    `mode = load`.
# MAGIC
# MAGIC Connects with `azure_sql_connect` (D-057): the service principal from the secret scope,
# MAGIC server, database, schemas and secret key names from `config/azure.yaml` of the checked-out
# MAGIC repo (the widgets override them). The cluster needs the ODBC Driver 17 for SQL Server, as
# MAGIC the loss-run clusters have.

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.dropdown("database", "pipeline", ["pipeline", "app"], "1. Database")
dbutils.widgets.text("sqlite_path", "/Volumes/<catalog>/<schema>/<volume>/uw.db", "2. SQLite file")
dbutils.widgets.dropdown("env", "dv", ["dv", "pp", "pr"], "3. Environment (config/azure.yaml)")
dbutils.widgets.text("target_schema", "", "4. Target schema (blank: from azure.yaml)")
dbutils.widgets.text("staging_schema", "stg_uw_copy", "5. Staging schema (dropped at the end)")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "load"], "6. Mode")
# Blank: from config/azure.yaml for the environment.
dbutils.widgets.text("server", "", "7. Server (blank: azure.yaml)")
dbutils.widgets.text("sql_database", "", "8. Database (blank: azure.yaml)")
dbutils.widgets.text("secret_scope", "", "9. Secret scope (blank: azure.yaml)")
dbutils.widgets.text("username_key", "", "10. Secret: client id key (blank: azure.yaml)")
dbutils.widgets.text("password_key", "", "11. Secret: client secret key (blank: azure.yaml)")

# COMMAND ----------

import os
import shutil
import sqlite3
import time

W = {k: dbutils.widgets.get(k).strip() for k in (
    "database", "sqlite_path", "env", "target_schema", "staging_schema", "mode", "server",
    "sql_database", "secret_scope", "username_key", "password_key")}  # fmt: skip
S = azure_sql_settings(W["env"], server=W["server"], database=W["sql_database"],
                       secret_scope=W["secret_scope"], username_key=W["username_key"],
                       password_key=W["password_key"])  # fmt: skip
DEFAULT_SCHEMA = {"pipeline": "uw_pipeline", "app": "dbo_professional360"}
TARGET = (W["target_schema"] or S.get(f"{W['database']}_schema")
          or DEFAULT_SCHEMA[W["database"]])  # fmt: skip
STAGE = W["staging_schema"]
assert TARGET != STAGE, "the staging schema must differ from the target schema"
engine = sql_engine(S)
print(f"target {S['server']} / {S['database']} / schema {TARGET} ({W['env']})")


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


# COMMAND ----------

# MAGIC %md ## 1. Source and target

# COMMAND ----------

# SQLite needs a local file: copy it off the volume (a network path) first.
LOCAL = "/local_disk0/tmp/uw_copy.db"
os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
shutil.copy(W["sqlite_path"], LOCAL)
src = sqlite3.connect(LOCAL)
src_tables = {
    name: [r[1] for r in src.execute(f"PRAGMA table_info({q(name)})")]
    for (name,) in src.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )
}
src_rows = {t: src.execute(f"SELECT COUNT(*) FROM {q(t)}").fetchone()[0] for t in src_tables}

target: dict[str, dict[str, dict]] = {}
for r in query(
    engine,
    f"""
    SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE,
           COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                          c.COLUMN_NAME, 'IsIdentity') AS IS_IDENTITY,
           CASE WHEN c.COLUMN_DEFAULT IS NULL THEN 0 ELSE 1 END AS HAS_DEFAULT
    FROM INFORMATION_SCHEMA.COLUMNS c
    JOIN INFORMATION_SCHEMA.TABLES t
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
    WHERE c.TABLE_SCHEMA = '{TARGET}' AND t.TABLE_TYPE = 'BASE TABLE'""",
):
    target.setdefault(r["TABLE_NAME"], {})[r["COLUMN_NAME"]] = r


def row_counts() -> dict[str, int]:
    return {
        r["name"]: int(r["n"])
        for r in query(
            engine,
            f"""
        SELECT t.name, SUM(p.rows) AS n FROM sys.tables t
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
        WHERE s.name = '{TARGET}' GROUP BY t.name""",
        )
    }


target_rows = row_counts()

# COMMAND ----------

# MAGIC %md ## 2. Plan (nothing is written yet)

# COMMAND ----------

plan, problems = [], []
for t in sorted(src_tables):
    if t not in target:
        print(f"skip   {t}: not in the target schema (not part of the DDL)")
        continue
    shared = [c for c in src_tables[t] if c in target[t]]
    extra = [c for c in src_tables[t] if c not in target[t]]
    missing = [
        c
        for c, m in target[t].items()
        if c not in src_tables[t]
        and m["IS_NULLABLE"] == "NO"
        and not m["HAS_DEFAULT"]
        and not m["IS_IDENTITY"]
    ]
    if missing:
        problems.append(f"{t}: required target columns not in SQLite: {missing}")
    if target_rows.get(t, 0):
        problems.append(f"{t}: target already has {target_rows[t]} rows (load into empty tables)")
    note = f" (SQLite-only columns left out: {extra})" if extra else ""
    print(f"copy   {t}: {src_rows[t]} rows, {len(shared)} columns{note}")
    plan.append((t, shared))
for t in sorted(set(target) - set(src_tables)):
    print(f"empty  {t}: in the target schema, not in the SQLite file")
for p in problems:
    print("PROBLEM", p)
assert not problems, "fix the problems above first"
if W["mode"] != "load":
    dbutils.notebook.exit(f"dry run: {len(plan)} tables to copy into {TARGET}; set mode = load")

# COMMAND ----------

# MAGIC %md ## 3. Load: staging tables, then the real tables with their ids

# COMMAND ----------


def as_text(v: object) -> str | None:
    """Every value as text for staging; SQL Server converts it to the column type on insert."""
    if v is None:
        return None
    if isinstance(v, float):
        return repr(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(v)
    if isinstance(v, bytes):
        return v.hex()
    return str(v)


def widths(t: str, cols: list[str]) -> list[str]:
    """NVARCHAR size of each staging column: twice the longest SQLite value (room for the
    text form of numbers and hex of blobs), MAX past 4,000."""
    longest = src.execute(f"SELECT {', '.join(f'MAX(LENGTH({q(c)}))' for c in cols)} "
                          f"FROM {q(t)}").fetchone()  # fmt: skip
    return ["MAX" if 2 * (n or 0) > 4000 else str(max(1, 2 * (n or 0))) for n in longest]


TIME_TYPES = ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset")


def select_list(t: str, shared: list[str]) -> str:
    """Columns for INSERT ... SELECT. Decimals go through FLOAT, which reads "1e-05"; dates and
    times through DATETIMEOFFSET, which reads SQLite's text with or without an offset and with 6
    decimals (a DATETIME column accepts at most 3)."""
    out = []
    for c in shared:
        kind = target[t][c]["DATA_TYPE"]
        if kind in ("decimal", "numeric"):
            out.append(f"CONVERT(FLOAT, {q(c)})")
        elif kind in TIME_TYPES:
            out.append(f"CONVERT(DATETIMEOFFSET, {q(c)})")
        else:
            out.append(q(c))
    return ", ".join(out)


run_sql(engine, f"IF SCHEMA_ID('{STAGE}') IS NULL EXEC('CREATE SCHEMA {q(STAGE)}')")
started = time.time()
for t, shared in plan:
    if not src_rows[t]:
        continue
    ws = widths(t, shared)
    cols = ", ".join(f"{q(c)} NVARCHAR({w}) NULL" for c, w in zip(shared, ws, strict=True))
    staged_table = f"{q(STAGE)}.{q(t)}"
    run_sql(engine, f"DROP TABLE IF EXISTS {staged_table}; CREATE TABLE {staged_table} ({cols})")
    insert = (f"INSERT INTO {q(STAGE)}.{q(t)} ({', '.join(q(c) for c in shared)}) "
              f"VALUES ({', '.join('?' * len(shared))})")  # fmt: skip
    # pyodbc's fast mode sends whole batches, sized by the column width; long text columns
    # (NVARCHAR(MAX)) go row by row in smaller batches instead.
    long_text = "MAX" in ws
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.fast_executemany = not long_text
        rows = src.execute(f"SELECT {', '.join(q(c) for c in shared)} FROM {q(t)}")
        staged = 0
        while batch := rows.fetchmany(2_000 if long_text else 50_000):
            cur.executemany(insert, [[as_text(v) for v in r] for r in batch])
            raw.commit()
            staged += len(batch)
    finally:
        raw.close()
    print(f"staged {t}: {staged} rows ({time.time() - started:.0f} s)")

loaded = []
try:
    for t, _ in plan:
        run_sql(engine, f"ALTER TABLE {q(TARGET)}.{q(t)} NOCHECK CONSTRAINT ALL")
    for t, shared in plan:
        if not src_rows[t]:
            continue
        identity = any(target[t][c]["IS_IDENTITY"] for c in shared)
        on = f"SET IDENTITY_INSERT {q(TARGET)}.{q(t)} ON;" if identity else ""
        off = f"SET IDENTITY_INSERT {q(TARGET)}.{q(t)} OFF;" if identity else ""
        run_sql(
            engine,
            f"""{on}
            INSERT INTO {q(TARGET)}.{q(t)} ({", ".join(q(c) for c in shared)})
            SELECT {select_list(t, shared)} FROM {q(STAGE)}.{q(t)};
            {off}""",
        )
        loaded.append(t)
        print(f"loaded {t}")
finally:  # keys are checked again even when a table failed
    for t, _ in plan:
        run_sql(engine, f"ALTER TABLE {q(TARGET)}.{q(t)} WITH CHECK CHECK CONSTRAINT ALL")

# COMMAND ----------

# MAGIC %md ## 4. Check row counts, drop the staging tables

# COMMAND ----------

after = row_counts()
diff = [(t, src_rows[t], after.get(t, 0)) for t, _ in plan if src_rows[t] != after.get(t, 0)]
for t, a, b in diff:
    print(f"MISMATCH {t}: SQLite {a}, Azure SQL {b}")
assert not diff, "row counts differ; the staging tables are kept for checking"
for t, _ in plan:
    run_sql(engine, f"DROP TABLE IF EXISTS {q(STAGE)}.{q(t)}")
run_sql(engine, f"IF SCHEMA_ID('{STAGE}') IS NOT NULL EXEC('DROP SCHEMA {q(STAGE)}')")
print(f"done: {len(loaded)} tables copied into {TARGET}, row counts match")
