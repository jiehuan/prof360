# Azure SQL from Databricks: create the tables, copy SQLite data

Three notebooks in `scripts/databricks/` (Databricks source format):

| Notebook | Does |
|---|---|
| `azure_sql_connect` | Shared connection, loaded by the other two with `%run` |
| `run_sql_script` | Runs a DDL script from `deploy/sql/` (creates schemas and tables) |
| `copy_sqlite_to_azure` | One-time copy of `data/uw.db` or `data/app.db` into Azure SQL, keeping row ids |

## How they connect

The same way as the team's loss-run project (D-057):

- `mssql+pyodbc` with `ODBC Driver 17 for SQL Server` and `ActiveDirectoryServicePrincipal`.
- The service principal's id and secret come from a Databricks secret scope. They are never
  printed.
- Server, database, schemas, the scope and the key names come from `config/azure.yaml` for the
  chosen environment (`dv`, `pp`, `pr`). Every notebook has widgets that override them.
- The server certificate is checked (`trust_server_certificate: false` in azure.yaml). If the
  connection fails with a certificate error on your cluster, set it to `true`, as the loss-run
  project does.

## Before you start

1. **Repo in Databricks.** Check the repo out as a Git folder (Repos) and open the notebooks
   there. They find `config/azure.yaml` and `deploy/sql/` by walking up from the notebook. If
   you import the notebooks on their own instead, put `azure_sql_connect` in the same folder
   and fill in all the widgets.
2. **Cluster.** It needs the ODBC Driver 17 for SQL Server, as the loss-run clusters have. The
   notebooks `%pip install pyodbc pyyaml` themselves. Any access mode works.
3. **azure.yaml.** Check the values marked `TO EDIT` / `TO VERIFY` for your environment:
   server, database, schemas, `secret_scope`, `username_key`, `password_key`.
4. **Permissions.** The service principal needs, for the duration of the setup:
   - `CREATE SCHEMA` and `CREATE TABLE`, for the DDL and the copy's staging tables;
   - `ALTER` on the target tables, for `IDENTITY_INSERT` and turning constraints off and on;
   - `INSERT` and `SELECT`;
   - `VIEW DEFINITION`, to read column metadata.

   `db_ddladmin` + `db_datareader` + `db_datawriter` covers all of this; revoke afterwards.
   Daily pipeline runs need only read and write.

## 1. Create the tables: `run_sql_script`

The scripts in `deploy/sql/` separate batches with `GO` lines. Only SSMS and sqlcmd understand
`GO`, so neither Databricks SQL nor a plain pyodbc call can run the files as they are. The
notebook splits them on `GO` and runs each batch in order.

1. Set `sql_file` to a repo path and `env` to the environment:
   - `deploy/sql/pipeline_schema_full.sql`: the pipeline's tables;
   - `deploy/sql/app_schema_full.sql`: the app tables, in a new environment;
   - `deploy/sql/app_schema_changes.sql`: the changes only, when the team's data model already
     has the app tables.
2. Run with `mode = dry_run`. It lists the batches (one line each) and the tables already in
   the script's schemas.
3. Run with `mode = run`. It prints the table count per schema at the end.

Every batch creates only what is missing (`IF SCHEMA_ID(...) IS NULL`,
`IF OBJECT_ID(...) IS NULL`), so a rerun is safe. On an error the notebook prints the failing
batch and stops; the batches before it stay applied.

The scripts carry the schema names from `config/azure.yaml` (`dv`). If another environment uses
other names, edit azure.yaml and run `scripts/export_app_ddl.py` first.

Other ways to run the same files: SSMS or Azure Data Studio (open the file, connect, run), or
`sqlcmd -S <server> -d <database> --authentication-method ActiveDirectoryServicePrincipal
-U <client id> -i deploy/sql/pipeline_schema_full.sql`.

## 2. Copy data: `copy_sqlite_to_azure`

Use it to move results produced locally (SQLite) into Azure SQL once, for example to seed a new
environment without rerunning the pipeline. The normal path is still the pipeline writing to
Azure SQL itself (`DATABASE_BACKEND=azure_sql`, OPERATIONS §10).

- Copy both files, and take them at the same moment (after a run has finished). `app.db` holds
  `pipeline_publish_map`, so later `publish` runs against Azure SQL update the copied rows
  instead of adding duplicates.
- Identity columns continue after the highest copied id, so later pipeline runs insert normally.
- The target tables must be empty: the notebook refuses to load into a table that has rows.

Steps:

1. **Snapshot.** To copy a file while the pipeline is running, take a consistent snapshot
   first: `sqlite3 data/uw.db ".backup uw-copy.db"`.
2. **Upload** the `.db` file to a Unity Catalog volume.
3. **Widgets.** Fill in `database` (pipeline or app), the SQLite path and `env`. Leave the rest
   blank to use azure.yaml.
4. **Dry run** (`mode = dry_run`). Nothing is written. It prints, per table:
   - `copy`: rows and columns to copy (SQLite-only columns are listed and left out);
   - `skip`: in SQLite but not in the DDL, e.g. local `User*` tables in `app.db`;
   - `empty`: in the target but not in SQLite;
   - `PROBLEM`: a required target column missing from SQLite, or a target that already has
     rows. The run stops on any problem.
5. **Load** (`mode = load`), in five steps:
   1. Stages each table as text in `stg_uw_copy`. Columns are sized from the longest value;
      long text columns use `NVARCHAR(MAX)` and go in smaller batches.
   2. Turns off foreign keys and checks on the target tables.
   3. Inserts each table with `IDENTITY_INSERT`, converting decimals through `FLOAT` and dates
      and times through `DATETIMEOFFSET`.
   4. Turns every constraint back on `WITH CHECK`, even if a table failed, so broken keys show
      up as errors.
   5. Compares row counts with SQLite and drops the staging schema.

### If it fails

- **Before the load step** (dry run, staging): fix the cause and run again; the target is
  untouched and staging tables are re-created.
- **During or after the load:** some target tables now have rows, so a rerun will refuse.
  Empty the target tables (or re-create them from the DDL), then run again.
- **Row count mismatch:** the staging tables are kept for comparison. Drop the `stg_uw_copy`
  schema by hand when done.

## Validation

Both notebooks were run on 2026-10-21 against SQL Server 2022 in Docker (the container of
`scripts/test_mssql.sh`). The test used a stub for `dbutils` and a SQL login instead of the
service principal; everything else ran unchanged.

- **`run_sql_script`:** `pipeline_schema_full.sql` (62 batches) and `app_schema_full.sql` (31)
  created 40 and 24 tables. A second run changed nothing.
- **`copy_sqlite_to_azure`, app:** 24 tables, row counts match, 27 seconds.
- **`copy_sqlite_to_azure`, pipeline:** 39 tables (the 40th is empty in SQLite), row counts
  match. The largest leaf tables were capped at 30,000 rows for the test.
- **Spot checks** matched SQLite for:
  - filing texts of up to 995,000 characters;
  - timestamps with microseconds;
  - floats, booleans, dates.

Not testable outside Databricks: the secret scope, the volume path, and Azure AD service
principal login. Run the dry runs first.
