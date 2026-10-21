# Databricks notebook source
# MAGIC %md
# MAGIC # Run a T-SQL script on Azure SQL
# MAGIC
# MAGIC Runs one of the repo's DDL scripts (`deploy/sql/pipeline_schema_full.sql`,
# MAGIC `app_schema_full.sql`, `app_schema_changes.sql`) batch by batch: the scripts separate
# MAGIC batches with `GO` lines, which only SSMS and sqlcmd understand, so this notebook splits on
# MAGIC them. The scripts create only what is missing (`IF ... IS NULL`), so a rerun is safe.
# MAGIC Guide: `docs/databricks-copy.md`.
# MAGIC
# MAGIC Run with `mode = dry_run` first: it lists the batches and the tables already there.

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.text("sql_file", "deploy/sql/pipeline_schema_full.sql", "1. Script (repo path)")
dbutils.widgets.dropdown("env", "dv", ["dv", "pp", "pr"], "2. Environment (config/azure.yaml)")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "run"], "3. Mode")
# Blank: from config/azure.yaml for the environment.
dbutils.widgets.text("server", "", "4. Server (blank: azure.yaml)")
dbutils.widgets.text("sql_database", "", "5. Database (blank: azure.yaml)")
dbutils.widgets.text("secret_scope", "", "6. Secret scope (blank: azure.yaml)")
dbutils.widgets.text("username_key", "", "7. Secret: client id key (blank: azure.yaml)")
dbutils.widgets.text("password_key", "", "8. Secret: client secret key (blank: azure.yaml)")

# COMMAND ----------

import re

W = {k: dbutils.widgets.get(k).strip() for k in (
    "sql_file", "env", "mode", "server", "sql_database", "secret_scope", "username_key",
    "password_key")}  # fmt: skip
S = azure_sql_settings(W["env"], server=W["server"], database=W["sql_database"],
                       secret_scope=W["secret_scope"], username_key=W["username_key"],
                       password_key=W["password_key"])  # fmt: skip
engine = sql_engine(S)

path = Path(W["sql_file"])
if not path.is_absolute():
    root = repo_root()
    assert root, "repo not found: give the script's absolute path"
    path = root / path
batches = []
for part in re.split(r"(?im)^\s*GO\s*$", path.read_text(encoding="utf-8")):
    code = [ln for ln in part.splitlines() if ln.strip() and not ln.strip().startswith("--")]
    if code:
        batches.append(part.strip())
script = path.read_text(encoding="utf-8")
schemas = sorted(set(re.findall(r"SCHEMA_ID\(N'([^']+)'\)", script))
                 | set(re.findall(r"OBJECT_ID\(N'([^'.]+)\.", script)))  # fmt: skip
print(f"{path.name}: {len(batches)} batches, schemas {schemas}, on {S['server']} / "
      f"{S['database']} ({W['env']})")  # fmt: skip


def tables_now() -> dict[str, int]:
    names = ", ".join(f"'{s}'" for s in schemas) or "''"
    return {r["name"]: r["n"] for r in query(engine, f"""
        SELECT s.name, COUNT(t.object_id) AS n FROM sys.schemas s
        LEFT JOIN sys.tables t ON t.schema_id = s.schema_id
        WHERE s.name IN ({names}) GROUP BY s.name""")}  # fmt: skip


print("tables now:", tables_now() or "schemas not created yet")

# COMMAND ----------

for i, b in enumerate(batches, 1):
    first = next(ln for ln in b.splitlines() if ln.strip() and not ln.strip().startswith("--"))
    print(f"{i:3}. {first.strip()[:110]}")
if W["mode"] != "run":
    dbutils.notebook.exit(f"dry run: {len(batches)} batches; set mode = run")

# COMMAND ----------

for i, b in enumerate(batches, 1):
    try:
        run_sql(engine, b)
    except Exception:
        print(f"batch {i} failed:\n{b[:2000]}")
        raise
print(f"done: {len(batches)} batches; tables now: {tables_now()}")
