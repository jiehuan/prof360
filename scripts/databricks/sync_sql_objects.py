# Databricks notebook source
# MAGIC %md
# MAGIC # Copy functions and procedures from one environment to another
# MAGIC
# MAGIC Copies every user-defined function (scalar, inline and multi-statement table functions) and
# MAGIC stored procedure of a schema, e.g. `dbo_professional360` from `dv` to `pp`, as they are
# MAGIC defined in the source database (`sys.sql_modules`), including those the BFF team wrote
# MAGIC there. Tables named in `tables` (the BFF's own, e.g. `UserNotificationRead`) are copied
# MAGIC too, with their rows (D-075), and those in `empty_tables` without them (D-086: the
# MAGIC structure only, e.g. the BFF's user tables for `pr`); no other table or data is touched.
# MAGIC One environment cannot reach the other, so it goes through a file, in two runs of this
# MAGIC notebook:
# MAGIC
# MAGIC 1. **`action = export`** in the source workspace (`env = dv`): writes `file` (JSON, the
# MAGIC    objects in dependency order with the tables each reads, and the `tables` with their
# MAGIC    columns, primary key and rows) and the same name with `.sql` (the functions and
# MAGIC    procedures only, a script runnable with `run_sql_script` or SSMS). Download the JSON.
# MAGIC 2. Upload it in the target workspace.
# MAGIC 3. **`action = import`** there (`env = pp`), `mode = dry_run`: lists each table of the file
# MAGIC    (created when missing; rows to insert), each object as new, changed or of another kind
# MAGIC    in the target, objects only in the target (never dropped) and tables the objects read
# MAGIC    that neither the target nor the file has (create them first, e.g. with
# MAGIC    `deploy/sql/bff_notifications.sql`). Then `mode = apply`: the tables first (a missing
# MAGIC    one created with its columns, defaults and primary key; then the file's rows whose key
# MAGIC    is not there yet: rows already in the target are never changed or deleted), then
# MAGIC    `CREATE OR ALTER` in dependency order, an object of another kind in the target dropped
# MAGIC    first (error 2010 otherwise), those already the same left alone; a rerun changes nothing.
# MAGIC
# MAGIC Permissions (`GRANT EXECUTE ...`) are not copied: principals differ between environments.
# MAGIC Nor are a copied table's foreign keys, indexes or triggers: add them with its DDL.
# MAGIC Connects with `azure_sql_connect` (D-057); guide: `docs/databricks-copy.md` section 3.

# COMMAND ----------

# MAGIC %pip install pyodbc pyyaml

# COMMAND ----------

# MAGIC %run ./azure_sql_connect

# COMMAND ----------

dbutils.widgets.dropdown("action", "export", ["export", "import"], "1. Action")
dbutils.widgets.dropdown("env", "dv", ["dv", "pp", "pr"], "2. Environment (export: source)")
dbutils.widgets.text("file", "/dbfs/FileStore/professional360/sql_objects.json", "3. File (JSON)")
dbutils.widgets.dropdown("mode", "dry_run", ["dry_run", "apply"], "4. Import mode")
dbutils.widgets.text("schema", "", "5. Schema (blank: azure.yaml app_schema)")
# Comma-separated object names; blank: all of them.
dbutils.widgets.text("only", "", "6. Only these objects (blank: all)")
# Export: comma-separated table names copied with their rows; blank: none. Import: every table
# in the file.
dbutils.widgets.text("tables", "", "7. Export: tables with their rows (blank: none)")
# Export: comma-separated tables copied without their rows (the structure only), e.g. the BFF's
# user tables for pr, whose users are not dv's (D-086); blank: none.
dbutils.widgets.text("empty_tables", "", "8. Export: tables without rows (blank: none)")

# COMMAND ----------

import json
import re
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from graphlib import CycleError, TopologicalSorter
from pathlib import Path

W = {k: dbutils.widgets.get(k).strip() for k in (
    "action", "env", "file", "mode", "schema", "only", "tables", "empty_tables")}  # fmt: skip
S = azure_sql_settings(W["env"])
SCHEMA = W["schema"] or S.get("app_schema") or "dbo_professional360"
engine = sql_engine(S)
FILE = Path(W["file"])
assert FILE.suffix == ".json", "file: a .json path, e.g. /dbfs/FileStore/... or /Volumes/..."
ONLY = {n.strip() for n in W["only"].split(",") if n.strip()}
TABLES = [n.strip() for n in W["tables"].split(",") if n.strip()]
EMPTY_TABLES = [n.strip() for n in W["empty_tables"].split(",") if n.strip()]
assert not {n.lower() for n in TABLES} & {n.lower() for n in EMPTY_TABLES}, (
    "a table is in both `tables` and `empty_tables`"
)
print(f"{W['action']}: {S['server']} / {S['database']} / {SCHEMA} ({W['env']}); file {FILE}")

KINDS = {"FN": "scalar function", "IF": "inline table function",
         "TF": "multi-statement table function", "P": "procedure"}  # fmt: skip
LEAD = r"(?:\s+|--[^\n]*(?:\n|$)|/\*.*?\*/)*"  # blanks and comments before CREATE
HEAD = re.compile(rf"^({LEAD})CREATE\s+(?:OR\s+ALTER\s+)?(FUNCTION|PROCEDURE|PROC)\b",
                  re.IGNORECASE | re.DOTALL)  # fmt: skip


def modules(schema: str) -> dict[str, dict]:
    """Functions and procedures of `schema` here: name -> {object_id, name, type, definition}."""
    rows = query(engine, f"""
        SELECT o.object_id, o.name, RTRIM(o.type) AS type, m.definition
        FROM sys.objects o
        JOIN sys.schemas s ON s.schema_id = o.schema_id
        LEFT JOIN sys.sql_modules m ON m.object_id = o.object_id
        WHERE s.name = N'{schema}' AND o.type IN ('FN', 'IF', 'TF', 'P')""")  # fmt: skip
    return {r["name"]: r for r in rows}


def create_or_alter(definition: str, src: str, dst: str) -> str:
    """A definition as CREATE OR ALTER, with schema `src` renamed to `dst`."""
    m = HEAD.match(definition)
    assert m, f"no CREATE FUNCTION / PROCEDURE at the start of:\n{definition[:300]}"
    sql = f"{m.group(1)}CREATE OR ALTER {m.group(2)}{definition[m.end() :]}"
    if src.lower() != dst.lower():
        sql = re.sub(rf"(\[{re.escape(src)}\]|\b{re.escape(src)}\b)\.", f"[{dst}].", sql,
                     flags=re.IGNORECASE)  # fmt: skip
    return sql


def same(a: str, b: str) -> bool:
    """Equal apart from line endings, trailing blanks and CREATE vs CREATE OR ALTER."""

    def norm(s: str) -> str:
        s = HEAD.sub(lambda m: f"{m.group(1)}CREATE {m.group(2).upper()}", s, count=1)
        return "\n".join(ln.rstrip() for ln in s.replace("\r\n", "\n").strip().splitlines())

    return norm(a) == norm(b)


def drop_other_kind(schema: str, name: str, type_: str) -> str:
    """T-SQL dropping `name` when it exists as another kind of object than `type_`."""
    oid = f"OBJECT_ID(N'[{schema}].[{name}]')"
    proc = f"IF EXISTS (SELECT 1 FROM sys.objects WHERE object_id = {oid} AND type = 'P') " \
           f"DROP PROCEDURE [{schema}].[{name}];"  # fmt: skip
    func = f"IF EXISTS (SELECT 1 FROM sys.objects WHERE object_id = {oid} " \
           f"AND type IN ('FN', 'IF', 'TF') AND type <> '{type_}') " \
           f"DROP FUNCTION [{schema}].[{name}];"  # fmt: skip
    return func if type_ == "P" else f"{proc}\n{func}"


# Tables copied with their rows (D-075).
CHAR_TYPES = ("nvarchar", "nchar", "varchar", "char")
BINARY_TYPES = ("varbinary", "binary")
TIME_TYPES = ("date", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "time")


def q(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


def table_info(schema: str, name: str) -> dict | None:
    """Columns (in order) and primary key of a table here; None if there is no such table."""
    cols = query(engine, f"""
        SELECT c.name, TYPE_NAME(c.system_type_id) AS type, c.max_length, c.precision, c.scale,
               c.is_nullable, c.is_identity, IDENT_SEED(QUOTENAME(s.name) + '.' + QUOTENAME(t.name))
               AS seed, IDENT_INCR(QUOTENAME(s.name) + '.' + QUOTENAME(t.name)) AS incr,
               cc.definition AS computed, dc.definition AS default_
        FROM sys.columns c
        JOIN sys.tables t ON t.object_id = c.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        LEFT JOIN sys.computed_columns cc
          ON cc.object_id = c.object_id AND cc.column_id = c.column_id
        LEFT JOIN sys.default_constraints dc
          ON dc.parent_object_id = c.object_id AND dc.parent_column_id = c.column_id
        WHERE s.name = N'{schema}' AND t.name = N'{name}'
        ORDER BY c.column_id""")  # fmt: skip
    if not cols:
        return None
    pk = query(engine, f"""
        SELECT i.name AS pk, i.type_desc, c.name AS col
        FROM sys.indexes i
        JOIN sys.tables t ON t.object_id = i.object_id
        JOIN sys.schemas s ON s.schema_id = t.schema_id
        JOIN sys.index_columns ic ON ic.object_id = i.object_id AND ic.index_id = i.index_id
        JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
        WHERE s.name = N'{schema}' AND t.name = N'{name}' AND i.is_primary_key = 1
        ORDER BY ic.key_ordinal""")  # fmt: skip
    for c in cols:
        c["sql_type"] = sql_type(c)
        # A computed or rowversion column cannot be inserted: its value is the server's.
        c["insertable"] = not c["computed"] and c["type"] != "timestamp"
        c["seed"] = None if c["seed"] is None else int(c["seed"])
        c["incr"] = None if c["incr"] is None else int(c["incr"])
        for k in ("max_length", "precision", "scale"):
            c[k] = int(c[k])
        c["is_nullable"], c["is_identity"] = bool(c["is_nullable"]), bool(c["is_identity"])
    return {
        "columns": cols,
        "primary_key": {"name": pk[0]["pk"], "clustered": pk[0]["type_desc"] == "CLUSTERED",
                        "columns": [r["col"] for r in pk]} if pk else None,
    }  # fmt: skip


def sql_type(c: dict) -> str:
    """The column's type as written in CREATE TABLE."""
    kind, n = c["type"], int(c["max_length"])
    if kind in CHAR_TYPES + BINARY_TYPES:
        size = "MAX" if n == -1 else n // 2 if kind in ("nvarchar", "nchar") else n
        return f"{kind}({size})"
    if kind in ("decimal", "numeric"):
        return f"{kind}({c['precision']}, {c['scale']})"
    if kind in ("datetime2", "datetimeoffset", "time"):
        return f"{kind}({c['scale']})"
    return kind


def create_table_sql(schema: str, name: str, info: dict) -> str:
    """CREATE TABLE for a table of the file: columns, identity, defaults, primary key."""
    defs = []
    for c in info["columns"]:
        if c["computed"]:
            defs.append(f"{q(c['name'])} AS {c['computed']}")
            continue
        ident = f" IDENTITY({c['seed']}, {c['incr']})" if c["is_identity"] else ""
        null = " NULL" if c["is_nullable"] else " NOT NULL"
        default = f" DEFAULT {c['default_']}" if c["default_"] else ""
        defs.append(f"{q(c['name'])} {c['sql_type']}{ident}{null}{default}")
    pk = info["primary_key"]
    if pk:  # only a table exported without rows may have none
        kind = "CLUSTERED" if pk["clustered"] else "NONCLUSTERED"
        defs.append(f"CONSTRAINT {q(pk['name'])} PRIMARY KEY {kind} "
                    f"({', '.join(q(c) for c in pk['columns'])})")  # fmt: skip
    return f"CREATE TABLE {q(schema)}.{q(name)} (\n    " + ",\n    ".join(defs) + "\n)"


def to_json(v: object) -> object:
    """A value read from SQL Server as JSON: decimals and ids as text, bytes as hex. Dates and
    times are read as ISO text already (`select_columns`)."""
    if isinstance(v, (Decimal, uuid.UUID)):
        return str(v)
    if isinstance(v, (bytes, bytearray)):
        return v.hex()
    return v


def select_columns(cols: list[dict]) -> str:
    """Columns to read for the file. Dates and times as ISO text (style 126): all their digits
    and the offset kept (Python's datetime stops at microseconds; pyodbc cannot read
    datetimeoffset). The import sends text back and the server converts it."""
    return ", ".join(
        f"CONVERT(NVARCHAR(40), {q(c['name'])}, 126) AS {q(c['name'])}"
        if c["type"] in TIME_TYPES else q(c["name"])
        for c in cols
    )  # fmt: skip


def insert_rows(name: str, t: dict, here: dict | None, apply: bool) -> int:
    """Rows of the file's table `t` whose primary key is not in the target: counted (dry run)
    or inserted (apply). Staged in a temporary table so the server compares the keys (types and
    collation of the target); rows already there are never changed."""
    target_cols = {c["name"]: c for c in (here or t)["columns"]}
    cols = [c for c in t["columns"] if c["insertable"] and c["name"] in target_cols
            and target_cols[c["name"]]["insertable"]]  # fmt: skip
    pk = (here or t)["primary_key"]["columns"]
    names = [c["name"] for c in t["columns"]]
    idx = [names.index(c["name"]) for c in cols]
    # The target's types: the staged values convert as they will on insert. Binary is staged
    # as hex text (a NULL parameter is sent as text, which binary columns refuse).
    kinds = [target_cols[c["name"]] for c in cols]
    stage = ", ".join(
        f"{q(k['name'])} "
        + ("NVARCHAR(MAX)" if k["type"] in BINARY_TYPES else k["sql_type"])
        + (" COLLATE DATABASE_DEFAULT" if k["type"] in CHAR_TYPES else "") + " NULL"
        for k in kinds
    )  # fmt: skip

    def value(k: dict) -> str:
        if k["type"] in BINARY_TYPES:
            return f"CONVERT({k['sql_type']}, S.{q(k['name'])}, 2)"
        return f"S.{q(k['name'])}"

    collist = ", ".join(q(c["name"]) for c in cols)
    values = ", ".join(value(k) for k in kinds)
    tgt = f"{q(SCHEMA)}.{q(name)}"
    new = (f"FROM #rows S WHERE NOT EXISTS (SELECT 1 FROM {tgt} T WHERE "
           + " AND ".join(f"T.{q(k['name'])} = {value(k)}" for k in kinds if k["name"] in pk)
           + ")")  # fmt: skip
    if apply:
        ident = any(k["is_identity"] for k in kinds)
        on, off = (f"SET IDENTITY_INSERT {tgt} ON;", f"SET IDENTITY_INSERT {tgt} OFF;") if ident \
            else ("", "")  # fmt: skip
        sql = (f"DECLARE @n INT; {on} INSERT INTO {tgt} ({collist}) SELECT {values} {new}; "
               f"SET @n = @@ROWCOUNT; {off} SELECT @n AS n;")  # fmt: skip
    else:
        sql = f"SELECT COUNT(*) AS n {new};"
    raw = engine.raw_connection()
    try:
        cur = raw.cursor()
        cur.execute("SET NOCOUNT ON;")
        # The ODBC driver may hand back a pooled session that still has the last table's #rows.
        cur.execute(f"DROP TABLE IF EXISTS #rows; CREATE TABLE #rows ({stage})")
        rows = [[r[i] for i in idx] for r in t["rows"]]
        for i in range(0, len(rows), 1_000):
            cur.executemany(f"INSERT INTO #rows ({collist}) VALUES "
                            f"({', '.join('?' * len(cols))})", rows[i : i + 1_000])  # fmt: skip
        cur.execute(sql)
        while cur.description is None and cur.nextset():
            pass
        n = int(cur.fetchone()[0] or 0)
        while cur.nextset():
            pass
        if apply:
            raw.commit()
        else:
            raw.rollback()
        cur.execute("DROP TABLE IF EXISTS #rows")
        raw.commit()
        return n
    finally:
        raw.close()


# COMMAND ----------

# MAGIC %md ## Export (in the source environment)

# COMMAND ----------

if W["action"] == "export":
    source = modules(SCHEMA)
    if ONLY:
        unknown = ONLY - source.keys()
        assert not unknown, f"not in {SCHEMA}: {sorted(unknown)}"
        source = {n: r for n, r in source.items() if n in ONLY}
    hidden = sorted(n for n, r in source.items() if r["definition"] is None)
    assert not hidden, f"definitions not readable (encrypted or no VIEW DEFINITION): {hidden}"

    # Dependency order: a function before the functions and procedures that call it.
    ids = {r["object_id"]: n for n, r in source.items()}
    graph: dict[str, set[str]] = {n: set() for n in source}
    for d in query(
        engine,
        f"""
            SELECT d.referencing_id, d.referenced_id
            FROM sys.sql_expression_dependencies d
            JOIN sys.objects o ON o.object_id = d.referencing_id
            WHERE SCHEMA_NAME(o.schema_id) = N'{SCHEMA}' AND d.referenced_id IS NOT NULL""",
    ):
        a, b = ids.get(d["referencing_id"]), ids.get(d["referenced_id"])
        if a and b and a != b:
            graph[a].add(b)
    try:
        order = list(TopologicalSorter(graph).static_order())
    except CycleError:  # the import retries what fails on a later object
        order = sorted(source, key=lambda n: (source[n]["type"] == "P", n))

    # Tables and views each object reads (checked against the target by the import).
    reads: dict[str, set[tuple[str, str]]] = {n: set() for n in source}
    for r in query(
        engine,
        f"""
            SELECT DISTINCT o.name AS obj, ISNULL(d.referenced_schema_name, N'{SCHEMA}') AS sch,
                   d.referenced_entity_name AS tbl
            FROM sys.sql_expression_dependencies d
            JOIN sys.objects o ON o.object_id = d.referencing_id
            LEFT JOIN sys.objects r ON r.object_id = d.referenced_id
            WHERE SCHEMA_NAME(o.schema_id) = N'{SCHEMA}' AND d.referenced_database_name IS NULL
              AND (r.type IN ('U', 'V') OR r.object_id IS NULL)""",
    ):
        if r["obj"] in reads and r["tbl"] not in source:
            reads[r["obj"]].add((r["sch"], r["tbl"]))

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")  # noqa: UP017 (DBR 3.10)
    objects = [{"name": n, "type": source[n]["type"], "definition": source[n]["definition"],
                "reads": sorted(reads[n])} for n in order]  # fmt: skip
    # Tables with their rows (D-075): small BFF tables. The pipeline's tables are copied from
    # its SQLite file by copy_sqlite_to_azure instead.
    tables_out = []
    for name in TABLES + EMPTY_TABLES:
        info = table_info(SCHEMA, name)
        assert info, f"no table {SCHEMA}.{name}"
        if name in EMPTY_TABLES:  # the structure only (D-086)
            tables_out.append({**info, "name": name, "rows": []})
            print(f"table {name}: {len(info['columns'])} columns, without rows")
            continue
        assert info["primary_key"], f"{name} has no primary key (the import matches rows by it)"
        rows = query(engine, f"SELECT {select_columns(info['columns'])} "
                             f"FROM {q(SCHEMA)}.{q(name)}")  # fmt: skip
        assert len(rows) <= 200_000, f"{name}: {len(rows)} rows, too many for a file copy"
        tables_out.append({**info, "name": name,
                           "rows": [[to_json(r[c["name"]]) for c in info["columns"]]
                                    for r in rows]})  # fmt: skip
        print(f"table {name}: {len(info['columns'])} columns, {len(rows)} rows")
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps({
        "exported_at": stamp, "env": W["env"], "server": S["server"],
        "database": S["database"], "schema": SCHEMA, "objects": objects, "tables": tables_out,
    }, indent=1), encoding="utf-8")  # fmt: skip
    # The same as a script for run_sql_script or SSMS (no comparison, no table check).
    batches = [
        f"-- {len(objects)} functions and procedures of [{SCHEMA}], exported from "
        f"{W['env']} ({S['database']}) {stamp} by sync_sql_objects. Run in this order."
    ]
    for o in objects:
        batches.append(drop_other_kind(SCHEMA, o["name"], o["type"]))
        batches.append(create_or_alter(o["definition"], SCHEMA, SCHEMA).strip())
    FILE.with_suffix(".sql").write_text("\nGO\n\n".join(batches) + "\nGO\n", encoding="utf-8")
    counts = {KINDS[t]: sum(1 for o in objects if o["type"] == t) for t in KINDS}
    print(f"exported {len(objects)} objects {counts} and {len(tables_out)} tables")
    print(f"  {FILE}\n  {FILE.with_suffix('.sql')} (functions and procedures only)")
    if str(FILE).startswith("/dbfs/FileStore/"):
        print(f"download: https://<this workspace>/files/{FILE.relative_to('/dbfs/FileStore')}")
    dbutils.notebook.exit(f"exported {len(objects)} objects and {len(tables_out)} tables to {FILE}")

# COMMAND ----------

# MAGIC %md ## Import (in the target environment): plan

# COMMAND ----------

assert W["action"] == "import"
assert FILE.is_file(), f"{FILE} not found: upload the exported file there"
exp = json.loads(FILE.read_text(encoding="utf-8"))
SRC = exp["schema"]
order = [o["name"] for o in exp["objects"] if not ONLY or o["name"] in ONLY]
source = {o["name"]: o for o in exp["objects"] if o["name"] in order}
if ONLY:
    unknown = ONLY - source.keys()
    assert not unknown, f"not in the file: {sorted(unknown)}"
assert (exp["server"], exp["database"], SRC) != (S["server"], S["database"], SCHEMA), (
    "the file was exported from this database and schema"
)
file_tables = exp.get("tables", [])
print(f"file: {len(source)} objects and {len(file_tables)} tables of {SRC} from {exp['env']} "
      f"({exp['database']}), exported {exp['exported_at']}")  # fmt: skip

# Tables of the file: created when missing, then the rows whose key is not here inserted.
table_plan: list[tuple[dict, dict | None]] = []  # (table in the file, the same table here)
table_problems: list[str] = []
for t in file_tables:
    here = table_info(SCHEMA, t["name"])
    table_plan.append((t, here))
    pk_cols = t["primary_key"]["columns"] if t["primary_key"] else "none"
    if here is None:
        print(f"table  {t['name']}: create ({len(t['columns'])} columns, primary key "
              f"{pk_cols}), insert {len(t['rows'])} rows")  # fmt: skip
        continue
    if not t["rows"]:
        print(f"table  {t['name']}: exists; no rows in the file, left as it is")
        continue
    filecols = {c["name"] for c in t["columns"] if c["insertable"]}
    dropped = sorted(filecols - {c["name"] for c in here["columns"]})
    if dropped:
        print(f"table  {t['name']}: columns of the file not here, left out: {dropped}")
    required = [
        c["name"]
        for c in here["columns"]
        if c["insertable"]
        and not c["is_nullable"]
        and not c["is_identity"]
        and not c["default_"]
        and c["name"] not in filecols
    ]
    if required:
        table_problems.append(f"{t['name']}: required columns here not in the file: {required}")
    elif not here["primary_key"] or not set(here["primary_key"]["columns"]) <= filecols:
        table_problems.append(f"{t['name']}: no primary key here made of the file's columns")
    else:
        n = insert_rows(t["name"], t, here, apply=False)
        print(f"table  {t['name']}: exists; {len(t['rows'])} rows in the file, {n} to insert "
              f"(the others are here already and left as they are)")  # fmt: skip
for p in table_problems:
    print("PROBLEM", p)
assert not table_problems, "fix the tables above first"

target = modules(SCHEMA)

plan: dict[str, str] = {}  # name -> new | changed | kind changed | same
for name, o in source.items():
    t = target.get(name)
    if t is None:
        plan[name] = "new"
    elif t["type"] != o["type"]:
        plan[name] = "kind changed"
    elif same(create_or_alter(o["definition"], SRC, SCHEMA), t["definition"] or ""):
        plan[name] = "same"
    else:
        plan[name] = "changed"

tables = query(engine, "SELECT SCHEMA_NAME(schema_id) AS sch, name FROM sys.objects "
                       "WHERE type IN ('U', 'V')")  # fmt: skip
have = {(r["sch"].lower(), r["name"].lower()) for r in tables}
have |= {(SCHEMA.lower(), t["name"].lower()) for t in file_tables}  # created by the apply
missing: dict[str, set[str]] = {}
for name, o in source.items():
    for sch, tbl in o["reads"]:
        sch = SCHEMA if sch.lower() == SRC.lower() else sch
        if (sch.lower(), tbl.lower()) not in have:
            missing.setdefault(f"{sch}.{tbl}", set()).add(name)

counts = {k: sum(1 for v in plan.values() if v == k)
          for k in ("new", "changed", "kind changed", "same")}  # fmt: skip
print("plan:", counts)
for name in order:
    if plan[name] != "same":
        kind = KINDS[source[name]["type"]]
        extra = f" (here: {KINDS[target[name]['type']]})" if plan[name] == "kind changed" else ""
        print(f"  {plan[name]:12} {name}  [{kind}]{extra}")
only_here = sorted(set(target) - set(source)) if not ONLY else []
if only_here:
    print(f"only here, not in the file (left as they are): {', '.join(only_here)}")
if missing:
    print("PROBLEM: tables missing here and not in the file (create them first, or export "
          "them with `tables` or `empty_tables`; these objects would fail):")  # fmt: skip
    for t, objs in sorted(missing.items()):
        print(f"  {t}: read by {', '.join(sorted(objs))}")
if W["mode"] != "apply":
    dbutils.notebook.exit(f"dry run: {counts}; set mode = apply")

# COMMAND ----------

# MAGIC %md ## Import: apply

# COMMAND ----------

# Tables first: the functions and procedures may read them.
for t, here in table_plan:
    if here is None:
        run_sql(engine, create_table_sql(SCHEMA, t["name"], t))
        print(f"table  {t['name']}: created")
    if not t["rows"]:
        continue
    n = insert_rows(t["name"], t, table_info(SCHEMA, t["name"]), apply=True)
    print(f"table  {t['name']}: {n} rows inserted")

todo = [n for n in order if plan[n] != "same"]
failed: dict[str, str] = {}
for _attempt in (1, 2):  # a second pass for anything that failed on an object created later
    failed = {}
    for name in todo:
        o = source[name]
        try:
            if plan[name] == "kind changed":
                run_sql(engine, drop_other_kind(SCHEMA, name, o["type"]))
            run_sql(engine, create_or_alter(o["definition"], SRC, SCHEMA))
            print(f"{plan[name]:12} {name}: done")
        except Exception as e:  # report every object, then stop below
            failed[name] = str(e).splitlines()[0][:400]
    todo = list(failed)
    if not todo:
        break

after = modules(SCHEMA)
still = [n for n, o in source.items() if n not in after or not same(
    create_or_alter(o["definition"], SRC, SCHEMA), after[n]["definition"] or "")]  # fmt: skip
for name, err in failed.items():
    print(f"FAILED {name}: {err}")
print(f"done: {len(source) - len(still)} of {len(source)} objects equal to the file")
assert not still, f"not equal to the file: {still}"
