# Databricks notebook source
# MAGIC %md
# MAGIC # Run a read-only query on Azure SQL
# MAGIC
# MAGIC Shows the result of a query, from a repo file (e.g. `deploy/sql/checks/*.sql`) or typed
# MAGIC into the `sql` widget, on the chosen environment. Read-only: a query that would write
# MAGIC (INSERT, UPDATE, DELETE, MERGE, CREATE, ALTER, DROP, EXEC, SELECT INTO, ...) is refused,
# MAGIC and the session is rolled back at the end in any case. Every result set is shown.
# MAGIC `user_id` declares `@UserId` for queries that check a user's page. Guide:
# MAGIC `docs/databricks-copy.md`. Connects with `azure_sql_connect` (D-057).

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.dropdown("env", "dv", ["dv", "pp", "pr"], "1. Environment (config/azure.yaml)")
dbutils.widgets.text("sql_file", "deploy/sql/checks/operational_metrics.sql",
                     "2. Query file (repo path; blank: widget 3)")  # fmt: skip
dbutils.widgets.text("sql", "", "3. Query (when no file)")
dbutils.widgets.text("user_id", "", "4. @UserId (blank: not declared)")
dbutils.widgets.text("max_rows", "1000", "5. Rows shown at most")

# COMMAND ----------

import re
from pathlib import Path

import pandas as pd

W = {k: dbutils.widgets.get(k).strip() for k in ("env", "sql_file", "sql", "user_id", "max_rows")}
engine = sql_engine(azure_sql_settings(W["env"]))
if W["sql_file"]:
    path = Path(W["sql_file"])
    if not path.is_absolute():
        root = repo_root()
        assert root, "repo not found: give the file's absolute path"
        path = root / path
    sql = path.read_text(encoding="utf-8")
    source = str(path)
else:
    sql = W["sql"]
    source = "widget sql"
assert sql.strip(), "no query: set sql_file or sql"

# Refuse anything that writes. Comments and string literals are left out of the check.
code = re.sub(r"--[^\n]*|/\*.*?\*/", " ", sql, flags=re.DOTALL)
code = re.sub(r"N?'(?:[^']|'')*'", "''", code)
WRITES = (r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|CREATE|ALTER|DROP|EXEC|EXECUTE|GRANT|"
          r"REVOKE|DENY|COMMIT|BACKUP|RESTORE|DBCC|INTO|OPENROWSET|OPENQUERY)\b")  # fmt: skip
found = sorted({m.upper() for m in re.findall(WRITES, code, flags=re.IGNORECASE)})
assert not found, f"read-only: the query contains {found}"
if W["user_id"]:
    sql = f"DECLARE @UserId INT = {int(W['user_id'])};\n{sql}"
print(f"{source} on {W['env']}")

# COMMAND ----------

raw = engine.raw_connection()
try:
    cur = raw.cursor()
    cur.execute("SET NOCOUNT ON;")
    cur.execute(sql)
    n = 0
    while True:
        if cur.description:
            n += 1
            cols = [d[0] for d in cur.description]
            df = pd.DataFrame.from_records([tuple(r) for r in cur.fetchall()], columns=cols)
            print(f"result {n}: {len(df)} rows")
            display(df.head(int(W["max_rows"] or 1000)))
        if not cur.nextset():
            break
    print(f"{n} result set(s)" if n else "no result set")
finally:
    raw.rollback()  # read-only either way
    raw.close()
