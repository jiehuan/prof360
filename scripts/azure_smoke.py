"""Smoke test for Azure SQL and ADLS before the first run on the corporate network (D-040).

    export AZURE_ENV=dv AZURE_SQL_CLIENT_ID=... AZURE_SQL_CLIENT_SECRET=...
    export ADLS_CLIENT_ID=... ADLS_CLIENT_SECRET=...
    DATABASE_BACKEND=azure_sql STORAGE_BACKEND=adls python scripts/azure_smoke.py

Checks, without changing any pipeline or application table:
- both databases connect (service principal), and the configured schemas exist;
- which pipeline / application tables are present (were the deploy/sql scripts applied?);
- SELECT / INSERT / UPDATE / DELETE permission on each schema (ALTER and CREATE TABLE are
  reported as warnings: only `uw-pipeline init-db`-style table creation needs them);
- ADLS: write, read and delete one small file under the configured root.
Credentials are never printed. Exit code 0 when everything needed is in place.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime

from sqlalchemy import inspect, text

from uw_pipeline.clients.db import app_engine, pipeline_engine, table_schema
from uw_pipeline.clients.storage import make_storage
from uw_pipeline.schema import metadata
from uw_pipeline.schema_app import app_metadata
from uw_pipeline.settings import Settings


def check_db(label: str, engine, expected: set[str], create: bool) -> bool:  # type: ignore[no-untyped-def]
    ok = True
    try:
        with engine.connect() as c:
            schema = table_schema(c) or "dbo"
            version = c.execute(text("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar)"))
            print(f"OK    {label}: connected (SQL Server {version.scalar()}), schema [{schema}]")
            if not inspect(c).has_schema(schema):
                print(f"FAIL  {label}: schema [{schema}] does not exist")
                return False
            present = {t.lower() for t in inspect(c).get_table_names(schema=schema)}
            missing = sorted(t for t in expected if t.lower() not in present)
            if missing:
                ok = False
                print(f"FAIL  {label}: {len(missing)} of {len(expected)} tables missing, e.g. "
                      f"{', '.join(missing[:5])} (apply deploy/sql/*.sql)")  # fmt: skip
            else:
                print(f"OK    {label}: all {len(expected)} tables present")
            for perm in ["SELECT", "INSERT", "UPDATE", "DELETE"]:
                has = c.execute(text("SELECT HAS_PERMS_BY_NAME(:s, 'SCHEMA', :p)"),
                                {"s": schema, "p": perm}).scalar()  # fmt: skip
                print(f"{'OK  ' if has else 'FAIL'}  {label}: {perm} on [{schema}]")
                ok = ok and bool(has)
            if create:  # runs use #temp tables only; creating the tables themselves needs these
                has_alter = c.execute(text("SELECT HAS_PERMS_BY_NAME(:s, 'SCHEMA', 'ALTER')"),
                                      {"s": schema}).scalar()  # fmt: skip
                has_create = c.execute(text("SELECT HAS_PERMS_BY_NAME(DB_NAME(), 'DATABASE', "
                                            "'CREATE TABLE')")).scalar()  # fmt: skip
                for perm, has in ((f"ALTER on [{schema}]", has_alter),
                                  ("CREATE TABLE", has_create)):  # fmt: skip
                    print(f"{'OK  ' if has else 'WARN'}  {label}: {perm} (only needed if the "
                          "pipeline creates its own tables)")  # fmt: skip
    except Exception as exc:  # report and continue with the other checks
        print(f"FAIL  {label}: {type(exc).__name__}: {str(exc)[:300]}")
        return False
    return ok


def check_adls(settings: Settings) -> bool:
    try:
        storage = make_storage(settings)
        key = f"_smoke/{datetime.now(UTC):%Y%m%dT%H%M%S}.txt"
        locator = storage.write_bytes(key, b"uw-pipeline smoke test")
        ok = storage.exists(key) and storage.read_bytes(key) == b"uw-pipeline smoke test"
        storage.delete(key)
        ok = ok and not storage.exists(key)
        print(f"{'OK  ' if ok else 'FAIL'}  ADLS: write/read/delete {locator}")
        return ok
    except Exception as exc:
        print(f"FAIL  ADLS: {type(exc).__name__}: {str(exc)[:300]}")
        return False


def main() -> int:
    s = Settings()
    print(f"database backend {s.database_backend}, storage backend {s.storage_backend}, "
          f"environment {s.azure_env or '(config default)'}\n")  # fmt: skip
    results = [
        check_db("pipeline DB", pipeline_engine(s), set(metadata.tables), create=True),
        check_db("app DB", app_engine(s), set(app_metadata.tables), create=False),
    ]
    if s.storage_backend == "adls":
        results.append(check_adls(s))
    else:
        print("SKIP  ADLS: STORAGE_BACKEND is not adls")
    print("\nAll checks passed." if all(results) else "\nSome checks failed (see above).")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
