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
# MAGIC 3. Fill in the widgets, run with `mode = dry_run`, read the plan, then run with
# MAGIC    `mode = load`.
# MAGIC
# MAGIC Needs a single-user (dedicated) cluster: shared access mode blocks the JDBC connection used
# MAGIC to run T-SQL statements.

# COMMAND ----------

# MAGIC %pip install azure-identity

# COMMAND ----------

dbutils.widgets.dropdown("database", "pipeline", ["pipeline", "app"], "1. Database")
dbutils.widgets.text("sqlite_path", "/Volumes/<catalog>/<schema>/<volume>/uw.db", "2. SQLite file")
dbutils.widgets.text("server", "", "3. Azure SQL server (*.database.windows.net)")
dbutils.widgets.text("sql_database", "", "4. Azure SQL database")
dbutils.widgets.text(
    "target_schema", "", "5. Target schema (blank: uw_pipeline / dbo_professional360)"
)
dbutils.widgets.text("staging_schema", "stg_uw_copy", "6. Staging schema (dropped at the end)")
dbutils.widgets.text("secret_scope", "uw", "7. Secret scope")
dbutils.widgets.text("tenant_key", "tenant-id", "8. Secret: tenant id")
dbutils.widgets.text("client_id_key", "sp-client-id", "9. Secret: client id")
dbutils.widgets.text("client_secret_key", "sp-client-secret", "10. Secret: client secret")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "load"], "11. Mode")

# COMMAND ----------

import os
import shutil
import sqlite3
import time

from azure.identity import ClientSecretCredential
from pyspark.sql.types import StringType, StructField, StructType

DEFAULT_SCHEMA = {"pipeline": "uw_pipeline", "app": "dbo_professional360"}  # config/azure.yaml
W = {k: dbutils.widgets.get(k).strip() for k in (
    "database", "sqlite_path", "server", "sql_database", "target_schema", "staging_schema",
    "secret_scope", "tenant_key", "client_id_key", "client_secret_key", "mode")}  # fmt: skip
TARGET = W["target_schema"] or DEFAULT_SCHEMA[W["database"]]
STAGE = W["staging_schema"]
assert W["server"] and W["sql_database"], "fill in the server and database widgets"
assert TARGET != STAGE, "the staging schema must differ from the target schema"

# Service principal from the secret scope; the access token is never printed.
token = (
    ClientSecretCredential(
        dbutils.secrets.get(W["secret_scope"], W["tenant_key"]),
        dbutils.secrets.get(W["secret_scope"], W["client_id_key"]),
        dbutils.secrets.get(W["secret_scope"], W["client_secret_key"]),
    )
    .get_token("https://database.windows.net/.default")
    .token
)

URL = (f"jdbc:sqlserver://{W['server']}:1433;database={W['sql_database']};"
       "encrypt=true;trustServerCertificate=false;loginTimeout=30")  # fmt: skip
PROPS = {"accessToken": token, "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver"}


def query(sql: str) -> list:
    """Rows of a T-SQL query."""
    return (spark.read.format("jdbc").option("url", URL).option("query", sql)
            .options(**PROPS).load().collect())  # fmt: skip


def run_sql(sql: str) -> None:
    """Run T-SQL statements in one batch on their own connection."""
    props = spark._jvm.java.util.Properties()
    props.setProperty("accessToken", token)
    conn = spark._jvm.java.sql.DriverManager.getConnection(URL, props)
    try:
        conn.createStatement().execute(sql)
    finally:
        conn.close()


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

cols = query(f"""
    SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE,
           COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                          c.COLUMN_NAME, 'IsIdentity') AS IS_IDENTITY,
           CASE WHEN c.COLUMN_DEFAULT IS NULL THEN 0 ELSE 1 END AS HAS_DEFAULT
    FROM INFORMATION_SCHEMA.COLUMNS c
    JOIN INFORMATION_SCHEMA.TABLES t
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
    WHERE c.TABLE_SCHEMA = '{TARGET}' AND t.TABLE_TYPE = 'BASE TABLE'""")
target: dict[str, dict[str, dict]] = {}
for r in cols:
    target.setdefault(r.TABLE_NAME, {})[r.COLUMN_NAME] = r.asDict()
target_rows = {
    r.name: int(r.n)
    for r in query(f"""
    SELECT t.name, SUM(p.rows) AS n FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
    WHERE s.name = '{TARGET}' GROUP BY t.name""")
}

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


CHUNK = 100_000  # rows per write: the largest table (xbrl_fact) has about 600,000
run_sql(f"IF SCHEMA_ID('{STAGE}') IS NULL EXEC('CREATE SCHEMA {q(STAGE)}')")
started = time.time()
for t, shared in plan:
    if not src_rows[t]:
        continue
    schema = StructType([StructField(c, StringType(), True) for c in shared])
    cur = src.execute(f"SELECT {', '.join(q(c) for c in shared)} FROM {q(t)}")
    mode, staged = "overwrite", 0
    while batch := cur.fetchmany(CHUNK):
        rows = [[as_text(v) for v in r] for r in batch]
        writer = spark.createDataFrame(rows, schema).write.mode(mode).option("batchsize", 10000)
        writer.jdbc(URL, f"{q(STAGE)}.{q(t)}", properties=PROPS)
        mode, staged = "append", staged + len(rows)
    print(f"staged {t}: {staged} rows ({time.time() - started:.0f} s)")

loaded = []
try:
    for t, _ in plan:
        run_sql(f"ALTER TABLE {q(TARGET)}.{q(t)} NOCHECK CONSTRAINT ALL")
    for t, shared in plan:
        if not src_rows[t]:
            continue
        identity = any(target[t][c]["IS_IDENTITY"] for c in shared)
        on = f"SET IDENTITY_INSERT {q(TARGET)}.{q(t)} ON;" if identity else ""
        off = f"SET IDENTITY_INSERT {q(TARGET)}.{q(t)} OFF;" if identity else ""
        run_sql(f"""{on}
            INSERT INTO {q(TARGET)}.{q(t)} ({", ".join(q(c) for c in shared)})
            SELECT {select_list(t, shared)} FROM {q(STAGE)}.{q(t)};
            {off}""")
        loaded.append(t)
        print(f"loaded {t}")
finally:  # keys are checked again even when a table failed
    for t, _ in plan:
        run_sql(f"ALTER TABLE {q(TARGET)}.{q(t)} WITH CHECK CHECK CONSTRAINT ALL")

# COMMAND ----------

# MAGIC %md ## 4. Check row counts, drop the staging tables

# COMMAND ----------

after = {
    r.name: int(r.n)
    for r in query(f"""
    SELECT t.name, SUM(p.rows) AS n FROM sys.tables t
    JOIN sys.schemas s ON s.schema_id = t.schema_id
    JOIN sys.partitions p ON p.object_id = t.object_id AND p.index_id IN (0, 1)
    WHERE s.name = '{TARGET}' GROUP BY t.name""")
}
diff = [(t, src_rows[t], after.get(t, 0)) for t, _ in plan if src_rows[t] != after.get(t, 0)]
for t, a, b in diff:
    print(f"MISMATCH {t}: SQLite {a}, Azure SQL {b}")
assert not diff, "row counts differ; the staging tables are kept for checking"
for t, _ in plan:
    run_sql(f"DROP TABLE IF EXISTS {q(STAGE)}.{q(t)}")
run_sql(f"IF SCHEMA_ID('{STAGE}') IS NOT NULL EXEC('DROP SCHEMA {q(STAGE)}')")
print(f"done: {len(loaded)} tables copied into {TARGET}, row counts match")
