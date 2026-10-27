# Databricks notebook source
# MAGIC %md
# MAGIC # Company logos into Azure SQL `Company.logo`
# MAGIC
# MAGIC Writes the logos kept in the repo (`config/logos/<TICKER>.png`, D-067) to `Company.logo`
# MAGIC as data URIs (`data:image/png;base64,...`), the same values the pipeline's `publish`
# MAGIC writes. Companies are matched by CIK (`config/companies.yaml` pins each ticker's CIK).
# MAGIC Only that column is written: no rows are inserted or deleted, other columns are not
# MAGIC touched, and a company without a logo file keeps what it has. Running it again changes
# MAGIC only logos whose file changed.
# MAGIC
# MAGIC 1. The `Company.logo` column must be `NVARCHAR(MAX)` (`deploy/sql/app_schema_changes.sql`).
# MAGIC 2. Pull the Git folder to a branch that has `config/logos/` (`phase17` or later).
# MAGIC 3. Run with `mode = dry_run`, read the counts, then `mode = apply`.
# MAGIC
# MAGIC Connects with `azure_sql_connect` (D-057), like `copy_sqlite_to_azure`.

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.dropdown("env", "dv", ["dv", "pp", "pr"], "1. Environment (config/azure.yaml)")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "apply"], "2. Mode")
dbutils.widgets.text("target_schema", "", "3. App schema (blank: from azure.yaml)")
# Blank: from config/azure.yaml for the environment.
dbutils.widgets.text("server", "", "4. Server (blank: azure.yaml)")
dbutils.widgets.text("sql_database", "", "5. Database (blank: azure.yaml)")
dbutils.widgets.text("secret_scope", "", "6. Secret scope (blank: azure.yaml)")
dbutils.widgets.text("username_key", "", "7. Secret: client id key (blank: azure.yaml)")
dbutils.widgets.text("password_key", "", "8. Secret: client secret key (blank: azure.yaml)")

# COMMAND ----------

import sys

import yaml

W = {k: dbutils.widgets.get(k).strip() for k in (
    "env", "mode", "target_schema", "server", "sql_database", "secret_scope", "username_key",
    "password_key")}  # fmt: skip
S = azure_sql_settings(W["env"], server=W["server"], database=W["sql_database"],
                       secret_scope=W["secret_scope"], username_key=W["username_key"],
                       password_key=W["password_key"])  # fmt: skip
TARGET = W["target_schema"] or S.get("app_schema") or "dbo_professional360"
engine = sql_engine(S)
print(f"target {S['server']} / {S['database']} / schema {TARGET} ({W['env']})")

ROOT = repo_root()
assert ROOT, "repo not found: the logos come from its config/logos/"
sys.path.insert(0, str(ROOT / "src"))
from uw_pipeline.logos import load_logos  # the pipeline's own checks and format

logos = load_logos(ROOT / "config")  # ticker -> data URI; files that fail the checks are left out
assert logos, "no logos in config/logos/: pull the Git folder to a branch that has them"
companies = yaml.safe_load((ROOT / "config" / "companies.yaml").read_text())["companies"]
cik_of = {c["ticker"]: f"{int(c['cik']):010d}" for c in companies if c.get("cik")}
rows = [(cik_of[t], t, uri) for t, uri in sorted(logos.items()) if t in cik_of]
print(f"{len(logos)} logo files, {len(rows)} for companies in companies.yaml")

col = query(
    engine,
    f"""
    SELECT CHARACTER_MAXIMUM_LENGTH AS n FROM INFORMATION_SCHEMA.COLUMNS
    WHERE TABLE_SCHEMA = '{TARGET}' AND TABLE_NAME = 'Company' AND COLUMN_NAME = 'logo'""",
)
assert col, f"{TARGET}.Company has no logo column"
assert col[0]["n"] == -1, (
    f"{TARGET}.Company.logo is NVARCHAR({col[0]['n']}): run deploy/sql/app_schema_changes.sql "
    "first (it makes the column NVARCHAR(MAX))"
)

# COMMAND ----------

# MAGIC %md ## Compare, then write (one connection: the logos go to a temporary table)

# COMMAND ----------

company = f"[{TARGET}].[Company]"
raw = engine.raw_connection()
try:
    cur = raw.cursor()
    cur.execute("SET NOCOUNT ON;")
    cur.execute("CREATE TABLE #logo (CIK NVARCHAR(10) PRIMARY KEY, Ticker NVARCHAR(16), "
                "logo NVARCHAR(MAX) NOT NULL)")  # fmt: skip
    cur.fast_executemany = False  # NVARCHAR(MAX) values: row by row
    cur.executemany("INSERT INTO #logo (CIK, Ticker, logo) VALUES (?, ?, ?)", rows)
    cur.execute(f"""
        SELECT
            SUM(CASE WHEN S.CIK IS NOT NULL AND C.logo IS NULL THEN 1 ELSE 0 END) AS new_logo,
            SUM(CASE WHEN S.CIK IS NOT NULL AND C.logo <> S.logo THEN 1 ELSE 0 END) AS changed,
            SUM(CASE WHEN S.CIK IS NOT NULL AND C.logo = S.logo THEN 1 ELSE 0 END) AS unchanged,
            SUM(CASE WHEN S.CIK IS NULL THEN 1 ELSE 0 END) AS no_file,
            COUNT(*) AS companies
        FROM {company} C LEFT JOIN #logo S ON S.CIK = C.CIK""")
    counts = dict(zip([d[0] for d in cur.description], cur.fetchone(), strict=True))
    print(f"Company rows: {counts['companies']}; logo added {counts['new_logo']}, replaced "
          f"{counts['changed']}, already the same {counts['unchanged']}; no logo file "
          f"{counts['no_file']} (left as they are)")  # fmt: skip
    cur.execute(f"""SELECT C.NameAbbreviation FROM {company} C
                    WHERE NOT EXISTS (SELECT 1 FROM #logo S WHERE S.CIK = C.CIK)
                    ORDER BY C.NameAbbreviation""")  # fmt: skip
    missing = [r[0] for r in cur.fetchall()]
    if missing:
        print("no logo file:", ", ".join(missing))
    if W["mode"] == "apply":
        cur.execute(f"""
            UPDATE C SET C.logo = S.logo
            FROM {company} C JOIN #logo S ON S.CIK = C.CIK
            WHERE C.logo IS NULL OR C.logo <> S.logo""")
        raw.commit()
        print(f"written: {counts['new_logo'] + counts['changed']} companies")
    else:
        raw.rollback()
        print("dry run: nothing written; set mode = apply")
finally:
    raw.close()

# COMMAND ----------

after = query(engine, f"SELECT COUNT(*) AS n, COUNT(logo) AS with_logo FROM {company}")[0]
print(f"{company}: {after['with_logo']} of {after['n']} companies have a logo")
