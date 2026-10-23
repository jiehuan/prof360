# Databricks notebook source
# MAGIC %md
# MAGIC # Azure SQL connection (shared)
# MAGIC
# MAGIC Loaded by the other notebooks in this folder with `%run ./azure_sql_connect`. Connects like
# MAGIC the team's other Databricks projects (D-057): `mssql+pyodbc`, the service principal's id and
# MAGIC secret from a Databricks secret scope, `ActiveDirectoryServicePrincipal`. Server, database,
# MAGIC schemas and secret key names come from `config/azure.yaml` of the checked-out repo; a
# MAGIC notebook's widgets can override them. Needs the ODBC Driver 17 for SQL Server on the
# MAGIC cluster and `pyodbc`, `pyyaml` (the calling notebook installs them).

# COMMAND ----------

from pathlib import Path

import yaml
from sqlalchemy import create_engine
from sqlalchemy.engine import URL, Engine
from sqlalchemy.pool import NullPool

REQUIRED = ("server", "database", "secret_scope", "username_key", "password_key")


def repo_root() -> Path | None:
    """The checked-out repo (the folder holding config/azure.yaml) above this notebook."""
    for d in (Path.cwd(), *Path.cwd().parents):
        if (d / "config" / "azure.yaml").is_file():
            return d
    return None


def azure_sql_settings(env: str, **overrides: str) -> dict:
    """config/azure.yaml's Azure SQL settings for `env` (dv, pp, pr), with the non-blank
    `overrides` (server, database, secret_scope, username_key, password_key, ...)."""
    root = repo_root()
    cfg = yaml.safe_load((root / "config" / "azure.yaml").read_text()) if root else {}
    sql = (cfg or {}).get("azure_sql", {})
    s = {
        "driver": sql.get("driver", "ODBC Driver 17 for SQL Server"),
        "authentication": sql.get("authentication", "ActiveDirectoryServicePrincipal"),
        "trust_server_certificate": bool(sql.get("trust_server_certificate", False)),
        "port": 1433,
        **sql.get("environments", {}).get(env, {}),
    }
    s.update({k: v for k, v in overrides.items() if v})
    missing = [k for k in REQUIRED if not s.get(k)]
    assert not missing, f"not in config/azure.yaml for {env} nor in the widgets: {missing}"
    return s


def sql_engine(s: dict) -> Engine:
    """Engine for `azure_sql_settings`; the service principal is read from the secret scope
    and never printed. No connection pool: every statement gets a new session, so a session
    setting left behind by a failed batch (SET IDENTITY_INSERT ... ON) cannot break the next
    table."""
    return create_engine(
        URL.create(
            "mssql+pyodbc",
            username=dbutils.secrets.get(scope=s["secret_scope"], key=s["username_key"]),
            password=dbutils.secrets.get(scope=s["secret_scope"], key=s["password_key"]),
            host=s["server"],
            port=int(s["port"]),
            database=s["database"],
            query={
                "driver": s["driver"],
                "authentication": s["authentication"],
                "Encrypt": "yes",
                "TrustServerCertificate": "yes" if s["trust_server_certificate"] else "no",
            },
        ),
        fast_executemany=True,
        poolclass=NullPool,
    )


def query(engine: Engine, sql: str) -> list[dict]:
    """Rows of one T-SQL query, as dicts."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute(sql)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
    finally:
        raw.close()


def run_sql(engine: Engine, sql: str) -> None:
    """Run a T-SQL batch (several statements allowed) and commit. Every result is read: pyodbc
    reports an error in a later statement of a batch only then. SET NOCOUNT ON goes first, on
    its own: CREATE FUNCTION / PROCEDURE / VIEW / TRIGGER must be the first statement of their
    batch, and the setting holds for the session."""
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("SET NOCOUNT ON;")
        cur.execute(sql)
        while cur.nextset():
            pass
        raw.commit()
    finally:
        raw.close()
