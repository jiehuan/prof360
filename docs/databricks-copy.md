# Azure SQL from Databricks: create the tables, copy SQLite data

Six notebooks in `scripts/databricks/` (Databricks source format):

| Notebook | Does |
|---|---|
| `azure_sql_connect` | Shared connection, loaded by the others with `%run` |
| `run_sql_script` | Runs a script from `deploy/sql/`: creates schemas and tables, or drops them (`*_drop.sql`) |
| `copy_sqlite_to_azure` | Copies `data/uw.db` or `data/app.db` into Azure SQL, keeping row ids: `load` once, then `sync` after each pipeline run (inserts, updates and deletes by primary key) |
| `run_query` | Shows the result of a read-only query (a repo file such as `deploy/sql/checks/operational_metrics.sql`, or typed in) on an environment (section 4) |
| `sync_sql_objects` | Copies every function and procedure of a schema from one environment to another (e.g. `dbo_professional360`, `dv` → `pp`) through a file: `export` in the source workspace, `import` (`dry_run`, then `apply`) in the target (section 3) |
| `merge_company_logos` | Writes the logo files of `config/logos/` to `Company.logo` as data URIs, matched by CIK (D-067); only that column, only changed logos. Needs `Company.logo` as `NVARCHAR(MAX)` (`app_schema_changes.sql`). `dry_run`, then `apply` |

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

To start over, `deploy/sql/pipeline_schema_drop.sql` and `app_schema_drop.sql` (generated
with the DDL) drop exactly the tables of the full scripts, children first, plus the copy
notebook's staging schema; the pipeline one also drops `uw_pipeline` once it is empty. Other
tables in the app schema (`Users`, `User*`, owned by the BFF) are not touched, and a foreign key
from one of them stops the script at the table it references. Run them with this notebook like
any script (dry run first), then the full script to create the tables again. They delete data:
never on an environment the pipeline or the BFF uses. After a failed copy, `clear_target` in the
copy notebook is usually enough (it keeps the tables and empties them).

Other ways to run the same files: SSMS or Azure Data Studio (open the file, connect, run), or
`sqlcmd -S <server> -d <database> --authentication-method ActiveDirectoryServicePrincipal
-U <client id> -i deploy/sql/pipeline_schema_full.sql`.

## 2. Copy data: `copy_sqlite_to_azure`

Use it to move results produced in SQLite into Azure SQL: `load` once into empty tables, then
`sync` after each pipeline run (D-064). Writing to Azure SQL directly from the pipeline
(`DATABASE_BACKEND=azure_sql`, OPERATIONS §10) needs no copy at all.

- Copy both files, and take them at the same moment (after a run has finished). `app.db` holds
  `pipeline_publish_map`, so later `publish` runs against Azure SQL update the copied rows
  instead of adding duplicates.
- Identity columns continue after the highest copied id, so later pipeline runs insert normally.
- `load` needs empty target tables: it refuses to load into a table that has rows.
- `sync` needs a target filled by `load` (the same ids) and written by nothing else in these
  tables.
- Every mode (`load`, `sync`, `clear_target`) touches only the pipeline's own tables: those the
  repo's DDL for the database creates (`deploy/sql/app_schema_full.sql` or
  `pipeline_schema_full.sql`). The application's tables (`Users`, `UserFavority`,
  `UserPinnedCompanies`, `UserHistory`, `UserFlaggedFollowupQuestions`, `UserMetricsSelection`,
  `UserSIC`, any other `User*`) are listed as `skip` and never copied, synced or cleared, even
  when the SQLite file has tables of that name (a local `app.db` from the BFF prototype does).

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

### Daily updates: `mode = sync`

After each pipeline run, upload the new `.db` file and run with `mode = sync`. The target then
equals the file, table by table:

1. Stages each table as text, as `load` does (empty tables too: their target is emptied).
2. Turns off foreign keys and checks on the target tables.
3. Runs one `MERGE` per table by primary key: rows new in the file are inserted with their ids,
   rows whose columns differ are updated (`EXCEPT`, so NULLs compare right), rows gone from
   the file are deleted. Values are converted exactly as `load` converts them, so an unchanged
   row is never rewritten. The pipeline deletes rows only from tables it rebuilds (metric
   values, links, activities, news, trends, loan classes); versioned content (key insights,
   risks, follow-up questions, takeaways) keeps its rows with `isActive = 0`, so users'
   favorites and flags keep pointing at existing rows.
4. Turns every constraint back on `WITH CHECK`, compares row counts and drops the staging
   schema. Each table prints `synced <table>: +inserted, updated, -deleted`; the last line
   gives the totals.

`keep_columns` (default empty) lists target columns the application writes: `sync` fills them
for new rows and never overwrites them. `Company.logo` is written by the pipeline since D-067;
before the first sync with logos, run `deploy/sql/app_schema_changes.sql` (it widens the column
to `NVARCHAR(MAX)`).

A sync running while the BFF reads sees each table switch as its `MERGE` commits; run it after
the pipeline run, outside working hours.

### If it fails

- **Before the load step** (dry run, staging): fix the cause and run again; the target is
  untouched and staging tables are re-created.
- **Row count mismatch:** the staging tables are kept for comparison. Drop the `stg_uw_copy`
  schema by hand when done.
- **Run again after a successful load** (e.g. Run all once more): the plan cell sees every
  table already holding the SQLite file's row count and stops with `already loaded`; nothing
  is written. Check that the load cell's last line reads `done: … row counts match`.
- **After a failed load** some target tables have rows and a rerun refuses. Run the notebook
  with `mode = clear_target` (empties the tables of this copy, re-enables their constraints,
  drops the staging schema), then run the whole notebook again; never rerun the load cell on
  its own (it now checks and refuses). The first error printed (`FAILED to load <table>`) is
  the cause; constraint messages after it are consequences.
- **`clear_target` is only for a target this copy filled and nothing else uses.** It refuses
  when rows of other tables (the `User*` tables, tables the BFF team added such as
  `CompanyKeyInsightsSummary`) reference the tables it would empty: such a target is in use,
  update it with `sync`. It runs as one transaction, so it empties every table or, on any
  error, none, and the constraints are back on either way.
- **`another run is using the staging schema`:** two runs of the notebook were staging at the
  same time (e.g. Run all twice, or a widget change that reruns the notebook: set the widget
  panel's "On widget change" to "Do nothing"). Nothing was written to the target. Let the
  other run finish or stop it, then run once. Before this check (2026-10-29) the same overlap
  showed up as duplicate primary keys in the load (`Cannot insert duplicate key ... Activities`).

## 3. Functions and procedures to another environment: `sync_sql_objects`

The BFF reads the app tables through functions and procedures, some from `deploy/sql/bff_*.sql`,
some written by the BFF team directly in `dv`. To give `pp` (or `pr`) the same set, copy them
from the database rather than from the repo's files. One environment cannot reach the other,
so the notebook runs twice, once in each workspace, with a file in between:

1. **Export, in the `dv` workspace:** `action = export`, `env = dv`, `file` a `.json` path the
   notebook can write, by default `/dbfs/FileStore/professional360/sql_objects.json`
   (or a Unity Catalog volume, `/Volumes/...`). It writes that file (every function and
   procedure in dependency order, with the tables each reads) and the same name with `.sql`.
   `only` limits it to a comma-separated list of names. Download the JSON file (FileStore:
   `https://<workspace>/files/professional360/sql_objects.json`; a volume: Catalog Explorer).
2. **Upload** it in the `pp` workspace (to FileStore or a volume).
3. **Import, dry run, in the `pp` workspace:** `action = import`, `env = pp`, `file` = the
   uploaded path, `mode = dry_run`. Lists each object as `new`, `changed` or `kind changed`
   (e.g. inline in the file, multi-statement here), in the order they will be created: a
   function before the functions and procedures that call it. Objects only in `pp` are listed
   and left as they are. `PROBLEM` lines name tables the objects read that `pp` lacks: create
   them first (e.g. `UserNotificationRead` with `deploy/sql/bff_notifications.sql` in
   `run_sql_script`), or those objects fail.
4. **Import, apply:** `mode = apply`. Runs each definition as `CREATE OR ALTER`; an object of
   another kind is dropped first (error 2010 otherwise). A second pass retries what failed. The
   last line must read `done: N of N objects equal to the file`; a failure is printed with its
   error and stops the notebook. Rerunning changes nothing once the two are equal.

The `.sql` file is the same as a plain script (a drop of another kind of object, then
`CREATE OR ALTER`, per object, in order): `run_sql_script` or SSMS can run it instead of step 3–4,
without the comparison and the table check.

The schema defaults to `app_schema` in azure.yaml for each environment (widget `schema`); if
the two differ, the import renames it in the definitions. Only definitions are copied: no
tables, data or permissions (`GRANT EXECUTE` for the BFF's identity is set per environment). The
service principal needs `VIEW DEFINITION` in `dv`, and `CREATE FUNCTION`, `CREATE PROCEDURE` and
`ALTER` on the schema in `pp`.

## 4. Look at the data: `run_query`

To check what an environment holds (e.g. why a company page looks wrong), run a query and see
the result in the notebook: `env`, then `sql_file` (a repo path) or the query typed into `sql`.
`user_id` declares `@UserId` for queries that check a user's page. Read-only: a query with
INSERT, UPDATE, DELETE, MERGE, CREATE, ALTER, DROP, EXEC, SELECT INTO and the like is refused,
and the session is rolled back at the end in any case. Every result set is shown (at most
`max_rows` rows each).

Checks kept in the repo (`deploy/sql/checks/`):

- `operational_metrics.sql`: per company, the operational metrics with data, the industry
  default for its SIC code, the user's saved choice for that SIC code, and what
  `udf_GetCompanyDetails` returns to the page.

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
- **`copy_sqlite_to_azure`, sync (2026-10-24):** `load` of `app.db` (24 tables, 47 s), then
  `sync` of the same file: 0 changes (unchanged rows are not rewritten). A second-day file
  with 5 metric values changed and 10 removed, a key insight retired and one added, an
  activity added, a link removed and a company refreshed: `sync` reported exactly those
  (2 inserted, 7 updated, 11 deleted, about 50 s); a `Company.logo` set in the target was
  kept; a further `sync` changed nothing.
- **User tables (2026-10-26):** a `sync` into a target holding a user's favorites, pin,
  history, metric choice and followed industry, from a file with other rows in its own
  `User*` tables: the 7 `User*` tables were skipped, the target's user rows were unchanged,
  the 24 pipeline tables synced as before.
- **`sync_sql_objects` (2026-10-28):** export of 34 functions and procedures from a `dv`
  stand-in to a file, import into an empty `pp` stand-in holding an outdated function, a
  multi-statement version of an inline one, an extra function and no `UserNotificationRead`
  table. The dry run listed all four cases in dependency order; apply created 32 and stopped on
  the two objects needing the table; after creating it, apply finished (34 of 34 equal, the
  multi-statement one now inline, the extra one kept); a further run changed nothing. The
  exported `.sql` run with sqlcmd on a fresh stand-in (twice) gave the same 34 objects.
- **`copy_sqlite_to_azure` safety (2026-10-29):** after a load of `app.db` (24 tables, row
  counts match): `clear_target` with a row in a BFF-style table referencing `Company` refused
  and changed nothing; with a trigger failing the delete of a late table it rolled back
  entirely (all rows and trusted constraints kept); with nothing in the way it emptied the 24
  tables. Two loads started together both stopped at staging (`Activities`: 7,246 staged for
  4,623 sent), the target untouched.
