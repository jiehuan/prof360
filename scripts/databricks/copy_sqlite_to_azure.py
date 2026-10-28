# Databricks notebook source
# MAGIC %md
# MAGIC # Copy a uw-pipeline SQLite database to Azure SQL
# MAGIC
# MAGIC Copies `data/uw.db` (pipeline tables) or `data/app.db` (application tables) into Azure SQL,
# MAGIC keeping every row id. Guide: `docs/databricks-copy.md`.
# MAGIC
# MAGIC 1. Create the target tables first with the repo's DDL (`deploy/sql/pipeline_schema_full.sql`
# MAGIC    or `deploy/sql/app_schema_full.sql`).
# MAGIC 2. Upload the `.db` file to a Unity Catalog volume.
# MAGIC 3. Pick the environment, run with `mode = dry_run`, read the plan, then run with
# MAGIC    `mode = load` the first time (empty tables) and `mode = sync` after each pipeline run:
# MAGIC    rows are inserted, updated or deleted by primary key so the target matches the file
# MAGIC    (D-064). Columns the application writes itself can be listed in `keep_columns`
# MAGIC    (`Table.column`, comma-separated): they are never overwritten. None by default; the
# MAGIC    pipeline writes `Company.logo` since D-067.
# MAGIC
# MAGIC Only the pipeline's own tables are touched in any mode: those created by the repo's DDL
# MAGIC for this database. The application's tables (users, favorites, pins, history, flags,
# MAGIC metric choices: `User*`) are never copied, synced or cleared, even when the SQLite file
# MAGIC has tables of that name.
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
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "load", "sync", "clear_target"], "6. Mode")
# Blank: from config/azure.yaml for the environment.
dbutils.widgets.text("server", "", "7. Server (blank: azure.yaml)")
dbutils.widgets.text("sql_database", "", "8. Database (blank: azure.yaml)")
dbutils.widgets.text("secret_scope", "", "9. Secret scope (blank: azure.yaml)")
dbutils.widgets.text("username_key", "", "10. Secret: client id key (blank: azure.yaml)")
dbutils.widgets.text("password_key", "", "11. Secret: client secret key (blank: azure.yaml)")
# sync: target columns the application writes, left as they are (Table.column, comma-separated).
dbutils.widgets.text("keep_columns", "", "12. Sync: columns the target keeps")

# COMMAND ----------

import os
import re
import shutil
import sqlite3
import time

W = {k: dbutils.widgets.get(k).strip() for k in (
    "database", "sqlite_path", "env", "target_schema", "staging_schema", "mode", "server",
    "sql_database", "secret_scope", "username_key", "password_key", "keep_columns")}  # fmt: skip
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


# The pipeline's own tables: those the repo's DDL for this database creates. Every other table
# (the application's User* tables above all) is left alone in every mode.
DDL = {"pipeline": "deploy/sql/pipeline_schema_full.sql", "app": "deploy/sql/app_schema_full.sql"}
_root = repo_root()
assert _root, "repo not found: the pipeline's table list comes from its DDL"
PIPELINE_TABLES = set(
    re.findall(
        r"(?im)^CREATE TABLE [^.\s]+\.\[?(\w+)\]? \(",
        (_root / DDL[W["database"]]).read_text(encoding="utf-8"),
    )
)
assert PIPELINE_TABLES, f"no tables found in {DDL[W['database']]}"


def app_owned(t: str) -> bool:
    """Never copied, synced or cleared: not created by the pipeline's DDL, or a User* table."""
    return t not in PIPELINE_TABLES or t.lower().startswith("user")


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
    SELECT c.TABLE_NAME, c.COLUMN_NAME, c.DATA_TYPE, c.IS_NULLABLE, c.CHARACTER_MAXIMUM_LENGTH,
           c.NUMERIC_PRECISION, c.NUMERIC_SCALE, c.DATETIME_PRECISION,
           COLUMNPROPERTY(OBJECT_ID(QUOTENAME(c.TABLE_SCHEMA) + '.' + QUOTENAME(c.TABLE_NAME)),
                          c.COLUMN_NAME, 'IsIdentity') AS IS_IDENTITY,
           CASE WHEN c.COLUMN_DEFAULT IS NULL THEN 0 ELSE 1 END AS HAS_DEFAULT
    FROM INFORMATION_SCHEMA.COLUMNS c
    JOIN INFORMATION_SCHEMA.TABLES t
      ON t.TABLE_SCHEMA = c.TABLE_SCHEMA AND t.TABLE_NAME = c.TABLE_NAME
    WHERE c.TABLE_SCHEMA = '{TARGET}' AND t.TABLE_TYPE = 'BASE TABLE'""",
):
    target.setdefault(r["TABLE_NAME"], {})[r["COLUMN_NAME"]] = r

# Primary key columns of each target table, in key order (sync matches rows by them).
pkeys: dict[str, list[str]] = {}
for r in query(
    engine,
    f"""
    SELECT k.TABLE_NAME, k.COLUMN_NAME
    FROM INFORMATION_SCHEMA.TABLE_CONSTRAINTS tc
    JOIN INFORMATION_SCHEMA.KEY_COLUMN_USAGE k
      ON k.CONSTRAINT_NAME = tc.CONSTRAINT_NAME AND k.TABLE_SCHEMA = tc.TABLE_SCHEMA
    WHERE tc.TABLE_SCHEMA = '{TARGET}' AND tc.CONSTRAINT_TYPE = 'PRIMARY KEY'
    ORDER BY k.TABLE_NAME, k.ORDINAL_POSITION""",
):
    pkeys.setdefault(r["TABLE_NAME"], []).append(r["COLUMN_NAME"])
keep = {tuple(x.strip().split(".", 1)) for x in W["keep_columns"].split(",") if "." in x}


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
    if app_owned(t):
        print(f"skip   {t}: not a pipeline table (application-owned tables are never touched)")
        continue
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
    if W["mode"] == "load" and target_rows.get(t, 0):
        problems.append(
            f"{t}: target already has {target_rows[t]} rows (load into empty tables; "
            "mode = sync updates a loaded target)"
        )
    if W["mode"] == "sync" and not set(pkeys.get(t, [])) <= set(shared):
        problems.append(f"{t}: primary key {pkeys.get(t)} not all in SQLite (sync matches by it)")
    if W["mode"] == "sync" and not pkeys.get(t):
        problems.append(f"{t}: no primary key in the target (sync matches rows by it)")
    note = f" (SQLite-only columns left out: {extra})" if extra else ""
    print(f"copy   {t}: {src_rows[t]} rows, {len(shared)} columns{note}")
    plan.append((t, shared))
for t in sorted(set(target) - set(src_tables)):
    if not app_owned(t):
        print(f"empty  {t}: in the target schema, not in the SQLite file")
if W["mode"] == "clear_target":
    # Only for a target this copy filled and nothing else uses (after a failed load). Rows of
    # other tables (the application's User* tables, tables the BFF team added) that reference
    # these tables mean the target is in use: refuse, a used target is updated with sync.
    copied = {t for t, _ in plan}
    in_use = []
    for r in query(engine, f"""
            SELECT OBJECT_SCHEMA_NAME(fk.parent_object_id) AS sch,
                   OBJECT_NAME(fk.parent_object_id) AS child,
                   OBJECT_NAME(fk.referenced_object_id) AS parent
            FROM sys.foreign_keys fk
            WHERE OBJECT_SCHEMA_NAME(fk.referenced_object_id) = '{TARGET}'"""):  # fmt: skip
        if r["parent"] not in copied or (r["sch"] == TARGET and r["child"] in copied):
            continue
        n = query(engine, f"SELECT COUNT_BIG(*) AS n FROM {q(r['sch'])}.{q(r['child'])}")[0]["n"]
        if n:
            in_use.append(f"{r['sch']}.{r['child']} ({n} rows) references {r['parent']}")
    for p in in_use:
        print("PROBLEM", p)
    assert not in_use, (
        "the target is in use (rows above refer to it): clear_target would break them. "
        "Update it with mode = sync instead"
    )
    # One transaction: every table is emptied, or (on any error) none is, and the constraints
    # are back on either way. DELETE, not TRUNCATE (refused on tables that others reference).
    total = sum(target_rows.get(t, 0) for t, _ in plan)
    print(f"clearing {total:,} rows in {TARGET}")
    stmts = [f"ALTER TABLE {q(TARGET)}.{q(t)} NOCHECK CONSTRAINT ALL;" for t, _ in plan]
    stmts += [f"DELETE FROM {q(TARGET)}.{q(t)};" for t, _ in plan if target_rows.get(t, 0)]
    stmts += [f"ALTER TABLE {q(TARGET)}.{q(t)} WITH CHECK CHECK CONSTRAINT ALL;" for t, _ in plan]
    run_sql(engine, "SET XACT_ABORT ON;\nBEGIN TRANSACTION;\n" + "\n".join(stmts) + "\nCOMMIT;")
    for t, _ in plan:
        run_sql(engine, f"DROP TABLE IF EXISTS {q(STAGE)}.{q(t)}")
    run_sql(engine, f"IF SCHEMA_ID('{STAGE}') IS NOT NULL EXEC('DROP SCHEMA {q(STAGE)}')")
    dbutils.notebook.exit(f"cleared {len(plan)} tables in {TARGET}; run mode = load again")
if (
    W["mode"] != "sync"
    and plan
    and all(target_rows.get(t, 0) == src_rows[t] for t, _ in plan)
    and any(target_rows.get(t, 0) for t, _ in plan)
):
    # Run again after a load that succeeded: the target holds this copy already.
    dbutils.notebook.exit(
        f"already loaded: the {len(plan)} tables in {TARGET} have the SQLite file's row counts; "
        "nothing to do (updates after a pipeline run: mode = sync)"
    )
for p in problems:
    print("PROBLEM", p)
assert not problems, "fix the problems above first"
if W["mode"] not in ("load", "sync"):
    dbutils.notebook.exit(
        f"dry run: {len(plan)} tables for {TARGET}; set mode = load (empty "
        "target) or sync (update a loaded target)"
    )

# COMMAND ----------

# MAGIC %md ## 3. Load or sync: staging tables, then the real tables with their ids

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


def target_type(t: str, c: str) -> str:
    """The SQL Server type of a target column, for CONVERT."""
    m = target[t][c]
    kind = m["DATA_TYPE"]
    if kind in ("nvarchar", "varchar", "nchar", "char", "varbinary", "binary"):
        n = m["CHARACTER_MAXIMUM_LENGTH"]
        return f"{kind}({'MAX' if n in (-1, None) else n})"
    if kind in ("decimal", "numeric"):
        return f"{kind}({m['NUMERIC_PRECISION']}, {m['NUMERIC_SCALE']})"
    if kind in ("datetime2", "datetimeoffset", "time"):
        return f"{kind}({m['DATETIME_PRECISION']})"
    return kind


def typed(t: str, c: str, alias: str = "") -> str:
    """A staged text column converted to its target type the way a load converts it (decimals
    through FLOAT, dates and times through DATETIMEOFFSET), so an unchanged value compares
    equal to the one already loaded."""
    col = f"{alias}{q(c)}"
    kind = target[t][c]["DATA_TYPE"]
    if kind in ("decimal", "numeric"):
        col = f"CONVERT(FLOAT, {col})"
    elif kind in TIME_TYPES:
        col = f"CONVERT(DATETIMEOFFSET, {col})"
    return f"CONVERT({target_type(t, c)}, {col})"


def merge_sql(t: str, shared: list[str]) -> str:
    """One MERGE making the target table equal to its staged copy, by primary key: new rows
    inserted (with their ids), changed rows updated, rows gone from the file deleted. The
    keep_columns are inserted for new rows and never updated. Returns the counts."""
    pk = pkeys[t]
    upd = [c for c in shared if c not in pk and (t, c) not in keep]
    tgt, stg = f"{q(TARGET)}.{q(t)}", f"{q(STAGE)}.{q(t)}"
    identity = any(target[t][c]["IS_IDENTITY"] for c in shared)
    matched = (
        f"WHEN MATCHED AND EXISTS (SELECT {', '.join(f'S.{q(c)}' for c in upd)} "
        f"EXCEPT SELECT {', '.join(f'T.{q(c)}' for c in upd)}) THEN UPDATE SET "
        + ", ".join(f"T.{q(c)} = S.{q(c)}" for c in upd)
        if upd
        else ""
    )
    return f"""
        {f"SET IDENTITY_INSERT {tgt} ON;" if identity else ""}
        DECLARE @changes TABLE (act NVARCHAR(10));
        MERGE {tgt} WITH (HOLDLOCK) AS T
        USING (SELECT {", ".join(f"{typed(t, c)} AS {q(c)}" for c in shared)} FROM {stg}) AS S
        ON {" AND ".join(f"T.{q(c)} = S.{q(c)}" for c in pk)}
        {matched}
        WHEN NOT MATCHED BY TARGET THEN
            INSERT ({", ".join(q(c) for c in shared)})
            VALUES ({", ".join(f"S.{q(c)}" for c in shared)})
        WHEN NOT MATCHED BY SOURCE THEN DELETE
        OUTPUT $action INTO @changes;
        {f"SET IDENTITY_INSERT {tgt} OFF;" if identity else ""}
        SELECT SUM(CASE act WHEN 'INSERT' THEN 1 ELSE 0 END) AS inserted,
               SUM(CASE act WHEN 'UPDATE' THEN 1 ELSE 0 END) AS updated,
               SUM(CASE act WHEN 'DELETE' THEN 1 ELSE 0 END) AS deleted
        FROM @changes;"""


def merge_counts(sql: str) -> dict:
    """Run a MERGE batch, commit, return its counts row."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("SET NOCOUNT ON;")
        cur.execute(sql)
        while cur.description is None and cur.nextset():
            pass
        row = cur.fetchone()
        cols = [d[0] for d in cur.description]
        while cur.nextset():
            pass
        raw.commit()
        return {k: int(v or 0) for k, v in zip(cols, row, strict=True)}
    finally:
        raw.close()


if W["mode"] == "load":
    # Checked again here, not only in the plan: a rerun of this cell alone (after a failed
    # load) would otherwise stage for minutes and then insert duplicates.
    now = row_counts()
    filled = [t for t, _ in plan if now.get(t)]
    assert not filled, (f"target tables already have rows: {filled}; run mode = clear_target, "
                        "then run the whole notebook again (not this cell alone)")  # fmt: skip
run_sql(engine, f"IF SCHEMA_ID('{STAGE}') IS NULL EXEC('CREATE SCHEMA {q(STAGE)}')")
started = time.time()
for t, shared in plan:
    # An empty file table is staged too: sync then empties the target table.
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
    # More rows than were sent: another run of this notebook is staging into the same schema
    # at the same time (its rows would show up later as duplicate keys).
    n = query(engine, f"SELECT COUNT_BIG(*) AS n FROM {staged_table}")[0]["n"]
    assert n == staged, (
        f"{staged_table} has {n} rows, {staged} were sent: another run is using the staging "
        f"schema {STAGE}. Let it finish (or stop it), then run this notebook once"
    )
    print(f"staged {t}: {staged} rows ({time.time() - started:.0f} s)")

loaded = []
totals = {"inserted": 0, "updated": 0, "deleted": 0}
try:
    for t, _ in plan:
        run_sql(engine, f"ALTER TABLE {q(TARGET)}.{q(t)} NOCHECK CONSTRAINT ALL")
    for t, shared in plan:
        if W["mode"] == "sync":
            try:
                n = merge_counts(merge_sql(t, shared))
            except Exception as exc:
                print(f"FAILED to sync {t}: {exc}")
                raise
            loaded.append(t)
            for k in totals:
                totals[k] += n[k]
            if any(n.values()):
                print(
                    f"synced {t}: +{n['inserted']} inserted, {n['updated']} updated, "
                    f"-{n['deleted']} deleted"
                )
            continue
        if not src_rows[t]:
            continue
        identity = any(target[t][c]["IS_IDENTITY"] for c in shared)
        on = f"SET IDENTITY_INSERT {q(TARGET)}.{q(t)} ON;" if identity else ""
        off = f"SET IDENTITY_INSERT {q(TARGET)}.{q(t)} OFF;" if identity else ""
        try:
            run_sql(
                engine,
                f"""{on}
                INSERT INTO {q(TARGET)}.{q(t)} ({", ".join(q(c) for c in shared)})
                SELECT {select_list(t, shared)} FROM {q(STAGE)}.{q(t)};
                {off}""",
            )
        except Exception as exc:
            print(f"FAILED to load {t}: {exc}")
            raise  # after the constraint checks below, this error is the one shown
        loaded.append(t)
        print(f"loaded {t}")
finally:
    # Keys are checked again on every table, even when one failed; a failed check is listed,
    # not raised, so the remaining tables still get their constraints back.
    unchecked = []
    for t, _ in plan:
        try:
            run_sql(engine, f"ALTER TABLE {q(TARGET)}.{q(t)} WITH CHECK CHECK CONSTRAINT ALL")
        except Exception as exc:
            unchecked.append(t)
            print(f"constraints of {t} not re-enabled (rows break a key): {str(exc)[:300]}")
assert not unchecked, f"constraints left disabled on {unchecked}: empty the target and reload"

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
if W["mode"] == "sync":
    print(
        f"done: {len(loaded)} tables synced into {TARGET}: {totals['inserted']} inserted, "
        f"{totals['updated']} updated, {totals['deleted']} deleted; row counts match"
    )
else:
    print(f"done: {len(loaded)} tables copied into {TARGET}, row counts match")
