# Copy a SQLite database to Azure SQL (Databricks)

Notebook: `scripts/databricks/copy_sqlite_to_azure.py` (Databricks source format; import it with
**Workspace > Import**).

It copies one local database into an Azure SQL schema in a single pass and keeps every row id:

| `database` widget | SQLite file | Default target schema |
|---|---|---|
| `pipeline` | `data/uw.db` | `uw_pipeline` |
| `app` | `data/app.db` | `dbo_professional360` |

## When to use it

Use it to move results produced locally (SQLite) into Azure SQL once, for example to seed a new
environment without rerunning the pipeline. The normal path is still the pipeline writing to Azure
SQL itself (`DATABASE_BACKEND=azure_sql`, OPERATIONS §10).

- Copy both files, and take them at the same moment (after a run has finished). `app.db` holds
  `pipeline_publish_map`, so later `publish` runs against Azure SQL update the copied rows instead
  of adding duplicates.
- Identity columns continue after the highest copied id, so later pipeline runs insert normally.
- The target tables must be empty. The notebook refuses to load into a table that has rows.

## Before you start

1. **Tables.** An engineer creates them with `deploy/sql/pipeline_schema_full.sql` and
   `deploy/sql/app_schema_full.sql`. The scripts use the schema names in `config/azure.yaml`; if
   you use other names, change that file and run `scripts/export_app_ddl.py` first.
2. **File.** Upload `uw.db` / `app.db` to a Unity Catalog volume. To copy a file while the
   pipeline is running, take a consistent snapshot first:
   `sqlite3 data/uw.db ".backup uw-copy.db"`.
3. **Cluster.** Single-user (dedicated) access mode. Shared mode blocks the JDBC connection the
   notebook uses for T-SQL statements. The notebook installs `azure-identity` itself.
4. **Secrets.** A secret scope (default `uw`) with the service principal's `tenant-id`,
   `sp-client-id` and `sp-client-secret`. Secrets are read with `dbutils.secrets.get` and never
   printed.
5. **Permissions.** The service principal needs, for the duration of the copy: `CREATE SCHEMA`
   and `CREATE TABLE` (staging), `ALTER` on the target tables (needed for `IDENTITY_INSERT` and
   for disabling constraints), `INSERT` and `SELECT`, and `VIEW DEFINITION` to read column
   metadata. `db_ddladmin` + `db_datareader` + `db_datawriter` covers this; revoke after.

## Running it

1. Fill in the widgets: database, SQLite path, server (`*.database.windows.net`), database name.
   Leave target schema blank for the default.
2. Run all with `mode = dry_run`. Nothing is written. It prints, per table:
   - `copy`: rows and columns to copy (SQLite-only columns are listed and left out);
   - `skip`: in SQLite but not in the DDL (e.g. local `User*` tables in `app.db`);
   - `empty`: in the target but not in SQLite;
   - `PROBLEM`: a required target column missing from SQLite, or a target that already has rows.
     The run stops on any problem.
3. Run all again with `mode = load`.

The load runs in four steps:

1. Stages each table as text in `stg_uw_copy`, 100,000 rows per write.
2. Disables foreign keys and checks on the target tables.
3. Inserts each table with `IDENTITY_INSERT`, converting decimals through `FLOAT` and dates/times
   through `DATETIMEOFFSET`.
4. Re-enables every constraint `WITH CHECK`, even if a table failed, so broken keys show up as
   errors.

It then compares row counts with SQLite and drops the staging schema. A run takes minutes for
the current data size (largest table: `xbrl_fact`, about 600,000 rows).

## If it fails

- **Before the load step** (dry run, staging): fix the cause and run again; the target is
  untouched and staging tables are overwritten.
- **During or after the load:** some target tables now have rows, so a rerun will refuse.
  Empty the target tables (or re-create them from the DDL), then run again.
- **Row count mismatch:** the staging tables are kept for comparison. Drop the
  `stg_uw_copy` schema by hand when done.
- **Expired token:** the Azure AD token lasts about an hour. If the load takes longer, rerun
  from the top after emptying the target.

## Validation

The notebook's SQL was replayed against SQL Server 2022 in Docker (the same container as
`scripts/test_mssql.sh`), using snapshots of the local databases on 2026-10-20:

| Database | Tables | Rows | Insert errors | Constraint errors | Count mismatches |
|---|---|---|---|---|---|
| `app` | 24 | 103,778 | 0 | 0 | 0 |
| `pipeline` | 40 | 362,037 | 0 | 0 | 0 |

- The replay capped the largest leaf tables at 30,000 rows.
- Spot checks matched SQLite for dates, datetimes with 6 decimal places, decimals, booleans and
  JSON text.
- The Spark parts (volume copy, JDBC staging writes, secret scope) can only be checked in
  Databricks. Run the dry run first.
